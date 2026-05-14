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
