"""Starlette ASGI backend and thread-safe state controller for the local Desktop App."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from engine.blueprints import get_blueprint, load_blueprints
from engine.bootstrapper import bootstrap_project, detect_existing_stack, ensure_git_repo_ready
from engine.bridges.agy_cli_executor import list_agy_models, smoke_test_agy
from engine.compiler import PromptCompiler
from engine.database import StateDB
from engine.evaluator import audit_historical_commits
from engine.git_guard import GitGuard
from engine.indexer import RepoIndexer
from engine.interviewer import (
    ProductInterviewer,
    persist_project_contracts,
    save_custom_blueprint_if_needed,
)
from engine.models import EngineConfig, ProductSpec, TaskSpec
from engine.orchestrator import Orchestrator
from engine.planner import Planner
from engine.verifier import Verifier

STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"


def _inspect_tools(config: EngineConfig) -> dict[str, dict[str, Any]]:
    checks = {
        "git": shutil.which("git"),
        "agy": shutil.which(config.agy.binary),
        "npx": shutil.which("npx.cmd") or shutil.which("npx"),
        "rtk": shutil.which("rtk"),
        "uv": shutil.which("uv"),
    }
    return {
        name: {"ready": bool(path), "path": path or "", "optional": name in ("rtk", "uv")}
        for name, path in checks.items()
    }


def _pick_folder_native(initial_dir: str | None = None) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(initialdir=initial_dir or None, title="Chọn thư mục dự án (Git repo)")
        return Path(chosen).as_posix() if chosen else ""
    finally:
        root.destroy()


def _pick_file_native(initial_dir: str | None = None) -> tuple[str, str]:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askopenfilename(
            initialdir=initial_dir or None,
            title="Chọn file PRD (.md) hoặc tasks.json",
            filetypes=[("PRD / Task JSON", "*.md *.markdown *.json *.txt"), ("All files", "*.*")],
        )
        if not chosen:
            return "", ""
        path = Path(chosen)
        return path.as_posix(), path.read_text(encoding="utf-8", errors="replace")
    finally:
        root.destroy()


def _pick_asset_native(initial_dir: str | None = None) -> tuple[str, str]:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askopenfilename(
            initialdir=initial_dir or None,
            title="Chọn ảnh thực tế, file 3D (.glb/.gltf) hoặc file cấu hình",
            filetypes=[
                ("Ảnh & 3D Assets", "*.png *.jpg *.jpeg *.webp *.glb *.gltf *.hdr *.env *.json *.csv"),
                ("All files", "*.*"),
            ],
        )
        if not chosen:
            return "", ""
        path = Path(chosen)
        return path.as_posix(), path.name
    finally:
        root.destroy()


class DesktopAppController:
    """Coordinates background Planner/Orchestrator execution and live SSE logging."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self._ensure_config_file()

        self._lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.worker_status: str = "idle"
        self.worker_message: str = "Sẵn sàng"
        self.clarifying_questions: list[str] = []

        # Mid-sprint Human-in-the-Loop interaction state
        self._interaction_event = threading.Event()
        self._interaction_answers: dict[str, str] = {}
        self.pending_interaction: dict[str, Any] | None = None

        # Layer 0 Non-Tech Wizard state
        self.wizard_step: int = 1
        self.wizard_idea: str = ""
        self.wizard_stack_hint: str = "auto"
        self.wizard_parent_dir: str = str(Path.home() / "AutoProjects")
        self.wizard_interview: dict[str, Any] | None = None
        self.wizard_spec: dict[str, Any] | None = None
        self.wizard_preview_cmd: str = ""
        self.wizard_done_message: str = ""

        self._logs: deque[dict[str, str]] = deque(maxlen=400)
        self._traces: deque[dict[str, Any]] = deque(maxlen=400)
        self._sse_backlog: deque[dict[str, Any]] = deque(maxlen=500)
        self._trace_seq: int = 0
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = []

    def _ensure_config_file(self) -> None:
        if self.config_path.is_file():
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        example = self.config_path.parent / "config.example.yaml"
        if not example.is_file():
            example = Path(__file__).resolve().parents[3] / "config.example.yaml"
        if example.is_file():
            shutil.copyfile(example, self.config_path)
        else:
            default_yaml = yaml.safe_dump(EngineConfig().model_dump(), sort_keys=False, allow_unicode=True)
            self.config_path.write_text(default_yaml, encoding="utf-8")

    def load_config(self) -> EngineConfig:
        return EngineConfig.load(self.config_path)

    def emit_log(self, level: str, message: str) -> None:
        entry = {
            "event_type": "log",
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        with self._lock:
            self._logs.append(entry)
            self._sse_backlog.append(entry)
            subs = list(self._subscribers)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, entry)
            except RuntimeError:
                pass

    def emit_trace(self, trace_entry: dict[str, Any]) -> None:
        with self._lock:
            self._trace_seq += 1
            enriched = dict(trace_entry)
            enriched.setdefault("time", datetime.now().strftime("%H:%M:%S"))
            enriched["seq"] = self._trace_seq
            self._traces.append(enriched)
            sse_payload = {"event_type": "trace", "trace": enriched}
            self._sse_backlog.append(sse_payload)
            subs = list(self._subscribers)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, sse_payload)
            except RuntimeError:
                pass

    def request_user_input(self, task_id: str, title: str, questions: list[str]) -> dict[str, str] | None:
        """Pause worker and wait for the user to answer task-specific questions from the UI."""
        with self._lock:
            prev_status = self.worker_status
            prev_msg = self.worker_message
            self.worker_status = "waiting_user"
            self.worker_message = f"Chờ người dùng trả lời cho {task_id}"
            self.pending_interaction = {
                "task_id": task_id,
                "title": title,
                "questions": list(questions),
            }
            self._interaction_answers = {}
            self._interaction_event.clear()

        self.emit_log("warn", f"[{task_id}] Cần bạn trả lời {len(questions)} câu hỏi trên giao diện để tiếp tục.")
        while not self._interaction_event.wait(timeout=0.3):
            if self.stop_event.is_set():
                with self._lock:
                    self.pending_interaction = None
                    self.worker_status = prev_status
                    self.worker_message = prev_msg
                return None

        with self._lock:
            answers = dict(self._interaction_answers)
            self.pending_interaction = None
            self.worker_status = prev_status
            self.worker_message = prev_msg
        self.emit_log("info", f"[{task_id}] Đã nhận {len(answers)} câu trả lời, đang gửi vào Antigravity...")
        return answers

    def submit_interaction_answers(self, answers: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.pending_interaction is None:
                raise ValueError("Hiện không có câu hỏi tương tác nào đang chờ trả lời.")
            self._interaction_answers = {str(k): str(v) for k, v in answers.items()}
            self._interaction_event.set()
        return self.get_state_payload()

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        with self._lock:
            backlog = list(self._sse_backlog)
            self._subscribers.append((loop, q))
        return backlog, q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = [(lp, sub_q) for lp, sub_q in self._subscribers if sub_q is not q]

    def _get_db(self, cfg: EngineConfig, *, create_if_missing: bool = False) -> StateDB | None:
        repo_dir = cfg.target_repo_path
        if not repo_dir.is_dir():
            return None
        if not create_if_missing and not cfg.state_db_path.is_file():
            return None
        return StateDB(cfg.state_db_path)

    def get_state_payload(self) -> dict[str, Any]:
        cfg = self.load_config()
        repo_dir = cfg.target_repo_path
        repo_exists = repo_dir.is_dir()
        is_git_repo = repo_exists and (repo_dir / ".git").exists()

        db = self._get_db(cfg, create_if_missing=False)
        tasks_out: list[dict[str, Any]] = []
        runs_out: list[dict[str, Any]] = []
        summary: dict[str, Any] = {
            "counts": {s: 0 for s in ("PENDING", "RUNNING", "VERIFYING", "DONE", "BLOCKED")},
            "total_tasks": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "verdicts": {},
        }
        plan_approved = False

        if db is not None:
            plan_approved = db.get_meta("plan_approved", "0") == "1"
            summary = db.get_sprint_summary()
            for row in db.list_tasks():
                spec = TaskSpec.model_validate_json(row["spec_json"]).model_dump()
                tasks_out.append(
                    {
                        "task_id": row["task_id"],
                        "title": row["title"],
                        "complexity": row["complexity"],
                        "status": row["status"],
                        "retry_count": row["retry_count"],
                        "prompt_tokens": row["total_prompt_tokens"],
                        "completion_tokens": row["total_completion_tokens"],
                        "cost_usd": row["total_cost_usd"],
                        "last_error": row["last_error"] or "",
                        "commit_sha": row["commit_sha"] or "",
                        "spec": spec,
                    }
                )
            runs_out = list(reversed(db.list_runs()))[:100]

        contracts_md = ""
        handoff_md = ""
        custom_bp_yaml = ""
        audit_report_md = ""
        detected_info: dict[str, Any] = {
            "stack": cfg.project.blueprint or "generic",
            "verify_commands": list(cfg.executor.default_verify_commands),
            "final_verify_commands": list(cfg.sprint.final_verify_commands),
            "preview_command": "",
        }
        if repo_exists:
            try:
                det_stack, det_vc, det_fc, det_prev = detect_existing_stack(repo_dir)
                detected_info = {
                    "stack": det_stack,
                    "verify_commands": det_vc,
                    "final_verify_commands": det_fc,
                    "preview_command": det_prev,
                }
            except Exception:
                pass
            for c_path in (cfg.auto_engine_dir / "contracts.md", repo_dir / "openspec" / "contracts.md"):
                if c_path.is_file():
                    contracts_md = c_path.read_text(encoding="utf-8", errors="replace")
                    break
            h_path = cfg.auto_engine_dir / "handoff.md"
            if h_path.is_file():
                handoff_md = h_path.read_text(encoding="utf-8", errors="replace")
            bp_path = cfg.auto_engine_dir / "blueprint.yaml"
            if bp_path.is_file():
                custom_bp_yaml = bp_path.read_text(encoding="utf-8", errors="replace")
            aud_path = cfg.auto_engine_dir / "ui_repo_audit.md"
            if aud_path.is_file():
                audit_report_md = aud_path.read_text(encoding="utf-8", errors="replace")

        with self._lock:
            worker = {"status": self.worker_status, "message": self.worker_message}
            questions = list(self.clarifying_questions)
            pending_interaction = dict(self.pending_interaction) if self.pending_interaction else None
            logs = list(self._logs)
            traces = list(self._traces)
            preview_cmd = self.wizard_preview_cmd or detected_info.get("preview_command", "")
            wizard = {
                "step": self.wizard_step,
                "idea": self.wizard_idea,
                "stack_hint": self.wizard_stack_hint,
                "parent_dir": self.wizard_parent_dir,
                "interview": self.wizard_interview,
                "spec": self.wizard_spec,
                "preview_cmd": preview_cmd,
                "done_message": self.wizard_done_message,
            }

        engine_insights = {
            "detected": detected_info,
            "contracts_md": contracts_md,
            "handoff_md": handoff_md,
            "custom_blueprint_yaml": custom_bp_yaml,
            "audit_report_md": audit_report_md,
            "pillars": {
                "locator_ast_brace": True,
                "contract_first": bool(contracts_md.strip()),
                "episodic_handoff": bool(handoff_md.strip()),
                "final_gate_auto_repair": True,
                "soft_scope": cfg.guards.scope_mode in ("soft", "flexible"),
                "planning_mode": cfg.sprint.planning_mode,
                "parallel_worktrees": cfg.sprint.max_parallel_tasks,
                "idle_watchdog_sec": cfg.executor.idle_timeout_seconds,
            },
        }

        return {
            "config_path": self.config_path.as_posix(),
            "repo_exists": repo_exists,
            "is_git_repo": is_git_repo,
            "config": cfg.model_dump(),
            "tools": _inspect_tools(cfg),
            "plan_approved": plan_approved,
            "tasks": tasks_out,
            "runs": runs_out,
            "summary": summary,
            "worker": worker,
            "clarifying_questions": questions,
            "pending_interaction": pending_interaction,
            "wizard": wizard,
            "engine_insights": engine_insights,
            "logs": logs,
            "traces": traces,
            "blueprints": [b.model_dump() for b in load_blueprints(repo_dir if repo_exists else None)],
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = EngineConfig.model_validate(payload)
        yaml_text = yaml.safe_dump(cfg.model_dump(), sort_keys=False, allow_unicode=True)
        self.config_path.write_text(yaml_text, encoding="utf-8")
        if (cfg.target_repo_path / ".git").exists():
            GitGuard(cfg.target_repo_path, ignore_artifacts=cfg.guards.ignore_artifacts).ensure_git_exclude()
        self.emit_log("info", f"Đã lưu cấu hình -> {self.config_path.name} (target={cfg.project.target_repo})")
        return self.get_state_payload()

    def _start_worker(self, status_name: str, label: str, fn: Any) -> bool:
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return False
            self.stop_event.clear()
            self.worker_status = status_name
            self.worker_message = label

            def _runner() -> None:
                try:
                    fn()
                except Exception as exc:
                    self.emit_log("error", f"{label} thất bại: {exc}")
                finally:
                    with self._lock:
                        self.worker_status = "idle"
                        self.worker_message = "Sẵn sàng"

            self._worker_thread = threading.Thread(target=_runner, daemon=True)
            self._worker_thread.start()
        return True

    def start_smoke_agy(self) -> bool:
        def _job() -> None:
            cfg = self.load_config()
            self.emit_log("info", "Đang chạy Smoke Test thật tới `agy --input-format stream-json`...")
            ok, msg = smoke_test_agy(cfg)
            self.emit_log("success" if ok else "error", msg)

        return self._start_worker("smoking", "Đang kiểm tra agy...", _job)

    def start_plan(
        self,
        *,
        prd_text: str = "",
        prd_path: str = "",
        from_tasks_json: bool = False,
        auto_approve: bool = False,
    ) -> bool:
        def _job() -> None:
            cfg = self.load_config()
            if not (cfg.target_repo_path / ".git").exists():
                self.emit_log("error", f"Thư mục đích không phải Git repo hợp lệ: {cfg.target_repo_path}")
                return

            db = StateDB(cfg.state_db_path)
            GitGuard(
                cfg.target_repo_path,
                ignore_artifacts=cfg.guards.ignore_artifacts,
                forbid_demo_patterns=cfg.guards.forbid_demo_patterns,
                enforce_production_only=cfg.guards.enforce_production_only,
            ).ensure_git_exclude()
            indexer = RepoIndexer(cfg)
            planner = Planner(
                cfg,
                db,
                indexer,
                on_event=self.emit_log,
                stop_event=self.stop_event,
                on_trace=self.emit_trace,
            )

            with self._lock:
                self.clarifying_questions = []

            if from_tasks_json:
                src = Path(prd_path.strip()) if prd_path.strip() else (cfg.auto_engine_dir / "tasks_import.json")
                if not src.exists() and prd_text.strip():
                    cfg.auto_engine_dir.mkdir(parents=True, exist_ok=True)
                    src.write_text(prd_text, encoding="utf-8")
                self.emit_log("info", f"Đang import tasks từ {src}...")
                res = planner.import_external_tasks(src, auto_approve=auto_approve)
            else:
                if prd_text.strip():
                    cfg.auto_engine_dir.mkdir(parents=True, exist_ok=True)
                    target_prd = cfg.auto_engine_dir / "ui_prd.md"
                    target_prd.write_text(prd_text, encoding="utf-8")
                elif prd_path.strip():
                    target_prd = Path(prd_path.strip())
                else:
                    self.emit_log("error", "Vui lòng nhập nội dung PRD hoặc chọn file PRD.")
                    return

                self.emit_log("info", "Đang nén repo skeleton (repomix) & gọi Planner...")
                res = planner.plan_from_prd(target_prd, auto_approve=auto_approve)

            if res.status == "NEEDS_CLARIFICATION":
                with self._lock:
                    self.clarifying_questions = list(res.clarifying_questions)
                self.emit_log(
                    "warn",
                    f"PRD cần làm rõ {len(res.clarifying_questions)} câu hỏi trước khi sinh task.",
                )
            else:
                self.emit_log("success", f"Đã lập kế hoạch xong {len(res.tasks)} tasks vào state.db.")

        return self._start_worker("planning", "Đang lập kế hoạch DAG...", _job)

    def approve_plan(self) -> dict[str, Any]:
        cfg = self.load_config()
        db = self._get_db(cfg, create_if_missing=True)
        if db is None:
            raise ValueError(f"Thư mục target_repo không tồn tại: {cfg.target_repo_path}")
        db.set_meta("plan_approved", "1")
        self.emit_log("success", "Đã phê duyệt kế hoạch (plan_approved = 1).")
        return self.get_state_payload()

    def start_run(self, *, auto_approve: bool = False, resume_mode: bool = False) -> bool:
        def _job() -> None:
            cfg = self.load_config()
            if not (cfg.target_repo_path / ".git").exists():
                self.emit_log("error", f"Thư mục đích không phải Git repo hợp lệ: {cfg.target_repo_path}")
                return
            db = StateDB(cfg.state_db_path)
            guard = GitGuard(
                cfg.target_repo_path,
                ignore_artifacts=cfg.guards.ignore_artifacts,
                forbid_demo_patterns=cfg.guards.forbid_demo_patterns,
                enforce_production_only=cfg.guards.enforce_production_only,
            )
            verifier = Verifier(cfg, guard)
            compiler = PromptCompiler(cfg)
            orch = Orchestrator(
                cfg,
                db,
                guard,
                verifier,
                compiler,
                on_event=self.emit_log,
                stop_event=self.stop_event,
                on_trace=self.emit_trace,
                ask_user=self.request_user_input,
            )
            self.emit_log("info", "Bắt đầu chạy Sprint qua Antigravity...")
            res = orch.run_sprint(auto_approve=auto_approve or resume_mode)
            counts = res["summary"]["counts"]
            halted = res.get("halted_reason")
            gate = "PASS" if res.get("final_verify_passed", True) else "FAIL"
            self.emit_log(
                "warn" if halted or gate == "FAIL" else "success",
                f"Kết thúc Sprint: {counts} | Halted={halted or 'None'} | FinalGate={gate}",
            )

        status = "resuming" if resume_mode else "running"
        label = "Đang khôi phục & chạy..." if resume_mode else "Đang chạy Sprint..."
        return self._start_worker(status, label, _job)

    def request_stop(self) -> None:
        self.stop_event.set()
        self.emit_log("warn", "Đã nhận lệnh Dừng: sẽ dừng an toàn sau bước hiện tại.")

    def rollback_task(self, task_id: str, reason: str = "Manual rollback via Desktop UI") -> dict[str, Any]:
        cfg = self.load_config()
        db = self._get_db(cfg, create_if_missing=False)
        if db is None:
            raise ValueError("Chưa có state.db cho dự án hiện tại.")
        row = db.get_task_row(task_id)
        if not row:
            raise ValueError(f"Không tìm thấy task `{task_id}`.")
        guard = GitGuard(cfg.target_repo_path, ignore_artifacts=cfg.guards.ignore_artifacts)
        if row.get("checkpoint_sha"):
            guard.hard_rollback(str(row["checkpoint_sha"]))
        db.update_task_status(task_id, "BLOCKED", last_error=reason)
        self.emit_log("warn", f"Đã rollback task {task_id} và đánh dấu BLOCKED.")
        return self.get_state_payload()

    def retry_task(self, task_id: str) -> dict[str, Any]:
        cfg = self.load_config()
        db = self._get_db(cfg, create_if_missing=False)
        if db is None:
            raise ValueError("Chưa có state.db cho dự án hiện tại.")
        row = db.get_task_row(task_id)
        if not row:
            raise ValueError(f"Không tìm thấy task `{task_id}`.")
        db.update_task_status(task_id, "PENDING", retry_count=0)
        db.set_meta("consecutive_blocked", "0")
        self.emit_log("info", f"Đã đặt lại task {task_id} về PENDING để chạy lại.")
        return self.get_state_payload()

    def reset_wizard(self) -> dict[str, Any]:
        with self._lock:
            self.wizard_step = 1
            self.wizard_idea = ""
            self.wizard_interview = None
            self.wizard_spec = None
            self.wizard_preview_cmd = ""
            self.wizard_done_message = ""
        return self.get_state_payload()

    def start_wizard_interview(
        self,
        idea: str,
        stack_hint: str = "auto",
        parent_dir: str = "",
        selected_model: str = "",
    ) -> bool:
        def _job() -> None:
            cfg = self.load_config()
            if selected_model.strip():
                for tier_key in cfg.agy.tiers:
                    cfg.agy.tiers[tier_key].model = selected_model.strip()
                self.save_config(cfg.model_dump())

            with self._lock:
                self.wizard_idea = idea
                self.wizard_stack_hint = stack_hint
                if parent_dir.strip():
                    self.wizard_parent_dir = parent_dir.strip()
                self.wizard_interview = None
                self.wizard_spec = None
                self.wizard_done_message = ""

            interviewer = ProductInterviewer(
                cfg,
                on_event=self.emit_log,
                stop_event=self.stop_event,
                on_trace=self.emit_trace,
            )
            out = interviewer.generate_interview(idea, stack_hint=stack_hint)  # type: ignore[arg-type]
            with self._lock:
                self.wizard_interview = out.model_dump()
                self.wizard_step = 2
            self.emit_log("success", f"Đã phân tích xong ý tưởng: {out.product_summary_vi}")

        return self._start_worker("interviewing", "Đang phân tích ý tưởng & tạo câu hỏi...", _job)

    def start_wizard_synthesize(self, idea: str, answers: dict[str, str], stack: str) -> bool:
        def _job() -> None:
            cfg = self.load_config()
            interviewer = ProductInterviewer(
                cfg,
                on_event=self.emit_log,
                stop_event=self.stop_event,
                on_trace=self.emit_trace,
            )
            spec = interviewer.synthesize_spec(idea, answers, stack)
            with self._lock:
                self.wizard_spec = spec.model_dump()
                self.wizard_step = 3
            self.emit_log("success", f"Đã tạo bản thiết kế tính năng: {spec.product_title}")

        return self._start_worker("synthesizing", "Đang tổng hợp tính năng sản phẩm...", _job)

    def _execute_build_pipeline(
        self,
        *,
        parent_dir: str,
        project_name: str,
        stack: str,
        prd_markdown: str,
        install_deps: bool = False,
        acceptance_criteria: list[str] | None = None,
        architecture_contract: str = "",
    ) -> None:
        with self._lock:
            self.wizard_step = 4
            self.wizard_done_message = ""
            if isinstance(self.wizard_spec, dict):
                if acceptance_criteria is not None:
                    self.wizard_spec["acceptance_criteria"] = list(acceptance_criteria)
                if architecture_contract.strip():
                    self.wizard_spec["architecture_contract"] = architecture_contract.strip()

        base = Path(parent_dir.strip() or self.wizard_parent_dir).expanduser()
        clean_name = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "-" for ch in project_name.strip()).strip("-")
        target_dir = base if (not clean_name or base.name == clean_name) else (base / clean_name)
        target_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(self.wizard_spec, dict):
            try:
                spec_obj = ProductSpec.model_validate(self.wizard_spec)
                save_custom_blueprint_if_needed(target_dir, spec_obj, stack=stack)
                persist_project_contracts(target_dir, spec_obj)
            except Exception:
                pass
        elif architecture_contract.strip():
            persist_project_contracts(target_dir, contract_markdown=architecture_contract.strip())

        self.emit_log("info", f"Bước 1/4: Dựng khung dự án 0-token tại {target_dir.as_posix()}...")
        boot = bootstrap_project(target_dir, stack=stack, install_deps=install_deps)  # type: ignore[arg-type]

        # Save openspec/prd.md and commit cleanly before sprint branch creation
        openspec_dir = target_dir / "openspec"
        openspec_dir.mkdir(parents=True, exist_ok=True)
        prd_file = openspec_dir / "prd.md"
        prd_file.write_text(prd_markdown, encoding="utf-8")
        subprocess.run(["git", "add", "openspec/prd.md"], cwd=str(target_dir), capture_output=True, check=False)
        if subprocess.run(["git", "status", "--porcelain"], cwd=str(target_dir), capture_output=True, text=True).stdout.strip():
            subprocess.run(["git", "commit", "-m", "docs: add openspec/prd.md"], cwd=str(target_dir), capture_output=True, check=False)
        ensure_git_repo_ready(target_dir)

        # Update config.yaml cleanly to point at target_dir with auto-detected verify commands
        cfg = self.load_config()
        cfg.project.target_repo = target_dir.resolve().as_posix()
        cfg.project.blueprint = boot.stack
        if boot.verify_commands:
            cfg.executor.default_verify_commands = boot.verify_commands
        if boot.final_verify_commands:
            cfg.sprint.final_verify_commands = boot.final_verify_commands
        bp = get_blueprint(boot.stack, target_dir)
        if bp is not None and bp.runtime_verify_commands:
            cfg.sprint.runtime_verify_commands = list(bp.runtime_verify_commands)
        self.save_config(cfg.model_dump())

        with self._lock:
            self.wizard_preview_cmd = boot.preview_command

        self.emit_log("info", "Bước 2/4: Định vị file 0-token & Lập kế hoạch công việc từ đặc tả...")
        db = StateDB(cfg.state_db_path)
        indexer = RepoIndexer(cfg)
        planner = Planner(
            cfg,
            db,
            indexer,
            on_event=self.emit_log,
            stop_event=self.stop_event,
            on_trace=self.emit_trace,
        )
        plan_res = planner.plan_from_prd(prd_file, auto_approve=True)
        if plan_res.status == "NEEDS_CLARIFICATION":
            self.emit_log("warn", "Planner cần bổ sung thông tin trước khi chạy.")
            return

        self.emit_log("info", f"Bước 3/4: Thực thi tự động {len(plan_res.tasks)} bước qua Antigravity (Hợp đồng Kiến trúc + Tự vá lỗi)...")
        guard = GitGuard(
            cfg.target_repo_path,
            ignore_artifacts=cfg.guards.ignore_artifacts,
            forbid_demo_patterns=cfg.guards.forbid_demo_patterns,
            enforce_production_only=cfg.guards.enforce_production_only,
        )
        verifier = Verifier(cfg, guard)
        compiler = PromptCompiler(cfg)
        orch = Orchestrator(
            cfg,
            db,
            guard,
            verifier,
            compiler,
            on_event=self.emit_log,
            stop_event=self.stop_event,
            on_trace=self.emit_trace,
            ask_user=self.request_user_input,
        )
        res = orch.run_sprint(auto_approve=True, sprint_name="sprint-1")
        msg, is_ok = self._format_sprint_done_message(res)
        with self._lock:
            self.wizard_done_message = msg
        self.emit_log("success" if is_ok else "warn", msg)

    @staticmethod
    def _format_sprint_done_message(res: dict[str, Any]) -> tuple[str, bool]:
        counts = res["summary"]["counts"]
        done_cnt = counts.get("DONE", 0)
        blocked_cnt = counts.get("BLOCKED", 0)
        tot_cnt = res["summary"].get("total_tasks", 0)
        halted = res.get("halted_reason")
        passed_gate = bool(res.get("final_verify_passed", False))
        if done_cnt >= tot_cnt and tot_cnt > 0 and blocked_cnt == 0 and passed_gate and not halted:
            return (
                f"✅ Hoàn tất! Đã xong {done_cnt}/{tot_cnt} phần (Cổng nghiệm thu cuối: PASS).",
                True,
            )
        if blocked_cnt > 0:
            return (
                f"⚠️ Tạm dừng: Đã xong {done_cnt}/{tot_cnt} phần ({blocked_cnt} phần bị gián đoạn/BLOCKED — bấm '🔄 Thử lại phần bị lỗi & Chạy tiếp' bên dưới).",
                False,
            )
        gate_label = "PASS" if passed_gate else "FAIL"
        return (
            f"⚠️ Chưa hoàn tất trọn vẹn: Đã xong {done_cnt}/{tot_cnt} phần (Halted={halted or 'None'}, Cổng cuối: {gate_label}).",
            False,
        )

    def start_wizard_build(
        self,
        *,
        parent_dir: str,
        project_name: str,
        stack: str,
        prd_markdown: str,
        install_deps: bool = False,
        acceptance_criteria: list[str] | None = None,
        architecture_contract: str = "",
    ) -> bool:
        def _job() -> None:
            self._execute_build_pipeline(
                parent_dir=parent_dir,
                project_name=project_name,
                stack=stack,
                prd_markdown=prd_markdown,
                install_deps=install_deps,
                acceptance_criteria=acceptance_criteria,
                architecture_contract=architecture_contract,
            )

        return self._start_worker("building", "Đang tự động xây dựng sản phẩm...", _job)

    def start_wizard_autopilot(
        self,
        *,
        idea: str,
        stack_hint: str = "auto",
        parent_dir: str = "",
        selected_model: str = "",
        install_deps: bool = False,
    ) -> bool:
        """1-Click Express Autopilot for non-tech users: 1 sentence -> Interview defaults -> Spec + AC -> Build & Verify."""
        def _job() -> None:
            cfg = self.load_config()
            if selected_model.strip():
                for tier_key in cfg.agy.tiers:
                    cfg.agy.tiers[tier_key].model = selected_model.strip()
                self.save_config(cfg.model_dump())

            with self._lock:
                self.wizard_idea = idea
                self.wizard_stack_hint = stack_hint
                if parent_dir.strip():
                    self.wizard_parent_dir = parent_dir.strip()
                self.wizard_interview = None
                self.wizard_spec = None
                self.wizard_done_message = ""
                self.wizard_step = 4

            self.emit_log("info", "⚡ [1-Chạm Autopilot] Bước 1: Tự động phân tích ý tưởng & chọn cấu hình tối ưu...")
            interviewer = ProductInterviewer(
                cfg,
                on_event=self.emit_log,
                stop_event=self.stop_event,
                on_trace=self.emit_trace,
            )
            iv = interviewer.generate_interview(idea, stack_hint=stack_hint)  # type: ignore[arg-type]
            auto_answers = {
                q.question: (q.default_answer or (q.options[0] if q.options else "Theo mặc định tốt nhất"))
                for q in iv.questions
            }
            chosen_stack = iv.recommended_stack if stack_hint in ("auto", "") else stack_hint
            with self._lock:
                self.wizard_interview = iv.model_dump()

            self.emit_log("info", f"⚡ [1-Chạm Autopilot] Bước 2: Thiết kế PRD, Hợp đồng Kiến trúc & Tiêu chí Nghiệm thu ({chosen_stack})...")
            spec = interviewer.synthesize_spec(idea, auto_answers, chosen_stack)
            with self._lock:
                self.wizard_spec = spec.model_dump()

            self._execute_build_pipeline(
                parent_dir=parent_dir or self.wizard_parent_dir,
                project_name=iv.suggested_project_name or "my-ai-product",
                stack=chosen_stack,
                prd_markdown=spec.prd_markdown,
                install_deps=install_deps,
                acceptance_criteria=spec.acceptance_criteria,
                architecture_contract=spec.architecture_contract,
            )

        return self._start_worker("autopilot", "Đang chạy 1-Chạm Autopilot từ A-Z...", _job)

    def start_wizard_quick_update(
        self,
        *,
        request_text: str,
        target_dir: str = "",
        selected_model: str = "",
    ) -> bool:
        """1-Click non-tech upgrade/bugfix on the current or chosen project using a single plain-Vietnamese sentence."""
        def _job() -> None:
            cfg = self.load_config()
            if selected_model.strip():
                for tier_key in cfg.agy.tiers:
                    cfg.agy.tiers[tier_key].model = selected_model.strip()
            if target_dir.strip():
                cfg.project.target_repo = Path(target_dir.strip()).expanduser().resolve().as_posix()

            repo_path = cfg.target_repo_path
            if not repo_path.is_dir():
                self.emit_log("error", f"Thư mục dự án không tồn tại: {repo_path}")
                return

            with self._lock:
                self.wizard_step = 4
                self.wizard_done_message = ""

            ensure_git_repo_ready(repo_path)
            det_stack, det_vc, det_fc, det_prev = detect_existing_stack(repo_path)
            cfg.project.blueprint = det_stack
            if det_vc and not cfg.executor.default_verify_commands:
                cfg.executor.default_verify_commands = det_vc
            if det_fc and not cfg.sprint.final_verify_commands:
                cfg.sprint.final_verify_commands = det_fc
            self.save_config(cfg.model_dump())

            with self._lock:
                if det_prev:
                    self.wizard_preview_cmd = det_prev

            cfg.auto_engine_dir.mkdir(parents=True, exist_ok=True)
            update_prd = cfg.auto_engine_dir / "quick_update_prd.md"
            update_prd.write_text(
                f"# Yêu cầu nâng cấp / chỉnh sửa sản phẩm\n\n{request_text.strip()}\n",
                encoding="utf-8",
            )
            self.emit_log("info", f"✨ Đang định vị file 0-token & lập kế hoạch nâng cấp trên {repo_path.name} ({det_stack})...")
            db = StateDB(cfg.state_db_path)
            guard = GitGuard(
                repo_path,
                ignore_artifacts=cfg.guards.ignore_artifacts,
                forbid_demo_patterns=cfg.guards.forbid_demo_patterns,
                enforce_production_only=cfg.guards.enforce_production_only,
            )
            guard.ensure_git_exclude()
            indexer = RepoIndexer(cfg)
            planner = Planner(
                cfg,
                db,
                indexer,
                on_event=self.emit_log,
                stop_event=self.stop_event,
                on_trace=self.emit_trace,
            )
            plan_res = planner.plan_from_prd(update_prd, auto_approve=True)
            if plan_res.status == "NEEDS_CLARIFICATION":
                with self._lock:
                    self.clarifying_questions = list(plan_res.clarifying_questions)
                self.emit_log("warn", "Yêu cầu cần làm rõ thêm trước khi sửa code.")
                return

            verifier = Verifier(cfg, guard)
            compiler = PromptCompiler(cfg)
            orch = Orchestrator(
                cfg,
                db,
                guard,
                verifier,
                compiler,
                on_event=self.emit_log,
                stop_event=self.stop_event,
                on_trace=self.emit_trace,
                ask_user=self.request_user_input,
            )
            res = orch.run_sprint(auto_approve=True, sprint_name="quick-update")
            msg, is_ok = self._format_sprint_done_message(res)
            with self._lock:
                self.wizard_done_message = msg
            self.emit_log("success" if is_ok else "warn", msg)

        return self._start_worker("updating", "Đang tự động nâng cấp sản phẩm...", _job)

    def retry_blocked_and_resume(self) -> bool:
        """Reset all BLOCKED tasks to PENDING and immediately resume the sprint from Step 4 or Dashboard."""
        cfg = self.load_config()
        db = self._get_db(cfg, create_if_missing=False)
        if db is None:
            raise ValueError("Chưa có state.db cho dự án hiện tại.")
        reset_ids: list[str] = []
        for row in db.list_tasks():
            if row["status"] in ("BLOCKED", "RUNNING", "VERIFYING"):
                db.update_task_status(row["task_id"], "PENDING", retry_count=0, last_error="")
                reset_ids.append(row["task_id"])
        db.set_meta("consecutive_blocked", "0")
        db.set_meta("plan_approved", "1")
        with self._lock:
            self.wizard_step = 4
            self.wizard_done_message = ""
        if reset_ids:
            self.emit_log("info", f"🔄 Đã đặt lại {len(reset_ids)} phần ({', '.join(reset_ids)}) về PENDING để chạy tiếp...")
        return self.start_run(auto_approve=True, resume_mode=True)

    def launch_preview_command(self, custom_command: str = "") -> dict[str, str]:
        """Launch the product preview command in a new visible terminal so non-tech users can run it in 1 click."""
        cfg = self.load_config()
        target = cfg.target_repo_path
        if not target.is_dir():
            raise ValueError(f"Thư mục dự án không tồn tại: {target}")
        det_stack, _vc, _fc, det_prev = detect_existing_stack(target)
        cmd_str = (custom_command.strip() or self.wizard_preview_cmd or det_prev).strip()
        if not cmd_str:
            self.open_target_folder()
            return {"launched": "folder", "command": "", "stack": det_stack}

        if os.name == "nt":
            ps_cmd = (
                f"Set-Location -LiteralPath '{target.as_posix()}'; "
                f"Write-Host '=== DANG CHAY SAN PHAM ({det_stack}) ===' -ForegroundColor Green; "
                f"Write-Host 'Lenh: {cmd_str}' -ForegroundColor Cyan; "
                f"{cmd_str}"
            )
            subprocess.Popen(
                ["cmd", "/c", "start", f"Auto-Engine Preview ({target.name})", "powershell", "-NoExit", "-Command", ps_cmd],
                cwd=str(target),
            )
        else:
            subprocess.Popen(cmd_str, shell=True, cwd=str(target))
        self.emit_log("success", f"▶️ Đã khởi chạy sản phẩm tại `{target.name}` với lệnh: `{cmd_str}`")
        return {"launched": "terminal", "command": cmd_str, "stack": det_stack}

    def save_contracts_and_memory(
        self,
        *,
        contracts_md: str | None = None,
        handoff_md: str | None = None,
        custom_blueprint_yaml: str | None = None,
    ) -> dict[str, Any]:
        cfg = self.load_config()
        if not cfg.target_repo_path.is_dir():
            raise ValueError(f"Thư mục target_repo không tồn tại: {cfg.target_repo_path}")
        cfg.auto_engine_dir.mkdir(parents=True, exist_ok=True)
        if contracts_md is not None:
            (cfg.auto_engine_dir / "contracts.md").write_text(contracts_md.strip() + "\n" if contracts_md.strip() else "", encoding="utf-8")
        if handoff_md is not None:
            (cfg.auto_engine_dir / "handoff.md").write_text(handoff_md.strip() + "\n" if handoff_md.strip() else "", encoding="utf-8")
        if custom_blueprint_yaml is not None and custom_blueprint_yaml.strip():
            (cfg.auto_engine_dir / "blueprint.yaml").write_text(custom_blueprint_yaml.strip() + "\n", encoding="utf-8")
        self.emit_log("success", "Đã lưu Hợp đồng Kiến trúc (`contracts.md`), Bộ nhớ (`handoff.md`) & Blueprint.")
        return self.get_state_payload()

    def start_repo_audit(self, max_commits: int = 10) -> bool:
        """Run 0-token Historical Commit Replay Auditor on target_repo from the UI."""
        def _job() -> None:
            cfg = self.load_config()
            repo = cfg.target_repo_path
            if not (repo / ".git").exists():
                self.emit_log("error", f"Thư mục không phải Git repo: {repo}")
                return
            self.emit_log("info", f"🔍 Đang chạy 0-Token Historical Commit Replay trên {max_commits} commits gần nhất của `{repo.name}`...")
            report = audit_historical_commits(repo, limit=max(1, min(50, max_commits)), config=cfg)
            md = report.to_markdown()
            cfg.auto_engine_dir.mkdir(parents=True, exist_ok=True)
            (cfg.auto_engine_dir / "ui_repo_audit.md").write_text(md, encoding="utf-8")
            self.emit_log(
                "success",
                f"Hoàn tất Audit {report.commits_evaluated} commits: Recall={report.avg_recall:.1%}, "
                f"Neighborhood={report.neighborhood_hit_rate:.1%}, WindowHit={report.window_hit_rate:.1%}, "
                f"AvgTokens={report.avg_prompt_tokens}",
            )

        return self._start_worker("auditing", "Đang kiểm định 0-Token Replay Audit...", _job)

    def open_target_folder(self) -> str:
        cfg = self.load_config()
        target = cfg.target_repo_path
        if not target.is_dir():
            raise ValueError(f"Thư mục không tồn tại: {target}")
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return target.as_posix()

    def get_agy_models(self) -> list[dict[str, str]]:
        cfg = self.load_config()
        return list_agy_models(cfg.agy.binary)

    def open_agy_login_window(self) -> None:
        cfg = self.load_config()
        agy_bin = cfg.agy.binary
        if os.name == "nt":
            ps_script = (
                "Write-Host '=== DANG NHAP ANTIGRAVITY CLI (PRO) ===' -ForegroundColor Cyan; "
                "Write-Host 'Neu dang o tai khoan cu, go: /logout roi dang nhap lai tai khoan Pro.' -ForegroundColor Yellow; "
                f"& '{agy_bin}'"
            )
            subprocess.Popen(
                ["cmd", "/c", "start", "Antigravity CLI Login", "powershell", "-NoExit", "-Command", ps_script]
            )
        else:
            subprocess.Popen([agy_bin])
        self.emit_log("info", "Đã mở cửa sổ đăng nhập Antigravity (`agy`). Sau khi đăng nhập xong hãy bấm Làm mới / Tải lại model.")


def create_ui_app(config_path: Path) -> Starlette:
    """Create the local Starlette web/desktop ASGI application."""
    controller = DesktopAppController(config_path)

    async def index(_request: Request) -> HTMLResponse:
        return HTMLResponse(STATIC_INDEX.read_text(encoding="utf-8"))

    async def api_state(_request: Request) -> JSONResponse:
        return JSONResponse(controller.get_state_payload())

    async def api_save_config(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            state = controller.save_config(body)
            return JSONResponse(state)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api_pick_folder(_request: Request) -> JSONResponse:
        cfg = controller.load_config()
        initial = str(cfg.target_repo_path) if cfg.target_repo_path.is_dir() else None
        chosen = await asyncio.to_thread(_pick_folder_native, initial)
        return JSONResponse({"path": chosen})

    async def api_pick_file(_request: Request) -> JSONResponse:
        cfg = controller.load_config()
        initial = str(cfg.target_repo_path) if cfg.target_repo_path.is_dir() else None
        path_str, content = await asyncio.to_thread(_pick_file_native, initial)
        return JSONResponse({"path": path_str, "content": content})

    async def api_pick_asset(_request: Request) -> JSONResponse:
        cfg = controller.load_config()
        initial = str(cfg.target_repo_path) if cfg.target_repo_path.is_dir() else None
        path_str, name = await asyncio.to_thread(_pick_asset_native, initial)
        return JSONResponse({"path": path_str, "name": name})

    async def api_interaction_answer(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            answers = dict(body.get("answers") or {})
            return JSONResponse(controller.submit_interaction_answers(answers))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api_smoke_agy(_request: Request) -> JSONResponse:
        if not controller.start_smoke_agy():
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_plan(request: Request) -> JSONResponse:
        body = await request.json()
        started = controller.start_plan(
            prd_text=str(body.get("prd_text", "")),
            prd_path=str(body.get("prd_path", "")),
            from_tasks_json=bool(body.get("from_tasks_json", False)),
            auto_approve=bool(body.get("auto_approve", False)),
        )
        if not started:
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_approve(_request: Request) -> JSONResponse:
        try:
            return JSONResponse(controller.approve_plan())
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api_run(request: Request) -> JSONResponse:
        body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
        if not controller.start_run(auto_approve=bool(body.get("auto_approve", False)), resume_mode=False):
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_resume(_request: Request) -> JSONResponse:
        if not controller.start_run(auto_approve=True, resume_mode=True):
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_stop(_request: Request) -> JSONResponse:
        controller.request_stop()
        return JSONResponse({"stopping": True})

    async def api_rollback(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]
        try:
            return JSONResponse(controller.rollback_task(task_id))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api_retry(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]
        try:
            return JSONResponse(controller.retry_task(task_id))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api_wizard_interview(request: Request) -> JSONResponse:
        body = await request.json()
        idea = str(body.get("idea", "")).strip()
        if not idea:
            return JSONResponse({"error": "Vui lòng mô tả ngắn ý tưởng sản phẩm."}, status_code=400)
        ok = controller.start_wizard_interview(
            idea=idea,
            stack_hint=str(body.get("stack_hint", "auto")),
            parent_dir=str(body.get("parent_dir", "")),
            selected_model=str(body.get("selected_model", "")),
        )
        if not ok:
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_wizard_synthesize(request: Request) -> JSONResponse:
        body = await request.json()
        ok = controller.start_wizard_synthesize(
            idea=str(body.get("idea", "")),
            answers=dict(body.get("answers") or {}),
            stack=str(body.get("stack", "web_react")),
        )
        if not ok:
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_wizard_build(request: Request) -> JSONResponse:
        body = await request.json()
        raw_ac = body.get("acceptance_criteria")
        ac_list = [str(x).strip() for x in raw_ac if str(x).strip()] if isinstance(raw_ac, list) else None
        ok = controller.start_wizard_build(
            parent_dir=str(body.get("parent_dir", "")),
            project_name=str(body.get("project_name", "my-ai-app")),
            stack=str(body.get("stack", "web_react")),
            prd_markdown=str(body.get("prd_markdown", "")),
            install_deps=bool(body.get("install_deps", False)),
            acceptance_criteria=ac_list,
            architecture_contract=str(body.get("architecture_contract", "")),
        )
        if not ok:
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_wizard_autopilot(request: Request) -> JSONResponse:
        body = await request.json()
        idea = str(body.get("idea", "")).strip()
        if not idea:
            return JSONResponse({"error": "Vui lòng mô tả ngắn ý tưởng sản phẩm."}, status_code=400)
        ok = controller.start_wizard_autopilot(
            idea=idea,
            stack_hint=str(body.get("stack_hint", "auto")),
            parent_dir=str(body.get("parent_dir", "")),
            selected_model=str(body.get("selected_model", "")),
            install_deps=bool(body.get("install_deps", False)),
        )
        if not ok:
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_wizard_quick_update(request: Request) -> JSONResponse:
        body = await request.json()
        req_txt = str(body.get("request_text", "")).strip()
        if not req_txt:
            return JSONResponse({"error": "Vui lòng nhập nội dung bạn muốn nâng cấp hoặc sửa lỗi."}, status_code=400)
        ok = controller.start_wizard_quick_update(
            request_text=req_txt,
            target_dir=str(body.get("target_dir", "")),
            selected_model=str(body.get("selected_model", "")),
        )
        if not ok:
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_preview_launch(request: Request) -> JSONResponse:
        try:
            body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
            res = controller.launch_preview_command(str(body.get("command", "")))
            return JSONResponse(res)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api_save_contracts(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            state = controller.save_contracts_and_memory(
                contracts_md=body.get("contracts_md"),
                handoff_md=body.get("handoff_md"),
                custom_blueprint_yaml=body.get("custom_blueprint_yaml"),
            )
            return JSONResponse(state)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api_audit_repo(request: Request) -> JSONResponse:
        body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
        commits = int(body.get("max_commits", 10))
        if not controller.start_repo_audit(max_commits=commits):
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_wizard_reset(_request: Request) -> JSONResponse:
        return JSONResponse(controller.reset_wizard())

    async def api_wizard_retry_blocked(_request: Request) -> JSONResponse:
        try:
            if not controller.retry_blocked_and_resume():
                return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
            return JSONResponse({"started": True})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api_open_folder(_request: Request) -> JSONResponse:
        try:
            opened = controller.open_target_folder()
            return JSONResponse({"opened": opened})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api_agy_models(_request: Request) -> JSONResponse:
        models = await asyncio.to_thread(controller.get_agy_models)
        return JSONResponse({"models": models})

    async def api_agy_login(_request: Request) -> JSONResponse:
        controller.open_agy_login_window()
        return JSONResponse({"launched": True})

    async def api_logs_stream(_request: Request) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        backlog, q = controller.subscribe(loop)

        async def _events():
            try:
                for item in backlog[-50:]:
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                while True:
                    try:
                        entry = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                controller.unsubscribe(q)

        return StreamingResponse(_events(), media_type="text/event-stream")

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/state", api_state, methods=["GET"]),
        Route("/api/config", api_save_config, methods=["POST"]),
        Route("/api/pick-folder", api_pick_folder, methods=["POST"]),
        Route("/api/pick-file", api_pick_file, methods=["POST"]),
        Route("/api/pick-asset", api_pick_asset, methods=["POST"]),
        Route("/api/interaction/answer", api_interaction_answer, methods=["POST"]),
        Route("/api/smoke-agy", api_smoke_agy, methods=["POST"]),
        Route("/api/plan", api_plan, methods=["POST"]),
        Route("/api/approve", api_approve, methods=["POST"]),
        Route("/api/run", api_run, methods=["POST"]),
        Route("/api/resume", api_resume, methods=["POST"]),
        Route("/api/stop", api_stop, methods=["POST"]),
        Route("/api/tasks/{task_id}/rollback", api_rollback, methods=["POST"]),
        Route("/api/tasks/{task_id}/retry", api_retry, methods=["POST"]),
        Route("/api/wizard/interview", api_wizard_interview, methods=["POST"]),
        Route("/api/wizard/synthesize", api_wizard_synthesize, methods=["POST"]),
        Route("/api/wizard/build", api_wizard_build, methods=["POST"]),
        Route("/api/wizard/autopilot", api_wizard_autopilot, methods=["POST"]),
        Route("/api/wizard/quick-update", api_wizard_quick_update, methods=["POST"]),
        Route("/api/wizard/retry-blocked", api_wizard_retry_blocked, methods=["POST"]),
        Route("/api/wizard/reset", api_wizard_reset, methods=["POST"]),
        Route("/api/preview/launch", api_preview_launch, methods=["POST"]),
        Route("/api/contracts", api_save_contracts, methods=["POST"]),
        Route("/api/audit-repo", api_audit_repo, methods=["POST"]),
        Route("/api/open-folder", api_open_folder, methods=["POST"]),
        Route("/api/agy/models", api_agy_models, methods=["GET"]),
        Route("/api/agy/login", api_agy_login, methods=["POST"]),
        Route("/api/events", api_logs_stream, methods=["GET"]),
        Route("/api/logs/stream", api_logs_stream, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.controller = controller
    return app
