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
