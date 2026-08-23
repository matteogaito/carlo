from pathlib import Path
from uuid import uuid4
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.discovery_runtime import DiscoveryRuntime
from carlo.models import Base, Discovery, DiscoveryMessage, DiscoveryTurn, Project
from carlo.provider import ConversationEvent, ConversationState


class Session:
    session_id = "discovery-test"

    async def prompt(self, message):
        assert "Investigate imports" in message
        yield ConversationEvent("tool_execution_start", {"toolName": "read", "args": {"path": "src/ingest.py"}})
        yield ConversationEvent("tool_execution_end", {"toolName": "read", "result": "source"})
        yield ConversationEvent("message_update", {"delta": "Use the ingestion service."})
        yield ConversationEvent("tool_execution_end", {"toolName": "discovery_state", "result": {"details": {
            "summary": "Reuse ingestion.", "findings": ["src/ingest.py"], "decisions": [],
            "unresolved_questions": [], "inspected_resources": ["src/ingest.py"],
            "commands": [], "task_proposals": [],
        }}})
        yield ConversationEvent("agent_end", {})

    async def get_state(self):
        return ConversationState(self.session_id, "/tmp/session.jsonl", False, 10.0, {})

    async def close(self): pass
    async def abort(self): pass
    async def get_entries(self, since=None): return [], None
    async def compact(self, instructions): return ""


class Provider:
    def __init__(self): self.session = Session()
    async def open_conversation(self, *args, **kwargs): return self.session


class CancellingSession(Session):
    def __init__(self, factory, discovery_id):
        self.factory = factory
        self.discovery_id = discovery_id
        self.aborted = False

    async def prompt(self, message):
        async with self.factory() as session:
            turn = await session.scalar(select(DiscoveryTurn).where(DiscoveryTurn.discovery_id == self.discovery_id))
            turn.cancel_requested_at = datetime.now(UTC)
            await session.commit()
        yield ConversationEvent("message_update", {"delta": "Must not persist"})

    async def abort(self): self.aborted = True


class CancellingProvider:
    def __init__(self, session): self.session = session
    async def open_conversation(self, *args, **kwargs): return self.session


@pytest.mark.asyncio
async def test_discovery_turn_persists_reply_state_and_memory(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"R{uuid4().hex[:6].upper()}"
    async with factory() as session:
        discovery = Discovery(project=Project(name=key, key=key, repository_path=str(tmp_path)), title="Imports", provider_session_id=f"discovery-{key}", state={}, memory_path=str(tmp_path / "MEMORY.md"))
        message = DiscoveryMessage(discovery=discovery, sequence=1, role="user", content="Investigate imports")
        session.add(DiscoveryTurn(discovery=discovery, input_message=message))
        await session.commit()
        discovery_id = discovery.id

    runtime = DiscoveryRuntime(factory, Provider(), Path("/guard.mjs"))
    assert await runtime.run_next() == discovery_id
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "COMPLETED"
        assert discovery.messages[-1].content == "Use the ingestion service."
        assert discovery.messages[-2].role == "tool"
        assert discovery.messages[-2].metadata_json["tool"] == "read"
        assert discovery.state["summary"] == "Reuse ingestion."
        assert Path(discovery.memory_path).read_text().startswith("# Discovery memory")
    await runtime.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_stop_cannot_be_overwritten_by_late_agent_output(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"S{uuid4().hex[:6].upper()}"
    async with factory() as session:
        discovery = Discovery(project=Project(name=key, key=key, repository_path=str(tmp_path)), title="Stop", provider_session_id=f"discovery-{key}", state={}, memory_path=str(tmp_path / "MEMORY.md"))
        message = DiscoveryMessage(discovery=discovery, sequence=1, role="user", content="Long investigation")
        session.add(DiscoveryTurn(discovery=discovery, input_message=message))
        await session.commit()
        discovery_id = discovery.id
    rpc = CancellingSession(factory, discovery_id)
    runtime = DiscoveryRuntime(factory, CancellingProvider(rpc), Path("/guard.mjs"))
    await runtime.run_next()
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "INTERRUPTED"
        assert [message.role for message in discovery.messages] == ["user"]
        assert rpc.aborted is True
    await runtime.close()
    await engine.dispose()
