import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.domain import (
    AttemptSignal,
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
from carlo.orchestrator import ImplementationPipeline, Orchestrator, major_deviation
from carlo.provider import AgentProfile, AgentResult
from carlo.git import GitWorkspace


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
async def test_rework_execution_uses_a_clean_numbered_worktree(tmp_path: Path) -> None:
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

    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
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
        implementation = AgentProfileRecord(name="implementation", provider="pi")
        escalation = AgentProfileRecord(name="escalation", provider="pi")
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
            goal="Create feature.txt",
            status="READY",
            stage="QUEUED",
            approved_plan_revision=2,
        )
        plan = PlanRevision(
            task=task,
            revision=2,
            brief_markdown="Brief",
            plan_markdown="Plan",
            metadata_json={"skills": [], "validation_commands": ["test -f feature.txt"]},
        )
        session.add_all([implementation, escalation, project, task, plan])
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
                    type="task.rework.started",
                    payload={"cycle": 1, "previous_attempt": 3},
                ),
            ]
        )
        await session.commit()

    class Provider:
        async def run(self, profile, instruction, cwd, session_id):
            Path(cwd, "feature.txt").write_text("fresh\n")
            return AgentResult(session_id, "done", (), 0)

        async def stop(self, session_id: str) -> None:
            return None

        def status(self, session_id: str) -> str:
            return "idle"

    pipeline = ImplementationPipeline(
        factory,
        Provider(),
        tmp_path / "worktrees",
        tmp_path / "artifacts",
        max_attempts=1,
    )
    await Orchestrator(engine, factory, pipeline.run).run_next()

    async with factory() as session:
        reworked = await session.get(Task, "CAR-1")
        assert reworked is not None
        assert reworked.status.value == "DONE"
        assert reworked.branch_name == "CAR-1-feature-rework-1"
        attempts = (
            await session.scalars(select(Attempt).order_by(Attempt.number))
        ).all()
        assert [attempt.number for attempt in attempts] == [1, 2, 3, 4]
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

    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
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
        implementation = AgentProfileRecord(name="implementation", provider="pi")
        escalation = AgentProfileRecord(name="escalation", provider="pi")
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
            metadata_json={"skills": ["testing"], "validation_commands": [command]},
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
                ("testing",),
            )

        async def stop(self, session_id: str) -> None:
            return None

        def status(self, session_id: str) -> str:
            return "idle"

    provider = Provider()
    pipeline = ImplementationPipeline(
        factory,
        provider,
        tmp_path / "worktrees",
        tmp_path / "artifacts",
        max_attempts=5,
    )
    assert await Orchestrator(engine, factory, pipeline.run).run_next() == "CAR-1"

    async with factory() as session:
        task = await session.get(Task, "CAR-1")
        assert task.status.value == "DONE"
        assert task.branch_name == "CAR-1-feature"
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
        assert completed.payload["skills"] == ["testing"]
    assert "repeated_outcome" in provider.escalation_instruction
    assert provider.implementation_calls == 3
    assert provider.implementation_skills == ("testing",)
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
    worktree = await GitWorkspace(
        repository, tmp_path / "worktrees", "carlo-Dev"
    ).prepare("CAR-1", "Recover")
    (worktree.path / "feature.txt").write_text("ok")

    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
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
        implementation = AgentProfileRecord(name="implementation", provider="pi")
        escalation = AgentProfileRecord(name="escalation", provider="pi")
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

    pipeline = ImplementationPipeline(
        factory, Provider(), tmp_path / "worktrees", tmp_path / "artifacts"
    )
    assert await pipeline.run("CAR-1") == "validated"
    async with factory() as session:
        task = await session.get(Task, "CAR-1")
        assert task.checkpoint_sha
        assert await session.scalar(select(func.count(ValidationRun.id))) == 1
    await engine.dispose()
