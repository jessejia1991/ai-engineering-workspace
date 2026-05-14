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
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from memory.vector_store import query_relevant_plans, format_plans_for_prompt

load_dotenv()

client = AsyncAnthropic()
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
