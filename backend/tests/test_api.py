import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.api import create_app
from carlo.config import Settings
from carlo.models import AgentProfile as AgentProfileRecord
from carlo.models import Base, ValidationRun
from carlo.provider import AgentProfile, AgentResult
from tests.fakes import FakeProvider


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

        plan_response = await client.post(f'/api/tasks/{task["id"]}/plan')
        assert plan_response.status_code == 200
        planned = (await client.get(f'/api/tasks/{task["id"]}')).json()
        assert planned["stage"] == "awaiting_approval"
        assert planned["plan"]["metadata"]["validation_commands"] == ["pytest -q"]

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
    assert provider.calls[0][2] == str(repository)
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
