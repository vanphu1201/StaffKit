"""Layer 0B Bootstrapper: zero-token project scaffolding, stack detection, and auto-Git setup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from engine.blueprints import get_blueprint, load_blueprints
from engine.git_guard import GitGuard
from engine.models import BootstrapResult, StackType


def _quote_exe(exe_path: str) -> str:
    return f'"{exe_path}"' if " " in exe_path else exe_path


def _default_python_cmd() -> str:
    return _quote_exe(sys.executable)


def detect_existing_stack(project_dir: Path) -> tuple[str, list[str], list[str], str]:
    """Infer stack and verification commands from existing project files (0 LLM tokens)."""
    # Match any custom/external YAML blueprint by detect_markers first (including .auto-engine/blueprint.yaml)
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
        return "python_app", [f"{py} -m pytest -q"], [f"{py} -m pytest -v"], f"{py} -m src.app"

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

    # Match any built-in/external YAML blueprint by detect_markers
    for bp in load_blueprints(project_dir):
        if bp.detect_markers and all(
            (project_dir / m.rstrip("/")).exists() for m in bp.detect_markers
        ):
            return (
                bp.id,
                list(bp.verify_commands),
                list(bp.verify_commands),
                bp.preview_command,
            )

    return "generic", [], [], ""


def _run_git(project_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def ensure_git_repo_ready(project_dir: Path, base_branch: str = "main") -> bool:
    """Initialize Git repo and initial commit if missing so non-tech users never touch Git."""
    created_repo = False
    if not (project_dir / ".git").exists():
        _run_git(project_dir, "init", "-b", base_branch)
        created_repo = True

    if not _run_git(project_dir, "config", "user.name", check=False).stdout.strip():
        _run_git(project_dir, "config", "user.name", "Auto-Prompt Engine")
    if not _run_git(project_dir, "config", "user.email", check=False).stdout.strip():
        _run_git(project_dir, "config", "user.email", "engine@local.dev")

    gitignore = project_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            ".venv/\n__pycache__/\n*.py[cod]\nnode_modules/\ndist/\n.auto-engine/\n.pytest_cache/\n.env\n*.db\n*.db-journal\n",
            encoding="utf-8",
        )

    GitGuard(project_dir).ensure_git_exclude()

    has_head = _run_git(project_dir, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
    if not has_head:
        _run_git(project_dir, "add", ".")
        status = _run_git(project_dir, "status", "--porcelain").stdout.strip()
        if status:
            _run_git(project_dir, "commit", "-m", "chore: initial project bootstrap")
        else:
            _run_git(project_dir, "commit", "--allow-empty", "-m", "chore: initial empty commit")
    elif created_repo:
        _run_git(project_dir, "add", ".")
        if _run_git(project_dir, "status", "--porcelain").stdout.strip():
            _run_git(project_dir, "commit", "-m", "chore: initial project bootstrap")

    return created_repo


def _write_fallback_react_vite(project_dir: Path) -> None:
    """Create minimal Vite + React + TS skeleton when offline or npx fails."""
    (project_dir / "src").mkdir(parents=True, exist_ok=True)
    (project_dir / "package.json").write_text(
        json.dumps(
            {
                "name": project_dir.name.lower().replace(" ", "-"),
                "private": True,
                "version": "0.1.0",
                "type": "module",
                "scripts": {"dev": "vite", "build": "tsc -b && vite build", "preview": "vite preview"},
                "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
                "devDependencies": {
                    "@types/react": "^18.3.12",
                    "@types/react-dom": "^18.3.1",
                    "@vitejs/plugin-react": "^4.3.3",
                    "typescript": "^5.6.2",
                    "vite": "^5.4.10",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "tsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "target": "ES2020",
                    "useDefineForClassFields": True,
                    "lib": ["ES2020", "DOM", "DOM.Iterable"],
                    "module": "ESNext",
                    "skipLibCheck": True,
                    "moduleResolution": "bundler",
                    "allowImportingTsExtensions": True,
                    "isolatedModules": True,
                    "noEmit": True,
                    "jsx": "react-jsx",
                    "strict": True,
                },
                "include": ["src"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "index.html").write_text(
        '<!doctype html>\n<html lang="vi">\n<head><meta charset="UTF-8" /><title>App</title></head>\n'
        '<body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body>\n</html>\n',
        encoding="utf-8",
    )
    (project_dir / "src" / "main.tsx").write_text(
        "import { StrictMode } from 'react';\nimport { createRoot } from 'react-dom/client';\n"
        "import App from './App.tsx';\n\n"
        "createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);\n",
        encoding="utf-8",
    )
    (project_dir / "src" / "App.tsx").write_text(
        "export default function App() {\n  return <main><h1>Ready</h1></main>;\n}\n",
        encoding="utf-8",
    )


def _scaffold_web_react(project_dir: Path, *, install_deps: bool = False) -> tuple[list[str], list[str], str]:
    npx_bin = shutil.which("npx.cmd") or shutil.which("npx")
    scaffolded = False
    if npx_bin:
        try:
            proc = subprocess.run(
                [npx_bin, "-y", "create-vite@latest", ".", "--template", "react-ts"],
                cwd=str(project_dir),
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                shell=(os.name == "nt" and npx_bin.lower().endswith(".ps1")),
            )
            scaffolded = proc.returncode == 0 and (project_dir / "package.json").is_file()
        except (subprocess.TimeoutExpired, OSError):
            scaffolded = False

    if not scaffolded:
        _write_fallback_react_vite(project_dir)

    if install_deps:
        npm_bin = shutil.which("npm.cmd") or shutil.which("npm")
        if npm_bin:
            subprocess.run(
                [npm_bin, "install"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )

    cmds = ["npx tsc --noEmit", "npm run build"]
    return cmds, list(cmds), "npm run dev"


def _scaffold_fullstack_web(project_dir: Path, *, install_deps: bool = False) -> tuple[list[str], list[str], str]:
    """Create a production-ready Full-Stack TypeScript + Express + Prisma SQLite scaffold at 0 token cost."""
    (project_dir / "src" / "lib").mkdir(parents=True, exist_ok=True)
    (project_dir / "server").mkdir(parents=True, exist_ok=True)
    (project_dir / "prisma").mkdir(parents=True, exist_ok=True)

    pkg_name = project_dir.name.lower().replace(" ", "-")
    (project_dir / "package.json").write_text(
        json.dumps(
            {
                "name": pkg_name,
                "private": True,
                "version": "0.1.0",
                "type": "module",
                "scripts": {
                    "dev": 'concurrently "vite" "tsx watch server/index.ts"',
                    "dev:client": "vite",
                    "dev:server": "tsx watch server/index.ts",
                    "build": "tsc --noEmit && vite build",
                    "db:push": "prisma db push",
                    "preview": "vite preview",
                },
                "dependencies": {
                    "@prisma/client": "^5.22.0",
                    "bcryptjs": "^2.4.3",
                    "clsx": "^2.1.1",
                    "cors": "^2.8.5",
                    "express": "^4.21.1",
                    "jsonwebtoken": "^9.0.2",
                    "lucide-react": "^0.460.0",
                    "react": "^18.3.1",
                    "react-dom": "^18.3.1",
                    "tailwind-merge": "^2.5.4",
                    "zod": "^3.23.8",
                },
                "devDependencies": {
                    "@types/bcryptjs": "^2.4.6",
                    "@types/cors": "^2.8.17",
                    "@types/express": "^5.0.0",
                    "@types/jsonwebtoken": "^9.0.7",
                    "@types/node": "^22.10.0",
                    "@types/react": "^18.3.12",
                    "@types/react-dom": "^18.3.1",
                    "@vitejs/plugin-react": "^4.3.3",
                    "autoprefixer": "^10.4.20",
                    "concurrently": "^9.1.0",
                    "postcss": "^8.4.47",
                    "prisma": "^5.22.0",
                    "tailwindcss": "^3.4.15",
                    "tsx": "^4.19.2",
                    "typescript": "^5.6.2",
                    "vite": "^5.4.10",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "tsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "target": "ES2022",
                    "useDefineForClassFields": True,
                    "lib": ["ES2022", "DOM", "DOM.Iterable"],
                    "module": "ESNext",
                    "skipLibCheck": True,
                    "moduleResolution": "bundler",
                    "allowImportingTsExtensions": True,
                    "isolatedModules": True,
                    "noEmit": True,
                    "jsx": "react-jsx",
                    "strict": True,
                },
                "include": ["src", "server"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "vite.config.ts").write_text(
        'import { defineConfig } from "vite";\n'
        'import react from "@vitejs/plugin-react";\n\n'
        "export default defineConfig({\n"
        "  plugins: [react()],\n"
        "  server: {\n"
        "    proxy: {\n"
        '      "/api": {\n'
        '        target: "http://localhost:3001",\n'
        "        changeOrigin: true,\n"
        "      },\n"
        "    },\n"
        "  },\n"
        "});\n",
        encoding="utf-8",
    )
    (project_dir / "tailwind.config.js").write_text(
        "/** @type {import('tailwindcss').Config} */\n"
        "export default {\n"
        '  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],\n'
        "  theme: { extend: {} },\n"
        "  plugins: [],\n"
        "};\n",
        encoding="utf-8",
    )
    (project_dir / "postcss.config.js").write_text(
        "export default {\n  plugins: {\n    tailwindcss: {},\n    autoprefixer: {},\n  },\n};\n",
        encoding="utf-8",
    )
    (project_dir / "index.html").write_text(
        '<!doctype html>\n<html lang="vi">\n<head>\n  <meta charset="UTF-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"  <title>{pkg_name}</title>\n</head>\n"
        '<body>\n  <div id="root"></div>\n  <script type="module" src="/src/main.tsx"></script>\n</body>\n</html>\n',
        encoding="utf-8",
    )
    (project_dir / "src" / "index.css").write_text(
        "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n\n"
        "body {\n  margin: 0;\n  min-height: 100vh;\n  background-color: #fafaf9;\n  color: #18181b;\n}\n",
        encoding="utf-8",
    )
    (project_dir / "src" / "lib" / "utils.ts").write_text(
        'import { clsx, type ClassValue } from "clsx";\n'
        'import { twMerge } from "tailwind-merge";\n\n'
        "export function cn(...inputs: ClassValue[]) {\n"
        "  return twMerge(clsx(inputs));\n"
        "}\n",
        encoding="utf-8",
    )
    (project_dir / "src" / "main.tsx").write_text(
        'import { StrictMode } from "react";\n'
        'import { createRoot } from "react-dom/client";\n'
        'import "./index.css";\n'
        'import App from "./App.tsx";\n\n'
        'createRoot(document.getElementById("root")!).render(\n'
        "  <StrictMode>\n    <App />\n  </StrictMode>\n);\n",
        encoding="utf-8",
    )
    (project_dir / "src" / "App.tsx").write_text(
        "export default function App() {\n"
        '  return <main className="min-h-screen bg-stone-50 text-zinc-900 p-8"><h1>Ready</h1></main>;\n'
        "}\n",
        encoding="utf-8",
    )
    (project_dir / "prisma" / "schema.prisma").write_text(
        'generator client {\n  provider = "prisma-client-js"\n}\n\n'
        'datasource db {\n  provider = "sqlite"\n  url      = env("DATABASE_URL")\n}\n\n'
        "model User {\n"
        "  id           String   @id @default(cuid())\n"
        "  email        String   @unique\n"
        "  name         String\n"
        "  passwordHash String\n"
        '  role         String   @default("USER")\n'
        "  createdAt    DateTime @default(now())\n"
        "  updatedAt    DateTime @updatedAt\n"
        "}\n",
        encoding="utf-8",
    )
    (project_dir / "server" / "index.ts").write_text(
        'import cors from "cors";\n'
        'import express from "express";\n\n'
        "const app = express();\n"
        "const port = Number(process.env.PORT || 3001);\n\n"
        "app.use(cors());\n"
        "app.use(express.json());\n\n"
        'app.get("/api/health", (_req, res) => {\n'
        "  res.json({ ok: true, timestamp: new Date().toISOString() });\n"
        "});\n\n"
        "app.listen(port, () => {\n"
        "  console.log(`API server running on http://localhost:${port}`);\n"
        "});\n",
        encoding="utf-8",
    )
    (project_dir / ".env.example").write_text(
        'DATABASE_URL="file:./dev.db"\nJWT_SECRET="change-me-in-production"\nPORT=3001\n',
        encoding="utf-8",
    )
    (project_dir / ".env").write_text(
        'DATABASE_URL="file:./dev.db"\nJWT_SECRET="local-dev-secret-change-in-production"\nPORT=3001\n',
        encoding="utf-8",
    )

    if install_deps:
        npm_bin = shutil.which("npm.cmd") or shutil.which("npm")
        if npm_bin:
            subprocess.run(
                [npm_bin, "install"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
            npx_bin = shutil.which("npx.cmd") or shutil.which("npx")
            if npx_bin:
                subprocess.run(
                    [npx_bin, "prisma", "generate"],
                    cwd=str(project_dir),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )

    cmds = ["npx tsc --noEmit", "npm run build"]
    return cmds, list(cmds), "npm run dev"


def _scaffold_python_app(project_dir: Path) -> tuple[list[str], list[str], str]:
    (project_dir / "src").mkdir(parents=True, exist_ok=True)
    (project_dir / "tests").mkdir(parents=True, exist_ok=True)
    pkg_name = project_dir.name.lower().replace(" ", "-").replace("_", "-")
    (project_dir / "pyproject.toml").write_text(
        f'[project]\nname = "{pkg_name}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n',
        encoding="utf-8",
    )
    (project_dir / "src" / "__init__.py").write_text("", encoding="utf-8")
    (project_dir / "src" / "app.py").write_text(
        '"""Application entrypoint."""\n\n\ndef main() -> str:\n    return "ready"\n\n\nif __name__ == "__main__":\n    print(main())\n',
        encoding="utf-8",
    )
    (project_dir / "tests" / "test_smoke.py").write_text(
        'from app import main\n\n\ndef test_smoke() -> None:\n    assert main() == "ready"\n',
        encoding="utf-8",
    )
    py = _default_python_cmd()
    return [f"{py} -m pytest -q"], [f"{py} -m pytest -v"], f"{py} -m src.app"


def _scaffold_generic(project_dir: Path, blueprint_id: str) -> tuple[list[str], list[str], str]:
    """Initialize an open, tech-agnostic workspace letting Antigravity pick any language/framework."""
    bp = get_blueprint(blueprint_id, project_dir)
    if bp is not None and bp.scaffold_commands:
        for raw_cmd in bp.scaffold_commands:
            subprocess.run(raw_cmd, cwd=str(project_dir), shell=True, check=False, capture_output=True)
    if bp is not None:
        return list(bp.verify_commands), list(bp.verify_commands), bp.preview_command
    return [], [], ""


def bootstrap_project(
    project_dir: Path,
    stack: StackType = "auto",
    *,
    install_deps: bool = False,
    base_branch: str = "main",
) -> BootstrapResult:
    """Detect existing stack or scaffold a new project at 0 token cost, ensuring Git is ready."""
    target = project_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)

    has_existing_code = (
        any(
            (target / marker).is_file()
            for marker in (
                "package.json",
                "pyproject.toml",
                "requirements.txt",
                "Cargo.toml",
                "go.mod",
                "pubspec.yaml",
                "pom.xml",
                "build.gradle",
                "build.gradle.kts",
                "CMakeLists.txt",
            )
        )
        or (target / ".git").exists()
    )

    if has_existing_code:
        detected_stack, cmds, final_cmds, preview = detect_existing_stack(target)
        is_new = ensure_git_repo_ready(target, base_branch=base_branch)
        return BootstrapResult(
            project_dir=target.as_posix(),
            stack=detected_stack,
            verify_commands=cmds,
            final_verify_commands=final_cmds,
            preview_command=preview,
            is_new_repo=is_new,
        )

    bp = get_blueprint(stack, target)
    template = bp.fallback_template if bp is not None else stack
    if stack in ("auto", "existing"):
        chosen = "generic"
        cmds, final_cmds, preview = _scaffold_generic(target, "generic")
    elif template == "fullstack_web" or stack == "fullstack_web":
        chosen = stack
        cmds, final_cmds, preview = _scaffold_fullstack_web(target, install_deps=install_deps)
    elif template == "web_react" or stack == "web_react":
        chosen = stack
        cmds, final_cmds, preview = _scaffold_web_react(target, install_deps=install_deps)
    elif template == "python_app" or stack == "python_app":
        chosen = stack
        cmds, final_cmds, preview = _scaffold_python_app(target)
    else:
        chosen = stack or "generic"
        cmds, final_cmds, preview = _scaffold_generic(target, chosen)

    is_new = ensure_git_repo_ready(target, base_branch=base_branch)
    return BootstrapResult(
        project_dir=target.as_posix(),
        stack=chosen,
        verify_commands=cmds,
        final_verify_commands=final_cmds,
        preview_command=preview,
        is_new_repo=is_new,
    )


def generate_nontech_delivery_guide(project_dir: Path, *, write_files: bool = True) -> dict[str, Any]:
    """Generate 0-token plain-Vietnamese usage guide (`HUONG_DAN_SU_DUNG.md`) and 1-click `CHAY_SAN_PHAM.bat`."""
    target = project_dir.resolve()
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
                extra_notes.append(f"Chế độ chạy hàng loạt (CLI Batch): `npm run batch -- --help`")
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
        first_line = next((ln.lstrip("# ").strip() for ln in prd_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()), "")
        if first_line:
            prd_title = first_line

    steps_vi: list[str] = []
    if run_cmd:
        steps_vi.append(f"Cách 1 (Dễ nhất — 1 chạm): Bấm nút '▶️ Chạy thử sản phẩm' ngay trên ứng dụng Auto-Prompt Engine, HOẶC mở thư mục dự án và nhấp đúp file `CHAY_SAN_PHAM.bat`.")
    if web_url:
        steps_vi.append(f"Cách 2 (Mở giao diện trên trình duyệt): Sau khi cửa sổ đen khởi chạy hiện lên 2–3 giây, mở trình duyệt truy cập địa chỉ {web_url}.")
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
            open_browser_line = f'start "" "{web_url}"\r\n' if web_url else ""
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
