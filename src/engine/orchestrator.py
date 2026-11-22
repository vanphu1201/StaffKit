"""Layer 4/5 Orchestrator: DAG loop, complexity/effort routing, circuit breakers & crash recovery."""

from __future__ import annotations

import os
import threading
from typing import Callable, Protocol

import httpx

from engine.bridges.agy_cli_executor import AgyCliExecutor
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


def build_executor(config: EngineConfig) -> ExecutorProtocol:
    """Instantiate configured execution bridge."""
    mode = config.executor.mode
    if mode == "agy_cli":
        return AgyCliExecutor(config)
    if mode == "agy_sdk":
        return AgySdkExecutor(config)
    if mode in ("aider", "opencode"):
        return CliFallbackExecutor(config)
    raise ValueError(f"Executor mode `{mode}` cannot run headless orchestrator; use `engine serve-mcp`.")


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
    ) -> None:
        self.config = config
        self.db = db
        self.guard = guard
        self.verifier = verifier
        self.compiler = compiler
        self.executor = executor or build_executor(config)
        self.on_event = on_event
        self.stop_event = stop_event

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
        """Run a single task through retries. Returns (passed, budget_halted)."""
        checkpoint = self.guard.create_checkpoint()
        self.db.update_task_status(task.task_id, "RUNNING", checkpoint_sha=checkpoint)

        prev_error_hash: str | None = None
        last_error = ""
        last_verdict: str | None = None
        force_escalation = False
        task_tokens = 0

        for attempt in range(self.config.limits.max_retries_per_task + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                self._emit("warn", f"[{task.task_id}] Stopped by user before attempt {attempt}.")
                self.guard.hard_rollback(checkpoint)
                self.db.update_task_status(task.task_id, "PENDING", retry_count=attempt)
                return False, True

            if task_tokens >= self.config.limits.max_tokens_per_task:
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
            compact_diff = self.guard.get_compact_diff(task.allowed_write_files) if attempt > 0 else ""

            try:
                prompt, est_tok = self.compiler.compile(
                    task,
                    attempt=attempt,
                    last_error=last_error,
                    last_verdict=last_verdict,
                    compact_diff=compact_diff,
                )
            except PromptTooLargeError as exc:
                last_error = str(exc)
                self._emit("error", f"[{task.task_id}] {last_error}")
                self.db.update_task_status(task.task_id, "BLOCKED", retry_count=attempt, last_error=last_error)
                return False, False

            self._emit("info", f"[{task.task_id}] Executing prompt (~{est_tok} tokens)...")
            exec_res = self.executor.execute(prompt, task, model, effort)
            task_tokens += exec_res.usage.total_tokens

            # Short-circuit if stream watchdog or SDK aborted execution early.
            if exec_res.verdict_override in ("MAX_TURNS_EXCEEDED", "BUDGET_EXCEEDED", "TIMEOUT", "SCOPE_VIOLATION"):
                last_verdict = exec_res.verdict_override
                last_error = exec_res.error_message
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
                if exec_res.verdict_override == "SCOPE_VIOLATION":
                    self.guard.hard_rollback(checkpoint)
                if exec_res.verdict_override == "BUDGET_EXCEEDED":
                    break
                if exec_res.verdict_override == "MAX_TURNS_EXCEEDED" and tier_name == "escalation":
                    break
                force_escalation = True
                self.db.update_task_status(task.task_id, "RUNNING", retry_count=attempt + 1, last_error=last_error)
                continue

            self.db.update_task_status(task.task_id, "VERIFYING")
            verify_res = self.verifier.verify(task)

            if verify_res.passed:
                self.db.record_run(
                    task.task_id,
                    attempt,
                    exec_res.model_used,
                    exec_res.effort_used,
                    exec_res.usage,
                    exec_res.duration_sec,
                    "PASS",
                )
                commit_sha = self.guard.commit_task(
                    task.task_id,
                    task.title,
                    task.allowed_write_files + task.create_new_files,
                )
                self.db.update_task_status(task.task_id, "DONE", commit_sha=commit_sha, retry_count=attempt)
                self._emit("success", f"[{task.task_id}] PASS -> Committed {commit_sha[:8]}")
                return True, False

            self._emit("warn", f"[{task.task_id}] Attempt {attempt} failed: {verify_res.verdict}")

            # Handle failure verdicts
            if verify_res.verdict in ("SCOPE_VIOLATION", "TEST_GAMING"):
                # Persist violation summary BEFORE rolling back dirty files.
                last_error = verify_res.trimmed_error_log
                last_verdict = verify_res.verdict
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
                self.guard.hard_rollback(checkpoint)
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
                self.db.update_task_status(task.task_id, "RUNNING", retry_count=attempt + 1, last_error=last_error)
                continue

            # VERIFY_FAIL or TIMEOUT: detect identical error loop
            if prev_error_hash and verify_res.error_hash == prev_error_hash:
                last_error = verify_res.trimmed_error_log
                last_verdict = "IDENTICAL_ERROR"
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

        self.guard.hard_rollback(checkpoint)
        self.db.update_task_status(task.task_id, "BLOCKED", last_error=last_error)
        return False, False

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

            task = self.db.get_next_ready_task()
            if task is None:
                break

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

        if halted_reason is None and final_verify_passed:
            self._notify_webhook({"event": "SPRINT_DONE", "summary": summary})

        return {
            "recovered_tasks": recovered,
            "halted_reason": halted_reason,
            "final_verify_passed": final_verify_passed,
            "final_verify_error": final_verify_error,
            "summary": summary,
        }
