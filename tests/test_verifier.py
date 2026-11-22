"""Unit tests for Layer 5 Verifier, log trimmer <= 30 lines, error hashing, and sequential commands."""

import subprocess
from pathlib import Path

from engine.git_guard import GitGuard
from engine.models import EngineConfig, GuardsConfig, LimitsConfig, ProjectConfig, TaskSpec, ToolsConfig
from engine.verifier import Verifier, compute_error_hash, trim_error_log


def _init_repo(repo: Path) -> GitGuard:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Bot"],
        ["config", "user.email", "bot@example.com"],
    ):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "src").mkdir()
    (repo / "src" / "calc.py").write_text(
        "\n".join(f"line_{i} = {i}" for i in range(1, 25)) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)
    return GitGuard(repo)


def test_trim_error_log_caps_at_30_lines_and_strips_noise(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    noise = [f"   at node_modules/pkg/index.js:{i}:1" for i in range(180)]
    noisy_log = "\n".join(
        [
            "\x1b[31mFAIL src/calc.py:12\x1b[0m",
            "AssertionError: expected 4 to equal 5",
            *noise,
            "Traceback line unrelated",
        ]
    )
    trimmed, err_hash = trim_error_log(noisy_log, repo, allowed_files=["src/calc.py"], max_lines=30)
    lines = trimmed.splitlines()
    assert len(lines) <= 30
    assert "\x1b[" not in trimmed
    assert "node_modules/" not in trimmed
    assert "AssertionError" in trimmed
    assert "> L12: line_12 = 12" in trimmed
    assert len(err_hash) == 64


def test_error_hash_ignores_volatile_timestamps_and_durations(tmp_path: Path) -> None:
    h1 = compute_error_hash("2026-11-22T10:00:01Z Error at C:\\repo\\a.py (120 ms)", Path(r"C:\repo"))
    h2 = compute_error_hash("2026-11-22T10:05:99Z Error at C:\\repo\\a.py (450 ms)", Path(r"C:\repo"))
    assert h1 == h2


def test_verifier_fail_fast_order_and_sequential_commands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    guard = _init_repo(repo)
    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(repo)),
        limits=LimitsConfig(max_error_log_lines=30),
        guards=GuardsConfig(),
        tools=ToolsConfig(use_rtk=False),
    )
    verifier = Verifier(cfg, guard)
    task = TaskSpec(
        task_id="T-01",
        title="Update calc",
        objective="Update calc",
        allowed_write_files=["src/calc.py"],
        verify_commands=["python -c \"print('ok1')\"", "python -c \"import sys; print('Error boom'); sys.exit(1)\""],
    )

    # 1. No edits -> EMPTY_DIFF without running commands
    res_empty = verifier.verify(task)
    assert not res_empty.passed and res_empty.verdict == "EMPTY_DIFF"

    # 2. Allowed edit -> runs sequential commands, fails on second command
    (repo / "src" / "calc.py").write_text("x = 1\n", encoding="utf-8")
    res_fail = verifier.verify(task)
    assert not res_fail.passed and res_fail.verdict == "VERIFY_FAIL"
    assert "Error boom" in res_fail.trimmed_error_log

    # 3. Passing commands -> PASS
    task_pass = task.model_copy(update={"verify_commands": ["python -c \"print('ok1')\"", "python -c \"print('ok2')\""]})
    res_ok = verifier.verify(task_pass)
    assert res_ok.passed and res_ok.verdict == "PASS"
