"""Industry-standard A/B Hidden-Test Evaluation Harness (`agy` baseline vs Auto-Prompt Engine)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Literal

import yaml
from pydantic import BaseModel, Field

from engine.bootstrapper import ensure_git_repo_ready
from engine.bridges.agy_cli_executor import (
    _extract_usage_from_event,
    build_agy_command,
    encode_stream_user_message,
    kill_process_tree,
)
from engine.compiler import PromptCompiler, PromptTooLargeError
from engine.database import StateDB
from engine.git_guard import GitGuard
from engine.indexer import (
    RepoIndexer,
    _is_doc_or_meta_path,
    _is_test_path,
    extract_symbol_windows,
    list_real_files,
    locate_relevant_files,
)
from engine.models import EngineConfig, ProjectConfig, TaskSpec, TokenUsage, normalize_posix_path
from engine.orchestrator import Orchestrator
from engine.planner import Planner
from engine.verifier import Verifier

ArmName = Literal["baseline", "engine"]
ArmRunner = Callable[["EvalCase", Path, EngineConfig], tuple[TokenUsage, float, str]]


def default_eval_cases_dir() -> Path:
    """Resolve built-in `evals/cases` directory cleanly regardless of current working directory."""
    return Path(__file__).resolve().parents[2] / "evals" / "cases"


def _quoted_python() -> str:
    exe = sys.executable
    return f'"{exe}"' if " " in exe else exe


def _format_cmd(cmd: str) -> str:
    return cmd.replace("{python}", _quoted_python())


@contextmanager
def _temp_env(extra_env: dict[str, str] | None) -> Iterator[None]:
    """Temporarily apply extra environment variables around subprocess/engine execution."""
    if not extra_env:
        yield
        return
    saved: dict[str, str | None] = {}
    try:
        for k, v in extra_env.items():
            saved[k] = os.environ.get(k)
            os.environ[k] = v
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


class EvalCase(BaseModel):
    """Single isolated benchmark task (starter_files or real git repo_url+base_commit) with holdout tests."""

    id: str
    title: str
    domain: str = "general"
    blueprint: str = "generic"
    prd_markdown: str
    repo_url: str = ""
    base_commit: str = ""
    setup_commands: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    starter_files: dict[str, str] = Field(default_factory=dict)
    public_verify_commands: list[str] = Field(default_factory=list)
    hidden_files: dict[str, str] = Field(default_factory=dict)
    hidden_patch: str = ""
    hidden_pass_to_pass_commands: list[str] = Field(default_factory=list)
    hidden_fail_to_pass_commands: list[str] = Field(default_factory=list)


class ArmCaseResult(BaseModel):
    """Execution and hidden-test grading outcome for one (case, arm, run_index) execution."""

    case_id: str
    arm: ArmName
    run_index: int = 1
    resolved: bool
    regression_passed: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_sec: float = 0.0
    files_changed: int = 0
    error_summary: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ArmSummary(BaseModel):
    """Aggregated benchmark metrics across all evaluated cases and runs for one arm."""

    arm: ArmName
    total_cases: int = 0
    runs_per_case: int = 1
    resolved_cases: int = 0
    regression_passed_cases: int = 0
    resolve_rate: float = 0.0
    regression_pass_rate: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tokens_per_resolved: float | None = None
    token_stddev: float = 0.0
    avg_duration_sec: float = 0.0


class EvalReport(BaseModel):
    """Comparative A/B evaluation report with delta metrics."""

    generated_at: str
    cases_evaluated: list[str] = Field(default_factory=list)
    results: list[ArmCaseResult] = Field(default_factory=list)
    summaries: dict[str, ArmSummary] = Field(default_factory=dict)
    resolve_rate_delta: float | None = None
    token_savings_pct: float | None = None

    def to_markdown(self) -> str:
        lines = [
            "# Auto-Prompt Engine A/B Hidden-Test Evaluation Report",
            f"- Generated: `{self.generated_at}`",
            f"- Cases ({len(self.cases_evaluated)}): {', '.join(self.cases_evaluated)}",
            "",
            "## Arm Summary",
            "| Arm | Runs/Case | Resolved | Resolve Rate | Regression PASS | Total Tokens | Tokens / Resolved | Token StdDev | Avg Duration |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for arm_key in ("baseline", "engine"):
            s = self.summaries.get(arm_key)
            if s is None:
                continue
            tpr = f"{s.tokens_per_resolved:,.0f}" if s.tokens_per_resolved is not None else "N/A"
            lines.append(
                f"| `{s.arm}` | {s.runs_per_case} | {s.resolved_cases}/{s.total_cases} | {s.resolve_rate * 100:.1f}% | "
                f"{s.regression_pass_rate * 100:.1f}% | {s.total_tokens:,} | {tpr} | {s.token_stddev:,.1f} | {s.avg_duration_sec:.2f}s |"
            )

        if self.resolve_rate_delta is not None or self.token_savings_pct is not None:
            lines.extend(["", "## A/B Comparison (`engine` vs `baseline`)"])
            if self.resolve_rate_delta is not None:
                lines.append(f"- Resolve Rate Delta: `{self.resolve_rate_delta * 100:+.1f} pp`")
            if self.token_savings_pct is not None:
                lines.append(f"- Token Savings: `{self.token_savings_pct:+.1f}%`")

        lines.extend(
            [
                "",
                "## Per-Case Breakdown",
                "| Case | Arm | Run | Resolved | Regression | Prompt Tok | Completion Tok | Time | Changed Files | Notes |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for r in self.results:
            note = r.error_summary.replace("\n", " ").strip()[:120] or "-"
            lines.append(
                f"| `{r.case_id}` | `{r.arm}` | {r.run_index} | {'PASS' if r.resolved else 'FAIL'} | "
                f"{'PASS' if r.regression_passed else 'FAIL'} | {r.prompt_tokens:,} | "
                f"{r.completion_tokens:,} | {r.duration_sec:.2f}s | {r.files_changed} | {note} |"
            )
        return "\n".join(lines) + "\n"


class CommitAuditRow(BaseModel):
    """Zero-token historical replay measurement for one ground-truth git commit."""

    commit_sha: str
    subject: str
    actual_files: list[str] = Field(default_factory=list)
    located_targets: list[str] = Field(default_factory=list)
    located_readonly: list[str] = Field(default_factory=list)
    target_hit: bool = False
    neighborhood_hit: bool = False
    recall: float = 0.0
    precision: float = 0.0
    window_hit: bool | None = None
    prompt_tokens: int = 0
    soft_scope_ok: bool = False


class RepoAuditReport(BaseModel):
    """Aggregated 0-token historical replay audit report across real repository commits."""

    repo_name: str
    commits_evaluated: int = 0
    target_hit_rate: float = 0.0
    neighborhood_hit_rate: float = 0.0
    avg_recall: float = 0.0
    avg_precision: float = 0.0
    avg_targets_count: float = 0.0
    window_hit_rate: float = 1.0
    avg_prompt_tokens: int = 0
    max_prompt_tokens: int = 0
    soft_scope_pass_rate: float = 0.0
    rows: list[CommitAuditRow] = Field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Auto-Prompt Engine 0-Token Historical Replay Audit: `{self.repo_name}`",
            f"- Commits Evaluated: `{self.commits_evaluated}`",
            f"- Target Hit Rate (`actual in targets`): `{self.target_hit_rate * 100:.1f}%`",
            f"- Neighborhood Hit Rate (`actual in targets+readonly`): `{self.neighborhood_hit_rate * 100:.1f}%`",
            f"- Avg Recall / Precision: `{self.avg_recall * 100:.1f}%` / `{self.avg_precision * 100:.1f}%`",
            f"- Avg Located Targets / Commit: `{self.avg_targets_count:.2f}`",
            f"- Window Hit Rate (files > 300 lines): `{self.window_hit_rate * 100:.1f}%`",
            f"- Avg / Max Prompt Tokens: `{self.avg_prompt_tokens:,}` / `{self.max_prompt_tokens:,}`",
            f"- Soft-Scope Pass Rate: `{self.soft_scope_pass_rate * 100:.1f}%`",
            "",
            "## Commit Breakdown",
            "| SHA | Subject | Actual | Located Targets | Recall | Precision | Window | Prompt Tok | Soft-Scope |",
            "|---|---|---:|---:|---:|---:|---|---:|---|",
        ]
        for r in self.rows:
            win_str = "-" if r.window_hit is None else ("HIT" if r.window_hit else "MISS")
            lines.append(
                f"| `{r.commit_sha[:8]}` | {r.subject[:52].replace('|', '/')} | {len(r.actual_files)} | "
                f"{len(r.located_targets)} | {r.recall * 100:.0f}% | {r.precision * 100:.0f}% | "
                f"{win_str} | {r.prompt_tokens:,} | {'PASS' if r.soft_scope_ok else 'FAIL'} |"
            )
        return "\n".join(lines) + "\n"


def audit_historical_commits(
    repo_path: Path,
    *,
    limit: int = 20,
    config: EngineConfig | None = None,
) -> RepoAuditReport:
    """Replay up to `limit` historical non-merge commits (0 LLM tokens) to score Locator, Windows, Compiler & Scope."""
    resolved_repo = repo_path.resolve()
    real_files = list_real_files(resolved_repo)
    cfg = (config.model_copy(deep=True) if config is not None else EngineConfig())
    cfg.project.target_repo = str(resolved_repo)
    compiler = PromptCompiler(cfg)

    log_proc = subprocess.run(
        ["git", "log", "--no-merges", "-n", str(max(10, limit * 5)), "--pretty=format:%H%x09%s%x09%b%x1e"],
        cwd=str(resolved_repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )

    root_manifests = {
        "package.json", "package-lock.json", "pyproject.toml",
        "uv.lock", "tsconfig.json", "requirements.txt",
    }
    rows: list[CommitAuditRow] = []
    for raw_rec in log_proc.stdout.split("\x1e"):
        if len(rows) >= limit:
            break
        rec = raw_rec.strip()
        if not rec:
            continue
        parts = rec.split("\t", 2)
        sha = parts[0].strip()
        subject = parts[1].strip() if len(parts) > 1 else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        commit_msg = f"{subject}\n\n{body}".strip() if body else subject
        if len(commit_msg) < 20:
            continue

        diff_proc = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
            cwd=str(resolved_repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if diff_proc.returncode != 0:
            continue
        actual_code = [
            normalize_posix_path(f)
            for f in diff_proc.stdout.splitlines()
            if f.strip()
            and normalize_posix_path(f) in real_files
            and not _is_test_path(f)
            and not _is_doc_or_meta_path(f)
        ]
        if not (1 <= len(actual_code) <= 6):
            continue

        located = locate_relevant_files(commit_msg, real_files, resolved_repo)
        actual_set = set(actual_code)
        targets_set = set(located.target_files)
        nbr_set = targets_set | set(located.readonly_files)

        target_hit = bool(actual_set & targets_set)
        neighborhood_hit = bool(actual_set & nbr_set)
        recall = len(actual_set & nbr_set) / len(actual_set)
        precision = (len(actual_set & targets_set) / len(targets_set)) if targets_set else 0.0

        win_hits: list[bool] = []
        for f_rel in actual_code:
            full_p = resolved_repo / f_rel
            if not full_p.is_file():
                continue
            line_cnt = len(full_p.read_text(encoding="utf-8", errors="replace").splitlines())
            if line_cnt <= cfg.limits.inline_file_max_lines:
                continue
            show_proc = subprocess.run(
                ["git", "show", "--unified=0", sha, "--", f_rel],
                cwd=str(resolved_repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            touched_lines: set[int] = set()
            for m in re.finditer(r"@@\s*-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s*@@", show_proc.stdout):
                s_ln = int(m.group(1))
                cnt = int(m.group(2)) if m.group(2) is not None else 1
                touched_lines.update(range(s_ln, s_ln + max(1, cnt)))
            if touched_lines:
                win_txt = extract_symbol_windows(
                    full_p,
                    located.identifiers,
                    radius=25,
                    max_total_lines=160,
                )
                covered = {int(n) for n in re.findall(r"^L(\d+):", win_txt, flags=re.MULTILINE)}
                win_hits.append(bool(covered & touched_lines))
        window_hit = any(win_hits) if win_hits else None

        task = TaskSpec(
            task_id=f"AUDIT-{sha[:7]}",
            title=subject[:80] or sha[:8],
            objective=commit_msg,
            allowed_write_files=located.target_files,
            readonly_context_files=located.readonly_files,
            protected_test_files=located.protected_tests[:25],
            strict_scope=False,
        )
        try:
            _, est_tok = compiler.compile(task)
        except PromptTooLargeError:
            est_tok = cfg.limits.max_prompt_tokens_per_call + 1

        allowed_dirs = {
            PurePosixPath(p).parent.as_posix()
            for p in targets_set
            if PurePosixPath(p).parent.as_posix() != "."
        }
        soft_scope_ok = all(
            (f in targets_set or f in root_manifests or PurePosixPath(f).parent.as_posix() in allowed_dirs)
            for f in actual_code
        )

        rows.append(
            CommitAuditRow(
                commit_sha=sha,
                subject=subject,
                actual_files=actual_code,
                located_targets=located.target_files,
                located_readonly=located.readonly_files,
                target_hit=target_hit,
                neighborhood_hit=neighborhood_hit,
                recall=round(recall, 4),
                precision=round(precision, 4),
                window_hit=window_hit,
                prompt_tokens=est_tok,
                soft_scope_ok=soft_scope_ok,
            )
        )

    n = len(rows)
    if n == 0:
        return RepoAuditReport(repo_name=resolved_repo.name)
    win_rows = [r for r in rows if r.window_hit is not None]
    toks = [r.prompt_tokens for r in rows]
    return RepoAuditReport(
        repo_name=resolved_repo.name,
        commits_evaluated=n,
        target_hit_rate=round(sum(1 for r in rows if r.target_hit) / n, 4),
        neighborhood_hit_rate=round(sum(1 for r in rows if r.neighborhood_hit) / n, 4),
        avg_recall=round(sum(r.recall for r in rows) / n, 4),
        avg_precision=round(sum(r.precision for r in rows) / n, 4),
        avg_targets_count=round(sum(len(r.located_targets) for r in rows) / n, 2),
        window_hit_rate=round((sum(1 for r in win_rows if r.window_hit) / len(win_rows)), 4) if win_rows else 1.0,
        avg_prompt_tokens=round(sum(toks) / n),
        max_prompt_tokens=max(toks),
        soft_scope_pass_rate=round(sum(1 for r in rows if r.soft_scope_ok) / n, 4),
        rows=rows,
    )


def load_eval_cases(
    cases_dir: Path | None = None,
    case_ids: list[str] | None = None,
) -> list[EvalCase]:
    """Load YAML hidden-test benchmark cases from `evals/cases`."""
    root = cases_dir or default_eval_cases_dir()
    if not root.is_dir():
        return []
    wanted = {cid.strip() for cid in (case_ids or []) if cid.strip()}
    loaded: list[EvalCase] = []
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        case = EvalCase.model_validate(raw)
        if not wanted or case.id in wanted:
            loaded.append(case)
    return loaded


def setup_isolated_case_repo(case: EvalCase, work_dir: Path) -> Path:
    """Create a fresh Git repository from `repo_url`@`base_commit` or `starter_files` (hidden tests withheld)."""
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)

    if case.repo_url.strip():
        repo_url = case.repo_url.strip()
        slug_base = re.sub(r"[^A-Za-z0-9._-]+", "_", case.id or Path(repo_url).stem)[:40]
        url_hash = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:8]
        cache_root = Path(tempfile.gettempdir()) / "auto_engine_eval_git_cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_dir = cache_root / f"{slug_base}_{url_hash}.git"

        if not cache_dir.is_dir():
            subprocess.run(
                ["git", "clone", "--bare", repo_url, str(cache_dir)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

        if case.base_commit.strip():
            sha = case.base_commit.strip()
            has_obj = subprocess.run(
                ["git", "--git-dir", str(cache_dir), "cat-file", "-e", f"{sha}^{{commit}}"],
                capture_output=True,
                check=False,
            )
            if has_obj.returncode != 0:
                subprocess.run(
                    ["git", "--git-dir", str(cache_dir), "fetch", "--all", "--tags"],
                    capture_output=True,
                    check=False,
                )
                has_obj = subprocess.run(
                    ["git", "--git-dir", str(cache_dir), "cat-file", "-e", f"{sha}^{{commit}}"],
                    capture_output=True,
                    check=False,
                )
                if has_obj.returncode != 0:
                    subprocess.run(
                        ["git", "--git-dir", str(cache_dir), "fetch", "origin", sha],
                        capture_output=True,
                        check=True,
                    )

        work_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", str(cache_dir), str(work_dir)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        checkout_cmd = ["git", "checkout", "-B", "main"]
        if case.base_commit.strip():
            checkout_cmd.append(case.base_commit.strip())
        subprocess.run(checkout_cmd, cwd=str(work_dir), check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "EvalBot"], cwd=str(work_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "eval@example.com"], cwd=str(work_dir), check=True, capture_output=True)

        run_env = (os.environ | case.env) if case.env else None
        for raw_setup in case.setup_commands:
            cmd = _format_cmd(raw_setup.strip())
            if cmd:
                subprocess.run(cmd, shell=True, cwd=str(work_dir), env=run_env, check=True)
        return work_dir

    work_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in case.starter_files.items():
        target = work_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    ensure_git_repo_ready(work_dir, base_branch="main")
    return work_dir


def _count_changed_files(repo_dir: Path, initial_sha: str) -> int:
    changed: set[str] = set()
    diff_proc = subprocess.run(
        ["git", "diff", "--name-only", initial_sha, "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    for line in diff_proc.stdout.splitlines():
        if line.strip() and not line.strip().startswith(".auto-engine"):
            changed.add(line.strip())
    st_proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    for line in st_proc.stdout.splitlines():
        if len(line) > 3:
            p = line[3:].strip()
            if p and not p.startswith(".auto-engine"):
                changed.add(p)
    return len(changed)


def _run_hidden_commands(
    commands: list[str],
    repo_dir: Path,
    timeout_sec: int,
    extra_env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    run_env = (os.environ | extra_env) if extra_env else None
    for raw_cmd in commands:
        cmd = _format_cmd(raw_cmd.strip())
        if not cmd:
            continue
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(repo_dir),
                env=run_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT running hidden check `{cmd}`"
        if proc.returncode != 0:
            snippet = (proc.stdout + "\n" + proc.stderr).strip().splitlines()[-8:]
            return False, f"FAIL `{cmd}`: " + " | ".join(snippet)
    return True, ""


def grade_with_hidden_tests(
    case: EvalCase,
    repo_dir: Path,
    timeout_sec: int = 90,
) -> tuple[bool, bool, str]:
    """Inject holdout hidden_files / hidden_patch AFTER agent completion and run PASS_TO_PASS + FAIL_TO_PASS."""
    for rel, content in case.hidden_files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    if case.hidden_patch.strip():
        patch_text = case.hidden_patch if case.hidden_patch.endswith("\n") else (case.hidden_patch + "\n")
        already_applied = subprocess.run(
            ["git", "apply", "--reverse", "--check", "--whitespace=nowarn", "-"],
            cwd=str(repo_dir),
            input=patch_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if already_applied.returncode != 0:
            patch_paths = sorted(set(re.findall(r"^\+\+\+\s+b/(\S+)", patch_text, flags=re.MULTILINE)))
            if patch_paths:
                rst = subprocess.run(
                    ["git", "checkout", "main", "--", *patch_paths],
                    cwd=str(repo_dir),
                    capture_output=True,
                    check=False,
                )
                if rst.returncode != 0:
                    subprocess.run(
                        ["git", "checkout", "--", *patch_paths],
                        cwd=str(repo_dir),
                        capture_output=True,
                        check=False,
                    )
            apply_proc = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=str(repo_dir),
                input=patch_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if apply_proc.returncode != 0:
                err = (apply_proc.stderr or apply_proc.stdout).strip()
                return False, False, f"git apply hidden_patch failed: {err}"

    reg_ok, reg_err = _run_hidden_commands(
        case.hidden_pass_to_pass_commands,
        repo_dir,
        timeout_sec,
        extra_env=case.env or None,
    )
    f2p_ok, f2p_err = _run_hidden_commands(
        case.hidden_fail_to_pass_commands,
        repo_dir,
        timeout_sec,
        extra_env=case.env or None,
    )

    resolved = reg_ok and f2p_ok
    errs = [e for e in (reg_err, f2p_err) if e]
    return resolved, reg_ok, "; ".join(errs)


def run_baseline_arm(case: EvalCase, repo_dir: Path, config: EngineConfig) -> tuple[TokenUsage, float, str]:
    """Baseline Arm A: call raw `agy` directly with PRD + public verify commands without Engine orchestration."""
    start = time.monotonic()
    tier = config.agy.tiers.get("coder_medium") or config.agy.tiers.get("planner")
    model = tier.model if tier else ""
    effort = tier.effort if tier else "medium"
    cmd = build_agy_command(config, model, effort, input_format="stream-json", output_format="stream-json")

    prompt = (
        f"# TASK: {case.title}\n\n"
        f"{case.prd_markdown.strip()}\n\n"
        f"Public checks available: {[_format_cmd(c) for c in case.public_verify_commands]}\n"
        "Implement the complete working solution directly in this repository."
    )
    run_env = (os.environ | case.env) if case.env else None
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo_dir),
        env=run_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        proc.stdin.write(encode_stream_user_message(prompt))
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass

    latest_usage = TokenUsage()
    timed_out = False
    try:
        stdout_data, stderr_data = proc.communicate(timeout=config.executor.timeout_seconds)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        stdout_data, stderr_data = proc.communicate()
        timed_out = True

    for line in (stdout_data or "").splitlines():
        try:
            ev = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            u = _extract_usage_from_event(ev)
            if u is not None:
                latest_usage = u

    duration = round(time.monotonic() - start, 3)
    if timed_out:
        return latest_usage, duration, f"Baseline `agy` timed out after {config.executor.timeout_seconds}s"
    if proc.returncode != 0:
        err = (stderr_data or "").strip() or f"agy exited with code {proc.returncode}"
        return latest_usage, duration, err
    return latest_usage, duration, ""


def run_engine_arm(case: EvalCase, repo_dir: Path, config: EngineConfig) -> tuple[TokenUsage, float, str]:
    """Treatment Arm B: run full Auto-Prompt Engine (Planner + Compiler + Orchestrator + Guards)."""
    start = time.monotonic()
    cfg = config.model_copy(deep=True)
    cfg.project.target_repo = str(repo_dir.resolve())
    cfg.project.base_branch = "main"
    cfg.project.blueprint = case.blueprint
    formatted_cmds = [_format_cmd(c) for c in case.public_verify_commands]
    cfg.executor.default_verify_commands = formatted_cmds
    cfg.sprint.final_verify_commands = formatted_cmds
    cfg.sprint.require_plan_approval = False

    auto_dir = repo_dir / ".auto-engine"
    auto_dir.mkdir(parents=True, exist_ok=True)
    prd_path = auto_dir / "eval_prd.md"
    cleaned_prd = case.prd_markdown.strip()
    full_prd = cleaned_prd if cleaned_prd.startswith("# ") else f"# {case.title}\n\n{cleaned_prd}\n"
    prd_path.write_text(full_prd, encoding="utf-8")

    with _temp_env(case.env):
        db = StateDB(cfg.state_db_path)
        guard = GitGuard(
            cfg.target_repo_path,
            ignore_artifacts=cfg.guards.ignore_artifacts,
            forbid_demo_patterns=cfg.guards.forbid_demo_patterns,
            enforce_production_only=cfg.guards.enforce_production_only,
        )
        guard.ensure_git_exclude()
        indexer = RepoIndexer(cfg)
        planner = Planner(cfg, db, indexer)
        plan_res = planner.plan_from_prd(prd_path, auto_approve=True)
        if plan_res.status != "READY":
            summary = db.get_sprint_summary()
            usage = TokenUsage(
                prompt_tokens=summary.get("planning_prompt_tokens", 0),
                completion_tokens=summary.get("planning_completion_tokens", 0),
            )
            return usage, round(time.monotonic() - start, 3), f"Planner status={plan_res.status}"

        verifier = Verifier(cfg, guard)
        compiler = PromptCompiler(cfg)
        orch = Orchestrator(cfg, db, guard, verifier, compiler)
        res = orch.run_sprint(auto_approve=True, sprint_name="eval-1")

    summary = db.get_sprint_summary()
    usage = TokenUsage(
        prompt_tokens=summary["prompt_tokens"] + summary.get("planning_prompt_tokens", 0),
        completion_tokens=summary["completion_tokens"] + summary.get("planning_completion_tokens", 0),
        cost_usd=summary["cost_usd"],
    )
    duration = round(time.monotonic() - start, 3)
    err = res.get("halted_reason") or res.get("final_verify_error") or ""
    return usage, duration, str(err)


def _summarize_arm(arm: ArmName, rows: list[ArmCaseResult], runs: int = 1) -> ArmSummary:
    n = len(rows)
    if n == 0:
        return ArmSummary(arm=arm, runs_per_case=runs)
    resolved_cnt = sum(1 for r in rows if r.resolved)
    reg_cnt = sum(1 for r in rows if r.regression_passed)
    p_tok = sum(r.prompt_tokens for r in rows)
    c_tok = sum(r.completion_tokens for r in rows)
    tot_tok = p_tok + c_tok
    tok_series = [r.total_tokens for r in rows]
    stddev = round(statistics.pstdev(tok_series), 2) if len(tok_series) > 1 else 0.0
    avg_dur = round(sum(r.duration_sec for r in rows) / n, 3)
    tpr = round(tot_tok / resolved_cnt, 2) if resolved_cnt > 0 else None
    return ArmSummary(
        arm=arm,
        total_cases=n,
        runs_per_case=runs,
        resolved_cases=resolved_cnt,
        regression_passed_cases=reg_cnt,
        resolve_rate=round(resolved_cnt / n, 4),
        regression_pass_rate=round(reg_cnt / n, 4),
        prompt_tokens=p_tok,
        completion_tokens=c_tok,
        total_tokens=tot_tok,
        tokens_per_resolved=tpr,
        token_stddev=stddev,
        avg_duration_sec=avg_dur,
    )


class Evaluator:
    """Runs isolated A/B hidden-test benchmarks and computes objective effectiveness deltas."""

    def __init__(
        self,
        config: EngineConfig,
        *,
        baseline_runner: ArmRunner | None = None,
        engine_runner: ArmRunner | None = None,
        hidden_test_timeout_sec: int = 90,
    ) -> None:
        self.config = config
        self.baseline_runner = baseline_runner or run_baseline_arm
        self.engine_runner = engine_runner or run_engine_arm
        self.hidden_test_timeout_sec = hidden_test_timeout_sec

    def evaluate_case(
        self,
        case: EvalCase,
        arm: ArmName,
        work_root: Path,
        *,
        run_index: int = 1,
    ) -> ArmCaseResult:
        """Set up fresh sandbox for one (case, arm, run_index), execute agent, then grade with hidden tests."""
        repo_dir = setup_isolated_case_repo(case, work_root / f"{case.id}__{arm}__r{run_index}")
        initial_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        runner = self.baseline_runner if arm == "baseline" else self.engine_runner
        run_err = ""
        usage = TokenUsage()
        duration = 0.0
        try:
            usage, duration, run_err = runner(case, repo_dir, self.config)
        except Exception as exc:
            run_err = str(exc)

        files_changed = _count_changed_files(repo_dir, initial_sha)
        resolved, reg_ok, grade_err = grade_with_hidden_tests(
            case,
            repo_dir,
            timeout_sec=self.hidden_test_timeout_sec,
        )
        combined_err = "; ".join(part for part in (run_err, grade_err) if part)
        return ArmCaseResult(
            case_id=case.id,
            arm=arm,
            run_index=run_index,
            resolved=resolved,
            regression_passed=reg_ok,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            duration_sec=duration,
            files_changed=files_changed,
            error_summary=combined_err,
        )

    def run_suite(
        self,
        cases: list[EvalCase],
        *,
        arms: tuple[ArmName, ...] = ("baseline", "engine"),
        runs: int = 1,
        work_dir: Path | None = None,
    ) -> EvalReport:
        """Run all cases x runs across requested arms and return comparative EvalReport."""
        repeat_runs = max(1, runs)
        results: list[ArmCaseResult] = []

        def _execute_all(base_work: Path) -> None:
            for case in cases:
                for run_idx in range(1, repeat_runs + 1):
                    for arm in arms:
                        results.append(self.evaluate_case(case, arm, base_work, run_index=run_idx))

        if work_dir is None:
            with tempfile.TemporaryDirectory(prefix="auto_engine_eval_") as tmp:
                _execute_all(Path(tmp))
        else:
            work_dir.mkdir(parents=True, exist_ok=True)
            _execute_all(work_dir)

        summaries: dict[str, ArmSummary] = {}
        for arm in arms:
            arm_rows = [r for r in results if r.arm == arm]
            summaries[arm] = _summarize_arm(arm, arm_rows, runs=repeat_runs)

        resolve_delta: float | None = None
        token_savings_pct: float | None = None
        if "baseline" in summaries and "engine" in summaries:
            base_s = summaries["baseline"]
            eng_s = summaries["engine"]
            resolve_delta = round(eng_s.resolve_rate - base_s.resolve_rate, 4)
            if base_s.total_tokens > 0:
                token_savings_pct = round((base_s.total_tokens - eng_s.total_tokens) / base_s.total_tokens * 100.0, 2)

        return EvalReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            cases_evaluated=[c.id for c in cases],
            results=results,
            summaries=summaries,
            resolve_rate_delta=resolve_delta,
            token_savings_pct=token_savings_pct,
        )

    @staticmethod
    def write_report(report: EvalReport, md_path: Path, json_path: Path | None = None) -> None:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(report.to_markdown(), encoding="utf-8")
        if json_path is not None:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
