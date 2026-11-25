"""Zero-token stack detection, entrypoint reconciliation, and Non-Tech Delivery Pack generator."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from engine.blueprints import load_blueprints

_SCRIPT_FILE_RE = re.compile(r"\b([A-Za-z0-9_./-]+\.(?:ts|tsx|js|mjs|cjs))\b")


def _quote_exe(exe_path: str) -> str:
    return f'"{exe_path}"' if " " in exe_path else exe_path


def _default_python_cmd() -> str:
    return _quote_exe(sys.executable)


def detect_existing_stack(project_dir: Path) -> tuple[str, list[str], list[str], str]:
    """Infer stack and verification commands from existing project files (0 LLM tokens)."""
    custom_bp_path = project_dir / ".auto-engine" / "blueprint.yaml"
    if custom_bp_path.is_file():
        for bp in load_blueprints(project_dir):
            if bp.id != "generic" and (bp.verify_commands or bp.preview_command):
                if not bp.detect_markers or all((project_dir / m.rstrip("/")).exists() for m in bp.detect_markers):
                    return (
                        bp.id,
                        list(bp.verify_commands),
                        list(bp.verify_commands),
                        bp.preview_command,
                    )

    pkg_json = project_dir / "package.json"
    if pkg_json.is_file():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            pkg = {}
        scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
        cmds: list[str] = []
        if (project_dir / "tsconfig.json").is_file():
            cmds.append("npx tsc --noEmit")
        elif "typecheck" in scripts:
            cmds.append("npm run typecheck")
        if "build" in scripts:
            cmds.append("npm run build")
        if "test" in scripts and "no test specified" not in str(scripts["test"]):
            cmds.append("npm test")
        preview = "npm run dev" if "dev" in scripts else ("npm start" if "start" in scripts else "")
        has_backend = (project_dir / "server").is_dir() or (project_dir / "prisma").is_dir()
        stack = "fullstack_web" if has_backend else "web_react"
        return stack, cmds, list(cmds), preview

    if (project_dir / "pyproject.toml").is_file() or (project_dir / "requirements.txt").is_file():
        venv_win = project_dir / ".venv" / "Scripts" / "python.exe"
        venv_nix = project_dir / ".venv" / "bin" / "python"
        if venv_win.is_file():
            py = _quote_exe(str(venv_win))
        elif venv_nix.is_file():
            py = _quote_exe(str(venv_nix))
        else:
            py = _default_python_cmd()
        preview = f"{py} -m src.app" if (project_dir / "src" / "app.py").is_file() else ""
        return "python_app", [f"{py} -m pytest -q"], [f"{py} -m pytest -v"], preview

    if (project_dir / "Cargo.toml").is_file():
        return "rust_app", ["cargo check"], ["cargo test"], "cargo run"
    if (project_dir / "go.mod").is_file():
        return "go_app", ["go build ./..."], ["go test ./..."], "go run ."
    if (project_dir / "pubspec.yaml").is_file():
        return "flutter_app", ["dart analyze"], ["flutter test"], "flutter run"
    if (project_dir / "pom.xml").is_file():
        return "java_maven", ["mvn test-compile"], ["mvn test"], ""
    if (project_dir / "build.gradle").is_file() or (project_dir / "build.gradle.kts").is_file():
        return "gradle_app", ["gradle classes"], ["gradle test"], ""
    if (project_dir / "CMakeLists.txt").is_file():
        return "cpp_cmake", ["cmake -B build"], ["cmake --build build"], ""

    for bp in load_blueprints(project_dir):
        if bp.detect_markers and all((project_dir / m.rstrip("/")).exists() for m in bp.detect_markers):
            return (
                bp.id,
                list(bp.verify_commands),
                list(bp.verify_commands),
                bp.preview_command,
            )

    return "generic", [], [], ""


def reconcile_project_entrypoints(project_dir: Path) -> list[str]:
    """Repair broken `package.json` script entrypoints & Vite config paths (0 LLM tokens)."""
    target = project_dir.resolve()
    pkg_path = target / "package.json"
    if not pkg_path.is_file():
        return []
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    scripts = pkg.get("scripts")
    if not isinstance(scripts, dict):
        return []

    fixes: list[str] = []
    has_sub_vite = (target / "web" / "vite.config.ts").is_file() and not (target / "vite.config.ts").is_file()

    for key, raw_cmd in list(scripts.items()):
        if not isinstance(raw_cmd, str):
            continue
        cmd = raw_cmd
        for m in _SCRIPT_FILE_RE.finditer(raw_cmd):
            rel = m.group(1)
            if (target / rel).is_file():
                continue
            p = Path(rel)
            candidates = [
                p.parent / "app.ts",
                p.parent / "server.ts",
                p.parent / "main.ts",
                p.parent / "src" / p.name,
                p.parent / "src" / "app.ts",
                p.parent / "src" / "index.ts",
            ]
            for cand in candidates:
                cand_posix = cand.as_posix()
                if (target / cand_posix).is_file():
                    cmd = cmd.replace(rel, cand_posix)
                    fixes.append(f"scripts.{key}: `{rel}` -> `{cand_posix}`")
                    break

        if has_sub_vite and "vite" in cmd and "--config" not in cmd:
            if "vite build" in cmd:
                cmd = cmd.replace("vite build", "vite build --config web/vite.config.ts")
                fixes.append(f"scripts.{key}: added `--config web/vite.config.ts`")
            elif cmd.strip() == "vite":
                cmd = "vite --config web/vite.config.ts"
                fixes.append(f"scripts.{key}: added `--config web/vite.config.ts`")

        scripts[key] = cmd

    if "dev" in scripts and "start" not in scripts and isinstance(scripts["dev"], str):
        start_cmd = scripts["dev"].replace("tsx watch ", "tsx ").replace("nodemon ", "node ")
        scripts["start"] = start_cmd
        fixes.append(f"scripts.start: added `{start_cmd}`")

    if fixes:
        pkg["scripts"] = scripts
        pkg_path.write_text(json.dumps(pkg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return fixes


def generate_nontech_delivery_guide(project_dir: Path, *, write_files: bool = True) -> dict[str, Any]:
    """Generate 0-token plain-Vietnamese usage guide (`HUONG_DAN_SU_DUNG.md`) and 1-click `CHAY_SAN_PHAM.bat`."""
    target = project_dir.resolve()
    if write_files and target.is_dir():
        reconcile_project_entrypoints(target)
    stack, _vc, _fc, det_preview = detect_existing_stack(target)
    run_cmd = det_preview or ""
    web_url = ""
    extra_notes: list[str] = []

    pkg_path = target / "package.json"
    if pkg_path.is_file():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
            if "start" in scripts:
                run_cmd = "npm start"
            elif "dev" in scripts:
                run_cmd = "npm run dev"
            elif "dev:web" in scripts:
                run_cmd = "npm run dev:web"

            if (target / "dist" / "web" / "index.html").is_file() or (target / "server").is_dir():
                web_url = "http://127.0.0.1:3001"
                if "dev:web" in scripts and not (target / "dist" / "web" / "index.html").is_file():
                    web_url = "http://127.0.0.1:5173"
                    extra_notes.append(f"Nếu muốn chạy riêng giao diện dev frontend: `{scripts['dev:web']}` (mở {web_url}).")
            elif (target / "vite.config.ts").is_file() or (target / "web" / "vite.config.ts").is_file():
                web_url = "http://127.0.0.1:5173"
            if "batch" in scripts:
                extra_notes.append("Chế độ chạy hàng loạt (CLI Batch): `npm run batch -- --help`")
            if "test" in scripts:
                extra_notes.append("Kiểm tra sức khỏe toàn bộ hệ thống (Test tự động): `npm test`")
        except (OSError, json.JSONDecodeError):
            pass
    elif (target / "pyproject.toml").is_file() or (target / "requirements.txt").is_file():
        if not run_cmd:
            run_cmd = "python -m src.app" if (target / "src" / "app.py").is_file() else "python main.py"
        extra_notes.append("Chạy bộ kiểm thử tự động: `pytest -q`")
    elif (target / "Cargo.toml").is_file():
        run_cmd = run_cmd or "cargo run"
    elif (target / "go.mod").is_file():
        run_cmd = run_cmd or "go run ."

    prd_title = target.name
    prd_path = target / "openspec" / "prd.md"
    if prd_path.is_file():
        first_line = next(
            (ln.lstrip("# ").strip() for ln in prd_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()),
            "",
        )
        if first_line:
            prd_title = first_line

    steps_vi: list[str] = []
    if run_cmd:
        steps_vi.append("Cách 1 (Dễ nhất — 1 chạm): Mở thư mục dự án và nhấp đúp file `CHAY_SAN_PHAM.bat`.")
    if web_url:
        steps_vi.append(f"Cách 2 (Mở giao diện trên trình duyệt): Sau khi cửa sổ khởi chạy hiện lên 2–3 giây, truy cập {web_url}.")
    if run_cmd:
        steps_vi.append(f"Cách 3 (Chạy bằng lệnh thủ công): Mở Terminal trong thư mục `{target.as_posix()}` và gõ `{run_cmd}`.")
    steps_vi.extend(extra_notes)

    guide_md_lines = [
        f"# Hướng dẫn chạy & sử dụng — {prd_title}",
        "",
        f"- **Thư mục dự án:** `{target.as_posix()}`",
        f"- **Công nghệ nhận diện:** `{stack}`",
    ]
    if run_cmd:
        guide_md_lines.append(f"- **Lệnh khởi chạy chính:** `{run_cmd}`")
    if web_url:
        guide_md_lines.append(f"- **Địa chỉ giao diện Web:** {web_url}")
    guide_md_lines.extend(["", "## Các bước chạy sản phẩm (Dành cho người Non-Tech)"])
    for idx, st in enumerate(steps_vi, start=1):
        guide_md_lines.append(f"{idx}. {st}")
    guide_md = "\n".join(guide_md_lines) + "\n"

    bat_path = target / "CHAY_SAN_PHAM.bat"
    guide_path = target / "HUONG_DAN_SU_DUNG.md"
    if write_files and target.is_dir() and run_cmd:
        try:
            guide_path.write_text(guide_md, encoding="utf-8")
            bat_content = (
                "@echo off\r\n"
                "chcp 65001 >nul\r\n"
                f'cd /d "{target}"\r\n'
                f"echo === DANG KHOI CHAY SAN PHAM: {prd_title} ===\r\n"
                f"echo Lenh: {run_cmd}\r\n"
                + (f"echo Dang mo trinh duyet tai {web_url} sau 3 giay...\r\n" if web_url else "")
                + (f'start "" /b cmd /c "timeout /t 3 /nobreak >nul & start "" "{web_url}""\r\n' if web_url else "")
                + f"{run_cmd}\r\n"
                + "pause\r\n"
            )
            bat_path.write_text(bat_content, encoding="utf-8")
        except OSError:
            pass

    return {
        "title": prd_title,
        "stack": stack,
        "run_command": run_cmd,
        "web_url": web_url,
        "steps_vi": steps_vi,
        "bat_path": bat_path.as_posix() if bat_path.is_file() else "",
        "guide_path": guide_path.as_posix() if guide_path.is_file() else "",
        "guide_markdown": guide_md,
    }
