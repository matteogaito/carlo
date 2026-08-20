import asyncio
import json
import os
import shlex
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .actions import minimal_environment, parse_dotenv, redact
from .models import ActionRun, ActionStep, Event, Project


ACTION_LOCK = 1_128_352_848
ActionRunner = Callable[[int], Awaitable[None]]


class ActionOrchestrator:
    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        runner: ActionRunner,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.runner = runner

    async def run_next(self) -> int | None:
        async with self.engine.connect() as lock_connection:
            await lock_connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": ACTION_LOCK}
            )
            await lock_connection.commit()
            try:
                run_id = await self._claim_next()
                if run_id is None:
                    return None
                try:
                    await self.runner(run_id)
                except Exception as error:
                    await self._interrupt(run_id, error)
                    raise
                await self._ensure_terminal(run_id)
                return run_id
            finally:
                await lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": ACTION_LOCK}
                )
                await lock_connection.commit()

    async def _claim_next(self) -> int | None:
        async with self.session_factory() as session:
            run = await session.scalar(
                select(ActionRun)
                .where(ActionRun.status == "running")
                .order_by(ActionRun.requested_at, ActionRun.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if run is not None:
                session.add(
                    Event(
                        type="action.recovery_started",
                        payload={"run_id": run.id, "project_id": run.project_id, "action_key": run.action_key},
                    )
                )
                await session.commit()
                return run.id
            run = await session.scalar(
                select(ActionRun)
                .where(ActionRun.status == "queued")
                .order_by(ActionRun.requested_at, ActionRun.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if run is None:
                return None
            run.status = "running"
            run.internal_stage = "preparing"
            run.started_at = datetime.now(UTC)
            session.add(
                Event(
                    type="action.started",
                    payload={"run_id": run.id, "project_id": run.project_id, "action_key": run.action_key},
                )
            )
            await session.commit()
            return run.id

    async def _ensure_terminal(self, run_id: int) -> None:
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            if run is None or run.status != "running":
                return
            run.status = "succeeded"
            run.internal_stage = "complete"
            run.finished_at = datetime.now(UTC)
            session.add(
                Event(
                    type="action.succeeded",
                    payload={"run_id": run.id, "project_id": run.project_id, "action_key": run.action_key},
                )
            )
            await session.commit()

    async def _interrupt(self, run_id: int, error: Exception) -> None:
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            if run is None:
                return
            run.status = "interrupted"
            run.internal_stage = "complete"
            run.finished_at = datetime.now(UTC)
            run.error = str(error)[:500]
            session.add(
                Event(
                    type="action.interrupted",
                    payload={
                        "run_id": run.id,
                        "project_id": run.project_id,
                        "action_key": run.action_key,
                        "error": run.error,
                    },
                )
            )
            await session.commit()


class ActionExecutor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        worktree_root: Path,
        artifact_root: Path,
        cancel_grace_seconds: float = 10,
    ) -> None:
        self.session_factory = session_factory
        self.worktree_root = worktree_root.resolve()
        self.artifact_root = artifact_root.resolve()
        self.cancel_grace_seconds = cancel_grace_seconds
        self._last_output_event: dict[int, float] = {}
        self._reapers: set[asyncio.Task[int]] = set()

    async def run(self, run_id: int) -> None:
        run, project = await self._context(run_id)
        if run.runner_name != "local":
            raise RuntimeError(f"unsupported runner: {run.runner_name}")
        workspace = (self.worktree_root / "actions" / str(run.id)).resolve()
        if not workspace.is_relative_to(self.worktree_root / "actions"):
            raise RuntimeError("invalid action worktree path")
        if not workspace.exists():
            await self._set_workspace(run.id, workspace)
            await _exec_checked(
                "git",
                "-C",
                project.repository_path,
                "worktree",
                "add",
                "--detach",
                str(workspace),
                run.commit_sha,
            )
        if run.log_offset == 0:
            await self._append(run, f"[CARLO] action {run.action_key} at {run.commit_sha}\n", ())

        secrets = self._secrets(run)
        environment = minimal_environment(secrets)
        final_status = "succeeded"
        final_exit = 0
        for step in run.steps:
            if step.status == "succeeded":
                continue
            if final_status != "succeeded":
                await self._skip(step.id)
                continue
            exit_code = await self._run_step(
                run, step, workspace, environment, tuple(secrets.values())
            )
            if exit_code is None:
                final_status = "cancelled"
                final_exit = None
            elif exit_code:
                final_status = "failed"
                final_exit = exit_code
        await self._finish(run.id, final_status, final_exit)
        await self._cleanup(run.id, Path(project.repository_path), workspace)

    async def _context(self, run_id: int) -> tuple[ActionRun, Project]:
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            if run is None:
                raise RuntimeError(f"action run {run_id} disappeared")
            project = await session.get(Project, run.project_id)
            if project is None:
                raise RuntimeError(f"project {run.project_id} disappeared")
            return run, project

    def _secrets(self, run: ActionRun) -> dict[str, str]:
        if not run.secret_path:
            return {}
        path = Path(run.secret_path).resolve()
        expected = (self.artifact_root / "actions" / str(run.id)).resolve()
        if not path.is_relative_to(expected) or not path.is_file():
            raise RuntimeError("action environment snapshot is missing or invalid")
        return parse_dotenv(path.read_text())

    async def _set_workspace(self, run_id: int, workspace: Path) -> None:
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            if run is None:
                raise RuntimeError("action run disappeared")
            run.workspace_path = str(workspace)
            run.internal_stage = "preparing"
            await session.commit()

    async def _run_step(
        self,
        run: ActionRun,
        step: ActionStep,
        workspace: Path,
        environment: dict[str, str],
        secrets: tuple[str, ...],
    ) -> int | None:
        arguments = shlex.split(step.command)
        runtime = self._runtime(run.id)
        process: asyncio.subprocess.Process | None = None
        if step.status != "running":
            await self._start_step(run.id, step.id)
            await self._append(run, f"$ {step.command}\n", secrets)
            runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
            runtime.chmod(0o700)
            for name in ("raw.log", "state", "state.tmp", "collected-offset"):
                (runtime / name).unlink(missing_ok=True)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "carlo.action_child",
                str(runtime),
                str(workspace),
                run.secret_path or "",
                json.dumps(arguments),
                env=minimal_environment({}),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            await self._set_process_group(run.id, process.pid)
        try:
            exit_code, cancelled = await self._monitor_child(run, runtime, secrets)
        except asyncio.CancelledError:
            if process is not None:
                reaper = asyncio.create_task(process.wait())
                self._reapers.add(reaper)
                reaper.add_done_callback(self._reapers.discard)
            raise
        await self._set_process_group(run.id, None)
        if cancelled:
            await self._finish_step(step.id, "cancelled", None)
            await self._append(run, "[CARLO] step cancelled\n", secrets, force_event=True)
            return None
        assert exit_code is not None
        await self._finish_step(step.id, "succeeded" if exit_code == 0 else "failed", exit_code)
        await self._append(run, f"[CARLO] step exited {exit_code}\n", secrets, force_event=True)
        self._clear_runtime(runtime)
        return exit_code

    async def _start_step(self, run_id: int, step_id: int) -> None:
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            step = await session.get(ActionStep, step_id)
            if run is None or step is None:
                raise RuntimeError("action step disappeared")
            run.internal_stage = "executing"
            run.current_step = step.position
            step.status = "running"
            step.started_at = datetime.now(UTC)
            step.log_start = run.log_offset
            await session.commit()

    async def _finish_step(self, step_id: int, status: str, exit_code: int | None) -> None:
        async with self.session_factory() as session:
            step = await session.get(ActionStep, step_id)
            if step is None:
                raise RuntimeError("action step disappeared")
            run = await session.get(ActionRun, step.run_id)
            step.status = status
            step.exit_code = exit_code
            step.finished_at = datetime.now(UTC)
            step.log_end = run.log_offset if run else None
            await session.commit()

    async def _skip(self, step_id: int) -> None:
        async with self.session_factory() as session:
            step = await session.get(ActionStep, step_id)
            if step is not None:
                step.status = "skipped"
                step.finished_at = datetime.now(UTC)
                await session.commit()

    async def _set_process_group(self, run_id: int, process_group: int | None) -> None:
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            if run is not None:
                run.process_group = process_group
                await session.commit()

    async def _cancel_requested(self, run_id: int) -> bool:
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            return run is not None and run.cancel_requested_at is not None

    async def _terminate_group(self, process_group: int) -> None:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = asyncio.get_running_loop().time() + self.cancel_grace_seconds
        while self._process_alive(process_group) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        if self._process_alive(process_group):
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                return

    async def _monitor_child(
        self, run: ActionRun, runtime: Path, secrets: tuple[str, ...]
    ) -> tuple[int | None, bool]:
        offset_file = runtime / "collected-offset"
        offset = int(offset_file.read_text()) if offset_file.is_file() else 0
        while True:
            state_path = runtime / "state"
            terminal = state_path.is_file()
            offset = await self._drain_raw(run, runtime, offset, secrets, terminal)
            if terminal:
                return int(json.loads(state_path.read_text())["exit_code"]), False
            current = await self._current(run.id)
            if current.cancel_requested_at is not None:
                if current.process_group is not None:
                    await self._terminate_group(current.process_group)
                await self._drain_raw(run, runtime, offset, secrets, True)
                self._clear_runtime(runtime)
                return None, True
            if current.process_group is None or not self._process_alive(current.process_group):
                raise RuntimeError("action process ended without terminal state")
            await asyncio.sleep(0.1)

    async def _drain_raw(
        self,
        run: ActionRun,
        runtime: Path,
        offset: int,
        secrets: tuple[str, ...],
        terminal: bool,
    ) -> int:
        raw = runtime / "raw.log"
        if not raw.is_file():
            return offset
        with raw.open("rb") as source:
            source.seek(offset)
            data = source.read()
        if not terminal and data and not data.endswith(b"\n"):
            boundary = data.rfind(b"\n") + 1
            data = data[:boundary]
        if data:
            await self._append(run, data.decode(errors="replace"), secrets)
            offset += len(data)
            temporary = runtime / "collected-offset.tmp"
            temporary.write_text(str(offset))
            os.replace(temporary, runtime / "collected-offset")
        return offset

    async def _current(self, run_id: int) -> ActionRun:
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            if run is None:
                raise RuntimeError("action run disappeared")
            return run

    def _runtime(self, run_id: int) -> Path:
        return (self.artifact_root / "actions" / str(run_id) / "runtime").resolve()

    @staticmethod
    def _process_alive(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _clear_runtime(runtime: Path) -> None:
        for name in ("raw.log", "state", "state.tmp", "collected-offset", "collected-offset.tmp"):
            (runtime / name).unlink(missing_ok=True)
        try:
            runtime.rmdir()
        except OSError:
            pass

    async def _append(
        self,
        run: ActionRun,
        output: str,
        secrets: tuple[str, ...],
        *,
        force_event: bool = False,
    ) -> None:
        safe = redact(output, secrets)
        artifact = Path(run.artifact_path)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        data = safe.encode()
        with artifact.open("ab") as destination:
            destination.write(data)
        offset = artifact.stat().st_size
        now = time.monotonic()
        publish = force_event or now - self._last_output_event.get(run.id, 0) >= 0.25
        async with self.session_factory() as session:
            current = await session.get(ActionRun, run.id)
            if current is None:
                raise RuntimeError("action run disappeared")
            current.log_offset = offset
            current.recent_output = (current.recent_output + safe)[-16_384:]
            if publish:
                session.add(
                    Event(
                        type="action.output_available",
                        payload={"run_id": run.id, "project_id": run.project_id, "offset": offset},
                    )
                )
                self._last_output_event[run.id] = now
            await session.commit()

    async def _finish(self, run_id: int, status: str, exit_code: int | None) -> None:
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            if run is None:
                raise RuntimeError("action run disappeared")
            run.status = status
            run.internal_stage = "cleaning"
            run.exit_code = exit_code
            run.finished_at = datetime.now(UTC)
            run.process_group = None
            session.add(
                Event(
                    type=f"action.{status}",
                    payload={"run_id": run.id, "project_id": run.project_id, "action_key": run.action_key, "exit_code": exit_code},
                )
            )
            await session.commit()

    async def _cleanup(self, run_id: int, repository: Path, workspace: Path) -> None:
        error: str | None = None
        if workspace.exists():
            try:
                await _exec_checked(
                    "git", "-C", str(repository), "worktree", "remove", "--force", str(workspace)
                )
            except Exception as cleanup_error:
                error = str(cleanup_error)
        async with self.session_factory() as session:
            run = await session.get(ActionRun, run_id)
            if run is None:
                return
            if run.secret_path:
                try:
                    Path(run.secret_path).unlink(missing_ok=True)
                    run.secret_path = None
                except OSError as cleanup_error:
                    error = str(cleanup_error)
            run.workspace_path = None if error is None else str(workspace)
            run.cleanup_pending = error is not None
            run.internal_stage = "complete" if error is None else "cleanup_pending"
            if error:
                run.error = f"cleanup failed: {error}"[:500]
            await session.commit()


async def _exec_checked(*arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip())
    return stdout.decode(errors="replace").strip()
