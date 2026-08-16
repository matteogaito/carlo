import json
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.api import create_app
from carlo.models import Base, ValidationRun
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
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles RESTART IDENTITY CASCADE"
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
    app = create_app(factory, provider)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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

    assert provider.calls[0][0].name == "plan"
    assert provider.calls[0][2] == str(repository)
    await engine.dispose()
