from datetime import datetime, timedelta
from threading import RLock
from typing import Optional

from fishagent.application.policy import evaluate_action
from fishagent.application.store import InMemoryStore
from fishagent.domain.models import (
    ActionProposal,
    AgentRun,
    Approval,
    ApprovalStatus,
    CameraSource,
    CommandStatus,
    Device,
    DeviceCommand,
    Farm,
    Incident,
    IncidentStatus,
    JobStatus,
    ManualTask,
    Pond,
    RiskLevel,
    ScheduleDefinition,
    ScheduleStatus,
    ScheduledJob,
    Sensor,
    SensorReading,
    TaskStatus,
    VerificationPlan,
    VerificationResult,
    new_id,
    utcnow,
)


class FishAgentSystem:
    def __init__(self, store: Optional[InMemoryStore] = None) -> None:
        self.store = store or InMemoryStore()
        self._job_lock = RLock()

    def initialize_demo(self) -> dict:
        self.store.reset_demo()
        return self.snapshot()

    def create_farm(self, payload: dict) -> Farm:
        farm = Farm(
            id=str(payload.get("id") or new_id("farm")),
            name=str(payload.get("name") or "未命名养殖场"),
            location=str(payload.get("location") or ""),
        )
        self.store.farms[farm.id] = farm
        self.store.emit("asset.farm.created", "创建养殖场：%s" % farm.name, {"farm_id": farm.id})
        return farm

    def create_pond(self, payload: dict) -> Pond:
        farm_id = str(payload.get("farm_id") or "")
        if farm_id and farm_id not in self.store.farms:
            raise ValueError("farm_id does not exist")
        pond = Pond(
            id=str(payload.get("id") or new_id("pond")),
            name=str(payload.get("name") or "未命名池塘"),
            species=str(payload.get("species") or ""),
            farm_id=farm_id,
            dissolved_oxygen_min=float(payload.get("dissolved_oxygen_min") or 4.0),
        )
        self.store.ponds[pond.id] = pond
        self.store.emit("asset.pond.created", "创建养殖单元：%s" % pond.name, {"pond_id": pond.id})
        return pond

    def create_sensor(self, payload: dict) -> Sensor:
        pond_id = str(payload.get("pond_id") or "")
        if pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        sensor = Sensor(
            id=str(payload.get("id") or new_id("sensor")),
            pond_id=pond_id,
            name=str(payload.get("name") or "未命名传感器"),
            metric=str(payload.get("metric") or "DO"),
            unit=str(payload.get("unit") or "mg/L"),
            status=str(payload.get("status") or "ONLINE"),
            freshness_seconds=int(payload.get("freshness_seconds") or 120),
        )
        self.store.sensors[sensor.id] = sensor
        self.store.emit("asset.sensor.created", "创建传感器：%s" % sensor.name, {"sensor_id": sensor.id})
        return sensor

    def create_device(self, payload: dict) -> Device:
        pond_id = str(payload.get("pond_id") or "")
        if pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        device = Device(
            id=str(payload.get("id") or new_id("device")),
            pond_id=pond_id,
            name=str(payload.get("name") or "未命名设备"),
            capability=str(payload.get("capability") or "aeration"),
            shadow_state=str(payload.get("shadow_state") or "off"),
            healthy=bool(payload.get("healthy", True)),
        )
        self.store.devices[device.id] = device
        self.store.emit("asset.device.created", "创建设备：%s" % device.name, {"device_id": device.id})
        return device

    def create_camera(self, payload: dict) -> CameraSource:
        pond_id = str(payload.get("pond_id") or "")
        if pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        camera = CameraSource(
            id=str(payload.get("id") or new_id("camera")),
            pond_id=pond_id,
            name=str(payload.get("name") or "未命名摄像头"),
            source_type=str(payload.get("source_type") or "HTTP_SNAPSHOT"),
            status=str(payload.get("status") or "UNAVAILABLE"),
        )
        self.store.cameras[camera.id] = camera
        self.store.emit("asset.camera.created", "创建摄像头：%s" % camera.name, {"camera_id": camera.id})
        return camera

    def create_schedule(self, payload: dict) -> ScheduleDefinition:
        interval_seconds = int(payload.get("interval_seconds") or 300)
        if interval_seconds < 5:
            raise ValueError("interval_seconds must be at least 5")
        job_type = str(payload.get("job_type") or "patrol")
        if job_type not in {"patrol", "verification"}:
            raise ValueError("job_type must be patrol or verification")
        schedule = ScheduleDefinition(
            id=str(payload.get("id") or new_id("schedule")),
            name=str(payload.get("name") or "全场巡查"),
            job_type=job_type,
            interval_seconds=interval_seconds,
            next_run_at=utcnow() + timedelta(seconds=interval_seconds),
        )
        self.store.schedules[schedule.id] = schedule
        self.store.emit(
            "schedule.created",
            "创建调度：%s" % schedule.name,
            {"schedule_id": schedule.id, "job_type": schedule.job_type},
        )
        return schedule

    def set_schedule_status(self, schedule_id: str, status: ScheduleStatus) -> ScheduleDefinition:
        schedule = self.store.schedules[schedule_id]
        schedule.status = status
        self.store.emit(
            "schedule.status.changed",
            "%s 已%s" % (schedule.name, "暂停" if status == ScheduleStatus.PAUSED else "恢复"),
            {"schedule_id": schedule_id, "status": status.value},
        )
        return schedule

    def run_schedule_now(self, schedule_id: str) -> ScheduledJob:
        schedule = self.store.schedules[schedule_id]
        job = ScheduledJob(
            id=new_id("job"),
            job_type=schedule.job_type,
            idempotency_key="%s:%s" % (schedule.id, utcnow().isoformat()),
            due_at=utcnow(),
            schedule_id=schedule.id,
        )
        self.store.scheduled_jobs[job.id] = job
        self.store.emit("schedule.job.due", "调度已立即触发：%s" % schedule.name, {"job_id": job.id})
        return job

    def _enqueue_due_schedules(self) -> None:
        now = utcnow()
        for schedule in self.store.schedules.values():
            if schedule.status != ScheduleStatus.ACTIVE or not schedule.next_run_at:
                continue
            if schedule.next_run_at > now:
                continue
            due_at = schedule.next_run_at
            key = "schedule:%s:%s" % (schedule.id, int(due_at.timestamp()))
            if not any(job.idempotency_key == key for job in self.store.scheduled_jobs.values()):
                job = ScheduledJob(
                    id=new_id("job"),
                    job_type=schedule.job_type,
                    idempotency_key=key,
                    due_at=due_at,
                    schedule_id=schedule.id,
                )
                self.store.scheduled_jobs[job.id] = job
                self.store.emit("schedule.job.due", "周期调度已到期：%s" % schedule.name, {"job_id": job.id})
            schedule.last_run_at = due_at
            schedule.next_run_at = now + timedelta(seconds=schedule.interval_seconds)

    def run_patrol(self) -> AgentRun:
        run = AgentRun(id=new_id("run"), goal="执行全场巡查", status="RUNNING")
        self.store.agent_runs[run.id] = run
        run.step("supervisor-agent", "start_patrol", "读取全场养殖单元和活动事件")
        for pond in self.store.ponds.values():
            latest = self.store.latest_reading(pond.id, "DO")
            summary = "最新溶氧 %.2fmg/L" % latest.value if latest else "暂无最新溶氧读数"
            run.step("sensor-monitor-agent", "inspect_pond", "%s：%s" % (pond.name, summary))
        run.status = "COMPLETED"
        run.stop_reason = "PATROL_COMPLETED"
        self.store.emit("patrol.completed", "全场巡查完成", {"run_id": run.id}, correlation_id=run.id)
        return run

    def run_goal(self, goal: str, pond_id: Optional[str] = None) -> AgentRun:
        normalized = goal.strip()
        if not normalized:
            raise ValueError("goal is required")
        if normalized.lower() in {"patrol", "巡查", "巡查全场", "全场巡查"}:
            return self.run_patrol()
        if pond_id and pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        active = self.store.active_incident_for_pond(pond_id) if pond_id else None
        if active and active.status == IncidentStatus.DETECTED:
            return self.run_incident_flow(active.id)
        run = AgentRun(id=new_id("run"), goal=normalized, status="RUNNING")
        self.store.agent_runs[run.id] = run
        run.step("supervisor-agent", "interpret_goal", "解析用户目标并检查可用证据")
        if pond_id:
            latest = self.store.latest_reading(pond_id, "DO")
            run.step(
                "sensor-monitor-agent",
                "get_pond_snapshot",
                "%s" % ("读取到最新溶氧 %.2fmg/L" % latest.value if latest else "暂无最新溶氧读数"),
            )
        run.status = "COMPLETED"
        run.stop_reason = "NO_ACTION_NEEDED"
        self.store.emit("agent.run.completed", "用户目标已完成：%s" % normalized, {"run_id": run.id}, correlation_id=run.id)
        return run

    def _create_verification_plan(self, incident: Incident, due_at: datetime) -> VerificationPlan:
        existing = self.store.verification_plans.get(incident.verification_plan_id or "")
        if existing:
            existing.earliest_at = due_at
            existing.latest_at = due_at + timedelta(seconds=60)
            return existing
        pond = self.store.ponds[incident.pond_id]
        plan = VerificationPlan(
            id=new_id("verify-plan"),
            incident_id=incident.id,
            threshold=pond.dissolved_oxygen_min,
            earliest_at=due_at,
            latest_at=due_at + timedelta(seconds=60),
        )
        self.store.verification_plans[plan.id] = plan
        incident.verification_plan_id = plan.id
        return plan

    def _schedule_verification(self, incident: Incident, due_at: datetime) -> ScheduledJob:
        self._create_verification_plan(incident, due_at)
        incident.verification_due_at = due_at
        key = "verification:%s" % incident.id
        existing = next(
            (job for job in self.store.scheduled_jobs.values() if job.idempotency_key == key),
            None,
        )
        if existing:
            existing.due_at = due_at
            existing.status = JobStatus.DUE
            return existing
        job = ScheduledJob(
            id=new_id("job"),
            job_type="verification",
            idempotency_key=key,
            due_at=due_at,
            incident_id=incident.id,
        )
        self.store.scheduled_jobs[job.id] = job
        self.store.emit(
            "verification.scheduled",
            "已安排复核：%s" % incident.title,
            {"incident_id": incident.id, "job_id": job.id, "due_at": due_at.isoformat()},
        )
        return job

    def create_manual_task(
        self,
        title: str,
        description: str,
        incident_id: Optional[str] = None,
        assignee: str = "现场操作员",
        priority: str = "HIGH",
    ) -> ManualTask:
        task = ManualTask(
            id=new_id("task"),
            incident_id=incident_id,
            title=title,
            description=description,
            assignee=assignee,
            priority=priority,
        )
        self.store.manual_tasks[task.id] = task
        if incident_id and incident_id in self.store.incidents:
            self.store.incidents[incident_id].manual_task_ids.append(task.id)
        self.store.emit("manual_task.created", title, {"task_id": task.id, "incident_id": incident_id})
        return task

    def complete_manual_task(self, task_id: str) -> ManualTask:
        task = self.store.manual_tasks[task_id]
        task.status = TaskStatus.COMPLETED
        task.completed_at = utcnow()
        self.store.emit("manual_task.completed", task.title, {"task_id": task.id})
        return task

    def ingest_do(
        self,
        pond_id: str,
        value: float,
        source_event_id: Optional[str] = None,
        seconds_old: int = 0,
        sensor_id: Optional[str] = None,
        quality: str = "GOOD",
        auto_run: bool = True,
    ) -> Optional[Incident]:
        if pond_id not in self.store.ponds:
            raise ValueError("pond_id does not exist")
        if quality not in {"GOOD", "SUSPECT", "STALE", "INVALID"}:
            raise ValueError("unsupported reading quality")
        reading = SensorReading(
            pond_id=pond_id,
            sensor_id=sensor_id or "do-%s" % pond_id.lower(),
            metric="DO",
            value=value,
            unit="mg/L",
            sampled_at=utcnow() - timedelta(seconds=seconds_old),
            quality=quality,
            source_event_id=source_event_id or new_id("reading"),
        )
        incident = self.store.add_reading(reading)
        if incident and incident.status == IncidentStatus.DETECTED and auto_run:
            self.run_incident_flow(incident.id)
        return incident

    def run_incident_flow(self, incident_id: str, risk_override: Optional[RiskLevel] = None) -> AgentRun:
        incident = self.store.incidents[incident_id]
        run = AgentRun(id=new_id("run"), goal="处理 %s" % incident.title, incident_id=incident_id, status="RUNNING")
        self.store.agent_runs[run.id] = run
        self.store.emit("agent.run.started", run.goal, {"run_id": run.id}, correlation_id=run.id)

        incident.transition(IncidentStatus.INVESTIGATING)
        run.step("supervisor-agent", "validate_trigger", "确认触发源为低溶氧传感器事件")

        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        run.step("sensor-monitor-agent", "get_pond_snapshot", "读取最新溶氧、水质质量和采样时间")
        if latest_do is None or not latest_do.is_fresh():
            run.step("supervisor-agent", "stop", "核心数据过期或缺失，要求刷新数据")
            run.status = "FAILED"
            run.stop_reason = "STALE_EVIDENCE"
            self.store.emit("agent.run.failed", "证据过期，未执行设备动作", {"run_id": run.id}, correlation_id=run.id)
            return run

        device = self.store.aeration_device_for_pond(incident.pond_id)
        if device is None:
            run.step("patrol-analysis-agent", "get_device_capabilities", "未找到可用增氧设备，升级人工处理")
            incident.transition(IncidentStatus.ACTION_PROPOSED)
            incident.transition(IncidentStatus.MANUAL_REQUIRED)
            incident.assignee = "现场操作员"
            self.create_manual_task(
                title="检查 %s 的增氧设备" % incident.title,
                description="没有找到健康且具备 aeration 能力的设备，请现场确认设备或手动增氧。",
                incident_id=incident.id,
            )
            run.status = "COMPLETED"
            run.stop_reason = "NO_CAPABLE_DEVICE"
            self.store.emit("agent.run.completed", "未找到可用增氧设备，已转人工", {"run_id": run.id}, correlation_id=run.id)
            return run
        run.step("patrol-analysis-agent", "get_device_shadow_state", "%s 当前为 %s" % (device.name, device.shadow_state))

        if device.shadow_state == "on":
            run.step("supervisor-agent", "route", "设备已开启，停止重复执行并转向效果复核/故障调查")
            incident.transition(IncidentStatus.ACTION_PROPOSED)
            incident.transition(IncidentStatus.EXECUTING)
            incident.transition(IncidentStatus.VERIFY_PENDING)
            self._schedule_verification(incident, utcnow())
            run.status = "COMPLETED"
            run.stop_reason = "ALREADY_SATISFIED"
            self.store.emit("agent.run.completed", "设备已在目标状态，已抑制重复动作", {"run_id": run.id}, correlation_id=run.id)
            return run

        run.step("action-planning-agent", "propose_action", "建议开启 %s，风险 L1，30 秒后复核溶氧" % device.name)
        incident.transition(IncidentStatus.ACTION_PROPOSED)

        risk = risk_override or RiskLevel.L1
        if risk != RiskLevel.L1:
            proposal = self.propose_action(
                incident_id=incident.id,
                device_id=device.id,
                target_state="on",
                risk=risk,
                rationale="低溶氧事件需要对设备执行受控动作",
            )
            run.step("execution-agent", "propose_action", proposal.rationale)
            run.status = "WAITING_APPROVAL" if risk == RiskLevel.L2 else "COMPLETED"
            run.stop_reason = "WAITING_APPROVAL" if risk == RiskLevel.L2 else "MANUAL_REQUIRED"
            return run

        command = self.request_action_execution(run, incident, device_id=device.id, target_state="on", risk=risk)
        if command.status == CommandStatus.CONFIRMED:
            incident.transition(IncidentStatus.EXECUTING)
            incident.transition(IncidentStatus.VERIFY_PENDING)
            self._schedule_verification(incident, utcnow() + timedelta(seconds=30))
            run.status = "COMPLETED"
            run.stop_reason = "ACTION_EXECUTED"
            self.store.emit("agent.run.completed", "增氧命令已确认，等待复核", {"run_id": run.id}, correlation_id=run.id)
        elif command.policy_reason.startswith("设备影子状态"):
            incident.transition(IncidentStatus.EXECUTING)
            incident.transition(IncidentStatus.VERIFY_PENDING)
            self._schedule_verification(incident, utcnow())
            run.status = "COMPLETED"
            run.stop_reason = "ALREADY_SATISFIED"
        else:
            incident.transition(IncidentStatus.ACTION_FAILED)
            incident.transition(IncidentStatus.ESCALATED)
            run.status = "FAILED"
            run.stop_reason = "POLICY_REJECTED"
        return run

    def propose_action(
        self,
        incident_id: str,
        device_id: str,
        target_state: str,
        risk: RiskLevel,
        rationale: str,
    ) -> ActionProposal:
        incident = self.store.incidents[incident_id]
        device = self.store.devices.get(device_id)
        if device is None:
            raise ValueError("device_id does not exist")
        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        if latest_do is None:
            raise ValueError("latest DO reading is required")
        policy = evaluate_action(
            actor="execution-agent",
            device=device,
            pond_id=incident.pond_id,
            target_state=target_state,
            risk=risk,
            latest_do=latest_do,
            idempotency_seen=False,
        )
        status = "PENDING_APPROVAL" if policy.status == "WAITING_APPROVAL" else policy.status
        proposal = ActionProposal(
            id=new_id("proposal"),
            incident_id=incident.id,
            device_id=device.id,
            pond_id=incident.pond_id,
            target_state=target_state,
            risk=risk,
            rationale=rationale or policy.reason,
            evidence_refs=[latest_do.source_event_id],
            status=status,
        )
        self.store.action_proposals[proposal.id] = proposal
        incident.action_proposal_ids.append(proposal.id)
        self.store.emit(
            "action.proposal.created",
            proposal.rationale,
            {"proposal_id": proposal.id, "risk": risk.value, "policy_status": policy.status},
        )
        if policy.status == "WAITING_APPROVAL":
            approval = Approval(id=new_id("approval"), proposal_id=proposal.id, incident_id=incident.id)
            proposal.approval_id = approval.id
            self.store.approvals[approval.id] = approval
            if incident.status == IncidentStatus.ACTION_PROPOSED:
                incident.transition(IncidentStatus.WAITING_APPROVAL)
            self.store.emit(
                "approval.requested",
                "中风险动作等待人工审批",
                {"approval_id": approval.id, "proposal_id": proposal.id},
            )
        elif policy.status == "MANUAL_REQUIRED":
            proposal.status = "MANUAL_REQUIRED"
            if incident.status == IncidentStatus.ACTION_PROPOSED:
                incident.transition(IncidentStatus.MANUAL_REQUIRED)
            self.create_manual_task(
                title="人工执行：%s" % incident.title,
                description=proposal.rationale,
                incident_id=incident.id,
            )
        return proposal

    def approve_action(self, proposal_id: str, approver: str, reason: str = "") -> DeviceCommand:
        proposal = self.store.action_proposals[proposal_id]
        if not proposal.approval_id:
            raise ValueError("proposal does not require approval")
        approval = self.store.approvals[proposal.approval_id]
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError("approval is no longer pending")
        incident = self.store.incidents[proposal.incident_id]
        approval.status = ApprovalStatus.APPROVED
        approval.decided_by = approver
        approval.reason = reason
        approval.decided_at = utcnow()
        proposal.status = "APPROVED"
        run = AgentRun(id=new_id("run"), goal="执行已批准动作", incident_id=incident.id, status="RUNNING")
        self.store.agent_runs[run.id] = run
        if incident.status == IncidentStatus.WAITING_APPROVAL:
            incident.transition(IncidentStatus.EXECUTING)
        command = self.request_action_execution(
            run,
            incident,
            device_id=proposal.device_id,
            target_state=proposal.target_state,
            risk=proposal.risk,
            approval_granted=True,
        )
        if command.status == CommandStatus.CONFIRMED:
            if incident.status == IncidentStatus.EXECUTING:
                incident.transition(IncidentStatus.VERIFY_PENDING)
            self._schedule_verification(incident, utcnow() + timedelta(seconds=30))
            run.status = "COMPLETED"
            run.stop_reason = "ACTION_EXECUTED_AFTER_APPROVAL"
        else:
            if incident.status == IncidentStatus.EXECUTING:
                incident.transition(IncidentStatus.ACTION_FAILED)
                incident.transition(IncidentStatus.ESCALATED)
            run.status = "FAILED"
            run.stop_reason = "POLICY_REJECTED"
        self.store.emit(
            "approval.approved",
            "动作已由 %s 批准" % approver,
            {"approval_id": approval.id, "proposal_id": proposal.id},
            correlation_id=run.id,
        )
        return command

    def reject_action(self, proposal_id: str, approver: str, reason: str) -> Approval:
        proposal = self.store.action_proposals[proposal_id]
        if not proposal.approval_id:
            raise ValueError("proposal does not require approval")
        approval = self.store.approvals[proposal.approval_id]
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError("approval is no longer pending")
        approval.status = ApprovalStatus.REJECTED
        approval.decided_by = approver
        approval.reason = reason
        approval.decided_at = utcnow()
        proposal.status = "REJECTED"
        incident = self.store.incidents[proposal.incident_id]
        if incident.status == IncidentStatus.WAITING_APPROVAL:
            incident.transition(IncidentStatus.DISMISSED)
        self.store.emit(
            "approval.rejected",
            "动作审批被拒绝：%s" % reason,
            {"approval_id": approval.id, "proposal_id": proposal.id},
        )
        return approval

    def request_action_execution(
        self,
        run: AgentRun,
        incident: Incident,
        device_id: str,
        target_state: str,
        risk: RiskLevel,
        approval_granted: bool = False,
    ) -> DeviceCommand:
        device = self.store.devices[device_id]
        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        idempotency_key = "%s:%s:%s" % (incident.pond_id, device_id, target_state)
        policy = evaluate_action(
            actor="execution-agent",
            device=device,
            pond_id=incident.pond_id,
            target_state=target_state,
            risk=risk,
            latest_do=latest_do,
            idempotency_seen=idempotency_key in self.store.executed_idempotency_keys,
            approval_granted=approval_granted,
        )
        command = DeviceCommand(
            id=new_id("cmd"),
            device_id=device.id,
            pond_id=incident.pond_id,
            target_state=target_state,
            risk=risk,
            idempotency_key=idempotency_key,
            policy_reason=policy.reason,
        )
        self.store.commands[command.id] = command
        incident.command_ids.append(command.id)
        run.step("execution-agent", "request_action_execution", policy.reason)
        self.store.emit("policy.evaluated", policy.reason, {"allowed": policy.allowed, "command_id": command.id}, correlation_id=run.id)
        if not policy.allowed:
            command.status = CommandStatus.REJECTED
            return command

        command.status = CommandStatus.AUTHORIZED
        command.status = CommandStatus.QUEUED
        command.status = CommandStatus.SENT
        command.status = CommandStatus.ACKNOWLEDGED
        device.shadow_state = target_state
        command.status = CommandStatus.CONFIRMED
        self.store.executed_idempotency_keys[idempotency_key] = command.id
        self.store.emit("device.command.confirmed", "%s 已切换为 %s" % (device.name, target_state), {"command_id": command.id}, correlation_id=run.id)
        return command

    def verify_incident(self, incident_id: str) -> Incident:
        incident = self.store.incidents[incident_id]
        if incident.status != IncidentStatus.VERIFY_PENDING:
            return incident
        plan = self.store.verification_plans.get(incident.verification_plan_id or "")
        run = AgentRun(id=new_id("run"), goal="复核 %s" % incident.title, incident_id=incident.id, status="RUNNING")
        self.store.agent_runs[run.id] = run
        latest_do = self.store.latest_reading(incident.pond_id, "DO")
        run.step("verification-agent", "record_verification", "读取复核溶氧并判断处置效果")
        passed = bool(
            latest_do
            and latest_do.is_fresh()
            and latest_do.value >= self.store.ponds[incident.pond_id].dissolved_oxygen_min
        )
        result = VerificationResult(
            id=new_id("verify"),
            incident_id=incident.id,
            plan_id=plan.id if plan else "",
            outcome="PASSED" if passed else "FAILED",
            observed_value=latest_do.value if latest_do else None,
            evidence_refs=[latest_do.source_event_id] if latest_do else [],
        )
        self.store.verification_results[result.id] = result
        incident.verification_result_ids.append(result.id)
        if plan:
            plan.status = result.outcome
        for job in self.store.scheduled_jobs.values():
            if job.incident_id == incident.id and job.job_type == "verification":
                job.status = JobStatus.COMPLETED
                job.attempts += 1
        if passed:
            incident.transition(IncidentStatus.RESOLVED)
            run.status = "COMPLETED"
            run.stop_reason = "RESOLVED"
            self.store.emit("verification.resolved", "复核通过，事件关闭", {"incident_id": incident.id}, correlation_id=run.id)
        else:
            incident.transition(IncidentStatus.VERIFY_FAILED)
            incident.transition(IncidentStatus.ESCALATED)
            incident.assignee = "现场操作员"
            run.step("verification-agent", "create_manual_task", "复核未恢复，升级设备故障与人工处理")
            self.create_manual_task(
                title="处理复核失败：%s" % incident.title,
                description="复核溶氧仍未达到安全线，请检查增氧设备、供电和水体状态。",
                incident_id=incident.id,
            )
            run.status = "COMPLETED"
            run.stop_reason = "ESCALATED"
            self.store.emit("verification.escalated", "复核失败，已升级人工任务", {"incident_id": incident.id}, correlation_id=run.id)
        return incident

    def run_due_jobs(self, limit: int = 50) -> list[ScheduledJob]:
        with self._job_lock:
            self._enqueue_due_schedules()
            completed: list[ScheduledJob] = []
            for job in self.store.due_jobs()[:limit]:
                job.status = JobStatus.RUNNING
                job.attempts += 1
                if job.job_type == "verification" and job.incident_id:
                    self.verify_incident(job.incident_id)
                elif job.job_type == "patrol":
                    self.run_patrol()
                job.status = JobStatus.COMPLETED
                completed.append(job)
                self.store.emit("schedule.job.completed", "后台作业已完成", {"job_id": job.id})
            return completed

    def run_demo(self, mode: str) -> dict:
        self.store.reset_demo()
        if mode == "approval":
            incident = self.ingest_do("B-01", 2.1, source_event_id="demo-approval", auto_run=False)
            if incident:
                self.run_incident_flow(incident.id, risk_override=RiskLevel.L2)
        elif mode == "dedup":
            self.store.devices["aerator-b01-1"].shadow_state = "on"
            incident = self.ingest_do("B-01", 2.1, source_event_id="demo-dedup")
            if incident:
                self.store.force_verification_due(incident.id)
                self.verify_incident(incident.id)
        elif mode == "failure":
            incident = self.ingest_do("B-01", 2.1, source_event_id="demo-failure")
            if incident:
                self.store.force_verification_due(incident.id)
                self.ingest_do("B-01", 2.3, source_event_id="demo-failure-review")
                self.verify_incident(incident.id)
        else:
            incident = self.ingest_do("B-01", 2.1, source_event_id="demo-success")
            if incident:
                self.store.force_verification_due(incident.id)
                self.ingest_do("B-01", 5.2, source_event_id="demo-success-review")
                self.verify_incident(incident.id)
        return self.snapshot()

    def snapshot(self) -> dict:
        return {
            "farms": [farm.__dict__ for farm in self.store.farms.values()],
            "ponds": [pond.__dict__ for pond in self.store.ponds.values()],
            "sensors": [sensor.__dict__ for sensor in self.store.sensors.values()],
            "devices": [device.__dict__ for device in self.store.devices.values()],
            "readings": [
                {
                    "pond_id": reading.pond_id,
                    "sensor_id": reading.sensor_id,
                    "metric": reading.metric,
                    "value": reading.value,
                    "unit": reading.unit,
                    "sampled_at": reading.sampled_at.isoformat(),
                    "received_at": reading.received_at.isoformat(),
                    "quality": reading.quality,
                    "source_event_id": reading.source_event_id,
                }
                for reading in self.store.readings[-200:]
            ],
            "cameras": [
                {
                    "id": camera.id,
                    "pond_id": camera.pond_id,
                    "name": camera.name,
                    "source_type": camera.source_type,
                    "status": camera.status,
                    "last_frame_at": camera.last_frame_at.isoformat() if camera.last_frame_at else None,
                }
                for camera in self.store.cameras.values()
            ],
            "incidents": [
                {
                    "id": item.id,
                    "pond_id": item.pond_id,
                    "title": item.title,
                    "status": item.status.value,
                    "risk": item.risk.value,
                    "evidence": [e.__dict__ for e in item.evidence],
                    "action_proposal_ids": item.action_proposal_ids,
                    "command_ids": item.command_ids,
                    "verification_plan_id": item.verification_plan_id,
                    "verification_result_ids": item.verification_result_ids,
                    "manual_task_ids": item.manual_task_ids,
                    "verification_due_at": item.verification_due_at.isoformat() if item.verification_due_at else None,
                    "assignee": item.assignee,
                }
                for item in self.store.incidents.values()
            ],
            "action_proposals": [
                {
                    "id": item.id,
                    "incident_id": item.incident_id,
                    "device_id": item.device_id,
                    "pond_id": item.pond_id,
                    "target_state": item.target_state,
                    "risk": item.risk.value,
                    "rationale": item.rationale,
                    "evidence_refs": item.evidence_refs,
                    "status": item.status,
                    "approval_id": item.approval_id,
                    "created_at": item.created_at.isoformat(),
                }
                for item in self.store.action_proposals.values()
            ],
            "approvals": [
                {
                    "id": item.id,
                    "proposal_id": item.proposal_id,
                    "incident_id": item.incident_id,
                    "status": item.status.value,
                    "requested_by": item.requested_by,
                    "decided_by": item.decided_by,
                    "reason": item.reason,
                    "created_at": item.created_at.isoformat(),
                    "decided_at": item.decided_at.isoformat() if item.decided_at else None,
                }
                for item in self.store.approvals.values()
            ],
            "commands": [
                {
                    "id": item.id,
                    "device_id": item.device_id,
                    "pond_id": item.pond_id,
                    "target_state": item.target_state,
                    "risk": item.risk.value,
                    "status": item.status.value,
                    "policy_reason": item.policy_reason,
                    "idempotency_key": item.idempotency_key,
                }
                for item in self.store.commands.values()
            ],
            "verification_plans": [
                {
                    "id": item.id,
                    "incident_id": item.incident_id,
                    "metric": item.metric,
                    "threshold": item.threshold,
                    "earliest_at": item.earliest_at.isoformat() if item.earliest_at else None,
                    "latest_at": item.latest_at.isoformat() if item.latest_at else None,
                    "status": item.status,
                }
                for item in self.store.verification_plans.values()
            ],
            "verification_results": [
                {
                    "id": item.id,
                    "incident_id": item.incident_id,
                    "plan_id": item.plan_id,
                    "outcome": item.outcome,
                    "observed_value": item.observed_value,
                    "evidence_refs": item.evidence_refs,
                    "created_at": item.created_at.isoformat(),
                }
                for item in self.store.verification_results.values()
            ],
            "manual_tasks": [
                {
                    "id": item.id,
                    "incident_id": item.incident_id,
                    "title": item.title,
                    "description": item.description,
                    "assignee": item.assignee,
                    "priority": item.priority,
                    "status": item.status.value,
                    "created_at": item.created_at.isoformat(),
                    "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                }
                for item in self.store.manual_tasks.values()
            ],
            "schedules": [
                {
                    "id": item.id,
                    "name": item.name,
                    "job_type": item.job_type,
                    "interval_seconds": item.interval_seconds,
                    "status": item.status.value,
                    "next_run_at": item.next_run_at.isoformat() if item.next_run_at else None,
                    "last_run_at": item.last_run_at.isoformat() if item.last_run_at else None,
                }
                for item in self.store.schedules.values()
            ],
            "scheduled_jobs": [
                {
                    "id": item.id,
                    "job_type": item.job_type,
                    "idempotency_key": item.idempotency_key,
                    "due_at": item.due_at.isoformat(),
                    "incident_id": item.incident_id,
                    "schedule_id": item.schedule_id,
                    "status": item.status.value,
                    "attempts": item.attempts,
                    "created_at": item.created_at.isoformat(),
                }
                for item in self.store.scheduled_jobs.values()
            ],
            "agent_runs": [
                {
                    "id": run.id,
                    "goal": run.goal,
                    "incident_id": run.incident_id,
                    "status": run.status,
                    "stop_reason": run.stop_reason,
                    "delegated_agents": run.delegated_agents,
                    "steps": [step.__dict__ for step in run.steps],
                    "budget": run.budget,
                }
                for run in self.store.agent_runs.values()
            ],
            "events": self.store.events[-80:],
        }
