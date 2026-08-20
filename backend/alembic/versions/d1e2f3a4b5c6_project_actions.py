"""project actions

Revision ID: d1e2f3a4b5c6
Revises: c7d4f0a82e11
Create Date: 2026-08-20 08:50:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "c7d4f0a82e11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runners",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("host", sa.String(253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("identity_file", sa.Text(), nullable=False),
        sa.Column("workspace_root", sa.Text(), nullable=False),
        sa.Column("host_key", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(160), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_check_ok", sa.Boolean(), nullable=True),
        sa.Column("last_check_error", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "action_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by_id", sa.BigInteger(), nullable=False),
        sa.Column("action_key", sa.String(120), nullable=False),
        sa.Column("action_name", sa.String(160), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("runner_name", sa.String(80), nullable=False),
        sa.Column("runner_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("internal_stage", sa.String(30), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("branch_name", sa.String(240), nullable=True),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("env_file", sa.Text(), nullable=True),
        sa.Column("env_names", postgresql.JSONB(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_step", sa.Integer(), nullable=True),
        sa.Column("process_group", sa.BigInteger(), nullable=True),
        sa.Column("workspace_path", sa.Text(), nullable=True),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("secret_path", sa.Text(), nullable=True),
        sa.Column("log_offset", sa.BigInteger(), nullable=False),
        sa.Column("recent_output", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cleanup_pending", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')",
            name="ck_action_runs_status",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_action_runs_project_id", "action_runs", ["project_id"])
    op.create_index("ix_action_runs_requested_by_id", "action_runs", ["requested_by_id"])
    op.create_index("ix_action_runs_requested_at", "action_runs", ["requested_at"])
    op.create_index("ix_action_runs_status", "action_runs", ["status"])
    op.create_index("ix_action_runs_queue", "action_runs", ["status", "requested_at", "id"])
    op.create_index("ix_action_runs_project_history", "action_runs", ["project_id", "requested_at"])
    op.create_table(
        "action_steps",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log_start", sa.BigInteger(), nullable=True),
        sa.Column("log_end", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'skipped')",
            name="ck_action_steps_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["action_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "position"),
    )
    op.create_index("ix_action_steps_run_id", "action_steps", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_action_steps_run_id", table_name="action_steps")
    op.drop_table("action_steps")
    op.drop_index("ix_action_runs_project_history", table_name="action_runs")
    op.drop_index("ix_action_runs_queue", table_name="action_runs")
    op.drop_index("ix_action_runs_status", table_name="action_runs")
    op.drop_index("ix_action_runs_requested_at", table_name="action_runs")
    op.drop_index("ix_action_runs_requested_by_id", table_name="action_runs")
    op.drop_index("ix_action_runs_project_id", table_name="action_runs")
    op.drop_table("action_runs")
    op.drop_table("runners")
