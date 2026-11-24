"""Zero-token Plugin Evaluator: Historical Commit Replay + 3-Scenario Plugin Micro-Eval Harness."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from engine.indexer import (
    _is_doc_or_meta_path,
    _is_test_path,
    extract_symbol_windows,
    list_real_files,
    locate_relevant_files,
)
from engine.models import EngineConfig, estimate_tokens, normalize_posix_path


class CommitAuditRow(BaseModel):
    """Zero-token historical replay measurement for one ground-truth git commit."""

    commit_sha: str
    subject: str
    actual_files: list[str] = Field(default_factory=list)
    located_targets: list[str] = Field(default_factory=list)
    located_readonly: list[str] = Field(default_factory=list)
    target_hit: bool = False
    neighborhood_hit: bool = False
    recall: float = 0.0
    precision: float = 0.0
    window_hit: bool | None = None
    context_tokens: int = 0
    soft_scope_ok: bool = False


class RepoAuditReport(BaseModel):
    """Aggregated 0-token historical replay audit report across real repository commits."""

    repo_name: str
    commits_evaluated: int = 0
    target_hit_rate: float = 0.0
    neighborhood_hit_rate: float = 0.0
    avg_recall: float = 0.0
    avg_precision: float = 0.0
    avg_targets_count: float = 0.0
    window_hit_rate: float = 1.0
    avg_context_tokens: int = 0
    max_context_tokens: int = 0
    soft_scope_pass_rate: float = 0.0
    rows: list[CommitAuditRow] = Field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Auto-Prompt Architect 0-Token Historical Replay Audit: `{self.repo_name}`",
            f"- Commits Evaluated: `{self.commits_evaluated}`",
            f"- Target Hit Rate (`actual in targets`): `{self.target_hit_rate * 100:.1f}%`",
            f"- Neighborhood Hit Rate (`actual in targets+readonly`): `{self.neighborhood_hit_rate * 100:.1f}%`",
            f"- Avg Recall / Precision: `{self.avg_recall * 100:.1f}%` / `{self.avg_precision * 100:.1f}%`",
            f"- Avg Located Targets / Commit: `{self.avg_targets_count:.2f}`",
            f"- Window Hit Rate (files > 300 lines): `{self.window_hit_rate * 100:.1f}%`",
            f"- Avg / Max Context Tokens: `{self.avg_context_tokens:,}` / `{self.max_context_tokens:,}`",
            f"- Soft-Scope Pass Rate: `{self.soft_scope_pass_rate * 100:.1f}%`",
            "",
            "## Commit Breakdown",
            "| SHA | Subject | Actual | Located Targets | Recall | Precision | Window | Context Tok | Soft-Scope |",
            "|---|---|---:|---:|---:|---:|---|---:|---|",
        ]
        for r in self.rows:
            win_str = "-" if r.window_hit is None else ("HIT" if r.window_hit else "MISS")
            lines.append(
                f"| `{r.commit_sha[:8]}` | {r.subject[:52].replace('|', '/')} | {len(r.actual_files)} | "
                f"{len(r.located_targets)} | {r.recall * 100:.0f}% | {r.precision * 100:.0f}% | "
                f"{win_str} | {r.context_tokens:,} | {'PASS' if r.soft_scope_ok else 'FAIL'} |"
            )
        return "\n".join(lines) + "\n"


def audit_historical_commits(
    repo_path: Path,
    *,
    limit: int = 20,
    config: EngineConfig | None = None,
) -> RepoAuditReport:
    """Replay up to `limit` historical non-merge commits (0 LLM tokens) to score Locator, Windows & Scope."""
    resolved_repo = repo_path.resolve()
    real_files = list_real_files(resolved_repo)
    cfg = config.model_copy(deep=True) if config is not None else EngineConfig()
    cfg.project.target_repo = str(resolved_repo)

    log_proc = subprocess.run(
        ["git", "log", "--no-merges", "-n", str(max(10, limit * 5)), "--pretty=format:%H%x09%s%x09%b%x1e"],
        cwd=str(resolved_repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )

    root_manifests = {
        "package.json", "package-lock.json", "pyproject.toml",
        "uv.lock", "tsconfig.json", "requirements.txt",
    }
    rows: list[CommitAuditRow] = []
    for raw_rec in log_proc.stdout.split("\x1e"):
        if len(rows) >= limit:
            break
        rec = raw_rec.strip()
        if not rec:
            continue
        parts = rec.split("\t", 2)
        sha = parts[0].strip()
        subject = parts[1].strip() if len(parts) > 1 else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        commit_msg = f"{subject}\n\n{body}".strip() if body else subject
        if len(commit_msg) < 20:
            continue

        diff_proc = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
            cwd=str(resolved_repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if diff_proc.returncode != 0:
            continue
        actual_code = [
            normalize_posix_path(f)
            for f in diff_proc.stdout.splitlines()
            if f.strip()
            and normalize_posix_path(f) in real_files
            and not _is_test_path(f)
            and not _is_doc_or_meta_path(f)
        ]
        if not (1 <= len(actual_code) <= 6):
            continue

        located = locate_relevant_files(commit_msg, real_files, resolved_repo)
        actual_set = set(actual_code)
        targets_set = set(located.target_files)
        nbr_set = targets_set | set(located.readonly_files)

        target_hit = bool(actual_set & targets_set)
        neighborhood_hit = bool(actual_set & nbr_set)
        recall = len(actual_set & nbr_set) / len(actual_set)
        precision = (len(actual_set & targets_set) / len(targets_set)) if targets_set else 0.0

        win_hits: list[bool] = []
        est_context_chars = 0
        for rel in located.target_files[:5]:
            fp = resolved_repo / rel
            if fp.is_file():
                est_context_chars += len(extract_symbol_windows(fp, located.identifiers, radius=18, max_total_lines=90))

        for f_rel in actual_code:
            full_p = resolved_repo / f_rel
            if not full_p.is_file():
                continue
            line_cnt = len(full_p.read_text(encoding="utf-8", errors="replace").splitlines())
            if line_cnt <= cfg.limits.inline_file_max_lines:
                continue
            show_proc = subprocess.run(
                ["git", "show", "--unified=0", sha, "--", f_rel],
                cwd=str(resolved_repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            touched_lines: set[int] = set()
            for m in re.finditer(r"@@\s*-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s*@@", show_proc.stdout):
                s_ln = int(m.group(1))
                cnt = int(m.group(2)) if m.group(2) is not None else 1
                touched_lines.update(range(s_ln, s_ln + max(1, cnt)))
            if touched_lines:
                win_txt = extract_symbol_windows(
                    full_p,
                    located.identifiers,
                    radius=25,
                    max_total_lines=160,
                )
                covered = {int(n) for n in re.findall(r"^L(\d+):", win_txt, flags=re.MULTILINE)}
                win_hits.append(bool(covered & touched_lines))
        window_hit = any(win_hits) if win_hits else None

        allowed_dirs = {
            PurePosixPath(p).parent.as_posix()
            for p in targets_set
            if PurePosixPath(p).parent.as_posix() != "."
        }
        soft_scope_ok = all(
            (f in targets_set or f in root_manifests or PurePosixPath(f).parent.as_posix() in allowed_dirs)
            for f in actual_code
        )

        rows.append(
            CommitAuditRow(
                commit_sha=sha,
                subject=subject,
                actual_files=actual_code,
                located_targets=located.target_files,
                located_readonly=located.readonly_files,
                target_hit=target_hit,
                neighborhood_hit=neighborhood_hit,
                recall=round(recall, 4),
                precision=round(precision, 4),
                window_hit=window_hit,
                context_tokens=estimate_tokens("x" * est_context_chars) if est_context_chars else 0,
                soft_scope_ok=soft_scope_ok,
            )
        )

    n = len(rows)
    if n == 0:
        return RepoAuditReport(repo_name=resolved_repo.name)
    win_rows = [r for r in rows if r.window_hit is not None]
    toks = [r.context_tokens for r in rows]
    return RepoAuditReport(
        repo_name=resolved_repo.name,
        commits_evaluated=n,
        target_hit_rate=round(sum(1 for r in rows if r.target_hit) / n, 4),
        neighborhood_hit_rate=round(sum(1 for r in rows if r.neighborhood_hit) / n, 4),
        avg_recall=round(sum(r.recall for r in rows) / n, 4),
        avg_precision=round(sum(r.precision for r in rows) / n, 4),
        avg_targets_count=round(sum(len(r.located_targets) for r in rows) / n, 2),
        window_hit_rate=round((sum(1 for r in win_rows if r.window_hit) / len(win_rows)), 4) if win_rows else 1.0,
        avg_context_tokens=round(sum(toks) / n),
        max_context_tokens=max(toks),
        soft_scope_pass_rate=round(sum(1 for r in rows if r.soft_scope_ok) / n, 4),
        rows=rows,
    )


class MicroEvalScenarioResult(BaseModel):
    """Result of one fast deterministic 0-token plugin micro-eval scenario."""

    id: str
    title: str
    passed: bool
    duration_ms: float
    details: str


class PluginMicroEvalReport(BaseModel):
    """Aggregated 3-scenario plugin micro-eval report."""

    generated_at: str
    passed_count: int
    total_count: int
    all_passed: bool
    scenarios: list[MicroEvalScenarioResult] = Field(default_factory=list)


def _init_temp_git_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "eval@local.test"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Eval"], cwd=str(repo_dir), check=True, capture_output=True)


def run_plugin_micro_evals() -> PluginMicroEvalReport:
    """Run 3 deterministic 0-token end-to-end plugin scenarios against the live 6-tool FastMCP server."""
    from engine.bridges.mcp_server import create_mcp_server

    async def _run_all() -> list[MicroEvalScenarioResult]:
        mcp = create_mcp_server(None)
        tools_list = await mcp.list_tools()
        tool_names = {t.name for t in tools_list}
        expected_tools = {
            "load_project_memory",
            "locate_context",
            "save_project_spec",
            "record_lesson",
            "verify_and_smoke",
            "snapshot_or_undo",
        }
        fn_map = {name: (await mcp.get_tool(name)).fn for name in expected_tools if name in tool_names}
        results: list[MicroEvalScenarioResult] = []

        # Scenario 1: Brainstorm Spec Persistence + 1-Hop Import Graph Context Localization (ESM .js -> .ts + Reverse Importer)
        t0 = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="apa_eval_s1_") as td:
                repo = Path(td)
                _init_temp_git_repo(repo)
                (repo / "src" / "ai").mkdir(parents=True)
                (repo / "src" / "server").mkdir(parents=True)
                (repo / "src" / "ai" / "webchatClient.ts").write_text(
                    "export async function sendWebchatPrompt(msg: string) { return msg; }\n",
                    encoding="utf-8",
                )
                (repo / "src" / "server" / "api.ts").write_text(
                    "import { sendWebchatPrompt } from '../ai/webchatClient.js';\n"
                    "export function handleChat(q: string) { return sendWebchatPrompt(q); }\n",
                    encoding="utf-8",
                )
                subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

                spec_out = fn_map["save_project_spec"](
                    repo_path=str(repo),
                    constitution_md="# Constitution\n- Zero mock responses.",
                    contracts_md="# Contracts\n- POST /api/chat -> { reply: string }",
                )
                loc_out = fn_map["locate_context"](
                    query="fix sendWebchatPrompt timeout",
                    repo_path=str(repo),
                )
                mem_out = fn_map["load_project_memory"](repo_path=str(repo))
                ok = (
                    "SAVED_SPEC:" in spec_out
                    and "src/ai/webchatClient.ts" in loc_out
                    and "src/server/api.ts" in loc_out
                    and "Zero mock responses" in mem_out
                )
                details = (
                    "Specs persisted & 1-hop reverse importer (api.ts -> webchatClient.ts) localized"
                    if ok
                    else f"Unexpected output: loc={loc_out[:120]}"
                )
        except Exception as exc:
            ok = False
            details = f"Exception: {exc}"
        results.append(
            MicroEvalScenarioResult(
                id="S1-SPEC-AND-GRAPH-LOCATOR",
                title="Spec Persistence + 1-Hop Import Graph Localization",
                passed=ok,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
                details=details,
            )
        )

        # Scenario 2: Anti-False-Green Live HTTP Runtime Smoke + Snapshot/Undo Rollback
        t1 = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="apa_eval_s2_") as td:
                repo = Path(td)
                _init_temp_git_repo(repo)
                (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
                subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

                snap_msg = fn_map["snapshot_or_undo"](action="snapshot", repo_path=str(repo))
                (repo / "app.py").write_text("VALUE = 999  # broken\n", encoding="utf-8")
                undo_msg = fn_map["snapshot_or_undo"](action="undo", repo_path=str(repo))
                restored_ok = (repo / "app.py").read_text(encoding="utf-8").strip() == "VALUE = 1"

                # Spin up a tiny live HTTP server to verify real behavioral probe via verify_and_smoke
                class _Handler(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"status":"ok"}')

                    def log_message(self, fmt: str, *args: object) -> None:
                        return

                srv = HTTPServer(("127.0.0.1", 0), _Handler)
                port = srv.server_address[1]
                th = threading.Thread(target=srv.serve_forever, daemon=True)
                th.start()
                try:
                    py_exe = f'"{sys.executable}"' if " " in sys.executable else sys.executable
                    smoke_out = fn_map["verify_and_smoke"](
                        repo_path=str(repo),
                        commands=[
                            f'{py_exe} -c "import app, urllib.request; assert app.VALUE == 1; '
                            f'r = urllib.request.urlopen(\'http://127.0.0.1:{port}/health\'); assert r.status == 200"'
                        ],
                        run_smoke=False,
                        generate_delivery_pack=False,
                    )
                finally:
                    srv.shutdown()
                    srv.server_close()

                ok = (
                    "ERROR" not in snap_msg
                    and "ERROR" not in undo_msg
                    and restored_ok
                    and "**OVERALL: PASS**" in smoke_out
                )
                details = (
                    "Snapshot/undo restored repo cleanly & live HTTP 200 behavioral smoke passed"
                    if ok
                    else f"Failed: restored={restored_ok}, snap={snap_msg[:60]}, smoke={smoke_out[:140]}"
                )
        except Exception as exc:
            ok = False
            details = f"Exception: {exc}"
        results.append(
            MicroEvalScenarioResult(
                id="S2-SMOKE-AND-ROLLBACK",
                title="Live HTTP Behavioral Smoke + Snapshot/Undo",
                passed=ok,
                duration_ms=round((time.monotonic() - t1) * 1000, 1),
                details=details,
            )
        )

        # Scenario 3: Cross-Project Global Playbook + Handoff Retention Across Fresh Sessions
        t2 = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="apa_eval_s3_") as td:
                repo = Path(td)
                _init_temp_git_repo(repo)
                (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
                subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

                rec_out = fn_map["record_lesson"](
                    title="ESM NodeNext .js import rule",
                    summary="Always resolve relative .js imports to .ts/.tsx in NodeNext repos.",
                    repo_path=str(repo),
                    touched_files=["src/server/api.ts"],
                    global_playbook=False,
                )
                mem_out = fn_map["load_project_memory"](repo_path=str(repo))
                ok = (
                    len(tool_names) == 6
                    and "RECORDED_LESSON" in rec_out
                    and "ESM NodeNext .js import rule" in mem_out
                    and "src/server/api.ts" in mem_out
                )
                details = (
                    "Exactly 6 lean MCP tools; lesson & handoff persisted and reloaded cleanly"
                    if ok
                    else f"Failed: tools={len(tool_names)}, rec={rec_out[:120]}"
                )
        except Exception as exc:
            ok = False
            details = f"Exception: {exc}"
        results.append(
            MicroEvalScenarioResult(
                id="S3-MEMORY-AND-PLAYBOOK",
                title="6-Tool Surface + Lesson & Handoff Retention",
                passed=ok,
                duration_ms=round((time.monotonic() - t2) * 1000, 1),
                details=details,
            )
        )

        return results

    scenarios = asyncio.run(_run_all())
    passed_count = sum(1 for s in scenarios if s.passed)
    return PluginMicroEvalReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        passed_count=passed_count,
        total_count=len(scenarios),
        all_passed=(passed_count == len(scenarios)),
        scenarios=scenarios,
    )
