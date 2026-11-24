"""Typer + Rich CLI entrypoint for Auto-Prompt Architect v2.0 (Antigravity IDE Native Plugin)."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from engine.bridges.mcp_server import create_mcp_server, run_mcp_server
from engine.evaluator import audit_historical_commits, run_plugin_micro_evals
from engine.models import estimate_tokens

app = typer.Typer(
    name="engine",
    help="Auto-Prompt Architect v2.0 — 0-token Antigravity IDE Native Plugin.",
    no_args_is_help=True,
)
console = Console()


@app.command(name="serve-mcp")
def serve_mcp(
    config_path: Path | None = typer.Option(None, "--config", help="Optional config.yaml path"),
    ide_only: bool = typer.Option(True, "--ide-only", help="Retained for backward compatibility (always 6 lean IDE tools)"),
) -> None:
    """Start the lean 6-tool FastMCP stdio server for Antigravity IDE."""
    _ = ide_only
    run_mcp_server(config_path)


@app.command(name="install-plugin")
def install_plugin(
    target_dir: Path | None = typer.Option(None, "--target", help="Custom plugin install directory"),
) -> None:
    """Install/sync the Auto-Prompt Architect plugin into ~/.gemini/config/plugins/ with machine-local Python path."""
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
                "args": ["-m", "engine.cli", "serve-mcp"],
            }
        }
    }
    (dst_dir / "mcp_config.json").write_text(
        json.dumps(mcp_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    console.print(f"[green]Installed Auto-Prompt Architect plugin -> {dst_dir.as_posix()}[/green]")
    console.print(f"[cyan]MCP Python: {py_exe} (-m engine.cli serve-mcp)[/cyan]")


@app.command(name="plugin-health")
def plugin_health(
    repo: Path = typer.Option(Path("."), "--repo", help="Target git repo to benchmark locate_context & memory"),
    commits: int = typer.Option(10, "--commits", help="Number of historical commits to replay (0-token)"),
) -> None:
    """0-token Plugin Health Scorecard: manifest check, token overhead, live MCP latency, commit replay recall & telemetry."""
    plugin_dir = Path.home() / ".gemini" / "config" / "plugins" / "auto-prompt-architect"
    repo_path = repo.expanduser().resolve()
    issues: list[str] = []

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
        rules_p = plugin_dir / "rules" / "AGENTS.md"
        if rules_p.is_file():
            rules_tok = estimate_tokens(rules_p.read_text(encoding="utf-8"))
        for sk in (plugin_dir / "skills").glob("*/SKILL.md"):
            skill_toks[sk.parent.name] = estimate_tokens(sk.read_text(encoding="utf-8"))
    else:
        issues.append(f"Plugin not installed at {plugin_dir}")

    mcp = create_mcp_server(None)

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
        loc_out = (await mcp.get_tool("locate_context")).fn(
            query="fix bug in server or client",
            repo_path=str(repo_path),
        )
        loc_ms = round((time.monotonic() - t1) * 1000, 1)
        loc_tok = estimate_tokens(loc_out)
        return len(tools), schema_tok, mem_ms, mem_tok, loc_ms, loc_tok

    tool_cnt, schema_tok, mem_ms, mem_tok, loc_ms, loc_tok = asyncio.run(_probe_mcp())
    always_on_tok = rules_tok + schema_tok
    if always_on_tok > 1000:
        issues.append(f"Always-on overhead ({always_on_tok} tok) > 1000 tok v2.0 target.")
    if mem_tok > 2500:
        issues.append(f"load_project_memory output ({mem_tok} tok) is large; trim constitution/lessons/handoff.")

    replay_str = "N/A (not a git repo)"
    if (repo_path / ".git").exists():
        rep = audit_historical_commits(repo_path, limit=max(1, min(30, commits)))
        replay_str = (
            f"{rep.commits_evaluated} commits | Recall={rep.avg_recall:.1%} | "
            f"Neighborhood={rep.neighborhood_hit_rate:.1%} | WindowHit={rep.window_hit_rate:.1%}"
        )
        if rep.commits_evaluated > 0 and rep.avg_recall < 0.55:
            issues.append(
                f"locate_context recall ({rep.avg_recall:.1%}) < 55% on `{repo_path.name}` — check VI->EN map or import graph."
            )

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


@app.command(name="plugin-eval")
def plugin_eval() -> None:
    """Run the fast 3-scenario 0-token Plugin Micro-Eval harness against the live 6-tool MCP server."""
    report = run_plugin_micro_evals()
    table = Table(title=f"Auto-Prompt Architect — Plugin Micro-Eval ({report.passed_count}/{report.total_count} PASS)")
    table.add_column("Scenario", style="cyan")
    table.add_column("Status")
    table.add_column("Time", justify="right")
    table.add_column("Details")
    for s in report.scenarios:
        status = "[bold green]PASS[/bold green]" if s.passed else "[bold red]FAIL[/bold red]"
        table.add_row(f"{s.id}: {s.title}", status, f"{s.duration_ms:.1f} ms", s.details)
    console.print(table)
    if not report.all_passed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
