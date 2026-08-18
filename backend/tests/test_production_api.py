from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.api import create_app
from carlo.config import Settings
from carlo.models import Base, UserSession
from tests.fakes import FakeProvider


@pytest.fixture
async def production_app(tmp_path: Path):
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
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<h1>CARLO production</h1>")
    (dist / "assets" / "app.js").write_text("console.log('carlo')")
    settings = Settings(
        app_origin="http://test",
        frontend_dist=str(dist),
        session_hours=24,
    )
    yield create_app(factory, FakeProvider("{}"), settings), factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_login_protects_api_and_logout_revokes_cookie(production_app) -> None:
    app, _ = production_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/projects")).status_code == 401

        response = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        assert response.status_code == 200
        assert response.json() == {"username": "admin", "role": "admin"}
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert (await client.get("/api/projects")).status_code == 200
        assert (await client.get("/api/auth/me")).json()["username"] == "admin"

        logout = await client.post(
            "/api/auth/logout", headers={"Origin": "http://test"}
        )
        assert logout.status_code == 204
        assert (await client.get("/api/projects")).status_code == 401


@pytest.mark.asyncio
async def test_login_has_generic_failure_and_throttle_response(production_app) -> None:
    app, _ = production_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.post(
            "/api/auth/login", json={"username": "missing", "password": "wrong-password"}
        )
        wrong = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong-password"}
        )
        assert missing.status_code == wrong.status_code == 401
        assert missing.json() == wrong.json() == {"detail": "invalid credentials"}
        for _ in range(3):
            await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong-password"},
            )
        throttled = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong-password"}
        )
        assert throttled.status_code == 429
        assert throttled.json() == {"detail": "try again later"}


@pytest.mark.asyncio
async def test_mutation_rejects_missing_or_wrong_origin(production_app) -> None:
    app, _ = production_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        payload = {"name": "CARLO", "key": "CAR", "repository_path": "/missing"}
        assert (await client.post("/api/projects", json=payload)).status_code == 403
        response = await client.post(
            "/api/projects",
            json=payload,
            headers={"Origin": "http://attacker.invalid"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_expired_session_is_rejected(production_app) -> None:
    app, factory = production_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        async with factory() as session:
            record = await session.scalar(select(UserSession))
            assert record is not None
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        assert (await client.get("/api/projects")).status_code == 401


@pytest.mark.asyncio
async def test_production_spa_fallback_does_not_capture_api_404(production_app) -> None:
    app, _ = production_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert "CARLO production" in (await client.get("/")).text
        assert "CARLO production" in (await client.get("/tasks/CAR-1")).text
        assert "console.log" in (await client.get("/assets/app.js")).text
        missing = await client.get("/api/missing")
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/json")
