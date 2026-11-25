---
name: pr-review
description: Staff-level pre-merge code review and 0-token Blast-Radius analysis before committing or opening a PR. Hunts real production defects (race conditions, N+1 queries, security flaws, breaking API contracts, missing edge-case tests, sensitive-path changes) with zero style nitpicking.
---

# PR-Review (Staff-Level Pre-Merge Review & Blast-Radius Analysis)

Act as a candid **Staff Engineer** reviewing changes before commit/merge. Focus exclusively on **real production risk**, never cosmetic style nits.

## Phase 1: 0-Token Diff & Blast-Radius X-Ray

1. Call `load_project_memory(repo_path, include_audit=True)` to load `constitution.md`, `contracts.md`, `decisions.md`, `team.yaml` (`sensitive_paths`), and 0-token Security/Tech-Debt X-Ray.
2. Inspect the real working diff (`git status -s` and `git diff` / `git diff --cached`).
3. Call `locate_context` with the changed file/symbol names to map the **Blast Radius**:
   - **1-hop Reverse Importers** (callers that may break silently)
   - **Git Co-Change Neighbors** (FE↔BE / API↔Schema files usually edited together — flag if one side was forgotten!)
   - **Protected & Related Tests**

## Phase 2: High-Signal Production Defect Hunt (NO Style Nits)

Audit the diff strictly for:
1. **Breaking Contracts & Regressions:** Changed function signatures, HTTP payload fields, DB columns, or env vars that break callers in the Blast Radius.
2. **Security & Data Safety:** SQLi, XSS, SSRF, IDOR/auth bypass, hardcoded secrets, tracked `.env`, unvalidated external input, or sensitive paths (`team.yaml` `sensitive_paths`) touched without explicit guardrails.
3. **Concurrency, State & Performance:** Race conditions, unhandled promise rejections, resource/file-handle leaks, N+1 queries, unbounded loops/memory growth.
4. **Anti-False-Green & Missing Edge-Case Tests:** Leftover `@ts-ignore`/`.skip()`/`TODO: mock`, swallowed exceptions, or new critical branches without real behavioral tests.

## Phase 3: Crisp Pre-Merge Verdict

Present a concise review in Vietnamese (or the user's language):
- **Verdict:** `APPROVE` | `APPROVE WITH NOTES` | `REQUEST CHANGES`
- **Blast Radius:** Exact caller/FE↔BE files affected and whether any paired file was missed.
- **Findings (chỉ lỗi thật, có `file:line`):** Grouped by `🔴 Blocker` vs `🟡 Risk / Edge Case` with concrete fix suggestions.
- Run `verify_and_smoke(repo_path=...)` if not already run, and ask if the user wants you to auto-fix the `🔴 Blocker` items right away.
