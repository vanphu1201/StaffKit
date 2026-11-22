"""Typer + Rich CLI entrypoint for Auto-Prompt Engine."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from engine.bridges.agy_cli_executor import smoke_test_agy
from engine.bridges.mcp_server import run_mcp_server
from engine.compiler import PromptCompiler
from engine.database import StateDB
from engine.git_guard import GitGuard
from engine.indexer import RepoIndexer
from engine.models import EngineConfig
from engine.orchestrator import Orchestrator, PlanNotApprovedError
from engine.planner import Planner, PlannerError
from engine.ui.launcher import launch_desktop_app

app = typer.Typer(
    name="engine",
    help="Auto-Prompt Engine: zero-waste orchestration for Antigravity.",
    no_args_is_help=True,
)
console = Console()


def _load_or_fail(config_path: Path) -> EngineConfig:
    if not config_path.is_file():
        console.print(f"[red]Config not found:[/red] {config_path}. Run `engine init` first.")
        raise typer.Exit(code=1)
    return EngineConfig.load(config_path)


def _check_tools(config: EngineConfig | None = None) -> dict[str, str | None]:
    agy_bin = config.agy.binary if config else "agy"
    tools = {
        "git": shutil.which("git"),
        "agy": shutil.which(agy_bin),
        "npx": shutil.which("npx.cmd") or shutil.which("npx"),
        "rtk": shutil.which("rtk"),
        "uv": shutil.which("uv"),
    }
    if config and config.executor.mode in ("aider", "opencode"):
        tools["aider"] = shutil.which("aider")
        tools["opencode"] = shutil.which("opencode")
        tools["litellm"] = shutil.which("litellm")
    return tools


@app.command()
def init(
    config_path: Path = typer.Option(Path("config.yaml"), "--config", help="Path to config.yaml"),
    check: bool = typer.Option(False, "--check", help="Only inspect required tools without writing files"),
    smoke_agy: bool = typer.Option(False, "--smoke-agy", help="Run 5-word real prompt smoke test on `agy`"),
    target_repo: Optional[Path] = typer.Option(None, "--target-repo", help="Target git repository path"),
) -> None:
    """Check installed tools, create config.yaml, and register `.auto-engine/` in `.git/info/exclude`."""
    example_path = Path("config.example.yaml")
    if not check and not config_path.exists() and example_path.is_file():
        content = example_path.read_text(encoding="utf-8")
        if target_repo is not None:
            content = content.replace("D:/code/target-repo", target_repo.resolve().as_posix())
        config_path.write_text(content, encoding="utf-8")
        console.print(f"[green]Created[/green] {config_path}")

    cfg = EngineConfig.load(config_path) if config_path.is_file() else EngineConfig(
        project={"target_repo": str(target_repo or ".")}  # type: ignore[arg-type]
    )
    if target_repo is not None:
        cfg.project.target_repo = target_repo.resolve().as_posix()

    table = Table(title="Auto-Prompt Engine Tool Check")
    table.add_column("Tool", style="cyan")
    table.add_column("Status")
    table.add_column("Path / Install Hint")

    install_hints = {
        "git": "Install Git: https://git-scm.com/",
        "agy": "Install Antigravity CLI (`agy`)",
        "npx": "Install Node.js LTS (for repomix & context7)",
        "rtk": "Optional (60-90% log compression): cargo install rtk / https://github.com/rtk-ai/rtk",
        "uv": "Optional (for serena / uvx): pip install uv",
        "aider": "Optional Mode C: pip install aider-chat",
        "opencode": "Optional Mode C: install opencode CLI",
        "litellm": "Optional Mode C: pip install 'litellm[proxy]'",
    }

    for name, resolved in _check_tools(cfg).items():
        if resolved:
            table.add_row(name, "[green]READY[/green]", resolved)
        else:
            style = "[yellow]OPTIONAL[/yellow]" if name in ("rtk", "uv", "aider", "opencode", "litellm") else "[red]MISSING[/red]"
            table.add_row(name, style, install_hints.get(name, ""))
    console.print(table)

    repo_dir = cfg.target_repo_path
    if (repo_dir / ".git").exists():
        guard = GitGuard(repo_dir, ignore_artifacts=cfg.guards.ignore_artifacts)
        excl = guard.ensure_git_exclude()
        console.print(f"[green]Verified `.auto-engine/` in[/green] {excl}")

    if smoke_agy:
        ok, details = smoke_test_agy(cfg)
        color = "green" if ok else "red"
        console.print(f"[{color}]{details}[/{color}]")
        if not ok:
            raise typer.Exit(code=1)


@app.command()
def plan(
    prd_file: Path = typer.Argument(..., help="Path to PRD markdown file or tasks.json/openspec dir"),
    config_path: Path = typer.Option(Path("config.yaml"), "--config"),
    from_tasks_json: bool = typer.Option(False, "--from-tasks-json", help="Import tasks.json / openspec/ instead of LLM planning"),
    auto_approve: bool = typer.Option(False, "--auto-approve", help="Approve generated plan immediately"),
    force_reindex: bool = typer.Option(False, "--force-reindex", help="Force repomix rebuild"),
) -> None:
    """Index repository and generate/import DAG tasks into state.db."""
    cfg = _load_or_fail(config_path)
    db = StateDB(cfg.state_db_path)
    indexer = RepoIndexer(cfg)
    planner = Planner(cfg, db, indexer)

    try:
        if from_tasks_json:
            result = planner.import_external_tasks(prd_file, auto_approve=auto_approve)
        else:
            result = planner.plan_from_prd(prd_file, auto_approve=auto_approve, force_reindex=force_reindex)
    except PlannerError as exc:
        console.print(f"[red]Planner failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if result.status == "NEEDS_CLARIFICATION":
        console.print("[yellow]PRD needs clarification before coding:[/yellow]")
        for i, q in enumerate(result.clarifying_questions, start=1):
            console.print(f"  {i}. {q}")
        raise typer.Exit(code=2)

    table = Table(title=f"Generated Plan ({len(result.tasks)} tasks)")
    table.add_column("ID", style="cyan")
    table.add_column("Complexity")
    table.add_column("Depends On")
    table.add_column("Write / New Files")
    table.add_column("Title")
    for t in result.tasks:
        table.add_row(
            t.task_id,
            t.complexity,
            ", ".join(t.depends_on) or "-",
            ", ".join(t.allowed_write_files + t.create_new_files),
            t.title,
        )
    console.print(table)
    if not auto_approve and cfg.sprint.require_plan_approval:
        console.print("[yellow]Plan saved to `.auto-engine/plan.json`. Run `engine approve` before `engine run`.[/yellow]")


@app.command()
def approve(config_path: Path = typer.Option(Path("config.yaml"), "--config")) -> None:
    """Approve the current plan in state.db to unlock execution."""
    cfg = _load_or_fail(config_path)
    db = StateDB(cfg.state_db_path)
    db.set_meta("plan_approved", "1")
    console.print("[green]Plan approved.[/green] Run `engine run` to start execution.")


def _build_orchestrator(cfg: EngineConfig) -> Orchestrator:
    db = StateDB(cfg.state_db_path)
    guard = GitGuard(cfg.target_repo_path, ignore_artifacts=cfg.guards.ignore_artifacts)
    verifier = Verifier(cfg, guard)
    compiler = PromptCompiler(cfg)
    return Orchestrator(cfg, db, guard, verifier, compiler)


@app.command()
def run(
    config_path: Path = typer.Option(Path("config.yaml"), "--config"),
    auto_approve: bool = typer.Option(False, "--auto-approve", help="Bypass Plan Approval Gate"),
    sprint_name: str = typer.Option("sprint-1", "--sprint", help="Work branch suffix"),
) -> None:
    """Execute ready tasks autonomously in stateless sessions."""
    cfg = _load_or_fail(config_path)
    orch = _build_orchestrator(cfg)
    try:
        res = orch.run_sprint(auto_approve=auto_approve, sprint_name=sprint_name)
    except PlanNotApprovedError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=1) from exc

    _print_report_table(res["summary"], res.get("halted_reason"), res.get("final_verify_passed", True))
    if res.get("halted_reason") or not res.get("final_verify_passed", True):
        if res.get("final_verify_error"):
            console.print(f"[red]Final Sprint Gate Error:[/red]\n{res['final_verify_error']}")
        raise typer.Exit(code=1)


@app.command()
def resume(
    config_path: Path = typer.Option(Path("config.yaml"), "--config"),
    sprint_name: str = typer.Option("sprint-1", "--sprint"),
) -> None:
    """Roll back any interrupted RUNNING/VERIFYING tasks to checkpoints and continue the sprint."""
    cfg = _load_or_fail(config_path)
    orch = _build_orchestrator(cfg)
    res = orch.run_sprint(auto_approve=True, sprint_name=sprint_name)
    console.print(f"[cyan]Recovered stale tasks:[/cyan] {res['recovered_tasks']}")
    _print_report_table(res["summary"], res.get("halted_reason"), res.get("final_verify_passed", True))


@app.command()
def status(config_path: Path = typer.Option(Path("config.yaml"), "--config")) -> None:
    """Show concise task counts and approval status."""
    cfg = _load_or_fail(config_path)
    db = StateDB(cfg.state_db_path)
    summary = db.get_sprint_summary()
    approved = db.get_meta("plan_approved", "0")
    console.print(f"Plan approved: {approved == '1'} | Tasks: {summary['counts']}")


@app.command(name="serve-mcp")
def serve_mcp(config_path: Path = typer.Option(Path("config.yaml"), "--config")) -> None:
    """Start the minimal 4-tool FastMCP stdio server for Antigravity IDE / App."""
    run_mcp_server(config_path)


def _print_report_table(summary: dict, halted_reason: str | None = None, final_ok: bool = True) -> None:
    table = Table(title="Sprint Token & Execution Report")
    table.add_column("Metric", style="cyan")
    table.add_column("Value")
    table.add_row("Tasks", str(summary["counts"]))
    table.add_row("Prompt Tokens", str(summary["prompt_tokens"]))
    table.add_row("Completion Tokens", str(summary["completion_tokens"]))
    table.add_row("Cost (USD)", f"${summary['cost_usd']:.4f}")
    table.add_row("Verdicts", str(summary["verdicts"]))
    if halted_reason:
        table.add_row("Halted Reason", f"[red]{halted_reason}[/red]")
    table.add_row("Final Sprint Gate", "[green]PASS[/green]" if final_ok else "[red]FAIL[/red]")
    console.print(table)


@app.command()
def report(config_path: Path = typer.Option(Path("config.yaml"), "--config")) -> None:
    """Print detailed token consumption, verdict breakdown, and `rtk gain` if available."""
    cfg = _load_or_fail(config_path)
    db = StateDB(cfg.state_db_path)
    _print_report_table(db.get_sprint_summary())
    if shutil.which("rtk"):
        proc = subprocess.run(["rtk", "gain"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0 and proc.stdout.strip():
            console.print(f"\n[cyan]rtk gain:[/cyan]\n{proc.stdout.strip()}")


@app.command()
def ui(
    config_path: Path = typer.Option(Path("config.yaml"), "--config", help="Path to config.yaml"),
    port: int = typer.Option(0, "--port", help="Localhost port (0 = auto free port)"),
    no_window: bool = typer.Option(False, "--no-window", help="Only start server without opening desktop window"),
) -> None:
    """Launch the local Desktop App GUI in a standalone window."""
    launch_desktop_app(config_path=config_path, port=port, no_window=no_window)


if __name__ == "__main__":
    app()
