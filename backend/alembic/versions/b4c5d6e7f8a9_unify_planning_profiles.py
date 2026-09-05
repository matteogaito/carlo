"""unify planning profiles

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-09-05 16:30:00
"""
from collections.abc import Sequence

from alembic import op


revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table, column in (
        ("discoveries", "profile_id"),
        ("tasks", "active_profile_id"),
        ("attempts", "profile_id"),
    ):
        op.execute(
            f"""
            UPDATE {table}
            SET {column} = (SELECT id FROM agent_profiles WHERE name = 'plan')
            WHERE {column} IN (
                SELECT id FROM agent_profiles WHERE name IN ('brief', 'discovery')
            )
            """
        )
    op.execute(
        """
        DELETE FROM agent_profile_packages
        WHERE agent_profile_id IN (
            SELECT id FROM agent_profiles WHERE name IN ('brief', 'discovery')
        )
        """
    )
    op.execute("DELETE FROM agent_profiles WHERE name IN ('brief', 'discovery')")


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO agent_profiles (
            name, provider, effort, permissions, default_skills,
            default_packages, context_policy, active, available_model_id
        )
        SELECT mode.name, plan.provider, plan.effort,
               CASE WHEN mode.name = 'discovery'
                    THEN '{"tools":["read","bash","grep","find","ls","discovery_state"]}'::jsonb
                    ELSE plan.permissions END,
               jsonb_build_array(mode.skill)
                   || (plan.default_skills - 'carlo-planning' - 'carlo-discovery'),
               plan.default_packages, plan.context_policy, plan.active,
               plan.available_model_id
        FROM agent_profiles AS plan
        CROSS JOIN (
            VALUES ('brief', 'carlo-planning'), ('discovery', 'carlo-discovery')
        ) AS mode(name, skill)
        WHERE plan.name = 'plan'
        """
    )
    op.execute(
        """
        INSERT INTO agent_profile_packages (agent_profile_id, package_id)
        SELECT restored.id, packages.package_id
        FROM agent_profiles AS restored
        CROSS JOIN agent_profiles AS plan
        JOIN agent_profile_packages AS packages ON packages.agent_profile_id = plan.id
        WHERE restored.name IN ('brief', 'discovery') AND plan.name = 'plan'
        """
    )
