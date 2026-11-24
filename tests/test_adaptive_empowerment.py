"""Unit tests for Adaptive Empowerment Architecture (dynamic budgets, log offloading, smart watchdog)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from engine.bridges.agy_cli_executor import AgyCliExecutor
from engine.compiler import PromptCompiler, compress_and_offload_objective
from engine.models import (
    AgyConfig,
    EngineConfig,
    LimitsConfig,
    ProjectConfig,
    TaskSpec,
    compute_dynamic_prompt_budget,
    compute_dynamic_tool_call_budget,
    resolve_task_execution_profile,
)


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self.pid = 424242
        self.killed = False

    def poll(self):
        return self.returncode if self.killed else 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


def test_dynamic_budgets_scale_flexibly_by_task_and_profile(tmp_path: Path) -> None:
    cfg = EngineConfig(project=ProjectConfig(target_repo=str(tmp_path)))

    simple_build = TaskSpec(
        task_id="T-01",
        title="Add helper",
        complexity="low",
        objective="Add a small formatting helper",
        allowed_write_files=["src/fmt.ts"],
    )
    complex_fix = TaskSpec(
        task_id="T-02",
        title="Sửa kết nối webchat2api trả 502",
        complexity="high",
        objective="Fix 502 model_catalog_failed on 127.0.0.1:17841",
        allowed_write_files=["server/a.ts", "server/b.ts", "web/c.tsx", "server/tests/a.test.ts"],
        acceptance_criteria=["AC1", "AC2", "AC3", "AC4"],
        readonly_context_files=["shared/schemas.ts", "server/db.ts"],
        execution_profile="investigate_fix",
    )

    assert resolve_task_execution_profile(simple_build) == "build"
    assert resolve_task_execution_profile(complex_fix) == "investigate_fix"

    s_soft, s_hard, s_strict = compute_dynamic_tool_call_budget(simple_build, cfg)
    c_soft, c_hard, c_strict = compute_dynamic_tool_call_budget(complex_fix, cfg, attempt=1)
    assert not s_strict and not c_strict
    assert c_soft > s_soft + 60
    assert c_hard > c_soft

    s_prompt, s_pstrict = compute_dynamic_prompt_budget(simple_build, cfg)
    c_prompt, c_pstrict = compute_dynamic_prompt_budget(complex_fix, cfg, attempt=1)
    assert not s_pstrict and not c_pstrict
    assert c_prompt > s_prompt + 20000

    # Strict small cap remains honored for explicit hard limits
    strict_cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(tmp_path)),
        limits=LimitsConfig(max_tool_calls_per_attempt=5, max_prompt_tokens_per_call=800),
    )
    assert compute_dynamic_tool_call_budget(complex_fix, strict_cfg) == (5, 5, True)
    assert compute_dynamic_prompt_budget(complex_fix, strict_cfg) == (800, True)


def test_raw_log_offloading_deduplicates_and_prevents_prompt_too_large(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    (repo / "server" / "client.ts").write_text("export function ping() { return true; }\n", encoding="utf-8")

    noisy_lines = [
        f'06:02:{i:02d} PM runtime · daemon stderr line: [codex-chatgpt-web] model_catalog_failed {{"request":{i},"at":"2026-11-23T11:02:{i:02d}.006Z","status":502,"elapsedMs":{i}}}'
        for i in range(1, 45)
    ]
    huge_objective = "webchat2api chưa được kết nối:\n" + "\n".join(noisy_lines)
    assert len(huge_objective) > 6000

    cfg = EngineConfig(project=ProjectConfig(target_repo=str(repo)))
    task = TaskSpec(
        task_id="T-01",
        title="Sửa kết nối webchat2api",
        objective=huge_objective,
        allowed_write_files=["server/client.ts"],
        execution_profile="investigate_fix",
    )

    compact_obj = compress_and_offload_objective(task, cfg.auto_engine_dir)
    assert "lặp lại" in compact_obj
    assert ".auto-engine/task_attachments/T-01_objective.txt" in compact_obj
    assert (cfg.auto_engine_dir / "task_attachments" / "T-01_objective.txt").is_file()
    assert len(compact_obj) < 2500

    compiler = PromptCompiler(cfg)
    prompt, tokens = compiler.compile(
        task,
        attempt=1,
        last_verdict="MAX_TURNS_EXCEEDED",
        last_error="previous timeout",
        compact_diff="diff --git a/server/client.ts b/server/client.ts\n+export const ok = 1;",
    )
    assert "INVESTIGATE & FIX" in prompt
    assert "CONTINUATION RESUME" in prompt
    assert "export const ok = 1;" in prompt
    assert tokens < 6000


def test_smart_watchdog_grants_grace_on_progress_and_aborts_stuck_loop(tmp_path: Path) -> None:
    cfg = EngineConfig(
        project=ProjectConfig(target_repo=str(tmp_path)),
        agy=AgyConfig(dynamic_mcp_gating=False),
        limits=LimitsConfig(adaptive_limits=True, max_tool_calls_per_attempt=0, max_tokens_per_task=50000),
    )
    executor = AgyCliExecutor(cfg)
    task = TaskSpec(
        task_id="T-01",
        title="Build feature",
        complexity="low",
        objective="Add helper",
        allowed_write_files=["a.py"],
    )

    # 1. Stuck loop: 5 identical read_file calls in a row -> immediate clean abort
    stuck_events = [
        json.dumps({"type": "tool_call", "tool_name": "read_file", "args": {"Path": "a.py"}})
        for _ in range(5)
    ]
    with (
        patch("subprocess.Popen", return_value=_FakeProc(stuck_events)),
        patch("engine.bridges.agy_cli_executor.kill_process_tree") as kill_mock,
    ):
        res_stuck = executor.execute("prompt", task, "m", "low")
    assert not res_stuck.success and res_stuck.verdict_override == "MAX_TURNS_EXCEEDED"
    assert "stuck loop" in res_stuck.error_message.lower()
    assert kill_mock.called

    # 2. Productive progress past soft budget (low + 1 file -> soft=45): alternating reads & writes up to 52 calls PASSES
    prod_events = []
    for idx in range(52):
        tool = "edit_file" if idx % 4 == 0 else "read_file"
        prod_events.append(
            json.dumps({"type": "tool_call", "tool_name": tool, "args": {"Path": f"f_{idx}.py"}})
        )
    with patch("subprocess.Popen", return_value=_FakeProc(prod_events)):
        res_ok = executor.execute("prompt", task, "m", "low")
    assert res_ok.success, f"Expected productive run to get grace extension, got: {res_ok.error_message}"


def test_multi_round_interviewer_proactive_suggestions_and_constitution(tmp_path: Path) -> None:
    from engine.interviewer import ProductInterviewer, persist_project_constitution
    from engine.models import InterviewOutput, InterviewQuestion, ProactiveSuggestion, ProductSpec

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("export const ok = true;\n", encoding="utf-8")

    cfg = EngineConfig(project=ProjectConfig(target_repo=str(repo)))
    prompts_seen: list[str] = []

    def _fake_llm(prompt: str, _schema: Path, _effort: str) -> str:
        prompts_seen.append(prompt)
        if "VÒNG HỎI ĐÀO SÂU THỨ 2" in prompt:
            out2 = InterviewOutput(
                recommended_stack="fullstack_web",
                suggested_project_name="studio-pro",
                product_summary_vi="Studio dựng video AI chuyên sâu",
                complexity_level="complex",
                round_number=2,
                clarity_score=92,
                ready_to_build=True,
                open_gaps_vi=["Xác nhận định dạng xuất âm thanh cuối cùng"],
                questions=[
                    InterviewQuestion(
                        id="q8",
                        category="Xử lý lỗi",
                        why_it_matters="Tránh mất dữ liệu khi render lâu",
                        question="Khi render video bị lỗi giữa chừng thì xử lý ra sao?",
                        options=["Tự thử lại 2 lần", "Lưu checkpoint để chạy tiếp"],
                        default_answer="Lưu checkpoint để chạy tiếp",
                    )
                ],
                proactive_suggestions=[
                    ProactiveSuggestion(
                        id="s3",
                        title="Tự động kiểm tra sức khỏe FFmpeg & API",
                        benefit_vi="Báo lỗi rõ ràng ngay từ đầu thay vì treo giữa chừng",
                        recommended=True,
                    )
                ],
            )
            return out2.model_dump_json()
        if "ProductSpec" in prompt:
            spec = ProductSpec(
                product_title="Studio Video AI Pro",
                features_vi=["Tạo kịch bản", "Render MP4 thật", "Health check kết nối"],
                acceptance_criteria=["AC-1: Render MP4 thật thành công"],
                project_constitution="# Constitution\n- Không bao giờ dùng mock data.\n- Mọi API ngoài phải có health check & timeout rõ ràng.",
                architecture_contract="## Contract\n- POST /api/render -> { jobId: string }",
                prd_markdown="# PRD\nChi tiết sản phẩm thật.",
            )
            return spec.model_dump_json()

        # Round 1: complex idea returns 8 questions (> old hard cap of 5!) + proactive suggestions
        out1 = InterviewOutput(
            recommended_stack="fullstack_web",
            suggested_project_name="studio-pro",
            product_summary_vi="Hệ thống dựng video tự động đa bước",
            complexity_level="complex",
            round_number=1,
            clarity_score=65,
            ready_to_build=False,
            open_gaps_vi=["Cơ chế xử lý lỗi khi API ngoài mất kết nối"],
            questions=[
                InterviewQuestion(
                    id=f"q{i}",
                    category="Luồng chính",
                    why_it_matters="Chốt nghiệp vụ",
                    question=f"Câu hỏi chuyên sâu số {i}?",
                    options=["A", "B"],
                    default_answer="A",
                )
                for i in range(1, 9)
            ],
            proactive_suggestions=[
                ProactiveSuggestion(
                    id="s1",
                    title="Auto-save tiến độ dự án",
                    benefit_vi="Không mất dữ liệu khi đóng trình duyệt",
                    recommended=True,
                ),
                ProactiveSuggestion(
                    id="s2",
                    title="Chế độ xem trước nhanh 480p",
                    benefit_vi="Kiểm tra kịch bản nhanh gấp 5 lần trước khi xuất Full HD",
                    recommended=False,
                ),
            ],
        )
        return out1.model_dump_json()

    interviewer = ProductInterviewer(cfg, llm_caller=_fake_llm)
    r1 = interviewer.generate_interview("Làm hệ thống studio dựng video AI đa bước tích hợp TTS và FFmpeg", round_number=1)
    assert r1.complexity_level == "complex"
    assert len(r1.questions) == 8  # No longer artificially capped at 5!
    assert len(r1.proactive_suggestions) == 2
    assert r1.clarity_score == 65

    r2 = interviewer.generate_interview(
        "Làm hệ thống studio dựng video AI đa bước tích hợp TTS và FFmpeg",
        previous_qa={"Câu hỏi chuyên sâu số 1?": "A"},
        selected_suggestions=["Auto-save tiến độ dự án"],
        user_notes="Cần kết nối webchat2api ở cổng 17841",
        round_number=2,
    )
    assert r2.round_number == 2
    assert r2.clarity_score == 92 and r2.ready_to_build is True
    assert "Auto-save tiến độ dự án" in prompts_seen[-1]
    assert "17841" in prompts_seen[-1]

    spec = interviewer.synthesize_spec(
        "Làm hệ thống studio dựng video AI",
        {"Câu hỏi chuyên sâu số 1?": "A"},
        "fullstack_web",
        selected_suggestions=["Auto-save tiến độ dự án"],
        user_notes="Cần kết nối webchat2api ở cổng 17841",
    )
    assert "Không bao giờ dùng mock data" in spec.project_constitution
    const_path = persist_project_constitution(repo, spec)
    assert const_path is not None and const_path.is_file()

    # Verify PromptCompiler injects <project_constitution>, <task_spec>, and Systematic Debugging protocol
    compiler = PromptCompiler(cfg)
    fix_task = TaskSpec(
        task_id="T-01",
        title="Sửa lỗi kết nối API",
        objective="Sửa lỗi 502 trên cổng 17841",
        allowed_write_files=["src/app.ts"],
        execution_profile="investigate_fix",
    )
    prompt, _tok = compiler.compile(fix_task)
    assert "<project_constitution>" in prompt
    assert "Không bao giờ dùng mock data" in prompt
    assert "<task_spec>" in prompt
    assert "Reproduce & Probe thật" in prompt
    assert "Isolate Root Cause" in prompt
