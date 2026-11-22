"""Zero-token Git safety guard: branch isolation, scope guard, anti-test-gaming, commit & rollback."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path, PurePosixPath

from engine.models import normalize_posix_path

ASSERTION_PATTERN = re.compile(
    r"\bexpect\s*\(|\bassert\b|\bassertEqual\b|\bassertRaises\b|\btoThrow\s*\(|\btoEqual\s*\(|\btoBe\s*\(|pytest\.raises"
)


class GitGuardError(RuntimeError):
    """Raised when a Git safety invariant is violated."""


def _matches_artifact_glob(rel_path: str, pattern: str) -> bool:
    """Match a POSIX relative path against common gitignore/glob patterns cross-platform."""
    norm_path = normalize_posix_path(rel_path)
    norm_pat = normalize_posix_path(pattern).rstrip("/")

    if norm_path == norm_pat or norm_path.startswith(f"{norm_pat}/"):
        return True
    if norm_pat.endswith("/**"):
        prefix = norm_pat[:-3].rstrip("/")
        if norm_path == prefix or norm_path.startswith(f"{prefix}/"):
            return True
    if norm_pat.startswith("**/"):
        sub_pat = norm_pat[3:]
        if _matches_artifact_glob(norm_path, sub_pat):
            return True
        if any(_matches_artifact_glob(part, sub_pat) for part in norm_path.split("/")):
            return True
    return fnmatch.fnmatch(norm_path, norm_pat)


def is_test_file(rel_path: str) -> bool:
    """Return True if path looks like a test file."""
    posix = normalize_posix_path(rel_path)
    p = PurePosixPath(posix)
    name = p.name.lower()
    parts = [part.lower() for part in p.parts]
    return (
        name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or "_test." in name
        or "tests" in parts
        or "__tests__" in parts
    )


class GitGuard:
    """Deterministic Git guardrails executing strictly inside target_repo."""

    def __init__(self, repo_path: Path, ignore_artifacts: list[str] | None = None) -> None:
        self.repo_path = repo_path.resolve()
        artifacts = list(ignore_artifacts or [])
        if ".auto-engine/**" not in artifacts:
            artifacts.insert(0, ".auto-engine/**")
        self.ignore_artifacts = artifacts

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and proc.returncode != 0:
            raise GitGuardError(
                f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    def is_ignored_artifact(self, rel_path: str) -> bool:
        """Check whether a relative path is an allowed engine/test-runner artifact."""
        return any(_matches_artifact_glob(rel_path, pat) for pat in self.ignore_artifacts)

    def ensure_git_exclude(self) -> Path:
        """Ensure `.auto-engine/` is excluded locally in `.git/info/exclude`."""
        proc = self._run_git("rev-parse", "--git-path", "info/exclude")
        exclude_raw = Path(proc.stdout.strip())
        exclude_path = exclude_raw if exclude_raw.is_absolute() else (self.repo_path / exclude_raw)
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        lines = [line.strip() for line in existing.splitlines()]
        if ".auto-engine/" not in lines and ".auto-engine" not in lines:
            separator = "" if (not existing or existing.endswith("\n")) else "\n"
            exclude_path.write_text(f"{existing}{separator}.auto-engine/\n", encoding="utf-8")
        return exclude_path

    def current_branch(self) -> str:
        """Return the currently checked-out branch name."""
        return self._run_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def ensure_work_branch(self, base_branch: str, work_branch: str) -> str:
        """Verify clean tree and switch to (or create) work_branch without touching base_branch."""
        if work_branch == base_branch:
            raise GitGuardError(f"Work branch cannot equal protected base branch '{base_branch}'.")
        if self.has_changes():
            tracked, untracked = self.get_changed_files()
            raise GitGuardError(
                f"Working tree is dirty before starting sprint: {tracked + untracked}"
            )

        current = self.current_branch()
        if current == work_branch:
            return work_branch

        branch_exists = (
            self._run_git("show-ref", "--verify", f"refs/heads/{work_branch}", check=False).returncode
            == 0
        )
        if branch_exists:
            self._run_git("checkout", work_branch)
        else:
            base_exists = (
                self._run_git("show-ref", "--verify", f"refs/heads/{base_branch}", check=False).returncode
                == 0
            )
            if base_exists:
                self._run_git("checkout", "-b", work_branch, base_branch)
            else:
                self._run_git("checkout", "-b", work_branch)
        return work_branch

    def create_checkpoint(self) -> str:
        """Return current HEAD commit SHA as clean task checkpoint."""
        return self._run_git("rev-parse", "HEAD").stdout.strip()

    def get_changed_files(self) -> tuple[list[str], list[str]]:
        """Return (tracked_changed, untracked_new) POSIX relative paths excluding ignore_artifacts."""
        proc = self._run_git("status", "--porcelain", "-z", "-uall")
        raw = proc.stdout
        if not raw:
            return [], []

        tracked: list[str] = []
        untracked: list[str] = []
        entries = raw.split("\0")
        idx = 0
        while idx < len(entries):
            entry = entries[idx]
            idx += 1
            if not entry:
                continue
            status = entry[:2]
            rel_path = normalize_posix_path(entry[3:])
            if status.startswith("R") or status.startswith("C"):
                # With -z, renames/copies include a second NUL-terminated original path.
                if idx < len(entries):
                    orig_path = normalize_posix_path(entries[idx])
                    idx += 1
                    if not self.is_ignored_artifact(orig_path):
                        tracked.append(orig_path)
            if self.is_ignored_artifact(rel_path):
                continue
            if status == "??":
                untracked.append(rel_path)
            else:
                tracked.append(rel_path)

        return sorted(set(tracked)), sorted(set(untracked))

    def has_changes(self) -> bool:
        """Return True if any non-artifact file was modified, deleted, or created."""
        tracked, untracked = self.get_changed_files()
        return bool(tracked or untracked)

    def check_scope(
        self,
        allowed_files: list[str],
        create_new_files: list[str],
        protected_test_files: list[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Ensure every changed/created file belongs strictly to allowed scope and is not protected."""
        tracked, untracked = self.get_changed_files()
        changed = tracked + untracked
        protected_set = {normalize_posix_path(p) for p in (protected_test_files or [])}
        permitted_set = (
            {normalize_posix_path(p) for p in allowed_files}
            | {normalize_posix_path(p) for p in create_new_files}
        ) - protected_set

        violations = [p for p in changed if p not in permitted_set]
        return len(violations) == 0, violations

    def _scan_lines_for_patterns(
        self,
        rel_path: str,
        lines: list[str],
        compiled_patterns: list[tuple[str, re.Pattern[str]]],
    ) -> list[str]:
        hits: list[str] = []
        for idx, line in enumerate(lines, start=1):
            for raw_pat, regex in compiled_patterns:
                if regex.search(line):
                    hits.append(f"{rel_path}:{idx} matches forbidden pattern `{raw_pat}`: {line.strip()}")
        return hits

    def check_diff_sanity(
        self,
        create_new_files: list[str],
        forbid_patterns: list[str],
        forbid_assertion_deletion: bool = True,
    ) -> tuple[bool, list[str]]:
        """Detect anti-test-gaming patterns in git diff + untracked files and net deleted assertions."""
        compiled = [(pat, re.compile(pat)) for pat in forbid_patterns]
        violations: list[str] = []

        # 1. Scan tracked diff (+ added lines and - deleted assertions in test files).
        diff_proc = self._run_git("diff", "-U0", "HEAD")
        current_file: str | None = None
        removed_assertions: dict[str, int] = {}
        added_assertions: dict[str, int] = {}

        for raw_line in diff_proc.stdout.splitlines():
            if raw_line.startswith("+++ b/"):
                current_file = normalize_posix_path(raw_line[6:])
                continue
            if raw_line.startswith("--- ") or raw_line.startswith("@@"):
                continue
            if not current_file or self.is_ignored_artifact(current_file):
                continue

            if raw_line.startswith("+"):
                content = raw_line[1:]
                for raw_pat, regex in compiled:
                    if regex.search(content):
                        violations.append(
                            f"{current_file} added forbidden `{raw_pat}`: {content.strip()}"
                        )
                if forbid_assertion_deletion and is_test_file(current_file) and ASSERTION_PATTERN.search(content):
                    added_assertions[current_file] = added_assertions.get(current_file, 0) + 1
            elif raw_line.startswith("-"):
                content = raw_line[1:]
                if forbid_assertion_deletion and is_test_file(current_file) and ASSERTION_PATTERN.search(content):
                    removed_assertions[current_file] = removed_assertions.get(current_file, 0) + 1

        for test_path, removed_cnt in removed_assertions.items():
            added_cnt = added_assertions.get(test_path, 0)
            if removed_cnt > added_cnt:
                violations.append(
                    f"{test_path} deleted {removed_cnt - added_cnt} test assertion(s) "
                    f"(removed={removed_cnt}, added={added_cnt})"
                )

        # 2. Scan untracked new files (git diff ignores untracked files).
        _, untracked = self.get_changed_files()
        allowed_new = {normalize_posix_path(p) for p in create_new_files}
        files_to_scan = sorted(set(untracked) | {p for p in allowed_new if (self.repo_path / p).is_file() and p in untracked})
        for rel_path in files_to_scan:
            full_path = self.repo_path / rel_path
            if full_path.is_file():
                text = full_path.read_text(encoding="utf-8", errors="replace")
                violations.extend(self._scan_lines_for_patterns(rel_path, text.splitlines(), compiled))

        return len(violations) == 0, violations

    def get_compact_diff(self, files: list[str] | None = None, max_lines: int = 120) -> str:
        """Return compact git diff limited to max_lines."""
        args = ["diff", "-U3", "HEAD"]
        if files:
            existing = [normalize_posix_path(f) for f in files]
            args.extend(["--", *existing])
        proc = self._run_git(*args, check=False)
        lines = proc.stdout.splitlines()
        if len(lines) <= max_lines:
            return proc.stdout.strip()
        head = "\n".join(lines[:max_lines])
        return f"{head}\n... [truncated {len(lines) - max_lines} diff lines]"

    def commit_task(self, task_id: str, title: str, files_to_stage: list[str]) -> str:
        """Stage strictly permitted files and commit cleanly (never via rtk)."""
        norm_files = sorted({normalize_posix_path(f) for f in files_to_stage})
        if not norm_files:
            raise GitGuardError(f"No files specified to commit for task {task_id}.")

        self._run_git("add", "-A", "--", *norm_files)
        status_proc = self._run_git("diff", "--cached", "--name-only")
        if not status_proc.stdout.strip():
            raise GitGuardError(f"Nothing staged to commit for task {task_id}.")

        msg = f"feat(auto-engine): [{task_id}] {title}"
        self._run_git("commit", "-m", msg)
        return self.create_checkpoint()

    def hard_rollback(self, checkpoint_sha: str) -> None:
        """Restore repository strictly to checkpoint_sha and clean untracked files outside .auto-engine."""
        self._run_git("reset", "--hard", checkpoint_sha)
        self._run_git("clean", "-fd", "-e", ".auto-engine")
