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
2. **Breadth-Then-Depth Drill-Down (NEVER Ask Only Shallow 1-Shot Questions):**
   - **Round 1 (Breadth — Frame the System):** Ask 3–5 concise questions to map the overall goal, target users, core inputs/outputs, and constraints.
   - **Round 2+ (Depth — Drill 2–3 Layers Deep into Critical Branches):** Do NOT stop after broad 1-line answers. Pick the **2–3 most critical or ambiguous branches** (e.g., core business logic, real-world output quality bar, edge cases, external integrations) and **drill down 2–3 layers inside each branch** using concrete examples:
     - *"Với nhánh X bạn vừa chọn, nếu gặp đầu vào thực tế kiểu Y thì kết quả chính xác phải trông như thế nào? Tiêu chuẩn nào để bạn nhìn vào thấy 'đạt' và 'ưng ý'?"*
   - Keep drilling branch-by-branch until both breadth AND depth are crystal clear.
3. **Proactive Value-Add Suggestions:**
   - Always propose **3–5 smart features, edge-case safeguards, or UX delights** the user might have forgotten (e.g., auto-save, live health check, emergency stop / confirmation gates for risky actions, retry/recovery, output preview), clearly explaining the real-world benefit in plain Vietnamese so the user can tick what they want.

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
4. **Echo-Back Confirm Gate (User Verifies AI Truly Understood Before Coding):**
   - Before writing a single line of code, restate your understanding in **plain, jargon-free Vietnamese**:
     - **Đầu vào thực tế → Hệ thống làm gì → Kết quả đầu ra cụ thể người dùng sẽ thấy**
     - **Các giả định (Assumptions) bạn đang tự suy ra** (so the user can immediately catch any misunderstanding!)
     - **Chosen architecture option, milestone plan & estimated Clarity Score (%)**
   - Explicitly let the user choose:
     - **Chỉnh/bổ sung hoặc đào sâu thêm (Vòng tiếp theo)** — fix misunderstandings or drill deeper into open gaps.
     - **AI đã hiểu đúng ý — Chốt đặc tả & Bắt đầu xây dựng (từ Chặng 1)** — proceed to Phase 2 ONLY when the user confirms!

## Phase 2: Lock Spec, Constitution & Contract (Zero Drift)

Once the user confirms they are ready:
1. Synthesize concisely:
   - **Project Constitution (`constitution.md`, 15–25 lines):** Immutable principles (architecture, real data/no mocks, UX/error clarity, OS/runtime compatibility).
   - **Architecture & Interface Contract (`contracts.md`, <= 35 lines):** Shared schemas, API signatures, module boundaries.
   - **Acceptance Criteria (`AC-1..AC-N`):** Concrete, testable real-world outcomes.
   - **Architecture Decisions (`decisions.md`, ADR):** Concise record of why the chosen Phase 1.5 approach won over rejected alternatives and key trade-offs.
   - **OpenSpec PRD (`openspec/prd.md`):** Clean implementation blueprint.
2. Persist all of them with one MCP call:
   `save_project_spec(repo_path=..., constitution_md=..., contracts_md=..., decisions_md=..., prd_md=..., acceptance_criteria=[...])`.

## Phase 3: Native Antigravity IDE Execution & Behavioral Reality Verification

1. Create a clean safety checkpoint if modifying an existing repo: `snapshot_or_undo(action="snapshot", repo_path=...)`.
2. Build the product natively using Antigravity IDE's full capabilities (parallel edits, real dependency installation, live testing, Browser UI inspection when applicable).
3. **Self-Run & Real-Output Proof Gate (NEVER Declare Done from Build/Unit Tests Alone):**
   - Run independent 0-token verification: `verify_and_smoke(repo_path=...)`.
   - **Act as a real user and actually run the feature with real input** (real CLI invocation, real API payload, real file/media output check, or live click-through in `browser` subagent for Web Apps).
   - **Inspect the actual produced artifact/output quality** against the user's agreed criteria — if the output is empty, broken, ugly, or misses the user's intent, fix it BEFORE reporting done!
4. Record any non-trivial runtime/environment discoveries and refresh module signatures via `record_lesson(title, summary, repo_path=..., touched_files=[...])`.
5. Deliver a clear plain-Vietnamese summary including **concrete proof of real output** (sample output / verified artifact / UI check) and exact run instructions.
