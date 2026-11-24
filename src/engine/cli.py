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
from engine.evaluator import ArmName, Evaluator, audit_historical_commits, load_eval_cases
from engine.git_guard import GitGuard
from engine.indexer import RepoIndexer
from engine.models import EngineConfig
from engine.orchestrator import Orchestrator, PlanNotApprovedError
from engine.planner import Planner, PlannerError
from engine.ui.launcher import launch_desktop_app
from engine.verifier import Verifier

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
    guard = GitGuard(
        cfg.target_repo_path,
        ignore_artifacts=cfg.guards.ignore_artifacts,
        forbid_demo_patterns=cfg.guards.forbid_demo_patterns,
        enforce_production_only=cfg.guards.enforce_production_only,
    )
    verifier = Verifier(cfg, guard)
    compiler = PromptCompiler(cfg)
    return Orchestrator(
        cfg,
        db,
        guard,
        verifier,
        compiler,
        on_event=lambda lvl, msg: console.print(f"[{lvl.upper()}] {msg}", markup=False),
    )


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
def serve_mcp(
    config_path: Path | None = typer.Option(None, "--config"),
    ide_only: bool = typer.Option(False, "--ide-only", help="Expose only the 6 lean 0-token Antigravity IDE tools"),
) -> None:
    """Start the FastMCP stdio server for Antigravity IDE / App."""
    run_mcp_server(config_path, ide_only=ide_only)


@app.command(name="install-plugin")
def install_plugin(
    target_dir: Path | None = typer.Option(None, "--target", help="Custom plugin install directory"),
) -> None:
    """Install/sync the Auto-Prompt Architect plugin into ~/.gemini/config/plugins/ with machine-local Python path."""
    import json
    import shutil
    import sys

    src_dir = Path(__file__).resolve().parents[2] / "antigravity-plugin" / "auto-prompt-architect"
    if not src_dir.is_dir():
        console.print(f"[red]Plugin source directory not found: {src_dir}[/red]")
        raise typer.Exit(code=1)

    dst_dir = target_dir or (Path.home() / ".gemini" / "config" / "plugins" / "auto-prompt-architect")
    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)

    py_exe = Path(sys.executable).resolve().as_posix()
    mcp_payload = {
        "mcpServers": {
            "auto-prompt-architect": {
                "command": py_exe,
                "args": ["-m", "engine.cli", "serve-mcp", "--ide-only"],
            }
        }
    }
    (dst_dir / "mcp_config.json").write_text(
        json.dumps(mcp_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    console.print(f"[green]Installed Auto-Prompt Architect plugin -> {dst_dir.as_posix()}[/green]")
    console.print(f"[cyan]MCP Python: {py_exe} (-m engine.cli serve-mcp --ide-only)[/cyan]")


@app.command(name="plugin-health")
def plugin_health(
    repo: Path = typer.Option(Path("."), "--repo", help="Target git repo to benchmark locate_context & memory"),
    commits: int = typer.Option(10, "--commits", help="Number of historical commits to replay (0-token)"),
) -> None:
    """0-token Plugin Health Scorecard: manifest check, token overhead, live MCP latency, commit replay recall & telemetry."""
    import asyncio
    import json
    import time

    from engine.bridges.mcp_server import create_mcp_server
    from engine.compiler import estimate_tokens
    from engine.evaluator import audit_historical_commits

    plugin_dir = Path.home() / ".gemini" / "config" / "plugins" / "auto-prompt-architect"
    repo_path = repo.expanduser().resolve()
    issues: list[str] = []

    # 1. Manifest & Install integrity
    installed = plugin_dir.is_dir() and (plugin_dir / "plugin.json").is_file()
    mcp_py_ok = False
    version = "missing"
    rules_tok = 0
    skill_toks: dict[str, int] = {}
    if installed:
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        version = str(manifest.get("version", "?"))
        if manifest.get("logo") and not (plugin_dir / manifest["logo"]).is_file():
            issues.append(f"Logo missing: {manifest['logo']}")
        mcp_cfg_p = plugin_dir / "mcp_config.json"
        if mcp_cfg_p.is_file():
            mcp_cfg = json.loads(mcp_cfg_p.read_text(encoding="utf-8"))
            srv = (mcp_cfg.get("mcpServers") or {}).get("auto-prompt-architect") or {}
            cmd = str(srv.get("command", ""))
            mcp_py_ok = Path(cmd).is_file() if ("/" in cmd or "\\" in cmd) else True
            if not mcp_py_ok:
                issues.append(f"MCP python binary not found: {cmd}")
            if "--ide-only" not in (srv.get("args") or []):
                issues.append("MCP args missing `--ide-only` (legacy sprint tools add schema bloat).")
        rules_p = plugin_dir / "rules" / "AGENTS.md"
        if rules_p.is_file():
            rules_tok = estimate_tokens(rules_p.read_text(encoding="utf-8"))
        for sk in (plugin_dir / "skills").glob("*/SKILL.md"):
            skill_toks[sk.parent.name] = estimate_tokens(sk.read_text(encoding="utf-8"))
    else:
        issues.append(f"Plugin not installed at {plugin_dir}")

    # 2. Live MCP 6-tool schema overhead + real execution latency
    mcp = create_mcp_server(None, ide_only=True)

    async def _probe_mcp() -> tuple[int, int, float, int, float, int]:
        tools = await mcp.list_tools()
        schema_str = json.dumps(
            [{"name": t.name, "desc": t.description, "params": getattr(t, "parameters", {})} for t in tools],
            ensure_ascii=False,
        )
        schema_tok = estimate_tokens(schema_str)

        t0 = time.monotonic()
        mem_out = (await mcp.get_tool("load_project_memory")).fn(repo_path=str(repo_path))
        mem_ms = round((time.monotonic() - t0) * 1000, 1)
        mem_tok = estimate_tokens(mem_out)

        t1 = time.monotonic()
        loc_out = (await mcp.get_tool("locate_context")).fn(query="fix bug in server or compiler", repo_path=str(repo_path))
        loc_ms = round((time.monotonic() - t1) * 1000, 1)
        loc_tok = estimate_tokens(loc_out)
        return len(tools), schema_tok, mem_ms, mem_tok, loc_ms, loc_tok

    tool_cnt, schema_tok, mem_ms, mem_tok, loc_ms, loc_tok = asyncio.run(_probe_mcp())
    always_on_tok = rules_tok + schema_tok
    if always_on_tok > 1200:
        issues.append(f"Always-on overhead ({always_on_tok} tok) > 1200 tok target.")
    if mem_tok > 2500:
        issues.append(f"load_project_memory output ({mem_tok} tok) is large; trim constitution/lessons/handoff.")

    # 3. 0-Token Historical Commit Replay
    replay_str = "N/A (not a git repo)"
    if (repo_path / ".git").exists():
        rep = audit_historical_commits(repo_path, limit=max(1, min(30, commits)))
        replay_str = (
            f"{rep.commits_evaluated} commits | Recall={rep.avg_recall:.1%} | "
            f"Neighborhood={rep.neighborhood_hit_rate:.1%} | WindowHit={rep.window_hit_rate:.1%}"
        )
        if rep.commits_evaluated > 0 and rep.avg_recall < 0.65:
            issues.append(f"locate_context recall ({rep.avg_recall:.1%}) < 65% on `{repo_path.name}` — consider improving identifier extraction.")

    # 4. Telemetry summary
    tel_path = repo_path / ".auto-engine" / "plugin_telemetry.jsonl"
    tel_counts: dict[str, int] = {}
    if tel_path.is_file():
        for ln in tel_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(ln)
                tel_counts[obj["tool"]] = tel_counts.get(obj["tool"], 0) + 1
            except Exception:
                continue

    table = Table(title=f"Auto-Prompt Architect v{version} — 0-Token Health Scorecard ({repo_path.name})")
    table.add_column("Check", style="cyan")
    table.add_column("Result", style="green")
    table.add_row("Plugin Installed & MCP Python", f"{'OK' if (installed and mcp_py_ok) else 'FAIL'} ({tool_cnt} IDE tools)")
    table.add_row("Always-On Overhead / Turn", f"{always_on_tok} tok (rules={rules_tok} + 6 MCP schemas={schema_tok})")
    table.add_row("On-Demand Skills Size", ", ".join(f"{k}={v} tok" for k, v in sorted(skill_toks.items())) or "none")
    table.add_row("Live `load_project_memory`", f"{mem_ms} ms | ~{mem_tok} tok")
    table.add_row("Live `locate_context`", f"{loc_ms} ms | ~{loc_tok} tok")
    table.add_row("0-Token Commit Replay Accuracy", replay_str)
    table.add_row("Recorded Telemetry Calls", json.dumps(tel_counts) if tel_counts else "(new — accumulates as you use IDE)")
    console.print(table)

    if issues:
        console.print("[yellow]Detected Weaknesses / Recommendations:[/yellow]")
        for iss in issues:
            console.print(f"  - [yellow]{iss}[/yellow]")
    else:
        console.print("[bold green]ALL CHECKS HEALTHY (0 weaknesses detected on static + live MCP + replay audit).[/bold green]")


def _print_report_table(summary: dict, halted_reason: str | None = None, final_ok: bool = True) -> None:
    table = Table(title="Sprint Token & Execution Report")
    table.add_column("Metric", style="cyan")
    table.add_column("Value")
    table.add_row("Tasks", str(summary["counts"]))
    table.add_row("Prompt Tokens", str(summary["prompt_tokens"]))
    table.add_row("Completion Tokens", str(summary["completion_tokens"]))
    if summary.get("planning_prompt_tokens") or summary.get("planning_completion_tokens"):
        table.add_row(
            "Planning Tokens (In/Out)",
            f"{summary.get('planning_prompt_tokens', 0)} / {summary.get('planning_completion_tokens', 0)}",
        )
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


@app.command(name="eval")
def eval_suite(
    config_path: Path = typer.Option(Path("config.yaml"), "--config", help="Path to config.yaml"),
    cases_dir: Optional[Path] = typer.Option(None, "--cases-dir", help="Directory containing YAML eval cases"),
    case_ids: Optional[list[str]] = typer.Option(None, "--case", help="Filter by specific case ID (repeatable)"),
    arm: str = typer.Option("both", "--arm", help="Which arm to evaluate: both | baseline | engine"),
    runs: int = typer.Option(1, "--runs", min=1, help="Repeated runs per case for statistical stability"),
    output: Path = typer.Option(Path(".auto-engine/eval_report.md"), "--output", help="Markdown report path"),
) -> None:
    """Run isolated A/B hidden-test benchmark (Baseline `agy` vs Auto-Prompt Engine)."""
    if arm not in ("both", "baseline", "engine"):
        console.print("[red]Invalid --arm value. Use: both | baseline | engine[/red]")
        raise typer.Exit(code=2)

    cfg = EngineConfig.load(config_path) if config_path.is_file() else EngineConfig()
    cases = load_eval_cases(cases_dir=cases_dir, case_ids=case_ids)
    if not cases:
        console.print("[yellow]No evaluation cases found.[/yellow]")
        raise typer.Exit(code=1)

    selected_arms: tuple[ArmName, ...] = ("baseline", "engine") if arm == "both" else (arm,)  # type: ignore[assignment]
    console.print(
        f"[cyan]Running A/B Hidden-Test Eval across {len(cases)} case(s) x {runs} run(s), arms={selected_arms}...[/cyan]"
    )
    evaluator = Evaluator(cfg)
    report_obj = evaluator.run_suite(cases, arms=selected_arms, runs=runs)

    json_out = output.with_suffix(".json")
    Evaluator.write_report(report_obj, output, json_path=json_out)

    table = Table(title="A/B Hidden-Test Benchmark Summary")
    table.add_column("Arm", style="cyan")
    table.add_column("Resolve Rate")
    table.add_column("Regression PASS")
    table.add_column("Total Tokens")
    table.add_column("Tokens / Resolved")
    table.add_column("Avg Time")
    for arm_name in selected_arms:
        s = report_obj.summaries[arm_name]
        tpr = f"{s.tokens_per_resolved:,.0f}" if s.tokens_per_resolved is not None else "N/A"
        table.add_row(
            s.arm,
            f"{s.resolved_cases}/{s.total_cases} ({s.resolve_rate * 100:.1f}%)",
            f"{s.regression_pass_rate * 100:.1f}%",
            f"{s.total_tokens:,}",
            tpr,
            f"{s.avg_duration_sec:.2f}s",
        )
    console.print(table)
    if report_obj.resolve_rate_delta is not None:
        console.print(f"Resolve Rate Delta (engine - baseline): [bold]{report_obj.resolve_rate_delta * 100:+.1f} pp[/bold]")
    if report_obj.token_savings_pct is not None:
        console.print(f"Token Savings: [bold]{report_obj.token_savings_pct:+.1f}%[/bold]")
    console.print(f"[green]Reports written:[/green] {output} & {json_out}")


@app.command(name="audit-repo")
def audit_repo(
    repo: Path = typer.Option(Path("."), "--repo", help="Path to local Git repository to replay commits on"),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Number of historical non-merge commits to replay"),
    output: Optional[Path] = typer.Option(None, "--output", help="Optional Markdown report path"),
) -> None:
    """Run 0-token historical commit replay audit (Recall, Precision, Window Hit, Prompt Tokens, Soft-Scope)."""
    target_repo = repo.resolve()
    if not (target_repo / ".git").exists():
        console.print(f"[red]Not a Git working repository:[/red] {target_repo}")
        raise typer.Exit(code=2)

    console.print(f"[cyan]Replaying up to {limit} historical commits in {target_repo.name} (0 LLM tokens)...[/cyan]")
    report = audit_historical_commits(target_repo, limit=limit)
    if report.commits_evaluated == 0:
        console.print("[yellow]No qualifying multi/single code commits found in git history.[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title=f"0-Token Historical Replay Scorecard ({report.repo_name})")
    table.add_column("Metric", style="cyan")
    table.add_column("Value")
    table.add_row("Commits Evaluated", str(report.commits_evaluated))
    table.add_row("Target Hit Rate", f"{report.target_hit_rate * 100:.1f}%")
    table.add_row("Neighborhood Hit Rate", f"{report.neighborhood_hit_rate * 100:.1f}%")
    table.add_row("Avg Recall / Precision", f"{report.avg_recall * 100:.1f}% / {report.avg_precision * 100:.1f}%")
    table.add_row("Avg Targets / Commit", f"{report.avg_targets_count:.2f}")
    table.add_row("Window Hit Rate (>300L files)", f"{report.window_hit_rate * 100:.1f}%")
    table.add_row("Avg / Max Prompt Tokens", f"{report.avg_prompt_tokens:,} / {report.max_prompt_tokens:,}")
    table.add_row("Soft-Scope Pass Rate", f"{report.soft_scope_pass_rate * 100:.1f}%")
    console.print(table)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report.to_markdown(), encoding="utf-8")
        console.print(f"[green]Audit report written:[/green] {output}")


if __name__ == "__main__":
    app()
