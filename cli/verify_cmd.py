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
import ast
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
from scanner.api_extractor import entity_name
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
            "verify run [--diff] [--url URL] [--fix] [--no-analyze]  |  "
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
    """One-file-per-controller naming: OwnerRestController.java -> 'owner'.
    Delegates to api_extractor.entity_name so the test-file basename and
    the topology entity name are guaranteed identical."""
    return entity_name(source_file)


# ---------- OpenAPI schema formatting (fed into the generation prompt) ----

def _flatten_schema(node, all_schemas, seen=None):
    """(properties dict, required list) for a schema node — resolves $ref
    and flattens allOf composition (petclinic DTOs are allOf-composed)."""
    if seen is None:
        seen = set()
    if not isinstance(node, dict):
        return {}, []
    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return {}, []
        seen.add(name)
        return _flatten_schema(all_schemas.get(name, {}), all_schemas, seen)
    props, required = {}, []
    for sub in node.get("allOf") or []:
        p, r = _flatten_schema(sub, all_schemas, seen)
        props.update(p)
        required += r
    if isinstance(node.get("properties"), dict):
        props.update(node["properties"])
    if isinstance(node.get("required"), list):
        required += node["required"]
    return props, required


def _prop_type(prop, all_schemas) -> str:
    """Human-readable type for a schema property."""
    if not isinstance(prop, dict):
        return "any"
    ref = prop.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    allof = prop.get("allOf") or []
    if allof:
        return _prop_type(allof[0], all_schemas)
    t = prop.get("type")
    if t == "array":
        return _prop_type(prop.get("items") or {}, all_schemas) + "[]"
    fmt = prop.get("format")
    return f"{t}<{fmt}>" if (t and fmt) else (t or "object")


def _format_schemas(names: list[str], all_schemas: dict) -> str:
    """Render the named schemas plus everything they transitively reference
    as a compact field/type/required block for the generation prompt."""
    if not all_schemas:
        return "  (no OpenAPI schema available — infer payloads conservatively)"
    wanted: list[str] = []
    seen: set[str] = set()
    queue = [n for n in names if n]
    while queue:
        n = queue.pop(0)
        if n in seen or n not in all_schemas:
            continue
        seen.add(n)
        wanted.append(n)
        props, _ = _flatten_schema(all_schemas[n], all_schemas)
        for p in props.values():
            base = _prop_type(p, all_schemas).rstrip("[]")
            if base in all_schemas and base not in seen:
                queue.append(base)
    if not wanted:
        return "  (no schema referenced by these endpoints)"
    lines: list[str] = []
    for n in wanted:
        props, required = _flatten_schema(all_schemas[n], all_schemas)
        req = set(required)
        lines.append(f"{n} {{")
        for fname, fprop in props.items():
            tag = " (required)" if fname in req else ""
            if isinstance(fprop, dict) and fprop.get("readOnly"):
                tag += " (read-only — omit from request bodies)"
            lines.append(f"  {fname}: {_prop_type(fprop, all_schemas)}{tag}")
        lines.append("}")
    return "\n".join(lines)


# ---------- verify generate ---------------------------------------------

GENERATE_PROMPT = """You are generating Python e2e tests that hit a running service's HTTP API from OUTSIDE.

## Context
You write ONE Python `pytest` file that uses `requests` to call live HTTP
endpoints of a running service. The whole generated suite runs as ONE
pytest session, in dependency order, sharing state — see the orchestration
contract below.

## Project runtime
{runtime_block}

## Controller under test: {resource}  (entity: `{entity}`)
Every endpoint below is implemented by ONE controller (`{source_file}`).
{depends_block}

{apis_block}

## Data shapes - request / response schemas (use these EXACT field names)
{schema_block}

{diff_block}## Prior lessons from this repo (DO NOT REPEAT THESE MISTAKES)
{lessons_block}

## Suite orchestration contract (a conftest.py already provides these fixtures)
The target database starts EMPTY. The suite handles that by sharing created
resources across files via three session fixtures - request them as test
arguments:

- `base_url`         - the API base URL string; build every request URL from it.
- `ctx`              - a shared dict; upstream entities' tests write resource
                       ids into it, your tests read what they need.
- `register_cleanup` - call `register_cleanup(fn)` with a zero-arg callable;
                       all cleanups run in reverse order at session end.

Rules - follow them EXACTLY so the files interlock:
- This file owns the `{entity}` entity. In ONE test, create a `{entity}`
  with a valid payload and store its id: `ctx["{entity}_id"] = <id>`.
  Immediately register its teardown:
  `register_cleanup(lambda: requests.delete(f"{{base_url}}/.../{{the_id}}"))`.
- The resource whose id you put in `ctx` is SHARED with downstream files; it
  MUST stay alive until the session ends. Therefore:
    * NEVER delete the ctx-shared resource in a normal test.
    * NEVER set a `ctx[...]` id back to None, and never pop it.
    * Its deletion happens ONLY through `register_cleanup` — the conftest
      runs every cleanup in reverse order at session end.
- To TEST a DELETE (or any destructive) endpoint, create a SEPARATE
  throwaway resource inside that test and operate on THAT one — never on
  the ctx-shared resource.
- For a resource a DEPENDENCY owns, READ it from `ctx` by the key
  `"<entity>_id"` (e.g. `ctx.get("owner_id")`). If it is missing, the upstream
  test did not succeed - call `pytest.skip("dependency <entity> not satisfied")`.
  NEVER hard-fail on a missing dependency.
- If `ctx` already holds an id you need, REUSE it - never create a duplicate.

## Your task
Generate ONE Python test file for the controller above. Cover its endpoints
with two kinds of checks:
1. Validation / negative checks (need no data): empty body -> 400/415,
   nonexistent id -> 404, GET collection -> 200 + JSON shape.
2. Real flow checks: create a `{entity}` with a VALID payload, then
   read / update / delete it - taking dependency ids from `ctx`.

Payload & assertion rules (this is where generated tests usually fail):
- Build request bodies from the schemas above - EXACT field names, correct
  nesting, correct types. Never invent a field. Omit read-only fields.
- A nested-object field is a JSON object, not an id - a field typed
  `PetType` is {{"id": 1, "name": "cat"}}, NOT 1 and NOT {{"typeId": 1}}.
- Assert status codes as a RANGE, NEVER an exact 2xx: write
  `assert 200 <= r.status_code < 300`, not `== 201` (servers differ on
  200 vs 201). For expected errors assert `400 <= r.status_code < 500`.
- A 2xx response MAY have an empty body (e.g. 204 No Content on PUT or
  DELETE). NEVER call `r.json()` unconditionally — guard it:
  `body = r.json() if r.content else None`, then assert on `body` only
  when it is not None.
- Use ONLY `requests`, `pytest`, `os`. Take `base_url`, `ctx`,
  `register_cleanup` as fixture arguments wherever needed.
- Honor the prior lessons above.

## Output format - STRICT JSON
Return ONLY a JSON array with EXACTLY ONE element:

[
  {{
    "test_id":      "test_{resource}",
    "description":  "one sentence describing what this file verifies",
    "code":         "<complete python file content as a single string>"
  }}
]

The "code" field MUST be a complete runnable pytest file: imports os,
pytest, requests; uses the `base_url` / `ctx` / `register_cleanup` fixtures
from conftest (do NOT redefine them); one or more `def test_*` functions,
each assert with a helpful message.

No prose, no markdown, no ```json fences. Just the JSON array.
"""


# conftest.py the tool writes alongside the generated tests — the shared
# orchestration layer. Fixed (not LLM-authored). `__BASE_URL__` is the only
# substitution; everything else is verbatim.
CONFTEST_TEMPLATE = '''# conftest.py — generated by ai-engineering-workspace (verify slice).
# Shared orchestration for the dependency-ordered e2e suite. Do NOT edit;
# `verify generate` regenerates it.
import os
import pytest

BASE_URL = os.environ.get("VERIFY_TARGET_URL", "__BASE_URL__").rstrip("/")


@pytest.fixture(scope="session")
def base_url():
    """The running service's API base URL."""
    return BASE_URL


@pytest.fixture(scope="session")
def ctx():
    """Context shared across the whole suite. An upstream test that creates
    a resource writes its id here (ctx["owner_id"] = ...); a downstream test
    reads it and skips when absent (its dependency did not succeed)."""
    return {}


@pytest.fixture(scope="session")
def register_cleanup():
    """Register a zero-arg cleanup callable. Every cleanup runs in LIFO
    order at session end — reverse of creation, i.e. reverse topology
    (a pet is deleted before its owner)."""
    _cleanups = []
    yield _cleanups.append
    for _fn in reversed(_cleanups):
        try:
            _fn()
        except Exception as _exc:
            print("[verify-cleanup] " + repr(_exc))
'''


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

    # Generate in dependency (topological) order — owner before pet before
    # visit — so an upstream entity's test runs (and fills ctx) first.
    topo = profile.get("entity_topology") or {}
    topo_order = topo.get("order") or []

    def _topo_rank(item):
        ent = entity_name(item[0])
        return topo_order.index(ent) if ent in topo_order else len(topo_order)

    pending.sort(key=_topo_rank)
    pending = pending[:max_n]   # cap on controllers == cap on LLM calls

    lessons_block = await _format_lessons(repo_id)
    runtime_block = (
        f"frameworks: {', '.join(runtime.get('frameworks', []))}\n"
        f"port: {runtime.get('port', 8080)}\n"
        f"run_commands: {', '.join(runtime.get('run_commands', []))}"
    )
    port = runtime.get("port", 8080)
    # Default base URL baked into the generated tests (overridable at run
    # time by $VERIFY_TARGET_URL) — from the OpenAPI servers block when present.
    from cli.verify_run import resolve_base_url
    base_url = resolve_base_url(profile)

    console.print(Panel.fit(
        f"[bold]verify generate[/bold]  repo={repo_id} · "
        f"scope={'diff' if use_diff else 'all'} · "
        f"controllers={len(pending)} (cap {max_n})",
        border_style="blue",
    ))

    out_dir = _generated_dir(repo_id)
    # Shared orchestration conftest (ctx + LIFO cleanup) — written every run.
    (out_dir / "conftest.py").write_text(
        CONFTEST_TEMPLATE.replace("__BASE_URL__", base_url)
    )
    written: list[dict] = []

    all_schemas = profile.get("api_schemas") or {}

    # One LLM call per controller → exactly one test file per controller.
    for source_file, eps in pending:
        resource = _resource_name(source_file)
        entity = entity_name(source_file)
        deps = (((topo.get("entities") or {}).get(entity)) or {}).get("depends_on") or []
        if deps:
            depends_block = (
                f"This file owns the `{entity}` entity. It DEPENDS ON: "
                f"{', '.join(deps)} — read each one's id from `ctx`."
            )
        else:
            depends_block = (
                f"This file owns the `{entity}` entity. It has no upstream "
                f"dependencies — it runs first."
            )
        apis_block = "\n".join(f"  - {a['method']:6s} {a['path']}" for a in eps)
        # Feed the exact request/response DTO shapes so payloads aren't guessed.
        schema_names: list[str] = []
        for a in eps:
            for key in ("request_schema", "response_schema"):
                s = a.get(key)
                if s:
                    schema_names.append(s)
        schema_block = _format_schemas(schema_names, all_schemas)
        diff_block = ""
        if use_diff and diff_text:
            diff_block = (
                "## Current diff (focus on what changed)\n"
                f"```diff\n{diff_text[:3000]}\n```\n"
            )
        prompt = GENERATE_PROMPT.format(
            runtime_block=runtime_block,
            resource=resource,
            entity=entity,
            source_file=source_file,
            depends_block=depends_block,
            apis_block=apis_block,
            schema_block=schema_block,
            diff_block=diff_block,
            lessons_block=lessons_block,
        )
        # Generate, then validate the result is real Python before writing.
        # A truncated / syntactically-broken file is the tool's OWN bug — we
        # catch it here and retry once rather than emitting a broken test
        # (ast.parse catches both max_tokens truncation and LLM syntax slips).
        code = ""
        description = f"e2e tests for {resource}"
        for attempt in (1, 2):
            trace_id = f"verify-gen-{uuid.uuid4().hex[:8]}"
            set_trace_context(trace_id=trace_id, agent_name="TestVerifier")
            try:
                with console.status(
                    f"[bold blue]Generating tests for {resource}"
                    f"{' (retry)' if attempt > 1 else ''}...[/bold blue]"
                ):
                    response = await llm_client.messages.create(
                        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                        max_tokens=16000,
                        messages=[{"role": "user", "content": prompt}],
                    )
                raw = response.content[0].text
            except Exception as e:
                console.print(f"  [red]✗ {resource}: generation failed: {type(e).__name__}: {e}[/red]")
                break

            parsed = _parse_generate_response(raw)
            candidate = (parsed[0].get("code") or "").strip() if parsed else ""
            if not candidate:
                console.print(
                    f"  [yellow]⊘ {resource}: LLM returned no usable code "
                    f"(attempt {attempt})[/yellow]"
                )
                continue
            try:
                ast.parse(candidate)
            except SyntaxError as e:
                console.print(
                    f"  [yellow]⊘ {resource}: generated file is not valid Python "
                    f"(attempt {attempt}: {e.msg}, line {e.lineno}) — "
                    f"{'retrying' if attempt == 1 else 'giving up'}[/yellow]"
                )
                continue
            code = candidate
            description = parsed[0].get("description", description)
            break

        if not code:
            console.print(f"  [red]✗ {resource}: skipped — no valid test file produced[/red]")
            continue

        test_id  = f"test_{resource}"
        filename = f"test_{resource}.py"
        out_path = out_dir / filename
        out_path.write_text(code)

        apis_covered = [f"{a['method']} {a['path']}" for a in eps]
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
