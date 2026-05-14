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
        reflection: list,
    ) -> str:

        files_text = ""
        for path, content in file_contents.items():
            files_text += f"\n### {path}\n```\n{content}\n```\n"

        corrections = self._format_corrections(repo_profile.get("corrections", []))
        history = self._format_reflection(reflection)

        return f"""You are a security code reviewer. Your job is to find real security issues in this code change.

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
- Missing input validation (e.g. no @Valid, no @NotNull, no @Size on entity fields)
- Missing authentication or authorization checks
- Unsafe handling of user-supplied data
- Exposed sensitive data in responses
- SQL injection risks
- Missing error handling that could leak stack traces
- Hardcoded secrets or credentials

## Instructions
- Only report issues you can see evidence of in the actual code
- Do not report hypothetical issues
- Do not report issues already handled correctly
- If you find no issues, return an empty array

Return a JSON array of findings. Each finding must have:
- severity: "low" | "medium" | "high" | "critical"
- category: short string e.g. "input-validation", "auth", "data-exposure"
- title: one line description
- detail: specific explanation referencing actual code
- suggestion: concrete fix
- file: relative file path (if applicable)
- line: line number (if applicable)

Return ONLY the JSON array, no other text.
Example:
[
  {{
    "severity": "high",
    "category": "input-validation",
    "title": "Missing @Valid on POST /api/owners request body",
    "detail": "OwnerRestController.addOwner() at line 42 binds @RequestBody without @Valid. Fields like firstName and lastName have no length constraints, allowing malformed data.",
    "suggestion": "Add @Valid to the @RequestBody parameter. Add @Size(max=30) and @NotBlank to entity fields.",
    "file": "src/main/java/org/springframework/samples/petclinic/rest/controller/OwnerRestController.java",
    "line": 42
  }}
]
"""
