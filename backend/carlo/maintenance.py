import asyncio
import logging
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

import fcntl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Event
from .model_providers import (
    CredentialCipher,
    DiscoveredModel,
    fetch_openai_models,
    refresh_due_model_providers,
)

logger = logging.getLogger("carlo.maintenance")
UPDATE_INTERVAL = timedelta(days=7)
FAILURE_RETRY_INTERVAL = timedelta(hours=1)


@asynccontextmanager
async def pi_process_lock(path: Path, *, exclusive: bool = True) -> AsyncIterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        while True:
            try:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


async def record_startup(
    factory: async_sessionmaker[AsyncSession],
    pi_executable: str,
    *,
    hostname: str | None = None,
    event_type: str = "system.started",
) -> None:
    async with factory() as session:
        session.add(
            Event(
                type=event_type,
                payload={
                    "hostname": hostname or socket.gethostname(),
                    "pi_version": await _version(pi_executable),
                },
            )
        )
        await session.commit()


async def update_pi_if_due(
    factory: async_sessionmaker[AsyncSession],
    npm_executable: str,
    pi_executable: str,
    lock_path: Path,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    if not await _update_due(factory, now):
        return False

    async with pi_process_lock(lock_path):
        if not await _update_due(factory, now):
            return False
        before = await _version(pi_executable)
        try:
            process = await asyncio.create_subprocess_exec(
                npm_executable,
                "install",
                "-g",
                "--ignore-scripts",
                "@earendil-works/pi-coding-agent",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            exit_code = process.returncode
            error = (stderr or stdout).decode(errors="replace").strip()[-500:]
        except OSError as exc:
            exit_code = None
            error = str(exc)[-500:]
        if exit_code:
            event = Event(
                type="pi.update_failed",
                payload={
                    "exit_code": exit_code,
                    "error": error,
                },
                created_at=now,
            )
        elif exit_code is None:
            event = Event(
                type="pi.update_failed",
                payload={"exit_code": None, "error": error},
                created_at=now,
            )
        else:
            event = Event(
                type="pi.update_completed",
                payload={
                    "version_before": before,
                    "version_after": await _version(pi_executable),
                },
                created_at=now,
            )
        async with factory() as session:
            session.add(event)
            await session.commit()
        return True


async def maintenance_loop(
    factory: async_sessionmaker[AsyncSession],
    npm_executable: str,
    pi_executable: str,
    lock_path: Path,
    credential_cipher: CredentialCipher | None = None,
) -> None:
    while True:
        await run_maintenance_cycle(
            factory,
            npm_executable,
            pi_executable,
            lock_path,
            credential_cipher,
        )
        await asyncio.sleep(60)


async def run_maintenance_cycle(
    factory: async_sessionmaker[AsyncSession],
    npm_executable: str,
    pi_executable: str,
    lock_path: Path,
    credential_cipher: CredentialCipher | None,
    *,
    now: datetime | None = None,
    model_fetcher=fetch_openai_models,
) -> None:
    try:
        await refresh_due_model_providers(
            factory,
            credential_cipher,
            now=now,
            fetcher=model_fetcher,
        )
    except Exception:
        logger.exception("model provider refresh cycle failed")
    try:
        await update_pi_if_due(
            factory, npm_executable, pi_executable, lock_path, now=now
        )
    except Exception:
        logger.exception("weekly Pi update cycle failed")


async def _update_due(
    factory: async_sessionmaker[AsyncSession], now: datetime
) -> bool:
    async with factory() as session:
        last_success = await session.scalar(
            select(func.max(Event.created_at)).where(Event.type == "pi.update_completed")
        )
        last_failure = await session.scalar(
            select(func.max(Event.created_at)).where(Event.type == "pi.update_failed")
        )
    if last_success is not None and now - last_success < UPDATE_INTERVAL:
        return False
    return last_failure is None or now - last_failure >= FAILURE_RETRY_INTERVAL


async def _version(executable: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        if process.returncode == 0 and stdout.strip():
            return stdout.decode(errors="replace").strip()[:120]
    except (OSError, TimeoutError):
        pass
    return "unknown"
