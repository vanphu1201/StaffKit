"""Tests for SQLite WAL state management and sprint ledger."""

from pathlib import Path

from engine.database import StateDB
from engine.models import TaskSpec, TokenUsage


def _make_task(task_id: str, depends_on: list[str] | None = None, complexity: str = "low") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        complexity=complexity,  # type: ignore[arg-type]
        depends_on=depends_on or [],
        objective=f"Implement {task_id}",
        allowed_write_files=["src/app.py"],
        verify_commands=["pytest"],
    )


def test_wal_and_task_dag_lifecycle(tmp_path: Path) -> None:
    db = StateDB(tmp_path / ".auto-engine" / "state.db")
    t1 = _make_task("T-01")
    t2 = _make_task("T-02", depends_on=["T-01"], complexity="medium")

    db.replace_plan([t1, t2], plan_hash="abc123", approved=False)
    assert db.get_meta("plan_approved") == "0"
    db.set_meta("plan_approved", "1")
    assert db.get_meta("plan_approved") == "1"

    ready = db.get_next_ready_task()
    assert ready is not None and ready.task_id == "T-01"

    db.update_task_status("T-01", "RUNNING", checkpoint_sha="sha0")
    assert len(db.get_stale_active_tasks()) == 1

    db.record_run(
        task_id="T-01",
        attempt=0,
        model_used="gemini-2.5-flash",
        effort_used="low",
        usage=TokenUsage(prompt_tokens=100, completion_tokens=50, cost_usd=0.01),
        duration_sec=1.2,
        verdict="PASS",
    )
    db.update_task_status("T-01", "DONE", commit_sha="sha1")

    ready2 = db.get_next_ready_task()
    assert ready2 is not None and ready2.task_id == "T-02"

    summary = db.get_sprint_summary()
    assert summary["counts"]["DONE"] == 1
    assert summary["counts"]["PENDING"] == 1
    assert summary["prompt_tokens"] == 100
    assert summary["completion_tokens"] == 50
    assert summary["verdicts"]["PASS"] == 1


def test_new_verdicts_and_crash_recovery(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db")
    t1 = _make_task("T-01")
    db.replace_plan([t1], plan_hash="h1", approved=True)

    db.update_task_status("T-01", "VERIFYING", checkpoint_sha="chk1")
    stale = db.get_stale_active_tasks()
    assert len(stale) == 1 and stale[0]["checkpoint_sha"] == "chk1"

    for idx, verdict in enumerate(("MAX_TURNS_EXCEEDED", "TEST_GAMING", "CRASH_RECOVERED")):
        db.record_run(
            task_id="T-01",
            attempt=idx,
            model_used="m",
            effort_used="medium",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, cost_usd=0.0),
            duration_sec=0.5,
            verdict=verdict,  # type: ignore[arg-type]
            error_hash=f"hash-{idx}",
            trimmed_error_log=f"log-{idx}",
        )

    runs = db.list_runs("T-01")
    assert [r["verdict"] for r in runs] == ["MAX_TURNS_EXCEEDED", "TEST_GAMING", "CRASH_RECOVERED"]
