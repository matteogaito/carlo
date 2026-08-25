import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from carlo.models import Base, Event, Project, Task


@pytest.mark.asyncio
async def test_shared_task_creation_allocates_identity_and_prompt(tmp_path: Path) -> None:
    assert importlib.util.find_spec("carlo.tasks") is not None, "shared task creation is missing"
    from carlo.tasks import create_task

    repository = tmp_path / "repo"
    repository.mkdir()
    key = f"S{uuid4().hex[:6].upper()}"
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        project = Project(name="Shared tasks", key=key, repository_path=str(repository))
        session.add(project)
        await session.commit()
        first = await create_task(session, project.id, "First goal", "First prompt")
        second = await create_task(session, project.id, "Second goal", "Second prompt")

        assert [first.id, second.id] == [f"{key}-1", f"{key}-2"]
        assert first.status.value == "NOT_READY"
        assert first.stage.value == "created"
        assert (repository / first.prompt_path).read_text() == "First prompt"
        assert (repository / second.prompt_path).read_text() == "Second prompt"
        events = (
            await session.scalars(
                select(Event).where(Event.task_id.in_([first.id, second.id])).order_by(Event.sequence)
            )
        ).all()
        assert [event.type for event in events] == ["task.created", "task.created"]
        assert await session.get(Task, second.id) is not None

    await engine.dispose()
