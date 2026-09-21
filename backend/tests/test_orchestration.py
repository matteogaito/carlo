import json
import subprocess
import uuid
from datetime import UTC, datetime
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
    AvailableModel,
    Base,
    Escalation,
    Event,
    ModelProvider,
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
from carlo.provider import AgentProfile, AgentResult, ContextLimitError, ProviderError
from carlo.git import GitWorkspace
from carlo.git import Checkout
from tests.test_work_package_escalation import package as escalation_package
from tests.fakes import add_managed_profiles


@pytest.mark.asyncio
async def test_last_child_runs_local_and_parent_integration_checks(tmp_path: Path) -> None:
    engine, factory = await empty_orchestration_store()
    package = {
        "id": "final", "title": "Finish", "position": 0, "objective": "Finish feature",
        "files": [{"path": "local.ok", "mode": "create", "reason": "Local proof"}],
        "interfaces": ["Final result"], "changes": {"local.ok": "Write it"},
        "constraints": [], "verification": {"commands": ["test -f local.ok"], "success": "Exists"},
        "done_when": ["Local and integration pass"],
    }
    async with factory() as session:
        project = Project(name="Test", key="TST", repository_path=str(tmp_path))
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Build", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1)
        first = Task(id="TST-2", project=project, sequence=2, title="First", goal="First", parent=parent, subtask_position=0, status=TaskStatus.DONE, stage=TaskStage.COMPLETE)
        last = Task(id="TST-3", project=project, sequence=3, title="Last", goal="Last", parent=parent, subtask_position=1, status=TaskStatus.IN_PROGRESS, stage=TaskStage.VALIDATING, approved_plan_revision=1)
        parent_plan = PlanRevision(task=parent, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"validation_commands": ["test -f integration.ok"]})
        child_plan = PlanRevision(task=last, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"implementation_tasks": [package], "validation_commands": ["true"]})
        attempt = Attempt(task_id=last.id, number=1, instruction="Finish")
        session.add_all([project, parent, first, last, parent_plan, child_plan])
        await session.flush()
        session.add(attempt)
        await session.commit()
        attempt_id = attempt.id
    (tmp_path / "local.ok").write_text("yes")
    pipeline = ImplementationPipeline(factory, object(), tmp_path / "artifacts")
    failed = await pipeline._validate(last, child_plan, Checkout("main", tmp_path), attempt_id, 1)
    assert failed.passed is False
    (tmp_path / "integration.ok").write_text("yes")
    passed = await pipeline._validate(last, child_plan, Checkout("main", tmp_path), attempt_id, 2)
    assert passed.passed is True
    async with factory() as session:
        revision = await session.scalar(select(PlanRevision).where(PlanRevision.task_id == parent.id))
        revision.metadata_json = {"validation_commands": ["missing-integration-check-command"]}
        await session.commit()
    unavailable = await pipeline._validate(last, child_plan, Checkout("main", tmp_path), attempt_id, 3)
    assert unavailable.passed is False
    async with factory() as session:
        commands = (await session.scalars(select(ValidationRun.command).where(ValidationRun.task_id == last.id).order_by(ValidationRun.id))).all()
        assert commands == ["test -f local.ok", "test -f integration.ok"] * 2 + ["test -f local.ok", "missing-integration-check-command"]
        last_run = await session.scalar(select(ValidationRun).where(ValidationRun.task_id == last.id).order_by(ValidationRun.id.desc()).limit(1))
        assert last_run.classification == "UNVERIFIABLE"
    await engine.dispose()


@pytest.mark.asyncio
async def test_xcode_validation_fails_when_a_requested_test_suite_did_not_run(tmp_path: Path) -> None:
    engine, factory = await empty_orchestration_store()
    fake_xcodebuild = tmp_path / "xcodebuild"
    fake_xcodebuild.write_text(
        "#!/bin/sh\n"
        "echo 'Command line invocation:'\n"
        "echo '    -only-testing:PhotoDiggerTests/MissingTests'\n"
        "echo '    -only-testing:PhotoDiggerTests/OtherExisting'\n"
        "echo '    -only-testing:PhotoDiggerTests/ExistingTests/testWork'\n"
        "echo '    -only-testing:PhotoDiggerTests/ExistingTests/testWorks'\n"
        "echo \"Test Suite 'ExistingTests' started at 2026-09-21\"\n"
        "echo \"Test Case '-[PhotoDiggerTests.OtherExistingTests testWorks]' passed (0.001 seconds).\"\n"
        "echo \"Test Case '-[PhotoDiggerTests.ExistingTests testWorksLonger]' passed (0.001 seconds).\"\n"
        "echo \"Test Case '-[PhotoDiggerTests.ExistingTests testWorks]' passed (0.001 seconds).\"\n"
        "exit 0\n"
    )
    fake_xcodebuild.chmod(0o755)
    command = (
        f"{fake_xcodebuild} test -only-testing:PhotoDiggerTests/MissingTests "
        "-only-testing:PhotoDiggerTests/OtherExisting "
        "-only-testing:PhotoDiggerTests/ExistingTests/testWork "
        "-only-testing:PhotoDiggerTests/ExistingTests/testWorks"
    )
    package = {
        "id": "final", "title": "Finish", "position": 0, "objective": "Finish feature",
        "files": [{"path": "feature.swift", "mode": "create", "reason": "Feature"}],
        "interfaces": ["Feature exists"], "changes": {"feature.swift": "Create it"},
        "constraints": [], "verification": {"commands": [command], "success": "Both suites run"},
        "done_when": ["Both suites pass"],
    }
    async with factory() as session:
        project = Project(name="Test", key="TST", repository_path=str(tmp_path))
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Build", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING)
        child = Task(id="TST-2", project=project, sequence=2, title="Child", goal="Build", parent=parent, subtask_position=0, status=TaskStatus.IN_PROGRESS, stage=TaskStage.VALIDATING, approved_plan_revision=1)
        plan = PlanRevision(task=child, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"implementation_tasks": [package]})
        session.add_all([project, parent, child, plan])
        await session.flush()
        attempt = Attempt(task_id=child.id, number=1, instruction="Build")
        session.add(attempt)
        await session.commit()
        attempt_id = attempt.id

    batch = await ImplementationPipeline(factory, object(), tmp_path / "artifacts")._validate(
        child, plan, Checkout("main", tmp_path), attempt_id, 1
    )
    assert batch.passed is False
    missing = set(batch.summary.rsplit("CARLO: requested tests did not run: ", 1)[1].split(", "))
    assert missing == {
        "PhotoDiggerTests/MissingTests",
        "PhotoDiggerTests/OtherExisting",
        "PhotoDiggerTests/ExistingTests/testWork",
    }
    async with factory() as session:
        run = await session.scalar(select(ValidationRun).where(ValidationRun.task_id == child.id))
        assert run.classification == "PARTIALLY_VERIFIED"
    await engine.dispose()


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


@pytest.mark.asyncio
async def test_work_package_escalation_creates_approved_revision_only_within_scope(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    engine, factory = await empty_orchestration_store()
    original = escalation_package()
    followup = {**original, "id": "wp-2", "title": "Ship parser", "position": 1}
    revised = {**original, "changes": {"src/parser.py": "Handle empty input too"}}
    async with factory() as session:
        project = Project(name="Test", key="TST", repository_path=str(repository), next_task_sequence=4)
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Parse", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1)
        child = Task(id="TST-2", project=project, sequence=2, title="Child", goal="Parse", parent=parent, subtask_position=0, status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1)
        sibling = Task(id="TST-3", project=project, sequence=3, title="Ship parser", goal="Ship", parent=parent, subtask_position=1, status=TaskStatus.READY, stage=TaskStage.QUEUED, approved_plan_revision=1)
        plan = PlanRevision(task=child, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"implementation_tasks": [original]})
        parent_plan = PlanRevision(task=parent, revision=1, brief_markdown="Parent brief", plan_markdown="Parent plan", metadata_json={"implementation_tasks": [original, followup]}, approved_at=datetime.now(UTC))
        session.add_all([project, parent, child, sibling, plan, parent_plan])
        await session.commit()

    class Provider:
        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            assert "may not raise max_tool_calls above the original package budget" in instruction
            assert "may be corrected or reverted without human approval" in instruction
            assert "Failing tests are not a human blocker" in instruction
            return AgentResult(session_id, json.dumps({"action": "revise", "diagnosis": "Missing empty case", "package": revised}), (), 0)

    pipeline = ImplementationPipeline(factory, Provider(), tmp_path / "artifacts")
    outcome = await pipeline._escalate_work_package(
        child, plan, AgentProfile("escalation", None, None, (), ()),
        Checkout("main", repository), original, "validation_failed", [],
    )
    assert outcome == "retry"
    async with factory() as session:
        updated = await session.get(Task, "TST-2")
        assert updated.approved_plan_revision == 2
        revision = await session.scalar(select(PlanRevision).where(PlanRevision.task_id == "TST-2", PlanRevision.revision == 2))
        assert revision.approved_at is not None
        assert revision.metadata_json["implementation_tasks"] == [revised]

    split_a = {**original, "id": "wp-1a", "budget": {"max_tool_calls": 10}}
    split_b = {**original, "id": "wp-1b", "title": "Fix parser part 2", "budget": {"max_tool_calls": 10}}

    class SplitProvider:
        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            return AgentResult(session_id, json.dumps({"action": "split", "diagnosis": "Need two smaller packages", "packages": [split_a, split_b]}), (), 0)

    pipeline.provider = SplitProvider()
    assert await pipeline._escalate_work_package(
        child, plan, AgentProfile("escalation", None, None, (), ()),
        Checkout("main", repository), original, "validation_failed", [],
    ) == "failed"
    async with factory() as session:
        superseded = await session.get(Task, "TST-2")
        assert superseded.superseded_at is not None
        parent_revision = await session.scalar(select(PlanRevision).where(PlanRevision.task_id == "TST-1", PlanRevision.revision == 2))
        assert parent_revision is not None
        assert parent_revision.approved_at is not None
        assert parent_revision.metadata_json["implementation_tasks"] == [
            split_a,
            {**split_b, "position": 1},
            {**followup, "position": 2},
        ]
        updated_parent = await session.get(Task, "TST-1")
        assert updated_parent.approved_plan_revision == 2
        new_children = (await session.scalars(select(Task).where(
            Task.parent_task_id == "TST-1", Task.superseded_at.is_(None)
        ).order_by(Task.subtask_position))).all()
        assert [(child.title, child.subtask_position) for child in new_children] == [
            ("Fix parser", 0),
            ("Fix parser part 2", 1),
            ("Ship parser", 2),
        ]
        assert (await session.get(Task, "TST-3")).superseded_at is None

    out_of_scope_a = {**original, "id": "wp-2a", "files": [*original["files"], {"path": "src/new.py", "mode": "create", "reason": "New"}],
                       "changes": {**original["changes"], "src/new.py": "Create"}}

    class OutOfScopeSplitProvider:
        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            return AgentResult(session_id, json.dumps({"action": "split", "diagnosis": "Needs a new file", "packages": [out_of_scope_a]}), (), 0)

    pipeline.provider = OutOfScopeSplitProvider()
    assert await pipeline._escalate_work_package(
        child, plan, AgentProfile("escalation", None, None, (), ()),
        Checkout("main", repository), original, "validation_failed", [],
    ) == "blocked"
    async with factory() as session:
        blocked_parent = await session.get(Task, "TST-1")
        assert blocked_parent.stage == TaskStage.BLOCKED
        assert blocked_parent.approved_plan_revision == 2
        pending_revision = await session.scalar(select(PlanRevision).where(PlanRevision.task_id == "TST-1", PlanRevision.revision == 3))
        assert pending_revision is not None
        assert pending_revision.approved_at is None

    expanded = {**original, "files": [*original["files"], {"path": "src/new.py", "mode": "create", "reason": "New scope"}],
                "changes": {**original["changes"], "src/new.py": "Create"}}

    class ExpandedProvider:
        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            return AgentResult(session_id, json.dumps({"action": "revise", "diagnosis": "Need another file", "package": expanded}), (), 0)

    pipeline.provider = ExpandedProvider()
    assert await pipeline._escalate_work_package(
        child, plan, AgentProfile("escalation", None, None, (), ()),
        Checkout("main", repository), original, "validation_failed", [],
    ) == "blocked"
    async with factory() as session:
        proposal = await session.scalar(select(PlanRevision).where(PlanRevision.task_id == "TST-2", PlanRevision.revision == 3))
        assert proposal.approved_at is None
        assert (await session.get(Task, "TST-2")).approved_plan_revision == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_revise_escalation_upgrades_model_for_expert_retry(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    engine, factory = await empty_orchestration_store()
    original = escalation_package()
    revised = {**original, "changes": {"src/parser.py": "Handle empty input too"}}
    async with factory() as session:
        project = Project(name="Test", key="TST", repository_path=str(repository))
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Parse", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING)
        child = Task(id="TST-2", project=project, sequence=2, title="Child", goal="Parse", parent=parent, subtask_position=0, status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1, available_model_id=None)
        plan = PlanRevision(task=child, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"implementation_tasks": [original]})
        expert_provider = ModelProvider(name="Expert", slug=f"expert-{uuid.uuid4().hex[:12]}", kind="openai-compatible", base_url="http://expert.test/v1")
        expert_model = AvailableModel(model_provider=expert_provider, external_id="expert-model", status="AVAILABLE", discovered_context_window=65_536, discovered_max_tokens=16_384)
        session.add_all([project, parent, child, plan, expert_provider, expert_model])
        await session.commit()
        expert_model_id = expert_model.id

    class ReviseProvider:
        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            return AgentResult(session_id, json.dumps({"action": "revise", "diagnosis": "Missing empty case", "package": revised}), (), 0)

    coder_expert = AgentProfileRecord(name="coder-expert", provider="pi", available_model_id=expert_model_id)
    pipeline = ImplementationPipeline(factory, ReviseProvider(), tmp_path / "artifacts")

    assert await pipeline._escalate_work_package(
        child, plan, AgentProfile("escalation", None, None, (), ()),
        Checkout("main", repository), original, "validation_failed", [],
        escalation_count=0, coder_expert=coder_expert,
    ) == "retry"
    async with factory() as session:
        assert (await session.get(Task, "TST-2")).available_model_id == expert_model_id
        upgrade_event = await session.scalar(select(Event).where(Event.task_id == "TST-2", Event.type == "escalation.model_upgraded"))
        assert upgrade_event is not None
        assert upgrade_event.payload["profile"] == "coder-expert"
    await engine.dispose()


@pytest.mark.asyncio
async def test_automatic_package_revision_gets_new_productive_retries(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    (repository / "src").mkdir()
    (repository / "src/parser.py").write_text("base\n")
    subprocess.run(["git", "-C", str(repository), "add", "src/parser.py"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "add parser"], check=True)
    subprocess.run(["git", "-C", str(repository), "branch", "-f", "carlo-Dev", "HEAD"], check=True)
    engine, factory = await empty_orchestration_store()
    original = escalation_package()
    original = {
        **original,
        "verification": {"commands": ["grep -q done src/parser.py"], "success": "Exit zero"},
    }
    revised = {**original, "changes": {"src/parser.py": "Write done"}}
    async with factory() as session:
        await add_managed_profiles(session, "implementation", "escalation", "coder-expert")
        project = Project(name="Test", key="TST", repository_path=str(repository))
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Parse", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING)
        child = Task(id="TST-2", project=project, sequence=2, title="Child", goal="Parse", parent=parent, subtask_position=0, status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1)
        plan = PlanRevision(task=child, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"implementation_tasks": [original], "validation_commands": ["grep -q done src/parser.py"]})
        session.add_all([project, parent, child, plan])
        await session.commit()

    class Provider:
        implementation_calls = 0

        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            if profile.name == "escalation":
                return AgentResult(session_id, json.dumps({"action": "revise", "diagnosis": "Use the exact output", "package": revised}), (), 0)
            self.implementation_calls += 1
            Path(cwd, "src/parser.py").write_text(
                "done\n" if self.implementation_calls == 4 else "wrong\n"
            )
            return AgentResult(session_id, "done", (), 0)

    pipeline = ImplementationPipeline(factory, Provider(), tmp_path / "artifacts")
    outcome = await pipeline.run("TST-2")
    assert outcome == "validated", pipeline.provider.implementation_calls
    async with factory() as session:
        attempts = (await session.scalars(select(Attempt).where(Attempt.task_id == "TST-2").order_by(Attempt.number))).all()
        assert [attempt.outcome for attempt in attempts] == [
            "validation_failed", "validation_failed", "validation_failed", "verified"
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_subtask_budget_stops_after_three_local_sessions_then_escalates(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    engine, factory = await empty_orchestration_store()
    package = {
        "id": "wp-1", "title": "Create feature", "position": 0,
        "objective": "Create feature.txt", "files": [{"path": "feature.txt", "mode": "create", "reason": "Output"}],
        "interfaces": ["feature.txt exists"], "changes": {"feature.txt": "Write done"},
        "constraints": [], "verification": {"commands": ["test -f feature.txt"], "success": "Exit zero"},
        "done_when": ["File exists"], "budget": {"max_tool_calls": 2},
    }
    async with factory() as session:
        await add_managed_profiles(session, "implementation", "escalation")
        project = Project(name="Test", key="TST", repository_path=str(repository))
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Build", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING)
        child = Task(id="TST-2", project=project, sequence=2, title="Child", goal="Create feature", parent=parent, subtask_position=0, status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1)
        session.add_all([project, parent, child, PlanRevision(task=child, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"implementation_tasks": [package], "validation_commands": ["test -f feature.txt"]})])
        await session.commit()

    class Provider:
        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            if profile.name == "escalation":
                return AgentResult(session_id, json.dumps({"action": "blocked", "diagnosis": "Human needed"}), (), 0)
            for _ in range(3):
                await on_event({"type": "tool_execution_start", "toolName": "read", "args": {"path": "README.md"}})
            raise AssertionError("the third tool must be stopped")

    pipeline = ImplementationPipeline(factory, Provider(), tmp_path / "artifacts")
    assert await pipeline.run("TST-2") == "blocked"
    async with factory() as session:
        attempts = (await session.scalars(select(Attempt).where(Attempt.task_id == "TST-2").order_by(Attempt.number))).all()
        assert [attempt.outcome for attempt in attempts] == ["budget_exceeded", "budget_exceeded", "budget_exceeded"]
        metrics = (await session.scalars(select(Event).where(Event.task_id == "TST-2", Event.type == "execution.session_metrics"))).all()
        assert len(metrics) == 3
        assert all(event.payload["outcome"] == "budget_exceeded" for event in metrics)
        escalation = await session.scalar(select(Escalation).where(Escalation.task_id == "TST-2"))
        assert escalation.evidence["reason"] == "repeated_outcome"

    class CrashedProvider:
        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            await on_event({"type": "message_end", "message": {"role": "assistant", "usage": {"input": 500, "output": 20}}})
            raise ProviderError("model unavailable")

    async with factory() as session:
        session.add(Event(task_id="TST-2", type="task.retry.started", payload={"previous_attempt": 3}))
        await session.commit()
    pipeline.provider = CrashedProvider()
    with pytest.raises(ProviderError, match="model unavailable"):
        await pipeline.run("TST-2")
    async with factory() as session:
        latest = await session.scalar(select(Event).where(Event.task_id == "TST-2", Event.type == "execution.session_metrics").order_by(Event.sequence.desc()))
        assert latest.payload["outcome"] == "failed"
        assert latest.payload["model_calls"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_subtask_budget_overruns_do_not_consume_productive_retries(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    engine, factory = await empty_orchestration_store()
    package = {
        "id": "wp-1", "title": "Create feature", "position": 0,
        "objective": "Create feature.txt", "files": [{"path": "feature.txt", "mode": "create", "reason": "Output"}],
        "interfaces": ["feature.txt exists"], "changes": {"feature.txt": "Write done"},
        "constraints": [], "verification": {"commands": ["grep -q done feature.txt"], "success": "Exit zero"},
        "done_when": ["File contains done"], "budget": {"max_tool_calls": 2},
    }
    async with factory() as session:
        await add_managed_profiles(session, "implementation", "escalation")
        project = Project(name="Test", key="TST", repository_path=str(repository))
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Build", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING)
        child = Task(id="TST-2", project=project, sequence=2, title="Child", goal="Create feature", parent=parent, subtask_position=0, status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1)
        session.add_all([project, parent, child, PlanRevision(task=child, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"implementation_tasks": [package], "validation_commands": ["grep -q done feature.txt"]})])
        await session.commit()

    class Provider:
        calls = 0

        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            if profile.name == "escalation":
                return AgentResult(session_id, json.dumps({"action": "blocked", "diagnosis": "Human needed"}), (), 0)
            self.calls += 1
            if self.calls <= 2:
                for _ in range(3):
                    await on_event({"type": "tool_execution_start", "toolName": "read", "args": {"path": "README.md"}})
            Path(cwd, "feature.txt").write_text("wrong\n" if self.calls == 3 else "done\n")
            return AgentResult(session_id, "done", (), 0)

    pipeline = ImplementationPipeline(factory, Provider(), tmp_path / "artifacts")
    assert await pipeline.run("TST-2") == "validated"
    async with factory() as session:
        attempts = (await session.scalars(select(Attempt).where(Attempt.task_id == "TST-2").order_by(Attempt.number))).all()
        assert [attempt.outcome for attempt in attempts] == [
            "budget_exceeded", "budget_exceeded", "validation_failed", "verified"
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_subtask_budget_retry_does_not_repack_partial_generated_files(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    engine, factory = await empty_orchestration_store()
    package = {
        "id": "wp-1", "title": "Create feature", "position": 0,
        "objective": "Create feature.txt", "files": [{"path": "feature.txt", "mode": "create", "reason": "Output"}],
        "interfaces": ["feature.txt exists"], "changes": {"feature.txt": "Write done"},
        "constraints": [], "verification": {"commands": ["test -f feature.txt"], "success": "Exit zero"},
        "done_when": ["File exists"], "budget": {"max_tool_calls": 2},
    }
    async with factory() as session:
        await add_managed_profiles(session, "implementation", "escalation")
        project = Project(name="Test", key="TST", repository_path=str(repository))
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Build", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING)
        child = Task(id="TST-2", project=project, sequence=2, title="Child", goal="Create feature", parent=parent, subtask_position=0, status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1)
        plan = PlanRevision(task=child, revision=1, brief_markdown="Shared architecture", plan_markdown="Plan", metadata_json={"implementation_tasks": [package], "validation_commands": ["test -f feature.txt"]})
        session.add_all([project, parent, child, plan])
        await session.commit()

    class Provider:
        calls = 0

        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            self.calls += 1
            if self.calls == 1:
                Path(cwd, "feature.txt").write_text("partial\n" * 20_000)
                for _ in range(3):
                    await on_event({"type": "tool_execution_start", "toolName": "write", "args": {"path": "feature.txt"}})
                raise AssertionError("the third tool must be stopped")
            assert "Shared architecture" in instruction
            assert len(instruction) < 18_000
            return AgentResult(session_id, "done", (), 0)

    pipeline = ImplementationPipeline(factory, Provider(), tmp_path / "artifacts")
    assert await pipeline.run("TST-2") == "validated"
    async with factory() as session:
        attempts = (await session.scalars(select(Attempt).where(Attempt.task_id == "TST-2").order_by(Attempt.number))).all()
        assert [attempt.outcome for attempt in attempts] == ["budget_exceeded", "verified"]
        assert await session.scalar(select(Event).where(Event.task_id == "TST-2", Event.type == "context_pack.replan_required")) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_subtask_retries_design_only_response_without_repository_changes(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    engine, factory = await empty_orchestration_store()
    package = {
        "id": "wp-1", "title": "Create feature", "position": 0,
        "objective": "Create feature.txt", "files": [{"path": "feature.txt", "mode": "create", "reason": "Output"}],
        "interfaces": ["feature.txt exists"], "changes": {"feature.txt": "Write done"},
        "constraints": [], "verification": {"commands": ["test -f feature.txt"], "success": "Exit zero"},
        "done_when": ["File exists"], "budget": {"max_tool_calls": 20},
    }
    async with factory() as session:
        await add_managed_profiles(session, "implementation", "escalation")
        project = Project(name="Test", key="TST", repository_path=str(repository))
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Build", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING)
        child = Task(id="TST-2", project=project, sequence=2, title="Child", goal="Create feature", parent=parent, subtask_position=0, status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1)
        plan = PlanRevision(task=child, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"implementation_tasks": [package], "validation_commands": ["test -f feature.txt"]})
        session.add_all([project, parent, child, plan])
        await session.commit()

    class Provider:
        calls = 0

        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            self.calls += 1
            if self.calls == 1:
                return AgentResult(session_id, "Ready for approval.", (), 0)
            Path(cwd, "feature.txt").write_text("done\n")
            return AgentResult(session_id, "Implemented.", (), 0)

    provider = Provider()
    pipeline = ImplementationPipeline(factory, provider, tmp_path / "artifacts")
    assert await pipeline.run("TST-2") == "validated"
    assert provider.calls == 2
    async with factory() as session:
        attempts = (await session.scalars(select(Attempt).where(Attempt.task_id == "TST-2").order_by(Attempt.number))).all()
        assert [attempt.outcome for attempt in attempts] == ["no_progress", "verified"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_subtask_retry_receives_validation_failure_output(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    checker = repository / "check.sh"
    checker.write_text("#!/bin/sh\ngrep -qx good feature.txt || { echo EXPECTED_GOOD_CONTENT; exit 1; }\n")
    checker.chmod(0o755)
    git(repository, "add", "check.sh")
    git(repository, "commit", "-m", "add checker")
    git(repository, "branch", "-f", "carlo-Dev", "HEAD")
    engine, factory = await empty_orchestration_store()
    package = {
        "id": "wp-1", "title": "Create feature", "position": 0,
        "objective": "Create feature.txt", "files": [{"path": "feature.txt", "mode": "create", "reason": "Output"}],
        "interfaces": ["feature.txt contains good"], "changes": {"feature.txt": "Write good"},
        "constraints": [], "verification": {"commands": ["./check.sh"], "success": "Exit zero"},
        "done_when": ["Checker passes"], "budget": {"max_tool_calls": 20},
    }
    async with factory() as session:
        await add_managed_profiles(session, "implementation", "escalation")
        project = Project(name="Test", key="TST", repository_path=str(repository))
        parent = Task(id="TST-1", project=project, sequence=1, title="Parent", goal="Build", status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING)
        child = Task(id="TST-2", project=project, sequence=2, title="Child", goal="Create feature", parent=parent, subtask_position=0, status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING, approved_plan_revision=1)
        session.add_all([project, parent, child, PlanRevision(task=child, revision=1, brief_markdown="Brief", plan_markdown="Plan", metadata_json={"implementation_tasks": [package]})])
        await session.commit()

    class Provider:
        calls = 0

        async def run(self, profile, instruction, cwd, session_id, on_event=None):
            self.calls += 1
            if self.calls == 1:
                Path(cwd, "feature.txt").write_text("bad\n")
            else:
                assert "EXPECTED_GOOD_CONTENT" in instruction
                Path(cwd, "feature.txt").write_text("good\n")
            return AgentResult(session_id, "Implemented.", (), 0)

    provider = Provider()
    assert await ImplementationPipeline(factory, provider, tmp_path / "artifacts").run("TST-2") == "validated"
    assert provider.calls == 2
    await engine.dispose()


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


def test_child_implementation_instruction_contains_prompt_once() -> None:
    prompt = "Implement only the import workflow."
    task = Task(id="CAR-2", goal=prompt, parent_task_id="CAR-1")
    plan = PlanRevision(
        plan_markdown=prompt,
        metadata_json={"implementation_tasks": [{"title": "Import", "prompt": prompt}]},
    )

    instruction = ImplementationPipeline._implementation_instruction(task, plan, "approved_plan")

    assert instruction.count(prompt) == 1


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
                Path(cwd, "feature.txt").write_text("done" if recovery_succeeds else "partial")
                raise ContextLimitError("Prompt too long")
            if not recovery_succeeds:
                raise ContextLimitError("Still too long")
            assert Path(cwd, "feature.txt").read_text() == "done"
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
            metadata_json={
                "skills": [], "validation_commands": ["test -f feature.txt"],
                "implementation_tasks": [{
                    "id": "wp-1", "title": "Child Feature", "position": 0,
                    "objective": "Create feature.txt",
                    "files": [{"path": "feature.txt", "mode": "create", "reason": "Requested output"}],
                    "interfaces": ["feature.txt exists after implementation"],
                    "changes": {"feature.txt": "Write fresh content"},
                    "constraints": [],
                    "verification": {"commands": ["test -f feature.txt"], "success": "File exists"},
                    "done_when": ["File is present"], "budget": {"max_tool_calls": 20},
                }],
            },
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
            assert instruction.count("Create feature.txt") == 1
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
    assert provider.implementation_skills == ("carlo-runtime", "carlo-ui-design")
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


@pytest.mark.asyncio
async def test_failed_root_task_blocks_a_later_root_task_in_the_same_project() -> None:
    engine, factory = await empty_orchestration_store()
    async with factory() as session:
        project = Project(name="CARLO", key="CAR", repository_path="/tmp/unused")
        first = Task(
            id="CAR-1",
            project=project,
            sequence=1,
            title="First",
            goal="First",
            status=TaskStatus.FAILED,
            stage=TaskStage.BLOCKED,
        )
        second = Task(
            id="CAR-2",
            project=project,
            sequence=2,
            title="Second",
            goal="Second",
            status=TaskStatus.READY,
            stage=TaskStage.QUEUED,
        )
        session.add_all([project, first, second])
        await session.commit()

    async def unused_runner(task_id: str) -> str:
        raise AssertionError(task_id)

    orchestrator = Orchestrator(engine, factory, unused_runner)
    assert await orchestrator._claim_next() is None

    async with factory() as session:
        first_task = await session.get(Task, "CAR-1")
        first_task.status, first_task.stage = TaskStatus.DONE, TaskStage.COMPLETE
        await session.commit()

    assert await orchestrator._claim_next() == "CAR-2"
    await engine.dispose()


@pytest.mark.asyncio
async def test_unresolved_earlier_root_blocks_a_later_roots_subtask_from_dispatch() -> None:
    # Mirrors real production shape: every approved task materializes at least
    # one subtask, and the parent flips to IN_PROGRESS/IMPLEMENTING immediately
    # at approval time — so the gate that matters lives on subtask eligibility,
    # not on the (always-has-children) root branch.
    engine, factory = await empty_orchestration_store()
    async with factory() as session:
        project = Project(name="CARLO", key="CAR", repository_path="/tmp/unused")
        first_root = Task(
            id="CAR-1",
            project=project,
            sequence=1,
            title="First goal",
            goal="First goal",
            status=TaskStatus.FAILED,
            stage=TaskStage.BLOCKED,
        )
        first_subtask = Task(
            id="CAR-2",
            project=project,
            sequence=2,
            parent_task_id="CAR-1",
            subtask_position=0,
            title="First goal step",
            goal="First goal step",
            status=TaskStatus.FAILED,
            stage=TaskStage.BLOCKED,
        )
        second_root = Task(
            id="CAR-3",
            project=project,
            sequence=3,
            title="Second goal",
            goal="Second goal",
            status=TaskStatus.IN_PROGRESS,
            stage=TaskStage.IMPLEMENTING,
        )
        second_subtask = Task(
            id="CAR-4",
            project=project,
            sequence=4,
            parent_task_id="CAR-3",
            subtask_position=0,
            title="Second goal step",
            goal="Second goal step",
            status=TaskStatus.READY,
            stage=TaskStage.QUEUED,
        )
        session.add_all([project, first_root, first_subtask, second_root, second_subtask])
        await session.commit()

    async def unused_runner(task_id: str) -> str:
        raise AssertionError(task_id)

    orchestrator = Orchestrator(engine, factory, unused_runner)
    assert await orchestrator._claim_next() is None

    async with factory() as session:
        first_root_task = await session.get(Task, "CAR-1")
        first_subtask_task = await session.get(Task, "CAR-2")
        first_root_task.status, first_root_task.stage = TaskStatus.DONE, TaskStage.COMPLETE
        first_subtask_task.status, first_subtask_task.stage = TaskStatus.DONE, TaskStage.COMPLETE
        await session.commit()

    assert await orchestrator._claim_next() == "CAR-4"
    await engine.dispose()


@pytest.mark.asyncio
async def test_root_task_serialization_does_not_cross_projects() -> None:
    engine, factory = await empty_orchestration_store()
    async with factory() as session:
        blocked_project = Project(name="Blocked", key="BLK", repository_path="/tmp/unused1")
        other_project = Project(name="Other", key="OTH", repository_path="/tmp/unused2")
        failed = Task(
            id="BLK-1",
            project=blocked_project,
            sequence=1,
            title="Failed",
            goal="Failed",
            status=TaskStatus.FAILED,
            stage=TaskStage.BLOCKED,
        )
        other = Task(
            id="OTH-1",
            project=other_project,
            sequence=1,
            title="Other",
            goal="Other",
            status=TaskStatus.READY,
            stage=TaskStage.QUEUED,
        )
        session.add_all([blocked_project, other_project, failed, other])
        await session.commit()

    async def unused_runner(task_id: str) -> str:
        raise AssertionError(task_id)

    assert await Orchestrator(engine, factory, unused_runner)._claim_next() == "OTH-1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_depends_on_blocks_until_the_dependency_is_done() -> None:
    engine, factory = await empty_orchestration_store()
    async with factory() as session:
        upstream_project = Project(name="Upstream", key="UPS", repository_path="/tmp/unused1")
        downstream_project = Project(name="Downstream", key="DWN", repository_path="/tmp/unused2")
        dependency = Task(
            id="UPS-1",
            project=upstream_project,
            sequence=1,
            title="Dependency",
            goal="Dependency",
            status=TaskStatus.READY,
            stage=TaskStage.QUEUED,
        )
        dependent = Task(
            id="DWN-1",
            project=downstream_project,
            sequence=1,
            title="Dependent",
            goal="Dependent",
            status=TaskStatus.READY,
            stage=TaskStage.QUEUED,
            depends_on_task_ids=["UPS-1"],
        )
        session.add_all([upstream_project, downstream_project, dependency, dependent])
        await session.commit()

    async with factory() as session:
        dependent_task = await session.get(Task, "DWN-1")
        assert await Orchestrator._eligible(session, dependent_task) is False

        dependency_task = await session.get(Task, "UPS-1")
        dependency_task.status, dependency_task.stage = TaskStatus.DONE, TaskStage.COMPLETE
        await session.commit()

        dependent_task = await session.get(Task, "DWN-1")
        assert await Orchestrator._eligible(session, dependent_task) is True
    await engine.dispose()
