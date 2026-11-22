"""Unit and API integration tests for the local Desktop App Starlette backend."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from engine.database import StateDB
from engine.models import TaskSpec
from engine.ui.server import create_ui_app


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Bot"],
        ["config", "user.email", "bot@example.com"],
    ):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)


def test_ui_index_state_config_approve_and_rollback(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    _init_git_repo(repo)
    cfg_path = tmp_path / "config.yaml"

    app = create_ui_app(cfg_path)
    client = TestClient(app)

    # 1. Serve HTML desktop UI
    home = client.get("/")
    assert home.status_code == 200
    assert "Auto-Prompt Engine" in home.text

    # 2. Initial state
    st = client.get("/api/state")
    assert st.status_code == 200
    payload = st.json()
    assert "tools" in payload and "config" in payload

    # 3. Save updated config pointing to temp git repo -> creates .git/info/exclude
    cfg = payload["config"]
    cfg["project"]["target_repo"] = repo.as_posix()
    saved = client.post("/api/config", json=cfg)
    assert saved.status_code == 200
    assert saved.json()["is_git_repo"] is True
    assert ".auto-engine/" in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")

    # 4. Seed task into state.db, test /api/approve and /api/tasks/T-01/rollback
    db = StateDB(repo / ".auto-engine" / "state.db")
    t1 = TaskSpec(task_id="T-01", title="Edit app", objective="Update x", allowed_write_files=["app.py"])
    db.replace_plan([t1], plan_hash="ui-test", approved=False)
    chk = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()
    db.update_task_status("T-01", "RUNNING", checkpoint_sha=chk)
    (repo / "app.py").write_text("x = 999\n", encoding="utf-8")

    app_res = client.post("/api/approve")
    assert app_res.status_code == 200
    assert app_res.json()["plan_approved"] is True

    rb_res = client.post("/api/tasks/T-01/rollback")
    assert rb_res.status_code == 200
    assert rb_res.json()["tasks"][0]["status"] == "BLOCKED"
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 1\n"

    rt_res = client.post("/api/tasks/T-01/retry")
    assert rt_res.status_code == 200
    assert rt_res.json()["tasks"][0]["status"] == "PENDING"

    # 5. Stop flag endpoint & verify Verifier symbol is imported cleanly
    stop_res = client.post("/api/stop")
    assert stop_res.status_code == 200
    assert stop_res.json()["stopping"] is True

    import engine.ui.server as ui_server

    assert hasattr(ui_server, "Verifier")

    with patch("engine.ui.server.list_agy_models", return_value=[{"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)"}]):
        m_res = client.get("/api/agy/models")
        assert m_res.status_code == 200
        assert m_res.json()["models"][0]["id"] == "gemini-4-neon-high"

    # 6. Live traces and mid-sprint Human-in-the-Loop interaction API
    controller = app.state.controller
    controller.emit_trace({"phase": "executor", "kind": "prompt_sent", "title": "P1", "content": "Hello"})
    st2 = client.get("/api/state").json()
    assert len(st2["traces"]) == 1
    assert st2["traces"][0]["title"] == "P1"
    assert st2["pending_interaction"] is None

    import threading

    result_box: list[dict | None] = []

    def _wait_for_user() -> None:
        result_box.append(controller.request_user_input("T-01", "Need asset", ["Model path?"]))

    t = threading.Thread(target=_wait_for_user, daemon=True)
    t.start()

    for _ in range(30):
        cur_state = client.get("/api/state").json()
        if cur_state["pending_interaction"] is not None:
            break
        import time

        time.sleep(0.05)

    assert client.get("/api/state").json()["pending_interaction"]["task_id"] == "T-01"
    ans_res = client.post("/api/interaction/answer", json={"answers": {"Model path?": "D:/models/jacket.glb"}})
    assert ans_res.status_code == 200
    assert ans_res.json()["pending_interaction"] is None
    t.join(timeout=2)
    assert result_box == [{"Model path?": "D:/models/jacket.glb"}]
