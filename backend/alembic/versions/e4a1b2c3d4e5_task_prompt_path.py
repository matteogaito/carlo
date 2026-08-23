"""task prompt path

Revision ID: e4a1b2c3d4e5
Revises: d1e2f3a4b5c6
Create Date: 2026-08-21 12:45:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e4a1b2c3d4e5"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("prompt_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "prompt_path")
