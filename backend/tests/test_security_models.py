from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.models import (
    Base,
    Event,
    NotificationCursor,
    NotificationDelivery,
    Project,
    ProjectMembership,
    User,
    UserSession,
)


@pytest.mark.asyncio
async def test_security_schema_supports_admin_membership_sessions_and_delivery() -> None:
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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        project = Project(name="CARLO", key="CAR", repository_path="/repo")
        session.add_all([user, project])
        await session.flush()
        session.add(
            ProjectMembership(
                user_id=user.id, project_id=project.id, role="contributor"
            )
        )
        session.add(
            UserSession(
                user_id=user.id,
                token_digest="a" * 64,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        event = Event(type="task.created", payload={})
        session.add(event)
        await session.flush()
        session.add(NotificationCursor(destination="telegram:123", last_sequence=0))
        session.add(
            NotificationDelivery(
                event_sequence=event.sequence,
                destination="telegram:123",
            )
        )
        await session.commit()

        assert await session.scalar(select(func.count(ProjectMembership.id))) == 1
        assert await session.scalar(select(func.count(UserSession.id))) == 1
        assert await session.scalar(select(func.count(NotificationDelivery.id))) == 1

    await engine.dispose()
