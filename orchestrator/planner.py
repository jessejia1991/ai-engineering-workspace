"""
Task graph planner with clarify gate.

The planner takes a natural-language requirement and a repo profile, then
returns ONE of:
  - {"action": "plan",     "graph": {"nodes": [...]}, "reasoning": "..."}
  - {"action": "clarify",  "reason": "too_vague|too_complex|ambiguous_target",
                           "questions": [...], "narrow_options": [...],
                           "reasoning": "..."}

The state machine is bounded: a clarify response is followed by exactly one
re-invocation with `force_plan=True`, which forbids another clarify round.
This guarantees the conversation cannot run away.

planning_memory injection (4th ChromaDB layer) hooks in here in a later
chunk; for now the planner runs cold every time.
"""

import os
import json
from dotenv import load_dotenv
from agents.llm_client import client    # P3: rate-limited HTTP wrapper
from memory.vector_store import query_relevant_plans, format_plans_for_prompt

load_dotenv()

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

NODE_TYPES = ["migration", "backend", "backend-test",
              "frontend", "frontend-test", "review"]

CLARIFY_REASONS = ["too_vague", "too_complex", "ambiguous_target"]


def _repo_profile_snippet(repo_profile: dict) -> str:
    """Compact, prompt-friendly summary of the repo so the LLM can ground
    node descriptions in real file paths instead of generic placeholders."""
    files = repo_profile.get("files", {})

    def _sample(key: str, n: int = 6) -> list[str]:
        return [f.get("path", str(f)) if isinstance(f, dict) else str(f)
                for f in files.get(key, [])[:n]]

    return (
        f"Project: {repo_profile.get('repo_id', 'unknown')}\n"
        f"File counts: backend={len(files.get('backend', []))}, "
        f"frontend={len(files.get('frontend', []))}, "
        f"test={len(files.get('test', []))}, "
        f"config={len(files.get('config', []))}\n"
        f"Backend sample: {_sample('backend')}\n"
        f"Frontend sample: {_sample('frontend')}\n"
        f"Test sample: {_sample('test')}\n"
    )


def _build_prompt(requirement: str, repo_profile: dict, *,
                  force_plan: bool, clarify_history: str = "",
                  past_plans_text: str = "") -> str:
    valid_types = " | ".join(NODE_TYPES)
    profile = _repo_profile_snippet(repo_profile)

    if force_plan:
        # Second-pass after a clarify round — must produce a plan.
        mode_instructions = f"""
You previously asked the user for clarification. Their answers are below.
You MUST now return action="plan". Do NOT ask more questions.

{clarify_history}
"""
    else:
        mode_instructions = ""

    past_section = f"\n{past_plans_text}\n" if past_plans_text else ""

    return f"""You are a task breakdown planner for a software engineering codebase.

Given a natural-language requirement and a repo profile, output ONE of:
(1) A task graph as a DAG, OR
(2) A clarify response if the requirement is genuinely too ambiguous to plan.

## Repo profile
{profile}
{past_section}
## Requirement
{requirement}

{mode_instructions}

## Output mode A — PLAN (preferred when requirement is concrete)
Return JSON of this exact shape:
{{
  "action": "plan",
  "reasoning": "one sentence on how you decomposed the requirement",
  "graph": {{
    "nodes": [
      {{
        "id": "n1",
        "type": "<one of: {valid_types}>",
        "description": "concrete one-sentence change, reference real files when possible",
        "dependencies": []
      }}
    ]
  }}
}}

Rules for nodes:
- id: short string. Use "n1", "n2", "n3" ... in DAG order.
- type: pick from the 5 valid types listed above.
- description: ONE sentence. Reference real files or symbols from the repo
  profile when relevant (e.g. "extend Pet entity in src/main/.../Pet.java").
- dependencies: list of upstream node ids.
- Topology rules: migration before backend; backend before frontend;
  tests follow their target (backend-test depends on backend, etc.).
- Typical full-stack feature: 3-6 nodes, mixing backend/frontend/test/migration.

## Output mode B — CLARIFY (use sparingly, only if truly necessary)
Return JSON of this exact shape:
{{
  "action": "clarify",
  "reason": "<one of: {' | '.join(CLARIFY_REASONS)}>",
  "reasoning": "one sentence on why you cannot plan yet",
  "questions": ["concrete Q1", "concrete Q2"],
  "narrow_options": ["Option 1: <smaller scope>", "Option 2: <smaller scope>"]
}}

When to use which reason:
- too_vague: requirement has no concrete target or dimension (e.g. "improve UX",
  "make it better"). Provide concrete questions; leave narrow_options empty.
- too_complex: requirement contains 3+ unrelated features (e.g. "rewrite auth,
  migrate DB, add MFA, update tests"). Provide narrow_options that each fit one
  build session; leave questions empty or short.
- ambiguous_target: requirement names something with multiple candidates in
  the repo (e.g. "fix the form" when there are 3 forms). Ask which one.

DO NOT clarify just to play it safe. If the requirement has a clear target
and a clear dimension and fits in one build session, output a plan even if
you have minor uncertainty — the user will edit the graph afterward anyway.

Return ONLY the JSON object. No preamble, no markdown fence.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    return text


def _validate_result(data: dict, *, force_plan: bool) -> dict:
    """
    Normalize and validate the LLM output. Mutates a copy and returns it.
    Raises ValueError with a clear message on schema failure.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    action = data.get("action")

    if action == "plan":
        graph = data.get("graph")
        if not isinstance(graph, dict) or "nodes" not in graph:
            raise ValueError("action=plan but graph.nodes missing")
        nodes = graph.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("action=plan but nodes list is empty")
        cleaned_nodes = []
        seen_ids: set[str] = set()
        for i, n in enumerate(nodes):
            if not isinstance(n, dict):
                continue
            nid = (n.get("id") or f"n{i+1}").strip()
            if nid in seen_ids:
                nid = f"{nid}_{i}"
            seen_ids.add(nid)
            cleaned_nodes.append({
                "id":           nid,
                "type":         n.get("type", "backend"),
                "description":  n.get("description", "").strip(),
                "dependencies": [d for d in n.get("dependencies", []) if isinstance(d, str)],
                "status":       "PENDING",
                "artifacts":    {},
                "pr_number":    None,
            })
        # Drop dependencies pointing to unknown ids — LLM occasionally
        # hallucinates "n7" while only producing n1..n5.
        valid_ids = {n["id"] for n in cleaned_nodes}
        for n in cleaned_nodes:
            n["dependencies"] = [d for d in n["dependencies"] if d in valid_ids]
        return {
            "action":    "plan",
            "reasoning": data.get("reasoning", ""),
            "graph":     {"nodes": cleaned_nodes},
        }

    if action == "clarify":
        if force_plan:
            raise ValueError(
                "Planner returned clarify on the force_plan pass — "
                "schema contract violated"
            )
        reason = data.get("reason", "too_vague")
        if reason not in CLARIFY_REASONS:
            reason = "too_vague"
        return {
            "action":          "clarify",
            "reason":          reason,
            "reasoning":       data.get("reasoning", ""),
            "questions":       [q for q in data.get("questions", []) if isinstance(q, str)],
            "narrow_options":  [o for o in data.get("narrow_options", []) if isinstance(o, str)],
        }

    raise ValueError(f"Unknown action: {action!r}")


async def plan(
    requirement: str,
    repo_profile: dict,
    *,
    force_plan: bool = False,
    clarify_history: str = "",
    max_tokens: int = 4000,
    use_planning_memory: bool = True,
) -> dict:
    """
    Run one planner invocation. Returns a validated dict per _validate_result.

    Arguments:
      requirement           Natural-language requirement from `build "..."`.
      repo_profile          Output of scan() — used to ground node descriptions.
      force_plan            True on the second call after a clarify round.
                            Forbids a recursive clarify response.
      clarify_history       Human-readable "Q: ... / A: ..." block injected into
                            the prompt on the force_plan pass.
      use_planning_memory   When True, semantically retrieve past approved
                            builds and inject them. The force_plan pass also
                            keeps memory (the memory may help the LLM honor
                            user style across calls).

    The caller is responsible for re-invoking with force_plan=True after
    presenting clarify questions to the user.
    """
    # 4th-layer memory injection: pull semantically-similar past builds so
    # the planner can skip redundant clarify rounds and follow the user's
    # established decomposition style.
    past_plans = query_relevant_plans(requirement, top_k=3) if use_planning_memory else []
    past_plans_text = format_plans_for_prompt(past_plans)

    prompt = _build_prompt(
        requirement, repo_profile,
        force_plan=force_plan,
        clarify_history=clarify_history,
        past_plans_text=past_plans_text,
    )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    stop_reason = response.stop_reason

    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Planner returned non-JSON output (stop_reason={stop_reason}): "
            f"{raw[:300]}..."
        ) from e

    result = _validate_result(data, force_plan=force_plan)
    result["_raw_response"]    = raw[:4000]
    result["_stop_reason"]     = stop_reason
    result["memory_injected"]  = {
        "planning_hits":   len(past_plans),
        "planning_titles": [
            (p.get("metadata", {}) or {}).get("requirement", "")[:120]
            for p in past_plans
        ],
    }
    return result


# ===================================================================
# P4 Chunk B — Multi-expert plan-phase orchestration
# ===================================================================
# `plan_with_experts` runs 2-5 expert agents concurrently against the
# requirement. Each expert returns its own angle (perspective_summary,
# clarify_questions, design_suggestions, proposed_criteria). The
# returned dict is the *raw* expert pool — Chunk C's Synthesizer turns
# it into an Architect Report + final plan + Contract.
#
# Concurrency is bounded by the RateLimitedAnthropicClient semaphore
# (P3, default max_concurrent=5), so a 5-way fan-out fits in one
# window without rate-limit cascades.

import asyncio


async def plan_with_experts(
    requirement: str,
    repo_profile: dict,
) -> dict:
    """
    Run all relevant plan-phase experts concurrently against the
    requirement. Returns a dict with per-expert outputs plus the
    selection metadata. Synthesis to a final plan + contract is the
    next layer's job.

    Returns:
        {
          "requirement":      str,
          "selection":        AgentSelection,
          "expert_outputs":   {agent_name: review_requirement_result, ...},
          "errors":           {agent_name: error_str, ...},   # subset
        }
    """
    from orchestrator.agent_selector import select_experts_for_plan
    from orchestrator.runner import AGENT_REGISTRY
    # DeliveryAgent lives outside the existing review-side AGENT_REGISTRY
    # because it has no review() implementation. Pull it in directly.
    from agents.delivery import DeliveryAgent

    selection = select_experts_for_plan(requirement, repo_profile)

    # Build the expert pool: review-side agents from registry + delivery
    plan_registry = dict(AGENT_REGISTRY)
    plan_registry["DeliveryAgent"] = DeliveryAgent()

    experts = [
        plan_registry[name]
        for name in selection.selected
        if name in plan_registry
    ]

    # Fire all experts concurrently. RateLimitedAnthropicClient (P3)
    # already enforces a global concurrency cap, so we don't need to
    # add our own semaphore here.
    coros = [
        e.review_requirement(requirement, repo_profile, memory={})
        for e in experts
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    expert_outputs: dict[str, dict] = {}
    errors:         dict[str, str]  = {}
    for agent, result in zip(experts, results):
        if isinstance(result, Exception):
            errors[agent.name] = str(result)
            continue
        # Tag owner_agent onto each proposed_criterion so downstream
        # code doesn't need to thread the agent name separately.
        for c in result.get("proposed_criteria", []) or []:
            c["owner_agent"] = agent.name
        # Also tag design_suggestions for display attribution.
        for s in result.get("design_suggestions", []) or []:
            s["owner_agent"] = agent.name
        expert_outputs[agent.name] = result

    return {
        "requirement":    requirement,
        "selection":      selection,
        "expert_outputs": expert_outputs,
        "errors":         errors,
    }


# ===================================================================
# P4 Chunk C — Synthesizer
# ===================================================================
# Single LLM call that consolidates 5 expert outputs into a
# triage-ready Architect Report. Semantic dedup ("sanitize XSS" ≈
# "escape HTML on render"), priority reconciliation when experts
# disagree, and polished assertion text in one pass.

def _flatten_experts_for_synth(expert_outputs: dict) -> dict:
    """Pass synth only the actionable fields, not debug metadata."""
    flat = {}
    for agent_name, output in expert_outputs.items():
        flat[agent_name] = {
            "perspective_summary": output.get("perspective_summary", ""),
            "clarify_questions":   output.get("clarify_questions", []),
            "design_suggestions":  [
                {k: s.get(k, "")
                 for k in ("priority", "category", "suggestion", "rationale")}
                for s in output.get("design_suggestions", []) or []
            ],
            "proposed_criteria":   [
                {k: c.get(k, "")
                 for k in ("priority", "category", "assertion", "rationale", "suggested_check")}
                for c in output.get("proposed_criteria", []) or []
            ],
        }
    return flat


def _build_synth_prompt(requirement: str, flat_experts: dict) -> str:
    payload = json.dumps(flat_experts, indent=2)
    agent_names = list(flat_experts.keys())

    return f"""You are a project lead synthesizing input from {len(agent_names)} expert reviewers into a single Architect Report for a developer to triage.

## Original requirement
{requirement}

## Expert outputs (raw)
The following JSON contains each expert's perspective, clarify questions,
design suggestions, and proposed contract criteria. Experts: {", ".join(agent_names)}.

```json
{payload}
```

## Your job
Consolidate these into ONE report with:

1. **expert_summaries** — keep each expert's perspective_summary verbatim.

2. **clarify_questions** — merge near-duplicates across experts. If two
   experts asked the same underlying question (even worded differently),
   merge into one with `owners` listing all the asking agents. Aim for
   3–5 final questions. Each MUST be answerable in one short response.

3. **design_suggestions** — merge near-duplicates (semantic, not just
   string match). Keep priority high/medium/low from the original
   experts; when experts disagree on priority for a merged item, pick
   the HIGHER one. Tag `owner_agent` for single-owner suggestions or
   `owners` list for multi-owner. Sort: high first, then medium, then
   low. Aim for 8–12 final suggestions.

4. **draft_criteria** — same dedup pattern. Priority winner is the
   higher of must_have > should_have > nice_to_have. Polish the
   `assertion` so it reads like a contract clause: testable, specific,
   no platitudes. Aim for 10–15 final criteria.

## Output format
Return JSON in EXACTLY this structure (no preamble, no markdown fence):

{{
  "expert_summaries": {{
    "AgentName": "their perspective_summary verbatim",
    ...
  }},
  "clarify_questions": [
    {{
      "id": "q1",
      "owners": ["AgentName1", "AgentName2"],
      "question": "Polished question"
    }}
  ],
  "design_suggestions": [
    {{
      "id": "s1",
      "priority": "high|medium|low",
      "owner_agent": "AgentName",
      "owners": ["AgentName1", "AgentName2"],
      "category": "short-tag",
      "suggestion": "Polished one-sentence improvement",
      "rationale": "Why this matters"
    }}
  ],
  "draft_criteria": [
    {{
      "id": "c1",
      "priority": "must_have|should_have|nice_to_have",
      "owner_agent": "AgentName",
      "owners": ["AgentName1", "AgentName2"],
      "category": "short-tag",
      "assertion": "A verifiable statement, e.g. 'notes field has @Size(max <= 2000)'.",
      "rationale": "Why",
      "suggested_check": "static-analysis|runtime-test|manual"
    }}
  ]
}}

`owners` and `owner_agent` rules:
- Single-owner item → `owner_agent: "X"`, `owners: ["X"]`
- Multi-owner (deduped from N experts) → `owner_agent: <pick the most natural owner>`, `owners: [all contributors]`
"""


def _validate_synth_result(data: dict, raw: str, stop_reason: str) -> dict:
    """Normalize and lightly validate synth output. Errors-out only on
    structural failures; soft-coerces priority enums and missing fields."""
    if not isinstance(data, dict):
        raise ValueError("Synthesizer did not return a JSON object")

    def _norm_question(i: int, q: dict) -> dict:
        return {
            "id":       q.get("id", f"q{i+1}"),
            "owners":   [o for o in (q.get("owners") or []) if isinstance(o, str)],
            "question": str(q.get("question", "")).strip(),
        }

    def _norm_suggestion(i: int, s: dict) -> dict:
        priority = s.get("priority", "medium")
        if priority not in ("high", "medium", "low"):
            priority = "medium"
        owners = [o for o in (s.get("owners") or []) if isinstance(o, str)]
        owner_agent = s.get("owner_agent") or (owners[0] if owners else "")
        return {
            "id":          s.get("id", f"s{i+1}"),
            "priority":    priority,
            "owner_agent": owner_agent,
            "owners":      owners or ([owner_agent] if owner_agent else []),
            "category":    s.get("category", "general"),
            "suggestion":  str(s.get("suggestion", "")).strip(),
            "rationale":   str(s.get("rationale", "")).strip(),
        }

    def _norm_criterion(i: int, c: dict) -> dict:
        priority = c.get("priority", "should_have")
        if priority not in ("must_have", "should_have", "nice_to_have"):
            priority = "should_have"
        owners = [o for o in (c.get("owners") or []) if isinstance(o, str)]
        owner_agent = c.get("owner_agent") or (owners[0] if owners else "")
        return {
            "id":               c.get("id", f"c{i+1}"),
            "priority":         priority,
            "owner_agent":      owner_agent,
            "owners":           owners or ([owner_agent] if owner_agent else []),
            "category":         c.get("category", "general"),
            "assertion":        str(c.get("assertion", "")).strip(),
            "rationale":        str(c.get("rationale", "")).strip(),
            "suggested_check":  c.get("suggested_check", "manual"),
        }

    questions = [_norm_question(i, q) for i, q in enumerate(data.get("clarify_questions", []) or []) if isinstance(q, dict)]
    suggestions = [_norm_suggestion(i, s) for i, s in enumerate(data.get("design_suggestions", []) or []) if isinstance(s, dict)]
    criteria = [_norm_criterion(i, c) for i, c in enumerate(data.get("draft_criteria", []) or []) if isinstance(c, dict)]

    return {
        "expert_summaries":   dict(data.get("expert_summaries", {})),
        "clarify_questions":  questions,
        "design_suggestions": suggestions,
        "draft_criteria":     criteria,
        "_raw_response":      raw[:6000],
        "_stop_reason":       stop_reason,
    }


async def synthesize_report(
    expert_outputs: dict,
    requirement: str,
    max_tokens: int = 8000,
) -> dict:
    """
    LLM-driven synthesis: ingest the 5 expert outputs and produce a
    triage-ready Architect Report (one consolidated payload). One LLM
    call, structured output, semantic dedupe.

    Returns:
        {
          "expert_summaries":   {agent_name: summary},
          "clarify_questions":  [{id, owners, question}, ...],
          "design_suggestions": [{id, priority, owner_agent, owners, category, suggestion, rationale}, ...],
          "draft_criteria":     [{id, priority, owner_agent, owners, category, assertion, rationale, suggested_check}, ...],
          "_raw_response":      raw LLM text (truncated, debug)
          "_stop_reason":       end_turn | max_tokens | ...
        }
    """
    if not expert_outputs:
        # Nothing to synthesize — return an empty report rather than
        # making a wasted LLM call.
        return {
            "expert_summaries":   {},
            "clarify_questions":  [],
            "design_suggestions": [],
            "draft_criteria":     [],
            "_raw_response":      "",
            "_stop_reason":       "skipped_empty_input",
        }

    flat = _flatten_experts_for_synth(expert_outputs)
    prompt = _build_synth_prompt(requirement, flat)

    response = await client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    stop_reason = response.stop_reason

    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Synthesizer returned non-JSON output "
            f"(stop_reason={stop_reason}): {raw[:300]}..."
        ) from e

    return _validate_synth_result(data, raw, stop_reason)
