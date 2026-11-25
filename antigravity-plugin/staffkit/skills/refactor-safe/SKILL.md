---
name: refactor-safe
description: Zero-behavior-change Strangler Refactoring for legacy or bloated modules. Locks observable behavior with Characterization Tests first, takes a Git safety snapshot, refactors in small atomic verified steps, and preserves rollback safety at every step.
---

# Refactor-Safe (Zero-Behavior-Change Strangler Refactoring)

Refactor bloated, tightly coupled, or legacy code cleanly **without breaking existing production behavior**.

## Phase 1: Map Callers & Lock Safety Net

1. Call `load_project_memory(repo_path)` and `locate_context(query, repo_path)` to map every caller (1-hop reverse importers + Git Co-Change neighbors) and existing tests.
2. Call `snapshot_or_undo(action="snapshot", message="before refactor-safe", repo_path=...)` so the entire repo can be restored in 1 step.
3. Read `contracts.md` and `decisions.md` (ADR) so you never accidentally undo an intentional architectural decision.

## Phase 2: Characterization Tests First (NEVER Refactor Blind)

1. Run existing tests via `verify_and_smoke(repo_path=..., run_smoke=False)` to confirm a clean green baseline.
2. If the target module lacks test coverage for its current public behavior/edge cases, **write Characterization Tests FIRST** that lock the current observable outputs — do NOT change production code until those tests pass!

## Phase 3: Atomic Strangler Steps

1. Decompose the refactor into **2–4 small, reversible steps** (e.g., *Step 1: Extract cohesive sub-module + keep backward-compatible facade* → *Step 2: Migrate callers* → *Step 3: Remove dead code*).
2. Execute **one atomic step at a time** and run `verify_and_smoke(repo_path=...)` after each step:
   - If tests/diff-sanity/smoke fail and cannot be fixed cleanly, immediately call `snapshot_or_undo(action="undo", repo_path=...)` rather than piling hacks on top.
3. Never mix unrelated feature changes into a pure refactor step.

## Phase 4: Persist Architectural Decision & Summary

1. Call `save_project_spec(repo_path=..., decisions_md=...)` (if a structural boundary changed) and `record_lesson(..., touched_files=[...])` to refresh `.auto-engine/handoff.md` signatures.
2. Report concisely: **(1) Cấu trúc trước/sau**, **(2) Bằng chứng Characterization Tests & `verify_and_smoke` PASS**, **(3) Các file đã tách/cập nhật**.
