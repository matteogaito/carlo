import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.auth import (
    InvalidCredentials,
    LoginThrottled,
    hash_password,
    login,
    resolve_session,
    revoke_session,
    verify_password,
)
from carlo.models import Base, LoginFailure, User, UserSession


@pytest.fixture
async def factory():
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


def test_password_hash_is_salted_and_verifiable() -> None:
    password = "correct horse battery staple"
    first = hash_password(password)
    second = hash_password(password)

    assert first != second
    assert verify_password(password, first)
    assert not verify_password("wrong password", first)
    assert not verify_password(password, "not-a-valid-hash")


def test_password_hash_rejects_short_passwords() -> None:
    with pytest.raises(ValueError, match="12 characters"):
        hash_password("too-short")


@pytest.mark.asyncio
async def test_bootstrap_admin_is_idempotent(factory) -> None:
    first = await bootstrap_admin(factory, "Admin", "first-password")
    second = await bootstrap_admin(factory, "CHANGE_ME", "CHANGE_ME")

    assert second.id == first.id
    assert second.username == "admin"
    assert verify_password("first-password", second.password_hash)
    assert not verify_password("different-password", second.password_hash)


@pytest.mark.asyncio
async def test_bootstrap_rejects_placeholders(factory) -> None:
    with pytest.raises(ValueError, match="placeholder"):
        await bootstrap_admin(factory, "CHANGE_ME", "CHANGE_ME")


@pytest.mark.asyncio
async def test_login_persists_only_digest_and_can_be_revoked(factory) -> None:
    user = await bootstrap_admin(factory, "admin", "admin-password")

    authenticated, token = await login(
        factory, " ADMIN ", "admin-password", "100.64.0.2", 24
    )

    assert authenticated.id == user.id
    assert len(token) >= 40
    async with factory() as session:
        stored = await session.scalar(select(UserSession))
        assert stored is not None
        assert stored.token_digest == hashlib.sha256(token.encode()).hexdigest()
        assert token not in stored.token_digest
    assert (await resolve_session(factory, token)).id == user.id

    await revoke_session(factory, token)
    assert await resolve_session(factory, token) is None


@pytest.mark.asyncio
async def test_login_rejects_disabled_or_expired_sessions(factory) -> None:
    user = await bootstrap_admin(factory, "admin", "admin-password")
    _, token = await login(factory, "admin", "admin-password", "100.64.0.2", 1)
    async with factory() as session:
        record = await session.scalar(select(UserSession))
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await resolve_session(factory, token) is None

    _, token = await login(factory, "admin", "admin-password", "100.64.0.2", 1)
    async with factory() as session:
        stored_user = await session.get(User, user.id)
        stored_user.active = False
        await session.commit()
    assert await resolve_session(factory, token) is None


@pytest.mark.asyncio
async def test_login_uses_generic_failure_and_throttles_fifth_attempt(factory) -> None:
    await bootstrap_admin(factory, "admin", "admin-password")

    for username in ("missing", "admin", "admin", "admin", "admin"):
        with pytest.raises(InvalidCredentials, match="invalid credentials"):
            await login(factory, username, "wrong-password", "100.64.0.2", 24)

    with pytest.raises(LoginThrottled, match="try again later"):
        await login(factory, "admin", "wrong-password", "100.64.0.2", 24)

    async with factory() as session:
        assert await session.scalar(select(func.count(LoginFailure.id))) == 5
