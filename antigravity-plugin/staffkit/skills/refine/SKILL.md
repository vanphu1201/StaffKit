---
name: refine
description: Deep-intent critique and above-and-beyond refinement loop when the agent finished coding but the user feels the result isn't quite right or doesn't match their vision. Observes the real current output first, drills into the gap between current vs desired outcome, surfaces blind spots and upgrades the user forgot to mention, confirms understanding via Echo-Back, then upgrades and proves the improved real output.
---

# Refine (Deep-Intent Critique & Above-and-Beyond Polish Loop)

Use when code "runs without errors" yet the user tests it and feels **"chưa đúng ý / chưa đã / còn thiếu gì đó"** — and needs help articulating what feels off plus discovering blind spots that elevate the result beyond expectations.

## Phase 1: Observe Reality First (NEVER Guess or Blindly Rewrite)

1. Call `load_project_memory(repo_path)` and `locate_context(query, repo_path)` (0-token).
2. **Experience the current product yourself before asking questions:**
   - Run the actual feature with real input and inspect the real output (or inspect the live UI via `browser` subagent / examine user screenshots).
   - Pinpoint *concretely* what the current implementation produces right now.

## Phase 2: Deep-Intent Elicitation & Blind-Spot Discovery (DO NOT Code Yet)

1. **Drill into the Gap (2–4 sharp questions in plain Vietnamese):**
   - Don't ask generic *"bạn muốn sửa gì?"* — compare **what it currently does** vs **specific concrete alternatives** (e.g., *"Hiện tại nó trả ra kiểu A; bạn muốn nó đổi sang kiểu B (ngắn gọn hơn) hay kiểu C (chi tiết kèm bảng)?"*).
2. **Surface 3–5 Blind Spots & Above-and-Beyond Upgrades:**
   - Proactively point out **subtle flaws, missing edge cases, or delight upgrades** the user hasn't noticed or forgot to list (e.g., better error recovery, smarter defaults, speed/caching, clearer visual hierarchy, missing keyboard/export/history convenience), explaining the real user value of each so they can tick what they want.
3. **Echo-Back Expected Output Contract (Wait for Approval):**
   - Summarize clearly in plain Vietnamese:
     - **Điểm chưa ưng hiện tại → Kết quả mới chính xác sẽ trông như thế nào (Trước vs Sau)**
     - **Các điểm nâng tầm bổ sung đã chọn**
     - **Ràng buộc không được làm vỡ (Working Core Flows)**
   - **STOP and ask the user to confirm** (*"Mình hiểu đúng ý bạn rồi chứ? Có muốn chỉnh thêm gì trước khi mình sửa không?"*).

## Phase 3: Safe Upgrade & Self-Run Real-Output Proof

1. Call `snapshot_or_undo(action="snapshot", message="before refine", repo_path=...)`.
2. Implement the agreed upgrades cleanly without breaking any existing working core flows.
3. **Self-Run & Before/After Proof:**
   - Run `verify_and_smoke(repo_path=...)`.
   - Execute the actual feature with real input (or inspect live UI in `browser` subagent) and **verify the real output genuinely matches every single item in the Expected Output Contract**.
   - If a core flow regressed, rollback via `snapshot_or_undo(action="undo", repo_path=...)`.
4. Call `record_lesson(..., touched_files=[...])` if new quality standards or pitfalls were discovered, and report **Trước vs Sau + Bằng chứng kết quả chạy thật**.
