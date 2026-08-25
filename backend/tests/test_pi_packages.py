import json
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.models import Base, Event, PiPackage


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
async def package_factory():
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("TRUNCATE agent_profile_packages, pi_packages, events RESTART IDENTITY CASCADE")
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.parametrize(
    ("source", "identity", "pinned"),
    [
        ("npm:ponytail", "npm:ponytail", False),
        ("npm:@scope/pippo", "npm:@scope/pippo", False),
        ("npm:@scope/pippo@1.2.3", "npm:@scope/pippo", True),
        ("git:github.com/owner/repo", "git:github.com/owner/repo", False),
        ("git:github.com/owner/repo@v1", "git:github.com/owner/repo", True),
    ],
)
def test_parse_package_source(source: str, identity: str, pinned: bool) -> None:
    from carlo.pi_packages import PackageSource, parse_package_source

    assert parse_package_source(source) == PackageSource(identity, pinned)


@pytest.mark.parametrize("source", ["", "../local", "/tmp/package", "file:local"])
def test_rejects_nonportable_package_sources(source: str) -> None:
    from carlo.pi_packages import PiPackageError, parse_package_source

    with pytest.raises(PiPackageError, match="npm or Git"):
        parse_package_source(source)


def test_locates_nested_git_checkout_root(tmp_path: Path) -> None:
    from carlo.pi_packages import PackageSource, PiPackageManager

    root = tmp_path / "agent/git/github.com/owner/package"
    (root / ".git").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"package"}')

    assert PiPackageManager._locate_package(
        tmp_path / "agent", PackageSource("git:github.com/owner/package", False)
    ) == root


@pytest.mark.asyncio
async def test_install_uses_pi_staging_and_promotes_manifest(tmp_path: Path) -> None:
    from carlo.pi_packages import PiPackageManager

    calls = tmp_path / "calls.json"
    pi = executable(
        tmp_path / "pi",
        f"""
python3 -c 'import json,os,pathlib,sys; root=pathlib.Path(os.environ["PI_CODING_AGENT_DIR"])/"npm/node_modules/pippo"; (root/"extensions").mkdir(parents=True); (root/"skills/pippo").mkdir(parents=True); (root/"extensions/index.js").write_text("export default () => {{}}") ; (root/"skills/pippo/SKILL.md").write_text("---\\nname: pippo\\ndescription: test\\n---\\n"); (root/"package.json").write_text(json.dumps({{"name":"pippo","version":"1.4.0","pi":{{"extensions":["./extensions/index.js"],"skills":["./skills"]}}}})); pathlib.Path("{calls}").write_text(json.dumps(sys.argv[1:]))' "$@"
""",
    )
    manager = PiPackageManager(pi, tmp_path / "artifacts", tmp_path / "pi.lock")

    installed = await manager.install("npm:pippo")

    assert json.loads(calls.read_text()) == ["install", "npm:pippo", "--no-approve"]
    assert installed.identity == "npm:pippo"
    assert installed.resolved_version == "1.4.0"
    assert Path(installed.artifact_path, "package.json").is_file()
    assert installed.resources["skills"] == ["pippo"]
    assert installed.resources["extensions"] == ["extensions/index.js"]


@pytest.mark.asyncio
async def test_failed_install_does_not_replace_promoted_artifact(tmp_path: Path) -> None:
    from carlo.pi_packages import PiPackageError, PiPackageManager

    pi = executable(
        tmp_path / "pi",
        """
python3 -c 'import json,os,pathlib; root=pathlib.Path(os.environ["PI_CODING_AGENT_DIR"])/"npm/node_modules/pippo"; root.mkdir(parents=True); (root/"package.json").write_text(json.dumps({"name":"pippo","version":"1.4.0","pi":{"skills":[]}}))'
""",
    )
    manager = PiPackageManager(pi, tmp_path / "artifacts", tmp_path / "pi.lock")
    installed = await manager.install("npm:pippo")
    old = Path(installed.artifact_path)
    executable(pi, 'echo "registry unavailable" >&2\nexit 4\n')

    with pytest.raises(PiPackageError, match="registry unavailable"):
        await manager.install("npm:pippo")

    assert old.is_dir()
    assert json.loads((old / "package.json").read_text())["version"] == "1.4.0"


@pytest.mark.asyncio
async def test_failed_refresh_retains_previous_package_and_backs_off(
    package_factory, tmp_path: Path
) -> None:
    from datetime import UTC, datetime, timedelta
    from carlo.maintenance import refresh_managed_pi_packages
    from carlo.pi_packages import PiPackageError

    old = tmp_path / "old"
    old.mkdir()
    async with package_factory() as session:
        session.add(
            PiPackage(
                source="npm:pippo",
                identity="npm:pippo",
                enabled=True,
                is_default=True,
                pinned=False,
                active_version="1.4.0",
                active_artifact_path=str(old),
            )
        )
        await session.commit()

    class FailingManager:
        calls = 0

        async def install(self, _source):
            self.calls += 1
            raise PiPackageError("registry unavailable")

    manager = FailingManager()
    now = datetime(2026, 8, 25, tzinfo=UTC)
    first = await refresh_managed_pi_packages(package_factory, manager, now)
    second = await refresh_managed_pi_packages(
        package_factory, manager, now + timedelta(minutes=5)
    )

    async with package_factory() as session:
        package = await session.scalar(select(PiPackage))
    assert package is not None
    assert package.active_version == "1.4.0"
    assert package.active_artifact_path == str(old)
    assert package.last_update_status == "FAILED"
    assert first.failed == ("npm:pippo",)
    assert second.unchanged == ("npm:pippo",)
    assert manager.calls == 1


@pytest.mark.asyncio
async def test_refresh_emits_one_aggregate_event(package_factory, tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from carlo.maintenance import refresh_managed_pi_packages
    from carlo.pi_packages import InstalledPiPackage

    frozen = tmp_path / "frozen"
    frozen.mkdir()
    async with package_factory() as session:
        session.add(
            PiPackage(
                source="npm:pippo",
                identity="npm:pippo",
                enabled=True,
                is_default=True,
                pinned=False,
            )
        )
        session.add(
            PiPackage(
                source="npm:frozen@2.0.0",
                identity="npm:frozen",
                enabled=True,
                is_default=True,
                pinned=True,
                active_version="2.0.0",
                active_artifact_path=str(frozen),
            )
        )
        await session.commit()

    artifact = tmp_path / "pippo"
    artifact.mkdir()

    class Manager:
        async def install(self, source):
            return InstalledPiPackage(
                "npm:pippo", source, "1.5.0", str(artifact), {"skills": ["pippo"]}
            )

    summary = await refresh_managed_pi_packages(
        package_factory, Manager(), datetime(2026, 8, 25, tzinfo=UTC)
    )
    async with package_factory() as session:
        events = list(await session.scalars(select(Event)))

    assert summary.updated == ("npm:pippo@1.5.0",)
    assert summary.unchanged == ("npm:frozen",)
    assert len(events) == 1
    assert events[0].payload["summary"] == "1 updated · 1 unchanged · 0 failed"
