"""
`repo {list,add,use,remove}` — top-level repo management.

Repo is the user-facing entity. Memory operations under `memory ...` are
implementation detail. This file owns the entity surface; `cli/memory_cmd.py`
owns the memory-only operations.
"""

import os
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from database import (
    init_db, list_repos, get_repo, add_repo, set_active_repo,
    remove_repo, get_active_repo,
)
from memory.vector_store import get_stats_by_repo, delete_repo_entries


console = Console()


async def cmd_repo(args: list[str]) -> None:
    """Dispatcher for `repo <action> [args...]`."""
    if not args:
        console.print(
            "[red]Usage: repo list  |  repo add <path> [--name X] [--use]  "
            "|  repo use <id>  |  repo remove <id>[/red]"
        )
        return

    action = args[0]
    rest = args[1:]

    if action == "list":
        await _repo_list()
    elif action == "add":
        await _repo_add(rest)
    elif action == "use":
        await _repo_use(rest)
    elif action == "remove":
        await _repo_remove(rest)
    else:
        console.print(f"[red]Unknown repo action: {action}[/red]")
        console.print(
            "[dim]Supported: list · add · use · remove[/dim]"
        )


# ---------- list ---------------------------------------------------------

async def _repo_list() -> None:
    await init_db()
    rows = await list_repos()
    by_repo_counts = get_stats_by_repo()

    if not rows:
        console.print(
            "[dim]No repos registered yet. Run [bold]scan[/bold] on a repo path "
            "(or [bold]repo add <path>[/bold]) to get started.[/dim]"
        )
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True, title="Registered repos")
    table.add_column("Active", style="bold", width=6, justify="center")
    table.add_column("ID",          style="cyan bold")
    table.add_column("Name",        style="white")
    table.add_column("Path",        style="dim white")
    table.add_column("Memory entries", justify="right", style="white")
    table.add_column("Created",     style="dim", width=19)

    for r in rows:
        rid     = r["id"]
        counts  = by_repo_counts.get(rid, {})
        n_total = sum(counts.values()) if counts else 0
        n_break = (
            f"{counts.get('findings_in_memory', 0)}f · "
            f"{counts.get('corrections_in_memory', 0)}c · "
            f"{counts.get('planning_in_memory', 0)}p"
            if n_total > 0 else "—"
        )
        table.add_row(
            "●" if r.get("is_active") else "",
            rid,
            r.get("display_name") or "",
            r.get("repo_path") or "",
            n_break,
            (r.get("created_at") or "")[:19],
        )

    console.print(table)
    console.print(
        "[dim]Legend: Nf=findings, Nc=corrections, Np=plans · "
        "Run [bold]repo use <id>[/bold] to switch active.[/dim]"
    )


# ---------- add ----------------------------------------------------------

async def _repo_add(rest: list[str]) -> None:
    if not rest:
        console.print("[red]Usage: repo add <path> [--name X] [--use][/red]")
        return

    path = os.path.abspath(rest[0])
    display_name: str | None = None
    use_after = False
    i = 1
    while i < len(rest):
        if rest[i] == "--name" and i + 1 < len(rest):
            display_name = rest[i + 1]
            i += 2
        elif rest[i] == "--use":
            use_after = True
            i += 1
        else:
            console.print(f"[red]Unknown flag: {rest[i]}[/red]")
            return

    if not os.path.isdir(path):
        console.print(f"[red]Path is not a directory: {path}[/red]")
        return

    # Derive repo_id from directory name (the scanner does the same thing for
    # the auto-register path; keep them aligned so `repo add <same path>` and
    # `scan` converge on the same id).
    repo_id = os.path.basename(path.rstrip("/")) or path

    await init_db()
    await add_repo(repo_id, path, display_name=display_name)
    msg = f"[green]✓ Registered repo '{repo_id}'[/green] [dim]({path})[/dim]"

    if use_after:
        await set_active_repo(repo_id)
        msg += " · [bold]now active[/bold]"

    console.print(msg)


# ---------- use ----------------------------------------------------------

async def _repo_use(rest: list[str]) -> None:
    if not rest:
        console.print("[red]Usage: repo use <id>[/red]")
        return
    repo_id = rest[0]

    await init_db()
    if not await get_repo(repo_id):
        console.print(
            f"[red]Repo '{repo_id}' not registered.[/red] "
            f"Run [bold]repo list[/bold] to see what's available, or "
            f"[bold]repo add <path>[/bold] first."
        )
        return

    await set_active_repo(repo_id)
    console.print(f"[green]✓ Active repo set to '{repo_id}'[/green]")


# ---------- remove -------------------------------------------------------

async def _repo_remove(rest: list[str]) -> None:
    if not rest:
        console.print("[red]Usage: repo remove <id>[/red]")
        return
    repo_id = rest[0]

    await init_db()
    row = await get_repo(repo_id)
    if not row:
        console.print(f"[red]Repo '{repo_id}' not registered.[/red]")
        return

    # Phase 1: typed-id confirmation. Belt-and-suspenders since this is
    # irreversible if the user also opts to purge.
    counts = get_stats_by_repo().get(repo_id, {})
    n_total = sum(counts.values()) if counts else 0
    console.print(
        f"[bold]About to remove repo[/bold] '{row.get('display_name') or repo_id}' "
        f"(id={repo_id})."
    )
    if n_total > 0:
        console.print(
            f"  [dim]Tied memory entries: "
            f"{counts.get('findings_in_memory', 0)} findings, "
            f"{counts.get('corrections_in_memory', 0)} corrections, "
            f"{counts.get('planning_in_memory', 0)} plans.[/dim]"
        )
    try:
        typed = console.input(f"  Confirm by typing repo id ('{repo_id}'): ").strip()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Cancelled.[/dim]")
        return
    if typed != repo_id:
        console.print("[red]Confirmation didn't match — cancelled.[/red]")
        return

    # Phase 2: optional cache purge. Default N — leaving entries un-attached
    # just makes them cross-repo retrieval candidates for everyone else,
    # which is mostly harmless and recoverable if the user re-adds the repo.
    purge = False
    if n_total > 0:
        try:
            ans = console.input("  Purge tied memory entries too? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Cancelled.[/dim]")
            return
        purge = (ans == "y")

    removed = await remove_repo(repo_id)
    if not removed:
        console.print("[yellow]Registry row was already gone.[/yellow]")

    if purge:
        purged_counts = delete_repo_entries(repo_id)
        purged_total = sum(purged_counts.values())
        console.print(
            f"[green]✓ Removed repo '{repo_id}' and purged {purged_total} "
            f"memory entries[/green] "
            f"[dim]({purged_counts['findings']}f · "
            f"{purged_counts['corrections']}c · "
            f"{purged_counts['planning']}p)[/dim]"
        )
    else:
        console.print(
            f"[green]✓ Removed repo '{repo_id}' from registry.[/green]"
        )
        if n_total > 0:
            console.print(
                f"[dim]  {n_total} memory entries left intact — they become "
                f"cross-repo retrieval candidates for other repos.[/dim]"
            )

    # If we just removed the active one, deactivate so guards fire.
    active = await get_active_repo()
    if active is None and not purge:
        # remove_repo also clears is_active by virtue of deleting the row.
        # Be explicit for the user.
        console.print("[dim]No active repo now — set one with `repo use <id>`.[/dim]")
