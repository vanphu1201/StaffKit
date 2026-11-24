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
    plugin_root = Path(__file__).resolve().parents[1] / "antigravity-plugin" / "auto-prompt-architect"
    manifest = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "2.0.0"
    assert (plugin_root / manifest["logo"]).is_file()

    rules_text = (plugin_root / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    rules_tok = estimate_tokens(rules_text)

    skills = sorted(p.parent.name for p in (plugin_root / "skills").glob("*/SKILL.md"))
    assert skills == ["audit-and-evolve", "brainstorm-to-product", "deep-fix"]

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
