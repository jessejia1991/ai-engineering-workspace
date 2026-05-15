"""
`apply` — auto-apply a review finding to the target repo's working tree.

Trigger flow (in CI):
  1. Reviewer (or anyone with PR access) replies to an AI review comment
     with `/apply <finding_id>` or `/apply <finding_id> <extra instruction>`.
  2. GitHub Actions `issue_comment.created` event fires the `ai-apply`
     job in examples/petclinic-ci.yml.
  3. That job runs:
        python -m cli.main apply --pr <N> --comment-id <C> \
          --target-path <petclinic-checkout> [--push]

What this command does:
  1. Fetch the comment body via PyGithub.
  2. Parse `/apply <finding_id> [extra]`.
  3. Look up the finding in `task_findings` (TASK-PR<N>).
  4. Read the target file at <target-path>/<finding.file>.
  5. LLM produces the COMPLETE modified file content (taking
     finding.suggestion + user's extra instructions into account).
  6. Diff vs. original. Render diff to console.
  7. If --push: write file, git add + commit + push to PR branch.
     Otherwise: write file but skip git operations (preview mode).

Safety:
  - Reuses REVIEW_ALLOWED_REPOS allowlist (same gate as `--post`). An
    out-of-allowlist target refuses even with --push.
  - Single-finding, single-file scope. Multi-finding batches and
    multi-file refactors are explicitly out of scope (design doc
    future work).
  - LLM output is written as-is, no syntax check. If the build breaks,
    the next CI run on the new commit will catch it — fail-forward.
"""

from __future__ import annotations

import os
import re
import sys
import json
import uuid
import difflib
import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from database import init_db, get_finding, get_active_repo
from agents.llm_client import client as llm_client, set_trace_context

console = Console()


_APPLY_RE = re.compile(
    r"/apply\s+(?P<fid>[A-Za-z0-9_-]+)\s*(?P<extra>.*)",
    re.DOTALL,
)


async def cmd_apply(
    pr_number: int,
    comment_id: int,
    target_path: str,
    push: bool = False,
) -> int:
    """Returns exit code. 0 = success. 1 = LLM/parse failure. 2 = setup/auth error."""
    await init_db()

    # 1. Resolve + verify target path
    target = Path(target_path).expanduser().resolve()
    if not target.is_dir():
        console.print(f"[red]target-path is not a directory: {target}[/red]")
        return 2
    git_dir = target / ".git"
    if not git_dir.exists():
        console.print(f"[red]target-path is not a git checkout: {target}[/red]")
        return 2

    # 2. Allowlist gate (reuse review's two-gate logic — same threat model)
    from github_client import is_post_allowed_repo
    repo = os.environ.get("GITHUB_REPO", "")
    if push:
        allowed, reason = is_post_allowed_repo()
        if not allowed:
            console.print(
                f"[red]Push BLOCKED by allowlist[/red] (target: {repo}): {reason}\n"
                f"[dim]Add the target repo to REVIEW_ALLOWED_REPOS to enable.[/dim]"
            )
            return 2

    # 3. Fetch the comment body via PyGithub
    body = _fetch_comment_body(repo, pr_number, comment_id)
    if body is None:
        return 2

    # 4. Parse /apply finding_id [extra]
    parsed = _APPLY_RE.search(body)
    if not parsed:
        console.print(
            "[red]Comment doesn't contain a recognizable '/apply <finding_id>' directive.[/red]\n"
            f"[dim]Body was: {body[:200]}[/dim]"
        )
        return 1
    finding_id = parsed.group("fid").strip()
    extra_instructions = parsed.group("extra").strip()

    console.print(Panel.fit(
        f"[bold]apply[/bold]  finding={finding_id}  pr={pr_number}  "
        f"target={target.name}  push={'[green]yes[/green]' if push else '[dim]no (preview)[/dim]'}",
        border_style="magenta",
    ))
    if extra_instructions:
        console.print(f"[dim]User extra instructions: {extra_instructions}[/dim]")

    # 5. Load finding from DB
    finding = await get_finding(finding_id)
    if finding is None:
        console.print(f"[red]Finding {finding_id} not found in task_findings.[/red]")
        return 1
    try:
        content = json.loads(finding.get("content") or "{}")
    except json.JSONDecodeError:
        content = {}
    file_rel = (content.get("file") or "").strip().lstrip("/")
    if not file_rel:
        console.print("[red]Finding has no file path — cannot apply.[/red]")
        return 1
    file_abs = target / file_rel
    if not file_abs.is_file():
        console.print(f"[red]File not found at target: {file_abs}[/red]")
        return 1

    # 6. Read original + ask LLM to apply
    original = file_abs.read_text(encoding="utf-8", errors="ignore")
    console.print(f"[dim]File: {file_rel} ({len(original)} chars)[/dim]")

    trace_id = f"apply-{uuid.uuid4().hex[:8]}"
    set_trace_context(trace_id=trace_id, agent_name="AutoApply")

    new_content = await _llm_apply(
        finding=content,
        original=original,
        file_rel=file_rel,
        extra=extra_instructions,
    )
    if not new_content:
        console.print("[red]LLM didn't return a valid file body.[/red]")
        return 1

    # 7. Diff
    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{file_rel}",
        tofile=f"b/{file_rel}",
        n=3,
    ))
    if not diff_lines:
        console.print("[yellow]LLM produced no changes — nothing to apply.[/yellow]")
        return 0

    diff_text = "".join(diff_lines)
    console.print(Panel(
        Syntax(diff_text, "diff", theme="ansi_dark", word_wrap=True),
        title=f"Proposed diff · {file_rel}",
        border_style="cyan",
    ))

    # 8. Write file
    file_abs.write_text(new_content, encoding="utf-8")
    console.print(f"[green]✓ Wrote new content to {file_rel}[/green]")

    if not push:
        console.print(
            "[dim]Preview mode — not pushing. "
            "Pass --push to commit + push to the PR branch.[/dim]"
        )
        return 0

    # 9. git add + commit + push
    return _git_commit_and_push(
        target=target, file_rel=file_rel, finding_id=finding_id,
        title=content.get("title", "AI-applied fix"), agent=content.get("agent", "AutoApply"),
    )


# ---------- helpers --------------------------------------------------

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


_APPLY_PROMPT = """You are applying a code-review suggestion to a single file.

## Review finding
Agent: {agent} ({severity})
Title: {title}
File: {file}
Detail: {detail}
Suggestion: {suggestion}

## Reviewer's request to auto-apply
The reviewer commented `/apply` on the finding above{extra_clause}.
Apply the suggestion to the file content below.

## Original file content
```
{original}
```

## Instructions
- Return the COMPLETE new file content — do NOT return a diff, do NOT
  return just the changed lines.
- Preserve everything in the file that wasn't part of the fix
  (imports, other methods, comments, formatting).
- If the suggestion is genuinely ambiguous or impossible to apply
  cleanly (e.g. references a method/field that doesn't exist), return
  the original content UNCHANGED — never invent code that doesn't fit.
- {extra_directive}

## Output
Return ONLY the new file content. No prose, no markdown fence, no
explanation. The first line should be the file's actual first line.
"""


async def _llm_apply(finding: dict, original: str, file_rel: str,
                     extra: str) -> str:
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

    prompt = _APPLY_PROMPT.format(
        agent=finding.get("agent", "?"),
        severity=(finding.get("severity") or "low").upper(),
        title=finding.get("title", "?"),
        file=file_rel,
        detail=finding.get("detail", ""),
        suggestion=finding.get("suggestion", ""),
        extra_clause=extra_clause,
        extra_directive=extra_directive,
        original=original[:30000],  # safety cap; large files won't fit otherwise
    )

    try:
        response = await llm_client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        console.print(f"[red]LLM call failed: {type(e).__name__}: {e}[/red]")
        return ""

    text = response.content[0].text
    # Tolerate markdown fence wrapping in case the LLM ignored the rule
    if text.lstrip().startswith("```"):
        body = text.split("```", 1)[1]
        # drop optional language tag
        if "\n" in body and body.split("\n", 1)[0].strip().isalpha():
            body = body.split("\n", 1)[1]
        if "```" in body:
            body = body.rsplit("```", 1)[0]
        text = body
    return text.rstrip("\n") + "\n"


def _git(target: Path, *args: str) -> tuple[int, str, str]:
    """Run a git command in `target`. Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(target),
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git_commit_and_push(target: Path, file_rel: str, finding_id: str,
                         title: str, agent: str) -> int:
    """Stage the single touched file, commit, push to current branch."""
    # 1. Confirm we're on a branch (detached-HEAD push is rude)
    rc, branch, _ = _git(target, "rev-parse", "--abbrev-ref", "HEAD")
    branch = (branch or "").strip()
    if rc != 0 or not branch or branch == "HEAD":
        console.print(
            f"[red]Not on a branch (detached HEAD or git error). "
            f"Refusing to push.[/red]"
        )
        return 2

    # 2. Stage + commit
    rc, _, err = _git(target, "add", file_rel)
    if rc != 0:
        console.print(f"[red]git add failed: {err.strip()}[/red]")
        return 2

    commit_msg = (
        f"AI-applied: {title} (finding {finding_id})\n\n"
        f"Agent: {agent}. Applied via /apply on the PR comment thread.\n"
        f"Co-Authored-By: ai-engineering-workspace <noreply@anthropic.com>"
    )
    rc, out, err = _git(target, "commit", "-m", commit_msg)
    if rc != 0:
        if "nothing to commit" in (out + err).lower():
            console.print("[yellow]No staged changes — nothing to commit.[/yellow]")
            return 0
        console.print(f"[red]git commit failed: {err.strip()}[/red]")
        return 2

    # 3. Push to current branch
    rc, _, err = _git(target, "push", "origin", branch)
    if rc != 0:
        console.print(f"[red]git push failed: {err.strip()}[/red]")
        return 2

    console.print(
        f"[green]✓ Committed + pushed to [bold]{branch}[/bold][/green]  "
        f"[dim](file: {file_rel})[/dim]"
    )
    return 0
