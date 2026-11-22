# AUTO-PROMPT ENGINE — Technical Specification & Implementation Guide for AI Agents

> **HƯỚNG DẪN DÀNH CHO AI THỰC THI (AGENT DIRECTIVE):**
> Tài liệu này là bản thiết kế kỹ thuật (Blueprint) để xây dựng hệ thống **Auto-Prompt Engine** — bộ máy điều phối lập trình tự động theo mô hình **Planner–Compiler–Executor–Verifier**, tối ưu hóa chuyên sâu cho **Antigravity (`agy` CLI / Python SDK / IDE)** nhằm tạo sản phẩm chất lượng cao nhất với mức tiêu thụ token thấp nhất và chống ảo giác / lặp vô tận.
>
> **4 Nguyên tắc bất di bất dịch khi triển khai dự án này:**
> 1. **Tận dụng công cụ chuẩn & hệ sinh thái Antigravity (Don't Reinvent the Wheel):** Dùng trực tiếp `agy` CLI / `google-antigravity` SDK làm Executor chính, kết hợp `repomix`, `serena`, `context7`, `rtk`, `fastmcp` (và tùy chọn `litellm`, `aider`/`opencode` nếu cần BYOK/local model). Không tự viết lại AST parser, LSP server hay log compressor.
> 2. **Zero-Waste Token Policy:** Mọi công việc mang tính xác định (quét file, nén repo map, kiểm tra phạm vi sửa file, chống lách luật test, chạy build/lint/test, cắt log lỗi, rollback Git) **bắt buộc chạy bằng code thuần (0 token)**. Mỗi task thực thi chạy trong phiên sạch (**Stateless**: 1 Task = 1 Session), **không nhồi toàn bộ `repo_map` vào từng task** và **bật/tắt MCP theo nhu cầu (Dynamic MCP Gating)** để không lãng phí 3.000–5.000 token tool-schema ở các task đơn giản.
> 3. **Zero-Hallucination & Anti-Gaming Guardrails:** Không bao giờ tin tưởng tuyệt đối đầu ra của LLM. Mọi yêu cầu mơ hồ phải qua **PRD Ambiguity Gate**; mọi kế hoạch phải qua Pydantic schema + DAG check + kiểm tra đường dẫn file thật + **Plan Approval Gate**; mọi code đầu ra phải qua `EMPTY_DIFF` check, **Scope Guard** (`git diff`), **Anti-Test-Gaming Guard** (chặn `@ts-ignore`, `.skip()`, `# type: ignore`...) và bộ test tự động.
> 4. **Anti-Loop & Multi-Layer Circuit Breaker:** Ngăn triệt để tình trạng AI sửa quẩn quanh không lối thoát bằng 3 tầng ngắt mạch: **Stagnation Detector** (`EMPTY_DIFF` / `IDENTICAL_ERROR` qua SHA-256 log lỗi), **Per-Task Token & Retry Cap** (tối đa 2 retry kèm nâng tier/effort), và **Global Circuit Breaker** (dừng toàn bộ sprint ngay nếu quá 3 task liên tiếp bị `BLOCKED`).

---

## 1. Tổng quan Kiến trúc Hệ thống (5-Layer Zero-Waste Architecture)

```text
[PRD / Yêu cầu người dùng (Markdown)]
                  │
                  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ TẦNG 1: CONTEXT & LAZY MCP LAYER (0 Token + Dynamic Gating)                │
│ • repomix --compress : Sinh `.auto-engine/repo_map.xml` cho Planner (Tầng2)│
│                        & tra cứu chữ ký XML 0-token cho Compiler (Tầng 3)  │
│ • git ls-files       : Nguồn sự thật kiểm tra đường dẫn file               │
│ • Dynamic MCP Gating : Chỉ bật `serena` / `context7` cho task có cờ yêu cầu│
│                        + `run.lock` & `try...finally` khôi phục state cũ   │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ TẦNG 2: SPEC, AMBIGUITY GATE & PLANNER (Gọi LLM 1 LẦN + Human Gate)        │
│ • Ambiguity Check : Nếu PRD mơ hồ -> xuất <= 5 câu hỏi làm rõ (không đoán) │
│ • Planner (`agy -p --json-schema` / LiteLLM): Xuất DAG Atomic Tasks JSON   │
│   kèm `complexity`, `protected_test_files`, `verify_commands: list[str]`   │
│ • Deterministic Validation: Pydantic + DAG Acyclic + `virtual_files(T)`    │
│   (gộp file có sẵn + `create_new_files` từ các task tổ tiên trên DAG)      │
│ • Plan Approval Gate: Xuất `plan.json` để duyệt trước khi đốt token code   │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ TẦNG 3: SURGICAL PROMPT COMPILER (Code thuần - 0 Token)                    │
│ • Lấy 1 task READY từ `state.db` (toàn bộ `depends_on` đã DONE)            │
│ • Local Neighborhood Context: Nạp target files <= 300 dòng; file > 300 dòng│
│   & `readonly_context_files` tra cứu trực tiếp từ XML `repo_map.xml` (0 tk)│
│ • Cấm tuyệt đối Executor tự chạy lệnh test/build/lint bên trong phiên      │
│ • Retry (`attempt > 0`): Tránh gửi lặp diff nếu file đã inline; gửi kèm   │
│   log <= 30 dòng + ±7 dòng code (hoặc tóm tắt vi phạm trước khi rollback)  │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ TẦNG 4: ANTIGRAVITY-FIRST EXECUTION BRIDGE (Stateless: 1 Task = 1 Session) │
│ • Mode A1 (Primary): `agy -p --output-format stream-json --mode            │
│   accept-edits --effort <low|medium|high>` (prompt qua stdin + cwd chuẩn)  │
│   + Real-time Stream Watchdog: kill ngay nếu vượt token/tool_calls/chạy test│
│ • Mode A2 (Deep Control): Antigravity Python SDK (`google-antigravity`)    │
│   giám sát & chặn `response.tool_calls` ngoài scope ngay trước khi ghi đĩa │
│ • Mode B (IDE/App): FastMCP Server (`fastmcp`) nối Antigravity GUI (4 tool)│
│ • Mode C (Optional): `aider` / `opencode` + `litellm` (chỉ khi BYOK/Local) │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ TẦNG 5: ZERO-TOKEN GATEKEEPER, ANTI-GAMING & MULTI-LAYER CIRCUIT BREAKER   │
│ 1. Empty Diff Check  : AI không sửa dòng nào (lọc `ignore_artifacts`)      │
│ 2. Scope Guard       : `git diff` ngoài scope hoặc sửa `protected_test`    │
│ 3. Anti-Test-Gaming  : Quét cả `git diff`, file mới (untracked) & hành vi  │
│                        xóa assertion (`expect`/`assert`) -> Rollback ngay  │
│ 4. Verifier (`rtk`)  : Chạy tuần tự `verify_commands: list[str]` (bọc `rtk`│
│                        từng lệnh đơn, không dùng `&&`) -> trim log <=30d   │
│ 5. Stagnation & Loop : Trùng `error_hash` hoặc `EMPTY_DIFF` -> nhảy tier   │
│                        hoặc dừng ngay; 3 task BLOCKED liên tiếp -> HALT    │
│ 6. Final Sprint Gate : Khi DAG xong, chạy `final_verify_commands` toàn repo│
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Danh sách Công cụ & Repo Tích hợp (External Dependencies)

AI thực thi cấu hình và gọi các công cụ bên dưới thay vì tự code lại chức năng tương đương:

| Thành phần | Công cụ / Repo / Package | Vai trò & Mức ưu tiên | Cách tích hợp |
| :--- | :--- | :--- | :--- |
| **Primary Executor (CLI)** | **Antigravity CLI (`agy`)** | **Chính (Mode A1):** Chạy headless stateless từng task (`agy -p`), hỗ trợ `--model`, `--effort`, `--json-schema`, `--output-format json` | Gọi subprocess + truyền prompt qua `stdin` trong `bridges/agy_cli_executor.py` |
| **Primary Executor (SDK)** | **`google-antigravity`** (`pip install google-antigravity`) | **Nâng cao (Mode A2):** Điều khiển agent qua Python async, chặn `tool_calls` vượt scope theo thời gian thực | Triển khai trong `bridges/agy_sdk_executor.py` |
| **Antigravity IDE Bridge** | `PrefectHQ/fastmcp` (`fastmcp`) | **Tương tác GUI (Mode B):** MCP Server tối giản 4 tool cho Antigravity IDE / App kéo task | Triển khai trong `bridges/mcp_server.py` |
| **Repo Compression** | `yamadashy/repomix` (`npx repomix`) | Nén toàn bộ repo thành Skeleton Map bằng Tree-sitter (`--compress`) **chỉ dùng cho Planner** | Chạy qua subprocess trong `core/indexer.py` (cache theo git hash) |
| **Symbol & LSP Edit** | `oraios/serena` (`uvx --from git+https://github.com/oraios/serena`) | Đọc/sửa thân hàm chuẩn xác cho file lớn > 300 dòng | **Lazy/Gated MCP:** chỉ bật khi `task.needs_lsp_mcp == true` |
| **Versioned Docs** | `upstash/context7` (`npx -y @upstash/context7-mcp`) | Tra cứu docs đúng phiên bản thư viện để chống bịa API | **Lazy/Gated MCP:** chỉ bật khi `task.needs_docs_mcp == true` |
| **Log Compressor** | `rtk-ai/rtk` (Binary `rtk`) | Proxy Rust nén 60–90% output terminal (`pytest`, `vitest`, `tsc`, `git`) | Bọc lệnh verify trong `core/verifier.py` (tự động fallback nếu chưa cài) |
| **Spec/Task Standard** | `Fission-AI/OpenSpec` / `eyaltoledano/claude-task-master` | Chuẩn hóa đầu vào từ PRD sang task list (tùy chọn import) | Bộ adapter đọc `tasks.json` / `openspec/` trong `core/planner.py` |
| **Fallback Gateway & CLI** | `BerriAI/litellm`, `Aider-AI/aider`, `anomalyco/opencode` | **Tùy chọn (Mode C):** Chỉ cần khi chạy model ngoài (BYOK / self-host local LLM) không qua `agy` | Gọi qua `bridges/cli_executor.py` và cấu hình `litellm_config.yaml` |

---

## 3. Cấu trúc Thư mục Dự án (Project Layout)

Xây dựng hệ thống bằng **Python 3.11+** (quản lý bằng `uv`), hỗ trợ đầy đủ **Windows PowerShell & Linux/macOS** (chuẩn hóa đường dẫn POSIX, UTF-8, truyền prompt qua `stdin` tránh giới hạn độ dài dòng lệnh Windows) và quản lý mã nguồn cho **mọi ngôn ngữ** của repo đích (TS/JS, Python, Java, C++, Go...).

```text
auto-prompt-engine/
├── pyproject.toml                 # Dependencies: pydantic, pyyaml, httpx, fastmcp, typer, rich (optional: google-antigravity)
├── config.example.yaml            # Cấu hình mẫu (agy routing + effort, budgets, anti-gaming patterns, verify cmds, rtk)
├── litellm_config.yaml            # [Tùy chọn - Mode C] Cấu hình LiteLLM Proxy nếu dùng BYOK / local model
├── mcp_stack.json                 # Cấu hình MCP mẫu (serena + context7 + auto-prompt-engine) cho IDE / Gated MCP
├── README.md                      # Hướng dẫn cài đặt, cấu hình Windows/Linux và chạy nhanh
├── src/
│   └── engine/
│       ├── __init__.py
│       ├── cli.py                 # CLI: init, plan, approve, run, resume, status, serve-mcp, report
│       ├── models.py              # Pydantic models (Config, TaskSpec, PlanSpec, RunResult, TokenUsage)
│       ├── database.py            # SQLite WAL manager (state.db: tasks, runs, error_hash, crash recovery)
│       ├── indexer.py             # Tầng 1: `repomix --compress` (cho Planner) + `list_real_files()` chuẩn POSIX
│       ├── planner.py             # Tầng 2: Ambiguity Check + Planner (`agy -p --json-schema` / LiteLLM) + Plan Gate
│       ├── compiler.py            # Tầng 3: Surgical Prompt Compiler (Local Neighborhood + Cache Prefix + Pre-flight Cap)
│       ├── git_guard.py           # Checkpoint, nhánh riêng, Scope Guard, Anti-Test-Gaming Diff Guard, Commit, Rollback
│       ├── verifier.py            # Tầng 5: Fail-Fast Gatekeeper (`EMPTY_DIFF` -> Scope -> Anti-Gaming -> `rtk` -> Log Trimmer & Hash)
│       ├── orchestrator.py        # State Machine, Complexity/Effort Routing, Stagnation & Global Circuit Breaker, Sprint Gate
│       └── bridges/
│           ├── __init__.py
│           ├── agy_cli_executor.py  # [Mode A1 - Primary] Chạy headless `agy -p` stateless + parse JSON output
│           ├── agy_sdk_executor.py  # [Mode A2] Chạy `google-antigravity` SDK + real-time tool_calls scope interceptor
│           ├── mcp_server.py        # [Mode B] FastMCP Server (4 tool tối giản) nối vào Antigravity IDE/App
│           └── cli_executor.py      # [Mode C - Optional] Fallback headless Aider / OpenCode + LiteLLM
└── tests/
    ├── test_database.py
    ├── test_git_guard.py          # Test repo thật: Scope Guard, Protected Tests, Anti-Test-Gaming, Rollback, Windows paths
    ├── test_verifier.py           # Test Empty Diff, error_hash, rtk fallback & Log Trimmer <= 30 dòng
    ├── test_compiler.py           # Test Static Prefix bất biến, Local Neighborhood (<=300 dòng vs >300 dòng), Token Cap
    ├── test_agy_bridge.py         # Test builder lệnh `agy -p`, stdin pipe, parse JSON & SDK interceptor mock
    └── test_orchestrator_mock.py  # Test toàn tuyến (0 token): PASS, Scope/Gaming, Identical Error, Global Breaker, Resume, Sprint Gate
```

---

## 4. Hợp đồng Dữ liệu & Cấu hình (Schemas & Contracts)

### 4.1. Cấu hình hệ thống (`config.example.yaml`)

> **Lưu ý Windows & Cross-platform:** Luôn dùng dấu `/` (forward slash) hoặc chuỗi raw trong YAML; mọi đường dẫn tương đối sẽ được chuẩn hóa về POSIX (`Path.as_posix()`) khi so khớp với `git diff`.

```yaml
project:
  target_repo: "D:/code/target-repo"   # Hỗ trợ cả Windows ("D:/...") và POSIX ("/path/to/...")
  base_branch: "main"
  work_branch_prefix: "auto-engine/"
  state_db: ".auto-engine/state.db"
  shared_rules_file: ""                # Tùy chọn: đường dẫn file quy ước cố định (vd: AGENTS.md) cho sprint lớn

executor:
  mode: "agy_cli"                      # Hợp lệ: "agy_cli" | "agy_sdk" | "mcp_only" | "aider" | "opencode"
  timeout_seconds: 300
  default_verify_commands:             # Mảng lệnh đơn (KHÔNG ghép `&&`) để bọc `rtk` & chạy ổn định trên PowerShell/Bash
    - "npm run typecheck"
    - "npm test"

agy:
  binary: "agy"                        # Đường dẫn binary hoặc tên lệnh trong PATH
  permission_mode: "accept-edits"      # Khớp flag `--mode accept-edits` của `agy`
  disable_slash_commands: true         # Khớp `--disable-slash-commands` (giảm noise trong print mode)
  auto_approve_tools: false            # Chỉ bật `true` (`--dangerously-skip-permissions`) khi chạy trong sandbox/CI
  dynamic_mcp_gating: true             # Dùng `try...finally` khôi phục MCP + `run.lock` (khuyên tắt hoặc dùng `agy_sdk` nếu mở song song nhiều IDE)
  tiers:
    planner:
      model: "gemini-2.5-pro"          # Hoặc để rỗng "" để dùng model mặc định đang cấu hình trong `agy`
      effort: "high"                   # low | medium | high
    coder_low:
      model: "gemini-2.5-flash"
      effort: "low"
    coder_medium:
      model: "gemini-2.5-pro"
      effort: "medium"
    escalation:
      model: "claude-sonnet-4-5"       # Dùng cho task complexity=high hoặc retry cuối / stagnation
      effort: "high"

limits:
  max_retries_per_task: 2              # Tổng tối đa 3 lần chạy (1 lần đầu + 2 retry)
  max_tokens_per_task: 60000           # Ngắt mạch real-time (gồm ~14k system+tool context nền của `agy`)
  max_tool_calls_per_attempt: 12       # Real-time watchdog: kill `agy` ngay nếu gọi quá 12 tool trong 1 lượt
  max_prompt_tokens_per_call: 12000    # Chặn pre-flight nếu Surgical Prompt biên dịch ra quá dài
  inline_file_max_lines: 300           # File > 300 dòng không nhồi nguyên văn, lấy symbol từ repo_map.xml
  max_budget_usd_per_sprint: 2.00      # Trần chi phí cứng (áp dụng khi gateway/executor trả về cost)
  max_files_per_task: 3                # Ép Planner chia nhỏ task (Atomic Task)
  max_error_log_lines: 30              # Số dòng log lỗi tối đa gửi ngược vào AI
  max_consecutive_blocked_tasks: 3     # Global Circuit Breaker: 3 task liên tiếp BLOCKED -> dừng toàn sprint

guards:
  forbid_test_assertion_deletion: true # Chặn AI xóa bớt `expect(`/ `assert` trong file test để ép xanh
  forbid_diff_patterns:                # Chặn AI gian lận (quét cả `git diff` + file tạo mới untracked)
    - "@ts-ignore"
    - "@ts-nocheck"
    - "@ts-expect-error"
    - "eslint-disable"
    - "# type: ignore"
    - "# noqa"
    - "\\.skip\\("
    - "\\.only\\("
    - "pytest\\.mark\\.skip"
    - "unittest\\.skip"
  ignore_artifacts:                    # Bỏ qua file cache/build do lệnh verify sinh ra giữa các lần retry
    - ".auto-engine/**"
    - ".pytest_cache/**"
    - "**/__pycache__/**"
    - "coverage/**"
    - "*.tsbuildinfo"
    - ".mypy_cache/**"
    - ".ruff_cache/**"

sprint:
  require_plan_approval: true          # Dừng sau `engine plan` để người dùng duyệt `plan.json` (trừ khi truyền `--auto-approve`)
  final_verify_commands:               # Mảng lệnh kiểm thử tích hợp cuối sprint (chạy tuần tự, bọc `rtk` từng lệnh)
    - "npm run typecheck"
    - "npm run build"
    - "npm test"

tools:
  use_rtk: true                        # Bọc lệnh verify qua `rtk` nếu tìm thấy binary
  rtk_exclude_commands:                # Tránh làm hỏng chữ ký GPG/SSH hoặc hook khi commit
    - "git commit"
  repomix_ignore:
    - "node_modules/**"
    - ".git/**"
    - "dist/**"
    - "build/**"
    - ".venv/**"
    - "*.lock"

notifications:
  webhook_url_env: "ENGINE_WEBHOOK_URL" # Tùy chọn: Discord / Telegram / Slack webhook khi BLOCKED / HALTED / DONE

# [TÙY CHỌN - CHỈ DÙNG KHI executor.mode LÀ "aider" HOẶC "opencode" (MODE C)]
# gateway:
#   base_url: "http://127.0.0.1:4000/v1"
#   api_key_env: "LITELLM_VIRTUAL_KEY"
#   models:
#     planner: "tier-planner"
#     executor: "tier-coder-fast"
#     escalation: "tier-coder-strong"
```

### 4.2. Cấu hình LiteLLM (`litellm_config.yaml` — Tùy chọn cho Mode C)

Chỉ cần tạo và chạy `litellm` proxy khi bạn sử dụng **Mode C** (`aider` / `opencode` với BYOK hoặc model self-host). Nếu dùng **Antigravity (`agy_cli` / `agy_sdk` / `mcp_only`)**, không cần chạy proxy này:

```yaml
model_list:
  - model_name: tier-planner
    litellm_params:
      model: "openai/gpt-5"
      api_key: os.environ/PLANNER_API_KEY

  - model_name: tier-coder-fast
    litellm_params:
      model: "openai/qwen2.5-coder-32b"
      api_base: os.environ/CODER_API_BASE
      api_key: os.environ/CODER_API_KEY

  - model_name: tier-coder-strong
    litellm_params:
      model: "anthropic/claude-sonnet-4-5"
      api_key: os.environ/ESCALATION_API_KEY

general_settings:
  max_budget: 5.0
  budget_duration: "1d"
```

### 4.3. Schema Đặc tả Task (`TaskSpec` — Pydantic Model trong `models.py`)

Mọi task do Planner sinh ra hoặc import từ `tasks.json` bắt buộc phải khớp schema sau:

```json
{
  "task_id": "T-01",
  "title": "Thêm middleware xác thực JWT và kiểm tra quyền admin",
  "complexity": "medium",
  "depends_on": [],
  "objective": "Triển khai hàm verifyAdminMiddleware trong src/middleware/auth.ts sử dụng verifyToken sẵn có.",
  "acceptance_criteria": [
    "Token hợp lệ + role=admin gọi next() không lỗi",
    "Token hết hạn trả HTTP 401",
    "Token hợp lệ nhưng role!=admin trả HTTP 403"
  ],
  "allowed_write_files": [
    "src/middleware/auth.ts",
    "tests/auth.test.ts"
  ],
  "create_new_files": [],
  "protected_test_files": [
    "tests/jwt.core.test.ts"
  ],
  "readonly_context_files": [
    "src/types/user.ts",
    "src/utils/jwt.ts"
  ],
  "needs_docs_mcp": false,
  "needs_lsp_mcp": false,
  "constraints": [
    "Không cài thêm package mới",
    "Không sửa chữ ký hàm verifyToken"
  ],
  "verify_commands": [
    "npx tsc --noEmit",
    "npx vitest run tests/auth.test.ts"
  ]
}
```

### 4.4. Schema Cơ sở dữ liệu SQLite (`state.db` trong `database.py`)

Khi mở kết nối SQLite, bắt buộc chạy `PRAGMA journal_mode=WAL;`, `PRAGMA busy_timeout=5000;` và `PRAGMA foreign_keys=ON;` để an toàn khi chạy đa tiến trình hoặc phục hồi sau crash.

```sql
CREATE TABLE IF NOT EXISTS sprint_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Lưu các khóa: 'plan_approved' ('0'|'1'), 'plan_hash', 'consecutive_blocked' ('0', '1', ...)

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    complexity TEXT NOT NULL CHECK (complexity IN ('low', 'medium', 'high')) DEFAULT 'medium',
    spec_json TEXT NOT NULL,          -- Lưu toàn bộ TaskSpec JSON
    depends_on_json TEXT NOT NULL,    -- JSON array các task_id phụ thuộc
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'RUNNING', 'VERIFYING', 'DONE', 'BLOCKED')
    ) DEFAULT 'PENDING',
    retry_count INTEGER NOT NULL DEFAULT 0,
    total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    total_completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    last_error TEXT,
    last_error_hash TEXT,             -- SHA-256 của trimmed_error_log để phát hiện lỗi lặp y hệt
    checkpoint_sha TEXT,              -- Git commit SHA sạch trước khi chạy task
    commit_sha TEXT,                  -- Git commit SHA sau khi PASS
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt INTEGER NOT NULL,
    model_used TEXT NOT NULL,
    effort_used TEXT DEFAULT 'medium',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    duration_sec REAL DEFAULT 0.0,
    verdict TEXT NOT NULL CHECK (
        verdict IN (
            'PASS',
            'SCOPE_VIOLATION',
            'TEST_GAMING',
            'EMPTY_DIFF',
            'IDENTICAL_ERROR',
            'VERIFY_FAIL',
            'TIMEOUT',
            'MAX_TURNS_EXCEEDED',
            'BUDGET_EXCEEDED',
            'CRASH_RECOVERED'
        )
    ),
    error_hash TEXT,
    trimmed_error_log TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 5. Đặc tả Chi tiết Từng Module (Implementation Details)

### 5.1. `src/engine/indexer.py` (Tầng 1 — 0 Token)
* **Nhiệm vụ:** Chạy với `cwd=config.project.target_repo` để sinh `.auto-engine/repo_map.xml`:
  ```bash
  npx -y repomix --compress --style xml --output .auto-engine/repo_map.xml --ignore "<patterns>"
  ```
* **Phạm vi sử dụng:** `repo_map.xml` được đưa toàn bộ **CHỈ cho Tầng 2 (Planner)**. Tầng 3 (`compiler.py`) **KHÔNG** nhồi nguyên `repo_map.xml` vào prompt Executor, mà chỉ dùng `xml.etree.ElementTree` đọc đúng các thẻ `<file path="...">` cần thiết để lấy chữ ký/symbol (0 token, < 10ms, không tự viết regex/AST parser).
* **Cơ chế Cache thông minh:** Tính SHA-256 của `git rev-parse HEAD` + `git status --porcelain`. Nếu hash không đổi, tái sử dụng ngay `repo_map.xml` hiện có.
* **Cô lập `.auto-engine/` trên repo đích:** Lệnh `engine init` tự động kiểm tra và ghi `.auto-engine/` vào `<target_repo>/.git/info/exclude` (không làm bẩn `.gitignore` chung của dự án) để `.auto-engine/` không bao giờ lẫn vào `git status` hoặc bị `git clean` xóa mất.
* **Hàm phụ trợ:** `list_real_files(repo_path) -> set[str]` dùng `git ls-files` (với `cwd=repo_path`) và chuẩn hóa dấu `\` thành `/` (POSIX).

### 5.2. `src/engine/planner.py` (Tầng 2 — Gọi LLM 1 lần + Ambiguity & Approval Gate)
* **Đầu vào:** File PRD (`.md`) + `.auto-engine/repo_map.xml` đã nén.
* **Bước 1 — PRD Ambiguity Check:** Prompt của Planner có quy tắc ép trả về đối tượng JSON `PlanOutput` gồm `status: "NEEDS_CLARIFICATION" | "READY"`. Nếu yêu cầu trong PRD mơ hồ, thiếu ràng buộc API/Schema quan trọng hoặc mâu thuẫn kiến trúc -> trả về `"NEEDS_CLARIFICATION"` kèm tối đa **3–5 câu hỏi ngắn gọn** và dừng ngay (không đoán mò rồi đẻ ra task sai).
* **Bước 2 — Sinh Plan (`agy -p --json-schema` hoặc LiteLLM fallback):**
  * Mặc định gọi `agy --print --output-format json --json-schema <PlanOutputSchema> --model <planner.model> --effort high` (truyền PRD + `repo_map.xml` qua `stdin`, `cwd=target_repo`). Nếu chạy Mode C thì gọi LiteLLM.
* **Bước 3 — Kiểm tra chống ảo giác theo thứ tự tô-pô DAG (Deterministic Validation — 0 token):**
  * Kiểm tra đồ thị phụ thuộc không có chu trình (**DAG Acyclic Check**).
  * Kiểm tra `len(allowed_write_files) <= config.limits.max_files_per_task`.
  * Kiểm tra `protected_test_files` không giao với `allowed_write_files` hoặc `create_new_files`.
  * **Kiểm tra đường dẫn file theo DAG (`virtual_files(T)`):** Duyệt các task theo thứ tự tô-pô từ gốc đến lá. Với mỗi task `T`, tập file hợp lệ tại thời điểm `T` chạy là:
    $$\text{virtual\_files}(T) = \text{real\_files} \cup \bigcup_{A \in \text{ancestors}(T)} A.\text{create\_new\_files}$$
    * Mọi file trong `readonly_context_files`, `protected_test_files` và `allowed_write_files` (ngoại trừ `T.create_new_files`) **phải nằm trong `virtual_files(T)`** (giải quyết đúng trường hợp `T-02` sửa/đọc file do `T-01` mới tạo).
    * Các file trong `T.create_new_files` **không được phép** đã tồn tại trong `virtual_files(T)`.
    * Nếu phát hiện đường dẫn sai/bịa, trả danh sách file sai cho Planner tự sửa **tối đa 1 lần duy nhất**.
* **Bước 4 — Plan Approval Gate:** Lưu plan vào `.auto-engine/plan.json` và bảng `tasks`, đặt `plan_approved = '0'` (trừ khi chạy `--auto-approve`), đồng thời in bảng tóm tắt task + mức `complexity` để người dùng rà soát và xác nhận bằng `engine approve` trước khi tiêu token thực thi.
* Hỗ trợ import từ file ngoài (`--from-tasks-json` tương thích `claude-task-master` hoặc `openspec/`).

### 5.3. `src/engine/compiler.py` (Tầng 3 — Surgical Prompt Compiler, 0 Token)
* **Nguyên lý cốt lõi (Chống loãng ngữ cảnh & Không tự viết AST parser):**
  * **KHÔNG** nhồi toàn bộ `repo_map.xml` vào prompt Executor mỗi task.
  * Thay vào đó, sử dụng **Local Neighborhood Context** dựa trên `TaskSpec` kết hợp **tra cứu thẻ `<file path="...">` trong `.auto-engine/repo_map.xml`** (đọc bằng `xml.etree.ElementTree`, đã được `repomix --compress` tạo sẵn bằng Tree-sitter đa ngôn ngữ -> tốn 0 token, < 10ms) để trích chữ ký/export cho `readonly_context_files` và danh sách symbol cho target file $> 300$ dòng.
* **Cấu trúc Prompt Bắt buộc:**
  ```markdown
  [STATIC PREFIX]
  # SYSTEM RULES
  - Thực thi chính xác theo đặc tả TASK bên dưới.
  - CHỈ được sửa/tạo file trong `ALLOWED_WRITE_FILES` và `CREATE_NEW_FILES`. Tuyệt đối không sửa `PROTECTED_TEST_FILES` hay file khác.
  - CHỈ sửa code theo đặc tả rồi kết thúc ngay. TUYỆT ĐỐI KHÔNG tự chạy lệnh test, build, typecheck hay lint bên trong phiên (hệ thống bên ngoài sẽ tự chạy verify và trả log rút gọn nếu cần).
  - KHÔNG lách luật bằng `@ts-ignore`, `eslint-disable`, `# type: ignore`, `# noqa`, `.skip()`, `.only()`, hoặc xóa bỏ assertion trong test.
  - Ưu tiên sửa gọn trực tiếp (targeted edit), không viết lại phần code không liên quan.
  <Nếu config.project.shared_rules_file được khai báo: chèn nội dung file quy ước dự án vào đây>

  ---
  [DYNAMIC TASK PAYLOAD - PER TASK]
  # TASK: {task_id} - {title}
  - Objective: {objective}
  - Acceptance Criteria: {acceptance_criteria}
  - Allowed Write Files: {allowed_write_files}
  - Create New Files: {create_new_files}
  - Protected Test Files (DO NOT TOUCH): {protected_test_files}
  - Constraints: {constraints}
  - Verification Commands (Engine sẽ chạy sau khi bạn xong): {verify_commands}

  # READONLY CONTEXT (Interfaces / Types / Signatures từ repo_map.xml hoặc file ngắn)
  <ưu tiên lấy phần đã nén từ `<file path="...">` trong repo_map.xml; fallback nội dung ngắn>

  # TARGET FILES TO MODIFY
  <Nếu file <= inline_file_max_lines (300 dòng): chèn nội dung hiện tại trên đĩa kèm số dòng `L1: ...`>
  <Nếu file > 300 dòng: CHỈ ghi đường dẫn + trích đoạn `<file>` từ `repo_map.xml` để đọc có chọn lọc>

  # PREVIOUS ATTEMPT FEEDBACK (Chỉ xuất hiện khi attempt > 0)
  <Tránh trùng token:
   - Nếu VERIFY_FAIL trên file <= 300 dòng (đã inline bản sửa lỗi ở trên): BỎ `Current Git Diff`, chỉ gửi `Trimmed Error Log` (<= 30 dòng + ±7 dòng code).
   - Nếu VERIFY_FAIL trên file > 300 dòng (chưa inline): Gửi kèm `Current Git Diff` rút gọn + `Trimmed Error Log`.
   - Nếu sau `SCOPE_VIOLATION` / `TEST_GAMING` (đã rollback sạch): Gửi bản tóm tắt vi phạm được lưu trước khi rollback.>
  ```
* **Lưu ý thực tế về Prompt Caching:** Ngưỡng kích hoạt Prompt Caching của các nhà cung cấp LLM thường là **1.024–2.048 token**. Với dự án nhỏ, giữ `[STATIC PREFIX]` cực gọn (~150 token) là cách tiết kiệm nhất (không cố nhồi rác để câu cache). Với sprint lớn có sẵn `shared_rules_file` (`AGENTS.md` / domain conventions), `[STATIC PREFIX]` tự nhiên vượt ngưỡng cache và giữ bất biến 100% giữa các task.
* **Kiểm tra độ dài Pre-flight:** Ước lượng số token (`len(text) / 3.5` hoặc `tiktoken`). Nếu vượt `limits.max_prompt_tokens_per_call` (12.000): tự động cắt bớt `readonly_context_files`. Nếu vẫn vượt ngưỡng, báo lỗi task quá to (`PROMPT_TOO_LARGE`) để tách nhỏ trước khi tốn 1 token nào gọi Executor.

### 5.4. `src/engine/git_guard.py` (An toàn mã nguồn & Anti-Gaming — 0 Token)
* Mọi lệnh Git bắt buộc truyền **`cwd=config.project.target_repo`** và chuẩn hóa toàn bộ đường dẫn về POSIX (`Path.as_posix()`) để chạy chính xác trên Windows & Linux/macOS.
* `ensure_work_branch(sprint_name)`: Kiểm tra working tree sạch (bỏ qua `config.guards.ignore_artifacts`); tạo/checkout nhánh `auto-engine/<sprint_name>` từ `base_branch`. Cấm commit trực tiếp lên `main`.
* `create_checkpoint() -> str`: Trả về `HEAD` SHA sạch trước khi chạy task.
* `has_changes(ignore_artifacts) -> bool`: Kiểm tra `git status --porcelain` sau khi lọc bỏ `ignore_artifacts` (như `.auto-engine/**`, `.pytest_cache/**`, `coverage/**`). Trả `False` nếu AI không sửa bất kỳ file nào (`EMPTY_DIFF`).
* `check_scope(allowed_files, create_new_files, protected_test_files, ignore_artifacts) -> tuple[bool, list[str]]`:
  * Chạy `git status --porcelain` và `git diff --name-only`, bỏ qua mọi đường dẫn khớp `ignore_artifacts` (tránh việc cache sinh ra bởi `pytest`/`tsc` ở `attempt 0` bị nhận nhầm là vi phạm scope ở `attempt 1`).
  * Trả `False` nếu có bất kỳ file nào bị thay đổi/tạo mới nằm ngoài `(allowed_files ∪ create_new_files) \ protected_test_files`.
* `check_diff_sanity(forbid_patterns, forbid_assertion_deletion) -> tuple[bool, list[str]]` (**Anti-Test-Gaming Guard toàn diện**):
  1. **Quét các dòng thêm mới trong `git diff -U0`** (`+` không phải `+++`) đối chiếu `forbid_patterns`.
  2. **Quét toàn bộ nội dung mọi file mới (untracked)** khớp `create_new_files` (vì `git diff` bỏ qua file untracked — chống AI tạo file test mới chứa `.skip()` hoặc `@ts-ignore`).
  3. **Assertion Deletion Guard (`forbid_test_assertion_deletion`):** Với mọi file test bị sửa (`test_*`, `*.test.*`, `*.spec.*`), so sánh số dòng chứa assertion (`expect(`, `assert`, `toThrow`, `toEqual`, `toBe`, `assertEqual`, `pytest.raises`) ở phần xóa (`-`) so với phần thêm (`+`). Nếu số assertion bị xóa nhiều hơn số thêm vào -> chặn `TEST_GAMING`.
* `commit_task(task_id, title) -> str`: `git add` đúng danh sách file hợp lệ và commit `feat(auto-engine): [<task_id>] <title>`. **Lưu ý:** không chạy `git commit` qua `rtk`.
* `hard_rollback(checkpoint_sha)`: Chạy `git reset --hard <checkpoint_sha>` và `git clean -fd` (loại trừ `.auto-engine/`) để khôi phục 100% trạng thái sạch.

### 5.5. `src/engine/verifier.py` (Gatekeeper, Log Trimmer & Hash — 0 Token)
* **Thứ tự kiểm tra bắt buộc (Fail-Fast — tiết kiệm CPU và thời gian):**
  1. **Empty Diff Check:** Gọi `git_guard.has_changes()`. Nếu `False` -> dừng ngay, trả verdict `EMPTY_DIFF` (không chạy build/test).
  2. **Scope Check:** Gọi `git_guard.check_scope()`. Nếu vi phạm -> dừng ngay, trả verdict `SCOPE_VIOLATION` kèm danh sách file cấm sửa.
  3. **Anti-Test-Gaming Check:** Gọi `git_guard.check_diff_sanity()`. Nếu phát hiện lách luật hoặc xóa assertion -> dừng ngay, trả verdict `TEST_GAMING` kèm dòng code vi phạm.
  4. **Sequential Command Execution:** Duyệt từng lệnh đơn trong `task.verify_commands: list[str]` với `cwd=config.project.target_repo`, timeout cứng (mặc định 120s/lệnh), ép `encoding="utf-8", errors="replace"`:
     * Nếu `config.tools.use_rtk` là `true` và `shutil.which("rtk")` tồn tại (và lệnh không nằm trong `rtk_exclude_commands`): bọc lệnh đơn qua `rtk`.
     * Nếu chưa cài `rtk`: thực thi trực tiếp lệnh đó.
     * **Dừng ngay ở lệnh đầu tiên fail** (không ghép chuỗi `&&` để đảm bảo `rtk` bọc chuẩn xác từng công cụ và chạy đồng nhất trên PowerShell 5.1, PowerShell 7 và Bash).
  5. **Log Trimmer (`trim_error_log`) & `error_hash`:**
     * Loại bỏ mã màu ANSI (`\x1b\[[0-9;]*m`), lọc bỏ stack trace rác trong `node_modules/`, `site-packages/`, `<frozen importlib`.
     * Giữ tối đa `config.limits.max_error_log_lines` (30 dòng) tập trung vào dòng chứa `Error`, `FAIL`, `AssertionError`, `TS\d{4}`, tên file đích và số dòng lỗi.
     * Tự động trích thêm **±7 dòng code** từ file thật quanh dòng lỗi để AI sửa ngay không cần gọi tool đọc file.
     * Tính `error_hash = sha256(normalized_trimmed_log)` (bỏ qua timestamp/đường dẫn tuyệt đối biến thiên) để Orchestrator nhận biết lỗi lặp y hệt (`IDENTICAL_ERROR`).

### 5.6. Execution Bridges (`src/engine/bridges/`)

#### A. `agy_cli_executor.py` (**Mode A1 — Primary, Khuyên dùng**)
Mỗi lần thực thi gọi **1 tiến trình `agy` mới hoàn toàn (Stateless 100%)** với `cwd=config.project.target_repo`:
* **Khóa chống đụng độ & Dynamic MCP Gating an toàn:**
  * Dùng file khóa `.auto-engine/run.lock` để không cho 2 tiến trình engine chạy đè lên nhau.
  * Nếu `config.agy.dynamic_mcp_gating == true`: đọc trạng thái ban đầu qua `agy mcp list`, bật/tắt `serena` (`needs_lsp_mcp`) và `context7` (`needs_docs_mcp`) qua `agy mcp enable/disable`, và **bắt buộc bọc toàn bộ quá trình chạy trong `try...finally` (kèm signal handler SIGINT/SIGTERM)** để luôn khôi phục nguyên trạng MCP ban đầu cho máy nếu crash hoặc bấm Ctrl+C. *(Lưu ý: nếu bạn thường xuyên mở song song cửa sổ Antigravity IDE khác trên cùng máy, hãy đặt `dynamic_mcp_gating: false` hoặc dùng Mode A2 `agy_sdk`).*
* **Lệnh thực thi chuẩn (`agy --help` & smoke-test verified) & Real-time Stream Watchdog:**
  ```bash
  agy --output-format stream-json \
      --input-format stream-json \
      --model <tier.model> \
      --effort <tier.effort> \
      --mode accept-edits \
      --disable-slash-commands \
      --print-timeout <executor.timeout_seconds>s
  ```
  * Nếu `config.agy.auto_approve_tools == true` (trong môi trường sandbox): thêm `--dangerously-skip-permissions`.
  * **Truyền prompt qua `stdin` dạng NDJSON (`{"event": "user", "message": {"content": prompt}}`):** Tuyệt đối tránh truyền `--print` làm tham số CLI để không vướng giới hạn 32.767 ký tự trên Windows và lỗi escape PowerShell.
  * **Real-time Stream Watchdog (Chặn đốt token ngầm trong 1 lượt `agy -p`):** Thay vì đợi `agy` chạy xong mới biết tốn bao nhiêu token, đọc từng dòng NDJSON từ `stdout` theo thời gian thực:
    1. Đếm số lần gọi tool tích lũy: nếu vượt `limits.max_tool_calls_per_attempt` (12) -> kill sạch cây tiến trình ngay, trả verdict `MAX_TURNS_EXCEEDED`.
    2. Theo dõi token tích lũy (nếu stream trả về): nếu vượt `limits.max_tokens_per_task` -> kill ngay, trả verdict `BUDGET_EXCEEDED`.
    3. Phát hiện lệnh shell bị cấm (tự chạy `pytest`, `vitest`, `tsc`, `npm test`...) -> kill ngay trước khi nó phun 500 dòng log vào context của chính agent.
* **Parse Output:** Lấy thống kê token cuối phiên từ event kết quả trong `stream-json` (hỗ trợ linh hoạt cả trường hợp gói OAuth không trả `cost_usd`) và ghi vào bảng `runs`. Nếu vượt `timeout_seconds`, kill cây tiến trình và trả `TIMEOUT`.

#### B. `agy_sdk_executor.py` (**Mode A2 — Kiểm soát sâu bằng Python SDK**)
* Dành cho người dùng muốn kiểm soát ở mức từng `ToolCall` thông qua `google-antigravity` (`Agent`, `LocalAgentConfig`, `CapabilitiesConfig`).
* Khởi tạo `async with Agent(config) as agent:` sạch cho mỗi task (chỉ cấp MCP tools khi `needs_lsp_mcp`/`needs_docs_mcp` bật).
* Lắng nghe `response.tool_calls`: kiểm tra tham số đường dẫn của các lệnh sửa/ghi file; ghi nhận cảnh báo/ngắt sớm nếu phát hiện agent có ý định sửa file ngoài `allowed_write_files`.

#### C. `mcp_server.py` (**Mode B — Nối vào Antigravity IDE / App**)
* Sử dụng `fastmcp`, chỉ đăng ký đúng **4 tool tối giản** (mọi log debug bắt buộc ghi ra `stderr`, giữ sạch tuyệt đối `stdout` cho giao thức stdio MCP):
  1. `get_next_task()`: Tìm task `PENDING` đủ điều kiện, tạo Git checkpoint, biên dịch Surgical Prompt gọn nhất và trả về cho Antigravity kèm nhắc nhở mở chat mới sau khi nộp bài.
  2. `submit_and_verify(task_id: str)`: Chạy toàn bộ Tầng 5 (`EMPTY_DIFF` -> Scope Guard -> Anti-Test-Gaming -> `rtk` Verifier). PASS thì commit + `DONE`; FAIL thì trả tối đa 30 dòng log + kiểm tra `IDENTICAL_ERROR` / vượt retry -> `hard_rollback` & `BLOCKED`.
  3. `get_sprint_status()`: Tóm tắt `DONE / PENDING / BLOCKED` và tổng token đã tiêu thụ.
  4. `rollback_current_task(task_id: str, reason: str)`: Rollback về checkpoint và đánh dấu `BLOCKED`.

#### D. `cli_executor.py` (**Mode C — Tùy chọn Fallback cho Aider / OpenCode**)
* Chỉ kích hoạt khi `executor.mode` là `"aider"` hoặc `"opencode"` (dùng với `litellm` proxy khi chạy BYOK/local model):
  * `aider`: chạy với `--yes-always --no-auto-commits --no-stream --edit-format diff --message-file .auto-engine/current_prompt.md <allowed_write_files...>` (luôn bật `--no-auto-commits` để `git_guard.py` giữ toàn quyền).
  * `opencode`: chạy chế độ headless kết nối LiteLLM endpoint.

### 5.7. `src/engine/orchestrator.py` (Vòng lặp Điều phối & Multi-Layer Circuit Breaker)

Thuật toán điều phối bắt buộc cho lệnh `engine run` và `engine resume`:

```text
0. Kiểm tra Plan Gate: đọc `sprint_meta['plan_approved']`. Nếu != '1' và không có `--auto-approve`:
   -> Dừng và nhắc chạy `engine approve` (tránh đốt token chạy plan chưa duyệt).
1. Crash Recovery (`engine resume` hoặc tự kiểm tra khi khởi động):
   -> Tìm các task đang kẹt ở `RUNNING` hoặc `VERIFYING` -> `git_guard.hard_rollback(checkpoint_sha)`
   -> Ghi run `CRASH_RECOVERED` -> trả task về `PENDING`.
2. Tạo/checkout work branch qua `git_guard.ensure_work_branch()`.
3. Kiểm tra tổng chi phí hiện tại < `max_budget_usd_per_sprint` và `consecutive_blocked < max_consecutive_blocked_tasks`.
4. Lặp chừng nào còn task `PENDING` có toàn bộ `depends_on` ở trạng thái `DONE`:
   a. Lấy 1 task T. Đặt `prev_error_hash = None`.
   b. `checkpoint_sha = git_guard.create_checkpoint()`. Lưu vào DB, `status = RUNNING`.
   c. Lặp `attempt` từ `0` đến `max_retries_per_task`:
      - Chọn Tier (Model + Effort):
          * `attempt == max_retries_per_task` hoặc `T.complexity == 'high'` -> `tier = escalation`
          * `attempt == 0` và `T.complexity == 'low'` -> `tier = coder_low`
          * Mặc định còn lại -> `tier = coder_medium`
      - Kiểm tra token task T < `max_tokens_per_task` (nếu vượt -> `BUDGET_EXCEEDED`, break).
      - Biên dịch Surgical Prompt (nếu `attempt > 0` sau `VERIFY_FAIL` trên file > 300 dòng mới kèm `git diff`; nếu sau `SCOPE_VIOLATION`/`TEST_GAMING` thì truyền bản tóm tắt vi phạm đã lưu trước khi rollback).
        Nếu vượt `max_prompt_tokens_per_call` -> đánh dấu `BLOCKED` (cần chia nhỏ task), break.
      - Gọi Executor đã cấu hình (`agy_cli` / `agy_sdk` / fallback CLI) trong phiên Stateless.
      - Cập nhật token, cost, model/effort vào SQLite (`runs` + `tasks`).
      - Nếu Executor trả về `MAX_TURNS_EXCEEDED` hoặc `BUDGET_EXCEEDED` từ Stream Watchdog:
          * Lưu thông báo watchdog vào `last_error`, xử lý tương tự `VERIFY_FAIL` (nâng lên `escalation` nếu chưa ở `escalation`, hoặc `BLOCKED` nếu đã vượt ngưỡng).
      - Chạy `verifier.verify(T)` (Empty Diff -> Scope -> Anti-Gaming -> `rtk` từng lệnh trong `verify_commands`):
          * `PASS`:
              `git_guard.commit_task(T.task_id, T.title)`
              Đặt `T = DONE`, reset `consecutive_blocked = 0` -> sang task kế tiếp.
          * `SCOPE_VIOLATION` hoặc `TEST_GAMING`:
              Lưu chi tiết file/dòng vi phạm vào `last_error` TRƯỚC KHI rollback.
              `git_guard.hard_rollback(checkpoint_sha)` (xóa sạch thay đổi phạm luật trước khi thử lại).
          * `EMPTY_DIFF`:
              Nếu đã dùng `escalation` -> đánh dấu `BLOCKED` ngay (không retry vô ích).
              Ngược lại ép lần kế tiếp nhảy thẳng lên `escalation` (`attempt` nâng cấp).
          * `VERIFY_FAIL` / `TIMEOUT`:
              Nếu `error_hash == prev_error_hash`:
                -> Ghi verdict `IDENTICAL_ERROR`: nếu đã ở `escalation` thì break (đánh dấu `BLOCKED` ngay); nếu chưa thì nhảy thẳng sang `escalation` ở lần thử kế.
              Cập nhật `prev_error_hash = error_hash`, lưu `trimmed_error_log` (<= 30 dòng).
   d. Nếu hết vòng lặp vẫn chưa `PASS`:
      - `git_guard.hard_rollback(checkpoint_sha)`
      - Đánh dấu `T = BLOCKED` trong SQLite; tăng `consecutive_blocked += 1`.
      - Gửi Webhook thông báo `task_id` + lý do lỗi.
      - Kiểm tra Global Circuit Breaker: nếu `consecutive_blocked >= max_consecutive_blocked_tasks` (3):
        -> Gửi cảnh báo khẩn và **HALT toàn sprint** (tránh lỗi môi trường/thiếu dependency gây đốt token dây chuyền).
      - Ngược lại, bỏ qua các task phụ thuộc T và tiếp tục các nhánh task độc lập khác.
5. Final Sprint Integration Gate (0 Token):
   - Nếu không có task nào chạy dở và có ít nhất 1 task `DONE`: chạy tuần tự từng lệnh trong `config.sprint.final_verify_commands` trên toàn repo (với `cwd=target_repo` và `rtk`).
   - Báo kết quả tích hợp cuối cùng (PASS / FAIL) kèm bảng Rich: tổng task `DONE / BLOCKED`, token, chi phí, số lần chặn bởi Circuit Breaker và kết quả `rtk gain` (nếu có).
```

---

## 6. Cấu hình MCP Stack & Dynamic MCP Gating (`mcp_stack.json`)

Nạp toàn bộ danh sách MCP tool lớn (Serena + Context7) vào mọi prompt làm tốn thêm ~3.000–5.000 input token cho mỗi lần gọi dù task đó chỉ sửa logic đơn thuần. Do đó hệ thống áp dụng nguyên tắc **Dynamic MCP Gating**:

* **Với Mode A1 (`agy_cli`):** Chỉ bật `serena` khi `task.needs_lsp_mcp == true` (thường cho file > 300 dòng) và chỉ bật `context7` khi `task.needs_docs_mcp == true` (khi dùng thư viện mới/phức tạp) thông qua `agy mcp enable` / `agy mcp disable`. Task `complexity == "low"` mặc định tắt cả hai để prompt nhẹ nhất có thể. Bắt buộc dùng `.auto-engine/run.lock` và khối `try...finally` (kèm signal handler) để khôi phục đúng trạng thái MCP ban đầu cho máy. *(Nếu chạy song song cửa sổ Antigravity IDE khác trên cùng máy, hãy đặt `dynamic_mcp_gating: false` hoặc chuyển sang Mode A2 `agy_sdk`).*
* **Với Mode A2 (`agy_sdk`):** Chỉ truyền MCP server tương ứng vào `LocalAgentConfig` của task có cờ bật.
* **Với Mode B (Antigravity IDE / App):** File `mcp_stack.json` chuẩn khai báo sẵn 3 server để người dùng bật/tắt linh hoạt trong IDE:

```json
{
  "mcpServers": {
    "auto-prompt-engine": {
      "command": "uv",
      "args": ["run", "--directory", "D:/code/auto-prompt-engine", "engine", "serve-mcp", "--config", "config.yaml"]
    },
    "serena": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/oraios/serena",
        "serena", "start-mcp-server",
        "--context", "ide-assistant",
        "--project", "D:/code/target-repo"
      ]
    },
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp@latest"]
    }
  }
}
```

---

## 7. Kế hoạch Triển khai Theo Giai đoạn (Phased Execution Plan)

AI thực thi **PHẢI** code và vượt qua kiểm thử theo đúng thứ tự 4 giai đoạn sau, không được nhảy cóc:

### Giai đoạn 1 — Khung Kỷ luật 0-Token & Chống Lách Luật (Foundation)
1. Khởi tạo dự án Python với `pyproject.toml` (`uv`).
2. Viết `models.py` (`verify_commands: list[str]`), `database.py` (SQLite WAL + `sprint_meta` + `error_hash` + `MAX_TURNS_EXCEEDED` + `CRASH_RECOVERED`), `git_guard.py` (`cwd=target_repo`, chuẩn hóa path Windows/POSIX, lọc `ignore_artifacts`, Scope Guard, `has_changes`, `protected_test_files`, Anti-Test-Gaming quét cả `git diff` + file untracked + chặn xóa assertion, Commit không qua `rtk`, Hard Rollback), `verifier.py` (`EMPTY_DIFF` -> Scope -> Anti-Gaming -> lặp từng lệnh `verify_commands` qua `rtk` + UTF-8 fail-fast -> `trim_error_log` <= 30 dòng + `error_hash`).
3. Viết và chạy pass 100%: `test_database.py`, `test_git_guard.py` (dùng temp git repo thật kiểm thử Scope Guard, `ignore_artifacts`, Anti-Test-Gaming chặn `@ts-ignore`/`.skip()` ở cả file mới untracked lẫn xóa `expect`/`assert`, bảo vệ `protected_test_files`, và `hard_rollback`), `test_verifier.py` (kiểm thử log 200 dòng nén <= 30 dòng, `verify_commands` fail-fast không dùng `&&`, `EMPTY_DIFF`, `error_hash` giống nhau khi chỉ khác timestamp).

### Giai đoạn 2 — Indexer & Surgical Prompt Compiler
1. Viết `indexer.py` (`repomix --compress` cache theo git hash + `list_real_files` chuẩn POSIX + tự thêm `.auto-engine/` vào `.git/info/exclude`).
2. Viết `compiler.py` với Local Neighborhood Context (không nhồi full `repo_map.xml` vào task Executor; dùng `xml.etree.ElementTree` tra cứu thẻ `<file path="...">` trong `repo_map.xml` để lấy chữ ký 0-token), ngưỡng `inline_file_max_lines` (300 dòng), cấm Executor tự chạy test/build trong `# SYSTEM RULES`, tránh gửi trùng `Current Git Diff` khi file đã inline, và chặn pre-flight khi vượt `max_prompt_tokens_per_call`.
3. Viết và chạy pass `test_compiler.py`.

### Giai đoạn 3 — Cầu nối Antigravity & MCP Stack
1. Viết `bridges/agy_cli_executor.py` (**Mode A1 — Primary**): gọi `agy -p --output-format stream-json --model ... --effort ... --mode accept-edits --disable-slash-commands`, `cwd=target_repo`, truyền prompt qua `stdin`, `.auto-engine/run.lock` + `try...finally` khôi phục trạng thái `agy mcp`, và **Real-time Stream Watchdog** (kill ngay khi vượt `max_tool_calls_per_attempt`, vượt `max_tokens_per_task` hoặc gọi lệnh test bị cấm).
2. Viết `bridges/agy_sdk_executor.py` (**Mode A2**): tích hợp `google-antigravity` SDK kèm real-time `tool_calls` scope interceptor.
3. Viết `bridges/mcp_server.py` (**Mode B**) bằng `fastmcp` (4 tool tối giản, log chỉ ra `stderr`) và `bridges/cli_executor.py` (**Mode C — Optional** cho `aider`/`opencode`).
4. Tạo sẵn các file mẫu chuẩn: `config.example.yaml`, `litellm_config.yaml` (tùy chọn), `mcp_stack.json`, và chạy pass `test_agy_bridge.py` (mock subprocess `stream-json` watchdog + SDK interceptor).

### Giai đoạn 4 — Planner, Orchestrator, Circuit Breaker & Kiểm thử Dry-Run Toàn tuyến
1. Viết `planner.py`: Ambiguity Check (`NEEDS_CLARIFICATION`), gọi `agy -p --json-schema` (hoặc LiteLLM fallback), kiểm tra DAG Acyclic, `max_files_per_task`, kiểm tra đường dẫn theo `virtual_files(T)` trên thứ tự tô-pô DAG (tự sửa tối đa 1 lần), lưu `.auto-engine/plan.json` chờ Approval Gate, hỗ trợ import `tasks.json`.
2. Viết `orchestrator.py`: Plan Approval Gate, Crash Recovery (`resume`), Complexity/Effort Routing, xử lý `SCOPE_VIOLATION` / `TEST_GAMING` (lưu vi phạm trước rollback) / `EMPTY_DIFF` / `MAX_TURNS_EXCEEDED` / `IDENTICAL_ERROR`, Global Circuit Breaker (`max_consecutive_blocked_tasks`), Webhook, và Final Sprint Integration Gate (`final_verify_commands`).
3. Hoàn thiện CLI trong `cli.py` bằng `typer` + `rich`:
   * `engine init [--check] [--smoke-agy]` — Kiểm tra `git`, `agy`, `repomix`, `rtk`, `uv`, tự thêm `.auto-engine/` vào `.git/info/exclude`, tạo `config.yaml`; `--smoke-agy` chạy 1 prompt thật 5 chữ để xác nhận cấu trúc JSON/stream thật của bản `agy` trên máy.
   * `engine plan <prd.md>` — Chạy Tầng 1–2, xuất câu hỏi làm rõ (nếu PRD mơ hồ) hoặc xuất `plan.json` chờ duyệt.
   * `engine approve` — Phê duyệt `plan.json` để mở khóa thực thi.
   * `engine run [--auto-approve]` — Chạy vòng lặp tự động (Mode A1/A2/C).
   * `engine resume` — Phục hồi sạch task đang kẹt `RUNNING`/`VERIFYING` sau crash/mất điện và chạy tiếp.
   * `engine serve-mcp` — Khởi chạy FastMCP Server cho Antigravity IDE/App (Mode B).
   * `engine report` — In thống kê chi tiết token, chi phí, số lần Circuit Breaker kích hoạt và tỷ lệ pass/fail từng task.
4. Viết và chạy pass `test_orchestrator_mock.py` giả lập trọn vẹn 7 kịch bản (0 token API):
   * Kịch bản 1: Task `low` chạy `coder_low` (`effort: low`) PASS ngay lần đầu -> tự commit -> mở khóa task phụ thuộc (kể cả đọc/sửa file do task cha tạo mới qua `virtual_files(T)`) -> Final Sprint Gate PASS.
   * Kịch bản 2: Executor sửa file ngoài `allowed_write_files` hoặc thêm `@ts-ignore` / `.skip()` (kể cả trong file mới untracked) / xóa `expect()` -> Scope/Anti-Gaming Guard chặn + lưu lỗi + rollback ngay -> retry sửa đúng -> PASS.
   * Kịch bản 3: Executor trả về `EMPTY_DIFF`, vượt `max_tool_calls_per_attempt` (`MAX_TURNS_EXCEEDED`), hoặc lặp y hệt `error_hash` (`IDENTICAL_ERROR`) -> nhảy thẳng `escalation` (`effort: high`) -> vẫn lỗi -> rollback -> đánh dấu `BLOCKED` không đốt retry vô ích.
   * Kịch bản 4: 3 task liên tiếp `BLOCKED` -> Global Circuit Breaker kích hoạt -> HALT toàn sprint, giữ nguyên trạng thái sạch nhánh chính.
   * Kịch bản 5: Giả lập task đang kẹt ở `RUNNING` kèm dirty files -> chạy `engine resume` -> rollback về `checkpoint_sha`, ghi `CRASH_RECOVERED` và chạy lại sạch sẽ.
   * Kịch bản 6: PRD mơ hồ -> Planner dừng ở trạng thái `NEEDS_CLARIFICATION` với <= 5 câu hỏi trước khi sinh task.
   * Kịch bản 7: Chạy `attempt 0` sinh artifact `.pytest_cache/` / `coverage/` rồi `VERIFY_FAIL` -> sang `attempt 1` `ignore_artifacts` không gây báo giả `SCOPE_VIOLATION`.

---

## 8. Tiêu chí Nghiệm thu (Acceptance Criteria)

Hệ thống chỉ được coi là hoàn thành khi AI thực thi chứng minh được toàn bộ các điều kiện sau:
- [ ] `uv run pytest` đạt **100% PASS** trên tất cả test suite (`test_database`, `test_git_guard`, `test_verifier`, `test_compiler`, `test_agy_bridge`, `test_orchestrator_mock`).
- [ ] `uv run engine init --check` chạy không lỗi trên Windows PowerShell & POSIX, tự thêm `.auto-engine/` vào `.git/info/exclude`, và `uv run engine init --smoke-agy` đọc đúng JSON stream thật từ bản `agy` đang cài.
- [ ] `uv run engine serve-mcp` khởi động sạch sẽ theo chuẩn stdio MCP mà không in bất kỳ log rác nào ra `stdout` (toàn bộ log ghi ra `stderr`).
- [ ] Kiểm chứng được: `compiler.py` lấy chữ ký 0-token từ `<file>` trong `repo_map.xml`, cấm Executor tự chạy test, không lặp diff khi file đã inline, giới hạn <= 30 dòng của `trim_error_log`, `verify_commands: list[str]` bọc `rtk` từng lệnh (không dùng `&&`), và `planner.py` duyệt đúng `virtual_files(T)` theo thứ tự tô-pô DAG.
- [ ] Kiểm chứng được: `git_guard.py` chặn `@ts-ignore` / `.skip()` ở cả file mới untracked và hành vi xóa assertion; `agy_cli_executor.py` truyền prompt qua `stdin` với `cwd=target_repo`, giữ `run.lock`, khôi phục MCP trong `try...finally`, kill tiến trình qua `stream-json` watchdog, và không làm rò rỉ API key trong mã nguồn.
