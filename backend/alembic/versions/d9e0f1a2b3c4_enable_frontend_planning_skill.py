"""enable managed frontend design for planning

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-08-24 16:00:00
"""
from collections.abc import Sequence

from alembic import op


revision: str = "d9e0f1a2b3c4"
down_revision: str | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE agent_profiles "
        "SET default_skills = default_skills || '[\"frontend-design\"]'::jsonb "
        "WHERE name = 'plan' "
        "AND NOT default_skills ? 'frontend-design'"
    )


def downgrade() -> None:
    # Existing profiles may already have selected this skill; do not destroy user data.
    pass
