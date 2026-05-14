import os
import json
import uuid
import asyncio
from abc import ABC, abstractmethod
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from models import AgentFinding, TaskSpec

load_dotenv()

client = AsyncAnthropic()
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


class BaseAgent(ABC):
    name: str

    async def review(
        self,
        task: TaskSpec,
        diff: str,
        file_contents: dict,
        repo_profile: dict,
        memory: dict,           # ChromaDB retrieval results
    ) -> tuple[list[AgentFinding], dict]:
        """
        Returns (findings, reasoning).
        reasoning contains: codebase_understanding, rejected_candidates, confidence.
        """
        prompt = self.build_prompt(task, diff, file_contents, repo_profile, memory)

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
