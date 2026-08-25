from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo import api, models
from carlo.domain import TaskStage, TaskStatus
from carlo.models import Base, Event, Project, Task


@pytest.mark.asyncio
async def test_task_identity_and_event_survive_a_new_session() -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    key = f"T{uuid4().hex[:6].upper()}"
    task_id = f"{key}-1"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        project = Project(name="Test project", key=key, repository_path="/tmp/repo")
        session.add(project)
        await session.flush()
        task = Task(
            id=task_id,
            project_id=project.id,
            sequence=1,
            title="Login",
            goal="Add login",
        )
        session.add(task)
        session.add(Event(task=task, type="task.created", payload={}))
        await session.commit()

    async with AsyncSession(engine) as session:
        task = await session.get(Task, task_id)
        event = await session.scalar(select(Event).where(Event.task_id == task_id))
        assert task is not None
        assert task.goal == "Add login"
        assert task.project.key == key
        assert event is not None
        assert event.type == "task.created"

        await session.delete(task.project)
        await session.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_startup_returns_orphaned_rework_planning_to_failed() -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    key = f"R{uuid4().hex[:6].upper()}"
    task_id = f"{key}-1"
    questioning_task_id = f"{key}-2"
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        project = Project(
            name="Rework project", key=key, repository_path=f"/tmp/{key}"
        )
        task = Task(
            id=task_id,
            project=project,
            sequence=1,
            title="Login",
            goal="Add login",
            status=TaskStatus.NOT_READY,
            stage=TaskStage.PLANNING,
            planning_session_id=f"{task_id}-plan-rework-1",
        )
        questioning = Task(
            id=questioning_task_id,
            project=project,
            sequence=2,
            title="Question",
            goal="Clarify login",
            status=TaskStatus.NOT_READY,
            stage=TaskStage.PLANNING,
            planning_session_id=f"{questioning_task_id}-plan-rework-1",
            planning_question={"text": "Which authentication provider?"},
        )
        session.add_all(
            [
                task,
                questioning,
                Event(task=task, type="task.rework.started", payload={"cycle": 1}),
                Event(
                    task=questioning,
                    type="task.rework.started",
                    payload={"cycle": 1},
                ),
            ]
        )
        await session.commit()

    recover = getattr(api, "recover_interrupted_reworks", None)
    assert recover is not None
    await recover(factory)

    async with factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.status == TaskStatus.FAILED
        assert task.stage == TaskStage.BLOCKED
        event = await session.scalar(
            select(Event).where(
                Event.task_id == task_id,
                Event.type == "task.rework.recovered_after_restart",
            )
        )
        assert event is not None
        questioning = await session.get(Task, questioning_task_id)
        assert questioning is not None
        assert questioning.status == TaskStatus.NOT_READY
        assert questioning.stage == TaskStage.PLANNING
        assert questioning.planning_question == {
            "text": "Which authentication provider?"
        }
    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_transcript_turn_and_event_survive_a_new_session() -> None:
    assert hasattr(models, "Discovery"), "Discovery persistence is missing"
    Discovery = models.Discovery
    DiscoveryMessage = models.DiscoveryMessage
    DiscoveryTurn = models.DiscoveryTurn

    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    key = f"D{uuid4().hex[:6].upper()}"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        project = Project(name="Discovery project", key=key, repository_path=f"/tmp/{key}")
        discovery = Discovery(
            project=project,
            title="Vinted direction",
            status="OPEN",
            state={
                "summary": "",
                "findings": [],
                "decisions": [],
                "unresolved_questions": [],
                "inspected_resources": [],
                "commands": [],
                "proposals": [],
            },
            provider_session_id=f"discovery-{key}",
            memory_path=f"/tmp/discoveries/{key}/MEMORY.md",
        )
        message = DiscoveryMessage(
            discovery=discovery,
            sequence=1,
            role="user",
            content="Explore Vinted",
        )
        turn = DiscoveryTurn(
            discovery=discovery,
            input_message=message,
            kind="CHAT",
            status="QUEUED",
        )
        session.add_all(
            [discovery, message, turn, Event(discovery=discovery, type="discovery.created", payload={})]
        )
        await session.commit()
        discovery_id = discovery.id

    async with AsyncSession(engine) as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery is not None
        assert discovery.status == "OPEN"
        assert discovery.state["findings"] == []
        assert [message.content for message in discovery.messages] == ["Explore Vinted"]
        assert discovery.turns[0].status == "QUEUED"
        event = await session.scalar(select(Event).where(Event.discovery_id == discovery_id))
        assert event is not None
        assert event.type == "discovery.created"
        task = Task(id=f"{key}-1", project=discovery.project, sequence=1, title="Task", goal="Goal")
        session.add(task)
        await session.flush()
        assert task.planning_session_id is None
        await session.delete(discovery.project)
        await session.commit()

    async with AsyncSession(engine) as session:
        assert await session.get(Discovery, discovery_id) is None
        assert await session.scalar(select(Event).where(Event.discovery_id == discovery_id)) is None

    await engine.dispose()
