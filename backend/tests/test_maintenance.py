import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.maintenance import pi_process_lock, record_startup, update_pi_if_due
from carlo.model_providers import CredentialCipher
from carlo.models import Base, Event, ModelProvider


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
                ", available_models, model_providers "
                "RESTART IDENTITY CASCADE"
            )
        )
    result = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield result
    await engine.dispose()


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_pi_sessions_share_lock_while_update_waits(tmp_path: Path) -> None:
    import asyncio

    lock_path = tmp_path / "pi.lock"
    release = asyncio.Event()
    both_running = asyncio.Event()
    running = 0

    async def session() -> None:
        nonlocal running
        async with pi_process_lock(lock_path, exclusive=False):
            running += 1
            if running == 2:
                both_running.set()
            await release.wait()

    first = asyncio.create_task(session())
    second = asyncio.create_task(session())
    await asyncio.wait_for(both_running.wait(), timeout=1)

    update_entered = asyncio.Event()

    async def update() -> None:
        async with pi_process_lock(lock_path):
            update_entered.set()

    updater = asyncio.create_task(update())
    await asyncio.sleep(0.05)
    assert not update_entered.is_set()
    release.set()
    await asyncio.gather(first, second, updater)
    assert update_entered.is_set()


@pytest.mark.asyncio
async def test_startup_and_weekly_pi_update_are_persisted_once(factory, tmp_path: Path) -> None:
    calls = tmp_path / "npm-calls.json"
    pi = executable(tmp_path / "pi", 'echo "0.81.0"\n')
    npm = executable(
        tmp_path / "npm",
        f"python3 -c 'import json,sys; json.dump(sys.argv[1:], open(\"{calls}\", \"w\"))' \"$@\"\n",
    )

    await record_startup(
        factory, str(pi), hostname="macstudio01", event_type="worker.started"
    )
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    assert await update_pi_if_due(factory, str(npm), str(pi), tmp_path / "pi.lock", now) is True
    assert await update_pi_if_due(factory, str(npm), str(pi), tmp_path / "pi.lock", now) is False

    assert json.loads(calls.read_text()) == [
        "install",
        "-g",
        "--ignore-scripts",
        "@earendil-works/pi-coding-agent",
    ]
    async with factory() as session:
        events = (await session.scalars(select(Event).order_by(Event.sequence))).all()
    assert [event.type for event in events] == ["worker.started", "pi.update_completed"]
    assert events[0].payload == {"hostname": "macstudio01", "pi_version": "0.81.0"}
    assert events[1].payload["version_before"] == "0.81.0"
    assert events[1].payload["version_after"] == "0.81.0"


@pytest.mark.asyncio
async def test_failed_pi_update_is_persisted_without_raising(factory, tmp_path: Path) -> None:
    pi = executable(tmp_path / "pi", 'echo "0.81.0"\n')
    npm = executable(tmp_path / "npm", 'echo "registry unavailable" >&2\nexit 4\n')

    updated = await update_pi_if_due(
        factory,
        str(npm),
        str(pi),
        tmp_path / "pi.lock",
        datetime(2026, 8, 21, 12, tzinfo=UTC),
    )

    assert updated is True
    async with factory() as session:
        event = await session.scalar(select(Event))
    assert event is not None
    assert event.type == "pi.update_failed"
    assert event.payload["exit_code"] == 4
    assert event.payload["error"] == "registry unavailable"

    assert await update_pi_if_due(
        factory,
        str(npm),
        str(pi),
        tmp_path / "pi.lock",
        datetime(2026, 8, 21, 12, 59, tzinfo=UTC),
    ) is False
    assert await update_pi_if_due(
        factory,
        str(npm),
        str(pi),
        tmp_path / "pi.lock",
        datetime(2026, 8, 21, 13, tzinfo=UTC),
    ) is True


@pytest.mark.asyncio
async def test_unstartable_npm_is_persisted_as_failed_update(factory, tmp_path: Path) -> None:
    pi = executable(tmp_path / "pi", 'echo "0.81.0"\n')

    assert await update_pi_if_due(
        factory,
        str(tmp_path / "missing-npm"),
        str(pi),
        tmp_path / "pi.lock",
        datetime(2026, 8, 21, 12, tzinfo=UTC),
    ) is True

    async with factory() as session:
        event = await session.scalar(select(Event))
    assert event is not None
    assert event.type == "pi.update_failed"
    assert event.payload["exit_code"] is None
    assert "missing-npm" in event.payload["error"]


@pytest.mark.asyncio
async def test_maintenance_cycle_refreshes_due_model_providers(
    factory, tmp_path: Path
) -> None:
    from carlo import maintenance

    assert hasattr(maintenance, "run_maintenance_cycle"), (
        "model refresh is not connected to maintenance"
    )
    cipher = CredentialCipher(b"x" * 32)
    encrypted = cipher.encrypt("local-key")
    async with factory() as session:
        session.add(
            ModelProvider(
                name="Local",
                slug="local",
                kind="openai-compatible",
                base_url="http://local.test/v1",
                credential_ciphertext=encrypted.ciphertext,
                credential_nonce=encrypted.nonce,
            )
        )
        await session.commit()

    async def fetcher(_provider, api_key):
        assert api_key == "local-key"
        from carlo.model_providers import parse_openai_models

        return parse_openai_models({"data": [{"id": "qwen", "max_model_len": 65536}]})

    pi = executable(tmp_path / "pi", 'echo "0.81.0"\n')
    npm = executable(tmp_path / "npm", "exit 0\n")
    await maintenance.run_maintenance_cycle(
        factory,
        str(npm),
        str(pi),
        tmp_path / "pi.lock",
        cipher,
        now=datetime(2026, 8, 24, 12, tzinfo=UTC),
        model_fetcher=fetcher,
    )

    async with factory() as session:
        event_types = list(await session.scalars(select(Event.type).order_by(Event.sequence)))
    assert event_types == [
        "model_provider.availability_changed",
        "model_provider.refresh_completed",
        "pi.update_completed",
    ]
