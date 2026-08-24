"""centralized model providers

Revision ID: b7c8d9e0f1a2
Revises: f6b7c8d9e0f1
Create Date: 2026-08-24 11:10:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "f6b7c8d9e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("credential_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("credential_key_version", sa.Integer(), nullable=False),
        sa.Column("credential_hint", sa.String(length=20), nullable=True),
        sa.Column(
            "compatibility",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("refresh_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("last_refresh_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refresh_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refresh_status", sa.String(length=20), nullable=False),
        sa.Column("last_refresh_error", sa.Text(), nullable=True),
        sa.Column("default_model_id", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "pi_runtime_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("compaction_enabled", sa.Boolean(), nullable=False),
        sa.Column("reserve_percent", sa.Integer(), nullable=False),
        sa.Column("keep_recent_percent", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_pi_runtime_settings_singleton"),
        sa.CheckConstraint(
            "reserve_percent BETWEEN 1 AND 90",
            name="ck_pi_runtime_settings_reserve_percent",
        ),
        sa.CheckConstraint(
            "keep_recent_percent BETWEEN 1 AND 90",
            name="ck_pi_runtime_settings_keep_recent_percent",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "available_models",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("model_provider_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.String(length=240), nullable=False),
        sa.Column("display_name", sa.String(length=240), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("discovered_context_window", sa.Integer(), nullable=True),
        sa.Column("discovered_max_tokens", sa.Integer(), nullable=True),
        sa.Column("context_window_override", sa.Integer(), nullable=True),
        sa.Column("max_tokens_override", sa.Integer(), nullable=True),
        sa.Column(
            "input_modalities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("reasoning", sa.Boolean(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('AVAILABLE', 'UNAVAILABLE')",
            name="ck_available_models_status",
        ),
        sa.ForeignKeyConstraint(
            ["model_provider_id"], ["model_providers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_provider_id", "external_id"),
    )
    op.create_index(
        op.f("ix_available_models_model_provider_id"),
        "available_models",
        ["model_provider_id"],
    )
    op.create_index(
        op.f("ix_available_models_status"), "available_models", ["status"]
    )
    op.create_foreign_key(
        "fk_model_providers_default_model_id",
        "model_providers",
        "available_models",
        ["default_model_id"],
        ["id"],
    )
    op.add_column(
        "agent_profiles", sa.Column("model_provider_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "agent_profiles", sa.Column("available_model_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_agent_profiles_model_provider_id",
        "agent_profiles",
        "model_providers",
        ["model_provider_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_agent_profiles_available_model_id",
        "agent_profiles",
        "available_models",
        ["available_model_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_agent_profiles_one_model_selection",
        "agent_profiles",
        "NOT (model_provider_id IS NOT NULL AND available_model_id IS NOT NULL)",
    )
    op.add_column(
        "tasks", sa.Column("available_model_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_tasks_available_model_id",
        "tasks",
        "available_models",
        ["available_model_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        "INSERT INTO pi_runtime_settings "
        "(id, compaction_enabled, reserve_percent, keep_recent_percent) "
        "VALUES (1, true, 10, 20)"
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_available_model_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "available_model_id")
    op.drop_constraint(
        "ck_agent_profiles_one_model_selection", "agent_profiles", type_="check"
    )
    op.drop_constraint(
        "fk_agent_profiles_available_model_id", "agent_profiles", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_agent_profiles_model_provider_id", "agent_profiles", type_="foreignkey"
    )
    op.drop_column("agent_profiles", "available_model_id")
    op.drop_column("agent_profiles", "model_provider_id")
    op.drop_constraint(
        "fk_model_providers_default_model_id", "model_providers", type_="foreignkey"
    )
    op.drop_index(op.f("ix_available_models_status"), table_name="available_models")
    op.drop_index(
        op.f("ix_available_models_model_provider_id"), table_name="available_models"
    )
    op.drop_table("available_models")
    op.drop_table("pi_runtime_settings")
    op.drop_table("model_providers")
