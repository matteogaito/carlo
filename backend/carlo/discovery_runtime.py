import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import AgentProfile as ProfileRecord
from .models import Discovery, DiscoveryMessage, DiscoveryTurn, Event
from .provider import AgentProfile, CodingAgentProvider, ConversationEvent, ConversationSession


@dataclass
class _LiveSession:
    project_id: int
    session: ConversationSession
    busy: bool = False
    used_at: float = 0


class DiscoveryRuntime:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: CodingAgentProvider,
        guard_extension: Path,
        max_sessions_per_project: int = 3,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.guard_extension = guard_extension
        self.max_sessions_per_project = max_sessions_per_project
        self._sessions: dict[int, _LiveSession] = {}
        self._active: set[int] = set()
        self._claim_lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()

    async def recover(self) -> None:
        async with self.session_factory() as session:
            turns = (
                await session.scalars(
                    select(DiscoveryTurn).where(DiscoveryTurn.status == "RUNNING")
                )
            ).all()
            for turn in turns:
                turn.status = "QUEUED"
                turn.started_at = None
            await session.commit()

    async def run_next(self) -> int | None:
        claimed = await self._claim()
        if claimed is None:
            return None
        turn_id, discovery_id = claimed
        try:
            await self._run(turn_id, discovery_id)
        finally:
            self._active.discard(discovery_id)
        return discovery_id

    async def close(self) -> None:
        for entry in list(self._sessions.values()):
            await entry.session.close()
        self._sessions.clear()

    async def _claim(self) -> tuple[int, int] | None:
        async with self._claim_lock, self.session_factory() as session:
            candidates = (
                await session.scalars(
                    select(DiscoveryTurn)
                    .join(Discovery)
                    .where(DiscoveryTurn.status == "QUEUED", Discovery.status == "OPEN")
                    .order_by(DiscoveryTurn.created_at, DiscoveryTurn.id)
                    .with_for_update(skip_locked=True)
                    .limit(50)
                )
            ).all()
            turn = next((item for item in candidates if item.discovery_id not in self._active), None)
            if turn is None:
                return None
            self._active.add(turn.discovery_id)
            turn.status = "RUNNING"
            turn.started_at = datetime.now(UTC)
            session.add(Event(discovery_id=turn.discovery_id, type="discovery.turn.started", payload={"turn_id": turn.id}))
            await session.commit()
            return turn.id, turn.discovery_id

    async def _run(self, turn_id: int, discovery_id: int) -> None:
        async with self.session_factory() as session:
            turn = await session.get(DiscoveryTurn, turn_id)
            discovery = await session.get(Discovery, discovery_id)
            if turn is None or discovery is None:
                return
            profile = await session.get(ProfileRecord, discovery.profile_id) if discovery.profile_id else None
            project = discovery.project
            message = turn.input_message.content
            memory = _memory_markdown(discovery)
            provider_profile = AgentProfile(
                name="discovery",
                model=profile.model if profile else None,
                effort=profile.effort if profile else None,
                tools=tuple((profile.permissions.get("tools") if profile else None) or ["read", "bash", "grep", "find", "ls", "discovery_state"]),
                skills=tuple((profile.default_skills if profile else None) or ["carlo-discovery"]),
            )
        live = await self._acquire(discovery_id, project.id, provider_profile, project.repository_path, discovery.provider_session_id)
        output: list[str] = []
        state: dict[str, Any] | None = None
        pending_tools: dict[str, dict[str, Any]] = {}
        tools: list[dict[str, Any]] = []
        try:
            async for event in live.session.prompt(f"{memory}\n\n# User message\n{message}"):
                if await self._should_stop(turn_id, discovery_id):
                    await live.session.abort()
                    await self._interrupt(turn_id, discovery_id)
                    return
                delta = _event_delta(event)
                if delta:
                    output.append(delta)
                found = _event_state(event)
                if found is not None:
                    state = found
                tool_key = str(event.payload.get("toolCallId") or event.payload.get("toolName") or "tool")
                if event.type == "tool_execution_start" and event.payload.get("toolName") != "discovery_state":
                    pending_tools[tool_key] = {
                        "tool": str(event.payload.get("toolName") or "tool"),
                        "args": event.payload.get("args", {}),
                    }
                elif event.type == "tool_execution_end" and tool_key in pending_tools:
                    tool = pending_tools.pop(tool_key)
                    tool["result"] = event.payload.get("result")
                    tools.append(tool)
            if await self._should_stop(turn_id, discovery_id):
                await self._interrupt(turn_id, discovery_id)
                return
            provider_state = await live.session.get_state()
            await self._complete(turn_id, discovery_id, "".join(output), state, provider_state.session_file, tools)
        except Exception as error:
            await self._fail(turn_id, discovery_id, str(error))
        finally:
            live.busy = False
            live.used_at = monotonic()

    async def _acquire(self, discovery_id: int, project_id: int, profile: AgentProfile, cwd: str, session_id: str) -> _LiveSession:
        while True:
            async with self._pool_lock:
                existing = self._sessions.get(discovery_id)
                if existing is not None:
                    existing.busy = True
                    return existing
                project_entries = [(key, value) for key, value in self._sessions.items() if value.project_id == project_id]
                if len(project_entries) < self.max_sessions_per_project:
                    session = await self.provider.open_conversation(profile, cwd, session_id, extensions=(self.guard_extension,))
                    entry = _LiveSession(project_id, session, True, monotonic())
                    self._sessions[discovery_id] = entry
                    return entry
                idle = [(key, value) for key, value in project_entries if not value.busy]
                if idle:
                    key, entry = min(idle, key=lambda item: item[1].used_at)
                    await entry.session.close()
                    del self._sessions[key]
                    continue
            await asyncio.sleep(0.1)

    async def _complete(self, turn_id: int, discovery_id: int, content: str, state: dict[str, Any] | None, session_path: str | None, tools: list[dict[str, Any]]) -> None:
        async with self.session_factory() as session:
            turn = await session.get(DiscoveryTurn, turn_id)
            discovery = await session.get(Discovery, discovery_id)
            if turn is None or discovery is None:
                return
            sequence = max((message.sequence for message in discovery.messages), default=0) + 1
            for tool in tools:
                session.add(
                    DiscoveryMessage(
                        discovery=discovery,
                        sequence=sequence,
                        role="tool",
                        content=json.dumps(tool.get("result"), ensure_ascii=False, default=str),
                        metadata_json={"tool": tool["tool"], "args": tool["args"]},
                    )
                )
                sequence += 1
            session.add(DiscoveryMessage(discovery=discovery, sequence=sequence, role="assistant", content=content or "No response was produced."))
            if state is not None:
                discovery.state = _normalized_state(state)
            discovery.session_path = session_path
            discovery.last_active_at = datetime.now(UTC)
            turn.status = "COMPLETED"
            turn.finished_at = datetime.now(UTC)
            session.add(Event(discovery=discovery, type="discovery.turn.completed", payload={"turn_id": turn.id}))
            await session.commit()
            _write_memory(Path(discovery.memory_path), _memory_markdown(discovery))

    async def _fail(self, turn_id: int, discovery_id: int, error: str) -> None:
        async with self.session_factory() as session:
            turn = await session.get(DiscoveryTurn, turn_id)
            if turn is not None:
                turn.status = "FAILED"
                turn.error = error[:2000]
                turn.finished_at = datetime.now(UTC)
                session.add(Event(discovery_id=discovery_id, type="discovery.turn.failed", payload={"turn_id": turn_id, "error": turn.error}))
                await session.commit()

    async def _should_stop(self, turn_id: int, discovery_id: int) -> bool:
        async with self.session_factory() as session:
            turn = await session.get(DiscoveryTurn, turn_id)
            discovery = await session.get(Discovery, discovery_id)
            return (
                turn is None
                or discovery is None
                or discovery.status != "OPEN"
                or turn.status == "INTERRUPTED"
                or turn.cancel_requested_at is not None
            )

    async def _interrupt(self, turn_id: int, discovery_id: int) -> None:
        async with self.session_factory() as session:
            turn = await session.get(DiscoveryTurn, turn_id)
            if turn is not None:
                turn.status = "INTERRUPTED"
                turn.finished_at = datetime.now(UTC)
                session.add(
                    Event(
                        discovery_id=discovery_id,
                        type="discovery.turn.interrupted",
                        payload={"turn_id": turn_id},
                    )
                )
                await session.commit()


def _event_delta(event: ConversationEvent) -> str:
    delta = event.payload.get("delta")
    if isinstance(delta, str):
        return delta
    update = event.payload.get("assistantMessageEvent")
    return str(update.get("delta", "")) if isinstance(update, dict) and update.get("type") == "text_delta" else ""


def _event_state(event: ConversationEvent) -> dict[str, Any] | None:
    if event.type != "tool_execution_end" or event.payload.get("toolName") != "discovery_state":
        return None
    result = event.payload.get("result")
    details = result.get("details") if isinstance(result, dict) else None
    return details if isinstance(details, dict) else None


def _normalized_state(state: dict[str, Any]) -> dict[str, Any]:
    result = {
        "summary": str(state.get("summary", "")),
        **{key: list(state.get(key, [])) for key in ("findings", "decisions", "unresolved_questions", "inspected_resources", "commands", "task_proposals")},
    }
    for index, proposal in enumerate(result["task_proposals"], start=1):
        if isinstance(proposal, dict) and not proposal.get("id"):
            proposal["id"] = f"proposal-{index}"
    return result


def _memory_markdown(discovery: Discovery) -> str:
    state = _normalized_state(discovery.state or {})
    sections = ["# Discovery memory", "", state["summary"] or "No summary yet."]
    for title, key in (("Findings", "findings"), ("Decisions", "decisions"), ("Unresolved questions", "unresolved_questions"), ("Inspected resources", "inspected_resources"), ("Commands", "commands")):
        sections.extend(["", f"## {title}", *[f"- {item}" for item in state[key]]])
    return "\n".join(sections).rstrip() + "\n"


def _write_memory(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
