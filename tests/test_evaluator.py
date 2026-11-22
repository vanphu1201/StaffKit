"""Tests for the A/B Hidden-Test Evaluation Harness (`evals/cases` & `engine.evaluator`)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from engine.cli import app
from engine.evaluator import (
    EvalCase,
    Evaluator,
    grade_with_hidden_tests,
    load_eval_cases,
    setup_isolated_case_repo,
)
from engine.models import EngineConfig, TokenUsage


def test_load_builtin_eval_cases_and_filter() -> None:
    cases = load_eval_cases()
    assert len(cases) >= 7
    ids = {c.id for c in cases}
    assert {
        "eval-sqlite-ledger",
        "eval-rate-limiter",
        "eval-node-rest-api",
        "eval-cli-log-analyzer",
        "eval-multi-file-pricing",
        "eval-large-inventory-dag",
        "eval-large-pipeline-dag",
    } <= ids

    filtered = load_eval_cases(case_ids=["eval-sqlite-ledger"])
    assert len(filtered) == 1 and filtered[0].id == "eval-sqlite-ledger"


def test_hidden_files_withheld_during_execution_and_catch_fake_impl(tmp_path: Path) -> None:
    cases = {c.id: c for c in load_eval_cases()}
    limiter_case = cases["eval-rate-limiter"]

    # 1. Starter sandbox contains public files only — hidden_tests must NOT exist yet
    repo = setup_isolated_case_repo(limiter_case, tmp_path / "sandbox")
    assert (repo / "src" / "limiter.py").is_file()
    assert (repo / "tests" / "test_existing_limiter.py").is_file()
    assert not (repo / "hidden_tests" / "test_hidden_limiter.py").exists()

    # 2. Naive implementation passes PASS_TO_PASS (existing public tests) but FAILS hidden FAIL_TO_PASS
    resolved_bad, reg_bad, err_bad = grade_with_hidden_tests(limiter_case, repo, timeout_sec=30)
    assert reg_bad is True
    assert resolved_bad is False
    assert "FAIL" in err_bad

    # 3. Genuine sliding-window fix passes BOTH public regression and holdout hidden tests
    (repo / "src" / "limiter.py").write_text(
        """import time
from collections import deque
from typing import Callable

class SlidingWindowLimiter:
    def __init__(self, max_requests: int, window_seconds: float, clock: Callable[[], float] | None = None) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.clock = clock or time.monotonic
        self._buckets: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float] | None:
        bucket = self._buckets.get(key)
        if bucket is None:
            return None
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            del self._buckets[key]
            return None
        return bucket

    def allow_request(self, key: str) -> bool:
        now = self.clock()
        bucket = self._prune(key, now)
        if bucket is not None and len(bucket) >= self.max_requests:
            return False
        if bucket is None:
            bucket = deque()
            self._buckets[key] = bucket
        bucket.append(now)
        return True

    def remaining(self, key: str) -> int:
        now = self.clock()
        bucket = self._prune(key, now)
        active = len(bucket) if bucket is not None else 0
        return max(0, self.max_requests - active)
""",
        encoding="utf-8",
    )
    resolved_ok, reg_ok, err_ok = grade_with_hidden_tests(limiter_case, repo, timeout_sec=30)
    assert reg_ok is True
    assert resolved_ok is True, err_ok


def test_ab_evaluator_computes_metrics_deltas_and_writes_reports(tmp_path: Path) -> None:
    case = EvalCase(
        id="mini-math",
        title="Double input",
        prd_markdown="Return x * 2 from calc.py",
        starter_files={
            "calc.py": "def fn(x):\n    return x\n",
            "test_pub.py": "from calc import fn\ndef test_zero():\n    assert fn(0) == 0\n",
        },
        hidden_files={
            "hidden_test.py": "from calc import fn\ndef test_hidden():\n    assert fn(21) == 42\n",
        },
        hidden_pass_to_pass_commands=["{python} -m pytest test_pub.py -q"],
        hidden_fail_to_pass_commands=["{python} -m pytest hidden_test.py -q"],
    )

    def _mock_baseline(_c: EvalCase, _repo: Path, _cfg: EngineConfig) -> tuple[TokenUsage, float, str]:
        # Leaves calc.py unchanged -> passes PASS_TO_PASS (0 * 2 == 0) but FAILS hidden test!
        return TokenUsage(prompt_tokens=800, completion_tokens=200), 5.0, ""

    def _mock_engine(_c: EvalCase, repo: Path, _cfg: EngineConfig) -> tuple[TokenUsage, float, str]:
        (repo / "calc.py").write_text("def fn(x):\n    return x * 2\n", encoding="utf-8")
        return TokenUsage(prompt_tokens=300, completion_tokens=100), 2.5, ""

    evaluator = Evaluator(EngineConfig(), baseline_runner=_mock_baseline, engine_runner=_mock_engine)
    report = evaluator.run_suite([case], arms=("baseline", "engine"), work_dir=tmp_path / "work")

    assert report.summaries["baseline"].resolve_rate == 0.0
    assert report.summaries["baseline"].regression_pass_rate == 1.0
    assert report.summaries["engine"].resolve_rate == 1.0
    assert report.summaries["engine"].tokens_per_resolved == 400.0
    assert report.resolve_rate_delta == 1.0
    assert report.token_savings_pct == 60.0

    md_path = tmp_path / "reports" / "eval.md"
    json_path = tmp_path / "reports" / "eval.json"
    Evaluator.write_report(report, md_path, json_path=json_path)
    assert "Resolve Rate Delta: `+100.0 pp`" in md_path.read_text(encoding="utf-8")
    assert "Token Savings: `+60.0%`" in md_path.read_text(encoding="utf-8")
    assert json_path.is_file()


def test_cli_eval_command_help() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["eval", "--help"])
    assert res.exit_code == 0
    assert "hidden-test benchmark" in res.stdout.lower()


def test_all_builtin_cases_pass_public_and_fail_hidden_on_starter(tmp_path: Path) -> None:
    for case in load_eval_cases():
        repo = setup_isolated_case_repo(case, tmp_path / case.id)
        resolved, reg_ok, err = grade_with_hidden_tests(case, repo, timeout_sec=30)
        assert reg_ok is True, f"{case.id} public PASS_TO_PASS failed on starter: {err}"
        assert resolved is False, f"{case.id} hidden FAIL_TO_PASS unexpectedly passed on starter!"


def test_ab_evaluator_multi_run_computes_stddev(tmp_path: Path) -> None:
    case = EvalCase(
        id="multi-run-check",
        title="Double input",
        prd_markdown="Return x * 2",
        starter_files={"calc.py": "def fn(x):\n    return x\n"},
        hidden_files={"hidden_test.py": "from calc import fn\ndef test_h():\n    assert fn(5) == 10\n"},
        hidden_fail_to_pass_commands=["{python} -m pytest hidden_test.py -q"],
    )
    seq = {"base": 0, "eng": 0}

    def _base(_c: EvalCase, repo: Path, _cfg: EngineConfig) -> tuple[TokenUsage, float, str]:
        seq["base"] += 1
        (repo / "calc.py").write_text("def fn(x):\n    return x * 2\n", encoding="utf-8")
        return TokenUsage(prompt_tokens=800 + seq["base"] * 100, completion_tokens=100), 1.0, ""

    def _eng(_c: EvalCase, repo: Path, _cfg: EngineConfig) -> tuple[TokenUsage, float, str]:
        seq["eng"] += 1
        (repo / "calc.py").write_text("def fn(x):\n    return x * 2\n", encoding="utf-8")
        return TokenUsage(prompt_tokens=300 + seq["eng"] * 50, completion_tokens=100), 0.5, ""

    report = Evaluator(EngineConfig(), baseline_runner=_base, engine_runner=_eng).run_suite(
        [case],
        arms=("baseline", "engine"),
        runs=2,
        work_dir=tmp_path / "w",
    )
    assert report.summaries["baseline"].runs_per_case == 2
    assert report.summaries["baseline"].total_cases == 2
    assert report.summaries["baseline"].token_stddev > 0
    assert report.summaries["engine"].token_stddev > 0
    md = report.to_markdown()
    assert "Token StdDev" in md


def test_smoke_http_health_script_validates_live_endpoint() -> None:
    import http.server
    import subprocess
    import sys
    import threading
    import json

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # type: ignore[override]
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # type: ignore[override]
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_http_health.py"
        ok_proc = subprocess.run(
            [sys.executable, str(script), "--url", f"http://127.0.0.1:{port}/health", "--expect-json-key", "ok", "--timeout", "5"],
            capture_output=True,
            text=True,
        )
        assert ok_proc.returncode == 0, ok_proc.stderr
        assert "HTTP_SMOKE_OK" in ok_proc.stdout

        bad_proc = subprocess.run(
            [sys.executable, str(script), "--url", f"http://127.0.0.1:{port}/health", "--expect-json-key", "missing_key", "--timeout", "1"],
            capture_output=True,
            text=True,
        )
        assert bad_proc.returncode != 0
    finally:
        server.shutdown()
        server.server_close()
