from pydantic import BaseModel
from typing import Optional


class TaskSpec(BaseModel):
    task_id: str
    type: str                    # plan | review
    title: str
    description: str
    affected_files: list[str] = []
    dependencies: list[str] = []
    acceptance: list[str] = []
    pr_url: Optional[str] = None
    branch: Optional[str] = None


class AgentFinding(BaseModel):
    finding_id: str
    task_id: str
    agent: str
    severity: str                # low | medium | high | critical
    category: str
    title: str
    detail: str
    suggestion: str
    file: Optional[str] = None
    line: Optional[int] = None
    accepted: Optional[bool] = None
    status: str = "ok"           # ok | failed
    error: Optional[str] = None


class RiskReport(BaseModel):
    task_id: str
    pr_url: Optional[str] = None
    overall_risk: str            # low | medium | high | critical
    agents_run: list[str] = []
    agents_skipped: dict = {}    # agent_name -> reason
    by_agent: dict = {}          # agent_name -> {risk, count}
    top_actions: list[str] = []
    merge_recommendation: str    # approve | request_changes | reject


class AgentSelection(BaseModel):
    selected: list[str]
    skipped: dict[str, str]      # agent_name -> reason
    reasoning: dict[str, str]    # agent_name -> why selected
