from agents.base import BaseAgent
from models import TaskSpec


class RefactoringAgent(BaseAgent):
    """
    Method- and file-scoped code-quality review. Distinct scope from
    ArchitectureAgent (module/layer concerns) and BugFindingAgent
    (correctness). Refactoring's job is "the code works, but a future
    reader / maintainer would struggle".
    """

    name = "RefactoringAgent"

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

        return f"""You are a refactoring reviewer for a software engineering team.

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

## What to look for (method / file scope)
- Long methods that have multiple unrelated responsibilities — propose an extract.
- Duplicated logic across nearby methods or files — propose a shared helper.
- Unclear names: variables/methods whose purpose isn't obvious from the name.
- Dead code: unreachable branches, unused parameters, unused private methods.
- Type-safety upgrades: parameters/returns typed as `Object` / `any` / raw map that should be a proper type.
- Misplaced responsibility within a class: a helper method that's clearly someone else's job.
- Testability: a method that's hard to test because of a hidden static dependency or a private setter — propose dependency injection.
- Comments that should be code: explanatory comments that disappear once you rename the method or extract a class.

## What NOT to look for here
- Layering / module-boundary issues — that's the ArchitectureAgent's job.
- Bugs or correctness issues — that's BugFindingAgent's job.
- Performance optimization — that's PerformanceAgent's job (unless the refactor is the perf fix itself).
- Missing tests — that's Testing / TestGeneration's job.

If your only finding is "this class is in the wrong package", that's architecture, not refactoring.

## Rules
- Suggestions must be concrete: name the extracted method, point at the duplicated block, propose the new type.
- Severity should reflect how much pain the current state causes a reader, not how badly you want the refactor. Most refactorings are `low` or `medium`.
- Skip findings where the diff is too small to judge ("could be cleaner" without specifics) — that's noise.
- If memory shows this style was rejected before (e.g., team prefers verbose names over short), document in rejected_candidates.

{self._reasoning_instructions()}
"""
