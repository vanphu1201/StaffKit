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


_BRACE_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".swift", ".php", ".dart", ".scala",
}

_BRACE_BLOCK_START_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:static\s+)?"
    r"(?:inline\s+)?(?:virtual\s+)?(?:override\s+)?(?:public\s+|private\s+|protected\s+)?"
    r"(?:function\b|func\b|fn\b|def\b|class\b|struct\b|interface\b|enum\b|impl\b|"
    r"(?:const|let|var)\s+[A-Za-z_]\w*\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_]\w*)\s*(?::[^=]+)?=>|"
    r"[A-Za-z_~]\w*(?:\s*<[^>]+>)?\s*\([^;]*\)\s*(?:const\s*)?(?:->\s*[^{;]+|:\s*[^{;]+)?\{)"
)

_STRIP_STR_COMMENT_RE = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
)


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


def _brace_block_spans(lines: list[str], max_scan_lines: int = 220) -> list[tuple[int, int]]:
    """Return 0-based (start_idx, end_idx) spans for brace-delimited functions/methods/components sorted tightest first."""
    cleaned_lines = [_STRIP_STR_COMMENT_RE.sub("", ln) for ln in lines]
    spans: list[tuple[int, int]] = []
    n = len(lines)
    for idx, line in enumerate(lines):
        if not (_DECL_LINE_RE.match(line) or _BRACE_BLOCK_START_RE.match(line)):
            continue
        start_idx = idx
        while start_idx > 0 and (idx - start_idx) < 3:
            prev_s = lines[start_idx - 1].strip()
            if prev_s.startswith(("@", "#[")):
                start_idx -= 1
            else:
                break

        depth = 0
        opened = False
        end_idx = -1
        limit = min(n, idx + max_scan_lines)
        for cur in range(idx, limit):
            cl = cleaned_lines[cur]
            if not opened and (cur - idx) > 5:
                break
            if not opened and ";" in cl and "{" not in cl:
                break
            for ch in cl:
                if ch == "{":
                    depth += 1
                    opened = True
                elif ch == "}" and opened:
                    depth -= 1
                    if depth == 0:
                        end_idx = cur
                        break
            if opened and depth == 0 and end_idx >= idx:
                break
        if end_idx >= idx:
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

    suffix = file_path.suffix.lower()
    if suffix == ".py":
        block_spans = _python_function_spans(text)
    elif suffix in _BRACE_EXTS:
        block_spans = _brace_block_spans(lines)
    else:
        block_spans = []
    raw_intervals: list[list[int]] = []
    for h in hits:
        enclosing = next((sp for sp in block_spans if sp[0] <= h <= sp[1]), None)
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


_SECRET_SCAN_RE = re.compile(
    r"""(?:\b(?:sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b)"""
)
_CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".cs", ".cpp", ".c", ".h", ".php", ".rb", ".swift", ".dart",
}


def scan_repo_tech_debt(repo_path: Path, *, max_files: int = 250) -> str:
    """0-token static X-Ray for `/audit-and-evolve`: bloated files, TODO/mock markers, hardcoded secrets & test ratio."""
    try:
        all_files = sorted(list_real_files(repo_path))
    except IndexerError:
        ignore_dirs = {".git", ".auto-engine", "node_modules", ".venv", "__pycache__", "dist", "build"}
        all_files = []
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for f in files:
                all_files.append((Path(root) / f).relative_to(repo_path).as_posix())
        all_files.sort()

    code_files = [
        rel for rel in all_files
        if not _is_doc_or_meta_path(rel) and Path(rel).suffix.lower() in _CODE_EXTS
    ]
    test_files = [rel for rel in code_files if _is_test_path(rel)]
    src_files = [rel for rel in code_files if not _is_test_path(rel)]

    bloated: list[tuple[int, str]] = []
    stub_hits: list[tuple[int, str]] = []
    secret_hits: list[str] = []

    for rel in src_files[:max_files]:
        fp = repo_path / rel
        if not fp.is_file():
            continue
        try:
            if fp.stat().st_size > 512_000:
                bloated.append((9999, rel))
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line_cnt = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        if line_cnt >= 450:
            bloated.append((line_cnt, rel))
        markers = len(_STUB_MARKER_RE.findall(text))
        if markers > 0:
            stub_hits.append((markers, rel))
        if _SECRET_SCAN_RE.search(text):
            secret_hits.append(rel)

    bloated.sort(key=lambda item: (-item[0], item[1]))
    stub_hits.sort(key=lambda item: (-item[0], item[1]))

    lines = [
        f"- Source vs Test Files: `{len(src_files)}` source / `{len(test_files)}` test",
    ]
    if bloated:
        top_b = ", ".join(f"`{rel}` ({cnt}L)" for cnt, rel in bloated[:5])
        lines.append(f"- Bloated Files (>=450 lines): {top_b}")
    else:
        lines.append("- Bloated Files (>=450 lines): None")
    if stub_hits:
        top_s = ", ".join(f"`{rel}` ({cnt})" for cnt, rel in stub_hits[:5])
        lines.append(f"- TODO/FIXME/Stub Markers: {top_s}")
    else:
        lines.append("- TODO/FIXME/Stub Markers: None")
    if secret_hits:
        lines.append(f"- Potential Hardcoded Secrets: {', '.join(f'`{r}`' for r in secret_hits[:5])}")
    else:
        lines.append("- Potential Hardcoded Secrets: None detected")
    return "\n".join(lines)
