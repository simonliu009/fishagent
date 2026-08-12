import json
import threading
from typing import Optional


class PersistenceError(RuntimeError):
    """Raised when the durable state store cannot be reached or written."""


class PostgresStateRepository:
    """Small synchronous state/outbox adapter used by the modular monolith.

    The domain remains storage-agnostic for tests, while production keeps one
    durable JSONB snapshot plus an append-only outbox. Domain invariants still
    live in the application service; PostgreSQL provides restart recovery and
    transactional persistence for the current vertical slice.
    """

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - exercised by misconfigured deployments
            raise PersistenceError("psycopg is required when FISHAGENT_DATABASE_URL is configured") from exc
        try:
            return psycopg.connect(self.database_url)
        except Exception as exc:  # pragma: no cover - depends on external service state
            raise PersistenceError("unable to connect to PostgreSQL") from exc

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS fishagent_state (
                            id SMALLINT PRIMARY KEY CHECK (id = 1),
                            version BIGINT NOT NULL,
                            payload JSONB NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS fishagent_outbox (
                            sequence BIGINT PRIMARY KEY,
                            event_id TEXT NOT NULL UNIQUE,
                            occurred_at TIMESTAMPTZ NOT NULL,
                            event_type TEXT NOT NULL,
                            correlation_id TEXT,
                            summary TEXT NOT NULL,
                            payload JSONB NOT NULL,
                            published_at TIMESTAMPTZ
                        )
                        """
                    )
            self._schema_ready = True

    def health(self) -> dict:
        self.ensure_schema()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return {"status": "ok", "backend": "postgres"}

    def load(self) -> Optional[dict]:
        self.ensure_schema()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM fishagent_state WHERE id = 1")
                row = cursor.fetchone()
        if not row:
            return None
        payload = row[0]
        return json.loads(payload) if isinstance(payload, str) else payload

    def save(self, payload: dict) -> None:
        self.ensure_schema()
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        events = payload.get("events", [])
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO fishagent_state (id, version, payload, updated_at)
                    VALUES (1, 1, %s::jsonb, now())
                    ON CONFLICT (id) DO UPDATE SET
                        version = fishagent_state.version + 1,
                        payload = EXCLUDED.payload,
                        updated_at = now()
                    """,
                    (encoded,),
                )
                for event in events:
                    cursor.execute(
                        """
                        INSERT INTO fishagent_outbox
                            (sequence, event_id, occurred_at, event_type, correlation_id, summary, payload)
                        VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (
                            event["sequence"],
                            event["event_id"],
                            event["occurred_at"],
                            event["event_type"],
                            event.get("correlation_id"),
                            event["summary"],
                            json.dumps(event.get("payload", {}), ensure_ascii=False, default=str),
                        ),
                    )


def repository_from_config(database_url: str) -> Optional[PostgresStateRepository]:
    return PostgresStateRepository(database_url) if database_url.strip() else None
