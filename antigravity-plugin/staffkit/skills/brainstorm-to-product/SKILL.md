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
2. **Dynamic Answer-Driven Interview + 10-Dimension Coverage Radar (NEVER Dump a Static Question List):**
   - **No rigid checklist:** Do NOT throw 8–10 generic pre-baked questions at the user at once.
   - **Internal 10-Dimension Radar:** Quietly track coverage (`Clear / Partial / Missing`) across: *Scope & Out-of-Scope, Data Model/State, UX & Error/Empty States, Perf/Security/Privacy, External Integrations, Edge Cases & Concurrency, Constraints/Trade-offs, Terminology, Testable Completion Signals, and Vague Adjectives* (flag unmeasured words like "nhanh / đẹp / mạnh / ổn định" and turn them into concrete criteria).
   - **Generate each round dynamically (2–4 sharp questions per round, prioritized by `Impact × Uncertainty`)** from **the user's previous answers + your repo & domain expertise**:
     - **Follow & Deepen Their Direction:** Drill 2–3 layers deeper into *their* exact path (concrete real inputs, edge cases, what a "great" real output looks like).
     - **Broaden Their Horizon Simultaneously:** Weave in **1–2 widening questions** from unresolved Radar dimensions so nothing critical is missed.
     - **Question Quality Rule:** Each question must be a real, plain-Vietnamese question (never a cryptic label), paired with **1 concise "Tại sao quan trọng" (*Why it matters*) line + 1 Recommended Option** when offering choices.
   - Continue adaptive rounds until both **depth on the chosen direction** and **breadth across high-impact Radar dimensions** are solid.
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

## Phase 3: Auto-Decompose Gate & Native Antigravity IDE Execution

0. **Automatic Complexity & Risk Check (Auto-Trigger `decompose` When Needed):**
   - Before coding, assess whether implementing the locked spec in a single chat risks subtle bugs, dropped requirements, or cross-layer regressions (e.g., large multi-part feature, FE+BE+DB/API/OS changes, or > 4–5 interacting files).
   - **If Large / High-Risk:** **AUTOMATICALLY switch into the `decompose` workflow** (follow `decompose/SKILL.md`: build lossless Requirement Inventory `R1..Rn`, split into independent Milestones `M1..Mk` with Real-Output Proof per milestone, execute one milestone at a time, and save `.auto-engine/session_handoff.md` after each milestone so the user can continue in a fresh chat with zero context loss).
   - **If Small / Focused:** Proceed directly with Steps 1–5 below.
1. Create a clean safety checkpoint if modifying an existing repo: `snapshot_or_undo(action="snapshot", repo_path=...)`.
2. Build the product natively using Antigravity IDE's full capabilities (parallel edits, real dependency installation, live testing, Browser UI inspection when applicable).
3. **Convergence Audit + Self-Run Real-Output Proof Gate (NEVER Declare Done from Checkboxes or Unit Tests Alone):**
   - **Spec ↔ Code Convergence Check:** Re-read `constitution.md`, `contracts.md`, and every `AC-1..AC-N` against the actual code on disk — **completion claims/checkboxes are NOT evidence**. Classify any gap as `missing`, `partial`, or `contradicts`, fix all gaps, and repeat until **`Converged`**.
   - Run independent 0-token verification: `verify_and_smoke(repo_path=...)`.
   - **Act as a real user and actually run the feature with real input** (real CLI invocation, real API payload, real file/media output check, or live click-through in `browser` subagent for Web Apps).
   - **Inspect the actual produced artifact/output quality** against the user's agreed criteria — if the output is empty, broken, ugly, or misses the user's intent, fix it BEFORE reporting done!
4. Record any non-trivial runtime/environment discoveries and refresh module signatures via `record_lesson(title, summary, repo_path=..., touched_files=[...])`.
5. Deliver a clear plain-Vietnamese summary including **concrete proof of real output** (sample output / verified artifact / UI check) and exact run instructions.
