# CLAUDE.md

Project-level instructions for Claude Code working on this repo.

## Project context

This is an **AI-powered code review system** — a take-home assessment for a Senior AI Engineer role at decodeorigin. It demonstrates multi-agent code review with semantic memory, observable agent reasoning, and human-in-the-loop refinement.

**Deadline:** May 18, 2026. **Status:** mid-Day-3 of a ~4-working-day window.

**Source brief:** `Gmail_-_Interview_Project_Timeline.pdf`
**Internal design:** `AI_Engineering_Workspace_Design_Doc_v4.docx`
**Working plan:** `PROGRESS.md` — single source of truth for what to do next.

## Communication

- **The user converses in Chinese; all code, comments, and documentation are in English.** Never write Chinese in source files, commit messages, or any deliverable artifact. Conversation in chat is in Chinese.
- The user is a senior engineer. Skip introductory explanations of common concepts. Be direct about tradeoffs and disagree when you have a reason to.

## How to work with PROGRESS.md

`PROGRESS.md` is the authoritative plan. It contains priorities §5–§10, each with user stories (X.1), tasks (X.2), and verifiable test cases (X.3). Always operate from it.

### Session start

At the beginning of every session, before doing anything else:

1. Run `git status` and report what's uncommitted.
2. Read PROGRESS.md §3.3 (uncommitted changes expected) and the cursor line near the top of §4.
3. If working-tree state doesn't match what §3.3 expects, raise that as a problem and ask the user before proceeding. Do not silently "fix" it.

### When the user says "continue", "next", "next task", "下一个", or doesn't specify

1. Read PROGRESS.md. Find the first unchecked `[ ]` task in the lowest-numbered open priority (§5 → §6 → §7 → §8 → §9 → §10 → §11).
2. State: *"Next task is §X.2 item N: `<task description>`. This is part of Priority N (`<priority name>`). Relevant user stories: §X.1. Test cases to satisfy: §X.3 items A, B, C."*
3. **Wait for the user to confirm** ("go" / "yes" / "做吧" / "好") before starting work. Do not auto-start.

### When working on a task

- If PROGRESS.md says "Decision before coding" or "design decision" or similar for this task, **stop and ask the user** before writing code. Do not guess.
- If a task is ambiguous, surface the ambiguity to the user. Do not pick an interpretation silently.
- Make the smallest change that completes the task. Don't refactor adjacent code unless the task asks for it.

### After completing a task

1. Run the relevant §X.3 test cases yourself when possible (syntax checks, unit tests, smoke tests against a temp DB, etc).
2. Report which §X.3 checkboxes can now be ticked.
3. Update PROGRESS.md — tick the completed `[ ]` items in both §X.2 and §X.3.
4. Update the **cursor line** at the top of §4 to point at the next task.
5. **Stop.** Do not auto-continue to the next task. Wait for user confirmation.

### After completing a full Priority

When all of §X.2 and §X.3 are ticked for a Priority:

1. Run a final pass of all §X.3 test cases.
2. Prompt the user to commit. Suggest a commit message like:
   ```
   feat(pN): <short description>

   - <key change 1>
   - <key change 2>
   - All §X.3 test cases pass
   ```
3. After commit, wait for user instruction before starting the next Priority.

## Critical guardrails

### Do not auto-advance

The most important rule. **Every task completion is a hard stop.** Even if the next task looks trivial. The user controls pacing.

### Honesty about test results

- If a §X.3 test case can't be run automatically (e.g., requires live Anthropic API), say so — don't claim it passes.
- If a test fails, report exactly what failed. Don't paper over.
- If you couldn't run a test, mark it explicitly as "not verified" rather than ticking the checkbox.

### Conservative scope (Priority 2 specifically)

PROGRESS.md §4.3 marks Priority 2's "auto-advance engine" as **stretch only**. The conservative version (breakdown + edit + persist, no auto-advance) is the commitment. Do not push toward the stretch version unless the user explicitly says so after seeing the conservative version land.

### File handling

- Read PROGRESS.md §13 (interface contracts) before modifying `agents/base.py`, `database.py`, `memory/vector_store.py`, or `orchestrator/runner.py`. These have established contracts that other code depends on.
- New tables/columns go through `init_db()` in `database.py` — don't write ad-hoc migrations.
- New agents must subclass `BaseAgent` and register in `AGENT_REGISTRY` (search for existing registration pattern in `orchestrator/runner.py`).

### Commit hygiene

- Commit after each Priority finishes, not after each task.
- **Every commit must include any uncommitted `PROGRESS.md` edits.** Cursor updates, §X.2/§X.3 checkbox ticks, and §3.3-style status notes belong with the work they describe — stage `PROGRESS.md` alongside the code in the same commit, never in a separate `docs: update PROGRESS.md` commit. If you forgot and the commit is still local (not pushed), amend it in.
- Commit messages: `feat(pN): ...` for new functionality, `fix(pN): ...` for bug fixes, `docs: ...` for docs-only changes (README, design doc).
- Never commit `.env`, `.ai-workspace/`, `__pycache__/`, or `venv/`. The `.gitignore` should prevent this; if it doesn't, fix `.gitignore` first.

### When stuck

If you've tried something twice and it's not working, **stop and ask the user** rather than trying a third variant. Time is the scarce resource here, not creativity.

## Useful pointers within the repo

- **CLI entry:** `python -m cli.main` opens the interactive shell
- **Shell commands:** `scan`, `review --pr N`, `reflect [TASK-ID]`, `logs [TASK-ID]`, `tasks`, `quit`
- **DB location:** `workspace.db` in repo root (SQLite)
- **ChromaDB location:** `.ai-workspace/chroma_db/`
- **Environment:** `.env` (copy from `.env.example`); needs `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `GITHUB_REPO`, `PETCLINIC_REPO_PATH`

## Quick reference — current state at handover

- **Branch:** `temp-branch`
- **Last commit:** `7e71c64 temp commit`
- **Uncommitted in working tree:** 5 files (see PROGRESS.md §3.3). These are real Day 3 work, not noise.
- **First thing to do in a fresh session:** run PROGRESS.md §12 verification path to confirm uncommitted Day 3 changes work end-to-end against real ChromaDB and Anthropic API.
