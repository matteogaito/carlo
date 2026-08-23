import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.api import create_app
from carlo.config import Settings
from carlo.models import AgentProfile as AgentProfileRecord
from carlo.domain import TaskStage, TaskStatus
from carlo.models import Base, Event, PlanRevision, Project, Task, ValidationRun
from carlo.provider import AgentProfile, AgentResult
from tests.fakes import FakeProvider


def planning_output(plan: str = "# Plan\nRun the existing tests.") -> str:
    return json.dumps(
        {
            "brief_markdown": "# Brief\nEvidence: README.md",
            "plan_markdown": plan,
            "metadata": {
                "skills": ["testing"],
                "validation_commands": ["pytest -q"],
                "browser_validation": False,
                "build_required": False,
                "run_required": False,
                "deployment_expected": False,
                "risk_flags": [],
                "affected_areas": ["backend"],
            },
        }
    )


@pytest.mark.asyncio
async def test_task_stays_not_ready_until_plan_is_approved(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "carlo-Dev", str(repository)],
        check=True,
        capture_output=True,
    )
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE notification_deliveries, notification_cursors, login_failures, "
                "user_sessions, project_memberships, users, events, validation_runs, "
                "escalations, attempts, plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    provider = FakeProvider(
        json.dumps(
            {
                "brief_markdown": "# Brief\nEvidence: README.md",
                "plan_markdown": "# Plan\nRun the existing tests.",
                "metadata": {
                    "skills": ["testing"],
                    "validation_commands": ["pytest -q"],
                    "browser_validation": False,
                    "build_required": False,
                    "run_required": False,
                    "deployment_expected": False,
                    "risk_flags": [],
                    "affected_areas": ["backend"],
                },
            }
        )
    )
    provider.events = (
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolName": "read",
            "args": {"path": "README.md"},
        },
        {
            "type": "tool_execution_end",
            "toolName": "read",
            "isError": False,
            "result": "do not persist",
        },
        {"type": "message_update", "delta": "{"},
        {"type": "message_update", "delta": '"brief_markdown"'},
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    app = create_app(factory, provider, Settings(app_origin="http://test"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        assert login.status_code == 200
        client.headers["Origin"] = "http://test"
        project_response = await client.post(
            "/api/projects",
            json={"name": "CARLO", "key": "CAR", "repository_path": str(repository)},
        )
        assert project_response.status_code == 201
        task_response = await client.post(
            "/api/tasks",
            json={
                "project_id": project_response.json()["id"],
                "title": "Login",
                "goal": "Add login",
            },
        )
        assert task_response.status_code == 201
        task = task_response.json()
        assert task["id"] == "CAR-1"
        assert task["status"] == "NOT_READY"
        assert task["prompt_path"].endswith("-CAR-1-login.md")
        prompt = repository / task["prompt_path"]
        assert prompt.parent == repository / "prompts"
        assert prompt.read_text() == "Add login"

        plan_response = await client.post(f'/api/tasks/{task["id"]}/plan')
        assert plan_response.status_code == 200
        planned = (await client.get(f'/api/tasks/{task["id"]}')).json()
        assert planned["stage"] == "awaiting_approval"
        assert planned["plan"]["metadata"]["validation_commands"] == ["pytest -q"]
        activity = [event for event in planned["events"] if event["type"].startswith("planning.")]
        assert sum(event["type"] == "planning.drafting" for event in activity) == 1
        assert any(
            event["type"] == "planning.tool.started"
            and event["payload"] == {"tool": "read", "detail": "README.md"}
            for event in activity
        )
        assert "do not persist" not in json.dumps(activity)

        approved = await client.post(
            f'/api/tasks/{task["id"]}/approve', json={"revision": 1, "version": planned["version"]}
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "READY"

        async with factory() as session:
            session.add(
                ValidationRun(
                    task_id=task["id"],
                    command="pytest -q",
                    exit_code=1,
                    classification="PARTIALLY_VERIFIED",
                    summary="2 failed",
                    failure_count=2,
                )
            )
            await session.commit()
        detail = (await client.get(f'/api/tasks/{task["id"]}')).json()
        assert detail["validations"][0]["failure_count"] == 2
        assert any(event["type"] == "plan.approved" for event in detail["events"])

        profiles = (await client.get("/api/agent-profiles")).json()
        assert any(profile["name"] == "plan" for profile in profiles)
        updated = await client.patch(
            "/api/agent-profiles/plan",
            json={"model": "openai/gpt-5", "effort": "high"},
        )
        assert updated.status_code == 200
        assert updated.json()["model"] == "openai/gpt-5"

    assert provider.calls[0][0].name == "plan"
    assert provider.calls[0][0].skills == ("carlo-planning",)
    assert provider.calls[0][1].startswith("/skill:carlo-planning ")
    assert provider.calls[0][2] == str(repository)
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_task_rework_replans_original_goal_and_preserves_history(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "carlo-Dev", str(repository)],
        check=True,
        capture_output=True,
    )
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE notification_deliveries, notification_cursors, login_failures, "
                "user_sessions, project_memberships, users, events, validation_runs, "
                "escalations, attempts, plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    provider = FakeProvider(planning_output("# Plan\nFresh implementation."))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    app = create_app(factory, provider, Settings(app_origin="http://test"))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"
        project = (
            await client.post(
                "/api/projects",
                json={
                    "name": "CARLO",
                    "key": "CAR",
                    "repository_path": str(repository),
                },
            )
        ).json()
        task = (
            await client.post(
                "/api/tasks",
                json={
                    "project_id": project["id"],
                    "title": "Login",
                    "goal": "Add login from the original request",
                },
            )
        ).json()
        planned = (await client.post(f'/api/tasks/{task["id"]}/plan')).json()
        await client.post(
            f'/api/tasks/{task["id"]}/approve',
            json={"revision": 1, "version": planned["version"]},
        )

        async with factory() as session:
            failed = await session.get(Task, task["id"])
            assert failed is not None
            failed.status = TaskStatus.FAILED
            failed.stage = TaskStage.BLOCKED
            failed.branch_name = "CAR-1-login"
            failed.worktree_path = "/tmp/CAR-1-login"
            failed.checkpoint_sha = "abc1234"
            session.add(
                Event(task=failed, type="execution.failed", payload={"outcome": "failed"})
            )
            await session.commit()

        response = await client.post(f'/api/tasks/{task["id"]}/rework')

        assert response.status_code == 200
        reworked = response.json()
        assert reworked["status"] == "NOT_READY"
        assert reworked["stage"] == "awaiting_approval"
        assert reworked["approved_plan_revision"] is None
        assert reworked["plan"]["revision"] == 2
        assert reworked["branch_name"] is None
        assert reworked["worktree_path"] is None
        assert reworked["checkpoint_sha"] is None
        detail = (await client.get(f'/api/tasks/{task["id"]}')).json()
        assert any(event["type"] == "execution.failed" for event in detail["events"])
        started = next(
            event for event in detail["events"] if event["type"] == "task.rework.started"
        )
        assert started["payload"]["previous_branch"] == "CAR-1-login"
        historical = await client.post(
            f'/api/tasks/{task["id"]}/approve',
            json={"revision": 1, "version": reworked["version"]},
        )
        assert historical.status_code == 409

        async with factory() as session:
            revisions = (
                await session.scalars(
                    select(PlanRevision)
                    .where(PlanRevision.task_id == task["id"])
                    .order_by(PlanRevision.revision)
                )
            ).all()
            assert [revision.revision for revision in revisions] == [1, 2]

    assert "Add login from the original request" in provider.calls[1][1]
    assert "fresh rework" in provider.calls[1][1].lower()
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_rework_planning_returns_task_to_retryable_failed_state(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "carlo-Dev", str(repository)],
        check=True,
        capture_output=True,
    )
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE notification_deliveries, notification_cursors, login_failures, "
                "user_sessions, project_memberships, users, events, validation_runs, "
                "escalations, attempts, plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    async with factory() as session:
        project = Project(
            name="CARLO", key="CAR", repository_path=str(repository)
        )
        session.add(
            Task(
                id="CAR-1",
                project=project,
                sequence=1,
                title="Login",
                goal="Add login",
                status=TaskStatus.FAILED,
                stage=TaskStage.BLOCKED,
            )
        )
        await session.commit()
    app = create_app(factory, FakeProvider("{}"), Settings(app_origin="http://test"))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"
        response = await client.post("/api/tasks/CAR-1/rework")
        assert response.status_code == 502
        task = (await client.get("/api/tasks/CAR-1")).json()
        assert task["status"] == "FAILED"
        assert task["stage"] == "blocked"
        assert any(
            event["type"] == "task.rework.planning_failed"
            for event in task["events"]
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_task_rejects_non_markdown_upload_without_creating_file(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "carlo-Dev", str(repository)],
        check=True,
        capture_output=True,
    )
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE notification_deliveries, notification_cursors, login_failures, "
                "user_sessions, project_memberships, users, events, validation_runs, "
                "escalations, attempts, plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    app = create_app(factory, FakeProvider("{}"), Settings(app_origin="http://test"))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"
        project = (
            await client.post(
                "/api/projects",
                json={"name": "CARLO", "key": "CAR", "repository_path": str(repository)},
            )
        ).json()
        response = await client.post(
            "/api/tasks",
            json={
                "project_id": project["id"],
                "title": "Bad prompt",
                "goal": "content",
                "prompt_filename": "prompt.txt",
            },
        )

    assert response.status_code == 422
    assert not (repository / "prompts").exists()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_planning_runs_concurrently_without_the_implementation_lock(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "carlo-Dev", str(repository)],
        check=True,
        capture_output=True,
    )
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE notification_deliveries, notification_cursors, login_failures, "
                "user_sessions, project_memberships, users, events, validation_runs, "
                "escalations, attempts, plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(AgentProfileRecord(name="plan", provider="pi"))
        await session.commit()
    await bootstrap_admin(factory, "admin", "admin-password")

    output = json.dumps(
        {
            "brief_markdown": "Brief",
            "plan_markdown": "Plan",
            "metadata": {
                "skills": [],
                "validation_commands": ["pytest -q"],
                "browser_validation": False,
                "build_required": False,
                "run_required": False,
                "deployment_expected": False,
                "risk_flags": [],
                "affected_areas": [],
            },
        }
    )

    class ConcurrentProvider:
        def __init__(self) -> None:
            self.entered = 0
            self.both_entered = asyncio.Event()
            self.release = asyncio.Event()

        async def run(
            self,
            profile: AgentProfile,
            instruction: str,
            cwd: str,
            session_id: str,
            on_event=None,
        ) -> AgentResult:
            self.entered += 1
            if self.entered == 2:
                self.both_entered.set()
            await self.release.wait()
            return AgentResult(session_id, output, (), 0)

        async def stop(self, session_id: str) -> None:
            return None

        def status(self, session_id: str) -> str:
            return "idle"

    provider = ConcurrentProvider()
    app = create_app(factory, provider, Settings(app_origin="http://test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        assert login.status_code == 200
        client.headers["Origin"] = "http://test"
        project = (
            await client.post(
                "/api/projects",
                json={"name": "CARLO", "key": "CAR", "repository_path": str(repository)},
            )
        ).json()
        tasks = [
            (
                await client.post(
                    "/api/tasks",
                    json={"project_id": project["id"], "title": title, "goal": title},
                )
            ).json()
            for title in ("One", "Two")
        ]
        planning = [
            asyncio.create_task(client.post(f'/api/tasks/{task["id"]}/plan'))
            for task in tasks
        ]
        await asyncio.wait_for(provider.both_entered.wait(), 2)
        provider.release.set()
        responses = await asyncio.gather(*planning)
        assert [response.status_code for response in responses] == [200, 200]
    await engine.dispose()
