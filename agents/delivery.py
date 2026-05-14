"""
DeliveryAgent — release / migration / backward-compatibility lens.

Plan-phase only. Does not implement build_prompt for review() because
delivery concerns surface as discrete contract criteria (rollback,
schema safety, feature-flag presence) rather than diff-level findings.
The review side checks delivery criteria the same way as any other
agent's criteria (PASS / FAIL / UNVERIFIED) via the standard contract
status mechanism in P4 Chunk D, no additional review-side prompt needed.
"""

from agents.base import BaseAgent


class DeliveryAgent(BaseAgent):
    name = "DeliveryAgent"

    def build_prompt(self, task, diff, file_contents, repo_profile, memory):
        # Not used in review() path — DeliveryAgent only contributes
        # plan-phase criteria. The review-side contract verifier reads
        # the criteria DeliveryAgent owns and applies them to the diff
        # without needing a separate review_requirement-style prompt.
        raise NotImplementedError(
            "DeliveryAgent only operates in the plan phase. "
            "Its review-side contract verification is handled by the "
            "standard contract verifier in P4 Chunk D."
        )

    # ----- P4 plan-phase: review_requirement -----

    def build_requirement_prompt(
        self,
        requirement: str,
        repo_profile: dict,
        memory: dict,
    ) -> str:
        return f"""You are a release engineer reviewing a feature requirement BEFORE any code is written.

## Requirement
{requirement}

## Project context
{self._compact_profile(repo_profile)}

## Your angle (lens)
Identify only **delivery / release / operational** concerns:
- Schema migration safety (additive vs destructive, nullable defaults, online vs offline)
- Rollback path (forward migration + DROP COLUMN / inverse script + data preservation)
- Backward compatibility (old clients hitting new API, new clients hitting old API during rollout)
- Feature flag candidacy (does this risk justify a flag, or is it small enough to ship behind no toggle)
- Observability (logs/metrics that confirm the feature is live and working)
- Dependency / version impact (does this touch third-party libs that require coordinated release)

## What to produce
- perspective_summary: one sentence on the delivery read of this feature.
- clarify_questions: only when you cannot tell deployment cadence, rollback expectations, or migration strategy.
- design_suggestions: actionable delivery improvements with priority high/medium/low.
- proposed_criteria: verifiable delivery requirements for the eventual contract.
  must_have = unsafe to merge without (e.g. destructive migration without rollback).
  should_have = standard release hygiene that prevents 2am pages.
  nice_to_have = polish.

Examples of strong assertions:
- "Schema migration adds the column as NULLABLE so existing rows continue to load."
- "Migration script ships with a paired rollback script that DROPs the new column."
- "Feature is exposed behind a flag that defaults off in production."
- "Release notes mention the new field so existing API consumers can update their schemas."

Be proportional: an additive nullable column on a small Spring app doesn't need a feature flag.
A breaking API change to authentication absolutely does. Match must_have intensity to actual risk.

{self._requirement_output_schema()}
"""
