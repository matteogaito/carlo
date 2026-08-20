import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.api import create_app
from carlo.actions import (
    ActionConfigError,
    load_catalog,
    minimal_environment,
    parse_catalog,
    parse_dotenv,
    preflight,
    redact,
)
from carlo.models import Project
from carlo.models import ActionRun, Base
from carlo.config import Settings
from tests.fakes import FakeProvider


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(tmp_path: Path, yaml_text: str) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "carlo-actions.yaml").write_text(yaml_text)
    (path / ".gitignore").write_text(".env*\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "actions")
    return path


def test_catalog_is_strict_and_parses_commands_without_a_shell() -> None:
    actions = parse_catalog(
        """
version: 1
actions:
  test:
    name: Run tests
    commands:
      - python -m pytest -q
      - echo left && echo right
"""
    )

    assert actions["test"].runner == "local"
    assert actions["test"].argv == (
        ("python", "-m", "pytest", "-q"),
        ("echo", "left", "&&", "echo", "right"),
    )

    with pytest.raises(ActionConfigError, match="unsupported version"):
        parse_catalog("version: 2\nactions: {}\n")
    with pytest.raises(ActionConfigError, match="unknown field"):
        parse_catalog("version: 1\nactions: {test: {name: Test, commands: [true], retries: 2}}\n")
    with pytest.raises(ActionConfigError, match="non-empty commands"):
        parse_catalog("version: 1\nactions: {test: {name: Test, commands: []}}\n")


def test_dotenv_is_validated_and_secrets_are_redacted(monkeypatch) -> None:
    values = parse_dotenv(
        "# comment\nexport TOKEN='top secret'\nEMPTY=\nQUOTED=\"hello world\"\n"
    )
    assert values == {"TOKEN": "top secret", "EMPTY": "", "QUOTED": "hello world"}
    with pytest.raises(ActionConfigError, match="line 1"):
        parse_dotenv("NOT-VALID=value")

    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("CARLO_DATABASE_URL", "do-not-leak")
    environment = minimal_environment(values)
    assert environment["PATH"] == "/bin"
    assert environment["TOKEN"] == "top secret"
    assert "CARLO_DATABASE_URL" not in environment
    assert redact("top secret and hello world", values.values()) == "*** and ***"


@pytest.mark.asyncio
async def test_catalog_comes_from_head_and_preflight_rejects_dirty_source(
    tmp_path: Path,
) -> None:
    source = repository(
        tmp_path,
        "version: 1\nactions: {test: {name: Run tests, commands: ['python check.py']}}\n",
    )
    project = Project(name="Test", key="TST", repository_path=str(source))

    catalog = await load_catalog(project)
    assert catalog.commit_sha == git(source, "rev-parse", "HEAD")
    assert catalog.actions["test"].commands == ("python check.py",)

    (source / "carlo-actions.yaml").write_text(
        "version: 1\nactions: {changed: {name: Changed, commands: ['false']}}\n"
    )
    catalog = await load_catalog(project)
    assert "test" in catalog.actions
    assert "changed" not in catalog.actions
    assert "carlo-actions.yaml" in catalog.dirty_paths
    with pytest.raises(ActionConfigError, match="repository has pending changes"):
        await preflight(project, "test")


@pytest.mark.asyncio
async def test_preflight_allows_ignored_env_and_rejects_path_escape(tmp_path: Path) -> None:
    source = repository(
        tmp_path,
        "version: 1\nactions:\n  deploy:\n    name: Deploy\n    env_file: .env.dev\n    commands: ['python deploy.py']\n",
    )
    (source / ".env.dev").write_text("TOKEN=secret\n")
    project = Project(name="Test", key="TST", repository_path=str(source))

    result = await preflight(project, "deploy")
    assert result.env_values == {"TOKEN": "secret"}
    assert result.env_path == source / ".env.dev"

    git(source, "show", "HEAD:carlo-actions.yaml")
    outside = tmp_path / "outside.env"
    outside.write_text("TOKEN=secret")
    (source / ".env.dev").unlink()
    (source / ".env.dev").symlink_to(outside)
    with pytest.raises(ActionConfigError, match="inside the project"):
        await preflight(project, "deploy")


@pytest.mark.asyncio
async def test_preflight_rejects_credential_origin_for_ssh(tmp_path: Path) -> None:
    source = repository(
        tmp_path,
        "version: 1\nactions:\n  deploy:\n    name: Deploy\n    runner: linux\n    commands: ['true']\n",
    )
    git(source, "remote", "add", "origin", "https://token@example.invalid/repo.git")

    with pytest.raises(ActionConfigError, match="embedded credentials"):
        await preflight(Project(name="Test", key="TST", repository_path=str(source)), "deploy")


@pytest.mark.asyncio
async def test_action_api_discovers_enqueues_reads_console_and_cancels(
    tmp_path: Path,
) -> None:
    source = repository(
        tmp_path,
        "version: 1\nactions:\n  test:\n    name: Run tests\n    env_file: .env.dev\n    commands: ['python check.py', 'python verify.py']\n",
    )
    (source / ".env.dev").write_text("TOKEN=super-secret\n")
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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    settings = Settings(app_origin="http://test", artifact_root=str(tmp_path / "artifacts"))
    app = create_app(factory, FakeProvider("{}"), settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})).status_code == 200
        client.headers["Origin"] = "http://test"
        project = (
            await client.post(
                "/api/projects",
                json={"name": "Test", "key": "TST", "repository_path": str(source)},
            )
        ).json()
        catalog = await client.get(f"/api/projects/{project['id']}/actions")
        assert catalog.status_code == 200
        assert catalog.json()["actions"][0]["key"] == "test"

        created = await client.post(f"/api/projects/{project['id']}/actions/test/runs")
        assert created.status_code == 201
        run = created.json()
        assert run["status"] == "queued"
        assert [step["status"] for step in run["steps"]] == ["pending", "pending"]
        assert "super-secret" not in str(run)
        secret_path = Path(run["secret_path"])
        assert secret_path.stat().st_mode & 0o777 == 0o600

        console_path = Path(run["artifact_path"])
        console_path.parent.mkdir(parents=True, exist_ok=True)
        console_path.write_text("0123456789")
        console = await client.get(f"/api/action-runs/{run['id']}/console?offset=4")
        assert console.json() == {
            "offset": 4,
            "next_offset": 10,
            "text": "456789",
            "eof": True,
        }

        cancelled = await client.post(f"/api/action-runs/{run['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert not secret_path.exists()
        repeated = await client.post(f"/api/action-runs/{run['id']}/cancel")
        assert repeated.status_code == 200
        history = await client.get(f"/api/action-runs?project_id={project['id']}&limit=10")
        assert [item["id"] for item in history.json()] == [run["id"]]

    async with factory() as session:
        stored = await session.scalar(select(ActionRun))
        assert stored is not None
        assert stored.env_names == ["TOKEN"]
        assert "super-secret" not in str(stored.definition)
        assert stored.secret_path is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_action_api_reports_catalog_error_and_blocks_dirty_run(tmp_path: Path) -> None:
    source = repository(
        tmp_path,
        "version: 1\nactions: {test: {name: Test, commands: ['true']}}\n",
    )
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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    app = create_app(factory, FakeProvider("{}"), Settings(app_origin="http://test", artifact_root=str(tmp_path / "artifacts")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        client.headers["Origin"] = "http://test"
        project = (
            await client.post("/api/projects", json={"name": "Test", "key": "TST", "repository_path": str(source)})
        ).json()
        (source / "pending.txt").write_text("dirty")
        catalog = (await client.get(f"/api/projects/{project['id']}/actions")).json()
        assert catalog["dirty_paths"] == ["pending.txt"]
        blocked = await client.post(f"/api/projects/{project['id']}/actions/test/runs")
        assert blocked.status_code == 409
        assert "pending.txt" in blocked.json()["detail"]

        git(source, "add", "pending.txt")
        git(source, "commit", "-m", "pending")
        (source / "carlo-actions.yaml").write_text("not: [valid")
        git(source, "add", "carlo-actions.yaml")
        git(source, "commit", "-m", "broken catalog")
        broken = (await client.get(f"/api/projects/{project['id']}/actions")).json()
        assert broken["actions"] == []
        assert "invalid YAML" in broken["error"]
    await engine.dispose()
