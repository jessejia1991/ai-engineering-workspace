import asyncio
import os
import json
import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box
from dotenv import load_dotenv

load_dotenv()

console = Console()

# REPO_PATH was previously a module-level constant; it's now read fresh
# in _cmd_scan so the `init` wizard's mid-process .env update is picked up
# without restart.


def print_banner():
    console.print(Panel.fit(
        "[bold blue]AI Engineering Workspace[/bold blue]\n"
        "[dim]Target: spring-petclinic-reactjs[/dim]",
        border_style="blue"
    ))


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """AI Engineering Workspace — multi-agent code review system."""
    if ctx.invoked_subcommand is None:
        # Auto-trigger the init wizard on first run. Threshold: no
        # ANTHROPIC_API_KEY in env. Other fields are nice-to-have but
        # nothing works without the key, so this is the right gate.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            console.print(
                "[yellow]No ANTHROPIC_API_KEY configured — running setup wizard.[/yellow]"
            )
            console.print(
                "[dim]This runs once. Re-run later with the [bold]init[/bold] command.[/dim]\n"
            )
            asyncio.run(_cmd_init())
            console.print()

        print_banner()
        console.print("[dim]Type 'help' for available commands, 'exit' to quit.[/dim]\n")
        _interactive_shell()


def _interactive_shell():
    while True:
        try:
            raw = console.input("[bold green]ai-eng>[/bold green] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not raw:
            continue
        if raw in ("exit", "quit"):
            console.print("[dim]Goodbye.[/dim]")
            break
        if raw == "help":
            _print_help()
            continue

        parts = raw.split()
        cmd = parts[0]
        args = parts[1:]

        if cmd == "scan":
            asyncio.run(_cmd_scan())
        elif cmd == "status":
            asyncio.run(_cmd_status())
        elif cmd == "review":
            pr_number = None
            branch = None
            graph_id = None
            no_graph = False
            post_decision = None         # None = use env; True = force; False = force-skip
            i = 0
            while i < len(args):
                if args[i] == "--pr" and i+1 < len(args):
                    pr_number = int(args[i+1])
                    i += 2
                elif args[i] == "--branch" and i+1 < len(args):
                    branch = args[i+1]
                    i += 2
                elif args[i] == "--graph" and i+1 < len(args):
                    graph_id = args[i+1]
                    i += 2
                elif args[i] == "--no-graph":
                    no_graph = True
                    i += 1
                elif args[i] == "--post":
                    post_decision = True
                    i += 1
                elif args[i] == "--no-post":
                    post_decision = False
                    i += 1
                else:
                    i += 1
            if pr_number is None:
                console.print("[red]Usage: review --pr <number> [--branch <name>] "
                              "[--graph GRAPH-xyz | --no-graph] [--post | --no-post][/red]")
            else:
                asyncio.run(_cmd_review(pr_number, branch, graph_id, no_graph, post_decision))
        elif cmd == "reflect":
            task_id = args[0] if args else None
            asyncio.run(_cmd_reflect(task_id))
        elif cmd == "logs":
            task_id = args[0] if args else None
            asyncio.run(_cmd_logs(task_id))
        elif cmd == "build":
            # `build "requirement text"` — args are joined and stripped of quotes.
            joined = raw[len(cmd):].strip()
            if joined.startswith('"') and joined.endswith('"') and len(joined) >= 2:
                joined = joined[1:-1]
            asyncio.run(_cmd_build(joined))
        elif cmd == "trace":
            asyncio.run(_cmd_trace(args))
        elif cmd == "repo":
            asyncio.run(_cmd_repo(args))
        elif cmd == "memory":
            asyncio.run(_cmd_memory(args))
        elif cmd == "init":
            asyncio.run(_cmd_init())
        else:
            console.print(f"[red]Unknown command: {cmd}[/red]")
            _print_help()


def _print_help():
    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Command", style="bold cyan", width=30)
    table.add_column("Description", style="white")
    table.add_row("scan",                         "Scan target repo and build profile")
    table.add_row("review --pr <N>",              "Run multi-agent review on PR #N")
    table.add_row("review --pr <N> --branch <B>", "Review specific branch")
    table.add_row("reflect [task_id]",            "Accept/reject findings, update reflection")
    table.add_row("build \"<requirement>\"",      "Break a natural-language requirement into a task graph")
    table.add_row("status",                       "Show all tasks and their current state")
    table.add_row("logs <task_id>",               "Show execution log for a task")
    table.add_row("trace show <trace_id>",        "Render LLM trace tree (use --prompt to expand)")
    table.add_row("trace replay <obs_id>",        "Re-run one captured generation with an edited prompt")
    table.add_row("repo list",                    "List registered repos (active marked)")
    table.add_row("repo add <path> [--use]",      "Register a repo for scoped memory")
    table.add_row("repo use <id>",                "Switch active repo")
    table.add_row("repo remove <id>",             "Unregister (with optional purge of cached memory)")
    table.add_row("memory stats [--repo X]",      "Memory counts per collection / per repo")
    table.add_row("memory prune [--dry-run]",     "LRU evict from memory (pinned + young protected)")
    table.add_row("memory compact",               "LLM-driven cluster merge of duplicate corrections")
    table.add_row("init",                         "Re-run the setup wizard (keys, model, first repo)")
    table.add_row("exit",                         "Exit the shell")
    console.print(table)


async def _cmd_scan():
    from scanner.repo_scanner import scan
    from database import init_db, add_repo, set_active_repo, get_active_repo, get_repo

    # Read fresh so post-init .env updates take effect without restart.
    repo_path = os.environ.get("PETCLINIC_REPO_PATH", "")
    if not repo_path:
        console.print(
            "[red]PETCLINIC_REPO_PATH not set in .env[/red] "
            "[dim]— run [bold]init[/bold] or edit .env.[/dim]"
        )
        return

    with console.status("[bold blue]Scanning repository...[/bold blue]"):
        try:
            profile = scan(repo_path)
        except FileNotFoundError as e:
            console.print(f"[red]{e}[/red]")
            return

    # Auto-register the scanned repo. `scan` is the natural discovery point;
    # forcing the user to also run `repo add` afterwards is needless friction.
    # Behavior: always upsert. Activate ONLY if there is no current active
    # repo — second-scan of a different path doesn't silently switch you.
    await init_db()
    scanned_id = profile["repo_id"]
    await add_repo(scanned_id, repo_path, display_name=scanned_id)
    active = await get_active_repo()
    if active is None:
        await set_active_repo(scanned_id)
        console.print(f"[green]✓ Registered + activated repo '{scanned_id}'[/green]")
    elif active["id"] != scanned_id:
        existing = await get_repo(scanned_id)
        console.print(
            f"[dim]Registered repo '{scanned_id}' "
            f"(active stays on '{active['id']}'; "
            f"run `repo use {scanned_id}` to switch).[/dim]"
        )
    # else: same repo re-scanned, no message needed

    console.print()
    console.print(Panel(
        f"[bold]{profile['repo_id']}[/bold]\n"
        f"[dim]{profile['repo_path']}[/dim]",
        title="Repository", border_style="blue"
    ))

    table = Table(box=box.SIMPLE_HEAVY, show_header=True)
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right", style="cyan")
    table.add_row("Backend files",  str(len(profile["files"]["backend"])))
    table.add_row("Frontend files", str(len(profile["files"]["frontend"])))
    table.add_row("Test files",     str(len(profile["files"]["test"])))
    table.add_row("Config files",   str(len(profile["files"]["config"])))
    table.add_row("Total files",    str(profile["files"]["total"]))
    console.print(table)

    if profile["corrections"]:
        console.print(f"\n[yellow]⚠ {len(profile['corrections'])} correction(s) loaded[/yellow]")
    else:
        console.print("\n[dim]No corrections recorded yet.[/dim]")

    console.print(f"\n[green]✓ Profile saved to .ai-workspace/repo-context.json[/green]")
    console.print(f"[dim]Scanned at: {profile['scanned_at']}[/dim]\n")


async def _cmd_status():
    from database import init_db, get_all_tasks

    await init_db()
    tasks = await get_all_tasks()

    if not tasks:
        console.print("[dim]No tasks yet. Run a review to create tasks.[/dim]")
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True)
    table.add_column("Task ID",  style="bold cyan")
    table.add_column("Type",     style="white")
    table.add_column("Status",   style="white")
    table.add_column("Created",  style="dim")

    status_colors = {
        "PENDING":        "yellow",
        "IN_PROGRESS":    "blue",
        "REVIEWING":      "blue",
        "AWAITING_HUMAN": "magenta",
        "DONE":           "green",
        "REJECTED":       "red",
    }

    for t in tasks:
        color = status_colors.get(t["status"], "white")
        table.add_row(
            t["id"],
            t["type"],
            f"[{color}]{t['status']}[/{color}]",
            t["created_at"][:19] if t["created_at"] else ""
        )

    console.print(table)


async def _cmd_review(pr_number: int, branch: str = None,
                      graph_id: str = None, no_graph: bool = False,
                      post_decision: bool = None):
    from cli.review_cmd import cmd_review
    await cmd_review(pr_number, branch, graph_id=graph_id, no_graph=no_graph,
                     post_decision=post_decision)


async def _cmd_reflect(task_id: str = None):
    from cli.reflect_cmd import cmd_reflect
    await cmd_reflect(task_id)


async def _cmd_build(requirement: str):
    from cli.build_cmd import cmd_build
    await cmd_build(requirement)


async def _cmd_trace(args: list[str]):
    from cli.trace_cmd import cmd_trace
    await cmd_trace(args)


async def _cmd_repo(args: list[str]):
    from cli.repo_cmd import cmd_repo
    await cmd_repo(args)


async def _cmd_memory(args: list[str]):
    from cli.memory_cmd import cmd_memory
    await cmd_memory(args)


async def _cmd_init():
    from cli.init_cmd import cmd_init
    await cmd_init()


async def _cmd_logs(task_id: str = None):
    from database import init_db, get_execution_log, get_all_tasks

    await init_db()

    # If no task_id, fall back to the most recent task
    if not task_id:
        tasks = await get_all_tasks()
        if not tasks:
            console.print("[dim]No tasks yet. Run a review first.[/dim]")
            return
        task_id = tasks[0]["id"]
        console.print(f"[dim]Using most recent task: {task_id}[/dim]\n")

    rows = await get_execution_log(task_id)
    if not rows:
        console.print(f"[dim]No execution log entries for {task_id}.[/dim]")
        return

    console.print(f"\n[bold]Execution log for {task_id}[/bold] ({len(rows)} entries)\n")

    for row in rows:
        event = row.get("event_type", "")
        agent = row.get("agent", "")
        ts    = (row.get("created_at") or "")[:19]

        try:
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        except (json.JSONDecodeError, TypeError):
            payload = {}

        console.print(
            f"[dim]{ts}[/dim]  [bold cyan]{event}[/bold cyan]  "
            f"[dim]{agent}[/dim]"
        )

        if event == "agent_selection":
            selected = payload.get("selected", [])
            skipped  = payload.get("skipped", {})
            console.print(f"  Selected: {', '.join(selected) or '(none)'}")
            for ag, reason in skipped.items():
                console.print(f"  [dim]Skipped {ag}: {reason}[/dim]")

        elif event == "agent_result":
            latency = payload.get("latency_ms")
            count   = payload.get("finding_count")
            status  = payload.get("status", "ok")
            console.print(
                f"  [dim]status={status}  latency={latency}ms  "
                f"findings={count}[/dim]"
            )

            memory = payload.get("memory_injected", {}) or {}
            mf = memory.get("findings_count", 0)
            mc = memory.get("corrections_count", 0)
            console.print(
                f"  [dim]Memory injected: {mf} finding(s), "
                f"{mc} correction(s)[/dim]"
            )

            reasoning = payload.get("reasoning", {}) or {}
            understanding = reasoning.get("codebase_understanding")
            if understanding:
                console.print(f"  [dim]Understanding:[/dim] {understanding}")

            rejected = reasoning.get("rejected_candidates", []) or []
            if rejected:
                console.print(f"  [dim]Rejected candidates ({len(rejected)}):[/dim]")
                for rc in rejected:
                    issue = rc.get("issue", "")
                    why   = rc.get("why_rejected", "")
                    console.print(f"    [yellow]–[/yellow] {issue}")
                    if why:
                        console.print(f"      [dim]→ {why}[/dim]")

        elif event == "agent_retry":
            attempt = payload.get("attempt")
            error   = payload.get("error", "")
            console.print(f"  [red]retry #{attempt}: {error}[/red]")

        console.print()


if __name__ == "__main__":
    cli()
