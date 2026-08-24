import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .maintenance import pi_process_lock

PI_JSON_EVENT_LIMIT = 4 * 1024 * 1024


class ProviderError(RuntimeError):
    pass


AgentEventHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AgentProfile:
    name: str
    model: str | None
    effort: str | None
    tools: tuple[str, ...]
    skills: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentResult:
    session_id: str
    output: str
    events: tuple[dict[str, Any], ...]
    exit_code: int
    used_skills: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConversationState:
    session_id: str
    session_file: str | None
    is_streaming: bool
    context_percent: float | None
    raw: dict[str, Any]


class ConversationSession(Protocol):
    session_id: str

    async def prompt(self, message: str) -> AsyncIterator[ConversationEvent]: ...

    async def abort(self) -> None: ...

    async def get_state(self) -> ConversationState: ...

    async def get_entries(
        self, since: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    async def compact(self, instructions: str) -> str: ...

    async def close(self) -> None: ...


class CodingAgentProvider(Protocol):
    async def run(
        self,
        profile: AgentProfile,
        instruction: str,
        cwd: str,
        session_id: str,
        on_event: AgentEventHandler | None = None,
    ) -> AgentResult: ...

    async def open_conversation(
        self,
        profile: AgentProfile,
        cwd: str,
        session_id: str,
        *,
        extensions: tuple[Path, ...] = (),
        environment: dict[str, str] | None = None,
    ) -> ConversationSession: ...

    async def stop(self, session_id: str) -> None: ...

    def status(self, session_id: str) -> str: ...


class PiProvider:
    def __init__(
        self, executable: str, session_dir: Path, skill_root: Path | None = None
    ) -> None:
        self.executable = executable
        self.session_dir = session_dir
        self.skill_root = skill_root or Path(__file__).resolve().parents[2] / "skills"
        self.lock_path = self.session_dir.parent / "pi-runtime.lock"
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def open_conversation(
        self,
        profile: AgentProfile,
        cwd: str,
        session_id: str,
        *,
        extensions: tuple[Path, ...] = (),
        environment: dict[str, str] | None = None,
    ) -> ConversationSession:
        if self.status(session_id) == "running":
            raise ProviderError(f"session is already running: {session_id}")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.executable,
            "--mode",
            "rpc",
            "--approve",
            "--session-id",
            session_id,
            "--session-dir",
            str(self.session_dir),
            *self._profile_arguments(profile),
        ]
        for extension in extensions:
            command.extend(("--extension", str(extension)))
        async with pi_process_lock(self.lock_path, exclusive=False):
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **(environment or {})},
            )
        self._processes[session_id] = process
        return PiRpcSession(
            session_id,
            process,
            lambda: self._processes.pop(session_id, None),
        )

    async def run(
        self,
        profile: AgentProfile,
        instruction: str,
        cwd: str,
        session_id: str,
        on_event: AgentEventHandler | None = None,
    ) -> AgentResult:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.executable,
            "--mode",
            "json",
            "--print",
            "--approve",
            "--session-id",
            session_id,
            "--session-dir",
            str(self.session_dir),
        ]
        command.extend(self._profile_arguments(profile))
        command.append(instruction)

        async with pi_process_lock(self.lock_path, exclusive=False):
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=PI_JSON_EVENT_LIMIT,
            )
            self._processes[session_id] = process
            stderr_task = asyncio.create_task(process.stderr.read())
            events: list[dict[str, Any]] = []
            try:
                while True:
                    try:
                        line = await process.stdout.readline()
                    except ValueError as error:
                        raise ProviderError(
                            "Pi emitted a JSON event larger than 4 MiB"
                        ) from error
                    if not line:
                        break
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as error:
                        raise ProviderError("Pi returned malformed JSON events") from error
                    if not isinstance(event, dict):
                        raise ProviderError("Pi returned a non-object JSON event")
                    events.append(event)
                    if on_event:
                        await on_event(event)
                await process.wait()
            except BaseException:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
                raise
            finally:
                self._processes.pop(session_id, None)
                stderr = await stderr_task

        if process.returncode:
            raise ProviderError(stderr.decode(errors="replace").strip())
        event_tuple = tuple(events)
        return AgentResult(
            session_id=session_id,
            output=_final_output(event_tuple),
            events=event_tuple,
            exit_code=process.returncode or 0,
            used_skills=_used_skills(instruction, event_tuple),
        )

    async def stop(self, session_id: str) -> None:
        process = self._processes.get(session_id)
        if process and process.returncode is None:
            process.terminate()
            await process.wait()

    def status(self, session_id: str) -> str:
        process = self._processes.get(session_id)
        return "running" if process and process.returncode is None else "idle"

    def _profile_arguments(self, profile: AgentProfile) -> list[str]:
        arguments: list[str] = []
        if profile.model:
            arguments.extend(("--model", profile.model))
        if profile.effort:
            arguments.extend(("--thinking", profile.effort))
        if profile.tools:
            arguments.extend(("--tools", ",".join(profile.tools)))
        for skill in profile.skills:
            path = Path(skill)
            bundled = self.skill_root / skill
            resolved = bundled if not path.is_absolute() and bundled.exists() else path
            arguments.extend(("--skill", str(resolved)))
        return arguments


def _used_skills(
    instruction: str, events: tuple[dict[str, Any], ...]
) -> tuple[str, ...]:
    skills: list[str] = []
    skill_command = instruction[7:] if instruction.startswith("/skill:") else ""
    if skill_command and not skill_command[0].isspace():
        skills.append(skill_command.split(maxsplit=1)[0])
    pending_reads: dict[str, str] = {}
    for event in events:
        event_type = event.get("type")
        tool_call_id = event.get("toolCallId")
        if event_type == "tool_execution_end" and isinstance(tool_call_id, str):
            skill = pending_reads.pop(tool_call_id, None)
            if skill and not event.get("isError"):
                skills.append(skill)
            continue
        if event_type != "tool_execution_start" or event.get("toolName") != "read":
            continue
        arguments = event.get("args")
        if not isinstance(arguments, dict) or not isinstance(tool_call_id, str):
            continue
        path = arguments.get("path") or arguments.get("file_path")
        if isinstance(path, str) and Path(path).name == "SKILL.md":
            pending_reads[tool_call_id] = Path(path).parent.name
    return tuple(dict.fromkeys(skills))


class PiRpcSession:
    def __init__(
        self,
        session_id: str,
        process: asyncio.subprocess.Process,
        on_close: Callable[[], None],
    ) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise ProviderError("Pi RPC pipes are unavailable")
        self.session_id = session_id
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.on_close = on_close
        self._request = 0
        self._responses: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[ConversationEvent | Exception] = asyncio.Queue()
        self._command_lock = asyncio.Lock()
        self._closed = False
        self._stderr = b""
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def prompt(self, message: str) -> AsyncIterator[ConversationEvent]:
        await self._command("prompt", message=message)
        while True:
            event = await self._events.get()
            if isinstance(event, Exception):
                raise event
            yield event
            if event.type == "agent_end":
                return

    async def abort(self) -> None:
        await self._command("abort")

    async def get_state(self) -> ConversationState:
        data = await self._command("get_state")
        context = data.get("contextUsage")
        percent = context.get("percent") if isinstance(context, dict) else None
        return ConversationState(
            session_id=str(data.get("sessionId") or self.session_id),
            session_file=data.get("sessionFile") if isinstance(data.get("sessionFile"), str) else None,
            is_streaming=bool(data.get("isStreaming")),
            context_percent=float(percent) if isinstance(percent, int | float) else None,
            raw=data,
        )

    async def get_entries(
        self, since: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        arguments = {"since": since} if since else {}
        data = await self._command("get_entries", **arguments)
        entries = data.get("entries")
        cursor = data.get("leafId")
        return (
            [entry for entry in entries if isinstance(entry, dict)]
            if isinstance(entries, list)
            else [],
            cursor if isinstance(cursor, str) else None,
        )

    async def compact(self, instructions: str) -> str:
        data = await self._command("compact", customInstructions=instructions)
        return str(data.get("summary") or "")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except TimeoutError:
            self.process.terminate()
            await self.process.wait()
        await asyncio.gather(self._reader_task, self._stderr_task, return_exceptions=True)
        self.on_close()

    async def _command(self, kind: str, **arguments: Any) -> dict[str, Any]:
        if self._closed or self.process.returncode is not None:
            raise ProviderError("Pi RPC session is closed")
        async with self._command_lock:
            self._request += 1
            request_id = f"carlo-{self._request}"
            future = asyncio.get_running_loop().create_future()
            self._responses[request_id] = future
            value = {"id": request_id, "type": kind, **arguments}
            self.stdin.write(json.dumps(value).encode() + b"\n")
            try:
                await self.stdin.drain()
                response = await future
            finally:
                self._responses.pop(request_id, None)
            if not response.get("success"):
                raise ProviderError(str(response.get("error") or f"Pi rejected {kind}"))
            data = response.get("data")
            return data if isinstance(data, dict) else {}

    async def _read_stdout(self) -> None:
        try:
            while line := await self.stdout.readline():
                try:
                    value = json.loads(line.removesuffix(b"\r\n").removesuffix(b"\n"))
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise ProviderError("Pi returned malformed RPC JSON") from error
                if not isinstance(value, dict):
                    raise ProviderError("Pi returned a non-object RPC record")
                if value.get("type") == "response" and isinstance(value.get("id"), str):
                    future = self._responses.get(value["id"])
                    if future is not None and not future.done():
                        future.set_result(value)
                    continue
                event_type = value.get("type")
                if isinstance(event_type, str):
                    await self._events.put(ConversationEvent(event_type, value))
        except Exception as error:
            failure = error if isinstance(error, ProviderError) else ProviderError(str(error))
            for future in self._responses.values():
                if not future.done():
                    future.set_exception(failure)
            await self._events.put(failure)
        finally:
            if not self._closed and self.process.returncode is not None:
                detail = self._stderr.decode(errors="replace").strip()
                failure = ProviderError(detail or "Pi RPC process exited")
                for future in self._responses.values():
                    if not future.done():
                        future.set_exception(failure)
                await self._events.put(failure)

    async def _read_stderr(self) -> None:
        self._stderr = await self.stderr.read()


def _final_output(events: tuple[dict[str, Any], ...]) -> str:
    for event in reversed(events):
        if isinstance(event.get("output"), str):
            return event["output"]
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content", [])
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
        if text:
            return text
    raise ProviderError("Pi returned no final output")
