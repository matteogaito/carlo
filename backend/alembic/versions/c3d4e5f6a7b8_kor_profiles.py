"""Assign the Kor Git skill to planning and implementation profiles.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""
from collections.abc import Sequence

from alembic import op


revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO pi_packages (source, identity, enabled, pinned, is_default)
        VALUES ('git:github.com/matteogaito/kor', 'git:github.com/matteogaito/kor', true, false, false)
        ON CONFLICT (identity) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO agent_profile_packages (agent_profile_id, package_id)
        SELECT profile.id, package.id
        FROM agent_profiles profile
        CROSS JOIN pi_packages package
        WHERE profile.name IN ('plan', 'implementation')
          AND package.identity = 'git:github.com/matteogaito/kor'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM agent_profile_packages
        WHERE package_id IN (
            SELECT id FROM pi_packages WHERE identity = 'git:github.com/matteogaito/kor'
        )
        """
    )
    op.execute("DELETE FROM pi_packages WHERE identity = 'git:github.com/matteogaito/kor'")
