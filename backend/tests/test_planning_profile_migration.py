import importlib.util
from datetime import UTC, datetime
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


def load_migration(filename: str):
    path = Path(__file__).parents[1] / f"alembic/versions/{filename}.py"
    spec = importlib.util.spec_from_file_location(filename, path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def run_migration(module, operation: str):
    def invoke(connection):
        module.op = Operations(MigrationContext.configure(connection))
        getattr(module, operation)()

    return invoke


async def test_unify_planning_profiles_preserves_legacy_references() -> None:
    migration = load_migration("b4c5d6e7f8a9_unify_planning_profiles")
    normalization = load_migration("c5d6e7f8a9b0_normalize_plan_workflow_skill")
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

    async with engine.begin() as connection:
        await connection.run_sync(run_migration(migration, "upgrade"))
        await connection.run_sync(run_migration(normalization, "upgrade"))

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
        await connection.run_sync(run_migration(migration, "downgrade"))

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


async def test_superseded_subtask_migration_preserves_existing_tasks() -> None:
    migration = load_migration("d6e7f8a9b0c1_superseded_subtasks")
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        project = Project(name="Migration", key="MIG", repository_path="/tmp/migration")
        parent = Task(id="MIG-1", project=project, sequence=1, title="Parent", goal="Parent")
        session.add_all(
            [
                parent,
                Task(
                    id="MIG-2",
                    project=project,
                    sequence=2,
                    title="Child",
                    goal="Child",
                    parent=parent,
                    subtask_position=0,
                ),
            ]
        )
        await session.commit()

    async with engine.begin() as connection:
        await connection.run_sync(run_migration(migration, "downgrade"))
        assert await connection.scalar(select(Task.id).where(Task.id == "MIG-2")) == "MIG-2"
        await connection.run_sync(run_migration(migration, "upgrade"))

    async with factory() as session:
        old = await session.get(Task, "MIG-2")
        parent = await session.get(Task, "MIG-1")
        old.superseded_at = datetime.now(UTC)
        session.add(
            Task(
                id="MIG-3",
                project=parent.project,
                sequence=3,
                title="Replacement",
                goal="Replacement",
                parent=parent,
                subtask_position=0,
            )
        )
        await session.commit()
        assert set(await session.scalars(select(Task.id))) == {"MIG-1", "MIG-2", "MIG-3"}

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()
