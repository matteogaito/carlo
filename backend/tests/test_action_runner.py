import asyncio
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.action_runner import ACTION_LOCK, ActionExecutor, ActionOrchestrator
from carlo.models import ActionRun, ActionStep, Base, Event, Project, User
from carlo.orchestrator import IMPLEMENTATION_LOCK


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def database():
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE action_steps, action_runs, runners, "
                "notification_deliveries, notification_cursors, login_failures, "
                "user_sessions, project_memberships, users, events, validation_runs, "
                "escalations, attempts, plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.invalid")
    (repository / "ok.py").write_text(
        "import os, sys\nprint('out:' + os.environ['TOKEN'])\nprint('err-line', file=sys.stderr)\n"
    )
    (repository / "fail.py").write_text("raise SystemExit(7)\n")
    (repository / "wait.py").write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: None)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    (repository / "recover.py").write_text(
        "from pathlib import Path\n"
        "import time\n"
        "path = Path('marker')\n"
        "path.write_text(path.read_text() + 'x' if path.exists() else 'x')\n"
        "time.sleep(0.5)\n"
        "print('recovered-output')\n"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "commands")
    return repository


async def add_run(factory, repository: Path, artifact_root: Path, commands: list[str]) -> int:
    async with factory() as session:
        project = Project(name="Test", key="TST", repository_path=str(repository))
        user = User(username="admin", password_hash="hash", role="admin")
        session.add_all([project, user])
        await session.flush()
        run = ActionRun(
            project_id=project.id,
            requested_by_id=user.id,
            action_key="test",
            action_name="Test",
            definition={"name": "Test", "runner": "local", "env_file": ".env", "commands": commands},
            runner_name="local",
            runner_snapshot={"type": "local", "name": "local"},
            status="queued",
            commit_sha=git(repository, "rev-parse", "HEAD"),
            branch_name="main",
            env_file=".env",
            env_names=["TOKEN"],
            artifact_path=str(artifact_root / "actions" / "1" / "console.log"),
            secret_path=str(artifact_root / "actions" / "1" / "environment"),
            steps=[ActionStep(position=index, command=command) for index, command in enumerate(commands, 1)],
        )
        session.add(run)
        await session.commit()
        secret = Path(run.secret_path)
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("TOKEN=super-secret\n")
        secret.chmod(0o600)
        return run.id


@pytest.mark.asyncio
async def test_local_action_runs_at_commit_redacts_output_and_stops_on_failure(
    tmp_path: Path,
) -> None:
    engine, factory = await database()
    repository = make_repository(tmp_path)
    artifacts = tmp_path / "artifacts"
    commands = [f"{sys.executable} ok.py", f"{sys.executable} fail.py", f"{sys.executable} ok.py"]
    run_id = await add_run(factory, repository, artifacts, commands)
    executor = ActionExecutor(factory, tmp_path / "worktrees", artifacts)

    assert await ActionOrchestrator(engine, factory, executor.run).run_next() == run_id

    async with factory() as session:
        run = await session.get(ActionRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.exit_code == 7
        assert [step.status for step in run.steps] == ["succeeded", "failed", "skipped"]
        assert run.secret_path is None
        assert run.workspace_path is None
        event_types = set(await session.scalars(select(Event.type)))
        assert {"action.started", "action.output_available", "action.failed"} <= event_types
    console = Path(run.artifact_path).read_text()
    assert "out:***" in console
    assert "err-line" in console
    assert "super-secret" not in console
    assert not (tmp_path / "worktrees" / "actions" / str(run_id)).exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_action_queue_is_global_and_separate_from_implementation_lock() -> None:
    engine, factory = await database()
    async with factory() as session:
        project = Project(name="Test", key="TST", repository_path="/tmp/repo")
        user = User(username="admin", password_hash="hash", role="admin")
        session.add_all([project, user])
        await session.flush()
        for number in (1, 2):
            session.add(
                ActionRun(
                    project_id=project.id,
                    requested_by_id=user.id,
                    action_key=f"test-{number}",
                    action_name="Test",
                    definition={"commands": ["true"]},
                    runner_name="local",
                    runner_snapshot={"type": "local"},
                    status="queued",
                    commit_sha="a" * 40,
                    artifact_path=f"/tmp/{number}.log",
                )
            )
        await session.commit()

    entered: asyncio.Queue[int] = asyncio.Queue()
    release = asyncio.Event()

    async def hold(run_id: int) -> None:
        await entered.put(run_id)
        await release.wait()

    orchestrator = ActionOrchestrator(engine, factory, hold)
    first = asyncio.create_task(orchestrator.run_next())
    assert await asyncio.wait_for(entered.get(), 1) == 1
    second = asyncio.create_task(orchestrator.run_next())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(entered.get(), 0.1)
    assert ACTION_LOCK != IMPLEMENTATION_LOCK
    release.set()
    assert await first == 1
    assert await second == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_running_action_can_be_cancelled_idempotently(tmp_path: Path) -> None:
    engine, factory = await database()
    repository = make_repository(tmp_path)
    artifacts = tmp_path / "artifacts"
    run_id = await add_run(factory, repository, artifacts, [f"{sys.executable} wait.py", f"{sys.executable} ok.py"])
    executor = ActionExecutor(factory, tmp_path / "worktrees", artifacts, cancel_grace_seconds=0.05)
    running = asyncio.create_task(ActionOrchestrator(engine, factory, executor.run).run_next())

    for _ in range(100):
        async with factory() as session:
            run = await session.get(ActionRun, run_id)
            if run and run.process_group:
                run.cancel_requested_at = datetime.now(UTC)
                await session.commit()
                break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("action process did not start")

    assert await asyncio.wait_for(running, 3) == run_id
    async with factory() as session:
        run = await session.get(ActionRun, run_id)
        assert run is not None
        assert run.status == "cancelled"
        assert [step.status for step in run.steps] == ["cancelled", "skipped"]
        assert run.process_group is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_executor_restart_reattaches_without_rerunning_command(tmp_path: Path) -> None:
    engine, factory = await database()
    repository = make_repository(tmp_path)
    artifacts = tmp_path / "artifacts"
    run_id = await add_run(factory, repository, artifacts, [f"{sys.executable} recover.py"])
    async with factory() as session:
        run = await session.get(ActionRun, run_id)
        assert run is not None
        run.status = "running"
        run.internal_stage = "preparing"
        run.started_at = datetime.now(UTC)
        await session.commit()

    first = ActionExecutor(factory, tmp_path / "worktrees", artifacts)
    interrupted = asyncio.create_task(first.run(run_id))
    for _ in range(100):
        async with factory() as session:
            run = await session.get(ActionRun, run_id)
            if run and run.process_group:
                break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("action child did not start")
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted

    restarted = ActionExecutor(factory, tmp_path / "worktrees", artifacts)
    await restarted.run(run_id)
    async with factory() as session:
        run = await session.get(ActionRun, run_id)
        assert run is not None
        assert run.status == "succeeded"
        assert run.steps[0].status == "succeeded"
    console = Path(run.artifact_path).read_text()
    assert "recovered-output" in console
    assert console.count("$ ") == 1
    await engine.dispose()


class FakeRemote:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.copies: list[tuple[Path, str]] = []
        self.raw_sent = False

    async def execute(self, runner, script: str, *arguments: str):
        self.calls.append((script, arguments))
        assert "super-secret" not in str(arguments)
        if "cat -- \"$runtime/state\"" in script:
            return 0, b'{"exit_code": 0}', b""
        if "tail -c" in script:
            if self.raw_sent:
                return 0, b"", b""
            self.raw_sent = True
            return 0, b"remote:super-secret\n", b""
        if "printf '%s' \"$!\"" in script:
            return 0, b"4321", b""
        if "printf alive" in script:
            return 0, b"alive", b""
        return 0, b"", b""

    async def copy_file(self, runner, source: Path, destination: str) -> None:
        self.copies.append((source, destination))


@pytest.mark.asyncio
async def test_ssh_action_fetches_exact_commit_and_collects_redacted_output(
    tmp_path: Path,
) -> None:
    engine, factory = await database()
    repository = make_repository(tmp_path)
    artifacts = tmp_path / "artifacts"
    identity = tmp_path / "id_ed25519"
    identity.write_text("private")
    identity.chmod(0o600)
    run_id = await add_run(factory, repository, artifacts, [f"{sys.executable} ok.py"])
    async with factory() as session:
        run = await session.get(ActionRun, run_id)
        assert run is not None
        run.runner_name = "linux-build"
        run.origin = "ssh://git@example.invalid/repo.git"
        run.runner_snapshot = {
            "type": "ssh",
            "name": "linux-build",
            "host": "builder.example",
            "port": 22,
            "username": "deploy",
            "identity_file": str(identity),
            "workspace_root": ".carlo",
            "fingerprint": "SHA256:abc",
            "host_key": "builder.example ssh-ed25519 AAAATEST",
        }
        await session.commit()
    remote = FakeRemote()
    executor = ActionExecutor(factory, tmp_path / "worktrees", artifacts, ssh_transport=remote)

    assert await ActionOrchestrator(engine, factory, executor.run).run_next() == run_id

    async with factory() as session:
        run = await session.get(ActionRun, run_id)
        assert run is not None
        assert run.status == "succeeded"
        assert run.steps[0].status == "succeeded"
        assert run.secret_path is None
    console = Path(run.artifact_path).read_text()
    assert "remote:***" in console
    assert "super-secret" not in console
    flattened = " ".join(str(call) for call in remote.calls)
    assert run.commit_sha in flattened
    assert run.origin in flattened
    assert len(remote.copies) == 2
    await engine.dispose()
