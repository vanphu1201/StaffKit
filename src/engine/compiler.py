"""Layer 3 Surgical Prompt Compiler: 0-token local-neighborhood builder with pre-flight cap."""

from __future__ import annotations

import json
from pathlib import Path

from engine.blueprints import get_blueprint
from engine.indexer import extract_file_skeletons
from engine.models import EngineConfig, TaskSpec, normalize_posix_path


class PromptTooLargeError(RuntimeError):
    """Raised when a compiled prompt still exceeds max_prompt_tokens_per_call after trimming."""


def estimate_tokens(text: str) -> int:
    """Fast heuristic token count (~3.5 chars per token)."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.5))


class PromptCompiler:
    """Compiles deterministic, token-minimal Surgical Prompts from TaskSpec + local files."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.repo_path = config.target_repo_path

    def build_static_prefix(self) -> str:
        """Build the immutable system rules prefix (optionally appending shared_rules_file)."""
        verify_rule = (
            "- CHỈ sửa code theo đặc tả rồi kết thúc ngay. TUYỆT ĐỐI KHÔNG tự chạy lệnh test, build, typecheck hay lint bên trong phiên (hệ thống bên ngoài sẽ tự chạy verify và trả log rút gọn nếu cần)."
            if self.config.agy.forbid_shell_verify_commands
            else "- Sau khi sửa xong, hãy kiểm tra nhanh bằng lệnh verify mục tiêu nếu cần rồi kết thúc ngay (tránh chạy lặp lại dư thừa)."
        )
        lines = [
            "[STATIC PREFIX]",
            "# SYSTEM RULES",
            "- Thực thi chính xác theo đặc tả TASK bên dưới.",
            "- Tuân thủ phạm vi file được giao; tuyệt đối không sửa `PROTECTED_TEST_FILES`.",
            verify_rule,
            "- KHÔNG lách luật bằng `@ts-ignore`, `eslint-disable`, `# type: ignore`, `# noqa`, `.skip()`, `.only()`, hoặc xóa bỏ assertion trong test.",
            "- Ưu tiên sửa gọn trực tiếp (targeted edit), không viết lại phần code không liên quan.",
            "- Thực thi sản phẩm thật, sạch sẽ theo đặc tả TASK; KHÔNG tạo tài khoản/dữ liệu demo, stub giữ chỗ hay bỏ qua chức năng thật.",
            "- Nếu thiếu thông tin hoặc tài nguyên thật từ người dùng (API key, cấu hình DB, tài khoản, file media/dữ liệu, hoặc quyết định nghiệp vụ) mà không thể suy ra an toàn, hãy dừng và xuất dòng `NEED_USER_INPUT: <câu hỏi cụ thể bằng tiếng Việt>` — hệ thống sẽ hỏi người dùng và truyền câu trả lời thật cho bạn ngay.",
        ]

        bp = get_blueprint(self.config.project.blueprint, self.repo_path)
        if bp is not None and bp.quality_rules:
            lines.append(f"\n# STACK QUALITY RULES ({bp.id})")
            for rule in bp.quality_rules:
                lines.append(f"- {rule}")

        shared_rel = self.config.project.shared_rules_file.strip()
        if shared_rel:
            shared_path = Path(shared_rel)
            if not shared_path.is_absolute():
                shared_path = self.repo_path / shared_path
            if shared_path.is_file():
                shared_text = shared_path.read_text(encoding="utf-8", errors="replace").strip()
                if shared_text:
                    lines.append("\n# PROJECT CONVENTIONS")
                    lines.append(shared_text)

        return "\n".join(lines)

    def _format_readonly_context(
        self,
        readonly_files: list[str],
        skeletons: dict[str, str],
        compact_mode: bool = False,
    ) -> str:
        if not readonly_files:
            return "(Không có)"
        blocks: list[str] = []
        for raw_rel in readonly_files:
            rel = normalize_posix_path(raw_rel)
            if rel in skeletons:
                body = skeletons[rel]
                if compact_mode:
                    body_lines = body.splitlines()[:12]
                    body = "\n".join(body_lines) + ("\n... [trimmed]" if len(body.splitlines()) > 12 else "")
                blocks.append(f"## {rel} (skeleton)\n```\n{body}\n```")
                continue

            full_path = self.repo_path / rel
            if full_path.is_file():
                lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
                max_raw = 15 if compact_mode else 80
                if len(lines) <= max_raw:
                    blocks.append(f"## {rel}\n```\n" + "\n".join(lines) + "\n```")
                else:
                    preview = "\n".join(lines[:max_raw])
                    blocks.append(
                        f"## {rel} (truncated to first {max_raw}/{len(lines)} lines)\n```\n{preview}\n```"
                    )
            else:
                blocks.append(f"## {rel}\n(Sẽ được tạo hoặc chưa có sẵn trên đĩa)")
        return "\n\n".join(blocks)

    def _format_target_files(
        self,
        task: TaskSpec,
        skeletons: dict[str, str],
    ) -> tuple[str, bool]:
        """Format target files and return (formatted_text, all_existing_targets_inlined)."""
        blocks: list[str] = []
        all_inlined = True
        max_lines = self.config.limits.inline_file_max_lines
        new_set = set(task.create_new_files)

        for raw_rel in task.allowed_write_files:
            rel = normalize_posix_path(raw_rel)
            if rel in new_set:
                continue
            full_path = self.repo_path / rel
            if not full_path.is_file():
                blocks.append(f"## {rel}\n(File sẽ tồn tại sau bước trước hoặc cần tạo mới)")
                continue

            lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) <= max_lines:
                numbered = "\n".join(f"L{idx}: {line}" for idx, line in enumerate(lines, start=1))
                blocks.append(f"## {rel} ({len(lines)} lines)\n```\n{numbered}\n```")
            else:
                all_inlined = False
                skel = skeletons.get(rel, "(File > 300 dòng; hãy đọc có chọn lọc theo symbol/line range)")
                blocks.append(
                    f"## {rel} ({len(lines)} lines > {max_lines} limit — skeleton only)\n```\n{skel}\n```"
                )

        for raw_rel in task.create_new_files:
            rel = normalize_posix_path(raw_rel)
            blocks.append(f"## {rel}\n(NEW FILE TO CREATE)")

        return ("\n\n".join(blocks) if blocks else "(Không có)"), all_inlined

    def _format_retry_feedback(
        self,
        attempt: int,
        last_error: str,
        last_verdict: str | None,
        compact_diff: str,
        all_targets_inlined: bool,
    ) -> str:
        if attempt <= 0 or (not last_error and not compact_diff):
            return ""

        parts = ["# PREVIOUS ATTEMPT FEEDBACK"]
        rolled_back_or_clean = last_verdict in {
            "SCOPE_VIOLATION",
            "TEST_GAMING",
            "EMPTY_DIFF",
            "MAX_TURNS_EXCEEDED",
            "BUDGET_EXCEEDED",
        }
        # Avoid sending duplicate diff when target files <= 300 lines are already inlined on disk.
        if not rolled_back_or_clean and not all_targets_inlined and compact_diff.strip():
            parts.append("## Current Git Diff (Rút gọn):\n```diff\n" + compact_diff.strip() + "\n```")

        if last_error.strip():
            parts.append("## Trimmed Error Log / Violation Summary:\n```\n" + last_error.strip() + "\n```")

        return "\n\n".join(parts)

    def _load_user_answers_text(self) -> str:
        ans_path = self.config.auto_engine_dir / "user_answers.json"
        if not ans_path.is_file():
            return ""
        try:
            data = json.loads(ans_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ""
        if not isinstance(data, dict) or not data:
            return ""
        lines = [f"- {k}: {v}" for k, v in data.items() if str(v).strip()]
        return "\n".join(lines)

    def _assemble_prompt(
        self,
        static_prefix: str,
        task: TaskSpec,
        readonly_text: str,
        targets_text: str,
        feedback_text: str,
        user_answers_text: str = "",
    ) -> str:
        verify_cmds = task.verify_commands or self.config.executor.default_verify_commands
        payload_lines = [
            "---",
            "[DYNAMIC TASK PAYLOAD - PER TASK]",
            f"# TASK: {task.task_id} - {task.title}",
            f"- Objective: {task.objective}",
            f"- Acceptance Criteria: {task.acceptance_criteria}",
            f"- Allowed Write Files: {task.allowed_write_files}",
            f"- Create New Files: {task.create_new_files}",
            f"- Scope Mode: {'STRICT (chỉ sửa/tạo các file trong danh sách trên)' if task.strict_scope else 'FLEXIBLE (có thể sửa/tạo các file cần thiết cho TASK; tuyệt đối không sửa PROTECTED_TEST_FILES)'}",
            f"- Protected Test Files (DO NOT TOUCH): {task.protected_test_files}",
            f"- Constraints: {task.constraints}",
            f"- Verification Commands (Engine sẽ chạy sau khi bạn xong): {verify_cmds}",
            "",
            "# READONLY CONTEXT (Interfaces / Types / Signatures)",
            readonly_text,
            "",
            "# TARGET FILES TO MODIFY",
            targets_text,
        ]
        if user_answers_text:
            payload_lines.extend(["", "# USER ANSWERS & REAL ASSETS", user_answers_text])
        if feedback_text:
            payload_lines.extend(["", feedback_text])
        return f"{static_prefix}\n\n" + "\n".join(payload_lines)

    def compile(
        self,
        task: TaskSpec,
        *,
        attempt: int = 0,
        last_error: str = "",
        last_verdict: str | None = None,
        compact_diff: str = "",
        repo_map_path: Path | None = None,
    ) -> tuple[str, int]:
        """Compile a Surgical Prompt and enforce max_prompt_tokens_per_call."""
        map_path = repo_map_path or (self.config.auto_engine_dir / "repo_map.xml")
        lookup_paths = list(dict.fromkeys(task.readonly_context_files + task.allowed_write_files))
        skeletons = extract_file_skeletons(map_path, lookup_paths)

        static_prefix = self.build_static_prefix()
        targets_text, all_inlined = self._format_target_files(task, skeletons)
        user_answers_text = self._load_user_answers_text()
        feedback_text = self._format_retry_feedback(
            attempt=attempt,
            last_error=last_error,
            last_verdict=last_verdict,
            compact_diff=compact_diff,
            all_targets_inlined=all_inlined,
        )

        readonly_text = self._format_readonly_context(
            task.readonly_context_files,
            skeletons,
            compact_mode=False,
        )
        prompt = self._assemble_prompt(
            static_prefix,
            task,
            readonly_text,
            targets_text,
            feedback_text,
            user_answers_text=user_answers_text,
        )
        tokens = estimate_tokens(prompt)

        cap = self.config.limits.max_prompt_tokens_per_call
        if tokens > cap:
            compact_readonly = self._format_readonly_context(
                task.readonly_context_files,
                skeletons,
                compact_mode=True,
            )
            prompt = self._assemble_prompt(
                static_prefix,
                task,
                compact_readonly,
                targets_text,
                feedback_text,
                user_answers_text=user_answers_text,
            )
            tokens = estimate_tokens(prompt)

        if tokens > cap:
            raise PromptTooLargeError(
                f"Task {task.task_id} prompt is ~{tokens} tokens, exceeding limit {cap}. Split task smaller."
            )
        return prompt, tokens
