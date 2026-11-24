"""Backward-compatible facade re-exporting `locator` and `ast_scanner`."""

from __future__ import annotations

from engine.ast_scanner import (
    extract_fast_skeleton,
    extract_symbol_windows,
    scan_repo_tech_debt,
)
from engine.locator import (
    IndexerError,
    LocatedFiles,
    _is_doc_or_meta_path,
    _is_test_path,
    list_real_files,
    locate_relevant_files,
)

__all__ = [
    "IndexerError",
    "LocatedFiles",
    "_is_doc_or_meta_path",
    "_is_test_path",
    "extract_fast_skeleton",
    "extract_symbol_windows",
    "list_real_files",
    "locate_relevant_files",
    "scan_repo_tech_debt",
]
