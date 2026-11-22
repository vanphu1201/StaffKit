"""Reusable 0-token HTTP runtime health/JSON smoke checker (stdlib only)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _kill_proc_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    try:
        proc.kill()
        proc.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify HTTP endpoint readiness and optional JSON response key.")
    parser.add_argument("--url", required=True, help="HTTP URL to poll (e.g. http://127.0.0.1:3001/api/health)")
    parser.add_argument("--start-cmd", default="", help="Optional shell command to start server before polling")
    parser.add_argument("--cwd", default=".", help="Working directory when --start-cmd is used")
    parser.add_argument("--timeout", type=float, default=15.0, help="Max wait seconds")
    parser.add_argument("--expect-json-key", default="", help="Optional top-level JSON key expected in response")
    args = parser.parse_args(argv)

    proc: subprocess.Popen[str] | None = None
    if args.start_cmd.strip():
        proc = subprocess.Popen(
            args.start_cmd.strip(),
            shell=True,
            cwd=args.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    deadline = time.monotonic() + max(0.5, args.timeout)
    last_err = ""
    try:
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                err_out = (proc.stderr.read() if proc.stderr else "").strip()
                print(f"Server exited prematurely (code {proc.returncode}): {err_out}", file=sys.stderr)
                return 1
            try:
                with urllib.request.urlopen(args.url, timeout=2.0) as resp:
                    status = getattr(resp, "status", 200)
                    body = resp.read().decode("utf-8", errors="replace")
                    if 200 <= status < 300:
                        if args.expect_json_key:
                            data = json.loads(body)
                            if not isinstance(data, dict) or args.expect_json_key not in data:
                                last_err = f"Missing JSON key `{args.expect_json_key}` in response: {body[:200]}"
                                time.sleep(0.25)
                                continue
                        print(f"HTTP_SMOKE_OK status={status} url={args.url}")
                        return 0
                    last_err = f"Unexpected HTTP status {status}"
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
                last_err = str(exc)
            time.sleep(0.25)

        print(f"HTTP_SMOKE_FAIL url={args.url} timeout={args.timeout}s last_error={last_err}", file=sys.stderr)
        return 1
    finally:
        if proc is not None:
            _kill_proc_tree(proc)


if __name__ == "__main__":
    sys.exit(main())
