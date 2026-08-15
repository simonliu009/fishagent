"""Deterministic weather API adapter used by the demo Agent tools."""

from datetime import datetime, timezone
from typing import Any, Mapping

from fishagent.application.demo_data import DEMO_WEATHER


class MockWeatherApi:
    """Expose the shape of a weather API without making an external request."""

    endpoint = "https://uapis.cn/api/v1/misc/weather"

    def __init__(self, observations: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.observations = observations or DEMO_WEATHER

    def forecast(self, pond_id: str = "", horizon: str = "明日") -> list[dict[str, Any]]:
        selected = [
            (key, value)
            for key, value in self.observations.items()
            if not pond_id or key == pond_id
        ]
        fetched_at = datetime.now(timezone.utc).isoformat()
        return [
            {
                "pond_id": key,
                **dict(value),
                "horizon": horizon,
                "source": "mock-weather-api",
                "endpoint": self.endpoint,
                "fetched_at": fetched_at,
            }
            for key, value in selected
        ]
