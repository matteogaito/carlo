"""discovery task rework link and coder-expert escalation profile

Revision ID: a1b2c3d4e5f6
Revises: e7f8a9b0c1d2
Create Date: 2026-09-19 00:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discoveries", sa.Column("task_id", sa.String(length=80), nullable=True)
    )
    op.create_foreign_key(
        "fk_discoveries_task_id",
        "discoveries",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_discoveries_task_id", "discoveries", ["task_id"]
    )
    op.execute(
        """
        INSERT INTO agent_profiles (
            name, provider, effort, permissions, default_skills,
            default_packages, context_policy, active, available_model_id
        )
        SELECT
            'coder-expert', implementation.provider, implementation.effort,
            implementation.permissions, implementation.default_skills,
            implementation.default_packages, implementation.context_policy,
            implementation.active, expert_model.id
        FROM agent_profiles AS implementation
        CROSS JOIN (
            SELECT id FROM available_models
            WHERE external_id = 'anthropic/claude-sonnet-5' AND status = 'AVAILABLE'
            LIMIT 1
        ) AS expert_model
        WHERE implementation.name = 'implementation'
        AND NOT EXISTS (SELECT 1 FROM agent_profiles WHERE name = 'coder-expert')
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM agent_profiles WHERE name = 'coder-expert'")
    op.drop_index("ix_discoveries_task_id", table_name="discoveries")
    op.drop_constraint("fk_discoveries_task_id", "discoveries", type_="foreignkey")
    op.drop_column("discoveries", "task_id")
