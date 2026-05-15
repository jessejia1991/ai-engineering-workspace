"""
`apply` — auto-apply one or more review findings to the target repo's
working tree, driven by a PR `/apply` comment.

Trigger flow (in CI):
  1. The reviewer interacts with the AI review on the PR in one of two ways:
       a. CHECKBOX  — ticks findings in the "AI Review — Apply Menu" comment,
                      then comments a bare `/apply`.
       b. EXPLICIT  — comments `/apply <finding_id> [<finding_id> ...]`,
                      optionally with free-text refinement after the ids.
  2. GitHub Actions `issue_comment.created` fires the `ai-apply` job.
  3. That job runs:
        python -m cli.main apply --pr <N> --comment-id <C> \
          --target-path <checkout> [--push]

What this command does:
  1. Fetch the `/apply` comment body via PyGithub.
  2. Parse it. Explicit ids win; a bare `/apply` falls back to reading the
     ticked checkboxes from the Apply Menu comment.
  3. For each resolved finding: look it up in `task_findings`, read the
     target file, ask the LLM for the complete modified file, render the
     diff, and write the file. Failures on one finding are reported and
     skipped — the rest of the batch still proceeds.
  4. If --push: one combined git commit covering every touched file,
     pushed to the PR branch. Without --push: files are written but git
     is skipped (preview mode).

Safety:
  - Reuses REVIEW_ALLOWED_REPOS allowlist (same gate as review `--post`).
    An out-of-allowlist target refuses to push even with --push.
  - Findings that touch a file missing from the checkout, or carry no
    file path, are skipped (logged) rather than aborting the batch.
  - LLM output is written as-is, no syntax check. If the build breaks,
    the next CI run on the new commit catches it — fail-forward.
"""

from __future__ import annotations

import os
import re
import json
import uuid
import difflib
import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from database import init_db, get_finding
from agents.llm_client import client as llm_client, set_trace_context
from github_client import is_post_allowed_repo, REVIEW_SUMMARY_MARKER

console = Console()


# Finding ids are the first 8 hex chars of a uuid4 (see database.save_finding).
_FINDING_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# A ticked checkbox line in the Apply Menu, e.g. "- [x] `a1b2c3d4` ...".
_CHECKED_RE = re.compile(
    r"^\s*[-*]\s*\[[xX]\]\s*`?([0-9a-f]{8})`?",
    re.MULTILINE,
)


async def cmd_apply(
    pr_number: int,
    comment_id: int,
    target_path: str,
    push: bool = False,
) -> int:
    """Returns exit code. 0 = success. 1 = parse/no-op/finding failure.
    2 = setup/auth error."""
    await init_db()

    # 1. Resolve + verify target path
    target = Path(target_path).expanduser().resolve()
    if not target.is_dir():
        console.print(f"[red]target-path is not a directory: {target}[/red]")
        return 2
    if not (target / ".git").exists():
        console.print(f"[red]target-path is not a git checkout: {target}[/red]")
        return 2

    # 2. Allowlist gate (reuse review's two-gate logic — same threat model)
    repo = os.environ.get("GITHUB_REPO", "")
    if push:
        allowed, reason = is_post_allowed_repo()
        if not allowed:
            console.print(
                f"[red]Push BLOCKED by allowlist[/red] (target: {repo}): {reason}\n"
                f"[dim]Add the target repo to REVIEW_ALLOWED_REPOS to enable.[/dim]"
            )
            return 2

    # 3. Fetch the /apply comment body
    body = _fetch_comment_body(repo, pr_number, comment_id)
    if body is None:
        return 2

    # 4. Parse the directive — explicit ids, or fall back to checkboxes
    parsed = _parse_apply_directive(body)
    if parsed is None:
        console.print(
            "[red]Comment doesn't contain a recognizable `/apply` directive.[/red]\n"
            f"[dim]Body was: {body[:200]}[/dim]"
        )
        return 1
    finding_ids, extra_instructions = parsed
    mode = "explicit"

    if not finding_ids:
        # Bare `/apply` — read the ticked boxes from the Apply Menu comment.
        mode = "checkbox"
        menu_body = _find_summary_comment(repo, pr_number)
        if menu_body is None:
            console.print(
                "[red]No AI Review Apply Menu comment found on this PR — "
                "nothing to apply.[/red]"
            )
            return 1
        finding_ids = _CHECKED_RE.findall(menu_body)
        if not finding_ids:
            console.print(
                "[yellow]No findings are ticked in the Apply Menu. "
                "Tick at least one box, then comment `/apply`.[/yellow]"
            )
            return 0

    # Dedupe, preserve order
    seen: set[str] = set()
    finding_ids = [x for x in finding_ids if not (x in seen or seen.add(x))]

    console.print(Panel.fit(
        f"[bold]apply[/bold]  mode={mode}  findings={len(finding_ids)}  "
        f"pr={pr_number}  target={target.name}  "
        f"push={'[green]yes[/green]' if push else '[dim]no (preview)[/dim]'}",
        border_style="magenta",
    ))
    if extra_instructions:
        console.print(f"[dim]User extra instructions: {extra_instructions}[/dim]")

    # 5. Apply each finding. One shared trace covers the whole batch.
    trace_id = f"apply-{uuid.uuid4().hex[:8]}"
    set_trace_context(trace_id=trace_id, agent_name="AutoApply")

    applied: list[dict] = []   # records with an actual diff (changed=True)
    skipped: list[str] = []    # finding ids that couldn't be applied

    for fid in finding_ids:
        record = await _apply_one_finding(fid, target, extra_instructions)
        if record is None:
            skipped.append(fid)
        elif record["changed"]:
            applied.append(record)

    # 6. Report
    console.print()
    console.print(
        f"[bold]apply summary:[/bold]  "
        f"[green]{len(applied)} applied[/green]  "
        f"[yellow]{len(skipped)} skipped[/yellow]"
    )
    if not applied:
        console.print("[yellow]No files changed — nothing to commit.[/yellow]")
        return 1 if skipped else 0

    if not push:
        console.print(
            "[dim]Preview mode — files written but not committed. "
            "Pass --push to commit + push to the PR branch.[/dim]"
        )
        return 0

    # 7. One combined commit for the whole batch
    return _git_commit_and_push(target, applied, pr_number)


# ---------- finding application -----------------------------------------

async def _apply_one_finding(
    finding_id: str, target: Path, extra: str
) -> dict | None:
    """
    Apply a single finding to the working tree. Returns a record dict
    {finding_id, file_rel, title, agent, changed} on success, or None
    when the finding can't be applied (logged).

    Handles two cases:
      - the finding's file exists  -> modify it in place
      - the finding's file is new  -> create it (parent dirs included)
    Either way, the LLM may decline with an APPLY-SKIP signal; we then
    skip the finding rather than writing junk. Writes happen in place,
    so a later finding touching the same file composes on top.
    """
    finding = await get_finding(finding_id)
    if finding is None:
        console.print(f"  [red]✗ {finding_id}: not found in task_findings[/red]")
        return None
    try:
        content = json.loads(finding.get("content") or "{}")
    except json.JSONDecodeError:
        content = {}

    file_rel = (content.get("file") or "").strip().lstrip("/")
    if not file_rel:
        console.print(f"  [yellow]⊘ {finding_id}: finding has no file path — skipped[/yellow]")
        return None
    file_abs = target / file_rel
    is_new = not file_abs.is_file()
    original = "" if is_new else file_abs.read_text(encoding="utf-8", errors="ignore")

    status, payload = await _llm_apply(
        finding=content, original=original, file_rel=file_rel,
        extra=extra, is_new=is_new,
    )
    if status == "error":
        console.print(f"  [red]✗ {finding_id}: LLM call failed — {payload}[/red]")
        return None
    if status == "skip":
        console.print(
            f"  [yellow]⊘ {finding_id}: cannot apply cleanly — {payload}[/yellow]"
        )
        return None

    new_content = payload
    record = {
        "finding_id": finding_id,
        "file_rel": file_rel,
        "title": content.get("title", "AI-applied fix"),
        "agent": content.get("agent", "AutoApply"),
        "changed": True,
    }

    if is_new:
        if not new_content.strip():
            console.print(f"  [yellow]⊘ {finding_id}: LLM returned empty file — skipped[/yellow]")
            return None
        diff_text = "".join(difflib.unified_diff(
            [], new_content.splitlines(keepends=True),
            fromfile="/dev/null", tofile=f"b/{file_rel}", n=3,
        ))
        console.print(Panel(
            Syntax(diff_text, "diff", theme="ansi_dark", word_wrap=True),
            title=f"{finding_id} · NEW {file_rel}",
            border_style="green",
        ))
        file_abs.parent.mkdir(parents=True, exist_ok=True)
        file_abs.write_text(new_content, encoding="utf-8")
        console.print(f"  [green]✓ {finding_id}: created {file_rel}[/green]")
        return record

    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{file_rel}",
        tofile=f"b/{file_rel}",
        n=3,
    ))
    if not diff_lines:
        console.print(f"  [dim]· {finding_id}: LLM produced no change ({file_rel})[/dim]")
        record["changed"] = False
        return record

    console.print(Panel(
        Syntax("".join(diff_lines), "diff", theme="ansi_dark", word_wrap=True),
        title=f"{finding_id} · {file_rel}",
        border_style="cyan",
    ))
    file_abs.write_text(new_content, encoding="utf-8")
    console.print(f"  [green]✓ {finding_id}: applied to {file_rel}[/green]")
    return record


# ---------- comment parsing ---------------------------------------------

def _parse_apply_directive(body: str) -> tuple[list[str], str] | None:
    """
    Parse a `/apply ...` directive. Returns (finding_ids, extra_instructions):
      - finding_ids: leading tokens that look like finding ids (8 hex).
        Empty list => checkbox mode (resolve from the Apply Menu).
      - extra_instructions: free text after the ids.
    Returns None if the body contains no `/apply` at all.
    """
    m = re.search(r"/apply\b(.*)", body, re.DOTALL)
    if not m:
        return None
    tokens = m.group(1).split()
    ids: list[str] = []
    i = 0
    while i < len(tokens) and _FINDING_ID_RE.match(tokens[i]):
        ids.append(tokens[i])
        i += 1
    extra = " ".join(tokens[i:]).strip()
    return ids, extra


def _find_summary_comment(repo: str, pr_number: int) -> str | None:
    """Return the body of the latest Apply Menu comment on the PR (located
    by REVIEW_SUMMARY_MARKER), or None if none / on API error."""
    if not repo:
        console.print("[red]GITHUB_REPO env var not set.[/red]")
        return None
    token = os.environ.get("GITHUB_TOKEN", "")
    try:
        from github import Github
    except ImportError:
        console.print("[red]PyGithub not installed. `pip install PyGithub`.[/red]")
        return None
    try:
        gh = Github(token) if token else Github()
        issue = gh.get_repo(repo).get_issue(pr_number)
        matches = [
            c.body for c in issue.get_comments()
            if c.body and REVIEW_SUMMARY_MARKER in c.body
        ]
        # get_comments() is chronological — the last match is the freshest
        # menu (re-reviews post a new one; stale ones are ignored).
        return matches[-1] if matches else None
    except Exception as e:
        console.print(f"[red]GitHub API error locating Apply Menu: {type(e).__name__}: {e}[/red]")
        return None


# ---------- GitHub fetch ------------------------------------------------

def _fetch_comment_body(repo: str, pr_number: int, comment_id: int) -> str | None:
    """Try PR review-comment first, then issue-comment. Returns body text
    or None on error (with a clear print)."""
    if not repo:
        console.print("[red]GITHUB_REPO env var not set.[/red]")
        return None
    token = os.environ.get("GITHUB_TOKEN", "")
    try:
        from github import Github, GithubException
    except ImportError:
        console.print("[red]PyGithub not installed. `pip install PyGithub`.[/red]")
        return None

    try:
        gh = Github(token) if token else Github()
        repository = gh.get_repo(repo)
        pr = repository.get_pull(pr_number)
        # Try review-comment id (inline code thread) first
        try:
            comment = pr.get_review_comment(comment_id)
            return comment.body
        except GithubException:
            pass
        # Fall back to issue-comment (PR conversation tab)
        try:
            comment = repository.get_issue(pr_number).get_comment(comment_id)
            return comment.body
        except GithubException as e:
            console.print(f"[red]Comment {comment_id} not found on PR #{pr_number}: {e}[/red]")
            return None
    except Exception as e:
        console.print(f"[red]GitHub API error: {type(e).__name__}: {e}[/red]")
        return None


# ---------- LLM apply ---------------------------------------------------

# Sentinel the LLM returns instead of file content when it cannot produce
# a sensible patch. Detected in _llm_apply -> the finding is skipped.
_SKIP_SENTINEL = "APPLY-SKIP:"


_APPLY_PROMPT = """You are applying a code-review suggestion to a single file.

## Review finding
Agent: {agent} ({severity})
Title: {title}
File: {file}
Detail: {detail}
Suggestion: {suggestion}

## Reviewer's request to auto-apply
The reviewer commented `/apply` on the finding above{extra_clause}.

{mode_block}

## When you CANNOT apply it
If the suggestion is ambiguous, impossible to turn into a concrete code
change, or would require edits in a file other than `{file}`, do NOT
guess and do NOT return prose. Return ONLY this single line:

  APPLY-SKIP: <one short sentence on why>

A half-finished file, an apology, or an explanatory comment in place of
real code all count as failure — use APPLY-SKIP instead.

## Output rules
- A successful result is ONLY the file content: no diff, no markdown
  fence, no commentary. The first line must be the file's real first line.
- A skip result is ONLY the single `APPLY-SKIP:` line.
- {extra_directive}
"""

_MODE_MODIFY = """## Apply the change
Apply the suggestion to the file content below. Return the COMPLETE new
file content — preserve every import, method, comment, and bit of
formatting that is not part of the fix.

## Original content of `{file}`
```
{original}
```"""

_MODE_CREATE = """## Create the file
The file `{file}` does NOT exist yet — you are creating it. Return the
COMPLETE content of this new file implementing the suggestion. It must be
a real, compilable file consistent with the project's conventions and
package layout (infer the package/imports from the path)."""


async def _llm_apply(finding: dict, original: str, file_rel: str,
                     extra: str, is_new: bool) -> tuple[str, str]:
    """
    Ask the LLM to apply the finding. Returns (status, payload):
      ("ok",    <new file content>)
      ("skip",  <reason>)            — LLM declined via APPLY-SKIP
      ("error", <message>)           — the API call itself failed
    """
    extra_clause = ""
    extra_directive = (
        "Stay minimal — touch only what the suggestion requires."
    )
    if extra:
        extra_clause = f", and additionally said: \"{extra}\""
        extra_directive = (
            "Apply the suggestion AND honor the reviewer's extra "
            "instructions above. If the extra instructions conflict "
            "with the original suggestion, prefer the extra instructions."
        )

    if is_new:
        mode_block = _MODE_CREATE.format(file=file_rel)
    else:
        mode_block = _MODE_MODIFY.format(
            file=file_rel,
            original=original[:30000],  # safety cap; huge files won't fit
        )

    prompt = _APPLY_PROMPT.format(
        agent=finding.get("agent", "?"),
        severity=(finding.get("severity") or "low").upper(),
        title=finding.get("title", "?"),
        file=file_rel,
        detail=finding.get("detail", ""),
        suggestion=finding.get("suggestion", ""),
        extra_clause=extra_clause,
        extra_directive=extra_directive,
        mode_block=mode_block,
    )

    try:
        response = await llm_client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"

    text = response.content[0].text

    # Strip markdown fence wrapping in case the LLM ignored the rule.
    if text.lstrip().startswith("```"):
        body = text.split("```", 1)[1]
        if "\n" in body and body.split("\n", 1)[0].strip().isalpha():
            body = body.split("\n", 1)[1]
        if "```" in body:
            body = body.rsplit("```", 1)[0]
        text = body

    # Skip signal — check after fence-stripping so a fenced sentinel
    # still gets caught.
    stripped = text.lstrip()
    if stripped.upper().startswith(_SKIP_SENTINEL):
        reason = stripped[len(_SKIP_SENTINEL):].strip().splitlines()[0]
        return "skip", (reason or "LLM gave no reason")

    return "ok", text.rstrip("\n") + "\n"


# ---------- git ---------------------------------------------------------

def _git(target: Path, *args: str) -> tuple[int, str, str]:
    """Run a git command in `target`. Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(target),
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git_commit_and_push(target: Path, records: list[dict],
                         pr_number: int) -> int:
    """Stage every touched file, make one commit for the batch, push to
    the current branch."""
    # 1. Confirm we're on a branch (detached-HEAD push is rude)
    rc, branch, _ = _git(target, "rev-parse", "--abbrev-ref", "HEAD")
    branch = (branch or "").strip()
    if rc != 0 or not branch or branch == "HEAD":
        console.print(
            "[red]Not on a branch (detached HEAD or git error). "
            "Refusing to push.[/red]"
        )
        return 2

    # 2. Stage each unique touched file
    files = sorted({r["file_rel"] for r in records})
    for file_rel in files:
        rc, _, err = _git(target, "add", file_rel)
        if rc != 0:
            console.print(f"[red]git add failed for {file_rel}: {err.strip()}[/red]")
            return 2

    # 3. Commit — one commit, every finding listed in the body
    if len(records) == 1:
        r = records[0]
        subject = f"AI-applied: {r['title']} (finding {r['finding_id']})"
    else:
        subject = f"AI-applied: {len(records)} review findings"

    msg_lines = [subject, ""]
    for r in records:
        msg_lines.append(
            f"- {r['finding_id']} {r['title']} ({r['agent']}) [{r['file_rel']}]"
        )
    msg_lines += [
        "",
        f"Applied via /apply on PR #{pr_number}.",
        "Co-Authored-By: ai-engineering-workspace <noreply@anthropic.com>",
    ]
    commit_msg = "\n".join(msg_lines)

    rc, out, err = _git(target, "commit", "-m", commit_msg)
    if rc != 0:
        if "nothing to commit" in (out + err).lower():
            console.print("[yellow]No staged changes — nothing to commit.[/yellow]")
            return 0
        console.print(f"[red]git commit failed: {err.strip()}[/red]")
        return 2

    # 4. Push to current branch
    rc, _, err = _git(target, "push", "origin", branch)
    if rc != 0:
        console.print(f"[red]git push failed: {err.strip()}[/red]")
        return 2

    console.print(
        f"[green]✓ Committed + pushed {len(records)} finding(s) to "
        f"[bold]{branch}[/bold][/green]  [dim]({len(files)} file(s))[/dim]"
    )
    return 0
