"""Create workflow tables for schedules, manual tasks and verification."""

from alembic import op

revision = "0004_workflow_tables"
down_revision = "0003_domain_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            job_type TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL,
            status TEXT NOT NULL,
            next_run_at TIMESTAMPTZ,
            last_run_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            due_at TIMESTAMPTZ NOT NULL,
            incident_id TEXT REFERENCES incidents(id) ON DELETE CASCADE,
            schedule_id TEXT REFERENCES schedules(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS manual_tasks (
            id TEXT PRIMARY KEY,
            incident_id TEXT REFERENCES incidents(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            assignee TEXT NOT NULL,
            priority TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS verification_plans (
            id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
            metric TEXT NOT NULL,
            threshold DOUBLE PRECISION NOT NULL,
            earliest_at TIMESTAMPTZ,
            latest_at TIMESTAMPTZ,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS verification_results (
            id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
            plan_id TEXT NOT NULL REFERENCES verification_plans(id) ON DELETE CASCADE,
            outcome TEXT NOT NULL,
            observed_value DOUBLE PRECISION,
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_scheduled_jobs_due ON scheduled_jobs (status, due_at);
        CREATE INDEX IF NOT EXISTS ix_verification_results_incident ON verification_results (incident_id, created_at DESC);
        """
    )


def downgrade() -> None:
    for table in ("verification_results", "verification_plans", "manual_tasks", "scheduled_jobs", "schedules"):
        op.execute("DROP TABLE IF EXISTS %s CASCADE" % table)
