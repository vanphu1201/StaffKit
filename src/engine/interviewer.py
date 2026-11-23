"""Layer 0A Product Interviewer: converts vague user ideas into plain-Vietnamese Q&A and OpenSpec PRD."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from engine.bridges.agy_cli_executor import (
    TextDeltaBuffer,
    _extract_text_delta,
    _extract_tool_call,
    _extract_usage_from_event,
    build_agy_command,
    emit_trace,
    encode_stream_user_message,
    kill_process_tree,
)
from engine.blueprints import get_blueprint, load_blueprints
from engine.models import EngineConfig, InterviewOutput, ProductSpec, StackType, TokenUsage, inline_json_schema
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
        on_trace: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self._llm_caller = llm_caller
        self.on_event = on_event
        self.stop_event = stop_event
        self.on_trace = on_trace

    def _emit(self, level: str, message: str) -> None:
        if self.on_event is not None:
            self.on_event(level, message)

    def _call_agy_no_tools(
        self,
        prompt: str,
        schema_obj: dict,
        schema_name: str,
        effort: str,
        *,
        phase_label: str = "Interviewer Prompt → Antigravity",
    ) -> str:
        schema_dir = self.config.auto_engine_dir if self.config.target_repo_path.is_dir() else Path.cwd() / ".auto-engine"
        schema_dir.mkdir(parents=True, exist_ok=True)
        schema_path = schema_dir / schema_name
        schema_path.write_text(json.dumps(inline_json_schema(schema_obj), indent=2), encoding="utf-8")

        tier = self.config.agy.tiers.get("planner")
        model = tier.model if tier else ""
        used_model = model or "agy-default"
        emit_trace(
            self.on_trace,
            phase="interviewer",
            kind="prompt_sent",
            title=phase_label,
            content=prompt,
            model=used_model,
            effort=effort,
        )

        if self._llm_caller is not None:
            raw_mock = self._llm_caller(prompt, schema_path, effort)
            emit_trace(
                self.on_trace,
                phase="interviewer",
                kind="response_done",
                title="OK",
                content=raw_mock[:500],
                model=used_model,
                effort=effort,
            )
            return raw_mock

        start = time.monotonic()
        cwd = self.config.target_repo_path if self.config.target_repo_path.is_dir() else Path.cwd()
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
        latest_usage = TokenUsage()
        delta_buf = TextDeltaBuffer(self.on_trace, phase="interviewer", model=used_model, effort=effort)
        deadline = time.monotonic() + min(180, self.config.executor.timeout_seconds)

        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                delta_buf.flush()
                kill_process_tree(proc)
                raise InterviewerError("Đã dừng phân tích theo yêu cầu người dùng.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                delta_buf.flush()
                kill_process_tree(proc)
                raise InterviewerError("Phân tích ý tưởng quá thời gian cho phép.")
            try:
                item = line_q.get(timeout=min(0.4, remaining))
            except queue.Empty:
                if proc.poll() is not None:
                    break
                delta_buf.flush()
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
            if isinstance(ev, dict):
                usage = _extract_usage_from_event(ev)
                if usage is not None:
                    latest_usage = usage
                delta = _extract_text_delta(ev)
                if delta:
                    delta_buf.push(delta)
                if _extract_tool_call(ev) is not None:
                    delta_buf.flush()
                    kill_process_tree(proc)
                    raise InterviewerError("Interviewer đã cố gọi tool không cần thiết; đã ngắt để tiết kiệm token.")

        proc.wait(timeout=5)
        delta_buf.flush()
        stderr_text = proc.stderr.read().strip()
        duration = round(time.monotonic() - start, 3)
        if proc.returncode != 0:
            err = stderr_text or "\n".join(out_lines)
            emit_trace(
                self.on_trace,
                phase="interviewer",
                kind="response_done",
                title="ERROR",
                content=err[:500],
                model=used_model,
                effort=effort,
                meta={"usage": latest_usage.model_dump(), "duration_sec": duration, "verdict": "VERIFY_FAIL"},
            )
            raise InterviewerError(f"agy failed (exit {proc.returncode}): {err}")

        emit_trace(
            self.on_trace,
            phase="interviewer",
            kind="response_done",
            title="OK",
            content=f"Hoàn tất phân tích trong {duration}s",
            model=used_model,
            effort=effort,
            meta={"usage": latest_usage.model_dump(), "duration_sec": duration, "verdict": "OK"},
        )
        return "\n".join(out_lines)

    def generate_interview(self, idea: str, stack_hint: StackType = "auto") -> InterviewOutput:
        """Analyze a vague idea and emit 3-5 plain-Vietnamese non-tech clarifying questions."""
        self._emit("info", "Đang phân tích ý tưởng & tạo 3-5 câu hỏi làm rõ (tiếng Việt dễ hiểu)...")
        target_hint = self.config.target_repo_path if self.config.target_repo_path.is_dir() else None
        blueprints = load_blueprints(target_hint)
        bp_lines = "\n".join(f"  - `{bp.id}`: {bp.label} — {bp.description}" for bp in blueprints)
        prompt = f"""Bạn là chuyên gia tư vấn sản phẩm công nghệ cho người KHÔNG rành kỹ thuật. Hệ thống có thể tạo ra MỌI loại sản phẩm công nghệ (Web, Mobile App, Desktop App, Backend API, CLI/Script, Bot, Game, AI Pipeline...) và luôn hướng tới SẢN PHẨM CHẠY THẬT (không làm bản demo giả lập).
QUY TẮC BẮT BUỘC: TUYỆT ĐỐI KHÔNG gọi bất kỳ tool nào (`view_file`, `list_dir`, `run_command`...). Chỉ trả về JSON đúng schema `InterviewOutput` trong 1 lượt duy nhất.

Người dùng có ý tưởng sau:
"{idea.strip()}"
Gợi ý loại sản phẩm từ người dùng: "{stack_hint}"

Danh sách blueprint dựng khung có sẵn:
{bp_lines}

Yêu cầu:
1. Chọn `recommended_stack` là một `id` trong danh sách blueprint trên (nếu sản phẩm thuộc Mobile, Desktop, Bot, Game, hệ thống đa dịch vụ hoặc công nghệ khác ngoài Web/Python cơ bản, hãy chọn `"generic"` để Antigravity tự thiết kế kiến trúc phù hợp nhất).
2. Đặt `suggested_project_name` ngắn gọn viết thường không dấu nối bằng gạch ngang (vd: `inventory-sync-bot`).
3. Viết `product_summary_vi`: 1-2 câu mô tả sản phẩm thật bằng tiếng Việt bình dân.
4. Sinh 3 đến 5 câu hỏi ngắn gọn trong `questions` hoàn toàn bằng tiếng Việt dễ hiểu (KHÔNG hỏi chuyện code; tập trung vào nhu cầu thực tế của đúng loại sản phẩm đó: người dùng mục tiêu, luồng sử dụng chính, nơi lưu dữ liệu/tích hợp thật nếu cần, tài nguyên hoặc API key thật, và giá trị mặc định `default_answer`). Mỗi câu có 2-4 `options` gợi ý để người dùng bấm chọn nhanh.
"""
        raw = self._call_agy_no_tools(
            prompt,
            InterviewOutput.model_json_schema(),
            "interview_schema.json",
            effort="low",
            phase_label="Phân tích ý tưởng → Antigravity",
        )
        out = InterviewOutput.model_validate_json(_extract_plan_json_payload(raw))
        out.questions = out.questions[:5]
        return out

    def synthesize_spec(self, idea: str, answers: dict[str, str], stack: str) -> ProductSpec:
        """Turn idea + user answers into plain-Vietnamese features and a production OpenSpec PRD."""
        self._emit("info", "Đang tổng hợp danh sách tính năng & bản đặc tả PRD chuẩn...")
        target_hint = self.config.target_repo_path if self.config.target_repo_path.is_dir() else None
        bp = get_blueprint(stack, target_hint)
        bp_rules = ""
        if bp is not None and bp.quality_rules:
            bp_rules = "\nTiêu chuẩn bổ sung của blueprint `" + bp.id + "`:\n" + "\n".join(
                f"     + {r}" for r in bp.quality_rules
            )
        qa_lines = "\n".join(f"- {k}: {v}" for k, v in answers.items()) or "- (Người dùng giữ mặc định hợp lý)"
        prompt = f"""Bạn là Product Manager & Software Architect xây dựng SẢN PHẨM CÔNG NGHỆ CHẠY THẬT (KHÔNG làm bản demo hay dữ liệu giả).
QUY TẮC BẮT BUỘC: TUYỆT ĐỐI KHÔNG gọi bất kỳ tool nào. Chỉ trả về JSON đúng schema `ProductSpec` trong 1 lượt duy nhất.

Ý tưởng gốc: "{idea.strip()}"
Blueprint / Nền tảng đã chọn: "{stack}"
Câu trả lời của người dùng:
{qa_lines}

Yêu cầu:
1. `product_title`: Tên sản phẩm tiếng Việt ngắn gọn, chuyên nghiệp.
2. `features_vi`: 4–7 gạch đầu dòng mô tả tính năng dùng thật bằng tiếng Việt đời thường để người dùng đọc và bấm duyệt.
3. `prd_markdown`: Tài liệu PRD chuẩn sản phẩm thật (Markdown) gồm:
   - Mục tiêu sản phẩm & phạm vi người dùng thật
   - Kiến trúc kỹ thuật, lưu trữ dữ liệu & tích hợp thật phù hợp nhất với loại sản phẩm (tránh tạo tài khoản/dữ liệu demo giả lập; nếu có giao diện thì thiết kế chuyên nghiệp, tiết chế, dễ dùng; nếu thiếu thông tin/API key/tài nguyên thật từ người dùng thì thiết kế để hệ thống hỏi qua `user_questions` / `NEED_USER_INPUT`){bp_rules}
   - Tiêu chí kiểm thử tự động & build/chạy thật phù hợp với công nghệ đã chọn.
4. `verify_commands`, `preview_command`, `quality_rules`: Nếu sản phẩm dùng công nghệ mới ngoài các blueprint có sẵn (vd: Go, Rust, Flutter, Mobile, Game, Desktop, Chrome Extension, C#/.NET...), hãy điền các lệnh kiểm tra build/test chuẩn vào `verify_commands`, lệnh chạy thử vào `preview_command`, và quy tắc chất lượng riêng vào `quality_rules` để hệ thống tự động tạo `.auto-engine/blueprint.yaml` cho dự án.
"""
        raw = self._call_agy_no_tools(
            prompt,
            ProductSpec.model_json_schema(),
            "product_spec_schema.json",
            effort="medium",
            phase_label="Tổng hợp đặc tả PRD → Antigravity",
        )
        spec = ProductSpec.model_validate_json(_extract_plan_json_payload(raw))
        if target_hint is not None:
            save_custom_blueprint_if_needed(target_hint, spec, stack=stack)
        return spec


def save_custom_blueprint_if_needed(project_dir: Path, spec: ProductSpec, stack: str = "generic") -> Path | None:
    """Persist a dynamic `.auto-engine/blueprint.yaml` when the user requests any non-builtin tech product."""
    if not (spec.verify_commands or spec.quality_rules or spec.preview_command):
        return None
    existing_bp = get_blueprint(stack, project_dir)
    if existing_bp is not None and existing_bp.id not in ("generic", "custom"):
        return None
    bp_id = stack if stack and stack not in ("auto", "generic") else "custom_product"
    auto_dir = project_dir / ".auto-engine"
    auto_dir.mkdir(parents=True, exist_ok=True)
    bp_path = auto_dir / "blueprint.yaml"
    payload = {
        "id": bp_id,
        "label": spec.product_title or bp_id,
        "description": "Dynamic blueprint synthesized from user requirements",
        "verify_commands": list(spec.verify_commands),
        "preview_command": spec.preview_command,
        "quality_rules": list(spec.quality_rules),
    }
    bp_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return bp_path
