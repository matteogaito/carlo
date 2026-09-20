import asyncio
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.discovery_runtime import DiscoveryRuntime, _normalized_state
from carlo.domain import TaskStage, TaskStatus
from carlo.model_providers import CredentialCipher
from carlo.models import (
    AgentProfile,
    Attempt,
    AvailableModel,
    Base,
    Discovery,
    DiscoveryMessage,
    DiscoveryTurn,
    Escalation,
    Event,
    ModelProvider,
    PlanRevision,
    Project,
    Task,
)
from carlo.provider import ConversationEvent, ConversationState
from carlo.planning import proposal_source
from tests.fakes import FakeProvider


def test_normalized_state_keeps_created_proposals_and_ignores_model_created_ids() -> None:
    previous = {"task_proposals": [{"id": "built", "title": "Built", "created_task_id": "TST-1"}]}
    state = _normalized_state({"task_proposals": [
        {"id": "new", "title": "New", "megaprompt": "Build", "depends_on": [], "created_task_id": "FAKE"},
        {"id": "built", "title": "Altered", "megaprompt": "Altered", "depends_on": []},
    ]}, previous)
    assert state["task_proposals"][0].get("created_task_id") is None
    assert state["task_proposals"][1]["created_task_id"] == "TST-1"
    assert state["task_proposals"][1]["title"] == "Built"
    assert _normalized_state({"task_proposals": []}, previous)["task_proposals"][0]["id"] == "built"


@pytest.fixture(autouse=True)
async def isolate_discovery_worker_queue() -> None:
    """Each worker test must claim only turns it created."""
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("TRUNCATE discovery_turns CASCADE"))
    await engine.dispose()


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


@pytest.mark.asyncio
async def test_discovery_candidates_use_canonical_planner_and_keep_unchanged_drafts(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"PLAN{uuid4().hex[:6].upper()}"
    state = {"summary": "Implement API and UI", "findings": ["api.py owns API"], "decisions": ["API first"],
             "task_proposals": [
                 {"id": "api", "title": "API", "megaprompt": "Build API", "depends_on": []},
                 {"id": "ui", "title": "UI", "megaprompt": "Build UI", "depends_on": ["api"]},
             ]}
    async with factory() as session:
        discovery = Discovery(project=Project(name=key, key=key, repository_path=str(tmp_path)),
                              title="Build", provider_session_id=f"discovery-{key}",
                              state=state, memory_path=str(tmp_path / "MEMORY.md"))
        session.add(discovery)
        await session.commit()
        discovery_id = discovery.id
    output = {"brief_markdown": "Brief", "plan_markdown": "Plan", "metadata": {
        "skills": [], "validation_commands": ["pytest -q"], "browser_validation": False,
        "build_required": False, "run_required": False, "deployment_expected": False,
        "risk_flags": [], "affected_areas": [], "implementation_tasks": [{
            "id": "one", "title": "Implement", "position": 0, "objective": "Deliver", "files": [
                {"path": "app.py", "mode": "edit", "reason": "Implementation"}],
            "interfaces": ["Preserve API"], "changes": {"app.py": "Implement"},
            "constraints": [], "verification": {"commands": ["pytest -q"], "success": "Pass"},
            "done_when": ["Tests pass"],
        }],
    }}
    provider = FakeProvider(json.dumps(output))
    runtime = DiscoveryRuntime(factory, provider, Path("/guard.mjs"))
    await runtime._plan_candidates(discovery_id)
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        proposals = deepcopy(discovery.state["task_proposals"])
        assert [item["plan_draft"]["metadata"]["implementation_tasks"][0]["title"] for item in proposals] == ["Implement", "Implement"]
        assert len(provider.calls) == 2
        assert all(call[0].name == "plan" for call in provider.calls)
        old_ui_source = proposals[1]["draft_source"]
        proposals[0]["megaprompt"] = "Build a revised API"
        discovery.state = {**discovery.state, "task_proposals": proposals}
        await session.commit()
    await runtime._plan_candidates(discovery_id)
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.state["task_proposals"][1]["draft_source"] == old_ui_source
        assert len(provider.calls) == 3
        assert discovery.state["task_proposals"][0]["draft_source"] == proposal_source(discovery.state["task_proposals"][0], discovery.state)

    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        changed = deepcopy(discovery.state)
        changed["task_proposals"][0]["megaprompt"] = "Build another API"
        discovery.state = changed
        await session.commit()

    class MutatingProvider(FakeProvider):
        async def run(self, *args, **kwargs):
            async with factory() as session:
                current = await session.get(Discovery, discovery_id)
                state = deepcopy(current.state)
                state["task_proposals"][0]["megaprompt"] = "Newest API request"
                current.state = state
                await session.commit()
            return await super().run(*args, **kwargs)

    stale_provider = MutatingProvider(json.dumps(output))
    await DiscoveryRuntime(factory, stale_provider, Path("/guard.mjs"))._plan_candidates(discovery_id)
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        candidate = discovery.state["task_proposals"][0]
        assert candidate["draft_source"] != proposal_source(candidate, discovery.state)
        assert len(stale_provider.calls) == 1
    await engine.dispose()


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


class ReworkSession(Session):
    async def prompt(self, message):
        assert "Failed task" in message
        assert "Missing empty case" in message
        yield ConversationEvent("message_update", {"delta": "Agreed, revising the package."})
        yield ConversationEvent("tool_execution_end", {"toolName": "task_fix_proposal", "result": {"details": {
            "action": "revise_task",
            "summary": "Load before migrating.",
            "brief_markdown": "Brief",
            "plan_markdown": "Plan",
            "package": {"id": "wp-1", "title": "Fix archive"},
            "packages": [],
        }}})
        yield ConversationEvent("agent_end", {})


@pytest.mark.asyncio
async def test_discovery_rework_mode_uses_task_context_and_fix_proposal_tool(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"RW{uuid4().hex[:6].upper()}"
    async with factory() as session:
        project = Project(name=key, key=key, repository_path=str(tmp_path))
        task = Task(
            id=f"{key}-1", project=project, sequence=1, title="Fix archive",
            goal="Persist destinations", status=TaskStatus.FAILED, stage=TaskStage.BLOCKED,
            approved_plan_revision=1,
        )
        plan = PlanRevision(
            task=task, revision=1, brief_markdown="Brief", plan_markdown="Plan",
            metadata_json={"implementation_tasks": [{"id": "wp-1", "title": "Fix archive"}]},
        )
        session.add_all([project, task, plan])
        await session.commit()
        session.add_all([
            Attempt(task_id=task.id, number=1, instruction="Go", outcome="budget_exceeded"),
            Escalation(task_id=task.id, reason="work_package", evidence={}, diagnosis="Missing empty case", strategy="revise", status="completed"),
        ])
        await session.commit()
        discovery = Discovery(
            project=project, task_id=task.id, title="Fix archive", provider_session_id=f"discovery-{key}",
            state={}, memory_path=str(tmp_path / "MEMORY.md"),
        )
        message = DiscoveryMessage(discovery=discovery, sequence=1, role="user", content="Show me the diagnosis")
        session.add(DiscoveryTurn(discovery=discovery, input_message=message))
        await session.commit()
        discovery_id = discovery.id

    provider = Provider()
    provider.session = ReworkSession()
    runtime = DiscoveryRuntime(
        factory, provider, Path("/discovery-guard.mjs"),
        rework_guard_extension=Path("/rework-guard.mjs"),
    )
    assert await runtime.run_next() == discovery_id
    assert provider.profiles[0].skills == ("carlo-rework",)
    assert provider.open_kwargs[0]["extensions"] == (Path("/rework-guard.mjs"),)
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "COMPLETED"
        proposal = discovery.state["fix_proposal"]
        assert proposal["action"] == "revise_task"
        assert proposal["package"] == {"id": "wp-1", "title": "Fix archive"}
        assert proposal["packages"] == []
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


class BusySession(Session):
    def __init__(self):
        self.attempts = 0

    async def prompt(self, message, *, images=()):
        self.attempts += 1
        raise RuntimeError(
            "Agent is already processing. Specify streamingBehavior "
            "('steer' or 'followUp') to queue the message."
        )
        yield  # pragma: no cover - makes this an async generator

    async def close(self):
        pass


class RetryProvider:
    def __init__(self):
        self.sessions = [BusySession(), RecoveredSession()]
        self.opened = 0

    async def open_conversation(self, *args, **kwargs):
        session = self.sessions[self.opened]
        self.opened += 1
        return session


@pytest.mark.asyncio
async def test_discovery_retries_a_transient_agent_busy_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "carlo.discovery_runtime.TRANSIENT_RETRY_DELAY_SECONDS", 0.01, raising=False
    )
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"BUSY{uuid4().hex[:6].upper()}"
    async with factory() as session:
        discovery = Discovery(
            project=Project(name=key, key=key, repository_path=str(tmp_path)),
            title="Busy",
            provider_session_id=f"discovery-{key}",
            state={},
            memory_path=str(tmp_path / "MEMORY.md"),
        )
        message = DiscoveryMessage(
            discovery=discovery, sequence=1, role="user", content="Investigate imports"
        )
        session.add(DiscoveryTurn(discovery=discovery, input_message=message))
        await session.commit()
        discovery_id = discovery.id

    provider = RetryProvider()
    runtime = DiscoveryRuntime(factory, provider, Path("/guard.mjs"))

    assert await runtime.run_next() == discovery_id
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "QUEUED"
        assert discovery.messages[-1].role == "system"
        assert "ritento automaticamente" in discovery.messages[-1].content

    assert await runtime.run_next() == discovery_id
    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "COMPLETED"
        assert discovery.messages[-1].content == "Recovered."

    assert provider.opened == 2
    assert provider.sessions[0].attempts == 1
    await runtime.close()
    await engine.dispose()


class StateFailureSession(Session):
    async def prompt(self, message, *, images=()):
        yield ConversationEvent(
            "tool_execution_end",
            {
                "toolName": "discovery_state",
                "result": {
                    "content": [{
                        "type": "text",
                        "text": 'Validation failed for tool "discovery_state": '
                        "task_proposals.0.metadata: must not have additional properties",
                    }],
                },
            },
        )
        yield ConversationEvent("message_update", {"delta": "Riprovo con uno stato più piccolo."})
        yield ConversationEvent(
            "tool_execution_end",
            {
                "toolName": "discovery_state",
                "result": {
                    "content": [{"type": "text", "text": "Discovery state recorded."}],
                    "details": {
                        "summary": "Done.", "findings": [], "decisions": [],
                        "unresolved_questions": [], "inspected_resources": [],
                        "commands": [], "task_proposals": [],
                    },
                },
            },
        )
        yield ConversationEvent("agent_end", {})


class StateFailureProvider:
    def __init__(self):
        self.session = StateFailureSession()

    async def open_conversation(self, *args, **kwargs):
        return self.session


@pytest.mark.asyncio
async def test_discovery_surfaces_state_validation_failures_in_chat(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"SF{uuid4().hex[:6].upper()}"
    async with factory() as session:
        discovery = Discovery(
            project=Project(name=key, key=key, repository_path=str(tmp_path)),
            title="StateFailure",
            provider_session_id=f"discovery-{key}",
            state={},
            memory_path=str(tmp_path / "MEMORY.md"),
        )
        message = DiscoveryMessage(
            discovery=discovery, sequence=1, role="user", content="Investigate imports"
        )
        session.add(DiscoveryTurn(discovery=discovery, input_message=message))
        await session.commit()
        discovery_id = discovery.id

    runtime = DiscoveryRuntime(factory, StateFailureProvider(), Path("/guard.mjs"))
    assert await runtime.run_next() == discovery_id

    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "COMPLETED"
        assert discovery.state["summary"] == "Done."
        system_messages = [m for m in discovery.messages if m.role == "system"]
        assert len(system_messages) == 1
        assert "Validation failed" in system_messages[0].content
    await runtime.close()
    await engine.dispose()


MISMATCHED_WORK_PACKAGE = {
    "id": "wp-1", "title": "Archivio", "position": 0,
    "objective": "Persist destinations.",
    "files": [
        {"path": "src/archive.py", "mode": "create", "reason": "New archive"},
        {"path": "src/store.py", "mode": "edit", "reason": "Wire it in"},
    ],
    "interfaces": ["archive persists records"], "changes": {"src/archive.py": "Create the archive", "docs": "stray key"},
    "constraints": [], "verification": {"commands": ["pytest -q"], "success": "Pass"},
    "done_when": ["Archive persists records"], "budget": {"max_tool_calls": 20},
}


class MismatchedProposalSession(Session):
    async def prompt(self, message, *, images=()):
        yield ConversationEvent("tool_execution_end", {"toolName": "discovery_state", "result": {"details": {
            "summary": "Ready.", "findings": [], "decisions": [], "unresolved_questions": [],
            "inspected_resources": [], "commands": [],
            "task_proposals": [{
                "id": "archive", "title": "Archivio destinazioni", "megaprompt": "Build the archive.",
                "depends_on": [],
            }],
        }}})
        yield ConversationEvent("agent_end", {})


class MismatchedProposalProvider(FakeProvider):
    def __init__(self):
        super().__init__(json.dumps({
            "brief_markdown": "# Brief", "plan_markdown": "# Plan",
            "metadata": {
                "skills": [], "implementation_tasks": [MISMATCHED_WORK_PACKAGE],
                "implementation_phases": ["Archivio"], "validation_commands": ["pytest -q"],
                "browser_validation": False, "build_required": False, "run_required": False,
                "deployment_expected": False, "risk_flags": [], "affected_areas": ["src"],
            },
        }))
        self.session = MismatchedProposalSession()

    async def open_conversation(self, *args, **kwargs):
        return self.session


@pytest.mark.asyncio
async def test_discovery_flags_a_proposal_whose_changes_do_not_match_its_files(tmp_path: Path) -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    key = f"MM{uuid4().hex[:6].upper()}"
    async with factory() as session:
        discovery = Discovery(
            project=Project(name=key, key=key, repository_path=str(tmp_path)),
            title="Mismatched", provider_session_id=f"discovery-{key}",
            state={}, memory_path=str(tmp_path / "MEMORY.md"),
        )
        message = DiscoveryMessage(discovery=discovery, sequence=1, role="user", content="Plan it")
        session.add(DiscoveryTurn(discovery=discovery, input_message=message))
        await session.commit()
        discovery_id = discovery.id

    runtime = DiscoveryRuntime(factory, MismatchedProposalProvider(), Path("/guard.mjs"))
    assert await runtime.run_next() == discovery_id

    async with factory() as session:
        discovery = await session.get(Discovery, discovery_id)
        assert discovery.turns[0].status == "COMPLETED"
        proposal = discovery.state["task_proposals"][0]
        assert "changes must describe every edit/create file" in proposal["validation_error"]
        system_messages = [m for m in discovery.messages if m.role == "system"]
        assert len(system_messages) == 1
        assert "Archivio destinazioni" in system_messages[0].content
        assert "changes must describe every edit/create file" in system_messages[0].content
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
        "carlo-runtime",
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
