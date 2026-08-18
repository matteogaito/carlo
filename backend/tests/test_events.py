from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.api import create_app
from carlo.config import Settings
from carlo.models import Base, Event
from tests.fakes import FakeProvider


@pytest.mark.asyncio
async def test_websocket_and_http_replay_persisted_events() -> None:
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
    await bootstrap_admin(factory, "admin", "admin-password")
    async with factory() as session:
        session.add_all(
            [Event(sequence=number, type=f"event.{number}", payload={"number": number}) for number in range(1, 5)]
        )
        await session.commit()

    app = create_app(factory, FakeProvider("{}"), Settings(app_origin="http://testserver"))
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect("/api/ws?after=1"):
                pass
        assert rejected.value.code == 4401
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        assert login.status_code == 200
        with client.websocket_connect("/api/ws?after=1") as websocket:
            assert websocket.receive_json()["sequence"] == 2
            assert websocket.receive_json()["sequence"] == 3
        replay = client.get("/api/events?after=3")
        assert replay.status_code == 200
        assert [event["sequence"] for event in replay.json()] == [4]
    await engine.dispose()
