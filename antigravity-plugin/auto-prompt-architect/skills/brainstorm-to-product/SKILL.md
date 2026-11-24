---
name: brainstorm-to-product
description: Adaptive multi-round product discovery, solution-architecture option comparison, feasibility check, phased milestones, Project Constitution, Architecture Contract, and end-to-end verified build directly inside Antigravity IDE. Use whenever the user shares a vague or complex product idea, wants to design a new system from scratch, or wants to brainstorm a major feature with multiple possible approaches before coding.
---

# Brainstorm-to-Product (Adaptive Discovery, Solution Options & Build)

Turn any vague user idea or complex major feature — even when the user doesn't know which technical approach is right — into a crystal-clear, production-ready product built at 100% of Antigravity IDE's native power.

## Phase 1: Adaptive Multi-Round Elicitation (NEVER rush to code)

1. **Inspect Context First:**
   - If the user attached images/mockups/documents, examine them directly first.
   - If working in an existing repository, call `load_project_memory(repo_path)` and `locate_context(query, repo_path)` (0-token) so you understand the existing architecture and never ask about things already built.
2. **Gauge Complexity & Ask Targeted Questions (using `ask_question` or structured plain-Vietnamese Q&A):**
   - **Simple tool (1 screen/script):** 3–4 focused questions.
   - **Standard app:** 5–7 questions grouped by category (*Luồng chính*, *Dữ liệu & Tích hợp thật*, *Trải nghiệm*).
   - **Complex / multi-module / open-ended major feature:** 7–11 deep questions grouped cleanly (*Mục tiêu & Luồng người dùng*, *Dữ liệu & Tích hợp thật*, *An toàn / Quyền / Ngoại lệ*, *UX & Bảo mật*).
3. **Proactive Value-Add Suggestions:**
   - Always propose **3–5 smart features or safeguards** the user might not have thought of (e.g., auto-save, live health check, emergency stop / confirmation gates for risky actions, retry/recovery, quick-start launcher), clearly explaining the real-world benefit in plain Vietnamese so the user can tick what they want.

## Phase 1.5: Solution Architecture Options, Feasibility & Phased Plan (For Standard / Complex Features)

Whenever a feature is large, open-ended, or has multiple valid technical approaches (or the user says they don't know which way is right), **NEVER silently pick one approach and start coding**:

1. **Feasibility & Current Best-Practice Check:**
   - For unfamiliar/fast-moving tech domains (e.g., OS/Computer-Use agents, realtime sync, local AI, mobile/desktop bridges), run a quick `search_web` or check actual host/OS constraints first so recommendations are current and feasible — never guess outdated APIs or ignore OS security/permission limits.
2. **Compare 2–3 Distinct Solution Approaches (Plain Vietnamese):**
   - Present **2–3 genuinely different architectural options** (e.g., *Approach A: Pure Vision/GUI* vs *Approach B: OS Native / API / Shell* vs *Approach C: Hybrid*) in a crisp comparison table covering:
     - *Cách hoạt động* (plain words, no jargon dump)
     - *Ưu điểm chính*
     - *Nhược điểm & Rủi ro ẩn* (latency, token cost, fragility, security/OS limits)
     - *Độ phù hợp với hệ thống hiện tại*
   - Clearly mark **1 Recommended Option** with concrete reasoning, and let the user pick via `ask_question`.
3. **Phased Milestone Breakdown (For Large Features):**
   - Break large features into **2–3 verifiable milestones** (*Chặng 1: Khung lõi chạy thật & an toàn* → *Chặng 2: Khả năng thông minh / nhiều bước* → *Chặng 3: Bảo vệ, tối ưu tốc độ & đồng bộ đa thiết bị*) so the user sees working software early instead of a risky big-bang rewrite.
4. **Clarity Gate (User Decides When It's Enough):**
   - Summarize the locked requirements, chosen approach, and milestone plan; report an estimated **Clarity Score (%)** and any remaining open gaps; and explicitly let the user choose:
     - **Hỏi sâu / So sánh thêm (Vòng tiếp theo)** — drill ONLY into open gaps without repeating answered questions.
     - **Chốt phương án & Tiến hành xây dựng (từ Chặng 1)** — proceed to Phase 2 ONLY when the user confirms!

## Phase 2: Lock Spec, Constitution & Contract (Zero Drift)

Once the user confirms they are ready:
1. Synthesize concisely:
   - **Project Constitution (`constitution.md`, 15–25 lines):** Immutable principles (architecture, real data/no mocks, UX/error clarity, OS/runtime compatibility).
   - **Architecture & Interface Contract (`contracts.md`, <= 35 lines):** Shared schemas, API signatures, module boundaries.
   - **Acceptance Criteria (`AC-1..AC-N`):** Concrete, testable real-world outcomes.
   - **OpenSpec PRD (`openspec/prd.md`):** Clean implementation blueprint.
2. Persist all of them with one MCP call:
   `save_project_spec(repo_path=..., constitution_md=..., contracts_md=..., prd_md=..., acceptance_criteria=[...])`.

## Phase 3: Native Antigravity IDE Execution & Behavioral Reality Verification

1. Create a clean safety checkpoint if modifying an existing repo: `snapshot_or_undo(action="snapshot", repo_path=...)`.
2. Build the product natively using Antigravity IDE's full capabilities (parallel edits, real dependency installation, live testing, Browser UI inspection when applicable).
3. Run independent 0-token verification:
   `verify_and_smoke(repo_path=..., generate_delivery_pack=True)` — confirms build, typecheck, tests, live Web/API server boot & HTTP response, AND generates `CHAY_SAN_PHAM.bat` + `HUONG_DAN_SU_DUNG.md` for non-tech users.
4. **Visual / Browser QA (for Web Apps):** After `verify_and_smoke` passes, if the app has a web UI, open it briefly with Antigravity IDE's `browser` subagent to confirm the UI actually renders cleanly with zero white-screen or console JS errors.
5. Record any non-trivial runtime/environment discoveries and refresh module signatures via `record_lesson(title, summary, repo_path=..., touched_files=[...])`.
6. Deliver a clear plain-Vietnamese summary including **exact run instructions** (`CHAY_SAN_PHAM.bat`) and **what was verified live**.
