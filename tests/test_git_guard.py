"""Integration tests for GitGuard using a real temporary Git repository."""

import subprocess
from pathlib import Path

import pytest

from engine.git_guard import GitGuard, GitGuardError


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def temp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "bot@example.com")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(
        "from src.app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_core.py").write_text(
        "def test_core():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_git_exclude_and_branch_isolation(temp_repo: Path) -> None:
    guard = GitGuard(temp_repo, ignore_artifacts=[".auto-engine/**", ".pytest_cache/**"])
    exclude_file = guard.ensure_git_exclude()
    assert ".auto-engine/" in exclude_file.read_text(encoding="utf-8")

    # Files in .auto-engine and .pytest_cache must not block branch setup or count as changes.
    (temp_repo / ".auto-engine").mkdir()
    (temp_repo / ".auto-engine" / "state.db").write_text("sqlite", encoding="utf-8")
    (temp_repo / ".pytest_cache").mkdir()
    (temp_repo / ".pytest_cache" / "README.md").write_text("cache", encoding="utf-8")
    assert not guard.has_changes()

    with pytest.raises(GitGuardError):
        guard.ensure_work_branch("main", "main")

    branch = guard.ensure_work_branch("main", "auto-engine/sprint-1")
    assert branch == "auto-engine/sprint-1"
    assert guard.current_branch() == "auto-engine/sprint-1"


def test_scope_guard_protected_tests_and_rollback(temp_repo: Path) -> None:
    guard = GitGuard(temp_repo, ignore_artifacts=[".auto-engine/**", "coverage/**"])
    guard.ensure_work_branch("main", "auto-engine/sprint-1")
    checkpoint = guard.create_checkpoint()

    # Allowed change with Windows backslash normalization + artifact ignored.
    (temp_repo / "src" / "app.py").write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
    (temp_repo / "coverage").mkdir()
    (temp_repo / "coverage" / "lcov.info").write_text("data", encoding="utf-8")
    ok, viols = guard.check_scope([r"src\app.py"], [], ["tests/test_core.py"])
    assert ok and viols == []

    # Touching protected test file must fail even if listed accidentally.
    (temp_repo / "tests" / "test_core.py").write_text("def test_core():\n    assert False\n", encoding="utf-8")
    ok, viols = guard.check_scope(["src/app.py", "tests/test_core.py"], [], ["tests/test_core.py"])
    assert not ok and "tests/test_core.py" in viols

    # Hard rollback restores clean tracked files while preserving .auto-engine/.
    (temp_repo / ".auto-engine").mkdir()
    (temp_repo / ".auto-engine" / "keep.txt").write_text("keep", encoding="utf-8")
    guard.hard_rollback(checkpoint)
    assert not guard.has_changes()
    assert (temp_repo / ".auto-engine" / "keep.txt").exists()
    assert "assert True" in (temp_repo / "tests" / "test_core.py").read_text(encoding="utf-8")


def test_anti_test_gaming_untracked_and_assertion_deletion(temp_repo: Path) -> None:
    guard = GitGuard(temp_repo)
    guard.ensure_work_branch("main", "auto-engine/sprint-2")
    checkpoint = guard.create_checkpoint()

    # 1. Untracked newly created test file containing .skip(
    (temp_repo / "tests" / "auth.test.ts").write_text(
        "describe('auth', () => {\n  it.skip('works', () => { expect(1).toBe(1); });\n});\n",
        encoding="utf-8",
    )
    ok, hits = guard.check_diff_sanity(["tests/auth.test.ts"], [r"\.skip\(", "@ts-ignore"], True)
    assert not ok
    assert any("tests/auth.test.ts" in h for h in hits)

    guard.hard_rollback(checkpoint)

    # 2. Deleting existing assertion in modified test file without adding replacement
    (temp_repo / "tests" / "test_app.py").write_text(
        "from src.app import add\n\ndef test_add():\n    pass\n",
        encoding="utf-8",
    )
    ok, hits = guard.check_diff_sanity([], [r"\.skip\("], forbid_assertion_deletion=True)
    assert not ok
    assert any("deleted 1 test assertion" in h for h in hits)


def test_commit_task_stages_only_permitted_files(temp_repo: Path) -> None:
    guard = GitGuard(temp_repo)
    guard.ensure_work_branch("main", "auto-engine/sprint-3")
    (temp_repo / "src" / "app.py").write_text("def add(a, b):\n    return int(a) + int(b)\n", encoding="utf-8")
    sha = guard.commit_task("T-01", "Cast inputs", ["src/app.py"])
    assert len(sha) >= 7
    assert not guard.has_changes()


def test_package_json_permits_and_commits_package_lock(temp_repo: Path) -> None:
    guard = GitGuard(temp_repo)
    guard.ensure_work_branch("main", "auto-engine/sprint-lock")
    (temp_repo / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    (temp_repo / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")

    ok, viols = guard.check_scope(["package.json"], [], [])
    assert ok and viols == []

    sha = guard.commit_task("T-01", "Add pkg", ["package.json"])
    assert len(sha) >= 7
    assert not guard.has_changes()
