import json
import uuid
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from database import (
    init_db, get_all_tasks, get_pending_findings, get_task,
    update_finding_accepted, get_agent_reasoning, get_active_repo
)
from memory.vector_store import add_finding, add_correction, get_stats

console = Console()

SEVERITY_COLORS = {
    "critical": "bold red",
    "high":     "red",
    "medium":   "yellow",
    "low":      "dim white",
}


def _render_finding_reasoning(entry: dict):
    """
    Render the originating agent's reasoning chain next to a finding so the
    human has enough context to make a quality judgment (Design Doc 4.3).

    'entry' is one agent's record from get_agent_reasoning(): it contains
    the reasoning chain plus the memory_injected summary. Reasoning is stored
    per-agent, so every finding from the same agent shares this context.
    """
    if not entry:
        return

    reasoning = entry.get("reasoning", {}) or {}
    memory    = entry.get("memory_injected", {}) or {}

    understanding = reasoning.get("codebase_understanding")
    if understanding:
        console.print(f"  [dim]Agent reasoning:[/dim] {understanding}")

    rejected = reasoning.get("rejected_candidates", []) or []
    if rejected:
        console.print("  [dim]Agent considered but rejected:[/dim]")
        for rc in rejected:
            issue = rc.get("issue", "")
            why   = rc.get("why_rejected", "")
            console.print(f"    [yellow]–[/yellow] {issue}")
            if why:
                console.print(f"      [dim]→ {why}[/dim]")

    mf = memory.get("findings_count", 0)
    mc = memory.get("corrections_count", 0)
    if mf or mc:
        console.print(
            f"  [dim]Memory used: {mf} past finding(s), "
            f"{mc} correction(s)[/dim]"
        )


async def cmd_reflect(task_id: str = None):
    await init_db()

    # If no task_id, find the most recent AWAITING_HUMAN task
    if not task_id:
        tasks = await get_all_tasks()
        awaiting = [t for t in tasks if t["status"] == "AWAITING_HUMAN"]
        if not awaiting:
            console.print("[dim]No tasks awaiting review. Run a review first.[/dim]")
            return
        task_id = awaiting[0]["id"]
        console.print(f"[dim]Using most recent task: {task_id}[/dim]\n")

    findings = await get_pending_findings(task_id)

    if not findings:
        console.print(f"[green]No pending findings for {task_id}.[/green]")
        return

    # Memory writes go to the task's repo, not the currently-active one.
    # User may have switched repos between the review and the reflect; we
    # still want this task's corrections attached to the right pool.
    task_row = await get_task(task_id)
    task_repo_id = None
    if task_row:
        try:
            artifacts = json.loads(task_row.get("artifacts") or "{}")
            task_repo_id = artifacts.get("repo_id")
        except (json.JSONDecodeError, TypeError):
            pass
    if not task_repo_id:
        # Fallback to current active repo with a warning. Better than dropping
        # the write entirely.
        active = await get_active_repo()
        if active:
            task_repo_id = active["id"]
            console.print(
                f"[yellow]⚠ Task {task_id} has no recorded repo_id; "
                f"falling back to active repo '{task_repo_id}'.[/yellow]"
            )
        else:
            console.print(
                f"[red]Task {task_id} has no recorded repo_id and no active "
                f"repo is set. Run [bold]repo use <id>[/bold] first.[/red]"
            )
            return

    # Per-agent reasoning chains from the review run (Design Doc 4.3) —
    # fetched once, shared across all findings from the same agent.
    reasoning_by_agent = await get_agent_reasoning(task_id)

    console.print(f"\n[bold]Pending findings for {task_id}[/bold] ({len(findings)} total)\n")

    accepted_count  = 0
    rejected_count  = 0
    skipped_count   = 0

    for i, row in enumerate(findings):
        content = json.loads(row["content"]) if isinstance(row["content"], str) else row["content"]
        finding_id = row["id"]

        severity = content.get("severity", "low")
        color    = SEVERITY_COLORS.get(severity, "white")

        # Display finding
        console.print(f"[{color}][{severity.upper()}][/{color}] "
                      f"[bold]{content.get('title', '')}[/bold]")
        console.print(f"  [dim]Agent: {content.get('agent', '')}  |  "
                      f"Category: {content.get('category', '')}[/dim]")

        if content.get("file"):
            loc = content["file"]
            if content.get("line"):
                loc += f":{content['line']}"
            console.print(f"  [dim]File: {loc}[/dim]")

        console.print(f"  {content.get('detail', '')}")
        console.print(f"  [cyan]→ {content.get('suggestion', '')}[/cyan]")

        # Show the originating agent's reasoning chain (stored per-agent in
        # execution_log, retrieved via get_agent_reasoning).
        agent_name = content.get("agent", "")
        _render_finding_reasoning(reasoning_by_agent.get(agent_name))
        console.print()

        # Prompt for decision
        console.print("  [bold][[green]a[/green]]ccept  "
                      "[[red]r[/red]]eject  "
                      "[[yellow]r+[/yellow]] reject with reason  "
                      "[[magenta]p+[/magenta]] reject + pin correction  "
                      "[[dim]s[/dim]]kip[/bold]")

        while True:
            try:
                choice = console.input("  > ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Reflect interrupted.[/dim]")
                return

            if choice == "a":
                # Accept: write to SQLite + ChromaDB
                await update_finding_accepted(finding_id, True)
                add_finding(finding_id, content, accepted=True, repo_id=task_repo_id)
                console.print("  [green]✓ Accepted — added to memory[/green]")
                accepted_count += 1
                break

            elif choice == "r":
                # Reject without reason
                await update_finding_accepted(finding_id, False)
                add_finding(finding_id, content, accepted=False, repo_id=task_repo_id)
                console.print("  [red]✗ Rejected[/red]")
                rejected_count += 1
                break

            elif choice in ("r+", "p+"):
                # Reject with reason — writes to corrections_memory.
                # `p+` additionally pins the correction so `memory prune`
                # cannot evict it (use for high-value team conventions).
                pin = (choice == "p+")
                try:
                    reason = console.input("  Reason: ").strip()
                except (KeyboardInterrupt, EOFError):
                    reason = ""

                if reason:
                    correction_id = str(uuid.uuid4())[:8]
                    add_correction(
                        correction_id=correction_id,
                        note=reason,
                        example=f"Finding rejected: {content.get('title', '')}",
                        correction_type="false-positive",
                        repo_id=task_repo_id,
                        pinned=pin,
                    )
                    pin_note = " (pinned)" if pin else ""
                    console.print(f"  [red]✗ Rejected[/red] — "
                                  f"[dim]correction saved to memory{pin_note}[/dim]")
                else:
                    console.print("  [red]✗ Rejected[/red]")

                await update_finding_accepted(finding_id, False)
                add_finding(finding_id, content, accepted=False, repo_id=task_repo_id)
                rejected_count += 1
                break

            elif choice == "s":
                console.print("  [dim]Skipped[/dim]")
                skipped_count += 1
                break

            else:
                console.print("  [dim]Please enter a, r, r+, p+, or s[/dim]")

        console.print()

    # Summary
    stats = get_stats()
    console.print(Panel(
        f"Accepted: [green]{accepted_count}[/green]  "
        f"Rejected: [red]{rejected_count}[/red]  "
        f"Skipped: [dim]{skipped_count}[/dim]\n\n"
        f"Memory: {stats['findings_in_memory']} findings  |  "
        f"{stats['corrections_in_memory']} corrections",
        title="Reflect Complete",
        border_style="blue",
    ))
