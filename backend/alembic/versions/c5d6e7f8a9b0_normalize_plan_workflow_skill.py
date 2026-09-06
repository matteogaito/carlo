"""normalize plan workflow skill

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-09-06 00:15:00
"""
from collections.abc import Sequence

from alembic import op


revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE agent_profiles
        SET default_skills = '["carlo-planning"]'::jsonb
            || (default_skills - 'carlo-planning' - 'carlo-discovery')
        WHERE name = 'plan'
        """
    )


def downgrade() -> None:
    pass
