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
    owned_criteria: list[dict] | None = None,
    max_retries: int = 3,
) -> tuple[list[AgentFinding], dict]:
    owned_criteria = owned_criteria or []
    for attempt in range(max_retries):
        start = time.time()
        try:
            findings, reasoning = await agent.review(
                task, diff, file_contents, repo_profile, memory,
                owned_criteria=owned_criteria,
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
                "owned_criteria_count": len(owned_criteria),
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


async def find_graph_for_pr(
    pr_description: str,
    min_similarity: float = 0.4,
) -> dict | None:
    """
    Semantic auto-match: scan planning_memory for an approved graph
    whose stored requirement is similar to the PR description. Returns
    one of:
      - dict (the loaded graph) if a single match is clearly ahead
      - {"_ambiguous": [(graph_id, similarity), ...]} when top-1 ≈ top-2
        (caller should require explicit --graph)
      - None when nothing crosses the threshold
    """
    from database import load_graph
    if not pr_description or not pr_description.strip():
        return None
    hits = query_relevant_plans(pr_description, top_k=3)
    if not hits:
        return None
    top = hits[0]
    if top.get("similarity", 0) < min_similarity:
        return None
    # Ambiguity guard: if top1 vs top2 are too close, don't auto-pick.
    if len(hits) > 1 and hits[1].get("similarity", 0) > min_similarity * 0.9:
        return {
            "_ambiguous": [
                (h.get("id"), h.get("similarity", 0)) for h in hits[:3]
            ],
        }
    return await load_graph(top.get("id"))


async def run_review(
    pr_number: int,
    branch: str = None,
    graph_id: str | None = None,
    pr_description: str = "",
    auto_match: bool = True,
    on_status=None,
) -> tuple[list[AgentFinding], RiskReport]:

    def status(msg):
        if on_status:
            on_status(msg)

    from database import load_graph

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

    # 4. Resolve contract — explicit --graph wins; else try semantic
    #    auto-match against planning_memory; else fall back to generic
    #    (P1 §12 closed-loop, unchanged behaviour).
    contract = None
    matched_graph_id = graph_id
    if graph_id:
        loaded = await load_graph(graph_id)
        if loaded and loaded.get("contract"):
            contract = loaded["contract"]
            status(f"Loaded contract from {graph_id} "
                   f"({len(contract.get('criteria', []))} criteria)")
        else:
            status(f"Warning: --graph {graph_id} not found or has no contract; "
                   f"running generic review")
    elif auto_match and pr_description:
        candidate = await find_graph_for_pr(pr_description)
        if candidate is None:
            status("No matching graph in planning_memory — generic review")
        elif candidate.get("_ambiguous"):
            amb = candidate["_ambiguous"]
            top_str = ", ".join(f"{gid}({sim:.2f})" for gid, sim in amb)
            status(f"Auto-match ambiguous (top: {top_str}) — generic review; "
                   f"pass --graph to disambiguate")
        elif candidate.get("contract"):
            contract = candidate["contract"]
            matched_graph_id = candidate.get("graph_id")
            status(f"Auto-matched PR #{pr_number} → {matched_graph_id} "
                   f"({len(contract.get('criteria', []))} criteria)")

    # 5. Create task
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

    # 6. Agent selection
    status("Selecting agents...")
    selection = await select_agents(
        task.description, changed_files,
        diff[:300] if diff else "", repo_profile
    )

    # Contract owner union: if a criterion is owned by an agent the
    # diff-based selection didn't pick, we MUST include that agent so
    # its criterion can be verified.
    if contract:
        contract_owners = {
            c.get("owner_agent")
            for c in contract.get("criteria", [])
            if c.get("owner_agent")
        }
        for owner in sorted(contract_owners):
            if owner in AGENT_REGISTRY and owner not in selection.selected:
                selection.selected.append(owner)
                selection.reasoning[owner] = (
                    f"included by contract — owns {sum(1 for c in contract.get('criteria', []) if c.get('owner_agent')==owner)} criteria"
                )
                selection.skipped.pop(owner, None)

    await log_execution(task_id, "agent_selection", "orchestrator", {
        "selected":      selection.selected,
        "skipped":       selection.skipped,
        "reasoning":     selection.reasoning,
        "changed_files": changed_files,
        "contract_graph_id": matched_graph_id,
        "contract_criteria_count": len(contract.get("criteria", [])) if contract else 0,
    })

    status(f"Selected {len(selection.selected)} agent(s): {', '.join(selection.selected)}")

    # 7. Run agents in parallel
    await update_task_status(task_id, "REVIEWING")

    agents_to_run = [
        AGENT_REGISTRY[name]
        for name in selection.selected
        if name in AGENT_REGISTRY
    ]

    # Pre-compute owned criteria per agent so each gets only its own slice
    owned_by_agent: dict[str, list[dict]] = {}
    if contract:
        for c in contract.get("criteria", []):
            owner = c.get("owner_agent", "")
            owned_by_agent.setdefault(owner, []).append(c)

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
            agent, task, diff, file_contents, repo_profile, memory,
            owned_criteria=owned_by_agent.get(agent.name, []),
        )
        for agent, memory in zip(agents_to_run, memories)
    ]

    results = await asyncio.gather(*review_tasks, return_exceptions=True)

    # 8. Collect findings + contract statuses
    all_findings = []
    contract_statuses: dict[str, dict] = {}  # criterion_id → {status, evidence, owner}
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
        # P4: each agent that owned criteria emits contract_status entries
        for cs in (reasoning.get("contract_status") or []):
            if not isinstance(cs, dict):
                continue
            cid = cs.get("criterion_id")
            if not cid:
                continue
            contract_statuses[cid] = {
                "criterion_id": cid,
                "status":       cs.get("status", "UNVERIFIED"),
                "evidence":     cs.get("evidence", ""),
                "owner_agent":  agent.name,
            }

    # 9. Aggregate
    status("Aggregating findings...")
    risk_report = aggregate_findings(all_findings, selection, task_id)

    # P4: if contract in scope, attach contract roll-up + adjust merge_recommendation
    if contract:
        criteria_summary = []
        any_must_fail = False
        for c in contract.get("criteria", []):
            cid = c.get("id")
            cs  = contract_statuses.get(cid, {
                "status":   "UNVERIFIED",
                "evidence": "no agent emitted a status for this criterion",
                "owner_agent": c.get("owner_agent", ""),
            })
            entry = {
                "criterion_id": cid,
                "priority":     c.get("priority"),
                "owner_agent":  c.get("owner_agent"),
                "category":     c.get("category"),
                "assertion":    c.get("assertion"),
                "status":       cs.get("status", "UNVERIFIED"),
                "evidence":     cs.get("evidence", ""),
            }
            criteria_summary.append(entry)
            if c.get("priority") == "must_have" and entry["status"] == "FAIL":
                any_must_fail = True

        risk_report.contract_summary = {
            "graph_id":      matched_graph_id,
            "criteria":      criteria_summary,
            "any_must_fail": any_must_fail,
        }
        if any_must_fail:
            risk_report.merge_recommendation = "request_changes"
        # Log contract roll-up too
        await log_execution(task_id, "contract_status", "orchestrator", {
            "graph_id":      matched_graph_id,
            "criteria":      criteria_summary,
            "any_must_fail": any_must_fail,
        })

    await update_task_status(task_id, "AWAITING_HUMAN")

    return all_findings, risk_report
