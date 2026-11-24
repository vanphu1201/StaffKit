"""0-token AST/regex skeleton extractor, symbol window slicer & Tech-Debt X-Ray."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from engine.locator import (
    IndexerError,
    _DECL_LINE_RE,
    _STUB_MARKER_RE,
    _clip_line,
    _is_doc_or_meta_path,
    _is_test_path,
    list_real_files,
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
