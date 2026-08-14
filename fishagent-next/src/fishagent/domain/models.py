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


class AgentRunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class HealthStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DRIFTING = "DRIFTING"
    ERROR = "ERROR"


@dataclass
class Farm:
    id: str
    name: str
    location: str = ""


@dataclass
class Zone:
    id: str
    farm_id: str
    name: str
    location: str = ""
    status: str = "ACTIVE"


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
class SensorHealth:
    sensor_id: str
    status: HealthStatus = HealthStatus.ONLINE
    last_heartbeat_at: Optional[datetime] = None
    last_reading_at: Optional[datetime] = None
    error_count: int = 0
    drift_score: float = 0.0
    message: str = ""


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
    camera_role: str = "SURFACE"
    status: str = "UNAVAILABLE"
    last_frame_at: Optional[datetime] = None
    source_url: str = ""
    privacy_policy: str = "EVENT_ONLY"
    last_frame_id: Optional[str] = None
    last_frame_hash: Optional[str] = None
    last_frame_width: Optional[int] = None
    last_frame_height: Optional[int] = None


@dataclass
class VisionFrame:
    id: str
    camera_id: str
    source_url: str
    object_name: str
    content_type: str
    sha256: str
    width: int
    height: int
    captured_at: datetime = field(default_factory=utcnow)


@dataclass
class WeatherObservation:
    id: str
    pond_id: str
    condition: str
    temperature_c: float
    wind_speed_mps: float
    wind_direction: str
    humidity_pct: int
    rain_probability_pct: int
    pressure_hpa: float
    forecast: str
    observed_at: datetime = field(default_factory=utcnow)


@dataclass
class CameraObservation:
    id: str
    camera_id: str
    pond_id: str
    camera_role: str
    observation_type: str
    status: str
    summary: str
    labels: List[str] = field(default_factory=list)
    confidence: float = 0.0
    captured_at: datetime = field(default_factory=utcnow)
    evidence_refs: List[str] = field(default_factory=list)


@dataclass
class DiseaseKnowledgeArticle:
    id: str
    name: str
    species: str
    signs: str
    visual_cues: List[str] = field(default_factory=list)
    recommended_actions: List[str] = field(default_factory=list)
    severity: str = "MEDIUM"


@dataclass
class AnalysisCase:
    id: str
    sequence: int
    title: str
    category: str
    pond_id: str
    trigger: str
    description: str
    evidence_refs: List[str] = field(default_factory=list)
    expected_path: str = ""
    expected_device_id: str = ""
    expected_target_state: str = ""
    expected_result: str = ""
    status: str = "READY"
    incident_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    result_summary: str = ""
    updated_at: datetime = field(default_factory=utcnow)


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
class PatrolFinding:
    id: str
    patrol_run_id: str
    pond_id: str
    status: str
    summary: str
    evidence_refs: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Escalation:
    id: str
    incident_id: str
    level: str
    reason: str
    status: str = "OPEN"
    manual_task_id: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class AuditEvent:
    id: str
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    correlation_id: Optional[str] = None
    payload: Dict[str, object] = field(default_factory=dict)
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
            IncidentStatus.EXECUTING: {IncidentStatus.VERIFY_PENDING, IncidentStatus.RESOLVED, IncidentStatus.ACTION_FAILED},
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
    status: str = AgentRunStatus.QUEUED.value
    stop_reason: Optional[str] = None
    steps: List[AgentStep] = field(default_factory=list)
    delegated_agents: List[str] = field(default_factory=list)
    budget: Dict[str, int] = field(default_factory=lambda: {"delegations": 8, "tool_calls": 20, "seconds": 300})

    def step(self, agent: str, action: str, summary: str) -> None:
        if agent not in self.delegated_agents:
            self.delegated_agents.append(agent)
        self.steps.append(AgentStep(agent=agent, action=action, summary=summary))
