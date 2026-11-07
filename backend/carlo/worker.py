import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .action_runner import ActionExecutor, ActionOrchestrator
from .config import Settings
from .db import make_engine, make_session_factory
from .domain import TaskStatus
from .discovery_runtime import DiscoveryRuntime
from .maintenance import (
    ensure_managed_pi_packages,
    ensure_pi_resources,
    maintenance_loop,
    record_startup,
)
from .model_providers import CredentialCipher
from .logging_config import configure_logging
from .pi_runtime import PiRuntimeSnapshotBuilder
from .pi_packages import PiPackageManager
from .models import Task
from .orchestrator import ImplementationPipeline, Orchestrator, TaskStopRequested
from .provider import PiProvider
from .ssh import SshTransport
from .telegram import (
    TelegramCommandBot,
    TelegramNotifier,
    TelegramTransport,
    command_loop,
    notification_loop,
    telegram_enabled,
)

logger = logging.getLogger("carlo.worker")


async def _run_implementation_with_stop(
    factory: async_sessionmaker[AsyncSession],
    provider: PiProvider,
    runner: Callable[[str], Awaitable[str]],
    task_id: str,
    *,
    poll_seconds: float = 0.5,
) -> str:
    stopped = asyncio.Event()
    running = asyncio.create_task(runner(task_id))

    async def watch_stop() -> None:
        while not running.done():
            await asyncio.sleep(poll_seconds)
            try:
                async with factory() as session:
                    task = await session.get(Task, task_id)
                    if task is not None and task.status == TaskStatus.IN_PROGRESS:
                        continue
                    task_ids = (task_id, task.parent_task_id) if task and task.parent_task_id else (task_id,)
                stopped.set()
                try:
                    await provider.stop_tasks(task_ids)
                except Exception:
                    logger.exception("could not stop Pi sessions for %s", task_id)
            except Exception:
                logger.exception("could not check stop state for %s", task_id)

    watcher = asyncio.create_task(watch_stop())
    try:
        try:
            result = await running
        except BaseException:
            if not stopped.is_set():
                raise
            result = None
        if stopped.is_set():
            await watcher
            raise TaskStopRequested()
        return result
    finally:
        if not stopped.is_set() and not watcher.done():
            watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher


async def run() -> None:
    settings = Settings.from_env()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    cipher = (
        CredentialCipher.from_base64(settings.credential_encryption_key)
        if settings.credential_encryption_key
        and "CHANGE_ME" not in settings.credential_encryption_key
        else None
    )
    runtime_builder = PiRuntimeSnapshotBuilder(
        Path(settings.artifact_root) / "pi-runtime"
    )
    runtime_builder.cleanup_temporary_files()
    resource_root = Path(settings.artifact_root) / "pi-resources"
    lock_path = Path(settings.artifact_root) / "pi-runtime.lock"
    package_manager = PiPackageManager(
        Path(settings.pi_executable),
        Path(settings.artifact_root) / "pi-packages",
        lock_path,
    )
    provider = PiProvider(
        settings.pi_executable,
        Path(settings.artifact_root) / "pi-sessions",
        runtime_builder=runtime_builder,
        resource_root=resource_root,
        managed_skills={"frontend-design": "skills/frontend-design"},
        resource_manifest=resource_root / "revisions.json",
        debug=settings.log_level == "DEBUG",
        idle_timeout_seconds=settings.pi_idle_timeout_seconds,
    )
    pipeline = ImplementationPipeline(
        factory,
        provider,
        Path(settings.artifact_root),
        settings.max_attempts,
        cipher,
        settings.pi_request_diagnostics,
        settings.max_escalations,
    )
    orchestrator = Orchestrator(
        engine, factory,
        lambda task_id: _run_implementation_with_stop(factory, provider, pipeline.run, task_id),
    )
    action_executor = ActionExecutor(
        factory,
        Path(settings.artifact_root),
        settings.action_cancel_grace_seconds,
        SshTransport(
            Path(settings.ssh_known_hosts),
            connect_timeout=settings.ssh_connect_timeout,
        ),
    )
    action_orchestrator = ActionOrchestrator(engine, factory, action_executor.run)
    action_task = asyncio.create_task(_action_loop(action_orchestrator))
    discovery_runtime = DiscoveryRuntime(
        factory,
        provider,
        Path(__file__).resolve().parents[2] / "extensions" / "carlo-discovery-guard.mjs",
        credential_cipher=cipher,
        rework_guard_extension=Path(__file__).resolve().parents[2] / "extensions" / "carlo-rework-guard.mjs",
    )
    notifier_task: asyncio.Task[None] | None = None
    command_task: asyncio.Task[None] | None = None
    notifier: TelegramNotifier | None = None
    if telegram_enabled(settings.telegram_bot_token, settings.telegram_chat_id):
        telegram_transport = TelegramTransport()
        notifier = TelegramNotifier(
            factory,
            telegram_transport,
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            settings.telegram_level,
            app_origin=settings.app_origin,
        )
        await notifier.initialize_cursor()
        command_task = asyncio.create_task(
            command_loop(
                TelegramCommandBot(
                    factory,
                    telegram_transport,
                    settings.telegram_bot_token,
                    settings.telegram_chat_id,
                )
            )
        )
    await record_startup(factory, settings.pi_executable, event_type="worker.started")
    if notifier is not None:
        notifier_task = asyncio.create_task(notification_loop(notifier))
    else:
        logger.info("Telegram notifications disabled: configure token and chat ID")
    await ensure_pi_resources(
        factory,
        "git",
        resource_root,
        lock_path,
    )
    await ensure_managed_pi_packages(factory, package_manager)
    await discovery_runtime.recover()
    discovery_task = asyncio.create_task(_discovery_loop(discovery_runtime))
    maintenance_task = asyncio.create_task(
        maintenance_loop(
            factory,
            settings.npm_executable,
            settings.pi_executable,
            lock_path,
            cipher,
            resource_root,
            package_manager,
        )
    )
    try:
        while True:
            try:
                task_id = await orchestrator.run_next()
            except Exception:
                logger.exception("worker cycle interrupted; persisted task will be recovered")
                await asyncio.sleep(2)
                continue
            if task_id is None:
                await asyncio.sleep(2)
    finally:
        maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance_task
        action_task.cancel()
        with suppress(asyncio.CancelledError):
            await action_task
        discovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await discovery_task
        await discovery_runtime.close()
        if notifier_task is not None:
            notifier_task.cancel()
            with suppress(asyncio.CancelledError):
                await notifier_task
        if command_task is not None:
            command_task.cancel()
            with suppress(asyncio.CancelledError):
                await command_task
        await engine.dispose()


async def _action_loop(orchestrator: ActionOrchestrator) -> None:
    while True:
        try:
            run_id = await orchestrator.run_next()
        except Exception:
            logger.exception("action cycle interrupted; persisted run will be recovered")
            await asyncio.sleep(2)
            continue
        if run_id is None:
            await asyncio.sleep(2)


async def _discovery_loop(runtime: DiscoveryRuntime) -> None:
    async def consume() -> None:
        while True:
            try:
                discovery_id = await runtime.run_next()
            except Exception:
                logger.exception("discovery cycle interrupted; persisted turn will be recovered")
                await asyncio.sleep(2)
                continue
            if discovery_id is None:
                await asyncio.sleep(1)

    await asyncio.gather(*(consume() for _ in range(6)))


def main() -> None:
    configure_logging(Settings.from_env().log_level)
    asyncio.run(run())


if __name__ == "__main__":
    main()
