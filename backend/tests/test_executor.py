import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.domain import TaskStage, TaskStatus
from carlo.models import Base, Project, Task
from carlo.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_implementation_is_globally_serialized() -> None:
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        project = Project(name="CARLO", key="CAR", repository_path="/tmp/carlo-test")
        session.add(project)
        await session.flush()
        session.add_all(
            [
                Task(
                    id=f"CAR-{number}",
                    project=project,
                    sequence=number,
                    title=f"Task {number}",
                    goal="Test serialization",
                    status=TaskStatus.READY,
                    stage=TaskStage.QUEUED,
                )
                for number in (1, 2)
            ]
        )
        await session.commit()

    entered: asyncio.Queue[str] = asyncio.Queue()
    release: asyncio.Queue[None] = asyncio.Queue()
    active = 0
    max_active = 0

    async def runner(task_id: str) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await entered.put(task_id)
        await release.get()
        active -= 1
        return "validated"

    first = asyncio.create_task(Orchestrator(engine, factory, runner).run_next())
    assert await asyncio.wait_for(entered.get(), 1) == "CAR-1"
    second = asyncio.create_task(Orchestrator(engine, factory, runner).run_next())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(entered.get(), 0.1)

    await release.put(None)
    assert await first == "CAR-1"
    assert await asyncio.wait_for(entered.get(), 1) == "CAR-2"
    await release.put(None)
    assert await second == "CAR-2"
    assert max_active == 1

    async with factory() as session:
        assert (await session.get(Task, "CAR-1")).status == TaskStatus.DONE
        assert (await session.get(Task, "CAR-2")).status == TaskStatus.DONE
    await engine.dispose()


@pytest.mark.asyncio
async def test_active_task_is_recovered_before_a_ready_task() -> None:
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        project = Project(name="CARLO", key="CAR", repository_path="/tmp/carlo-test")
        session.add(project)
        await session.flush()
        session.add_all(
            [
                Task(
                    id="CAR-1",
                    project=project,
                    sequence=1,
                    title="Recover",
                    goal="Resume validation",
                    status=TaskStatus.IN_PROGRESS,
                    stage=TaskStage.VALIDATING,
                ),
                Task(
                    id="CAR-2",
                    project=project,
                    sequence=2,
                    title="Wait",
                    goal="Stay queued",
                    status=TaskStatus.READY,
                    stage=TaskStage.QUEUED,
                ),
            ]
        )
        await session.commit()

    calls: list[str] = []

    async def runner(task_id: str) -> str:
        calls.append(task_id)
        return "validated"

    assert await Orchestrator(engine, factory, runner).run_next() == "CAR-1"
    assert calls == ["CAR-1"]
    async with factory() as session:
        assert (await session.get(Task, "CAR-1")).status == TaskStatus.DONE
        assert (await session.get(Task, "CAR-2")).status == TaskStatus.READY
    await engine.dispose()
