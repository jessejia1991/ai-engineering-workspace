import os
import json
import uuid
import asyncio
from abc import ABC, abstractmethod
from dotenv import load_dotenv
from agents.llm_client import client    # P3: rate-limited HTTP wrapper
from models import AgentFinding, TaskSpec

load_dotenv()

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


class BaseAgent(ABC):
    name: str

    async def review(
        self,
        task: TaskSpec,
        diff: str,
        file_contents: dict,
        repo_profile: dict,
        memory: dict,                                 # ChromaDB retrieval results
        owned_criteria: list[dict] | None = None,     # P4: criteria this agent must verify
    ) -> tuple[list[AgentFinding], dict]:
        """
        Returns (findings, reasoning).
        reasoning contains: codebase_understanding, rejected_candidates,
        confidence, and (when owned_criteria is non-empty) contract_status.
        """
        owned_criteria = owned_criteria or []
        prompt = self.build_prompt(task, diff, file_contents, repo_profile, memory)
        if owned_criteria:
            prompt = prompt + "\n\n" + self._contract_block(owned_criteria)

        try:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=6000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text
            stop_reason = response.stop_reason
            findings, reasoning = self.parse_response(task.task_id, raw)
            reasoning = reasoning or {}
            reasoning["_raw_response"] = raw[:8000]
            reasoning["_stop_reason"] = stop_reason
            # If the agent owned criteria but the response didn't include
            # contract_status, synthesize UNVERIFIED stubs so the
            # renderer doesn't lose criteria silently.
            if owned_criteria and not reasoning.get("contract_status"):
                reasoning["contract_status"] = [
                    {
                        "criterion_id": c.get("id", ""),
                        "status":       "UNVERIFIED",
                        "evidence":     "agent did not emit contract_status for this criterion",
                    }
                    for c in owned_criteria
                ]
            return findings, reasoning

        except Exception as e:
            error_finding = AgentFinding(
                finding_id=str(uuid.uuid4())[:8],
                task_id=task.task_id,
                agent=self.name,
                severity="low",
                category="agent-error",
                title=f"{self.name} failed",
                detail=str(e),
                suggestion="Check agent logs",
                status="failed",
                error=str(e)
            )
            return [error_finding], {}

    @abstractmethod
    def build_prompt(
        self,
        task: TaskSpec,
        diff: str,
        file_contents: dict,
        repo_profile: dict,
        memory: dict,
    ) -> str:
        pass

    def parse_response(self, task_id: str, raw: str) -> tuple[list[AgentFinding], dict]:
        """
        Parse LLM response into (findings, reasoning).
        LLM is asked to return:
        {
          "reasoning": { "codebase_understanding": "...", "rejected_candidates": [...], "confidence_per_finding": {...} },
          "findings": [...]
        }
        Falls back to plain array if LLM returns old format.
        """
        text = raw.strip()

        # Strip markdown code fences
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        # Try parsing as object with reasoning + findings
        reasoning = {}
        findings_raw = []

        try:
            data = json.loads(text)
            if isinstance(data, dict):
                reasoning = data.get("reasoning", {})
                findings_raw = data.get("findings", [])
            elif isinstance(data, list):
                # Fallback: plain array (old format)
                findings_raw = data
        except json.JSONDecodeError:
            # Try to extract JSON array as last resort
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end != -1:
                try:
                    findings_raw = json.loads(text[start:end+1])
                except Exception:
                    return [], {}

        findings = []
        for i, item in enumerate(findings_raw):
            if not isinstance(item, dict):
                continue
            try:
                findings.append(AgentFinding(
                    finding_id=str(uuid.uuid4())[:8],
                    task_id=task_id,
                    agent=self.name,
                    severity=item.get("severity", "low"),
                    category=item.get("category", "general"),
                    title=item.get("title", ""),
                    detail=item.get("detail", ""),
                    suggestion=item.get("suggestion", ""),
                    file=item.get("file"),
                    line=item.get("line"),
                ))
            except Exception:
                continue

        return findings, reasoning

    def _format_memory(self, memory: dict) -> str:
        """Format ChromaDB memory for prompt injection."""
        from memory.vector_store import format_memory_for_prompt
        return format_memory_for_prompt(memory)

    def _format_files(self, file_contents: dict) -> str:
        """Format file contents for prompt."""
        text = ""
        for path, content in file_contents.items():
            text += f"\n### {path}\n```\n{content}\n```\n"
        return text

    def _compact_profile(self, profile: dict) -> str:
        """One-line repo summary for plan-phase prompts (no code yet)."""
        files = profile.get("files", {}) or {}
        return (
            f"Repo: {profile.get('repo_id', 'unknown')} | "
            f"backend={len(files.get('backend', []))}, "
            f"frontend={len(files.get('frontend', []))}, "
            f"test={len(files.get('test', []))}"
        )

    def _contract_block(self, owned_criteria: list[dict]) -> str:
        """
        Append-block injected into the review prompt when the agent
        owns contract criteria (P4 Chunk D). Tells the LLM about its
        owned criteria and asks for a contract_status entry per criterion
        inside the reasoning JSON.
        """
        if not owned_criteria:
            return ""
        lines = []
        for c in owned_criteria:
            lines.append(
                f"  {c.get('id', '?')} [{c.get('priority', '?')}] "
                f"[{c.get('category', '')}] {c.get('assertion', '')}"
            )
        crit_text = "\n".join(lines)
        return f"""
## Contract criteria you own (P4)

You are the agent responsible for verifying these criteria against the diff
and surrounding code. Each one was agreed in the plan phase — do not
re-litigate whether the criterion is reasonable; only assess whether the
implementation in this PR satisfies it.

{crit_text}

For each criterion above, ADD an entry to your reasoning JSON under a new
`contract_status` array — same level as `rejected_candidates`:

  "contract_status": [
    {{
      "criterion_id": "c1",
      "status": "PASS | FAIL | UNVERIFIED",
      "evidence": "Short pointer to what you looked at: file:line, snippet, or '\
why you could not verify in this diff'"
    }}
  ]

Status rules:
- PASS: you found concrete evidence in the diff or unchanged code that the
  assertion holds. Cite the file/line/method in evidence.
- FAIL: you found evidence the assertion is violated. Cite where.
- UNVERIFIED: the diff doesn't cover this aspect (e.g. a backend authz
  criterion when the diff is frontend-only). Explain why in evidence.

Findings you already report can additionally cite a criterion by setting
`finding.criterion_id`, but this is optional. The primary surface is the
`contract_status` array.
"""

    def _reasoning_instructions(self) -> str:
        """Standard reasoning format instructions appended to every agent prompt."""
        return """
## Output format
Return a JSON object with this exact structure:

{
  "reasoning": {
    "codebase_understanding": "Brief description of what you understand about this codebase from the code",
    "rejected_candidates": [
      {
        "issue": "Issue you considered but decided not to report",
        "why_rejected": "Specific reason — reference the code or memory",
        "confidence_to_reject": 0.90
      }
    ],
    "confidence_per_finding": {
      "finding_0": 0.85
    }
  },
  "findings": [
    {
      "severity": "low|medium|high|critical",
      "category": "short-category-string",
      "title": "One line description",
      "detail": "Specific explanation referencing actual code",
      "suggestion": "Concrete fix",
      "file": "relative/path/to/file.java",
      "line": 42
    }
  ]
}

Rules:
- Only report issues with evidence in the actual code
- If no issues found, return empty findings array
- Always populate rejected_candidates to show your reasoning
- Return ONLY the JSON object, no other text
"""

    # ===================================================================
    # P4 Chunk B — plan-phase requirement review
    # ===================================================================
    #
    # Each expert agent (Security/UIUX/Testing/Performance/Delivery) opts
    # in by overriding `build_requirement_prompt`. BugFindingAgent doesn't
    # (no meaningful plan-phase angle — it lives in `review()`). Agents
    # that haven't opted in raise NotImplementedError; the selector
    # filters them out before calling.

    async def review_requirement(
        self,
        requirement: str,
        repo_profile: dict,
        memory: dict | None = None,
    ) -> dict:
        """
        Plan-phase entrypoint. Returns:
            {
              "perspective_summary": "one-line angle on this requirement",
              "clarify_questions":   [...],   # genuine ambiguities only
              "design_suggestions":  [{priority: high|medium|low, ...}, ...],
              "proposed_criteria":   [{priority: must_have|should_have|nice_to_have, ...}, ...],
              "_raw_response":       "...",
              "_stop_reason":        "..."
            }
        On error returns a payload with `_error` set so the synthesizer
        can decide whether to fail or proceed with partial perspectives.
        """
        prompt = self.build_requirement_prompt(requirement, repo_profile, memory or {})

        try:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text
            stop_reason = response.stop_reason
            parsed = self.parse_requirement_response(raw)
            parsed["_raw_response"] = raw[:6000]
            parsed["_stop_reason"]  = stop_reason
            return parsed

        except Exception as e:
            return {
                "perspective_summary": "",
                "clarify_questions":   [],
                "design_suggestions":  [],
                "proposed_criteria":   [],
                "_error":              str(e),
            }

    def build_requirement_prompt(
        self,
        requirement: str,
        repo_profile: dict,
        memory: dict,
    ) -> str:
        """Subclasses opt in by overriding. Default refuses."""
        raise NotImplementedError(
            f"{self.name} does not implement build_requirement_prompt — "
            f"it is not a plan-phase expert."
        )

    def parse_requirement_response(self, raw: str) -> dict:
        """
        Parse review_requirement LLM output. Shared across all expert
        agents — they all return the same schema.
        """
        text = raw.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {
                "perspective_summary": "",
                "clarify_questions":   [],
                "design_suggestions":  [],
                "proposed_criteria":   [],
                "_parse_error":        True,
            }

        if not isinstance(data, dict):
            return {
                "perspective_summary": "",
                "clarify_questions":   [],
                "design_suggestions":  [],
                "proposed_criteria":   [],
                "_parse_error":        True,
            }

        def _str_list(key: str) -> list[str]:
            v = data.get(key, [])
            return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

        def _suggestions() -> list[dict]:
            out = []
            for s in data.get("design_suggestions", []) or []:
                if not isinstance(s, dict):
                    continue
                priority = s.get("priority", "medium")
                if priority not in ("high", "medium", "low"):
                    priority = "medium"
                out.append({
                    "priority":  priority,
                    "category":  s.get("category", "general"),
                    "suggestion": s.get("suggestion", "").strip(),
                    "rationale":  s.get("rationale", "").strip(),
                })
            return out

        def _criteria() -> list[dict]:
            out = []
            for c in data.get("proposed_criteria", []) or []:
                if not isinstance(c, dict):
                    continue
                priority = c.get("priority", "should_have")
                if priority not in ("must_have", "should_have", "nice_to_have"):
                    priority = "should_have"
                out.append({
                    # owner_agent + id are filled in by the orchestrator,
                    # not the agent itself — separation of concerns.
                    "priority":         priority,
                    "category":         c.get("category", "general"),
                    "assertion":        c.get("assertion", "").strip(),
                    "rationale":        c.get("rationale", "").strip(),
                    "suggested_check":  c.get("suggested_check", "manual"),
                })
            return out

        return {
            "perspective_summary": data.get("perspective_summary", "").strip(),
            "clarify_questions":   _str_list("clarify_questions"),
            "design_suggestions":  _suggestions(),
            "proposed_criteria":   _criteria(),
        }

    def _requirement_output_schema(self) -> str:
        """Shared output-schema fragment for plan-phase prompts."""
        return """
## Output format
Return a JSON object with this exact structure:

{
  "perspective_summary": "One sentence on how your angle reads this requirement.",
  "clarify_questions": [
    "Only include questions that are genuinely ambiguous from your angle. Empty list if none."
  ],
  "design_suggestions": [
    {
      "priority":   "high | medium | low",
      "category":   "short-tag",
      "suggestion": "Specific design improvement, one sentence.",
      "rationale":  "Why this matters from your angle."
    }
  ],
  "proposed_criteria": [
    {
      "priority":        "must_have | should_have | nice_to_have",
      "category":        "short-tag",
      "assertion":       "A verifiable statement about the implementation, e.g. 'notes field has @Size(max <= 2000)'.",
      "rationale":       "Why this matters.",
      "suggested_check": "static-analysis | runtime-test | manual"
    }
  ]
}

Rules:
- Be selective. Don't pile on nice-to-haves. Two strong must_haves beat ten low-confidence suggestions.
- clarify_questions: only true ambiguities you cannot resolve from the requirement + repo profile. Leave empty if you can proceed.
- proposed_criteria.assertion must be testable — observable in code or runtime, not aesthetic.
- Return ONLY the JSON object. No preamble. No markdown fence.
"""
