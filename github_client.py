import os
from github import Github, GithubException
from dotenv import load_dotenv
from models import AgentFinding, RiskReport

load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "")

SEVERITY_EMOJI = {
    "critical": "🔴",
    "high":     "🟠",
    "medium":   "🟡",
    "low":      "🔵",
}

# Hidden marker on the Apply Menu comment. cli/apply_cmd.py locates the
# menu by scanning PR comments for this string, so a bare `/apply` can
# resolve which findings were ticked. Keep it in sync with apply_cmd.py.
REVIEW_SUMMARY_MARKER = "<!-- ai-eng-review-summary -->"


def get_repo():
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN not set in .env")
    if not GITHUB_REPO:
        raise RuntimeError("GITHUB_REPO not set in .env")
    g = Github(GITHUB_TOKEN)
    return g.get_repo(GITHUB_REPO)


def get_pr_description(pr_number: int) -> str:
    """
    Return PR title + body. Used by P4 auto-match to semantically locate
    the corresponding plan_graph in planning_memory. Returns empty string
    on any failure — caller falls back to generic review.
    """
    try:
        repo = get_repo()
        pr = repo.get_pull(pr_number)
        title = pr.title or ""
        body  = pr.body or ""
        return f"{title}\n\n{body}".strip()
    except (GithubException, RuntimeError):
        return ""


def is_post_allowed_repo() -> tuple[bool, str]:
    """
    Defense-in-depth allowlist for write paths. Returns (allowed, reason).
    Even if a caller sets `REVIEW_POST_COMMENTS=true` or passes --post,
    we refuse to post unless GITHUB_REPO is explicitly listed in
    REVIEW_ALLOWED_REPOS (comma-separated). Discovered the hard way:
    accidentally posted to a public OSS repo during scalability testing
    because env was switched but a CLI flag wasn't.
    """
    repo = os.environ.get("GITHUB_REPO", "")
    if not repo:
        return False, "GITHUB_REPO env var is not set"
    allowed = os.environ.get("REVIEW_ALLOWED_REPOS", "")
    if not allowed:
        return False, (
            "REVIEW_ALLOWED_REPOS env var is not set; refusing to write to "
            f"'{repo}' as a safety default. Add the repo to "
            "REVIEW_ALLOWED_REPOS (comma-separated) to enable posting."
        )
    allowed_list = [r.strip() for r in allowed.split(",") if r.strip()]
    if repo not in allowed_list:
        return False, (
            f"GITHUB_REPO='{repo}' is not in REVIEW_ALLOWED_REPOS "
            f"({allowed_list}). Refusing to write."
        )
    return True, ""


def post_review_comments(
    pr_number: int,
    findings: list[AgentFinding],
    risk_report: RiskReport,
) -> bool:
    """
    Post findings as PR review comments on specific file+line.
    Post overall RiskReport as a PR review summary.
    Returns True if successful, False if blocked or failed.

    Safety: refuses unless GITHUB_REPO is in REVIEW_ALLOWED_REPOS env
    (defense-in-depth — caller-level --post flag is not enough on its own).
    """
    allowed, reason = is_post_allowed_repo()
    if not allowed:
        print(f"[post_review_comments] BLOCKED by allowlist: {reason}")
        return False

    try:
        repo = get_repo()
        pr   = repo.get_pull(pr_number)

        # Get latest commit for review comments
        commits     = list(pr.get_commits())
        last_commit = commits[-1] if commits else None

        if not last_commit:
            print("No commits found on PR")
            return False

        # Tier the findings so the PR doesn't drown in inline comments:
        #   critical / high -> inline review comment on the code line
        #   medium / low    -> Apply Menu only
        # Every ok finding still shows up in the Apply Menu checkbox list,
        # which is the single surface a reviewer uses to /apply fixes.
        ok_findings = [f for f in findings if f.status == "ok"]
        tier1 = [f for f in ok_findings if f.severity in ("critical", "high")]
        comments_posted = 0

        for finding in tier1:
            emoji = SEVERITY_EMOJI.get(finding.severity, "⚪")
            fid   = finding.finding_id
            apply_hint = (
                f"\n\n[ai-eng · finding `{fid}`] "
                f"_Reply `/apply {fid}` to auto-fix this one, or tick it in "
                f"the Apply Menu comment and `/apply` the batch._"
            )
            body  = (
                f"{emoji} **[{finding.severity.upper()}] {finding.title}**\n\n"
                f"**Agent:** {finding.agent}  |  **Category:** `{finding.category}`\n\n"
                f"{finding.detail}\n\n"
                f"**Suggestion:** {finding.suggestion}"
                f"{apply_hint}"
            )
            posted = False
            if finding.file and finding.line:
                try:
                    pr.create_review_comment(
                        body=body,
                        commit=last_commit,
                        path=finding.file,
                        line=finding.line,
                    )
                    posted = True
                except GithubException:
                    posted = False
            if not posted:
                # Line not in diff (or no location) — fall back to PR comment.
                loc = (
                    f"`{finding.file}:{finding.line}`" if finding.file
                    else "_(no file location)_"
                )
                try:
                    pr.create_issue_comment(
                        f"{emoji} **[{finding.severity.upper()}] {finding.title}**\n"
                        f"File: {loc}\n\n"
                        f"{finding.detail}\n\n"
                        f"**Suggestion:** {finding.suggestion}"
                        f"{apply_hint}"
                    )
                    posted = True
                except Exception:
                    pass
            if posted:
                comments_posted += 1

        # Post RiskReport summary as a PR review — this is the merge verdict.
        summary = _format_risk_summary(risk_report, findings)

        event = "REQUEST_CHANGES" if risk_report.merge_recommendation != "approve" else "APPROVE"

        try:
            pr.create_review(
                body=summary,
                event=event,
                commit=last_commit,
            )
        except GithubException:
            # Fallback: post as issue comment
            pr.create_issue_comment(summary)

        # Post the Apply Menu — one issue comment, a checkbox per ok finding.
        # `/apply` (cli/apply_cmd.py) reads the ticked boxes from this comment.
        if ok_findings:
            try:
                pr.create_issue_comment(_format_apply_menu(ok_findings))
            except Exception as e:
                print(f"Apply Menu post failed: {e}")

        return True

    except Exception as e:
        print(f"GitHub posting failed: {e}")
        return False


def _format_risk_summary(risk_report: RiskReport, findings: list[AgentFinding]) -> str:
    valid    = [f for f in findings if f.status == "ok"]
    by_sev   = {}
    for f in valid:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1

    risk_emoji = SEVERITY_EMOJI.get(risk_report.overall_risk, "⚪")

    lines = [
        f"## {risk_emoji} AI Engineering Workspace — Review Report",
        f"",
        f"**Overall Risk:** {risk_report.overall_risk.upper()}  |  "
        f"**Recommendation:** {risk_report.merge_recommendation}",
        f"",
        f"### Findings Summary",
        f"| Severity | Count |",
        f"|----------|-------|",
    ]

    for sev in ["critical", "high", "medium", "low"]:
        count = by_sev.get(sev, 0)
        if count > 0:
            lines.append(f"| {SEVERITY_EMOJI[sev]} {sev.capitalize()} | {count} |")

    lines += ["", "### Agents Run"]
    for agent in risk_report.agents_run:
        agent_data = risk_report.by_agent.get(agent, {})
        lines.append(
            f"- **{agent}**: {agent_data.get('count', 0)} finding(s), "
            f"risk: {agent_data.get('risk', 'low')}"
        )

    if risk_report.agents_skipped:
        lines += ["", "### Agents Skipped"]
        for agent, reason in risk_report.agents_skipped.items():
            lines.append(f"- {agent}: {reason}")

    if risk_report.top_actions:
        lines += ["", "### Top Actions Required"]
        for action in risk_report.top_actions:
            lines.append(f"- {action}")

    lines += [
        "",
        "---",
        "*Generated by AI Engineering Workspace — "
        "findings require human review before merging*"
    ]

    return "\n".join(lines)


def _format_apply_menu(findings: list[AgentFinding]) -> str:
    """
    Render the Apply Menu — one PR comment, a checkbox per ok finding. A
    reviewer ticks the boxes they want and comments `/apply`; the
    REVIEW_SUMMARY_MARKER on the first line lets cli/apply_cmd.py locate
    this comment and read which boxes are checked.
    """
    lines = [
        REVIEW_SUMMARY_MARKER,
        "## 🤖 AI Review — Apply Menu",
        "",
        "Tick the fixes you want, then comment **`/apply`** on this PR — "
        "the AI commits the checked fixes to the PR branch.",
        "",
        "_Prefer to pick explicitly? Comment `/apply <finding_id> "
        "[<finding_id> …]`. Add free text after the ids to refine, "
        "e.g. `/apply a1b2c3d4 keep the change minimal`._",
        "",
    ]
    for f in findings:
        emoji = SEVERITY_EMOJI.get(f.severity, "⚪")
        if f.file and f.line:
            loc = f"  `{f.file}:{f.line}`"
        elif f.file:
            loc = f"  `{f.file}`"
        else:
            loc = ""
        lines.append(
            f"- [ ] `{f.finding_id}` {emoji} **{f.severity.upper()}** "
            f"{f.title}{loc} — _{f.agent}_"
        )
    lines += ["", "---", "*Generated by AI Engineering Workspace*"]
    return "\n".join(lines)
