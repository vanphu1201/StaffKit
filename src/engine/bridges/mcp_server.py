"""Auto-Prompt Architect v2.0.0 — Pure 6-Tool 0-Token FastMCP Server for Antigravity IDE."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

from engine.bootstrapper import detect_existing_stack, generate_nontech_delivery_guide
from engine.git_guard import GitGuard
from engine.indexer import (
    extract_fast_skeleton,
    extract_symbol_windows,
    list_real_files,
    locate_relevant_files,
    scan_repo_tech_debt,
)
from engine.models import EngineConfig, ProjectConfig
from engine.verifier import Verifier, probe_http_endpoint, run_web_runtime_smoke

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[auto-prompt-architect] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

GLOBAL_PLAYBOOK_DIR = Path.home() / ".gemini" / "auto-prompt-playbooks"
GLOBAL_LESSONS_PATH = GLOBAL_PLAYBOOK_DIR / "global_lessons.md"


def _resolve_repo_path(repo_path: str, fallback: Path) -> Path:
    cleaned = (repo_path or "").strip()
    if cleaned:
        p = Path(cleaned).expanduser().resolve()
        if p.is_dir():
            return p
    return fallback.resolve()


def _read_optional(path: Path, max_chars: int = 3500) -> str:
    if not path.is_file():
        return ""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace").strip()
        return txt[:max_chars] + ("\n... [trimmed]" if len(txt) > max_chars else "")
    except OSError:
        return ""


def _append_markdown_entry(file_path: Path, header_prefix: str, entry: str, *, max_entries: int = 12) -> int:
    """Append or replace a `### <title>` block in a markdown memory file cleanly."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    if file_path.is_file():
        raw = file_path.read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            for idx, b in enumerate(raw.split("\n### ")):
                bc = b.strip()
                if not bc:
                    continue
                hdr = bc if (idx == 0 and bc.startswith("### ")) else f"### {bc}"
                if hdr != entry and not hdr.startswith(header_prefix):
                    blocks.append(hdr)
    blocks.append(entry)
    kept = blocks[-max_entries:]
    file_path.write_text("\n\n".join(kept) + "\n", encoding="utf-8")
    return len(kept)


def _record_telemetry(repo: Path, tool_name: str, started: float, output_text: str, ok: bool = True) -> None:
    """Append 1 lightweight 0-token telemetry row to `.auto-engine/plugin_telemetry.jsonl`."""
    try:
        auto_dir = repo / ".auto-engine"
        auto_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "tool": tool_name,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "output_tokens": max(1, int(len(output_text) / 3.5)) if output_text else 0,
            "ok": ok,
        }
        with (auto_dir / "plugin_telemetry.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def create_mcp_server(config_path: Path | None = None, *, global_playbook_path: Path | None = None) -> FastMCP:
    """Instantiate pure 6-tool 0-token FastMCP server for Antigravity IDE."""
    if config_path is not None and config_path.is_file():
        config = EngineConfig.load(config_path)
    else:
        config = EngineConfig(project=ProjectConfig(target_repo=str(Path.cwd())))

    playbook_path = global_playbook_path or GLOBAL_LESSONS_PATH
    mcp = FastMCP("auto-prompt-architect")

    @mcp.tool()
    def load_project_memory(repo_path: str = "", include_audit: bool = False) -> str:
        """0-token X-ray briefing: stack, verify/preview commands, constitution, contracts, lessons, handoff, global playbook & optional tech-debt X-Ray."""
        t0 = time.monotonic()
        repo = _resolve_repo_path(repo_path, config.target_repo_path)
        auto_dir = repo / ".auto-engine"
        det_stack, det_vc, det_fc, det_prev = detect_existing_stack(repo)

        commits = ""
        if (repo / ".git").exists():
            proc = subprocess.run(
                ["git", "log", "-n", "10", "--date=short", "--pretty=format:- [%h] %ad %s"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            commits = proc.stdout.strip()

        constitution = _read_optional(auto_dir / "constitution.md", 2200)
        contracts = _read_optional(auto_dir / "contracts.md", 2200)
        lessons = _read_optional(auto_dir / "lessons.md", 2200)
        handoff = _read_optional(auto_dir / "handoff.md", 1800)
        global_lessons = _read_optional(playbook_path, 1600)

        sections = [
            f"# PROJECT MEMORY BRIEFING: `{repo.name}`",
            f"- Path: `{repo.as_posix()}` | Stack: `{det_stack}`",
            f"- Verify: `{det_vc}` | Final Gate: `{det_fc}` | Preview: `{det_prev or '(auto)'}`",
        ]
        if include_audit:
            sections.append(f"\n## 0-Token Tech-Debt & Health X-Ray\n{scan_repo_tech_debt(repo)}")
        if commits:
            sections.append(f"\n## Recent Git Commits\n{commits}")
        if constitution:
            sections.append(f"\n## Project Constitution\n{constitution}")
        if contracts:
            sections.append(f"\n## Architecture Contract\n{contracts}")
        if lessons:
            sections.append(f"\n## Project Lessons Learned\n{lessons}")
        if global_lessons:
            sections.append(f"\n## Cross-Project Global Playbook\n{global_lessons}")
        if handoff:
            sections.append(f"\n## Module Handoff Memory\n{handoff}")
        out = "\n".join(sections)
        _record_telemetry(repo, "load_project_memory", t0, out, ok=True)
        return out

    @mcp.tool()
    def locate_context(query: str, repo_path: str = "", max_files: int = 5) -> str:
        """0-token AST + VI-EN + 1-hop import-graph locator: returns relevant starting files, signatures & symbol windows."""
        t0 = time.monotonic()
        repo = _resolve_repo_path(repo_path, config.target_repo_path)
        try:
            real_files = list_real_files(repo)
        except Exception:
            ignore_dirs = {".git", ".auto-engine", "node_modules", ".venv", "__pycache__", "dist", "build"}
            real_files = set()
            for root, dirs, files in os.walk(repo):
                dirs[:] = [d for d in dirs if d not in ignore_dirs]
                for f in files:
                    real_files.add((Path(root) / f).relative_to(repo).as_posix())
        if not real_files:
            return f"No source files found in `{repo.as_posix()}`."

        located = locate_relevant_files(query, real_files, repo, max_targets=max(1, min(10, max_files)))
        lines = [
            f"# 0-TOKEN CONTEXT LOCATOR (`{repo.name}` — {len(real_files)} files)",
            f"- Suggested Starting Points: {located.target_files}",
            f"- Dependency / Interface Context: {located.readonly_files}",
            f"- Protected Test Files: {located.protected_tests[:10]}",
        ]
        for rel in located.target_files[:max_files]:
            fp = repo / rel
            if not fp.is_file():
                continue
            skel = extract_fast_skeleton(fp, max_lines=22, priority_identifiers=located.identifiers)
            win = extract_symbol_windows(fp, located.identifiers, radius=12, max_total_lines=45)
            lines.append(f"\n## {rel}")
            if skel:
                lines.append(f"```\n{skel}\n```")
            if win:
                lines.append(f"// Matched symbol windows:\n```\n{win}\n```")
        out = "\n".join(lines)
        _record_telemetry(repo, "locate_context", t0, out, ok=True)
        return out

    @mcp.tool()
    def save_project_spec(
        repo_path: str = "",
        constitution_md: str = "",
        contracts_md: str = "",
        prd_md: str = "",
        acceptance_criteria: list[str] | None = None,
    ) -> str:
        """Persist agreed Brainstorm outputs (Constitution, Architecture Contract, OpenSpec PRD & AC-1..AC-N)."""
        t0 = time.monotonic()
        repo = _resolve_repo_path(repo_path, config.target_repo_path)
        auto_dir = repo / ".auto-engine"
        auto_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []

        if constitution_md.strip():
            p = auto_dir / "constitution.md"
            p.write_text(constitution_md.strip() + "\n", encoding="utf-8")
            saved.append(p.as_posix())

        contract_parts: list[str] = []
        if contracts_md.strip():
            contract_parts.append(contracts_md.strip())
        if acceptance_criteria:
            ac_lines = ["## Acceptance Criteria (Must Pass Before Completion)"]
            for idx, ac in enumerate(acceptance_criteria, start=1):
                s = str(ac).strip()
                if s:
                    prefix = "" if s.upper().startswith("AC-") else f"AC-{idx}: "
                    ac_lines.append(f"- {prefix}{s}")
            if len(ac_lines) > 1:
                contract_parts.append("\n".join(ac_lines))
        if contract_parts:
            p = auto_dir / "contracts.md"
            p.write_text("\n\n".join(contract_parts) + "\n", encoding="utf-8")
            saved.append(p.as_posix())

        if prd_md.strip():
            os_dir = repo / "openspec"
            os_dir.mkdir(parents=True, exist_ok=True)
            p = os_dir / "prd.md"
            p.write_text(prd_md.strip() + "\n", encoding="utf-8")
            saved.append(p.as_posix())

        if (repo / ".git").exists():
            GitGuard(repo, ignore_artifacts=config.guards.ignore_artifacts).ensure_git_exclude()

        out = "SAVED_SPEC:\n" + "\n".join(f"- `{s}`" for s in saved) if saved else "NO_CONTENT_PROVIDED"
        _record_telemetry(repo, "save_project_spec", t0, out, ok=bool(saved))
        return out

    @mcp.tool()
    def record_lesson(
        title: str,
        summary: str,
        repo_path: str = "",
        touched_files: list[str] | None = None,
        global_playbook: bool = False,
    ) -> str:
        """Persist verified root-cause discovery to `.auto-engine/lessons.md`, refresh `handoff.md`, and optionally save to Cross-Project Global Playbook."""
        t0 = time.monotonic()
        repo = _resolve_repo_path(repo_path, config.target_repo_path)
        auto_dir = repo / ".auto-engine"
        lessons_path = auto_dir / "lessons.md"

        clean_title = title.strip()
        clean_lines = [ln.strip() for ln in summary.strip().splitlines() if ln.strip()][:6]
        entry = f"### {clean_title}\n" + "\n".join(f"- {ln.lstrip('-* ')}" for ln in clean_lines)
        cnt = _append_markdown_entry(lessons_path, f"### {clean_title}", entry, max_entries=12)

        notes = [f"RECORDED_LESSON -> `{lessons_path.as_posix()}` ({cnt} repo lessons)"]
        if global_playbook:
            g_cnt = _append_markdown_entry(playbook_path, f"### {clean_title}", entry, max_entries=20)
            notes.append(f"+ saved to Global Playbook `{playbook_path.as_posix()}` ({g_cnt} universal lessons)")

        clean_touched = [str(f).strip().replace("\\", "/") for f in (touched_files or []) if str(f).strip()]
        if clean_touched:
            h_lines = [f"### {clean_title}", f"- Touched files: {', '.join(f'`{p}`' for p in clean_touched[:6])}"]
            for rel in clean_touched[:5]:
                fp = repo / rel
                if fp.is_file():
                    skel = extract_fast_skeleton(fp, max_lines=10)
                    if skel:
                        sigs = " | ".join(ln.strip() for ln in skel.splitlines()[:8] if ln.strip() and not ln.startswith("..."))
                        if sigs:
                            h_lines.append(f"- `{rel}` signatures: `{sigs[:380]}`")
            _append_markdown_entry(auto_dir / "handoff.md", f"### {clean_title}", "\n".join(h_lines), max_entries=10)
            notes.append(f"+ updated `{(auto_dir / 'handoff.md').as_posix()}`")

        out = " ".join(notes)
        _record_telemetry(repo, "record_lesson", t0, out, ok=True)
        return out

    @mcp.tool()
    def verify_and_smoke(
        repo_path: str = "",
        commands: list[str] | None = None,
        run_smoke: bool = True,
        smoke_url: str = "",
        expected_status: int = 200,
        generate_delivery_pack: bool = False,
    ) -> str:
        """Run 0-token independent verification (diff sanity + build/tests + live HTTP/runtime smoke; optional Delivery Pack)."""
        t0 = time.monotonic()
        repo = _resolve_repo_path(repo_path, config.target_repo_path)
        det_stack, det_vc, det_fc, _ = detect_existing_stack(repo)
        cmds = [c.strip() for c in (commands or []) if str(c).strip()] or det_fc or det_vc
        local_cfg = config.model_copy(deep=True)
        local_cfg.project.target_repo = repo.as_posix()
        guard = GitGuard(repo, ignore_artifacts=local_cfg.guards.ignore_artifacts)
        verifier = Verifier(local_cfg, guard)

        report: list[str] = [f"# INDEPENDENT VERIFICATION (`{repo.name}`, stack=`{det_stack}`)"]
        all_ok = True
        if (repo / ".git").exists():
            _, untracked = guard.get_changed_files()
            sanity_ok, gaming_hits = guard.check_diff_sanity(
                untracked,
                local_cfg.guards.forbid_diff_patterns,
                forbid_assertion_deletion=local_cfg.guards.forbid_test_assertion_deletion,
                forbid_demo_patterns=local_cfg.guards.forbid_demo_patterns,
                enforce_production_only=local_cfg.guards.enforce_production_only,
            )
            if not sanity_ok:
                all_ok = False
                report.append("- Diff Sanity Gate (anti-mock/anti-skip): **FAIL**")
                report.append("```\n" + "\n".join(gaming_hits[:10]) + "\n```")
            else:
                report.append("- Diff Sanity Gate (anti-mock/anti-skip): **PASS**")
        if cmds:
            res = verifier.run_commands(cmds)
            all_ok = all_ok and res.passed
            report.append(f"- Static/Test Gate ({cmds}): **{'PASS' if res.passed else 'FAIL'}**")
            if res.trimmed_error_log:
                report.append(f"```\n{res.trimmed_error_log}\n```")
        else:
            report.append("- Static/Test Gate: (no commands detected; pass `commands=[...]` to run specific checks)")

        if all_ok and smoke_url.strip():
            ep_ok, ep_msg = probe_http_endpoint(smoke_url.strip(), expected_status=expected_status)
            all_ok = all_ok and ep_ok
            report.append(f"- HTTP Endpoint Probe: **{'PASS' if ep_ok else 'FAIL'}** — {ep_msg}")
        elif all_ok and run_smoke and ((repo / "package.json").is_file() or (repo / "pyproject.toml").is_file()):
            smoke_ok, smoke_msg = run_web_runtime_smoke(repo)
            all_ok = all_ok and smoke_ok
            report.append(f"- Runtime Smoke Probe: **{'PASS' if smoke_ok else 'FAIL'}** — {smoke_msg}")

        if all_ok and generate_delivery_pack:
            try:
                guide = generate_nontech_delivery_guide(repo, write_files=True)
                report.append(
                    f"- Non-Tech Delivery Pack: Generated `CHAY_SAN_PHAM.bat` & `HUONG_DAN_SU_DUNG.md` "
                    f"(run=`{guide.get('run_command', '')}`"
                    + (f", url=`{guide['web_url']}`" if guide.get("web_url") else "")
                    + ")"
                )
            except Exception:
                pass

        report.append(f"\n**OVERALL: {'PASS' if all_ok else 'FAIL'}**")
        out = "\n".join(report)
        _record_telemetry(repo, "verify_and_smoke", t0, out, ok=all_ok)
        return out

    @mcp.tool()
    def snapshot_or_undo(action: str = "snapshot", message: str = "", repo_path: str = "") -> str:
        """Create a safe Git snapshot commit before major edits (`action='snapshot'`) or rollback cleanly (`action='undo'`)."""
        t0 = time.monotonic()
        repo = _resolve_repo_path(repo_path, config.target_repo_path)
        if not (repo / ".git").exists():
            return f"ERROR: `{repo.as_posix()}` is not a Git repository."
        guard = GitGuard(repo, ignore_artifacts=config.guards.ignore_artifacts)
        guard.ensure_git_exclude()

        act = action.strip().lower()
        if act == "snapshot":
            tracked, untracked = guard.get_changed_files()
            dirty = tracked + untracked
            if dirty:
                subprocess.run(["git", "add", "--", *dirty], cwd=str(repo), capture_output=True, check=False)
                msg = message.strip() or "chore: auto-prompt-architect safety snapshot"
                subprocess.run(["git", "commit", "-m", msg], cwd=str(repo), capture_output=True, check=False)
            sha = guard.create_checkpoint()
            (repo / ".auto-engine").mkdir(parents=True, exist_ok=True)
            (repo / ".auto-engine" / "last_snapshot.json").write_text(
                json.dumps({"sha": sha, "message": message.strip()}, indent=2),
                encoding="utf-8",
            )
            out = f"SNAPSHOT_SAVED: `{sha[:8]}` ({len(dirty)} files committed)"
            _record_telemetry(repo, "snapshot_or_undo", t0, out, ok=True)
            return out

        if act == "undo":
            snap_file = repo / ".auto-engine" / "last_snapshot.json"
            sha = ""
            if snap_file.is_file():
                try:
                    sha = str(json.loads(snap_file.read_text(encoding="utf-8")).get("sha") or "")
                except Exception:
                    sha = ""
            if not sha:
                return "ERROR: No saved snapshot found to undo."
            guard.hard_rollback(sha)
            out = f"UNDONE: Rolled back `{repo.name}` cleanly to `{sha[:8]}`."
            _record_telemetry(repo, "snapshot_or_undo", t0, out, ok=True)
            return out

        return f"ERROR: Unknown action `{action}`. Use `snapshot` or `undo`."

    return mcp


def run_mcp_server(config_path: Path | None = None) -> None:
    """Start the pure 6-tool FastMCP server on stdio transport."""
    server = create_mcp_server(config_path)
    logger.info("Starting auto-prompt-architect v2.0.0 FastMCP server (stdio, 6 tools)")
    server.run(transport="stdio")
