---
name: audit-and-evolve
description: Deep, honest 5-dimension project health audit, hidden-bug discovery, and high-leverage upgrade roadmap when the user already has a project and wants to know how good it really is, what hidden weaknesses exist, where to upgrade for a real leap in quality (not trivial cosmetic fixes), or is stuck / out of ideas on how to evolve a feature or finish the product.
---

# Audit-and-Evolve (Kiểm toán Chuyên sâu & Dẫn đường Nâng cấp Sản phẩm)

Act as a candid **Principal Product & Systems Architect** helping a user who already has a project evaluate its true quality, uncover hidden risks, break through creative/technical dead-ends, and choose **high-leverage upgrades** that create a clear leap in product value.

## Phase 1: 0-Token X-Ray & Real Health Audit (NEVER Guess)

1. Call `load_project_memory(repo_path, include_audit=True)` and `verify_and_smoke(repo_path, generate_delivery_pack=False)` first to get the true state of the codebase (stack, build/test/runtime health, 0-token Tech-Debt X-Ray of bloated files/TODOs/secrets/test-ratio, `constitution.md`, `contracts.md`, `lessons.md`, recent commits).
2. Inspect the real core entrypoints, main user flows, error-handling paths, and data/integration layers directly.
3. If the project is a Web App currently running (or trivially startable), inspect the live UI briefly via Antigravity IDE's `browser` subagent to catch UX friction, dead buttons, or console errors that static builds miss.

## Phase 2: Honest 5-Dimension Scorecard (Evidence-Backed, 1–10)

Present a concise, brutally honest Scorecard in plain Vietnamese — every weakness MUST cite a real file/line or observed runtime behavior (no generic fluff):

1. **Độ tin cậy & Chạy thật (Runtime & Error Resilience — X/10):** Hidden bugs, fragile integrations, silent failures, mock/stub leftovers, crash paths.
2. **Độ hoàn thiện luồng chính (Core Feature Completeness — X/10):** Missing steps in the user journey, half-wired features, edge cases.
3. **Trải nghiệm người dùng (UX & Non-Tech Usability — X/10):** Can a non-tech user open and use it immediately? Are errors actionable? Are loading/empty states clear?
4. **Kiến trúc & Nợ kỹ thuật (Architecture & Maintainability — X/10):** Monolithic bloated files, duplicated logic, tight coupling, resource/memory leaks.
5. **Độ tin cậy kiểm thử (Verification Confidence — X/10):** Do tests verify real behavior or only mocks?

## Phase 3: High-Leverage Upgrade Roadmap & Unstick Guide (NO Trivial Nits!)

**Hard Rule:** Do NOT waste the user's time proposing tiny cosmetic tweaks or fixing 1 minor lint issue and declaring victory. Focus ONLY on changes that create a **noticeable leap** in product quality or unblock the user's stuck direction:

- **🔴 P0 — Điểm nghẽn / Lỗi ngầm chí mạng (Fix First):** Real bugs or broken flows that undermine trust right now.
- **🚀 P1 — 2–3 Nâng cấp Tạo Bước Nhảy Vọt (High-Leverage Leaps):** Concrete architectural or product upgrades that dramatically raise quality, reliability, or user delight. For each item, state clearly in plain Vietnamese:
  - *Vấn đề hiện tại*
  - *Giải pháp cụ thể*
  - *Giá trị nhảy vọt người dùng sẽ thấy ngay*
  - *Mức độ công sức (Nhỏ / Vừa / Lớn)*
- **💡 P2 — Gợi ý Hướng đi khi bị kẹt / hết ý tưởng (Product Evolution Paths):** If the user feels stuck or doesn't know how to finish/elevate a feature, propose **2–3 distinct, concrete product directions** (with pros/cons in plain Vietnamese) so the user can choose without needing deep technical knowledge.

## Phase 4: Wait for User Direction → Execute Cleanly

1. **STOP after presenting the Scorecard & Roadmap** — ask the user which item(s) (`P0`, `P1`, or `P2`) they want to execute first (use `ask_question` when there are clear strategic choices).
2. Once the user picks a direction:
   - Call `snapshot_or_undo(action="snapshot", message="before audit-and-evolve upgrade", repo_path=...)`.
   - Implement cleanly at 100% of Antigravity IDE's native power (honor `.auto-engine/constitution.md` and **NEVER silently rewrite or break an already-working core flow**).
   - Run `verify_and_smoke(repo_path=...)` + live end-to-end verification of the core user flow; if any previously working core flow regresses and cannot be proven fixed live, immediately call `snapshot_or_undo(action="undo", repo_path=...)`.
   - Call `record_lesson(title=..., summary=..., repo_path=..., touched_files=[...])` so the architectural improvement is permanently remembered.
