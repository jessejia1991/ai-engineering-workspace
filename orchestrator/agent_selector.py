import os
import json
from dotenv import load_dotenv
from agents.llm_client import client    # P3: rate-limited HTTP wrapper
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

    return {
        "has_backend": has_backend,
        "has_frontend": has_frontend,
        "has_test_only": has_test_only,
        "has_db": has_db,
        "has_controller": has_controller,
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
        "TestingAgent": "Reviews test coverage: missing tests, regression risks, weak assertions",
        "PerformanceAgent": "Finds performance issues: N+1 queries, expensive loops, large payloads",
        "UIUXAgent": "Reviews UI/UX: loading states, error display, accessibility — ONLY for frontend changes",
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

## Diff summary
{diff_summary[:500] if diff_summary else 'No diff available'}

## Known corrections about this codebase
{corrections_text}

## Available agents
{agents_text}

## Rules
- UIUXAgent MUST ONLY run when frontend files (.ts, .tsx, .js, .jsx in client/ or frontend/) are changed
- If only test files changed, focus on TestingAgent and BugFindingAgent
- Always include at least one agent
- SecurityAgent is especially important when controller or entity files change

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
        # 실패 시 기본값: 안전하게 모든 관련 agent 실행
        selected = ["SecurityAgent", "BugFindingAgent", "TestingAgent"]
        if hints["has_frontend"]:
            selected.append("UIUXAgent")
        if hints["has_db"]:
            selected.append("PerformanceAgent")

        return AgentSelection(
            selected=selected,
            skipped={},
            reasoning={"fallback": f"Agent selector failed ({e}), using rule-based defaults"},
        )
