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

    # ----- P4 plan-phase: review_requirement -----

    def build_requirement_prompt(
        self,
        requirement: str,
        repo_profile: dict,
        memory: dict,
    ) -> str:
        return f"""You are a performance engineer reviewing a feature requirement BEFORE any code is written.

## Requirement
{requirement}

## Project context
{self._compact_profile(repo_profile)}

## Your angle (lens)
Identify only **performance and scalability** concerns:
- Query patterns implied by the feature (N+1 risk, missing indexes, full-table scans)
- Payload size / serialization cost (unbounded text fields, nested entity expansion)
- Hot-path impact (validation, logging, sync work in critical loops)
- Caching opportunities and invalidation hazards
- Frontend rendering cost (large lists, missing pagination, unnecessary re-renders)

## What to produce
- perspective_summary: one sentence on the performance read of this feature.
- clarify_questions: only when you cannot tell expected volume, frequency, or whether this is on a hot path.
- design_suggestions: actionable performance improvements with priority high/medium/low.
- proposed_criteria: verifiable performance requirements for the eventual contract.
  must_have = clear regression on hot path or unbounded growth.
  should_have = measurable improvement with cheap engineering cost.
  nice_to_have = micro-optimization.

Examples of strong assertions:
- "Notes column has a length cap that fits in a single VARCHAR page (e.g. @Size(max=2000))."
- "Pet list endpoint paginates with default page size <= 50."
- "Visit form does not re-fetch the full Pet list on every keystroke."

Be honest about scale: petclinic-style apps don't have a hot path in the netflix sense.
If the feature is straightforwardly low-traffic and small-payload, your perspective_summary can say so
and your proposed_criteria can be empty or nice_to_have only. Don't manufacture concerns.

{self._requirement_output_schema()}
"""
