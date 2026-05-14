import asyncio
import time
import uuid
from models import TaskSpec, AgentFinding, RiskReport
from database import (
    init_db, create_task, update_task_status,
    save_finding, log_execution, get_all_tasks,
    clear_unreviewed_findings,
)
from scanner.repo_scanner import load_profile, get_diff, get_changed_files, get_files_content
from orchestrator.agent_selector import select_agents
from agents.security import SecurityAgent
from agents.bug_finding import BugFindingAgent
from agents.testing import TestingAgent
from agents.uiux import UIUXAgent
from agents.performance import PerformanceAgent
from memory.vector_store import query_relevant_memory

AGENT_REGISTRY = {
    "SecurityAgent":    SecurityAgent(),
    "BugFindingAgent":  BugFindingAgent(),
    "TestingAgent":     TestingAgent(),
    "UIUXAgent":        UIUXAgent(),
    "PerformanceAgent": PerformanceAgent(),
}

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


async def execute_agent_with_retry(
    agent,
    task: TaskSpec,
    diff: str,
    file_contents: dict,
    repo_profile: dict,
    memory: dict,
    max_retries: int = 3,
) -> tuple[list[AgentFinding], dict]:
    for attempt in range(max_retries):
        start = time.time()
        try:
            findings, reasoning = await agent.review(
                task, diff, file_contents, repo_profile, memory
            )
            latency = int((time.time() - start) * 1000)

            await log_execution(task.task_id, "agent_result", agent.name, {
                "attempt":        attempt,
                "latency_ms":     latency,
                "finding_count":  len(findings),
                "status":         "ok",
                "reasoning":      reasoning,
                "memory_injected": {
                    "findings_count":    memory.get("findings_count", 0),
                    "corrections_count": memory.get("corrections_count", 0),
                },
            })
            return findings, reasoning

        except Exception as e:
            latency = int((time.time() - start) * 1000)
            await log_execution(task.task_id, "agent_retry", agent.name, {
                "attempt":    attempt,
                "latency_ms": latency,
                "error":      str(e),
            })
            if attempt == max_retries - 1:
                error_finding = AgentFinding(
                    finding_id=str(uuid.uuid4())[:8],
                    task_id=task.task_id,
                    agent=agent.name,
                    severity="low",
                    category="agent-error",
                    title=f"{agent.name} failed after {max_retries} attempts",
                    detail=str(e),
                    suggestion="Check execution logs",
                    status="failed",
                    error=str(e),
                )
                return [error_finding], {}
            await asyncio.sleep(2 ** attempt)

    return [], {}


def aggregate_findings(
    all_findings: list[AgentFinding],
    selection,
    task_id: str,
) -> RiskReport:
    valid = [f for f in all_findings if f.status == "ok"]

    by_agent = {}
    for f in valid:
        if f.agent not in by_agent:
            by_agent[f.agent] = {"risk": "low", "count": 0}
        by_agent[f.agent]["count"] += 1
        current  = SEVERITY_RANK.get(by_agent[f.agent]["risk"], 0)
        incoming = SEVERITY_RANK.get(f.severity, 0)
        if incoming > current:
            by_agent[f.agent]["risk"] = f.severity

    overall = "low"
    for f in valid:
        if SEVERITY_RANK.get(f.severity, 0) > SEVERITY_RANK.get(overall, 0):
            overall = f.severity

    top_actions = []
    for f in sorted(valid,
                    key=lambda x: SEVERITY_RANK.get(x.severity, 0),
                    reverse=True):
        if f.severity in ("high", "critical") and f.suggestion:
            if f.suggestion not in top_actions:
                top_actions.append(f.suggestion)
        if len(top_actions) >= 5:
            break

    recommendation = "approve" if overall == "low" else "request_changes"

    return RiskReport(
        task_id=task_id,
        overall_risk=overall,
        agents_run=selection.selected,
        agents_skipped=selection.skipped,
        by_agent=by_agent,
        top_actions=top_actions,
        merge_recommendation=recommendation,
    )


async def run_review(
    pr_number: int,
    branch: str = None,
    on_status=None,
) -> tuple[list[AgentFinding], RiskReport]:

    def status(msg):
        if on_status:
            on_status(msg)

    await init_db()

    # 1. Load repo profile
    try:
        repo_profile = load_profile()
    except FileNotFoundError:
        raise RuntimeError("Repo not scanned yet. Run: ai-eng scan")

    repo_path = repo_profile["repo_path"]

    # 2. Get diff + changed files
    status("Analyzing diff...")
    diff          = get_diff(repo_path, branch)
    changed_files = get_changed_files(repo_path, branch)

    if not changed_files:
        raise RuntimeError("No changed files detected. Check branch name.")

    # 3. Read changed file contents
    status(f"Reading {len(changed_files)} changed file(s)...")
    file_contents = get_files_content(repo_path, changed_files)

    # 4. Create task
    task_id = f"TASK-PR{pr_number}"
    task = TaskSpec(
        task_id=task_id,
        type="review",
        title=f"Review PR #{pr_number}",
        description=f"Multi-agent review of PR #{pr_number}" +
                    (f" branch: {branch}" if branch else ""),
        affected_files=changed_files,
        pr_url=f"https://github.com/{repo_profile.get('repo_id', '')}/pull/{pr_number}",
        branch=branch,
    )

    await create_task(task_id, "review", task.model_dump())
    cleared = await clear_unreviewed_findings(task_id)
    if cleared:
        status(f"Cleared {cleared} stale finding(s) from a previous review")
    await update_task_status(task_id, "IN_PROGRESS")

    # 5. Agent selection
    status("Selecting agents...")
    selection = await select_agents(
        task.description, changed_files,
        diff[:300] if diff else "", repo_profile
    )

    await log_execution(task_id, "agent_selection", "orchestrator", {
        "selected":      selection.selected,
        "skipped":       selection.skipped,
        "reasoning":     selection.reasoning,
        "changed_files": changed_files,
    })

    status(f"Selected {len(selection.selected)} agent(s): {', '.join(selection.selected)}")

    # 6. Run agents in parallel
    await update_task_status(task_id, "REVIEWING")

    agents_to_run = [
        AGENT_REGISTRY[name]
        for name in selection.selected
        if name in AGENT_REGISTRY
    ]

    # Fetch memory for each agent (semantic retrieval from ChromaDB).
    # Run serially: chromadb 1.5.9's Rust bindings deadlock under
    # concurrent first-call init from multiple worker threads. Memory
    # queries are ms-scale after warm-up, so this is essentially free.
    status("Retrieving relevant memory...")
    query_text = (diff[:300] if diff else "") + " ".join(changed_files)

    memories = [
        query_relevant_memory(agent.name, query_text)
        for agent in agents_to_run
    ]

    status("Running agents in parallel...")

    review_tasks = [
        execute_agent_with_retry(
            agent, task, diff, file_contents, repo_profile, memory
        )
        for agent, memory in zip(agents_to_run, memories)
    ]

    results = await asyncio.gather(*review_tasks, return_exceptions=True)

    # 7. Collect findings
    all_findings = []
    for agent, result in zip(agents_to_run, results):
        if isinstance(result, Exception):
            status(f"{agent.name} raised exception: {result}")
            continue
        findings, reasoning = result
        for finding in findings:
            all_findings.append(finding)
            if finding.status == "ok":
                await save_finding(
                    task_id, finding.agent,
                    finding.severity, finding.model_dump()
                )

    # 8. Aggregate
    status("Aggregating findings...")
    risk_report = aggregate_findings(all_findings, selection, task_id)

    await update_task_status(task_id, "AWAITING_HUMAN")

    return all_findings, risk_report
