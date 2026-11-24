"""Mode A1 (Primary): Antigravity CLI (`agy -p`) stateless executor with stream-json watchdog."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from engine.models import (
    EngineConfig,
    ExecutionResult,
    TaskSpec,
    TokenUsage,
    compute_dynamic_tool_call_budget,
)

FORBIDDEN_SHELL_CMD_RE = re.compile(
    r"\b(pytest|vitest|jest|tsc|mypy|ruff|eslint|cargo\s+test|go\s+test|npm\s+(test|run\s+(test|lint|build|typecheck))|npx\s+(tsc|vitest|jest))\b",
    re.IGNORECASE,
)
SHELL_TOOL_NAMES = {"run_command", "send_command_input", "bash", "shell", "terminal", "execute_command"}
PROGRESS_WRITE_TOOL_NAMES = {
    "write_file",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
    "write_to_file",
    "create_file",
    "apply_patch",
}


class LockError(RuntimeError):
    """Raised when another engine instance holds .auto-engine/run.lock."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return str(pid) in proc.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextmanager
def acquire_run_lock(auto_engine_dir: Path) -> Iterator[Path]:
    """Prevent concurrent engine runs from overwriting MCP config or target_repo state."""
    auto_engine_dir.mkdir(parents=True, exist_ok=True)
    lock_path = auto_engine_dir / "run.lock"

    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text(encoding="utf-8").strip())
        except ValueError:
            old_pid = -1
        if _pid_alive(old_pid):
            raise LockError(f"Another engine process (PID {old_pid}) is already running ({lock_path}).")
        lock_path.unlink(missing_ok=True)

    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield lock_path
    finally:
        lock_path.unlink(missing_ok=True)


def kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate a running subprocess and all child processes cleanly across OSes."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    try:
        proc.kill()
    except OSError:
        pass


def _latest_repo_activity_mtime(repo_path: Path, focus_rel_paths: list[str]) -> float:
    """Return latest mtime across target files and shallow project tree so disk writes reset the idle watchdog."""
    latest = 0.0
    if not repo_path.is_dir():
        return latest
    for rel in focus_rel_paths:
        p = repo_path / rel
        for candidate in (p, p.parent):
            try:
                if candidate.exists():
                    mt = candidate.stat().st_mtime
                    if mt > latest:
                        latest = mt
            except OSError:
                pass
    try:
        checked = 0
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in {".git", ".auto-engine", "node_modules", ".venv", "__pycache__", "dist", "build"}]
            for fname in files:
                checked += 1
                if checked > 160:
                    return latest
                try:
                    mt = (Path(root) / fname).stat().st_mtime
                    if mt > latest:
                        latest = mt
                except OSError:
                    pass
    except OSError:
        pass
    return latest


def build_agy_command(
    config: EngineConfig,
    model: str,
    effort: str,
    *,
    input_format: str = "stream-json",
    output_format: str = "stream-json",
    json_schema_path: Path | None = None,
    inline_prompt: str | None = None,
    timeout_override: int | None = None,
) -> list[str]:
    """Construct verified `agy` CLI flags from EngineConfig (defaults to stdin NDJSON streaming)."""
    cmd = [
        config.agy.binary,
        "--output-format",
        output_format,
    ]
    if inline_prompt is None:
        cmd.extend(["--input-format", input_format])
    cleaned_model = model.strip()
    if cleaned_model:
        cmd.extend(["--model", cleaned_model])
    model_has_effort = cleaned_model.lower().endswith(("-low", "-medium", "-high", "-xhigh"))
    if effort and not model_has_effort:
        cmd.extend(["--effort", effort])
    if config.agy.permission_mode:
        cmd.extend(["--mode", config.agy.permission_mode])
    if config.agy.disable_slash_commands:
        cmd.append("--disable-slash-commands")
    if config.agy.auto_approve_tools:
        cmd.append("--dangerously-skip-permissions")
    if json_schema_path is not None:
        cmd.extend(["--json-schema", str(json_schema_path)])
    eff_timeout = timeout_override if timeout_override is not None else config.executor.timeout_seconds
    cmd.extend(["--print-timeout", f"{eff_timeout}s"])
    if inline_prompt is not None:
        cmd.append(f"--print={inline_prompt}")
    return cmd


def encode_stream_user_message(prompt: str) -> str:
    """Encode a prompt into the verified `agy --input-format stream-json` NDJSON line."""
    return json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"


def _extract_usage_from_event(event: dict[str, Any]) -> TokenUsage | None:
    """Extract token counts from real `agy` stream-json (`result.usage` / `step_update.usage`) or flat events."""
    candidates: list[Any] = [event, event.get("usage"), event.get("token_usage"), event.get("metrics")]
    for nested_key in ("result", "step_update"):
        nested = event.get(nested_key)
        if isinstance(nested, dict):
            candidates.extend([nested, nested.get("usage"), nested.get("token_usage")])

    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        p = obj.get("prompt_tokens")
        if p is None:
            p = obj.get("input_tokens")
        if p is None:
            p = obj.get("promptTokens")
        c = obj.get("completion_tokens")
        if c is None:
            c = obj.get("output_tokens")
        if c is None:
            c = obj.get("completionTokens")
        cost = obj.get("cost_usd") or obj.get("total_cost_usd") or 0.0
        if p is not None or c is not None:
            return TokenUsage(
                prompt_tokens=int(p or 0),
                completion_tokens=int(c or 0),
                cost_usd=float(cost or 0.0),
            )
    return None


def _extract_tool_call(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Detect a tool call event in flat or `step_update`-wrapped NDJSON stream output."""
    scopes: list[dict[str, Any]] = [event]
    if isinstance(event.get("step_update"), dict):
        scopes.append(event["step_update"])

    for scope in scopes:
        ev_type = str(scope.get("type") or scope.get("step_type") or "").lower()
        if ("tool" in ev_type and "result" not in ev_type) or "tool_name" in scope:
            tool_info = scope.get("tool_info") if isinstance(scope.get("tool_info"), dict) else {}
            name = str(scope.get("tool_name") or tool_info.get("name") or scope.get("name") or "")
            args = (
                scope.get("args")
                or scope.get("arguments")
                or scope.get("input")
                or tool_info.get("parameters")
                or {}
            )
            if name:
                return name, args if isinstance(args, dict) else {"raw": str(args)}

        tool_call = scope.get("tool_call") or scope.get("toolCall")
        if isinstance(tool_call, dict):
            name = str(tool_call.get("name") or tool_call.get("tool_name") or "")
            args = tool_call.get("args") or tool_call.get("arguments") or {}
            if name:
                return name, args if isinstance(args, dict) else {"raw": str(args)}
    return None


def _extract_text_delta(event: dict[str, Any]) -> str:
    """Extract incremental text chunk from `step_update.text_delta` if present."""
    step_obj = event.get("step_update")
    if isinstance(step_obj, dict):
        delta = step_obj.get("text_delta")
        if isinstance(delta, str):
            return delta
    delta = event.get("text_delta")
    return delta if isinstance(delta, str) else ""


def emit_trace(
    on_trace: Callable[[dict[str, Any]], None] | None,
    *,
    phase: str,
    kind: str,
    title: str,
    content: str = "",
    task_id: str = "",
    attempt: int = 0,
    model: str = "",
    effort: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    """Emit a structured real-time Engine <-> Antigravity interaction event."""
    if on_trace is None:
        return
    on_trace(
        {
            "time": datetime.now().strftime("%H:%M:%S"),
            "phase": phase,
            "kind": kind,
            "title": title,
            "content": content,
            "task_id": task_id,
            "attempt": attempt,
            "model": model,
            "effort": effort,
            "meta": meta or {},
        }
    )


class TextDeltaBuffer:
    """Coalesce high-frequency `step_update.text_delta` chunks into readable UI updates."""

    def __init__(
        self,
        on_trace: Callable[[dict[str, Any]], None] | None,
        *,
        phase: str,
        task_id: str = "",
        attempt: int = 0,
        model: str = "",
        effort: str = "",
        min_chars: int = 180,
        flush_interval: float = 0.3,
    ) -> None:
        self.on_trace = on_trace
        self.phase = phase
        self.task_id = task_id
        self.attempt = attempt
        self.model = model
        self.effort = effort
        self.min_chars = min_chars
        self.flush_interval = flush_interval
        self._buf: list[str] = []
        self._chars = 0
        self._last_flush = time.monotonic()

    def push(self, delta: str) -> None:
        if not delta or self.on_trace is None:
            return
        self._buf.append(delta)
        self._chars += len(delta)
        if self._chars >= self.min_chars or (time.monotonic() - self._last_flush) >= self.flush_interval:
            self.flush()

    def flush(self) -> None:
        if not self._buf or self.on_trace is None:
            return
        text = "".join(self._buf)
        self._buf.clear()
        self._chars = 0
        self._last_flush = time.monotonic()
        emit_trace(
            self.on_trace,
            phase=self.phase,
            kind="text_delta",
            title="Phản hồi từ Antigravity",
            content=text,
            task_id=self.task_id,
            attempt=self.attempt,
            model=self.model,
            effort=self.effort,
        )


def _snapshot_mcp_states(agy_binary: str) -> dict[str, bool]:
    """Query `agy mcp list` and return {server_name: is_enabled} when available."""
    proc = subprocess.run(
        [agy_binary, "mcp", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return {}
    states: dict[str, bool] = {}
    for line in proc.stdout.splitlines():
        lowered = line.lower()
        for srv in ("serena", "context7"):
            if re.search(rf"\b{srv}\b", lowered):
                is_disabled = "disabled" in lowered or "off" in lowered
                states[srv] = not is_disabled
    return states


def _set_mcp_state(agy_binary: str, server: str, enabled: bool) -> None:
    subcmd = "enable" if enabled else "disable"
    subprocess.run(
        [agy_binary, "mcp", subcmd, server],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@contextmanager
def dynamic_mcp_guard(config: EngineConfig, task: TaskSpec) -> Iterator[None]:
    """Toggle serena/context7 for the task and always restore original states in finally."""
    if not config.agy.dynamic_mcp_gating:
        yield
        return

    initial = _snapshot_mcp_states(config.agy.binary)
    if not initial:
        yield
        return

    desired = {
        "serena": task.needs_lsp_mcp,
        "context7": task.needs_docs_mcp,
    }
    changed: dict[str, bool] = {}

    def _restore() -> None:
        for srv, prev_enabled in changed.items():
            _set_mcp_state(config.agy.binary, srv, prev_enabled)

    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM) if hasattr(signal, "SIGTERM") else None
    is_main_thread = threading.current_thread() is threading.main_thread()

    def _handle_signal(signum: int, frame: Any) -> None:
        _restore()
        raise KeyboardInterrupt(f"Interrupted by signal {signum}")

    if is_main_thread:
        signal.signal(signal.SIGINT, _handle_signal)
        if prev_sigterm is not None:
            signal.signal(signal.SIGTERM, _handle_signal)

    try:
        for srv, want in desired.items():
            if srv in initial and initial[srv] != want:
                changed[srv] = initial[srv]
                _set_mcp_state(config.agy.binary, srv, want)
        yield
    finally:
        _restore()
        if is_main_thread:
            signal.signal(signal.SIGINT, prev_sigint)
            if prev_sigterm is not None:
                signal.signal(signal.SIGTERM, prev_sigterm)


class AgyCliExecutor:
    """Stateless headless executor wrapping `agy --output-format stream-json`."""

    def __init__(
        self,
        config: EngineConfig,
        on_trace: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.on_trace = on_trace

    def execute(
        self,
        prompt: str,
        task: TaskSpec,
        model: str,
        effort: str,
        *,
        attempt: int = 0,
    ) -> ExecutionResult:
        """Run one stateless `agy` session via stdin with progress-aware stream-json watchdog."""
        start = time.monotonic()
        soft_tool_budget, hard_tool_ceiling, strict_tool_cap = compute_dynamic_tool_call_budget(
            task, self.config, attempt=attempt
        )
        raw_base_timeout = self.config.executor.timeout_seconds
        focus_paths = list(dict.fromkeys(task.allowed_write_files + task.create_new_files))
        if raw_base_timeout >= 60:
            file_bonus = max(0, len(focus_paths) - 2) * 60
            effort_bonus = 120 if effort == "high" else 0
            base_timeout = raw_base_timeout + file_bonus + effort_bonus
            cfg_idle = self.config.executor.idle_timeout_seconds
            if len(task.create_new_files) >= 3:
                min_idle = 360 if effort == "high" else 300
            elif effort == "high":
                min_idle = 240
            else:
                min_idle = cfg_idle
            idle_limit = min(max(cfg_idle, min_idle), base_timeout)
        else:
            base_timeout = raw_base_timeout
            idle_limit = max(self.config.executor.idle_timeout_seconds, base_timeout)

        cmd = build_agy_command(self.config, model, effort, output_format="stream-json", timeout_override=base_timeout)
        used_model = model or "agy-default"

        emit_trace(
            self.on_trace,
            phase="executor",
            kind="prompt_sent",
            title=f"[{task.task_id}] Surgical Prompt → Antigravity",
            content=prompt,
            task_id=task.task_id,
            model=used_model,
            effort=effort,
        )
        delta_buf = TextDeltaBuffer(
            self.on_trace,
            phase="executor",
            task_id=task.task_id,
            model=used_model,
            effort=effort,
        )

        with acquire_run_lock(self.config.auto_engine_dir), dynamic_mcp_guard(self.config, task):
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.config.target_repo_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None

            try:
                proc.stdin.write(encode_stream_user_message(prompt))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

            line_queue: queue.Queue[str | None] = queue.Queue()

            def _reader() -> None:
                try:
                    for raw_line in proc.stdout:  # type: ignore[union-attr]
                        line_queue.put(raw_line)
                finally:
                    line_queue.put(None)

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            raw_lines: list[str] = []
            tool_calls = 0
            seen_tool_steps: set[int] = set()
            recent_tool_sigs: list[str] = []
            last_write_tool_call = 0
            grace_announced = False
            soft_budget_disk_mtime = 0.0
            latest_usage = TokenUsage()
            verdict_override: str | None = None
            err_msg = ""
            deadline = start + base_timeout
            hard_deadline = start + int(base_timeout * 1.5)
            last_activity = start
            last_disk_check = start
            last_disk_mtime = _latest_repo_activity_mtime(self.config.target_repo_path, focus_paths)

            while True:
                now = time.monotonic()
                if (now - last_disk_check) >= 2.5:
                    last_disk_check = now
                    cur_mtime = _latest_repo_activity_mtime(self.config.target_repo_path, focus_paths)
                    if cur_mtime > last_disk_mtime:
                        last_disk_mtime = cur_mtime
                        last_activity = now

                remaining = deadline - now
                if remaining <= 0:
                    if (tool_calls > 0 or last_activity > start + 5.0) and (now - last_activity) < 45.0 and deadline < hard_deadline:
                        deadline = min(hard_deadline, now + 90.0)
                        remaining = deadline - now
                    else:
                        kill_process_tree(proc)
                        verdict_override = "TIMEOUT"
                        err_msg = f"agy CLI timed out after {base_timeout}s"
                        break
                if base_timeout >= 60 and (now - last_activity) >= idle_limit:
                    kill_process_tree(proc)
                    verdict_override = "TIMEOUT"
                    err_msg = f"agy CLI stalled with no stdout or disk activity for {int(idle_limit)}s"
                    break
                try:
                    item = line_queue.get(timeout=min(0.5, max(0.05, remaining)))
                except queue.Empty:
                    if proc.poll() is not None and not reader_thread.is_alive():
                        break
                    delta_buf.flush()
                    continue

                if item is None:
                    break

                line = item.strip()
                if not line:
                    continue
                last_activity = time.monotonic()
                raw_lines.append(line)

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                text_delta = _extract_text_delta(event)
                if text_delta:
                    delta_buf.push(text_delta)

                usage = _extract_usage_from_event(event)
                if usage is not None:
                    latest_usage = usage
                    if latest_usage.total_tokens > self.config.limits.max_tokens_per_task:
                        kill_process_tree(proc)
                        verdict_override = "BUDGET_EXCEEDED"
                        err_msg = (
                            f"Stream watchdog aborted: {latest_usage.total_tokens} tokens > "
                            f"limit {self.config.limits.max_tokens_per_task}"
                        )
                        break

                tc = _extract_tool_call(event)
                if tc is not None:
                    tool_name, args = tc
                    step_obj = event.get("step_update")
                    step_idx = step_obj.get("step_index") if isinstance(step_obj, dict) else event.get("step_index")
                    is_new_step = not isinstance(step_idx, int) or step_idx not in seen_tool_steps
                    if is_new_step:
                        if isinstance(step_idx, int):
                            seen_tool_steps.add(step_idx)
                        tool_calls += 1
                        if tool_name.lower() in PROGRESS_WRITE_TOOL_NAMES:
                            last_write_tool_call = tool_calls
                        try:
                            args_sig = json.dumps(args, sort_keys=True, ensure_ascii=False)[:180]
                        except TypeError:
                            args_sig = str(args)[:180]
                        recent_tool_sigs.append(f"{tool_name.lower()}:{args_sig}")
                        if len(recent_tool_sigs) > 8:
                            del recent_tool_sigs[:-8]
                        delta_buf.flush()
                        emit_trace(
                            self.on_trace,
                            phase="executor",
                            kind="tool_call",
                            title=tool_name,
                            content=json.dumps(args, ensure_ascii=False)[:600],
                            task_id=task.task_id,
                            model=used_model,
                            effort=effort,
                        )
                    if self.config.agy.forbid_shell_verify_commands and tool_name.lower() in SHELL_TOOL_NAMES:
                        cmd_str = str(args.get("CommandLine") or args.get("command") or args.get("raw") or "")
                        if FORBIDDEN_SHELL_CMD_RE.search(cmd_str):
                            kill_process_tree(proc)
                            verdict_override = "MAX_TURNS_EXCEEDED"
                            err_msg = f"Stream watchdog aborted forbidden shell test/build command: {cmd_str}"
                            break
                    if not strict_tool_cap and len(recent_tool_sigs) >= 5 and len(set(recent_tool_sigs[-5:])) == 1:
                        kill_process_tree(proc)
                        verdict_override = "MAX_TURNS_EXCEEDED"
                        err_msg = (
                            f"Stream watchdog aborted stuck loop: repeated identical tool call `{tool_name}` 5 times consecutively"
                        )
                        break
                    if strict_tool_cap:
                        if tool_calls > soft_tool_budget:
                            kill_process_tree(proc)
                            verdict_override = "MAX_TURNS_EXCEEDED"
                            err_msg = (
                                f"Stream watchdog aborted: {tool_calls} tool calls > "
                                f"limit {soft_tool_budget}"
                            )
                            break
                    elif tool_calls > soft_tool_budget:
                        if soft_budget_disk_mtime == 0.0:
                            soft_budget_disk_mtime = last_disk_mtime
                        cur_disk_mtime = _latest_repo_activity_mtime(self.config.target_repo_path, focus_paths)
                        if cur_disk_mtime > last_disk_mtime:
                            last_disk_mtime = cur_disk_mtime
                        making_progress = (
                            (last_write_tool_call > 0 and (tool_calls - last_write_tool_call) <= 12)
                            or (cur_disk_mtime > soft_budget_disk_mtime)
                        )
                        if tool_calls > hard_tool_ceiling:
                            kill_process_tree(proc)
                            verdict_override = "MAX_TURNS_EXCEEDED"
                            err_msg = (
                                f"Stream watchdog aborted: {tool_calls} tool calls > "
                                f"adaptive ceiling {hard_tool_ceiling} (soft budget {soft_tool_budget})"
                            )
                            break
                        if not making_progress and (tool_calls - soft_tool_budget) >= 12:
                            kill_process_tree(proc)
                            verdict_override = "MAX_TURNS_EXCEEDED"
                            err_msg = (
                                f"Stream watchdog aborted: {tool_calls} tool calls > "
                                f"adaptive budget {soft_tool_budget} with no recent file progress"
                            )
                            break
                        if making_progress and not grace_announced:
                            grace_announced = True
                            emit_trace(
                                self.on_trace,
                                phase="executor",
                                kind="status",
                                title="Adaptive Watchdog Grace",
                                content=(
                                    f"[{task.task_id}] Đang tiến triển thực tế trên đĩa ({tool_calls}/{soft_tool_budget} tool calls) "
                                    f"→ tự động nới nhịp linh hoạt tối đa tới {hard_tool_ceiling} tool calls để Antigravity hoàn tất trọn vẹn."
                                ),
                                task_id=task.task_id,
                                model=used_model,
                                effort=effort,
                            )

            proc.wait(timeout=5)
            delta_buf.flush()
            stderr_text = proc.stderr.read().strip()
            duration = round(time.monotonic() - start, 3)

            if verdict_override:
                emit_trace(
                    self.on_trace,
                    phase="executor",
                    kind="response_done",
                    title=verdict_override,
                    content=err_msg,
                    task_id=task.task_id,
                    model=used_model,
                    effort=effort,
                    meta={"usage": latest_usage.model_dump(), "duration_sec": duration, "verdict": verdict_override},
                )
                return ExecutionResult(
                    success=False,
                    model_used=used_model,
                    effort_used=effort,
                    usage=latest_usage,
                    duration_sec=duration,
                    verdict_override=verdict_override,  # type: ignore[arg-type]
                    error_message=err_msg,
                    raw_output="\n".join(raw_lines),
                )

            ok = proc.returncode == 0
            final_err = "" if ok else (stderr_text or f"agy exited with code {proc.returncode}")
            emit_trace(
                self.on_trace,
                phase="executor",
                kind="response_done",
                title="OK" if ok else "VERIFY_FAIL",
                content=final_err or f"Hoàn tất trong {duration}s",
                task_id=task.task_id,
                model=used_model,
                effort=effort,
                meta={
                    "usage": latest_usage.model_dump(),
                    "duration_sec": duration,
                    "verdict": "OK" if ok else "VERIFY_FAIL",
                },
            )
            return ExecutionResult(
                success=ok,
                model_used=used_model,
                effort_used=effort,
                usage=latest_usage,
                duration_sec=duration,
                verdict_override=None if ok else "VERIFY_FAIL",
                error_message=final_err,
                raw_output="\n".join(raw_lines),
            )


def smoke_test_agy(config: EngineConfig) -> tuple[bool, str]:
    """Run a tiny 5-word real prompt against installed `agy` to inspect stream-json output."""
    if shutil.which(config.agy.binary) is None:
        return False, f"Binary `{config.agy.binary}` not found in PATH."

    cmd = [
        config.agy.binary,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--disable-slash-commands",
        "--print-timeout",
        "45s",
    ]
    proc = subprocess.run(
        cmd,
        input=encode_stream_user_message("Reply ONLY with OK"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if proc.returncode != 0:
        return False, f"agy smoke test failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"

    event_types: list[str] = []
    found_usage = TokenUsage()
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            ev_name = ev.get("event") or ev.get("type")
            if ev_name:
                event_types.append(str(ev_name))
            u = _extract_usage_from_event(ev)
            if u is not None:
                found_usage = u

    summary = (
        f"agy stream-json OK (events={event_types[:6]}, "
        f"prompt_tokens={found_usage.prompt_tokens}, completion_tokens={found_usage.completion_tokens})"
    )
    return True, summary


def list_agy_models(agy_binary: str = "agy") -> list[dict[str, str]]:
    """Query `agy models` and return a list of {id, label} dictionaries."""
    if shutil.which(agy_binary) is None:
        return []
    try:
        proc = subprocess.run(
            [agy_binary, "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if proc.returncode != 0:
        return []

    models: list[dict[str, str]] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("fetching available models"):
            continue
        if "\t" in line:
            model_id, label = line.split("\t", 1)
        else:
            parts = line.split(None, 1)
            model_id = parts[0]
            label = parts[1] if len(parts) > 1 else parts[0]
        if model_id.strip():
            models.append({"id": model_id.strip(), "label": label.strip()})
    return models
