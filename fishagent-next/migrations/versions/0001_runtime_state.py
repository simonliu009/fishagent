"""Create the durable runtime snapshot and event outbox."""

from alembic import op

revision = "0001_runtime_state"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fishagent_state (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            version BIGINT NOT NULL,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
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
    op.execute("CREATE INDEX IF NOT EXISTS ix_fishagent_outbox_unpublished ON fishagent_outbox (published_at, sequence)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fishagent_outbox")
    op.execute("DROP TABLE IF EXISTS fishagent_state")
