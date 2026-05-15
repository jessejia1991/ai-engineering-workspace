from agents.base import BaseAgent
from models import TaskSpec


class ArchitectureAgent(BaseAgent):
    """
    Review-side: critiques structural concerns on a diff — layering,
    module boundaries, dependency direction, abstraction leaks. Distinct
    from RefactoringAgent (which is method/file-scoped quality) and
    SecurityAgent (which is correctness/risk).

    Plan-side: gives an architecture lens on the requirement before
    decomposition — opt-in via build_requirement_prompt so the multi-
    expert plan phase can ask "does this requirement imply a new module
    boundary, a new layer, a cross-cutting concern" early.
    """

    name = "ArchitectureAgent"

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

        return f"""You are an architecture-review reviewer for a software engineering team.

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

## What to look for (structural concerns only)
- Layering violations: controller importing repository directly, model knowing about UI, infrastructure leaking into domain.
- Wrong file location: business logic put into utils, persistence put into controllers, view models in core packages.
- Tight coupling introduced by the change: new direct dependencies that should go through an interface or abstract type.
- Missing or broken abstraction: hard-coded values that should be config, duplicated structure that signals a missing base type.
- Circular import risk (new edge in the file dependency graph that closes a cycle).
- New cross-cutting concern (auth, transactions, caching, logging) being inlined into one call site instead of going through the existing aspect/middleware.
- Public API shape changes that affect callers outside the diff.

## What NOT to look for here
- Function-level smells (long methods, naming, duplication WITHIN a file) — that's the RefactoringAgent's job.
- Bugs, null risk, edge cases — that's BugFindingAgent's job.
- Security or perf concerns — that's Security/Performance's job.
- Missing tests — that's Testing/TestGeneration's job.

If your only finding is "this method is too long" or "this name is unclear", do not report — leave it to Refactoring.

## Rules
- Only report issues with clear evidence in the actual code or its location.
- Cite specific file paths, package boundaries, or call chains.
- Do not report hypothetical "what if the codebase grows" concerns.
- If memory shows this pattern was rejected before, document why in rejected_candidates.

{self._reasoning_instructions()}
"""

    # ----- P4 plan-phase: review_requirement -----

    def build_requirement_prompt(
        self,
        requirement: str,
        repo_profile: dict,
        memory: dict,
    ) -> str:
        return f"""You are an architecture reviewer for a feature requirement, BEFORE any code is written.

## Requirement
{requirement}

## Project context
{self._compact_profile(repo_profile)}

## Your angle (lens)
Identify only **structural** concerns in this feature:
- Does this requirement imply a new module boundary or a new layer? Should it?
- Which existing package/layer should own this code? Risk of splitting it across packages.
- Cross-cutting concerns (auth, logging, transactions, caching) that should be designed once, not per-call-site.
- Public API changes that propagate to callers — should there be a versioned interface?
- New external dependencies (DB schema migration, third-party API, message broker) and their lifecycle.

## What to produce
- perspective_summary: one sentence on the architectural read of this feature.
- clarify_questions: only when you cannot tell where ownership should sit or how data flows. Skip if obvious.
- design_suggestions: actionable structural improvements with priority high/medium/low.
- proposed_criteria: verifiable structural requirements for the eventual contract.
  must_have = layering/ownership rule that, if violated, would force a redesign.
  should_have = boundary discipline that catches drift early.
  nice_to_have = naming / packaging hygiene.

Examples of strong assertions:
- "Notes-feature code lives in the existing `pet` package, not a new top-level `notes` package."
- "Frontend reads notes via the existing PetController endpoint; no new controller is introduced."
- "Migration script is reversible (defines DROP COLUMN in down())."

Avoid generic platitudes ("good architecture"). Every criterion must be testable by looking at the resulting code.

{self._requirement_output_schema()}
"""
