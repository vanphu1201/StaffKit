"""Layer 3 Surgical Prompt Compiler: 0-token local-neighborhood builder with pre-flight cap."""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

from engine.blueprints import get_blueprint
from engine.indexer import (
    IndexerError,
    _extract_prd_identifiers,
    build_hierarchical_dir_summary,
    extract_fast_skeleton,
    extract_file_skeletons,
    extract_symbol_windows,
    list_real_files,
)
from engine.models import (
    EngineConfig,
    ExecutionProfile,
    TaskSpec,
    compute_dynamic_prompt_budget,
    normalize_posix_path,
    resolve_task_execution_profile,
)
from engine.verifier import extract_error_locations

_LOG_NOISE_NORM_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2}T[\d:.]+Z|\b\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)?\b|"request"\s*:\s*\d+|"elapsedMs"\s*:\s*\d+|\bpid:\s*\d+|\bport:\s*\d+)',
    re.IGNORECASE,
)


def compress_and_offload_objective(
    task: TaskSpec,
    auto_engine_dir: Path,
    *,
    max_inline_chars: int = 2000,
) -> str:
    """Deduplicate repetitive log noise in long objectives and offload verbatim text to disk (0-token)."""
    raw = (task.objective or "").strip()
    if len(raw) <= max_inline_chars:
        return raw

    attach_dir = auto_engine_dir / "task_attachments"
    attach_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "_", task.task_id) or "task"
    attach_path = attach_dir / f"{safe_id}_objective.txt"
    attach_path.write_text(raw + "\n", encoding="utf-8")
    rel_attach = f".auto-engine/task_attachments/{safe_id}_objective.txt"

    # Split either by newline or by timestamped single-line pasted log entries
    chunks = raw.splitlines()
    if len(chunks) <= 4 and len(raw) > max_inline_chars:
        chunks = re.split(r"(?=\b\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)\b)", raw)

    deduped: list[str] = []
    seen_counts: dict[str, int] = {}
    order_sigs: list[tuple[str, str]] = []
    for chunk in chunks:
        cleaned = chunk.strip()
        if not cleaned:
            continue
        sig = _LOG_NOISE_NORM_RE.sub("<TS>", cleaned)[:140]
        if sig in seen_counts:
            seen_counts[sig] += 1
        else:
            seen_counts[sig] = 1
            order_sigs.append((sig, cleaned))

    for sig, sample in order_sigs:
        cnt = seen_counts[sig]
        if cnt > 1:
            deduped.append(f"{sample[:320]}  [... lặp lại {cnt} lần]")
        else:
            deduped.append(sample[:420])

    compact = "\n".join(deduped)
    if len(compact) > max_inline_chars:
        compact = compact[: max_inline_chars - 160].rstrip() + "\n... [đã rút gọn log dài để tiết kiệm token]"
    return (
        f"{compact}\n\n"
        f"[Raw objective/log đầy đủ ({len(raw)} ký tự) đã được lưu tại `{rel_attach}` — "
        f"dùng `read_file` nếu cần xem chi tiết từng dòng gốc]"
    )


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

    def build_static_prefix(self, *, profile: ExecutionProfile = "build") -> str:
        """Build the immutable system rules prefix with Head-Start (not handcuffs) empowerment."""
        if self.config.agy.forbid_shell_verify_commands:
            verify_rule = (
                "- CHỈ sửa code theo đặc tả rồi kết thúc ngay. TUYỆT ĐỐI KHÔNG tự chạy lệnh test, build, typecheck hay lint "
                "bên trong phiên (hệ thống bên ngoài sẽ tự chạy verify và trả log rút gọn nếu cần)."
            )
        elif profile == "investigate_fix":
            verify_rule = (
                "- CHẾ ĐỘ ĐIỀU TRA & SỬA LỖI SÂU (INVESTIGATE & FIX — chuẩn `superpowers:systematic-debugging` + `OpenSpec`):\n"
                "  1. **Reproduce & Probe thật:** ĐƯỢC TOÀN QUYỀN đọc mở rộng file liên quan và chạy lệnh chẩn đoán nhanh (`curl`/HTTP probe dịch vụ local, kiểm tra port/process, chạy test đơn lẻ `npx vitest run <file>` / `pytest <file>`, `tsc --noEmit`) để nhìn thấy hành vi thật trước khi sửa, TUYỆT ĐỐI KHÔNG đoán mò.\n"
                "  2. **Isolate Root Cause:** Truy tận nguyên nhân gốc (kiểm tra payload/status thật, hợp đồng dữ liệu, cấu hình).\n"
                "  3. **Minimal Targeted Fix & Preserve Invariants:** Sửa triệt để đúng gốc rễ nhưng bảo toàn 100% các tính năng và test đang chạy tốt.\n"
                "  4. **Verify thật & Regression Test:** Tự chạy kiểm chứng nhanh bản vá + thêm/cập nhật test bao phủ kịch bản lỗi trước khi kết thúc (Engine bên ngoài vẫn sẽ chạy toàn bộ Verification Commands độc lập)."
            )
        else:
            verify_rule = (
                "- CHẾ ĐỘ XÂY DỰNG TỐI ƯU (HEAD-START, NOT HANDCUFFS): Engine đã chuẩn bị sẵn bản đồ, Contract và các file mục tiêu "
                "để bạn giảm tối đa thao tác mò mẫm thừa. Nếu cần đọc mở rộng sang file liên quan hoặc chạy nhanh lệnh kiểm tra "
                "cục bộ (typecheck/test lẻ) để chắc chắn code hoạt động thật 100%, bạn hoàn toàn chủ động thực hiện; tránh chạy "
                "lệnh cài đặt/build nặng lặp lại vì ngay sau khi bạn xong, Engine bên ngoài sẽ tự động chạy toàn bộ Verification Commands ở mức 0 token."
            )
        lines = [
            "[STATIC PREFIX]",
            "# SYSTEM RULES",
            "- Thực thi chính xác theo đặc tả TASK bên dưới.",
            "- Tuân thủ phạm vi file được giao; tuyệt đối không sửa `PROTECTED_TEST_FILES`.",
            verify_rule,
            "- KHÔNG lách luật bằng `@ts-ignore`, `eslint-disable`, `# type: ignore`, `# noqa`, `.skip()`, `.only()`, hoặc xóa bỏ assertion trong test.",
            "- Ưu tiên sửa gọn trực tiếp (targeted edit), không viết lại phần code không liên quan.",
            "- QUY TẮC GHI FILE TỪNG BƯỚC (INCREMENTAL WRITE): Khi TASK cần tạo mới hoặc sửa từ 2 file trở lên, BẮT BUỘC gọi tool `write_file` / `edit_file` cho TỪNG FILE MỘT (1 file mỗi lượt gọi tool) theo thứ tự phụ thuộc để lưu tiến độ ngay xuống đĩa; TUYỆT ĐỐI KHÔNG dồn tạo 3–5 file lớn vào cùng 1 lượt phản hồi duy nhất.",
            "- Kiểm tra và xử lý đầy đủ toàn bộ Acceptance Criteria cùng các nhánh biên (edge cases / input validation) nêu trong TASK và ARCHITECTURE CONTRACT để vượt qua cả kiểm thử đơn vị lẫn kiểm thử tích hợp cuối.",
            "- Tương thích môi trường thực thi (Windows / Node.js / Python): nếu một package C++ native (`node-gyp` như `better-sqlite3` trên Node v24 chưa cài Visual Studio C++) gây lỗi cài đặt hoặc thiếu binary, hãy chủ động dùng phiên bản prebuilt hoặc module tương đương có sẵn (`node:sqlite` `DatabaseSync` của Node 22+/24+ hoặc adapter tương thích cùng interface) để lệnh verify vượt qua 100%.",
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
        max_skel_lines = 14 if compact_mode else 40
        max_files = 4 if compact_mode else 6
        for raw_rel in readonly_files[:max_files]:
            rel = normalize_posix_path(raw_rel)
            if rel in ("openspec/prd.md", "prd.md") and compact_mode:
                continue
            if rel in skeletons:
                raw_lines = skeletons[rel].splitlines()
                body = "\n".join(raw_lines[:max_skel_lines])
                if len(raw_lines) > max_skel_lines:
                    body += "\n... [trimmed]"
                blocks.append(f"## {rel} (skeleton)\n```\n{body}\n```")
                continue

            full_path = self.repo_path / rel
            if full_path.is_file():
                lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
                max_raw = 14 if compact_mode else 28
                if len(lines) <= max_raw:
                    blocks.append(f"## {rel}\n```\n" + "\n".join(lines) + "\n```")
                else:
                    fast_skel = extract_fast_skeleton(full_path, max_lines=max_skel_lines)
                    if fast_skel:
                        blocks.append(f"## {rel} (pointer signatures, {len(lines)} lines)\n```\n{fast_skel}\n```")
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
        *,
        max_inline_lines: int | None = None,
        window_max_lines: int = 75,
    ) -> tuple[str, bool]:
        """Format target files with ultra-lean primary inlining and ranked AST function windows for large files."""
        blocks: list[str] = []
        all_inlined = True
        cfg_max_lines = self.config.limits.inline_file_max_lines
        max_lines = cfg_max_lines if max_inline_lines is None else max_inline_lines

        ordered_write_paths = list(
            dict.fromkeys(
                normalize_posix_path(r)
                for r in (*task.allowed_write_files, *task.create_new_files)
                if r and r.strip()
            )
        )
        existing_on_disk = [r for r in ordered_write_paths if (self.repo_path / r).is_file()]
        missing_on_disk = [r for r in ordered_write_paths if not (self.repo_path / r).is_file()]
        multi_target = len(existing_on_disk) > 1

        for idx, rel in enumerate(existing_on_disk):
            full_path = self.repo_path / rel
            lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
            effective_max = min(max_lines, 160 if (multi_target and idx == 0) else (90 if multi_target else max_lines))
            if max_inline_lines is not None:
                effective_max = max_inline_lines

            if len(lines) <= effective_max:
                numbered = "\n".join(f"L{ln_idx}: {line}" for ln_idx, line in enumerate(lines, start=1))
                blocks.append(f"## {rel} ({len(lines)} lines — ALREADY EXISTS ON DISK)\n```\n{numbered}\n```")
            else:
                all_inlined = False
                identifiers = _extract_prd_identifiers(task.objective)
                skel_cap = 30 if window_max_lines >= 50 else 20
                raw_skel = skeletons.get(rel) or extract_fast_skeleton(
                    full_path,
                    max_lines=skel_cap,
                    priority_identifiers=identifiers,
                )
                if raw_skel:
                    skel_lines = raw_skel.splitlines()
                    skel = "\n".join(skel_lines[:skel_cap]) + (
                        "\n... [skeleton trimmed]" if len(skel_lines) > skel_cap else ""
                    )
                else:
                    skel = "(File > 300 dòng; hãy đọc có chọn lọc theo symbol/line range)"
                radius = 18 if window_max_lines >= 60 else 10
                win_text = extract_symbol_windows(full_path, identifiers, radius=radius, max_total_lines=window_max_lines)
                body = skel
                if win_text:
                    body = f"{skel}\n\n// Relevant line windows:\n{win_text}"
                blocks.append(
                    f"## {rel} ({len(lines)} lines > {cfg_max_lines} limit — skeleton only)\n```\n{body}\n```"
                )

        for rel in missing_on_disk:
            blocks.append(f"## {rel}\n(NEW FILE TO CREATE — hãy dùng `write_file` tạo riêng từng file một)")

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
        if last_verdict in {"MAX_TURNS_EXCEEDED", "TIMEOUT"}:
            parts.append(
                "## CONTINUATION RESUME (KHÔNG LÀM LẠI TỪ ĐẦU)\n"
                "Phiên trước đã chạm giới hạn khi bạn đang thực hiện dở. Các thay đổi hợp lệ trên đĩa ĐÃ ĐƯỢC GIỮ NGUYÊN — "
                "hãy đọc nhanh phần đã sửa/diff bên dưới, tiếp tục hoàn tất các file/test còn thiếu rồi xác nhận verify PASS."
            )
        rolled_back_or_clean = last_verdict in {
            "SCOPE_VIOLATION",
            "TEST_GAMING",
            "EMPTY_DIFF",
            "BUDGET_EXCEEDED",
        }
        # Avoid sending duplicate diff when target files <= 300 lines are already inlined on disk.
        if not rolled_back_or_clean and (not all_targets_inlined or last_verdict in {"MAX_TURNS_EXCEEDED", "TIMEOUT"}) and compact_diff.strip():
            parts.append("## Current Git Diff / Partial Progress (Rút gọn):\n```diff\n" + compact_diff.strip() + "\n```")

        if last_error.strip():
            parts.append("## Trimmed Error Log / Violation Summary:\n```\n" + last_error.strip() + "\n```")
            err_locs = extract_error_locations(last_error, self.repo_path, max_locations=3)
            loc_Snippets: list[str] = []
            for rel, lineno in err_locs:
                full_p = self.repo_path / rel
                if not full_p.is_file():
                    continue
                f_lines = full_p.read_text(encoding="utf-8", errors="replace").splitlines()
                if not f_lines or lineno < 1 or lineno > len(f_lines):
                    continue
                s_ln = max(1, lineno - 10)
                e_ln = min(len(f_lines), lineno + 10)
                numbered = "\n".join(
                    f"{'>' if ln == lineno else ' '} L{ln}: {f_lines[ln - 1]}"
                    for ln in range(s_ln, e_ln + 1)
                )
                loc_Snippets.append(f"### {rel} (around L{lineno})\n```\n{numbered}\n```")
            if loc_Snippets:
                parts.append("## Crashing Code Context (Auto-Extracted from Traceback):\n" + "\n\n".join(loc_Snippets))

            err_low = last_error.lower()
            if any(k in err_low for k in ("npm run ", "vite ", "tsc", "entry module", "package.json")):
                pkg_p = self.repo_path / "package.json"
                if pkg_p.is_file():
                    pkg_txt = pkg_p.read_text(encoding="utf-8", errors="replace").strip()
                    parts.append(
                        "## Root `package.json` (ĐƯỢC PHÉP sửa script build/dev/test nếu lệch đường dẫn config như `--config web/vite.config.ts`):\n```json\n"
                        + pkg_txt[:2500]
                        + "\n```"
                    )

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

    def _load_constitution_text(self, max_lines: int = 28) -> str:
        """Load `.auto-engine/constitution.md` (spec-kit style immutable project principles)."""
        for candidate in (
            self.config.auto_engine_dir / "constitution.md",
            self.repo_path / "openspec" / "constitution.md",
        ):
            if candidate.is_file():
                try:
                    raw = candidate.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                if raw:
                    c_lines = raw.splitlines()
                    if len(c_lines) > max_lines:
                        return "\n".join(c_lines[:max_lines]) + "\n... [constitution trimmed]"
                    return "\n".join(c_lines)
        return ""

    def _load_contract_text(self, max_lines: int = 40) -> str:
        """Load shared `.auto-engine/contracts.md` or `openspec/contracts.md` so all tasks stay interface-aligned."""
        for candidate in (
            self.config.auto_engine_dir / "contracts.md",
            self.repo_path / "openspec" / "contracts.md",
        ):
            if candidate.is_file():
                try:
                    raw = candidate.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                if raw:
                    c_lines = raw.splitlines()
                    if len(c_lines) > max_lines:
                        return "\n".join(c_lines[:max_lines]) + "\n... [contract trimmed]"
                    return "\n".join(c_lines)
        return ""

    def _load_handoff_text(self, current_task_id: str, max_lines: int = 28) -> str:
        """Load 0-token episodic handoff memory from upstream completed tasks (`.auto-engine/handoff.md`)."""
        handoff_path = self.config.auto_engine_dir / "handoff.md"
        if not handoff_path.is_file():
            return ""
        try:
            raw = handoff_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        if not raw:
            return ""
        blocks = [b.strip() for b in raw.split("\n### ") if b.strip()]
        kept: list[str] = []
        for idx, b in enumerate(blocks):
            header = b if (idx == 0 and b.startswith("### ")) else f"### {b}"
            if header.startswith(f"### {current_task_id}:") or header.startswith(f"### {current_task_id} "):
                continue
            kept.append(header)
        if not kept:
            return ""
        merged = "\n\n".join(kept[-6:]).splitlines()
        if len(merged) > max_lines:
            return "\n".join(merged[-max_lines:])
        return "\n".join(merged)

    def _format_repo_file_tree(self, task: TaskSpec) -> str:
        """Build a relevant-first paths/folder tree so mega-repos stay compact while skipping list_dir exploration."""
        try:
            real_files = sorted(list_real_files(self.repo_path))
        except (IndexerError, OSError):
            return ""
        known = len(task.allowed_write_files) + len(task.readonly_context_files)
        if len(real_files) <= 8 or len(real_files) <= known:
            return ""
        if len(real_files) <= 30:
            return "\n".join(real_files)

        focus_paths = [
            normalize_posix_path(p)
            for p in (*task.allowed_write_files, *task.readonly_context_files)
        ]
        exact_dirs = {posixpath.dirname(p) or "." for p in focus_paths}
        depth_dirs: set[str] = set()
        for p in focus_paths:
            parts = p.split("/")
            depth_dirs.add("." if len(parts) <= 1 else "/".join(parts[: min(len(parts) - 1, 3)]))

        siblings = [p for p in real_files if (posixpath.dirname(p) or ".") in exact_dirs][:25]
        summary = build_hierarchical_dir_summary(set(real_files), priority_dirs=depth_dirs, max_entries=25)
        parts_out: list[str] = []
        if siblings:
            parts_out.append("## Nearby Sibling Files\n" + "\n".join(siblings))
        parts_out.append(f"## Folder Overview ({len(real_files)} tracked files)\n{summary}")
        return "\n\n".join(parts_out)

    def _format_protected_tests(self, task: TaskSpec) -> str:
        """Keep Protected Test Files concise on mega-repos while GitGuard enforces the full list."""
        protected = task.protected_test_files
        if len(protected) <= 25:
            return repr(protected)
        target_tokens = {
            tok.lower()
            for p in task.allowed_write_files
            for tok in normalize_posix_path(p).replace(".", "/").split("/")
            if len(tok) >= 3 and tok.lower() not in {"src", "lib", "app", "test", "tests", "py", "ts", "js"}
        }
        ranked = sorted(
            protected,
            key=lambda p: (
                -sum(1 for tok in target_tokens if tok in p.lower()),
                p,
            ),
        )
        nearest = ranked[:15]
        extra = len(protected) - 15
        return f"{nearest!r} ... (+{extra} more protected test files; DO NOT modify any existing test file unless listed in Allowed Write Files)"

    def _assemble_prompt(
        self,
        static_prefix: str,
        task: TaskSpec,
        readonly_text: str,
        targets_text: str,
        feedback_text: str,
        user_answers_text: str = "",
        repo_tree_text: str = "",
        contract_text: str = "",
        handoff_text: str = "",
        constitution_text: str = "",
        objective_override: str | None = None,
    ) -> str:
        verify_cmds = task.verify_commands or self.config.executor.default_verify_commands
        is_strict = task.strict_scope and self.config.guards.scope_mode == "strict"
        scope_desc = (
            "STRICT (chỉ sửa/tạo các file trong danh sách trên)"
            if is_strict
            else "SOFT / FLEXIBLE (Ưu tiên sửa/tạo các file trong Allowed Write Files; nếu quá trình điều tra/build/verify phát hiện nguyên nhân gốc nằm ở file liên quan hoặc cấu hình như `package.json`, `tsconfig.json`, `vite.config.ts` thì ĐƯỢC PHÉP sửa để sản phẩm chạy thật 100%; tuyệt đối không sửa PROTECTED_TEST_FILES)"
        )
        effective_objective = objective_override if objective_override is not None else task.objective
        payload_lines = [
            "---",
            "[DYNAMIC TASK PAYLOAD - PER TASK]",
            "<task_spec>",
            f"# TASK: {task.task_id} - {task.title}",
            f"- Objective: {effective_objective}",
            f"- Acceptance Criteria: {task.acceptance_criteria}",
            f"- Allowed Write Files: {task.allowed_write_files}",
            f"- Create New Files: {task.create_new_files}",
            f"- Scope Mode: {scope_desc}",
            f"- Protected Test Files (DO NOT TOUCH): {self._format_protected_tests(task)}",
            f"- Constraints: {task.constraints}",
            f"- Verification Commands (Engine sẽ chạy sau khi bạn xong): {verify_cmds}",
            "</task_spec>",
        ]
        if constitution_text:
            payload_lines.extend(
                [
                    "",
                    "<project_constitution>",
                    "# PROJECT CONSTITUTION (Immutable Principles — Inherited Across All Tasks & Updates)",
                    constitution_text,
                    "</project_constitution>",
                ]
            )
        if contract_text:
            payload_lines.extend(
                [
                    "",
                    "<architecture_contract>",
                    "# ARCHITECTURE & INTERFACE CONTRACT (Shared Across All Tasks)",
                    contract_text,
                    "</architecture_contract>",
                ]
            )
        if handoff_text:
            payload_lines.extend(
                [
                    "",
                    "<upstream_handoff>",
                    "# UPSTREAM COMPLETED TASKS HANDOFF (0-Token Memory — Files & Signatures Already Built)",
                    handoff_text,
                    "</upstream_handoff>",
                ]
            )
        payload_lines.extend(
            [
                "",
                "<readonly_context>",
                "# READONLY CONTEXT (Interfaces / Types / Signatures)",
                readonly_text,
                "</readonly_context>",
                "",
                "<target_files>",
                "# TARGET FILES TO MODIFY",
                targets_text,
                "</target_files>",
            ]
        )
        if repo_tree_text:
            payload_lines.extend(["", "<repo_tree>", "# REPO FILE TREE (paths only)", repo_tree_text, "</repo_tree>"])
        if user_answers_text:
            payload_lines.extend(["", "<user_answers>", "# USER ANSWERS & REAL ASSETS", user_answers_text, "</user_answers>"])
        if feedback_text:
            payload_lines.extend(["", "<retry_feedback>", feedback_text, "</retry_feedback>"])
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
        """Compile a Surgical Prompt with adaptive per-task token budgeting and 0-token log offloading."""
        map_path = repo_map_path or (self.config.auto_engine_dir / "repo_map.xml")
        lookup_paths = list(dict.fromkeys(task.readonly_context_files + task.allowed_write_files))
        skeletons = extract_file_skeletons(map_path, lookup_paths)

        profile = resolve_task_execution_profile(task, attempt=attempt)
        static_prefix = self.build_static_prefix(profile=profile)
        compact_objective = compress_and_offload_objective(task, self.config.auto_engine_dir)
        targets_text, all_inlined = self._format_target_files(task, skeletons)
        user_answers_text = self._load_user_answers_text()
        constitution_text = self._load_constitution_text(max_lines=28)
        contract_text = self._load_contract_text(max_lines=40)
        handoff_text = self._load_handoff_text(task.task_id, max_lines=28)
        repo_tree_text = self._format_repo_file_tree(task)
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
            repo_tree_text=repo_tree_text,
            contract_text=contract_text,
            handoff_text=handoff_text,
            constitution_text=constitution_text,
            objective_override=compact_objective,
        )
        tokens = estimate_tokens(prompt)

        cap, is_strict_cap = compute_dynamic_prompt_budget(task, self.config, attempt=attempt)
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
                repo_tree_text="",
                contract_text=self._load_contract_text(max_lines=18),
                objective_override=compact_objective,
            )
            tokens = estimate_tokens(prompt)

        if tokens > cap and (not is_strict_cap or cap >= 2000):
            for inline_cap, win_cap in ((60, 60), (0, 30)):
                compact_targets, _ = self._format_target_files(
                    task,
                    skeletons,
                    max_inline_lines=inline_cap,
                    window_max_lines=win_cap,
                )
                compact_readonly = self._format_readonly_context(
                    task.readonly_context_files,
                    skeletons,
                    compact_mode=True,
                )
                prompt = self._assemble_prompt(
                    static_prefix,
                    task,
                    compact_readonly,
                    compact_targets,
                    feedback_text,
                    user_answers_text=user_answers_text,
                    repo_tree_text="",
                    contract_text="",
                    objective_override=compact_objective,
                )
                tokens = estimate_tokens(prompt)
                if tokens <= cap:
                    break

        if tokens > cap and not is_strict_cap:
            # Adaptive graceful pointer-only fallback: never block a real task with PromptTooLargeError
            pointer_targets = "\n".join(
                f"- `{p}` (file đã có trên đĩa — dùng `read_file` có chọn lọc)"
                for p in dict.fromkeys(task.allowed_write_files + task.create_new_files)
            ) or "(Không có)"
            pointer_readonly = "\n".join(f"- `{p}`" for p in task.readonly_context_files[:8]) or "(Không có)"
            prompt = self._assemble_prompt(
                static_prefix,
                task,
                pointer_readonly,
                pointer_targets,
                feedback_text[:6000],
                user_answers_text=user_answers_text[:2000],
                repo_tree_text="",
                contract_text="",
                objective_override=compact_objective[:2000],
            )
            tokens = estimate_tokens(prompt)
            return prompt, tokens

        if tokens > cap:
            raise PromptTooLargeError(
                f"Task {task.task_id} prompt is ~{tokens} tokens, exceeding limit {cap}. Split task smaller."
            )
        return prompt, tokens
