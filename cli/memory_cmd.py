"""
`memory {prune,compact,stats}` — maintenance + observability for memory pools.

Design choices (from PROGRESS.md §16.7 + 2026-05-14 industry survey):
  - prune is LRU-based but with three safeguards: never evict pinned items,
    never evict items younger than `age_floor` days (give cold-but-valuable
    items time to be hit), keep at least `size_floor` items per collection.
  - compact is LLM-driven cluster merge with per-cluster human confirmation,
    same shape as Phoenix / LangSmith / Braintrust prompt playgrounds: load
    the cluster, propose merged content, human says yes/no for each.
  - Both default to dry-run-style safety (prune --dry-run shows what would
    go; compact is always per-cluster confirmed).
"""

from __future__ import annotations

import json
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from database import init_db, get_active_repo
from memory.vector_store import (
    list_entries, delete_entries,
    get_stats, get_stats_by_repo,
)


console = Console()


# ---------- dispatcher --------------------------------------------------

async def cmd_memory(args: list[str]) -> None:
    if not args:
        console.print(
            "[red]Usage: memory stats [--repo X]  |  "
            "memory prune [...]  |  memory compact [...][/red]"
        )
        return

    action = args[0]
    rest = args[1:]
    if action == "stats":
        await _memory_stats(rest)
    elif action == "prune":
        await _memory_prune(rest)
    elif action == "compact":
        await _memory_compact(rest)
    else:
        console.print(f"[red]Unknown memory action: {action}[/red]")
        console.print("[dim]Supported: stats · prune · compact[/dim]")


# ---------- common arg parsing ------------------------------------------

def _parse_kv_args(rest: list[str], known_flags: dict[str, str]) -> dict:
    """
    Tiny CLI parser. `known_flags` maps flag name → 'value' (takes next arg)
    or 'bool' (no value). Unknown flags raise.
    """
    out: dict = {}
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag not in known_flags:
            raise ValueError(f"unknown flag: {flag}")
        kind = known_flags[flag]
        if kind == "value":
            if i + 1 >= len(rest):
                raise ValueError(f"flag {flag} expects a value")
            out[flag.lstrip("-")] = rest[i + 1]
            i += 2
        elif kind == "bool":
            out[flag.lstrip("-")] = True
            i += 1
        else:
            raise ValueError(f"bad flag kind: {kind}")
    return out


# ---------- stats -------------------------------------------------------

async def _memory_stats(rest: list[str]) -> None:
    try:
        opts = _parse_kv_args(rest, {"--repo": "value"})
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return

    await init_db()
    repo_id = opts.get("repo")

    if repo_id:
        stats = get_stats(repo_id=repo_id)
        console.print(Panel.fit(
            f"[bold]Memory stats for repo[/bold] '{repo_id}'\n"
            f"  Findings:    [cyan]{stats['findings_in_memory']}[/cyan]\n"
            f"  Corrections: [cyan]{stats['corrections_in_memory']}[/cyan]\n"
            f"  Plans:       [cyan]{stats['planning_in_memory']}[/cyan]",
            border_style="blue",
        ))
        return

    # No repo: per-repo breakdown
    by_repo = get_stats_by_repo()
    if not by_repo:
        console.print("[dim]Memory is empty.[/dim]")
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True, title="Memory by repo")
    table.add_column("Repo",        style="cyan bold")
    table.add_column("Findings",    justify="right")
    table.add_column("Corrections", justify="right")
    table.add_column("Plans",       justify="right")
    table.add_column("Total",       justify="right", style="bold")

    grand = {"f": 0, "c": 0, "p": 0}
    for rid in sorted(by_repo):
        c = by_repo[rid]
        f, cor, pl = c["findings_in_memory"], c["corrections_in_memory"], c["planning_in_memory"]
        grand["f"] += f; grand["c"] += cor; grand["p"] += pl
        table.add_row(rid, str(f), str(cor), str(pl), str(f + cor + pl))
    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{grand['f']}[/bold]",
        f"[bold]{grand['c']}[/bold]",
        f"[bold]{grand['p']}[/bold]",
        f"[bold]{sum(grand.values())}[/bold]",
    )
    console.print(table)


# ---------- prune -------------------------------------------------------

async def _memory_prune(rest: list[str]) -> None:
    try:
        opts = _parse_kv_args(rest, {
            "--repo":                 "value",
            "--age-floor-days":       "value",
            "--max-per-collection":   "value",
            "--dry-run":              "bool",
        })
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return

    repo_id        = opts.get("repo")
    age_floor_days = float(opts.get("age-floor-days", 7))
    max_per_coll   = int(opts.get("max-per-collection", 50))
    dry_run        = bool(opts.get("dry-run", False))

    await init_db()
    if not repo_id:
        active = await get_active_repo()
        if active:
            repo_id = active["id"]
        # else: no repo filter — prune across the global pool. Surprising
        # but legitimate operator action.

    console.print(Panel.fit(
        f"[bold]memory prune[/bold]  "
        f"repo={repo_id or 'ALL'} · age_floor={age_floor_days}d · "
        f"max_per_collection={max_per_coll} · "
        f"{'[yellow]DRY RUN[/yellow]' if dry_run else '[red]LIVE[/red]'}",
        border_style="blue",
    ))

    now_ts = datetime.now().timestamp()
    age_floor_secs = age_floor_days * 86400
    total_evicted = 0

    for collection in ("findings", "corrections", "planning"):
        entries = list_entries(collection, repo_id=repo_id)
        if not entries:
            console.print(f"[dim]{collection}: empty — skipped.[/dim]")
            continue

        # Partition: pinned never evicted; young never evicted; the rest are
        # candidates ordered by last_accessed_at ascending (least recently
        # used first).
        protected: list[dict] = []
        candidates: list[dict] = []
        for e in entries:
            meta = e.get("metadata") or {}
            pinned    = bool(meta.get("pinned"))
            last_acc  = float(meta.get("last_accessed_at",
                                       meta.get("timestamp", now_ts)))
            age_secs  = now_ts - last_acc
            if pinned or age_secs < age_floor_secs:
                protected.append(e)
            else:
                e["_last_acc"] = last_acc
                candidates.append(e)
        candidates.sort(key=lambda x: x["_last_acc"])

        # Size floor: keep at least max_per_coll across protected + survivors.
        # Already-protected items count toward the floor.
        keep_count = max(0, max_per_coll - len(protected))
        # Evict the LRU prefix that pushes the survivor count above keep_count.
        to_evict = candidates[:max(0, len(candidates) - keep_count)]
        survivors = candidates[len(to_evict):]

        console.print(
            f"[bold]{collection}[/bold]: "
            f"[white]{len(entries)}[/white] total · "
            f"[green]{len(protected)} protected[/green] "
            f"([dim]pinned + young[/dim]) · "
            f"[cyan]{len(survivors)} survivors[/cyan] · "
            f"[red]{len(to_evict)} to evict[/red]"
        )

        for e in to_evict[:10]:
            meta = e.get("metadata") or {}
            age_days = (now_ts - e["_last_acc"]) / 86400
            doc = (e.get("document") or "")[:80]
            console.print(
                f"  [red]✗[/red] {e['id']:8s}  "
                f"[dim]last_accessed: {age_days:.1f}d ago · "
                f"repo={meta.get('repo_id', '?')}[/dim]  {doc}"
            )
        if len(to_evict) > 10:
            console.print(f"  [dim]… and {len(to_evict) - 10} more[/dim]")

        if not dry_run and to_evict:
            n_deleted = delete_entries(collection, [e["id"] for e in to_evict])
            console.print(f"  [green]→ deleted {n_deleted} entries[/green]")
            total_evicted += n_deleted

    if dry_run:
        console.print(
            "\n[yellow]Dry run — no entries deleted.[/yellow] "
            "Drop [bold]--dry-run[/bold] to commit."
        )
    else:
        console.print(f"\n[green]Total evicted: {total_evicted}[/green]")


# ---------- compact ----------------------------------------------------
# M7 lives in cli/memory_compact.py — kept separate because the LLM
# integration is meatier than prune's pure-data logic. Dispatched here:

async def _memory_compact(rest: list[str]) -> None:
    from cli.memory_compact import run_compact
    await run_compact(rest)
