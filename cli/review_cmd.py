import asyncio
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from agents.llm_client import client as llm_client, format_usage_summary

console = Console()

SEVERITY_COLORS = {
    "critical": "bold red",
    "high":     "red",
    "medium":   "yellow",
    "low":      "dim white",
}

# P4: contract status rendering
STATUS_ICON = {
    "PASS":         "[green]✓[/green]",
    "FAIL":         "[red]✗[/red]",
    "UNVERIFIED":   "[yellow]?[/yellow]",
}
PRIORITY_COLOR = {
    "must_have":    "red",
    "should_have":  "yellow",
    "nice_to_have": "dim white",
}


def _render_contract_status(summary: dict) -> None:
    criteria = summary.get("criteria", [])
    if not criteria:
        return

    by_status = {"PASS": 0, "FAIL": 0, "UNVERIFIED": 0}
    for c in criteria:
        by_status[c.get("status", "UNVERIFIED")] = by_status.get(c.get("status", "UNVERIFIED"), 0) + 1

    must_fail_count = sum(
        1 for c in criteria
        if c.get("priority") == "must_have" and c.get("status") == "FAIL"
    )

    title = (
        f"Contract Status  "
        f"[dim](graph: {summary.get('graph_id', 'unknown')}, "
        f"{by_status['PASS']} pass · "
        f"{by_status['FAIL']} fail · "
        f"{by_status['UNVERIFIED']} unverified)[/dim]"
    )

    table = Table(box=box.SIMPLE_HEAVY, show_header=True)
    table.add_column("ID",       style="bold")
    table.add_column("Priority", style="white")
    table.add_column("Owner",    style="cyan")
    table.add_column("Status",   style="white", justify="center")
    table.add_column("Assertion / Evidence", style="white", overflow="fold")

    for c in criteria:
        pc    = PRIORITY_COLOR.get(c.get("priority", ""), "white")
        icon  = STATUS_ICON.get(c.get("status", "UNVERIFIED"), "?")
        owner = c.get("owner_agent", "")[:4]
        body  = c.get("assertion", "")
        ev    = c.get("evidence", "")
        if ev:
            body = body + f"\n[dim]→ {ev}[/dim]"
        table.add_row(
            c.get("criterion_id", ""),
            f"[{pc}]{c.get('priority', '')}[/{pc}]",
            owner,
            icon,
            body,
        )

    console.print()
    console.print(Panel(table, title=title, border_style="magenta"))

    if must_fail_count:
        console.print(
            f"[bold red]⚠ {must_fail_count} must_have criterion FAILED — "
            f"merge_recommendation downgraded to request_changes.[/bold red]"
        )


def _render_agent_reasoning(reasoning_by_agent: dict, agents_run: list):
    """
    Render the reasoning chain for each agent that ran.

    This surfaces the hidden agent state (Design Doc 3.2): codebase
    understanding, rejected candidates, and how much memory was injected.
    rejected_candidates is the most important hidden state — it shows what
    each agent considered but decided NOT to report.
    """
    if not reasoning_by_agent:
        return

    console.print("[bold]Agent Reasoning:[/bold]\n")

    for agent in agents_run:
        entry = reasoning_by_agent.get(agent)
        if not entry:
            continue

        reasoning = entry.get("reasoning", {}) or {}
        memory    = entry.get("memory_injected", {}) or {}

        console.print(f"  [bold cyan]{agent}[/bold cyan]")

        # Memory injection summary — proof the memory loop is working
        mf = memory.get("findings_count", 0)
        mc = memory.get("corrections_count", 0)
        if mf or mc:
            console.print(
                f"    [dim]Memory injected: {mf} finding(s), "
                f"{mc} correction(s)[/dim]"
            )
        else:
            console.print("    [dim]Memory injected: none (cold start)[/dim]")

        # Codebase understanding
        understanding = reasoning.get("codebase_understanding")
        if understanding:
            console.print(f"    [dim]Understanding:[/dim] {understanding}")

        # Rejected candidates — the key observable hidden state
        rejected = reasoning.get("rejected_candidates", []) or []
        if rejected:
            console.print(
                f"    [dim]Considered but rejected "
                f"({len(rejected)}):[/dim]"
            )
            for rc in rejected:
                issue      = rc.get("issue", "")
                why        = rc.get("why_rejected", "")
                confidence = rc.get("confidence_to_reject")
                conf_str   = f" [dim](confidence {confidence})[/dim]" if confidence is not None else ""
                console.print(f"      [yellow]–[/yellow] {issue}{conf_str}")
                if why:
                    console.print(f"        [dim]→ {why}[/dim]")

        console.print()


async def cmd_review(pr_number: int, branch: str = None, *,
                     graph_id: str = None, no_graph: bool = False,
                     post_decision: bool | None = None):
    try:
        await _cmd_review_inner(pr_number, branch,
                                graph_id=graph_id, no_graph=no_graph,
                                post_decision=post_decision)
    finally:
        # P3: LLM usage observability — print on every exit path
        # (success, RuntimeError, unexpected exception). Reset for next.
        usage = llm_client.usage_summary()
        if usage["requests"] > 0:
            console.print(f"[dim]LLM usage: {format_usage_summary(usage)}[/dim]")
        llm_client.reset_usage()


async def _cmd_review_inner(pr_number: int, branch: str = None, *,
                            graph_id: str = None, no_graph: bool = False,
                            post_decision: bool | None = None):
    from orchestrator.runner import run_review
    from github_client import post_review_comments, get_pr_description
    from database import get_agent_reasoning, init_db, get_active_repo

    # Memory slice guard: a review against no repo is meaningless — there's
    # no codebase to scope memory to. Block at entry with an actionable hint.
    await init_db()
    active = await get_active_repo()
    if not active:
        console.print(
            "[red]No active repo set.[/red] Run [bold]repo add <path>[/bold] "
            "then [bold]repo use <id>[/bold] before reviewing. "
            "[dim]Or just run [bold]scan[/bold] — it auto-registers + activates.[/dim]"
        )
        return

    console.print()
    console.print(
        f"[bold blue]Reviewing PR #{pr_number}[/bold blue]" +
        (f" [dim](branch: {branch})[/dim]" if branch else "") +
        f"\n[dim]repo: {active['id']}[/dim]"
    )
    console.print()

    def on_status(msg):
        console.print(f"  [dim]{msg}[/dim]")

    # P4: pull PR description for auto-match (skip if --no-graph or
    # explicit --graph already provided).
    pr_description = ""
    auto_match = not no_graph and graph_id is None
    if auto_match:
        on_status("Fetching PR description for auto-match...")
        pr_description = get_pr_description(pr_number)
        if not pr_description:
            on_status("PR description unavailable; auto-match disabled")
            auto_match = False

    try:
        findings, risk_report = await run_review(
            pr_number=pr_number,
            branch=branch,
            graph_id=graph_id,
            pr_description=pr_description,
            auto_match=auto_match,
            on_status=on_status,
            repo_id=active["id"],
        )
    except RuntimeError as e:
        console.print(f"\n[red]Error: {e}[/red]")
        return
    except Exception as e:
        console.print(f"\n[red]Unexpected error: {e}[/red]")
        raise

    console.print()

    # Agent selection summary
    console.print("[bold]Agent Selection:[/bold]")
    for agent in risk_report.agents_run:
        console.print(f"  [green]✓[/green] {agent}")
    for agent, reason in risk_report.agents_skipped.items():
        console.print(f"  [dim]–[/dim] {agent}  [dim]SKIPPED ({reason})[/dim]")
    console.print()

    # Agent reasoning — hidden state made observable (Design Doc 3.2)
    task_id = f"TASK-PR{pr_number}"
    reasoning_by_agent = await get_agent_reasoning(task_id)
    _render_agent_reasoning(reasoning_by_agent, risk_report.agents_run)

    # Findings
    valid_findings = [f for f in findings if f.status == "ok"]

    if not valid_findings:
        console.print("[green]No issues found.[/green]")
    else:
        console.print(f"[bold]Findings ({len(valid_findings)} total):[/bold]\n")
        for f in sorted(
            valid_findings,
            key=lambda x: {"critical":4,"high":3,"medium":2,"low":1}.get(x.severity, 0),
            reverse=True
        ):
            color = SEVERITY_COLORS.get(f.severity, "white")
            console.print(
                f"  [{color}][{f.severity.upper()}][/{color}] "
                f"[bold]{f.title}[/bold]"
            )
            console.print(
                f"  [dim]Agent: {f.agent}  |  Category: {f.category}[/dim]"
            )
            if f.file:
                loc = f.file + (f":{f.line}" if f.line else "")
                console.print(f"  [dim]File: {loc}[/dim]")
            console.print(f"  {f.detail}")
            console.print(f"  [cyan]→ {f.suggestion}[/cyan]")
            console.print()

    # Risk report panel
    risk_color = {
        "critical": "bold red", "high": "red",
        "medium": "yellow",     "low":  "green",
    }.get(risk_report.overall_risk, "white")

    rec_color = "green" if risk_report.merge_recommendation == "approve" else "yellow"

    console.print(Panel(
        f"Overall Risk: [{risk_color}]{risk_report.overall_risk.upper()}[/{risk_color}]\n"
        f"Recommendation: [{rec_color}]{risk_report.merge_recommendation}[/{rec_color}]\n"
        f"Agents run: {len(risk_report.agents_run)}  |  Findings: {len(valid_findings)}",
        title="Risk Report",
        border_style="blue",
    ))

    if risk_report.top_actions:
        console.print("\n[bold]Top Actions:[/bold]")
        for action in risk_report.top_actions:
            console.print(f"  [cyan]→[/cyan] {action}")

    # P4: Contract Status panel — rendered only when a contract is in scope
    if risk_report.contract_summary:
        _render_contract_status(risk_report.contract_summary)

    # Post to GitHub — two-gate safety:
    #   Gate 1 (this caller): --post / REVIEW_POST_COMMENTS opt-in
    #   Gate 2 (post_review_comments): REVIEW_ALLOWED_REPOS allowlist
    # Both must pass. Posting to a real OSS repo without permission is
    # the kind of bug you only need to make once — we made it once.
    import os
    from github_client import is_post_allowed_repo
    repo = os.environ.get("GITHUB_REPO", "")
    env_opt_in = os.environ.get("REVIEW_POST_COMMENTS", "").lower() in ("1", "true", "yes")
    if post_decision is None:
        do_post = env_opt_in
    else:
        do_post = post_decision

    console.print()
    if not do_post:
        target = f"{repo}#{pr_number}" if repo else f"PR #{pr_number}"
        console.print(
            f"[dim]Skipped GitHub comment post (target: {target}). "
            f"Pass --post or set REVIEW_POST_COMMENTS=true to enable.[/dim]"
        )
    else:
        allowed, reason = is_post_allowed_repo()
        if not allowed:
            console.print(
                f"[yellow]⚠ GitHub post BLOCKED by allowlist:[/yellow] {reason}"
            )
        else:
            console.print("[dim]Posting findings to GitHub PR...[/dim]")
            success = post_review_comments(pr_number, findings, risk_report)
            if success:
                url = f"https://github.com/{repo}/pull/{pr_number}" if repo else ""
                console.print(
                    f"[green]✓ Posted to GitHub PR #{pr_number}[/green]  "
                    + (f"[dim]{url}[/dim]" if url else "")
                )
            else:
                console.print("[yellow]⚠ GitHub posting failed[/yellow]")

    console.print()
    console.print(
        f"[dim]Task: TASK-PR{pr_number} — "
        f"run 'reflect' to accept/reject findings[/dim]"
    )
    console.print()
