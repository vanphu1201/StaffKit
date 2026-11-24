"""Zero-token Layer 5 verifier: fail-fast checks, rtk wrapper, log trimmer, and error hashing."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from engine.blueprints import get_blueprint
from engine.git_guard import GitGuard
from engine.models import EngineConfig, TaskSpec, VerifyResult, normalize_posix_path

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PRIORITY_RE = re.compile(r"(Error|FAIL|AssertionError|Exception|Traceback|TS\d{4}|SyntaxError|TypeError)")
_PY_TRACEBACK_FILE_RE = re.compile(r'File "([^"]+\.[A-Za-z0-9]+)", line (\d+)')
FILE_LINE_RE = re.compile(r'([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)(?::|\(|", line )(\d+)')
NOISE_SUBSTRINGS = ("node_modules/", "node_modules\\", "site-packages/", "site-packages\\", "<frozen importlib")

TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b")
HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")


def compute_error_hash(trimmed_log: str, repo_path: Path | None = None) -> str:
    """Compute stable SHA-256 of trimmed error log ignoring volatile timestamps/durations/root paths."""
    normalized = trimmed_log
    if repo_path is not None:
        root_posix = repo_path.resolve().as_posix()
        root_win = str(repo_path.resolve())
        normalized = normalized.replace(root_posix, "<REPO>").replace(root_win, "<REPO>")
    normalized = TIMESTAMP_RE.sub("<TS>", normalized)
    normalized = DURATION_RE.sub("<DUR>", normalized)
    normalized = HEX_ADDR_RE.sub("<ADDR>", normalized)
    normalized = "\n".join(line.strip() for line in normalized.splitlines() if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def extract_error_locations(
    error_log: str,
    repo_path: Path,
    max_locations: int = 3,
) -> list[tuple[str, int]]:
    """Extract up to `max_locations` unique (rel_posix_path, line_no) pairs from tracebacks/compiler logs."""
    if not error_log:
        return []
    cleaned = ANSI_RE.sub("", error_log)
    resolved_root = repo_path.resolve()
    locations: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    for line in cleaned.splitlines():
        if any(noise in line for noise in NOISE_SUBSTRINGS):
            continue
        matches = list(_PY_TRACEBACK_FILE_RE.finditer(line)) + list(FILE_LINE_RE.finditer(line))
        for match in matches:
            raw_file, raw_lineno = match.group(1), match.group(2)
            try:
                lineno = int(raw_lineno)
            except ValueError:
                continue
            cand = Path(raw_file)
            if not cand.is_absolute():
                cand = (resolved_root / normalize_posix_path(raw_file)).resolve()
            else:
                cand = cand.resolve()
            if not cand.is_file():
                continue
            try:
                rel = cand.relative_to(resolved_root).as_posix()
            except ValueError:
                continue
            key = (rel, lineno)
            if key not in seen:
                seen.add(key)
                locations.append(key)
                if len(locations) >= max_locations:
                    return locations
    return locations


def _extract_code_context(repo_path: Path, trimmed_lines: list[str], budget: int) -> list[str]:
    """Extract +/-7 source lines around the first valid file:line error found in repo_path."""
    if budget < 3:
        return []
    for line in trimmed_lines:
        match = FILE_LINE_RE.search(line)
        if not match:
            continue
        raw_file, raw_lineno = match.group(1), match.group(2)
        rel_candidate = normalize_posix_path(raw_file)
        candidate = Path(raw_file) if Path(raw_file).is_absolute() else (repo_path / rel_candidate)
        if not candidate.is_file():
            continue
        try:
            target_line = int(raw_lineno)
        except ValueError:
            continue
        file_lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        if not file_lines or target_line < 1 or target_line > len(file_lines):
            continue

        start = max(1, target_line - 7)
        end = min(len(file_lines), target_line + 7)
        header = f"--- Context {normalize_posix_path(str(candidate))} (L{start}-L{end}) ---"
        snippet = [header]
        for ln in range(start, end + 1):
            marker = ">" if ln == target_line else " "
            snippet.append(f"{marker} L{ln}: {file_lines[ln - 1]}")
        return snippet[:budget]
    return []


def trim_error_log(
    raw_output: str,
    repo_path: Path,
    allowed_files: list[str] | None = None,
    max_lines: int = 30,
) -> tuple[str, str]:
    """Strip ANSI/noise, retain <= max_lines prioritizing diagnostic lines + code context, and hash."""
    cleaned = ANSI_RE.sub("", raw_output or "")
    all_lines = [line.rstrip() for line in cleaned.splitlines() if line.strip()]
    filtered = [line for line in all_lines if not any(noise in line for noise in NOISE_SUBSTRINGS)]
    if not filtered:
        filtered = all_lines

    target_names = [normalize_posix_path(f) for f in (allowed_files or [])]
    target_basenames = [Path(f).name for f in target_names]

    prioritized: list[str] = []
    other: list[str] = []
    for line in filtered:
        is_diag = bool(PRIORITY_RE.search(line))
        mentions_target = any(name in line for name in target_names + target_basenames if name)
        if is_diag or mentions_target:
            prioritized.append(line)
        else:
            other.append(line)

    # Reserve room for up to 12 lines of +/-7 source context when available while keeping total <= max_lines.
    diag_budget = max(1, max_lines - 12) if max_lines > 16 else max_lines
    selected = prioritized[:diag_budget]
    if len(selected) < diag_budget:
        needed = diag_budget - len(selected)
        selected.extend(other[-needed:])

    remaining = max_lines - len(selected)
    context_lines = _extract_code_context(repo_path, selected + prioritized, remaining)
    final_lines = (selected + context_lines)[:max_lines]

    trimmed_text = "\n".join(final_lines)
    return trimmed_text, compute_error_hash(trimmed_text, repo_path=repo_path)


class Verifier:
    """Runs fail-fast deterministic checks and sequential verification commands."""

    def __init__(self, config: EngineConfig, git_guard: GitGuard) -> None:
        self.config = config
        self.git_guard = git_guard

    def _should_use_rtk(self, command: str) -> bool:
        if not self.config.tools.use_rtk or shutil.which("rtk") is None:
            return False
        stripped = command.strip()
        if stripped.startswith("rtk "):
            return False
        return not any(stripped.startswith(excl) for excl in self.config.tools.rtk_exclude_commands)

    def _wrap_command(self, command: str) -> str:
        if self._should_use_rtk(command):
            return f"rtk {command}"
        return command

    def run_commands(
        self,
        commands: list[str],
        allowed_files: list[str] | None = None,
        timeout_per_cmd: int | None = None,
    ) -> VerifyResult:
        """Execute single commands sequentially (fail-fast, never chaining with &&)."""
        if not commands:
            return VerifyResult(passed=True, verdict="PASS")

        timeout_sec = timeout_per_cmd or min(120, self.config.executor.timeout_seconds)
        for raw_cmd in commands:
            cmd = raw_cmd.strip()
            if not cmd:
                continue
            wrapped = self._wrap_command(cmd)
            try:
                proc = subprocess.run(
                    wrapped,
                    shell=True,
                    cwd=str(self.git_guard.repo_path),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_sec,
                )
            except subprocess.TimeoutExpired as exc:
                combined = f"[TIMEOUT] Command exceeded {timeout_sec}s: {cmd}\n{exc.stdout or ''}\n{exc.stderr or ''}"
                trimmed, err_hash = trim_error_log(
                    combined,
                    self.git_guard.repo_path,
                    allowed_files=allowed_files,
                    max_lines=self.config.limits.max_error_log_lines,
                )
                return VerifyResult(
                    passed=False,
                    verdict="TIMEOUT",
                    trimmed_error_log=trimmed,
                    error_hash=err_hash,
                )

            if proc.returncode != 0:
                combined = f"[FAIL exit={proc.returncode}] {cmd}\n{proc.stdout}\n{proc.stderr}"
                trimmed, err_hash = trim_error_log(
                    combined,
                    self.git_guard.repo_path,
                    allowed_files=allowed_files,
                    max_lines=self.config.limits.max_error_log_lines,
                )
                return VerifyResult(
                    passed=False,
                    verdict="VERIFY_FAIL",
                    trimmed_error_log=trimmed,
                    error_hash=err_hash,
                )

        return VerifyResult(passed=True, verdict="PASS")

    def verify(self, task: TaskSpec) -> VerifyResult:
        """Run Layer 5 checks in strict fail-fast order: Empty Diff -> Scope -> Anti-Gaming -> Commands."""
        if not self.git_guard.has_changes():
            msg = f"EMPTY_DIFF: Task {task.task_id} made zero file changes."
            return VerifyResult(
                passed=False,
                verdict="EMPTY_DIFF",
                trimmed_error_log=msg,
                error_hash=compute_error_hash(msg),
            )

        scope_ok, violations = self.git_guard.check_scope(
            task.allowed_write_files,
            task.create_new_files,
            task.protected_test_files,
            strict_scope=task.strict_scope,
            scope_mode=self.config.guards.scope_mode,
        )
        if not scope_ok:
            msg = "SCOPE_VIOLATION: Modified or created forbidden files:\n" + "\n".join(
                f"- {v}" for v in violations
            )
            return VerifyResult(
                passed=False,
                verdict="SCOPE_VIOLATION",
                trimmed_error_log=msg,
                error_hash=compute_error_hash(msg),
                violating_files=violations,
            )

        bp = get_blueprint(self.config.project.blueprint, self.git_guard.repo_path)
        demo_patterns = list(self.config.guards.forbid_demo_patterns)
        if bp is not None and bp.forbid_patterns:
            demo_patterns.extend(bp.forbid_patterns)

        sanity_ok, gaming_hits = self.git_guard.check_diff_sanity(
            task.create_new_files,
            self.config.guards.forbid_diff_patterns,
            forbid_assertion_deletion=self.config.guards.forbid_test_assertion_deletion,
            forbid_demo_patterns=demo_patterns,
            enforce_production_only=self.config.guards.enforce_production_only,
        )
        if not sanity_ok:
            lines = ["TEST_GAMING: Detected forbidden suppression/skip, demo/mock placeholder, or assertion deletion:"]
            lines.extend(f"- {hit}" for hit in gaming_hits[: max(1, self.config.limits.max_error_log_lines - 1)])
            msg = "\n".join(lines)
            return VerifyResult(
                passed=False,
                verdict="TEST_GAMING",
                trimmed_error_log=msg,
                error_hash=compute_error_hash(msg),
            )

        commands = task.verify_commands or self.config.executor.default_verify_commands
        return self.run_commands(
            commands,
            allowed_files=task.allowed_write_files + task.create_new_files,
        )


def run_web_runtime_smoke(repo_path: Path, *, timeout_sec: float = 16.0) -> tuple[bool, str]:
    """0-token lightweight runtime smoke check for Web/Node apps: verify server boots & responds HTTP < 500."""
    import json
    import time
    import urllib.error
    import urllib.request

    pkg_path = repo_path / "package.json"
    if not pkg_path.is_file():
        return True, "Skipped runtime smoke (no package.json)."
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return True, "Skipped runtime smoke (unreadable package.json)."
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    if not isinstance(scripts, dict) or not str(scripts.get("start", "")).strip():
        return True, "Skipped runtime smoke (no `start` script)."

    # Detect likely port (default 3001 / 3000 / 8000)
    candidate_ports = [3001, 3000, 8000, 5173]
    start_cmd = str(scripts["start"]).strip()
    m_port = re.search(r"(?:PORT=|--port\s+|:)(\d{4,5})", start_cmd)
    if m_port:
        p_num = int(m_port.group(1))
        if p_num not in candidate_ports:
            candidate_ports.insert(0, p_num)

    def _probe_port(port: int) -> tuple[bool, str]:
        url = f"http://127.0.0.1:{port}/"
        try:
            with urllib.request.urlopen(url, timeout=2.5) as resp:
                body = resp.read(2048).decode("utf-8", errors="replace")
                if resp.status >= 500:
                    return False, f"GET {url} returned HTTP {resp.status}"
                if not body.strip():
                    return False, f"GET {url} returned empty body"
                return True, f"GET {url} -> HTTP {resp.status} ({len(body)} bytes)"
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return True, f"GET {url} -> HTTP {exc.code}"
            return False, f"GET {url} -> HTTP {exc.code}"
        except Exception:
            return False, ""

    # If already running locally, probe directly without spawning a duplicate process
    for p in candidate_ports[:3]:
        ok_live, detail_live = _probe_port(p)
        if detail_live:
            return ok_live, f"Runtime Smoke (live server): {detail_live}"

    proc = subprocess.Popen(
        "npm start",
        cwd=str(repo_path),
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + timeout_sec
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = (proc.stdout.read() if proc.stdout else "")[:1200]
                return False, f"Runtime Smoke FAIL: `npm start` exited early (code {proc.returncode}):\n{out}"
            for p in candidate_ports[:3]:
                ok_live, detail_live = _probe_port(p)
                if detail_live:
                    return ok_live, f"Runtime Smoke PASS: {detail_live}"
            time.sleep(0.8)
        return True, "Runtime Smoke: server stayed alive without crashing (custom port not auto-detected)."
    finally:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
            try:
                proc.kill()
            except OSError:
                pass
