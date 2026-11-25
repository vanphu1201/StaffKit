---
name: ui-craft
description: One-command high-craft UI/UX design, redesign, and frontend polish pipeline. Intelligently orchestrates installed Antigravity design skills (taste-skill, ui-ux-pro-max, design-system, google-design-md, ui-styling, emil-design-eng, impeccable) without prompt bloat, protects backend/core flows with StaffKit snapshots & verification, and requires real Browser Visual QA on Desktop & Mobile before delivery.
---

# UI-Craft (Orchestrated High-Craft Frontend & Visual QA Pipeline)

Produce sharp, intentional, non-slop user interfaces in **one command** by selectively orchestrating installed design skills across 4 disciplined phases — **NEVER** load all design skills at once (which causes token bloat and conflicting styles).

## Phase 1: Context, Safety Net & Selective Skill Routing

1. Call `load_project_memory(repo_path)` and `locate_context(query, repo_path)` (0-token) to inspect the existing frontend stack (Tailwind, shadcn/ui, CSS modules, Vite, Next.js, etc.), existing tokens/themes, and core backend API contracts.
2. Call `snapshot_or_undo(action="snapshot", message="before ui-craft", repo_path=...)` so working backend/core flows are 100% protected.
3. **Select ONLY 2–3 relevant skills from your Available Skills list** (if a named skill is not installed on this machine, follow the built-in craft rules below — never fail):
   - **Landing / Marketing / Editorial site:** Read `taste-skill` (anti-slop art direction) + `google-design-md` (or `design-system`) to lock a concise `DESIGN.md` (palette, typography, spacing scale, visual hierarchy, restraint).
   - **Web App / Dashboard / SaaS / Internal Tool:** Read `ui-ux-pro-max` + `design-system` (or `google-design-md`) to lock `DESIGN.md` (clear information density, crisp contrast, scannable layout, zero decorative clutter).
   - **Redesigning / Fixing an existing ugly UI:** Read `impeccable` first to critique the current layout and identify the highest-leverage visual fixes before touching code.
4. Keep `DESIGN.md` concise (palette with 1 primary accent + neutral scale, max 2 font families, explicit spacing/radius rules) and save/update it in the repo if the project doesn't already have a design contract.

## Phase 2: Clean Component Implementation (Zero Core Regression)

1. If the project uses Tailwind / React / shadcn/ui and `ui-styling` is available in your skills list, read `ui-styling` before writing components.
2. Build with production frontend discipline:
   - **Anti-Slop Guardrails:** Avoid generic purple/neon gradients, noisy glowing cards, redundant icons on every label, or wall-of-boxes layouts. Prioritize typography, whitespace, alignment, and clear primary vs secondary actions.
   - **Complete States:** Always implement clean **loading, empty, error, and responsive (mobile + desktop)** states.
   - **Zero Backend/Core Regressions:** Do NOT alter working API contracts, auth/headers, or core business logic while restyling UI.

## Phase 3: Interaction Polish & Restraint

1. If `emil-design-eng` is available (for new UI polish) or `impeccable` (for redesign critique), read it briefly and apply targeted polish:
   - Crisp hover/focus-visible/active states, smooth purposeful transitions (`150–250ms` ease-out, zero distracting bounce), keyboard accessibility, and consistent alignment.

## Phase 4: Real Verification & Mandatory Browser Visual QA

1. Run `verify_and_smoke(repo_path=...)` to confirm clean build/typecheck, Diff Sanity PASS, and zero regressions on `core_smoke_commands` / backend endpoints.
2. **Mandatory Live Visual Inspection:** Open the live app via Antigravity IDE's `browser` subagent and visually inspect **both Desktop (~1440px) and Mobile (~390px)** viewports:
   - Check console for JS/network errors.
   - Critique the actual rendered screenshots honestly (broken alignment, clipped text, low contrast, cramped spacing, awkward scrollbars, dead buttons).
   - Fix any visual defect immediately and re-verify before reporting done.
3. Record key design/frontend conventions via `record_lesson(..., touched_files=[...])` if new shared UI primitives or tokens were introduced.
