"""
`build "<requirement>"` — single-pass + clarify-gate breakdown UX.

Flow:
    1. Scan-load repo_profile.
    2. Call planner. If it asks to clarify, collect user answers and
       re-invoke with force_plan=True.
    3. Render the proposed DAG.
    4. Accept structured edits (e/d/s/n) until the user approves (a) or
       quits (q).
    5. On approve, persist the graph via database.save_graph.

planning_memory write-back + retrieval is wired in a later chunk.
"""

import uuid
import json
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from database import init_db, save_graph
from scanner.repo_scanner import load_profile
from orchestrator.planner import plan
from memory.vector_store import add_plan, get_stats
from models import TaskNode, TaskGraph


console = Console()

TYPE_COLORS = {
    "migration":      "magenta",
    "backend":        "cyan",
    "backend-test":   "blue",
    "frontend":       "green",
    "frontend-test":  "yellow",
    "review":         "white",
}


# ---------- Rendering ----------

def _render_graph(graph: TaskGraph) -> None:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True)
    table.add_column("ID",          style="bold")
    table.add_column("Type",        style="white")
    table.add_column("Description", style="white", overflow="fold")
    table.add_column("Depends on",  style="dim")

    for node in graph.nodes:
        color = TYPE_COLORS.get(node.type, "white")
        table.add_row(
            node.id,
            f"[{color}]{node.type}[/{color}]",
            node.description,
            ", ".join(node.dependencies) or "—",
        )

    console.print()
    console.print(Panel.fit(
        f"[bold]Proposed Task Graph[/bold]   "
        f"[dim]({len(graph.nodes)} nodes, "
        f"{sum(len(n.dependencies) for n in graph.nodes)} edges)[/dim]\n"
        f"[dim]Requirement:[/dim] {graph.root_requirement}",
        border_style="blue",
    ))
    console.print(table)


def _print_edit_help() -> None:
    console.print(
        "  [bold][[green]a[/green]]pprove  "
        "[[yellow]e[/yellow]] <id> edit  "
        "[[red]d[/red]] <id> delete  "
        "[[cyan]s[/cyan]] <id> split  "
        "[[magenta]n[/magenta]] new node  "
        "[[dim]l[/dim]] list  "
        "[[red]q[/red]]uit[/bold]"
    )


# ---------- Clarify round handling ----------

def _format_clarify(result: dict) -> str:
    """Pretty-print a clarify response and capture user answer.

    Returns the user's answer text (possibly multi-line collapsed) which the
    caller injects into the second-pass prompt.
    """
    reason = result["reason"]
    console.print()
    console.print(Panel.fit(
        f"[bold yellow]Architect needs clarification[/bold yellow]   "
        f"[dim](reason: {reason})[/dim]\n\n"
        f"[dim]{result['reasoning']}[/dim]",
        border_style="yellow",
    ))

    if result.get("questions"):
        console.print("\n  [bold]Questions:[/bold]")
        for i, q in enumerate(result["questions"], 1):
            console.print(f"    [yellow]Q{i}.[/yellow] {q}")

    if result.get("narrow_options"):
        console.print("\n  [bold]Pick one (or rephrase):[/bold]")
        for i, o in enumerate(result["narrow_options"], 1):
            console.print(f"    [cyan]{i}.[/cyan] {o}")

    # NOTE: square-bracket tokens like "[q]" are interpreted as Rich markup;
    # use escaped brackets so they render as literal text.
    console.print(
        "\n  [dim]Answer on one line, or type a number to pick a narrow "
        "option. \\[q] to abort.[/dim]"
    )

    try:
        answer = console.input("  > ").strip()
    except (KeyboardInterrupt, EOFError):
        return ""

    # If the user typed a number and we have narrow_options, expand it.
    if answer.isdigit() and result.get("narrow_options"):
        idx = int(answer) - 1
        opts = result["narrow_options"]
        if 0 <= idx < len(opts):
            return opts[idx]
    return answer


def _augmented_requirement(original: str, clarify_result: dict, answer: str) -> str:
    """Build the requirement text sent on the force_plan pass."""
    parts = [original.strip()]
    parts.append("")
    parts.append("Clarification context from user:")
    if clarify_result.get("questions"):
        for i, q in enumerate(clarify_result["questions"], 1):
            parts.append(f"  Q{i}: {q}")
    parts.append(f"  User answer: {answer}")
    return "\n".join(parts)


def _build_clarify_history(clarify_result: dict, answer: str) -> str:
    """Prompt-friendly transcript injected on the force_plan pass."""
    lines = []
    for q in clarify_result.get("questions", []):
        lines.append(f"Q: {q}")
    for o in clarify_result.get("narrow_options", []):
        lines.append(f"Option: {o}")
    lines.append(f"User answer: {answer}")
    return "\n".join(lines)


# ---------- Graph editing ----------

def _next_node_id(graph: TaskGraph, hint: str = "n") -> str:
    """Generate a fresh id that doesn't collide with existing ones."""
    existing = {n.id for n in graph.nodes}
    if hint not in existing:
        return hint
    i = 1
    while f"{hint}_{i}" in existing:
        i += 1
    return f"{hint}_{i}"


def _delete_node(graph: TaskGraph, node_id: str) -> bool:
    """Delete node + scrub its id from other nodes' dependencies."""
    before = len(graph.nodes)
    graph.nodes = [n for n in graph.nodes if n.id != node_id]
    if len(graph.nodes) == before:
        return False
    for n in graph.nodes:
        n.dependencies = [d for d in n.dependencies if d != node_id]
    return True


def _edit_description(graph: TaskGraph, node_id: str) -> bool:
    for n in graph.nodes:
        if n.id == node_id:
            try:
                new_desc = console.input(
                    f"  New description for [bold]{node_id}[/bold] "
                    f"(current: {n.description[:60]}…): "
                ).strip()
            except (KeyboardInterrupt, EOFError):
                return False
            if new_desc:
                n.description = new_desc
                return True
            return False
    return False


def _split_node(graph: TaskGraph, node_id: str) -> bool:
    """
    Split node X into X_a, X_b, ... — the split parts inherit X's
    dependencies, X_b depends on X_a (linear chain by default), and any node
    that previously depended on X now depends on the last split part.
    """
    target = next((n for n in graph.nodes if n.id == node_id), None)
    if not target:
        return False
    try:
        n_str = console.input(
            f"  Split [bold]{node_id}[/bold] into how many parts? [2] "
        ).strip()
        n_parts = int(n_str) if n_str else 2
    except (KeyboardInterrupt, EOFError, ValueError):
        return False
    if n_parts < 2:
        return False

    descs = []
    for i in range(n_parts):
        try:
            d = console.input(
                f"  Description for split {i+1}: "
            ).strip()
        except (KeyboardInterrupt, EOFError):
            return False
        if not d:
            return False
        descs.append(d)

    suffixes = "abcdefghijklmn"
    new_ids = [f"{node_id}{suffixes[i]}" for i in range(n_parts)]
    new_nodes = []
    for i, (nid, desc) in enumerate(zip(new_ids, descs)):
        deps = list(target.dependencies) if i == 0 else [new_ids[i-1]]
        new_nodes.append(TaskNode(
            id=nid, type=target.type, description=desc, dependencies=deps,
        ))

    # Re-wire any node that previously depended on `node_id` to depend on
    # the LAST split part (preserves linear order).
    final_id = new_ids[-1]

    idx = next(i for i, n in enumerate(graph.nodes) if n.id == node_id)
    graph.nodes[idx:idx+1] = new_nodes

    for n in graph.nodes:
        if n.id in new_ids:
            continue
        n.dependencies = [
            final_id if d == node_id else d
            for d in n.dependencies
        ]
    return True


def _add_node(graph: TaskGraph) -> bool:
    try:
        type_ = console.input(
            "  Type [migration/backend/backend-test/frontend/frontend-test/review]: "
        ).strip() or "backend"
        desc = console.input("  Description: ").strip()
        deps_str = console.input(
            "  Depends on (comma-separated ids, blank for none): "
        ).strip()
    except (KeyboardInterrupt, EOFError):
        return False
    if not desc:
        return False

    deps = [d.strip() for d in deps_str.split(",") if d.strip()] if deps_str else []
    valid_ids = {n.id for n in graph.nodes}
    deps = [d for d in deps if d in valid_ids]

    nid = _next_node_id(graph, f"n{len(graph.nodes)+1}")
    graph.nodes.append(TaskNode(
        id=nid, type=type_, description=desc, dependencies=deps,
    ))
    return True


def _edit_loop(graph: TaskGraph) -> tuple[bool, list[str]]:
    """
    Returns (approved, edits) where:
      approved  True if the user approved (graph should be persisted),
                False if the user quit without saving.
      edits     Ordered list of human-readable edit operations the user
                performed before approval. Used by planning_memory so future
                builds can mimic the user's preferred decomposition style.
    """
    edits: list[str] = []
    _print_edit_help()
    while True:
        try:
            raw = console.input("  > ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n  [dim]Aborted.[/dim]")
            return False, edits

        if not raw:
            continue

        parts = raw.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("a", "approve"):
            if not graph.nodes:
                console.print(
                    "  [red]Cannot approve an empty graph. "
                    "Add a node with [n] or quit with [q].[/red]"
                )
                continue
            return True, edits
        if cmd in ("q", "quit"):
            return False, edits
        if cmd in ("l", "list"):
            _render_graph(graph)
            continue
        if cmd in ("h", "help", "?"):
            _print_edit_help()
            continue
        if cmd == "e":
            if not arg:
                console.print("  [dim]Usage: e <node_id>[/dim]")
                continue
            ok = _edit_description(graph, arg)
            if ok:
                edits.append(f"edited {arg}")
                console.print(f"  [green]✓ {arg} updated[/green]")
                _render_graph(graph)
            else:
                console.print(f"  [red]Could not edit {arg}[/red]")
            continue
        if cmd == "d":
            if not arg:
                console.print("  [dim]Usage: d <node_id>[/dim]")
                continue
            ok = _delete_node(graph, arg)
            if ok:
                edits.append(f"deleted {arg}")
                console.print(f"  [green]✓ {arg} deleted[/green]")
                _render_graph(graph)
            else:
                console.print(f"  [red]Node {arg} not found[/red]")
            continue
        if cmd == "s":
            if not arg:
                console.print("  [dim]Usage: s <node_id>[/dim]")
                continue
            ok = _split_node(graph, arg)
            if ok:
                edits.append(f"split {arg}")
                console.print(f"  [green]✓ {arg} split[/green]")
                _render_graph(graph)
            else:
                console.print(f"  [red]Could not split {arg}[/red]")
            continue
        if cmd == "n":
            n_before = len(graph.nodes)
            ok = _add_node(graph)
            if ok:
                new_id = graph.nodes[-1].id if len(graph.nodes) > n_before else "?"
                edits.append(f"added new node {new_id}")
                console.print(f"  [green]✓ added new node[/green]")
                _render_graph(graph)
            else:
                console.print(f"  [red]Add cancelled[/red]")
            continue

        console.print(f"  [dim]Unknown command: {cmd}. Try 'h' for help.[/dim]")


# ---------- Entry point ----------

async def cmd_build(requirement: str) -> None:
    requirement = requirement.strip()
    if not requirement:
        console.print("[red]Usage: build \"<natural-language requirement>\"[/red]")
        return

    await init_db()

    try:
        repo_profile = load_profile()
    except FileNotFoundError:
        console.print("[red]Repo not scanned yet. Run: scan[/red]")
        return

    console.print(Panel.fit(
        f"[bold]Build[/bold]: {requirement}",
        border_style="blue",
    ))

    # Track clarify trace for planning_memory write-back.
    needed_clarify  = False
    clarify_record  = ""

    # ---------- Planner pass 1 ----------
    with console.status("[bold blue]Planning task graph...[/bold blue]"):
        try:
            result = await plan(requirement, repo_profile)
        except Exception as e:
            console.print(f"[red]Planner failed: {e}[/red]")
            return

    # Surface planning_memory hits — demo signal that the system is using
    # what it learned from past builds.
    hits = (result.get("memory_injected") or {}).get("planning_hits", 0)
    if hits:
        console.print(
            f"  [dim]Planner memory: {hits} similar past build(s) "
            f"retrieved into the prompt[/dim]"
        )

    # ---------- Clarify round (at most once) ----------
    if result["action"] == "clarify":
        needed_clarify = True
        answer = _format_clarify(result)
        if not answer or answer.lower() == "q":
            console.print("\n  [dim]Aborted at clarify step.[/dim]")
            return
        clarify_record = _build_clarify_history(result, answer)

        with console.status("[bold blue]Re-planning with your answers...[/bold blue]"):
            try:
                result = await plan(
                    _augmented_requirement(requirement, result, answer),
                    repo_profile,
                    force_plan=True,
                    clarify_history=clarify_record,
                )
            except Exception as e:
                console.print(f"[red]Planner failed on force_plan pass: {e}[/red]")
                return

        if result["action"] != "plan":
            console.print(
                "[red]Planner returned clarify on force_plan pass — aborting.[/red]"
            )
            return

    # ---------- Construct TaskGraph ----------
    graph_id = f"GRAPH-{uuid.uuid4().hex[:8]}"
    graph = TaskGraph(
        graph_id=graph_id,
        root_requirement=requirement,
        nodes=[TaskNode(**n) for n in result["graph"]["nodes"]],
        created_at=datetime.now().isoformat(),
    )

    _render_graph(graph)
    if result.get("reasoning"):
        console.print(f"  [dim]Planner reasoning:[/dim] {result['reasoning']}")
    console.print()

    # ---------- Edit loop ----------
    approved, edits = _edit_loop(graph)

    if not approved:
        console.print("\n  [dim]Graph discarded — not saved.[/dim]")
        return

    # ---------- Persist graph ----------
    payload = graph.model_dump()
    payload["approved"] = True
    await save_graph(payload)

    # ---------- Reflection write-back to planning_memory ----------
    # Every approved build becomes a training signal for future builds:
    # similar requirements pull this trace forward, and edits the user
    # made nudge the next planner output toward the same shape.
    node_types = [n.type for n in graph.nodes]
    try:
        add_plan(
            plan_id        = graph_id,
            requirement    = requirement,
            needed_clarify = needed_clarify,
            clarify_qa     = clarify_record,
            node_count     = len(graph.nodes),
            node_types     = node_types,
            edits          = edits,
            approved       = True,
        )
        memory_msg = "added to planning_memory"
    except Exception as e:
        memory_msg = f"planning_memory write FAILED: {e}"

    stats = get_stats()
    console.print(Panel.fit(
        f"[green]✓ Saved as {graph_id}[/green]   "
        f"[dim]({len(graph.nodes)} nodes, "
        f"{sum(len(n.dependencies) for n in graph.nodes)} edges)[/dim]\n"
        f"[dim]{memory_msg} "
        f"({stats.get('planning_in_memory', 0)} total plans in memory)[/dim]",
        border_style="green",
    ))
