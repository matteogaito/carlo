from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.models import (
    Base,
    Event,
    NotificationCursor,
    NotificationDelivery,
)
from carlo.telegram import TelegramNotifier, format_event, telegram_enabled


class FakeTransport:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.messages: list[tuple[str, str, str]] = []

    async def send(self, token: str, chat_id: str, message: str) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("telegram unavailable")
        self.messages.append((token, chat_id, message))


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
    factory: async_sessionmaker[AsyncSession], event_type: str, task_id: str | None = None
) -> Event:
    async with factory() as session:
        event = Event(type=event_type, task_id=task_id, payload={"revision": 2})
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


@pytest.mark.parametrize(
    "event_type",
    ["planning.failed", "execution.blocked", "execution.failed", "execution.interrupted"],
)
def test_failure_and_blocked_events_are_blocking(event_type: str) -> None:
    assert format_event(Event(type=event_type, payload={}))[1] == "blocking"


def test_missing_placeholders_disable_telegram() -> None:
    assert not telegram_enabled("CHANGE_ME", "123")
    assert not telegram_enabled("token", "CHANGE_ME")
    assert telegram_enabled("token", "123")


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
