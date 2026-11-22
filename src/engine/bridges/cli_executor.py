"""Mode C (Optional Fallback): Headless CLI executor for `aider` and `opencode` via LiteLLM."""

from __future__ import annotations

import os
import re
import subprocess
import time

from engine.bridges.agy_cli_executor import kill_process_tree
from engine.models import EngineConfig, ExecutionResult, TaskSpec, TokenUsage

AIDER_TOKENS_RE = re.compile(r"Tokens:\s*([\d,]+k?)\s*sent,\s*([\d,]+k?)\s*received", re.IGNORECASE)
AIDER_COST_RE = re.compile(r"Cost:\s*\$?([\d.]+)")


def _parse_human_tokens(val: str) -> int:
    cleaned = val.replace(",", "").strip().lower()
    if cleaned.endswith("k"):
        return int(float(cleaned[:-1]) * 1000)
    return int(float(cleaned))


class CliFallbackExecutor:
    """Runs stateless `aider` or `opencode` subprocesses for BYOK / self-hosted models."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config

    def execute(
        self,
        prompt: str,
        task: TaskSpec,
        model_alias: str,
        effort: str = "medium",
    ) -> ExecutionResult:
        """Execute one stateless Aider or OpenCode session with cwd=target_repo."""
        self.config.auto_engine_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = self.config.auto_engine_dir / "current_prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        gw = self.config.gateway
        api_key = os.environ.get(gw.api_key_env, "") if gw else ""
        base_url = gw.base_url if gw else "http://127.0.0.1:4000/v1"

        mode = self.config.executor.mode
        if mode == "aider":
            cmd = [
                "aider",
                "--model",
                f"openai/{model_alias}",
                "--openai-api-base",
                base_url,
                "--openai-api-key",
                api_key,
                "--yes-always",
                "--no-auto-commits",
                "--no-stream",
                "--edit-format",
                "diff",
                "--message-file",
                str(prompt_file),
                *task.allowed_write_files,
            ]
        elif mode == "opencode":
            cmd = [
                "opencode",
                "run",
                "--model",
                model_alias,
                "--file",
                str(prompt_file),
            ]
        else:
            raise ValueError(f"Unsupported fallback CLI executor mode: {mode}")

        start = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.config.target_repo_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=self.config.executor.timeout_seconds)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            return ExecutionResult(
                success=False,
                model_used=model_alias,
                effort_used=effort,
                duration_sec=round(time.monotonic() - start, 3),
                verdict_override="TIMEOUT",
                error_message=f"{mode} timed out after {self.config.executor.timeout_seconds}s",
            )

        combined = f"{stdout}\n{stderr}"
        usage = TokenUsage()
        tok_match = AIDER_TOKENS_RE.search(combined)
        if tok_match:
            usage.prompt_tokens = _parse_human_tokens(tok_match.group(1))
            usage.completion_tokens = _parse_human_tokens(tok_match.group(2))
        cost_match = AIDER_COST_RE.search(combined)
        if cost_match:
            usage.cost_usd = float(cost_match.group(1))

        ok = proc.returncode == 0
        return ExecutionResult(
            success=ok,
            model_used=model_alias,
            effort_used=effort,
            usage=usage,
            duration_sec=round(time.monotonic() - start, 3),
            verdict_override=None if ok else "VERIFY_FAIL",
            error_message="" if ok else (stderr.strip() or f"{mode} exited {proc.returncode}"),
            raw_output=combined,
        )
