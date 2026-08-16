from collections.abc import Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .domain import TaskStage, TaskStatus, transition
from .models import Event, Task

IMPLEMENTATION_LOCK = 1_128_352_847
TaskRunner = Callable[[str], Awaitable[str]]


class Orchestrator:
    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        runner: TaskRunner,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.runner = runner

    async def run_next(self) -> str | None:
        async with self.engine.connect() as lock_connection:
            await lock_connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": IMPLEMENTATION_LOCK}
            )
            await lock_connection.commit()
            try:
                task_id = await self._claim_next()
                if task_id is None:
                    return None
                try:
                    outcome = await self.runner(task_id)
                except Exception:
                    await self._finish(task_id, "failed")
                    raise
                await self._finish(task_id, outcome)
                return task_id
            finally:
                await lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": IMPLEMENTATION_LOCK},
                )
                await lock_connection.commit()

    async def _claim_next(self) -> str | None:
        async with self.session_factory() as session:
            task = await session.scalar(
                select(Task)
                .where(Task.status == TaskStatus.READY, Task.stage == TaskStage.QUEUED)
                .order_by(Task.priority.desc(), Task.created_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if task is None:
                return None
            task.status, task.stage = transition(task.status, task.stage, "start")
            task.version += 1
            session.add(Event(task=task, type="execution.started", payload={}))
            await session.commit()
            return task.id

    async def _finish(self, task_id: str, outcome: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise RuntimeError(f"active task {task_id} disappeared")
            if outcome == "validated":
                task.stage = TaskStage.VALIDATING
                task.status, task.stage = transition(
                    task.status, task.stage, "validated"
                )
                event_type = "execution.completed"
            else:
                task.status = TaskStatus.FAILED
                task.stage = TaskStage.BLOCKED
                event_type = "execution.failed"
            task.version += 1
            session.add(Event(task=task, type=event_type, payload={"outcome": outcome}))
            await session.commit()
