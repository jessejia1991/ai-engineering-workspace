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
        reflection: list,
    ) -> str:

        files_text = ""
        for path, content in file_contents.items():
            files_text += f"\n### {path}\n```\n{content}\n```\n"

        corrections = self._format_corrections(repo_profile.get("corrections", []))
        history = self._format_reflection(reflection)

        return f"""You are a bug-finding code reviewer. Your job is to find real bugs and logic errors in this code change.

## Past corrections about this codebase
These are things we previously misunderstood — do not repeat these mistakes:
{corrections}

## Your review history on this repo
{history}

## Task
{task.description}

## Changed files content
{files_text}

## Git diff
```diff
{diff}
```

## What to look for
- Null pointer / NullPointerException risks
- Missing null checks on return values from database or API calls
- Edge cases not handled (empty list, zero, negative numbers)
- Async/concurrency issues
- Logic errors in conditionals
- Missing error handling on external calls
- Incorrect data transformation
- Off-by-one errors
- API response errors shown only in console, not to the user

## Instructions
- Only report issues you can see evidence of in the actual code
- Do not report style issues or minor improvements
- Do not report issues already handled correctly
- If you find no issues, return an empty array

Return a JSON array of findings. Each finding must have:
- severity: "low" | "medium" | "high" | "critical"
- category: short string e.g. "null-handling", "error-handling", "logic-error"
- title: one line description
- detail: specific explanation referencing actual code
- suggestion: concrete fix
- file: relative file path (if applicable)
- line: line number (if applicable)

Return ONLY the JSON array, no other text.
"""
