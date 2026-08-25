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
from carlo.models import Base
from carlo.orchestrator import ImplementationPipeline, Orchestrator
from carlo.provider import AgentProfile, AgentResult
from tests.fakes import add_managed_profiles


@pytest.mark.asyncio
async def test_goal_reaches_done_through_api_planning_worker_and_validation(
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
                "TRUNCATE notification_deliveries, notification_cursors, login_failures, "
                "user_sessions, project_memberships, users, events, validation_runs, "
                "escalations, attempts, plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await add_managed_profiles(session, "plan", "implementation", "escalation")
        await session.commit()
    await bootstrap_admin(factory, "admin", "admin-password")

    validation = (
        "python3 -c \"from pathlib import Path; "
        "assert Path('feature.txt').read_text() == 'done'\""
    )

    class Provider:
        async def run(
            self,
            profile: AgentProfile,
            instruction: str,
            cwd: str,
            session_id: str,
            on_event=None,
        ) -> AgentResult:
            if profile.name == "plan":
                output = json.dumps(
                    {
                        "brief_markdown": "# Brief\nREADME.md establishes the base.",
                        "plan_markdown": "# Plan\nCreate feature.txt and validate it.",
                        "metadata": {
                            "skills": ["carlo-ui-design"],
                            "validation_commands": [validation],
                            "browser_validation": False,
                            "build_required": False,
                            "run_required": False,
                            "deployment_expected": False,
                            "risk_flags": [],
                            "affected_areas": ["feature.txt"],
                        },
                    }
                )
            else:
                Path(cwd, "feature.txt").write_text("done")
                output = "implemented"
            return AgentResult(session_id, output, (), 0)

        async def stop(self, session_id: str) -> None:
            return None

        def status(self, session_id: str) -> str:
            return "idle"

    provider = Provider()
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
        task = (
            await client.post(
                "/api/tasks",
                json={"project_id": project["id"], "title": "Feature", "goal": "Create feature.txt"},
            )
        ).json()
        planned = (await client.post(f'/api/tasks/{task["id"]}/plan')).json()
        approved = await client.post(
            f'/api/tasks/{task["id"]}/approve',
            json={"revision": 1, "version": planned["version"]},
        )
        assert approved.json()["status"] == "READY"

        pipeline = ImplementationPipeline(
            factory, provider, tmp_path / "worktrees", tmp_path / "artifacts"
        )
        assert await Orchestrator(engine, factory, pipeline.run).run_next() == task["id"]
        detail = (await client.get(f'/api/tasks/{task["id"]}')).json()
        assert detail["status"] == "DONE"
        assert detail["branch_name"] == "CAR-1-feature"
        assert detail["checkpoint_sha"]
        assert detail["validations"][0]["classification"] == "VERIFIED"
        assert any(event["type"] == "execution.completed" for event in detail["events"])
    await engine.dispose()
