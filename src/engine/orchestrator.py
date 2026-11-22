"""Layer 4/5 Orchestrator: DAG loop, complexity/effort routing, circuit breakers & crash recovery."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from engine.bridges.agy_cli_executor import AgyCliExecutor, emit_trace
from engine.bridges.agy_sdk_executor import AgySdkExecutor
from engine.bridges.cli_executor import CliFallbackExecutor
from engine.compiler import PromptCompiler, PromptTooLargeError
from engine.database import StateDB
from engine.git_guard import GitGuard
from engine.models import EngineConfig, ExecutionResult, TaskSpec, TokenUsage
from engine.verifier import Verifier


class ExecutorProtocol(Protocol):
    """Unified stateless executor contract across CLI, SDK, and fallback bridges."""

    def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult: ...


class PlanNotApprovedError(RuntimeError):
    """Raised when attempting to execute a sprint before Plan Approval Gate."""


def build_executor(
    config: EngineConfig,
    on_trace: Callable[[dict[str, Any]], None] | None = None,
) -> ExecutorProtocol:
    """Instantiate configured execution bridge."""
    mode = config.executor.mode
    if mode == "agy_cli":
        return AgyCliExecutor(config, on_trace=on_trace)
    if mode == "agy_sdk":
        return AgySdkExecutor(config)
    if mode in ("aider", "opencode"):
        return CliFallbackExecutor(config)
    raise ValueError(f"Executor mode `{mode}` cannot run headless orchestrator; use `engine serve-mcp`.")


_NEED_INPUT_LINE_RE = re.compile(r"(?im)^NEED_USER_INPUT:\s*(.+)$")


def _extract_need_user_input(raw_output: str) -> list[str]:
    """Extract unique `NEED_USER_INPUT: <question>` lines from plain or NDJSON stream-json output."""
    if not raw_output or "NEED_USER_INPUT:" not in raw_output:
        return []

    stream_deltas: list[str] = []
    result_text = ""
    plain_lines: list[str] = []
    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            plain_lines.append(raw_line)
            continue
        if not isinstance(ev, dict):
            continue
        step = ev.get("step_update")
        if isinstance(step, dict) and isinstance(step.get("text_delta"), str):
            stream_deltas.append(step["text_delta"])
        res_obj = ev.get("result")
        if isinstance(res_obj, dict) and isinstance(res_obj.get("response"), str):
            result_text = res_obj["response"]

    candidate = result_text or "".join(stream_deltas) or "\n".join(plain_lines) or raw_output
    seen: list[str] = []
    for m in _NEED_INPUT_LINE_RE.finditer(candidate):
        q = m.group(1).strip()
        if q and q not in seen:
            seen.append(q)
    return seen


class Orchestrator:
    """Coordinates stateless task execution with multi-layer anti-loop circuit breakers."""

    def __init__(
        self,
        config: EngineConfig,
        db: StateDB,
        guard: GitGuard,
        verifier: Verifier,
        compiler: PromptCompiler,
        executor: ExecutorProtocol | None = None,
        on_event: Callable[[str, str], None] | None = None,
        stop_event: threading.Event | None = None,
        on_trace: Callable[[dict[str, Any]], None] | None = None,
        ask_user: Callable[[str, str, list[str]], dict[str, str] | None] | None = None,
        executor_factory: Callable[[EngineConfig], ExecutorProtocol] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.guard = guard
        self.verifier = verifier
        self.compiler = compiler
        self.on_trace = on_trace
        self.ask_user = ask_user
        self.executor_factory = executor_factory
        self._executor_injected = executor is not None
        self.executor = executor or (
            executor_factory(config) if executor_factory is not None else build_executor(config, on_trace=on_trace)
        )
        self.on_event = on_event
        self.stop_event = stop_event
        self._db_lock = threading.Lock()

    @property
    def _user_answers_path(self) -> Path:
        return self.config.auto_engine_dir / "user_answers.json"

    def _load_user_answers(self) -> dict[str, str]:
        if not self._user_answers_path.is_file():
            return {}
        try:
            data = json.loads(self._user_answers_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if str(v).strip()}
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_user_answers(self, new_answers: dict[str, str]) -> None:
        merged = self._load_user_answers()
        for k, v in new_answers.items():
            if k.strip() and str(v).strip():
                merged[k.strip()] = str(v).strip()
        self.config.auto_engine_dir.mkdir(parents=True, exist_ok=True)
        self._user_answers_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _prompt_user_for_task(
        self,
        task: TaskSpec,
        unanswered: list[str],
        *,
        checkpoint: str,
        retry_count: int = 0,
        guard: GitGuard | None = None,
    ) -> bool:
        """Pause and collect real user input; returns False if stopped during waiting."""
        assert self.ask_user is not None
        active_guard = guard or self.guard
        emit_trace(
            self.on_trace,
            phase="orchestrator",
            kind="user_question",
            title=f"[{task.task_id}] Cần bạn cung cấp thông tin thật trước khi code",
            content="\n".join(f"- {q}" for q in unanswered),
            task_id=task.task_id,
            meta={"questions": unanswered},
        )
        self._emit("warn", f"[{task.task_id}] Tạm dừng chờ người dùng trả lời {len(unanswered)} câu hỏi...")
        answers = self.ask_user(task.task_id, task.title, unanswered)
        if self.stop_event is not None and self.stop_event.is_set():
            active_guard.hard_rollback(checkpoint)
            with self._db_lock:
                self.db.update_task_status(task.task_id, "PENDING", retry_count=retry_count)
            return False
        if answers:
            clean_answers = {k: str(v).strip() for k, v in answers.items() if str(v).strip()}
            if clean_answers:
                with self._db_lock:
                    self._save_user_answers(clean_answers)
                emit_trace(
                    self.on_trace,
                    phase="orchestrator",
                    kind="user_answer",
                    title=f"[{task.task_id}] Đã nhận câu trả lời từ người dùng",
                    content=json.dumps(clean_answers, ensure_ascii=False),
                    task_id=task.task_id,
                )
        return True

    def _emit(self, level: str, message: str) -> None:
        if self.on_event is not None:
            self.on_event(level, message)

    def _select_tier(self, task: TaskSpec, attempt: int, force_escalation: bool) -> tuple[str, str, str]:
        """Select (tier_name, model, effort) based on complexity, retry index, and escalation."""
        tiers = self.config.agy.tiers
        if force_escalation or attempt >= self.config.limits.max_retries_per_task or task.complexity == "high":
            t_name = "escalation"
        elif attempt == 0 and task.complexity == "low":
            t_name = "coder_low"
        else:
            t_name = "coder_medium"

        if self.config.executor.mode in ("aider", "opencode") and self.config.gateway is not None:
            gw_key = "escalation" if t_name == "escalation" else "executor"
            model = self.config.gateway.models.get(gw_key, "tier-coder-fast")
            return t_name, model, tiers[t_name].effort if t_name in tiers else "medium"

        tier_cfg = tiers.get(t_name) or tiers.get("coder_medium")
        model = tier_cfg.model if tier_cfg else ""
        effort = tier_cfg.effort if tier_cfg else "medium"
        return t_name, model, effort

    def _notify_webhook(self, payload: dict) -> None:
        url = os.environ.get(self.config.notifications.webhook_url_env, "").strip()
        if not url:
            return
        try:
            httpx.post(url, json=payload, timeout=10.0)
        except httpx.HTTPError:
            pass

    def resume(self) -> int:
        """Recover stale RUNNING/VERIFYING tasks back to clean checkpoints."""
        stale = self.db.get_stale_active_tasks()
        recovered = 0
        for row in stale:
            task_id = row["task_id"]
            checkpoint = row["checkpoint_sha"]
            if checkpoint:
                self.guard.hard_rollback(checkpoint)
            self.db.record_run(
                task_id=task_id,
                attempt=int(row["retry_count"]),
                model_used="recovery",
                effort_used="none",
                usage=TokenUsage(),
                duration_sec=0.0,
                verdict="CRASH_RECOVERED",
                trimmed_error_log="Recovered from unclean exit and rolled back to checkpoint.",
            )
            self.db.update_task_status(task_id, "PENDING", retry_count=0)
            recovered += 1
        return recovered

    def _run_single_task(self, task: TaskSpec) -> tuple[bool, bool]:
        """Run a single task in the primary workspace. Returns (passed, stopped_mid_task)."""
        passed, stopped, _sha = self._run_single_task_with(
            task,
            guard=self.guard,
            verifier=self.verifier,
            compiler=self.compiler,
            executor=self.executor,
            defer_done=False,
        )
        return passed, stopped

    def _run_single_task_with(
        self,
        task: TaskSpec,
        *,
        guard: GitGuard,
        verifier: Verifier,
        compiler: PromptCompiler,
        executor: ExecutorProtocol,
        defer_done: bool = False,
    ) -> tuple[bool, bool, str | None]:
        """Run a single task through retries inside `guard`'s repo/worktree."""
        checkpoint = guard.create_checkpoint()
        with self._db_lock:
            self.db.update_task_status(task.task_id, "RUNNING", checkpoint_sha=checkpoint)

        prev_error_hash: str | None = None
        last_error = ""
        last_verdict: str | None = None
        force_escalation = False
        task_tokens = 0
        dynamic_asks = 0

        if task.user_questions and self.ask_user is not None:
            with self._db_lock:
                saved_answers = self._load_user_answers()
            unanswered = [q.strip() for q in task.user_questions if q.strip() and q.strip() not in saved_answers]
            if unanswered and not self._prompt_user_for_task(
                task, unanswered, checkpoint=checkpoint, retry_count=0, guard=guard
            ):
                return False, True, None

        for attempt in range(self.config.limits.max_retries_per_task + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                self._emit("warn", f"[{task.task_id}] Stopped by user before attempt {attempt}.")
                guard.hard_rollback(checkpoint)
                with self._db_lock:
                    self.db.update_task_status(task.task_id, "PENDING", retry_count=attempt)
                return False, True, None

            if task_tokens >= self.config.limits.max_tokens_per_task:
                with self._db_lock:
                    self.db.record_run(
                        task.task_id,
                        attempt,
                        "budget-guard",
                        "none",
                        TokenUsage(),
                        0.0,
                        "BUDGET_EXCEEDED",
                        trimmed_error_log=f"Task token budget exhausted ({task_tokens}).",
                    )
                break

            tier_name, model, effort = self._select_tier(task, attempt, force_escalation)
            self._emit("info", f"[{task.task_id}] Attempt {attempt} | tier={tier_name} effort={effort}")

            exec_res: ExecutionResult | None = None
            for _dyn in range(3):
                compact_diff = guard.get_compact_diff(task.allowed_write_files) if attempt > 0 else ""

                try:
                    prompt, est_tok = compiler.compile(
                        task,
                        attempt=attempt,
                        last_error=last_error,
                        last_verdict=last_verdict,
                        compact_diff=compact_diff,
                    )
                except PromptTooLargeError as exc:
                    last_error = str(exc)
                    self._emit("error", f"[{task.task_id}] {last_error}")
                    with self._db_lock:
                        self.db.update_task_status(task.task_id, "BLOCKED", retry_count=attempt, last_error=last_error)
                    return False, False, None

                self._emit("info", f"[{task.task_id}] Executing prompt (~{est_tok} tokens)...")
                exec_res = executor.execute(prompt, task, model, effort)
                task_tokens += exec_res.usage.total_tokens

                dyn_questions = _extract_need_user_input(exec_res.raw_output)
                if dyn_questions and self.ask_user is not None and dynamic_asks < 2:
                    with self._db_lock:
                        saved_answers = self._load_user_answers()
                    unanswered_dyn = [q for q in dyn_questions if q not in saved_answers]
                    if unanswered_dyn:
                        dynamic_asks += 1
                        guard.hard_rollback(checkpoint)
                        if not self._prompt_user_for_task(
                            task, unanswered_dyn, checkpoint=checkpoint, retry_count=attempt, guard=guard
                        ):
                            return False, True, None
                        continue
                break
            assert exec_res is not None

            # Short-circuit if stream watchdog, CLI error, or SDK aborted execution early.
            if not exec_res.success and exec_res.verdict_override in (
                "MAX_TURNS_EXCEEDED",
                "BUDGET_EXCEEDED",
                "TIMEOUT",
                "SCOPE_VIOLATION",
                "VERIFY_FAIL",
            ):
                last_verdict = exec_res.verdict_override
                last_error = exec_res.error_message or f"Executor failed ({exec_res.verdict_override})"
                self._emit("warn", f"[{task.task_id}] {last_verdict}: {last_error[:240]}")
                with self._db_lock:
                    self.db.record_run(
                        task.task_id,
                        attempt,
                        exec_res.model_used,
                        exec_res.effort_used,
                        exec_res.usage,
                        exec_res.duration_sec,
                        exec_res.verdict_override,
                        trimmed_error_log=last_error,
                    )
                if exec_res.verdict_override in ("SCOPE_VIOLATION", "BUDGET_EXCEEDED", "MAX_TURNS_EXCEEDED", "TIMEOUT"):
                    guard.hard_rollback(checkpoint)
                if exec_res.verdict_override == "BUDGET_EXCEEDED":
                    break
                if exec_res.verdict_override == "MAX_TURNS_EXCEEDED" and tier_name == "escalation":
                    break
                force_escalation = True
                with self._db_lock:
                    self.db.update_task_status(task.task_id, "RUNNING", retry_count=attempt + 1, last_error=last_error)
                continue

            with self._db_lock:
                self.db.update_task_status(task.task_id, "VERIFYING")
            verify_res = verifier.verify(task)
            emit_trace(
                self.on_trace,
                phase="verifier",
                kind="verify_result",
                title=f"[{task.task_id}] Verify: {verify_res.verdict}",
                content=verify_res.trimmed_error_log or "Scope Guard, Anti-Demo & Verify commands đều PASS.",
                task_id=task.task_id,
                attempt=attempt,
                meta={"passed": verify_res.passed, "verdict": verify_res.verdict},
            )

            if verify_res.passed:
                commit_sha = guard.commit_task(
                    task.task_id,
                    task.title,
                    task.allowed_write_files + task.create_new_files,
                    stage_all_changed=(not task.strict_scope or self.config.guards.scope_mode in ("soft", "flexible")),
                )
                with self._db_lock:
                    self.db.record_run(
                        task.task_id,
                        attempt,
                        exec_res.model_used,
                        exec_res.effort_used,
                        exec_res.usage,
                        exec_res.duration_sec,
                        "PASS",
                    )
                    if not defer_done:
                        self.db.update_task_status(task.task_id, "DONE", commit_sha=commit_sha, retry_count=attempt)
                if not defer_done:
                    self._emit("success", f"[{task.task_id}] PASS -> Committed {commit_sha[:8]}")
                return True, False, commit_sha

            self._emit("warn", f"[{task.task_id}] Attempt {attempt} failed: {verify_res.verdict}")

            # Handle failure verdicts
            if verify_res.verdict in ("SCOPE_VIOLATION", "TEST_GAMING"):
                # Persist violation summary BEFORE rolling back dirty files.
                last_error = verify_res.trimmed_error_log
                last_verdict = verify_res.verdict
                with self._db_lock:
                    self.db.record_run(
                        task.task_id,
                        attempt,
                        exec_res.model_used,
                        exec_res.effort_used,
                        exec_res.usage,
                        exec_res.duration_sec,
                        verify_res.verdict,
                        error_hash=verify_res.error_hash,
                        trimmed_error_log=last_error,
                    )
                guard.hard_rollback(checkpoint)
                with self._db_lock:
                    self.db.update_task_status(
                        task.task_id,
                        "RUNNING",
                        retry_count=attempt + 1,
                        last_error=last_error,
                        last_error_hash=verify_res.error_hash,
                    )
                continue

            if verify_res.verdict == "EMPTY_DIFF":
                last_error = verify_res.trimmed_error_log
                last_verdict = "EMPTY_DIFF"
                with self._db_lock:
                    self.db.record_run(
                        task.task_id,
                        attempt,
                        exec_res.model_used,
                        exec_res.effort_used,
                        exec_res.usage,
                        exec_res.duration_sec,
                        "EMPTY_DIFF",
                        error_hash=verify_res.error_hash,
                        trimmed_error_log=last_error,
                    )
                if tier_name == "escalation":
                    break
                force_escalation = True
                with self._db_lock:
                    self.db.update_task_status(task.task_id, "RUNNING", retry_count=attempt + 1, last_error=last_error)
                continue

            # VERIFY_FAIL or TIMEOUT: detect identical error loop
            if prev_error_hash and verify_res.error_hash == prev_error_hash:
                last_error = verify_res.trimmed_error_log
                last_verdict = "IDENTICAL_ERROR"
                with self._db_lock:
                    self.db.record_run(
                        task.task_id,
                        attempt,
                        exec_res.model_used,
                        exec_res.effort_used,
                        exec_res.usage,
                        exec_res.duration_sec,
                        "IDENTICAL_ERROR",
                        error_hash=verify_res.error_hash,
                        trimmed_error_log=last_error,
                    )
                if tier_name == "escalation":
                    break
                force_escalation = True
                with self._db_lock:
                    self.db.update_task_status(
                        task.task_id,
                        "RUNNING",
                        retry_count=attempt + 1,
                        last_error=last_error,
                        last_error_hash=verify_res.error_hash,
                    )
                continue

            prev_error_hash = verify_res.error_hash
            last_error = verify_res.trimmed_error_log
            last_verdict = verify_res.verdict
            with self._db_lock:
                self.db.record_run(
                    task.task_id,
                    attempt,
                    exec_res.model_used,
                    exec_res.effort_used,
                    exec_res.usage,
                    exec_res.duration_sec,
                    verify_res.verdict,
                    error_hash=verify_res.error_hash,
                    trimmed_error_log=last_error,
                )
                self.db.update_task_status(
                    task.task_id,
                    "RUNNING",
                    retry_count=attempt + 1,
                    last_error=last_error,
                    last_error_hash=verify_res.error_hash,
                )

        guard.hard_rollback(checkpoint)
        with self._db_lock:
            self.db.update_task_status(task.task_id, "BLOCKED", last_error=last_error)
        return False, False, None

    def _run_parallel_batch(self, tasks: list[TaskSpec]) -> tuple[int, int, bool]:
        """Execute independent file-disjoint tasks concurrently in isolated git worktrees, then merge."""
        self._emit("info", f"Chạy song song {len(tasks)} task độc lập trong git worktree: {[t.task_id for t in tasks]}")
        work_items: list[tuple[TaskSpec, Path, str, EngineConfig, GitGuard, Verifier, PromptCompiler, ExecutorProtocol]] = []

        for task in tasks:
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", task.task_id)
            wt_dir = self.config.auto_engine_dir / "worktrees" / safe_id
            wt_branch = f"auto-wt-{safe_id}"
            self.guard.create_worktree(wt_dir, wt_branch)

            wt_cfg = self.config.model_copy(deep=True)
            wt_cfg.project.target_repo = str(wt_dir)
            wt_cfg.agy.dynamic_mcp_gating = False
            wt_auto = wt_dir / ".auto-engine"
            wt_auto.mkdir(parents=True, exist_ok=True)
            if self._user_answers_path.is_file():
                shutil.copyfile(self._user_answers_path, wt_auto / "user_answers.json")

            wt_guard = GitGuard(
                wt_dir,
                ignore_artifacts=wt_cfg.guards.ignore_artifacts,
                forbid_demo_patterns=wt_cfg.guards.forbid_demo_patterns,
                enforce_production_only=wt_cfg.guards.enforce_production_only,
            )
            wt_verifier = Verifier(wt_cfg, wt_guard)
            wt_compiler = PromptCompiler(wt_cfg)
            wt_exec = (
                self.executor_factory(wt_cfg)
                if self.executor_factory is not None
                else build_executor(wt_cfg, on_trace=self.on_trace)
            )
            work_items.append((task, wt_dir, wt_branch, wt_cfg, wt_guard, wt_verifier, wt_compiler, wt_exec))

        passed_cnt = 0
        blocked_cnt = 0
        stopped_any = False

        try:
            outcomes: list[tuple[TaskSpec, str, bool, bool, str | None]] = []
            with ThreadPoolExecutor(max_workers=len(work_items)) as pool:
                futures = [
                    (
                        item[0],
                        item[2],
                        pool.submit(
                            self._run_single_task_with,
                            item[0],
                            guard=item[4],
                            verifier=item[5],
                            compiler=item[6],
                            executor=item[7],
                            defer_done=True,
                        ),
                    )
                    for item in work_items
                ]
                for task, wt_branch, fut in futures:
                    passed, stopped, sha = fut.result()
                    outcomes.append((task, wt_branch, passed, stopped, sha))

            for task, wt_branch, passed, stopped, _sha in outcomes:
                if stopped:
                    stopped_any = True
                    continue
                if not passed:
                    blocked_cnt += 1
                    continue
                ok, merge_err = self.guard.merge_branch_into_current(wt_branch)
                if ok:
                    merged_sha = self.guard.create_checkpoint()
                    with self._db_lock:
                        self.db.update_task_status(task.task_id, "DONE", commit_sha=merged_sha)
                    self._emit("success", f"[{task.task_id}] Parallel PASS -> Merged {merged_sha[:8]}")
                    passed_cnt += 1
                else:
                    self._emit("warn", f"[{task.task_id}] Merge conflict ({merge_err[:120]}); retrying sequentially.")
                    with self._db_lock:
                        self.db.update_task_status(task.task_id, "PENDING", retry_count=0)
        finally:
            for _task, wt_dir, wt_branch, *_rest in work_items:
                self.guard.remove_worktree(wt_dir, wt_branch)
            wt_root = self.config.auto_engine_dir / "worktrees"
            if wt_root.is_dir() and not any(wt_root.iterdir()):
                shutil.rmtree(wt_root, ignore_errors=True)

        return passed_cnt, blocked_cnt, stopped_any

    def run_sprint(self, *, auto_approve: bool = False, sprint_name: str = "sprint-1") -> dict:
        """Execute ready tasks in DAG order until completion, budget cap, or Global Circuit Breaker."""
        approved = self.db.get_meta("plan_approved", "0")
        if self.config.sprint.require_plan_approval and approved != "1" and not auto_approve:
            raise PlanNotApprovedError("Plan is not approved yet. Run `engine approve` or pass `--auto-approve`.")

        recovered = self.resume()
        self.guard.ensure_git_exclude()
        work_branch = f"{self.config.project.work_branch_prefix}{sprint_name}"
        self.guard.ensure_work_branch(self.config.project.base_branch, work_branch)

        consecutive_blocked = int(self.db.get_meta("consecutive_blocked", "0") or "0")
        halted_reason: str | None = None

        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                halted_reason = "STOPPED_BY_USER"
                self._emit("warn", "Sprint halted cleanly by user stop request.")
                break
            if self.db.get_total_cost_usd() >= self.config.limits.max_budget_usd_per_sprint:
                halted_reason = "SPRINT_BUDGET_EXCEEDED"
                break
            if consecutive_blocked >= self.config.limits.max_consecutive_blocked_tasks:
                halted_reason = "GLOBAL_CIRCUIT_BREAKER"
                break

            allow_parallel = (
                self.config.sprint.max_parallel_tasks > 1
                and (not self._executor_injected or self.executor_factory is not None)
            )
            limit = self.config.sprint.max_parallel_tasks if allow_parallel else 1
            ready_batch = self.db.get_ready_tasks(limit=limit)
            if not ready_batch:
                break

            if len(ready_batch) >= 2:
                passed_cnt, blocked_cnt, stopped_mid_task = self._run_parallel_batch(ready_batch)
                if stopped_mid_task:
                    halted_reason = "STOPPED_BY_USER"
                    break
                if passed_cnt > 0:
                    consecutive_blocked = blocked_cnt
                else:
                    consecutive_blocked += blocked_cnt
                self.db.set_meta("consecutive_blocked", str(consecutive_blocked))
                if consecutive_blocked >= self.config.limits.max_consecutive_blocked_tasks:
                    halted_reason = "GLOBAL_CIRCUIT_BREAKER"
                    self._notify_webhook(
                        {"event": "SPRINT_HALTED", "reason": halted_reason, "consecutive_blocked": consecutive_blocked}
                    )
                    break
                continue

            task = ready_batch[0]
            passed, stopped_mid_task = self._run_single_task(task)
            if stopped_mid_task:
                halted_reason = "STOPPED_BY_USER"
                break
            if passed:
                consecutive_blocked = 0
                self.db.set_meta("consecutive_blocked", "0")
            else:
                consecutive_blocked += 1
                self.db.set_meta("consecutive_blocked", str(consecutive_blocked))
                self._notify_webhook(
                    {"event": "TASK_BLOCKED", "task_id": task.task_id, "consecutive_blocked": consecutive_blocked}
                )
                if consecutive_blocked >= self.config.limits.max_consecutive_blocked_tasks:
                    halted_reason = "GLOBAL_CIRCUIT_BREAKER"
                    self._notify_webhook(
                        {"event": "SPRINT_HALTED", "reason": halted_reason, "consecutive_blocked": consecutive_blocked}
                    )
                    break

        summary = self.db.get_sprint_summary()
        final_verify_passed = True
        final_verify_error = ""

        if (
            halted_reason is None
            and summary["counts"]["DONE"] > 0
            and self.config.sprint.final_verify_commands
        ):
            gate_res = self.verifier.run_commands(self.config.sprint.final_verify_commands)
            final_verify_passed = gate_res.passed
            final_verify_error = gate_res.trimmed_error_log
            emit_trace(
                self.on_trace,
                phase="verifier",
                kind="verify_result",
                title=f"[FINAL_GATE] {gate_res.verdict}",
                content=final_verify_error or "Final Sprint Gate PASS.",
                meta={"passed": gate_res.passed, "verdict": gate_res.verdict},
            )

        if (
            halted_reason is None
            and final_verify_passed
            and summary["counts"]["DONE"] > 0
            and self.config.sprint.runtime_verify_commands
        ):
            rt_res = self.verifier.run_commands(self.config.sprint.runtime_verify_commands)
            final_verify_passed = rt_res.passed
            if not rt_res.passed:
                final_verify_error = rt_res.trimmed_error_log
            emit_trace(
                self.on_trace,
                phase="verifier",
                kind="verify_result",
                title=f"[RUNTIME_GATE] {rt_res.verdict}",
                content=rt_res.trimmed_error_log or "Runtime Smoke Gate PASS.",
                meta={"passed": rt_res.passed, "verdict": rt_res.verdict},
            )

        if halted_reason is None and final_verify_passed:
            self._notify_webhook({"event": "SPRINT_DONE", "summary": summary})

        return {
            "recovered_tasks": recovered,
            "halted_reason": halted_reason,
            "final_verify_passed": final_verify_passed,
            "final_verify_error": final_verify_error,
            "summary": summary,
        }
