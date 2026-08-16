import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ProviderError(RuntimeError):
    pass


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


class CodingAgentProvider(Protocol):
    async def run(
        self,
        profile: AgentProfile,
        instruction: str,
        cwd: str,
        session_id: str,
    ) -> AgentResult: ...

    async def stop(self, session_id: str) -> None: ...

    def status(self, session_id: str) -> str: ...


class PiProvider:
    def __init__(self, executable: str, session_dir: Path) -> None:
        self.executable = executable
        self.session_dir = session_dir
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def run(
        self,
        profile: AgentProfile,
        instruction: str,
        cwd: str,
        session_id: str,
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
        if profile.model:
            command.extend(("--model", profile.model))
        if profile.effort:
            command.extend(("--thinking", profile.effort))
        if profile.tools:
            command.extend(("--tools", ",".join(profile.tools)))
        for skill in profile.skills:
            command.extend(("--skill", skill))
        command.append(instruction)

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes[session_id] = process
        try:
            stdout, stderr = await process.communicate()
        finally:
            self._processes.pop(session_id, None)

        if process.returncode:
            raise ProviderError(stderr.decode(errors="replace").strip())
        try:
            events = tuple(
                json.loads(line) for line in stdout.decode().splitlines() if line.strip()
            )
        except json.JSONDecodeError as error:
            raise ProviderError("Pi returned malformed JSON events") from error
        return AgentResult(
            session_id=session_id,
            output=_final_output(events),
            events=events,
            exit_code=process.returncode or 0,
        )

    async def stop(self, session_id: str) -> None:
        process = self._processes.get(session_id)
        if process and process.returncode is None:
            process.terminate()
            await process.wait()

    def status(self, session_id: str) -> str:
        process = self._processes.get(session_id)
        return "running" if process and process.returncode is None else "idle"


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
