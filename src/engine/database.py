"""SQLite WAL state persistence for tasks, runs, token ledger, and sprint metadata."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from engine.models import TaskSpec, TaskStatus, TokenUsage, Verdict

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sprint_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    complexity TEXT NOT NULL CHECK (complexity IN ('low', 'medium', 'high')) DEFAULT 'medium',
    spec_json TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'RUNNING', 'VERIFYING', 'DONE', 'BLOCKED')
    ) DEFAULT 'PENDING',
    retry_count INTEGER NOT NULL DEFAULT 0,
    total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    total_completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    last_error TEXT,
    last_error_hash TEXT,
    checkpoint_sha TEXT,
    commit_sha TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt INTEGER NOT NULL,
    model_used TEXT NOT NULL,
    effort_used TEXT DEFAULT 'medium',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    duration_sec REAL DEFAULT 0.0,
    verdict TEXT NOT NULL CHECK (
        verdict IN (
            'PASS',
            'SCOPE_VIOLATION',
            'TEST_GAMING',
            'EMPTY_DIFF',
            'IDENTICAL_ERROR',
            'VERIFY_FAIL',
            'TIMEOUT',
            'MAX_TURNS_EXCEEDED',
            'BUDGET_EXCEEDED',
            'CRASH_RECOVERED'
        )
    ),
    error_hash TEXT,
    trimmed_error_log TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class StateDB:
    """Manages SQLite WAL database state for an Auto-Prompt Engine sprint."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        """Create tables if they do not exist."""
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a sprint metadata key-value entry."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sprint_meta (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Retrieve a sprint metadata value or return default."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sprint_meta WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else default

    def upsert_task(self, task: TaskSpec) -> None:
        """Insert or update a single TaskSpec (used for auto-synthesized final/runtime repair tasks)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (task_id, title, complexity, spec_json, depends_on_json, status)
                VALUES (?, ?, ?, ?, ?, 'PENDING')
                ON CONFLICT(task_id) DO UPDATE SET
                    title = excluded.title,
                    complexity = excluded.complexity,
                    spec_json = excluded.spec_json,
                    depends_on_json = excluded.depends_on_json,
                    status = 'PENDING',
                    retry_count = 0,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    task.task_id,
                    task.title,
                    task.complexity,
                    task.model_dump_json(),
                    json.dumps(task.depends_on),
                ),
            )

    def replace_plan(
        self,
        tasks: list[TaskSpec],
        plan_hash: str,
        approved: bool = False,
    ) -> None:
        """Replace existing plan tasks/runs cleanly and initialize sprint approval state."""
        with self._connect() as conn:
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM tasks")
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO tasks (task_id, title, complexity, spec_json, depends_on_json, status)
                    VALUES (?, ?, ?, ?, ?, 'PENDING')
                    """,
                    (
                        task.task_id,
                        task.title,
                        task.complexity,
                        task.model_dump_json(),
                        json.dumps(task.depends_on),
                    ),
                )
        self.set_meta("plan_hash", plan_hash)
        self.set_meta("plan_approved", "1" if approved else "0")
        self.set_meta("consecutive_blocked", "0")

    def upsert_tasks(self, tasks: list[TaskSpec]) -> None:
        """Insert or update TaskSpec entries without wiping unrelated runs."""
        with self._connect() as conn:
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO tasks (task_id, title, complexity, spec_json, depends_on_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        title = excluded.title,
                        complexity = excluded.complexity,
                        spec_json = excluded.spec_json,
                        depends_on_json = excluded.depends_on_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        task.task_id,
                        task.title,
                        task.complexity,
                        task.model_dump_json(),
                        json.dumps(task.depends_on),
                    ),
                )

    def get_task_row(self, task_id: str) -> dict | None:
        """Fetch raw task dictionary by task_id."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def get_task_spec(self, task_id: str) -> TaskSpec | None:
        """Deserialize TaskSpec for task_id if present."""
        row = self.get_task_row(task_id)
        if not row:
            return None
        return TaskSpec.model_validate_json(row["spec_json"])

    def list_tasks(self) -> list[dict]:
        """List all tasks ordered by insertion/task_id."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY rowid ASC").fetchall()
            return [dict(r) for r in rows]

    def get_ready_tasks(self, limit: int = 1) -> list[TaskSpec]:
        """Return up to `limit` file-disjoint PENDING tasks whose dependencies are all DONE."""
        rows = self.list_tasks()
        done_ids = {r["task_id"] for r in rows if r["status"] == "DONE"}
        ready: list[TaskSpec] = []
        claimed_files: set[str] = set()

        for row in rows:
            if row["status"] != "PENDING":
                continue
            deps: list[str] = json.loads(row["depends_on_json"])
            if not all(dep in done_ids for dep in deps):
                continue
            spec = TaskSpec.model_validate_json(row["spec_json"])
            if limit <= 1:
                return [spec]
            if spec.user_questions and ready:
                continue
            task_files = set(spec.allowed_write_files) | set(spec.create_new_files)
            if ready and (task_files & claimed_files):
                continue
            ready.append(spec)
            claimed_files.update(task_files)
            if spec.user_questions or len(ready) >= limit:
                break
        return ready

    def get_next_ready_task(self) -> TaskSpec | None:
        """Return the first PENDING task whose dependencies are all DONE."""
        batch = self.get_ready_tasks(limit=1)
        return batch[0] if batch else None

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        checkpoint_sha: str | None = None,
        commit_sha: str | None = None,
        retry_count: int | None = None,
        last_error: str | None = None,
        last_error_hash: str | None = None,
    ) -> None:
        """Update task status and optional lifecycle metadata."""
        clauses = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        params: list[object] = [status]

        if status == "DONE":
            if last_error is None:
                last_error = ""
            if last_error_hash is None:
                last_error_hash = ""

        optional_fields = {
            "checkpoint_sha": checkpoint_sha,
            "commit_sha": commit_sha,
            "retry_count": retry_count,
            "last_error": last_error,
            "last_error_hash": last_error_hash,
        }
        for col, val in optional_fields.items():
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)

        params.append(task_id)
        sql = f"UPDATE tasks SET {', '.join(clauses)} WHERE task_id = ?"
        with self._connect() as conn:
            conn.execute(sql, params)

    def record_run(
        self,
        task_id: str,
        attempt: int,
        model_used: str,
        effort_used: str,
        usage: TokenUsage,
        duration_sec: float,
        verdict: Verdict,
        *,
        error_hash: str | None = None,
        trimmed_error_log: str | None = None,
    ) -> None:
        """Insert an execution attempt into runs and accumulate totals on tasks."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    task_id, attempt, model_used, effort_used,
                    prompt_tokens, completion_tokens, cost_usd, duration_sec,
                    verdict, error_hash, trimmed_error_log
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    attempt,
                    model_used,
                    effort_used,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.cost_usd,
                    duration_sec,
                    verdict,
                    error_hash,
                    trimmed_error_log,
                ),
            )
            conn.execute(
                """
                UPDATE tasks
                SET total_prompt_tokens = total_prompt_tokens + ?,
                    total_completion_tokens = total_completion_tokens + ?,
                    total_cost_usd = total_cost_usd + ?,
                    last_error = COALESCE(?, last_error),
                    last_error_hash = COALESCE(?, last_error_hash),
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.cost_usd,
                    trimmed_error_log,
                    error_hash,
                    task_id,
                ),
            )

    def list_runs(self, task_id: str | None = None) -> list[dict]:
        """Return run history optionally filtered by task_id."""
        with self._connect() as conn:
            if task_id:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE task_id = ? ORDER BY id ASC",
                    (task_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM runs ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def get_stale_active_tasks(self) -> list[dict]:
        """Return tasks left in RUNNING or VERIFYING after an unclean exit."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('RUNNING', 'VERIFYING') ORDER BY rowid ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def record_phase_usage(self, phase: str, usage: TokenUsage) -> None:
        """Accumulate non-task phase token usage (interviewer/planner) in sprint_meta."""
        if usage.total_tokens <= 0 and usage.cost_usd <= 0.0:
            return
        prefix = f"phase_{phase.strip().lower()}"
        with self._connect() as conn:
            for suffix, delta in (
                ("prompt_tokens", usage.prompt_tokens),
                ("completion_tokens", usage.completion_tokens),
            ):
                key = f"{prefix}_{suffix}"
                row = conn.execute("SELECT value FROM sprint_meta WHERE key = ?", (key,)).fetchone()
                cur_val = int(row["value"]) if row else 0
                conn.execute(
                    """
                    INSERT INTO sprint_meta (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (key, str(cur_val + int(delta))),
                )

    def get_total_cost_usd(self) -> float:
        """Sum cost across all tasks in the current sprint."""
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(total_cost_usd), 0.0) AS total FROM tasks").fetchone()
            return float(row["total"]) if row else 0.0

    def get_sprint_summary(self) -> dict:
        """Aggregate counts, token usage, cost, and verdict breakdowns."""
        with self._connect() as conn:
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
            ).fetchall()
            counts = {s: 0 for s in ("PENDING", "RUNNING", "VERIFYING", "DONE", "BLOCKED")}
            for row in status_rows:
                counts[row["status"]] = int(row["cnt"])

            totals = conn.execute(
                """
                SELECT
                    COALESCE(SUM(total_prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(total_completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(total_cost_usd), 0.0) AS cost_usd
                FROM tasks
                """
            ).fetchone()

            verdict_rows = conn.execute(
                "SELECT verdict, COUNT(*) AS cnt FROM runs GROUP BY verdict"
            ).fetchall()
            verdicts = {r["verdict"]: int(r["cnt"]) for r in verdict_rows}

            dur_row = conn.execute(
                "SELECT COALESCE(SUM(duration_sec), 0.0) AS total_duration_sec FROM runs"
            ).fetchone()
            total_duration_sec = float(dur_row["total_duration_sec"]) if dur_row else 0.0

            meta_rows = conn.execute(
                "SELECT key, value FROM sprint_meta WHERE key LIKE 'phase_%'"
            ).fetchall()
            phase_prompt = sum(int(r["value"]) for r in meta_rows if r["key"].endswith("_prompt_tokens"))
            phase_completion = sum(int(r["value"]) for r in meta_rows if r["key"].endswith("_completion_tokens"))

        p_tok = int(totals["prompt_tokens"]) if totals else 0
        c_tok = int(totals["completion_tokens"]) if totals else 0
        raw_cost = float(totals["cost_usd"]) if totals else 0.0
        total_in = p_tok + phase_prompt
        total_out = c_tok + phase_completion
        # When CLI uses a Pro subscription (cost_usd == 0), provide transparent API-equivalent cost estimate
        est_cost = round((total_in * 1.25 + total_out * 5.0) / 1_000_000.0, 4)
        cost_is_estimated = raw_cost <= 0.0 and (total_in + total_out) > 0

        return {
            "counts": counts,
            "total_tasks": sum(counts.values()),
            "prompt_tokens": p_tok,
            "completion_tokens": c_tok,
            "planning_prompt_tokens": phase_prompt,
            "planning_completion_tokens": phase_completion,
            "cost_usd": raw_cost,
            "estimated_cost_usd": raw_cost if raw_cost > 0.0 else est_cost,
            "cost_is_estimated": cost_is_estimated,
            "duration_sec": round(total_duration_sec, 1),
            "verdicts": verdicts,
        }
