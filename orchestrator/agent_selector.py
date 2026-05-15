import os
import json
from dotenv import load_dotenv
from agents.llm_client import client, set_trace_context    # P3 wrapper + observability
from models import AgentSelection

load_dotenv()

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _rule_based_hints(changed_files: list[str]) -> dict:
    """
    Deterministic hints based on file paths.
    These are passed to the LLM as context, not as hard rules.
    The LLM makes the final decision.
    """
    has_backend = any(
        f.endswith(".java") or f.endswith(".py") or f.endswith(".go")
        for f in changed_files
        if "test" not in f.lower()
    )
    has_frontend = any(
        f.endswith((".ts", ".tsx", ".js", ".jsx"))
        and any(p in f for p in ["client/", "frontend/", "src/"])
        for f in changed_files
    )
    has_test_only = all(
        "test" in f.lower() or f.endswith(("Test.java", "Tests.java", ".test.ts", ".spec.ts"))
        for f in changed_files
    ) if changed_files else False
    has_db = any(
        "repository" in f.lower() or "entity" in f.lower()
        or f.endswith(".sql") or "migration" in f.lower()
        for f in changed_files
    )
    has_controller = any(
        "controller" in f.lower() or "handler" in f.lower()
        for f in changed_files
    )

    # ArchitectureAgent signals: structural changes — new files, files in
    # different packages (cross-package touching), or large diffs.
    distinct_dirs = {os.path.dirname(f) for f in changed_files if "/" in f}
    has_new_file_likely = any(
        # Heuristic — we don't have git status here, but new entities /
        # controllers in non-test paths are usually new functionality.
        ("Controller" in f or "Service" in f or "Repository" in f)
        and "test" not in f.lower()
        for f in changed_files
    )
    crosses_packages = len(distinct_dirs) >= 3

    return {
        "has_backend":         has_backend,
        "has_frontend":        has_frontend,
        "has_test_only":       has_test_only,
        "has_db":              has_db,
        "has_controller":      has_controller,
        "crosses_packages":    crosses_packages,
        "has_new_file_likely": has_new_file_likely,
    }


async def select_agents(
    task_description: str,
    changed_files: list[str],
    diff_summary: str,
    repo_profile: dict,
) -> AgentSelection:
    """
    LLM decides which agents to run based on:
    - what files changed
    - what the task is about
    - rule-based hints (deterministic signals passed as context)
    """
    hints = _rule_based_hints(changed_files)

    available_agents = {
        "SecurityAgent": "Finds security issues: missing validation, auth problems, data exposure, injection risks",
        "BugFindingAgent": "Finds bugs: null handling, logic errors, missing error handling, edge cases",
        "TestingAgent": "Reviews existing test coverage: missing tests near the diff, weak assertions",
        "TestGenerationAgent": "Proposes runnable new test code (JUnit/Jest/pytest) for changed behavior — pick when diff adds logic that isn't trivially covered",
        "PerformanceAgent": "Finds performance issues: N+1 queries, expensive loops, large payloads",
        "UIUXAgent": "Reviews UI/UX: loading states, error display, accessibility — ONLY for frontend changes",
        "ArchitectureAgent": "Critiques structural concerns: layering violations, misplaced files, tight coupling, new module boundaries — pick when the diff crosses packages, introduces new top-level files, or touches public API shape",
        "RefactoringAgent": "Method/file-scoped code-quality: long methods, duplication, naming, dead code, type-safety — pick for non-trivial code changes; skip for pure docs / config",
    }

    agents_text = "\n".join([f"- {name}: {desc}" for name, desc in available_agents.items()])
    files_text = "\n".join([f"  - {f}" for f in changed_files]) if changed_files else "  (none)"
    corrections = repo_profile.get("corrections", [])
    corrections_text = "\n".join([f"  - {c['note']}" for c in corrections[-3:]]) if corrections else "  None"

    prompt = f"""You are an AI orchestrator deciding which code review agents to run for a pull request.

## Task description
{task_description}

## Changed files
{files_text}

## Signals detected from changed files
- Has backend code changes: {hints['has_backend']}
- Has frontend code changes: {hints['has_frontend']}
- Has only test file changes: {hints['has_test_only']}
- Has database/repository changes: {hints['has_db']}
- Has controller/handler changes: {hints['has_controller']}
- Crosses 3+ distinct directories: {hints['crosses_packages']}  (signal for ArchitectureAgent)
- Likely new service/controller/repository file: {hints['has_new_file_likely']}  (signal for ArchitectureAgent + TestGenerationAgent)

## Diff summary
{diff_summary[:500] if diff_summary else 'No diff available'}

## Known corrections about this codebase
{corrections_text}

## Available agents
{agents_text}

## Rules
- UIUXAgent MUST ONLY run when frontend files (.ts, .tsx, .js, .jsx in client/ or frontend/) are changed
- If only test files changed, focus on TestingAgent and BugFindingAgent (TestGenerationAgent doesn't help — the tests ARE the change)
- Always include at least one agent
- SecurityAgent is especially important when controller or entity files change
- ArchitectureAgent should fire on cross-package diffs or when new top-level service/controller files appear; skip for single-file localized changes
- RefactoringAgent should fire on any non-trivial code change; skip for pure config / docs / generated code
- TestGenerationAgent should fire when the diff adds behavior (new method, new branch, new endpoint) without adding tests in the same diff; skip on pure refactors / renames

Select which agents should run and which should be skipped.
For each skipped agent, provide a brief reason.

Return ONLY a JSON object in this exact format:
{{
  "selected": ["SecurityAgent", "BugFindingAgent"],
  "skipped": {{
    "UIUXAgent": "no frontend files changed",
    "PerformanceAgent": "no database or query changes"
  }},
  "reasoning": {{
    "SecurityAgent": "controller file changed with potential validation issues",
    "BugFindingAgent": "entity changes may introduce null handling issues"
  }}
}}
"""

    try:
        set_trace_context(agent_name="AgentSelector")
        response = await client.messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()

        # JSON 파싱
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        data = json.loads(raw)
        return AgentSelection(
            selected=data.get("selected", []),
            skipped=data.get("skipped", {}),
            reasoning=data.get("reasoning", {}),
        )

    except Exception as e:
        # Fallback: rule-based safe defaults if LLM selection fails.
        # Bias toward including the new agents — better to over-include
        # than to miss coverage on a fallback path.
        selected = ["SecurityAgent", "BugFindingAgent", "TestingAgent",
                    "RefactoringAgent"]
        if hints["has_frontend"]:
            selected.append("UIUXAgent")
        if hints["has_db"]:
            selected.append("PerformanceAgent")
        if hints.get("crosses_packages") or hints.get("has_new_file_likely"):
            selected.append("ArchitectureAgent")
        if not hints.get("has_test_only"):
            selected.append("TestGenerationAgent")

        return AgentSelection(
            selected=selected,
            skipped={},
            reasoning={"fallback": f"Agent selector failed ({e}), using rule-based defaults"},
        )


# ===================================================================
# P4 Chunk B — Expert selection for plan phase
# ===================================================================
# The plan-phase expert pool is different from the review-phase agent
# pool: BugFindingAgent doesn't have a plan-phase angle (it works on
# code that exists), and DeliveryAgent is plan-phase only.
#
# Selection is rule-based — there is no diff to look at, and an extra
# LLM call to pick among 5 experts is more cost than signal. The rules
# always include Security + Testing + Delivery (every change has those
# angles), and conditionally include UIUX (frontend mentioned) and
# Performance (backend / data / scale mentioned).

PLAN_EXPERTS_ALWAYS = ["SecurityAgent", "TestingAgent", "DeliveryAgent"]

_BACKEND_KEYWORDS = [
    "entity", "api", "endpoint", "database", "schema", "migration",
    "controller", "dto", "service", "repository", "mapper", "column",
    "backend", "java", "spring",
]
_FRONTEND_KEYWORDS = [
    "form", "input", "button", "component", "ui", "ux", "page", "screen",
    "react", "typescript", "tsx", "frontend", "client", "client-side",
    "css", "responsive", "accessibility", "a11y",
]
_PERF_KEYWORDS = [
    "performance", "fast", "slow", "scale", "scalability", "load",
    "concurrent", "throughput", "latency", "hot path", "query", "cache",
    "pagination", "n+1",
]
# Architecture lens fires when the requirement implies structural change,
# not just additive feature work. The plan phase always benefits from a
# Security / Testing / Delivery angle, but Architecture should be paid
# for only when there's a real structural decision to make.
_ARCH_KEYWORDS = [
    "module", "package", "boundary", "layer", "refactor", "restructure",
    "migration", "new service", "abstract", "interface", "rename ",
    "extract", "split", "merge", "rewrite", "cross-cutting", "middleware",
]


def select_experts_for_plan(requirement: str, repo_profile: dict) -> AgentSelection:
    """
    Rule-based selection of plan-phase experts. Returns the same
    AgentSelection shape as select_agents so callers can treat them
    uniformly when logging / observing.
    """
    req = (requirement or "").lower()

    selected = list(PLAN_EXPERTS_ALWAYS)
    skipped: dict[str, str] = {}
    reasoning: dict[str, str] = {
        a: "always included in plan phase (every change has this angle)"
        for a in PLAN_EXPERTS_ALWAYS
    }

    has_frontend = any(w in req for w in _FRONTEND_KEYWORDS) or "full stack" in req or "full-stack" in req
    has_backend  = any(w in req for w in _BACKEND_KEYWORDS) or "full stack" in req or "full-stack" in req
    has_perf     = any(w in req for w in _PERF_KEYWORDS)

    if has_frontend:
        selected.append("UIUXAgent")
        reasoning["UIUXAgent"] = "frontend-related vocabulary detected in requirement"
    else:
        skipped["UIUXAgent"] = "no frontend vocabulary in requirement"

    if has_backend or has_perf:
        selected.append("PerformanceAgent")
        reasoning["PerformanceAgent"] = (
            "backend / data path detected — assess query patterns and payload size"
            if has_backend
            else "explicit performance keywords in requirement"
        )
    else:
        skipped["PerformanceAgent"] = "no backend or perf vocabulary in requirement"

    has_arch = any(w in req for w in _ARCH_KEYWORDS)
    if has_arch:
        selected.append("ArchitectureAgent")
        reasoning["ArchitectureAgent"] = (
            "structural-change vocabulary detected (module / boundary / refactor / migration / etc.)"
        )
    else:
        skipped["ArchitectureAgent"] = (
            "no structural-change vocabulary — additive feature work doesn't need an architecture lens"
        )

    return AgentSelection(
        selected=selected,
        skipped=skipped,
        reasoning=reasoning,
    )
