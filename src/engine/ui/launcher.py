"""Standalone local desktop window launcher (Chrome/Edge --app mode + Uvicorn server)."""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from engine.ui.server import create_ui_app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _find_app_browser() -> str | None:
    """Locate Chrome or Edge executable capable of running `--app=` desktop windows."""
    candidates: list[Path] = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(env_key)
        if not base:
            continue
        root = Path(base)
        candidates.extend(
            [
                root / "Google" / "Chrome" / "Application" / "chrome.exe",
                root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def launch_desktop_app(config_path: Path, port: int = 0, no_window: bool = False) -> None:
    """Start local Uvicorn server on 127.0.0.1 and open a dedicated desktop app window."""
    chosen_port = port if port > 0 else _find_free_port()
    asgi_app = create_ui_app(config_path)

    uv_config = uvicorn.Config(
        asgi_app,
        host="127.0.0.1",
        port=chosen_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Wait briefly until socket accepts connections
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", chosen_port)) == 0:
                break
        time.sleep(0.08)

    url = f"http://127.0.0.1:{chosen_port}"
    print(f"Auto-Prompt Engine Desktop UI running at {url}")

    browser_proc: subprocess.Popen[bytes] | None = None
    if not no_window:
        browser_exe = _find_app_browser()
        if browser_exe:
            profile_dir = config_path.resolve().parent / ".ui-profile"
            profile_dir.mkdir(parents=True, exist_ok=True)
            browser_proc = subprocess.Popen(
                [
                    browser_exe,
                    f"--app={url}",
                    "--window-size=1360,880",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]
            )
        else:
            webbrowser.open(url)

    try:
        if browser_proc is not None:
            browser_proc.wait()
        else:
            while server_thread.is_alive():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        server_thread.join(timeout=3.0)
