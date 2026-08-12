import json
from typing import Optional


class RedisEventPublisher:
    """Best-effort live event publisher backed by Redis Pub/Sub."""

    def __init__(self, redis_url: str, channel: str = "fishagent.events") -> None:
        self.redis_url = redis_url
        self.channel = channel
        self._client = None
        self._last_sequence = 0
        self.last_error: Optional[str] = None

    def _get_client(self):
        if self._client is None:
            try:
                import redis

                self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
            except ImportError as exc:  # pragma: no cover - deployment configuration
                raise RuntimeError("redis package is required when FISHAGENT_REDIS_URL is configured") from exc
        return self._client

    def health(self) -> dict:
        try:
            self._get_client().ping()
        except Exception as exc:  # pragma: no cover - depends on external service state
            self.last_error = str(exc)
            return {"status": "degraded", "backend": "redis", "detail": "Redis 暂不可用"}
        self.last_error = None
        return {"status": "ok", "backend": "redis", "channel": self.channel}

    def publish(self, events: list[dict]) -> int:
        if not events:
            return 0
        client = self._get_client()
        published = 0
        for event in events:
            sequence = int(event.get("sequence", 0))
            if sequence <= self._last_sequence:
                continue
            client.publish(self.channel, json.dumps(event, ensure_ascii=False, default=str))
            self._last_sequence = sequence
            published += 1
        self.last_error = None
        return published


def publisher_from_config(redis_url: str) -> Optional[RedisEventPublisher]:
    return RedisEventPublisher(redis_url) if redis_url.strip() else None
