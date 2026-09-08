"""enable pi sandbox

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-09-07 12:00:00
"""
from collections.abc import Sequence

from alembic import op


revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO pi_packages (
            source, identity, enabled, pinned, is_default, resources, last_update_status
        )
        VALUES ('npm:pi-sandbox', 'npm:pi-sandbox', true, false, true, '{}'::jsonb, 'NEVER')
        ON CONFLICT (identity) DO UPDATE
        SET source = EXCLUDED.source,
            enabled = true,
            pinned = false,
            is_default = true
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM pi_packages WHERE identity = 'npm:pi-sandbox'")
