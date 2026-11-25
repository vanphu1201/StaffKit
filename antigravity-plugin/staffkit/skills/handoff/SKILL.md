---
name: handoff
description: Save a compact, high-signal session handoff (~300-500 tokens) into `.auto-engine/session_handoff.md` so the user can start a brand-new, zero-bloat Antigravity chat without losing any context, progress, decisions, or next steps.
---

# Session Handoff (Zero-Waste Fresh Chat Continuity)

When the current conversation grows long or finishes a milestone, distill the active working state into a crisp **300–500 token** handoff so the user can open a brand-new chat (resetting history tokens to 0) and resume seamlessly in 1 turn.

## Step 1: Distill Active State (Keep It Dense & Actionable)

Synthesize ONLY what the next fresh chat actually needs (no chat fluff, no old dead ends):
1. **Mục tiêu hiện tại (Current Goal):** 1–2 sentences on what we are building or fixing right now.
2. **Đã hoàn thành & đã kiểm chứng (Done & Verified):** Concrete changes, commits, and passing checks.
3. **Đang dở / Ràng buộc quan trọng (Open State & Constraints):** Unfinished work, active bug/error trace, or critical invariants that MUST NOT break.
4. **File trọng tâm (Key Files):** Exact relative paths of the 3–8 most relevant files.
5. **Bước tiếp theo ngay lập tức (Immediate Next Step):** Exact first action the new chat should execute.

## Step 2: Persist via `save_project_spec`

1. Call `save_project_spec(repo_path=..., session_handoff_md=...)` to write `.auto-engine/session_handoff.md`.
2. If any non-trivial root-cause lesson or architectural decision was finalized in this session, also pass `decisions_md=...` or call `record_lesson(...)` so it persists permanently.

## Step 3: Give the User a 1-Line Resume Prompt

Reply concisely confirming `.auto-engine/session_handoff.md` is saved, and give the user a 1-line prompt to paste into a brand-new chat:
> `Mở chat mới và nhắn: "Tiếp tục từ session handoff hiện tại"`
> *(Khung chat mới sẽ tự gọi `load_project_memory` để nạp ngay `session_handoff.md` với ~500 token thay vì gánh hàng chục nghìn token lịch sử cũ.)*
