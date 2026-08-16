import asyncio
import logging
from pathlib import Path

from .config import Settings
from .db import make_engine, make_session_factory
from .orchestrator import ImplementationPipeline, Orchestrator
from .provider import PiProvider

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
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
