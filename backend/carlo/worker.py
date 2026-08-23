import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from .action_runner import ActionExecutor, ActionOrchestrator
from .config import Settings
from .db import make_engine, make_session_factory
from .maintenance import maintenance_loop, record_startup
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
    provider = PiProvider(
        settings.pi_executable, Path(settings.artifact_root) / "pi-sessions"
    )
    pipeline = ImplementationPipeline(
        factory,
        provider,
        Path(settings.worktree_root),
        Path(settings.artifact_root),
        settings.max_attempts,
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
    maintenance_task = asyncio.create_task(
        maintenance_loop(
            factory,
            settings.npm_executable,
            settings.pi_executable,
            Path(settings.artifact_root) / "pi-runtime.lock",
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


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
