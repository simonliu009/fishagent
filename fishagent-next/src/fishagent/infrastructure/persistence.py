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
                    cursor.execute("CREATE SEQUENCE IF NOT EXISTS fishagent_event_sequence AS BIGINT START WITH 1")
                    cursor.execute(
                        """
                        SELECT setval(
                            'fishagent_event_sequence',
                            GREATEST(COALESCE((SELECT MAX(sequence) FROM fishagent_outbox), 0), 1),
                            COALESCE((SELECT MAX(sequence) FROM fishagent_outbox), 0) > 0
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

    def save(self, payload: dict) -> int:
        self.ensure_schema()
        events = payload.get("events", [])
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(817263541)")
                for event in events:
                    cursor.execute(
                        """
                        INSERT INTO fishagent_outbox
                            (sequence, event_id, occurred_at, event_type, correlation_id, summary, payload)
                        VALUES (nextval('fishagent_event_sequence'), %s, %s::timestamptz, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (event_id) DO UPDATE SET event_id = EXCLUDED.event_id
                        RETURNING sequence
                        """,
                        (
                            event["event_id"],
                            event["occurred_at"],
                            event["event_type"],
                            event.get("correlation_id"),
                            event["summary"],
                            json.dumps(event.get("payload", {}), ensure_ascii=False, default=str),
                        ),
                    )
                    event["sequence"] = cursor.fetchone()[0]
                cursor.execute("SELECT COALESCE(MAX(sequence), 0) FROM fishagent_outbox")
                event_sequence = max(int(payload.get("event_sequence", 0)), int(cursor.fetchone()[0]))
                payload["event_sequence"] = event_sequence
                encoded = json.dumps(payload, ensure_ascii=False, default=str)
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
                self._sync_domain_projection(cursor, payload)
                return event_sequence

    @staticmethod
    def _sync_domain_projection(cursor, payload: dict) -> None:
        """Project the JSON-compatible domain snapshot into relational tables."""
        cursor.execute("TRUNCATE farms, schedules, audit_events CASCADE")
        sensor_ids = {item["id"] for item in payload.get("sensors", [])}
        cursor.executemany(
            "INSERT INTO farms (id, name, location) VALUES (%s, %s, %s)",
            [(item["id"], item["name"], item.get("location", "")) for item in payload.get("farms", [])],
        )
        cursor.executemany(
            "INSERT INTO ponds (id, farm_id, name, species, dissolved_oxygen_min) VALUES (%s, %s, %s, %s, %s)",
            [
                (item["id"], item.get("farm_id") or None, item["name"], item.get("species", ""), item.get("dissolved_oxygen_min", 4.0))
                for item in payload.get("ponds", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO sensors (id, pond_id, name, metric, unit, status, freshness_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (item["id"], item["pond_id"], item["name"], item["metric"], item["unit"], item.get("status", "ONLINE"), item.get("freshness_seconds", 120))
                for item in payload.get("sensors", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO devices (id, pond_id, name, capability, shadow_state, healthy) VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (item["id"], item["pond_id"], item["name"], item["capability"], item.get("shadow_state", "off"), item.get("healthy", True))
                for item in payload.get("devices", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO cameras (id, pond_id, name, source_type, status, last_frame_at) VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (item["id"], item["pond_id"], item["name"], item["source_type"], item.get("status", "UNAVAILABLE"), item.get("last_frame_at"))
                for item in payload.get("cameras", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO sensor_readings (source_event_id, pond_id, sensor_id, metric, value, unit, quality, sampled_at, received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)",
            [
                (item["source_event_id"], item["pond_id"], item.get("sensor_id") if item.get("sensor_id") in sensor_ids else None, item["metric"], item["value"], item["unit"], item.get("quality", "GOOD"), item["sampled_at"], item["received_at"])
                for item in payload.get("readings", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO incidents (id, pond_id, title, status, risk, opened_at, payload) VALUES (%s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()), %s::jsonb)",
            [
                (item["id"], item["pond_id"], item["title"], item["status"], item["risk"], item.get("verification_due_at"), json.dumps(item, ensure_ascii=False, default=str))
                for item in payload.get("incidents", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO action_proposals (id, incident_id, device_id, target_state, risk, status, rationale, evidence_refs, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::timestamptz)",
            [
                (item["id"], item["incident_id"], item["device_id"], item["target_state"], item["risk"], item.get("status", "PROPOSED"), item.get("rationale", ""), json.dumps(item.get("evidence_refs", [])), item["created_at"])
                for item in payload.get("action_proposals", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO approvals (id, proposal_id, status, requested_by, decided_by, reason, created_at, decided_at) VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)",
            [
                (item["id"], item["proposal_id"], item["status"], item.get("requested_by", "execution-agent"), item.get("decided_by"), item.get("reason", ""), item["created_at"], item.get("decided_at"))
                for item in payload.get("approvals", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO device_commands (id, device_id, pond_id, target_state, risk, idempotency_key, status, policy_reason, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)",
            [
                (item["id"], item["device_id"], item["pond_id"], item["target_state"], item["risk"], item["idempotency_key"], item["status"], item.get("policy_reason", ""), item["created_at"])
                for item in payload.get("commands", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO schedules (id, name, job_type, interval_seconds, status, next_run_at, last_run_at) VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)",
            [
                (item["id"], item["name"], item["job_type"], item["interval_seconds"], item["status"], item.get("next_run_at"), item.get("last_run_at"))
                for item in payload.get("schedules", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO scheduled_jobs (id, job_type, idempotency_key, due_at, incident_id, schedule_id, status, attempts, created_at) VALUES (%s, %s, %s, %s::timestamptz, %s, %s, %s, %s, %s::timestamptz)",
            [
                (item["id"], item["job_type"], item["idempotency_key"], item["due_at"], item.get("incident_id"), item.get("schedule_id"), item["status"], item.get("attempts", 0), item["created_at"])
                for item in payload.get("scheduled_jobs", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO manual_tasks (id, incident_id, title, description, assignee, priority, status, created_at, completed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)",
            [
                (item["id"], item.get("incident_id"), item["title"], item["description"], item["assignee"], item["priority"], item["status"], item["created_at"], item.get("completed_at"))
                for item in payload.get("manual_tasks", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO verification_plans (id, incident_id, metric, threshold, earliest_at, latest_at, status) VALUES (%s, %s, %s, %s, %s::timestamptz, %s::timestamptz, %s)",
            [
                (item["id"], item["incident_id"], item["metric"], item["threshold"], item.get("earliest_at"), item.get("latest_at"), item["status"])
                for item in payload.get("verification_plans", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO verification_results (id, incident_id, plan_id, outcome, observed_value, evidence_refs, created_at) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::timestamptz)",
            [
                (item["id"], item["incident_id"], item["plan_id"], item["outcome"], item.get("observed_value"), json.dumps(item.get("evidence_refs", [])), item["created_at"])
                for item in payload.get("verification_results", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO agent_runs (id, goal, incident_id, status, stop_reason, delegated_agents, steps, started_at) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, now())",
            [
                (item["id"], item["goal"], item.get("incident_id"), item["status"], item.get("stop_reason"), json.dumps(item.get("delegated_agents", []), ensure_ascii=False), json.dumps(item.get("steps", []), ensure_ascii=False, default=str))
                for item in payload.get("agent_runs", [])
            ],
        )
        cursor.executemany(
            "INSERT INTO audit_events (actor, action, resource_type, resource_id, correlation_id, payload, created_at) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::timestamptz)",
            [
                ("system", event["event_type"], "event", event.get("event_id"), event.get("correlation_id"), json.dumps(event.get("payload", {}), ensure_ascii=False, default=str), event["occurred_at"])
                for event in payload.get("events", [])
            ],
        )


def repository_from_config(database_url: str) -> Optional[PostgresStateRepository]:
    return PostgresStateRepository(database_url) if database_url.strip() else None
