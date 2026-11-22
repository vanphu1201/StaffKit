"""Layer 1 Indexer: delegates repo skeleton compression to repomix and queries git ls-files."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from engine.models import EngineConfig, normalize_posix_path


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
    """Parse `<file path='...'>` nodes from cached repomix XML in O(file) with 0 LLM tokens."""
    if not repo_map_path.is_file() or not target_paths:
        return {}

    wanted = {normalize_posix_path(p) for p in target_paths}
    result: dict[str, str] = {}

    try:
        tree = ET.parse(repo_map_path)
        root = tree.getroot()
    except ET.ParseError:
        return {}

    for elem in root.iter("file"):
        raw_path = elem.attrib.get("path", "")
        norm = normalize_posix_path(raw_path)
        if norm in wanted:
            content = (elem.text or "").strip()
            if content:
                result[norm] = content
    return result
