"""Core v2.0.0 tests for Auto-Prompt Architect Antigravity IDE Native Plugin."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from engine.bridges import mcp_server
from engine.evaluator import audit_historical_commits, run_plugin_micro_evals
from engine.indexer import list_real_files, locate_relevant_files
from engine.models import estimate_tokens


def _init_git_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local.dev"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_dir), check=True, capture_output=True)


def test_plugin_manifest_and_always_on_overhead_under_1000_tokens() -> None:
    plugin_root = Path(__file__).resolve().parents[1] / "antigravity-plugin" / "staffkit"
    manifest = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "staffkit"
    assert manifest["displayName"] == "StaffKit"
    assert manifest["version"] == "3.0.1"
    assert (plugin_root / manifest["logo"]).is_file()

    rules_text = (plugin_root / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    rules_tok = estimate_tokens(rules_text)

    skills = sorted(p.parent.name for p in (plugin_root / "skills").glob("*/SKILL.md"))
    assert skills == ["audit-and-evolve", "brainstorm-to-product", "deep-fix", "pr-review", "refactor-safe"]

    mcp = mcp_server.create_mcp_server(None)

    async def _measure() -> tuple[int, int]:
        tools = await mcp.list_tools()
        schema_str = json.dumps(
            [{"name": t.name, "desc": t.description, "params": getattr(t, "parameters", {})} for t in tools],
            ensure_ascii=False,
        )
        return len(tools), estimate_tokens(schema_str)

    tool_count, schema_tok = asyncio.run(_measure())
    assert tool_count == 6
    assert (rules_tok + schema_tok) < 1000, f"Always-on overhead {rules_tok + schema_tok} >= 1000 tok"


def test_1hop_import_graph_esm_and_reverse_importers(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "src" / "ai").mkdir(parents=True)
    (tmp_path / "src" / "server").mkdir(parents=True)
    (tmp_path / "src" / "web").mkdir(parents=True)

    (tmp_path / "src" / "ai" / "webchatClient.ts").write_text(
        "import { parseStream } from './streamParser.js';\n"
        "export async function sendWebchatPrompt(msg: string) { return parseStream(msg); }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "ai" / "streamParser.ts").write_text(
        "export function parseStream(raw: string) { return raw.trim(); }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "server" / "chatRoute.ts").write_text(
        "import { sendWebchatPrompt } from '../ai/webchatClient.js';\n"
        "export function routeChat(q: string) { return sendWebchatPrompt(q); }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "web" / "ChatBox.tsx").write_text(
        "import { routeChat } from '@/server/chatRoute';\n"
        "export const ChatBox = () => routeChat('hi');\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    real_files = list_real_files(tmp_path)
    located = locate_relevant_files("fix sendWebchatPrompt bug", real_files, tmp_path)
    neighborhood = set(located.target_files) | set(located.readonly_files)

    assert "src/ai/webchatClient.ts" in located.target_files
    # Forward ESM .js -> .ts import
    assert "src/ai/streamParser.ts" in neighborhood
    # Reverse 1-hop importer
    assert "src/server/chatRoute.ts" in neighborhood


def test_cross_project_global_playbook_persistence(tmp_path: Path) -> None:
    fake_playbook = tmp_path / "global_playbooks" / "global_lessons.md"

    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    mcp = mcp_server.create_mcp_server(None, global_playbook_path=fake_playbook)

    async def _run() -> tuple[str, str]:
        rec_fn = (await mcp.get_tool("record_lesson")).fn
        mem_fn = (await mcp.get_tool("load_project_memory")).fn
        rec_res = rec_fn(
            title="Windows copytree rule",
            summary="Always use dirs_exist_ok=True instead of rmtree on live plugin dirs.",
            repo_path=str(repo_a),
            global_playbook=True,
        )
        mem_b = mem_fn(repo_path=str(repo_b))
        return rec_res, mem_b

    rec_res, mem_b = asyncio.run(_run())
    assert "global playbook" in rec_res.lower()
    assert fake_playbook.is_file()
    assert "Windows copytree rule" in mem_b


def test_plugin_micro_evals_all_pass() -> None:
    report = run_plugin_micro_evals()
    assert report.total_count == 3
    assert report.all_passed, f"Micro-eval failed: {[s.model_dump() for s in report.scenarios if not s.passed]}"


def test_audit_historical_commits_runs_cleanly_on_self() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    report = audit_historical_commits(repo_root, limit=5)
    assert report.commits_evaluated >= 1
    assert report.avg_recall >= 0.5


def test_verify_and_smoke_rejects_http_500_false_green(tmp_path: Path) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _FailHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"internal error")

        def log_message(self, fmt: str, *args: object) -> None:
            return

    srv = HTTPServer(("127.0.0.1", 0), _FailHandler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        mcp = mcp_server.create_mcp_server(None)

        async def _probe() -> str:
            fn = (await mcp.get_tool("verify_and_smoke")).fn
            return fn(
                repo_path=str(tmp_path),
                commands=[],
                smoke_url=f"http://127.0.0.1:{port}/api/health",
                expected_status=200,
                generate_delivery_pack=False,
            )

        out = asyncio.run(_probe())
        assert "HTTP Endpoint Probe: **FAIL**" in out
        assert "**OVERALL: FAIL**" in out
    finally:
        srv.shutdown()
        srv.server_close()


def test_verify_and_smoke_catches_diff_sanity_anti_mock_and_skip(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "app.ts").write_text("export const ok = true;\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    # Introduce forbidden @ts-ignore & TODO: mock placeholder in working tree
    (tmp_path / "app.ts").write_text(
        "// @ts-ignore\n// TODO: mock response\nexport const ok = true;\n",
        encoding="utf-8",
    )

    mcp = mcp_server.create_mcp_server(None)

    async def _probe() -> str:
        fn = (await mcp.get_tool("verify_and_smoke")).fn
        return fn(repo_path=str(tmp_path), commands=[], run_smoke=False, generate_delivery_pack=False)

    out = asyncio.run(_probe())
    assert "Diff Sanity Gate (anti-mock/anti-skip): **FAIL**" in out
    assert "**OVERALL: FAIL**" in out


def test_load_project_memory_include_audit_tech_debt_xray(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    big_code = "\n".join(f"x_{i} = {i}" for i in range(470)) + "\n# TODO: refactor later\n"
    (tmp_path / "bloated.py").write_text(big_code, encoding="utf-8")
    (tmp_path / "test_bloated.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    mcp = mcp_server.create_mcp_server(None)

    async def _probe() -> str:
        fn = (await mcp.get_tool("load_project_memory")).fn
        return fn(repo_path=str(tmp_path), include_audit=True)

    out = asyncio.run(_probe())
    assert "0-Token Tech-Debt & Health X-Ray" in out
    assert "bloated.py" in out
    assert "TODO/FIXME/Stub Markers" in out


def test_modular_locator_and_ast_scanner_and_plugin_stats_cli(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from engine.ast_scanner import extract_fast_skeleton, extract_symbol_windows, scan_repo_tech_debt
    from engine.cli import app
    from engine.locator import locate_relevant_files

    assert callable(locate_relevant_files)
    assert callable(extract_fast_skeleton)
    assert callable(extract_symbol_windows)
    assert callable(scan_repo_tech_debt)

    auto_dir = tmp_path / ".auto-engine"
    auto_dir.mkdir(parents=True)
    (auto_dir / "plugin_telemetry.jsonl").write_text(
        json.dumps({"ts": "2026-11-24T10:00:00", "tool": "load_project_memory", "latency_ms": 42.5, "output_tokens": 450, "ok": True})
        + "\n"
        + json.dumps({"ts": "2026-11-24T10:01:00", "tool": "locate_context", "latency_ms": 88.0, "output_tokens": 820, "ok": True})
        + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    res = runner.invoke(app, ["plugin-stats", "--repo", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "load_project_memory" in res.output
    assert "locate_context" in res.output
    assert "2 calls" in res.output


def test_pro_hardening_git_cochange_security_xray_and_clean_default_verify(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "server").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "server" / "billingRoute.ts").write_text("export function chargeInvoice() { return 1; }\n", encoding="utf-8")
    (tmp_path / "web" / "BillingPanel.tsx").write_text("export const BillingPanel = () => 'v1';\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET_KEY=supersecret\n", encoding="utf-8")
    (tmp_path / "server" / "db.ts").write_text(
        "export const DB_URL = 'postgresql://admin:hunter2@db.internal:5432/prod';\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    # 2 commits touching FE & BE together (NO direct import between them)
    for i in (2, 3):
        (tmp_path / "server" / "billingRoute.ts").write_text(f"export function chargeInvoice() {{ return {i}; }}\n", encoding="utf-8")
        (tmp_path / "web" / "BillingPanel.tsx").write_text(f"export const BillingPanel = () => 'v{i}';\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"update billing v{i}"], cwd=str(tmp_path), check=True, capture_output=True)

    real_files = list_real_files(tmp_path)
    located = locate_relevant_files("fix chargeInvoice rounding bug", real_files, tmp_path)
    assert "server/billingRoute.ts" in located.target_files
    # Caught purely via 0-token Git Co-Change!
    assert "web/BillingPanel.tsx" in located.readonly_files

    mcp = mcp_server.create_mcp_server(None)

    async def _check() -> tuple[str, str]:
        mem = (await mcp.get_tool("load_project_memory")).fn(repo_path=str(tmp_path), include_audit=True)
        ver = (await mcp.get_tool("verify_and_smoke")).fn(repo_path=str(tmp_path), commands=[], run_smoke=False)
        return mem, ver

    mem_out, ver_out = asyncio.run(_check())
    assert ".env" in mem_out
    assert "server/db.ts" in mem_out
    assert "**OVERALL: PASS**" in ver_out
    # Default verify_and_smoke MUST NOT pollute pro repos with .bat / .md delivery pack files
    assert not (tmp_path / "CHAY_SAN_PHAM.bat").exists()
    assert not (tmp_path / "HUONG_DAN_SU_DUNG.md").exists()


def test_v3_adr_decisions_and_team_yaml_policy(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001_init.sql").write_text("CREATE TABLE users(id INT);\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    auto_dir = tmp_path / ".auto-engine"
    auto_dir.mkdir(parents=True, exist_ok=True)
    (auto_dir / "team.yaml").write_text(
        "verify_commands: []\n"
        "sensitive_paths:\n  - migrations/\n"
        "forbid_patterns:\n  - 'debugger'\n",
        encoding="utf-8",
    )

    # Touch sensitive migration + forbidden team pattern `debugger`
    (tmp_path / "migrations" / "001_init.sql").write_text("CREATE TABLE users(id INT, email TEXT);\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("debugger;\nexport const x = 2;\n", encoding="utf-8")

    mcp = mcp_server.create_mcp_server(None)

    async def _run() -> tuple[str, str, str]:
        spec_out = (await mcp.get_tool("save_project_spec")).fn(
            repo_path=str(tmp_path),
            decisions_md="## ADR-001: Hybrid OS Agent\n- Chose Shell/API first with Vision fallback.",
        )
        mem_out = (await mcp.get_tool("load_project_memory")).fn(repo_path=str(tmp_path))
        ver_out = (await mcp.get_tool("verify_and_smoke")).fn(
            repo_path=str(tmp_path),
            commands=[],
            run_smoke=False,
        )
        return spec_out, mem_out, ver_out

    spec_out, mem_out, ver_out = asyncio.run(_run())
    assert "decisions.md" in spec_out
    assert "ADR-001: Hybrid OS Agent" in mem_out
    assert "Team Policy (`team.yaml`)" in mem_out
    assert "Diff Sanity Gate (anti-mock/anti-skip): **FAIL**" in ver_out
    assert "Team Sensitive-Zone Warning" in ver_out
    assert "migrations/001_init.sql" in ver_out


def test_core_smoke_commands_blocks_core_flow_regression(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "downloader.py").write_text("def download_drive_video(): return False  # regressed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    import sys

    py_exe = f'"{sys.executable}"' if " " in sys.executable else sys.executable
    auto_dir = tmp_path / ".auto-engine"
    auto_dir.mkdir(parents=True, exist_ok=True)
    (auto_dir / "team.yaml").write_text(
        f'core_smoke_commands:\n  - \'{py_exe} -c "import downloader; assert downloader.download_drive_video() is True"\'\n',
        encoding="utf-8",
    )

    mcp = mcp_server.create_mcp_server(None)

    async def _probe() -> str:
        return (await mcp.get_tool("verify_and_smoke")).fn(
            repo_path=str(tmp_path),
            commands=[],
            run_smoke=False,
        )

    out = asyncio.run(_probe())
    assert "Core-Flow E2E Smoke Gate" in out
    assert "**FAIL**" in out
    assert "**OVERALL: FAIL**" in out
