"""Layer 1 Indexer: delegates repo skeleton compression to repomix, queries git ls-files, and performs 0-token file localization."""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from engine.models import EngineConfig, normalize_posix_path

FILE_TAG_RE = re.compile(r'<file path="([^"]+)">\r?\n?([\s\S]*?)\r?\n?</file>')
DIR_STRUCT_RE = re.compile(r"<directory_structure>([\s\S]*?)</directory_structure>")
_NO_SENTENCE_END = r"(?:[^\n;:)]|\.(?=\S))"
_GUARD_CLAUSE_RE = re.compile(
    rf"(?:"
    rf"\bwithout\s+breaking\b"
    rf"|\bkeep\b{_NO_SENTENCE_END}*?\b(?:passing|untouched|intact|as-is)\b"
    rf"|\b(?:do\s+not|don't|never)\s+(?:touch|modify|change|break|edit)\b"
    rf"|(?:không\s+(?:làm\s+hỏng|sửa|đụng)|giữ\s+nguyên)\b"
    rf"){_NO_SENTENCE_END}*",
    re.IGNORECASE,
)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_CAMEL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_PY_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(\.*[A-Za-z0-9_.]*)(?:\s+import\s+([^\n#]+))?", re.MULTILINE)
_JS_IMPORT_RE = re.compile(
    r"""(?:from\s+|require\s*\(\s*|import\s*\(\s*)["'](\.[^"']+)["']"""
)
_STUB_MARKER_RE = re.compile(r"\bNotImplementedError\b|\bTODO\b|\bFIXME\b")
_PRD_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "must", "should", "all",
    "any", "not", "are", "can", "will", "has", "have", "into", "when", "each",
    "only", "then", "else", "elif", "return", "returns", "raise", "raises",
    "str", "int", "float", "bool", "dict", "list", "tuple", "set", "none",
    "true", "false", "null", "undefined", "void", "object", "string", "number",
    "boolean", "class", "def", "import", "export", "const", "let", "var",
    "function", "async", "await", "try", "except", "pass", "keep", "passing",
    "without", "breaking", "implement", "upgrade", "extend", "existing",
    "support", "complete", "Across", "across", "using", "via", "per",
}


@dataclass
class LocatedFiles:
    """0-token located target, readonly neighborhood, and protected test files for a PRD."""

    target_files: list[str] = field(default_factory=list)
    readonly_files: list[str] = field(default_factory=list)
    protected_tests: list[str] = field(default_factory=list)
    confident: bool = False


def _is_test_path(rel_path: str) -> bool:
    norm = normalize_posix_path(rel_path).lower()
    parts = norm.split("/")
    name = parts[-1]
    if any(p in {"tests", "test", "__tests__"} for p in parts[:-1]):
        return True
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.go")
        or ".test." in name
        or ".spec." in name
    )


def _split_guard_clauses(prd_text: str) -> tuple[str, str]:
    """Return (active_prd_text, guard_clause_text) by isolating do-not-break clauses."""
    spans = [m.span() for m in _GUARD_CLAUSE_RE.finditer(prd_text)]
    if not spans:
        return prd_text, ""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    active_parts: list[str] = []
    guard_parts: list[str] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            active_parts.append(prd_text[cursor:start])
        guard_parts.append(prd_text[start:end])
        cursor = end
    if cursor < len(prd_text):
        active_parts.append(prd_text[cursor:])
    return "\n".join(active_parts), "\n".join(guard_parts)


def _contains_path_token(text_lower: str, token_lower: str) -> bool:
    if not token_lower or not text_lower:
        return False
    pat = rf"(?<![a-z0-9_./-]){re.escape(token_lower)}(?![a-z0-9_.-])"
    return re.search(pat, text_lower) is not None


def _extract_prd_identifiers(active_prd_text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        if len(tok) < 3 or tok.lower() in _PRD_STOPWORDS or tok in seen:
            return
        seen.add(tok)
        tokens.append(tok)

    for block in _BACKTICK_RE.findall(active_prd_text):
        for tok in _IDENT_RE.findall(block):
            _add(tok)
    for tok in _CAMEL_RE.findall(active_prd_text):
        _add(tok)
    for tok in _SNAKE_RE.findall(active_prd_text):
        _add(tok)
    return tokens


def _resolve_local_imports(rel_path: str, content: str, norm_real: set[str]) -> list[str]:
    """Resolve 1-hop Python & JS/TS relative/local imports to tracked repo files."""
    resolved: list[str] = []
    parent_dir = posixpath.dirname(rel_path)

    for match in _PY_IMPORT_RE.finditer(content):
        mod_spec = match.group(1) or ""
        imported_names = match.group(2) or ""
        dots = len(mod_spec) - len(mod_spec.lstrip("."))
        mod_tail = mod_spec[dots:].replace(".", "/")
        base_dir = parent_dir
        if dots > 1:
            for _ in range(dots - 1):
                base_dir = posixpath.dirname(base_dir)
        candidates: list[str] = []
        if dots > 0:
            prefix = posixpath.normpath(posixpath.join(base_dir, mod_tail)) if mod_tail else base_dir
            prefix = "" if prefix == "." else prefix.lstrip("./")
            if prefix:
                candidates.extend([f"{prefix}.py", f"{prefix}/__init__.py"])
            for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", imported_names):
                sub = f"{prefix}/{name}" if prefix else name
                candidates.extend([f"{sub}.py", f"{sub}/__init__.py"])
        elif mod_tail:
            candidates.extend([f"{mod_tail}.py", f"{mod_tail}/__init__.py", f"src/{mod_tail}.py", f"src/{mod_tail}/__init__.py"])
        for cand in candidates:
            norm_cand = normalize_posix_path(cand)
            if norm_cand in norm_real and norm_cand != rel_path and norm_cand not in resolved:
                resolved.append(norm_cand)

    for match in _JS_IMPORT_RE.finditer(content):
        spec = match.group(1)
        joined = posixpath.normpath(posixpath.join(parent_dir, spec))
        if joined.startswith("../"):
            continue
        exts = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts", "/index.ts", "/index.tsx", "/index.js", "/index.jsx")
        for ext in exts:
            cand = normalize_posix_path(f"{joined}{ext}")
            if cand in norm_real and cand != rel_path and cand not in resolved:
                resolved.append(cand)
                break

    return resolved


def locate_relevant_files(
    prd_text: str,
    real_files: set[str],
    repo_path: Path | None = None,
    *,
    max_targets: int = 6,
    max_readonly: int = 8,
) -> LocatedFiles:
    """0-token locator that finds target files, 1-hop readonly imports, and protected tests."""
    norm_real = sorted({normalize_posix_path(f) for f in real_files if f and f.strip()})
    norm_set = set(norm_real)
    test_files = [p for p in norm_real if _is_test_path(p)]
    non_test_files = [p for p in norm_real if not _is_test_path(p)]

    active_prd, guard_text = _split_guard_clauses(prd_text)
    active_lower = active_prd.lower()
    guard_lower = guard_text.lower()
    identifiers = _extract_prd_identifiers(active_prd)

    base_counts: dict[str, int] = {}
    for rel in norm_real:
        base = rel.rsplit("/", 1)[-1].lower()
        base_counts[base] = base_counts.get(base, 0) + 1

    guarded_paths: set[str] = set()
    for rel in norm_real:
        rel_lower = rel.lower()
        base_lower = rel_lower.rsplit("/", 1)[-1]
        in_guard = _contains_path_token(guard_lower, rel_lower) or (
            base_lower != "__init__.py" and _contains_path_token(guard_lower, base_lower)
        )
        in_active = _contains_path_token(active_lower, rel_lower) or (
            base_lower != "__init__.py" and len(base_lower) >= 4 and _contains_path_token(active_lower, base_lower)
        )
        if in_guard and not in_active:
            guarded_paths.add(rel)

    scored: list[tuple[int, str]] = []
    file_texts: dict[str, str] = {}
    for rel in non_test_files:
        if rel in guarded_paths:
            continue
        rel_lower = rel.lower()
        base_lower = rel_lower.rsplit("/", 1)[-1]
        if base_lower == "__init__.py" and not _contains_path_token(active_lower, rel_lower):
            continue

        score = 0
        if _contains_path_token(active_lower, rel_lower):
            score += 100
        elif base_lower != "__init__.py" and _contains_path_token(active_lower, base_lower):
            # Unique basenames score full points; ambiguous basenames still get a strong boost.
            score += 60 if base_counts.get(base_lower, 1) == 1 else 45

        if repo_path is not None and identifiers:
            full_path = repo_path / rel
            if full_path.is_file() and full_path.stat().st_size <= 262_144:
                text = full_path.read_text(encoding="utf-8", errors="replace")
                file_texts[rel] = text
                matched_ids = sum(
                    1 for ident in identifiers if re.search(rf"\b{re.escape(ident)}\b", text)
                )
                score += min(40, matched_ids * 8)
                if matched_ids >= 1 and _STUB_MARKER_RE.search(text):
                    score += 20

        if score >= 20:
            scored.append((score, rel))

    scored.sort(key=lambda item: (-item[0], item[1]))
    top = scored[:max_targets]
    target_files = [rel for _sc, rel in top]
    top_score = top[0][0] if top else 0
    confident = bool(target_files) and (top_score >= 60 or top_score >= 36)

    readonly_files: list[str] = []
    seen_ro = set(target_files) | set(test_files)
    if repo_path is not None:
        for rel in target_files:
            content = file_texts.get(rel)
            if content is None:
                full_path = repo_path / rel
                if full_path.is_file() and full_path.stat().st_size <= 262_144:
                    content = full_path.read_text(encoding="utf-8", errors="replace")
            if not content:
                continue
            for imported in _resolve_local_imports(rel, content, norm_set):
                if imported not in seen_ro:
                    seen_ro.add(imported)
                    readonly_files.append(imported)
                    if len(readonly_files) >= max_readonly:
                        break
            if len(readonly_files) >= max_readonly:
                break

    if len(readonly_files) < max_readonly:
        for rel in sorted(guarded_paths):
            if rel not in seen_ro and not _is_test_path(rel):
                seen_ro.add(rel)
                readonly_files.append(rel)
                if len(readonly_files) >= max_readonly:
                    break

    protected_tests: list[str] = []
    for rel in test_files:
        rel_lower = rel.lower()
        base_lower = rel_lower.rsplit("/", 1)[-1]
        explicitly_requested = (
            rel not in guarded_paths
            and (
                _contains_path_token(active_lower, rel_lower)
                or _contains_path_token(active_lower, base_lower)
            )
        )
        if not explicitly_requested:
            protected_tests.append(rel)

    return LocatedFiles(
        target_files=target_files,
        readonly_files=readonly_files,
        protected_tests=protected_tests,
        confident=confident,
    )


class IndexerError(RuntimeError):
    """Raised when repository indexing or repomix invocation fails."""


class RepoIndexer:
    """Generates cached Tree-sitter repo skeletons via `repomix --compress` (0 LLM tokens)."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.repo_path = config.target_repo_path
        self.auto_dir = config.auto_engine_dir
        self.repo_map_path = self.auto_dir / "repo_map.xml"
        self.hash_path = self.auto_dir / "repo_map.hash"

    def _run_git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise IndexerError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def compute_repo_state_hash(self) -> str:
        """Hash HEAD + porcelain status (excluding .auto-engine) for smart cache invalidation."""
        head = self._run_git("rev-parse", "HEAD").strip()
        status_lines = [
            line
            for line in self._run_git("status", "--porcelain").splitlines()
            if ".auto-engine" not in line
        ]
        payload = f"{head}\n" + "\n".join(status_lines)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def build_repo_map(self, force: bool = False) -> Path:
        """Run `npx repomix --compress` if repo state changed; return path to repo_map.xml."""
        self.auto_dir.mkdir(parents=True, exist_ok=True)
        state_hash = self.compute_repo_state_hash()

        if (
            not force
            and self.repo_map_path.is_file()
            and self.hash_path.is_file()
            and self.hash_path.read_text(encoding="utf-8").strip() == state_hash
        ):
            return self.repo_map_path

        npx_bin = shutil.which("npx.cmd") or shutil.which("npx")
        if not npx_bin:
            raise IndexerError("`npx` not found in PATH; Node.js is required to run repomix.")

        ignore_csv = ",".join([".auto-engine/**", *self.config.tools.repomix_ignore])
        cmd = [
            npx_bin,
            "-y",
            "repomix",
            "--compress",
            "--style",
            "xml",
            "--output",
            str(self.repo_map_path),
            "--ignore",
            ignore_csv,
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=(os.name == "nt" and npx_bin.lower().endswith(".ps1")),
        )
        if proc.returncode != 0 or not self.repo_map_path.is_file():
            raise IndexerError(
                f"repomix failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )

        self.hash_path.write_text(state_hash, encoding="utf-8")
        return self.repo_map_path


def list_real_files(repo_path: Path) -> set[str]:
    """Return normalized POSIX relative file paths tracked in git ls-files."""
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(repo_path.resolve()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise IndexerError(f"git ls-files failed: {proc.stderr.strip()}")
    return {
        normalize_posix_path(entry)
        for entry in proc.stdout.split("\0")
        if entry.strip()
    }


def extract_file_skeletons(repo_map_path: Path, target_paths: list[str]) -> dict[str, str]:
    """Parse `<file path='...'>` blocks from repomix output (supports plain-text preamble & unescaped HTML/XML)."""
    if not repo_map_path.is_file() or not target_paths:
        return {}

    wanted = {normalize_posix_path(p) for p in target_paths}
    text = repo_map_path.read_text(encoding="utf-8", errors="replace")
    result: dict[str, str] = {}

    for match in FILE_TAG_RE.finditer(text):
        norm = normalize_posix_path(match.group(1))
        if norm in wanted:
            content = match.group(2).strip()
            if content:
                result[norm] = content
    return result


def compact_repo_map_for_planner(
    repo_map_text: str,
    max_chars: int = 50000,
    priority_files: set[str] | None = None,
) -> str:
    """Keep directory structure + code skeletons while prioritizing relevant files and skipping large docs."""
    sections: list[str] = []
    dir_match = DIR_STRUCT_RE.search(repo_map_text)
    if dir_match:
        sections.append(f"<directory_structure>{dir_match.group(1)}</directory_structure>")

    norm_priority = {normalize_posix_path(p) for p in priority_files} if priority_files else set()
    priority_blocks: list[str] = []
    other_blocks: list[str] = []

    for match in FILE_TAG_RE.finditer(repo_map_text):
        rel = normalize_posix_path(match.group(1))
        body = match.group(2).strip()
        if rel.lower().endswith(".md") and len(body) > 4000:
            block = f'<file path="{rel}">\n(Markdown doc omitted: {len(body)} chars)\n</file>'
        else:
            block = f'<file path="{rel}">\n{body}\n</file>'
        if rel in norm_priority:
            priority_blocks.append(block)
        else:
            other_blocks.append(block)

    used = sum(len(s) for s in sections)
    for block in priority_blocks + other_blocks:
        if used + len(block) > max_chars:
            sections.append("... [remaining files omitted to stay within planner budget]")
            break
        sections.append(block)
        used += len(block)

    if not sections:
        return repo_map_text[:max_chars]
    return "\n\n".join(sections)
