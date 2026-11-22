"""Mode A2: Antigravity Python SDK (`google-antigravity`) executor with real-time tool-call guard."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from engine.models import EngineConfig, ExecutionResult, TaskSpec, TokenUsage, normalize_posix_path

WRITE_TOOL_NAMES = {"edit_file", "write_file", "replace_file_content", "multi_replace_file_content"}
PATH_ARG_KEYS = ("Path", "path", "file_path", "TargetFile", "target_file")


class AgySdkNotInstalledError(RuntimeError):
    """Raised when `google-antigravity` package is not installed."""


def inspect_tool_call_scope(
    tool_name: str,
    args: dict[str, Any],
    allowed_paths: set[str],
    protected_paths: set[str],
) -> str | None:
    """Return violation reason if a write tool call targets an out-of-scope file."""
    if tool_name.lower() not in WRITE_TOOL_NAMES:
        return None
    for key in PATH_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            norm = normalize_posix_path(val)
            # Strip repo prefix if absolute path matches tail, or compare normalized relative path
            if norm in protected_paths or not any(norm == p or norm.endswith(f"/{p}") for p in allowed_paths):
                return f"SDK intercepted out-of-scope write `{tool_name}` targeting `{norm}`"
    return None


class AgySdkExecutor:
    """Executes one stateless task via `google.antigravity.Agent` with tool-call monitoring."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config

    async def _execute_async(
        self,
        prompt: str,
        task: TaskSpec,
        model: str,
        effort: str,
    ) -> ExecutionResult:
        try:
            from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AgySdkNotInstalledError(
                "Mode `agy_sdk` requires `google-antigravity`. Install with: pip install google-antigravity"
            ) from exc

        start = time.monotonic()
        allowed = {normalize_posix_path(p) for p in task.allowed_write_files + task.create_new_files}
        protected = {normalize_posix_path(p) for p in task.protected_test_files}

        agent_cfg = LocalAgentConfig(
            system_instructions=f"Working directory: {self.config.target_repo_path}",
            capabilities=CapabilitiesConfig(),
        )
        raw_chunks: list[str] = []
        tool_count = 0

        async with Agent(agent_cfg) as agent:
            response = await agent.chat(prompt)

            async def _collect_text() -> None:
                async for token in response:
                    raw_chunks.append(str(token))

            async def _watch_tools() -> str | None:
                nonlocal tool_count
                async for call in response.tool_calls:
                    tool_count += 1
                    args = call.args if isinstance(call.args, dict) else {}
                    viol = inspect_tool_call_scope(call.name, args, allowed, protected)
                    if viol:
                        return viol
                    if tool_count > self.config.limits.max_tool_calls_per_attempt:
                        return f"SDK watchdog exceeded max_tool_calls_per_attempt ({tool_count})"
                return None

            text_task = asyncio.create_task(_collect_text())
            viol_msg = await _watch_tools()
            if viol_msg:
                text_task.cancel()
                verdict = "SCOPE_VIOLATION" if "out-of-scope" in viol_msg else "MAX_TURNS_EXCEEDED"
                return ExecutionResult(
                    success=False,
                    model_used=model or "agy-sdk-default",
                    effort_used=effort,
                    usage=TokenUsage(),
                    duration_sec=round(time.monotonic() - start, 3),
                    verdict_override=verdict,  # type: ignore[arg-type]
                    error_message=viol_msg,
                    raw_output="".join(raw_chunks),
                )
            await text_task

        return ExecutionResult(
            success=True,
            model_used=model or "agy-sdk-default",
            effort_used=effort,
            usage=TokenUsage(),
            duration_sec=round(time.monotonic() - start, 3),
            raw_output="".join(raw_chunks),
        )

    def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
        """Synchronous wrapper running the async Antigravity SDK loop."""
        return asyncio.run(self._execute_async(prompt, task, model, effort))
