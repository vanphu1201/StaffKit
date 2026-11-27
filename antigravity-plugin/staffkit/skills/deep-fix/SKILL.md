---
name: deep-fix
description: Systematic root-cause debugging and zero-regression feature fixes powered by 0-token Project Memory, AST Context Locator, Behavioral Reality Verification, and Lessons Learned persistence. Use whenever the user reports a bug, failed integration, broken UI, or wants to fix/upgrade an existing project cleanly on the first try.
---

# Deep-Fix (Systematic Root-Cause Debugging + Behavioral Reality Check)

Solve bugs and upgrades cleanly on the first try with zero blind guessing and zero false-green passes.

## Step 1: 0-Token X-Ray & Safety Snapshot
1. Call `load_project_memory(repo_path)` and `locate_context(query, repo_path)` in parallel to load `constitution.md`, `contracts.md`, `lessons.md`, recent commits, and the most relevant starting files without wasting exploration tokens.
2. Treat `Suggested Starting Points` as a head-start, NOT a boundary — follow real evidence anywhere it leads.
3. Call `snapshot_or_undo(action="snapshot", message="before deep-fix", repo_path=...)` so the user can always undo in 1 click.

## Step 2: Preserve Working Core Flow & Reproduce Real Runtime (NEVER Guess)
1. Examine any user-provided screenshots, logs, or error traces directly.
2. **Baseline Before Edit (Protect Working Core Flows):** Before touching any working core mechanism (e.g., downloader, streaming, auth, bypass, payment), inspect and record *exactly why it currently works* (headers, cookies, endpoints, fallback order). **NEVER silently replace a working mechanism with an unverified "cleaner" rewrite.**
3. **Reproduce against the real runtime:** probe the actual local port/API/process, inspect the actual HTTP status/payload, or inspect the live UI/console.
4. **Hard Rule:** NEVER "fix" a broken integration by merely rewording an error message or writing mock tests that assert your wrong guess.

## Step 3: Surgical Fix, End-to-End Core-Flow Verify & Rollback on Regression
1. Apply a clean, minimal root-cause fix while honoring `.auto-engine/constitution.md` and preserving all existing working core flows.
2. **Self-Run & Real-Output Proof (Prove BOTH the Fix AND Core Flow Produce Good Real Output):**
   - NEVER stop at `build PASS`, unit test green, or `GET / -> 200`.
   - Actually execute the real feature end-to-end with real input and **inspect the actual produced output/artifact/UI** (e.g., verify downloaded video is valid & non-empty, API JSON has exact expected fields, Web UI renders cleanly & button action works in `browser` subagent).
   - Only declare done when the **real output quality** genuinely satisfies the user's expectation.
3. Run `verify_and_smoke(repo_path=...)` for independent diff-sanity, build/test, and `core_smoke_commands` verification.
4. **Auto-Rollback Rule:** If a previously working core flow breaks after your edit and you cannot prove it works live again, immediately run `snapshot_or_undo(action="undo", repo_path=...)` so you NEVER leave the user with a broken core feature.

## Step 4: Compound Knowledge (Zero Future Regression)
1. Call `record_lesson(title=..., summary=..., repo_path=..., touched_files=[...])` capturing the exact root cause and refreshing `.auto-engine/handoff.md` signatures so future sessions never re-introduce the bug.
2. Report concisely in Vietnamese: **(1) Nguyên nhân gốc đã xác minh**, **(2) Bằng chứng chạy thật sau khi sửa**, **(3) Các file đã thay đổi**.
