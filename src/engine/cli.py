"""Typer + Rich CLI entrypoint for StaffKit v3.0 (Antigravity IDE Pro Engineering Plugin)."""

from __future__ import annotations

import asyncio
import json
import os
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
    name="staffkit",
    help="StaffKit v3.0 — 0-token Staff-Engineer Plugin for Antigravity IDE.",
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
    """Install/sync the StaffKit plugin into ~/.gemini/config/plugins/staffkit with machine-local Python path."""
    src_dir = Path(__file__).resolve().parents[2] / "antigravity-plugin" / "staffkit"
    if not src_dir.is_dir():
        console.print(f"[red]Plugin source directory not found: {src_dir}[/red]")
        raise typer.Exit(code=1)

    dst_dir = target_dir or (Path.home() / ".gemini" / "config" / "plugins" / "staffkit")
    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)

    # IMPORTANT: Do NOT call `.resolve()` on sys.executable — on Linux/macOS `.venv/bin/python` is a symlink
    # to `/usr/bin/python3.x`, and dereferencing it bypasses the virtualenv's site-packages (`ModuleNotFoundError: engine`).
    py_exe = Path(os.path.abspath(sys.executable)).as_posix()
    mcp_payload = {
        "mcpServers": {
            "staffkit": {
                "command": py_exe,
                "args": ["-m", "engine.cli", "serve-mcp"],
            }
        }
    }
    (dst_dir / "mcp_config.json").write_text(
        json.dumps(mcp_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    console.print(f"[green]Installed StaffKit plugin -> {dst_dir.as_posix()}[/green]")
    console.print(f"[cyan]MCP Python: {py_exe} (-m engine.cli serve-mcp)[/cyan]")


@app.command(name="plugin-health")
def plugin_health(
    repo: Path = typer.Option(Path("."), "--repo", help="Target git repo to benchmark locate_context & memory"),
    commits: int = typer.Option(10, "--commits", help="Number of historical commits to replay (0-token)"),
) -> None:
    """0-token Plugin Health Scorecard: manifest check, token overhead, live MCP latency, commit replay recall & telemetry."""
    plugin_dir = Path.home() / ".gemini" / "config" / "plugins" / "staffkit"
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
            srv = (mcp_cfg.get("mcpServers") or {}).get("staffkit") or {}
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

    table = Table(title=f"StaffKit v{version} — 0-Token Health Scorecard ({repo_path.name})")
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
    table = Table(title=f"StaffKit — Plugin Micro-Eval ({report.passed_count}/{report.total_count} PASS)")
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


@app.command(name="plugin-stats")
def plugin_stats(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repo containing .auto-engine/plugin_telemetry.jsonl"),
    is_global: bool = typer.Option(False, "--global", help="Aggregate concrete effectiveness evidence across ALL projects"),
    days: int = typer.Option(0, "--days", min=0, help="Filter telemetry to last N days (0 = all time)"),
    reset: bool = typer.Option(False, "--reset", help="Clear the selected telemetry log cleanly"),
) -> None:
    """Analyze real MCP telemetry & concrete effectiveness evidence (catches, rollbacks, lessons) per repo or --global."""
    from datetime import datetime, timedelta

    from engine.bridges.mcp_server import GLOBAL_TELEMETRY_PATH

    if is_global:
        tel_path = GLOBAL_TELEMETRY_PATH
        scope_label = "ALL PROJECTS (Global)"
    else:
        repo_path = repo.expanduser().resolve()
        tel_path = repo_path / ".auto-engine" / "plugin_telemetry.jsonl"
        scope_label = repo_path.name

    if reset:
        if tel_path.is_file():
            tel_path.unlink()
        console.print(f"[green]Cleared telemetry log:[/green] {tel_path.as_posix()}")
        return

    if not tel_path.is_file():
        console.print(f"[yellow]No telemetry recorded yet at {tel_path.as_posix()}. Use StaffKit in Antigravity IDE first.[/yellow]")
        return

    cutoff = (datetime.now() - timedelta(days=days)) if days > 0 else None
    stats: dict[str, dict[str, float]] = {}
    events: dict[str, int] = {}
    repos_seen: set[str] = set()
    total_calls = 0
    for ln in tel_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(ln)
            if cutoff is not None and obj.get("ts"):
                if datetime.fromisoformat(str(obj["ts"])) < cutoff:
                    continue
            name = str(obj.get("tool") or "unknown")
            row = stats.setdefault(name, {"calls": 0.0, "ok": 0.0, "latency_ms": 0.0, "tokens": 0.0})
            row["calls"] += 1
            if obj.get("ok", True):
                row["ok"] += 1
            row["latency_ms"] += float(obj.get("latency_ms", 0.0))
            row["tokens"] += float(obj.get("output_tokens", 0.0))
            if obj.get("repo"):
                repos_seen.add(str(obj["repo"]))
            ev = str(obj.get("event") or "")
            if ev:
                events[ev] = events.get(ev, 0) + 1
            total_calls += 1
        except Exception:
            continue

    if total_calls == 0:
        console.print("[yellow]No matching telemetry entries found.[/yellow]")
        return

    table = Table(title=f"StaffKit — Live Telemetry Stats ({scope_label}, {total_calls} calls)")
    table.add_column("MCP Tool", style="cyan", no_wrap=True)
    table.add_column("Calls", justify="right")
    table.add_column("Success", justify="right")
    table.add_column("Avg Latency", justify="right")
    table.add_column("Avg Output Tok", justify="right")
    table.add_column("Total Output Tok", justify="right")

    for name, r in sorted(stats.items(), key=lambda kv: (-kv[1]["calls"], kv[0])):
        cnt = int(r["calls"])
        ok_pct = (r["ok"] / cnt) * 100.0
        avg_ms = r["latency_ms"] / cnt
        avg_tok = r["tokens"] / cnt
        table.add_row(
            name,
            str(cnt),
            f"{ok_pct:.0f}%",
            f"{avg_ms:.1f} ms",
            f"~{avg_tok:.0f}",
            f"~{int(r['tokens']):,}",
        )
    console.print(table)

    # Concrete Effectiveness Evidence Summary
    diff_catches = events.get("caught_diff_sanity", 0)
    core_catches = events.get("caught_core_regression", 0)
    run_catches = events.get("caught_runtime_or_test", 0)
    total_catches = diff_catches + core_catches + run_catches
    verify_passes = events.get("verify_pass", 0)
    undo_saves = events.get("undo_rollback", 0)
    lessons_total = events.get("repo_lesson", 0) + events.get("global_lesson", 0)

    ev_table = Table(title=f"StaffKit — Concrete Effectiveness Evidence ({len(repos_seen) or 1} project(s))")
    ev_table.add_column("Evidence Metric", style="cyan")
    ev_table.add_column("Observed Value", style="green")
    ev_table.add_row(
        "Bugs / False-Greens Caught Before Delivery",
        f"{total_catches} caught (diff_sanity={diff_catches}, core_regression={core_catches}, test/http={run_catches}) | {verify_passes} clean PASS",
    )
    ev_table.add_row("Regression Rollbacks Saved (`undo`)", str(undo_saves))
    ev_table.add_row(
        "Compounded Lessons Recorded",
        f"{lessons_total} (repo={events.get('repo_lesson', 0)}, global={events.get('global_lesson', 0)})",
    )
    if repos_seen:
        ev_table.add_row("Tracked Projects", ", ".join(sorted(repos_seen)[:8]))
    console.print(ev_table)


@app.command(name="init-team")
def init_team(
    repo: Path = typer.Option(Path("."), "--repo", help="Target repository to scaffold `.auto-engine/team.yaml`"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing `.auto-engine/team.yaml`"),
) -> None:
    """Auto-detect repo stack and scaffold a clean `.auto-engine/team.yaml` (verify_commands, core_smoke_commands, sensitive_paths, forbid_patterns)."""
    import yaml

    from engine.bootstrapper import detect_existing_stack

    repo_path = repo.expanduser().resolve()
    auto_dir = repo_path / ".auto-engine"
    team_path = auto_dir / "team.yaml"
    if team_path.is_file() and not force:
        console.print(f"[yellow]Already exists:[/yellow] {team_path.as_posix()} (pass `--force` to overwrite).")
        return

    stack, verify_cmds, _fc, _prev = detect_existing_stack(repo_path)
    sensitive: list[str] = []
    for cand in ("migrations/", "prisma/", "infra/", "terraform/", ".github/workflows/"):
        if (repo_path / cand.rstrip("/")).exists():
            sensitive.append(cand)
    if not sensitive:
        sensitive = ["migrations/", ".github/workflows/"]

    forbid = ["debugger"] if stack in ("web_react", "fullstack_web") else []
    payload = {
        "verify_commands": verify_cmds,
        "core_smoke_commands": [],
        "sensitive_paths": sensitive,
        "forbid_patterns": forbid,
    }
    auto_dir.mkdir(parents=True, exist_ok=True)
    header = (
        f"# StaffKit Team Policy for `{repo_path.name}` (detected stack: {stack})\n"
        "# Add real end-to-end checks to `core_smoke_commands` to lock critical flows against regressions.\n"
    )
    team_path.write_text(
        header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    console.print(f"[green]Created Team Policy -> {team_path.as_posix()}[/green] (stack=`{stack}`)")


@app.command(name="ab-compare")
def ab_compare(
    branch_a: str = typer.Argument(..., help="Baseline branch/commit (e.g. ab-no-plugin)"),
    branch_b: str = typer.Argument(..., help="Treatment branch/commit (e.g. ab-staffkit)"),
    base: str = typer.Option("HEAD", "--base", help="Common ancestor commit/branch"),
    repo: Path = typer.Option(Path("."), "--repo", help="Target git repository"),
    cmd: list[str] | None = typer.Option(None, "--cmd", help="Custom verification command(s) to run on both branches"),
) -> None:
    """Objectively compare two Git branches (Baseline vs StaffKit) using isolated worktrees (0 LLM tokens)."""
    from engine.evaluator import compare_ab_branches

    repo_path = repo.expanduser().resolve()
    rep = compare_ab_branches(repo_path, base, branch_a, branch_b, verify_cmds=cmd)
    a, b = rep.branch_a, rep.branch_b

    table = Table(title=f"StaffKit — Objective A/B Comparison ({rep.repo_name}, base=`{rep.base_ref}`)")
    table.add_column("Metric", style="cyan")
    table.add_column(f"A: {a.label} ({a.ref})")
    table.add_column(f"B: {b.label} ({b.ref})")
    table.add_row("Files Changed (+ins / -del)", f"{a.files_changed} (+{a.insertions}/-{a.deletions})", f"{b.files_changed} (+{b.insertions}/-{b.deletions})")
    table.add_row("Build / Test Gate", f"{'PASS' if a.verify_passed else 'FAIL'} ({a.verify_duration_sec}s)", f"{'PASS' if b.verify_passed else 'FAIL'} ({b.verify_duration_sec}s)")
    table.add_row(
        "Diff Sanity (0 mock/@ts-ignore/skip)",
        "PASS" if a.diff_sanity_ok else f"FAIL ({len(a.diff_sanity_hits)} hits)",
        "PASS" if b.diff_sanity_ok else f"FAIL ({len(b.diff_sanity_hits)} hits)",
    )
    table.add_row("Security X-Ray (0 .env/secrets)", "PASS" if a.security_ok else "FAIL", "PASS" if b.security_ok else "FAIL")
    if a.core_smoke_passed is not None or b.core_smoke_passed is not None:
        table.add_row(
            "Core-Flow Smoke Gate",
            "PASS" if a.core_smoke_passed else "FAIL",
            "PASS" if b.core_smoke_passed else "FAIL",
        )
    table.add_row("Objective Score (0-100)", f"[bold]{a.objective_score}[/bold]", f"[bold]{b.objective_score}[/bold]")
    console.print(table)
    console.print(f"[bold green]Winner: {rep.winner}[/bold green] (Delta B - A: {rep.score_delta_b_minus_a:+d} pts)")


if __name__ == "__main__":
    app()
