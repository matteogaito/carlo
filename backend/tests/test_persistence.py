from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from carlo.models import Base, Event, Project, Task


@pytest.mark.asyncio
async def test_task_identity_and_event_survive_a_new_session() -> None:
    engine = create_async_engine("postgresql+psycopg:///carlov3")
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
        session.add(Event(sequence=1, task=task, type="task.created", payload={}))
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
