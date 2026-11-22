"""External YAML blueprint loader so tech stacks, verify commands, and quality rules avoid hardcoding."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


def default_blueprints_dir() -> Path:
    """Resolve the engine's built-in `blueprints/` directory cleanly from any CWD."""
    return Path(__file__).resolve().parents[2] / "blueprints"


class BlueprintSpec(BaseModel):
    """Pluggable stack/domain profile loaded from `blueprints/*.yaml` or `.auto-engine/blueprint.yaml`."""

    id: str
    label: str
    description: str = ""
    detect_markers: list[str] = Field(default_factory=list)
    scaffold_commands: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)
    runtime_verify_commands: list[str] = Field(default_factory=list)
    preview_command: str = ""
    quality_rules: list[str] = Field(default_factory=list)
    forbid_patterns: list[str] = Field(default_factory=list)
    fallback_template: str = "generic"


def _parse_blueprint_file(path: Path) -> BlueprintSpec | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    return BlueprintSpec.model_validate(raw)


def load_blueprints(
    target_repo: Path | None = None,
    *,
    blueprints_dir: Path | None = None,
) -> list[BlueprintSpec]:
    """Load built-in YAML blueprints and merge optional `<target_repo>/.auto-engine/blueprint.yaml`."""
    by_id: dict[str, BlueprintSpec] = {}
    root_dir = blueprints_dir or default_blueprints_dir()
    if root_dir.is_dir():
        for file_path in sorted(root_dir.glob("*.yaml")):
            spec = _parse_blueprint_file(file_path)
            if spec is not None:
                by_id[spec.id] = spec

    if target_repo is not None:
        custom_path = target_repo / ".auto-engine" / "blueprint.yaml"
        if custom_path.is_file():
            custom = _parse_blueprint_file(custom_path)
            if custom is not None:
                by_id[custom.id] = custom

    if "generic" not in by_id:
        by_id["generic"] = BlueprintSpec(
            id="generic",
            label="Mọi sản phẩm công nghệ (Tùy chọn tự do)",
            description="Không ràng buộc stack; Antigravity tự thiết kế kiến trúc phù hợp.",
        )

    preferred_order = ["generic", "fullstack_web", "web_react", "python_app"]
    ordered: list[BlueprintSpec] = [by_id[k] for k in preferred_order if k in by_id]
    for k, spec in by_id.items():
        if k not in preferred_order:
            ordered.append(spec)
    return ordered


def get_blueprint(
    blueprint_id: str,
    target_repo: Path | None = None,
    *,
    blueprints_dir: Path | None = None,
) -> BlueprintSpec | None:
    """Return a loaded BlueprintSpec by id, or None for `auto`/unknown."""
    cleaned = (blueprint_id or "").strip()
    if not cleaned or cleaned == "auto":
        return None
    for bp in load_blueprints(target_repo, blueprints_dir=blueprints_dir):
        if bp.id == cleaned:
            return bp
    return None
