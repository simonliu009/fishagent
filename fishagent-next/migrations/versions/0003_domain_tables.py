"""Create the relational domain tables used by the production runtime."""

from alembic import op


revision = "0003_domain_tables"
down_revision = "0002_global_event_sequence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS farms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS ponds (
            id TEXT PRIMARY KEY,
            farm_id TEXT REFERENCES farms(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            species TEXT NOT NULL DEFAULT '',
            dissolved_oxygen_min DOUBLE PRECISION NOT NULL DEFAULT 4.0,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS sensors (
            id TEXT PRIMARY KEY,
            pond_id TEXT NOT NULL REFERENCES ponds(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            metric TEXT NOT NULL,
            unit TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ONLINE',
            freshness_seconds INTEGER NOT NULL DEFAULT 120,
            last_seen_at TIMESTAMPTZ,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            pond_id TEXT NOT NULL REFERENCES ponds(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            capability TEXT NOT NULL,
            shadow_state TEXT NOT NULL DEFAULT 'off',
            healthy BOOLEAN NOT NULL DEFAULT true,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS cameras (
            id TEXT PRIMARY KEY,
            pond_id TEXT NOT NULL REFERENCES ponds(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'UNAVAILABLE',
            last_frame_at TIMESTAMPTZ,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id BIGSERIAL PRIMARY KEY,
            source_event_id TEXT UNIQUE,
            pond_id TEXT NOT NULL REFERENCES ponds(id) ON DELETE CASCADE,
            sensor_id TEXT REFERENCES sensors(id) ON DELETE SET NULL,
            metric TEXT NOT NULL,
            value DOUBLE PRECISION NOT NULL,
            unit TEXT NOT NULL,
            quality TEXT NOT NULL DEFAULT 'GOOD',
            sampled_at TIMESTAMPTZ NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE TABLE IF NOT EXISTS incidents (
            id TEXT PRIMARY KEY,
            pond_id TEXT NOT NULL REFERENCES ponds(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            risk TEXT NOT NULL,
            correlation_id TEXT,
            opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE TABLE IF NOT EXISTS action_proposals (
            id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
            device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
            target_state TEXT NOT NULL,
            risk TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PROPOSED',
            rationale TEXT NOT NULL DEFAULT '',
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL REFERENCES action_proposals(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'PENDING',
            requested_by TEXT NOT NULL,
            decided_by TEXT,
            reason TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            decided_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS device_commands (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
            pond_id TEXT NOT NULL REFERENCES ponds(id) ON DELETE CASCADE,
            target_state TEXT NOT NULL,
            risk TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            policy_reason TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            confirmed_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            incident_id TEXT REFERENCES incidents(id) ON DELETE SET NULL,
            status TEXT NOT NULL,
            stop_reason TEXT,
            delegated_agents JSONB NOT NULL DEFAULT '[]'::jsonb,
            steps JSONB NOT NULL DEFAULT '[]'::jsonb,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS patrol_runs (
            id TEXT PRIMARY KEY,
            farm_id TEXT REFERENCES farms(id) ON DELETE SET NULL,
            status TEXT NOT NULL,
            summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id BIGSERIAL PRIMARY KEY,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT,
            correlation_id TEXT,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_sensor_readings_pond_metric_time
            ON sensor_readings (pond_id, metric, sampled_at DESC);
        CREATE INDEX IF NOT EXISTS ix_incidents_active
            ON incidents (pond_id, status) WHERE status NOT IN ('RESOLVED', 'DISMISSED');
        CREATE INDEX IF NOT EXISTS ix_audit_events_created_at
            ON audit_events (created_at DESC);
        """
    )


def downgrade() -> None:
    for table in (
        "audit_events",
        "patrol_runs",
        "agent_runs",
        "device_commands",
        "approvals",
        "action_proposals",
        "incidents",
        "sensor_readings",
        "cameras",
        "devices",
        "sensors",
        "ponds",
        "farms",
    ):
        op.execute("DROP TABLE IF EXISTS %s CASCADE" % table)
