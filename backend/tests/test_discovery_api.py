import json
from copy import deepcopy
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.api import create_app
from carlo.config import Settings
from carlo.domain import TaskStage, TaskStatus
from carlo.models import AgentProfile, Base, Discovery, DiscoveryTurn, PlanRevision, Project, Task
from carlo.planning import PlanPayload, proposal_source
from tests.fakes import FakeProvider


def package(title: str, path: str, position: int) -> dict:
    return {
        "id": f"wp-{position + 1}", "title": title, "position": position,
        "objective": title, "files": [{"path": path, "mode": "edit", "reason": "Owns this change"}],
        "interfaces": ["Preserve existing public behavior"], "changes": {path: title},
        "constraints": [], "verification": {"commands": ["pytest -q --tb=short"], "success": "Tests pass"},
        "done_when": ["The behavior is tested"], "budget": {"max_tool_calls": 20},
    }


def full_metadata(tasks: list[dict]) -> dict:
    return {
        "implementation_tasks": tasks, "skills": [], "implementation_phases": [item["title"] for item in tasks],
        "validation_commands": ["pytest -q"], "browser_validation": False, "build_required": False,
        "run_required": False, "deployment_expected": False, "risk_flags": [], "affected_areas": [],
    }


def reviewed_state(state: dict) -> dict:
    for proposal in state["task_proposals"]:
        proposal["plan_draft"] = PlanPayload.model_validate({
            "brief_markdown": proposal["brief_markdown"],
            "plan_markdown": proposal["plan_markdown"],
            "metadata": proposal["metadata"],
        }).model_dump()
        proposal["draft_source"] = proposal_source(proposal, state)
    return state


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
        assert (await client.delete(f'/api/discoveries/{discovery["id"]}')).status_code == 409
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
        assert "stale or pending" in unplanned.json()["detail"]

        async with factory() as session:
            record = await session.get(Discovery, discovery["id"])
            record.state = reviewed_state({
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
                        "metadata": {"skills": [], "implementation_tasks": [package("Implement parsing", "src/ingest.py", 0), package("Validate imports", "tests/test_ingest.py", 1)], "implementation_phases": ["Implement parsing", "Validate imports"], "validation_commands": ["pytest -q"], "browser_validation": False, "build_required": False, "run_required": False, "deployment_expected": False, "risk_flags": [], "affected_areas": ["src/ingest.py"]},
                    },
                    {
                        "id": "ui", "title": "Add import UI",
                        "megaprompt": "Add the CSV import UI after the backend.",
                        "depends_on": ["csv"], "brief_markdown": "# Brief\nUse the existing UI.",
                        "plan_markdown": "# Plan\nAdd and browser-test the import flow.",
                        "metadata": {"skills": [], "implementation_tasks": [
                            package("Add import UI", "frontend/import.ts", 0),
                        ], "implementation_phases": ["Add import UI"], "validation_commands": ["npm test"], "browser_validation": True, "build_required": True, "run_required": True, "deployment_expected": False, "risk_flags": [], "affected_areas": ["frontend"]},
                    },
                ],
            })
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
            csv_task = await session.get(Task, "REP-1")
            ui_task = await session.get(Task, "REP-4")
            assert csv_task.depends_on_task_ids == []
            assert ui_task.depends_on_task_ids == ["REP-1"]
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
        deleted = await client.delete(f'/api/discoveries/{discovery["id"]}')
        assert deleted.status_code == 204
        assert (await client.get(f'/api/discoveries/{discovery["id"]}')).status_code == 404
        assert (await client.get('/api/tasks/REP-1')).status_code == 200
        assert not (tmp_path / "artifacts" / "discoveries" / str(discovery["id"])).exists()


@pytest.mark.asyncio
async def test_discovery_task_creation_resolves_depends_on_by_proposal_title(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("TRUNCATE projects, users, agent_profiles RESTART IDENTITY CASCADE"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    provider = FakeProvider(json.dumps({"question": "x"}))
    app = create_app(factory, provider, Settings(app_origin="http://test", artifact_root=str(tmp_path / "artifacts")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        client.headers["Origin"] = "http://test"
        project = (await client.post("/api/projects", json={"name": "Repo", "key": "REP", "repository_path": str(repository)})).json()
        discovery = (await client.post("/api/discoveries", json={"project_id": project["id"], "title": "Explore", "message": "Go"})).json()

        async with factory() as session:
            record = await session.get(Discovery, discovery["id"])
            record.state = reviewed_state({
                "task_proposals": [
                    {
                        "id": "p1", "title": "Backend import",
                        "megaprompt": "Implement backend import.",
                        "depends_on": [], "brief_markdown": "# Brief", "plan_markdown": "# Plan",
                        "metadata": full_metadata([package("Implement parsing", "src/ingest.py", 0)]),
                    },
                    {
                        "id": "p2", "title": "Import UI",
                        "megaprompt": "Add the import UI.",
                        "depends_on": ["Backend import"],
                        "brief_markdown": "# Brief", "plan_markdown": "# Plan",
                        "metadata": full_metadata([package("Add UI", "frontend/import.ts", 0)]),
                    },
                ]
            })
            await session.commit()
            reviewed = deepcopy(record.state)

        missing_parent = await client.post(f'/api/discoveries/{discovery["id"]}/tasks', json={"proposal_ids": ["p2"]})
        assert missing_parent.status_code == 409
        async with factory() as session:
            record = await session.get(Discovery, discovery["id"])
            changed = deepcopy(reviewed)
            changed["task_proposals"][0]["megaprompt"] = "Changed after review"
            record.state = changed
            await session.commit()
        stale = await client.post(f'/api/discoveries/{discovery["id"]}/tasks', json={"proposal_ids": []})
        assert stale.status_code == 409
        async with factory() as session:
            record = await session.get(Discovery, discovery["id"])
            cycle = deepcopy(reviewed)
            cycle["task_proposals"][0]["depends_on"] = ["p2"]
            record.state = cycle
            await session.commit()
        assert (await client.post(f'/api/discoveries/{discovery["id"]}/tasks', json={"proposal_ids": []})).status_code == 409
        async with factory() as session:
            record = await session.get(Discovery, discovery["id"])
            record.state = reviewed
            await session.commit()

        first = await client.post(f'/api/discoveries/{discovery["id"]}/tasks', json={"proposal_ids": ["p1"]})
        assert first.status_code == 201
        first_id = first.json()[0]["id"]
        async with factory() as session:
            parent = await session.get(Task, first_id)
            parent.status, parent.stage = TaskStatus.READY, TaskStage.QUEUED
            await session.commit()
        assert (await client.delete(f'/api/tasks/{first_id}')).status_code == 204
        detail = (await client.get(f'/api/discoveries/{discovery["id"]}')).json()
        assert "created_task_id" not in detail["state"]["task_proposals"][0]
        assert (await client.post(f'/api/discoveries/{discovery["id"]}/tasks', json={"proposal_ids": ["p2"]})).status_code == 409

        tasks = await client.post(f'/api/discoveries/{discovery["id"]}/tasks', json={"proposal_ids": []})
        assert tasks.status_code == 201
        task_ids = [task["id"] for task in tasks.json()]
        retry = await client.post(f'/api/discoveries/{discovery["id"]}/tasks', json={"proposal_ids": []})
        assert [task["id"] for task in retry.json()] == task_ids
        async with factory() as session:
            backend_task = await session.get(Task, task_ids[0])
            ui_task = await session.get(Task, task_ids[1])
            assert backend_task.depends_on_task_ids == []
            assert ui_task.depends_on_task_ids == [backend_task.id]
            saved = await session.scalar(select(PlanRevision).where(PlanRevision.task_id == backend_task.id))
            assert saved.metadata_json == reviewed["task_proposals"][0]["plan_draft"]["metadata"]


@pytest.mark.asyncio
async def test_parent_task_delete_cascades_children_but_rejects_dependencies(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    prompt = repository / ".carlo" / "prompts" / "request.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Build parser")
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("TRUNCATE projects, users, agent_profiles RESTART IDENTITY CASCADE"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    async with factory() as session:
        project = Project(name="Repo", key="REP", repository_path=str(repository))
        parent = Task(id="REP-1", project=project, sequence=1, title="Parent", goal="Build", prompt_path=".carlo/prompts/request.md", status=TaskStatus.READY, stage=TaskStage.QUEUED)
        child = Task(id="REP-2", project=project, sequence=2, title="Child", goal="Parse", parent=parent, subtask_position=0, status=TaskStatus.READY, stage=TaskStage.QUEUED)
        dependent = Task(id="REP-3", project=project, sequence=3, title="Dependent", goal="Use parser", depends_on_task_ids=[parent.id], status=TaskStatus.READY, stage=TaskStage.QUEUED)
        session.add_all([project, parent, child, dependent])
        await session.commit()
    artifact = tmp_path / "artifacts" / "REP-1" / "attempt.log"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("log")
    app = create_app(factory, FakeProvider("{}"), Settings(app_origin="http://test", artifact_root=str(tmp_path / "artifacts")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        client.headers["Origin"] = "http://test"
        assert (await client.get("/api/tasks/REP-1")).json()["delete_allowed"] is False
        assert (await client.delete("/api/tasks/REP-1")).status_code == 409
        async with factory() as session:
            dependent = await session.get(Task, "REP-3")
            dependent.depends_on_task_ids = []
            await session.commit()
        assert (await client.get("/api/tasks/REP-1")).json()["delete_allowed"] is True
        assert (await client.delete("/api/tasks/REP-1")).status_code == 204
        assert (await client.get("/api/tasks/REP-1")).status_code == 404
    async with factory() as session:
        assert await session.get(Task, "REP-2") is None
        assert await session.get(Task, "REP-3") is not None
    assert not prompt.exists()
    assert not artifact.exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_rework_chat_applies_a_task_level_fix(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("TRUNCATE projects, users, agent_profiles RESTART IDENTITY CASCADE"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    provider = FakeProvider(json.dumps({}))
    app = create_app(factory, provider, Settings(app_origin="http://test", artifact_root=str(tmp_path / "artifacts")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        client.headers["Origin"] = "http://test"
        project_id = (await client.post("/api/projects", json={"name": "Repo", "key": "REP", "repository_path": str(repository)})).json()["id"]

        async with factory() as session:
            project = await session.get(Project, project_id)
            task = Task(
                id="REP-1", project=project, sequence=1, title="Fix archive",
                goal="Persist destinations", status=TaskStatus.FAILED, stage=TaskStage.BLOCKED,
                approved_plan_revision=1,
            )
            plan = PlanRevision(
                task=task, revision=1, brief_markdown="Brief", plan_markdown="Plan",
                metadata_json=full_metadata([package("Fix archive", "src/archive.py", 0)]),
            )
            session.add_all([task, plan])
            await session.commit()

        async with factory() as session:
            (await session.get(Task, "REP-1")).status = TaskStatus.READY
            await session.commit()
        not_failed = await client.post("/api/tasks/REP-1/rework-chat")
        assert not_failed.status_code == 409
        async with factory() as session:
            (await session.get(Task, "REP-1")).status = TaskStatus.FAILED
            await session.commit()

        opened = await client.post("/api/tasks/REP-1/rework-chat")
        assert opened.status_code == 201
        discovery_id = opened.json()["id"]

        reopened = await client.post("/api/tasks/REP-1/rework-chat")
        assert reopened.status_code == 201
        assert reopened.json()["id"] == discovery_id

        not_ready = await client.post(f"/api/discoveries/{discovery_id}/apply-fix")
        assert not_ready.status_code == 409

        async with factory() as session:
            record = await session.get(Discovery, discovery_id)
            record.state = {
                "fix_proposal": {
                    "action": "revise_task",
                    "summary": "Load before migrating.",
                    "brief_markdown": "# Brief\nLoad first.",
                    "plan_markdown": "# Plan\nLoad, then migrate.",
                    "package": package("Fix archive correctly", "src/archive.py", 0),
                    "packages": [],
                }
            }
            await session.commit()

        applied = await client.post(f"/api/discoveries/{discovery_id}/apply-fix")
        assert applied.status_code == 200
        body = applied.json()
        assert body["id"] == "REP-1"
        assert body["status"] == "READY"
        assert body["stage"] == "queued"
        assert body["approved_plan_revision"] == 2
        assert body["plan"]["plan_markdown"] == "# Plan\nLoad, then migrate."

        closed = await client.get(f"/api/discoveries/{discovery_id}")
        assert closed.json()["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_apply_fix_with_an_invalid_package_asks_pi_to_correct_it(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("TRUNCATE projects, users, agent_profiles RESTART IDENTITY CASCADE"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    provider = FakeProvider(json.dumps({}))
    app = create_app(factory, provider, Settings(app_origin="http://test", artifact_root=str(tmp_path / "artifacts")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        client.headers["Origin"] = "http://test"
        project_id = (await client.post("/api/projects", json={"name": "Repo", "key": "REP", "repository_path": str(repository)})).json()["id"]

        async with factory() as session:
            project = await session.get(Project, project_id)
            task = Task(
                id="REP-1", project=project, sequence=1, title="Fix archive",
                goal="Persist destinations", status=TaskStatus.FAILED, stage=TaskStage.BLOCKED,
                approved_plan_revision=1,
            )
            plan = PlanRevision(
                task=task, revision=1, brief_markdown="Brief", plan_markdown="Plan",
                metadata_json=full_metadata([package("Fix archive", "src/archive.py", 0)]),
            )
            session.add_all([task, plan])
            await session.commit()

        opened = await client.post("/api/tasks/REP-1/rework-chat")
        discovery_id = opened.json()["id"]

        # An escalation-shaped package with no interfaces — the same class of
        # mismatch a diagnosing model can produce when it invents a shape.
        broken_package = {**package("Fix archive correctly", "src/archive.py", 0), "interfaces": []}
        async with factory() as session:
            record = await session.get(Discovery, discovery_id)
            record.state = {
                "fix_proposal": {
                    "action": "revise_task",
                    "summary": "Load before migrating.",
                    "brief_markdown": "# Brief\nLoad first.",
                    "plan_markdown": "# Plan\nLoad, then migrate.",
                    "package": broken_package,
                    "packages": [],
                }
            }
            await session.commit()

        applied = await client.post(f"/api/discoveries/{discovery_id}/apply-fix")
        assert applied.status_code == 409
        assert "ho chiesto a Pi" in applied.json()["detail"]

        async with factory() as session:
            record = await session.get(Discovery, discovery_id)
            fix_request = record.messages[-1]
            assert fix_request.role == "system"
            assert "non è applicabile" in fix_request.content
            assert "richiama task_fix_proposal" in fix_request.content
            queued_turn = await session.scalar(
                select(DiscoveryTurn).where(DiscoveryTurn.input_message_id == fix_request.id)
            )
            assert queued_turn is not None
            assert queued_turn.status == "QUEUED"
            # the target task was left alone — no half-applied fix
            task = await session.get(Task, "REP-1")
            assert task.status == TaskStatus.FAILED
            assert task.approved_plan_revision == 1


@pytest.mark.asyncio
async def test_rework_chat_applies_a_parent_level_split(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("TRUNCATE projects, users, agent_profiles RESTART IDENTITY CASCADE"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    provider = FakeProvider(json.dumps({}))
    app = create_app(factory, provider, Settings(app_origin="http://test", artifact_root=str(tmp_path / "artifacts")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        client.headers["Origin"] = "http://test"
        project_id = (await client.post("/api/projects", json={"name": "Repo", "key": "REP", "repository_path": str(repository)})).json()["id"]

        async with factory() as session:
            project = await session.get(Project, project_id)
            project.next_task_sequence = 3
            original_package = package("Do everything", "src/archive.py", 0)
            parent = Task(
                id="REP-1", project=project, sequence=1, title="Archive goal",
                goal="Persist destinations", status=TaskStatus.FAILED, stage=TaskStage.BLOCKED,
                approved_plan_revision=1,
            )
            parent_plan = PlanRevision(
                task=parent, revision=1, brief_markdown="Brief", plan_markdown="Plan",
                metadata_json=full_metadata([original_package]),
            )
            session.add_all([parent, parent_plan])
            await session.commit()
            child = Task(
                id="REP-2", project=project, sequence=2, title="Do everything",
                goal="Persist destinations", parent_task_id="REP-1", subtask_position=0,
                status=TaskStatus.FAILED, stage=TaskStage.BLOCKED, approved_plan_revision=1,
            )
            child_plan = PlanRevision(
                task=child, revision=1, brief_markdown="Brief", plan_markdown="Plan",
                metadata_json=full_metadata([original_package]),
            )
            session.add_all([child, child_plan])
            await session.commit()

        opened = await client.post("/api/tasks/REP-2/rework-chat")
        assert opened.status_code == 201
        discovery_id = opened.json()["id"]

        async with factory() as session:
            record = await session.get(Discovery, discovery_id)
            record.state = {
                "fix_proposal": {
                    "action": "revise_parent",
                    "summary": "Split into two packages.",
                    "brief_markdown": "# Brief\nSplit the work.",
                    "plan_markdown": "# Plan\nTwo smaller packages.",
                    "package": None,
                    "packages": [
                        package("Persist records", "src/archive.py", 0),
                        package("Migrate legacy slot", "src/migration.py", 1),
                    ],
                }
            }
            await session.commit()

        applied = await client.post(f"/api/discoveries/{discovery_id}/apply-fix")
        assert applied.status_code == 200
        body = applied.json()
        assert body["id"] == "REP-1"
        assert body["status"] == "IN_PROGRESS"
        assert body["stage"] == "implementing"
        assert body["approved_plan_revision"] == 2

        async with factory() as session:
            old_child = await session.get(Task, "REP-2")
            assert old_child.superseded_at is not None
            new_children = (
                await session.scalars(
                    select(Task).where(Task.parent_task_id == "REP-1", Task.superseded_at.is_(None)).order_by(Task.subtask_position)
                )
            ).all()
            assert [child.title for child in new_children] == ["Persist records", "Migrate legacy slot"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_rework_chat_covers_a_blocked_escalation_with_no_amendment(tmp_path: Path) -> None:
    """A work-package escalation that returns action=blocked leaves the task
    IN_PROGRESS/BLOCKED with no PlanRevision to approve — rework-chat must still
    be reachable there, not only for FAILED tasks.
    """
    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("TRUNCATE projects, users, agent_profiles RESTART IDENTITY CASCADE"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    provider = FakeProvider(json.dumps({}))
    app = create_app(factory, provider, Settings(app_origin="http://test", artifact_root=str(tmp_path / "artifacts")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        client.headers["Origin"] = "http://test"
        project_id = (await client.post("/api/projects", json={"name": "Repo", "key": "REP", "repository_path": str(repository)})).json()["id"]

        async with factory() as session:
            project = await session.get(Project, project_id)
            task = Task(
                id="REP-1", project=project, sequence=1, title="Fix archive",
                goal="Persist destinations", status=TaskStatus.IN_PROGRESS, stage=TaskStage.BLOCKED,
                approved_plan_revision=1,
            )
            plan = PlanRevision(
                task=task, revision=1, brief_markdown="Brief", plan_markdown="Plan",
                metadata_json=full_metadata([package("Fix archive", "src/archive.py", 0)]),
            )
            session.add_all([task, plan])
            await session.commit()

        opened = await client.post("/api/tasks/REP-1/rework-chat")
        assert opened.status_code == 201
        discovery_id = opened.json()["id"]

        async with factory() as session:
            record = await session.get(Discovery, discovery_id)
            record.state = {
                "fix_proposal": {
                    "action": "revise_task",
                    "summary": "Revert the out-of-scope edits and retry within bounds.",
                    "brief_markdown": "# Brief\nStay in scope.",
                    "plan_markdown": "# Plan\nRevert, then reapply narrowly.",
                    "package": package("Fix archive within scope", "src/archive.py", 0),
                    "packages": [],
                }
            }
            await session.commit()

        applied = await client.post(f"/api/discoveries/{discovery_id}/apply-fix")
        assert applied.status_code == 200
        body = applied.json()
        assert body["status"] == "IN_PROGRESS"
        assert body["stage"] == "implementing"
        assert body["approved_plan_revision"] == 2

    await engine.dispose()
