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
    add_test_to_catalog, update_test_run_status, delete_test_from_catalog,
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
            "verify list  |  verify impact <file>  |  "
            "verify catalog search \"<query>\" | clear[/red]"
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
    elif action == "impact":
        from cli.verify_run import run_verify_impact
        await run_verify_impact(rest)
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


def _resource_name(source_file: str) -> str:
    """Derive a short resource slug from a controller path, used for the
    one-file-per-controller naming: OwnerRestController.java -> 'owner'."""
    stem = Path(source_file).stem
    for suffix in ("RestController", "Controller"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return _slugify(stem) or "api"


# ---------- verify generate ---------------------------------------------

GENERATE_PROMPT = """You are generating Python e2e tests that hit a running target system's HTTP API from OUTSIDE.

## Context
You are NOT writing JUnit / Jest / pytest-against-internal-code tests. You
are writing ONE Python `pytest` file that uses `requests` to call the live
HTTP endpoints of a running service. The user runs the target locally
(e.g. `mvn spring-boot:run`); your tests hit `$VERIFY_TARGET_URL`.

## Project runtime
{runtime_block}

## Controller under test: {resource}
Every endpoint below is implemented by ONE controller (`{source_file}`).
Generate a SINGLE test file covering them — one file per controller keeps
coverage non-overlapping.

{apis_block}

{diff_block}
## Prior lessons from this repo (DO NOT REPEAT THESE MISTAKES)
{lessons_block}

## Your task
Generate ONE Python test file for the controller above. Cover as many of
its endpoints as you reasonably can, biased toward **generic, high-signal**
checks that don't need domain knowledge of payload schemas:
  * `GET` endpoints: 200 + JSON shape (list? dict? expected keys?)
  * `POST/PUT` endpoints: empty body returns 400 / 415 (validation works)
  * `*/{{id}}` endpoints: 404 for a nonexistent id
- Use `os.environ.get("VERIFY_TARGET_URL", "http://localhost:{port}")` as the
  base URL (so the test honors the env var at runtime).
- Use ONLY `requests`, `pytest`, `os`. No project-specific imports.
- Honor the prior lessons above.

## Output format — STRICT JSON
Return ONLY a JSON array with EXACTLY ONE element:

[
  {{
    "test_id":      "test_{resource}",
    "description":  "one sentence describing what this file verifies",
    "code":         "<complete python file content as a single string>"
  }}
]

The "code" field MUST be a complete runnable pytest file:
- starts with a comment block of metadata
- imports os, pytest, requests
- defines BASE_URL = os.environ.get("VERIFY_TARGET_URL", "http://localhost:{port}")
- one or more `def test_*` functions, each assert with a helpful message

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

    # Registration filter — skip endpoints with no handler in the codebase
    # (declared in openapi.yml but never implemented). No point generating a
    # test that can only ever 404/501.
    registered = [a for a in apis if a.get("registered", True)]
    unregistered = [a for a in apis if not a.get("registered", True)]
    if unregistered:
        names = ", ".join(f"{a['method']} {a['path']}" for a in unregistered[:5])
        console.print(
            f"  [dim]Skipping {len(unregistered)} unregistered endpoint(s) "
            f"(in the spec, no handler in code): {names}"
            f"{'...' if len(unregistered) > 5 else ''}[/dim]"
        )

    # Scope to --apis / --diff
    target_apis = registered
    if apis_filter:
        target_apis = [a for a in registered
                       if f"{a['method']} {a['path']}" in apis_filter]
    elif use_diff and changed_files:
        chf_lower = [cf.lower() for cf in changed_files]
        def _api_in_diff(a: dict) -> bool:
            # Primary signal: the controller implementing this endpoint is
            # among the changed files. Falls back to the declaring file.
            sf = (a.get("source_file") or "").lower()
            if sf and any(sf in cf or cf in sf for cf in chf_lower):
                return True
            file_ = (a.get("file") or "").lower()
            return bool(file_ and any(file_ in cf for cf in chf_lower))
        diff_apis = [a for a in registered if _api_in_diff(a)]
        if diff_apis:
            target_apis = diff_apis
        # else: fall through to all registered APIs

    # Group endpoints by the controller that implements them — one test
    # file per controller keeps coverage non-overlapping and trivially
    # mappable back to source.
    groups: dict[str, list] = {}
    for a in target_apis:
        sf = a.get("source_file") or a.get("file") or "(unknown)"
        groups.setdefault(sf, []).append(a)

    # Controller-level dedup: a controller already represented by a catalog
    # entry is skipped wholesale. This is what stops duplicate-coverage test
    # files (e.g. vets tested by two files) from accumulating across runs.
    covered: set[str] = set()
    for e in list_catalog_entries(repo_id=repo_id):
        raw = (e.get("metadata") or {}).get("source_files", "")
        covered.update(s.strip() for s in raw.split(",") if s.strip())
    pending = [(sf, eps) for sf, eps in groups.items() if sf not in covered]
    n_skipped = len(groups) - len(pending)
    if n_skipped:
        console.print(
            f"  [dim]Skipping {n_skipped} controller(s) already covered by "
            f"the catalog.[/dim]"
        )

    if not pending:
        console.print(
            "[green]Nothing to generate — every targeted controller already "
            "has a test file in the catalog.[/green]  "
            "[dim](`verify catalog clear` to wipe and regenerate.)[/dim]"
        )
        return

    pending = pending[:max_n]   # cap on controllers == cap on LLM calls

    lessons_block = await _format_lessons(repo_id)
    runtime_block = (
        f"frameworks: {', '.join(runtime.get('frameworks', []))}\n"
        f"port: {runtime.get('port', 8080)}\n"
        f"run_commands: {', '.join(runtime.get('run_commands', []))}"
    )
    port = runtime.get("port", 8080)

    console.print(Panel.fit(
        f"[bold]verify generate[/bold]  repo={repo_id} · "
        f"scope={'diff' if use_diff else 'all'} · "
        f"controllers={len(pending)} (cap {max_n})",
        border_style="blue",
    ))

    out_dir = _generated_dir(repo_id)
    written: list[dict] = []

    # One LLM call per controller → exactly one test file per controller.
    for source_file, eps in pending:
        resource = _resource_name(source_file)
        apis_block = "\n".join(f"  - {a['method']:6s} {a['path']}" for a in eps)
        diff_block = ""
        if use_diff and diff_text:
            diff_block = (
                "## Current diff (focus on what changed)\n"
                f"```diff\n{diff_text[:3000]}\n```\n"
            )
        prompt = GENERATE_PROMPT.format(
            runtime_block=runtime_block,
            resource=resource,
            source_file=source_file,
            apis_block=apis_block,
            diff_block=diff_block,
            lessons_block=lessons_block,
            port=port,
        )
        trace_id = f"verify-gen-{uuid.uuid4().hex[:8]}"
        set_trace_context(trace_id=trace_id, agent_name="TestVerifier")
        try:
            with console.status(f"[bold blue]Generating tests for {resource}...[/bold blue]"):
                response = await llm_client.messages.create(
                    model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                    max_tokens=8000,
                    messages=[{"role": "user", "content": prompt}],
                )
            raw = response.content[0].text
        except Exception as e:
            console.print(f"  [red]✗ {resource}: generation failed: {type(e).__name__}: {e}[/red]")
            continue

        parsed = _parse_generate_response(raw)
        code = (parsed[0].get("code") or "").strip() if parsed else ""
        if not code:
            console.print(f"  [red]✗ {resource}: no usable code in LLM output[/red]")
            continue

        test_id  = f"test_{resource}"
        filename = f"test_{resource}.py"
        out_path = out_dir / filename
        out_path.write_text(code)

        apis_covered = [f"{a['method']} {a['path']}" for a in eps]
        description  = parsed[0].get("description", f"e2e tests for {resource}")
        delete_test_from_catalog(test_id)   # replace any stale entry
        try:
            add_test_to_catalog(
                test_id=test_id,
                description=description,
                apis_covered=apis_covered,
                file_path=str(out_path.relative_to(WORKSPACE_ROOT)),
                repo_id=repo_id,
                source_files=[source_file],
            )
        except Exception:
            pass
        written.append({
            "filename": filename, "source_file": source_file,
            "apis_covered": apis_covered, "trace_id": trace_id,
        })
        console.print(
            f"  [green]✓ {resource}[/green] → {filename}  "
            f"[dim]({len(eps)} endpoint(s), trace {trace_id})[/dim]"
        )

    if not written:
        console.print("[red]No tests produced. Check the raw LLM output via `trace show`.[/red]")
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True,
                  title=f"Generated {len(written)} test file(s) → {out_dir.relative_to(WORKSPACE_ROOT)}")
    table.add_column("File", style="cyan bold")
    table.add_column("Controller", style="dim")
    table.add_column("Endpoints", style="white", justify="right")
    for w in written:
        table.add_row(w["filename"], Path(w["source_file"]).name,
                      str(len(w["apis_covered"])))
    console.print(table)
    console.print(
        f"\n[dim]Next: [bold]verify run --diff[/bold] (after starting "
        f"the target at port {port})[/dim]"
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
