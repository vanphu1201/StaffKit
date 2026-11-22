"""Layer 2 Planner: PRD Ambiguity Gate, DAG Plan Generation, and virtual_files(T) Validation."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import httpx

from engine.bridges.agy_cli_executor import (
    _extract_tool_call,
    build_agy_command,
    encode_stream_user_message,
    kill_process_tree,
)
from engine.database import StateDB
from engine.indexer import RepoIndexer, compact_repo_map_for_planner, list_real_files
from engine.models import EngineConfig, PlanOutput, TaskSpec, inline_json_schema, normalize_posix_path

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


class PlannerError(RuntimeError):
    """Raised when PRD planning or DAG validation fails."""


def topological_sort_tasks(tasks: list[TaskSpec]) -> tuple[list[TaskSpec], dict[str, set[str]]]:
    """Topologically sort tasks and compute ancestor IDs; raise PlannerError on cycles/unknown deps."""
    by_id: dict[str, TaskSpec] = {}
    for t in tasks:
        if t.task_id in by_id:
            raise PlannerError(f"Duplicate task_id `{t.task_id}` in plan.")
        by_id[t.task_id] = t

    in_degree: dict[str, int] = {tid: 0 for tid in by_id}
    children: dict[str, list[str]] = {tid: [] for tid in by_id}

    for t in tasks:
        for dep in t.depends_on:
            if dep not in by_id:
                raise PlannerError(f"Task `{t.task_id}` depends on unknown task `{dep}`.")
            if dep == t.task_id:
                raise PlannerError(f"Task `{t.task_id}` cannot depend on itself.")
            in_degree[t.task_id] += 1
            children[dep].append(t.task_id)

    queue: deque[str] = deque(tid for tid, deg in in_degree.items() if deg == 0)
    ordered: list[TaskSpec] = []
    ancestors: dict[str, set[str]] = {tid: set() for tid in by_id}

    while queue:
        cur = queue.popleft()
        ordered.append(by_id[cur])
        for nxt in children[cur]:
            ancestors[nxt].add(cur)
            ancestors[nxt].update(ancestors[cur])
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    if len(ordered) != len(tasks):
        raise PlannerError("Dependency cycle detected in task DAG.")
    return ordered, ancestors


def validate_plan_deterministic(
    plan: PlanOutput,
    real_files: set[str],
    max_files_per_task: int,
) -> tuple[list[TaskSpec], list[str]]:
    """Run 0-token DAG, max-files, protected-disjoint, and virtual_files(T) checks."""
    if not plan.tasks:
        return [], ["Plan has status READY but contains zero tasks."]

    ordered, ancestors = topological_sort_tasks(plan.tasks)
    by_id = {t.task_id: t for t in ordered}
    norm_real = {normalize_posix_path(f) for f in real_files}
    errors: list[str] = []

    for task in ordered:
        if len(task.allowed_write_files) > max_files_per_task:
            errors.append(
                f"{task.task_id}: allowed_write_files count ({len(task.allowed_write_files)}) "
                f"> max_files_per_task ({max_files_per_task})."
            )

        write_or_new = set(task.allowed_write_files) | set(task.create_new_files)
        overlap = write_or_new & set(task.protected_test_files)
        if overlap:
            errors.append(
                f"{task.task_id}: protected_test_files overlaps allowed_write_files/create_new_files: {sorted(overlap)}"
            )

        ancestor_created: set[str] = set()
        for anc_id in ancestors[task.task_id]:
            ancestor_created.update(by_id[anc_id].create_new_files)
        virtual_files = norm_real | ancestor_created

        new_set = set(task.create_new_files)
        for existing_new in new_set & virtual_files:
            errors.append(
                f"{task.task_id}: create_new_files entry `{existing_new}` already exists in virtual_files(T)."
            )

        must_exist = (
            (set(task.allowed_write_files) - new_set)
            | set(task.readonly_context_files)
            | set(task.protected_test_files)
        )
        for path in sorted(must_exist):
            if path not in virtual_files:
                errors.append(
                    f"{task.task_id}: referenced file `{path}` does not exist in virtual_files(T)."
                )

    return ordered, errors


def _extract_plan_json_payload(raw_text: str) -> str:
    """Unwrap JSON payload from raw text, markdown fences, or agy stream-json NDJSON output."""
    stripped = raw_text.strip()

    # Handle NDJSON stream-json output from `agy --output-format stream-json`
    for line in reversed(stripped.splitlines()):
        line_str = line.strip()
        if not line_str:
            continue
        try:
            ev = json.loads(line_str)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("event") == "result" and isinstance(ev.get("result"), dict):
            resp = ev["result"].get("response")
            if isinstance(resp, str) and resp.strip():
                stripped = resp.strip()
                break
            if isinstance(resp, dict):
                return json.dumps(resp)

    try:
        outer = json.loads(stripped)
        if isinstance(outer, dict):
            if "status" in outer and ("tasks" in outer or "clarifying_questions" in outer):
                return stripped
            for key in ("result", "response", "text", "output", "content"):
                inner = outer.get(key)
                if isinstance(inner, str) and inner.strip():
                    stripped = inner.strip()
                    break
                if isinstance(inner, dict) and "status" in inner:
                    return json.dumps(inner)
    except json.JSONDecodeError:
        pass

    fence_match = JSON_FENCE_RE.search(stripped)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


class Planner:
    """Generates and deterministically validates DAG TaskSpecs from PRD or external task files."""

    def __init__(
        self,
        config: EngineConfig,
        db: StateDB,
        indexer: RepoIndexer,
        llm_caller: Callable[[str, Path], str] | None = None,
        on_event: Callable[[str, str], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.indexer = indexer
        self._llm_caller = llm_caller
        self.on_event = on_event
        self.stop_event = stop_event

    def _emit(self, level: str, message: str) -> None:
        if self.on_event is not None:
            self.on_event(level, message)

    def _call_llm(self, prompt: str, schema_path: Path) -> str:
        if self._llm_caller is not None:
            return self._llm_caller(prompt, schema_path)

        if self.config.executor.mode in ("aider", "opencode") and self.config.gateway is not None:
            return self._call_litellm_planner(prompt)

        tier = self.config.agy.tiers.get("planner")
        model = tier.model if tier else ""
        effort = tier.effort if tier else "high"
        cmd = build_agy_command(
            self.config,
            model,
            effort,
            input_format="stream-json",
            output_format="stream-json",
            json_schema_path=schema_path,
        )
        self._emit("info", f"Gọi Planner `agy` (effort={effort}, prompt={len(prompt)} chars)...")
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.config.target_repo_path),
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

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        out_lines: list[str] = []
        tool_calls = 0
        seen_tool_steps: set[int] = set()
        deadline = time.monotonic() + self.config.executor.timeout_seconds

        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                kill_process_tree(proc)
                raise PlannerError("Planner đã được dừng theo yêu cầu người dùng.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kill_process_tree(proc)
                raise PlannerError(f"Planner `agy` quá thời gian {self.config.executor.timeout_seconds}s.")
            try:
                item = line_q.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if proc.poll() is not None and not t.is_alive():
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
            if not isinstance(ev, dict):
                continue

            tc = _extract_tool_call(ev)
            if tc is not None:
                step_obj = ev.get("step_update")
                step_idx = step_obj.get("step_index") if isinstance(step_obj, dict) else ev.get("step_index")
                if not isinstance(step_idx, int) or step_idx not in seen_tool_steps:
                    if isinstance(step_idx, int):
                        seen_tool_steps.add(step_idx)
                    tool_calls += 1
                    self._emit("warn", f"Planner đang gọi tool `{tc[0]}` ({tool_calls}/3)...")
                    if tool_calls > 3:
                        kill_process_tree(proc)
                        raise PlannerError(
                            "Planner cố gọi quá nhiều tool đọc repo thay vì dùng Repo Skeleton Map. Đã ngắt để tiết kiệm token."
                        )

            step = ev.get("step_update")
            if isinstance(step, dict) and step.get("state") == "DONE":
                stype = step.get("step_type")
                if stype == "agent_response":
                    self._emit("info", "Planner đã phản hồi xong, đang kiểm tra DAG...")

        proc.wait(timeout=5)
        stderr_text = proc.stderr.read().strip()
        if proc.returncode != 0:
            raise PlannerError(
                f"Planner agy invocation failed (exit {proc.returncode}): {stderr_text or '\n'.join(out_lines)}"
            )
        return "\n".join(out_lines)

    def _call_litellm_planner(self, prompt: str) -> str:
        gw = self.config.gateway
        assert gw is not None
        api_key = os.environ.get(gw.api_key_env, "")
        model = gw.models.get("planner", "tier-planner")
        resp = httpx.post(
            f"{gw.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _build_planner_prompt(self, prd_text: str, repo_map_xml: str, repair_errors: list[str] | None = None) -> str:
        compact_map = compact_repo_map_for_planner(repo_map_xml)
        base = f"""Bạn là Software Architect lập kế hoạch DAG cho Auto-Prompt Engine.
QUY TẮC TỐI QUAN TRỌNG: TUYỆT ĐỐI KHÔNG gọi bất kỳ tool nào (`view_file`, `list_dir`, `grep_search`, `run_command`...). Toàn bộ danh sách file và chữ ký code cần thiết đã có sẵn trong `# REPO SKELETON MAP` bên dưới — hãy phân tích và trả về JSON ngay trong 1 lượt duy nhất.

Hãy phân tích PRD và Repo Skeleton Map dưới đây để trả về JSON đúng schema `PlanOutput`:
1. Nếu PRD mơ hồ, thiếu thông số API/DB/UX cốt lõi hoặc mâu thuẫn không thể suy đoán an toàn:
   - Đặt `status = "NEEDS_CLARIFICATION"`, liệt kê tối đa 5 câu hỏi ngắn gọn trong `clarifying_questions`, để `tasks = []`.
2. Nếu PRD đủ rõ:
   - Đặt `status = "READY"`, chia thành các Atomic Task nhỏ gọn (`len(allowed_write_files) <= {self.config.limits.max_files_per_task}`).
   - Gán đúng `complexity` (`low` | `medium` | `high`), `protected_test_files` (không trùng `allowed_write_files`), `needs_lsp_mcp`, `needs_docs_mcp`, và mảng `verify_commands: list[str]` (từng lệnh đơn, KHÔNG ghép `&&`).
   - Tuyệt đối KHÔNG bịa đường dẫn file không có trong repo (trừ file khai báo trong `create_new_files`).

# PRD
{prd_text}

# REPO SKELETON MAP
{compact_map}
"""
        if repair_errors:
            err_bullets = "\n".join(f"- {e}" for e in repair_errors)
            base += f"\n# FIX VALIDATION ERRORS (Tối đa 1 lần sửa)\n{err_bullets}\n"
        return base

    def _persist_plan(self, ordered_tasks: list[TaskSpec], auto_approve: bool) -> Path:
        auto_dir = self.config.auto_engine_dir
        auto_dir.mkdir(parents=True, exist_ok=True)
        plan_path = auto_dir / "plan.json"
        serialized = json.dumps([t.model_dump() for t in ordered_tasks], indent=2, ensure_ascii=False)
        plan_path.write_text(serialized, encoding="utf-8")
        plan_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        approved = auto_approve or (not self.config.sprint.require_plan_approval)
        self.db.replace_plan(ordered_tasks, plan_hash=plan_hash, approved=approved)
        return plan_path

    def plan_from_prd(
        self,
        prd_path: Path,
        *,
        auto_approve: bool = False,
        force_reindex: bool = False,
    ) -> PlanOutput:
        """Generate, validate via DAG virtual_files(T), and store a sprint plan from PRD."""
        if not prd_path.is_file():
            raise PlannerError(f"PRD file not found: {prd_path}")

        prd_text = prd_path.read_text(encoding="utf-8", errors="replace")
        repo_map_path = self.indexer.build_repo_map(force=force_reindex)
        repo_map_xml = repo_map_path.read_text(encoding="utf-8", errors="replace")
        real_files = list_real_files(self.config.target_repo_path)

        schema_path = self.config.auto_engine_dir / "plan_schema.json"
        schema_path.write_text(
            json.dumps(inline_json_schema(PlanOutput.model_json_schema()), indent=2),
            encoding="utf-8",
        )

        prompt = self._build_planner_prompt(prd_text, repo_map_xml)
        raw_resp = self._call_llm(prompt, schema_path)
        plan = PlanOutput.model_validate_json(_extract_plan_json_payload(raw_resp))

        if plan.status == "NEEDS_CLARIFICATION":
            plan.clarifying_questions = plan.clarifying_questions[:5]
            return plan

        ordered, errors = validate_plan_deterministic(
            plan,
            real_files,
            self.config.limits.max_files_per_task,
        )
        if errors:
            # Allow strictly one deterministic repair round.
            repair_prompt = self._build_planner_prompt(prd_text, repo_map_xml, repair_errors=errors)
            raw_repair = self._call_llm(repair_prompt, schema_path)
            plan = PlanOutput.model_validate_json(_extract_plan_json_payload(raw_repair))
            if plan.status == "NEEDS_CLARIFICATION":
                plan.clarifying_questions = plan.clarifying_questions[:5]
                return plan
            ordered, errors = validate_plan_deterministic(
                plan,
                real_files,
                self.config.limits.max_files_per_task,
            )
            if errors:
                raise PlannerError("Planner failed deterministic DAG/file validation:\n" + "\n".join(errors))

        self._persist_plan(ordered, auto_approve=auto_approve)
        return PlanOutput(status="READY", tasks=ordered)

    def import_external_tasks(self, source_path: Path, *, auto_approve: bool = False) -> PlanOutput:
        """Import tasks from claude-task-master tasks.json or an openspec/ directory."""
        target = source_path
        if source_path.is_dir():
            candidate = source_path / "tasks.json"
            if not candidate.is_file():
                raise PlannerError(f"No tasks.json found inside directory {source_path}")
            target = candidate

        raw = json.loads(target.read_text(encoding="utf-8"))
        raw_tasks = raw.get("tasks", raw) if isinstance(raw, dict) else raw
        if not isinstance(raw_tasks, list):
            raise PlannerError(f"Expected list of tasks in {target}")

        parsed_tasks: list[TaskSpec] = []
        for idx, item in enumerate(raw_tasks, start=1):
            if not isinstance(item, dict):
                continue
            tid = str(item.get("task_id") or item.get("id") or f"T-{idx:02d}")
            title = str(item.get("title") or tid)
            objective = str(item.get("objective") or item.get("description") or title)
            deps = [str(d) for d in (item.get("depends_on") or item.get("dependencies") or [])]

            # Convert legacy single string verify_command if present in external JSON
            v_cmds = item.get("verify_commands")
            if not v_cmds and item.get("verify_command"):
                v_cmds = [part.strip() for part in str(item["verify_command"]).split("&&") if part.strip()]

            parsed_tasks.append(
                TaskSpec(
                    task_id=tid,
                    title=title,
                    complexity=item.get("complexity", "medium"),
                    depends_on=deps,
                    objective=objective,
                    acceptance_criteria=item.get("acceptance_criteria", []),
                    allowed_write_files=item.get("allowed_write_files", []),
                    create_new_files=item.get("create_new_files", []),
                    protected_test_files=item.get("protected_test_files", []),
                    readonly_context_files=item.get("readonly_context_files", []),
                    needs_docs_mcp=bool(item.get("needs_docs_mcp", False)),
                    needs_lsp_mcp=bool(item.get("needs_lsp_mcp", False)),
                    constraints=item.get("constraints", []),
                    verify_commands=v_cmds or [],
                )
            )

        real_files = list_real_files(self.config.target_repo_path)
        ordered, errors = validate_plan_deterministic(
            PlanOutput(status="READY", tasks=parsed_tasks),
            real_files,
            self.config.limits.max_files_per_task,
        )
        if errors:
            raise PlannerError("Imported tasks failed validation:\n" + "\n".join(errors))

        self._persist_plan(ordered, auto_approve=auto_approve)
        return PlanOutput(status="READY", tasks=ordered)
