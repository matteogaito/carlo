import asyncio
from pathlib import Path
from uuid import uuid4
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.discovery_runtime import DiscoveryRuntime
from carlo.model_providers import CredentialCipher
from carlo.models import (
    AgentProfile,
    AvailableModel,
    Base,
    Discovery,
    DiscoveryMessage,
    DiscoveryTurn,
    Event,
    ModelProvider,
    Project,
)
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
    def __init__(self):
        self.session = Session()
        self.profiles = []
        self.open_kwargs = []

    async def open_conversation(self, profile, *args, **kwargs):
        self.profiles.append(profile)
        self.open_kwargs.append(kwargs)
        return self.session


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


class StallingSession(Session):
    def __init__(self):
        self.closed = False

    async def prompt(self, message, *, images=()):
        await asyncio.Event().wait()
        yield ConversationEvent("agent_end", {})

    async def close(self):
        self.closed = True


class RecoveredSession(Session):
    async def prompt(self, message, *, images=()):
        yield ConversationEvent("message_update", {"delta": "Recovered."})
        yield ConversationEvent("agent_end", {})


class StallingProvider:
    def __init__(self):
        self.sessions = [StallingSession(), RecoveredSession()]
        self.opened = 0

    async def open_conversation(self, *args, **kwargs):
        session = self.sessions[self.opened]
        self.opened += 1
        return session


@pytest.mark.asyncio
async def test_discovery_turn_persists_reply_state_and_memory(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
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

    provider = Provider()
    runtime = DiscoveryRuntime(factory, provider, Path("/guard.mjs"))
    assert await runtime.run_next() == discovery_id
    assert provider.open_kwargs[0]["sandbox_read_paths"] == (
        tmp_path / "attachments",
    )
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "COMPLETED"
        assert discovery.messages[-1].content == "Use the ingestion service."
        assert discovery.messages[-2].role == "tool"
        assert discovery.messages[-2].metadata_json["tool"] == "read"
        assert discovery.state["summary"] == "Reuse ingestion."
        assert Path(discovery.memory_path).read_text().startswith("# Discovery memory")
        events = (await session.scalars(select(Event).where(Event.discovery_id == discovery_id))).all()
        assert "Use the ingestion service." == "".join(event.payload["delta"] for event in events if event.type == "discovery.message.delta")
        assert any(
            event.type == "discovery.tool.started"
            and event.payload == {"tool": "read", "detail": "src/ingest.py"}
            for event in events
        )
        assert any(
            event.type == "discovery.tool.completed"
            and event.payload == {"tool": "read", "failed": False}
            for event in events
        )
        assert "source" not in str([event.payload for event in events])
    await runtime.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_fails_a_turn_that_stops_emitting_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "carlo.discovery_runtime.EVENT_IDLE_TIMEOUT_SECONDS", 0.01, raising=False
    )
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"STALL{uuid4().hex[:6].upper()}"
    async with factory() as session:
        discovery = Discovery(
            project=Project(name=key, key=key, repository_path=str(tmp_path)),
            title="Stalled",
            provider_session_id=f"discovery-{key}",
            state={},
            memory_path=str(tmp_path / "MEMORY.md"),
        )
        message = DiscoveryMessage(
            discovery=discovery, sequence=1, role="user", content="Inspect"
        )
        retry = DiscoveryMessage(
            discovery=discovery, sequence=2, role="user", content="Retry"
        )
        session.add_all(
            [
                DiscoveryTurn(discovery=discovery, input_message=message),
                DiscoveryTurn(discovery=discovery, input_message=retry),
            ]
        )
        await session.commit()
        discovery_id = discovery.id

    provider = StallingProvider()
    runtime = DiscoveryRuntime(factory, provider, Path("/guard.mjs"))
    await asyncio.wait_for(runtime.run_next(), 0.2)
    await asyncio.wait_for(runtime.run_next(), 0.2)

    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "FAILED"
        assert "Pi produced no activity" in discovery.turns[0].error
        assert discovery.turns[1].status == "COMPLETED"
    assert provider.opened == 2
    assert provider.sessions[0].closed is True
    await runtime.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_stop_cannot_be_overwritten_by_late_agent_output(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
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


@pytest.mark.asyncio
async def test_discovery_restart_keeps_its_original_managed_model(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"M{uuid4().hex[:6].upper()}"
    cipher = CredentialCipher(b"m" * 32)
    encrypted = cipher.encrypt("secret")
    async with factory() as session:
        provider = ModelProvider(
            name=key,
            slug=key.lower(),
            kind="openai-compatible",
            base_url="http://model.test/v1",
            credential_ciphertext=encrypted.ciphertext,
            credential_nonce=encrypted.nonce,
        )
        first = AvailableModel(
            model_provider=provider,
            external_id="first",
            status="AVAILABLE",
            discovered_context_window=65_536,
            discovered_max_tokens=16_384,
        )
        second = AvailableModel(
            model_provider=provider,
            external_id="second",
            status="AVAILABLE",
            discovered_context_window=65_536,
            discovered_max_tokens=16_384,
        )
        profile = AgentProfile(
            name=f"plan-{key}",
            provider="pi",
            default_skills=["carlo-planning", "frontend-design"],
        )
        project = Project(name=key, key=key, repository_path=str(tmp_path))
        discovery = Discovery(
            project=project,
            profile_id=None,
            title="Imports",
            provider_session_id=f"discovery-{key}",
            state={},
            memory_path=str(tmp_path / "MEMORY.md"),
        )
        message = DiscoveryMessage(
            discovery=discovery,
            sequence=1,
            role="user",
            content="Investigate imports",
        )
        session.add_all([provider, first, second, profile, discovery])
        await session.flush()
        profile.available_model_id = first.id
        discovery.profile_id = profile.id
        session.add(DiscoveryTurn(discovery=discovery, input_message=message))
        await session.commit()
        discovery_id, profile_id, second_id = discovery.id, profile.id, second.id

    first_process = Provider()
    runtime = DiscoveryRuntime(
        factory, first_process, Path("/guard.mjs"), credential_cipher=cipher
    )
    await runtime.run_next()
    await runtime.close()
    async with factory() as session:
        profile = await session.get(AgentProfile, profile_id)
        discovery = await session.get(Discovery, discovery_id)
        profile.available_model_id = second_id
        sequence = max(message.sequence for message in discovery.messages) + 1
        message = DiscoveryMessage(
            discovery=discovery,
            sequence=sequence,
            role="user",
            content="Investigate imports",
        )
        session.add(DiscoveryTurn(discovery=discovery, input_message=message))
        await session.commit()

    second_process = Provider()
    restarted = DiscoveryRuntime(
        factory, second_process, Path("/guard.mjs"), credential_cipher=cipher
    )
    await restarted.run_next()

    assert first_process.profiles[0].resolved_model.external_id == "first"
    assert second_process.profiles[0].resolved_model.external_id == "first"
    assert first_process.profiles[0].skills == (
        "carlo-discovery",
        "frontend-design",
    )
    await restarted.close()
    await engine.dispose()


class ImageSession(Session):
    async def prompt(self, message, *, images=()):
        assert images == ()
        assert "Allegati." in message
        assert "shot.png" in message
        assert "sample.json" in message
        assert "tool read" in message
        yield ConversationEvent("message_update", {"delta": "Screenshot analizzato."})
        yield ConversationEvent("agent_end", {})


class ImageProvider:
    def __init__(self):
        self.session = ImageSession()

    async def open_conversation(self, *args, **kwargs):
        return self.session


@pytest.mark.asyncio
async def test_discovery_message_passes_screenshot_path_to_pi(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"IMG{uuid4().hex[:6].upper()}"
    attachment_folder = tmp_path / "attachments"
    attachment_folder.mkdir()
    image_path = attachment_folder / "shot.png"
    image_path.write_bytes(b"png")
    async with factory() as session:
        discovery = Discovery(
            project=Project(name=key, key=key, repository_path=str(tmp_path)),
            title="Screenshot",
            provider_session_id=f"discovery-{key}",
            state={},
            memory_path=str(tmp_path / "MEMORY.md"),
        )
        message = DiscoveryMessage(
            discovery=discovery,
            sequence=1,
            role="user",
            content="Guarda questo",
            metadata_json={
                "attachments": [
                    {
                        "name": "shot.png",
                        "path": str(image_path),
                        "kind": "image",
                        "content_type": "image/png",
                    },
                    {
                        "name": "sample.json",
                        "path": str(tmp_path / "attachments" / "sample.json"),
                        "kind": "file",
                    }
                ],
            },
        )
        session.add(DiscoveryTurn(discovery=discovery, input_message=message))
        await session.commit()
        discovery_id = discovery.id

    runtime = DiscoveryRuntime(factory, ImageProvider(), Path("/guard.mjs"))
    assert await runtime.run_next() == discovery_id
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "COMPLETED"
        assert discovery.messages[-1].content == "Screenshot analizzato."
    await runtime.close()
    await engine.dispose()
