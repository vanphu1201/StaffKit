# Auto-Prompt Architect Rules

1. **0-Token Memory First:** On existing repos, call `load_project_memory` + `locate_context` before scanning files. Honor `constitution.md`, `contracts.md`, `lessons.md`, and global playbook lessons.
2. **Vague Idea → `brainstorm-to-product`:** Elicit across adaptive rounds with proactive suggestions; wait for user confirmation before coding.
3. **Audit / Stuck → `audit-and-evolve`:** Deliver an evidence-backed 5D scorecard + P0/P1/P2 high-leverage roadmap before editing.
4. **No False-Green Passes:** Verify real runtime/browser behavior — never "fix" a bug by only rewording errors or mocking wrong guesses.
5. **Compound Knowledge:** Call `snapshot_or_undo("snapshot")` before big refactors; call `record_lesson(..., touched_files=[...])` after solving root-cause issues (`global_playbook=True` for universal lessons).
