"""Lean Pydantic data models, configuration schemas, and verdicts for Auto-Prompt Architect v2.0."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

Complexity = Literal["low", "medium", "high"]
Verdict = Literal[
    "PASS",
    "SCOPE_VIOLATION",
    "TEST_GAMING",
    "EMPTY_DIFF",
    "IDENTICAL_ERROR",
    "VERIFY_FAIL",
    "TIMEOUT",
]


def normalize_posix_path(path_value: str) -> str:
    """Normalize a relative path string to clean POSIX format."""
    cleaned = path_value.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def estimate_tokens(text: str) -> int:
    """Fast 0-dependency token estimator (~4 chars/token) for prompt and schema budgeting."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class ProjectConfig(BaseModel):
    """Target repository and blueprint settings."""

    target_repo: str = "."
    base_branch: str = "main"
    blueprint: str = "auto"


class ExecutorConfig(BaseModel):
    """Command timeout and default verification commands."""

    timeout_seconds: int = Field(default=300, ge=5)
    default_verify_commands: list[str] = Field(default_factory=list)


class LimitsConfig(BaseModel):
    """Context window and error-log trimming limits."""

    inline_file_max_lines: int = Field(default=300, ge=20)
    max_error_log_lines: int = Field(default=30, ge=5)


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
            "CHAY_SAN_PHAM.bat",
            "HUONG_DAN_SU_DUNG.md",
            "**/*.db",
            "**/*.db-shm",
            "**/*.db-wal",
            "**/*.sqlite",
            "**/*.sqlite3",
        ]
    )


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


class EngineConfig(BaseModel):
    """Root configuration schema for Auto-Prompt Architect."""

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    guards: GuardsConfig = Field(default_factory=GuardsConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @property
    def target_repo_path(self) -> Path:
        """Resolve the configured target repository path."""
        return Path(self.project.target_repo).expanduser().resolve()

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
    """Atomic verification scope specification used by Verifier."""

    task_id: str
    title: str
    complexity: Complexity = "medium"
    objective: str = ""
    allowed_write_files: list[str] = Field(default_factory=list)
    create_new_files: list[str] = Field(default_factory=list)
    protected_test_files: list[str] = Field(default_factory=list)
    readonly_context_files: list[str] = Field(default_factory=list)
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


class VerifyResult(BaseModel):
    """Outcome from the deterministic verification gatekeeper."""

    passed: bool
    verdict: Verdict
    trimmed_error_log: str = ""
    error_hash: str | None = None
    violating_files: list[str] = Field(default_factory=list)
