"""Allocate Outbox sequence numbers in PostgreSQL for multi-process writes."""

from alembic import op

revision = "0002_global_event_sequence"
down_revision = "0001_runtime_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS fishagent_event_sequence AS BIGINT START WITH 1")
    op.execute(
        """
        SELECT setval(
            'fishagent_event_sequence',
            GREATEST(COALESCE((SELECT MAX(sequence) FROM fishagent_outbox), 0), 1),
            COALESCE((SELECT MAX(sequence) FROM fishagent_outbox), 0) > 0
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS fishagent_event_sequence")
