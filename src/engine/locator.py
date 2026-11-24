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
_CAMEL_RE = re.compile(r"\b[A-Za-z][a-z0-9]*(?:[A-Z][A-Za-z0-9]*)+\b")
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
    r"""(?:from\s+|require\s*\(\s*|import\s*\(\s*)["']((?:\.|@/|~/)[^"']+)["']"""
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

    # 0-token Vietnamese -> English software concept expansion so pure-Vietnamese requests locate English source files
    vi_low = active_prd_text.lower()
    for vi_phrase, en_terms in _VI_EN_LOCATOR_MAP:
        if vi_phrase in vi_low:
            for term in en_terms:
                _add(term)
    return tokens


_VI_EN_LOCATOR_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cài đặt", ("settings", "config", "preferences")),
    ("kết nối", ("client", "health", "connect", "bridge", "gateway", "api")),
    ("giao diện", ("component", "components", "web", "ui", "view", "page", "app")),
    ("nút", ("button", "component", "ui", "handler")),
    ("giọng đọc", ("tts", "audio", "voice", "speech", "media")),
    ("âm thanh", ("audio", "ffmpeg", "mixer", "sound", "media")),
    ("video", ("video", "render", "renderer", "pipeline", "remotion", "mp4")),
    ("kịch bản", ("script", "prompt", "pipeline", "generate")),
    ("đăng nhập", ("auth", "login", "session", "token")),
    ("phân quyền", ("auth", "role", "permission", "guard")),
    ("cơ sở dữ liệu", ("db", "database", "sqlite", "schema", "storage")),
    ("lưu trữ", ("storage", "db", "database", "store")),
    ("hàng đợi", ("queue", "batch", "worker", "job")),
    ("kiểm thử", ("test", "tests", "verify", "verifier")),
    ("xuất file", ("export", "render", "output", "download")),
    ("thống kê", ("stats", "analytics", "dashboard", "chart", "report")),
    ("báo cáo", ("report", "export", "summary")),
    ("cấu hình", ("config", "settings", "env")),
    ("lỗi", ("error", "handler", "client", "validator")),
    ("tìm kiếm", ("search", "filter", "query", "index")),
    ("tải lên", ("upload", "file", "storage", "media")),
    ("thanh toán", ("payment", "billing", "stripe", "checkout", "order")),
    ("thông báo", ("notification", "toast", "alert", "message")),
    ("biểu mẫu", ("form", "input", "modal", "dialog", "validation")),
    ("bảng", ("table", "grid", "list", "pagination")),
    ("trang chủ", ("home", "index", "dashboard", "landing", "app")),
    ("điều hướng", ("nav", "navbar", "sidebar", "router", "route")),
    ("mô hình", ("model", "models", "provider", "client", "ai")),
    ("nhân vật", ("character", "asset", "scene", "story")),
)


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
        if spec.startswith(("@/", "~/")):
            spec_tail = spec[2:]
            base_candidates = [f"src/{spec_tail}", spec_tail]
        else:
            joined = posixpath.normpath(posixpath.join(parent_dir, spec))
            if joined.startswith("../"):
                continue
            base_candidates = [joined]

        # Handle TypeScript NodeNext ESM imports like `./client.js` -> `./client.ts` / `.tsx`
        expanded_bases: list[str] = []
        for base in base_candidates:
            expanded_bases.append(base)
            for js_ext in (".js", ".mjs", ".cjs", ".jsx"):
                if base.endswith(js_ext):
                    expanded_bases.append(base[: -len(js_ext)])
                    break

        exts = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts", "/index.ts", "/index.tsx", "/index.js", "/index.jsx")
        found_match = False
        for base in expanded_bases:
            for ext in exts:
                cand = normalize_posix_path(f"{base}{ext}")
                if cand in norm_real and cand != rel_path and cand not in resolved:
                    resolved.append(cand)
                    found_match = True
                    break
            if found_match:
                break

    return resolved


def _find_reverse_importers(
    target_files: list[str],
    candidate_texts: dict[str, str],
    norm_real: set[str],
    *,
    max_importers: int = 4,
) -> list[str]:
    """Find 1-hop reverse importers (files that import one of `target_files`) from already-loaded repo texts."""
    if not target_files or not candidate_texts:
        return []
    target_set = set(target_files)
    target_stems = {
        t.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        for t in target_files
        if len(t.rsplit("/", 1)[-1].rsplit(".", 1)[0]) >= 3
    }
    importers: list[str] = []
    for rel, text in candidate_texts.items():
        if rel in target_set or not text:
            continue
        low = text.lower()
        if not any(stem in low for stem in target_stems):
            continue
        imported = _resolve_local_imports(rel, text, norm_real)
        if any(imp in target_set for imp in imported):
            importers.append(rel)
            if len(importers) >= max_importers:
                break
    return importers


def _find_git_cochange_files(
    repo_path: Path,
    target_files: list[str],
    norm_real: set[str],
    *,
    max_commits: int = 45,
    max_results: int = 3,
) -> list[str]:
    """0-token Git Co-Change signal: find files frequently edited in the same commits as `target_files` (catches FE<->BE links)."""
    if not target_files or not (repo_path / ".git").exists():
        return []
    try:
        proc = subprocess.run(
            ["git", "log", "--no-merges", f"-n{max_commits}", "--name-only", "--pretty=format:---"],
            cwd=str(repo_path.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    target_set = set(target_files)
    counts: dict[str, int] = defaultdict(int)
    for block in proc.stdout.split("---"):
        files = [
            normalize_posix_path(ln)
            for ln in block.splitlines()
            if ln.strip()
        ]
        # Ignore giant bulk/init commits (> 12 files) that add co-change noise
        if not (2 <= len(files) <= 12):
            continue
        file_set = set(files)
        if not (file_set & target_set):
            continue
        for f in file_set - target_set:
            if f in norm_real and not _is_test_path(f) and not _is_doc_or_meta_path(f):
                counts[f] += 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f for f, cnt in ranked if cnt >= 2][:max_results]


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
                token_pts.append(max(22.0, (idf ** 1.4) * 8.5))
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

    # Priority 1: 1-hop Forward Local Imports (direct dependencies of target_files)
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

    # Priority 2: 1-hop Reverse Importers (callers/consumers of top target_files)
    if repo_path is not None and target_files and len(readonly_files) < max_readonly:
        reverse_pool = dict(file_texts)
        if len(reverse_pool) < 80:
            for rel in non_test_files:
                if rel in reverse_pool or _is_doc_or_meta_path(rel):
                    continue
                full_path = repo_path / rel
                if full_path.is_file() and full_path.stat().st_size <= 262_144:
                    reverse_pool[rel] = full_path.read_text(encoding="utf-8", errors="replace")
                if len(reverse_pool) >= 120:
                    break
        for caller in _find_reverse_importers(target_files[:3], reverse_pool, norm_set, max_importers=4):
            if caller not in seen_ro and not _is_test_path(caller):
                seen_ro.add(caller)
                readonly_files.append(caller)
                if len(readonly_files) >= max_readonly:
                    break

    # Priority 2.5: 0-token Git Co-Change neighbors (catches FE<->BE / API<->Schema links without direct imports)
    if repo_path is not None and target_files and len(readonly_files) < max_readonly:
        for co_file in _find_git_cochange_files(repo_path, target_files[:3], norm_set, max_results=3):
            if co_file not in seen_ro and not _is_test_path(co_file):
                seen_ro.add(co_file)
                readonly_files.append(co_file)
                if len(readonly_files) >= max_readonly:
                    break

    # Priority 3: Demoted scoring runners-up
    for rel in demoted_runners_up:
        if len(readonly_files) >= max_readonly:
            break
        if rel not in seen_ro:
            seen_ro.add(rel)
            readonly_files.append(rel)

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


class IndexerError(RuntimeError):
    """Raised when repository file discovery fails."""


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
