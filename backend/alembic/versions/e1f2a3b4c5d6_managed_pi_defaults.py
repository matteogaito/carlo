"""managed Pi defaults

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
Create Date: 2026-08-24 18:35:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pi_runtime_settings",
        sa.Column(
            "default_packages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[\"superpowers\", \"ponytail\"]'::jsonb"),
        ),
    )
    op.add_column(
        "pi_runtime_settings",
        sa.Column(
            "default_skills",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "agent_profiles",
        sa.Column(
            "default_packages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_profiles", "default_packages")
    op.drop_column("pi_runtime_settings", "default_skills")
    op.drop_column("pi_runtime_settings", "default_packages")
