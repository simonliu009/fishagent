"""Create zones, sensor health, patrol findings and escalation tables."""

from alembic import op

revision = "0005_health_patrol_tables"
down_revision = "0004_workflow_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS zones (
            id TEXT PRIMARY KEY,
            farm_id TEXT NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS sensor_health (
            sensor_id TEXT PRIMARY KEY REFERENCES sensors(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            last_heartbeat_at TIMESTAMPTZ,
            last_reading_at TIMESTAMPTZ,
            error_count INTEGER NOT NULL DEFAULT 0,
            drift_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS patrol_findings (
            id TEXT PRIMARY KEY,
            patrol_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            pond_id TEXT NOT NULL REFERENCES ponds(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            confidence DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS escalations (
            id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
            level TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            manual_task_id TEXT REFERENCES manual_tasks(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_sensor_health_status ON sensor_health (status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS ix_patrol_findings_run ON patrol_findings (patrol_run_id, created_at);
        CREATE INDEX IF NOT EXISTS ix_escalations_open ON escalations (status, created_at DESC);
        """
    )


def downgrade() -> None:
    for table in ("escalations", "patrol_findings", "sensor_health", "zones"):
        op.execute("DROP TABLE IF EXISTS %s CASCADE" % table)
