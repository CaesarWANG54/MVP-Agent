from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class WorkerSpec:
    worker_id: str
    worker_type: str
    display_name: str
    role: str
    capabilities: list[str]
    cost_tier: int
    quality_tier: int
    speed_tier: int
    local_only: bool
    api_cost: bool
    supports_workspace_write: bool
    enabled: bool = True
    platform: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunProfile:
    name: str
    planner_worker: str
    reviewer_worker: str
    prefer_local: bool
    cost_weight: float
    quality_weight: float
    speed_weight: float
    local_bonus: float
    simple_repeatable_bonus: float
    escalate_difficulty: int
    escalate_risk: int
    latency_weight: float = 1.0
    max_subtasks_hint: int = 3


@dataclass(slots=True)
class Subtask:
    task_id: str
    title: str
    goal: str
    kind: str
    difficulty: int
    repeatability: int
    risk: int
    needs_write_access: bool
    preferred_capabilities: list[str]
    acceptance_criteria: list[str]
    notes: str = ""
    depends_on: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=list)
    parallel_group: str = ""
    required_skills: list[str] = field(default_factory=list)
    coordination_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TaskPlan:
    summary: str
    execution_strategy: str
    subtasks: list[Subtask]
    planner_worker: str
    raw_response: str
    doctrine: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    capability_gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "execution_strategy": self.execution_strategy,
            "planner_worker": self.planner_worker,
            "raw_response": self.raw_response,
            "doctrine": self.doctrine,
            "required_skills": self.required_skills,
            "capability_gaps": self.capability_gaps,
            "subtasks": [item.to_dict() for item in self.subtasks],
        }


@dataclass(slots=True)
class Assignment:
    subtask: Subtask
    worker_id: str
    score: float
    rationale: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask": self.subtask.to_dict(),
            "worker_id": self.worker_id,
            "score": round(self.score, 2),
            "rationale": self.rationale,
        }


@dataclass(slots=True)
class WorkerResult:
    worker_id: str
    status: str
    summary: str
    deliverable: str
    artifacts: list[str]
    changes_made: list[str]
    risks: list[str]
    recommended_next_steps: list[str]
    confidence: float
    raw_response: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReviewResult:
    reviewer_worker: str
    decision: str
    summary: str
    findings: list[str]
    confidence: float
    raw_response: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AssignmentOutcome:
    assignment: Assignment
    result: WorkerResult
    review: ReviewResult | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment": self.assignment.to_dict(),
            "result": self.result.to_dict(),
            "review": None if self.review is None else self.review.to_dict(),
        }


@dataclass(slots=True)
class LeaderTraceEntry:
    timestamp: str
    phase: str
    decision: str
    detail: str
    worker_id: str = ""
    task_id: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunReport:
    task: str
    mode: str
    profile: str
    status: str
    leader_notes: str
    plan: TaskPlan
    planned_assignments: list[Assignment]
    outcomes: list[AssignmentOutcome]
    performance: dict[str, Any] = field(default_factory=dict)
    report_path: str | None = None
    leader_trace: list[LeaderTraceEntry] = field(default_factory=list)
    synthesis_worker: str = ""
    synthesis_raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "mode": self.mode,
            "profile": self.profile,
            "status": self.status,
            "leader_notes": self.leader_notes,
            "report_path": self.report_path,
            "plan": self.plan.to_dict(),
            "planned_assignments": [item.to_dict() for item in self.planned_assignments],
            "outcomes": [item.to_dict() for item in self.outcomes],
            "performance": self.performance,
            "leader_trace": [item.to_dict() for item in self.leader_trace],
            "synthesis_worker": self.synthesis_worker,
            "synthesis_raw": self.synthesis_raw,
        }
