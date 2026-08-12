import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from fishagent.application.policy import evaluate_action
from fishagent.domain.models import Device, RiskLevel, SensorReading, utcnow


class PolicyPropertyTests(unittest.TestCase):
    @settings(max_examples=50, deadline=None)
    @given(
        value=st.floats(min_value=-100, max_value=100, allow_nan=False, allow_infinity=False),
        risk=st.sampled_from(list(RiskLevel)),
        stale=st.booleans(),
        approval=st.booleans(),
    )
    def test_policy_never_allows_unsafe_risk_or_evidence(self, value, risk, stale, approval) -> None:
        sampled_at = utcnow()
        if stale:
            from datetime import timedelta

            sampled_at -= timedelta(seconds=121)
        reading = SensorReading(
            pond_id="B-01",
            sensor_id="do-b-01",
            metric="DO",
            value=value,
            unit="mg/L",
            sampled_at=sampled_at,
        )
        result = evaluate_action(
            actor="execution-agent",
            device=Device(id="aerator-b01-1", pond_id="B-01", name="增氧机", capability="aeration", shadow_state="off"),
            pond_id="B-01",
            target_state="on",
            risk=risk,
            latest_do=reading,
            idempotency_seen=False,
            approval_granted=approval,
        )
        if stale or value >= 4.0 or risk in {RiskLevel.L0, RiskLevel.L3} or (risk == RiskLevel.L2 and not approval):
            self.assertFalse(result.allowed)


if __name__ == "__main__":
    unittest.main()
