from agents.base import BaseAgent
from models import TaskSpec


class UIUXAgent(BaseAgent):
    name = "UIUXAgent"

    def build_prompt(self, task, diff, file_contents, repo_profile, memory):
        files_text  = self._format_files(file_contents)
        memory_text = self._format_memory(memory)

        return f"""You are a UI/UX code reviewer for a React/TypeScript frontend.

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
- Missing loading states during async API calls
- Missing error states when API calls fail
- Error messages shown only in console, not to the user
- Missing empty states (when list returns no results)
- Missing accessibility attributes (aria-label, role, alt text)
- Form fields missing validation feedback
- Hardcoded text that should be in a constant

## Rules
- Only report issues visible in the changed frontend code
- Do not report backend issues
- Reference specific component names and line numbers

{self._reasoning_instructions()}
"""

    # ----- P4 plan-phase: review_requirement -----

    def build_requirement_prompt(
        self,
        requirement: str,
        repo_profile: dict,
        memory: dict,
    ) -> str:
        return f"""You are a UX-minded frontend engineer reviewing a feature requirement BEFORE any code is written.

## Requirement
{requirement}

## Project context
{self._compact_profile(repo_profile)}

## Your angle (lens)
Identify only **user-experience-relevant** concerns:
- How the end user perceives this feature (clarity, friction, feedback)
- Accessibility (keyboard nav, screen readers, aria-* attributes, color contrast)
- Form design (required vs optional indicators, validation feedback, character limits visible to users)
- Loading / empty / error states
- Mobile vs desktop differences if relevant
- Internationalization / hard-coded strings

## What to produce
- perspective_summary: one sentence on the UX read of this feature.
- clarify_questions: only when you cannot tell what user role uses this, what form/screen, or what user task it supports.
- design_suggestions: actionable UX improvements with priority high/medium/low.
- proposed_criteria: verifiable UX requirements for the eventual contract.
  must_have = breaks the feature for a class of users (e.g. screen reader users).
  should_have = improves perceived quality (e.g. char counter when @Size enforced).
  nice_to_have = polish.

Examples of strong assertions:
- "Textarea has aria-label or associated <label> element."
- "Submit button is disabled while async save is in flight."
- "Validation error from server is rendered next to the field, not as a toast."

Avoid generic UX advice ("make it user-friendly"). Every criterion must be testable.

{self._requirement_output_schema()}
"""
