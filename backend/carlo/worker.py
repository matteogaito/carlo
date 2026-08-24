import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from .action_runner import ActionExecutor, ActionOrchestrator
from .config import Settings
from .db import make_engine, make_session_factory
from .discovery_runtime import DiscoveryRuntime
from .maintenance import (
    ensure_pi_resources,
    maintenance_loop,
    record_startup,
)
from .model_providers import CredentialCipher
from .pi_runtime import PiRuntimeSnapshotBuilder
from .orchestrator import ImplementationPipeline, Orchestrator
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
    provider = PiProvider(
        settings.pi_executable,
        Path(settings.artifact_root) / "pi-sessions",
        runtime_builder=runtime_builder,
        resource_root=resource_root,
        managed_packages=("superpowers", "ponytail"),
        managed_skills={"frontend-design": "skills/frontend-design"},
        resource_manifest=resource_root / "revisions.json",
    )
    pipeline = ImplementationPipeline(
        factory,
        provider,
        Path(settings.worktree_root),
        Path(settings.artifact_root),
        settings.max_attempts,
        cipher,
    )
    orchestrator = Orchestrator(engine, factory, pipeline.run)
    action_executor = ActionExecutor(
        factory,
        Path(settings.worktree_root),
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
        Path(settings.artifact_root) / "pi-runtime.lock",
    )
    await discovery_runtime.recover()
    discovery_task = asyncio.create_task(_discovery_loop(discovery_runtime))
    maintenance_task = asyncio.create_task(
        maintenance_loop(
            factory,
            settings.npm_executable,
            settings.pi_executable,
            Path(settings.artifact_root) / "pi-runtime.lock",
            cipher,
            resource_root,
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
    asyncio.run(run())


if __name__ == "__main__":
    main()
