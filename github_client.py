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

        # Post individual findings as inline review comments
        valid_findings = [f for f in findings if f.status == "ok" and f.file and f.line]
        comments_posted = 0

        for finding in valid_findings:
            emoji = SEVERITY_EMOJI.get(finding.severity, "⚪")
            fid   = finding.finding_id
            # The footer surfaces the finding_id and the `/apply` syntax
            # so a human (or the workflow's issue_comment trigger) can
            # auto-apply the suggested fix via cli/apply_cmd.py.
            apply_hint = (
                f"\n\n[ai-eng · finding `{fid}`] "
                f"_Reply with `/apply {fid}` to auto-fix, "
                f"or `/apply {fid} <extra instructions>` to refine._"
            )
            body  = (
                f"{emoji} **[{finding.severity.upper()}] {finding.title}**\n\n"
                f"**Agent:** {finding.agent}  |  **Category:** `{finding.category}`\n\n"
                f"{finding.detail}\n\n"
                f"**Suggestion:** {finding.suggestion}"
                f"{apply_hint}"
            )
            try:
                pr.create_review_comment(
                    body=body,
                    commit=last_commit,
                    path=finding.file,
                    line=finding.line,
                )
                comments_posted += 1
            except GithubException as e:
                # Line may not exist in diff — fall back to PR comment
                try:
                    pr.create_issue_comment(
                        f"{emoji} **[{finding.severity.upper()}] {finding.title}**\n"
                        f"File: `{finding.file}:{finding.line}`\n\n"
                        f"{finding.detail}\n\n"
                        f"**Suggestion:** {finding.suggestion}"
                        f"{apply_hint}"
                    )
                    comments_posted += 1
                except Exception:
                    pass

        # Post RiskReport summary as PR review
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
