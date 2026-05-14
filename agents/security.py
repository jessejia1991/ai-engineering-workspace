from agents.base import BaseAgent
from models import TaskSpec


class SecurityAgent(BaseAgent):
    name = "SecurityAgent"

    def build_prompt(
        self,
        task: TaskSpec,
        diff: str,
        file_contents: dict,
        repo_profile: dict,
        memory: dict,
    ) -> str:

        files_text  = self._format_files(file_contents)
        memory_text = self._format_memory(memory)

        return f"""You are a security code reviewer for a software engineering team.

## Relevant memory from past reviews
{memory_text}

## Task
{task.description}

## Changed files
{files_text}

## Git diff
```diff
{diff}
```

## What to look for
- Missing input validation (@Valid, @NotNull, @NotBlank, @Size on entity fields)
- Missing authentication or authorization checks
- Unsafe handling of user-supplied data
- Exposed sensitive data or stack traces in responses
- SQL injection risks
- Hardcoded secrets or credentials
- Missing error handling that leaks internal details

## Rules
- Only report issues with clear evidence in the actual code
- Do not report hypothetical issues
- Do not repeat issues already present in memory as ACCEPTED
- Check rejected_candidates: if memory shows this pattern was rejected before, explain why you are or are not reporting it again

{self._reasoning_instructions()}
"""

    # ----- P4 plan-phase: review_requirement -----

    def build_requirement_prompt(
        self,
        requirement: str,
        repo_profile: dict,
        memory: dict,
    ) -> str:
        return f"""You are a security architect reviewing a feature requirement BEFORE any code is written.

## Requirement
{requirement}

## Project context
{self._compact_profile(repo_profile)}

## Your angle (lens)
Identify only **security-relevant** concerns in this feature:
- Attack surfaces opened by the new code path (input validation, injection, XSS, deserialization)
- Authentication / authorization gaps
- Sensitive data handling, logging, exposure
- Compliance constraints (e.g. data retention, PII)
- Trust-boundary crossings

## What to produce
- perspective_summary: one sentence on the security read of this feature.
- clarify_questions: only when you cannot tell who can write / where data flows / what schema.
  Do NOT ask "are there security concerns" — that is your job to identify.
- design_suggestions: actionable security improvements with priority high/medium/low.
- proposed_criteria: verifiable security requirements for the eventual contract.
  must_have = compliance / clear vulnerability prevention.
  should_have = defense-in-depth that you'd push for in code review.
  nice_to_have = audit hygiene / observability.

Examples of strong assertions:
- "Notes field is sanitized for XSS at render time (no raw HTML)."
- "Migration script does not log the value of secrets in plain text."
- "API endpoint enforces authentication before write."

Avoid generic platitudes ("be secure"). Every criterion must be testable.

{self._requirement_output_schema()}
"""
