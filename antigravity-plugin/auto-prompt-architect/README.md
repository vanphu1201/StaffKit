# Auto-Prompt Architect v2.0 (Antigravity IDE Native Plugin)

Biến mọi ý tưởng mơ hồ thành sản phẩm hoàn chỉnh, gỡ lỗi tận gốc không báo PASS giả, và thẩm định / mở khóa nâng cấp dự án **ngay bên trong Antigravity IDE** — kết hợp 100% sức mạnh gốc của Antigravity IDE với bộ 6 công cụ MCP 0-token.

## Thành phần đi kèm
- **MCP Server (`auto-prompt-architect`, 6 công cụ 0-token):**
  - `load_project_memory` — Nạp Hiến pháp, Hợp đồng kiến trúc, Bài học dự án, Handoff và Cross-Project Global Playbook.
  - `locate_context` — Khoanh vùng file + 1-hop Import/Export Dependency Graph + cửa sổ AST chính xác (hỗ trợ cả tiếng Việt).
  - `save_project_spec` — Lưu `.auto-engine/constitution.md`, `contracts.md`, `handoff.md`.
  - `record_lesson` — Ghi nhớ bài học gốc (tùy chọn lưu vào Cross-Project Global Playbook `~/.gemini/auto-prompt-playbooks/global_lessons.md`).
  - `verify_and_smoke` — Chạy kiểm thử + kiểm chứng hành vi runtime thực tế (`http_smoke`).
  - `snapshot_or_undo` — Tạo điểm lưu Git an toàn / hoàn tác tức thì.
- **3 Skills kích hoạt theo ngữ cảnh:**
  - `brainstorm-to-product` — Phỏng vấn đa vòng linh hoạt + Gợi ý chủ động + Cổng chốt đủ ý + Hiến pháp & Hợp đồng dự án.
  - `deep-fix` — Gỡ lỗi tận gốc 4 bước + Kiểm chứng hành vi thực tế + Ghi nhớ bài học.
  - `audit-and-evolve` — Thẩm định sức khỏe dự án 5 chiều trung thực + Bắt lỗi ngầm + Lộ trình nâng cấp P0/P1/P2 + Gỡ kẹt tính năng.
- **Always-On Rules (`rules/AGENTS.md`):** Siêu gọn (~185 token), tổng overhead luôn bật `< 1,000 token/lượt`.
