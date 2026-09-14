import json
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.api import create_app
from carlo.config import Settings
from carlo.models import AgentProfile, Base, Discovery, Task
from tests.fakes import FakeProvider


@pytest.mark.asyncio
async def test_discovery_chat_task_handoff_and_close(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("TRUNCATE projects, users, agent_profiles RESTART IDENTITY CASCADE"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    provider = FakeProvider(json.dumps({"question": "Which error format should the API use?"}))
    app = create_app(factory, provider, Settings(app_origin="http://test", artifact_root=str(tmp_path / "artifacts")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        client.headers["Origin"] = "http://test"
        project = (await client.post("/api/projects", json={"name": "Repo", "key": "REP", "repository_path": str(repository)})).json()
        created = await client.post("/api/discoveries", json={"project_id": project["id"], "title": "Explore imports", "message": "How should imports work?"})
        assert created.status_code == 201
        discovery = created.json()
        assert discovery["status"] == "OPEN"
        assert discovery["messages"][0]["content"] == "How should imports work?"
        assert discovery["current_turn"]["status"] == "QUEUED"
        async with factory() as session:
            record = await session.get(Discovery, discovery["id"])
            profile = await session.get(AgentProfile, record.profile_id)
            assert profile.name == "plan"

        reply = await client.post(f'/api/discoveries/{discovery["id"]}/messages', json={"content": "Focus on CSV first"})
        assert reply.status_code == 202

        attached = await client.post(
            f'/api/discoveries/{discovery["id"]}/messages/attachments',
            data={"content": "Inspect these files"},
            files=[
                ("files", ("sample.json", b'{"ok": true}', "application/json")),
                ("files", ("preview.png", b"png", "image/png")),
            ],
        )
        assert attached.status_code == 202
        attachments = attached.json()["messages"][-1]["metadata"]["attachments"]
        assert [item["name"] for item in attachments] == ["sample.json", "preview.png"]
        for item in attachments:
            path = Path(item["path"])
            assert path.is_file()
            assert path.parent == tmp_path / "artifacts" / "discoveries" / str(discovery["id"]) / "attachments"
            served = await client.get(
                f'/api/discoveries/{discovery["id"]}/attachments/{item["stored_name"]}'
            )
            assert served.status_code == 200

        rejected_attachment = await client.post(
            f'/api/discoveries/{discovery["id"]}/messages/attachments',
            data={"content": "No"},
            files={"files": ("notes.txt", b"no", "text/plain")},
        )
        assert rejected_attachment.status_code == 422

        async with factory() as session:
            record = await session.get(Discovery, discovery["id"])
            record.state = {
                "task_proposals": [
                    {
                        "id": "csv",
                        "title": "Add CSV import",
                        "megaprompt": "Implement CSV import.",
                        "depends_on": [],
                        "brief_markdown": "# Brief\nReuse ingestion.",
                        "plan_markdown": "# Plan\nImplement CSV parsing.",
                        "metadata": {"skills": [], "implementation_tasks": [], "implementation_phases": [], "validation_commands": ["pytest -q"], "browser_validation": False, "build_required": False, "run_required": False, "deployment_expected": False, "risk_flags": [], "affected_areas": ["src/ingest.py"]},
                    }
                ]
            }
            await session.commit()
        unplanned = await client.post(
            f'/api/discoveries/{discovery["id"]}/tasks', json={"proposal_ids": []}
        )
        assert unplanned.status_code == 409

        async with factory() as session:
            record = await session.get(Discovery, discovery["id"])
            record.state = {
                "summary": "CSV import should reuse the existing ingestion service.",
                "findings": ["src/ingest.py owns ingestion"],
                "decisions": ["CSV first"],
                "unresolved_questions": [],
                "inspected_resources": ["src/ingest.py"],
                "commands": ["pytest -q: passed"],
                "task_proposals": [
                    {
                        "id": "csv", "title": "Add CSV import",
                        "megaprompt": "Implement CSV import using src/ingest.py.",
                        "depends_on": [], "brief_markdown": "# Brief\nReuse ingestion.",
                        "plan_markdown": "# Plan\nImplement CSV parsing, then test it.",
                        "metadata": {"skills": [], "implementation_phases": ["Implement parsing", "Validate imports"], "validation_commands": ["pytest -q"], "browser_validation": False, "build_required": False, "run_required": False, "deployment_expected": False, "risk_flags": [], "affected_areas": ["src/ingest.py"]},
                    },
                    {
                        "id": "ui", "title": "Add import UI",
                        "megaprompt": "Add the CSV import UI after the backend.",
                        "depends_on": ["csv"], "brief_markdown": "# Brief\nUse the existing UI.",
                        "plan_markdown": "# Plan\nAdd and browser-test the import flow.",
                        "metadata": {"skills": [], "implementation_tasks": [
                            {"title": "Add import UI", "prompt": "Add and browser-test the CSV import UI.", "intervention_points": ["frontend"]},
                        ], "implementation_phases": ["Add import UI"], "validation_commands": ["npm test"], "browser_validation": True, "build_required": True, "run_required": True, "deployment_expected": False, "risk_flags": [], "affected_areas": ["frontend"]},
                    },
                ],
            }
            await session.commit()

        tasks = await client.post(f'/api/discoveries/{discovery["id"]}/tasks', json={"proposal_ids": []})
        assert tasks.status_code == 201
        assert [task["id"] for task in tasks.json()] == ["REP-1", "REP-4"]
        assert [task["status"] for task in tasks.json()] == ["IN_PROGRESS", "IN_PROGRESS"]
        assert [task["stage"] for task in tasks.json()] == ["implementing", "implementing"]
        assert tasks.json()[0]["approved_plan_revision"] == 1
        assert tasks.json()[0]["plan"]["plan_markdown"].startswith("# Plan")
        assert tasks.json()[0]["plan"]["approved_at"] is not None
        async with factory() as session:
            children = (
                await session.scalars(
                    select(Task)
                    .where(Task.parent_task_id.in_(["REP-1", "REP-4"]))
                    .order_by(Task.sequence)
                )
            ).all()
            assert [
                (child.id, child.parent_task_id, child.subtask_position, child.title)
                for child in children
            ] == [
                ("REP-2", "REP-1", 0, "Implement parsing"),
                ("REP-3", "REP-1", 1, "Validate imports"),
                ("REP-5", "REP-4", 0, "Add import UI"),
            ]
        detail = (await client.get(f'/api/discoveries/{discovery["id"]}')).json()
        assert detail["status"] == "OPEN"
        assert [proposal["created_task_id"] for proposal in detail["state"]["task_proposals"]] == ["REP-1", "REP-4"]

        closed = await client.post(f'/api/discoveries/{discovery["id"]}/close')
        assert closed.status_code == 200
        assert closed.json()["status"] == "CLOSED"
        assert closed.json()["final_summary"]
        rejected = await client.post(f'/api/discoveries/{discovery["id"]}/messages', json={"content": "More"})
        assert rejected.status_code == 409

    await engine.dispose()
