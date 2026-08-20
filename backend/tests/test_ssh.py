from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.api import create_app
from carlo.config import Settings
from carlo.models import Base, Runner
from carlo.ssh import HostScan, SshError, SshTransport, validate_runner
from tests.fakes import FakeProvider


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []

    async def __call__(
        self, arguments: tuple[str, ...], stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        self.calls.append((arguments, stdin))
        if arguments[0] == "ssh-keyscan":
            return 0, b"builder.example ssh-ed25519 AAAATEST\n", b""
        if arguments[0] == "ssh-keygen":
            return 0, b"256 SHA256:abc builder.example (ED25519)\n", b""
        return 0, b"connected\n", b""


def runner(identity: Path) -> Runner:
    return Runner(
        name="linux-build",
        host="builder.example",
        port=2222,
        username="deploy",
        identity_file=str(identity),
        workspace_root=".carlo",
        host_key="builder.example ssh-ed25519 AAAATEST",
        fingerprint="SHA256:abc",
        enabled=True,
    )


def test_runner_requires_restrictive_existing_identity(tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    identity.write_text("private")
    identity.chmod(0o644)
    with pytest.raises(SshError, match="permissions"):
        validate_runner(runner(identity))
    identity.chmod(0o600)
    validate_runner(runner(identity))
    identity.unlink()
    identity.symlink_to(tmp_path / "missing")
    with pytest.raises(SshError, match="regular file"):
        validate_runner(runner(identity))


@pytest.mark.asyncio
async def test_scan_confirm_and_check_use_strict_native_ssh(tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    identity.write_text("private")
    identity.chmod(0o600)
    known_hosts = tmp_path / "ssh" / "known_hosts"
    commands = FakeCommands()
    transport = SshTransport(known_hosts, command=commands, connect_timeout=7)

    scan = await transport.scan_host("builder.example", 2222)
    assert scan.fingerprint == "SHA256:abc"
    assert not known_hosts.exists()
    with pytest.raises(SshError, match="fingerprint"):
        await transport.confirm_host(scan, "SHA256:wrong")
    await transport.confirm_host(scan, "SHA256:abc")
    assert known_hosts.read_text() == "builder.example ssh-ed25519 AAAATEST\n"
    assert known_hosts.stat().st_mode & 0o777 == 0o600

    result = await transport.check(runner(identity))
    assert result == "connected"
    ssh = commands.calls[-1][0]
    for expected in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={known_hosts}",
        "ConnectTimeout=7",
        str(identity),
        "2222",
        "deploy@builder.example",
    ):
        assert expected in ssh


class FakeSsh:
    def __init__(self) -> None:
        self.confirmed: list[str] = []

    async def scan_host(self, host: str, port: int) -> HostScan:
        return HostScan(f"{host} ssh-ed25519 AAAATEST", "SHA256:abc", "ssh-ed25519")

    async def confirm_host(self, scan: HostScan, fingerprint: str) -> None:
        if fingerprint != scan.fingerprint:
            raise SshError("fingerprint confirmation does not match")
        self.confirmed.append(fingerprint)

    async def check(self, runner: Runner) -> str:
        return "connected"


@pytest.mark.asyncio
async def test_runner_api_requires_scan_trust_and_connection_test(tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    identity.write_text("private")
    identity.chmod(0o600)
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
    fake_ssh = FakeSsh()
    app = create_app(
        factory,
        FakeProvider("{}"),
        Settings(app_origin="http://test"),
        ssh_transport=fake_ssh,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        client.headers["Origin"] = "http://test"
        created = await client.post(
            "/api/runners",
            json={
                "name": "linux-build",
                "host": "builder.example",
                "port": 2222,
                "username": "deploy",
                "identity_file": str(identity),
                "workspace_root": ".carlo",
            },
        )
        assert created.status_code == 201
        pending = created.json()
        assert pending["enabled"] is False
        assert pending["pending_fingerprint"] == "SHA256:abc"
        rejected = await client.post(
            f"/api/runners/{pending['id']}/trust", json={"fingerprint": "SHA256:wrong"}
        )
        assert rejected.status_code == 409
        trusted = await client.post(
            f"/api/runners/{pending['id']}/trust", json={"fingerprint": "SHA256:abc"}
        )
        assert trusted.status_code == 200
        assert trusted.json()["enabled"] is True
        assert trusted.json()["fingerprint"] == "SHA256:abc"
        assert fake_ssh.confirmed == ["SHA256:abc"]
        listed = (await client.get("/api/runners")).json()
        assert [item["name"] for item in listed] == ["local", "linux-build"]
        disabled = await client.patch(
            f"/api/runners/{pending['id']}", json={"enabled": False}
        )
        assert disabled.json()["enabled"] is False
    await engine.dispose()
