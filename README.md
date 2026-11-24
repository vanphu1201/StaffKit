# Auto-Prompt Architect v2.0 — Antigravity IDE Native Plugin

> **Bộ khung 0-token gắn trực tiếp vào Antigravity IDE** giúp biến ý tưởng mơ hồ thành sản phẩm chạy thật, gỡ lỗi tận gốc không báo PASS giả, và thẩm định / mở khóa nâng cấp dự án khi bị kẹt.

*(Phiên bản App Studio headless cũ đã được lưu trữ an toàn tại git tag `v1.2-legacy-studio-archive`; nhánh `main` hiện tại tinh gọn 100% cho Antigravity IDE Native Plugin.)*

---

## Cài đặt nhanh (2 bước)

```bash
# 1. Cài package vào virtualenv cục bộ
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Linux/macOS: .venv/bin/python -m pip install -e ".[dev]"

# 2. Cài/đồng bộ plugin vào ~/.gemini/config/plugins/auto-prompt-architect
.venv/Scripts/python.exe -m engine.cli install-plugin
```

Sau đó mở **Plugins Manager** trong Antigravity IDE để kiểm tra **Auto-Prompt Architect** đang ở trạng thái **Enabled** (nếu vừa nâng cấp từ bản cũ, tắt rồi bật lại công tắc 1 lần để nạp MCP server v2.0 mới).

---

## 3 Chế độ làm việc (Skills tự động kích hoạt trong Antigravity IDE)

1. **`brainstorm-to-product` (Ý tưởng mới / Tính năng lớn):**
   - Phỏng vấn đa vòng linh hoạt theo độ phức tạp + Gợi ý chủ động các tính năng thực dụng + Lưu Hiến pháp (`constitution.md`) & Hợp đồng kiến trúc (`contracts.md`) trước khi code.
2. **`deep-fix` (Gỡ lỗi tận gốc / Không báo PASS giả):**
   - Nạp trí nhớ dự án -> Khoanh vùng 0-token bằng 1-hop Import/Export Graph + AST windows -> Snapshot Git an toàn -> Kiểm chứng hành vi thực tế (`verify_and_smoke`) -> Ghi nhớ bài học (`record_lesson`).
3. **`audit-and-evolve` (Thẩm định sức khỏe dự án & Gỡ kẹt nâng cấp):**
   - Chấm điểm 5 chiều trung thực bằng bằng chứng thực tế -> Săn lỗi ngầm -> Đề xuất lộ trình nâng cấp P0/P1/P2 tạo bước nhảy vọt -> Gợi ý hướng đi mới khi người dùng hết ý tưởng.

---

## 6 Công cụ MCP 0-token (`auto-prompt-architect`)

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

# 3. Chạy toàn bộ unit & integration tests
.venv/Scripts/pytest.exe -q
```
