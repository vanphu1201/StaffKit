---
name: decompose
description: Lossless decomposition and milestone-by-milestone execution for huge, long, or multi-part requests that would otherwise suffer from LLM context rot / missed middle requirements in a single turn. Extracts a complete requirement checklist, confirms understanding via Echo-Back, executes one verified milestone at a time with real-output checks, and persists progress into `.auto-engine/session_handoff.md` for zero-loss continuation across fresh chats.
---

# Decompose (Lossless Chunked Execution for Large & Complex Requests)

When a user gives a very long, dense, or multi-part prompt, **NEVER try to code everything in one chaotic pass** (which causes missed middle requirements, sloppy tail code, and cross-feature regressions).

## Phase 1: Lossless Requirement Inventory & Echo-Back Confirm

1. Call `load_project_memory(repo_path)` and `locate_context(query, repo_path)` (0-token).
2. **Extract Every Single Requirement (Zero Dropped Details):**
   - Break the user's long prompt into a numbered **Requirement Inventory (`R1, R2, ... Rn`)** in plain Vietnamese so the user can verify **not a single detail was missed or misunderstood**.
   - Explicitly list any **Assumptions / Ambiguities** that need a quick answer.
3. **Group into 2–5 Independent, Verifiable Milestones (`M1..Mk`):**
   - Order milestones by dependency (Core/Data first → Business Logic → UI/Integration → Polish).
   - For EACH milestone, define:
     - *Yêu cầu bao phủ:* (`R1, R2...`)
     - *Kết quả chạy thật chứng minh hoàn thành (Real-Output Proof):* Concrete live check, not just `build PASS`.
4. **Echo-Back Confirm Gate:** Present the inventory + milestone plan and **wait for user confirmation** before coding `M1`.

## Phase 2: Execute Strictly One Milestone at a Time

For the current milestone (`Mi`):
1. Call `snapshot_or_undo(action="snapshot", message="before milestone Mi", repo_path=...)`.
2. Implement ONLY `Mi`'s scope cleanly without touching unrelated future milestones.
3. **Self-Run Real-Output Proof for `Mi`:**
   - Run `verify_and_smoke(repo_path=...)` AND actually execute `Mi` with real input to verify its real output quality + zero regression on `M1..M(i-1)`.
4. **Checkpoint Progress to Disk:**
   - Call `save_project_spec(repo_path=..., session_handoff_md=...)` recording: completed milestones (`M1..Mi`), verified evidence, key files, and exact next milestone (`M(i+1)`).
5. Report `Mi`'s real-output proof concisely to the user.

## Phase 3: Fresh-Chat Continuity (Beat Context Rot)

- After completing 1–2 heavy milestones (or whenever the conversation grows long), proactively tell the user:
  - Progress is safely saved in `.auto-engine/session_handoff.md`.
  - They can either **continue `M(i+1)` here** OR **open a brand-new chat** (0 token bloat) and say *"Tiếp tục từ session handoff (Chặng M(i+1))"* with zero context loss.
