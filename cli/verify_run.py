"""
`verify run` + `verify list` + `verify catalog search`.

run flow:
  1. Resolve target URL (--url > $VERIFY_TARGET_URL > detected port default).
  2. Select tests (--diff via catalog query_tests_by_apis; default = all).
  3. subprocess pytest -v --tb=short on the generated-tests dir, env VERIFY_TARGET_URL set.
  4. Parse stdout for PASSED/FAILED lines.
  5. Update test_catalog last_run_at + last_status for each test.
  6. For each FAILED test (unless --no-analyze): LLM call classifies
     test-bug-* / regression / flaky / config. test-bug-* writes a
     correction to corrections_memory (type='test-gen-lesson') so next
     `verify generate` is smarter.
"""

from __future__ import annotations

import os
import re
import ast
import json
import uuid
import subprocess
from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from database import init_db, get_active_repo
from scanner.repo_scanner import load_profile, get_changed_files
from memory.vector_store import (
    list_catalog_entries, query_tests_by_files, query_tests_by_description,
    update_test_run_status, add_correction, path_overlap,
)
from agents.llm_client import client as llm_client, set_trace_context

console = Console()

# Generated tests live under ~/.ai-workspace (see paths.py), shared across
# every clone of the tool — not inside this checkout.
from paths import GENERATED_TESTS_DIR as GENERATED_TESTS_ROOT


# ---------- verify run --------------------------------------------------

def resolve_base_url(profile: dict) -> str:
    """
    The default verify target URL when neither --url nor $VERIFY_TARGET_URL
    is given. Prefers the OpenAPI `servers[].url` from the scan profile
    (made absolute against the detected port if it is a bare path), so the
    context path / API prefix the user would otherwise hand-set is picked
    up automatically. Falls back to http://localhost:<detected-port>.
    """
    port = (profile.get("runtime") or {}).get("port", 8080)
    spec_url = (profile.get("api_base_url") or "").strip()
    if spec_url.startswith(("http://", "https://")):
        return spec_url
    if spec_url.startswith("/"):
        return f"http://localhost:{port}{spec_url.rstrip('/')}"
    return f"http://localhost:{port}"


def _run_pytest(selected_files: list[Path], url: str, repo_dir: Path):
    """Run pytest over `selected_files`, from `repo_dir`. Returns
    (results, stdout), or an int exit code on a fatal error."""
    env = os.environ.copy()
    env["VERIFY_TARGET_URL"] = url
    # Run from the test dir with bare filenames so pytest reports plain
    # `test_x.py::test_y` — deterministic wherever the state dir lives.
    cmd = ["pytest", "-v", "--tb=short", "--no-header",
           *[p.name for p in selected_files]]
    console.print(
        f"  [dim]$ VERIFY_TARGET_URL={url} pytest "
        f"{' '.join(p.name for p in selected_files)}[/dim]\n"
    )
    try:
        proc = subprocess.run(
            cmd, env=env, cwd=str(repo_dir),
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        console.print("[red]pytest timed out after 180s[/red]")
        return 1
    except FileNotFoundError:
        console.print(
            "[red]pytest not found in PATH.[/red] "
            "Install: [bold]pip install pytest requests[/bold]"
        )
        return 2
    return _parse_pytest_output(proc.stdout), proc.stdout


def _render_results(results: list[dict]) -> tuple[int, int, int]:
    """Print the results table. Returns (n_pass, n_fail, n_err)."""
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, title="Test results")
    table.add_column("Status", style="bold", width=8, justify="center")
    table.add_column("Test", style="white")
    table.add_column("Detail", style="dim")
    n_pass = n_fail = n_err = 0
    for r in results:
        status = {
            "PASSED":  "[green]✓ PASS[/green]",
            "FAILED":  "[red]✗ FAIL[/red]",
            "ERROR":   "[red]! ERR[/red]",
            "SKIPPED": "[dim]- SKIP[/dim]",
        }.get(r["status"], r["status"])
        table.add_row(status, r["test"], r.get("detail", "")[:80])
        if r["status"] == "PASSED":
            n_pass += 1
        elif r["status"] == "FAILED":
            n_fail += 1
        elif r["status"] == "ERROR":
            n_err += 1
    console.print(table)
    console.print(
        f"[bold]{n_pass} passed · {n_fail} failed · {n_err} errored[/bold]"
    )
    return n_pass, n_fail, n_err


# ---------- auto-fix (self-heal) -----------------------------------------

_FIX_PROMPT = """A generated pytest e2e-test file has failing tests caused by
bugs in the TEST CODE itself (not the system under test). Fix the file.

## Test file: {filename}
```python
{source}
```

## Failing tests in this file + diagnosis
{failures_block}

## Rules
- Fix ONLY the diagnosed test-code bugs. Leave passing tests untouched.
- Keep using the conftest fixtures (`base_url`, `ctx`, `register_cleanup`)
  exactly as the file already uses them — the suite shares state via them.
- Preserve the dependency contract: read deps from `ctx`, skip if absent,
  store created ids in `ctx`, register cleanups, never delete a ctx-shared
  resource inline.
- Return the COMPLETE corrected file as raw Python. No prose, no fences.
"""


async def _llm_fix_test(file_abs: Path, file_label: str,
                        failures: list[tuple]) -> str:
    """LLM-patch a test file given its test-bug failures + analyses. Returns
    the corrected file content (ast-validated), or '' if it can't."""
    try:
        source = file_abs.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    lines = []
    for f, a in failures:
        lines.append(
            f"- {f['test']}  [{a.get('category')}]\n"
            f"  diagnosis: {a.get('reasoning', '')}\n"
            f"  fix hint:  {a.get('fix_hint', '')}"
        )
    prompt = _FIX_PROMPT.format(
        filename=file_label,
        source=source[:24000],
        failures_block="\n".join(lines),
    )
    set_trace_context(trace_id=f"verify-fix-{uuid.uuid4().hex[:8]}",
                      agent_name="TestVerifier")
    try:
        resp = await llm_client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
    except Exception as e:
        console.print(f"  [red]✗ fix LLM call failed: {type(e).__name__}: {e}[/red]")
        return ""
    # Strip a stray markdown fence if the model added one.
    if text.lstrip().startswith("```"):
        body = text.split("```", 1)[1]
        if "\n" in body and body.split("\n", 1)[0].strip().isalpha():
            body = body.split("\n", 1)[1]
        if "```" in body:
            body = body.rsplit("```", 1)[0]
        text = body
    text = text.strip() + "\n"
    try:
        ast.parse(text)                      # never write a broken patch
    except SyntaxError:
        return ""
    return text


async def _autofix_files(fixable: list[tuple], repo_id: str) -> None:
    """Group test-bug failures by file and LLM-patch each file in place."""
    repo_dir = GENERATED_TESTS_ROOT / repo_id
    by_file: dict[str, list[tuple]] = {}
    for f, a in fixable:
        by_file.setdefault(f["file"], []).append((f, a))
    for fname, items in by_file.items():
        file_abs = repo_dir / Path(fname).name
        label = Path(fname).name
        fixed = await _llm_fix_test(file_abs, label, items)
        if not fixed:
            console.print(
                f"  [yellow]⊘ {label}: could not produce a valid fix[/yellow]"
            )
            continue
        file_abs.write_text(fixed, encoding="utf-8")
        console.print(
            f"  [green]✓ {label}: patched[/green] "
            f"[dim]({len(items)} test-bug failure(s))[/dim]"
        )


# Auto-fix self-heal loop: at most this many fix-and-rerun rounds. The loop
# also stops early when no test-bug failures remain or a round makes no
# progress — so this cap is a backstop, not the usual exit.
MAX_FIX_ROUNDS = 3


async def run_verify_run(rest: list[str]) -> int:
    use_diff = False
    url: str | None = None
    do_analyze = True
    select_all = False
    fix_mode = False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--diff":
            use_diff = True; i += 1
        elif a == "--url" and i + 1 < len(rest):
            url = rest[i + 1]; i += 2
        elif a == "--no-analyze":
            do_analyze = False; i += 1
        elif a == "--fix":
            fix_mode = True; i += 1
        elif a == "--select" and i + 1 < len(rest):
            if rest[i + 1] == "all":
                select_all = True
            i += 2
        else:
            console.print(f"[red]Unknown flag: {a}[/red]")
            return 2

    await init_db()
    active = await get_active_repo()
    if not active:
        console.print("[red]No active repo set. Run [bold]repo use <id>[/bold] first.[/red]")
        return 2
    repo_id = active["id"]

    # Resolve URL: --url > $VERIFY_TARGET_URL > OpenAPI servers.url > port
    try:
        profile = load_profile()
    except FileNotFoundError:
        profile = {}
    if not url:
        url = os.environ.get("VERIFY_TARGET_URL")
    if not url:
        url = resolve_base_url(profile)

    # Test selection
    repo_dir = GENERATED_TESTS_ROOT / repo_id
    if not repo_dir.is_dir() or not any(repo_dir.glob("test_*.py")):
        console.print(
            f"[red]No generated tests under {repo_dir}.[/red] "
            f"Run [bold]verify generate[/bold] first."
        )
        return 2

    selected_files: list[Path] = []
    if use_diff and not select_all:
        # File→test impact: changed files -> the controller's catalog entry
        # -> its test file. One hop, via the source_files index.
        try:
            changed = get_changed_files(profile.get("repo_path", ""))
        except Exception:
            changed = []
        hits = query_tests_by_files(changed, repo_id) if changed else []
        if hits:
            wanted = {(h.get("metadata") or {}).get("file_path", "")
                      for h in hits}
            selected_files = [p for p in sorted(repo_dir.glob("test_*.py"))
                              if p.name in wanted]
        if not selected_files:
            console.print(
                "[yellow]No catalog test maps to the changed files — "
                "running all generated tests.[/yellow]"
            )
            selected_files = sorted(repo_dir.glob("test_*.py"))
    else:
        selected_files = sorted(repo_dir.glob("test_*.py"))

    # Order the files by entity topology so an upstream entity's tests run
    # (and populate the shared ctx) before a downstream entity reads it.
    # test_<entity>.py -> entity -> position in the topological order.
    topo_order = ((profile.get("entity_topology") or {}).get("order")) or []

    def _file_rank(p: Path) -> int:
        ent = p.stem[5:] if p.stem.startswith("test_") else p.stem
        return topo_order.index(ent) if ent in topo_order else len(topo_order)

    selected_files = sorted(selected_files, key=_file_rank)

    console.print(Panel.fit(
        f"[bold]verify run[/bold]  "
        f"repo={repo_id} · url={url} · selected={len(selected_files)} test file(s)",
        border_style="blue",
    ))

    # ---- run → analyze → (with --fix) auto-heal → rerun --------------------
    round_no = 0
    prev_fixable = None
    results: list[dict] = []

    while True:
        ran = _run_pytest(selected_files, url, repo_dir)
        if isinstance(ran, int):
            return ran                       # pytest missing / timed out
        results, stdout = ran
        _render_results(results)

        failures = [r for r in results if r["status"] in ("FAILED", "ERROR")]
        analyses: dict[str, tuple] = {}       # test label -> (failure, analysis)
        if failures and do_analyze:
            console.print("\n[bold]Analyzing failures...[/bold]")
            for f in failures:
                a = await _analyze_failure(f, stdout, repo_id, url)
                if a:
                    analyses[f["test"]] = (f, a)
        elif failures:
            console.print(
                "\n[dim]Pass --analyze to LLM-classify failures.[/dim]"
            )

        # Self-heal: only with --fix, only test-code bugs (never regression).
        if not (fix_mode and failures and do_analyze):
            break
        fixable = [
            (f, a) for (f, a) in analyses.values()
            if a.get("category") in ("test-bug-script", "test-bug-payload")
        ]
        if not fixable:
            console.print(
                "[dim]auto-fix: nothing auto-fixable — remaining failures are "
                "regression / flaky / config (left for human review).[/dim]"
            )
            break
        if round_no >= MAX_FIX_ROUNDS:
            console.print(
                f"[yellow]auto-fix: hit the {MAX_FIX_ROUNDS}-round cap — "
                f"stopping with {len(fixable)} test-bug failure(s) left.[/yellow]"
            )
            break
        if prev_fixable is not None and len(fixable) >= prev_fixable:
            console.print(
                "[yellow]auto-fix: the last round did not reduce test-bug "
                "failures — stopping (more rounds won't help).[/yellow]"
            )
            break
        prev_fixable = len(fixable)
        round_no += 1
        console.print(
            f"\n[bold magenta]── auto-fix round {round_no} ──[/bold magenta]  "
            f"patching {len(fixable)} test-bug failure(s), then re-running"
        )
        await _autofix_files(fixable, repo_id)
        # loop back: re-run pytest

    # Catalog last_run rollup reflects the FINAL run.
    file_status = _rollup_file_status(results, selected_files)
    for fp, status in file_status.items():
        test_id = _test_id_from_file(fp, repo_id)
        if test_id:
            update_test_run_status(test_id, status)

    # Exit code: 0 if all green, 1 if any test failed or errored.
    n_fail = sum(1 for r in results if r["status"] == "FAILED")
    n_err = sum(1 for r in results if r["status"] == "ERROR")
    return 1 if (n_fail or n_err) else 0


# ---------- pytest output parsing ----------------------------------------

# Match lines like:
#   .ai-workspace/generated-tests/petclinic/test_x.py::test_foo PASSED   [ 50%]
#   .ai-workspace/generated-tests/petclinic/test_x.py::test_foo FAILED
_PYTEST_LINE_RE = re.compile(
    r"^(?P<file>[\w./\-]+\.py)::(?P<test>[\w_]+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED)"
)


def _parse_pytest_output(stdout: str) -> list[dict]:
    results: list[dict] = []
    for line in stdout.splitlines():
        m = _PYTEST_LINE_RE.search(line)
        if not m:
            continue
        results.append({
            "file":   m.group("file"),
            "test":   f"{Path(m.group('file')).name}::{m.group('test')}",
            "status": m.group("status"),
            "detail": "",
        })
    # Pull failure detail from FAILED block
    fail_blocks = re.split(r"^_{5,}\s+([\w./:]+)\s+_{5,}$", stdout, flags=re.MULTILINE)
    # Best-effort — leave detail empty if parsing fails. The full trace
    # is captured in stdout and fed to the analysis step.
    return results


def _rollup_file_status(results: list[dict], files: list[Path]) -> dict[Path, str]:
    """Per-file: any FAIL → FAIL; any ERROR → ERROR; else PASS."""
    by_file: dict[str, list[str]] = {}
    for r in results:
        by_file.setdefault(r["file"], []).append(r["status"])
    out: dict[Path, str] = {}
    for f in files:
        statuses = by_file.get(f.name, [])
        if not statuses:
            out[f] = "UNKNOWN"
        elif "ERROR" in statuses:
            out[f] = "ERROR"
        elif "FAILED" in statuses:
            out[f] = "FAIL"
        else:
            out[f] = "PASS"
    return out


def _test_id_from_file(fp: Path, repo_id: str) -> str | None:
    """Reverse-lookup catalog entry by file name."""
    target = fp.name
    for e in list_catalog_entries(repo_id=repo_id):
        if (e.get("metadata") or {}).get("file_path") == target:
            return e["id"]
    return None


# ---------- failure analysis loop ---------------------------------------

ANALYZE_PROMPT = """You are debugging a failed e2e test that targeted a running system.

## Target URL the test hit
{url}

## The test that failed
File: {file}
Test name: {test_name}

## Pytest output (full failure trace; may include stdout, stderr, traceback)
```
{trace}
```

## Test source (the actual code that ran)
```python
{source}
```

## Your task
Classify this failure. Pick exactly ONE category:

- **test-bug-script**: The test code itself has a bug (typo, wrong import,
  syntax error, wrong assertion semantics). The system being tested is
  fine. Fix is in the test code.

- **test-bug-payload**: The test sent a payload/url shape the API doesn't
  accept (e.g., wrong content-type, wrong field name, missing required
  parameter the test couldn't have known about from the spec alone).
  System is fine; future test generations should know the right shape.

- **test-bug-config**: Wrong URL, wrong port, missing auth header, the
  target service isn't actually running on the configured host, etc.
  Environmental, not the system's fault.

- **regression**: The test is correct and the system actually broke.
  The expected behavior is not happening. Real production bug.

- **flaky**: Transient — network timeout, race condition, timing
  dependency. Re-running would likely succeed.

## Output format — STRICT JSON
{{
  "category":     "test-bug-script | test-bug-payload | test-bug-config | regression | flaky",
  "reasoning":    "one to three sentences explaining the classification with evidence from the trace",
  "lesson":       "if category is test-bug-*, a single short sentence stating a TRANSFERABLE rule — a general principle that applies to ANY REST API, NOT specific to this project's endpoint or field names. Good: 'a reference field typed as a nested DTO is a JSON object, not a scalar id'. Bad: 'petclinic pets use type not typeId'. If the cause is irreducibly specific to this one project and would not help generate tests for a different API, return an empty string. Empty for regression/flaky.",
  "fix_hint":     "if category is test-bug-*, a concrete change to the test code. If regression, what to look at in the codebase. Empty for flaky."
}}

No prose, no fences. Just the JSON.
"""


async def _analyze_failure(failure: dict, full_stdout: str, repo_id: str, url: str) -> None:
    """One LLM call per failed test. Writes lesson to corrections_memory if
    test-bug-*. Surfaces regression for the human."""
    file_path = (GENERATED_TESTS_ROOT / repo_id / Path(failure["file"]).name)
    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except Exception:
        source = "<unreadable>"

    # Pull the relevant section of pytest output (trace for this test only)
    trace = _extract_trace_for_test(full_stdout, failure["test"])

    prompt = ANALYZE_PROMPT.format(
        url=url, file=failure["file"], test_name=failure["test"],
        trace=trace[:3500], source=source,
    )

    trace_id = f"verify-analyze-{uuid.uuid4().hex[:8]}"
    set_trace_context(trace_id=trace_id, agent_name="FailureAnalyzer")

    # The analyzer LLM call occasionally returns an empty / non-JSON body
    # (transient). Retry once before giving up, and fail quietly.
    data = None
    for _attempt in (1, 2):
        try:
            response = await llm_client.messages.create(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = (response.content[0].text or "").strip()
            if raw.startswith("```"):
                raw = raw.split("```", 1)[1]
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                if "```" in raw:
                    raw = raw.split("```", 1)[0]
            if raw.strip():
                data = json.loads(raw)
                break
        except Exception:
            data = None  # fall through to a retry, then give up
    if data is None:
        console.print(
            f"  [yellow]⚠ Failure analysis inconclusive for {failure['test']} "
            f"— LLM gave no parseable verdict, skipped[/yellow]"
        )
        return

    cat = data.get("category", "unknown")
    reasoning = data.get("reasoning", "")
    lesson = data.get("lesson", "")
    fix = data.get("fix_hint", "")

    # Render verdict
    cat_color = {
        "regression":       "red",
        "test-bug-script":  "yellow",
        "test-bug-payload": "yellow",
        "test-bug-config":  "yellow",
        "flaky":            "dim",
    }.get(cat, "white")
    console.print(Panel(
        f"[bold {cat_color}]{cat.upper()}[/bold {cat_color}]\n\n"
        f"{reasoning}\n\n"
        f"[dim]Fix hint:[/dim] {fix or '(none)'}",
        title=f"Analysis: {failure['test']}",
        border_style=cat_color,
    ))

    # Persist learning. Only generation-actionable categories produce
    # lessons: test-bug-script (the LLM wrote bad test code) and
    # test-bug-payload (the LLM guessed the wrong API shape). test-bug-config
    # is an environment problem — saving it would dilute corrections_memory
    # with rules future generation can't act on. regression / flaky also skip.
    if cat in ("test-bug-script", "test-bug-payload") and lesson:
        try:
            corr_id = "lesson-" + uuid.uuid4().hex[:8]
            add_correction(
                correction_id=corr_id,
                note=lesson,
                example=f"Failure in {failure['test']} ({cat}): {reasoning[:200]}",
                correction_type="test-gen-lesson",
                repo_id=repo_id,
                pinned=False,
            )
            console.print(
                f"  [dim]→ Lesson saved to corrections_memory: {lesson[:80]}[/dim]"
            )
        except Exception as e:
            console.print(f"  [yellow]⚠ Could not save lesson: {e}[/yellow]")
    elif cat == "test-bug-config":
        console.print(
            "  [dim]Config issue — not saving as generation lesson "
            "(env, not test-code).[/dim]"
        )

    # Returned so the --fix self-heal loop can decide what is auto-fixable.
    return data


def _extract_trace_for_test(stdout: str, test_label: str) -> str:
    """Crude extraction: grab the section of pytest output mentioning this
    test. If we can't pin a tight window, return the last 3.5K of stdout."""
    test_name = test_label.split("::")[-1]
    idx = stdout.find(test_name)
    if idx < 0:
        return stdout[-3500:]
    start = max(0, idx - 200)
    end = min(len(stdout), idx + 3500)
    return stdout[start:end]


# ---------- verify list -------------------------------------------------

async def run_verify_list(rest: list[str]) -> None:
    repo_id: str | None = None
    i = 0
    while i < len(rest):
        if rest[i] == "--repo" and i + 1 < len(rest):
            repo_id = rest[i + 1]; i += 2
        else:
            console.print(f"[red]Unknown flag: {rest[i]}[/red]")
            return

    if not repo_id:
        await init_db()
        active = await get_active_repo()
        if not active:
            console.print("[red]No active repo and no --repo flag.[/red]")
            return
        repo_id = active["id"]

    entries = list_catalog_entries(repo_id=repo_id)
    if not entries:
        console.print(
            f"[dim]Test catalog for repo '{repo_id}' is empty. "
            f"Run [bold]verify generate[/bold] to populate.[/dim]"
        )
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True,
                  title=f"Test catalog · repo={repo_id}")
    table.add_column("Test ID",  style="cyan bold")
    table.add_column("Status",   style="white", justify="center")
    table.add_column("Source file(s)", style="dim")
    table.add_column("APIs covered", style="dim")
    table.add_column("Description", style="white", overflow="fold")
    table.add_column("Last run",   style="dim", width=19)

    for e in entries:
        m = e.get("metadata") or {}
        status_raw = m.get("last_status", "UNKNOWN")
        status = {
            "PASS":    "[green]PASS[/green]",
            "FAIL":    "[red]FAIL[/red]",
            "ERROR":   "[red]ERR[/red]",
            "UNKNOWN": "[dim]—[/dim]",
        }.get(status_raw, status_raw)
        last_run = (m.get("last_run_at") or "")[:19]
        sources = ", ".join(
            Path(s).name for s in (m.get("source_files", "") or "").split(",")
            if s.strip()
        ) or "—"
        table.add_row(
            e["id"],
            status,
            sources,
            m.get("apis_covered", ""),
            e.get("document", "")[:80].replace("Tests ", ""),
            last_run,
        )
    console.print(table)


# ---------- verify impact ------------------------------------------------

async def run_verify_impact(rest: list[str]) -> None:
    """
    `verify impact <file>` — answer "if this file changes, what do I verify?":
    the API endpoints implemented in the file, and the catalog tests that
    cover them. A manual-inspection surface — the CI pipeline already does
    this automatically via `verify run --diff` (same source_files index).
    """
    target = ""
    repo_id: str | None = None
    i = 0
    while i < len(rest):
        if rest[i] == "--repo" and i + 1 < len(rest):
            repo_id = rest[i + 1]; i += 2
        elif not rest[i].startswith("-") and not target:
            target = rest[i]; i += 1
        else:
            i += 1
    if not target:
        console.print("[red]Usage: verify impact <file> [--repo X][/red]")
        return

    await init_db()
    if not repo_id:
        active = await get_active_repo()
        if not active:
            console.print("[red]No active repo and no --repo flag.[/red]")
            return
        repo_id = active["id"]

    # Endpoints implemented in the file (from the scan profile).
    try:
        profile = load_profile()
    except FileNotFoundError:
        profile = {}
    eps = [
        a for a in (profile.get("apis") or [])
        if path_overlap(a.get("source_file", "") or "", target)
    ]
    # Catalog tests whose source_files cover the file.
    tests = query_tests_by_files([target], repo_id)

    console.print(Panel.fit(
        f"[bold]verify impact[/bold]  file={target}  repo={repo_id}",
        border_style="blue",
    ))

    if eps:
        t1 = Table(box=box.SIMPLE_HEAVY, show_header=True,
                   title=f"Endpoints implemented here ({len(eps)})")
        t1.add_column("Method", style="cyan", width=7)
        t1.add_column("Path", style="white")
        for a in sorted(eps, key=lambda x: (x.get("path", ""), x.get("method", ""))):
            t1.add_row(a.get("method", ""), a.get("path", ""))
        console.print(t1)
    else:
        console.print("[dim]No API endpoints map to this file.[/dim]")

    if tests:
        t2 = Table(box=box.SIMPLE_HEAVY, show_header=True,
                   title=f"Tests to run when this file changes ({len(tests)})")
        t2.add_column("Test ID", style="cyan bold")
        t2.add_column("Status", justify="center", width=8)
        t2.add_column("APIs covered", style="dim")
        for e in tests:
            m = e.get("metadata") or {}
            t2.add_row(e["id"], m.get("last_status", "UNKNOWN"),
                       m.get("apis_covered", ""))
        console.print(t2)
        console.print(
            "\n[dim]CI runs exactly this set automatically — "
            "`verify run --diff` selects it from the same index.[/dim]"
        )
    else:
        console.print(
            "[yellow]No catalog test covers this file.[/yellow] "
            "[dim]Run `verify generate` to add coverage.[/dim]"
        )


# ---------- verify health-check -----------------------------------------

async def run_verify_health_check(rest: list[str]) -> int:
    """
    HTTP probe against the target system. Returns:
      0 — service responded (200/201/3xx/404 all count: any response means
          the server is up; 404 just means the probe path isn't bound).
      1 — connection refused / timeout / 5xx server error / unreachable.

    Used as a CD gate in the GitHub Actions workflow — exit-code-driven
    so the workflow can `if: success()`.
    """
    import urllib.request, urllib.error, socket

    url: str | None = None
    timeout = 5.0
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--url" and i + 1 < len(rest):
            url = rest[i + 1]; i += 2
        elif a == "--timeout" and i + 1 < len(rest):
            try:
                timeout = float(rest[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            console.print(f"[red]Unknown flag: {a}[/red]")
            return 1

    # Resolve URL: --url > env > runtime.health_endpoint > runtime.port > 8080
    try:
        profile = load_profile()
    except FileNotFoundError:
        profile = {}
    runtime = profile.get("runtime") or {}

    if not url:
        base = os.environ.get("VERIFY_TARGET_URL") or resolve_base_url(profile)
        health = runtime.get("health_endpoint")
        if health:
            # If health is like "/actuator/health" or "/api/health", join
            url = base.rstrip("/") + (health if health.startswith("/") else "/" + health)
        else:
            url = base

    console.print(f"[dim]Probing {url}  (timeout {timeout}s)[/dim]")

    try:
        req = urllib.request.Request(url, method="GET", headers={
            "User-Agent": "ai-eng-verify-healthcheck/1.0",
            "Accept": "*/*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
        if 200 <= status < 400 or status == 404:
            console.print(
                f"[green]✓ Service is up[/green] "
                f"[dim](HTTP {status} from {url})[/dim]"
            )
            return 0
        if 500 <= status:
            console.print(
                f"[red]✗ Service error[/red] "
                f"[dim](HTTP {status} from {url})[/dim]"
            )
            return 1
        # 4xx other than 404 — server is up but rejecting the probe shape.
        # Count as up (service is responding).
        console.print(
            f"[yellow]⚠ Service responded with {status}[/yellow] "
            f"[dim]({url}) — treating as up[/dim]"
        )
        return 0
    except urllib.error.HTTPError as e:
        # urllib raises HTTPError for 4xx/5xx
        if 500 <= e.code:
            console.print(f"[red]✗ HTTP {e.code} from {url}[/red]")
            return 1
        console.print(f"[yellow]⚠ HTTP {e.code} from {url} — treating as up[/yellow]")
        return 0
    except (urllib.error.URLError, socket.timeout, ConnectionRefusedError, OSError) as e:
        console.print(
            f"[red]✗ Could not reach {url}: {type(e).__name__}: {e}[/red]\n"
            f"[dim]Is the target system running? "
            f"Start it (e.g. `mvn spring-boot:run`) and retry.[/dim]"
        )
        return 1


# ---------- verify catalog search ---------------------------------------

async def _catalog_clear(rest: list[str]) -> None:
    """Wipe the active repo's generated test files + test_catalog entries —
    a clean slate before re-generating (removes stale / duplicate-coverage
    test files accumulated across runs)."""
    import shutil
    from memory.vector_store import clear_catalog

    repo_id: str | None = None
    i = 0
    while i < len(rest):
        if rest[i] == "--repo" and i + 1 < len(rest):
            repo_id = rest[i + 1]; i += 2
        else:
            i += 1
    if not repo_id:
        await init_db()
        active = await get_active_repo()
        if not active:
            console.print("[red]No active repo and no --repo flag.[/red]")
            return
        repo_id = active["id"]

    n_entries = clear_catalog(repo_id)
    repo_dir = GENERATED_TESTS_ROOT / repo_id
    n_files = 0
    if repo_dir.is_dir():
        n_files = len(list(repo_dir.glob("test_*.py")))
        shutil.rmtree(repo_dir)

    console.print(
        f"[green]✓ Catalog cleared for '{repo_id}'[/green]  "
        f"[dim]({n_entries} catalog entr{'y' if n_entries == 1 else 'ies'} + "
        f"{n_files} test file(s) removed)[/dim]"
    )


async def run_verify_catalog(rest: list[str]) -> None:
    if rest and rest[0] == "clear":
        await _catalog_clear(rest[1:])
        return
    if not rest or rest[0] != "search":
        console.print(
            "[red]Usage: verify catalog search \"<query>\" [--repo X]  |  "
            "verify catalog clear [--repo X][/red]"
        )
        return
    rest = rest[1:]
    if not rest:
        console.print("[red]Need a query string.[/red]")
        return

    query_parts: list[str] = []
    repo_id: str | None = None
    i = 0
    while i < len(rest):
        if rest[i] == "--repo" and i + 1 < len(rest):
            repo_id = rest[i + 1]; i += 2
        else:
            query_parts.append(rest[i]); i += 1
    query = " ".join(query_parts).strip().strip('"').strip("'")
    if not query:
        console.print("[red]Empty query.[/red]")
        return

    if not repo_id:
        await init_db()
        active = await get_active_repo()
        if not active:
            console.print("[red]No active repo and no --repo flag.[/red]")
            return
        repo_id = active["id"]

    hits = query_tests_by_description(query, repo_id, top_k=10)
    if not hits:
        console.print(f"[dim]No catalog entries match '{query}'.[/dim]")
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True,
                  title=f"Semantic search · query='{query}'")
    table.add_column("Sim",      style="cyan", width=6, justify="right")
    table.add_column("Test ID",  style="bold")
    table.add_column("Status",   width=8, justify="center")
    table.add_column("APIs",     style="dim")
    table.add_column("Description", style="white", overflow="fold")

    for h in hits:
        m = h.get("metadata") or {}
        status_raw = m.get("last_status", "UNKNOWN")
        status = {
            "PASS":  "[green]PASS[/green]",
            "FAIL":  "[red]FAIL[/red]",
            "ERROR": "[red]ERR[/red]",
        }.get(status_raw, "[dim]—[/dim]")
        table.add_row(
            f"{h.get('similarity', 0):.2f}",
            h["id"],
            status,
            (m.get("apis_covered") or "")[:40],
            h.get("document", "")[:80].replace("Tests ", ""),
        )
    console.print(table)
