import asyncio
from rich.console import Console
from rich.panel import Panel
from rich import box

from agents.llm_client import client as llm_client, format_usage_summary

console = Console()

SEVERITY_COLORS = {
    "critical": "bold red",
    "high":     "red",
    "medium":   "yellow",
    "low":      "dim white",
}


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


async def cmd_review(pr_number: int, branch: str = None):
    try:
        await _cmd_review_inner(pr_number, branch)
    finally:
        # P3: LLM usage observability — print on every exit path
        # (success, RuntimeError, unexpected exception). Reset for next.
        usage = llm_client.usage_summary()
        if usage["requests"] > 0:
            console.print(f"[dim]LLM usage: {format_usage_summary(usage)}[/dim]")
        llm_client.reset_usage()


async def _cmd_review_inner(pr_number: int, branch: str = None):
    from orchestrator.runner import run_review
    from github_client import post_review_comments
    from database import get_agent_reasoning

    console.print()
    console.print(
        f"[bold blue]Reviewing PR #{pr_number}[/bold blue]" +
        (f" [dim](branch: {branch})[/dim]" if branch else "")
    )
    console.print()

    def on_status(msg):
        console.print(f"  [dim]{msg}[/dim]")

    try:
        findings, risk_report = await run_review(
            pr_number=pr_number,
            branch=branch,
            on_status=on_status,
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

    # Post to GitHub
    console.print()
    console.print("[dim]Posting findings to GitHub PR...[/dim]")
    success = post_review_comments(pr_number, findings, risk_report)
    if success:
        console.print(
            f"[green]✓ Posted to GitHub PR #{pr_number}[/green]  "
            f"[dim]https://github.com/"
            f"jessejia1991/spring-petclinic-reactjs/pull/{pr_number}[/dim]"
        )
    else:
        console.print("[yellow]⚠ GitHub posting failed — check GITHUB_TOKEN[/yellow]")

    console.print()
    console.print(
        f"[dim]Task: TASK-PR{pr_number} — "
        f"run 'reflect' to accept/reject findings[/dim]"
    )
    console.print()
