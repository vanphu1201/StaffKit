"""Mode B: Minimal 4-tool FastMCP Server for Antigravity IDE / App (logs strictly to stderr)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastmcp import FastMCP

from engine.compiler import PromptCompiler
from engine.database import StateDB
from engine.git_guard import GitGuard
from engine.models import EngineConfig, TokenUsage
from engine.verifier import Verifier

# Ensure stdio MCP transport remains completely clean by routing all logs to stderr.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[auto-prompt-engine] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_mcp_server(config_path: Path) -> FastMCP:
    """Instantiate FastMCP server bound to config_path with exactly 4 minimal tools."""
    config = EngineConfig.load(config_path)
    db = StateDB(config.state_db_path)
    guard = GitGuard(config.target_repo_path, ignore_artifacts=config.guards.ignore_artifacts)
    verifier = Verifier(config, guard)
    compiler = PromptCompiler(config)

    mcp = FastMCP("auto-prompt-engine")

    @mcp.tool()
    def get_next_task() -> str:
        """Pull the next READY task, create a clean Git checkpoint, and return a Surgical Prompt."""
        approved = db.get_meta("plan_approved", "0")
        if config.sprint.require_plan_approval and approved != "1":
            return "BLOCKED: Plan is not approved yet. Run `engine approve` first."

        task = db.get_next_ready_task()
        if task is None:
            summary = db.get_sprint_summary()
            return f"NO_READY_TASKS: Summary = {summary['counts']}"

        row = db.get_task_row(task.task_id)
        attempt = int(row["retry_count"]) if row else 0
        last_error = str(row["last_error"] or "") if row else ""

        checkpoint = guard.create_checkpoint()
        db.update_task_status(task.task_id, "RUNNING", checkpoint_sha=checkpoint)

        compact_diff = guard.get_compact_diff(task.allowed_write_files) if attempt > 0 else ""
        prompt, est_tokens = compiler.compile(
            task,
            attempt=attempt,
            last_error=last_error,
            compact_diff=compact_diff,
        )
        logger.info("Dispatched task %s (attempt=%d, ~%d tokens)", task.task_id, attempt, est_tokens)
        return (
            f"{prompt}\n\n"
            "---\n"
            f"[MCP REMINDER] Hoàn thành xong task `{task.task_id}`, hãy gọi `submit_and_verify(task_id=\"{task.task_id}\")`, "
            "sau đó mở chat mới trước khi nhận task tiếp theo để giữ token ở mức thấp nhất."
        )

    @mcp.tool()
    def submit_and_verify(task_id: str) -> str:
        """Verify task changes (Empty Diff -> Scope -> Anti-Gaming -> rtk Commands) and commit or return trimmed error."""
        task = db.get_task_spec(task_id)
        row = db.get_task_row(task_id)
        if not task or not row:
            return f"ERROR: Unknown task_id `{task_id}`."

        db.update_task_status(task_id, "VERIFYING")
        checkpoint = row["checkpoint_sha"] or guard.create_checkpoint()
        attempt = int(row["retry_count"])
        result = verifier.verify(task)

        db.record_run(
            task_id=task_id,
            attempt=attempt,
            model_used="antigravity-ide",
            effort_used="interactive",
            usage=TokenUsage(),
            duration_sec=0.0,
            verdict=result.verdict,
            error_hash=result.error_hash,
            trimmed_error_log=result.trimmed_error_log or None,
        )

        if result.passed:
            sha = guard.commit_task(task.task_id, task.title, task.allowed_write_files + task.create_new_files)
            db.update_task_status(task_id, "DONE", commit_sha=sha)
            return f"PASS - Committed {sha}. Hãy mở phiên chat mới trước khi gọi `get_next_task()`."

        if result.verdict in ("SCOPE_VIOLATION", "TEST_GAMING"):
            guard.hard_rollback(checkpoint)

        next_retry = attempt + 1
        if next_retry > config.limits.max_retries_per_task:
            guard.hard_rollback(checkpoint)
            db.update_task_status(task_id, "BLOCKED", retry_count=next_retry, last_error=result.trimmed_error_log)
            return (
                f"BLOCKED ({result.verdict}): Vượt quá {config.limits.max_retries_per_task} lần retry. "
                f"Đã rollback về {checkpoint[:8]}.\n{result.trimmed_error_log}"
            )

        db.update_task_status(
            task_id,
            "RUNNING",
            retry_count=next_retry,
            last_error=result.trimmed_error_log,
            last_error_hash=result.error_hash,
        )
        return (
            f"FAIL ({result.verdict}) - Retry {next_retry}/{config.limits.max_retries_per_task}:\n"
            f"{result.trimmed_error_log}"
        )

    @mcp.tool()
    def get_sprint_status() -> str:
        """Return summary of DONE / PENDING / BLOCKED tasks and token consumption."""
        summary = db.get_sprint_summary()
        return (
            f"Sprint Status: {summary['counts']} | "
            f"Tokens: prompt={summary['prompt_tokens']}, completion={summary['completion_tokens']} | "
            f"Cost: ${summary['cost_usd']:.4f}"
        )

    @mcp.tool()
    def rollback_current_task(task_id: str, reason: str) -> str:
        """Rollback task to its clean checkpoint SHA and mark it BLOCKED."""
        row = db.get_task_row(task_id)
        if not row:
            return f"ERROR: Unknown task_id `{task_id}`."
        checkpoint = row["checkpoint_sha"]
        if checkpoint:
            guard.hard_rollback(checkpoint)
        db.update_task_status(task_id, "BLOCKED", last_error=reason)
        return f"ROLLED_BACK `{task_id}` to {checkpoint or 'HEAD'} and marked BLOCKED: {reason}"

    return mcp


def run_mcp_server(config_path: Path) -> None:
    """Start the FastMCP server on stdio transport."""
    server = create_mcp_server(config_path)
    logger.info("Starting auto-prompt-engine FastMCP server (stdio)")
    server.run(transport="stdio")
