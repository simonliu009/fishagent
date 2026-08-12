"""Persist validated camera frame references and metadata."""

from alembic import op

revision = "0006_vision_frames"
down_revision = "0005_health_patrol_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE cameras ADD COLUMN IF NOT EXISTS source_url TEXT NOT NULL DEFAULT '';
        ALTER TABLE cameras ADD COLUMN IF NOT EXISTS privacy_policy TEXT NOT NULL DEFAULT 'EVENT_ONLY';
        ALTER TABLE cameras ADD COLUMN IF NOT EXISTS last_frame_id TEXT;
        ALTER TABLE cameras ADD COLUMN IF NOT EXISTS last_frame_hash TEXT;
        ALTER TABLE cameras ADD COLUMN IF NOT EXISTS last_frame_width INTEGER;
        ALTER TABLE cameras ADD COLUMN IF NOT EXISTS last_frame_height INTEGER;
        CREATE TABLE IF NOT EXISTS vision_frames (
            id TEXT PRIMARY KEY,
            camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
            source_url TEXT NOT NULL DEFAULT '',
            object_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_vision_frames_camera_time
            ON vision_frames (camera_id, captured_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS vision_frames")
