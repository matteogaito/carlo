import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from .config import Settings
from .db import make_engine, make_session_factory
from .orchestrator import ImplementationPipeline, Orchestrator
from .provider import PiProvider
from .telegram import (
    TelegramNotifier,
    TelegramTransport,
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
    notifier_task: asyncio.Task[None] | None = None
    if telegram_enabled(settings.telegram_bot_token, settings.telegram_chat_id):
        notifier_task = asyncio.create_task(
            notification_loop(
                TelegramNotifier(
                    factory,
                    TelegramTransport(),
                    settings.telegram_bot_token,
                    settings.telegram_chat_id,
                    settings.telegram_level,
                )
            )
        )
    else:
        logger.info("Telegram notifications disabled: configure token and chat ID")
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
        if notifier_task is not None:
            notifier_task.cancel()
            with suppress(asyncio.CancelledError):
                await notifier_task
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
