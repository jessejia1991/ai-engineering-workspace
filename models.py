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
    criterion_id: Optional[str] = None   # links a finding to a Contract Criterion (P4)


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


class TaskNode(BaseModel):
    id: str                       # short human-readable, e.g. "n1", "n2a"
    type: str                     # frontend | backend | test | migration | review
    description: str
    dependencies: list[str] = []  # ids of nodes this depends on
    status: str = "PENDING"       # PENDING | IMPLEMENTING | REVIEWING | AWAITING_HUMAN | MERGED | BLOCKED
    artifacts: dict = {}          # free-form: file paths, PR url, etc.
    pr_number: Optional[int] = None


# ---------- P4: contract data model ----------

class Criterion(BaseModel):
    id: str                       # e.g. "c1", "c2"
    owner_agent: str              # SecurityAgent | UIUXAgent | TestingAgent | PerformanceAgent | DeliveryAgent
    priority: str                 # must_have | should_have | nice_to_have
    category: str                 # short tag, e.g. "input-validation"
    assertion: str                # the testable statement
    rationale: str                # one-sentence "why this matters"
    suggested_check: str = "manual"  # static-analysis | runtime-test | manual


class Contract(BaseModel):
    contract_id: str
    graph_id: str
    criteria: list[Criterion] = []
    created_at: Optional[str] = None

    def owners(self) -> set[str]:
        return {c.owner_agent for c in self.criteria}

    def criteria_for(self, agent_name: str) -> list[Criterion]:
        return [c for c in self.criteria if c.owner_agent == agent_name]


class CriterionStatus(BaseModel):
    """A review-side agent's verdict on one Criterion. Returned in the
    reasoning payload of agent_result events."""
    criterion_id: str
    status: str                   # PASS | FAIL | UNVERIFIED
    evidence: str = ""            # what the agent looked at to conclude


class TaskGraph(BaseModel):
    graph_id: str
    root_requirement: str
    nodes: list[TaskNode] = []
    current_node_id: Optional[str] = None
    created_at: Optional[str] = None
    contract: Optional[Contract] = None   # P4: bundled spec stored alongside the DAG

    @property
    def edges(self) -> list[tuple[str, str]]:
        # Derived from node.dependencies — single source of truth, never stored.
        return [(dep, n.id) for n in self.nodes for dep in n.dependencies]
