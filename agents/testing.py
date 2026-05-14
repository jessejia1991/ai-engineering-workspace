from agents.base import BaseAgent
from models import TaskSpec


class TestingAgent(BaseAgent):
    name = "TestingAgent"

    def build_prompt(self, task, diff, file_contents, repo_profile, memory):
        files_text  = self._format_files(file_contents)
        memory_text = self._format_memory(memory)

        return f"""You are a test coverage reviewer for a software engineering team.

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
- Missing tests for new code paths introduced in this diff
- Missing edge case tests (null input, empty list, boundary values)
- Missing error/exception path tests
- Existing tests that may be broken by this change
- Assertions that are too weak (e.g. just checking not null)
- Missing integration tests for new endpoints

## Rules
- Only report gaps that are directly related to code changed in this diff
- Do not report general test improvement suggestions unrelated to the change
- Be specific: name the method or class that needs a test

{self._reasoning_instructions()}
"""

    # ----- P4 plan-phase: review_requirement -----

    def build_requirement_prompt(
        self,
        requirement: str,
        repo_profile: dict,
        memory: dict,
    ) -> str:
        return f"""You are a test strategy lead reviewing a feature requirement BEFORE any code is written.

## Requirement
{requirement}

## Project context
{self._compact_profile(repo_profile)}

## Your angle (lens)
Identify only **testability and coverage** concerns:
- What categories of tests are warranted (unit / integration / e2e / property)
- Boundary conditions and edge cases the implementation will need to handle
- Regressions in existing tests that this change might trigger
- Test infrastructure required (fixtures, factories, test DB state)
- Observable assertions vs. trivial getter/setter tests

## What to produce
- perspective_summary: one sentence on the testing read of this feature.
- clarify_questions: only when you cannot tell what behavior to test (e.g. what's a valid input range).
- design_suggestions: actionable test strategy improvements with priority high/medium/low.
- proposed_criteria: verifiable test-coverage requirements for the eventual contract.
  must_have = the feature is unreviewable without this test (e.g. schema migration round-trip).
  should_have = realistic regression risk if absent.
  nice_to_have = additional confidence.

Examples of strong assertions:
- "Integration test persists Visit with non-null notes and reloads, asserting notes value survives round-trip."
- "Unit test asserts setNotes(null) is allowed and getNotes() returns null."
- "End-to-end test fills the form's notes field and verifies it appears in the visit history view."

Avoid trivial getter/setter unit tests unless the field has non-trivial logic — those are low signal.
Every criterion must be testable (meta-test: an engineer can write a passing assertion for it).

{self._requirement_output_schema()}
"""
