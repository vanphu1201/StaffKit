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

    def generate_interview(
        self,
        idea: str,
        stack_hint: StackType = "auto",
        *,
        previous_qa: dict[str, str] | None = None,
        selected_suggestions: list[str] | None = None,
        user_notes: str = "",
        round_number: int = 1,
    ) -> InterviewOutput:
        """Adaptive multi-round elicitation (BMAD + Superpowers brainstorming): scales depth by complexity & offers suggestions."""
        if round_number <= 1:
            self._emit("info", "Đang phân tích độ phức tạp ý tưởng, tạo bộ câu hỏi chuyên sâu & gợi ý nâng tầm sản phẩm...")
        else:
            self._emit("info", f"Đang phân tích câu trả lời Vòng {round_number - 1} để hỏi đào sâu thêm các điểm còn mở...")
        target_hint = self.config.target_repo_path if self.config.target_repo_path.is_dir() else None
        blueprints = load_blueprints(target_hint)
        bp_lines = "\n".join(f"  - `{bp.id}`: {bp.label} — {bp.description}" for bp in blueprints)

        history_section = ""
        if previous_qa or selected_suggestions or user_notes.strip():
            hist_parts: list[str] = [f"\n=== NGỮ CẢNH ĐÃ TRAO ĐỔI (VÒNG 1..{max(1, round_number - 1)}) ==="]
            if previous_qa:
                hist_parts.append("Các câu hỏi & câu trả lời người dùng đã chốt (TUYỆT ĐỐI KHÔNG HỎI LẠI):")
                for k, v in previous_qa.items():
                    hist_parts.append(f"- {k}: {v}")
            if selected_suggestions:
                hist_parts.append("Các gợi ý nâng tầm người dùng đã chọn thêm:")
                for s in selected_suggestions:
                    hist_parts.append(f"- [ĐÃ CHỌN] {s}")
            if user_notes.strip():
                hist_parts.append(f"Ghi chú / mong muốn bổ sung tự do của người dùng:\n{user_notes.strip()}")
            history_section = "\n".join(hist_parts) + "\n"

        round_guidance = (
            "4. Đánh giá `complexity_level` (`simple` / `standard` / `complex`) dựa trên quy mô thực tế của ý tưởng:\n"
            "   - `simple` (công cụ nhỏ 1 màn hình): sinh 3–4 câu hỏi trọng tâm.\n"
            "   - `standard` (ứng dụng đầy đủ 1–2 nhóm tính năng): sinh 5–7 câu hỏi.\n"
            "   - `complex` (hệ thống đa module, tích hợp AI/API ngoài, xử lý media, phân quyền, workflow phức tạp): sinh 7–11 câu hỏi CHUYÊN SÂU chia rõ theo `category` (vd: 'Luồng người dùng chính', 'Dữ liệu & Tích hợp thật', 'Xử lý lỗi & Ngoại lệ', 'Trải nghiệm giao diện').\n"
            if round_number <= 1
            else f"4. Đây là VÒNG HỎI ĐÀO SÂU THỨ {round_number}: TUYỆT ĐỐI KHÔNG lặp lại bất kỳ câu hỏi nào đã có trong lịch sử. "
            "Chỉ hỏi 2–6 câu hỏi thật sự SÂU vào các khoảng trống nghiệp vụ, nhánh biên, quy trình xử lý lỗi, hoặc tích hợp thực tế còn mờ. "
            "Nếu thông tin đã cực kỳ đầy đủ, đặt `ready_to_build: true`, `clarity_score >= 90` và chỉ đưa 0–2 câu hỏi tinh chỉnh cuối cùng.\n"
        )

        prompt = f"""Bạn là Product Architect & Chuyên gia Khám phá Sản phẩm (áp dụng chuẩn Brainstorming Elicitation của BMAD-METHOD & Superpowers) giúp người KHÔNG rành kỹ thuật làm rõ trọn vẹn nhu cầu trước khi xây dựng SẢN PHẨM CHẠY THẬT (không làm bản demo giả lập).
QUY TẮC BẮT BUỘC: TUYỆT ĐỐI KHÔNG gọi bất kỳ tool nào (`view_file`, `list_dir`, `run_command`...). Chỉ trả về JSON đúng schema `InterviewOutput` trong 1 lượt duy nhất.

Ý tưởng gốc của người dùng:
"{idea.strip()}"
Gợi ý loại sản phẩm từ người dùng: "{stack_hint}"
{history_section}
Danh sách blueprint dựng khung có sẵn:
{bp_lines}

Yêu cầu chi tiết:
1. Chọn `recommended_stack` là một `id` trong danh sách blueprint trên (nếu sản phẩm thuộc Mobile, Desktop, Bot, Game, hệ thống đa dịch vụ hoặc công nghệ khác ngoài Web/Python cơ bản, chọn `"generic"` để Antigravity tự thiết kế kiến trúc tối ưu).
2. Đặt `suggested_project_name` ngắn gọn viết thường không dấu nối bằng gạch ngang (vd: `inventory-sync-bot`).
3. Viết `product_summary_vi`: 1–3 câu tiếng Việt bình dân tóm tắt đúng bản chất sản phẩm và những gì đã hiểu tới thời điểm hiện tại.
{round_guidance}5. Mỗi câu hỏi trong `questions` PHẢI hoàn toàn bằng tiếng Việt dễ hiểu (KHÔNG hỏi thuật ngữ code khó hiểu), có `category`, `why_it_matters` (1 câu ngắn giải thích vì sao cần chốt ý này), 2–4 `options` gợi ý thực tế để bấm chọn nhanh, và `default_answer` hợp lý.
6. Sinh 3–5 `proactive_suggestions` (Gợi ý nâng tầm sản phẩm): các tính năng hoặc cơ chế bảo vệ thông minh mà người dùng có thể chưa nghĩ tới (vd: tự động lưu tiến độ, kiểm tra sức khỏe kết nối API ngoài, khôi phục khi lỗi, xuất/nhập cấu hình, chế độ xem trước nhanh...), mỗi gợi ý có `title`, `benefit_vi` dễ hiểu và đánh dấu `recommended: true` cho 1–2 gợi ý đáng giá nhất. (Không lặp lại gợi ý đã có trong `[ĐÃ CHỌN]`).
7. Chấm `clarity_score` (0–100) phản ánh mức độ rõ ràng hiện tại của yêu cầu, đặt `round_number: {round_number}`, `ready_to_build` (`true` nếu đã đủ thông tin để tạo PRD sắc nét), và liệt kê `open_gaps_vi` (2–4 gạch đầu dòng ngắn những điểm có thể đào sâu thêm nếu người dùng muốn hỏi tiếp).
"""
        raw = self._call_agy_no_tools(
            prompt,
            InterviewOutput.model_json_schema(),
            "interview_schema.json",
            effort="medium" if round_number > 1 or len(idea) > 220 else "low",
            phase_label=f"Phỏng vấn chuyên sâu Vòng {round_number} → Antigravity",
        )
        out = InterviewOutput.model_validate_json(_extract_plan_json_payload(raw))
        out.round_number = round_number
        out.questions = out.questions[:12]
        out.proactive_suggestions = out.proactive_suggestions[:6]
        return out

    def synthesize_spec(
        self,
        idea: str,
        answers: dict[str, str],
        stack: str,
        *,
        selected_suggestions: list[str] | None = None,
        user_notes: str = "",
    ) -> ProductSpec:
        """Turn idea + multi-round answers + chosen suggestions into features, Constitution, Contract & OpenSpec PRD."""
        self._emit("info", "Đang tổng hợp Danh sách tính năng, Hiến pháp dự án (Constitution), Hợp đồng Kiến trúc & PRD...")
        target_hint = self.config.target_repo_path if self.config.target_repo_path.is_dir() else None
        bp = get_blueprint(stack, target_hint)
        bp_rules = ""
        if bp is not None and bp.quality_rules:
            bp_rules = "\nTiêu chuẩn bổ sung của blueprint `" + bp.id + "`:\n" + "\n".join(
                f"     + {r}" for r in bp.quality_rules
            )
        qa_lines = "\n".join(f"- {k}: {v}" for k, v in answers.items()) or "- (Người dùng giữ mặc định hợp lý)"
        sugg_lines = (
            "\n".join(f"- {s}" for s in (selected_suggestions or []) if s.strip())
            or "- (Không chọn thêm gợi ý phụ)"
        )
        notes_block = f"\nGhi chú bổ sung từ người dùng:\n{user_notes.strip()}\n" if user_notes.strip() else ""
        prompt = f"""Bạn là Product Manager & Software Architect xây dựng SẢN PHẨM CÔNG NGHỆ CHẠY THẬT (KHÔNG làm bản demo hay dữ liệu giả), kết hợp chuẩn `github/spec-kit` (Constitution) + `Fission-AI/OpenSpec` (Executable PRD & Contracts).
QUY TẮC BẮT BUỘC: TUYỆT ĐỐI KHÔNG gọi bất kỳ tool nào. Chỉ trả về JSON đúng schema `ProductSpec` trong 1 lượt duy nhất.

Ý tưởng gốc: "{idea.strip()}"
Blueprint / Nền tảng đã chọn: "{stack}"
Toàn bộ câu trả lời người dùng đã chốt qua các vòng phỏng vấn:
{qa_lines}
Các tính năng gợi ý người dùng đã chọn tích hợp thêm:
{sugg_lines}{notes_block}

Yêu cầu:
1. `product_title`: Tên sản phẩm tiếng Việt ngắn gọn, chuyên nghiệp.
2. `features_vi`: 5–9 gạch đầu dòng mô tả tính năng dùng thật (bao gồm cả các gợi ý đã chọn) bằng tiếng Việt đời thường để người dùng đọc và bấm duyệt.
3. `acceptance_criteria`: 5–10 tiêu chí nghiệm thu cụ thể, kiểm thử được, đánh mã `AC-1: ...`, `AC-2: ...` (bao phủ luồng nghiệp vụ chính, tích hợp thật, xử lý lỗi/dữ liệu biên, và trạng thái khởi tạo ban đầu để mở sản phẩm lên dùng được ngay).
4. `project_constitution`: "Hiến pháp dự án" (15–25 dòng Markdown ngắn gọn, bất biến theo chuẩn `spec-kit`) khóa chặt các nguyên tắc cốt lõi mà MỌI task và MỌI lần nâng cấp sau này đều phải tuân theo:
   - Nguyên tắc kiến trúc & phân tách module
   - Quy chuẩn UX/UI, thông báo lỗi rõ ràng bằng tiếng Việt/ngôn ngữ đích, trạng thái loading/empty/error đầy đủ
   - Nguyên tắc dữ liệu thật, bảo mật, xử lý lỗi mạng/dịch vụ ngoài (không dùng mock/fake)
   - Tương thích môi trường thực thi (Windows/Node.js/Python không lỗi native build).
5. `prd_markdown`: Tài liệu PRD chuẩn sản phẩm thật (Markdown) gồm:
   - Mục tiêu sản phẩm & phạm vi người dùng thật
   - Kiến trúc kỹ thuật, lưu trữ dữ liệu, xử lý lỗi đầu vào (validation/error boundary), trạng thái giao diện đầy đủ & tích hợp thật phù hợp nhất với loại sản phẩm (Lưu ý tương thích Windows/Node.js hiện đại: ưu tiên các thư viện có sẵn prebuilt binary hoặc `node:sqlite` / Prisma / pure-JS để `npm install` không bị lỗi biên dịch C++ `node-gyp` khi máy người dùng không cài Visual Studio C++){bp_rules}
   - Danh sách tiêu chí nghiệm thu (`AC-1..AC-N`) và kịch bản kiểm thử tự động (unit/integration/acceptance test) chứng minh từng tính năng hoạt động đúng.
6. `architecture_contract`: Bản hợp đồng kiến trúc kỹ thuật ngắn gọn (<= 35 dòng Markdown) khóa cứng:
   - Schema dữ liệu / bảng DB / kiểu dữ liệu dùng chung
   - Giao diện API / chữ ký hàm liên module (input, output, mã lỗi)
   - Ranh giới file/module chính để các task chạy song song khớp tuyệt đối 100%.
7. `verify_commands`, `runtime_verify_commands`, `preview_command`, `quality_rules`: Nếu sản phẩm dùng công nghệ mới ngoài các blueprint có sẵn (vd: Go, Rust, Flutter, Mobile, Game, Desktop, Chrome Extension, C#/.NET...), hãy điền các lệnh kiểm tra build/test chuẩn vào `verify_commands`, lệnh smoke test runtime vào `runtime_verify_commands`, lệnh chạy thử vào `preview_command`, và quy tắc chất lượng riêng vào `quality_rules` để hệ thống tự động tạo `.auto-engine/blueprint.yaml` cho dự án.
"""
        raw = self._call_agy_no_tools(
            prompt,
            ProductSpec.model_json_schema(),
            "product_spec_schema.json",
            effort="medium",
            phase_label="Tổng hợp đặc tả PRD & Hiến pháp dự án → Antigravity",
        )
        spec = ProductSpec.model_validate_json(_extract_plan_json_payload(raw))
        if target_hint is not None:
            save_custom_blueprint_if_needed(target_hint, spec, stack=stack)
            persist_project_contracts(target_hint, spec)
            persist_project_constitution(target_hint, spec)
        return spec


def persist_project_constitution(
    project_dir: Path,
    spec: ProductSpec | None = None,
    constitution_markdown: str = "",
) -> Path | None:
    """Persist `.auto-engine/constitution.md` (github/spec-kit style) inherited by all tasks & future updates."""
    text = ((spec.project_constitution if spec is not None else "") or constitution_markdown).strip()
    if not text:
        return None
    auto_dir = project_dir / ".auto-engine"
    auto_dir.mkdir(parents=True, exist_ok=True)
    const_path = auto_dir / "constitution.md"
    const_path.write_text(text + "\n", encoding="utf-8")
    return const_path


def persist_project_contracts(project_dir: Path, spec: ProductSpec | None = None, contract_markdown: str = "") -> Path | None:
    """Persist `.auto-engine/contracts.md` so all DAG and parallel worktree tasks share one immutable technical contract."""
    parts: list[str] = []
    base_contract = ((spec.architecture_contract if spec is not None else "") or contract_markdown).strip()
    if base_contract:
        parts.append(base_contract)
    if spec is not None and spec.acceptance_criteria:
        ac_lines = ["## Acceptance Criteria (Must Pass Before Completion)"]
        for idx, ac in enumerate(spec.acceptance_criteria[:10], start=1):
            ac_str = ac.strip()
            if not ac_str:
                continue
            prefix = "" if ac_str.upper().startswith("AC-") else f"AC-{idx}: "
            ac_lines.append(f"- {prefix}{ac_str}")
        if len(ac_lines) > 1:
            parts.append("\n".join(ac_lines))
    cleaned = "\n\n".join(parts).strip()
    if not cleaned:
        return None
    auto_dir = project_dir / ".auto-engine"
    auto_dir.mkdir(parents=True, exist_ok=True)
    contract_path = auto_dir / "contracts.md"
    contract_path.write_text(cleaned + "\n", encoding="utf-8")
    return contract_path


def save_custom_blueprint_if_needed(project_dir: Path, spec: ProductSpec, stack: str = "generic") -> Path | None:
    """Persist a dynamic `.auto-engine/blueprint.yaml` when the user requests any non-builtin tech product."""
    if not (spec.verify_commands or spec.runtime_verify_commands or spec.quality_rules or spec.preview_command):
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
        "runtime_verify_commands": list(spec.runtime_verify_commands),
        "preview_command": spec.preview_command,
        "quality_rules": list(spec.quality_rules),
    }
    bp_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return bp_path
