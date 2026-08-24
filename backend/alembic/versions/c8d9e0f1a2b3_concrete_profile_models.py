"""select concrete models in agent profiles

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-24 15:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c8d9e0f1a2b3"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE agent_profiles AS profile "
        "SET available_model_id = provider.default_model_id "
        "FROM model_providers AS provider "
        "WHERE profile.model_provider_id = provider.id "
        "AND profile.available_model_id IS NULL "
        "AND provider.default_model_id IS NOT NULL"
    )
    op.drop_constraint(
        "ck_agent_profiles_one_model_selection", "agent_profiles", type_="check"
    )
    op.drop_constraint(
        "fk_agent_profiles_model_provider_id", "agent_profiles", type_="foreignkey"
    )
    op.drop_column("agent_profiles", "model_provider_id")
    op.drop_column("agent_profiles", "model")
    op.drop_constraint(
        "fk_model_providers_default_model_id", "model_providers", type_="foreignkey"
    )
    op.drop_column("model_providers", "default_model_id")


def downgrade() -> None:
    op.add_column(
        "model_providers", sa.Column("default_model_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_model_providers_default_model_id",
        "model_providers",
        "available_models",
        ["default_model_id"],
        ["id"],
    )
    op.add_column(
        "agent_profiles", sa.Column("model", sa.String(length=160), nullable=True)
    )
    op.add_column(
        "agent_profiles", sa.Column("model_provider_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_agent_profiles_model_provider_id",
        "agent_profiles",
        "model_providers",
        ["model_provider_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_agent_profiles_one_model_selection",
        "agent_profiles",
        "NOT (model_provider_id IS NOT NULL AND available_model_id IS NOT NULL)",
    )
