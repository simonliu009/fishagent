"""Contracts exchanged between model interpretation and device execution.

Model output is external input and is therefore interpreted defensively here.
The device-control Skill remains the strict execution boundary.
"""

import json
import re
from dataclasses import dataclass, field

DECISION_ACTIONS = {
    "EXECUTE",
    "REQUEST_APPROVAL",
    "MANUAL_REQUIRED",
    "NO_ACTION",
    "REFRESH_EVIDENCE",
}
RISK_LEVELS = {"L1", "L2", "L3"}

_ACTION_ALIASES = {
    "EXECUTE": "EXECUTE",
    "EXEC": "EXECUTE",
    "RUN": "EXECUTE",
    "ACT": "EXECUTE",
    "TURN_ON": "EXECUTE",
    "START": "EXECUTE",
    "OPEN": "EXECUTE",
    "ENABLE": "EXECUTE",
    "ON": "EXECUTE",
    "开启": "EXECUTE",
    "启动": "EXECUTE",
    "打开": "EXECUTE",
    "TURN_OFF": "EXECUTE",
    "STOP": "EXECUTE",
    "CLOSE": "EXECUTE",
    "DISABLE": "EXECUTE",
    "OFF": "EXECUTE",
    "关闭": "EXECUTE",
    "停止": "EXECUTE",
    "REQUEST_APPROVAL": "REQUEST_APPROVAL",
    "APPROVAL": "REQUEST_APPROVAL",
    "REQUIRE_APPROVAL": "REQUEST_APPROVAL",
    "待审批": "REQUEST_APPROVAL",
    "审批": "REQUEST_APPROVAL",
    "MANUAL_REQUIRED": "MANUAL_REQUIRED",
    "MANUAL": "MANUAL_REQUIRED",
    "HUMAN_REQUIRED": "MANUAL_REQUIRED",
    "ESCALATE": "MANUAL_REQUIRED",
    "转人工": "MANUAL_REQUIRED",
    "人工": "MANUAL_REQUIRED",
    "NO_ACTION": "NO_ACTION",
    "NOOP": "NO_ACTION",
    "NONE": "NO_ACTION",
    "HOLD": "NO_ACTION",
    "IGNORE": "NO_ACTION",
    "保持现状": "NO_ACTION",
    "无需动作": "NO_ACTION",
    "REFRESH_EVIDENCE": "REFRESH_EVIDENCE",
    "REFRESH": "REFRESH_EVIDENCE",
    "RECHECK": "REFRESH_EVIDENCE",
    "REVERIFY": "REFRESH_EVIDENCE",
    "重新采集": "REFRESH_EVIDENCE",
    "复核": "REFRESH_EVIDENCE",
}

_TARGET_ALIASES = {
    "ON": "on",
    "START": "on",
    "OPEN": "on",
    "ENABLE": "on",
    "开启": "on",
    "启动": "on",
    "打开": "on",
    "OFF": "off",
    "STOP": "off",
    "CLOSE": "off",
    "DISABLE": "off",
    "关闭": "off",
    "停止": "off",
}

_RISK_ALIASES = {
    "L1": "L1",
    "LOW": "L1",
    "LOW_RISK": "L1",
    "低": "L1",
    "低风险": "L1",
    "L2": "L2",
    "MEDIUM": "L2",
    "MEDIUM_RISK": "L2",
    "中": "L2",
    "中风险": "L2",
    "L3": "L3",
    "HIGH": "L3",
    "HIGH_RISK": "L3",
    "高": "L3",
    "高风险": "L3",
}


def _text(value: object, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:limit]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:limit]
    except (TypeError, ValueError):
        return str(value).strip()[:limit]


def _token(value: object) -> str:
    return re.sub(r"[\s\-]+", "_", _text(value).upper()).strip("_")


def _first_value(payload: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return value
    return None


def _normalise_target(value: object) -> str:
    token = _token(value)
    return _TARGET_ALIASES.get(token, "")


def _normalise_action(value: object) -> str:
    token = _token(value)
    return _ACTION_ALIASES.get(token, "")


def _infer_text_action(raw: str) -> tuple[str, str, str]:
    """Interpret common free-form responses without treating them as executable."""
    normalized = raw.lower()
    if any(marker in normalized for marker in ("无需动作", "保持现状", "no action", "noop", "不需要执行")):
        return "NO_ACTION", "", ""
    if any(marker in normalized for marker in ("人工确认", "转人工", "manual", "human", "审批", "approval")):
        action = "REQUEST_APPROVAL" if any(marker in normalized for marker in ("审批", "approval")) else "MANUAL_REQUIRED"
        return action, "", ""
    target = ""
    if any(marker in normalized for marker in ("turn_on", "turn on", "开启", "启动", "打开", "start", "open", "enable")):
        target = "on"
    elif any(marker in normalized for marker in ("turn_off", "turn off", "关闭", "停止", "stop", "close", "disable")):
        target = "off"
    if target:
        device_match = re.search(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){1,}\b", raw, flags=re.IGNORECASE)
        return "EXECUTE", target, device_match.group(0) if device_match else ""
    return "", "", ""


@dataclass
class IncidentDecision:
    """The only model output that may influence an incident workflow."""

    action: str
    device_id: str = ""
    target_state: str = ""
    risk: str = "L3"
    rationale: str = ""
    verification_delay_seconds: int = 30
    evidence_refs: list[str] = field(default_factory=list)
    requires_manual_review: bool = False

    @classmethod
    def from_payload(cls, payload: object) -> "IncidentDecision":
        if isinstance(payload, cls):
            return payload

        requires_manual_review = False
        interpretation_notes: list[str] = []
        if isinstance(payload, str):
            raw = payload.strip()
            parsed: object = None
            decoder = json.JSONDecoder()
            for index, char in enumerate(raw):
                if char != "{":
                    continue
                try:
                    parsed, _ = decoder.raw_decode(raw[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    break
            if isinstance(parsed, dict):
                payload = parsed
            else:
                action, target_state, device_id = _infer_text_action(raw)
                requires_manual_review = True
                if not action:
                    action = "MANUAL_REQUIRED"
                    interpretation_notes.append("无法从模型文本中可靠识别动作")
                else:
                    interpretation_notes.append("已从模型文本中提取动作，仍需执行边界复核")
                rationale = raw or "模型未返回可理解内容"
                return cls(
                    action=action,
                    device_id=device_id,
                    target_state=target_state,
                    risk="L3",
                    rationale=("；".join(interpretation_notes) + "：" + rationale)[:2000],
                    requires_manual_review=requires_manual_review,
                )
        if not isinstance(payload, dict):
            raw = _text(payload) or "模型未返回可理解内容"
            return cls(
                action="MANUAL_REQUIRED",
                risk="L3",
                rationale="无法将模型输出理解为动作对象，已转人工确认：%s" % raw,
                requires_manual_review=True,
            )

        # Some providers wrap the useful object in decision/result/output.
        for wrapper_key in ("decision", "result", "output", "response"):
            nested = payload.get(wrapper_key)
            if isinstance(nested, dict):
                merged = dict(nested)
                merged.update({key: value for key, value in payload.items() if key not in merged and key != wrapper_key})
                payload = merged
                break

        action_value = _first_value(payload, ("action", "command", "operation", "intent"))
        if isinstance(action_value, dict):
            action_value = _first_value(action_value, ("action", "command", "operation", "intent"))
        action = _normalise_action(action_value)
        raw_action = _text(action_value)
        target_value = _first_value(payload, ("target_state", "desired_state", "state", "target"))
        if isinstance(target_value, dict):
            target_value = _first_value(target_value, ("state", "target_state", "value"))
        target_state = _normalise_target(target_value)
        if not target_state and _token(action_value) in {"TURN_ON", "START", "OPEN", "ENABLE", "ON", "开启", "启动", "打开"}:
            target_state = "on"
        if not target_state and _token(action_value) in {"TURN_OFF", "STOP", "CLOSE", "DISABLE", "OFF", "关闭", "停止"}:
            target_state = "off"
        device_value = _first_value(payload, ("device_id", "device", "target_device", "asset_id"))
        if isinstance(device_value, dict):
            device_value = _first_value(device_value, ("id", "device_id", "name"))
        device_id = _text(device_value, 300)
        rationale_value = _first_value(payload, ("rationale", "reason", "explanation", "summary", "message"))
        rationale = _text(rationale_value, 2000)

        if not action:
            action, inferred_target, inferred_device = _infer_text_action(_text(payload))
            target_state = target_state or inferred_target
            device_id = device_id or inferred_device
        if not action:
            action = "MANUAL_REQUIRED"
            requires_manual_review = True
            interpretation_notes.append("模型未提供可识别的动作，已保留内容供人工确认")
        elif not raw_action or _normalise_action(raw_action) != raw_action.upper().strip():
            if raw_action and raw_action.upper().strip() != action:
                interpretation_notes.append("已将模型动作 %s 理解为 %s" % (raw_action, action))

        risk_value = _token(_first_value(payload, ("risk", "risk_level", "severity")) or "L3")
        risk = _RISK_ALIASES.get(risk_value, "L3")
        if risk_value not in _RISK_ALIASES and risk_value not in {"", "L3"}:
            interpretation_notes.append("风险等级 %s 无法直接识别，按 L3 交由执行策略复核" % risk_value)

        delay_value = _first_value(payload, ("verification_delay_seconds", "verification_delay", "delay_seconds"))
        try:
            verification_delay = int(float(delay_value if delay_value is not None else 30))
        except (TypeError, ValueError):
            verification_delay = 30
            interpretation_notes.append("复核时间无法识别，使用默认 30 秒")
        verification_delay = max(5, min(3600, verification_delay))

        refs_value = _first_value(payload, ("evidence_refs", "evidence", "references"))
        if isinstance(refs_value, str):
            try:
                decoded_refs = json.loads(refs_value)
            except (TypeError, ValueError):
                decoded_refs = [item.strip() for item in refs_value.split(",") if item.strip()]
            refs_value = decoded_refs
        refs = [str(item).strip() for item in refs_value] if isinstance(refs_value, list) else []

        if action in {"EXECUTE", "REQUEST_APPROVAL"} and not device_id:
            interpretation_notes.append("缺少设备标识，进入执行 Skill 时将由策略门拒绝并转人工")
        if action in {"EXECUTE", "REQUEST_APPROVAL"} and target_state not in {"on", "off"}:
            interpretation_notes.append("目标状态无法识别，进入执行 Skill 时将由策略门拒绝并转人工")
        if not rationale:
            rationale = "模型未提供明确理由，已根据可识别字段继续处理"
            interpretation_notes.append("缺少处置理由")
        if action == "NO_ACTION":
            target_state = ""
        if interpretation_notes:
            rationale = "%s；%s" % ("；".join(interpretation_notes), rationale)
        return cls(
            action=action,
            device_id=device_id,
            target_state=target_state,
            risk=risk,
            rationale=rationale[:2000],
            verification_delay_seconds=verification_delay,
            evidence_refs=refs[:20],
            requires_manual_review=requires_manual_review,
        )
