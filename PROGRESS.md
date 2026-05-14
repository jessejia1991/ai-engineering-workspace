# PROGRESS.md — AI Engineering Workspace

**Repo:** `jessejia1991/ai-engineering-workspace` (branch: `temp-branch`)
**Last commit:** `7e71c64 temp commit`
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

### 2.1 Three-layer memory (ChromaDB)

| Layer | Scope | Grows when | Used at |
|---|---|---|---|
| `findings_memory` | per-agent | Any finding saved during a review | Retrieval before each agent runs |
| `corrections_memory` | global across agents | Human rejects a finding in `reflect` with a reason | Retrieval before each agent runs |
| `repo_profile` | repo-level | `scan` command | Injected into every prompt |

Forgetting is by **time decay at retrieval time**, not deletion. Retrieval is **semantic top-K**, not full dump, so memory size doesn't blow up the prompt.

### 2.2 Hidden state made observable

Every agent returns `(findings, reasoning)` where `reasoning = {codebase_understanding, rejected_candidates, confidence_per_finding}`. `rejected_candidates` — what the agent considered but decided not to report — is the most important observable; it's how a human audits false negatives. Stored per-agent in `execution_log.payload` for `agent_result` events.

### 2.3 Dynamic agent selection

`orchestrator/agent_selector.py` combines rule-based hints (e.g., `*.java` → SecurityAgent + BugFindingAgent) with an LLM-based final selection. Skipped agents get logged with a reason.

### 2.4 Implicit evaluation via reflect

There is no separate eval pipeline. The human's accept/reject in `reflect` IS the evaluation — accepted findings reinforce memory, rejected ones produce corrections that subsequent reviews retrieve semantically.

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

**Missing:** `README.md`, `requirements.txt`, `.gitignore`, `tests/`.

### 3.2 What works (Day 1–3)

- **Day 1 (committed):** repo scanner, file classification, repo_profile generation, SQLite schema.
- **Day 2 (committed):** five agents inheriting from BaseAgent run in parallel via `runner.run_review`. Dynamic agent selection works. Findings post to GitHub via `github_client.post_review_comments`.
- **Day 3 (uncommitted, working tree):** ChromaDB memory wired in. Agents return `(findings, reasoning)`. `execution_log` captures reasoning. `reflect` writes corrections back to ChromaDB on reject. Both `review` and `reflect` CLI output surface reasoning (`codebase_understanding`, `rejected_candidates`, `memory_injected`).

### 3.3 Uncommitted changes in working tree

Five files modified, **not yet committed**:

| File | Change | Why |
|---|---|---|
| `database.py` | `get_agent_reasoning(task_id)` reads `agent_result` rows into per-agent dict (handles retries — last attempt wins). | Shared retrieval helper for reasoning display. |
| `database.py` | `clear_unreviewed_findings(task_id)` deletes findings where `accepted IS NULL`. | Re-review of same PR clears stale untriaged findings while keeping accepted/rejected as history. |
| `database.py` | `create_task` → `INSERT … ON CONFLICT DO UPDATE` upsert. | Re-running review resets task to PENDING instead of crashing on UNIQUE constraint. |
| `cli/review_cmd.py` | `_render_agent_reasoning()` called between "Agent Selection" and "Findings". | Make hidden state observable in review output. |
| `cli/reflect_cmd.py` | `_render_finding_reasoning()` called per-finding. | Human needs reasoning context before accept/reject (Design Doc §4.3). |
| `cli/main.py` | Removed duplicate `_cmd_reflect`; implemented `_cmd_logs`. | Cleanup + make `logs` command actually work. |
| `orchestrator/runner.py` | Import + call `clear_unreviewed_findings` after `create_task`. | Pairs with the create_task upsert. |

These passed isolated unit tests in the conversation that produced this doc. **End-to-end with real ChromaDB + Anthropic API has not been run yet** — that's Priority 1 below.

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

> **Cursor:** §5 Priority 1, first unchecked task. Last verified: none — start by running §12 verification path against real ChromaDB + Anthropic API.
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

For honesty's sake:

- **Confident:** Priority 1, Priority 2 conservative version, Priority 3, Priority 4, Priority 5 design section.
- **Stretch:** Priority 2 full auto-advance engine, Priority 6 Skills.
- **Fallback if running short:** Priority 4 can degrade to single-conflict demo + design discussion; Priority 3 can drop the cost-cap feature.

The walk-through follow-up is live — so **demoability of the end-to-end example matters more than feature completeness**. Better to land Priority 2 conservative + 3 + 4 with a clean demo than half-built versions of all six.

---

## 5. Priority 1 — Submission essentials

### 5.1 User stories

- A reviewer cloning the repo can install dependencies, set environment variables, and run their first review by following only the README — no chat history required.
- Reading the design doc, the reviewer sees explicit ownership of what was cut and why, instead of having to infer it from code.
- The reviewer can map every one of the brief's 7 evaluation dimensions to a corresponding section in the design doc.

### 5.2 Tasks

- [ ] Run §6 end-to-end verification path (review → reflect → second review with non-zero memory injected)
- [ ] Commit + push the 5-file working-tree change to `temp-branch`
- [ ] Write `requirements.txt` (anthropic / aiosqlite / chromadb / sentence-transformers / click / rich / pydantic / python-dotenv / PyGithub / gitpython)
- [ ] Write `.gitignore` (`.ai-workspace/`, `__pycache__/`, `venv/`, `.env`)
- [ ] Write `README.md` (architecture diagram, quickstart, command reference, demo path)
- [ ] Design doc — add "Tradeoffs and Limitations" section
- [ ] Design doc — add "Evaluation Against Brief" section mapped to the brief's 7 dimensions

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
- [ ] `models.py` — add `TaskNode` (id, type ∈ {frontend / backend / test / migration / review}, description, dependencies, status, artifacts, optional pr_number)
- [ ] `models.py` — add `TaskGraph` (nodes, edges, root_requirement, current_node_id)
- [ ] `database.py` — add `task_graphs` table + CRUD (`save_graph` / `load_graph` / `update_node_status`)

**Breakdown**
- [ ] New `orchestrator/planner.py` — input: natural-language requirement + repo_profile; output: `TaskGraph`
- [ ] LLM prompt — output must be a DAG (not flat list), with explicit dependency edges
- [ ] Breakdown must produce typical node mix: frontend, backend, test, migration

**Human-in-the-loop interaction**
- [ ] New `cli/build_cmd.py` — interactive `build "<requirement>"`
- [ ] Display generated `TaskGraph` (tree or table)
- [ ] Node-level operations: approve / edit / split / merge / delete
- [ ] **Decision before coding:** single-pass breakdown + edit (1 day) vs multi-turn design conversation (3 days). Default to single-pass.
- [ ] Persist confirmed graph

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
- [ ] `build "add a notes field to Pet entity"` produces a graph with at least 3 distinct node types (backend + frontend + test minimum)
- [ ] The breakdown DAG has no cycles (topological sort succeeds)
- [ ] Backend node is a dependency of frontend node (frontend depends on backend API existing first)
- [ ] Migration node is a dependency of backend node
- [ ] Test node depends on the code it tests (backend test depends on backend node, etc.)

**Human-in-loop interaction**
- [ ] User can delete a node in shell and the deleted node disappears from re-rendered graph
- [ ] User can edit a node's description in shell and the new description persists after re-rendering
- [ ] Approving a graph writes it to the `task_graphs` table; reopening the shell and querying retrieves the same graph
- [ ] Approving an empty graph (all nodes deleted) fails gracefully with a clear error

**Persistence**
- [ ] Two `build` calls produce two distinct rows in `task_graphs` with unique IDs
- [ ] Loading a saved graph reconstructs all node attributes and edges intact

**Advance engine** *(only if implemented)*
- [ ] Running the advance engine on a 3-node graph (backend → test → frontend) executes them in topological order, never reverses
- [ ] When a node enters AWAITING_HUMAN, the engine pauses and returns control to the shell
- [ ] A node marked BLOCKED prevents its downstream nodes from being picked up
- [ ] Mock-merge writes to `.ai-workspace/merged_changes/<node_id>/` and updates node status to MERGED

---

## 7. Priority 3 — Agent scheduling layer

### 7.1 User stories

- A reviewer running a review on a large diff (20+ files) sees agents queued and dispatched safely — concurrent agents never exceed the configured limit, no single agent hangs forever, and the system reports actual token spend.
- When an agent times out or hits a rate limit, the reviewer sees a clear log entry instead of a generic Python traceback, and the remaining agents continue to completion.
- *(Demo moment)* The reviewer can inspect the `logs` output and see queue depth, start time, completion time, and token spend per agent.

### 7.2 Tasks

**Core abstraction**
- [ ] New `orchestrator/scheduler.py` — `AgentScheduler` class replacing `asyncio.gather` in `runner.py`
- [ ] Semaphore for max concurrent agents (configurable, default 3)
- [ ] `TokenBudget` tracker — accumulate per-task tokens, warn/reject on overrun
- [ ] Per-agent timeout (configurable, default 60s)

**Safety**
- [ ] Failure isolation — one agent crash does not block others (partial today; verify)
- [ ] Rate-limit handling — backoff-retry vs give-up logic on Anthropic 429
- [ ] Cost cap — total cost ceiling per review, abort early if exceeded

**Observability**
- [ ] `execution_log` — add event types: `agent_queued` / `agent_started` / `agent_timeout` / `budget_exceeded`
- [ ] `logs` command — render these events
- [ ] Design doc — explain how this layer keeps large diffs safe

**Validation**
- [ ] Use Priority 2's end-to-end example. Inject a deliberately-slow node to trigger timeout.

### 7.3 Test cases for verification

**Concurrency limits**
- [ ] With semaphore = 2 and 5 agents to run, at most 2 are in `agent_started` state without a corresponding `agent_result` at any moment in the execution log
- [ ] All 5 agents eventually reach `agent_result` (none stuck in queue forever)

**Timeout**
- [ ] An agent that sleeps longer than the configured timeout gets killed and an `agent_timeout` event is logged
- [ ] After a timeout, the remaining agents complete normally
- [ ] A timed-out agent does not corrupt `task_findings` (no partial finding rows)

**Token budget**
- [ ] Setting `TokenBudget.max_tokens = 100` and running a review that would consume more aborts with a `budget_exceeded` event
- [ ] Budget tracking accumulates across agents (not reset between agents within one task)
- [ ] Within-budget review completes without any `budget_exceeded` event

**Rate-limit handling**
- [ ] A mocked 429 response triggers backoff-retry (logged as `agent_retry`)
- [ ] After N consecutive 429s, the agent fails cleanly with a clear error finding (no infinite retry loop)

**Failure isolation**
- [ ] If `SecurityAgent` raises an unhandled exception, `BugFindingAgent` still runs to completion and its findings appear in `task_findings`
- [ ] The crashed agent produces an `agent_result` with `status="failed"` and an error message, not a missing row

**Observability**
- [ ] `logs TASK-PR1` shows the full lifecycle for each agent: `agent_queued` → `agent_started` → `agent_result` (or `agent_timeout` / `agent_retry`)
- [ ] Each event has a timestamp; the timestamps form a coherent timeline

---

## 8. Priority 4 — Multi-agent trade-off review

### 8.1 User stories

- A developer reviews a PR where SecurityAgent says "add input validation" and PerformanceAgent says "this code is on the hot path, validation will cost ~15% latency." Instead of two independent findings, the developer sees a single trade-off matrix with options (add validation / don't add / add with caching) and a recommended choice with rationale.
- The developer picks an option in `reflect`, including notes on why the other options were rejected. The decision feeds into memory.
- On a second review of the same PR, the SynthesizerAgent surfaces the prior trade-off preference and applies it (or flags that the prior choice is contradicted by new evidence).

### 8.2 Tasks

**SynthesizerAgent**
- [ ] New `agents/synthesizer.py` — second-pass agent. Input: other agents' findings + reasoning. Output: trade-off matrix.
- [ ] Prompt design — require conflict identification, cost quantification, default recommendation with rationale
- [ ] Pydantic schema — `TradeoffDecision`, `TradeoffOption`, `AffectedAgents`

**Conflict detection**
- [ ] Group flat findings by code location (file + line range)
- [ ] Same-location, multi-agent → trade-off flow; single-agent → existing accept/reject
- [ ] Design doc — define exactly what counts as a conflict

**Trade-off display**
- [ ] `reflect` — detect trade-off matrix findings and render differently (table + highlighted recommendation)
- [ ] Human choice is now "pick option N", not accept/reject
- [ ] ChromaDB write-back — correction records "why not the other options" as future trade-off preference memory

**Demo**
- [ ] Construct a PR with a deliberate conflict (e.g., validation slows hot path)
- [ ] Run full trade-off flow
- [ ] Second review of same PR — verify Synthesizer retrieves prior trade-off preference

### 8.3 Test cases for verification

**Conflict identification**
- [ ] Two agents producing findings on the same `file + line range` triggers the Synthesizer
- [ ] Two agents producing findings on different lines do NOT trigger the Synthesizer (each goes through standard accept/reject)
- [ ] An empty findings list does not crash the Synthesizer (returns empty trade-offs)
- [ ] A single agent's multiple findings on the same line do not trigger the Synthesizer (no conflict to resolve)

**Trade-off matrix output**
- [ ] Synthesizer output passes Pydantic validation (no malformed `TradeoffOption` allowed through)
- [ ] Every trade-off includes at least 2 options
- [ ] Every trade-off has exactly one `recommended_option_id`
- [ ] Every option includes the affected agents' positions

**Reflect display**
- [ ] `reflect` renders trade-off matrices in a different visual style than standard findings (table format)
- [ ] User input accepts option IDs (1, 2, 3), not y/n
- [ ] Selecting an option records the choice in `task_findings` with the chosen option captured
- [ ] User can add a free-text rationale when selecting an option

**Memory write-back**
- [ ] Selecting an option writes a correction to ChromaDB tagged as trade-off type
- [ ] The correction includes both the chosen option AND the rejected options (with reasons)
- [ ] `get_stats()` shows `corrections_in_memory` increased by 1 per trade-off resolved

**Second review (closed loop)**
- [ ] Running review on the same PR a second time injects the prior trade-off preference into the Synthesizer's prompt (visible in `execution_log` as memory_injected)
- [ ] If the new diff matches the old conflict pattern, Synthesizer applies the prior preference (recommendation matches)
- [ ] If the new diff has different evidence, Synthesizer can override the prior preference (logged explicitly)

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
