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
from .models import ActionRun, ActionStep, Event, Project, Runner
from .ssh import SshError, SshTransport


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
        ssh_transport: SshTransport | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.worktree_root = worktree_root.resolve()
        self.artifact_root = artifact_root.resolve()
        self.cancel_grace_seconds = cancel_grace_seconds
        self.ssh_transport = ssh_transport
        self._last_output_event: dict[int, float] = {}
        self._reapers: set[asyncio.Task[int]] = set()

    async def run(self, run_id: int) -> None:
        run, project = await self._context(run_id)
        if run.runner_name != "local":
            await self._run_remote(run, project)
            return
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

    async def _run_remote(self, run: ActionRun, project: Project) -> None:
        if self.ssh_transport is None:
            raise RuntimeError("SSH transport is not configured")
        runner = self._snapshot_runner(run)
        remote_root = f"{runner.workspace_root}/runs/{run.id}"
        remote_workspace = f"~/{remote_root}/worktree"
        if not run.workspace_path:
            prepare = """set -eu
root="$HOME/$1"
mirror="$root/repositories/$2.git"
run="$root/runs/$5"
mkdir -p "$root/repositories" "$run"
chmod 700 "$run"
if [ ! -d "$mirror" ]; then git clone --mirror "$3" "$mirror"; else git -C "$mirror" remote set-url origin "$3"; fi
git -C "$mirror" fetch --prune origin
git -C "$mirror" cat-file -e "$4^{commit}"
git -C "$mirror" worktree add --detach "$run/worktree" "$4"
"""
            await self._remote_checked(
                runner,
                prepare,
                runner.workspace_root,
                project.key,
                run.origin or "",
                run.commit_sha,
                str(run.id),
            )
            await self._set_workspace(run.id, Path(remote_workspace))
        if run.log_offset == 0:
            await self._append(run, f"[CARLO] action {run.action_key} at {run.commit_sha}\n", ())

        values = self._secrets(run)
        local_environment = self.artifact_root / "actions" / str(run.id) / "remote-environment.json"
        local_environment.write_text(json.dumps(values))
        local_environment.chmod(0o600)
        wrapper = Path(__file__).with_name("remote_action_child.py")
        try:
            await self.ssh_transport.copy_file(
                runner, local_environment, f"{remote_root}/environment.json"
            )
            await self.ssh_transport.copy_file(
                runner, wrapper, f"{remote_root}/remote_action_child.py"
            )
            await self._remote_checked(
                runner,
                'root="$HOME/$1"; chmod 600 "$root/environment.json" && chmod 700 "$root/remote_action_child.py"',
                remote_root,
            )
            final_status = "succeeded"
            final_exit: int | None = 0
            for step in run.steps:
                if step.status == "succeeded":
                    continue
                if final_status != "succeeded":
                    await self._skip(step.id)
                    continue
                exit_code = await self._run_remote_step(
                    run, step, runner, remote_root, tuple(values.values())
                )
                if exit_code is None:
                    final_status, final_exit = "cancelled", None
                elif exit_code:
                    final_status, final_exit = "failed", exit_code
            await self._finish(run.id, final_status, final_exit)
            await self._remote_cleanup(run, runner, remote_root, project.key)
        finally:
            local_environment.unlink(missing_ok=True)

    def _snapshot_runner(self, run: ActionRun) -> Runner:
        snapshot = run.runner_snapshot
        required = {
            "name", "host", "port", "username", "identity_file", "workspace_root",
            "fingerprint", "host_key",
        }
        if not required <= snapshot.keys():
            raise RuntimeError("SSH runner snapshot is incomplete")
        return Runner(
            name=str(snapshot["name"]),
            host=str(snapshot["host"]),
            port=int(snapshot["port"]),
            username=str(snapshot["username"]),
            identity_file=str(snapshot["identity_file"]),
            workspace_root=str(snapshot["workspace_root"]),
            fingerprint=str(snapshot["fingerprint"]),
            host_key=str(snapshot["host_key"]),
            enabled=True,
        )

    async def _run_remote_step(
        self,
        run: ActionRun,
        step: ActionStep,
        runner: Runner,
        remote_root: str,
        secrets: tuple[str, ...],
    ) -> int | None:
        runtime = remote_root
        if step.status != "running":
            await self._start_step(run.id, step.id)
            await self._append(run, f"$ {step.command}\n", secrets)
            launch = """set -eu
runtime="$HOME/$1"
rm -f "$runtime/raw.log" "$runtime/state" "$runtime/state.tmp" "$runtime/process-group"
nohup python3 "$runtime/remote_action_child.py" "$runtime" "$runtime/worktree" "$runtime/environment.json" "$2" >/dev/null 2>&1 &
printf '%s' "$!"
"""
            _, stdout, _ = await self._remote_checked(
                runner, launch, runtime, json.dumps(shlex.split(step.command))
            )
            process_group = int(stdout.decode().strip())
            await self._set_process_group(run.id, process_group)

        raw_offset = 0
        offset_file = self._runtime(run.id) / "remote-offset"
        if offset_file.is_file():
            raw_offset = int(offset_file.read_text())
        while True:
            _, raw, _ = await self._remote_checked(
                runner,
                'runtime="$HOME/$1"; if [ -f "$runtime/raw.log" ]; then tail -c "+$(( $2 + 1 ))" -- "$runtime/raw.log"; fi',
                runtime,
                str(raw_offset),
            )
            if raw:
                await self._append(run, raw.decode(errors="replace"), secrets)
                raw_offset += len(raw)
                offset_file.parent.mkdir(parents=True, exist_ok=True)
                offset_file.write_text(str(raw_offset))
            _, state, _ = await self._remote_checked(
                runner, 'runtime="$HOME/$1"; if [ -f "$runtime/state" ]; then cat -- "$runtime/state"; fi', runtime
            )
            if state:
                exit_code = int(json.loads(state)["exit_code"])
                await self._set_process_group(run.id, None)
                await self._finish_step(
                    step.id, "succeeded" if exit_code == 0 else "failed", exit_code
                )
                await self._append(
                    run, f"[CARLO] step exited {exit_code}\n", secrets, force_event=True
                )
                offset_file.unlink(missing_ok=True)
                return exit_code
            current = await self._current(run.id)
            if current.cancel_requested_at is not None:
                await self._remote_cancel(runner, runtime)
                await self._set_process_group(run.id, None)
                await self._finish_step(step.id, "cancelled", None)
                offset_file.unlink(missing_ok=True)
                return None
            _, alive, _ = await self._remote_checked(
                runner,
                'runtime="$HOME/$1"; if [ -f "$runtime/process-group" ] && kill -0 -- "-$(cat "$runtime/process-group")" 2>/dev/null; then printf alive; fi',
                runtime,
            )
            if alive != b"alive":
                raise RuntimeError("remote action process ended without terminal state")
            await asyncio.sleep(0.25)

    async def _remote_cancel(self, runner: Runner, runtime: str) -> None:
        await self._remote_checked(
            runner,
            'runtime="$HOME/$1"; if [ -f "$runtime/process-group" ]; then kill -TERM -- "-$(cat "$runtime/process-group")" 2>/dev/null || true; fi',
            runtime,
        )
        await asyncio.sleep(self.cancel_grace_seconds)
        await self._remote_checked(
            runner,
            'runtime="$HOME/$1"; if [ -f "$runtime/process-group" ] && kill -0 -- "-$(cat "$runtime/process-group")" 2>/dev/null; then kill -KILL -- "-$(cat "$runtime/process-group")"; fi',
            runtime,
        )

    async def _remote_cleanup(
        self, run: ActionRun, runner: Runner, remote_root: str, project_key: str
    ) -> None:
        cleanup = """set -eu
root="$HOME/$1"
mirror="$root/repositories/$2.git"
run="$root/runs/$3"
git -C "$mirror" worktree remove --force "$run/worktree" 2>/dev/null || true
rm -f "$run/environment.json" "$run/raw.log" "$run/state" "$run/state.tmp" "$run/process-group" "$run/remote_action_child.py"
rmdir "$run" 2>/dev/null || true
"""
        try:
            await self._remote_checked(
                runner, cleanup, runner.workspace_root, project_key, str(run.id)
            )
        except Exception as error:
            async with self.session_factory() as session:
                current = await session.get(ActionRun, run.id)
                if current is not None:
                    current.cleanup_pending = True
                    current.internal_stage = "cleanup_pending"
                    current.error = f"remote cleanup failed: {error}"[:500]
                    await session.commit()
            return
        async with self.session_factory() as session:
            current = await session.get(ActionRun, run.id)
            if current is not None:
                if current.secret_path:
                    Path(current.secret_path).unlink(missing_ok=True)
                    current.secret_path = None
                current.workspace_path = None
                current.internal_stage = "complete"
                await session.commit()

    async def _remote_checked(
        self, runner: Runner, script: str, *arguments: str
    ) -> tuple[int, bytes, bytes]:
        assert self.ssh_transport is not None
        offline_event_sent = False
        while True:
            try:
                result = await self.ssh_transport.execute(runner, script, *arguments)
                if result[0]:
                    message = result[2].decode(errors="replace").strip() or "remote command failed"
                    if result[0] == 255:
                        raise SshError(message)
                    raise RuntimeError(message)
                return result
            except SshError as error:
                if not offline_event_sent:
                    await self._mark_reconnecting(runner.name, str(error))
                    offline_event_sent = True
                await asyncio.sleep(2)

    async def _mark_reconnecting(self, runner_name: str, error: str) -> None:
        async with self.session_factory() as session:
            run = await session.scalar(
                select(ActionRun).where(
                    ActionRun.status == "running", ActionRun.runner_name == runner_name
                )
            )
            if run is None:
                return
            run.internal_stage = "reconnecting"
            session.add(
                Event(
                    type="action.runner_offline",
                    payload={"run_id": run.id, "project_id": run.project_id, "runner": runner_name, "error": error[:500]},
                )
            )
            await session.commit()

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
