"""superseded subtasks

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-09-06 09:30:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d6e7f8a9b0c1"
down_revision: str | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint("uq_tasks_parent_position", "tasks", type_="unique")
    op.create_index(
        "uq_tasks_active_parent_position",
        "tasks",
        ["parent_task_id", "subtask_position"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_active_parent_position", table_name="tasks")
    op.create_unique_constraint(
        "uq_tasks_parent_position", "tasks", ["parent_task_id", "subtask_position"]
    )
    op.drop_column("tasks", "superseded_at")
