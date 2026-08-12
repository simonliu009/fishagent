from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid4().hex[:12])


class IncidentStatus(str, Enum):
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    EXECUTING = "EXECUTING"
    VERIFY_PENDING = "VERIFY_PENDING"
    RESOLVED = "RESOLVED"
    VERIFY_FAILED = "VERIFY_FAILED"
    ACTION_FAILED = "ACTION_FAILED"
    ESCALATED = "ESCALATED"
    DISMISSED = "DISMISSED"


class CommandStatus(str, Enum):
    PROPOSED = "PROPOSED"
    AUTHORIZED = "AUTHORIZED"
    QUEUED = "QUEUED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class RiskLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class TaskStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ScheduleStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class JobStatus(str, Enum):
    DUE = "DUE"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    RETRY_WAIT = "RETRY_WAIT"
    DEAD_LETTER = "DEAD_LETTER"


@dataclass
class Farm:
    id: str
    name: str
    location: str = ""


@dataclass
class Pond:
    id: str
    name: str
    species: str
    farm_id: str = ""
    dissolved_oxygen_min: float = 4.0


@dataclass
class Sensor:
    id: str
    pond_id: str
    name: str
    metric: str
    unit: str
    status: str = "ONLINE"
    freshness_seconds: int = 120


@dataclass
class SensorReading:
    pond_id: str
    sensor_id: str
    metric: str
    value: float
    unit: str
    sampled_at: datetime
    received_at: datetime = field(default_factory=utcnow)
    quality: str = "GOOD"
    source_event_id: str = field(default_factory=lambda: new_id("reading"))

    def is_fresh(self, max_age_seconds: int = 120) -> bool:
        return self.quality == "GOOD" and self.sampled_at >= utcnow() - timedelta(seconds=max_age_seconds)


@dataclass
class Device:
    id: str
    pond_id: str
    name: str
    capability: str
    shadow_state: str = "off"
    healthy: bool = True


@dataclass
class CameraSource:
    id: str
    pond_id: str
    name: str
    source_type: str
    status: str = "UNAVAILABLE"
    last_frame_at: Optional[datetime] = None


@dataclass
class Evidence:
    id: str
    type: str
    summary: str
    created_at: datetime = field(default_factory=utcnow)
    refs: List[str] = field(default_factory=list)


@dataclass
class DeviceCommand:
    id: str
    device_id: str
    pond_id: str
    target_state: str
    risk: RiskLevel
    idempotency_key: str
    status: CommandStatus = CommandStatus.PROPOSED
    policy_reason: str = ""
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class ActionProposal:
    id: str
    incident_id: str
    device_id: str
    pond_id: str
    target_state: str
    risk: RiskLevel
    rationale: str
    evidence_refs: List[str] = field(default_factory=list)
    status: str = "PROPOSED"
    approval_id: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Approval:
    id: str
    proposal_id: str
    incident_id: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: str = "execution-agent"
    decided_by: Optional[str] = None
    reason: str = ""
    created_at: datetime = field(default_factory=utcnow)
    decided_at: Optional[datetime] = None


@dataclass
class VerificationPlan:
    id: str
    incident_id: str
    metric: str = "DO"
    threshold: float = 4.0
    earliest_at: Optional[datetime] = None
    latest_at: Optional[datetime] = None
    status: str = "PENDING"


@dataclass
class VerificationResult:
    id: str
    incident_id: str
    plan_id: str
    outcome: str
    observed_value: Optional[float] = None
    evidence_refs: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class ManualTask:
    id: str
    incident_id: Optional[str]
    title: str
    description: str
    assignee: str = "现场操作员"
    priority: str = "HIGH"
    status: TaskStatus = TaskStatus.OPEN
    created_at: datetime = field(default_factory=utcnow)
    completed_at: Optional[datetime] = None


@dataclass
class ScheduleDefinition:
    id: str
    name: str
    job_type: str
    interval_seconds: int
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None


@dataclass
class ScheduledJob:
    id: str
    job_type: str
    idempotency_key: str
    due_at: datetime
    incident_id: Optional[str] = None
    schedule_id: Optional[str] = None
    status: JobStatus = JobStatus.DUE
    attempts: int = 0
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Incident:
    id: str
    pond_id: str
    title: str
    status: IncidentStatus = IncidentStatus.DETECTED
    risk: RiskLevel = RiskLevel.L1
    evidence: List[Evidence] = field(default_factory=list)
    action_proposal_ids: List[str] = field(default_factory=list)
    command_ids: List[str] = field(default_factory=list)
    verification_plan_id: Optional[str] = None
    verification_result_ids: List[str] = field(default_factory=list)
    manual_task_ids: List[str] = field(default_factory=list)
    verification_due_at: Optional[datetime] = None
    assignee: Optional[str] = None

    # Incident status machine from the product document:
    # DETECTED -> INVESTIGATING -> ACTION_PROPOSED
    # L1 policy pass -> EXECUTING -> VERIFY_PENDING -> RESOLVED
    # L2 -> WAITING_APPROVAL, L3 -> MANUAL_REQUIRED
    # VERIFY_PENDING -> VERIFY_FAILED -> ESCALATED
    def transition(self, target: IncidentStatus) -> None:
        allowed = {
            IncidentStatus.DETECTED: {IncidentStatus.INVESTIGATING, IncidentStatus.DISMISSED},
            IncidentStatus.INVESTIGATING: {IncidentStatus.ACTION_PROPOSED, IncidentStatus.DISMISSED},
            IncidentStatus.ACTION_PROPOSED: {
                IncidentStatus.EXECUTING,
                IncidentStatus.WAITING_APPROVAL,
                IncidentStatus.MANUAL_REQUIRED,
                IncidentStatus.ACTION_FAILED,
            },
            IncidentStatus.WAITING_APPROVAL: {IncidentStatus.EXECUTING, IncidentStatus.DISMISSED},
            IncidentStatus.EXECUTING: {IncidentStatus.VERIFY_PENDING, IncidentStatus.ACTION_FAILED},
            IncidentStatus.VERIFY_PENDING: {IncidentStatus.RESOLVED, IncidentStatus.VERIFY_FAILED},
            IncidentStatus.VERIFY_FAILED: {IncidentStatus.ESCALATED},
            IncidentStatus.ACTION_FAILED: {IncidentStatus.ESCALATED},
        }
        if target not in allowed.get(self.status, set()):
            raise ValueError("invalid incident transition %s -> %s" % (self.status, target))
        self.status = target


@dataclass
class AgentStep:
    agent: str
    action: str
    summary: str
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class AgentRun:
    id: str
    goal: str
    incident_id: Optional[str] = None
    status: str = "QUEUED"
    stop_reason: Optional[str] = None
    steps: List[AgentStep] = field(default_factory=list)
    delegated_agents: List[str] = field(default_factory=list)
    budget: Dict[str, int] = field(default_factory=lambda: {"delegations": 8, "tool_calls": 20, "seconds": 90})

    def step(self, agent: str, action: str, summary: str) -> None:
        if agent not in self.delegated_agents:
            self.delegated_agents.append(agent)
        self.steps.append(AgentStep(agent=agent, action=action, summary=summary))
