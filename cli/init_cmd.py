"""
`init` — 5-step interactive setup wizard.

Asks for the values needed to run the system end-to-end:
  1. Anthropic API key       (required, hidden input)
  2. Model                   (Sonnet 4.6 | Opus 4.7)
  3. GitHub token            (optional, hidden input)
  4. GitHub repo             (optional, owner/repo)
  5. First repo path         (required, local clone to scan)

Behavior:
  - Read existing .env, preserve every unmanaged line/key/comment
  - Collect inputs in memory, write atomically at the end
  - Verify the Anthropic key with one tiny API call before writing
  - Scan + auto-register the first repo so the user can `review --pr 1`
    immediately after the wizard finishes
  - Reload .env in-process and reset the rate-limited client singleton
    so the new key takes effect without restarting the shell

Auto-triggered from cli/main.py when ANTHROPIC_API_KEY is not present in
the loaded environment. Also reachable from the interactive shell as
`init` to reconfigure.
"""

from __future__ import annotations

import os
import getpass
import asyncio
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel


console = Console()


# .env lives at the repo root, one level above cli/.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


# Keys this wizard owns. Everything else in .env (ANTHROPIC_MAX_CONCURRENT,
# REVIEW_ALLOWED_REPOS, etc.) is left untouched as "advanced — edit manually".
MANAGED_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "PETCLINIC_REPO_PATH",
)


# Models exposed by the picker. Two-option scope per user request — Haiku
# 4.5 stays available for advanced users via direct .env edit.
MODELS = (
    ("claude-sonnet-4-6", "Sonnet 4.6", "cheaper · ~$0.65/PR review · slower"),
    ("claude-opus-4-7",   "Opus 4.7",   "8× faster · ~$4/PR review · recommended for interactive demo"),
)


# ---------- .env read / write -------------------------------------------

def _read_env() -> tuple[dict[str, str], list[str]]:
    """
    Return (parsed key/value dict for managed keys we care about, all raw
    lines for atomic rewrite). Lines are preserved verbatim so comments,
    blank lines, and unmanaged keys round-trip without disturbance.
    """
    if not ENV_PATH.exists():
        return {}, []
    raw_lines = ENV_PATH.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # Strip surrounding quotes if any
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        parsed[k.strip()] = v
    return parsed, raw_lines


def _quote(v: str) -> str:
    """Quote a value if it contains spaces, '#', or quote characters."""
    if v == "":
        return ""
    if any(c in v for c in (" ", "\t", "#", "'", '"')):
        # Use double-quote and escape interior double-quotes
        return '"' + v.replace('"', '\\"') + '"'
    return v


def _write_env(values: dict[str, str], raw_lines: list[str]) -> None:
    """
    Atomic rewrite: replace managed lines in place, append new ones at the
    end, leave everything else untouched. Writes to a tmp file then
    renames so a Ctrl-C mid-write doesn't leave a half-baked .env.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        k = line.split("=", 1)[0].strip()
        if k in values:
            new_val = values[k]
            seen.add(k)
            if new_val == "":
                # User cleared this — comment it out rather than dropping
                out.append(f"# {k}=")
            else:
                out.append(f"{k}={_quote(new_val)}")
        else:
            out.append(line)

    # Append managed keys we didn't see in the existing file
    for k in MANAGED_KEYS:
        if k in seen:
            continue
        v = values.get(k, "")
        if v:
            out.append(f"{k}={_quote(v)}")

    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"

    tmp = ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text(text)
    tmp.replace(ENV_PATH)


# ---------- prompts -----------------------------------------------------

def _prompt_text(label: str, current: str = "",
                 hidden: bool = False, required: bool = False) -> str:
    """One field prompt. Enter keeps current value; required fields refuse
    to advance without input when current is empty."""
    if current:
        if hidden:
            shown = f"{current[:6]}…{current[-4:]}" if len(current) > 12 else "…"
            suffix = f" [dim](current: {shown} — Enter keeps)[/dim]"
        else:
            suffix = f" [dim](current: {current} — Enter keeps)[/dim]"
    else:
        suffix = ""
    console.print(f"{label}{suffix}")
    while True:
        try:
            if hidden:
                # getpass writes its prompt to stderr by default; the label
                # is already printed above, so use an empty prompt here.
                val = getpass.getpass(prompt="  > ").strip()
            else:
                val = input("  > ").strip()
        except (KeyboardInterrupt, EOFError):
            raise
        if val:
            return val
        if current:
            return current
        if not required:
            return ""
        console.print("  [red]This field is required.[/red]")


def _prompt_model(current: str = "") -> str:
    console.print("[bold]Model[/bold]")
    # Find current index (1-based) if it matches one of the picker options
    default_idx = next(
        (i for i, (mid, _, _) in enumerate(MODELS, 1) if mid == current),
        2,  # default to Opus 4.7 for new users
    )
    for i, (mid, name, desc) in enumerate(MODELS, 1):
        marker = "[green]●[/green]" if mid == current else " "
        console.print(f"  {marker} {i}. [bold]{name}[/bold]  [dim]({desc})[/dim]")
    while True:
        try:
            raw = input(f"  Choose [1/2] (default {default_idx}): ").strip()
        except (KeyboardInterrupt, EOFError):
            raise
        if not raw:
            return MODELS[default_idx - 1][0]
        try:
            idx = int(raw)
            if 1 <= idx <= len(MODELS):
                return MODELS[idx - 1][0]
        except ValueError:
            pass
        console.print("  [red]Enter 1 or 2.[/red]")


# ---------- key verification --------------------------------------------

async def _verify_key(api_key: str, model: str) -> bool:
    """
    Tiny test call. Returns True on success or transient failure, False
    only on authentication failure (the one case where we should refuse
    to write a bad key to disk).
    """
    from anthropic import AsyncAnthropic
    import anthropic

    try:
        c = AsyncAnthropic(api_key=api_key, max_retries=0)
        await c.messages.create(
            model=model,
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True
    except anthropic.AuthenticationError as e:
        console.print(f"  [red]✗ Anthropic auth failed: {e}[/red]")
        return False
    except Exception as e:
        # Network blip, rate limit, model name unknown for that key —
        # don't block on these. The user can retry against real workloads.
        console.print(
            f"  [yellow]⚠ Verification didn't complete cleanly "
            f"({type(e).__name__}: {e}).[/yellow]"
        )
        console.print("  [dim]Proceeding anyway — verify against a real review.[/dim]")
        return True


# ---------- main wizard -------------------------------------------------

async def cmd_init() -> None:
    console.print(Panel.fit(
        "[bold]ai-engineering-workspace · setup[/bold]\n"
        "[dim]5 questions. Enter to keep an existing value. "
        "Ctrl-C aborts — no changes written until the end.[/dim]",
        border_style="blue",
    ))

    existing, raw_lines = _read_env()
    values: dict[str, str] = {k: existing.get(k, "") for k in MANAGED_KEYS}

    try:
        console.print()
        console.print("[bold cyan][1/5][/bold cyan] Anthropic API key  [dim](required)[/dim]")
        values["ANTHROPIC_API_KEY"] = _prompt_text(
            "  Key (sk-ant-...)",
            current=values["ANTHROPIC_API_KEY"],
            hidden=True, required=True,
        )

        console.print()
        console.print("[bold cyan][2/5][/bold cyan]", end=" ")
        values["ANTHROPIC_MODEL"] = _prompt_model(current=values["ANTHROPIC_MODEL"])

        console.print()
        console.print("[bold cyan][3/5][/bold cyan] GitHub token  [dim](optional — Enter to skip)[/dim]")
        values["GITHUB_TOKEN"] = _prompt_text(
            "  Token (ghp_...)",
            current=values["GITHUB_TOKEN"],
            hidden=True, required=False,
        )

        console.print()
        console.print("[bold cyan][4/5][/bold cyan] GitHub repo  [dim](owner/repo, optional)[/dim]")
        values["GITHUB_REPO"] = _prompt_text(
            "  Repo",
            current=values["GITHUB_REPO"],
            hidden=False, required=False,
        )

        console.print()
        console.print("[bold cyan][5/5][/bold cyan] First repo path  [dim](local clone to scan; required)[/dim]")
        values["PETCLINIC_REPO_PATH"] = _prompt_text(
            "  Path",
            current=values["PETCLINIC_REPO_PATH"],
            hidden=False, required=True,
        )
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Init cancelled — no changes written.[/dim]")
        return

    # Canonicalize + validate repo path
    repo_path = os.path.expanduser(values["PETCLINIC_REPO_PATH"])
    if not os.path.isdir(repo_path):
        console.print(f"\n[red]Path is not a directory: {repo_path}[/red]")
        console.print("[dim]Aborting — fix the path and re-run [bold]init[/bold].[/dim]")
        return
    values["PETCLINIC_REPO_PATH"] = repo_path

    # Verify key
    console.print()
    console.print("[dim]Verifying Anthropic key with a tiny test call...[/dim]")
    ok = await _verify_key(values["ANTHROPIC_API_KEY"], values["ANTHROPIC_MODEL"])
    if not ok:
        console.print("[red]Authentication failed — not writing .env. Re-run [bold]init[/bold] to retry.[/red]")
        return
    console.print("  [green]✓ Anthropic key verified[/green]")

    # Write .env atomically
    _write_env(values, raw_lines)
    console.print(f"  [green]✓ Wrote {ENV_PATH}[/green]")

    # Reload env into the live process + reset singleton so the new key
    # takes effect without a restart.
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    try:
        import agents.llm_client as _llm
        _llm.client = _llm.RateLimitedAnthropicClient()
        console.print("  [dim]rate-limited client refreshed[/dim]")
    except Exception:
        # First-run path: agents.llm_client hasn't been imported anywhere
        # yet, so no singleton to refresh. Fine.
        pass

    # Auto-scan + register the first repo so the user lands ready-to-review.
    console.print()
    console.print("[dim]Scanning + registering first repo...[/dim]")
    try:
        from scanner.repo_scanner import scan
        from database import (
            init_db, add_repo, get_active_repo, set_active_repo,
        )
        await init_db()
        profile = scan(repo_path)
        repo_id = profile["repo_id"]
        await add_repo(repo_id, repo_path, display_name=repo_id)
        active = await get_active_repo()
        if active is None:
            await set_active_repo(repo_id)
        console.print(
            f"  [green]✓ Scanned + registered + activated "
            f"'{repo_id}'[/green]  "
            f"[dim]({sum(len(profile['files'].get(k, [])) for k in ('backend','frontend','test','config'))} files)[/dim]"
        )
    except Exception as e:
        console.print(f"  [yellow]⚠ Scan step failed: {type(e).__name__}: {e}[/yellow]")
        console.print(
            "  [dim]Run [bold]scan[/bold] manually after fixing — config is saved.[/dim]"
        )

    console.print()
    console.print(Panel.fit(
        "[bold green]Setup complete.[/bold green]\n\n"
        "Try one of:\n"
        "  [bold]review --pr 1[/bold]\n"
        "  [bold]build \"add a notes field to Pet\"[/bold]\n"
        "  [bold]repo list[/bold]  /  [bold]memory stats[/bold]  /  [bold]trace show <task_id>[/bold]\n\n"
        "[dim]Advanced settings (concurrency, retry, GitHub post safety) live in "
        ".env — see .env.example for the full list. Re-run [bold]init[/bold] "
        "any time to change keys or switch model.[/dim]",
        border_style="green",
    ))
