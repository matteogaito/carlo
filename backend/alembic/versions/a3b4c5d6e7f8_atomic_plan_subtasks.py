"""atomic plan subtasks

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-25 18:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("parent_task_id", sa.String(length=80)))
    op.add_column("tasks", sa.Column("subtask_position", sa.Integer()))
    op.create_foreign_key(
        "fk_tasks_parent_task_id",
        "tasks",
        "tasks",
        ["parent_task_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])
    op.create_unique_constraint(
        "uq_tasks_parent_position", "tasks", ["parent_task_id", "subtask_position"]
    )
    op.create_check_constraint(
        "ck_tasks_subtask_position",
        "tasks",
        "subtask_position IS NULL OR subtask_position >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tasks_subtask_position", "tasks", type_="check")
    op.drop_constraint("uq_tasks_parent_position", "tasks", type_="unique")
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_constraint("fk_tasks_parent_task_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "subtask_position")
    op.drop_column("tasks", "parent_task_id")
