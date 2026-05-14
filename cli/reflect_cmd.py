import json
import uuid
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from database import (
    init_db, get_all_tasks, get_pending_findings,
    update_finding_accepted
)
from memory.vector_store import add_finding, add_correction, get_stats

console = Console()

SEVERITY_COLORS = {
    "critical": "bold red",
    "high":     "red",
    "medium":   "yellow",
    "low":      "dim white",
}


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

        # Show reasoning if available
        # (stored in execution_log, not in finding content directly)
        console.print()

        # Prompt for decision
        console.print("  [bold][[green]a[/green]]ccept  "
                      "[[red]r[/red]]eject  "
                      "[[yellow]r+[/yellow]] reject with reason  "
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
                add_finding(finding_id, content, accepted=True)
                console.print("  [green]✓ Accepted — added to memory[/green]")
                accepted_count += 1
                break

            elif choice == "r":
                # Reject without reason
                await update_finding_accepted(finding_id, False)
                add_finding(finding_id, content, accepted=False)
                console.print("  [red]✗ Rejected[/red]")
                rejected_count += 1
                break

            elif choice == "r+":
                # Reject with reason — writes to corrections_memory
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
                    )
                    console.print(f"  [red]✗ Rejected[/red] — "
                                  f"[dim]correction saved to memory[/dim]")
                else:
                    console.print("  [red]✗ Rejected[/red]")

                await update_finding_accepted(finding_id, False)
                add_finding(finding_id, content, accepted=False)
                rejected_count += 1
                break

            elif choice == "s":
                console.print("  [dim]Skipped[/dim]")
                skipped_count += 1
                break

            else:
                console.print("  [dim]Please enter a, r, r+, or s[/dim]")

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
