from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.domain import TaskStage, TaskStatus
from carlo.models import (
    ActionRun,
    Base,
    Event,
    NotificationCursor,
    NotificationDelivery,
    Project,
    Task,
    User,
)
from carlo.telegram import (
    TelegramCommandBot,
    TelegramNotifier,
    format_event,
    telegram_enabled,
)


class FakeTransport:
    def __init__(self, failures: int = 0, updates: list[dict] | None = None) -> None:
        self.failures = failures
        self.messages: list[tuple[str, str, str]] = []
        self.updates = updates or []
        self.commands: list[dict[str, str]] = []
        self.offsets: list[int] = []

    async def send(self, token: str, chat_id: str, message: str) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("telegram unavailable")
        self.messages.append((token, chat_id, message))

    async def set_commands(
        self, token: str, commands: list[dict[str, str]]
    ) -> None:
        self.commands = commands

    async def get_updates(
        self, token: str, offset: int, timeout: int
    ) -> list[dict]:
        self.offsets.append(offset)
        return [update for update in self.updates if update["update_id"] >= offset]


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE notification_deliveries, notification_cursors, login_failures, "
                "user_sessions, project_memberships, users, events, validation_runs, "
                "escalations, attempts, plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    result = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield result
    await engine.dispose()


async def add_event(
    factory: async_sessionmaker[AsyncSession],
    event_type: str,
    task_id: str | None = None,
    payload: dict | None = None,
) -> Event:
    async with factory() as session:
        event = Event(type=event_type, task_id=task_id, payload=payload or {"revision": 2})
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event


def test_format_includes_task_and_severity() -> None:
    event = Event(type="planning.completed", task_id="CAR-7", payload={"revision": 2})

    message, severity = format_event(event)

    assert severity == "info"
    assert "CAR-7" in message
    assert "Plan completed" in message
    assert "revision: 2" in message


def test_planning_failure_only_includes_the_short_error() -> None:
    event = Event(
        type="planning.failed",
        task_id="CAR-7",
        payload={"error": "Pi event exceeded 4 MiB", "internal": "noisy detail"},
    )

    message, severity = format_event(event)

    assert severity == "blocking"
    assert "error: Pi event exceeded 4 MiB" in message
    assert "internal" not in message
    assert "noisy detail" not in message


@pytest.mark.parametrize(
    "event_type",
    [
        "planning.failed",
        "planning.question",
        "execution.blocked",
        "execution.failed",
        "execution.interrupted",
        "escalation.completed",
    ],
)
def test_failure_and_blocked_events_are_blocking(event_type: str) -> None:
    assert format_event(Event(type=event_type, payload={}))[1] == "blocking"


def test_missing_placeholders_disable_telegram() -> None:
    assert not telegram_enabled("CHANGE_ME", "123")
    assert not telegram_enabled("token", "CHANGE_ME")
    assert telegram_enabled("token", "123")


@pytest.mark.asyncio
async def test_status_command_is_private_reports_work_and_persists_offset(factory) -> None:
    async with factory() as session:
        project = Project(name="ECADMO", key="ECA", repository_path="/tmp/ecadmo")
        user = User(username="admin", password_hash="unused", role="admin")
        session.add_all([project, user])
        await session.flush()
        session.add_all(
            [
                Task(
                    id="ECA-1",
                    project_id=project.id,
                    sequence=1,
                    title="Marketplace integration",
                    goal="Implement it",
                    status=TaskStatus.IN_PROGRESS,
                    stage=TaskStage.IMPLEMENTING,
                ),
                Task(
                    id="ECA-2",
                    project_id=project.id,
                    sequence=2,
                    title="Plan next feature",
                    goal="Plan it",
                    status=TaskStatus.NOT_READY,
                    stage=TaskStage.PLANNING,
                ),
                Task(
                    id="ECA-3",
                    project_id=project.id,
                    sequence=3,
                    title="Queued work",
                    goal="Do it",
                    status=TaskStatus.READY,
                    stage=TaskStage.QUEUED,
                ),
                ActionRun(
                    project_id=project.id,
                    requested_by_id=user.id,
                    action_key="deploy-dev",
                    action_name="Deploy to Dev",
                    definition={},
                    runner_name="local",
                    status="running",
                    internal_stage="executing",
                    commit_sha="a" * 40,
                    artifact_path="/tmp/action.log",
                    current_step=2,
                ),
            ]
        )
        await session.commit()

    transport = FakeTransport(
        updates=[
            {"update_id": 10, "message": {"chat": {"id": 999}, "text": "/status"}},
            {"update_id": 11, "message": {"chat": {"id": 123}, "text": "/status"}},
        ]
    )
    bot = TelegramCommandBot(factory, transport, "token", "123")

    assert await bot.poll_once() is True
    assert transport.commands == [
        {"command": "status", "description": "Show what CARLO is doing"}
    ]
    assert len(transport.messages) == 1
    message = transport.messages[0][2]
    assert "ECA-1 — implementing" in message
    assert "Deploy to Dev #1 — executing, step 2" in message
    assert "ECA-2 — planning" in message
    assert "Ready queue: 1" in message
    async with factory() as session:
        cursor = await session.get(NotificationCursor, "telegram-inbound:123")
    assert cursor is not None
    assert cursor.last_sequence == 11

    assert await bot.poll_once() is False
    assert len(transport.messages) == 1


@pytest.mark.asyncio
async def test_delivery_skips_history_and_is_not_duplicated_after_restart(factory) -> None:
    old = await add_event(factory, "task.created")
    transport = FakeTransport()
    notifier = TelegramNotifier(factory, transport, "token", "123", "all")

    await notifier.initialize_cursor()
    async with factory() as session:
        cursor = await session.get(NotificationCursor, "telegram:123")
        assert cursor is not None
        assert cursor.last_sequence == old.sequence

    current = await add_event(factory, "planning.completed")
    assert await notifier.deliver_next() is True
    assert len(transport.messages) == 1
    assert "Plan completed" in transport.messages[0][2]

    restarted = TelegramNotifier(factory, transport, "token", "123", "all")
    assert await restarted.deliver_next() is False
    assert len(transport.messages) == 1
    async with factory() as session:
        delivery = await session.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.event_sequence == current.sequence
            )
        )
        assert delivery is not None
        assert delivery.status == "sent"


@pytest.mark.asyncio
async def test_all_level_skips_internal_planning_activity(factory) -> None:
    transport = FakeTransport()
    notifier = TelegramNotifier(factory, transport, "token", "123", "all")
    await notifier.initialize_cursor()
    started = await add_event(factory, "planning.tool.started")
    completed = await add_event(factory, "planning.tool.completed")
    await add_event(factory, "planning.completed")

    assert await notifier.deliver_next() is True
    assert await notifier.deliver_next() is True
    assert await notifier.deliver_next() is True
    assert len(transport.messages) == 1
    assert "Plan completed" in transport.messages[0][2]
    async with factory() as session:
        deliveries = (
            await session.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.event_sequence.in_(
                        (started.sequence, completed.sequence)
                    )
                )
                .order_by(NotificationDelivery.event_sequence)
            )
        ).all()
        assert [delivery.status for delivery in deliveries] == ["skipped", "skipped"]


@pytest.mark.asyncio
async def test_blocking_level_skips_info_and_sends_blocker(factory) -> None:
    transport = FakeTransport()
    notifier = TelegramNotifier(factory, transport, "token", "123", "blocking")
    await notifier.initialize_cursor()
    await add_event(factory, "planning.completed")
    await add_event(factory, "execution.failed")

    assert await notifier.deliver_next() is True
    assert transport.messages == []
    assert await notifier.deliver_next() is True
    assert len(transport.messages) == 1
    assert "BLOCKING" in transport.messages[0][2]


@pytest.mark.asyncio
async def test_explicit_system_notifications_ignore_blocking_filter(factory) -> None:
    transport = FakeTransport()
    notifier = TelegramNotifier(factory, transport, "token", "123", "blocking")
    await notifier.initialize_cursor()
    await add_event(factory, "system.started")
    await add_event(factory, "worker.started")
    await add_event(factory, "pi.update_completed")
    await add_event(
        factory,
        "pi.resources_updated",
        payload={"summary": "superpowers@abc1234, frontend-design@def5678"},
    )
    await add_event(
        factory,
        "pi.packages_update_completed",
        payload={"summary": "1 updated · 1 unchanged · 0 failed"},
    )

    assert await notifier.deliver_next() is True
    assert await notifier.deliver_next() is True
    assert await notifier.deliver_next() is True
    assert await notifier.deliver_next() is True
    assert await notifier.deliver_next() is True
    assert len(transport.messages) == 5
    assert "CARLO started" in transport.messages[0][2]
    assert "CARLO worker started" in transport.messages[1][2]
    assert "Pi weekly update completed" in transport.messages[2][2]
    assert "Pi skills updated" in transport.messages[3][2]
    assert "superpowers@abc1234" in transport.messages[3][2]
    assert "Pi packages updated" in transport.messages[4][2]


@pytest.mark.asyncio
async def test_failure_retries_then_abandons_without_blocking_later_events(factory) -> None:
    transport = FakeTransport(failures=2)
    notifier = TelegramNotifier(
        factory,
        transport,
        "token",
        "123",
        "all",
        max_attempts=2,
        retry_seconds=0,
    )
    await notifier.initialize_cursor()
    failed = await add_event(factory, "execution.failed")
    later = await add_event(factory, "execution.completed")

    assert await notifier.deliver_next() is True
    assert await notifier.deliver_next() is True
    assert await notifier.deliver_next() is True
    assert len(transport.messages) == 1
    assert "Task completed" in transport.messages[0][2]

    async with factory() as session:
        deliveries = (
            await session.scalars(
                select(NotificationDelivery).order_by(
                    NotificationDelivery.event_sequence
                )
            )
        ).all()
        assert [(item.event_sequence, item.status) for item in deliveries] == [
            (failed.sequence, "abandoned"),
            (later.sequence, "sent"),
        ]
