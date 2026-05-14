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

REPO_PATH = os.environ.get("PETCLINIC_REPO_PATH", "")


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
        # 交互式shell模式
        print_banner()
        console.print("[dim]Type 'help' for available commands, 'exit' to quit.[/dim]\n")
        _interactive_shell()


def _interactive_shell():
    """简单的交互式shell"""
    import subprocess
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

        # 把输入转成命令执行
        parts = raw.split()
        cmd = parts[0]
        args = parts[1:]

        if cmd == "scan":
            asyncio.run(_cmd_scan())
        elif cmd == "status":
            asyncio.run(_cmd_status())
        elif cmd == "review":
            if not args:
                console.print("[red]Usage: review --pr <number> [--branch <name>][/red]")
            else:
                # 解析简单参数
                pr_number = None
                branch = None
                i = 0
                while i < len(args):
                    if args[i] == "--pr" and i+1 < len(args):
                        pr_number = int(args[i+1])
                        i += 2
                    elif args[i] == "--branch" and i+1 < len(args):
                        branch = args[i+1]
                        i += 2
                    else:
                        i += 1
                asyncio.run(_cmd_review(pr_number, branch))
        elif cmd == "reflect":
            task_id = args[0] if args else None
            asyncio.run(_cmd_reflect(task_id))
        elif cmd == "logs":
            task_id = args[0] if args else None
            asyncio.run(_cmd_logs(task_id))
        else:
            console.print(f"[red]Unknown command: {cmd}[/red]")
            _print_help()


def _print_help():
    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Command", style="bold cyan", width=30)
    table.add_column("Description", style="white")
    table.add_row("scan",                        "Scan target repo and build profile")
    table.add_row("review --pr <N>",             "Run multi-agent review on PR #N")
    table.add_row("review --pr <N> --branch <B>","Review specific branch")
    table.add_row("reflect [task_id]",           "Accept/reject findings, update reflection")
    table.add_row("status",                      "Show all tasks and their current state")
    table.add_row("logs <task_id>",              "Show execution log for a task")
    table.add_row("exit",                        "Exit the shell")
    console.print(table)


async def _cmd_scan():
    from scanner.repo_scanner import scan

    if not REPO_PATH:
        console.print("[red]PETCLINIC_REPO_PATH not set in .env[/red]")
        return

    with console.status("[bold blue]Scanning repository...[/bold blue]"):
        try:
            profile = scan(REPO_PATH)
        except FileNotFoundError as e:
            console.print(f"[red]{e}[/red]")
            return

    # 展示结果
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
        console.print(f"\n[yellow]⚠ {len(profile['corrections'])} correction(s) loaded from history[/yellow]")
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
    table.add_column("Task ID",    style="bold cyan")
    table.add_column("Type",       style="white")
    table.add_column("Status",     style="white")
    table.add_column("Created",    style="dim")

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


async def _cmd_review(pr_number: int, branch: str = None):
    """placeholder — Day 2에서 구현"""
    console.print(f"[yellow]review command coming in Day 2[/yellow]")
    console.print(f"[dim]PR: #{pr_number}  Branch: {branch or 'auto'}[/dim]")


async def _cmd_reflect(task_id: str = None):
    """placeholder — Day 3에서 구현"""
    console.print(f"[yellow]reflect command coming in Day 3[/yellow]")


async def _cmd_logs(task_id: str = None):
    """placeholder — Day 3에서 구현"""
    console.print(f"[yellow]logs command coming in Day 3[/yellow]")


if __name__ == "__main__":
    cli()
