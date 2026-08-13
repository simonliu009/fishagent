"""Structured contracts exchanged between the LLM and the domain service."""

from dataclasses import dataclass, field

DECISION_ACTIONS = {
    "EXECUTE",
    "REQUEST_APPROVAL",
    "MANUAL_REQUIRED",
    "NO_ACTION",
    "REFRESH_EVIDENCE",
}
RISK_LEVELS = {"L1", "L2", "L3"}


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

    @classmethod
    def from_payload(cls, payload: object) -> "IncidentDecision":
        if not isinstance(payload, dict):
            raise ValueError("LLM decision must be a JSON object")
        action = str(payload.get("action") or "").upper().strip()
        if action not in DECISION_ACTIONS:
            raise ValueError("unsupported LLM decision action")
        risk = str(payload.get("risk") or "L3").upper().strip()
        if risk not in RISK_LEVELS:
            raise ValueError("unsupported LLM risk level")
        target_state = str(payload.get("target_state") or "").lower().strip()
        if target_state not in {"", "on", "off"}:
            raise ValueError("unsupported target state")
        device_id = str(payload.get("device_id") or "").strip()
        rationale = str(payload.get("rationale") or "").strip()
        if not rationale:
            raise ValueError("LLM decision rationale is required")
        try:
            verification_delay = int(payload.get("verification_delay_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise ValueError("verification delay must be an integer") from exc
        if not 5 <= verification_delay <= 3600:
            raise ValueError("verification delay must be between 5 and 3600 seconds")
        refs = payload.get("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
            raise ValueError("evidence_refs must be a list of strings")
        if action in {"EXECUTE", "REQUEST_APPROVAL"} and (not device_id or target_state not in {"on", "off"}):
            raise ValueError("device_id and target_state on/off are required for an action")
        if action == "NO_ACTION" and target_state:
            raise ValueError("NO_ACTION cannot contain a target state")
        return cls(
            action=action,
            device_id=device_id,
            target_state=target_state,
            risk=risk,
            rationale=rationale[:2000],
            verification_delay_seconds=verification_delay,
            evidence_refs=refs[:20],
        )
