"""Pi package catalog

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-25 00:08:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pi_packages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("identity", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active_version", sa.String(length=255), nullable=True),
        sa.Column("active_artifact_path", sa.Text(), nullable=True),
        sa.Column(
            "resources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_update_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_update_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_update_status",
            sa.String(length=20),
            nullable=False,
            server_default="NEVER",
        ),
        sa.Column("last_update_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity"),
    )
    op.create_table(
        "agent_profile_packages",
        sa.Column("agent_profile_id", sa.BigInteger(), nullable=False),
        sa.Column("package_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"], ["agent_profiles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["package_id"], ["pi_packages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_profile_id", "package_id"),
    )
    op.execute(
        """
        INSERT INTO pi_packages (source, identity, enabled, pinned, is_default)
        VALUES
          ('git:github.com/obra/superpowers', 'superpowers', true, false,
           EXISTS (SELECT 1 FROM pi_runtime_settings,
             jsonb_array_elements_text(default_packages) value WHERE value = 'superpowers')),
          ('git:github.com/DietrichGebert/ponytail', 'ponytail', true, false,
           EXISTS (SELECT 1 FROM pi_runtime_settings,
             jsonb_array_elements_text(default_packages) value WHERE value = 'ponytail'))
        """
    )
    op.execute(
        """
        INSERT INTO agent_profile_packages (agent_profile_id, package_id)
        SELECT profile.id, package.id
        FROM agent_profiles profile
        JOIN pi_packages package
          ON profile.default_packages ? package.identity
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("agent_profile_packages")
    op.drop_table("pi_packages")
