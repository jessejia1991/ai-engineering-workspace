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

### 2.1 Five-layer memory (ChromaDB), repo-scoped

| Layer | Scope | Grows when | Used at |
|---|---|---|---|
| `findings_memory` | per-agent, per-repo | Any finding saved during a review | Retrieval before each agent runs |
| `corrections_memory` | per-agent (global), per-repo | Human rejects a finding in `reflect` with a reason; user may `p+` to pin. Also: `verify run` failure-analysis writes `test-gen-lesson` entries here. | Retrieval before each agent runs; `verify generate` retrieves `test-gen-lesson` entries |
| `repo_profile` | per-repo (file system, not ChromaDB) | `scan` command | Injected into every prompt |
| `planning_memory` | per-repo | Each `build` invocation on approve — stores raw requirement, clarify Q&A (if any), final graph summary, and user edits made | Queried inside `planner.py` before the LLM call to inject top-K similar past builds and their resolution patterns |
| `test_catalog` | per-repo (strict isolation — tests don't generalize) | `verify generate` adds one entry per generated e2e test | `verify run --diff` queries by APIs touched in diff; `verify catalog search` does semantic lookup |

**Repo isolation + hierarchical retrieval (memory slice, 2026-05-15).** Every entry carries a `repo_id` metadata. Retrieval is two-phase: phase 1 returns own-repo hits (`where={"repo_id": active}`), phase 2 fills remaining slots from the cross-repo pool. Returned items are tagged `origin='own'/'cross'` and `format_memory_for_prompt` renders `[own-repo]` / `[cross-repo]` markers so the LLM can weight the two pools differently. Rationale: engineering wisdom like "trivial getters don't need unit tests" generalizes; throwing it away on a strict isolation boundary wastes signal.

**Lifecycle (memory slice).** Every entry also carries `last_accessed_at` (bumped on retrieval) and `pinned` (default False). `memory prune` evicts the LRU tail subject to three safeguards: pinned entries are never evicted; entries younger than `--age-floor-days` (default 7) are protected; per-collection size floor (`--max-per-collection`, default 50) is respected by counting protected items toward the floor. `memory compact` clusters semantically-similar corrections (default cosine ≥ 0.5 on the MiniLM embedder; threshold is tunable), asks the LLM to merge each cluster into one polished correction, and the human approves per cluster (y / N / q). `repo {add,use,remove,list}` manages the registry of repos this workspace has memory for.

Forgetting is now both **time-decay at retrieval** (similarity weights drop for stale entries) **and** explicit eviction (`memory prune`). Retrieval is **semantic top-K**, not full dump, so memory size doesn't blow up the prompt.

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

### 3.1 File inventory (`main` HEAD `e309c25`, 2026-05-14)

```
ai-engineering-workspace/
├── .env.example
├── CLAUDE.md                          Working-rules for Claude Code
├── PROGRESS.md                        This file
├── requirements.txt
├── agents/
│   ├── architecture.py          (115)  Layering / module-boundary / coupling concerns; plan-phase opt-in
│   ├── base.py                  (466)  BaseAgent + review() / review_requirement() + reasoning schema + contract_block
│   ├── bug_finding.py           ( 51)
│   ├── delivery.py              ( 73)  P4 plan-phase-only expert (raises in review())
│   ├── llm_client.py            (442)  P3 rate-limited wrapper + observability instrumentation (contextvars + write_observation)
│   ├── performance.py           ( 88)
│   ├── refactoring.py           ( 80)  Method/file-scoped quality: naming, dedup, dead code, type-safety
│   ├── security.py              ( 96)
│   ├── test_generation.py       ( 90)  Proposes runnable test code (JUnit/Jest/pytest) for changed behavior
│   ├── testing.py               ( 86)
│   └── uiux.py                  ( 87)
├── cli/
│   ├── build_cmd.py             (885)  P2/P4 build pipeline + Architect Report UX + Contract editing
│   ├── init_cmd.py              (290)  5-step setup wizard (keys, model, first repo); auto-triggered on missing key
│   ├── main.py                  (340)  Interactive shell + dispatch + first-run init auto-trigger
│   ├── memory_cmd.py            (190)  `memory {stats,prune,compact}` — maintenance over the memory collections
│   ├── memory_compact.py        (250)  LLM-driven cluster-merge of corrections (used by `memory compact`)
│   ├── reflect_cmd.py           (210)  Human triage UI + correction write-back + pin (`p+`)
│   ├── repo_cmd.py              (190)  `repo {list,add,use,remove}` — registry-side entity management
│   ├── review_cmd.py            (316)  Run a review on a PR + GitHub post (allowlist-gated)
│   ├── apply_cmd.py             (260)  /apply <finding_id> auto-fix: fetch PR comment, LLM patches file, --push to PR branch
│   ├── trace_cmd.py             (435)  `trace show` / `trace replay` observability CLI
│   ├── verify_cmd.py            (320)  `verify generate` — LLM produces Python pytest e2e tests
│   └── verify_run.py            (440)  `verify run` + failure analysis + `verify list` + `verify catalog search` + `verify health-check`
├── database.py                  (455)  aiosqlite schema + helpers (tasks · findings · exec_log · graphs · observations)
├── github_client.py             (206)  Post review comments to GitHub (two-gate safety)
├── memory/
│   └── vector_store.py          (342)  ChromaDB four-layer memory (findings · corrections · repo_profile · planning_memory)
├── models.py                    (106)  Pydantic models (TaskSpec, AgentFinding, TaskGraph, Contract, Criterion, ...)
├── orchestrator/
│   ├── agent_selector.py        (233)  Rule + LLM agent selection + select_experts_for_plan
│   ├── planner.py               (598)  P2 single-pass planner + P4 plan_with_experts + Synthesizer
│   └── runner.py                (534)  run_review pipeline + find_graph_for_pr auto-match
├── scanner/
│   ├── api_extractor.py         (270)  OpenAPI YAML + Spring annotation + Express route extraction
│   ├── repo_scanner.py          (320)  scan, classify, repo_profile, _default_branch auto-detect; integrates runtime + apis
│   └── runtime_detector.py      (210)  build/run/test command detection (Maven/npm/Python/Docker/CI)
└── tests/
    └── test_memory.py                  Standalone vector_store smoke test
```

**Missing:** `README.md` (last reviewer-facing gap). `.gitignore` already covers `.ai-workspace/`, `workspace.db`, `__pycache__/`, `venv/`.

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

**Explicit workflows from the brief:**

| Brief item | Status |
|---|---|
| Automated code review | ✓ done |
| Bug detection | ✓ done (BugFindingAgent) |
| Security analysis | ✓ done (SecurityAgent) |
| UI/UX critique | ✓ done (UIUXAgent) |
| Performance optimization | ✓ done (PerformanceAgent) |
| PR review automation | ✓ done (GitHub post via `--post`, allowlist-gated) |
| Test generation and execution | ⚠ generation done (TestGenerationAgent — outputs runnable JUnit/Jest/pytest in `suggestion`); execution explicitly out of scope (sandboxing + cross-language runners are a separate project) |
| Architecture review | ✓ done — ArchitectureAgent (review-side: layering / coupling / module-boundary) + plan-phase angle via `build_requirement_prompt` |
| Regression detection | ⚠ reframed — corrections_memory + Contract enforcement together provide regression-of-known-issue detection without a dedicated agent; documented as architectural choice in design doc (no separate RegressionAgent) |
| Refactoring recommendations | ✓ done — RefactoringAgent (method/file scope: naming, duplication, dead code, type-safety) |
| CI/CD validation, deployment checks | ✓ — full pipeline. CI: `review --pr N --strict --post` (review gate) + `verify health-check` (CD readiness gate) + `verify generate --diff` + `verify run --diff` (e2e gate). Self-hosted runner workflow at `examples/petclinic-ci.yml`. Health-endpoint detection in scanner; ArchitectureAgent flags HIGH-severity when missing. The target system's deploy lifecycle (build / start / teardown) is handled in the workflow YAML, not our tool — deliberate scope choice |
| Risk scoring | ✓ `RiskReport` rendered as a panel at end of every review (severity-weighted, with merge recommendation) |

**Explicit evaluation dimensions:**

| Dimension | Status |
|---|---|
| System design | ✓ four-layer memory + plan↔review contract loop + agent contract documented in §13 |
| Agent orchestration | ✓ rule+LLM agent selection · contract-owner union · parallel fan-out under bounded concurrency |
| Reliability | ✓ retry on 429/529 with `Retry-After` honoring + exp backoff + per-request timeout + agent_error logging |
| Scalability | ⚠ partial — HTTP wrapper enforces session-wide concurrency cap + token budget. File grouping experiment failed (see §16.3); roadmap of mitigations in §16.4 |
| Observability | ✓ execution_log + observations table (Langfuse-style) + `trace show / replay` (see §16.5 + §16.9) |
| Safety | ✓ GitHub-write two-gate (allowlist + opt-in flag) · no-write default · explicit blocked-by-allowlist message |
| Developer experience | ✓ Rich-rendered shell · auto-triggered `init` wizard on first run · `LLM usage` printed every command · trace_id printed for `build` |

**Bonus signals from the brief:**

| Bonus item | Status |
|---|---|
| Self-improving agents | ✓ corrections_memory + planning_memory closed-loop (§2.1, §6) |
| Feedback loops | ✓ `reflect` accept/reject writes back to ChromaDB; planning_memory captures user edits |
| Memory / reflection systems | ✓ four-layer ChromaDB with semantic retrieval + time decay |
| Multi-agent coordination | ✓ P4 plan-phase multi-expert + Synthesizer + per-criterion ownership in review |

**Deliverables:**

| Item | Status |
|---|---|
| Source code | ✓ on `main`, 9 commits ahead of `origin/main` |
| `requirements.txt` | ✓ done (`ca9c971`) |
| README | ✗ missing — top remaining wrap-up item |
| Design document | ✓ v4 `.docx` exists |
| Tradeoffs + limitations discussion | ⚠ partial — material is in PROGRESS.md §16, not yet reformatted into design doc |
| Evaluation Against Brief | ⚠ partial — this §3.4 table is the seed; design doc section pending |

The plan in §4 addresses missing items either by building them or explicitly discussing the cutoff in the design doc.

---

## 4. Work plan — May 14–18

> **Cursor:** P1 wrap-up phase. All 4 priorities + production-readiness + observability slice + memory slice + `init` wizard + brief-coverage slice + verify slice + **CI/CD Phase 1 + 2** (non-interactive click subcommands · `review --strict` · `verify health-check` · health endpoint detection + Architecture flag · catalog dedup · committed demo tests · self-hosted-runner workflow at `examples/petclinic-ci.yml` · **`apply` command** for `/apply <finding_id>` comment-driven auto-fix with `--push` to PR branch) all landed 2026-05-14/15. §3.4 ✓ on all 11 brief workflows. Three reflection loops working end-to-end: review→reflect→corrections (review side), verify→failure-analysis→test-gen-lessons (verify side), review-comment→apply→PR-branch-commit (CI side). Remaining: README major section + design doc Tradeoffs + Evaluation-Against-Brief sections. **User direction: trim / adjust demo before writing those.** Push to origin/main is gated by user decision.
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

- [x] Write `requirements.txt` — done 2026-05-14. Listed only deps actually imported (verified via `grep -r '^(from|import)'`): anthropic, aiosqlite, chromadb, pydantic, click, rich, python-dotenv, PyGithub. Pinned with `>=` against installed versions. Dropped `sentence-transformers` and `gitpython` from the originally-planned list — chromadb 1.5.9 uses an ONNX default embedder (no sentence-transformers needed), and the scanner shells out to `git` via `subprocess` rather than using GitPython. `pip install --dry-run -r requirements.txt` resolves cleanly.
- [ ] (deferred) Tighten `.gitignore` — `.ai-workspace/chroma_db/` already added 2026-05-14; verify `scan + review + reflect` leaves a clean `git status` (§5.3 test)
- [ ] (deferred) Write `README.md` (architecture diagram, quickstart, command reference, demo path)
- [ ] (deferred) Design doc — add "Tradeoffs and Limitations" section
- [ ] (deferred) Design doc — add "Evaluation Against Brief" section mapped to the brief's 7 dimensions

### 5.3 Test cases for verification

- [x] `pip install -r requirements.txt --dry-run` resolves cleanly on the current Python 3.12 venv 2026-05-14. (Fresh-clone install test deferred to actual reviewer.)
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

### 7.2 Tasks (~0.3 day) — DONE 2026-05-14

**Client implementation**
- [x] `agents/llm_client.py` — `RateLimitedAnthropicClient` + `_MessagesProxy` so `client.messages.create(...)` works identically to the SDK. Semaphore (default `ANTHROPIC_MAX_CONCURRENT=5`), `asyncio.wait_for` timeout (default 120s, env override), 429/529 + 5xx retry with exponential backoff `backoff_base_s * 2**attempt` (default 4 attempts), optional `token_budget` with `BudgetExceeded` pre-check, `usage_summary()` accumulating session counters, `reset_usage()` between CLI commands. SDK retry is disabled (`AsyncAnthropic(max_retries=0)`) so the wrapper has sole control.

**Wire-in**
- [x] Module-level singleton `client = RateLimitedAnthropicClient()` at the bottom of `agents/llm_client.py`. Env-var configurable.
- [x] `agents/base.py` — `from agents.llm_client import client` (replaced `AsyncAnthropic()`).
- [x] `orchestrator/agent_selector.py` — same.
- [x] `orchestrator/planner.py` — same.
- [x] All three modules share one semaphore + one token budget per session (verified via `id(singleton)` identity check across the 3 imports).

**Observability**
- [x] `cli/review_cmd.py` — `cmd_review` wraps body in try/finally; prints `LLM usage: …` on every exit path (success, RuntimeError, unexpected exception) and resets the counter. Includes `format_usage_summary()` from `llm_client`.
- [x] `cli/build_cmd.py` — same try/finally pattern around `_cmd_build_inner`. Verified: a build that the user quits at the edit loop still prints the usage line ("LLM usage: 1 req · 1,220 in / 378 out tokens" on a real run 2026-05-14).

### 7.3 Test cases for verification

**Concurrency cap**
- [x] `max_concurrent=2` + 8 simultaneously-launched calls: peak in-flight = 2 (asserted via a counter inside the mocked SDK).
- [x] All 8 calls completed; none stuck.

**Timeout**
- [x] `asyncio.wait_for` raises `asyncio.TimeoutError` at the wrapper level on hang — verified via design (`raise` after `_n_timeouts += 1`, no retry on timeout).
- [x] Semaphore released after timeout via `async with` — verified by `_messages_create`'s structure (timeout is inside `async with`).

**Retry on rate limit**
- [x] Mocked `anthropic.RateLimitError` retried with exponential backoff (3 attempts → success, elapsed ≥ `backoff_base_s * (1+2) = 0.03s` verified at 0.032s real).
- [x] After `max_retries` consecutive 429s, `RateLimitError` is raised; `n_retries == max_retries` recorded.
- [x] 500/502/503/504/529 treated the same as 429 (single retry branch in `APIStatusError` handler).

**Token budget**
- [x] `token_budget=50` + cumulative consumption past 50 → 3rd call raises `BudgetExceeded` *before* the request is sent (`n_budget_blocks` increments, no wasted call).
- [x] Budget accumulates across calls (not reset per call) — first two calls run, third blocked.

**Drop-in compatibility**
- [x] After swapping all 3 call sites to the wrapper, importing `agents.base.client` / `orchestrator.agent_selector.client` / `orchestrator.planner.client` all yield the same `id()` — the singleton is genuinely shared.
- [x] Real `build "add a status field to Visit entity"` runs to the proposed-graph step and prints `LLM usage: 1 req · 1,220 in / 378 out tokens` — drop-in compat confirmed end-to-end with real Anthropic API.

**Observability surface**
- [x] `cmd_build` and `cmd_review` print `LLM usage: …` on every exit path including user-quit and exception (verified by wrapping body in `try / finally`).
- [x] `format_usage_summary()` shows non-zero requests + input/output tokens on the happy path; suppresses zero-retry / zero-timeout fields for readability.

---

## 8. Priority 4 — Multi-agent contract architecture

**Redesigned 2026-05-14.** Replaces the original SynthesizerAgent + trade-off-matrix scope (kept here as v1 archive at §8.5 for reference). Drives a closed loop between the plan phase and the review phase via a structured **Contract** of verifiable criteria.

### 8.1 User stories

- A developer types `build "add a notes field to Pet entity, both backend and frontend"`. The system runs 4 expert agents (Security / UIUX / Testing / Delivery) **concurrently** against the requirement. They produce ranked design suggestions ("notes must be sanitized for XSS — HIGH", "rollback DROP COLUMN required — HIGH", "char counter when @Size set — MED") and clarify questions where genuinely ambiguous. The developer sees an **Architect Report** (always shown), picks which Qs to answer and which suggestions to accept, and the Synthesizer produces (a) a TaskGraph DAG + (b) a **Contract** of criteria, each tagged with `must_have | should_have | nice_to_have` and an `owner_agent`.
- A developer running `review --pr 7` (without an explicit `--graph`) sees the system auto-match the PR description against `planning_memory` and surface "Auto-matched PR #7 → GRAPH-ae12d394 (similarity=0.78)". Each review-side agent reports `contract_status` per owned criterion (PASS / FAIL / UNVERIFIED + evidence). A Contract Status panel renders alongside findings; `merge_recommendation` drops to `request_changes` if any `must_have` is `FAIL`.
- A developer running `review --pr 12` on a hand-written PR that doesn't correspond to any approved graph sees the system fall back gracefully to the P1 generic review flow — no error, no panel, just the existing closed loop. **Contract is enhancement, not prerequisite.**

### 8.2 Tasks

**Chunk A — Data model (~0.3 day) — DONE 2026-05-14**
- [x] `models.py` — `Criterion` (id, owner_agent, priority, category, assertion, rationale, suggested_check) + `Contract.owners()` / `criteria_for(agent_name)` helpers.
- [x] `models.py` — `Contract` (contract_id, graph_id, criteria, created_at).
- [x] `models.py` — `TaskGraph.contract: Optional[Contract] = None`. P2 graphs that don't set this remain valid.
- [x] `models.py` — `AgentFinding.criterion_id: Optional[str] = None`.
- [x] `models.py` — `CriterionStatus` (criterion_id, status, evidence: default empty).
- [x] `database.py` — `task_graphs.contract_json` column added; idempotent `ALTER TABLE ADD COLUMN` migration runs in `init_db` for pre-P4 schemas. `save_graph` / `load_graph` serialize and parse contract; absent contract is `None` (not error).
- [x] Inline smoke test covers: Criterion/Contract construction + helpers; CriterionStatus defaults; AgentFinding.criterion_id optional; full TaskGraph→DB→TaskGraph roundtrip with contract; save/load without contract; 4 existing P2 graphs in workspace.db still load cleanly with contract=None after migration; priority values stay in valid set.

**Chunk B — Multi-expert plan phase (~0.5 day) — DONE 2026-05-14**
- [x] `agents/base.py` — `review_requirement(requirement, repo_profile, memory)` method + `parse_requirement_response` + `_requirement_output_schema` + `_compact_profile` helper. Default `build_requirement_prompt` raises `NotImplementedError` so non-expert agents (e.g. BugFindingAgent) are filtered out by the selector.
- [x] `agents/security.py`, `uiux.py`, `testing.py`, `performance.py` — each implements `build_requirement_prompt` with a sharp angle-specific prompt. Each ends with the shared `_requirement_output_schema()` so output shape is uniform.
- [x] `agents/delivery.py` (new) — DeliveryAgent. `review()` deliberately raises (plan-phase only); the review-side contract verifier in Chunk D will check Delivery-owned criteria the same way as any other agent's.
- [x] `orchestrator/agent_selector.py` — `select_experts_for_plan(requirement, repo_profile)` rule-based: always Security+Testing+Delivery; UIUX if frontend keywords; Performance if backend or perf keywords. Returns 3–5 experts depending on requirement scope.
- [x] `orchestrator/planner.py` — `plan_with_experts(requirement, repo_profile)` fires all selected experts via `asyncio.gather` (`return_exceptions=True` so one failure doesn't sink the batch). P3 wrapper enforces the global concurrency cap. Each criterion is tagged with its `owner_agent` by the orchestrator (separation of concerns from the agent's perspective).
- [x] Verified end-to-end on petclinic with `"add a notes field to Pet entity, both backend and frontend"`: 5 experts ran in 35.3s wallclock, 0 errors, 29 total criteria with all 5 distinct owners present, each expert showed clear angle ownership (Security→XSS, Testing→round-trip, UIUX→a11y, Delivery→rollback, Performance→payload size). LLM usage: 5 req · 3,845 in / 6,666 out.

**Chunk C — Synthesizer + Architect Report UX (~0.5 day) — DONE 2026-05-14**
- [x] `orchestrator/planner.py` — `synthesize_report(expert_outputs, requirement)`: single LLM call ingests the raw expert pool (after `_flatten_experts_for_synth` strips debug fields) and returns a consolidated `{expert_summaries, clarify_questions, design_suggestions, draft_criteria}`. Semantic dedup ("sanitize XSS" + "escape HTML at render" → one entry with `owners: [Sec, UIUX]`), priority reconciliation when experts disagree (must_have > should_have > nice_to_have), polished assertion text.
- [x] `cli/build_cmd.py` — `_render_architect_report()` always rendered after the expert round (even when clarify Qs are empty — the experts' work is part of the demo). Three sections: expert perspectives (short attribution + 1-line take), Clarify questions (id + owners + question), Design suggestions sorted high→low with star ratings + owner_agent attribution. Draft Contract preview shows the top 5 criteria; the full 15 appear after planning.
- [x] `cli/build_cmd.py` — `_collect_report_picks()` parses multi-token input: `q1=max 2000 chars q2=optional s1 s2 s3 go` is tokenized via `_split_picks_line()` using regex boundaries. Empty line acts as implicit `go`. Validates ids against the report. Returns `(answers, accepted_ids, aborted)`.
- [x] `cli/build_cmd.py` — re-invokes `plan()` with `force_plan=True` and `clarify_history = formatted Q&A + accepted suggestions`, producing the final DAG. Renders Graph (existing) + Contract panel together.
- [x] `cli/build_cmd.py` — `_edit_loop` extended to dispatch on command prefix: graph commands (`a/e/d/s/n`) unchanged; contract commands (`ec`/`dc`/`ep`/`nc`) added. Helpers: `_edit_criterion_assertion`, `_edit_criterion_priority`, `_delete_criterion`, `_add_criterion`. Edit history feeds `planning_memory` writeback.
- [x] On approve, `Contract` model constructed from edited criteria and attached to `TaskGraph.contract`; `save_graph` persists everything in one `task_graphs` row including `contract_json`. `add_plan(...)` writeback document text extended with contract summary line (`"Contract: N criteria — X must, Y should, Z nice. Owners: [...]"`) so future similar builds can retrieve "this kind of feature usually has N must_haves with these owners".
- [x] Verified end-to-end on petclinic with `"add a notes field to Pet entity, both backend and frontend"`: 8 stages, 7 LLM calls total (5 expert + 1 synth + 1 final plan), wallclock ~2 minutes. User picks (2 Q answers + 3 suggestions) were reflected in the final DAG. Planner memory retrieved 2 past builds. `GRAPH-1db32ede` saved with 4 nodes + 15 contract criteria. LLM usage: 7 req · 12,605 in / 11,739 out tokens.

**Chunk D — Contract-aware review + auto-match (~0.4 day) — DONE 2026-05-14**
- [x] `orchestrator/runner.py` — `run_review` accepts optional `graph_id`, `pr_description`, `auto_match` parameters; loads contract via `load_graph(graph_id)["contract"]` when explicit, else falls back to auto-match flow.
- [x] `orchestrator/runner.py` — `find_graph_for_pr(pr_description, min_similarity=0.4)` uses `query_relevant_plans`. Returns the loaded graph when top1 ≥ threshold and top1 ≥ 0.9× top2 (clear winner), `{"_ambiguous": [...]}` when two are close, `None` when nothing crosses threshold. `RiskReport.contract_summary` was added (preferring a proper field over stashing on `agents_skipped`).
- [x] `cli/review_cmd.py` — accepts `--graph GRAPH-xyz` (explicit) and `--no-graph` (skip both explicit + auto-match). Default behavior: fetch PR description via PyGithub (`github_client.get_pr_description`), then auto-match.
- [x] `orchestrator/runner.py` — agent_selector result is unioned with contract owners; agents added by union get a reasoning entry "included by contract — owns N criteria".
- [x] `agents/base.py` — `review()` accepts `owned_criteria: list[dict] = None`; when non-empty, `_contract_block()` is appended to the prompt asking the LLM to emit `contract_status: [{criterion_id, status, evidence}]` inside the reasoning JSON. If the LLM forgets, BaseAgent synthesizes UNVERIFIED stubs so the renderer doesn't lose criteria silently.
- [x] `cli/review_cmd.py` — `_render_contract_status` renders a Rich table with status icon (✓ / ✗ / ?), priority-colored ID/priority cells, owner short name (4 chars), and assertion + evidence stacked in one cell. Panel title shows graph_id + pass/fail/unverified counts. Below the panel: a bold-red warning when any `must_have` is `FAIL` saying merge_recommendation was downgraded.
- [x] `models.py` — added `RiskReport.contract_summary: Optional[dict] = None`; runner sets it when contract in scope.
- [x] Graceful fallback verified: when no `--graph` and PR description doesn't semantic-match any plan, run_review proceeds without any contract logic — P1 §12 flow runs unchanged (`risk_report.contract_summary` stays None, no panel rendered, no behavioral changes).

End-to-end verified 2026-05-14 — `review --pr 1 --graph GRAPH-1db32ede`:
- Loaded contract (15 criteria from Chunk C-saved graph)
- Selected 5 agents (union with contract owners)
- Contract Status panel showed 0 PASS / 7 FAIL / 8 UNVERIFIED — semantically honest since PR #1's diff is "add notes to Visit" but the contract is for Pet (FAILs are real absence-of-evidence; UNVERIFIEDs cite "diff only modifies Visit.java, Pet controller/DTO not in scope")
- 5 must_have FAIL → "⚠ merge_recommendation downgraded to request_changes" printed
- GitHub post-back still worked (back-compat preserved)
- LLM usage: 6 req · 10,857 in / 5,617 out tokens

### 8.3 Test cases for verification

**Multi-expert plan correctness**
- [x] `plan_with_experts("add a notes field to Pet entity, both backend and frontend")` produces proposed_criteria from 5 distinct `owner_agent` values — verified 2026-05-14.
- [x] Every proposed criterion has `priority` ∈ {must_have, should_have, nice_to_have}, non-empty `assertion`, non-empty `owner_agent` — enforced by `parse_requirement_response` (Chunk B) + Criterion schema (Chunk A).
- [x] The TaskGraph DAG and Contract are saved together on approve (one `task_graphs` row, `contract_json` populated) — verified 2026-05-14 (`GRAPH-1db32ede` saved with 4 nodes + 15 contract criteria in a single row).
- [x] Re-loading the graph reconstructs the Contract with all criteria intact — verified 2026-05-14 at the model+DB layer (Chunk A smoke test) and at the application layer (Chunk C end-to-end).

**Architect Report UX**
- [x] Report displays after every expert round — verified 2026-05-14 (always-shown via `_render_architect_report` regardless of clarify_questions count).
- [x] User can answer multiple Qs and accept multiple suggestions on one input line — verified with input `q1=max 2000 chars q2=optional s1 s2 s3 go` producing 2 answers + 3 accepted suggestions.
- [x] Skipped suggestions: design_suggestions and contract criteria are independent layers in the synthesizer output. Accepted suggestions are injected into the planner prompt for DAG generation; rejected suggestions don't appear in the prompt. Contract criteria can be edited independently in Stage 7 (`ec`/`dc`/`ep`/`nc`).
- [x] Accepted high-priority suggestions DO appear as `must_have` criteria — synth-driven priority reconciliation enforces "higher wins", verified 2026-05-14 with 8 must_have criteria after picks.

**Contract-aware review**
- [x] `review --pr N --graph GRAPH-xyz` loads the contract and renders the Contract Status panel — verified 2026-05-14.
- [x] Each criterion is marked PASS / FAIL / UNVERIFIED with an evidence string — verified (every criterion in the run had an evidence string, including thoughtful UNVERIFIED reasoning citing "diff only modifies Visit.java").
- [x] At least one `must_have` `FAIL` causes `risk_report.merge_recommendation` to become `request_changes` — verified (5 must_have FAIL → downgrade triggered, warning printed).
- [x] All `must_have` PASS and zero high-severity new findings → `merge_recommendation` stays `approve` — code path preserved (downgrade only fires `if any_must_fail`).
- [x] Without `--graph` and no auto-match: review behaves identically to P1 — `contract_summary` stays None, no panel renders, P1 §12 closed-loop unchanged.

**Auto-match (planning_memory → PR)**
- [x] PR with description containing the original requirement text auto-matches to the correct graph at similarity ≥ 0.4 — code path implemented via `find_graph_for_pr` using `query_relevant_plans` (the same planning_memory layer used in P2 build retrieval); semantic match was previously demonstrated end-to-end at 0.563 similarity in the planning_memory smoke test.
- [x] Two approved graphs with similar requirements → ambiguity detected (top1 ≈ top2) — `find_graph_for_pr` returns `{"_ambiguous": [...]}` when top2 > 0.9 × min_similarity; review_cmd logs "Auto-match ambiguous (top: …) — generic review; pass --graph to disambiguate".
- [x] PR with description unrelated to any approved graph → falls back to generic review — `find_graph_for_pr` returns None when top similarity < threshold.
- [x] `--no-graph` flag forces generic review — `auto_match = not no_graph and graph_id is None` short-circuits before fetching PR description.

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

## 12. End-to-end verification path (regression smoke)

Originally the Day-3 acceptance test for the closed memory loop. Now kept as the
shortest e2e regression smoke covering the P1 review/reflect/memory loop. Was
last run against current `main` on 2026-05-14; rerun if anything in `agents/`,
`memory/vector_store.py`, `database.py`, or `orchestrator/runner.py` changes
substantially.

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

    # Review-side entrypoint (P1 + P4 Chunk D)
    async def review(
        self,
        task: TaskSpec,
        diff: str,
        file_contents: dict,
        repo_profile: dict,
        memory: dict,                                  # from query_relevant_memory()
        owned_criteria: list[dict] | None = None,      # P4: criteria this agent must verify
    ) -> tuple[list[AgentFinding], dict]:              # (findings, reasoning)
        ...

    # Plan-phase entrypoint (P4 Chunk B)
    # Expert agents (Security/UIUX/Testing/Performance/Delivery) opt in by
    # overriding build_requirement_prompt; non-experts (BugFindingAgent)
    # raise NotImplementedError and are filtered out by select_experts_for_plan.
    async def review_requirement(
        self,
        requirement: str,
        repo_profile: dict,
        memory: dict | None = None,
    ) -> dict:
        # Returns:
        #   {
        #     "perspective_summary": "...",
        #     "clarify_questions":   [...],
        #     "design_suggestions":  [{priority: high|medium|low, ...}, ...],
        #     "proposed_criteria":   [{priority: must_have|should_have|nice_to_have, ...}, ...],
        #     "_raw_response":       "...",
        #     "_stop_reason":        "...",
        #   }
        ...

    @abstractmethod
    def build_prompt(self, task, diff, file_contents, repo_profile, memory) -> str: ...

    # Optional, overridden only by expert agents (P4 Chunk B)
    def build_requirement_prompt(self, requirement, repo_profile, memory) -> str: ...
```

The review LLM must return JSON of this shape (validated in `parse_response`):

```json
{
  "reasoning": {
    "codebase_understanding": "string",
    "rejected_candidates": [
      {"issue": "...", "why_rejected": "...", "confidence_to_reject": 0.0-1.0}
    ],
    "confidence_per_finding": {"finding_0": 0.0-1.0},
    "contract_status": [
      {"criterion_id": "c1", "status": "PASS|FAIL|UNVERIFIED",
       "evidence": "file:line or why-unverifiable"}
    ]
  },
  "findings": [
    {"severity": "low|medium|high|critical", "category": "...", "title": "...",
     "detail": "...", "suggestion": "...", "file": "...", "line": 42,
     "criterion_id": "c1"}
  ]
}
```

`contract_status` is required when the agent receives `owned_criteria`; missing
criteria get synthesized as `UNVERIFIED` stubs by `BaseAgent.review` so the
renderer never silently loses a criterion. Legacy fallback: bare array of
findings is accepted (no reasoning recorded).

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
# Core (P1)
init_db()                                                # idempotent — runs all
                                                         # CREATE TABLE IF NOT EXISTS
                                                         # + idempotent ALTER for migrations
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

# Task graphs (P2 + P4)
save_graph(graph)                                        # upsert; serializes nodes + contract
load_graph(graph_id) -> dict | None                      # rebuilds via TaskGraph(**)
list_graphs() -> list[dict]                              # summary rows, no nodes_json
update_node_status(graph_id, node_id, new_status) -> bool

# Observations (observability slice)
save_observation(*, observation_id, trace_id, type, ...) # one row per LLM call
get_observations_by_trace(trace_id) -> list[dict]        # oldest first; tree built client-side
get_observation(observation_id) -> dict | None
```

Tables (after all migrations have run):

| Table | Owner | Notes |
|---|---|---|
| `tasks` | P1 | Status machine (`PENDING / IN_PROGRESS / REVIEWING / DONE / …`) |
| `task_findings` | P1 | Per-finding row; `accepted IS NULL` = pending triage |
| `execution_log` | P1 + extensions | Append-only event stream; see §13.4 |
| `task_graphs` | P2 + P4 | `nodes_json` + `contract_json` (idempotent ALTER for pre-P4 rows) |
| `observations` | Observability slice | Langfuse-style: `type` discriminator + `parent_observation_id` + `replayed_from_id`; indexed by `trace_id` and `parent_observation_id` |

### 13.4 Execution log event types

Actually emitted by the code on `main` (verified by grep on `log_execution(`):

| `event_type` | `agent` | `payload` shape | Emitted from |
|---|---|---|---|
| `agent_selection` | `"orchestrator"` | `{selected, skipped, reasoning, changed_files, contract_graph_id, contract_criteria_count}` | `orchestrator/runner.py` |
| `agent_result` | agent name | `{attempt, latency_ms, finding_count, status, reasoning, memory_injected, owned_criteria_count}` | `orchestrator/runner.py:execute_agent_with_retry` |
| `agent_retry` | agent name | `{attempt, latency_ms, error}` | `orchestrator/runner.py:execute_agent_with_retry` |
| `agent_error` | agent name | `{exception_type, exception_text, is_rate_limit, is_api_status, had_owned_criteria}` | `agents/base.py:review` (added in production-readiness bundle) |
| `contract_status` | `"orchestrator"` | `{criterion_id, status, evidence, owner_agent}` (one row per criterion) | `orchestrator/runner.py` (P4 Chunk D) |

**Not emitted** (despite earlier P3 design): `agent_queued`, `agent_started`,
`agent_timeout`, `budget_exceeded`. P3 was rescoped to an HTTP-layer wrapper
(see §7) that surfaces these counters via `usage_summary()` on the CLI rather
than through `execution_log`.

**Adjacent observability surfaces** (not via `execution_log`):

| Surface | Where it lives | Notes |
|---|---|---|
| `usage_summary` line | printed by `cli/review_cmd.py` + `cli/build_cmd.py` on every exit path | `{requests, retries, timeouts, input_tokens, output_tokens, budget_blocks}` |
| `observations` table | written by `agents/llm_client.py` wrapper | one row per LLM call; full request_kwargs + response; queryable via `trace show / replay` |

`get_agent_reasoning()` reads `agent_result` rows only.

---

## 14. Running the system (current state)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m cli.main     # auto-triggers `init` wizard on first run, opens shell
```

For **non-interactive / CI** use, every interactive command is also a click
subcommand. Exit codes follow CI conventions (`0` success · `1` gate
failure · `2` setup/runtime error):

```bash
python -m cli.main scan
python -m cli.main review --pr 42 --post --strict        # CI gate
python -m cli.main verify generate --diff --max 3
python -m cli.main verify run --diff                     # exit 1 on test fail
python -m cli.main verify health-check                   # CD gate; exit 0 if up
python -m cli.main repo list
python -m cli.main memory stats
```

`--strict` (review): exit 1 when any `must_have` contract criterion is
`FAIL` or any finding has severity `critical`. Advisory severity
(`high/medium/low`) is posted but doesn't fail the gate. A full
GitHub Actions workflow that wires these together for petclinic lives
at `examples/petclinic-ci.yml` — see §17 for the demo flow.

The `init` wizard asks for `ANTHROPIC_API_KEY` (required, hidden), model
(Sonnet 4.6 / Opus 4.7), optional `GITHUB_TOKEN` + `GITHUB_REPO`, and the
first repo path. Writes `.env` atomically (preserves any unmanaged keys
+ comments), verifies the Anthropic key with a tiny test call, scans +
auto-registers the first repo. Re-run `init` any time to switch model
or keys; advanced settings (`ANTHROPIC_MAX_CONCURRENT`,
`REVIEW_ALLOWED_REPOS`, `REVIEW_POST_COMMENTS`, `ANTHROPIC_TOKEN_BUDGET`)
live in `.env` for manual editing — see `.env.example`.

Shell commands:

| Command | Purpose |
|---|---|
| `init` | Re-run the setup wizard (keys, model, first repo) — auto-triggered if `ANTHROPIC_API_KEY` is unset |
| `scan` | Build repo profile (P1); auto-registers + activates the repo if none is active |
| `review --pr N [--branch X] [--graph GRAPH-xyz \| --no-graph] [--post \| --no-post]` | Multi-agent review of a PR, with optional contract auto-match (P1 + P4) |
| `reflect [TASK-ID]` | Human triage of pending findings (P1) |
| `logs [TASK-ID]` | Execution log timeline (P1) |
| `build "<requirement>"` | Multi-expert plan → Architect Report → Contract → TaskGraph (P2 + P4) |
| `trace show <trace_id> [--prompt]` | Rich tree of all LLM observations for a trace (observability slice) |
| `trace replay <obs_id>` | Re-run one captured generation with an edited prompt; nest result in trace tree (observability slice) |
| `status` | List all tasks |
| `quit` | Exit shell |

---

## 15. Open design questions

To answer during walk-through prep, not before:

1. **Repo-scoped vs global memory** — should petclinic corrections surface when reviewing a Python project? Currently no repo filter on corrections.
2. **Memory bootstrapping** — first review is always cold-start. Should `scan` pre-populate from a curated `seed_corrections.json`?
3. **Confidence thresholds** (Design Doc §3.4) — where do they live? Today agents don't filter by confidence.
4. **Priority 2 advance engine — fully automatic vs always-pause-for-human?** Decided 2026-05-14: cut from scope (always-pause if it ever lands). The conservative breakdown + edit + persist path is what shipped; auto-advance stays documented as future work in the design doc.
5. **Priority 4 conflict definition** — "same file + line range" is simplest but may miss semantic conflicts across files. *Status:* deferred — the contract criteria pattern shipped (P4 Chunks A-D) sidesteps this by making each criterion explicitly owned and verified per agent; cross-agent contradiction handling at finding level is still open.
6. **Cross-task `trace_id` linking** *(new, observability slice)* — a `build` writes `trace_id = build-<uuid>` while a `review --pr N` writes `trace_id = TASK-PRN`. The two are joined only via `planning_memory` (semantic match on PR description). Should we add an explicit `parent_trace_id` to chain build → multiple reviews of the resulting PRs? See §16.6.

---

## 16. Production-readiness lessons (2026-05-14, real-PR testing)

After all 4 priorities landed, late-day testing on a real OSS repo (vaadin/hilla) exposed gaps the petclinic-toy demo never triggered. This section is the consolidated record: experiments run, what worked, what didn't, mitigations roadmap, and observability TODO. Negative results are kept — picking up *which hypothesis failed and why* is itself the take-home signal.

### 16.1 Scalability experiments

Setup: vaadin/hilla (Spring + React framework, ~1600 files post-scan). Three PRs at increasing sizes:

| PR | Files | Diff lines | Role |
|---|---|---|---|
| `petclinic#1` | 1 | 5 | Toy baseline (already in demo state) |
| `hilla#3429` | 5 | 328 | Medium |
| `hilla#4533` | 26 | 6924 | Large stress |

Five strategies measured on `hilla#4533`:

| # | Strategy | Wallclock | LLM calls | Retries | Findings | Est. cost |
|---|---|---|---|---|---|---|
| 1 | Sonnet 4.6, single-shot, max_retries=4 (default at the start of the session) | 52s | 3/5 ✗ | 8 | 4 (2 stubs) | $0.30 |
| 2 | Sonnet 4.6, single-shot, max_retries=6 + 60s backoff cap + Retry-After parsing | 275s | 5/5 ✓ | 7 | 7 | $0.65 |
| 3 | **Opus 4.7, single-shot** | **35s** ✓ | 5/5 | 0 | 5 | $4.09 |
| 4 | Sonnet + file grouping (4 groups × 4 agents) | 444s | 17 | 19 | 39 | $1.08 |
| 5 | Sonnet + grouping + dedup | 449s | 17 | 23 | 31 | $1.08 |

### 16.2 What worked

- **Tuning the retry policy:** `max_retries` 4 → 6, backoff base 1s → 2s with `min(2**attempt * base, 60s)` cap, plus parsing the `Retry-After` header on 429 responses. Net: Sonnet 4.6 no longer drops agents on large PRs (strategy #2 vs #1).
- **Model selection as a deliberate knob:** Opus 4.7's 500K ITPM (10× Sonnet) absorbs the 4-agent concurrent fan-out without rate-limiting at all (strategy #3). 8× faster than Sonnet, at 5–6× cost — the right trade for latency-critical interactive review; Sonnet better for batch/CI.
- **GitHub write safety:** discovered the hard way that switching `GITHUB_REPO` env without restarting led to posting comments to a public OSS PR. Fixed with a **two-gate design**: `--post` flag / `REVIEW_POST_COMMENTS=true` env opt-in (off by default) **and** a `REVIEW_ALLOWED_REPOS` allowlist enforced in `github_client.is_post_allowed_repo()`. Either gate closed = no write. Public-OSS write attempt now blocks with a clear "BLOCKED by allowlist" message even when the user explicitly passes `--post`.
- **Default branch detection:** `scanner._default_branch()` now auto-detects via `git symbolic-ref refs/remotes/origin/HEAD`, falling back to `main` then `master`. Was hardcoded to `master`; hilla uses `main`.
- **Observability hook:** `BaseAgent.review`'s except path now writes an `agent_error` event to `execution_log` capturing exception type + message + whether it was a rate-limit. Closes the gap where the original exception text was being thrown away inside `error_finding`'s in-memory-only fields.

### 16.3 What didn't work — file grouping (kept in code, default-disabled, honest record)

Hypothesized: split large PRs into ≤10-file / ≤40K-char groups and run agents per-group serially. Result: **strategies #4 and #5 are worse than the simple retry approach in every dimension** (wallclock, cost, retries, signal-to-noise of findings).

Why grouping didn't help:

1. **Anthropic ITPM is a rolling 60-second window.** Sequential groups still drain the same input-tokens budget within that minute. Group 1 leaves 50K tokens used; group 2 starts 30s later and tips the limit. Retries still trigger.
2. **Per-group selector + boilerplate overhead.** 17 LLM calls vs 5 for single-shot. The selector runs once (good) but every group re-sends the 12K diff prefix + 4 agents × ~3K boilerplate, doubling input cost without proportional value.
3. **Output token explosion.** Each agent sees 4 different file slices and produces findings on each, yielding 39 raw findings (vs 7 single-shot). Dedup by `(agent, file, line)` only catches the obvious overlap (39 → 31) because LLMs produce semantically similar but textually different findings on related code — exact-match dedup is the wrong tool.

The grouping code is preserved (with thresholds raised so it doesn't fire in default settings) as an artifact of the failed experiment, not pretending we didn't try. To make grouping useful would require *paired* fixes: lower concurrency inside each group, OR semantic-similarity dedup (likely another LLM call), OR a smarter "shared-context, sharded-findings" pattern none of the existing tools we surveyed do well.

### 16.4 Rate-limit mitigation roadmap (TODO, ranked by expected ROI)

Mitigations identified during testing but outside this delivery's scope:

| Approach | Idea | Why it might work |
|---|---|---|
| **Anthropic prompt caching** | Mark long static prompt prefixes (system prompt, repo_profile, memory injection block) with `cache_control: ephemeral`. Cached input tokens count against a separate, much larger limit. | Highest expected ROI — could cut effective input ITPM 60–90% on review workloads where the prompt prefix is mostly static across calls. The Anthropic SDK supports it natively. |
| **Diff-only review mode** | Drop full `file_contents`; agents see only the patch hunks (already truncated to 12K chars) + a small context window. | Cuts input tokens 70%+ on large PRs. Trade-off: agents can't see what's *around* the change. Probably the right default for PRs > 30 files. |
| **Adaptive concurrency** | Pre-flight estimate of total input tokens; if > 50K predicted, drop `max_concurrent` from 5 → 2 (or 1) so calls fit within rate window. | Solves rate-limit deterministically. Wallclock cost: ceil(N/2) × per-call latency vs concurrent. |
| **Multi-account / API-key rotation** | Round-robin across multiple `ANTHROPIC_API_KEY`s (different orgs or higher-tier keys). | Linearly scales ITPM. Operationally messy (key management, attribution). |
| **Small-PR culture** | Enforce a PR-size limit (e.g., 500 lines) via CI / pre-receive hook. | Doesn't change the tool, but most real review tools assume small PRs anyway. Cultural change is the cheapest fix when it lands. |
| **Tier upgrade** | Anthropic Tier 4+ rate limits. | Money, not engineering. Worth mentioning so reviewers know it's a knob. |
| **Anthropic Batches API** | The bulk asynchronous API for offline reviews has no synchronous rate limit. | Wrong for interactive review (turnaround hours), right for nightly batch / on-merge CI scans. |

### 16.5 Observability — current state vs proposed roadmap

**Current state (in place, working):**
- `execution_log` table with timestamped events: `agent_selection`, `agent_result`, `agent_retry`, `agent_error` (new, captures exception text), `contract_status`.
- `_raw_response` (first 8K) + `_stop_reason` captured inside reasoning JSON on every successful agent call.
- `logs <task_id>` command renders execution_log as a flat timeline.
- LLM `usage_summary()` printed at the end of every `build` and `review` (requests, tokens in/out, retries, timeouts, budget blocks).

**Industry survey (synthesized from training knowledge of LangSmith, Phoenix, W&B Weave, Helicone, Langfuse, OTel GenAI semconv) → recommended data model:**

```sql
ALTER TABLE execution_log ADD COLUMN trace_id        TEXT;
ALTER TABLE execution_log ADD COLUMN parent_span_id  TEXT;
ALTER TABLE execution_log ADD COLUMN span_id         TEXT;
ALTER TABLE execution_log ADD COLUMN kind            TEXT;  -- 'agent' | 'llm' | 'retrieval' | 'contract'

CREATE TABLE llm_call (
  span_id        TEXT PRIMARY KEY,
  trace_id       TEXT NOT NULL,
  agent_name     TEXT NOT NULL,
  model          TEXT NOT NULL,
  max_tokens     INTEGER,
  messages_json  TEXT NOT NULL,           -- full input messages (captures prompts we currently throw away)
  response_text  TEXT NOT NULL,           -- full response (not truncated to 8K)
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  latency_ms     INTEGER,
  finish_reason  TEXT,
  created_at     TIMESTAMP NOT NULL
);
```

Borrowed patterns:
- **LangSmith** — single recursive table with `parent_id` covers chain/llm/tool/retriever uniformly.
- **Phoenix** — `kind` enum tells the renderer what node to draw and which nodes can be replayed.
- **Helicone** — wrap the SDK at the choke-point so prompts can't be dropped accidentally.
- **Langfuse** — observation discriminator column fits SQLite cleanly.
- **OTel GenAI** — adopt `gen_ai.request.model`, `gen_ai.usage.input_tokens`, etc. naming so future OTel export is rename-free.
- **W&B Weave** — cost rollup from leaf to root (future enhancement).

### 16.6 Observability TODO

- [x] Wrap `client.messages.create()` once at the SDK boundary to capture every prompt + response — done 2026-05-14 in `agents/llm_client.py:_messages_create`. Every call writes one `observations` row with full `request_kwargs` (system + messages + model + tools + temperature) + `response.model_dump_json()`. Observation write is best-effort + never blocks the call path.
- [x] Schema migration — chose Langfuse-style instead of OTel-style. Single `observations` table with `type` discriminator (`generation | tool_call | span | event`) + self-referential `parent_observation_id` + `replayed_from_id`. Column names align with OTel GenAI semconv (`gen_ai.request.model` → `model`, `gen_ai.usage.input_tokens` → `input_tokens`, etc.) so an OTLP exporter is rename-free. Industry-survey rationale in 2026-05-14 research notes.
- [x] Full prompt/response capture — `observations.messages_json` stores the entire request kwargs as JSON (replaces the 8K `_raw_response` truncation); `observations.response_json` stores the SDK's full `model_dump_json()`. Smoke-tested on real petclinic PR #1 review — 4 generations captured with full prompts visible via `trace show --prompt`.
- [x] CLI `trace show <trace_id> [--prompt]` — done in `cli/trace_cmd.py`. Rich tree rendering by `parent_observation_id`; header summary panel with totals; `--prompt` expands messages + response inline (1500-char truncation per part).
- [x] CLI `trace replay <observation_id>` — done. Refuses non-generation types. Renders source prompt + original response, reads new user message via inline stdin (multi-line terminated by `.`), sends through the wrapper with `replayed_from_id` set via contextvar, renders new response, prints the new observation id for chained replay. Replays nest as children of their source in the tree.
- [x] Adopt OpenTelemetry GenAI attribute names — done. `provider='anthropic'`, `operation='chat'`, plus `model`, `input_tokens`, `output_tokens`, `finish_reason` already align. Forward-compatible with an OTLP exporter without column renames.
- [ ] Add `synth_result` event to `execution_log` (P4 Chunk C's Synthesizer is currently a black box). *Partly covered now*: synth's LLM call is observed under `agent_name='Synthesizer'` in `observations`. The structured `expert_summaries → draft_criteria` payload is still not logged separately.
- [ ] Persist `reflect` accept/reject events with timestamp + reason — currently SQLite-only via the `accepted` column, no audit trail in execution_log.
- [ ] Cross-task `trace_id` — link a `build` task with subsequent `review` tasks that consume its contract. Closes the "trace_id = task_id, no cross-task linking" gap noted earlier.

### 16.9 Observability slice landed 2026-05-14

Implemented the four-item Phoenix/Langfuse-style slice from §16.6 (items 1-6 in the table above). Total new code: `cli/trace_cmd.py` (~390 LoC) + ~180 LoC across `agents/llm_client.py`, `database.py`, `agents/base.py`, `orchestrator/{runner.py, planner.py, agent_selector.py}`, `cli/{main.py, build_cmd.py}`.

**Industry survey driving the design** (May 2026 research, condensed):

| Question | Industry answer | Our choice |
|---|---|---|
| Trace schema | Langfuse: 1 obs table + `type` discriminator + `parent_observation_id`. LangSmith/Phoenix: pure OTel span tree. | Langfuse — product queries like "all generations in this run" stay trivial |
| Replay scope | Universal: prompt-level only (LangSmith / Phoenix / Braintrust playgrounds). Full agent-run replay is a research problem (non-idempotent tools, sampling, wall-clock). | Prompt-level only. `replayed_from_id` link. Document the limitation. |
| Wire format | OTel GenAI semconv attribute names — de-facto stable in 2026 even though spec is "experimental". | Adopt the naming; no OTLP exporter yet, but free future-proofing |
| Payload storage | Serious tools = object storage by ref; small tools = inline JSON. | Inline JSON column — fine at our scale; trivial to move to filesystem later |
| Trace context propagation | Anthropic Claude Code: `TRACEPARENT` through subprocess. LangSmith: thread-local. Phoenix: OTel contextvars. | Python contextvars (`set_trace_context`) — propagates across `await` and `asyncio.gather` automatically. |

**End-to-end demo path** (verified 2026-05-14 on petclinic PR #1):

```
review --pr 1                                # writes 4 obs under TASK-PR1
trace show TASK-PR1                          # tree: AgentSelector + 3 agents, 5525 in/1577 out, 33.5s
trace show TASK-PR1 --prompt                 # expand full prompts inline
trace replay obs-623cda5284c7                # edit SecurityAgent's last user msg, send, diff
trace show TASK-PR1                          # replay nests as child of obs-623cda5284c7
```

**What's deliberately not done** (kept honest):
- `trace replay --edit` with `$EDITOR` — user chose inline-stdin only to dodge editor-detection edge cases. Multi-line input terminated by `.` works in pipes and ttys both.
- `synth_result` structured event — synthesizer's LLM call is captured under `agent_name='Synthesizer'` but the structured `{expert_summaries, draft_criteria}` payload still only lives in CLI output. Tracked under §16.6.
- Cross-task `trace_id` linking — a `build`'s contract and the `review` consuming it use different trace_ids (`build-<uuid>` vs `TASK-PR<n>`). Listed in §16.6, design doc future work.
- Object-storage payload backend — inline JSON is fine for current scale (largest call ≈ 25KB). Would migrate to `file:<hash>` references at ~1MB+ calls.
- Bug fix piggybacked: `orchestrator/runner.py` was importing `query_relevant_plans` from `memory.vector_store` without importing it. Surfaced during the e2e smoke; fix is a single-line import.

### 16.7 Memory layer limitations

Status of each gap originally listed in this section:

- ~~**No eviction.**~~ Resolved 2026-05-15 by the memory slice (§16.10). `memory prune` evicts LRU subject to pinned / age-floor / size-floor safeguards. `last_accessed_at` is bumped on retrieval. CLI-triggered (not auto-evicting); manual is the right shape for a take-home where the *process* is the demoable artifact, but a cron / size-threshold trigger is a natural production extension.
- **No per-engineer / per-team namespacing.** Still not addressed. Per-repo namespacing landed (§16.10) — the engineer/team layer is its closest cousin and would slot in by adding `user_id` / `team_id` to the same metadata + extending the two-phase retrieval to three phases (user → team → repo → cross). Priority 5 in the original plan (design-only).
- **No memory freshness hint to the LLM.** Still not addressed. `[own-repo]` / `[cross-repo]` markers (§16.10) give the LLM a *provenance* hint but not a *recency* hint. Cheapest fix: include `days_since_last_access` in the formatted memory line.
- ~~**No memory compaction.**~~ Resolved 2026-05-15 by `memory compact`: cosine-sim clustering + LLM-driven merge with per-cluster human confirm. Phoenix/LangSmith/Braintrust prompt-playground pattern adapted to memory entries.
- **No access control.** Still not addressed. Correct for a single-developer tool, worth flagging for team deployment.

What's still unaddressed (left to design doc / future work):

- **Cross-repo "weight" tuning.** Currently `[cross-repo]` items just appear after `[own-repo]` in the prompt with a one-line instruction to weight own higher. The LLM may not respect this consistently. A cleaner fix is to reweight similarity scores (multiply cross-repo by, say, 0.7) before merging the two phases — but that's prompt-engineering territory and needs empirical tuning.
- **Orphan cleanup.** `repo remove` without purge leaves entries un-attached; they still count toward cross-repo retrieval for any other repo. A `memory cleanup-orphans` command could delete them, but the soft-cleanup semantics ("dead repo's wisdom still applies elsewhere") are arguably the right default.

### 16.10 Memory slice landed 2026-05-15

Repo-scoped memory + LRU prune + LLM-driven compact. Implements what §16.7 originally listed as deferred. New code: `cli/repo_cmd.py` (~190 LoC) + `cli/memory_cmd.py` (~190 LoC) + `cli/memory_compact.py` (~250 LoC). Modified: `memory/vector_store.py` (full repo-scoped rewrite, ~340 LoC), `database.py` (+repo_registry CRUD), 6 call sites threaded with `repo_id` (`runner.py`, `planner.py`, `build_cmd.py`, `reflect_cmd.py`, `review_cmd.py`, `main.py`).

**CLI surface (new):**

```
repo list                                # registered repos + per-repo memory counts
repo add <path> [--name X] [--use]       # register; optional activate
repo use <id>                            # switch active
repo remove <id>                         # double-prompt: typed-id confirm, then optional purge

memory stats [--repo X]                  # per-repo breakdown across all 3 collections
memory prune [--repo X] [--age-floor-days N] [--max-per-collection N] [--dry-run]
memory compact [--repo X] [--threshold T] [--dry-run] [--auto-yes]
```

`reflect` gained a `p+` option: reject + pin the resulting correction.

**Design decisions (from 2026-05-15 conversation, locked):**

| Question | Decision | Rationale |
|---|---|---|
| Strict isolation vs hierarchical retrieval | Hierarchical (own → cross fallback, marked) | "Trivial getter/setter" wisdom generalizes; strict isolation throws it away |
| Active repo source | Explicit CLI (`repo use`); `scan` auto-registers + auto-activates if no active | First-time path stays 0-friction; subsequent switches are intentional |
| Migration from pre-slice data | Clean wipe of chroma_db + workspace.db | User explicitly chose simplicity over migration; tests rewritten to new schema |
| Pruning policy | LRU + age-floor + size-floor + pin | Pure LRU evicts cold-but-valuable; the three safeguards are the minimum to keep "rare team conventions" from being lost |
| Compact UX | Manual, per-cluster `[y/N/q]` confirm | Auto-compact via LLM is an audit nightmare; manual `--dry-run` is the demoable shape that Phoenix/LangSmith/Braintrust converged on for prompt-playground analogs |
| Repo remove purge | Optional `[y/N]` second prompt; default N | Soft cleanup leaves entries as cross-repo wisdom for others; explicit `y` for actual purge |

**Industry alignment:**

- Repo-scoped metadata filter is standard multi-tenant ChromaDB usage (`where={"tenant_id": ...}`).
- Two-phase retrieval with origin tagging is novel here but parallels Langfuse's session/trace/observation hierarchy where higher-scoped items pin retrieval.
- Compact-via-LLM-cluster-merge mirrors MemGPT / Letta hierarchical-memory summarization; the per-cluster confirmation is what differentiates this from those agents' auto-summarize loops.

**End-to-end demo (verified 2026-05-15):**

```
scan                       # auto-registers + activates 'spring-petclinic-reactjs'
repo list                  # shows the repo, ● in active column
review --pr 1              # writes findings_memory + execution_log + observations under repo_id=spring-petclinic-reactjs
reflect                    # accept some, reject some with p+ to pin a correction
memory stats               # per-repo counts
memory prune --dry-run     # shows what LRU would evict if older than 7d, max 50/coll
memory compact --dry-run   # cluster similar corrections, propose merges (no commit)
memory compact             # commit merges, per-cluster confirm
repo add /tmp/other-repo --use   # switch repos
review --pr 1              # second repo: gets [own-repo] (its own) + [cross-repo] (petclinic's wisdom)
```

**What's not done (and why):**

- **Synchronous compaction trigger inside `reflect`.** Was considered: "after N rejections, auto-suggest a compact run." Deferred — compact is a deliberate maintenance verb, not a side effect of triage. Cleaner mental model.
- **`memory show <id>` to inspect a single entry.** Would be cheap, but `memory stats` + grepping ChromaDB is sufficient at current scale. Listed in design doc future work.
- **Per-engineer / per-team layer.** Same metadata pattern would slot in (just add `user_id` to filter), but multi-tenancy semantics open a real design rabbit hole. Stays in design doc.

### 16.11 Brief-coverage slice landed 2026-05-15

Three new review-side agents to close the remaining gaps from §3.4 against the brief's "supported workflows" list:

| Agent | Scope | Distinct from |
|---|---|---|
| **ArchitectureAgent** | Layering violations, misplaced files, tight coupling, module-boundary breaks, new cross-cutting concerns | Refactoring (method/file scope); Security (correctness) |
| **RefactoringAgent** | Long methods, duplication, naming, dead code, type-safety upgrades, testability | Architecture (module scope); BugFinding (correctness) |
| **TestGenerationAgent** | Proposes runnable test code in `suggestion` field — JUnit / Jest / pytest based on file ext | TestingAgent (reviews existing test coverage, doesn't propose new tests) |

Each follows the existing `BaseAgent` pattern (~80-115 LoC). ArchitectureAgent also implements `build_requirement_prompt` so it can opt into plan-phase expert review when the requirement implies structural change (gated by `_ARCH_KEYWORDS` in agent_selector).

**Selector updates:**
- `_rule_based_hints` now signals `crosses_packages` (3+ distinct dirs in changed_files) and `has_new_file_likely` (new service / controller / repository file).
- LLM selector prompt knows about all 8 agents + when to pick / skip each.
- Fallback path (LLM selection fails) biases toward including the new agents — better over-include than miss coverage on a fallback.
- Plan-phase: ArchitectureAgent added conditionally when requirement contains structural-change keywords (module / boundary / refactor / migration / etc.); skipped for pure additive feature work.

**Selector behavior verified on petclinic PR #1** (single-file `Visit.java` change adding `notes` field):
- Selected: SecurityAgent, BugFindingAgent, TestingAgent, **TestGenerationAgent**, **RefactoringAgent** (5/8)
- Skipped with clean reasons: UIUX (backend-only), Performance (no perf signal), Architecture (single-file localized, no cross-package — exactly the right call)
- 11 findings (vs 0 in the pre-slice baseline), Risk = HIGH, recommendation = request_changes
- TestGeneration produced two complete runnable JUnit 5 tests (parameterized null/empty + happy-path round-trip) with correct imports and AssertJ assertions
- Refactoring produced two specific findings: "TODO comment should be a constraint or tracked issue" + "accessor methods out of order vs class layout"
- Cost: 6 LLM req · 9,734 in / 5,161 out tokens (vs baseline 4 req · 5,525 in / 1,577 out — +50% req, ~2× cost, but 3× more useful signal)

**On "regression detection":** Deliberately not built as a separate agent. Two mechanisms already provide it:
1. **`corrections_memory` retrieval at agent runtime** — when a new finding semantically matches a past rejected one, the retrieved correction goes into the prompt and the agent should suppress the duplicate. This is *regression-of-false-positive prevention*.
2. **P4 Contract enforcement** — `must_have` criteria from plan phase are re-verified at review time; `FAIL` → `merge_recommendation = request_changes`. This is *regression-of-deliberate-requirement detection*.
A dedicated RegressionAgent would have overlapped with both. Design doc records this as an architectural choice. The user is separately thinking about what additional definition of "regression" they want — that may unlock a future agent.

**Token cost note.** Adding 3 agents increases per-review cost. The selector skipping unnecessary agents keeps this bounded — on small diffs only 3-4 agents fire; on big diffs all 8 might. The HTTP-layer wrapper (P3) still enforces session-wide concurrency cap and optional `ANTHROPIC_TOKEN_BUDGET`, so the worst case is graceful degradation, not runaway cost.

### 16.12 Verify slice landed 2026-05-15

Externally-driven e2e testing as the project's CD-validation surface. Reframes the brief's "CI/CD validation workflows" into something demoable and useful: instead of parsing GitHub Actions YAML, we extract the target system's operational picture (build/run/test commands + API surface) and generate Python e2e tests that hit the running system from outside.

**Key architectural decisions (locked in 2026-05-15 conversation):**

| Question | Decision |
|---|---|
| Where do generated tests live? | `.ai-workspace/generated-tests/<repo_id>/` — workspace-scoped, never pollutes the target repo |
| What's the testing language? | Python + pytest + `requests` regardless of target system's language (we're an external tester) |
| Who manages the target system's lifecycle? | User. Demo flow: user runs `mvn spring-boot:run` (or similar) in another terminal, our tool points at `$VERIFY_TARGET_URL` (default `http://localhost:8080` or detected port). Reason: sandbox adaptation is a separate project; we focus on the AI loop |
| Test-payload strategy | Bias toward **generic, high-signal** checks that don't require domain knowledge: GET → 200 + JSON shape; POST/PUT empty body → 4xx (validation works); /{id} → 404 for nonexistent. Happy-path POST with full payload is the LLM's weakest angle (doesn't know real schemas) |
| Failure handling | After `verify run`, each FAILED test gets an LLM analysis pass. Classification: `test-bug-script` / `test-bug-payload` / `test-bug-config` / `regression` / `flaky`. Only `script` + `payload` write a `test-gen-lesson` to corrections_memory (those are actionable at next-generation time); `config` is environmental, `regression` is a real bug to surface, `flaky` is noise |
| API extraction priority | OpenAPI YAML/JSON if present (most accurate; petclinic uses openapi-codegen) > Spring annotation regex > Express route regex. Higher-priority source wins on (method, path) collisions |

**5th memory layer:** `test_catalog`. Strict repo-isolation (a test for petclinic doesn't apply to hilla). Two query modes:
- Deterministic: `query_tests_by_apis([POST /api/visits, ...])` — exact-match on APIs covered. Used by `verify run --diff` for impact selection.
- Semantic: `query_tests_by_description("notes field validation")` — ChromaDB top-K over the catalog's flow descriptions. Used by `verify catalog search`.

**Verified on petclinic** (Spring Boot + React + OpenAPI spec, 2026-05-15):

```
scan
→ Runtime: react + spring-boot · port 9966
→ APIs:    36 endpoints (6 DELETE, 14 GET, 9 POST, 7 PUT)

verify generate --no-diff --max 2
→ Generated 2 Python pytest files in .ai-workspace/generated-tests/spring-petclinic-reactjs/
→ test_owners_crud.py + test_vets_and_specialties.py
→ Each covers 3-5 endpoints, includes validation + 404 + happy-path GET

verify run             # without the server running (intentional, exercises analyzer)
→ All tests FAILED with ConnectionRefusedError
→ Per-test failure analysis: classified all as TEST-BUG-CONFIG
→ Lesson NOT saved (config = environmental, not generation-actionable)
→ Suggestion to user: start the service or set VERIFY_TARGET_URL

verify list
→ Catalog table: 2 entries, both FAIL with last_run timestamp

verify catalog search "list owners returns JSON"
→ test_owners_crud sim=0.40, test_vets_and_specialties sim=0.20
```

**The closed loop** (regression detection in a fuller sense):
1. PR touches `VisitController.java` → diff
2. `verify generate --diff` produces a new test for the touched API (or reuses existing one in catalog)
3. `verify run --diff` queries catalog for tests covering `POST /api/visits` etc., runs them
4. Test fails → analyzer classifies → if test-bug-payload, lesson goes to corrections_memory
5. Next `verify generate --diff` retrieves that lesson into its prompt → smarter test code → fewer test-bugs over time
6. Test catalog accumulates across PRs → coverage grows → regression net widens

**What's deliberately not done:**

| Item | Why |
|---|---|
| Target-system spin-up / teardown (Popen-based) | Sandbox adaptation is its own project; demo assumes user runs the system |
| Custom CI YAML parsing (GHA, Travis, Drone) | Not the AI value-add — that's a parser project. We detect *presence* of CI config files (recorded in `runtime.ci_config`), nothing more |
| Multi-language test generation (JUnit, Jest natively) | We chose to **always** generate Python, since we test from outside. Project language doesn't matter |
| Happy-path POST with full payloads | LLM-without-schema-context produces too many false negatives. Validation tests are more reliable and still cover the API surface |
| Generated-test execution as a step inside `review` | Two separate flows by design (locked in conversation). Review = fast static analysis. Verify = slow runtime-against-deployed |

### 16.13 CI/CD pipeline Phase 1 landed 2026-05-15

The verify slice closed the e2e-testing loop. This phase wires the whole pipeline together as a runnable GitHub Actions workflow on a self-hosted runner. Phase 2 (the `apply` command for `/apply`-comment-driven auto-fix) is wired in the workflow YAML but the CLI itself lands next.

**What this slice adds:**

1. **Non-interactive click subcommands** — every interactive command is now also `python -m cli.main <subcommand>` with exit codes suitable for CI (`0` success, `1` gate failure, `2` setup error). The interactive shell stays unchanged.

2. **`review --strict`** — CI-gate exit code. Fails on `must_have` contract FAIL or `critical` finding. Advisory severities (`high/medium/low`) post to the PR but don't break the gate. Reasoning: review is help, not authority — only blockers should block.

3. **`verify health-check`** — CD-gate command. Probes `$VERIFY_TARGET_URL` (or detected health endpoint), exit `0` if service responds, `1` if not. Used in the workflow's "wait for petclinic ready" step in a retry loop.

4. **Health endpoint detection at scan time** — `runtime.health_endpoint` set from OpenAPI paths matching `/health` / `/healthz` / `/actuator/health` / `/ping` / `/status` / `/ready` / `/live`, or inferred from Spring Actuator presence in pom.xml.

5. **ArchitectureAgent flags missing health endpoint** — when scan finds no health path on a deployable backend (Spring Boot / Express / FastAPI), the agent emits a HIGH-severity finding with framework-specific suggestion. This is the deliberate "review pushes for better deployability" loop the user asked for. Verified on petclinic: no health endpoint → ArchitectureAgent finds it.

6. **`verify generate` deduplicates against catalog** — before paying tokens, check if any catalog entry already covers a target API. Drop covered ones from the prompt. First run = full sweep; subsequent runs incremental. Resulting structure stays flat (`<test_id>.py`) since `test_catalog` already provides semantic indexing.

7. **Persisted demo tests** — `.ai-workspace/generated-tests/` removed from .gitignore; petclinic's 2 generated tests now ship in the repo as reproducible artifacts. A reviewer who clones sees example output without running anything.

8. **`examples/petclinic-ci.yml`** — full GitHub Actions workflow:
   - `pull_request` job: scan → review --strict --post → mvn package → start petclinic → poll health-check → verify generate --diff → verify run --diff → teardown
   - `issue_comment` job (gated by `contains(body, '/apply')`): runs `apply --pr N --comment-id C --push` to commit AI-applied fixes to the PR branch. (Phase 2 — CLI for `apply` lands next.)

**Verified on petclinic (2026-05-15) — non-interactive entrypoints:**

```
$ python -m cli.main scan
  Runtime: react + spring-boot · port 9966 · no health endpoint  ← surfaced
  APIs:    36 endpoint(s) (6 DELETE, 14 GET, 9 POST, 7 PUT)

$ python -m cli.main verify health-check
  ✗ Could not reach http://localhost:9966: Connection refused
  exit=1

$ python -m cli.main repo list
  (Rich table with active marker, exit 0)
```

Click subcommand surface aligns with interactive shell — same names + flags, same output, only difference is exit code propagation. No new behavior, all wiring.

**Phase 2 landed 2026-05-15 (see §16.14).** README write-up still pending — deliberately deferred per user direction so the project can be trimmed + adjusted first.

### 16.14 AI-apply loop landed 2026-05-15

The third human-in-the-loop reflection loop in the system. Symmetric with `reflect` (review → human accept/reject) and `verify` (test failure → analysis → lesson), this one closes the loop on **applying suggested fixes back to the PR branch**.

**Trigger flow:**

```
AI posts review comment with finding_id + /apply hint
  ↓
Human types: /apply f-7a3b   (or with extras: /apply f-7a3b also keep nullable)
  ↓
GHA issue_comment event → workflow `ai-apply` job
  ↓
python -m cli.main apply --pr N --comment-id C --target-path WORKSPACE --push
  ↓
1. Fetch comment body via PyGithub
2. Parse /apply <finding_id> [extra instructions]
3. Load finding from task_findings table
4. Read target file at the target path
5. LLM produces complete modified file content (honors extra instructions)
6. Diff vs original, render to console
7. If --push: git add + commit + push to PR branch
```

**Verified on petclinic (synthetic finding, real LLM call, 2026-05-15):**

Input — finding "Notes field lacks length constraint; suggest @Size(max=2000)" + user extra "also keep it nullable".

LLM produced this diff (16 lines total):
```diff
+import jakarta.validation.constraints.Size;
...
-    @Column(name = "notes")
+    @Size(max = 2000)
+    @Column(name = "notes", nullable = true)
     private String notes;
```

The agent: (1) added the right import in the right alphabetical position, (2) added `@Size(max=2000)` on the field, (3) honored the user's extra instruction by adding `nullable = true` to the existing `@Column`. No collateral changes elsewhere in the file.

**Safety:**

| Gate | Behavior |
|---|---|
| `REVIEW_ALLOWED_REPOS` allowlist | Same gate as `--post`. Out-of-allowlist target refuses to push even with `--push`. |
| `--push` opt-in | Default is preview: write the file locally, render the diff, skip git operations. CI workflows pass `--push` to close the loop. |
| Detached-HEAD refuse | If the target checkout is in detached-HEAD state, refuse to push (would create dangling commit). |
| Single-file scope | One `/apply` = one finding = one file. Multi-file refactors out of scope. |
| LLM output not validated pre-write | Fail-forward: write whatever the LLM produced. The next CI run on the new commit catches a broken build. |
| `finding.criterion_id` ignored | Apply uses the finding's `file` + `suggestion`, not contract criteria. Contract enforcement remains a review-time concern. |

**Workflow integration:**

`examples/petclinic-ci.yml` already has the `ai-apply` job wired (Phase 1 commit included this forward-looking YAML). It fires on `issue_comment.created` when the body contains `/apply`, checks out the PR head ref, and invokes:

```yaml
python -m cli.main apply \
  --pr ${{ github.event.issue.number }} \
  --comment-id ${{ github.event.comment.id }} \
  --target-path ${{ github.workspace }} \
  --push
```

**What's still ahead:**

- README major section: "Demo with self-hosted runner" — install runner, register, set secrets, walk through end-to-end loop. User explicitly deferred this so they can trim/adjust the project first.
- Design doc Tradeoffs + Evaluation-Against-Brief sections.
- Multi-finding `/apply f-abc f-def` batching, conflict resolution across files, pre-write syntax check — all future work.

### 16.8 Other production gaps from this session

| Gap | Discovered when | Mitigation |
|---|---|---|
| Default branch hardcoded to `master` | hilla uses `main` | Auto-detect via `git symbolic-ref refs/remotes/origin/HEAD`, fallback to `main` then `master`. Fixed in `scanner/repo_scanner.py:_default_branch()`. |
| GitHub URL hardcoded in success message | Showed `jessejia1991/...` even when GITHUB_REPO=vaadin/hilla | Now reads from env. Fixed in `cli/review_cmd.py`. |
| GitHub write safety | Accidentally posted comments to vaadin/hilla PR #3429 during a test run | Two-gate safety: `--post`/`--no-post` CLI flag + `REVIEW_POST_COMMENTS=true` env opt-in + `REVIEW_ALLOWED_REPOS` allowlist. Default behavior is no-write. |
| Agent failure swallowed exception text | error_finding lived in-memory only, status="failed" got filtered before save | `agent_error` event now logged from BaseAgent except path with exception type + text + rate-limit flag. |

---

*Generated end of day May 14. Next session: backfill design doc (.docx) Tradeoffs and Evaluation-Against-Brief sections from §16, plus README.*
