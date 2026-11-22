"""Tests for Antigravity CLI stream-json watchdog, MCP gating restore, run.lock, and SDK guard."""

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.bridges.agy_cli_executor import (
    AgyCliExecutor,
    LockError,
    acquire_run_lock,
    build_agy_command,
    dynamic_mcp_guard,
)
from engine.bridges.agy_sdk_executor import inspect_tool_call_scope
from engine.models import AgyConfig, EngineConfig, LimitsConfig, ProjectConfig, TaskSpec


def test_build_agy_command_flags(tmp_path: Path) -> None:
    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(tmp_path)),
        agy=AgyConfig(
            binary="agy",
            permission_mode="accept-edits",
            disable_slash_commands=True,
            auto_approve_tools=False,
        ),
    )
    cmd = build_agy_command(cfg, "gemini-2.5-flash", "low")
    assert cmd[:5] == ["agy", "--output-format", "stream-json", "--input-format", "stream-json"]
    assert "--model" in cmd and "gemini-2.5-flash" in cmd
    assert "--effort" in cmd and "low" in cmd
    assert "--mode" in cmd and "accept-edits" in cmd
    assert "--disable-slash-commands" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "--print-timeout" in cmd and "300s" in cmd


def test_run_lock_prevents_concurrent_execution(tmp_path: Path) -> None:
    auto_dir = tmp_path / ".auto-engine"
    with acquire_run_lock(auto_dir):
        with pytest.raises(LockError):
            with acquire_run_lock(auto_dir):
                pass
    assert not (auto_dir / "run.lock").exists()


def test_dynamic_mcp_guard_restores_on_exception(tmp_path: Path) -> None:
    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(tmp_path)),
        agy=AgyConfig(dynamic_mcp_gating=True),
    )
    task = TaskSpec(
        task_id="T-01",
        title="Needs docs",
        objective="Test",
        needs_docs_mcp=True,
        needs_lsp_mcp=False,
    )
    calls: list[tuple[str, bool]] = []
    with (
        patch("engine.bridges.agy_cli_executor._snapshot_mcp_states", return_value={"serena": True, "context7": False}),
        patch("engine.bridges.agy_cli_executor._set_mcp_state", side_effect=lambda _b, s, e: calls.append((s, e))),
    ):
        with pytest.raises(RuntimeError):
            with dynamic_mcp_guard(cfg, task):
                raise RuntimeError("boom")

    # Initial changes: serena -> False, context7 -> True; restored: serena -> True, context7 -> False
    assert calls[:2] == [("serena", False), ("context7", True)]
    assert set(calls[2:]) == {("serena", True), ("context7", False)}


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.pid = 999999
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self.killed = False

    def poll(self):
        return self.returncode if self.killed else 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


def test_stream_watchdog_aborts_on_forbidden_shell_and_max_turns(tmp_path: Path) -> None:
    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(tmp_path)),
        agy=AgyConfig(dynamic_mcp_gating=False),
        limits=LimitsConfig(max_tool_calls_per_attempt=2, max_tokens_per_task=5000),
    )
    executor = AgyCliExecutor(cfg)
    task = TaskSpec(task_id="T-01", title="Fix", objective="Fix", allowed_write_files=["a.py"])

    # 1. Forbidden shell command triggers immediate kill
    shell_event = json.dumps({"type": "tool_call", "tool_name": "run_command", "args": {"CommandLine": "pytest tests/"}})
    fake_proc = _FakeProc([shell_event])
    with (
        patch("subprocess.Popen", return_value=fake_proc),
        patch("engine.bridges.agy_cli_executor.kill_process_tree") as kill_mock,
    ):
        res = executor.execute("prompt", task, "m", "low")
    assert not res.success and res.verdict_override == "MAX_TURNS_EXCEEDED"
    assert kill_mock.called

    # 2. Exceeding max_tool_calls_per_attempt triggers MAX_TURNS_EXCEEDED
    events = [
        json.dumps({"type": "tool_call", "tool_name": "read_file", "args": {"Path": "a.py"}}),
        json.dumps({"type": "tool_call", "tool_name": "edit_file", "args": {"Path": "a.py"}}),
        json.dumps({"type": "tool_call", "tool_name": "read_file", "args": {"Path": "b.py"}}),
    ]
    fake_proc2 = _FakeProc(events)
    with (
        patch("subprocess.Popen", return_value=fake_proc2),
        patch("engine.bridges.agy_cli_executor.kill_process_tree") as kill_mock2,
    ):
        res2 = executor.execute("prompt", task, "m", "low")
    assert not res2.success and res2.verdict_override == "MAX_TURNS_EXCEEDED"
    assert kill_mock2.called


def test_stream_watchdog_aborts_on_budget_exceeded(tmp_path: Path) -> None:
    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(tmp_path)),
        agy=AgyConfig(dynamic_mcp_gating=False),
        limits=LimitsConfig(max_tokens_per_task=1000),
    )
    executor = AgyCliExecutor(cfg)
    task = TaskSpec(task_id="T-01", title="Fix", objective="Fix")

    events = [
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "usage": {"input_tokens": 900, "output_tokens": 200},
                },
            }
        )
    ]
    fake_proc = _FakeProc(events)
    with (
        patch("subprocess.Popen", return_value=fake_proc),
        patch("engine.bridges.agy_cli_executor.kill_process_tree") as kill_mock,
    ):
        res = executor.execute("prompt", task, "m", "low")
    assert not res.success and res.verdict_override == "BUDGET_EXCEEDED"
    assert res.usage.total_tokens == 1100
    assert kill_mock.called


def test_sdk_tool_call_scope_inspector() -> None:
    allowed = {"src/app.py"}
    protected = {"tests/core.test.ts"}
    assert inspect_tool_call_scope("read_file", {"Path": "secret.py"}, allowed, protected) is None
    assert inspect_tool_call_scope("edit_file", {"Path": r"src\app.py"}, allowed, protected) is None
    viol = inspect_tool_call_scope("edit_file", {"Path": "tests/core.test.ts"}, allowed, protected)
    assert viol is not None and "out-of-scope" in viol
