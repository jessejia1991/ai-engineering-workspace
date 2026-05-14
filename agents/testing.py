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
