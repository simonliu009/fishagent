from dataclasses import dataclass

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
    latest_do: SensorReading,
    idempotency_seen: bool,
) -> PolicyResult:
    if actor != "execution-agent":
        return PolicyResult(False, "REJECTED", "只有执行 Agent 可提交经过策略门的动作请求")
    if device.pond_id != pond_id:
        return PolicyResult(False, "REJECTED", "设备与池塘不匹配")
    if device.capability != "aeration" or target_state != "on":
        return PolicyResult(False, "REJECTED", "设备能力或目标状态不在低风险白名单")
    if not latest_do.is_fresh():
        return PolicyResult(False, "REJECTED", "核心证据过期或质量不足，需要刷新数据")
    if latest_do.value >= 4.0:
        return PolicyResult(False, "REJECTED", "溶氧未低于阈值，拒绝无依据动作")
    if device.shadow_state == target_state:
        return PolicyResult(False, "ALREADY_SATISFIED", "设备影子状态已达到目标，抑制重复动作")
    if risk == RiskLevel.L2:
        return PolicyResult(False, "WAITING_APPROVAL", "中风险动作需要人工审批")
    if risk == RiskLevel.L3:
        return PolicyResult(False, "MANUAL_REQUIRED", "高风险动作只能建议人工执行")
    if risk != RiskLevel.L1:
        return PolicyResult(False, "REJECTED", "只读动作不能产生设备写命令")
    if idempotency_seen:
        return PolicyResult(False, "ALREADY_SATISFIED", "幂等键已执行，抑制重复命令")
    return PolicyResult(True, "AUTHORIZED", "L1 低风险动作通过：证据新鲜、阈值满足、设备白名单、无重复执行")
