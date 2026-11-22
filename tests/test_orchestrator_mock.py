"""End-to-end 0-token dry-run orchestrator & planner tests covering all 7 spec scenarios."""

import json
import subprocess
from pathlib import Path

from engine.compiler import PromptCompiler
from engine.database import StateDB
from engine.git_guard import GitGuard
from engine.indexer import RepoIndexer
from engine.models import (
    AgyConfig,
    AgyTierConfig,
    EngineConfig,
    ExecutionResult,
    GuardsConfig,
    LimitsConfig,
    ProjectConfig,
    SprintConfig,
    TaskSpec,
    TokenUsage,
    ToolsConfig,
)
from engine.orchestrator import Orchestrator
from engine.planner import Planner
from engine.verifier import Verifier


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Bot"],
        ["config", "user.email", "bot@example.com"],
    ):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_base.py").write_text(
        "from src.base import VALUE\n\ndef test_val():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)


def _make_config(repo: Path, **limit_overrides) -> EngineConfig:
    return EngineConfig(
        project=ProjectConfig(target_repo=str(repo)),
        agy=AgyConfig(
            dynamic_mcp_gating=False,
            tiers={
                "planner": AgyTierConfig(model="m-plan", effort="high"),
                "coder_low": AgyTierConfig(model="m-low", effort="low"),
                "coder_medium": AgyTierConfig(model="m-med", effort="medium"),
                "escalation": AgyTierConfig(model="m-esc", effort="high"),
            },
        ),
        limits=LimitsConfig(**limit_overrides),
        guards=GuardsConfig(),
        sprint=SprintConfig(
            require_plan_approval=True,
            final_verify_commands=["python -m pytest -q"],
        ),
        tools=ToolsConfig(use_rtk=False),
    )


def test_scenario_1_happy_path_virtual_files_and_final_gate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cfg = _make_config(repo)
    db = StateDB(cfg.state_db_path)
    guard = GitGuard(repo, ignore_artifacts=cfg.guards.ignore_artifacts)
    verifier = Verifier(cfg, guard)
    compiler = PromptCompiler(cfg)

    # T-01 creates src/helper.py; T-02 depends on T-01 and modifies src/helper.py (virtual_files(T) validation)
    plan_json = json.dumps(
        {
            "status": "READY",
            "tasks": [
                {
                    "task_id": "T-01",
                    "title": "Create helper",
                    "complexity": "low",
                    "depends_on": [],
                    "objective": "Create src/helper.py",
                    "allowed_write_files": ["src/helper.py"],
                    "create_new_files": ["src/helper.py"],
                    "verify_commands": ["python -c \"import sys; sys.path.insert(0,'.'); import src.helper\""],
                },
                {
                    "task_id": "T-02",
                    "title": "Extend helper",
                    "complexity": "medium",
                    "depends_on": ["T-01"],
                    "objective": "Add double() to src/helper.py",
                    "allowed_write_files": ["src/helper.py"],
                    "readonly_context_files": ["src/base.py"],
                    "verify_commands": ["python -c \"import src.helper; assert src.helper.double(2)==4\""],
                },
            ],
        }
    )
    (repo / ".auto-engine").mkdir(exist_ok=True)
    (repo / ".auto-engine" / "repo_map.xml").write_text("<repomix/>", encoding="utf-8")

    indexer = RepoIndexer(cfg)
    indexer.build_repo_map = lambda force=False: repo / ".auto-engine" / "repo_map.xml"  # type: ignore[method-assign]

    prd = tmp_path / "prd.md"
    prd.write_text("Build helper", encoding="utf-8")
    planner = Planner(cfg, db, indexer, llm_caller=lambda _p, _s: plan_json)
    out = planner.plan_from_prd(prd, auto_approve=True)
    assert out.status == "READY" and len(out.tasks) == 2

    tiers_seen: list[tuple[str, str, str]] = []

    class MockExec:
        def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
            tiers_seen.append((task.task_id, model, effort))
            if task.task_id == "T-01":
                (repo / "src" / "helper.py").write_text("def double(x):\n    return x\n", encoding="utf-8")
            else:
                (repo / "src" / "helper.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
            return ExecutionResult(success=True, model_used=model, effort_used=effort, usage=TokenUsage(prompt_tokens=10, completion_tokens=5))

    orch = Orchestrator(cfg, db, guard, verifier, compiler, executor=MockExec())
    res = orch.run_sprint()
    assert res["halted_reason"] is None
    assert res["final_verify_passed"] is True
    assert res["summary"]["counts"]["DONE"] == 2
    assert tiers_seen == [("T-01", "m-low", "low"), ("T-02", "m-med", "medium")]


def test_scenario_2_scope_and_anti_gaming_rollback_then_pass(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cfg = _make_config(repo, max_retries_per_task=2)
    db = StateDB(cfg.state_db_path)
    guard = GitGuard(repo, ignore_artifacts=cfg.guards.ignore_artifacts)
    verifier = Verifier(cfg, guard)
    compiler = PromptCompiler(cfg)

    t1 = TaskSpec(
        task_id="T-01",
        title="Add tested func",
        complexity="low",
        objective="Add test_extra.py",
        allowed_write_files=["src/base.py", "tests/test_extra.py"],
        create_new_files=["tests/test_extra.py"],
        verify_commands=["python -m pytest -q"],
    )
    db.replace_plan([t1], plan_hash="p2", approved=True)

    seen_prompts: list[str] = []

    class MockExec:
        def __init__(self) -> None:
            self.step = 0

        def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
            seen_prompts.append(prompt)
            self.step += 1
            if self.step == 1:
                # Untracked new test containing pytest.mark.skip -> TEST_GAMING
                (repo / "tests" / "test_extra.py").write_text(
                    "import pytest\n@pytest.mark.skip\ndef test_x():\n    assert True\n",
                    encoding="utf-8",
                )
            elif self.step == 2:
                # Edit forbidden file -> SCOPE_VIOLATION
                (repo / "tests" / "test_base.py").write_text("assert True\n", encoding="utf-8")
            else:
                (repo / "tests" / "test_extra.py").write_text("def test_x():\n    assert 1 == 1\n", encoding="utf-8")
            return ExecutionResult(success=True, model_used=model, effort_used=effort)

    orch = Orchestrator(cfg, db, guard, verifier, compiler, executor=MockExec())
    res = orch.run_sprint()
    assert res["summary"]["counts"]["DONE"] == 1
    assert "TEST_GAMING" in seen_prompts[1]
    assert "SCOPE_VIOLATION" in seen_prompts[2]


def test_scenario_3_and_4_stagnation_and_global_circuit_breaker(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cfg = _make_config(repo, max_retries_per_task=2, max_consecutive_blocked_tasks=3)
    db = StateDB(cfg.state_db_path)
    guard = GitGuard(repo, ignore_artifacts=cfg.guards.ignore_artifacts)
    verifier = Verifier(cfg, guard)
    compiler = PromptCompiler(cfg)

    tasks = [
        TaskSpec(
            task_id=f"T-0{i}",
            title=f"Broken {i}",
            complexity="low",
            objective="Will fail",
            allowed_write_files=["src/base.py"],
            verify_commands=["python -c \"import sys; print('Error constant'); sys.exit(1)\""],
        )
        for i in range(1, 5)
    ]
    db.replace_plan(tasks, plan_hash="p3", approved=True)

    class AlwaysSameFailExec:
        def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
            if task.task_id == "T-01":
                # Triggers EMPTY_DIFF on attempt 0 -> escalates straight to m-esc, then BLOCKED
                return ExecutionResult(success=True, model_used=model, effort_used=effort)
            (repo / "src" / "base.py").write_text(f"VALUE = {task.task_id[-1]}\n", encoding="utf-8")
            return ExecutionResult(success=True, model_used=model, effort_used=effort)

    orch = Orchestrator(cfg, db, guard, verifier, compiler, executor=AlwaysSameFailExec())
    res = orch.run_sprint()
    assert res["halted_reason"] == "GLOBAL_CIRCUIT_BREAKER"
    # Stopped after 3 consecutive BLOCKED tasks; T-04 never ran
    assert res["summary"]["counts"]["BLOCKED"] == 3
    assert res["summary"]["counts"]["PENDING"] == 1
    assert res["summary"]["verdicts"].get("IDENTICAL_ERROR", 0) >= 1


def test_scenario_5_crash_recovery_resume(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cfg = _make_config(repo)
    db = StateDB(cfg.state_db_path)
    guard = GitGuard(repo, ignore_artifacts=cfg.guards.ignore_artifacts)
    verifier = Verifier(cfg, guard)
    compiler = PromptCompiler(cfg)

    t1 = TaskSpec(
        task_id="T-01",
        title="Recover me",
        complexity="low",
        objective="Fix",
        allowed_write_files=["src/base.py"],
        verify_commands=["python -m pytest -q"],
    )
    db.replace_plan([t1], plan_hash="p5", approved=True)
    chk = guard.create_checkpoint()
    db.update_task_status("T-01", "RUNNING", checkpoint_sha=chk)
    (repo / "src" / "base.py").write_text("CORRUPTED = True\n", encoding="utf-8")

    class CleanExec:
        def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
            # Verify base.py was rolled back to clean VALUE = 1 before this run!
            assert "VALUE = 1" in prompt
            (repo / "src" / "base.py").write_text("VALUE = 1\n# clean comment\n", encoding="utf-8")
            return ExecutionResult(success=True, model_used=model, effort_used=effort)

    orch = Orchestrator(cfg, db, guard, verifier, compiler, executor=CleanExec())
    res = orch.run_sprint()
    assert res["recovered_tasks"] == 1
    assert res["summary"]["counts"]["DONE"] == 1
    assert res["summary"]["verdicts"]["CRASH_RECOVERED"] == 1


def test_scenario_6_ambiguous_prd_returns_questions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cfg = _make_config(repo)
    db = StateDB(cfg.state_db_path)
    (repo / ".auto-engine").mkdir(exist_ok=True)
    (repo / ".auto-engine" / "repo_map.xml").write_text("<repomix/>", encoding="utf-8")
    indexer = RepoIndexer(cfg)
    indexer.build_repo_map = lambda force=False: repo / ".auto-engine" / "repo_map.xml"  # type: ignore[method-assign]

    ambiguous_resp = json.dumps(
        {
            "status": "NEEDS_CLARIFICATION",
            "clarifying_questions": ["Which auth algorithm (HS256/RS256)?", "Token TTL?"],
            "tasks": [],
        }
    )
    prd = tmp_path / "prd.md"
    prd.write_text("Add auth", encoding="utf-8")
    planner = Planner(cfg, db, indexer, llm_caller=lambda _p, _s: ambiguous_resp)
    out = planner.plan_from_prd(prd)
    assert out.status == "NEEDS_CLARIFICATION"
    assert len(out.clarifying_questions) == 2
    assert db.list_tasks() == []


def test_scenario_7_ignore_artifacts_across_retries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cfg = _make_config(repo, max_retries_per_task=1)
    db = StateDB(cfg.state_db_path)
    guard = GitGuard(repo, ignore_artifacts=cfg.guards.ignore_artifacts)
    verifier = Verifier(cfg, guard)
    compiler = PromptCompiler(cfg)

    t1 = TaskSpec(
        task_id="T-01",
        title="Artifact tolerance",
        complexity="low",
        objective="Update base.py VALUE to 2",
        allowed_write_files=["src/base.py"],
        verify_commands=["python -c \"import src.base; assert src.base.VALUE == 2\""],
    )
    db.replace_plan([t1], plan_hash="p7", approved=True)

    class ArtifactExec:
        def __init__(self) -> None:
            self.n = 0

        def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
            self.n += 1
            if self.n == 1:
                # Wrong value + leaves behind test-runner cache artifacts
                (repo / "src" / "base.py").write_text("VALUE = 999\n", encoding="utf-8")
                (repo / ".pytest_cache").mkdir(exist_ok=True)
                (repo / ".pytest_cache" / "v.txt").write_text("cache", encoding="utf-8")
                (repo / "coverage").mkdir(exist_ok=True)
                (repo / "coverage" / "lcov.info").write_text("cov", encoding="utf-8")
            else:
                (repo / "src" / "base.py").write_text("VALUE = 2\n", encoding="utf-8")
            return ExecutionResult(success=True, model_used=model, effort_used=effort)

    orch = Orchestrator(cfg, db, guard, verifier, compiler, executor=ArtifactExec())
    res = orch.run_sprint()
    assert res["summary"]["counts"]["DONE"] == 1
    assert res["summary"]["verdicts"].get("SCOPE_VIOLATION", 0) == 0


def test_orchestrator_mid_sprint_hitl_saves_answers_and_injects_into_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cfg = _make_config(repo)
    db = StateDB(cfg.state_db_path)
    guard = GitGuard(repo, ignore_artifacts=cfg.guards.ignore_artifacts)
    verifier = Verifier(cfg, guard)
    compiler = PromptCompiler(cfg)

    t1 = TaskSpec(
        task_id="T-HITL",
        title="Configure real brand & 3D assets",
        complexity="low",
        objective="Write config values from user",
        allowed_write_files=["src/base.py"],
        user_questions=["So dien thoai Zalo that cua shop la gi?", "Duong dan file model 3D .glb?"],
        verify_commands=["python -c \"import src.base; assert src.base.VALUE == 7\""],
    )
    db.replace_plan([t1], plan_hash="p-hitl", approved=True)

    captured_prompts: list[str] = []
    traces: list[dict] = []

    class HitlExec:
        def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
            captured_prompts.append(prompt)
            (repo / "src" / "base.py").write_text("VALUE = 7\n", encoding="utf-8")
            return ExecutionResult(success=True, model_used=model, effort_used=effort)

    def _ask_user(task_id: str, title: str, questions: list[str]) -> dict[str, str]:
        assert task_id == "T-HITL"
        assert len(questions) == 2
        return {
            questions[0]: "0988777666",
            questions[1]: "C:/assets/shirt.glb",
        }

    orch = Orchestrator(
        cfg,
        db,
        guard,
        verifier,
        compiler,
        executor=HitlExec(),
        on_trace=traces.append,
        ask_user=_ask_user,
    )
    res = orch.run_sprint()
    assert res["summary"]["counts"]["DONE"] == 1
    assert "0988777666" in captured_prompts[0]
    assert "C:/assets/shirt.glb" in captured_prompts[0]
    assert (repo / ".auto-engine" / "user_answers.json").is_file()
    trace_kinds = [t["kind"] for t in traces]
    assert "user_question" in trace_kinds
    assert "user_answer" in trace_kinds
    assert "verify_result" in trace_kinds


def test_orchestrator_dynamic_need_user_input_and_runtime_gate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cfg = _make_config(repo, max_retries_per_task=0)
    cfg.sprint.runtime_verify_commands = ["python -c \"import src.runtime_cfg; assert src.runtime_cfg.VALUE == 42\""]
    db = StateDB(cfg.state_db_path)
    guard = GitGuard(repo, ignore_artifacts=cfg.guards.ignore_artifacts)
    verifier = Verifier(cfg, guard)
    compiler = PromptCompiler(cfg)

    t1 = TaskSpec(
        task_id="T-DYN",
        title="Dynamic escalation",
        complexity="low",
        objective="Produce real runtime_cfg VALUE",
        allowed_write_files=["src/runtime_cfg.py"],
        create_new_files=["src/runtime_cfg.py"],
        verify_commands=["python -c \"import src.runtime_cfg; assert src.runtime_cfg.VALUE == 42\""],
    )
    db.replace_plan([t1], plan_hash="p-dyn", approved=True)

    seen_prompts: list[str] = []
    calls = {"n": 0}

    class DynExec:
        def execute(self, prompt: str, task: TaskSpec, model: str, effort: str) -> ExecutionResult:
            seen_prompts.append(prompt)
            calls["n"] += 1
            if calls["n"] == 1:
                stream = "\n".join(
                    [
                        json.dumps({"event": "step_update", "step_update": {"text_delta": "NEED_USER_INPUT: Nhap "}}),
                        json.dumps({"event": "step_update", "step_update": {"text_delta": "ma bi mat thuc te?"}}),
                    ]
                )
                return ExecutionResult(success=True, model_used=model, effort_used=effort, raw_output=stream)
            (repo / "src" / "runtime_cfg.py").write_text("VALUE = 42\n", encoding="utf-8")
            return ExecutionResult(success=True, model_used=model, effort_used=effort)

    orch = Orchestrator(
        cfg,
        db,
        guard,
        verifier,
        compiler,
        executor=DynExec(),
        ask_user=lambda _tid, _t, qs: {qs[0]: "SECRET-PROD-42"},
    )
    res = orch.run_sprint()
    assert res["summary"]["counts"]["DONE"] == 1
    assert res["final_verify_passed"] is True
    assert calls["n"] == 2
    assert "SECRET-PROD-42" in seen_prompts[1]
