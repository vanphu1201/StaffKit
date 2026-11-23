"""Pydantic data models, configuration schemas, and verdicts for Auto-Prompt Engine."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

Complexity = Literal["low", "medium", "high"]
Effort = Literal["low", "medium", "high"]
StackType = str
ExecutorMode = Literal["agy_cli", "agy_sdk", "mcp_only", "aider", "opencode"]
TaskStatus = Literal["PENDING", "RUNNING", "VERIFYING", "DONE", "BLOCKED"]
Verdict = Literal[
    "PASS",
    "SCOPE_VIOLATION",
    "TEST_GAMING",
    "EMPTY_DIFF",
    "IDENTICAL_ERROR",
    "VERIFY_FAIL",
    "TIMEOUT",
    "MAX_TURNS_EXCEEDED",
    "BUDGET_EXCEEDED",
    "CRASH_RECOVERED",
]


def normalize_posix_path(path_value: str) -> str:
    """Normalize a relative path string to clean POSIX format."""
    cleaned = path_value.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def inline_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve all `#/$defs/...` `$ref` pointers inline so CLI validators need no external URI fetch."""
    defs: dict[str, Any] = copy.deepcopy(schema.get("$defs") or {})

    def _resolve(node: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [_resolve(item, stack) for item in node]
        if not isinstance(node, dict):
            return node

        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            def_name = ref[len("#/$defs/") :]
            if def_name in defs and def_name not in stack:
                resolved = copy.deepcopy(defs[def_name])
                for k, v in node.items():
                    if k != "$ref":
                        resolved[k] = v
                return _resolve(resolved, (*stack, def_name))

        return {k: _resolve(v, stack) for k, v in node.items() if k != "$defs"}

    return _resolve(schema)


class ProjectConfig(BaseModel):
    """Target repository and persistence settings."""

    target_repo: str = "."
    base_branch: str = "main"
    work_branch_prefix: str = "auto-engine/"
    state_db: str = ".auto-engine/state.db"
    shared_rules_file: str = ""
    blueprint: str = "auto"


class ExecutorConfig(BaseModel):
    """Execution mode, timeout, and default verification commands."""

    mode: ExecutorMode = "agy_cli"
    timeout_seconds: int = Field(default=300, ge=5)
    idle_timeout_seconds: int = Field(default=120, ge=5)
    default_verify_commands: list[str] = Field(default_factory=list)


class AgyTierConfig(BaseModel):
    """Model and reasoning effort pair for a specific tier."""

    model: str = ""
    effort: Effort = "medium"


class AgyConfig(BaseModel):
    """Antigravity CLI and SDK routing configuration."""

    binary: str = "agy"
    permission_mode: Literal["accept-edits", "plan"] = "accept-edits"
    disable_slash_commands: bool = True
    auto_approve_tools: bool = True
    dynamic_mcp_gating: bool = False
    forbid_shell_verify_commands: bool = False
    tiers: dict[str, AgyTierConfig] = Field(
        default_factory=lambda: {
            "planner": AgyTierConfig(model="", effort="high"),
            "coder_low": AgyTierConfig(model="", effort="low"),
            "coder_medium": AgyTierConfig(model="", effort="medium"),
            "escalation": AgyTierConfig(model="", effort="high"),
        }
    )


class LimitsConfig(BaseModel):
    """Hard token, cost, retry, and circuit-breaker thresholds."""

    max_retries_per_task: int = Field(default=2, ge=0)
    max_tokens_per_task: int = Field(default=150000, ge=500)
    max_tool_calls_per_attempt: int = Field(default=30, ge=1)
    max_prompt_tokens_per_call: int = Field(default=12000, ge=500)
    inline_file_max_lines: int = Field(default=300, ge=20)
    max_budget_usd_per_sprint: float = Field(default=2.0, ge=0.0)
    max_files_per_task: int = Field(default=3, ge=1)
    max_error_log_lines: int = Field(default=30, ge=5)
    max_consecutive_blocked_tasks: int = Field(default=3, ge=1)


class GuardsConfig(BaseModel):
    """Zero-token anti-gaming patterns, anti-demo production rules, and artifact exclusions."""

    forbid_test_assertion_deletion: bool = True
    enforce_production_only: bool = True
    scope_mode: Literal["strict", "soft", "flexible"] = "soft"
    forbid_demo_patterns: list[str] = Field(
        default_factory=lambda: [
            r"@[a-z0-9-]*demo\.(vn|com|local|io)\b",
            r"\b(TODO|FIXME):\s*(mock|fake|placeholder)\b",
        ]
    )
    forbid_diff_patterns: list[str] = Field(
        default_factory=lambda: [
            "@ts-ignore",
            "@ts-nocheck",
            "@ts-expect-error",
            "eslint-disable",
            "# type: ignore",
            "# noqa",
            r"\.skip\(",
            r"\.only\(",
            r"pytest\.mark\.skip",
            r"unittest\.skip",
        ]
    )
    ignore_artifacts: list[str] = Field(
        default_factory=lambda: [
            ".auto-engine/**",
            ".pytest_cache/**",
            "**/__pycache__/**",
            "coverage/**",
            "*.tsbuildinfo",
            ".mypy_cache/**",
            ".ruff_cache/**",
        ]
    )


class SprintConfig(BaseModel):
    """Sprint-level plan approval and final integration verification."""

    require_plan_approval: bool = True
    planning_mode: Literal["auto", "single", "dag"] = "auto"
    max_parallel_tasks: int = Field(default=2, ge=1, le=4)
    final_verify_commands: list[str] = Field(default_factory=list)
    runtime_verify_commands: list[str] = Field(default_factory=list)


class ToolsConfig(BaseModel):
    """External tool wrappers and ignore globs."""

    use_rtk: bool = True
    rtk_exclude_commands: list[str] = Field(default_factory=lambda: ["git commit"])
    repomix_ignore: list[str] = Field(
        default_factory=lambda: [
            "node_modules/**",
            ".git/**",
            "dist/**",
            "build/**",
            ".venv/**",
            ".ui-profile/**",
            "*.lock",
        ]
    )


class NotificationsConfig(BaseModel):
    """Optional webhook notification environment variable name."""

    webhook_url_env: str = "ENGINE_WEBHOOK_URL"


class GatewayConfig(BaseModel):
    """Optional LiteLLM gateway configuration for Mode C fallback CLIs."""

    base_url: str = "http://127.0.0.1:4000/v1"
    api_key_env: str = "LITELLM_VIRTUAL_KEY"
    models: dict[str, str] = Field(
        default_factory=lambda: {
            "planner": "tier-planner",
            "executor": "tier-coder-fast",
            "escalation": "tier-coder-strong",
        }
    )


class EngineConfig(BaseModel):
    """Root configuration schema loaded from config.yaml."""

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    agy: AgyConfig = Field(default_factory=AgyConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    guards: GuardsConfig = Field(default_factory=GuardsConfig)
    sprint: SprintConfig = Field(default_factory=SprintConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    gateway: GatewayConfig | None = None

    @property
    def target_repo_path(self) -> Path:
        """Resolve the configured target repository path."""
        return Path(self.project.target_repo).expanduser().resolve()

    @property
    def state_db_path(self) -> Path:
        """Resolve state.db path relative to target_repo if not absolute."""
        raw = Path(self.project.state_db).expanduser()
        if raw.is_absolute():
            return raw
        return (self.target_repo_path / raw).resolve()

    @property
    def auto_engine_dir(self) -> Path:
        """Directory inside target_repo storing engine metadata and caches."""
        return self.target_repo_path / ".auto-engine"

    @classmethod
    def load(cls, path: Path) -> EngineConfig:
        """Load and validate EngineConfig from a YAML file."""
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)


class TaskSpec(BaseModel):
    """Atomic task specification emitted by the Planner."""

    task_id: str
    title: str
    complexity: Complexity = "medium"
    depends_on: list[str] = Field(default_factory=list)
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    allowed_write_files: list[str] = Field(default_factory=list)
    create_new_files: list[str] = Field(default_factory=list)
    protected_test_files: list[str] = Field(default_factory=list)
    readonly_context_files: list[str] = Field(default_factory=list)
    needs_docs_mcp: bool = False
    needs_lsp_mcp: bool = False
    constraints: list[str] = Field(default_factory=list)
    user_questions: list[str] = Field(default_factory=list)
    strict_scope: bool = True
    verify_commands: list[str] = Field(default_factory=list)

    @field_validator(
        "allowed_write_files",
        "create_new_files",
        "protected_test_files",
        "readonly_context_files",
        mode="before",
    )
    @classmethod
    def _normalize_path_lists(cls, value: list[str] | None) -> list[str]:
        if not value:
            return []
        return [normalize_posix_path(item) for item in value if item and item.strip()]


class PlanOutput(BaseModel):
    """Structured planner response supporting PRD ambiguity clarification."""

    status: Literal["NEEDS_CLARIFICATION", "READY"]
    clarifying_questions: list[str] = Field(default_factory=list)
    tasks: list[TaskSpec] = Field(default_factory=list)


class TokenUsage(BaseModel):
    """Token and USD cost breakdown for one execution attempt."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ExecutionResult(BaseModel):
    """Result returned by any execution bridge (CLI, SDK, or fallback)."""

    success: bool
    model_used: str
    effort_used: str = "medium"
    usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_sec: float = 0.0
    verdict_override: Verdict | None = None
    error_message: str = ""
    raw_output: str = ""


class VerifyResult(BaseModel):
    """Outcome from the deterministic Layer 5 gatekeeper."""

    passed: bool
    verdict: Verdict
    trimmed_error_log: str = ""
    error_hash: str | None = None
    violating_files: list[str] = Field(default_factory=list)


class InterviewQuestion(BaseModel):
    """A single plain-Vietnamese clarifying question for non-technical users."""

    id: str
    question: str
    options: list[str] = Field(default_factory=list)
    default_answer: str = ""


class InterviewOutput(BaseModel):
    """Structured output of Layer 0A ProductInterviewer."""

    recommended_stack: str = "generic"
    suggested_project_name: str = "my-ai-app"
    product_summary_vi: str = ""
    questions: list[InterviewQuestion] = Field(default_factory=list)


class ProductSpec(BaseModel):
    """Plain-Vietnamese feature list plus synthesized technical PRD and dynamic stack rules for Layer 2 Planner."""

    product_title: str
    features_vi: list[str] = Field(default_factory=list)
    prd_markdown: str
    verify_commands: list[str] = Field(default_factory=list)
    preview_command: str = ""
    quality_rules: list[str] = Field(default_factory=list)


class BootstrapResult(BaseModel):
    """Outcome of zero-token project scaffolding and Git initialization."""

    project_dir: str
    stack: str
    verify_commands: list[str] = Field(default_factory=list)
    final_verify_commands: list[str] = Field(default_factory=list)
    preview_command: str = ""
    is_new_repo: bool = False
