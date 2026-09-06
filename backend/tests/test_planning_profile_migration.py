import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from carlo.domain import TaskStage, TaskStatus
from carlo.models import (
    AgentProfile,
    AgentProfilePackage,
    Attempt,
    Base,
    Discovery,
    PiPackage,
    Project,
    Task,
)


async def test_unify_planning_profiles_preserves_legacy_references() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/b4c5d6e7f8a9_unify_planning_profiles.py"
    )
    spec = importlib.util.spec_from_file_location(
        "planning_profile_migration", migration_path
    )
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    normalization_path = (
        Path(__file__).parents[1]
        / "alembic/versions/c5d6e7f8a9b0_normalize_plan_workflow_skill.py"
    )
    spec = importlib.util.spec_from_file_location(
        "plan_skill_normalization", normalization_path
    )
    assert spec and spec.loader
    normalization = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(normalization)
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        plan = AgentProfile(
            name="plan",
            provider="pi",
            default_skills=["carlo-planning", "carlo-discovery", "frontend-design"],
        )
        brief = AgentProfile(name="brief", provider="pi")
        discovery_profile = AgentProfile(name="discovery", provider="pi")
        package = PiPackage(source="test", identity="test-package")
        project = Project(name="Migration", key="MIG", repository_path="/tmp/migration")
        session.add_all([plan, brief, discovery_profile, package, project])
        await session.flush()
        task = Task(
            id="MIG-1",
            project_id=project.id,
            sequence=1,
            title="Migration",
            goal="Preserve references",
            status=TaskStatus.READY,
            stage=TaskStage.QUEUED,
            active_profile_id=brief.id,
        )
        session.add_all(
            [
                task,
                Discovery(
                    project_id=project.id,
                    title="Migration",
                    profile_id=discovery_profile.id,
                    provider_session_id="migration-test",
                    state={},
                    memory_path="migration.md",
                ),
                AgentProfilePackage(
                    agent_profile_id=discovery_profile.id, package_id=package.id
                ),
                AgentProfilePackage(agent_profile_id=plan.id, package_id=package.id),
            ]
        )
        await session.flush()
        session.add(
            Attempt(
                task_id=task.id,
                number=1,
                profile_id=discovery_profile.id,
                instruction="test",
            )
        )
        await session.commit()

    def run(module, operation: str):
        def invoke(connection):
            module.op = Operations(MigrationContext.configure(connection))
            getattr(module, operation)()

        return invoke

    async with engine.begin() as connection:
        await connection.run_sync(run(migration, "upgrade"))
        await connection.run_sync(run(normalization, "upgrade"))

    async with factory() as session:
        profiles = {profile.name: profile for profile in await session.scalars(select(AgentProfile))}
        task = await session.get(Task, "MIG-1")
        attempt = await session.scalar(select(Attempt).where(Attempt.task_id == "MIG-1"))
        discovery = await session.scalar(
            select(Discovery).where(Discovery.provider_session_id == "migration-test")
        )
        assert set(profiles) == {"plan"}
        assert profiles["plan"].default_skills == [
            "carlo-planning",
            "frontend-design",
        ]
        assert {task.active_profile_id, attempt.profile_id, discovery.profile_id} == {
            profiles["plan"].id
        }
        associations = list(await session.scalars(select(AgentProfilePackage)))
        assert [association.agent_profile_id for association in associations] == [
            profiles["plan"].id
        ]

    async with engine.begin() as connection:
        await connection.run_sync(run(migration, "downgrade"))

    async with factory() as session:
        profiles = {profile.name: profile for profile in await session.scalars(select(AgentProfile))}
        assert set(profiles) == {"plan", "brief", "discovery"}
        assert profiles["brief"].default_skills[0] == "carlo-planning"
        assert profiles["discovery"].default_skills[0] == "carlo-discovery"
        associations = list(await session.scalars(select(AgentProfilePackage)))
        assert {association.agent_profile_id for association in associations} == {
            profile.id for profile in profiles.values()
        }

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()
