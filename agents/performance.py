from agents.base import BaseAgent
from models import TaskSpec


class PerformanceAgent(BaseAgent):
    name = "PerformanceAgent"

    def build_prompt(self, task, diff, file_contents, repo_profile, memory):
        files_text  = self._format_files(file_contents)
        memory_text = self._format_memory(memory)

        return f"""You are a performance reviewer for a software engineering team.

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
- N+1 query patterns (fetching related entities in a loop)
- Missing pagination on list endpoints that could return large datasets
- Expensive operations inside loops
- Missing database indexes implied by new query patterns
- Large payload sizes returned by API endpoints
- Unnecessary re-renders in React components (missing useMemo/useCallback)
- Blocking operations that should be async

## Rules
- Only report issues with clear evidence in the changed code
- Do not speculate about performance without code evidence
- Be specific about the query or loop causing the issue

{self._reasoning_instructions()}
"""
