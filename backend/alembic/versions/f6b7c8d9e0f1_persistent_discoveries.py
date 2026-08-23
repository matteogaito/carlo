"""persistent discoveries

Revision ID: f6b7c8d9e0f1
Revises: e4a1b2c3d4e5
Create Date: 2026-08-23 08:10:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f6b7c8d9e0f1"
down_revision: str | None = "e4a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("planning_session_id", sa.String(length=160), nullable=True))
    op.add_column("tasks", sa.Column("planning_cursor", sa.String(length=160), nullable=True))
    op.add_column("tasks", sa.Column("planning_question", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_table(
        "discoveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("provider_session_id", sa.String(length=160), nullable=False),
        sa.Column("session_path", sa.Text(), nullable=True),
        sa.Column("provider_cursor", sa.String(length=160), nullable=True),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("memory_path", sa.Text(), nullable=False),
        sa.Column("final_summary", sa.Text(), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('OPEN', 'CLOSED')", name="ck_discoveries_status"),
        sa.ForeignKeyConstraint(["profile_id"], ["agent_profiles.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_session_id"),
    )
    op.create_index(op.f("ix_discoveries_project_id"), "discoveries", ["project_id"])
    op.create_index(op.f("ix_discoveries_status"), "discoveries", ["status"])
    op.create_index(op.f("ix_discoveries_last_active_at"), "discoveries", ["last_active_at"])
    op.create_index(
        "ix_discoveries_project_status_activity",
        "discoveries",
        ["project_id", "status", "last_active_at"],
    )

    op.create_table(
        "discovery_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("discovery_id", sa.BigInteger(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("provider_entry_id", sa.String(length=160), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool', 'system')",
            name="ck_discovery_messages_role",
        ),
        sa.ForeignKeyConstraint(["discovery_id"], ["discoveries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discovery_id", "provider_entry_id"),
        sa.UniqueConstraint("discovery_id", "sequence"),
    )
    op.create_index(op.f("ix_discovery_messages_discovery_id"), "discovery_messages", ["discovery_id"])

    op.create_table(
        "discovery_turns",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("discovery_id", sa.BigInteger(), nullable=False),
        sa.Column("input_message_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider_request_id", sa.String(length=160), nullable=True),
        sa.Column("provider_cursor", sa.String(length=160), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('CHAT', 'CLOSE')", name="ck_discovery_turns_kind"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'INTERRUPTED', 'FAILED')",
            name="ck_discovery_turns_status",
        ),
        sa.ForeignKeyConstraint(["discovery_id"], ["discoveries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["input_message_id"], ["discovery_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("input_message_id"),
    )
    op.create_index(op.f("ix_discovery_turns_discovery_id"), "discovery_turns", ["discovery_id"])
    op.create_index(op.f("ix_discovery_turns_status"), "discovery_turns", ["status"])
    op.create_index("ix_discovery_turns_queue", "discovery_turns", ["status", "created_at", "id"])

    op.add_column("events", sa.Column("discovery_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_events_discovery_id"), "events", ["discovery_id"])
    op.create_foreign_key(
        "fk_events_discovery_id_discoveries",
        "events",
        "discoveries",
        ["discovery_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_events_discovery_id_discoveries", "events", type_="foreignkey")
    op.drop_index(op.f("ix_events_discovery_id"), table_name="events")
    op.drop_column("events", "discovery_id")
    op.drop_index("ix_discovery_turns_queue", table_name="discovery_turns")
    op.drop_index(op.f("ix_discovery_turns_status"), table_name="discovery_turns")
    op.drop_index(op.f("ix_discovery_turns_discovery_id"), table_name="discovery_turns")
    op.drop_table("discovery_turns")
    op.drop_index(op.f("ix_discovery_messages_discovery_id"), table_name="discovery_messages")
    op.drop_table("discovery_messages")
    op.drop_index("ix_discoveries_project_status_activity", table_name="discoveries")
    op.drop_index(op.f("ix_discoveries_last_active_at"), table_name="discoveries")
    op.drop_index(op.f("ix_discoveries_status"), table_name="discoveries")
    op.drop_index(op.f("ix_discoveries_project_id"), table_name="discoveries")
    op.drop_table("discoveries")
    op.drop_column("tasks", "planning_question")
    op.drop_column("tasks", "planning_cursor")
    op.drop_column("tasks", "planning_session_id")
