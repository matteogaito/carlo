"""enable carlo planning skill

Revision ID: c7d4f0a82e11
Revises: 288887f9351f
Create Date: 2026-08-18 11:30:00
"""
from collections.abc import Sequence

from alembic import op


revision: str = "c7d4f0a82e11"
down_revision: str | None = "288887f9351f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE agent_profiles
        SET default_skills = default_skills || '["carlo-planning"]'::jsonb
        WHERE name = 'plan'
          AND NOT default_skills @> '["carlo-planning"]'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE agent_profiles
        SET default_skills = default_skills - 'carlo-planning'
        WHERE name = 'plan'
        """
    )
