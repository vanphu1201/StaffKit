"""Mode B (Primary IDE Amplifier): FastMCP Server powering Antigravity IDE with 0-token Context, Memory, Spec & Verification."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

from engine.bootstrapper import detect_existing_stack, generate_nontech_delivery_guide
from engine.compiler import PromptCompiler
from engine.database import StateDB
from engine.git_guard import GitGuard
from engine.indexer import extract_fast_skeleton, extract_symbol_windows, list_real_files, locate_relevant_files
from engine.models import EngineConfig, ProjectConfig, TokenUsage
from engine.verifier import Verifier, run_web_runtime_smoke

# Ensure stdio MCP transport remains completely clean by routing all logs to stderr.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[auto-prompt-engine] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _resolve_repo_path(repo_path: str, fallback: Path) -> Path:
    cleaned = (repo_path or "").strip()
    if cleaned:
        p = Path(cleaned).expanduser().resolve()
        if p.is_dir():
            return p
    return fallback.resolve()


def _read_optional(path: Path, max_chars: int = 4000) -> str:
    if not path.is_file():
        return ""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace").strip()
        return txt[:max_chars] + ("\n... [trimmed]" if len(txt) > max_chars else "")
    except OSError:
        return ""


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


def create_mcp_server(config_path: Path | None = None, *, ide_only: bool = False) -> FastMCP:
    """Instantiate FastMCP server exposing 6 lean 0-token IDE Amplifier tools (+ optional legacy Sprint tools)."""
    if config_path is not None and config_path.is_file():
        config = EngineConfig.load(config_path)
    else:
        config = EngineConfig(project=ProjectConfig(target_repo=str(Path.cwd())))

    mcp = FastMCP("auto-prompt-architect")

    # =========================================================================
    # TIER 1: 0-TOKEN ANTIGRAVITY IDE AMPLIFIER TOOLS (Work on ANY repo_path)
    # =========================================================================

    @mcp.tool()
    def load_project_memory(repo_path: str = "") -> str:
        """0-token X-ray briefing of a project: stack, verify/run commands, constitution, contracts, lessons, handoff & recent git log."""
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

        constitution = _read_optional(auto_dir / "constitution.md", 2500)
        contracts = _read_optional(auto_dir / "contracts.md", 2500)
        lessons = _read_optional(auto_dir / "lessons.md", 2500)
        handoff = _read_optional(auto_dir / "handoff.md", 2000)

        sections = [
            f"# PROJECT MEMORY BRIEFING: `{repo.name}`",
            f"- Path: `{repo.as_posix()}`",
            f"- Detected Stack: `{det_stack}`",
            f"- Verify Commands: `{det_vc}` | Final Gate: `{det_fc}` | Preview: `{det_prev or '(auto)'}`",
        ]
        if commits:
            sections.append(f"\n## Recent Git Commits\n{commits}")
        if constitution:
            sections.append(f"\n## Project Constitution (`.auto-engine/constitution.md`)\n{constitution}")
        if contracts:
            sections.append(f"\n## Architecture Contract (`.auto-engine/contracts.md`)\n{contracts}")
        if lessons:
            sections.append(f"\n## Lessons Learned & Verified Discoveries (`.auto-engine/lessons.md`)\n{lessons}")
        if handoff:
            sections.append(f"\n## Module Handoff Memory (`.auto-engine/handoff.md`)\n{handoff}")
        out = "\n".join(sections)
        _record_telemetry(repo, "load_project_memory", t0, out, ok=True)
        return out

    @mcp.tool()
    def locate_context(query: str, repo_path: str = "", max_files: int = 5) -> str:
        """0-token AST & brace-block locator: returns the most relevant starting files, signatures & symbol windows to save exploration tokens."""
        import os

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
            f"# 0-TOKEN CONTEXT LOCATOR (`{repo.name}` — {len(real_files)} tracked files)",
            f"- Suggested Starting Points: {located.target_files}",
            f"- Readonly Context / Interfaces: {located.readonly_files}",
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
        """Persist agreed Brainstorm outputs (Constitution, Architecture Contract, OpenSpec PRD & AC-1..AC-N) to the repo."""
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

        return "SAVED_SPEC:\n" + "\n".join(f"- `{s}`" for s in saved) if saved else "NO_CONTENT_PROVIDED"

    @mcp.tool()
    def record_lesson(
        title: str,
        summary: str,
        repo_path: str = "",
        touched_files: list[str] | None = None,
    ) -> str:
        """Persist a verified root-cause discovery into `.auto-engine/lessons.md` and optionally refresh `.auto-engine/handoff.md` signatures (0-token)."""
        repo = _resolve_repo_path(repo_path, config.target_repo_path)
        auto_dir = repo / ".auto-engine"
        auto_dir.mkdir(parents=True, exist_ok=True)
        lessons_path = auto_dir / "lessons.md"

        clean_lines = [ln.strip() for ln in summary.strip().splitlines() if ln.strip()][:6]
        entry = f"### {title.strip()}\n" + "\n".join(f"- {ln.lstrip('-* ')}" for ln in clean_lines)
        blocks: list[str] = []
        if lessons_path.is_file():
            raw = lessons_path.read_text(encoding="utf-8", errors="replace").strip()
            if raw:
                for idx, b in enumerate(raw.split("\n### ")):
                    bc = b.strip()
                    if not bc:
                        continue
                    hdr = bc if (idx == 0 and bc.startswith("### ")) else f"### {bc}"
                    if hdr != entry:
                        blocks.append(hdr)
        blocks.append(entry)
        lessons_path.write_text("\n\n".join(blocks[-12:]) + "\n", encoding="utf-8")

        handoff_note = ""
        clean_touched = [str(f).strip().replace("\\", "/") for f in (touched_files or []) if str(f).strip()]
        if clean_touched:
            h_lines = [f"### {title.strip()}", f"- Touched files: {', '.join(f'`{p}`' for p in clean_touched[:6])}"]
            for rel in clean_touched[:5]:
                fp = repo / rel
                if fp.is_file():
                    skel = extract_fast_skeleton(fp, max_lines=10)
                    if skel:
                        sigs = " | ".join(ln.strip() for ln in skel.splitlines()[:8] if ln.strip() and not ln.startswith("..."))
                        if sigs:
                            h_lines.append(f"- `{rel}` signatures: `{sigs[:380]}`")
            h_entry = "\n".join(h_lines)
            handoff_path = auto_dir / "handoff.md"
            h_blocks: list[str] = []
            if handoff_path.is_file():
                raw_h = handoff_path.read_text(encoding="utf-8", errors="replace").strip()
                if raw_h:
                    for idx, b in enumerate(raw_h.split("\n### ")):
                        bc = b.strip()
                        if not bc:
                            continue
                        hdr = bc if (idx == 0 and bc.startswith("### ")) else f"### {bc}"
                        if not hdr.startswith(f"### {title.strip()}"):
                            h_blocks.append(hdr)
            h_blocks.append(h_entry)
            handoff_path.write_text("\n\n".join(h_blocks[-10:]) + "\n", encoding="utf-8")
            handoff_note = f" + updated `{handoff_path.as_posix()}`"

        return f"RECORDED_LESSON -> `{lessons_path.as_posix()}` ({len(blocks[-12:])} lessons stored){handoff_note}"

    @mcp.tool()
    def verify_and_smoke(
        repo_path: str = "",
        commands: list[str] | None = None,
        run_smoke: bool = True,
        generate_delivery_pack: bool = True,
    ) -> str:
        """Run 0-token independent verification (build/typecheck/tests + live Web runtime smoke + non-tech Delivery Pack)."""
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
        if cmds:
            res = verifier.run_commands(cmds)
            all_ok = all_ok and res.passed
            report.append(f"- Static/Test Gate ({cmds}): **{'PASS' if res.passed else 'FAIL'}**")
            if res.trimmed_error_log:
                report.append(f"```\n{res.trimmed_error_log}\n```")
        else:
            report.append("- Static/Test Gate: (no commands detected; pass `commands=[...]` to run specific checks)")

        if all_ok and run_smoke and det_stack in ("web_react", "fullstack_web") and (repo / "package.json").is_file():
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
        """Create a safe Git snapshot commit before major edits (`action='snapshot'`) or rollback to the previous snapshot (`action='undo'`)."""
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
            return f"SNAPSHOT_SAVED: `{sha[:8]}` ({len(dirty)} files committed)"

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
            return f"UNDONE: Rolled back `{repo.name}` cleanly to `{sha[:8]}`."

        return f"ERROR: Unknown action `{action}`. Use `snapshot` or `undo`."

    if ide_only:
        return mcp

    # =========================================================================
    # TIER 2: LEGACY SPRINT ORCHESTRATION TOOLS (100% Backward Compatible)
    # =========================================================================

    def _sprint_deps() -> tuple[StateDB, GitGuard, Verifier, PromptCompiler]:
        db = StateDB(config.state_db_path)
        guard = GitGuard(config.target_repo_path, ignore_artifacts=config.guards.ignore_artifacts)
        return db, guard, Verifier(config, guard), PromptCompiler(config)

    @mcp.tool()
    def get_next_task() -> str:
        """Pull the next READY task, create a clean Git checkpoint, and return a Surgical Prompt."""
        db, guard, _verifier, compiler = _sprint_deps()
        approved = db.get_meta("plan_approved", "0")
        if config.sprint.require_plan_approval and approved != "1":
            return "BLOCKED: Plan is not approved yet. Run `engine approve` first."

        task = db.get_next_ready_task()
        if task is None:
            summary = db.get_sprint_summary()
            return f"NO_READY_TASKS: Summary = {summary['counts']}"

        row = db.get_task_row(task.task_id)
        attempt = int(row["retry_count"]) if row else 0
        last_error = str(row["last_error"] or "") if row else ""

        checkpoint = guard.create_checkpoint()
        db.update_task_status(task.task_id, "RUNNING", checkpoint_sha=checkpoint)

        compact_diff = guard.get_compact_diff(task.allowed_write_files) if attempt > 0 else ""
        prompt, est_tokens = compiler.compile(
            task,
            attempt=attempt,
            last_error=last_error,
            compact_diff=compact_diff,
        )
        logger.info("Dispatched task %s (attempt=%d, ~%d tokens)", task.task_id, attempt, est_tokens)
        return (
            f"{prompt}\n\n"
            "---\n"
            f"[MCP REMINDER] Hoàn thành xong task `{task.task_id}`, hãy gọi `submit_and_verify(task_id=\"{task.task_id}\")`, "
            "sau đó mở chat mới trước khi nhận task tiếp theo để giữ token ở mức thấp nhất."
        )

    @mcp.tool()
    def submit_and_verify(task_id: str) -> str:
        """Verify task changes (Empty Diff -> Scope -> Anti-Gaming -> rtk Commands) and commit or return trimmed error."""
        db, guard, verifier, _compiler = _sprint_deps()
        task = db.get_task_spec(task_id)
        row = db.get_task_row(task_id)
        if not task or not row:
            return f"ERROR: Unknown task_id `{task_id}`."

        db.update_task_status(task_id, "VERIFYING")
        checkpoint = row["checkpoint_sha"] or guard.create_checkpoint()
        attempt = int(row["retry_count"])
        result = verifier.verify(task)

        db.record_run(
            task_id=task_id,
            attempt=attempt,
            model_used="antigravity-ide",
            effort_used="interactive",
            usage=TokenUsage(),
            duration_sec=0.0,
            verdict=result.verdict,
            error_hash=result.error_hash,
            trimmed_error_log=result.trimmed_error_log or None,
        )

        if result.passed:
            sha = guard.commit_task(task.task_id, task.title, task.allowed_write_files + task.create_new_files)
            db.update_task_status(task_id, "DONE", commit_sha=sha)
            return f"PASS - Committed {sha}. Hãy mở phiên chat mới trước khi gọi `get_next_task()`."

        if result.verdict in ("SCOPE_VIOLATION", "TEST_GAMING"):
            guard.hard_rollback(checkpoint)

        next_retry = attempt + 1
        if next_retry > config.limits.max_retries_per_task:
            guard.hard_rollback(checkpoint)
            db.update_task_status(task_id, "BLOCKED", retry_count=next_retry, last_error=result.trimmed_error_log)
            return (
                f"BLOCKED ({result.verdict}): Vượt quá {config.limits.max_retries_per_task} lần retry. "
                f"Đã rollback về {checkpoint[:8]}.\n{result.trimmed_error_log}"
            )

        db.update_task_status(
            task_id,
            "RUNNING",
            retry_count=next_retry,
            last_error=result.trimmed_error_log,
            last_error_hash=result.error_hash,
        )
        return (
            f"FAIL ({result.verdict}) - Retry {next_retry}/{config.limits.max_retries_per_task}:\n"
            f"{result.trimmed_error_log}"
        )

    @mcp.tool()
    def get_sprint_status() -> str:
        """Return summary of DONE / PENDING / BLOCKED tasks and token consumption."""
        db, _guard, _verifier, _compiler = _sprint_deps()
        summary = db.get_sprint_summary()
        return (
            f"Sprint Status: {summary['counts']} | "
            f"Tokens: prompt={summary['prompt_tokens']}, completion={summary['completion_tokens']} | "
            f"Cost: ${summary['cost_usd']:.4f}"
        )

    @mcp.tool()
    def rollback_current_task(task_id: str, reason: str) -> str:
        """Rollback task to its clean checkpoint SHA and mark it BLOCKED."""
        db, guard, _verifier, _compiler = _sprint_deps()
        row = db.get_task_row(task_id)
        if not row:
            return f"ERROR: Unknown task_id `{task_id}`."
        checkpoint = row["checkpoint_sha"]
        if checkpoint:
            guard.hard_rollback(checkpoint)
        db.update_task_status(task_id, "BLOCKED", last_error=reason)
        return f"ROLLED_BACK `{task_id}` to {checkpoint or 'HEAD'} and marked BLOCKED: {reason}"

    return mcp


def run_mcp_server(config_path: Path | None = None, *, ide_only: bool = False) -> None:
    """Start the FastMCP server on stdio transport."""
    server = create_mcp_server(config_path, ide_only=ide_only)
    logger.info("Starting auto-prompt-architect FastMCP server (stdio, ide_only=%s)", ide_only)
    server.run(transport="stdio")
