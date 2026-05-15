"""
`build "<requirement>"` — multi-expert plan + Architect Report + contract.

Flow (P4 Chunk C):
    Stage 1  plan_with_experts   — 5 expert agents review the requirement
    Stage 2  synthesize_report   — LLM consolidates into a triage payload
    Stage 3  render Report       — always shown to the user
    Stage 4  collect picks       — user answers Qs + accepts suggestions
    Stage 5  plan() final        — generate the DAG using augmented req
    Stage 6  render Graph + Contract
    Stage 7  unified edit loop   — graph (e/d/s/n) and contract (ec/dc/nc)
    Stage 8  save                — graph + contract together, planning_memory
"""

import uuid
import re
import json
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from database import init_db, save_graph
from scanner.repo_scanner import load_profile
from orchestrator.planner import plan, plan_with_experts, synthesize_report
from memory.vector_store import add_plan, get_stats
from agents.llm_client import client as llm_client, format_usage_summary, set_trace_context
from models import TaskNode, TaskGraph, Contract, Criterion


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
        "  [bold]Graph:[/bold]    "
        "[green]a[/green]pprove   "
        "[yellow]e[/yellow] <id> edit   "
        "[red]d[/red] <id> delete   "
        "[cyan]s[/cyan] <id> split   "
        "[magenta]n[/magenta] new node"
    )
    console.print(
        "  [bold]Contract:[/bold] "
        "[yellow]ec[/yellow] <id> edit assertion   "
        "[red]dc[/red] <id> delete   "
        "[cyan]ep[/cyan] <id> edit priority   "
        "[magenta]nc[/magenta] new criterion"
    )
    console.print(
        "  [bold]Other:[/bold]    "
        "[dim]l[/dim] list   "
        "[dim]h[/dim] help   "
        "[red]q[/red] quit"
    )


# ---------- Architect Report rendering + pick collection ----------

AGENT_SHORT = {
    "SecurityAgent":    "Sec",
    "TestingAgent":     "Test",
    "DeliveryAgent":    "Del",
    "UIUXAgent":        "UIUX",
    "PerformanceAgent": "Perf",
}

PRIORITY_STARS_SUGGESTION = {"high": "★★★", "medium": "★★ ", "low":  "★  "}
PRIORITY_STARS_CRITERION  = {"must_have": "★★★", "should_have": "★★ ", "nice_to_have": "★  "}
PRIORITY_COLOR_CRITERION  = {"must_have": "red", "should_have": "yellow", "nice_to_have": "dim white"}


def _short(name: str) -> str:
    return AGENT_SHORT.get(name, name)


def _render_architect_report(report: dict) -> None:
    """
    Show the consolidated expert output. Always rendered, even when
    clarify_questions is empty — demo signal that the multi-expert
    intelligence happened.
    """
    summaries  = report.get("expert_summaries", {})
    questions  = report.get("clarify_questions", [])
    suggestions = report.get("design_suggestions", [])
    criteria   = report.get("draft_criteria", [])

    console.print()
    console.print(Panel.fit(
        f"[bold]Architect Report[/bold]   "
        f"[dim]({len(summaries)} experts · {len(questions)} questions · "
        f"{len(suggestions)} suggestions · {len(criteria)} draft criteria)[/dim]",
        border_style="cyan",
    ))

    # Expert summaries — short attribution + one-line take
    if summaries:
        console.print("\n  [bold]Expert perspectives[/bold]")
        for agent, summary in summaries.items():
            console.print(f"    [cyan]{_short(agent):<5}[/cyan] {summary}")

    # Clarify questions
    if questions:
        console.print("\n  [bold]Clarify questions[/bold]")
        for q in questions:
            owners = ", ".join(_short(o) for o in q.get("owners", []))
            console.print(f"    [yellow]{q['id']}[/yellow] [dim]\\[{owners}][/dim]")
            console.print(f"        {q.get('question', '')}")
    else:
        console.print("\n  [dim]No clarify questions — experts have enough context.[/dim]")

    # Design suggestions grouped by priority
    if suggestions:
        console.print("\n  [bold]Design suggestions[/bold]")
        for s in suggestions:
            stars = PRIORITY_STARS_SUGGESTION.get(s["priority"], "    ")
            owner = _short(s["owner_agent"])
            console.print(
                f"    [yellow]{s['id']}[/yellow] {stars} "
                f"[dim]\\[{owner}/{s.get('category', '')}][/dim] "
                f"{s.get('suggestion', '')}"
            )

    # Draft contract preview (first 5 criteria for brevity in the report)
    if criteria:
        console.print(
            f"\n  [bold]Draft contract preview[/bold] "
            f"[dim](full {len(criteria)} criteria shown after planning)[/dim]"
        )
        for c in criteria[:5]:
            stars = PRIORITY_STARS_CRITERION.get(c["priority"], "    ")
            color = PRIORITY_COLOR_CRITERION.get(c["priority"], "white")
            owner = _short(c["owner_agent"])
            console.print(
                f"    [{color}]{c['id']}[/{color}] {stars} "
                f"[dim]\\[{owner}/{c.get('category', '')}][/dim] "
                f"{c.get('assertion', '')}"
            )
        if len(criteria) > 5:
            console.print(f"    [dim]…and {len(criteria) - 5} more[/dim]")

    console.print(
        "\n  [dim]Respond with [yellow]q<n>=<answer>[/yellow] for questions "
        "and [yellow]s<n>[/yellow] to accept suggestions, then end with "
        "[green]go[/green]. Or [red]cancel[/red] to abort.\n"
        "  Example:  q1=2000 chars  q2=optional  s1 s2 s3 s5  go[/dim]"
    )


# Token shapes:
#   q<digit>+=<text>   answer to clarify question, text can have spaces if quoted
#   s<digit>+          accept suggestion id (also written as S1 case-insensitive)
#   go | proceed       end input, proceed to planning
#   cancel | quit      abort

_TOKEN_Q  = re.compile(r"^q(\d+)=(.*)$", re.IGNORECASE)
_TOKEN_S  = re.compile(r"^s(\d+)$",       re.IGNORECASE)
_TOKEN_GO = {"go", "proceed", "g"}
_TOKEN_CX = {"cancel", "quit", "abort"}


def _collect_report_picks(report: dict) -> tuple[dict, list[str], bool]:
    """
    Read user input for the Architect Report. Supports multiple lines —
    accumulate until 'go' (or 'cancel') is typed. Empty input on its
    own line means 'go' implicitly (UX shortcut).

    Returns: (answers: {q_id: text}, accepted_suggestion_ids: [s_id], aborted: bool)
    """
    valid_q_ids = {q["id"].lower() for q in report.get("clarify_questions", [])}
    valid_s_ids = {s["id"].lower() for s in report.get("design_suggestions", [])}

    answers:  dict[str, str] = {}
    accepted: list[str]      = []

    while True:
        try:
            line = console.input("  > ").strip()
        except (KeyboardInterrupt, EOFError):
            return {}, [], True

        if not line:
            return answers, accepted, False
        if line.lower() in _TOKEN_CX:
            return {}, [], True

        # Tokenize. First handle q<n>=<rest of line> case where the
        # answer might contain whitespace (everything after the = up to
        # the next q<n>= or s<n> or 'go' marker).
        tokens = _split_picks_line(line)
        proceed = False
        for tok in tokens:
            if tok.lower() in _TOKEN_GO:
                proceed = True
                continue
            if tok.lower() in _TOKEN_CX:
                return {}, [], True
            qm = _TOKEN_Q.match(tok)
            if qm:
                qid = f"q{qm.group(1)}".lower()
                if qid in valid_q_ids:
                    answers[qid] = qm.group(2).strip()
                else:
                    console.print(f"  [dim]Ignoring unknown question id: {qid}[/dim]")
                continue
            sm = _TOKEN_S.match(tok)
            if sm:
                sid = f"s{sm.group(1)}".lower()
                if sid in valid_s_ids:
                    if sid not in accepted:
                        accepted.append(sid)
                else:
                    console.print(f"  [dim]Ignoring unknown suggestion id: {sid}[/dim]")
                continue
            console.print(f"  [dim]Unrecognized token: {tok!r}[/dim]")

        if proceed:
            return answers, accepted, False


def _split_picks_line(line: str) -> list[str]:
    """
    Split a picks line into tokens where q<n>=<answer> captures
    everything up to the next q<n>= / s<n> / go / cancel marker.
    Simpler than full quoting; good enough for one-line UX.
    """
    boundary = re.compile(
        r"(?=\bq\d+=)|(?=\bs\d+\b)|(?=\bgo\b)|(?=\bproceed\b)|"
        r"(?=\bcancel\b)|(?=\bquit\b)|(?=\babort\b)",
        re.IGNORECASE,
    )
    parts = [p.strip() for p in boundary.split(line) if p.strip()]
    return parts


def _build_clarify_history(
    requirement: str,
    answers: dict,
    accepted_suggestions: list[dict],
) -> str:
    """
    Prompt-friendly transcript injected into the final plan() call so the
    DAG generation respects user-chosen scope.
    """
    lines: list[str] = []
    if answers:
        lines.append("Clarify Q&A:")
        for q_id, ans in answers.items():
            lines.append(f"  {q_id} answer: {ans}")
    if accepted_suggestions:
        lines.append("")
        lines.append("Accepted design suggestions (must be reflected in the DAG):")
        for s in accepted_suggestions:
            lines.append(
                f"  - [{s['priority']}/{s.get('category', '')}] "
                f"{s.get('suggestion', '')}"
            )
    return "\n".join(lines)


# ---------- Contract rendering + editing ----------

def _render_contract(criteria: list[dict]) -> None:
    if not criteria:
        console.print("\n  [dim]Contract is empty.[/dim]")
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True)
    table.add_column("ID",        style="bold")
    table.add_column("Priority",  style="white")
    table.add_column("Owner",     style="cyan")
    table.add_column("Assertion", style="white", overflow="fold")

    pri_count = {"must_have": 0, "should_have": 0, "nice_to_have": 0}
    for c in criteria:
        color = PRIORITY_COLOR_CRITERION.get(c["priority"], "white")
        pri_count[c["priority"]] = pri_count.get(c["priority"], 0) + 1
        table.add_row(
            c["id"],
            f"[{color}]{c['priority']}[/{color}]",
            _short(c["owner_agent"]),
            c.get("assertion", ""),
        )

    console.print()
    console.print(Panel.fit(
        f"[bold]Contract[/bold]   "
        f"[dim]({len(criteria)} criteria — "
        f"{pri_count.get('must_have', 0)} must · "
        f"{pri_count.get('should_have', 0)} should · "
        f"{pri_count.get('nice_to_have', 0)} nice)[/dim]",
        border_style="magenta",
    ))
    console.print(table)


def _delete_criterion(criteria: list[dict], cid: str) -> bool:
    for i, c in enumerate(criteria):
        if c["id"] == cid:
            del criteria[i]
            return True
    return False


def _edit_criterion_assertion(criteria: list[dict], cid: str) -> bool:
    for c in criteria:
        if c["id"] == cid:
            try:
                new = console.input(
                    f"  New assertion for [bold]{cid}[/bold] "
                    f"(current: {c.get('assertion', '')[:60]}…): "
                ).strip()
            except (KeyboardInterrupt, EOFError):
                return False
            if new:
                c["assertion"] = new
                return True
            return False
    return False


def _edit_criterion_priority(criteria: list[dict], cid: str) -> bool:
    for c in criteria:
        if c["id"] == cid:
            try:
                new = console.input(
                    f"  New priority for [bold]{cid}[/bold] "
                    f"(current: {c['priority']}) "
                    f"[must_have | should_have | nice_to_have]: "
                ).strip()
            except (KeyboardInterrupt, EOFError):
                return False
            if new in ("must_have", "should_have", "nice_to_have"):
                c["priority"] = new
                return True
            console.print("  [red]Invalid priority[/red]")
            return False
    return False


def _next_criterion_id(criteria: list[dict]) -> str:
    existing = {c["id"] for c in criteria}
    i = 1
    while f"c{i}" in existing:
        i += 1
    return f"c{i}"


def _add_criterion(criteria: list[dict]) -> bool:
    try:
        priority = console.input(
            "  Priority [must_have | should_have | nice_to_have]: "
        ).strip() or "should_have"
        if priority not in ("must_have", "should_have", "nice_to_have"):
            console.print("  [red]Invalid priority[/red]")
            return False
        category  = console.input("  Category (e.g. 'input-validation'): ").strip() or "general"
        assertion = console.input("  Assertion (testable statement): ").strip()
        if not assertion:
            return False
        rationale = console.input("  Rationale: ").strip()
        owner     = console.input(
            "  Owner agent [SecurityAgent | UIUXAgent | TestingAgent | "
            "PerformanceAgent | DeliveryAgent]: "
        ).strip() or "SecurityAgent"
    except (KeyboardInterrupt, EOFError):
        return False

    cid = _next_criterion_id(criteria)
    criteria.append({
        "id":              cid,
        "priority":        priority,
        "owner_agent":     owner,
        "owners":          [owner],
        "category":        category,
        "assertion":       assertion,
        "rationale":       rationale,
        "suggested_check": "manual",
    })
    return True


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


def _edit_loop(
    graph: TaskGraph,
    criteria: list[dict] | None = None,
) -> tuple[bool, list[str]]:
    """
    Unified edit loop for graph (e/d/s/n) and contract (ec/dc/ep/nc).
    `criteria` is an optional list (mutated in place) of Contract
    criteria dicts — when present, contract commands are available.

    Returns (approved, edits) where edits is the ordered list of user
    actions feeding planning_memory.
    """
    criteria = criteria if criteria is not None else []
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
            _render_contract(criteria)
            continue
        if cmd in ("h", "help", "?"):
            _print_edit_help()
            continue

        # ----- Graph editing -----
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

        # ----- Contract editing -----
        if cmd == "ec":
            if not arg:
                console.print("  [dim]Usage: ec <criterion_id>[/dim]")
                continue
            ok = _edit_criterion_assertion(criteria, arg)
            if ok:
                edits.append(f"edited criterion {arg}")
                console.print(f"  [green]✓ criterion {arg} assertion updated[/green]")
                _render_contract(criteria)
            else:
                console.print(f"  [red]Could not edit criterion {arg}[/red]")
            continue
        if cmd == "dc":
            if not arg:
                console.print("  [dim]Usage: dc <criterion_id>[/dim]")
                continue
            ok = _delete_criterion(criteria, arg)
            if ok:
                edits.append(f"deleted criterion {arg}")
                console.print(f"  [green]✓ criterion {arg} deleted[/green]")
                _render_contract(criteria)
            else:
                console.print(f"  [red]Criterion {arg} not found[/red]")
            continue
        if cmd == "ep":
            if not arg:
                console.print("  [dim]Usage: ep <criterion_id>[/dim]")
                continue
            ok = _edit_criterion_priority(criteria, arg)
            if ok:
                edits.append(f"changed priority of {arg}")
                console.print(f"  [green]✓ {arg} priority updated[/green]")
                _render_contract(criteria)
            else:
                console.print(f"  [red]Could not update priority for {arg}[/red]")
            continue
        if cmd == "nc":
            before = len(criteria)
            ok = _add_criterion(criteria)
            if ok and len(criteria) > before:
                new_id = criteria[-1]["id"]
                edits.append(f"added criterion {new_id}")
                console.print(f"  [green]✓ added criterion {new_id}[/green]")
                _render_contract(criteria)
            else:
                console.print(f"  [red]Add criterion cancelled[/red]")
            continue

        console.print(f"  [dim]Unknown command: {cmd}. Try 'h' for help.[/dim]")


# ---------- Entry point ----------

async def cmd_build(requirement: str) -> None:
    requirement = requirement.strip()
    if not requirement:
        console.print("[red]Usage: build \"<natural-language requirement>\"[/red]")
        return

    try:
        await _cmd_build_inner(requirement)
    finally:
        # P3: LLM usage observability — print on every exit path
        # (success, user quit, planner exception). Reset for next command.
        usage = llm_client.usage_summary()
        if usage["requests"] > 0:
            console.print(f"[dim]LLM usage: {format_usage_summary(usage)}[/dim]")
        llm_client.reset_usage()


async def _cmd_build_inner(requirement: str) -> None:
    await init_db()

    # Memory slice guard: build writes to planning_memory + reads it on next
    # iteration. Need an active repo for both to make sense.
    from database import get_active_repo
    active = await get_active_repo()
    if not active:
        console.print(
            "[red]No active repo set.[/red] Run [bold]repo add <path>[/bold] "
            "then [bold]repo use <id>[/bold]. "
            "[dim](Or run [bold]scan[/bold] — it auto-registers + activates.)[/dim]"
        )
        return
    repo_id = active["id"]

    try:
        repo_profile = load_profile()
    except FileNotFoundError:
        console.print("[red]Repo not scanned yet. Run: scan[/red]")
        return

    # Observability: a build doesn't have a graph_id until approval, so use
    # a one-off trace_id. Every LLM call inside this build (planner,
    # experts, synthesizer) writes observations under this trace.
    build_trace_id = f"build-{uuid.uuid4().hex[:8]}"
    set_trace_context(trace_id=build_trace_id, agent_name="Orchestrator")

    console.print(Panel.fit(
        f"[bold]Build[/bold]: {requirement}",
        border_style="blue",
    ))
    console.print(f"  [dim]trace_id: {build_trace_id} · repo: {repo_id}[/dim]")

    # ---------- Stage 1: plan_with_experts ----------
    with console.status("[bold blue]Running expert agents in parallel...[/bold blue]"):
        try:
            pwe = await plan_with_experts(requirement, repo_profile)
        except Exception as e:
            console.print(f"[red]Expert plan phase failed: {e}[/red]")
            return

    if pwe.get("errors"):
        for agent, err in pwe["errors"].items():
            console.print(f"  [yellow]⚠ {agent} errored: {err}[/yellow]")
    if not pwe.get("expert_outputs"):
        console.print("[red]No expert outputs — cannot synthesize.[/red]")
        return

    sel = pwe["selection"]
    console.print(
        f"  [dim]Experts: {', '.join(_short(n) for n in sel.selected)}[/dim]"
    )

    # ---------- Stage 2: synthesize_report ----------
    with console.status("[bold blue]Synthesizing Architect Report...[/bold blue]"):
        try:
            report = await synthesize_report(pwe["expert_outputs"], requirement)
        except Exception as e:
            console.print(f"[red]Synthesizer failed: {e}[/red]")
            return

    # ---------- Stage 3: render Architect Report ----------
    _render_architect_report(report)

    # ---------- Stage 4: collect user picks ----------
    answers, accepted_ids, aborted = _collect_report_picks(report)
    if aborted:
        console.print("\n  [dim]Cancelled at Architect Report step.[/dim]")
        return

    accepted_suggestions = [
        s for s in report["design_suggestions"] if s["id"].lower() in {a.lower() for a in accepted_ids}
    ]
    console.print(
        f"  [dim]Picks: {len(answers)} question answer(s), "
        f"{len(accepted_suggestions)} suggestion(s) accepted[/dim]"
    )

    needed_clarify = bool(answers or accepted_suggestions)
    clarify_record = _build_clarify_history(requirement, answers, accepted_suggestions)
    augmented_req  = requirement
    if clarify_record:
        augmented_req = f"{requirement}\n\n{clarify_record}"

    # ---------- Stage 5: plan() for final DAG ----------
    with console.status("[bold blue]Generating task graph...[/bold blue]"):
        try:
            # force_plan=True so the planner cannot ask for clarification —
            # the Architect Report already handled that.
            plan_result = await plan(
                augmented_req,
                repo_profile,
                force_plan=True,
                clarify_history=clarify_record,
                repo_id=repo_id,
            )
        except Exception as e:
            console.print(f"[red]DAG planner failed: {e}[/red]")
            return

    if plan_result["action"] != "plan":
        console.print(
            "[red]Planner returned clarify on the force_plan pass — aborting.[/red]"
        )
        return

    hits = (plan_result.get("memory_injected") or {}).get("planning_hits", 0)
    if hits:
        console.print(
            f"  [dim]Planner memory: {hits} similar past build(s) "
            f"retrieved into the prompt[/dim]"
        )

    # ---------- Stage 6: construct + render Graph + Contract ----------
    graph_id = f"GRAPH-{uuid.uuid4().hex[:8]}"
    graph = TaskGraph(
        graph_id=graph_id,
        root_requirement=requirement,
        nodes=[TaskNode(**n) for n in plan_result["graph"]["nodes"]],
        created_at=datetime.now().isoformat(),
    )

    # Start contract from synth's draft_criteria.
    criteria = [dict(c) for c in report.get("draft_criteria", [])]

    _render_graph(graph)
    if plan_result.get("reasoning"):
        console.print(f"  [dim]Planner reasoning:[/dim] {plan_result['reasoning']}")
    _render_contract(criteria)

    # ---------- Stage 7: unified edit loop ----------
    approved, edits = _edit_loop(graph, criteria)
    if not approved:
        console.print("\n  [dim]Graph + contract discarded — not saved.[/dim]")
        return

    # ---------- Stage 8: persist + planning_memory write-back ----------
    contract = Contract(
        contract_id = f"CON-{uuid.uuid4().hex[:8]}",
        graph_id    = graph_id,
        criteria    = [Criterion(**c) for c in criteria],
        created_at  = datetime.now().isoformat(),
    )
    graph.contract = contract

    payload = graph.model_dump()
    payload["approved"] = True
    await save_graph(payload)

    # Reflection: each approved build feeds future ones. Include
    # contract summary in the document so future similar builds can
    # retrieve "this kind of feature usually has N must-haves".
    node_types = [n.type for n in graph.nodes]
    contract_summary = (
        f"Contract: {len(criteria)} criteria — "
        f"{sum(1 for c in criteria if c['priority']=='must_have')} must_have, "
        f"{sum(1 for c in criteria if c['priority']=='should_have')} should_have, "
        f"{sum(1 for c in criteria if c['priority']=='nice_to_have')} nice_to_have. "
        f"Owners: {sorted({c['owner_agent'] for c in criteria})}"
    )

    try:
        add_plan(
            plan_id        = graph_id,
            requirement    = requirement,
            needed_clarify = needed_clarify,
            clarify_qa     = (clarify_record + "\n" + contract_summary).strip(),
            node_count     = len(graph.nodes),
            node_types     = node_types,
            edits          = edits,
            approved       = True,
            repo_id        = repo_id,
        )
        memory_msg = "added to planning_memory"
    except Exception as e:
        memory_msg = f"planning_memory write FAILED: {e}"

    stats = get_stats()
    console.print(Panel.fit(
        f"[green]✓ Saved as {graph_id}[/green]   "
        f"[dim]({len(graph.nodes)} nodes, "
        f"{sum(len(n.dependencies) for n in graph.nodes)} edges, "
        f"{len(criteria)} contract criteria)[/dim]\n"
        f"[dim]{memory_msg} "
        f"({stats.get('planning_in_memory', 0)} total plans in memory)[/dim]",
        border_style="green",
    ))
