"""Use the Carlo Superpowers fork for implementation.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""
from collections.abc import Sequence

from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE = "git:github.com/matteogaito/carlo-superpowers"


def upgrade() -> None:
    op.execute(
        """
        UPDATE pi_packages SET enabled = false, is_default = false
        WHERE identity = 'superpowers'
          AND source = 'git:github.com/obra/superpowers'
        """
    )
    op.execute(
        f"""
        INSERT INTO pi_packages (source, identity, enabled, pinned, is_default)
        VALUES ('{SOURCE}', '{SOURCE}', true, false, false)
        ON CONFLICT (identity) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO agent_profile_packages (agent_profile_id, package_id)
        SELECT profile.id, package.id
        FROM agent_profiles profile
        CROSS JOIN pi_packages package
        WHERE profile.name = 'implementation'
          AND package.identity = '{SOURCE}'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM agent_profile_packages
        WHERE package_id IN (SELECT id FROM pi_packages WHERE identity = '{SOURCE}')
        """
    )
    op.execute(f"DELETE FROM pi_packages WHERE identity = '{SOURCE}'")
