# Auto-Prompt Architect — Always-On Exoskeleton Rules

When working on any software project in Antigravity IDE:

1. **Load Project Memory First (0-Token Head-Start):** If `.auto-engine/` exists in the workspace (or when starting work on an existing repo), call `load_project_memory(repo_path)` and `locate_context(query, repo_path)` before scanning files blindly. Always honor `.auto-engine/constitution.md`, `.auto-engine/contracts.md`, and `.auto-engine/lessons.md`.
2. **Never Build on Vague Requirements:** When the user gives an open-ended or vague product idea, activate the `brainstorm-to-product` skill — elicit requirements across adaptive rounds with proactive suggestions and wait for explicit user confirmation before coding.
3. **Honest Audit & Strategic Guidance When Stuck:** When the user asks how good their project is, what weaknesses/hidden bugs exist, what high-leverage upgrades to make, or feels stuck/out of ideas, activate the `audit-and-evolve` skill — deliver an evidence-backed 5-dimension scorecard and P0/P1/P2 roadmap before editing code.
4. **Behavioral Reality Check (No False-Green Passes):** Build, typecheck, and mock unit tests are necessary but NOT sufficient. Always verify the actual end-to-end runtime behavior (live endpoint probe, browser check, or real command execution) before declaring a task complete. Never "fix" a broken feature by merely rewording an error message or writing mocks for a wrong assumption.
5. **Preserve & Compound Knowledge:** Before risky multi-file refactors, call `snapshot_or_undo(action="snapshot")`. After solving a non-trivial root-cause bug or runtime quirk, call `record_lesson(title, summary, touched_files=[...])` so future sessions never regress.
