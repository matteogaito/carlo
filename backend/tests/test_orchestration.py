import json
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.domain import (
    AttemptSignal,
    TaskStage,
    TaskStatus,
    ValidationSnapshot,
    assess_progress,
    detect_stall,
    fingerprint,
)
from carlo.models import (
    AgentProfile as AgentProfileRecord,
    Attempt,
    Base,
    Escalation,
    Event,
    PlanRevision,
    Project,
    Task,
    ValidationRun,
)
from carlo.orchestrator import (
    ImplementationPipeline,
    Orchestrator,
    TaskStopRequested,
    major_deviation,
)
from carlo.provider import AgentProfile, AgentResult, ContextLimitError
from carlo.git import GitWorkspace
from tests.fakes import add_managed_profiles


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository_at(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("base\n")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "base")
    git(path, "branch", "carlo-Dev")
    return path


async def empty_orchestration_store():
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    return engine, async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )


def test_fewer_failures_is_progress() -> None:
    previous = ValidationSnapshot(failures=7, completed_steps=1)
    current = ValidationSnapshot(failures=2, completed_steps=1)
    assert assess_progress(previous, current).is_progress


def test_later_validation_step_is_progress() -> None:
    previous = ValidationSnapshot(failures=1, completed_steps=1)
    current = ValidationSnapshot(failures=1, completed_steps=2)
    assert assess_progress(previous, current).is_progress


def test_repeated_fingerprint_diff_and_strategy_is_stalled() -> None:
    history = [
        AttemptSignal("abc", "diff1", "fix parser"),
        AttemptSignal("abc", "diff1", "fix parser"),
    ]
    assert detect_stall(history).reason == "repeated_outcome"


def test_two_state_oscillation_is_stalled() -> None:
    history = [
        AttemptSignal("a", "1", "first"),
        AttemptSignal("b", "2", "second"),
        AttemptSignal("a", "1", "first"),
        AttemptSignal("b", "2", "second"),
    ]
    assert detect_stall(history).reason == "oscillation"


def test_fingerprint_ignores_paths_and_line_numbers() -> None:
    first = fingerprint(1, "/tmp/one/app.py:42: AssertionError")
    second = fingerprint(1, "/other/two/app.py:91: AssertionError")
    assert first == second


def test_major_deviation_is_extracted_from_provider_output() -> None:
    deviation = major_deviation(
        json.dumps(
            {
                "major_deviation": {
                    "summary": "Public API must change",
                    "reason": "Existing interface cannot support the goal",
                }
            }
        )
    )
    assert deviation == {
        "summary": "Public API must change",
        "reason": "Existing interface cannot support the goal",
    }


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            {
                "implementation_tasks": [
                    {
                        "title": "Add endpoint",
                        "prompt": "Reuse the existing API boundary.",
                        "intervention_points": ["backend/carlo/api.py:create_app"],
                    }
                ]
            },
            '"prompt": "Reuse the existing API boundary."',
        ),
        ({}, "Implementation tasks:\n[]"),
    ],
)
def test_implementation_instruction_preserves_structured_plan_tasks(
    metadata: dict, expected: str
) -> None:
    task = Task(id="CAR-1", goal="Add health endpoint")
    plan = PlanRevision(plan_markdown="Concise approved overview", metadata_json=metadata)

    instruction = ImplementationPipeline._implementation_instruction(task, plan, "follow plan")

    assert expected in instruction


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_succeeds", [True, False])
async def test_context_limit_rolls_over_to_a_focused_subtask_attempt(
    tmp_path: Path, recovery_succeeds: bool,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repository / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(repository), "branch", "carlo-Dev"], check=True)
    initial_integration = git(repository, "rev-parse", "carlo-Dev")

    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    command = "python3 -c \"from pathlib import Path; assert Path('feature.txt').read_text() == 'done'\""
    async with factory() as session:
        profiles = await add_managed_profiles(session, "implementation", "escalation")
        project = Project(
            name="CARLO",
            key="CAR",
            repository_path=str(repository),
            integration_branch="carlo-Dev",
        )
        task = Task(
            id="CAR-1",
            project=project,
            sequence=1,
            title="Feature",
            goal="Finish the focused subtask",
            status="READY",
            stage="QUEUED",
            approved_plan_revision=1,
        )
        plan = PlanRevision(
            task=task,
            revision=1,
            brief_markdown="Brief",
            plan_markdown="FULL PLAN MUST NOT BE RESENT",
            metadata_json={
                "implementation_tasks": [{"prompt": "ALL PLAN TASKS"}],
                "validation_commands": [command],
            },
        )
        session.add_all([project, task, plan])
        await session.commit()

    class Provider:
        def __init__(self) -> None:
            self.instructions: list[str] = []
            self.sessions: list[str] = []

        async def run(
            self,
            profile: AgentProfile,
            instruction: str,
            cwd: str,
            session_id: str,
            on_event=None,
        ) -> AgentResult:
            self.instructions.append(instruction)
            self.sessions.append(session_id)
            if len(self.instructions) == 1:
                Path(cwd, "feature.txt").write_text("partial")
                raise ContextLimitError("Prompt too long")
            if not recovery_succeeds:
                raise ContextLimitError("Still too long")
            assert Path(cwd, "feature.txt").read_text() == "partial"
            Path(cwd, "feature.txt").write_text("done")
            return AgentResult(session_id, "complete", (), 0)

        async def stop(self, session_id: str) -> None:
            return None

        def status(self, session_id: str) -> str:
            return "idle"

    provider = Provider()
    pipeline = ImplementationPipeline(
        factory, provider, tmp_path / "artifacts", max_attempts=5
    )

    assert await Orchestrator(engine, factory, pipeline.run).run_next() == "CAR-1"

    assert provider.sessions == ["CAR-1-implementation-1", "CAR-1-implementation-2"]
    assert "FULL PLAN MUST NOT BE RESENT" in provider.instructions[0]
    assert "Finish the focused subtask" in provider.instructions[1]
    assert "FULL PLAN MUST NOT BE RESENT" not in provider.instructions[1]
    assert "ALL PLAN TASKS" not in provider.instructions[1]
    async with factory() as session:
        task = await session.get(Task, "CAR-1")
        attempts = (await session.scalars(select(Attempt).order_by(Attempt.number))).all()
        context_events = (
            await session.scalars(
                select(Event).where(
                    Event.task_id == "CAR-1", Event.type == "execution.context_limit"
                )
            )
        ).all()
        assert task is not None
        assert task.status.value == ("DONE" if recovery_succeeds else "FAILED")
        assert [attempt.outcome for attempt in attempts] == (
            ["context_limit", "verified"]
            if recovery_succeeds
            else ["context_limit", "context_limit"]
        )
        assert attempts[0].checkpoint_sha
        integration_tip = git(repository, "rev-parse", "carlo-Dev")
        if recovery_succeeds:
            assert integration_tip == task.checkpoint_sha
        else:
            assert task.checkpoint_sha
            assert integration_tip == initial_integration
        assert len(context_events) == (1 if recovery_succeeds else 2)
    await engine.dispose()


@pytest.mark.asyncio
async def test_standalone_completion_promotes_integration_branch(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Feature")
    (repository / "feature.txt").write_text("done\n")
    checkpoint = await workspace.checkpoint(checkout, "validated")
    engine, factory = await empty_orchestration_store()
    async with factory() as session:
        project = Project(
            name="CARLO",
            key="CAR",
            repository_path=str(repository),
            integration_branch="carlo-Dev",
        )
        task = Task(
            id="CAR-1",
            project=project,
            sequence=1,
            title="Feature",
            goal="Feature",
            status=TaskStatus.IN_PROGRESS,
            stage=TaskStage.VALIDATING,
            branch_name=checkout.branch,
            worktree_path=str(repository),
            checkpoint_sha=checkpoint,
        )
        session.add_all([project, task])
        await session.commit()

    async def unused_runner(task_id: str) -> str:
        raise AssertionError(task_id)

    await Orchestrator(engine, factory, unused_runner)._finish("CAR-1", "validated")

    async with factory() as session:
        task = await session.get(Task, "CAR-1")
        events = (
            await session.scalars(select(Event).where(Event.task_id == "CAR-1"))
        ).all()
        assert task is not None
        assert task.status == TaskStatus.DONE
        assert git(repository, "rev-parse", "carlo-Dev") == checkpoint
        assert git(repository, "branch", "--show-current") == "CAR-1_feature"
        assert any(
            event.type == "git.integration_promoted"
            and event.payload
            == {"integration_branch": "carlo-Dev", "checkpoint": checkpoint}
            for event in events
        )

    next_checkout = await GitWorkspace(repository, "carlo-Dev").prepare(
        "CAR-2", "Next Feature"
    )
    assert next_checkout.branch == "CAR-2_nextfeature"
    assert git(repository, "merge-base", "--is-ancestor", checkpoint, "HEAD") == ""
    await engine.dispose()


@pytest.mark.asyncio
async def test_last_subtask_promotes_parent_checkpoint(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Parent Feature")
    (repository / "first.txt").write_text("first\n")
    first_checkpoint = await workspace.checkpoint(checkout, "first validated")
    (repository / "second.txt").write_text("second\n")
    second_checkpoint = await workspace.checkpoint(checkout, "second validated")
    assert git(repository, "rev-parse", "carlo-Dev") != second_checkpoint

    engine, factory = await empty_orchestration_store()
    async with factory() as session:
        project = Project(
            name="CARLO",
            key="CAR",
            repository_path=str(repository),
            integration_branch="carlo-Dev",
        )
        parent = Task(
            id="CAR-1",
            project=project,
            sequence=1,
            title="Parent Feature",
            goal="Parent",
            status=TaskStatus.IN_PROGRESS,
            stage=TaskStage.IMPLEMENTING,
        )
        first = Task(
            id="CAR-2",
            project=project,
            parent_task_id="CAR-1",
            subtask_position=0,
            sequence=2,
            title="First",
            goal="First",
            status=TaskStatus.DONE,
            stage=TaskStage.COMPLETE,
            branch_name=checkout.branch,
            worktree_path=str(repository),
            checkpoint_sha=first_checkpoint,
        )
        second = Task(
            id="CAR-3",
            project=project,
            parent_task_id="CAR-1",
            subtask_position=1,
            sequence=3,
            title="Second",
            goal="Second",
            status=TaskStatus.IN_PROGRESS,
            stage=TaskStage.VALIDATING,
            branch_name=checkout.branch,
            worktree_path=str(repository),
            checkpoint_sha=second_checkpoint,
        )
        session.add_all([project, parent, first, second])
        await session.commit()

    async def unused_runner(task_id: str) -> str:
        raise AssertionError(task_id)

    await Orchestrator(engine, factory, unused_runner)._finish("CAR-3", "validated")

    async with factory() as session:
        parent = await session.get(Task, "CAR-1")
        first = await session.get(Task, "CAR-2")
        second = await session.get(Task, "CAR-3")
        promotions = (
            await session.scalars(
                select(Event).where(Event.type == "git.integration_promoted")
            )
        ).all()
        assert parent is not None and parent.status == TaskStatus.DONE
        assert first is not None and first.status == TaskStatus.DONE
        assert second is not None and second.status == TaskStatus.DONE
        assert parent.checkpoint_sha == second_checkpoint
        assert git(repository, "rev-parse", "carlo-Dev") == second_checkpoint
        assert [(event.task_id, event.payload) for event in promotions] == [
            (
                "CAR-1",
                {
                    "integration_branch": "carlo-Dev",
                    "checkpoint": second_checkpoint,
                },
            )
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_diverged_integration_blocks_completion_without_looping(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Feature")
    (repository / "feature.txt").write_text("done\n")
    checkpoint = await workspace.checkpoint(checkout, "validated")
    git(repository, "switch", "carlo-Dev")
    (repository / "other.txt").write_text("other\n")
    git(repository, "add", "other.txt")
    git(repository, "commit", "-m", "diverged")
    diverged_sha = git(repository, "rev-parse", "HEAD")
    git(repository, "switch", checkout.branch)

    engine, factory = await empty_orchestration_store()
    async with factory() as session:
        project = Project(
            name="CARLO",
            key="CAR",
            repository_path=str(repository),
            integration_branch="carlo-Dev",
        )
        task = Task(
            id="CAR-1",
            project=project,
            sequence=1,
            title="Feature",
            goal="Feature",
            status=TaskStatus.IN_PROGRESS,
            stage=TaskStage.VALIDATING,
            branch_name=checkout.branch,
            worktree_path=str(repository),
            checkpoint_sha=checkpoint,
        )
        session.add_all([project, task])
        await session.commit()

    async def unused_runner(task_id: str) -> str:
        raise AssertionError(task_id)

    orchestrator = Orchestrator(engine, factory, unused_runner)
    await orchestrator._finish("CAR-1", "validated")

    async with factory() as session:
        task = await session.get(Task, "CAR-1")
        events = (
            await session.scalars(select(Event).where(Event.task_id == "CAR-1"))
        ).all()
        assert task is not None
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.stage == TaskStage.BLOCKED
        assert git(repository, "rev-parse", "carlo-Dev") == diverged_sha
        assert git(repository, "rev-parse", task.branch_name) == task.checkpoint_sha
        assert any(
            event.type == "git.integration_promotion_failed"
            and "diverged" in event.payload["error"]
            for event in events
        )
    assert await orchestrator.run_next() is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_checkout_retry_uses_the_parent_branch_and_full_plan(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repository / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(repository), "branch", "carlo-Dev"], check=True)

    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        profiles = await add_managed_profiles(session, "implementation", "escalation")
        implementation = profiles["implementation"]
        escalation = profiles["escalation"]
        project = Project(
            name="CARLO",
            key="CAR",
            repository_path=str(repository),
            integration_branch="carlo-Dev",
        )
        parent = Task(
            id="CAR-1",
            project=project,
            sequence=1,
            title="Parent Feature",
            goal="Complete the parent feature",
            status="IN_PROGRESS",
            stage="IMPLEMENTING",
        )
        task = Task(
            id="CAR-2",
            project=project,
            sequence=2,
            title="Child Feature",
            goal="Create feature.txt",
            status="READY",
            stage="QUEUED",
            approved_plan_revision=2,
            parent=parent,
            subtask_position=0,
        )
        plan = PlanRevision(
            task=task,
            revision=2,
            brief_markdown="Brief",
            plan_markdown="Plan",
            metadata_json={"skills": [], "validation_commands": ["test -f feature.txt"]},
        )
        session.add_all([implementation, escalation, project, parent, task, plan])
        await session.flush()
        session.add_all(
            [
                *[
                    Attempt(
                        task_id=task.id,
                        number=number,
                        profile_id=implementation.id,
                        instruction="failed cycle",
                        outcome="validation_failed",
                    )
                    for number in range(1, 4)
                ],
                Event(
                    task=task,
                    type="task.retry.started",
                    payload={"previous_attempt": 3, "fresh_checkout": True},
                ),
            ]
        )
        await session.commit()

    class Provider:
        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            assert "Approved plan:\nPlan" in instruction
            Path(cwd, "feature.txt").write_text("fresh\n")
            return AgentResult(session_id, "done", (), 0)

        async def stop(self, session_id: str) -> None:
            return None

        def status(self, session_id: str) -> str:
            return "idle"

    pipeline = ImplementationPipeline(
        factory, Provider(), tmp_path / "artifacts", max_attempts=1
    )
    await Orchestrator(engine, factory, pipeline.run).run_next()

    async with factory() as session:
        reworked = await session.get(Task, "CAR-2")
        assert reworked is not None
        assert reworked.status.value == "DONE"
        assert reworked.branch_name == "CAR-1_parentfeature"
        assert reworked.worktree_path == str(repository.resolve())
        attempts = (
            await session.scalars(select(Attempt).order_by(Attempt.number))
        ).all()
        assert [attempt.number for attempt in attempts] == [1, 2, 3, 4]
    await engine.dispose()


@pytest.mark.asyncio
async def test_subtask_retry_reuses_worktree_with_focused_instruction(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repository / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(repository), "branch", "carlo-Dev"], check=True)
    workspace = GitWorkspace(repository, "carlo-Dev")
    worktree = await workspace.prepare("CAR-2", "Child")
    Path(worktree.path, "feature.txt").write_text("partial")
    checkpoint = await workspace.checkpoint(worktree, "partial child")

    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    command = "python3 -c \"from pathlib import Path; assert Path('feature.txt').read_text() == 'done'\""
    async with factory() as session:
        profiles = await add_managed_profiles(session, "implementation", "escalation")
        implementation = profiles["implementation"]
        project = Project(
            name="CARLO",
            key="CAR",
            repository_path=str(repository),
            integration_branch="carlo-Dev",
        )
        task = Task(
            id="CAR-2",
            project=project,
            sequence=2,
            title="Child",
            goal="Finish only this child",
            status="READY",
            stage="QUEUED",
            approved_plan_revision=1,
            branch_name=worktree.branch,
            worktree_path=str(worktree.path),
            checkpoint_sha=checkpoint,
        )
        plan = PlanRevision(
            task=task,
            revision=1,
            brief_markdown="Brief",
            plan_markdown="FULL PLAN MUST NOT BE RESENT",
            metadata_json={"validation_commands": [command]},
        )
        session.add_all([project, task, plan])
        await session.flush()
        session.add_all(
            [
                Attempt(
                    task_id=task.id,
                    number=3,
                    profile_id=implementation.id,
                    instruction="Previous attempt",
                    outcome="validation_failed",
                ),
                Event(
                    task=task,
                    type="task.retry.started",
                    payload={"previous_attempt": 3, "checkpoint": checkpoint},
                ),
            ]
        )
        await session.commit()

    class Provider:
        def __init__(self) -> None:
            self.instruction = ""
            self.cwd = ""
            self.session_id = ""

        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            self.instruction = instruction
            self.cwd = cwd
            self.session_id = session_id
            assert Path(cwd, "feature.txt").read_text() == "partial"
            Path(cwd, "feature.txt").write_text("done")
            return AgentResult(session_id, "done", (), 0)

        async def stop(self, session_id: str) -> None:
            return None

        def status(self, session_id: str) -> str:
            return "idle"

    provider = Provider()
    pipeline = ImplementationPipeline(
        factory, provider, tmp_path / "artifacts", max_attempts=1
    )

    assert await Orchestrator(engine, factory, pipeline.run).run_next() == "CAR-2"
    assert provider.cwd == str(worktree.path)
    assert provider.session_id == "CAR-2-implementation-4"
    assert "Finish only this child" in provider.instruction
    assert "FULL PLAN MUST NOT BE RESENT" not in provider.instruction
    async with factory() as session:
        retried = await session.get(Task, "CAR-2")
        assert retried is not None and retried.status.value == "DONE"
    await engine.dispose()


@pytest.mark.asyncio
async def test_stall_escalates_then_local_validation_completes(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repository / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(repository), "branch", "carlo-Dev"], check=True)

    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    command = (
        "python3 -c \"from pathlib import Path; "
        "assert Path('feature.txt').read_text() == 'ok'\""
    )
    async with factory() as session:
        profiles = await add_managed_profiles(session, "implementation", "escalation")
        implementation = profiles["implementation"]
        escalation = profiles["escalation"]
        project = Project(
            name="CARLO",
            key="CAR",
            repository_path=str(repository),
            integration_branch="carlo-Dev",
        )
        task = Task(
            id="CAR-1",
            project=project,
            sequence=1,
            title="Feature",
            goal="Create a valid feature file",
            status="READY",
            stage="QUEUED",
            approved_plan_revision=1,
        )
        plan = PlanRevision(
            task=task,
            revision=1,
            brief_markdown="# Brief\nUse the repository.",
            plan_markdown="# Plan\nCreate feature.txt.",
            metadata_json={"skills": ["carlo-ui-design"], "validation_commands": [command]},
        )
        session.add_all([implementation, escalation, project, task, plan])
        await session.commit()

    class Provider:
        def __init__(self) -> None:
            self.implementation_calls = 0
            self.escalation_instruction = ""
            self.implementation_skills: tuple[str, ...] = ()

        async def run(
            self,
            profile: AgentProfile,
            instruction: str,
            cwd: str,
            session_id: str,
            on_event=None,
        ) -> AgentResult:
            if profile.name == "escalation":
                self.escalation_instruction = instruction
                return AgentResult(
                    session_id,
                    json.dumps({"diagnosis": "same broken value", "strategy": "write ok"}),
                    (),
                    0,
                )
            self.implementation_calls += 1
            self.implementation_skills = profile.skills
            Path(cwd, "feature.txt").write_text(
                "broken" if self.implementation_calls < 3 else "ok"
            )
            return AgentResult(
                session_id,
                "implementation complete",
                (),
                0,
                ("carlo-ui-design",),
            )

        async def stop(self, session_id: str) -> None:
            return None

        def status(self, session_id: str) -> str:
            return "idle"

    provider = Provider()
    pipeline = ImplementationPipeline(
        factory, provider, tmp_path / "artifacts", max_attempts=5
    )
    assert await Orchestrator(engine, factory, pipeline.run).run_next() == "CAR-1"

    async with factory() as session:
        task = await session.get(Task, "CAR-1")
        assert task.status.value == "DONE"
        assert task.branch_name == "CAR-1_feature"
        assert task.checkpoint_sha
        assert await session.scalar(select(func.count(Attempt.id))) == 3
        assert await session.scalar(select(func.count(Escalation.id))) == 1
        assert await session.scalar(select(func.count(ValidationRun.id))) == 3
        attempts = (await session.scalars(select(Attempt).order_by(Attempt.number))).all()
        assert all(attempt.artifact_path and Path(attempt.artifact_path).is_file() for attempt in attempts)
        completed = await session.scalar(
            select(Event)
            .where(Event.type == "agent.completed")
            .order_by(Event.sequence.desc())
        )
        assert completed is not None
        assert completed.payload["skills"] == ["carlo-ui-design"]
    assert "repeated_outcome" in provider.escalation_instruction
    assert provider.implementation_calls == 3
    assert provider.implementation_skills == ("carlo-ui-design",)
    await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_resumes_validation_without_rerunning_provider(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repository / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(repository), "branch", "carlo-Dev"], check=True)
    worktree = await GitWorkspace(repository, "carlo-Dev").prepare(
        "CAR-1", "Recover"
    )
    (worktree.path / "feature.txt").write_text("ok")

    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    command = (
        "python3 -c \"from pathlib import Path; "
        "assert Path('feature.txt').read_text() == 'ok'\""
    )
    async with factory() as session:
        profiles = await add_managed_profiles(session, "implementation", "escalation")
        implementation = profiles["implementation"]
        escalation = profiles["escalation"]
        project = Project(
            name="CARLO", key="CAR", repository_path=str(repository), integration_branch="carlo-Dev"
        )
        task = Task(
            id="CAR-1",
            project=project,
            sequence=1,
            title="Recover",
            goal="Resume validation",
            status="IN_PROGRESS",
            stage="VALIDATING",
            approved_plan_revision=1,
            branch_name=worktree.branch,
            worktree_path=str(worktree.path),
        )
        plan = PlanRevision(
            task=task,
            revision=1,
            brief_markdown="Brief",
            plan_markdown="Plan",
            metadata_json={"skills": [], "validation_commands": [command]},
        )
        session.add_all([implementation, escalation, project, task, plan])
        await session.flush()
        session.add(
            Attempt(
                task_id=task.id,
                number=1,
                profile_id=implementation.id,
                provider_session_id="CAR-1-implementation-1",
                instruction="Implement",
                outcome="completed",
            )
        )
        await session.commit()

    class Provider:
        async def run(self, *args, **kwargs):
            raise AssertionError("provider must not run during validation recovery")

        async def stop(self, session_id: str) -> None:
            return None

        def status(self, session_id: str) -> str:
            return "idle"

    pipeline = ImplementationPipeline(factory, Provider(), tmp_path / "artifacts")
    assert await pipeline.run("CAR-1") == "validated"
    async with factory() as session:
        task = await session.get(Task, "CAR-1")
        assert task.checkpoint_sha
        assert await session.scalar(select(func.count(ValidationRun.id))) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_stop_kills_running_provider_step() -> None:
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    class _NullProvider:
        pass

    pipeline = ImplementationPipeline(
        factory, _NullProvider(), Path("/tmp/carlo-test-art")
    )

    async with factory() as session:
        project = Project(
            name="X",
            key="X",
            repository_path=f"/tmp/x-{uuid.uuid4().hex}.git",
            integration_branch="main",
        )
        task = Task(
            id="CAR-KILL",
            project=project,
            sequence=1,
            title="Kill me",
            goal="do work",
            status="IN_PROGRESS",
            stage="IMPLEMENTING",
        )
        session.add_all([project, task])
        await session.commit()

    handler = pipeline._pi_step_handler("CAR-KILL")
    handler_event: dict = {"type": "response", "output": "thinking\x00 hard"}

    await handler(handler_event)  # task IN_PROGRESS -> emits pi.step
    async with factory() as session:
        task = await session.get(Task, "CAR-KILL")
        task.status = "READY"
        task.stage = "QUEUED"
        await session.commit()

    with pytest.raises(TaskStopRequested):
        await handler(handler_event)  # stopped -> kill request

    async with factory() as session:
        event = await session.scalar(
            select(Event).where(Event.task_id == "CAR-KILL", Event.type == "pi.step")
        )
        assert event is not None
        assert event.payload["summary"] == "thinking hard"
    await engine.dispose()
