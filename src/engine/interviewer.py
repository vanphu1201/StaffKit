"""Layer 0A Product Interviewer: converts vague user ideas into plain-Vietnamese Q&A and OpenSpec PRD."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from engine.bridges.agy_cli_executor import (
    _extract_tool_call,
    build_agy_command,
    encode_stream_user_message,
    kill_process_tree,
)
from engine.models import EngineConfig, InterviewOutput, ProductSpec, StackType, inline_json_schema
from engine.planner import _extract_plan_json_payload


class InterviewerError(RuntimeError):
    """Raised when non-tech product discovery fails or is aborted."""


class ProductInterviewer:
    """OpenSpec-inspired 2-step interviewer (1: plain-Vietnamese Q&A, 2: feature list + PRD)."""

    def __init__(
        self,
        config: EngineConfig,
        llm_caller: Callable[[str, Path, str], str] | None = None,
        on_event: Callable[[str, str], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self._llm_caller = llm_caller
        self.on_event = on_event
        self.stop_event = stop_event

    def _emit(self, level: str, message: str) -> None:
        if self.on_event is not None:
            self.on_event(level, message)

    def _call_agy_no_tools(self, prompt: str, schema_obj: dict, schema_name: str, effort: str) -> str:
        schema_dir = self.config.auto_engine_dir if self.config.target_repo_path.is_dir() else Path.cwd() / ".auto-engine"
        schema_dir.mkdir(parents=True, exist_ok=True)
        schema_path = schema_dir / schema_name
        schema_path.write_text(json.dumps(inline_json_schema(schema_obj), indent=2), encoding="utf-8")

        if self._llm_caller is not None:
            return self._llm_caller(prompt, schema_path, effort)

        cwd = self.config.target_repo_path if self.config.target_repo_path.is_dir() else Path.cwd()
        tier = self.config.agy.tiers.get("planner")
        model = tier.model if tier else ""
        cmd = build_agy_command(
            self.config,
            model,
            effort,
            input_format="stream-json",
            output_format="stream-json",
            json_schema_path=schema_path,
        )
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None

        try:
            proc.stdin.write(encode_stream_user_message(prompt))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        line_q: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            try:
                for raw in proc.stdout:  # type: ignore[union-attr]
                    line_q.put(raw)
            finally:
                line_q.put(None)

        threading.Thread(target=_reader, daemon=True).start()

        out_lines: list[str] = []
        deadline = time.monotonic() + min(180, self.config.executor.timeout_seconds)

        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                kill_process_tree(proc)
                raise InterviewerError("Đã dừng phân tích theo yêu cầu người dùng.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kill_process_tree(proc)
                raise InterviewerError("Phân tích ý tưởng quá thời gian cho phép.")
            try:
                item = line_q.get(timeout=min(0.4, remaining))
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if item is None:
                break
            line = item.strip()
            if not line:
                continue
            out_lines.append(line)
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and _extract_tool_call(ev) is not None:
                kill_process_tree(proc)
                raise InterviewerError("Interviewer đã cố gọi tool không cần thiết; đã ngắt để tiết kiệm token.")

        proc.wait(timeout=5)
        stderr_text = proc.stderr.read().strip()
        if proc.returncode != 0:
            raise InterviewerError(f"agy failed (exit {proc.returncode}): {stderr_text or '\n'.join(out_lines)}")
        return "\n".join(out_lines)

    def generate_interview(self, idea: str, stack_hint: StackType = "auto") -> InterviewOutput:
        """Analyze a vague idea and emit 3-5 plain-Vietnamese non-tech clarifying questions."""
        self._emit("info", "Đang phân tích ý tưởng & tạo 3-5 câu hỏi làm rõ (tiếng Việt dễ hiểu)...")
        prompt = f"""Bạn là chuyên gia tư vấn sản phẩm công nghệ cho người KHÔNG rành kỹ thuật.
QUY TẮC BẮT BUỘC: TUYỆT ĐỐI KHÔNG gọi bất kỳ tool nào (`view_file`, `list_dir`, `run_command`...). Chỉ trả về JSON đúng schema `InterviewOutput` trong 1 lượt duy nhất.

Người dùng có ý tưởng sau:
"{idea.strip()}"
Gợi ý loại sản phẩm: "{stack_hint}"

Yêu cầu:
1. Chọn `recommended_stack` là `"web_react"` (nếu là website/dashboard/quản lý) hoặc `"python_app"` (nếu là tool tự động hóa/AI script/CLI/xử lý file).
2. Đặt `suggested_project_name` ngắn gọn viết thường không dấu nối bằng gạch ngang (vd: `ai-hr-manager`).
3. Viết `product_summary_vi`: 1-2 câu mô tả sản phẩm bằng tiếng Việt bình dân.
4. Sinh 3 đến 5 câu hỏi ngắn gọn trong `questions` hoàn toàn bằng tiếng Việt dễ hiểu (KHÔNG nhắc tên framework, database hay code; tập trung vào nhu cầu thật: người dùng cần màn hình gì, lưu trữ/xuất file gì, có cần đăng nhập hay tích hợp API AI nào, giá trị mặc định hợp lý `default_answer`). Mỗi câu có 2-4 `options` gợi ý để người dùng bấm chọn nhanh.
"""
        raw = self._call_agy_no_tools(
            prompt,
            InterviewOutput.model_json_schema(),
            "interview_schema.json",
            effort="low",
        )
        out = InterviewOutput.model_validate_json(_extract_plan_json_payload(raw))
        out.questions = out.questions[:5]
        return out

    def synthesize_spec(self, idea: str, answers: dict[str, str], stack: str) -> ProductSpec:
        """Turn idea + user answers into plain-Vietnamese features and a crisp OpenSpec PRD."""
        self._emit("info", "Đang tổng hợp danh sách tính năng & bản đặc tả PRD chuẩn...")
        qa_lines = "\n".join(f"- {k}: {v}" for k, v in answers.items()) or "- (Người dùng giữ mặc định hợp lý)"
        prompt = f"""Bạn là Product Manager & Software Architect.
QUY TẮC BẮT BUỘC: TUYỆT ĐỐI KHÔNG gọi bất kỳ tool nào. Chỉ trả về JSON đúng schema `ProductSpec` trong 1 lượt duy nhất.

Ý tưởng gốc: "{idea.strip()}"
Loại sản phẩm đã chọn: "{stack}"
Câu trả lời của người dùng:
{qa_lines}

Yêu cầu:
1. `product_title`: Tên sản phẩm tiếng Việt ngắn gọn.
2. `features_vi`: 4–7 gạch đầu dòng mô tả tính năng cốt lõi bằng tiếng Việt đời thường để người phi công nghệ đọc và bấm duyệt.
3. `prd_markdown`: Tài liệu PRD ngắn gọn, rõ ràng (Markdown) gồm:
   - Mục tiêu sản phẩm
   - Các chức năng & hành vi cụ thể cần xây dựng (chia vừa tầm 2–5 atomic tasks, ưu tiên làm sản phẩm chạy thật sạch sẽ, có giao diện/đầu ra rõ ràng, không dùng tính năng thừa)
   - Tiêu chí kiểm thử tự động (chạy được với `pytest` nếu là `python_app` hoặc `tsc --noEmit` + `npm run build` nếu là `web_react`)
   - Lưu ý: nếu tính năng cần gọi LLM/API ngoài mà chưa có key, thiết kế hỗ trợ chế độ Demo/Mock hoạt động ngay + đọc key từ biến môi trường.
"""
        raw = self._call_agy_no_tools(
            prompt,
            ProductSpec.model_json_schema(),
            "product_spec_schema.json",
            effort="medium",
        )
        return ProductSpec.model_validate_json(_extract_plan_json_payload(raw))
