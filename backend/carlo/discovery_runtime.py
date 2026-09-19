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
from .models import Attempt, Discovery, DiscoveryMessage, DiscoveryTurn, Escalation, Event, PlanRevision, Task
from .model_providers import (
    CredentialCipher,
    model_runtime_evidence,
    resolve_agent_profile,
)
from .provider import AgentProfile, CodingAgentProvider, ConversationEvent, ConversationSession
from .api import _queue_fix_proposal_fix_request, validate_fix_proposal, validate_task_proposal

EVENT_IDLE_TIMEOUT_SECONDS = 180
MAX_TRANSIENT_RETRIES = 2
TRANSIENT_RETRY_DELAY_SECONDS = 1.0
TRANSIENT_PROVIDER_ERROR_MARKERS = ("already processing",)


def _is_transient_provider_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in TRANSIENT_PROVIDER_ERROR_MARKERS)


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
        credential_cipher: CredentialCipher | None = None,
        rework_guard_extension: Path | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.guard_extension = guard_extension
        self.rework_guard_extension = rework_guard_extension or guard_extension
        self.max_sessions_per_project = max_sessions_per_project
        self.credential_cipher = credential_cipher
        self._sessions: dict[int, _LiveSession] = {}
        self._active: set[int] = set()
        self._claim_lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()
        self._retry_counts: dict[int, int] = {}

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
            input_message = turn.input_message
            message = input_message.content
            metadata = input_message.metadata_json or {}
            attachments = metadata.get("attachments") or []
            attachment_folder = Path(discovery.memory_path).resolve().parent / "attachments"
            is_rework = discovery.task_id is not None
            workflow_skill = "carlo-rework" if is_rework else "carlo-discovery"
            terminal_tool = "task_fix_proposal" if is_rework else "discovery_state"
            guard_extension = self.rework_guard_extension if is_rework else self.guard_extension
            memory = (
                await _task_failure_markdown(session, discovery.task_id)
                if is_rework
                else _memory_markdown(discovery)
            )
            pinned_model_id = discovery.state.get("model_runtime", {}).get(
                "available_model_id"
            )
            try:
                provider_profile = (
                    await resolve_agent_profile(
                        session,
                        profile,
                        self.credential_cipher,
                        task_model_id=pinned_model_id,
                        default_tools=(
                            "read",
                            "bash",
                            "grep",
                            "find",
                            "ls",
                            terminal_tool,
                        ),
                        workflow_skill=workflow_skill,
                    )
                    if profile
                    else AgentProfile(
                        "plan",
                        None,
                        None,
                        ("read", "bash", "grep", "find", "ls", terminal_tool),
                        (workflow_skill,),
                        packages=("superpowers", "ponytail"),
                    )
                )
            except Exception as error:
                await self._fail(turn_id, discovery_id, str(error))
                return
            runtime_evidence = model_runtime_evidence(provider_profile)
            if runtime_evidence and not pinned_model_id:
                discovery.state = {
                    **discovery.state,
                    "model_runtime": runtime_evidence,
                }
                await session.commit()
        live = await self._acquire(
            discovery_id,
            project.id,
            provider_profile,
            project.repository_path,
            discovery.provider_session_id,
            project.validation_commands,
            attachment_folder,
            guard_extension,
        )
        output: list[str] = []
        stream = ""
        state: dict[str, Any] | None = None
        pending_tools: dict[str, dict[str, Any]] = {}
        tools: list[dict[str, Any]] = []
        state_failures: list[str] = []
        try:
            readable_attachments = list(attachments)
            if metadata.get("image_path"):
                readable_attachments.append(
                    {
                        "name": Path(str(metadata["image_path"])).name,
                        "path": metadata["image_path"],
                    }
                )
            if readable_attachments:
                paths = "\n".join(
                    f"- `{item['path']}` ({item['name']})"
                    for item in readable_attachments
                )
                message = (
                    f"{message}\n\nAllegati. Leggili con il tool read prima di "
                    f"rispondere:\n{paths}"
                )
            prompt = f"{memory}\n\n# User message\n{message}"
            events = live.session.prompt(prompt)
            async for event in _events_with_idle_timeout(events):
                if await self._should_stop(turn_id, discovery_id):
                    await live.session.abort()
                    await self._interrupt(turn_id, discovery_id)
                    return
                delta = _event_delta(event)
                if delta:
                    output.append(delta)
                    stream += delta
                    if len(stream) >= 200:
                        await self._emit_delta(turn_id, discovery_id, stream)
                        stream = ""
                found = _event_state(event, terminal_tool)
                if found is not None:
                    state = found
                elif event.type == "tool_execution_end" and event.payload.get("toolName") == terminal_tool:
                    failure_text = _tool_result_text(event.payload.get("result"))
                    if failure_text:
                        state_failures.append(failure_text)
                tool_key = str(event.payload.get("toolCallId") or event.payload.get("toolName") or "tool")
                if event.type == "tool_execution_start" and event.payload.get("toolName") != terminal_tool:
                    pending_tools[tool_key] = {
                        "tool": str(event.payload.get("toolName") or "tool"),
                        "args": event.payload.get("args", {}),
                    }
                    await self._emit_tool_activity(
                        discovery_id,
                        "discovery.tool.started",
                        pending_tools[tool_key],
                    )
                elif event.type == "tool_execution_end" and tool_key in pending_tools:
                    tool = pending_tools.pop(tool_key)
                    await self._emit_tool_activity(
                        discovery_id,
                        "discovery.tool.completed",
                        {"tool": tool["tool"], "failed": bool(event.payload.get("isError"))},
                    )
                    tool["result"] = event.payload.get("result")
                    tools.append(tool)
            if await self._should_stop(turn_id, discovery_id):
                await self._interrupt(turn_id, discovery_id)
                return
            if stream:
                await self._emit_delta(turn_id, discovery_id, stream)
            provider_state = await live.session.get_state()
            await self._complete(turn_id, discovery_id, "".join(output), state, provider_state.session_file, tools, state_failures, is_rework)
        except TimeoutError:
            try:
                await asyncio.wait_for(live.session.abort(), 5)
            except Exception:
                pass
            await self._discard_session(discovery_id, live)
            await self._fail(
                turn_id,
                discovery_id,
                f"Pi produced no activity for {EVENT_IDLE_TIMEOUT_SECONDS:g} seconds",
            )
        except Exception as error:
            message_text = str(error)
            retries = self._retry_counts.get(turn_id, 0)
            if _is_transient_provider_error(message_text) and retries < MAX_TRANSIENT_RETRIES:
                self._retry_counts[turn_id] = retries + 1
                await self._discard_session(discovery_id, live)
                await self._requeue(turn_id, discovery_id, message_text)
            else:
                self._retry_counts.pop(turn_id, None)
                await self._fail(turn_id, discovery_id, message_text)
        finally:
            live.busy = False
            live.used_at = monotonic()

    async def _discard_session(
        self, discovery_id: int, live: _LiveSession
    ) -> None:
        async with self._pool_lock:
            if self._sessions.get(discovery_id) is live:
                del self._sessions[discovery_id]
        try:
            await live.session.close()
        except Exception:
            pass

    async def _acquire(self, discovery_id: int, project_id: int, profile: AgentProfile, cwd: str, session_id: str, validation_commands: list[str], attachment_folder: Path, guard_extension: Path) -> _LiveSession:
        while True:
            async with self._pool_lock:
                existing = self._sessions.get(discovery_id)
                if existing is not None:
                    existing.busy = True
                    return existing
                project_entries = [(key, value) for key, value in self._sessions.items() if value.project_id == project_id]
                if len(project_entries) < self.max_sessions_per_project:
                    session = await self.provider.open_conversation(
                        profile,
                        cwd,
                        session_id,
                        extensions=(guard_extension,),
                        environment={
                            "CARLO_DISCOVERY_COMMANDS": json.dumps(
                                validation_commands
                            )
                        },
                        sandbox_read_paths=(attachment_folder,),
                    )
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

    async def _complete(self, turn_id: int, discovery_id: int, content: str, state: dict[str, Any] | None, session_path: str | None, tools: list[dict[str, Any]], state_failures: list[str] | None = None, is_rework: bool = False) -> None:
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
            terminal_tool = "task_fix_proposal" if is_rework else "discovery_state"
            for failure in state_failures or ():
                session.add(
                    DiscoveryMessage(
                        discovery=discovery,
                        sequence=sequence,
                        role="system",
                        content=f"⚠️ Salvataggio dello stato non riuscito, Pi ha ritentato: {failure[:1500]}",
                        metadata_json={"tool": terminal_tool, "failed": True},
                    )
                )
                sequence += 1
            normalized: dict[str, Any] | None = None
            fix_proposal_error: str | None = None
            if state is not None:
                if is_rework:
                    normalized = await _normalized_fix_proposal(session, discovery.task_id, state)
                    fix_proposal = normalized.get("fix_proposal")
                    fix_proposal_error = (
                        fix_proposal.get("validation_error") if isinstance(fix_proposal, dict) else None
                    )
                    if fix_proposal_error:
                        session.add(
                            DiscoveryMessage(
                                discovery=discovery,
                                sequence=sequence,
                                role="system",
                                content=f"⚠️ La correzione proposta non è ancora applicabile: {fix_proposal_error[:1500]}",
                                metadata_json={"tool": terminal_tool, "failed": True},
                            )
                        )
                        sequence += 1
                else:
                    normalized = _normalized_state(state)
                    for proposal in normalized.get("task_proposals", []):
                        error = proposal.get("validation_error") if isinstance(proposal, dict) else None
                        if not error:
                            continue
                        session.add(
                            DiscoveryMessage(
                                discovery=discovery,
                                sequence=sequence,
                                role="system",
                                content=(
                                    f"⚠️ La proposta \"{proposal.get('title', 'senza titolo')}\" non è ancora creabile: "
                                    f"{error[:1500]}"
                                ),
                                metadata_json={"proposal_id": proposal.get("id"), "failed": True},
                            )
                        )
                        sequence += 1
            session.add(DiscoveryMessage(discovery=discovery, sequence=sequence, role="assistant", content=content or "No response was produced."))
            if normalized is not None:
                if "model_runtime" in discovery.state:
                    normalized["model_runtime"] = discovery.state["model_runtime"]
                discovery.state = normalized
            discovery.session_path = session_path
            discovery.last_active_at = datetime.now(UTC)
            turn.status = "COMPLETED"
            turn.finished_at = datetime.now(UTC)
            session.add(Event(discovery=discovery, type="discovery.turn.completed", payload={"turn_id": turn.id}))
            await session.commit()
            if not is_rework:
                _write_memory(Path(discovery.memory_path), _memory_markdown(discovery))
            elif fix_proposal_error:
                await _queue_fix_proposal_fix_request(session, discovery, fix_proposal_error)
        self._retry_counts.pop(turn_id, None)

    async def _fail(self, turn_id: int, discovery_id: int, error: str) -> None:
        async with self.session_factory() as session:
            turn = await session.get(DiscoveryTurn, turn_id)
            discovery = await session.get(Discovery, discovery_id)
            if turn is not None:
                turn.status = "FAILED"
                turn.error = error[:2000]
                turn.finished_at = datetime.now(UTC)
                session.add(Event(discovery_id=discovery_id, type="discovery.turn.failed", payload={"turn_id": turn_id, "error": turn.error}))
                if discovery is not None:
                    sequence = max((message.sequence for message in discovery.messages), default=0) + 1
                    session.add(
                        DiscoveryMessage(
                            discovery=discovery,
                            sequence=sequence,
                            role="system",
                            content=f"⚠️ Turno interrotto da un errore: {turn.error}",
                        )
                    )
                await session.commit()
        self._retry_counts.pop(turn_id, None)

    async def _requeue(self, turn_id: int, discovery_id: int, error: str) -> None:
        async with self.session_factory() as session:
            turn = await session.get(DiscoveryTurn, turn_id)
            discovery = await session.get(Discovery, discovery_id)
            if turn is not None:
                turn.status = "QUEUED"
                turn.started_at = None
                if discovery is not None:
                    sequence = max((message.sequence for message in discovery.messages), default=0) + 1
                    session.add(
                        DiscoveryMessage(
                            discovery=discovery,
                            sequence=sequence,
                            role="system",
                            content=f"⚠️ Pi era ancora occupato con il turno precedente, ritento automaticamente: {error[:500]}",
                        )
                    )
                session.add(
                    Event(
                        discovery_id=discovery_id,
                        type="discovery.turn.retrying",
                        payload={"turn_id": turn_id, "error": error[:2000]},
                    )
                )
                await session.commit()
        await asyncio.sleep(TRANSIENT_RETRY_DELAY_SECONDS)

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
        self._retry_counts.pop(turn_id, None)

    async def _emit_delta(self, turn_id: int, discovery_id: int, delta: str) -> None:
        async with self.session_factory() as session:
            session.add(
                Event(
                    discovery_id=discovery_id,
                    type="discovery.message.delta",
                    payload={"turn_id": turn_id, "delta": delta},
                )
            )
            await session.commit()

    async def _emit_tool_activity(
        self, discovery_id: int, event_type: str, tool: dict[str, Any]
    ) -> None:
        payload = {"tool": str(tool.get("tool") or "tool")[:80]}
        if event_type == "discovery.tool.started":
            args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
            payload["detail"] = next(
                (str(args[key]) for key in ("path", "query", "command") if key in args),
                "",
            )[:500]
        else:
            payload["failed"] = bool(tool.get("failed"))
        async with self.session_factory() as session:
            session.add(Event(discovery_id=discovery_id, type=event_type, payload=payload))
            await session.commit()


def _event_delta(event: ConversationEvent) -> str:
    delta = event.payload.get("delta")
    if isinstance(delta, str):
        return delta
    update = event.payload.get("assistantMessageEvent")
    return str(update.get("delta", "")) if isinstance(update, dict) and update.get("type") == "text_delta" else ""


async def _events_with_idle_timeout(events):
    iterator = aiter(events)
    while True:
        try:
            yield await asyncio.wait_for(
                anext(iterator), EVENT_IDLE_TIMEOUT_SECONDS
            )
        except StopAsyncIteration:
            return


def _event_state(event: ConversationEvent, terminal_tool: str = "discovery_state") -> dict[str, Any] | None:
    if event.type != "tool_execution_end" or event.payload.get("toolName") != terminal_tool:
        return None
    result = event.payload.get("result")
    details = result.get("details") if isinstance(result, dict) else None
    return details if isinstance(details, dict) else None


def _tool_result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def _normalized_state(state: dict[str, Any]) -> dict[str, Any]:
    result = {
        "summary": str(state.get("summary", "")),
        **{key: list(state.get(key, [])) for key in ("findings", "decisions", "unresolved_questions", "inspected_resources", "commands", "task_proposals")},
    }
    for index, proposal in enumerate(result["task_proposals"], start=1):
        if not isinstance(proposal, dict):
            continue
        if not proposal.get("id"):
            proposal["id"] = f"proposal-{index}"
        if proposal.get("created_task_id"):
            proposal.pop("validation_error", None)
            continue
        _, detail = validate_task_proposal(proposal)
        if detail:
            proposal["validation_error"] = detail
        else:
            proposal.pop("validation_error", None)
    return result


async def _normalized_fix_proposal(
    session: AsyncSession, task_id: str | None, state: dict[str, Any]
) -> dict[str, Any]:
    package = state.get("package")
    packages = state.get("packages")
    proposal = {
        "action": str(state.get("action", "")),
        "summary": str(state.get("summary", "")),
        "brief_markdown": str(state.get("brief_markdown", "")),
        "plan_markdown": str(state.get("plan_markdown", "")),
        "package": package if isinstance(package, dict) else None,
        "packages": list(packages) if isinstance(packages, list) else [],
    }
    task = await session.get(Task, task_id) if task_id else None
    if task is not None and proposal["action"] in {"revise_task", "revise_parent"}:
        _, detail = await validate_fix_proposal(session, task, proposal)
        if detail:
            proposal["validation_error"] = detail
    return {"fix_proposal": proposal}


async def _task_failure_markdown(session: AsyncSession, task_id: str | None) -> str:
    task = await session.get(Task, task_id) if task_id else None
    if task is None:
        return "# Failed task\n\nThe task could not be loaded.\n"
    plan = (
        await session.scalar(
            select(PlanRevision).where(
                PlanRevision.task_id == task_id,
                PlanRevision.revision == task.approved_plan_revision,
            )
        )
        if task.approved_plan_revision
        else None
    )
    attempts = (
        await session.scalars(
            select(Attempt).where(Attempt.task_id == task_id).order_by(Attempt.number)
        )
    ).all()
    escalations = (
        await session.scalars(
            select(Escalation).where(Escalation.task_id == task_id).order_by(Escalation.created_at)
        )
    ).all()
    related_task_ids = [task_id] + [
        child_id
        for child_id in (
            await session.scalars(
                select(Task.id).where(Task.parent_task_id == task_id, Task.superseded_at.is_(None))
            )
        ).all()
    ]
    rejection_event = await session.scalar(
        select(Event)
        .where(Event.task_id.in_(related_task_ids), Event.type == "escalation.approval_required")
        .order_by(Event.created_at.desc())
        .limit(1)
    )
    sections = [
        "# Failed task",
        "",
        f"**{task.id}** — {task.title}",
        task.goal,
    ]
    if plan is not None:
        sections += ["", "## Approved plan", "", plan.brief_markdown, "", plan.plan_markdown]
        packages = plan.metadata_json.get("implementation_tasks") or []
        if packages:
            sections += [
                "",
                "## Work package(s)",
                "```json",
                json.dumps(packages, indent=2, ensure_ascii=False),
                "```",
            ]
    if attempts:
        sections += ["", "## Attempts"]
        sections += [f"- #{attempt.number}: {attempt.outcome or 'unknown'}" for attempt in attempts]
    if escalations:
        sections += ["", "## Escalation diagnosis"]
        sections += [
            f"- **{escalation.reason}** ({escalation.strategy or 'n/a'}): {escalation.diagnosis or 'no diagnosis recorded'}"
            for escalation in escalations
        ]
    if rejection_event is not None and rejection_event.payload.get("rejected_reason"):
        sections += [
            "",
            "## Why the proposed fix was not applied automatically",
            rejection_event.payload["rejected_reason"],
        ]
    return "\n".join(sections).rstrip() + "\n"


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
