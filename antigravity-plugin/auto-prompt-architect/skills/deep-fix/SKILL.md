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

## Step 2: Reproduce & Probe Real Runtime (NEVER Guess)
1. Examine any user-provided screenshots, logs, or error traces directly.
2. **Reproduce the failure against the real runtime:** probe the actual local port/API/process, inspect the actual HTTP status/payload, or inspect the live UI/console.
3. Isolate the true root cause before editing any code.
4. **Hard Rule:** NEVER "fix" a broken integration or feature by merely rewording an error message or writing mock tests that assert your wrong guess.

## Step 3: Surgical Fix & Behavioral Reality Verification
1. Apply a clean, minimal root-cause fix while honoring `.auto-engine/constitution.md` and preserving all existing tests/invariants.
2. **Prove the fix works live:** re-run the real runtime probe / user flow and confirm real success (e.g., HTTP 200 with real output, UI works in browser, CLI succeeds).
3. Run `verify_and_smoke(repo_path=...)` for independent build/test/runtime smoke verification.

## Step 4: Compound Knowledge (Zero Future Regression)
1. Call `record_lesson(title=..., summary=..., repo_path=...)` capturing the exact root cause and runtime contract discovered so future sessions never re-introduce the bug.
2. Report concisely in Vietnamese: **(1) Nguyên nhân gốc đã xác minh**, **(2) Bằng chứng chạy thật sau khi sửa**, **(3) Các file đã thay đổi**.
