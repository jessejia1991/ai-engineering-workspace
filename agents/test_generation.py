from agents.base import BaseAgent
from models import TaskSpec


class TestGenerationAgent(BaseAgent):
    """
    Proposes new test cases for the changed code. Distinct from
    TestingAgent, which reviews test *coverage* of existing tests.
    This agent's findings each contain a runnable test in the
    `suggestion` field — the user is expected to paste them into the
    project's test directory and run. No execution here (sandboxing
    + cross-language test runners are out of scope for this take-home;
    documented in §16.x as future work).

    Output language follows the diff: Java (JUnit 5) for *.java, TS/JS
    (Jest / Vitest depending on what the repo already uses) for
    *.ts/*.tsx/*.js/*.jsx, Python (pytest) for *.py. If the diff is
    multi-language, pick the language of the file with the most logic
    in the diff.
    """

    name = "TestGenerationAgent"

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

        return f"""You are a test-generation agent for a software engineering team.

Your job is to propose **new test cases** for the changed code — not to
review existing tests. Each test you propose must include runnable code
that compiles in the project's existing test framework.

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

## What to produce
For each piece of new behavior introduced by the diff, propose 1-3 test
cases covering:
- Happy path: the most common successful invocation.
- Edge case: empty / null / boundary value relevant to the diff.
- Error case: invalid input, dependency failure, or invariant violation.

Skip test cases that the existing test suite obviously already covers
(visible in `repo_profile.files.test` or in unchanged test files).

## Output rules
- Severity: use `low` for happy-path-only gaps, `medium` for missing edge,
  `high` for a missing test on a known risky operation (auth, payment,
  data deletion).
- `category` must be `test-gap`.
- `title` is one line: "Missing test: <what the test verifies>".
- `detail` explains why this test matters.
- `suggestion` is the complete test code — class declaration if needed,
  imports if not obvious, one test method body. Pick the framework the
  repo already uses (JUnit 5 / Jest / Vitest / pytest) — do not introduce
  a new framework.
- `file` should be the proposed test file path (e.g.,
  `src/test/java/.../VisitTest.java`); align with the repo's existing
  test layout if the changed code is in `src/main/java`.

## Rules
- Do not propose tests for code the diff didn't change.
- Do not propose tests that duplicate visible existing coverage.
- If the diff is pure refactor (no behavior change) and existing tests
  already cover the behavior, return zero findings — say so in
  `rejected_candidates` so a reviewer sees you considered it.

{self._reasoning_instructions()}
"""
