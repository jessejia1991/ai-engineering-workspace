from agents.base import BaseAgent
from models import TaskSpec


class BugFindingAgent(BaseAgent):
    name = "BugFindingAgent"

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

        return f"""You are a bug-finding code reviewer for a software engineering team.

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
- Null pointer / NullPointerException risks
- Missing null checks on return values from DB or API calls
- Edge cases not handled (empty list, zero, negative numbers)
- Logic errors in conditionals
- Missing error handling on external calls
- API response errors shown only in console, not to the user
- Incorrect data transformation or off-by-one errors

## Rules
- Only report issues with clear evidence in the actual code
- Do not report style issues or minor improvements
- Check memory: if a similar finding was rejected before, document why in rejected_candidates

{self._reasoning_instructions()}
"""
