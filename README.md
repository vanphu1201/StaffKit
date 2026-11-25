# StaffKit v3.0 — Antigravity IDE Pro Engineering Plugin

> **Bộ khung 0-token gắn trực tiếp vào Antigravity IDE** dành cho Developer chuyên nghiệp & Team: từ thiết kế kiến trúc đa phương án, gỡ lỗi tận gốc không báo PASS giả, review PR cấp Staff Engineer (Blast Radius), refactor legacy an toàn, đến X-quang bảo mật & quản trị quyết định kiến trúc (ADR).

*(Phiên bản App Studio headless cũ đã được lưu trữ an toàn tại git tag `v1.2-legacy-studio-archive`; nhánh `main` hiện tại tinh gọn 100% cho Antigravity IDE Native Plugin.)*

---

## Cài đặt 1 chạm (Khuyên dùng)

- **Linux / macOS / Remote SSH Server:**
  ```bash
  curl -fsSL https://raw.githubusercontent.com/vanphu1201/automatic-promt/main/install.sh | bash
  ```
- **Windows (PowerShell):**
  ```powershell
  irm https://raw.githubusercontent.com/vanphu1201/automatic-promt/main/install.ps1 | iex
  ```

### Hoặc cài thủ công từ mã nguồn (2 bước)

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Linux/macOS: .venv/bin/python -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m engine.cli install-plugin
```

Sau đó mở **Plugins Manager** trong Antigravity IDE để kiểm tra **StaffKit** đang ở trạng thái **Enabled**.

---

## 5 Chế độ làm việc (Skills tự động kích hoạt trong Antigravity IDE)

1. **`/brainstorm-to-product` (Ý tưởng mới / Tính năng lớn):** Phỏng vấn đa vòng + tra cứu khả thi + so sánh 2–3 phương án kiến trúc + chia chặng + lưu Hiến pháp (`constitution.md`), Hợp đồng (`contracts.md`) & Quyết định kiến trúc (`decisions.md`).
2. **`/deep-fix` (Gỡ lỗi tận gốc / Không báo PASS giả):** Khoanh vùng 0-token bằng 1-hop Import Graph + Git Co-Change + AST windows → Snapshot Git → Kiểm chứng Diff Sanity + HTTP thực tế (`verify_and_smoke`) → Ghi nhớ bài học (`record_lesson`).
3. **`/audit-and-evolve` (Thẩm định sức khỏe dự án & Gỡ kẹt nâng cấp):** X-Quang 0-token (file lớn, TODO thật, `.env`/secret lộ, tỷ lệ test) + bảng điểm 5 chiều trung thực + lộ trình P0/P1/P2.
4. **`/pr-review` (Staff-Level Pre-Merge Review & Blast Radius):** Đọc `git diff` + 1-hop callers + Git Co-Change để chỉ ra bán kính ảnh hưởng và săn lỗi production thật (race condition, N+1, SQLi/XSS/IDOR, breaking contract).
5. **`/refactor-safe` (Refactor Legacy Không Vỡ Hành Vi):** Snapshot Git → khóa **Characterization Tests** trước → tách bước atomic → `verify_and_smoke` sau từng bước → rollback tức thì nếu hồi quy.

### Cấu hình Chuẩn Team theo từng Repo (Tùy chọn: `.auto-engine/team.yaml`)
```yaml
verify_commands: ["pnpm turbo typecheck", "pytest -q"]
sensitive_paths: ["migrations/", "infra/", ".github/workflows/"]
forbid_patterns: ["console.log(", "debugger"]
```

---

## 6 Công cụ MCP 0-token (`staffkit`)

| Công cụ | Tác dụng |
|---|---|
| `load_project_memory` | Nạp `constitution.md`, `contracts.md`, `lessons.md`, `handoff.md` và **Cross-Project Global Playbook** (`~/.gemini/auto-prompt-playbooks/global_lessons.md`) trong 1 lần gọi. |
| `locate_context` | Khoanh vùng file + **1-hop Import/Export Dependency Graph** (forward + reverse importers, ESM `.js`->`.ts`, `@/` alias) + cửa sổ AST + mở rộng từ khóa Việt→Anh. |
| `save_project_spec` | Ghi và lưu bền vững Hiến pháp, Hợp đồng kiến trúc, Handoff vào `.auto-engine/`. |
| `record_lesson` | Ghi nhận bài học nguyên nhân gốc để các phiên sau không tái phạm; hỗ trợ `global_playbook=True` để dùng chung mọi dự án. |
| `verify_and_smoke` | Chạy build/test + kiểm chứng endpoint HTTP thực tế (`smoke_url`), chống báo PASS giả. |
| `snapshot_or_undo` | Tạo snapshot Git trước khi refactor lớn hoặc hoàn tác tức thì nếu xảy ra hồi quy. |

---

## Lệnh CLI kiểm chứng sức khỏe plugin (0 token)

```bash
# 1. Đo bảng điểm sức khỏe: overhead token, latency MCP, Recall commit replay, telemetry
.venv/Scripts/python.exe -m engine.cli plugin-health --repo .

# 2. Chạy bộ Micro-Eval 3 kịch bản end-to-end (0 token, < 3 giây)
.venv/Scripts/python.exe -m engine.cli plugin-eval

# 3. Xem thống kê telemetry dùng thật (lượt gọi, độ trễ, token đầu ra)
.venv/Scripts/python.exe -m engine.cli plugin-stats --repo .

# 4. Chạy toàn bộ unit & integration tests
.venv/Scripts/pytest.exe -q
```
