from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .git import slug
from .models import Event, Project, Task


class TaskCreationError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def create_task(
    session: AsyncSession,
    project_id: int,
    title: str,
    goal: str,
    *,
    priority: int = 0,
    created_source: str = "web",
) -> Task:
    project = await session.scalar(
        select(Project).where(Project.id == project_id).with_for_update()
    )
    if project is None:
        raise TaskCreationError(404, "project not found")
    if len(goal.encode()) > 1024 * 1024:
        raise TaskCreationError(413, "megaprompt must be at most 1 MiB")

    sequence = project.next_task_sequence
    project.next_task_sequence += 1
    task_id = f"{project.key}-{sequence}"
    repository = Path(project.repository_path).resolve()
    prompt_directory = repository / "prompts"
    try:
        prompt_directory.mkdir(parents=True, exist_ok=True)
        if not prompt_directory.resolve().is_relative_to(repository):
            raise TaskCreationError(422, "project prompts path leaves the repository")
        prompt_path = prompt_directory / (
            f"{datetime.now().astimezone().date().isoformat()}-{task_id}-{slug(title)}.md"
        )
        with prompt_path.open("x", encoding="utf-8") as destination:
            destination.write(goal)
    except FileExistsError as error:
        raise TaskCreationError(409, "task prompt already exists") from error
    except OSError as error:
        raise TaskCreationError(500, "could not save task prompt") from error

    task = Task(
        id=task_id,
        project=project,
        sequence=sequence,
        title=title,
        goal=goal,
        prompt_path=str(prompt_path.relative_to(repository)),
        priority=priority,
        created_source=created_source,
    )
    session.add_all(
        [task, Event(task=task, type="task.created", payload={"source": created_source})]
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        prompt_path.unlink(missing_ok=True)
        raise
    return task
