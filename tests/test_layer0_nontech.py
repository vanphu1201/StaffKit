"""Tests for Layer 0 Non-Tech discovery, zero-token project bootstrapper, and wizard UI routes."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from engine.blueprints import load_blueprints
from engine.bootstrapper import bootstrap_project, detect_existing_stack
from engine.interviewer import ProductInterviewer
from engine.models import EngineConfig, InterviewOutput, PlanOutput, inline_json_schema
from engine.ui.server import create_ui_app


def test_bootstrap_python_app_greenfield_creates_git_and_passing_smoke(tmp_path: Path) -> None:
    proj = tmp_path / "ai-video-tool"
    res = bootstrap_project(proj, stack="python_app")

    assert res.is_new_repo is True
    assert res.stack == "python_app"
    assert (proj / ".git").exists()
    assert (proj / ".git" / "info" / "exclude").is_file()
    assert ".auto-engine/" in (proj / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert (proj / "pyproject.toml").is_file()
    assert (proj / "src" / "app.py").is_file()
    assert (proj / "tests" / "test_smoke.py").is_file()

    # Generated smoke test passes cleanly with 0 modifications
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(proj),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_bootstrap_react_offline_fallback_and_existing_stack_detection(tmp_path: Path) -> None:
    web_dir = tmp_path / "hr-web"
    with patch("shutil.which", return_value=None):
        res = bootstrap_project(web_dir, stack="web_react")

    assert res.stack == "web_react"
    assert (web_dir / ".git").exists()
    assert (web_dir / "package.json").is_file()
    assert (web_dir / "tsconfig.json").is_file()
    assert (web_dir / "src" / "App.tsx").is_file()

    detected, cmds, _, preview = detect_existing_stack(web_dir)
    assert detected == "web_react"
    assert "npx tsc --noEmit" in cmds
    assert "npm run build" in cmds
    assert preview == "npm run dev"


def test_bootstrap_fullstack_web_creates_real_backend_and_prisma(tmp_path: Path) -> None:
    fs_dir = tmp_path / "real-shop"
    res = bootstrap_project(fs_dir, stack="fullstack_web", install_deps=False)

    assert res.stack == "fullstack_web"
    assert (fs_dir / "server" / "index.ts").is_file()
    assert (fs_dir / "prisma" / "schema.prisma").is_file()
    assert (fs_dir / "src" / "lib" / "utils.ts").is_file()
    assert (fs_dir / "vite.config.ts").is_file()
    assert (fs_dir / ".env").is_file()

    detected, cmds, _, preview = detect_existing_stack(fs_dir)
    assert detected == "fullstack_web"
    assert "npx tsc --noEmit" in cmds
    assert "npm run build" in cmds
    assert preview == "npm run dev"


def test_blueprints_and_generic_bootstrap_support_any_tech_product(tmp_path: Path) -> None:
    built_ins = {bp.id: bp for bp in load_blueprints()}
    assert {"generic", "fullstack_web", "web_react", "python_app"} <= set(built_ins.keys())

    # Custom target blueprint overrides cleanly without editing Python code
    custom_repo = tmp_path / "rust-cli"
    (custom_repo / ".auto-engine").mkdir(parents=True)
    (custom_repo / ".auto-engine" / "blueprint.yaml").write_text(
        'id: rust_cli\nlabel: "Rust CLI"\nverify_commands:\n  - "cargo test"\nforbid_patterns:\n  - "unimplemented!\\\\("\n',
        encoding="utf-8",
    )
    loaded = {bp.id: bp for bp in load_blueprints(custom_repo)}
    assert "rust_cli" in loaded
    assert loaded["rust_cli"].verify_commands == ["cargo test"]

    # Generic stack initializes clean Git repo without forcing React/Python templates
    gen_dir = tmp_path / "telegram-bot"
    res = bootstrap_project(gen_dir, stack="generic")
    assert res.stack == "generic"
    assert (gen_dir / ".git").exists()
    assert not (gen_dir / "package.json").exists()
    assert not (gen_dir / "pyproject.toml").exists()


def test_interviewer_parses_stream_json_for_interview_and_spec(tmp_path: Path) -> None:
    cfg = EngineConfig()
    cfg.project.target_repo = str(tmp_path)

    interview_payload = {
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "response": json.dumps(
                {
                    "recommended_stack": "web_react",
                    "suggested_project_name": "ai-hr-portal",
                    "product_summary_vi": "Website quản lý nhân viên có AI gợi ý đánh giá.",
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Bạn cần quản lý thông tin gì của nhân viên?",
                            "options": ["Hồ sơ & Chấm công", "Hồ sơ & KPI", "Tất cả"],
                            "default_answer": "Tất cả",
                        }
                    ],
                }
            ),
        },
    }
    spec_payload = {
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "response": json.dumps(
                {
                    "product_title": "Hệ thống Quản lý Nhân sự AI",
                    "features_vi": ["Danh sách nhân viên", "AI tóm tắt hiệu suất", "Xuất CSV"],
                    "prd_markdown": "# PRD\n## Mục tiêu\nXây dựng HR AI.",
                }
            ),
        },
    }

    responses = [json.dumps(interview_payload), json.dumps(spec_payload)]
    interviewer = ProductInterviewer(cfg, llm_caller=lambda _p, _s, _e: responses.pop(0))

    iv = interviewer.generate_interview("tạo web quản lý nhân viên có AI")
    assert iv.recommended_stack == "web_react"
    assert iv.suggested_project_name == "ai-hr-portal"
    assert len(iv.questions) == 1

    spec = interviewer.synthesize_spec("tạo web quản lý nhân viên có AI", {"q1": "Tất cả"}, "web_react")
    assert "Danh sách nhân viên" in spec.features_vi
    assert "# PRD" in spec.prd_markdown


def test_ui_wizard_endpoints_and_reset(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    app = create_ui_app(cfg_path)
    client = TestClient(app)

    home = client.get("/")
    assert home.status_code == 200
    assert "Tạo sản phẩm mới" in home.text
    assert "Prompt Sửa nhanh" in home.text

    st = client.get("/api/state").json()
    assert st["wizard"]["step"] == 1

    bad = client.post("/api/wizard/interview", json={"idea": "   "})
    assert bad.status_code == 400

    rst = client.post("/api/wizard/reset")
    assert rst.status_code == 200
    assert rst.json()["wizard"]["step"] == 1


def test_inline_json_schema_removes_refs_and_defs() -> None:
    for raw_schema in (PlanOutput.model_json_schema(), InterviewOutput.model_json_schema()):
        dumped = json.dumps(inline_json_schema(raw_schema))
        assert '"$ref"' not in dumped
        assert '"$defs"' not in dumped


def test_reconcile_project_entrypoints_and_nontech_delivery_guide(tmp_path: Path) -> None:
    from engine.bootstrapper import generate_nontech_delivery_guide, reconcile_project_entrypoints

    proj = tmp_path / "fullstack-demo"
    (proj / "server" / "src").mkdir(parents=True)
    (proj / "cli" / "src").mkdir(parents=True)
    (proj / "web").mkdir(parents=True)
    (proj / "dist" / "web").mkdir(parents=True)

    (proj / "server" / "src" / "app.ts").write_text("console.log('ok')\n", encoding="utf-8")
    (proj / "cli" / "src" / "batch.ts").write_text("console.log('batch')\n", encoding="utf-8")
    (proj / "web" / "vite.config.ts").write_text("export default {}\n", encoding="utf-8")
    (proj / "dist" / "web" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (proj / "package.json").write_text(
        json.dumps(
            {
                "name": "fullstack-demo",
                "scripts": {
                    "dev": "tsx watch server/src/index.ts",
                    "dev:web": "vite",
                    "build": "tsc --noEmit && vite build",
                    "batch": "tsx cli/batch.ts",
                },
            }
        ),
        encoding="utf-8",
    )

    fixes = reconcile_project_entrypoints(proj)
    assert len(fixes) >= 4
    updated_pkg = json.loads((proj / "package.json").read_text(encoding="utf-8"))
    assert updated_pkg["scripts"]["dev"] == "tsx watch server/src/app.ts"
    assert updated_pkg["scripts"]["start"] == "tsx server/src/app.ts"
    assert updated_pkg["scripts"]["batch"] == "tsx cli/src/batch.ts"
    assert "--config web/vite.config.ts" in updated_pkg["scripts"]["build"]
    assert "--config web/vite.config.ts" in updated_pkg["scripts"]["dev:web"]

    guide = generate_nontech_delivery_guide(proj, write_files=True)
    assert guide["run_command"] == "npm start"
    assert guide["web_url"] == "http://127.0.0.1:3001"
    assert (proj / "CHAY_SAN_PHAM.bat").is_file()
    assert (proj / "HUONG_DAN_SU_DUNG.md").is_file()

