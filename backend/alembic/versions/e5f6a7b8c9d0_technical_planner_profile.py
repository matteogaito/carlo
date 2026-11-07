"""Give technical planning its own configurable Pi profile.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
"""
from collections.abc import Sequence

from alembic import op


revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO agent_profiles (
            name, provider, effort, permissions, default_skills,
            default_packages, context_policy, active, available_model_id
        )
        SELECT 'technical-plan', provider, 'low', permissions,
               '["carlo-planning"]'::jsonb, default_packages,
               context_policy, active, available_model_id
        FROM agent_profiles WHERE name = 'plan'
        ON CONFLICT (name) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO agent_profile_packages (agent_profile_id, package_id)
        SELECT technical.id, packages.package_id
        FROM agent_profiles AS technical
        JOIN agent_profiles AS feature ON feature.name = 'plan'
        JOIN agent_profile_packages AS packages ON packages.agent_profile_id = feature.id
        WHERE technical.name = 'technical-plan'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE tasks SET active_profile_id = (SELECT id FROM agent_profiles WHERE name = 'plan')
        WHERE active_profile_id = (SELECT id FROM agent_profiles WHERE name = 'technical-plan')
        """
    )
    op.execute(
        """
        UPDATE attempts SET profile_id = (SELECT id FROM agent_profiles WHERE name = 'plan')
        WHERE profile_id = (SELECT id FROM agent_profiles WHERE name = 'technical-plan')
        """
    )
    op.execute("DELETE FROM agent_profiles WHERE name = 'technical-plan'")
