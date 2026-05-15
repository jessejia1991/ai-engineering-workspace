"""
`trace show <trace_id>` and `trace replay <observation_id>` — the human-facing
surface of the observations table.

Design choices borrowed from the May-2026 industry survey (see PROGRESS.md
§16.6 and the design doc):
  - Langfuse-style data model: a single `observations` table with a `type`
    discriminator and `parent_observation_id` for tree shape. Rendering
    walks the rows once and builds the tree client-side here.
  - Phoenix / LangSmith / Braintrust playground replay model: replay is
    restricted to one LLM generation at a time. We do NOT attempt
    deterministic agent-run replay — non-idempotent tools and downstream
    fan-out make that a separate research problem.
  - New replayed generation is linked via `replayed_from_id`, surfaced in
    the tree as an indented child of the original.
"""

import json
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree
from rich.table import Table
from rich.text import Text
from rich import box

from database import (
    init_db,
    get_observations_by_trace,
    get_observation,
)
from agents.llm_client import client as llm_client, set_trace_context, reset_trace_context


console = Console()


# ---------- trace show ---------------------------------------------------

def _fmt_tokens(n: Optional[int]) -> str:
    if n is None:
        return "-"
    return f"{n:,}"


def _fmt_latency(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    if ms >= 10_000:
        return f"{ms/1000:.1f}s"
    return f"{ms}ms"


def _label_for(obs: dict) -> Text:
    """One-line label for an observation node in the tree."""
    typ    = obs.get("type", "?")
    agent  = obs.get("agent_name") or "?"
    model  = obs.get("model") or "-"
    in_t   = _fmt_tokens(obs.get("input_tokens"))
    out_t  = _fmt_tokens(obs.get("output_tokens"))
    lat    = _fmt_latency(obs.get("latency_ms"))
    finish = obs.get("finish_reason") or ""
    obs_id = obs.get("id", "")

    if typ == "generation":
        # Color by finish_reason — errors red, normal green-ish.
        if finish == "error":
            color = "red"
        elif finish in ("max_tokens",):
            color = "yellow"
        else:
            color = "green"

        replayed = obs.get("replayed_from_id")
        replayed_marker = " [magenta](replay)[/magenta]" if replayed else ""

        return Text.from_markup(
            f"[bold {color}]{agent}[/bold {color}]  "
            f"[dim]{model}[/dim]  "
            f"in={in_t} out={out_t}  {lat}  "
            f"[dim]finish={finish}[/dim]"
            f"{replayed_marker}  "
            f"[dim]{obs_id}[/dim]"
        )

    # span / event / tool_call fallthrough
    return Text.from_markup(
        f"[cyan]{typ}[/cyan] [bold]{agent}[/bold]  [dim]{obs_id}[/dim]"
    )


def _build_forest(observations: list[dict]) -> list[dict]:
    """
    Group observations into a forest by parent_observation_id.
    Returns a list of root dicts: {"obs": <row>, "children": [...]}.
    Observations whose parent is not in this trace become roots
    (defensive: shouldn't happen in practice, but tree must be total).
    """
    by_id: dict[str, dict] = {}
    for o in observations:
        by_id[o["id"]] = {"obs": o, "children": []}

    roots: list[dict] = []
    for o in observations:
        pid = o.get("parent_observation_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(by_id[o["id"]])
        else:
            roots.append(by_id[o["id"]])

    return roots


def _attach_to_tree(parent_node, forest_node: dict, show_prompts: bool):
    obs = forest_node["obs"]
    branch = parent_node.add(_label_for(obs))

    if show_prompts and obs.get("type") == "generation":
        _attach_prompt_panel(branch, obs)

    for child in forest_node["children"]:
        _attach_to_tree(branch, child, show_prompts)


def _attach_prompt_panel(node, obs: dict) -> None:
    """Expand messages + response text under a generation node when
    --prompt is passed. Truncated to ~1500 chars each so the tree stays
    scannable; for full content use `trace replay --show`."""

    msgs_raw = obs.get("messages_json")
    if msgs_raw:
        try:
            req = json.loads(msgs_raw)
        except Exception:
            req = None
        if isinstance(req, dict):
            msgs = req.get("messages", []) or []
            sys = req.get("system")
            if sys:
                node.add(
                    Text.from_markup(
                        f"[dim cyan]system[/dim cyan]: {_trunc(str(sys), 1500)}"
                    )
                )
            for m in msgs:
                role = m.get("role", "?")
                content = m.get("content", "")
                if isinstance(content, list):
                    # multi-part content (Anthropic format) — stringify
                    content = " ".join(
                        c.get("text", str(c)) if isinstance(c, dict) else str(c)
                        for c in content
                    )
                node.add(
                    Text.from_markup(
                        f"[dim]{role}[/dim]: {_trunc(str(content), 1500)}"
                    )
                )

    resp_raw = obs.get("response_json")
    if resp_raw:
        try:
            resp = json.loads(resp_raw)
        except Exception:
            resp = None
        if isinstance(resp, dict):
            text = _extract_response_text(resp)
            node.add(
                Text.from_markup(
                    f"[dim green]response[/dim green]: {_trunc(text, 1500)}"
                )
            )

    err = obs.get("error_message")
    if err:
        node.add(Text.from_markup(f"[red]error[/red]: {_trunc(err, 800)}"))


def _extract_response_text(resp: dict) -> str:
    """Pull the text content out of an Anthropic Message dump."""
    content = resp.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        if parts:
            return "\n".join(parts)
    return json.dumps(resp)[:1500]


def _trunc(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n] + " …"


async def cmd_trace_show(trace_id: str, show_prompts: bool = False) -> None:
    """`trace show <trace_id> [--prompt]`."""
    await init_db()

    if not trace_id:
        console.print("[red]Usage: trace show <trace_id> [--prompt][/red]")
        return

    rows = await get_observations_by_trace(trace_id)
    if not rows:
        console.print(
            f"[dim]No observations for trace {trace_id}. "
            f"Either the trace_id is wrong, or the run pre-dates the "
            f"observability slice.[/dim]"
        )
        return

    # Header summary
    n_gen = sum(1 for r in rows if r.get("type") == "generation")
    n_err = sum(1 for r in rows
                if r.get("type") == "generation" and r.get("finish_reason") == "error")
    tot_in  = sum((r.get("input_tokens") or 0) for r in rows)
    tot_out = sum((r.get("output_tokens") or 0) for r in rows)
    tot_lat = sum((r.get("latency_ms") or 0) for r in rows
                  if r.get("type") == "generation")

    console.print(Panel.fit(
        f"[bold]trace[/bold] {trace_id}\n"
        f"[dim]{len(rows)} observations · {n_gen} generations "
        f"({n_err} errors) · {tot_in:,} in / {tot_out:,} out tokens · "
        f"wall LLM time {_fmt_latency(tot_lat)}[/dim]",
        border_style="blue",
    ))

    forest = _build_forest(rows)
    tree = Tree(f"[bold]{trace_id}[/bold]")
    for root in forest:
        _attach_to_tree(tree, root, show_prompts)
    console.print(tree)

    if not show_prompts:
        console.print(
            "\n[dim]Pass `--prompt` to expand messages + response inline. "
            "`trace replay <obs_id>` to iterate on one generation.[/dim]"
        )


# ---------- trace replay -------------------------------------------------

async def cmd_trace_replay(observation_id: str) -> None:
    """`trace replay <observation_id>`.

    Loads one generation, prints the captured messages + response, prompts
    the user for a new user-message (inline, multi-line, terminated with a
    line containing only `.`), re-invokes the same model via the wrapper
    with `replayed_from_id` set, and renders the new response.

    Refuses to operate on non-generation rows. Refuses if messages_json
    can't be parsed.
    """
    await init_db()

    if not observation_id:
        console.print("[red]Usage: trace replay <observation_id>[/red]")
        return

    obs = await get_observation(observation_id)
    if not obs:
        console.print(f"[red]Observation {observation_id} not found.[/red]")
        return

    if obs.get("type") != "generation":
        console.print(
            f"[red]Cannot replay observation of type "
            f"'{obs.get('type')}' — replay is generation-only.[/red]"
        )
        return

    try:
        req = json.loads(obs.get("messages_json") or "{}")
    except json.JSONDecodeError:
        console.print("[red]Stored messages_json is not parseable.[/red]")
        return

    msgs = req.get("messages") or []
    if not msgs:
        console.print("[red]No messages stored on this observation.[/red]")
        return

    # Find the last user message (the "frontier" we'll edit).
    last_user_idx: Optional[int] = None
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        console.print("[red]No user message found in stored messages.[/red]")
        return

    # ---- display current state ----
    console.print(Panel.fit(
        f"[bold]replay[/bold]  source obs: {observation_id}\n"
        f"[dim]model: {obs.get('model')} · trace: {obs.get('trace_id')}[/dim]",
        border_style="magenta",
    ))

    current_content = msgs[last_user_idx].get("content", "")
    if isinstance(current_content, list):
        current_content = " ".join(
            c.get("text", str(c)) if isinstance(c, dict) else str(c)
            for c in current_content
        )

    console.print("[bold dim]Current user message (last):[/bold dim]")
    console.print(Panel(_trunc(str(current_content), 4000), border_style="dim"))

    if obs.get("response_json"):
        try:
            resp = json.loads(obs["response_json"])
            console.print("[bold dim]Original response:[/bold dim]")
            console.print(Panel(
                _trunc(_extract_response_text(resp), 4000),
                border_style="dim",
            ))
        except Exception:
            pass

    # ---- collect new prompt ----
    console.print(
        "\n[bold]Enter the replacement user message[/bold] "
        "(end with a line containing only `.`; blank input = rerun unchanged):"
    )

    lines: list[str] = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Replay cancelled.[/dim]")
            return
        if line.strip() == ".":
            break
        lines.append(line)

    new_user_content = "\n".join(lines).strip()
    if not new_user_content:
        # rerun unchanged
        new_user_content = current_content
        console.print("[dim]Empty input — replaying with original prompt.[/dim]")
    else:
        console.print(f"[dim]Replacement is {len(new_user_content)} chars.[/dim]")

    # ---- build new request_kwargs ----
    new_msgs = [dict(m) for m in msgs]
    new_msgs[last_user_idx] = {"role": "user", "content": new_user_content}

    new_kwargs = {k: v for k, v in req.items() if k != "messages"}
    new_kwargs["messages"] = new_msgs

    # ---- replay through the wrapper ----
    # Parent the new observation at the SOURCE obs (not the source's parent),
    # so the tree nests replay-of-replay chains under their lineage. The
    # `replayed_from_id` link is set redundantly for explicit semantics.
    tokens = set_trace_context(
        trace_id=obs.get("trace_id"),
        agent_name=(obs.get("agent_name") or "Replay"),
        parent_observation_id=observation_id,
        replayed_from_id=observation_id,
    )
    try:
        console.print("[dim]Sending…[/dim]")
        try:
            response = await llm_client.messages.create(**new_kwargs)
        except Exception as e:
            console.print(f"[red]Replay call failed: {type(e).__name__}: {e}[/red]")
            return
    finally:
        reset_trace_context(tokens)

    # ---- render new response + diff hint ----
    new_text = ""
    try:
        new_text = response.content[0].text
    except Exception:
        new_text = str(response)

    console.print("\n[bold]New response:[/bold]")
    console.print(Panel(_trunc(new_text, 6000), border_style="green"))

    # Find the new observation row that we just wrote so we can show the id.
    # We look it up by replayed_from_id rather than threading the id out of
    # the wrapper, which keeps the wrapper API clean.
    new_rows = [
        r for r in await get_observations_by_trace(obs["trace_id"])
        if r.get("replayed_from_id") == observation_id
    ]
    if new_rows:
        # Pick the most recent (last created_at).
        new_rows.sort(key=lambda r: r.get("created_at") or "")
        latest = new_rows[-1]
        console.print(
            f"[dim]Wrote new observation {latest['id']} "
            f"(in={_fmt_tokens(latest.get('input_tokens'))} "
            f"out={_fmt_tokens(latest.get('output_tokens'))} "
            f"· {_fmt_latency(latest.get('latency_ms'))} "
            f"· finish={latest.get('finish_reason')}).\n"
            f"Replay again with: trace replay {latest['id']}[/dim]"
        )


# ---------- dispatcher ---------------------------------------------------

async def cmd_trace(args: list[str]) -> None:
    """Top-level `trace <action> [args...]` dispatcher used by cli/main.py."""
    if not args:
        console.print(
            "[red]Usage: trace show <trace_id> [--prompt]  "
            "|  trace replay <observation_id>[/red]"
        )
        return

    action = args[0]
    rest = args[1:]

    if action == "show":
        trace_id = rest[0] if rest else ""
        show_prompts = ("--prompt" in rest) or ("-p" in rest)
        await cmd_trace_show(trace_id, show_prompts=show_prompts)
    elif action == "replay":
        obs_id = rest[0] if rest else ""
        await cmd_trace_replay(obs_id)
    else:
        console.print(f"[red]Unknown trace action: {action}[/red]")
        console.print(
            "[dim]Supported: trace show <trace_id> [--prompt]  "
            "|  trace replay <observation_id>[/dim]"
        )
