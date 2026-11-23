"""Layer 1 Indexer: delegates repo skeleton compression to repomix, queries git ls-files, and performs 0-token file localization."""

from __future__ import annotations

import ast
import hashlib
import math
import os
import posixpath
import re
import shutil
import subprocess
from collections import defaultdict
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
_DOTTED_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
_CAMEL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+\b")
_PASCAL_WORD_RE = re.compile(r"\b[A-Z][a-z]{2,}[A-Za-z0-9_]*\b")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9_]*\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_COMMON_PROSE_CAPS = {
    "The", "This", "That", "These", "Those", "When", "Where", "What", "Which",
    "While", "With", "Without", "From", "Into", "Over", "Under", "After", "Before",
    "Ensure", "Ensures", "Update", "Updates", "Upgrade", "Extend", "Fix", "Fixes",
    "Fixed", "Allow", "Allows", "Add", "Adds", "Added", "Remove", "Removes",
    "Move", "Moves", "Improve", "Improves", "Refactor", "List", "Always", "Stop",
    "Resolve", "Forwards", "Close", "Land", "Open", "Various", "Some", "Other",
    "Note", "Error", "Warning", "Usage", "Options", "Description", "Checklist",
    "Type", "Tests", "Bug", "New", "Parameter", "Hint", "Nothing", "Help", "Also",
    "Simple", "Mixed", "True", "False", "None", "Release", "Revert", "Drop",
}
_PY_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(\.*[A-Za-z0-9_.]*)(?:\s+import\s+([^\n#]+))?", re.MULTILINE)
_JS_IMPORT_RE = re.compile(
    r"""(?:from\s+|require\s*\(\s*|import\s*\(\s*)["'](\.[^"']+)["']"""
)
_STUB_MARKER_RE = re.compile(r"\bNotImplementedError\b|\bTODO\b|\bFIXME\b")
_DECL_LINE_RE = re.compile(
    r"^\s*(?:"
    r"export\s+(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|enum|const)\b"
    r"|(?:async\s+)?(?:function|def|class)\b"
    r"|(?:interface|type|enum|struct|trait|impl|module)\s+[A-Za-z_]"
    r"|func\s+(?:\([^)]*\)\s*)?[A-Za-z_]"
    r"|(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|mod)\s+[A-Za-z_]"
    r"|(?:public|private|protected|internal)\s+(?:static\s+)?(?:async\s+)?[A-Za-z_<>"
    r"\[\]]+\s+[A-Za-z_]"
    r"|const\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*(?:async\s+)?(?:\(|function\b)"
    r")"
)
_ROOT_MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "tsconfig.json", "go.mod", "cargo.toml", "pom.xml", "build.gradle",
    "readme.md", "makefile", "dockerfile", "docker-compose.yml",
}
_DOC_META_DIRS = {"docs", "doc", ".github", "examples", "benchmarks", "changes"}
_DOC_META_EXTS = {".md", ".rst", ".txt", ".adoc", ".lock", ".png", ".jpg", ".svg", ".gif"}
_DOC_META_STEMS = {"changes", "changelog", "history", "contributing", "authors", "license", "readme"}
_LOWER_WORD_RE = re.compile(r"\b[a-z][a-z0-9]{3,}\b")
_PRD_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "must", "should", "all",
    "any", "not", "are", "can", "will", "has", "have", "into", "when", "each",
    "only", "then", "else", "elif", "return", "returns", "raise", "raises",
    "str", "int", "float", "bool", "dict", "list", "tuple", "set", "none",
    "true", "false", "null", "undefined", "void", "object", "string", "number",
    "boolean", "class", "def", "import", "export", "const", "let", "var",
    "function", "async", "await", "try", "except", "pass", "keep", "passing",
    "without", "breaking", "implement", "upgrade", "extend", "existing",
    "support", "complete", "across", "using", "via", "per",
    "also", "update", "logic", "issue", "issues", "fix", "fixes", "fixing",
    "fixed", "error", "hint", "special", "case", "cases", "output", "screen",
    "before", "after", "value", "nothing", "missing", "invalid", "parameter",
    "rendering", "ensure", "ensuring", "proper", "multiple", "line", "lines",
    "other", "followed", "within", "sits", "move", "there", "enough", "entire",
    "current", "appropriate", "location", "locations", "retain", "retaining",
    "same", "both", "more", "less", "when", "some", "such", "been", "what",
    "which", "where", "about", "make", "makes", "made", "does", "done",
    "show", "shows", "clean", "cleanly", "handling", "handle", "work", "works",
}


@dataclass
class LocatedFiles:
    """0-token located target, readonly neighborhood, and protected test files for a PRD."""

    target_files: list[str] = field(default_factory=list)
    readonly_files: list[str] = field(default_factory=list)
    protected_tests: list[str] = field(default_factory=list)
    confident: bool = False
    candidate_files: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)


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


def _is_doc_or_meta_path(rel_path: str) -> bool:
    norm = normalize_posix_path(rel_path).lower()
    parts = norm.split("/")
    name = parts[-1]
    stem = name.rsplit(".", 1)[0]
    ext = ("." + name.rsplit(".", 1)[1]) if "." in name else ""
    if any(p in _DOC_META_DIRS for p in parts[:-1]):
        return True
    return ext in _DOC_META_EXTS or stem in _DOC_META_STEMS


def _clip_line(line: str, max_chars: int = 200) -> str:
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + " ... [clipped]"


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
    for cls_part, member_part in _DOTTED_SYMBOL_RE.findall(active_prd_text):
        _add(cls_part)
        _add(member_part)
    for tok in _CAMEL_RE.findall(active_prd_text):
        _add(tok)
    for tok in _SNAKE_RE.findall(active_prd_text):
        _add(tok)
    for tok in _PASCAL_WORD_RE.findall(active_prd_text):
        if tok not in _COMMON_PROSE_CAPS:
            _add(tok)
    for tok in _ACRONYM_RE.findall(active_prd_text):
        if len(tok) >= 3 and tok not in {"TODO", "FIXME", "NOTE"}:
            _add(tok)

    for word in _LOWER_WORD_RE.findall(active_prd_text):
        if word in _PRD_STOPWORDS:
            continue
        _add(word)
        if word.endswith("ing") and len(word) >= 6:
            base = word[:-3]
            _add(base)
            if len(base) >= 2 and base[-1] == base[-2]:
                _add(base[:-1])
        elif word.endswith("ed") and len(word) >= 5:
            base = word[:-2]
            _add(base)
            if len(base) >= 2 and base[-1] == base[-2]:
                _add(base[:-1])
        elif word.endswith("es") and len(word) >= 5:
            _add(word[:-2])
            _add(word[:-1])
        elif word.endswith("s") and len(word) >= 5:
            _add(word[:-1])
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


def _git_grep_candidate_files(
    repo_path: Path,
    identifiers: list[str],
    max_files: int = 120,
) -> set[str] | None:
    """Fast native C `git grep` pre-filter so content scoring never reads thousands of unrelated files."""
    ranked_ids = sorted(
        [ident for ident in identifiers if len(ident) >= 3],
        key=lambda s: (-int("_" in s or any(c.isupper() for c in s[1:])), -len(s), s),
    )[:20]
    if not ranked_ids:
        return set()
    cmd = ["git", "grep", "-z", "-I", "-l", "-F"]
    for ident in ranked_ids:
        cmd.extend(["-e", ident])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_path.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None
    hits: set[str] = set()
    for entry in proc.stdout.split("\0"):
        if entry.strip():
            hits.add(normalize_posix_path(entry))
            if len(hits) >= max_files:
                break
    return hits


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
    ident_lower_set = {i.lower() for i in identifiers}
    dotted_owners = {
        m.group(1).lower()
        for m in _DOTTED_SYMBOL_RE.finditer(active_prd)
        if len(m.group(1)) >= 3 and m.group(1) not in _COMMON_PROSE_CAPS
    }
    pascal_classes = {
        w.lower()
        for w in _PASCAL_WORD_RE.findall(active_prd)
        if w not in _COMMON_PROSE_CAPS and len(w) >= 4
    }
    ranked_ident_subset = identifiers[:60]
    ident_regex = (
        re.compile(
            r"\b(?:" + "|".join(re.escape(i) for i in sorted(ranked_ident_subset, key=len, reverse=True)) + r")\b"
        )
        if ranked_ident_subset
        else None
    )

    base_counts: dict[str, int] = {}
    for rel in norm_real:
        base = rel.rsplit("/", 1)[-1].lower()
        base_counts[base] = base_counts.get(base, 0) + 1

    guarded_paths: set[str] = set()
    for rel in norm_real:
        rel_lower = rel.lower()
        base_lower = rel_lower.rsplit("/", 1)[-1]
        if base_lower not in guard_lower:
            continue
        in_guard = _contains_path_token(guard_lower, rel_lower) or (
            base_lower != "__init__.py" and _contains_path_token(guard_lower, base_lower)
        )
        if not in_guard:
            continue
        in_active = (
            base_lower in active_lower
            and (
                _contains_path_token(active_lower, rel_lower)
                or (
                    base_lower != "__init__.py"
                    and len(base_lower) >= 4
                    and _contains_path_token(active_lower, base_lower)
                )
            )
        )
        if not in_active:
            guarded_paths.add(rel)

    path_scores: dict[str, int] = {}
    stem_matched_ids: set[str] = set()
    candidate_pool: list[str] = []
    for rel in non_test_files:
        if rel in guarded_paths:
            continue
        rel_lower = rel.lower()
        base_lower = rel_lower.rsplit("/", 1)[-1]
        if base_lower == "__init__.py":
            if rel_lower not in active_lower or not _contains_path_token(active_lower, rel_lower):
                continue
            path_scores[rel] = 100
            candidate_pool.append(rel)
            continue

        explicit_doc_mention = (
            base_lower in active_lower
            and (
                _contains_path_token(active_lower, rel_lower)
                or _contains_path_token(active_lower, base_lower)
            )
        )
        if _is_doc_or_meta_path(rel) and not explicit_doc_mention:
            continue

        candidate_pool.append(rel)
        if base_lower in active_lower:
            if rel_lower in active_lower and _contains_path_token(active_lower, rel_lower):
                path_scores[rel] = 100
            elif _contains_path_token(active_lower, base_lower):
                path_scores[rel] = 60 if base_counts.get(base_lower, 1) == 1 else 45

        stem = base_lower.rsplit(".", 1)[0].lstrip("_")
        stripped_stem = stem.rstrip("s")
        if len(stem) >= 3 and (stem in dotted_owners or stem in pascal_classes):
            path_scores[rel] = max(path_scores.get(rel, 0), 75)
            stem_matched_ids.add(stem)
        elif len(stem) >= 4 and any(cls.endswith(stem) for cls in (dotted_owners | pascal_classes)):
            path_scores[rel] = max(path_scores.get(rel, 0), 52)
            stem_matched_ids.add(stem)
        elif len(stem) >= 4 and (
            stem in ident_lower_set
            or (len(stripped_stem) >= 4 and stripped_stem in ident_lower_set)
        ):
            if path_scores.get(rel, 0) < 38:
                path_scores[rel] = 38
            stem_matched_ids.add(stem)

    content_scan_set: set[str] | None = None
    if repo_path is not None and ident_regex is not None and len(candidate_pool) > 150:
        grep_priority = list(
            dict.fromkeys(
                [i for i in identifiers if "_" in i or any(c.isupper() for c in i) or i.lower() in stem_matched_ids]
                + identifiers
            )
        )
        grep_hits = _git_grep_candidate_files(repo_path, grep_priority, max_files=120)
        if grep_hits is not None:
            content_scan_set = (grep_hits & set(candidate_pool)) | set(path_scores.keys())
        else:
            content_scan_set = set(path_scores.keys()) | set(candidate_pool[:250])

    scored: list[tuple[float, str]] = []
    file_texts: dict[str, str] = {}
    matched_by_file: dict[str, set[str]] = {}
    doc_freq: dict[str, int] = defaultdict(int)
    iter_files = (
        sorted(content_scan_set - guarded_paths)
        if content_scan_set is not None
        else candidate_pool
    )
    high_spec_lower = (
        stem_matched_ids
        | {t.lower() for block in _BACKTICK_RE.findall(active_prd) for t in _IDENT_RE.findall(block)}
        | {t.lower() for t in _CAMEL_RE.findall(active_prd)}
        | {t.lower() for t in _SNAKE_RE.findall(active_prd)}
    )
    total_scanned = max(1, len(iter_files))

    for rel in iter_files:
        if rel in guarded_paths or _is_test_path(rel):
            continue
        if repo_path is not None and ident_regex is not None:
            full_path = repo_path / rel
            if full_path.is_file() and full_path.stat().st_size <= 262_144:
                text = full_path.read_text(encoding="utf-8", errors="replace")
                file_texts[rel] = text
                matched = set(ident_regex.findall(text))
                if matched:
                    matched_by_file[rel] = matched
                    for ident in matched:
                        doc_freq[ident] += 1

    all_candidates = sorted((set(path_scores.keys()) | set(file_texts.keys())) - guarded_paths)
    for rel in all_candidates:
        if _is_test_path(rel):
            continue
        score = float(path_scores.get(rel, 0))
        matched = matched_by_file.get(rel, set())
        token_pts: list[float] = []
        for ident in matched:
            df = doc_freq.get(ident, 1)
            idf = 1.0 + math.log((total_scanned + 1.0) / (df + 1.0))
            is_high_spec = (
                ident.lower() in high_spec_lower
                or "_" in ident
                or any(c.isupper() for c in ident)
            )
            if is_high_spec and df <= 3:
                token_pts.append((idf ** 1.4) * 7.5)
            elif is_high_spec:
                token_pts.append(idf * 5.0)
            else:
                token_pts.append(idf * 2.2)
        token_pts.sort(reverse=True)
        content_pts = sum(token_pts[:14])
        stem_now = rel.lower().rsplit("/", 1)[-1].rsplit(".", 1)[0].lstrip("_")
        if len(stem_now) >= 4 and stem_now in {m.lower() for m in matched}:
            content_pts += 22.0
        score += content_pts
        if matched and _STUB_MARKER_RE.search(file_texts.get(rel, "")):
            score += 20.0

        if score >= 20.0:
            scored.append((round(score, 2), rel))

    scored.sort(key=lambda item: (-item[0], item[1]))
    top_pool = scored[:max_targets]
    target_files: list[str] = []
    demoted_runners_up: list[str] = []
    if top_pool:
        top_score = top_pool[0][0]
        target_files.append(top_pool[0][1])
        prev_score = top_score
        for sc, rel in top_pool[1:]:
            p_sc = path_scores.get(rel, 0)
            if p_sc >= 60 or (p_sc >= 38 and len(target_files) < 3 and sc >= 38.0):
                target_files.append(rel)
                prev_score = sc
            elif (
                len(target_files) < 3
                and sc >= max(24.0, top_score * 0.68)
                and sc >= prev_score * 0.65
            ):
                target_files.append(rel)
                prev_score = sc
            else:
                demoted_runners_up.append(rel)
    else:
        top_score = 0.0

    candidate_files = [rel for _sc, rel in scored[:60]]
    confident = bool(target_files) and top_score >= 36.0

    readonly_files: list[str] = []
    seen_ro = set(target_files) | set(test_files)
    for rel in demoted_runners_up:
        if rel not in seen_ro:
            seen_ro.add(rel)
            readonly_files.append(rel)
            if len(readonly_files) >= max_readonly:
                break

    if repo_path is not None and len(readonly_files) < max_readonly:
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
            and base_lower in active_lower
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
        candidate_files=candidate_files,
        identifiers=identifiers,
    )


def extract_fast_skeleton(
    file_path: Path,
    max_lines: int = 120,
    *,
    priority_identifiers: list[str] | None = None,
) -> str:
    """Fast 0-dependency AST/regex skeleton extractor with exact L<n> line numbers."""
    if not file_path.is_file():
        return ""
    try:
        raw_bytes = file_path.read_bytes()[:1_048_576]
        text = raw_bytes.decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""

    out: list[str] = []
    suffix = file_path.suffix.lower()
    if suffix == ".py":
        try:
            tree = ast.parse(text)
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)) and len(out) < 12:
                    out.append(f"L{node.lineno}: {_clip_line(lines[node.lineno - 1])}")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(f"L{node.lineno}: {_clip_line(lines[node.lineno - 1])}")
                elif isinstance(node, ast.ClassDef):
                    out.append(f"L{node.lineno}: {_clip_line(lines[node.lineno - 1])}")
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            out.append(f"L{sub.lineno}: {_clip_line(lines[sub.lineno - 1])}")
        except SyntaxError:
            out.clear()

    if not out:
        for idx, line in enumerate(lines, start=1):
            if _DECL_LINE_RE.match(line):
                out.append(f"L{idx}: {_clip_line(line.rstrip())}")

    if not out:
        limit = min(len(lines), min(max_lines, 40))
        out = [f"L{idx}: {_clip_line(lines[idx - 1].rstrip())}" for idx in range(1, limit + 1)]
        if len(lines) > limit:
            out.append("... [skeleton trimmed]")
        return "\n".join(out)

    if len(out) > max_lines:
        keep_indices: set[int] = set()
        if priority_identifiers:
            filtered = [ident for ident in priority_identifiers if len(ident) >= 3][:25]
            if filtered:
                pat = re.compile(
                    r"\b(?:" + "|".join(re.escape(i) for i in sorted(filtered, key=len, reverse=True)) + r")\b"
                )
                last_class_idx: int | None = None
                for idx, entry in enumerate(out):
                    if re.match(r"^L\d+:\s*(?:export\s+)?class\b", entry):
                        last_class_idx = idx
                    if pat.search(entry):
                        keep_indices.add(idx)
                        if last_class_idx is not None:
                            keep_indices.add(last_class_idx)

        rem = max(0, max_lines - len(keep_indices))
        head_count = max(1, (rem * 2) // 3)
        tail_count = max(0, rem - head_count)
        for idx in range(min(head_count, len(out))):
            keep_indices.add(idx)
        for idx in range(max(0, len(out) - tail_count), len(out)):
            keep_indices.add(idx)

        selected_indices = sorted(keep_indices)[:max_lines]
        trimmed: list[str] = []
        prev_idx = -1
        for idx in selected_indices:
            if prev_idx != -1 and idx > prev_idx + 1:
                trimmed.append("... [skeleton trimmed]")
            trimmed.append(out[idx])
            prev_idx = idx
        return "\n".join(trimmed)
    return "\n".join(out)


def _python_function_spans(text: str) -> list[tuple[int, int]]:
    """Return 0-based (start_idx, end_idx) line spans for Python functions/methods sorted tightest first."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            dec_lines = [getattr(d, "lineno", node.lineno) for d in getattr(node, "decorator_list", [])]
            start_idx = max(0, min([node.lineno, *dec_lines]) - 1)
            end_idx = max(start_idx, (getattr(node, "end_lineno", None) or node.lineno) - 1)
            spans.append((start_idx, end_idx))
    spans.sort(key=lambda sp: (sp[1] - sp[0], sp[0]))
    return spans


def extract_symbol_windows(
    file_path: Path,
    identifiers: list[str],
    *,
    radius: int = 25,
    max_total_lines: int = 180,
) -> str:
    """Extract complete AST function scopes or declaration-aligned windows around PRD identifiers."""
    if not file_path.is_file():
        return ""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""

    filtered = [ident for ident in identifiers if len(ident) >= 3][:35]
    hits: list[int] = []
    pat: re.Pattern[str] | None = None
    id_weights: dict[str, float] = {}
    if filtered:
        for idx, ident in enumerate(filtered):
            base_w = 18.0 if ("_" in ident or any(c.isupper() for c in ident)) else 6.0
            key = ident.lower()
            if key not in id_weights:
                id_weights[key] = base_w / (1.0 + idx * 0.15)
        pat = re.compile(r"\b(?:" + "|".join(re.escape(i) for i in sorted(filtered, key=len, reverse=True)) + r")\b")
        hits = [idx for idx, line in enumerate(lines) if pat.search(line)]

    if not hits:
        head = [f"L{i + 1}: {_clip_line(lines[i])}" for i in range(min(30, len(lines)))]
        if len(lines) > 50:
            head.append("...")
            head.extend(f"L{i + 1}: {_clip_line(lines[i])}" for i in range(max(30, len(lines) - 15), len(lines)))
        return "\n".join(head)

    py_spans = _python_function_spans(text) if file_path.suffix.lower() == ".py" else []
    raw_intervals: list[list[int]] = []
    for h in hits:
        enclosing = next((sp for sp in py_spans if sp[0] <= h <= sp[1]), None)
        if enclosing is not None:
            fn_start, fn_end = enclosing
            if (fn_end - fn_start) <= 65:
                raw_intervals.append([fn_start, fn_end])
            else:
                raw_intervals.append([fn_start, min(fn_end, fn_start + 3)])
                raw_intervals.append([max(fn_start, h - radius), min(fn_end, h + radius)])
        else:
            start = max(0, h - radius)
            end = min(len(lines) - 1, h + radius)
            for back in range(start - 1, max(-1, start - 13), -1):
                if _DECL_LINE_RE.match(lines[back]):
                    start = back
                    break
            raw_intervals.append([start, end])

    raw_intervals.sort(key=lambda iv: (iv[0], iv[1]))
    intervals: list[list[int]] = []
    for start, end in raw_intervals:
        if intervals and start <= intervals[-1][1] + 2:
            intervals[-1][1] = max(intervals[-1][1], end)
        else:
            intervals.append([start, end])

    total_interval_lines = sum((end - start + 1) for start, end in intervals)
    if total_interval_lines > max_total_lines and pat is not None:
        scored_ivs: list[tuple[float, int, int]] = []
        for start, end in intervals:
            block_text = "\n".join(lines[start : end + 1])
            found_ids = {m.lower() for m in pat.findall(block_text)}
            win_score = sum(id_weights.get(fid, 3.0) for fid in found_ids)
            header_text = "\n".join(lines[start : min(end + 1, start + 3)])
            header_ids = {m.lower() for m in pat.findall(header_text)}
            if header_ids:
                win_score += 30.0 + sum(id_weights.get(fid, 3.0) for fid in header_ids)
            if (end - start) <= 45:
                win_score += 5.0
            scored_ivs.append((win_score, start, end))

        scored_ivs.sort(key=lambda item: (-item[0], item[1]))
        chosen: list[list[int]] = []
        used_lines = 0
        for _sc, start, end in scored_ivs:
            span_len = end - start + 1
            if used_lines + span_len <= max_total_lines:
                chosen.append([start, end])
                used_lines += span_len
            elif max_total_lines - used_lines >= 12:
                rem_len = max_total_lines - used_lines
                chosen.append([start, start + rem_len - 1])
                used_lines = max_total_lines
                break
        if chosen:
            chosen.sort(key=lambda iv: iv[0])
            intervals = chosen

    emitted: list[str] = []
    for w_idx, (start, end) in enumerate(intervals):
        if w_idx > 0:
            emitted.append("...")
        for i in range(start, end + 1):
            emitted.append(f"L{i + 1}: {_clip_line(lines[i])}")
            if len(emitted) >= max_total_lines:
                emitted.append("... [window limit reached]")
                return "\n".join(emitted)
    return "\n".join(emitted)


def build_hierarchical_dir_summary(
    real_files: set[str],
    *,
    priority_dirs: set[str] | None = None,
    max_entries: int = 80,
) -> str:
    """Compress thousands of files across hundreds of folders into a compact summary."""
    groups: dict[str, list[str]] = defaultdict(list)
    for raw in real_files:
        rel = normalize_posix_path(raw)
        if not rel:
            continue
        parts = rel.split("/")
        if len(parts) <= 1:
            d = "."
        else:
            depth = min(len(parts) - 1, 3)
            d = "/".join(parts[:depth])
        groups[d].append(parts[-1])

    norm_prio = {normalize_posix_path(d).strip("/") or "." for d in (priority_dirs or set())}
    prio_keys = sorted(d for d in groups if d in norm_prio)
    other_keys = sorted((d for d in groups if d not in norm_prio), key=lambda d: (-len(groups[d]), d))
    ordered = prio_keys + other_keys

    lines: list[str] = []
    for d in ordered[:max_entries]:
        files = sorted(groups[d])
        samples = ", ".join(files[:3])
        if len(files) > 3:
            samples += f", +{len(files) - 3} more"
        prefix = "./" if d == "." else f"{d}/"
        lines.append(f"{prefix} ({len(files)} files: {samples})")

    if len(ordered) > max_entries:
        rem_dirs = ordered[max_entries:]
        rem_files = sum(len(groups[d]) for d in rem_dirs)
        lines.append(f"... (+{len(rem_dirs)} more folders, {rem_files} files)")
    return "\n".join(lines)


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

    def _build_fast_repo_map(
        self,
        real_files: set[str],
        state_hash: str,
        *,
        focus_files: list[str] | None = None,
        priority_identifiers: list[str] | None = None,
    ) -> Path:
        """Build scoped XML repo map with hierarchical dir summary + fast skeletons (for mega-repos or repomix fallback)."""
        norm_set = {normalize_posix_path(f) for f in real_files if f and f.strip()}
        priority_dirs: set[str] = set()
        selected: list[str] = []
        seen: set[str] = set()

        for raw in focus_files or []:
            rel = normalize_posix_path(raw)
            if rel in norm_set and rel not in seen:
                seen.add(rel)
                selected.append(rel)
                parts = rel.split("/")
                if len(parts) > 1:
                    priority_dirs.add("/".join(parts[: min(len(parts) - 1, 3)]))

        for rel in sorted(norm_set):
            if len(selected) >= 80:
                break
            if rel not in seen and rel.rsplit("/", 1)[-1].lower() in _ROOT_MANIFEST_NAMES:
                seen.add(rel)
                selected.append(rel)

        for rel in sorted(norm_set):
            if len(selected) >= 80:
                break
            if rel in seen or _is_test_path(rel) or _is_doc_or_meta_path(rel):
                continue
            full_path = self.repo_path / rel
            if full_path.is_file() and full_path.stat().st_size > 120_000:
                continue
            seen.add(rel)
            selected.append(rel)

        dir_summary = build_hierarchical_dir_summary(norm_set, priority_dirs=priority_dirs, max_entries=80)
        blocks = [f"<directory_structure>\n{dir_summary}\n</directory_structure>", "<files>"]
        for rel in selected:
            skel = extract_fast_skeleton(
                self.repo_path / rel,
                max_lines=80,
                priority_identifiers=priority_identifiers,
            )
            if skel:
                blocks.append(f'<file path="{rel}">\n{skel}\n</file>')
        blocks.append("</files>")

        self.repo_map_path.write_text("<repomix>\n" + "\n".join(blocks) + "\n</repomix>\n", encoding="utf-8")
        self.hash_path.write_text(state_hash, encoding="utf-8")
        return self.repo_map_path

    def build_repo_map(
        self,
        force: bool = False,
        *,
        focus_files: list[str] | None = None,
        priority_identifiers: list[str] | None = None,
    ) -> Path:
        """Build cached Repo Skeleton Map (`repomix --compress` on <=250 files, scoped fast indexer on mega-repos)."""
        self.auto_dir.mkdir(parents=True, exist_ok=True)
        base_hash = self.compute_repo_state_hash()
        focus_key = ""
        if focus_files:
            focus_key = ":" + hashlib.sha256("\n".join(sorted(focus_files)).encode("utf-8")).hexdigest()[:12]
        state_hash = f"{base_hash}{focus_key}"

        if (
            not force
            and self.repo_map_path.is_file()
            and self.hash_path.is_file()
            and self.hash_path.read_text(encoding="utf-8").strip() in {state_hash, base_hash}
        ):
            if not focus_key or self.hash_path.read_text(encoding="utf-8").strip() == state_hash:
                return self.repo_map_path

        try:
            real_files = list_real_files(self.repo_path)
        except IndexerError:
            real_files = set()

        if len(real_files) > 250:
            return self._build_fast_repo_map(
                real_files,
                state_hash,
                focus_files=focus_files,
                priority_identifiers=priority_identifiers,
            )

        npx_bin = shutil.which("npx.cmd") or shutil.which("npx")
        if not npx_bin:
            if real_files:
                return self._build_fast_repo_map(
                    real_files,
                    state_hash,
                    focus_files=focus_files,
                    priority_identifiers=priority_identifiers,
                )
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
            if real_files:
                return self._build_fast_repo_map(
                    real_files,
                    state_hash,
                    focus_files=focus_files,
                    priority_identifiers=priority_identifiers,
                )
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
        dir_body = dir_match.group(1)
        dir_cap = min(8000, max(1000, max_chars // 4))
        if len(dir_body) > dir_cap:
            cut = dir_body.rfind("\n", 0, dir_cap)
            cut = cut if cut > 0 else dir_cap
            dir_body = dir_body[:cut] + "\n... [directory tree truncated]"
        sections.append(f"<directory_structure>{dir_body}</directory_structure>")

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
