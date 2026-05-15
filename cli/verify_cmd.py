"""
`verify {generate,run,list,catalog}` — external e2e testing as the
project's CD-validation surface.

Architecture (locked in 2026-05-15 conversation):
  - We are an EXTERNAL tester. Generated tests are Python + pytest +
    requests. They live in `.ai-workspace/generated-tests/<repo_id>/`,
    not in the target repo's test tree.
  - We don't manage the target system's lifecycle. The user runs the
    server (`mvn spring-boot:run` etc.) in another terminal; we point
    at $VERIFY_TARGET_URL (default http://localhost:8080) or whatever
    `--url` says.
  - Each generated test is registered in the `test_catalog` 5th
    memory layer — semantically searchable, and queryable by
    "which tests cover the APIs the current diff touched".
  - Test failures auto-trigger an analysis LLM call. The result is
    classified as test-bug-* (write a "test-gen-lesson" to
    corrections_memory so future generations don't repeat) vs
    regression (real production bug; surfaced for the human).
"""

from __future__ import annotations

import os
import re
import json
import uuid
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from database import init_db, get_active_repo
from scanner.repo_scanner import (
    load_profile, get_diff, get_changed_files,
)
from memory.vector_store import (
    add_test_to_catalog, update_test_run_status,
    list_catalog_entries, query_tests_by_apis, query_tests_by_description,
    add_correction, query_relevant_memory,
)
from agents.llm_client import client as llm_client, set_trace_context

console = Console()


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
GENERATED_TESTS_ROOT = WORKSPACE_ROOT / ".ai-workspace" / "generated-tests"


# ---------- dispatcher --------------------------------------------------

async def cmd_verify(args: list[str]) -> int:
    """Returns exit code. 0 = success; non-zero = failure (verify run / health-check)."""
    if not args:
        console.print(
            "[red]Usage: verify generate [--diff] [--max N] [--apis ...]  |  "
            "verify run [--diff] [--url URL] [--no-analyze]  |  "
            "verify health-check [--url URL]  |  "
            "verify list  |  verify catalog search \"<query>\"[/red]"
        )
        return 1

    action = args[0]
    rest = args[1:]
    if action == "generate":
        await _verify_generate(rest)
        return 0
    elif action == "run":
        return await _verify_run(rest)
    elif action == "health-check":
        from cli.verify_run import run_verify_health_check
        return await run_verify_health_check(rest)
    elif action == "list":
        await _verify_list(rest)
        return 0
    elif action == "catalog":
        await _verify_catalog(rest)
        return 0
    else:
        console.print(f"[red]Unknown verify action: {action}[/red]")
        return 1


# ---------- shared helpers ----------------------------------------------

async def _active_repo_or_abort() -> dict | None:
    await init_db()
    active = await get_active_repo()
    if not active:
        console.print(
            "[red]No active repo set.[/red] Run [bold]repo use <id>[/bold] first."
        )
        return None
    return active


def _generated_dir(repo_id: str) -> Path:
    d = GENERATED_TESTS_ROOT / repo_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _strip_python_fence(text: str) -> str:
    """LLMs sometimes wrap code in ```python ... ``` fences. Strip."""
    t = text.strip()
    if "```python" in t:
        t = t.split("```python", 1)[1]
        if "```" in t:
            t = t.split("```", 1)[0]
    elif t.startswith("```"):
        t = t.split("```", 1)[1]
        if "```" in t:
            t = t.split("```", 1)[0]
    return t.strip()


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", text.lower()).strip("_")
    return s[:50] or "test"


# ---------- verify generate ---------------------------------------------

GENERATE_PROMPT = """You are generating Python e2e tests that hit a running target system's HTTP API from OUTSIDE.

## Context
The target system is the project below. You are NOT writing JUnit / Jest /
pytest-against-internal-code tests. You are writing Python `pytest` test
files that use `requests` to call the live HTTP endpoints. The user will
run the target system locally (e.g. `mvn spring-boot:run`); your tests
will hit `http://localhost:PORT` (or whatever `$VERIFY_TARGET_URL` is set
to at runtime).

## Project runtime
{runtime_block}

## Target APIs (full extracted list)
{apis_block}

{diff_block}

## Prior lessons from this repo (DO NOT REPEAT THESE MISTAKES)
{lessons_block}

## Existing generated tests (DO NOT REGENERATE these — pick different angles)
{existing_block}

## Your task
Generate up to {max_n} NEW Python test files, each targeting ONE API endpoint
from the "Target APIs" list (prefer APIs touched by the diff if a diff block
is shown above).

For each test:
- Severity bias: prefer **generic, high-signal** checks that don't require
  domain knowledge of payloads:
    * `GET` endpoints: 200 + JSON shape (is it a list? a dict? right keys?)
    * `POST/PUT` endpoints: empty body returns 400 / 415 (validation works)
    * `*/{{id}}` endpoints: 404 for nonexistent id
  These are robust against the LLM-doesn't-know-the-real-payload-schema
  problem and still demonstrate the validation chain works end-to-end.
- Use `os.environ.get("VERIFY_TARGET_URL", "http://localhost:{port}")` as the
  base URL (so the test honors the env var at runtime).
- Use ONLY `requests`, `pytest`, `os`. No project-specific imports.
- Read the prior lessons. If a lesson says "POST /api/foo accepts trailing
  slashes only" — honor it.

## Output format — STRICT JSON
Return ONLY a JSON array. Each element is one test file:

[
  {{
    "test_id":      "test_<short_descriptive_id>",
    "description":  "one sentence describing what flow this verifies",
    "apis_covered": ["GET /api/foo", "GET /api/foo/{{id}}"],
    "filename":     "test_<short_descriptive_id>.py",
    "code":         "<complete python file content as a single string>"
  }}
]

The "code" field MUST be a complete runnable pytest file:
- starts with a comment block of metadata (test_id, covers, generated_at)
- imports os, pytest, requests
- defines BASE_URL = os.environ.get("VERIFY_TARGET_URL", "http://localhost:{port}")
- one or more `def test_*` functions
- assertions include a helpful failure message string

No prose, no markdown, no ```json fences. Just the JSON array.
"""


async def _verify_generate(rest: list[str]) -> None:
    # Arg parsing
    use_diff = True
    max_n = 3
    apis_filter: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--diff":
            use_diff = True; i += 1
        elif a == "--no-diff":
            use_diff = False; i += 1
        elif a == "--max" and i + 1 < len(rest):
            try:
                max_n = max(1, int(rest[i + 1]))
            except ValueError:
                pass
            i += 2
        elif a == "--apis" and i + 1 < len(rest):
            apis_filter = [s.strip() for s in rest[i + 1].split(",") if s.strip()]
            i += 2
        else:
            console.print(f"[red]Unknown flag: {a}[/red]")
            return

    active = await _active_repo_or_abort()
    if not active:
        return
    repo_id = active["id"]

    try:
        profile = load_profile()
    except FileNotFoundError:
        console.print("[red]Repo not scanned yet. Run: scan[/red]")
        return

    runtime = profile.get("runtime") or {}
    apis    = profile.get("apis") or []
    if not apis:
        console.print(
            "[yellow]No APIs detected in this repo.[/yellow] "
            "[dim]The verify pipeline needs at least one endpoint — "
            "check scanner/api_extractor.py against your framework.[/dim]"
        )
        return

    # Diff scoping
    diff_text = ""
    changed_files: list[str] = []
    if use_diff:
        try:
            diff_text = get_diff(profile["repo_path"])
            changed_files = get_changed_files(profile["repo_path"])
        except Exception:
            pass

    # Filter apis if --apis or --diff is set
    target_apis = apis
    if apis_filter:
        target_apis = [a for a in apis if f"{a['method']} {a['path']}" in apis_filter]
    elif use_diff and changed_files:
        # Heuristic: an API is "in diff" if its handler's class name appears
        # in any changed filename, OR its file (for direct hits) is changed.
        chf_lower = " ".join(changed_files).lower()
        def _api_in_diff(a: dict) -> bool:
            handler = (a.get("handler") or "").lower()
            file_   = (a.get("file") or "").lower()
            handler_class = handler.split(".")[0] if "." in handler else handler
            return (
                (file_ and any(file_ in cf.lower() for cf in changed_files))
                or (handler_class and handler_class in chf_lower)
            )
        diff_apis = [a for a in apis if _api_in_diff(a)]
        if diff_apis:
            target_apis = diff_apis
        # If diff matches nothing, fall through to all apis (avoids "0 tests
        # generated because the heuristic was too strict").

    # Dedup against existing catalog. For each target API, check if any
    # catalog entry already covers it. If yes, drop from the target list —
    # we don't pay tokens to regenerate what's already there.
    target_keys = [f"{a['method']} {a['path']}" for a in target_apis]
    if target_keys:
        already_covered = query_tests_by_apis(target_keys, repo_id)
        covered_set: set[str] = set()
        for hit in already_covered:
            covered_raw = (hit.get("metadata") or {}).get("apis_covered", "")
            covered_set.update(c.strip() for c in covered_raw.split(",") if c.strip())
        if covered_set:
            kept_apis = []
            dropped = []
            for a, key in zip(target_apis, target_keys):
                if key in covered_set:
                    dropped.append(key)
                else:
                    kept_apis.append(a)
            target_apis = kept_apis
            if dropped:
                console.print(
                    f"  [dim]Skipping {len(dropped)} API(s) already covered by catalog: "
                    f"{', '.join(dropped[:5])}{'...' if len(dropped) > 5 else ''}[/dim]"
                )

    if not target_apis:
        console.print(
            "[green]Nothing to generate — every targeted API already has a "
            "test in the catalog.[/green] "
            "[dim](Use `verify list` to inspect; delete entries to regenerate.)[/dim]"
        )
        return

    # Pull lessons (corrections of type="test-gen-lesson")
    lessons_block = await _format_lessons(repo_id)
    existing_block = _format_existing(repo_id)
    runtime_block = (
        f"frameworks: {', '.join(runtime.get('frameworks', []))}\n"
        f"port: {runtime.get('port', 8080)}\n"
        f"run_commands: {', '.join(runtime.get('run_commands', []))}"
    )
    apis_block = "\n".join(
        f"  - {a['method']:6s} {a['path']:50s} ({a.get('handler', '')})"
        for a in target_apis[:30]
    )
    diff_block = ""
    if use_diff and diff_text:
        diff_block = f"## Current diff (focus generation on APIs touched by this)\n```diff\n{diff_text[:4000]}\n```\n"

    prompt = GENERATE_PROMPT.format(
        runtime_block=runtime_block,
        apis_block=apis_block,
        diff_block=diff_block,
        lessons_block=lessons_block,
        existing_block=existing_block,
        max_n=max_n,
        port=runtime.get("port", 8080),
    )

    console.print(Panel.fit(
        f"[bold]verify generate[/bold]  "
        f"repo={repo_id} · scope={'diff' if use_diff else 'all'} · "
        f"target_apis={len(target_apis)} · max={max_n}",
        border_style="blue",
    ))

    trace_id = f"verify-gen-{uuid.uuid4().hex[:8]}"
    set_trace_context(trace_id=trace_id, agent_name="TestVerifier")
    console.print(f"[dim]trace_id: {trace_id}[/dim]")

    try:
        with console.status("[bold blue]Generating tests...[/bold blue]"):
            response = await llm_client.messages.create(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
        raw = response.content[0].text
    except Exception as e:
        console.print(f"[red]Generation failed: {type(e).__name__}: {e}[/red]")
        return

    # Parse JSON
    tests = _parse_generate_response(raw)
    if not tests:
        console.print(
            "[red]Could not parse LLM output as test array.[/red] "
            "[dim]Re-run with smaller scope or check trace.[/dim]"
        )
        return

    out_dir = _generated_dir(repo_id)
    written: list[dict] = []
    for t in tests[:max_n]:
        test_id = t.get("test_id") or f"test_{_slugify(t.get('description', 'untitled'))}"
        filename = t.get("filename") or f"{test_id}.py"
        if not filename.endswith(".py"):
            filename += ".py"
        code = t.get("code", "").strip()
        if not code:
            continue
        out_path = out_dir / filename
        out_path.write_text(code)
        try:
            add_test_to_catalog(
                test_id=test_id,
                description=t.get("description", filename),
                apis_covered=t.get("apis_covered") or [],
                file_path=str(out_path.relative_to(WORKSPACE_ROOT)),
                repo_id=repo_id,
            )
        except Exception:
            # If add_to_catalog fails (e.g., duplicate id), retry with a
            # suffixed id so we don't lose the file
            try:
                add_test_to_catalog(
                    test_id=test_id + "_" + uuid.uuid4().hex[:4],
                    description=t.get("description", filename),
                    apis_covered=t.get("apis_covered") or [],
                    file_path=str(out_path.relative_to(WORKSPACE_ROOT)),
                    repo_id=repo_id,
                )
            except Exception:
                pass
        written.append({"test_id": test_id, "filename": filename,
                        "description": t.get("description", ""),
                        "apis_covered": t.get("apis_covered") or []})

    if not written:
        console.print("[red]No tests produced. Check the raw LLM output via `trace show`.[/red]")
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True,
                  title=f"Generated {len(written)} test(s) → {out_dir.relative_to(WORKSPACE_ROOT)}")
    table.add_column("ID", style="cyan bold")
    table.add_column("Description", style="white")
    table.add_column("APIs covered", style="dim")
    for w in written:
        table.add_row(
            w["test_id"],
            w["description"][:80],
            ", ".join(w["apis_covered"])[:80],
        )
    console.print(table)
    console.print(
        f"\n[dim]Next: [bold]verify run --diff[/bold] (after starting "
        f"the target at port {runtime.get('port', 8080)})[/dim]"
    )


def _parse_generate_response(raw: str) -> list[dict]:
    """LLM is asked for a JSON array; tolerate light fence-wrap noise."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 1)[1]
        if text.lower().startswith("json"):
            text = text[4:]
        if "```" in text:
            text = text.split("```", 1)[0]
    try:
        data = json.loads(text)
    except Exception:
        # Last resort: hunt for `[...]` span
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except Exception:
                return []
        else:
            return []
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, dict)]


async def _format_lessons(repo_id: str) -> str:
    """Pull `corrections_memory` entries with type='test-gen-lesson'.
    We reuse the standard query API; entries with no test-gen-lesson type
    will just rank low. Filter by metadata after retrieval."""
    mem = query_relevant_memory(
        agent_name="TestVerifier",
        query_text="test generation prior failure analysis",
        repo_id=repo_id,
        top_k_corrections=10,
        top_k_findings=0,
    )
    lessons = [
        c for c in (mem.get("relevant_corrections") or [])
        if (c.get("metadata") or {}).get("type") == "test-gen-lesson"
    ]
    if not lessons:
        return "  (none yet — this is the first generation cycle)"
    lines = []
    for c in lessons[:8]:
        lines.append(f"  - {c['document'][:200]}")
    return "\n".join(lines)


def _format_existing(repo_id: str) -> str:
    entries = list_catalog_entries(repo_id=repo_id)
    if not entries:
        return "  (none)"
    lines = []
    for e in entries[:15]:
        m = e.get("metadata") or {}
        lines.append(
            f"  - {e['id']}: covers {m.get('apis_covered', '')} "
            f"(last_status={m.get('last_status', 'UNKNOWN')})"
        )
    return "\n".join(lines)


# ---------- verify run, list, catalog: placeholders for V4 + V5 ----------

async def _verify_run(rest: list[str]) -> int:
    from cli.verify_run import run_verify_run
    return await run_verify_run(rest)


async def _verify_list(rest: list[str]) -> None:
    # Implemented in V5
    from cli.verify_run import run_verify_list
    await run_verify_list(rest)


async def _verify_catalog(rest: list[str]) -> None:
    # Implemented in V5
    from cli.verify_run import run_verify_catalog
    await run_verify_catalog(rest)
