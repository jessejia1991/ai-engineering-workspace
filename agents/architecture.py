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
        ops_block   = self._format_ops_readiness(repo_profile)

        return f"""You are an architecture-review reviewer for a software engineering team.

{ops_block}

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

## Operational readiness — a special checklist item
If the "Operational readiness" block above says **NO health endpoint
detected** AND the runtime is a deployable backend (Spring Boot, Express,
FastAPI, etc.), you MUST emit one finding with:
  - severity: high
  - category: ops-readiness
  - title: "Missing health-check endpoint — required for safe CI/CD"
  - detail: explains that the current code has no `/health`, `/healthz`,
    `/actuator/health`, or equivalent, so CD pipelines cannot verify the
    service is live after deploy
  - suggestion: concrete framework-specific path. For Spring Boot, recommend
    adding `spring-boot-starter-actuator` to `pom.xml` and confirming
    `/actuator/health` is exposed. For Express, recommend adding a tiny
    `GET /health` handler.

This finding is independent of the diff — emit it on every review until
the runtime detection picks up a health endpoint. It is the single most
common CI/CD readiness gap and the reviewer wants it surfaced.

## Rules
- Only report issues with clear evidence in the actual code or its location.
- Cite specific file paths, package boundaries, or call chains.
- Do not report hypothetical "what if the codebase grows" concerns.
- If memory shows this pattern was rejected before, document why in rejected_candidates.

{self._reasoning_instructions()}
"""

    def _format_ops_readiness(self, repo_profile: dict) -> str:
        """Surface the runtime-detection signals that matter for the
        ops-readiness rule below."""
        runtime = repo_profile.get("runtime") or {}
        frameworks = runtime.get("frameworks") or []
        port = runtime.get("port")
        hp = runtime.get("health_endpoint")
        lines = ["## Operational readiness signals (from scan)"]
        lines.append(f"  - frameworks: {', '.join(frameworks) or 'unknown'}")
        if port:
            lines.append(f"  - server port: {port}")
        if hp:
            lines.append(f"  - health endpoint: {hp} (good — verify pipeline can probe it)")
        else:
            lines.append("  - health endpoint: NO health endpoint detected — see operational-readiness checklist below")
        return "\n".join(lines)

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
