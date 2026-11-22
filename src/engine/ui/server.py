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

from engine.bootstrapper import bootstrap_project, ensure_git_repo_ready
from engine.bridges.agy_cli_executor import list_agy_models, smoke_test_agy
from engine.compiler import PromptCompiler
from engine.database import StateDB
from engine.git_guard import GitGuard
from engine.indexer import RepoIndexer
from engine.interviewer import ProductInterviewer
from engine.models import EngineConfig, TaskSpec
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
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, str]]]] = []

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
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        with self._lock:
            self._logs.append(entry)
            subs = list(self._subscribers)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, entry)
            except RuntimeError:
                pass

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> tuple[list[dict[str, str]], asyncio.Queue[dict[str, str]]]:
        q: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=500)
        with self._lock:
            backlog = list(self._logs)
            self._subscribers.append((loop, q))
        return backlog, q

    def unsubscribe(self, q: asyncio.Queue[dict[str, str]]) -> None:
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

        with self._lock:
            worker = {"status": self.worker_status, "message": self.worker_message}
            questions = list(self.clarifying_questions)
            logs = list(self._logs)
            wizard = {
                "step": self.wizard_step,
                "idea": self.wizard_idea,
                "stack_hint": self.wizard_stack_hint,
                "parent_dir": self.wizard_parent_dir,
                "interview": self.wizard_interview,
                "spec": self.wizard_spec,
                "preview_cmd": self.wizard_preview_cmd,
                "done_message": self.wizard_done_message,
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
            "wizard": wizard,
            "logs": logs,
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
            GitGuard(cfg.target_repo_path, ignore_artifacts=cfg.guards.ignore_artifacts).ensure_git_exclude()
            indexer = RepoIndexer(cfg)
            planner = Planner(cfg, db, indexer, on_event=self.emit_log, stop_event=self.stop_event)

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
            guard = GitGuard(cfg.target_repo_path, ignore_artifacts=cfg.guards.ignore_artifacts)
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

            interviewer = ProductInterviewer(cfg, on_event=self.emit_log, stop_event=self.stop_event)
            out = interviewer.generate_interview(idea, stack_hint=stack_hint)  # type: ignore[arg-type]
            with self._lock:
                self.wizard_interview = out.model_dump()
                self.wizard_step = 2
            self.emit_log("success", f"Đã phân tích xong ý tưởng: {out.product_summary_vi}")

        return self._start_worker("interviewing", "Đang phân tích ý tưởng & tạo câu hỏi...", _job)

    def start_wizard_synthesize(self, idea: str, answers: dict[str, str], stack: str) -> bool:
        def _job() -> None:
            cfg = self.load_config()
            interviewer = ProductInterviewer(cfg, on_event=self.emit_log, stop_event=self.stop_event)
            spec = interviewer.synthesize_spec(idea, answers, stack)
            with self._lock:
                self.wizard_spec = spec.model_dump()
                self.wizard_step = 3
            self.emit_log("success", f"Đã tạo bản thiết kế tính năng: {spec.product_title}")

        return self._start_worker("synthesizing", "Đang tổng hợp tính năng sản phẩm...", _job)

    def start_wizard_build(
        self,
        *,
        parent_dir: str,
        project_name: str,
        stack: str,
        prd_markdown: str,
        install_deps: bool = False,
    ) -> bool:
        def _job() -> None:
            with self._lock:
                self.wizard_step = 4
                self.wizard_done_message = ""

            base = Path(parent_dir.strip() or self.wizard_parent_dir).expanduser()
            clean_name = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "-" for ch in project_name.strip()).strip("-")
            target_dir = base if (not clean_name or base.name == clean_name) else (base / clean_name)

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
            if boot.verify_commands:
                cfg.executor.default_verify_commands = boot.verify_commands
            if boot.final_verify_commands:
                cfg.sprint.final_verify_commands = boot.final_verify_commands
            self.save_config(cfg.model_dump())

            with self._lock:
                self.wizard_preview_cmd = boot.preview_command

            self.emit_log("info", "Bước 2/4: Lập kế hoạch công việc DAG từ đặc tả...")
            db = StateDB(cfg.state_db_path)
            indexer = RepoIndexer(cfg)
            planner = Planner(cfg, db, indexer, on_event=self.emit_log, stop_event=self.stop_event)
            plan_res = planner.plan_from_prd(prd_file, auto_approve=True)
            if plan_res.status == "NEEDS_CLARIFICATION":
                self.emit_log("warn", "Planner cần bổ sung thông tin trước khi chạy.")
                return

            self.emit_log("info", f"Bước 3/4: Thực thi tự động {len(plan_res.tasks)} bước qua Antigravity...")
            guard = GitGuard(cfg.target_repo_path, ignore_artifacts=cfg.guards.ignore_artifacts)
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
            )
            res = orch.run_sprint(auto_approve=True, sprint_name="sprint-1")
            counts = res["summary"]["counts"]
            gate = "PASS" if res.get("final_verify_passed", True) else "FAIL"
            msg = f"Hoàn tất! Đã xong {counts.get('DONE', 0)}/{res['summary']['total_tasks']} phần (Kiểm thử cuối: {gate})."
            with self._lock:
                self.wizard_done_message = msg
            self.emit_log("success" if gate == "PASS" and not res.get("halted_reason") else "warn", msg)

        return self._start_worker("building", "Đang tự động xây dựng sản phẩm...", _job)

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
        ok = controller.start_wizard_build(
            parent_dir=str(body.get("parent_dir", "")),
            project_name=str(body.get("project_name", "my-ai-app")),
            stack=str(body.get("stack", "web_react")),
            prd_markdown=str(body.get("prd_markdown", "")),
            install_deps=bool(body.get("install_deps", False)),
        )
        if not ok:
            return JSONResponse({"error": "Đang có tác vụ nền khác chạy."}, status_code=409)
        return JSONResponse({"started": True})

    async def api_wizard_reset(_request: Request) -> JSONResponse:
        return JSONResponse(controller.reset_wizard())

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
        Route("/api/wizard/reset", api_wizard_reset, methods=["POST"]),
        Route("/api/open-folder", api_open_folder, methods=["POST"]),
        Route("/api/agy/models", api_agy_models, methods=["GET"]),
        Route("/api/agy/login", api_agy_login, methods=["POST"]),
        Route("/api/logs/stream", api_logs_stream, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.controller = controller
    return app
