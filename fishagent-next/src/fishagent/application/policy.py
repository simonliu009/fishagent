from dataclasses import dataclass
from typing import Optional

from fishagent.domain.models import Device, RiskLevel, SensorReading


@dataclass
class PolicyResult:
    allowed: bool
    status: str
    reason: str


def evaluate_action(
    *,
    actor: str,
    device: Device,
    pond_id: str,
    target_state: str,
    risk: RiskLevel,
    latest_do: Optional[SensorReading],
    idempotency_seen: bool,
    approval_granted: bool = False,
    multimodal_evidence: bool = False,
) -> PolicyResult:
    if actor != "execution-agent":
        return PolicyResult(False, "REJECTED", "只有执行 Agent 可提交经过策略门的动作请求")
    if device.pond_id != pond_id:
        return PolicyResult(False, "REJECTED", "设备与池塘不匹配")
    allowed_states = {
        "aeration": {"on", "off"},
        "feeding": {"off"},
        "valve_control": {"off"},
        "circulation": {"on", "off"},
        "water_intake": {"off"},
        "drainage": {"on", "off"},
        "dosing": {"off"},
    }
    if target_state not in allowed_states.get(device.capability, set()):
        return PolicyResult(False, "REJECTED", "设备能力或目标状态不在受控动作白名单")
    if device.capability == "aeration" and target_state == "on" and not multimodal_evidence:
        if latest_do is None:
            return PolicyResult(False, "REJECTED", "证据缺失：增氧动作需要可信溶氧读数")
        if not latest_do.is_fresh():
            return PolicyResult(False, "REJECTED", "溶氧证据过期或质量不足，需要刷新数据")
        if latest_do.value >= 4.0:
            return PolicyResult(False, "REJECTED", "溶氧未低于安全线，拒绝无依据增氧动作")
    if device.shadow_state == target_state:
        return PolicyResult(False, "ALREADY_SATISFIED", "设备影子状态已达到目标，抑制重复动作")
    if risk == RiskLevel.L2:
        if not approval_granted:
            return PolicyResult(False, "WAITING_APPROVAL", "中风险动作需要人工审批")
    elif risk == RiskLevel.L3:
        return PolicyResult(False, "MANUAL_REQUIRED", "高风险动作只能建议人工执行")
    elif risk != RiskLevel.L1:
        return PolicyResult(False, "REJECTED", "只读动作不能产生设备写命令")
    if idempotency_seen:
        return PolicyResult(False, "ALREADY_SATISFIED", "幂等键已执行，抑制重复命令")
    approval_note = "，审批已通过" if risk == RiskLevel.L2 else ""
    health_note = "，设备健康状态异常，仍允许下发并等待设备确认" if not device.healthy else ""
    evidence_note = "多模态证据已由 Agent 汇总、" if multimodal_evidence else "溶氧证据新鲜、"
    return PolicyResult(True, "AUTHORIZED", "L%s 受控动作通过：%s设备白名单、无重复执行%s%s" % (risk.value[1:], evidence_note, approval_note, health_note))
