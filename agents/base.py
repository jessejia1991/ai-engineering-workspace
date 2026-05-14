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
        reflection: list,
    ) -> list[AgentFinding]:
        prompt = self.build_prompt(task, diff, file_contents, repo_profile, reflection)

        try:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text
            return self.parse_findings(task.task_id, raw)
        except Exception as e:
            return [AgentFinding(
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
            )]

    @abstractmethod
    def build_prompt(
        self,
        task: TaskSpec,
        diff: str,
        file_contents: dict,
        repo_profile: dict,
        reflection: list,
    ) -> str:
        pass

    def parse_findings(self, task_id: str, raw: str) -> list[AgentFinding]:
        """
        LLM에게 JSON array를 반환하도록 요청.
        파싱 실패 시 빈 리스트 반환 (pipeline 차단 안 함).
        """
        # JSON 블록 추출
        text = raw.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        # JSON array 찾기
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            return []

        try:
            items = json.loads(text[start:end+1])
        except json.JSONDecodeError:
            return []

        findings = []
        for item in items:
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

        return findings

    def _format_corrections(self, corrections: list) -> str:
        if not corrections:
            return "None recorded yet."
        lines = []
        for c in corrections[-5:]:  # 최근 5개만
            lines.append(f"- [{c['type']}] {c['note']}")
        return "\n".join(lines)

    def _format_reflection(self, reflection: list) -> str:
        if not reflection:
            return "No history yet."
        accepted = [r for r in reflection if r.get("accepted") == 1]
        rejected = [r for r in reflection if r.get("accepted") == 0]
        lines = []
        if accepted:
            lines.append("Findings this team values:")
            for r in accepted[-3:]:
                content = json.loads(r["content"]) if isinstance(r["content"], str) else r["content"]
                lines.append(f"  + {content.get('title', '')}")
        if rejected:
            lines.append("Findings this team rejects (false positives):")
            for r in rejected[-3:]:
                content = json.loads(r["content"]) if isinstance(r["content"], str) else r["content"]
                lines.append(f"  - {content.get('title', '')}")
        return "\n".join(lines) if lines else "No history yet."
