import asyncio
import time
import uuid
from models import TaskSpec, AgentFinding, RiskReport
from database import (
    init_db, create_task, update_task_status,
    save_finding, log_execution, get_all_tasks
)
from scanner.repo_scanner import load_profile, get_diff, get_changed_files, get_files_content
from orchestrator.agent_selector import select_agents
from agents.security import SecurityAgent
from agents.bug_finding import BugFindingAgent


AGENT_REGISTRY = {
    "SecurityAgent":    SecurityAgent(),
    "BugFindingAgent":  BugFindingAgent(),
    # Add in Day 3
    # "TestingAgent":   TestingAgent(),
    # "PerformanceAgent": PerformanceAgent(),
    # "UIUXAgent":      UIUXAgent(),
}

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


async def execute_agent_with_retry(
    agent,
    task: TaskSpec,
    diff: str,
    file_contents: dict,
    repo_profile: dict,
    reflection: list,
    max_retries: int = 3,
) -> list[AgentFinding]:
    """단일 agent 실행 + retry 로직"""
    for attempt in range(max_retries):
        start = time.time()
        try:
            findings = await agent.review(task, diff, file_contents, repo_profile, reflection)
            latency = int((time.time() - start) * 1000)

            await log_execution(task.task_id, "agent_result", agent.name, {
                "attempt": attempt,
                "latency_ms": latency,
                "finding_count": len(findings),
                "status": "ok",
            })
            return findings

        except Exception as e:
            latency = int((time.time() - start) * 1000)
            await log_execution(task.task_id, "agent_retry", agent.name, {
                "attempt": attempt,
                "latency_ms": latency,
                "error": str(e),
            })
            if attempt == max_retries - 1:
                return [AgentFinding(
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
                )]
            await asyncio.sleep(2 ** attempt)  # exponential backoff

    return []


async def load_reflection(agent_name: str) -> list:
    """agent별 reflection history 로드"""
    import os, json
    reflection_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".ai-workspace", "reflection-log.json"
    )
    if not os.path.exists(reflection_file):
        return []
    try:
        with open(reflection_file) as f:
            data = json.load(f)
        return data.get(agent_name, [])
    except Exception:
        return []


def aggregate_findings(
    all_findings: list[AgentFinding],
    selection,
    task_id: str,
) -> RiskReport:
    """findings를 집계하여 RiskReport 생성"""
    # filter out failed agents
    valid = [f for f in all_findings if f.status == "ok"]

    # count by severity
    by_agent = {}
    for f in valid:
        if f.agent not in by_agent:
            by_agent[f.agent] = {"risk": "low", "count": 0}
        by_agent[f.agent]["count"] += 1
        current = SEVERITY_RANK.get(by_agent[f.agent]["risk"], 0)
        incoming = SEVERITY_RANK.get(f.severity, 0)
        if incoming > current:
            by_agent[f.agent]["risk"] = f.severity

    # overall risk = highest severity
    overall = "low"
    for f in valid:
        if SEVERITY_RANK.get(f.severity, 0) > SEVERITY_RANK.get(overall, 0):
            overall = f.severity

    # top actions: suggestions from high/critical findings
    top_actions = []
    for f in sorted(valid, key=lambda x: SEVERITY_RANK.get(x.severity, 0), reverse=True):
        if f.severity in ("high", "critical") and f.suggestion:
            if f.suggestion not in top_actions:
                top_actions.append(f.suggestion)
        if len(top_actions) >= 5:
            break

    # merge recommendation
    if overall in ("critical", "high"):
        recommendation = "request_changes"
    elif overall == "medium":
        recommendation = "request_changes"
    else:
        recommendation = "approve"

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
    on_status=None,  # callback for CLI progress updates
) -> tuple[list[AgentFinding], RiskReport]:
    """
    전체 review pipeline 실행.
    on_status(message): CLI에 진행상황 전달용 callback
    """
    def status(msg):
        if on_status:
            on_status(msg)

    await init_db()

    # 1. load repo profile
    try:
        repo_profile = load_profile()
    except FileNotFoundError:
        raise RuntimeError("Repo not scanned yet. Run: ai-eng scan")

    repo_path = repo_profile["repo_path"]

    # 2. get diff + changed files
    status("Analyzing diff...")
    diff = get_diff(repo_path, branch)
    changed_files = get_changed_files(repo_path, branch)

    if not changed_files:
        raise RuntimeError("No changed files detected. Check branch name.")

    # 3. read changed file contents
    status(f"Reading {len(changed_files)} changed file(s)...")
    file_contents = get_files_content(repo_path, changed_files)

    # 4. create task
    task_id = f"TASK-PR{pr_number}"
    task = TaskSpec(
        task_id=task_id,
        type="review",
        title=f"Review PR #{pr_number}",
        description=f"Multi-agent review of PR #{pr_number}" + (f" branch: {branch}" if branch else ""),
        affected_files=changed_files,
        pr_url=f"https://github.com/{repo_profile.get('repo_id', '')}/pull/{pr_number}",
        branch=branch,
    )

    await create_task(task_id, "review", task.model_dump())
    await update_task_status(task_id, "IN_PROGRESS")

    # 5. agent selection
    status("Selecting agents...")
    diff_summary = diff[:300] if diff else ""
    selection = await select_agents(
        task.description, changed_files, diff_summary, repo_profile
    )

    await log_execution(task_id, "agent_selection", "orchestrator", {
        "selected": selection.selected,
        "skipped": selection.skipped,
        "reasoning": selection.reasoning,
        "changed_files": changed_files,
    })

    status(f"Selected {len(selection.selected)} agent(s): {', '.join(selection.selected)}")

    # 6. run selected agents in parallel
    await update_task_status(task_id, "REVIEWING")

    agents_to_run = [
        AGENT_REGISTRY[name]
        for name in selection.selected
        if name in AGENT_REGISTRY
    ]

    not_implemented = [
        name for name in selection.selected
        if name not in AGENT_REGISTRY
    ]
    if not_implemented:
        status(f"[dim]Not yet implemented: {', '.join(not_implemented)}[/dim]")

    status("Running agents in parallel...")

    reflection_tasks = [load_reflection(agent.name) for agent in agents_to_run]
    reflections = await asyncio.gather(*reflection_tasks)

    review_tasks = [
        execute_agent_with_retry(
            agent, task, diff, file_contents, repo_profile, reflection
        )
        for agent, reflection in zip(agents_to_run, reflections)
    ]

    results = await asyncio.gather(*review_tasks, return_exceptions=True)

    # 7. collect findings
    all_findings = []
    for agent, result in zip(agents_to_run, results):
        if isinstance(result, Exception):
            status(f"[red]{agent.name} raised exception: {result}[/red]")
            continue
        for finding in result:
            all_findings.append(finding)
            if finding.status == "ok":
                await save_finding(
                    task_id, finding.agent,
                    finding.severity, finding.model_dump()
                )

    # 8. aggregate
    status("Aggregating findings...")
    risk_report = aggregate_findings(all_findings, selection, task_id)

    await update_task_status(task_id, "AWAITING_HUMAN")

    return all_findings, risk_report
