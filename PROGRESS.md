# PROGRESS.md — AI Engineering Workspace

**Repo:** `jessejia1991/ai-engineering-workspace` (branch: `main`)
**Last commit:** `981a8cf docs: add CLAUDE.md and PROGRESS.md for work plan tracking`
**Deadline:** 2026-05-18 (received 2026-05-13 evening; ~4 working days)
**Follow-up:** live walk-through meeting after submission
**Source brief:** `Gmail_-_Interview_Project_Timeline.pdf` (decodeorigin Senior AI Engineer assessment)
**Internal design:** `AI_Engineering_Workspace_Design_Doc_v4.docx`

This document is the handover snapshot for picking up work in Claude CLI. It captures (1) what the brief asks for, (2) what's built, (3) the prioritized backlog with user stories and verifiable test cases, and (4) the interface contracts.

---

## 1. What the brief asks for

The take-home asks for an AI-powered engineering workspace — a multi-agent system that analyzes and improves a real codebase, behaving "like an AI engineering team."

**Explicit workflows in the brief:** automated code review, bug detection, security analysis, architecture review, UI/UX critique, test generation and execution, regression detection, performance optimization, refactoring recommendations, PR review automation, CI/CD validation, risk scoring and deployment checks.

**Explicit evaluation dimensions:** system design, agent orchestration, reliability, scalability, observability, safety, developer experience.

**Explicit deliverables:** source code, README with architecture explanation, design document, discussion of tradeoffs and limitations.

**Bonus signals:** self-improving agents, feedback loops, memory/reflection systems, multi-agent coordination.

**Stated framing:** "not a toy demo … production-quality workflows, safety checks, developer experience." Looks for engineers who bridge AI systems / software engineering / product thinking / production reliability.

---

## 2. Overall design

The system reviews PRs by running specialized agents in parallel against a diff, surfacing findings to a human in a `reflect` step, and feeding the human's accept/reject decisions back into long-lived memory so subsequent reviews improve. Three architectural ideas matter most:

### 2.1 Four-layer memory (ChromaDB)

| Layer | Scope | Grows when | Used at |
|---|---|---|---|
| `findings_memory` | per-agent | Any finding saved during a review | Retrieval before each agent runs |
| `corrections_memory` | global across agents | Human rejects a finding in `reflect` with a reason | Retrieval before each agent runs |
| `repo_profile` | repo-level | `scan` command | Injected into every prompt |
| `planning_memory` | repo-level | Each `build` invocation on approve — stores raw requirement, clarify Q&A (if any), final graph summary, and user edits made | Queried inside `planner.py` before the LLM call to inject top-K similar past builds and their resolution patterns |

`planning_memory` is the system's "this user / this repo" reflection layer: every requirement breakdown leaves a trace, and similar future requirements pull these traces forward so the planner asks fewer questions and matches the user's preferred decomposition style over time. This is the build-side counterpart to `corrections_memory`'s review-side learning loop.

Forgetting is by **time decay at retrieval time**, not deletion. Retrieval is **semantic top-K**, not full dump, so memory size doesn't blow up the prompt.

### 2.2 Hidden state made observable

Every agent returns `(findings, reasoning)` where `reasoning = {codebase_understanding, rejected_candidates, confidence_per_finding}`. `rejected_candidates` — what the agent considered but decided not to report — is the most important observable; it's how a human audits false negatives. Stored per-agent in `execution_log.payload` for `agent_result` events.

### 2.3 Dynamic agent selection

`orchestrator/agent_selector.py` combines rule-based hints (e.g., `*.java` → SecurityAgent + BugFindingAgent) with an LLM-based final selection. Skipped agents get logged with a reason.

### 2.4 Implicit evaluation via reflect

There is no separate eval pipeline. The human's accept/reject in `reflect` IS the evaluation — accepted findings reinforce memory, rejected ones produce corrections that subsequent reviews retrieve semantically.

### 2.5 Plan↔Review contract loop (P4, multi-agent design)

`build "<requirement>"` runs 2–4 expert agents (Security / UIUX / Testing / Performance / Delivery — selected per requirement) **concurrently** against the natural-language requirement. Each expert produces (1) clarify questions, (2) ranked design suggestions, (3) proposed contract criteria tagged with `must_have | should_have | nice_to_have` and an `owner_agent`. A Synthesizer step then bundles all expert outputs into an **Architect Report** — always shown — that the human triages (pick which Qs to answer, which suggestions to accept) before the final TaskGraph + Contract are produced.

`review --pr N` accepts an optional `--graph GRAPH-xyz` and otherwise **auto-matches** the PR description against `planning_memory` via semantic similarity (top-1 if confidence > threshold and unambiguous, else fall back to generic review). When a contract is in scope, each review-side agent receives the criteria it owns and emits a `contract_status` per criterion (`PASS | FAIL | UNVERIFIED` + evidence). The contract panel + per-criterion findings are rendered; `merge_recommendation` is downgraded to `request_changes` if any `must_have` is `FAIL`. PRs with no matching graph (human-authored, hotfixes, out-of-band) go through the existing P1 review flow unchanged — contract is **enhancement, not prerequisite**.

This closes the loop between plan and review: the plan-phase contract is the verifiable specification the review phase validates against, instead of those two phases being independent walls of text.

---

## 3. Current implementation status

### 3.1 File inventory (`temp-branch` HEAD)

```
ai-engineering-workspace/
├── .env.example
├── agents/
│   ├── base.py                  (181)  BaseAgent + reasoning schema
│   ├── bug_finding.py           ( 51)
│   ├── performance.py           ( 43)
│   ├── security.py              ( 52)
│   ├── testing.py               ( 42)
│   └── uiux.py                  ( 43)
├── cli/
│   ├── main.py                  (275)  Interactive shell + click commands
│   ├── reflect_cmd.py           (192)  Human triage UI
│   └── review_cmd.py            (184)  Run a review on a PR
├── database.py                  (222)  aiosqlite schema + helpers
├── github_client.py             (154)  Post review comments to GitHub
├── memory/
│   └── vector_store.py          (218)  ChromaDB three-layer memory
├── models.py                    ( 47)  Pydantic models
├── orchestrator/
│   ├── agent_selector.py        (159)  Rule + LLM agent selection
│   └── runner.py                (255)  run_review pipeline
└── scanner/
    └── repo_scanner.py          (269)  scan, classify, repo_profile
```

**Missing:** `README.md`, `requirements.txt`. (`.gitignore` exists but is incomplete — missing `.ai-workspace/`, `workspace.db`, `.cache/`; see §5.2. `tests/` now contains `test_memory.py` from §12 verification.)

### 3.2 What works (Day 1–3)

- **Day 1 (committed, `66f2524`):** repo scanner, file classification, repo_profile generation, SQLite schema.
- **Day 2 (partially committed, `a234260`):** `BaseAgent` + `BugFindingAgent` + `SecurityAgent` + `agent_selector` + `runner.run_review` parallel orchestration. The other three agents (`performance`, `testing`, `uiux`) and `github_client.py` were authored after the commit and currently live in the working tree.
- **Day 3 (uncommitted, working tree):** ChromaDB memory wired in (`memory/vector_store.py`). Agents return `(findings, reasoning)`. `execution_log` captures reasoning. New `cli/reflect_cmd.py` writes corrections back to ChromaDB on reject. Both `review` and `reflect` CLI output surface reasoning (`codebase_understanding`, `rejected_candidates`, `memory_injected`). A temporary `temp commit` that bundled all of Day 2-residue + Day 3 was reset to keep history clean, so everything below is uncommitted.

### 3.3 Day 3 delta (now committed on `main`)

**Resolved 2026-05-14.** The table below was the working-tree snapshot before commit; it remains as a record of what Day 3 actually contained. After resetting an earlier temp commit, the entire Day 2-residue + Day 3 delta lived uncommitted on `main` and was reconciled against `git status` before being committed. See `git log` on `main` for the exact commit SHA — kept out of this doc so amends do not invalidate it.

**Modified (8 tracked files):**

| File | Change | Why |
|---|---|---|
| `database.py` | `get_agent_reasoning(task_id)` reads `agent_result` rows into per-agent dict (handles retries — last attempt wins). | Shared retrieval helper for reasoning display. |
| `database.py` | `clear_unreviewed_findings(task_id)` deletes findings where `accepted IS NULL`. | Re-review clears stale untriaged findings while keeping accepted/rejected as history. |
| `database.py` | `create_task` → `INSERT … ON CONFLICT DO UPDATE` upsert. | Re-running review resets task to PENDING instead of crashing on UNIQUE constraint. |
| `agents/base.py` | Reasoning schema + `parse_response` updates + memory parameter on `review()`. | Implements the agent contract in §13.1 (return `(findings, reasoning)`). |
| `agents/bug_finding.py` | Prompt rewritten to emit the new JSON-with-reasoning shape. | Day 3 reasoning contract. |
| `agents/security.py` | Prompt rewritten to emit the new JSON-with-reasoning shape. | Day 3 reasoning contract. |
| `cli/main.py` | Removed duplicate `_cmd_reflect`; implemented `_cmd_logs`; wired `reflect_cmd`. | Cleanup + make `logs` command work. |
| `cli/review_cmd.py` | `_render_agent_reasoning()` called between "Agent Selection" and "Findings"; memory_injected display. | Make hidden state observable. |
| `orchestrator/runner.py` | Calls `clear_unreviewed_findings` after `create_task`; queries memory before each agent; logs `memory_injected` in `agent_result`. | Pairs with upsert; closes the memory loop. |
| `.ai-workspace/repo-context.json` | Re-run of `scan` output. | Runtime artifact — likely should be `.gitignore`d (see §5.2). |

**New (7 untracked entries):**

| Path | Purpose |
|---|---|
| `agents/performance.py` | PerformanceAgent (concurrency, N+1, hot paths). |
| `agents/testing.py` | TestingAgent — currently does test *review*, not generation (gap noted in §3.4). |
| `agents/uiux.py` | UIUXAgent for accessibility / UX critique on frontend diffs. |
| `cli/reflect_cmd.py` | Whole reflect command — human triage loop with per-finding reasoning render + correction write-back. |
| `github_client.py` | Posts findings to GitHub PR via PyGithub. |
| `memory/vector_store.py` | ChromaDB three-layer memory (findings / corrections / repo_profile) + `query_relevant_memory` + `format_memory_for_prompt` + `get_stats`. |
| `.ai-workspace/chroma_db/` | Runtime artifact — must be `.gitignore`d (handled in §5.2). |

The individual files have been smoke-tested in prior conversations, but **end-to-end with real ChromaDB + Anthropic API has not been run yet** on the current working-tree state — that's §12 / Priority 1 below.

**Addendum (2026-05-14, during §12 verification):** three additional fixes had to be applied for end-to-end to actually work:

| File | Change | Why |
|---|---|---|
| `memory/vector_store.py` | New module-level `_init_lock = threading.RLock()` (started as `Lock`, changed to `RLock`). | `get_findings_collection()` holds the lock then calls `get_client()` which tries to take it again — same thread, non-reentrant Lock deadlocks. RLock fixes; verified by `test_memory.py`. |
| `orchestrator/runner.py` | Memory retrieval changed from `asyncio.gather(asyncio.to_thread(...))` to a serial list comprehension. | ChromaDB 1.5.9's Rust bindings hang when `PersistentClient` init races across multiple worker threads. Memory queries are ms-scale, so serial is essentially free. |
| `agents/base.py` | `max_tokens 2500 → 6000`; capture `_raw_response` + `_stop_reason` into the returned reasoning dict. | First verification run had SecurityAgent's JSON truncated at 2500 tokens, fell through to fallback parser and produced 4 empty findings. Higher cap stopped truncation; raw_response captured for future debugging. |
| `tests/test_memory.py` + `tests/__init__.py` (new) | Standalone smoke test for vector_store sync / async / write-back. Run with `python -m tests.test_memory [sync|async|write|all]`. | Used to isolate the RLock bug; now a useful regression check. |

### 3.4 Coverage vs the brief

| Brief item | Status |
|---|---|
| Automated code review | ✓ done |
| Bug detection | ✓ done (BugFindingAgent) |
| Security analysis | ✓ done (SecurityAgent) |
| UI/UX critique | ✓ done (UIUXAgent) |
| Performance optimization | ✓ done (PerformanceAgent) |
| PR review automation | ✓ done |
| Test generation and execution | ⚠ TestingAgent does test *review* not *generation* — gap |
| Architecture review | ✗ no agent |
| Regression detection | ✗ not built |
| Refactoring recommendations | ✗ not built |
| CI/CD validation, deployment checks | ✗ explicitly deferred (no CI integration in scope) |
| Risk scoring | ⚠ `RiskReport` model exists, not surfaced |
| README | ✗ missing |
| Design document | ✓ v4 exists |
| Tradeoffs + limitations discussion | ✗ missing |

The plan in §4 addresses missing items either by building them or explicitly discussing the cutoff in the design doc.

---

## 4. Work plan — May 14–18

> **Cursor:** §8 Priority 4 Chunk A first item = `models.py — Criterion / Contract / CriterionStatus + TaskGraph.contract + AgentFinding.criterion_id`. P4 redesigned 2026-05-14 from the original SynthesizerAgent trade-off matrix into a **multi-agent contract architecture** that closes the loop between plan and review (see §2.5 + §8). **Sequence: P4 Chunk A → P3 (HTTP wrapper) → P4 Chunks B / C / D**, ~2 days total. P3 is **purely a network-layer rate-limit + retry + timeout wrapper around `AsyncAnthropic`**, drop-in replacement at `agents/base.py:client` — not agent orchestration (see §7). P1 docs / wrap-up deferred to end.
>
> *Update this line as work progresses. Claude Code reads this on every "continue" request to find the next task.*

### 4.0 Working with this document via Claude Code

`CLAUDE.md` (in repo root) contains the rules Claude Code follows when working from this document. The short version:

- When the user says "continue" / "next" / "下一个", Claude reads this file, finds the first unchecked `[ ]` in the lowest-numbered open priority, states what it will do, and **waits for user confirmation** before starting.
- After each task, Claude ticks the checkbox, updates the cursor line above, runs relevant §X.3 test cases, and **stops** (does not auto-advance).
- After each full Priority, Claude prompts for a commit with a `feat(pN): ...` message.
- Decisions marked "Decision before coding" in the tasks (e.g., §6.2 single-pass vs multi-turn) are escalated to the user, not guessed.

See `CLAUDE.md` for the full rule set.

### 4.1 Strategy

~4 working days for what realistically estimates to ~10. The conscious decision is **depth over breadth** along one thematic line: **"how AI agents evolve from single-task executors into a sustained engineering system."**

That theme is best told through three layers, which become the core of the submission:

- **Macro (Priority 2)** — how a requirement breaks down into a task graph and gets driven to completion
- **Meso (Priority 3)** — how concurrent agent execution stays safe under budget and rate-limit constraints
- **Micro (Priority 4)** — how multiple agents reach a decision when they disagree (engineering is trade-off, not accept/reject)

A single end-to-end example (e.g., "add a notes field to Pet entity, frontend + backend") threads through all three. The remaining items (Priorities 5 and 6) get design-only sections or minimal PoCs depending on remaining time.

**Risk-managed scope decision:** Priority 2's "in-shell multi-turn design conversation" can blow up if pushed too far. The conservative version — single-pass breakdown + human approve/edit + persistence, with the auto-advance engine designed but not fully built — leaves room for Priorities 3 and 4 to land properly. The full auto-advance engine is a stretch goal.

### 4.2 Priority sequencing rationale

- **Priority 1 (P0 essentials):** must-haves for any submission to be complete.
- **Priorities 2, 3, 4:** the core thematic story. Loses meaning if any one is cut entirely.
- **Priority 5:** design-only section. Low cost, real value (multi-engineer reasoning is a senior-level concern).
- **Priority 6 (Skills):** stretch only. Skipped if 2–4 take longer than estimated.

### 4.3 Stretch vs commitment

Updated 2026-05-14 after P2 closed-loop landed and P4 was redesigned in conversation:

- **Done:** Priority 1 (review + reflect + memory closed loop) + Priority 2 chunks 1+2 (build with clarify gate + planning_memory closed loop).
- **In flight:** Priority 4 redesigned as **multi-agent contract architecture** (replacing the original SynthesizerAgent trade-off matrix). Multi-expert plan-phase review + auto-matched contract-aware code review. See §8.
- **Implemented at HTTP layer:** Priority 3 (~0.3 day). The user clarified P3 is *purely* a `RateLimitedAnthropicClient` wrapper around `AsyncAnthropic` — semaphore on concurrent in-flight requests, retry on 429/529 with exponential backoff, per-request timeout, optional token budget. **Not** an agent-orchestration layer (planner / selector remain unchanged). Drops in at `agents/base.py` and a shared module-level instance covers `planner.py` + `agent_selector.py` so the limits are session-wide.
- **Design-only doc sections:** Priority 5 (multi-engineer collaboration).
- **Stretch:** Priority 2 full auto-advance engine, Priority 6 Skills.
- **Fallback if running short:** Priority 4 can degrade to "expert-plan + contract output but review-side contract consumption stays soft (parse but don't enforce)". This still demonstrates the loop architecturally.

The walk-through follow-up is live — so **demoability of the end-to-end example matters more than feature completeness**. The strongest demo story now: same notes-field requirement runs through expert plan → contract → mock PR → contract-aware review, end-to-end.

---

## 5. Priority 1 — Submission essentials

### 5.1 User stories

- A reviewer cloning the repo can install dependencies, set environment variables, and run their first review by following only the README — no chat history required.
- Reading the design doc, the reviewer sees explicit ownership of what was cut and why, instead of having to infer it from code.
- The reviewer can map every one of the brief's 7 evaluation dimensions to a corresponding section in the design doc.

### 5.2 Tasks

- [x] Run §12 end-to-end verification path (review → reflect → second review with non-zero memory injected) — completed 2026-05-14, all 6 steps passed; closed loop demonstrably working (3 findings → 1 after one reflect cycle).
- [x] Commit Day 3 + verification fixes to `main` — done 2026-05-14: `feat(p1): wire end-to-end memory loop and harden ChromaDB init`. See `git log` for the current HEAD SHA (kept out of this doc so amends don't invalidate it). **Push to origin/main not done — awaits user confirmation.**

**The five items below are intentionally deferred to wrap-up (after P2/P3/P4 land).** Rationale: core-theme feature work has the highest technical uncertainty and biggest demo payoff. Docs are easier to write after we know what actually shipped. If time runs out, the §12 closed-loop demo + design doc still covers the brief — these five are sharpening, not the floor.

- [ ] (deferred) Write `requirements.txt` (anthropic / aiosqlite / chromadb / sentence-transformers / click / rich / pydantic / python-dotenv / PyGithub / gitpython)
- [ ] (deferred) Tighten `.gitignore` — `.ai-workspace/chroma_db/` already added 2026-05-14; verify `scan + review + reflect` leaves a clean `git status` (§5.3 test)
- [ ] (deferred) Write `README.md` (architecture diagram, quickstart, command reference, demo path)
- [ ] (deferred) Design doc — add "Tradeoffs and Limitations" section
- [ ] (deferred) Design doc — add "Evaluation Against Brief" section mapped to the brief's 7 dimensions

### 5.3 Test cases for verification

- [ ] Fresh clone + `pip install -r requirements.txt` succeeds on a clean Python 3.12 venv
- [ ] Following only the README quickstart, a new user gets to a successful `scan` and `review --pr <N>`
- [ ] README architecture diagram includes all of: scanner, agents, orchestrator, memory layer, reflect loop
- [ ] Design doc "Tradeoffs" section names at least: architecture-review agent, refactoring agent, test generation, CI integration — with rationale for each cut
- [ ] Design doc "Evaluation Against Brief" section has a subsection per dimension (reliability / scalability / safety / observability / system design / orchestration / DX)
- [ ] `.gitignore` keeps a fresh clone clean — `git status` after a full `scan + review + reflect` cycle shows no `.ai-workspace/` or `__pycache__/` clutter
- [ ] `git log --oneline` shows the Day 3 commit landed cleanly (descriptive message, no merge mess)

---

## 6. Priority 2 — Requirement → breakdown → drive → merge workflow

### 6.1 User stories

- A developer types `build "add a notes field to Pet entity, frontend + backend"` in the shell and sees the system propose a task graph: a backend node (model + controller), a migration node, a frontend node (form field + display), a test node, with dependency arrows between them.
- The developer can edit that graph in-shell — splitting one node, deleting another, adding a missed one — and the system persists the edited graph.
- *(Stretch)* After approval, the developer watches the system drive each ready node through implement → review → reflect → merge, surfacing for human input only on review/reflect, until all nodes are MERGED.

### 6.2 Tasks

**Core data structures**
- [x] `models.py` — `TaskNode` (id, type, description, dependencies, status, artifacts, optional pr_number). Done 2026-05-14. Pydantic v2, str-comment enum style consistent with existing models.
- [x] `models.py` — `TaskGraph` (graph_id, root_requirement, nodes, current_node_id, created_at). `edges` is a derived `@property` from `node.dependencies` — single source of truth, never stored. Done 2026-05-14.
- [x] `database.py` — `task_graphs` table + `save_graph` (upsert) / `load_graph` / `list_graphs` / `update_node_status` CRUD. Nodes serialized as JSON in `nodes_json` (graphs are small enough that a normalized table isn't worth it). Done 2026-05-14, 10 smoke-test assertions pass.

**Breakdown**
- [x] New `orchestrator/planner.py` — takes NL requirement + repo_profile; returns dict with `action` ∈ {`plan`, `clarify`}. Cold-runs (planning_memory injection wired in chunk 2). Done 2026-05-14.
- [x] LLM prompt — output is a DAG with explicit dependency edges; topology rules in the prompt (migration before backend, tests follow target, etc.).
- [x] Breakdown produces typical node mix: verified on petclinic with concrete notes-field requirement → 6 nodes spanning migration / backend / backend-test / frontend / frontend-test.
- [x] **Clarify gate** — planner returns `{"action": "plan", "graph": {...}}` or `{"action": "clarify", "reason", "questions", "narrow_options"}`. CLI collects one round of answers and re-invokes with `force_plan=True` (validator rejects a recursive clarify, so the state machine is bounded). End-to-end verified twice (concrete → direct plan; vague → clarify too_vague → answer → plan).
- [x] **`memory/vector_store.py`** — 4th collection `planning_memory` + `get_planning_collection` / `add_plan` / `query_relevant_plans` / `format_plans_for_prompt` + `get_stats` extended with `planning_in_memory`. Same time-decay + cosine-similarity pattern as the other three layers. Done 2026-05-14.
- [x] **planner.py memory injection** — `plan()` cold-queries `planning_memory` for top-3 similar past builds before the LLM call, formats them into a "## Past similar builds" prompt section, and returns `memory_injected.planning_hits` in the result. Done 2026-05-14.
- [x] **`cli/build_cmd.py` reflection write-back** — `_edit_loop` returns ordered edits (`["edited n2", "deleted n4", "split n1", "added new node n7"]`); on approve, `add_plan(...)` captures (raw requirement, clarify Q&A if any, node count + types, edits, approved flag). Demo signal lines (`Planner memory: N similar past build(s) retrieved` and `added to planning_memory (X total)`) make the loop visible during the build session. Done 2026-05-14.

**Human-in-the-loop interaction**
- [x] New `cli/build_cmd.py` — interactive `build "<requirement>"`. Done 2026-05-14.
- [x] Display generated `TaskGraph` as a Rich table with type colors + "Depends on" column.
- [x] Node-level operations: approve (`a`) / edit (`e <id>`) / delete (`d <id>` + cascade-clean deps) / split (`s <id>` into N linear parts) / new (`n`). `merge` not implemented — `split + edit` covers the same intent.
- [x] **Decision before coding:** single-pass + 0-1 turn clarify gate (chosen 2026-05-14 after walking through risk vs UX trade-offs; multi-turn deferred to chunk-2-style design discussion in design doc).
- [x] Persist confirmed graph — `save_graph(graph_id, ..., approved=True)` on `a`; empty-graph approve is guarded with an error message and continues the loop.

**Advance engine** *(stretch — conservative cut: design only, document in design doc as future work)*
- [ ] `orchestrator/graph_runner.py` — topological sort, pick next ready node, execute, update status
- [ ] Per-node execution: implement (agent) → review → reflect → merge/redo
- [ ] Node state machine: PENDING → IMPLEMENTING → REVIEWING → AWAITING_HUMAN → MERGED / BLOCKED
- [ ] Failure handling: single-node failure blocks downstream? (design decision)

**Merge abstraction**
- [ ] Define `MergeStrategy` interface
- [ ] First impl: mock — write to `.ai-workspace/merged_changes/`, don't touch git
- [ ] Design doc — discuss real version: rebase/squash strategies, conflict escalation

**Demo example**
- [ ] Pick concrete requirement (suggested: "add a notes field to Pet entity")
- [ ] Run from `build "..."` to all nodes marked MERGED (mock-merge fine)

### 6.3 Test cases for verification

**Breakdown correctness**
- [x] `build "add a notes field to Pet entity"` produces a graph with at least 3 distinct node types — verified 2026-05-14 with `GRAPH-4916d82d`: 5 distinct types in 6 nodes (migration / backend / backend-test / frontend / frontend-test).
- [x] The breakdown DAG has no cycles — verified, linear topology n1→n2→{n3,n4,n5}→n6.
- [x] Backend node is a dependency of frontend node — n5 (frontend) depends on n3 (backend).
- [x] Migration node is a dependency of backend node — n2 depends on n1.
- [x] Test node depends on the code it tests — n4 (backend-test) → n3, n6 (frontend-test) → n5.

**Human-in-loop interaction**
- [ ] User can delete a node in shell and the deleted node disappears from re-rendered graph *(code path written; not yet exercised by an interactive smoke test)*
- [ ] User can edit a node's description in shell and the new description persists after re-rendering *(same)*
- [x] Approving a graph writes it to `task_graphs`; reopening retrieves the same graph — verified 2026-05-14 (two builds end-to-end, `list_graphs()` returns both with `approved=1`, `load_graph()` reconstructs nodes + dependencies intact).
- [x] Approving an empty graph fails gracefully — guard added in `_edit_loop`: "Cannot approve an empty graph" message + continue.

**Persistence**
- [x] Two `build` calls produce two distinct rows in `task_graphs` with unique IDs — verified 2026-05-14: `GRAPH-4916d82d` ("notes field to Pet") and `GRAPH-91e4f463` ("improve the pet form").
- [x] Loading a saved graph reconstructs all node attributes and edges intact — verified at both model layer (JSON roundtrip) and DB layer (`load_graph` + `TaskGraph(**)` reconstruction preserves the `edges` property).

**Clarify gate + planning memory** *(new)*
- [x] Vague input `"improve the pet form"` triggers `action: "clarify"` with `reason == "too_vague"` and 3 concrete questions referencing real files (PetRestController.java, client/src/components/pets/) — verified 2026-05-14.
- [x] Concrete input `"add a notes field to Pet entity, both backend and frontend"` goes straight to `action: "plan"` — no clarify round, 6 nodes — verified 2026-05-14.
- [x] Complex input `"rewrite auth, migrate to OAuth, add MFA, update all tests"` triggers `action: "clarify"` with `reason == "too_complex"` and 3 `narrow_options` each scoped to one build — verified 2026-05-14 at planner level.
- [x] After approving a first `build`, the second `build` with a semantically similar requirement shows ≥ 1 hit retrieved from `planning_memory` in the planner prompt — verified 2026-05-14: build #1 "add notes to Pet" → build #2 "add notes to Visit, full stack" → `Planner memory: 1 similar past build(s) retrieved`.
- [x] If the user edited the first build's DAG, the second build's initial draft reflects that edit pattern — verified 2026-05-14: build #1 user deleted both test nodes (n4 + n6) ending at 4 nodes; build #2's initial draft was 4 nodes with NO test nodes, and the planner's own reasoning cited the prior pattern ("Following the same pattern as the 'add notes field to Pet entity' build…").
- [x] After 1 approved build, `query_relevant_plans()` returns ≥ 1 result; after 0 builds, returns `[]` without crashing — verified in `vector_store` smoke test (similarity 0.563 for related query vs. 0.099 for unrelated, 5× gap shows the embedder is doing useful work).

**Advance engine** *(only if implemented)*
- [ ] Running the advance engine on a 3-node graph (backend → test → frontend) executes them in topological order, never reverses
- [ ] When a node enters AWAITING_HUMAN, the engine pauses and returns control to the shell
- [ ] A node marked BLOCKED prevents its downstream nodes from being picked up
- [ ] Mock-merge writes to `.ai-workspace/merged_changes/<node_id>/` and updates node status to MERGED

---

## 7. Priority 3 — HTTP-layer LLM client wrapper

**Scope clarified 2026-05-14.** P3 is *not* an agent-orchestration layer. It is a thin wrapper around `AsyncAnthropic` that drops in transparently at `agents/base.py:client` (and `planner.py`, `agent_selector.py`). All existing call sites — `client.messages.create(...)` — keep working without modification; the wrapper just enforces concurrency / retry / timeout / budget invariants on the underlying HTTP traffic.

The agentic orchestration layer (`agent_selector`, `planner`, `runner`, `build_cmd`) is unaffected and was never the target.

### 7.1 User stories

- A reviewer running a review (or a build) sees concurrent HTTP requests stay below the configured cap — no rate-limit cascades, no hung sockets — even when the orchestrator fans out to 4–5 agents at once.
- When the Anthropic API returns 429 / 529 (rate-limit / overloaded), the wrapper retries with exponential backoff transparently; the agent code never sees the transient failure. After `max_retries` exhausted, it surfaces a clear error.
- A request that exceeds `request_timeout_s` is killed cleanly; in-flight peers are not affected.
- At the end of every `review` and `build`, the CLI prints a `usage_summary`: `{requests, input_tokens, output_tokens, retries, timeouts, budget_used / budget_cap}`. This makes the run cost observable per session.
- Setting `token_budget` raises `BudgetExceeded` before the next request goes out — preventing runaway cost in misconfigured prompts.

### 7.2 Tasks (~0.3 day)

**Client implementation**
- [ ] New `agents/llm_client.py` — `RateLimitedAnthropicClient`
  - Wraps `AsyncAnthropic` instance internally
  - `messages` property returns a proxy so `client.messages.create(...)` works identically to the SDK
  - `asyncio.Semaphore(max_concurrent)` gates concurrent in-flight requests (default 5)
  - `asyncio.wait_for(..., timeout=request_timeout_s)` per request (default 120s)
  - Retry on `RateLimitError` / `APIStatusError` 429/529 with exponential backoff: `backoff_base_s * 2**attempt`, max `max_retries` attempts (default 4)
  - `token_budget`: optional; track `response.usage.input_tokens + output_tokens` per call, raise `BudgetExceeded` before the next request if exceeded
  - `usage_summary()` returns the accumulated counters
  - Cancellation-safe: if the caller's task is cancelled, semaphore is released in `finally`

**Wire-in**
- [ ] Module-level shared instance in `agents/llm_client.py`: `client = RateLimitedAnthropicClient()` — env-var configurable (`ANTHROPIC_MAX_CONCURRENT`, etc.)
- [ ] `agents/base.py` — replace `client = AsyncAnthropic()` with `from agents.llm_client import client`
- [ ] `orchestrator/agent_selector.py` — same replacement
- [ ] `orchestrator/planner.py` — same replacement
- [ ] All three modules now share one semaphore + one token budget per session

**Observability**
- [ ] `cli/review_cmd.py` — print `usage_summary` at end of run (dim panel below Risk Report)
- [ ] `cli/build_cmd.py` — print `usage_summary` at end of run (after the saved-graph panel)

### 7.3 Test cases for verification

**Concurrency cap**
- [ ] With `max_concurrent=2` and 5 simultaneously-launched `messages.create(...)` calls, at most 2 are in-flight at any single instant (measure via a counter inside the wrapper)
- [ ] All 5 calls eventually complete; none stuck

**Timeout**
- [ ] A call mocked to hang past `request_timeout_s` raises `TimeoutError` (or `asyncio.TimeoutError`) at the wrapper level — does not propagate as a generic stuck future
- [ ] Other in-flight calls continue and complete normally after one peer times out
- [ ] A timed-out call releases its semaphore slot (verifiable by launching a 6th call right after)

**Retry on rate limit**
- [ ] A mocked 429 response is retried with exponential backoff (verify total wallclock ≥ `backoff_base_s * (1+2+4)` for 3 retries)
- [ ] After `max_retries` consecutive 429s the call raises a clear `RateLimitError`, not an infinite loop
- [ ] A 529 (overloaded) is treated the same as 429

**Token budget**
- [ ] `token_budget=100` + a request that would consume more raises `BudgetExceeded` *before* sending the request (no wasted call)
- [ ] Budget accumulates across calls (not reset per call)
- [ ] Within-budget session completes with `usage_summary.budget_used < budget_cap`

**Drop-in compatibility**
- [ ] Existing `agents/base.py` `review()` flow runs unchanged with `client.messages.create(...)` after swap — no signature changes required
- [ ] An end-to-end `review --pr 1` with the wrapper installed produces the same findings shape as before the swap
- [ ] An end-to-end `build "..."` with the wrapper installed completes and prints a `usage_summary` line

**Observability surface**
- [ ] CLI output at end of `review` shows `usage_summary` with non-zero `requests` and `input_tokens`
- [ ] CLI output at end of `build` shows the same; `retries` and `timeouts` are 0 on the happy path

---

## 8. Priority 4 — Multi-agent contract architecture

**Redesigned 2026-05-14.** Replaces the original SynthesizerAgent + trade-off-matrix scope (kept here as v1 archive at §8.5 for reference). Drives a closed loop between the plan phase and the review phase via a structured **Contract** of verifiable criteria.

### 8.1 User stories

- A developer types `build "add a notes field to Pet entity, both backend and frontend"`. The system runs 4 expert agents (Security / UIUX / Testing / Delivery) **concurrently** against the requirement. They produce ranked design suggestions ("notes must be sanitized for XSS — HIGH", "rollback DROP COLUMN required — HIGH", "char counter when @Size set — MED") and clarify questions where genuinely ambiguous. The developer sees an **Architect Report** (always shown), picks which Qs to answer and which suggestions to accept, and the Synthesizer produces (a) a TaskGraph DAG + (b) a **Contract** of criteria, each tagged with `must_have | should_have | nice_to_have` and an `owner_agent`.
- A developer running `review --pr 7` (without an explicit `--graph`) sees the system auto-match the PR description against `planning_memory` and surface "Auto-matched PR #7 → GRAPH-ae12d394 (similarity=0.78)". Each review-side agent reports `contract_status` per owned criterion (PASS / FAIL / UNVERIFIED + evidence). A Contract Status panel renders alongside findings; `merge_recommendation` drops to `request_changes` if any `must_have` is `FAIL`.
- A developer running `review --pr 12` on a hand-written PR that doesn't correspond to any approved graph sees the system fall back gracefully to the P1 generic review flow — no error, no panel, just the existing closed loop. **Contract is enhancement, not prerequisite.**

### 8.2 Tasks

**Chunk A — Data model (~0.3 day)**
- [ ] `models.py` — `Criterion` (id, owner_agent, priority ∈ {must_have, should_have, nice_to_have}, category, assertion, rationale, suggested_check ∈ {static-analysis, runtime-test, manual})
- [ ] `models.py` — `Contract` (contract_id, graph_id, criteria: list[Criterion], created_at)
- [ ] `models.py` — `TaskGraph.contract: Optional[Contract] = None`
- [ ] `models.py` — `AgentFinding.criterion_id: Optional[str] = None` (links a finding back to the criterion it addresses)
- [ ] `models.py` — `CriterionStatus` (criterion_id, status ∈ {PASS, FAIL, UNVERIFIED}, evidence: str)
- [ ] `database.py` — `task_graphs.contract_json` column; `save_graph` and `load_graph` serialize / parse it (graphs stay self-contained — one row, all info)
- [ ] `tests/test_models.py` (extend the existing smoke test) — Contract roundtrip, criterion priority enum, contract-status validation

**Chunk B — Multi-expert plan phase (~0.5 day)**
- [ ] `agents/base.py` — new abstract method `async review_requirement(self, requirement, repo_profile, memory) -> dict` returning `{perspective_summary, clarify_questions, design_suggestions, proposed_criteria}`. Same memory + retry + RLock infra as `review()`.
- [ ] `agents/{security,uiux,testing,performance}.py` — each implements `review_requirement` with an angle-specific prompt (SecurityAgent asks "what are the attack surfaces / compliance hard-requirements"; UIUXAgent asks "what user impact / accessibility concerns"; TestingAgent asks "what coverage / test strategy"; PerformanceAgent asks "what hot-path concerns").
- [ ] `agents/delivery.py` (new) — DeliveryAgent focuses on release risk (rollback, schema migration safety, backward compatibility, feature flags). `review_requirement` only at first — no `review()` counterpart yet.
- [ ] `orchestrator/planner.py` — new function `plan_with_experts(requirement, repo_profile)`. Picks 2–4 experts via `agent_selector_for_plan` (or a per-build LLM call mirroring P1's `select_agents`). Runs them concurrently via `asyncio.gather` + the already-validated RLock pattern. Each expert pulls its own planning_memory (top-K) for its prompt.

**Chunk C — Synthesizer + Architect Report UX (~0.5 day)**
- [ ] `orchestrator/planner.py` — `synthesize_report(expert_outputs, requirement, repo_profile) -> ReportPayload` bundles clarify Qs + ranked suggestions + draft contract. Synthesizer is a single LLM call that takes all expert outputs as structured JSON input.
- [ ] `cli/build_cmd.py` — render Architect Report (always shown after experts complete): Clarify Questions section + Design Suggestions with `★★★ / ★★ / ★` priority stars. Input parser accepts `q1=a q2=b s1 s3` style multi-pick on one line.
- [ ] `cli/build_cmd.py` — re-invoke planner with selections to produce final `{TaskGraph, Contract}`. Render proposed Graph + Contract panels side-by-side.
- [ ] `cli/build_cmd.py` — contract editing commands: `ec <criterion_id>` to edit, `dc <criterion_id>` to delete, `nc` to add a criterion manually. Track edits in the existing `edits` list so `planning_memory` reflection covers contract decisions too.
- [ ] `cli/build_cmd.py` — `add_plan(...)` extended to include contract summary in the document text (so future builds retrieve "this kind of feature usually has 6 must-have criteria, here's the shape").

**Chunk D — Contract-aware review + auto-match (~0.4 day)**
- [ ] `orchestrator/runner.py` — `run_review` accepts optional `graph_id` parameter; loads contract via `load_graph` if provided.
- [ ] `orchestrator/runner.py` — new `find_graph_for_pr(pr_description, min_similarity=0.4) -> Optional[dict]` using `query_relevant_plans`. Returns top hit if similarity ≥ threshold AND top1 is clearly ahead of top2 (no ambiguity). Ambiguous matches log all top-3 + similarities and require explicit `--graph`.
- [ ] `cli/review_cmd.py` — accept `--graph GRAPH-xyz` flag and `--no-graph` (force generic review). Without either, run auto-match.
- [ ] `orchestrator/agent_selector.py` — when a contract is in scope, **union** the diff-based selection with the set of contract owner agents (a SecurityAgent-owned criterion always selects SecurityAgent, even if the diff didn't trigger it).
- [ ] `agents/base.py` — `review()` signature extended to accept `owned_criteria: list[Criterion] = []`. Each agent's `build_prompt` includes the criteria block when non-empty. Each agent's parser pulls `contract_status: list[CriterionStatus]` from the reasoning block.
- [ ] `cli/review_cmd.py` — render Contract Status panel (criteria grouped by `owner_agent`, color-coded by priority, status icon per criterion). `risk_report.merge_recommendation` becomes `request_changes` if any `must_have` is `FAIL`, else preserves existing logic.
- [ ] `cli/review_cmd.py` — when no contract in scope: zero rendering / zero plumbing, the §12 closed-loop flow runs as today (graceful fallback, not error).

### 8.3 Test cases for verification

**Multi-expert plan correctness**
- [ ] `build "add a notes field to Pet entity"` produces a Contract with criteria from ≥ 2 distinct `owner_agent` values
- [ ] Every criterion has `priority` ∈ {must_have, should_have, nice_to_have}, non-empty `assertion`, non-empty `owner_agent`
- [ ] The TaskGraph DAG and Contract are saved together on approve (one `task_graphs` row, `contract_json` populated)
- [ ] Re-loading the graph reconstructs the Contract with all criteria intact

**Architect Report UX**
- [ ] Report displays after every expert round (even if no clarify questions / all suggestions are nice_to_have) — "always show" property verified
- [ ] User can answer multiple Qs and accept multiple suggestions on one input line (`q1=a q2=b s1 s3 s5`)
- [ ] Skipped suggestions do NOT appear as criteria in the final contract
- [ ] Accepted high-priority suggestions DO appear as `must_have` criteria

**Contract-aware review**
- [ ] `review --pr N --graph GRAPH-xyz` loads the contract and renders the Contract Status panel
- [ ] Each criterion is marked PASS / FAIL / UNVERIFIED with an evidence string
- [ ] At least one `must_have` `FAIL` causes `risk_report.merge_recommendation` to become `request_changes`
- [ ] All `must_have` PASS and zero high-severity new findings → `merge_recommendation` stays `approve`
- [ ] Without `--graph` and no auto-match: review behaves identically to P1 (no contract panel, no errors)

**Auto-match (planning_memory → PR)**
- [ ] PR with description containing the original requirement text auto-matches to the correct graph at similarity ≥ 0.4
- [ ] Two approved graphs with similar requirements → ambiguity detected (top1 ≈ top2), top-3 logged, user prompted for explicit `--graph`
- [ ] PR with description unrelated to any approved graph → no match logged, generic review runs
- [ ] `--no-graph` flag forces generic review even when auto-match would succeed

### 8.4 B-track TODOs (deferred, not part of P4 commitment)

These were considered during P4 design conversation and intentionally deferred to keep P4 within ~1.7 days. Each item has a clear use case and isolated scope — pick up in P6 / wrap-up if time remains.

- [ ] `task_graphs.status` lifecycle column (DRAFT / APPROVED_PENDING / UNDER_REVIEW / MERGED / ARCHIVED). Currently all `approved=1` graphs are eligible for auto-match. With status tracking, only `APPROVED_PENDING` and `UNDER_REVIEW` would be searched, avoiding stale matches.
- [ ] **Contract memory injection** — query past *contracts* (not just plans) when generating new ones. Lets the synthesizer pull "for similar features, SecurityAgent typically contributes a sanitization criterion" patterns. The data is already in `planning_memory` doc text; needs a dedicated retrieval that surfaces criterion-level patterns.
- [ ] `GRAPH-xxx` slug grep — when the user explicitly writes `Builds GRAPH-xyz` in the PR description, short-circuit semantic match for a deterministic path (cheaper + bulletproof for ops-style workflows). Fall back to semantic match if slug absent.

### 8.5 Original P4 design (archived 2026-05-14)

The pre-redesign P4 was "SynthesizerAgent reads other agents' findings on the same file:line, produces a trade-off matrix, human picks an option, choice persists to corrections_memory." This was a **review-side** synthesis layer, with no plan-side counterpart.

Why we replaced it: it tackled the symptom (conflicting findings) rather than the cause (vague specifications). The contract pattern moves the trade-off decision **forward** into the plan phase, where the decisions are easier to make and the artifact (the contract) becomes a reusable spec for review. Plan↔Review forms a real loop instead of two disconnected pipelines.

If P4 chunks A–D run long, fall back to the §4.3 fallback: keep expert plan + contract output (Chunks A–C), let review consume the contract softly (parse but don't enforce). The loop is still architecturally present, just less strict at the validation gate.

---

## 9. Priority 5 — Multi-engineer collaboration (design only)

### 9.1 User stories

- A reviewer reading the design doc finds a clear answer to: "What happens when two engineers' corrections contradict each other on the same finding type?"
- The reviewer understands the proposed memory namespace model (per-engineer / per-team / global) and the rationale for the chosen default.
- The reviewer can trace, in the proposed audit design, which engineer's correction influenced which subsequent review's outcome.

### 9.2 Tasks

- [ ] Design doc — new section "Multi-Engineer Collaboration"
- [ ] Memory namespace strategy: per-engineer / per-team / global trade-offs
- [ ] Correction voting / priority: when does one engineer's correction affect everyone else's reviews?
- [ ] Bias isolation: avoid one engineer's bias polluting team memory
- [ ] Conflict resolution: opposing corrections on the same finding type
- [ ] Audit: who added what correction, when, which subsequent reviews it influenced

### 9.3 Test cases for verification

(All design-doc checks, no code.)

- [ ] Section explicitly proposes a default namespace model and gives at least one alternative considered
- [ ] Section discusses how the system avoids one engineer's repeated rejections from training the system on their personal style preferences
- [ ] Section describes a concrete audit query — given a finding, identify which past corrections influenced its reasoning
- [ ] Section names at least one scenario that the proposed design does NOT solve (e.g., adversarial engineer)
- [ ] Section references concrete fields/tables that would need to change in the current schema (so it's grounded, not hand-wavy)

---

## 10. Priority 6 — Skills integration (stretch)

### 10.1 User stories

- A non-engineer (e.g., a security architect) can contribute a new check pattern to SecurityAgent by editing a markdown file in `skills/security_review/`, without touching Python.
- When SecurityAgent runs on a small diff, it loads only the SKILL.md and relevant resource sections, not the entire OWASP corpus — progressive disclosure keeps context cost down.
- Reading the design doc, a reviewer understands why Skills are a better abstraction than hardcoded prompts for an agent system intended to evolve over time.

### 10.2 Tasks

**Minimum viable: convert one agent**
- [ ] Pick SecurityAgent as the prototype
- [ ] Create `skills/security_review/SKILL.md` (name, description, when to use)
- [ ] Move hardcoded prompt in `agents/security.py` to `skills/security_review/prompt.md`
- [ ] Reference material (OWASP checklist, common vuln patterns) under `skills/security_review/resources/`
- [ ] Modify `BaseAgent` to support Skills mode (progressive disclosure — read SKILL.md first, load resources on demand)

**Design section**
- [ ] Design doc — new section "Skills vs Hardcoded Prompts"
- [ ] Why Skills suit agent systems (separation of concerns, non-engineer contribution, version control, progressive disclosure saves context)
- [ ] When hardcoded prompts are still better

**(If extra time) extend to other agents**
- [ ] Convert BugFindingAgent / PerformanceAgent — demonstrate extensibility

### 10.3 Test cases for verification

**Skill loading**
- [ ] `skills/security_review/SKILL.md` exists with the required fields (name, description, when_to_use)
- [ ] BaseAgent successfully loads SKILL.md when agent is constructed in Skills mode
- [ ] If SKILL.md is missing or malformed, BaseAgent falls back to hardcoded prompt with a clear warning (no silent failure)

**Progressive disclosure**
- [ ] On a small diff, only SKILL.md is loaded (verified by inspecting the prompt's token count)
- [ ] When the prompt asks for OWASP-specific info, the relevant `resources/owasp.md` section is fetched
- [ ] Token spend per review is lower with progressive disclosure than with the all-in-prompt version (measured on the demo PR)

**Behavioral equivalence**
- [ ] SecurityAgent in Skills mode produces findings of the same quality as the hardcoded version on the demo PR (manual judgment, not strict equality)
- [ ] Reasoning output shape is unchanged (`codebase_understanding` + `rejected_candidates` still populated)

**Design section**
- [ ] Section names at least 3 concrete advantages of Skills over hardcoded prompts
- [ ] Section names at least 1 scenario where hardcoded prompts are still better

---

## 11. Wrap-up

- [ ] Design doc full pass for consistency
- [ ] README ↔ design doc cross-check
- [ ] Demo recording or screenshot sequence (optional but strongly recommended)
- [ ] Submission email reply — repo link + design doc link

---

## 12. Day 3 verification path (run this before any new work)

This is the verification path for the uncommitted Day 3 changes (§3.3). Run it first.

1. **`scan`** → confirm `repo_profile` written
2. **`review --pr <N>`** (first run) → verify:
   - "Agent Selection" shows the right agents for the diff
   - "Agent Reasoning" section appears with `codebase_understanding` + `rejected_candidates`
   - "Memory injected: none (cold start)" — ChromaDB is empty on first run
3. **`logs TASK-PR<N>`** → confirm `agent_result` entries include reasoning JSON
4. **`reflect TASK-PR<N>`** → for each pending finding, confirm "Agent reasoning:" + "Agent considered but rejected:" appear above the prompt. Accept 1, reject 1 with reason, leave 1 unreviewed.
5. **Check ChromaDB**: `from memory.vector_store import get_stats; print(get_stats())` — findings_in_memory and corrections_in_memory both ≥ 1
6. **`review --pr <N>` (second run, same PR)** → verify:
   - No `UNIQUE constraint failed` error (upsert fix)
   - "Cleared 1 stale finding(s) from a previous review" status message
   - "Memory injected:" now shows non-zero — closed-loop proof
   - Accepted (f1) and rejected (f2) findings from step 4 are NOT in new pending list, but ARE in `task_findings` as history

If any step fails, fix before moving on.

---

## 13. Interface contracts

### 13.1 Agent contract (`agents/base.py:BaseAgent`)

```python
class BaseAgent(ABC):
    name: str   # e.g. "SecurityAgent"

    async def review(
        self,
        task: TaskSpec,
        diff: str,
        file_contents: dict,
        repo_profile: dict,
        memory: dict,                       # from query_relevant_memory()
    ) -> tuple[list[AgentFinding], dict]:   # (findings, reasoning)
        ...

    @abstractmethod
    def build_prompt(self, task, diff, file_contents, repo_profile, memory) -> str: ...
```

The LLM must return JSON of this exact shape (validated in `parse_response`):

```json
{
  "reasoning": {
    "codebase_understanding": "string",
    "rejected_candidates": [
      {"issue": "...", "why_rejected": "...", "confidence_to_reject": 0.0-1.0}
    ],
    "confidence_per_finding": {"finding_0": 0.0-1.0}
  },
  "findings": [
    {"severity": "low|medium|high|critical", "category": "...", "title": "...",
     "detail": "...", "suggestion": "...", "file": "...", "line": 42}
  ]
}
```

Legacy fallback: bare array of findings is accepted (no reasoning recorded).

### 13.2 Memory API (`memory/vector_store.py`)

```python
add_finding(finding_id, finding: dict, accepted: bool)
add_correction(correction_id, note, example, related_finding_id)
query_relevant_memory(agent_name, query_text,
                      top_k_findings=5, top_k_corrections=3) -> {
    "relevant_findings": [...],
    "relevant_corrections": [...],
    "findings_count": int,
    "corrections_count": int,
}
format_memory_for_prompt(memory: dict) -> str    # ~800 token budget
get_stats() -> dict
```

`findings_count` and `corrections_count` are what `runner.py` logs as `memory_injected` in `execution_log`.

### 13.3 Database API (`database.py`)

```python
init_db()                                                # idempotent
create_task(task_id, task_type, artifacts)               # upsert (resets to PENDING)
update_task_status(task_id, status)
get_all_tasks() / get_task(task_id)
save_finding(task_id, agent, severity, content) -> finding_id
get_pending_findings(task_id)                            # accepted IS NULL only
clear_unreviewed_findings(task_id) -> int                # rowcount
update_finding_accepted(finding_id, accepted: bool)
log_execution(task_id, event_type, agent, payload)
get_execution_log(task_id)
get_agent_reasoning(task_id)                             # parsed per-agent dict
```

Tables: `tasks`, `task_findings`, `execution_log`. New for Priority 2: `task_graphs` table.

### 13.4 Execution log event types

| `event_type` | `agent` | `payload` shape |
|---|---|---|
| `agent_selection` | `"orchestrator"` | `{selected, skipped, reasoning, changed_files}` |
| `agent_result` | agent name | `{attempt, latency_ms, finding_count, status, reasoning, memory_injected}` |
| `agent_retry` | agent name | `{attempt, error}` |
| `agent_queued` (Priority 3) | agent name | `{queued_at, queue_depth}` |
| `agent_started` (Priority 3) | agent name | `{started_at}` |
| `agent_timeout` (Priority 3) | agent name | `{timeout_after_ms}` |
| `budget_exceeded` (Priority 3) | `"scheduler"` | `{budget_type, used, cap}` |

`get_agent_reasoning()` reads `agent_result` rows only.

---

## 14. Running the system (current state)

Once `requirements.txt` exists:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit: ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPO, PETCLINIC_REPO_PATH
python -m cli.main     # interactive shell
```

Shell commands: `scan`, `review --pr N [--branch X]`, `reflect [TASK-ID]`, `logs [TASK-ID]`, `tasks`, `quit`.

After Priority 2 lands: `build "<requirement>"` for the breakdown workflow.

---

## 15. Open design questions

To answer during walk-through prep, not before:

1. **Repo-scoped vs global memory** — should petclinic corrections surface when reviewing a Python project? Currently no repo filter on corrections.
2. **Memory bootstrapping** — first review is always cold-start. Should `scan` pre-populate from a curated `seed_corrections.json`?
3. **Confidence thresholds** (Design Doc §3.4) — where do they live? Today agents don't filter by confidence.
4. **Priority 2 advance engine — fully automatic vs always-pause-for-human?** Likely the latter for safety.
5. **Priority 4 conflict definition** — "same file + line range" is simplest but may miss semantic conflicts across files.

---

*Generated end of day May 14. Next session: §12 verification, then Priorities 1 → 6 in order.*
