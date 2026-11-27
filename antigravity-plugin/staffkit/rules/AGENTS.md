# StaffKit Rules

1. **0-Token Memory First:** On existing repos, call `load_project_memory` + `locate_context` before scanning files. Honor `constitution.md`, `contracts.md`, `decisions.md`, `lessons.md`, and `session_handoff.md`.
2. **Echo-Back Confirm Gate:** For non-trivial/ambiguous tasks, restate your understanding in plain user terms (Input -> Behavior -> Expected Output + Assumptions) and wait for user approval before coding.
3. **Right Skill:** Vague/new -> `brainstorm-to-product`; unsatisfied output -> `refine`; huge/multi-part request -> `decompose`; audit/stuck -> `audit-and-evolve`; UI -> `ui-craft`.
4. **Self-Run Real-Output Proof & Zero Regressions:** NEVER declare done from unit tests or build PASS alone — run the real feature with real input, inspect actual output quality, verify core flows didn't regress, and call `snapshot_or_undo("undo")` if a core flow breaks.
5. **Compound Knowledge:** Snapshot before big edits; call `record_lesson(..., touched_files=[...])` after root-cause fixes (`global_playbook=True` for universal rules).
