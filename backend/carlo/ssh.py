import asyncio
import os
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import Runner


class SshError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HostScan:
    host_key: str
    fingerprint: str
    key_type: str


Command = Callable[
    [tuple[str, ...], bytes | None], Awaitable[tuple[int, bytes, bytes]]
]
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9._-]{0,79}")


def validate_runner(runner: Runner) -> None:
    if not _HOST.fullmatch(runner.host) or not 1 <= runner.port <= 65535:
        raise SshError("invalid SSH host or port")
    if not _USER.fullmatch(runner.username):
        raise SshError("invalid SSH username")
    workspace = PurePosixPath(runner.workspace_root)
    if (
        not runner.workspace_root
        or workspace.is_absolute()
        or ".." in workspace.parts
        or any(part in {"", "."} for part in workspace.parts)
    ):
        raise SshError("workspace root must be relative to the remote home")
    identity = Path(runner.identity_file)
    if (
        not identity.is_absolute()
        or identity.is_symlink()
        or not identity.is_file()
        or not os.access(identity, os.R_OK)
    ):
        raise SshError("identity must be an absolute readable regular file")
    if identity.stat().st_mode & 0o077:
        raise SshError("identity file permissions must be 600 or stricter")


class SshTransport:
    def __init__(
        self,
        known_hosts: Path,
        *,
        command: Command | None = None,
        connect_timeout: int = 10,
    ) -> None:
        self.known_hosts = known_hosts.resolve()
        self.command = command or _command
        self.connect_timeout = connect_timeout

    async def scan_host(self, host: str, port: int) -> HostScan:
        if not _HOST.fullmatch(host) or not 1 <= port <= 65535:
            raise SshError("invalid SSH host or port")
        code, stdout, stderr = await self.command(
            ("ssh-keyscan", "-T", str(self.connect_timeout), "-p", str(port), host),
            None,
        )
        if code or not stdout.strip():
            raise SshError(stderr.decode(errors="replace").strip() or "host scan failed")
        line = stdout.decode(errors="replace").splitlines()[0].strip()
        parts = line.split()
        if len(parts) < 3:
            raise SshError("host scan returned an invalid key")
        code, fingerprint, stderr = await self.command(
            ("ssh-keygen", "-lf", "-"), (line + "\n").encode()
        )
        if code:
            raise SshError(stderr.decode(errors="replace").strip() or "fingerprint failed")
        fingerprint_parts = fingerprint.decode(errors="replace").split()
        if len(fingerprint_parts) < 2:
            raise SshError("ssh-keygen returned an invalid fingerprint")
        return HostScan(line, fingerprint_parts[1], parts[1])

    async def confirm_host(self, scan: HostScan, expected_fingerprint: str) -> None:
        if scan.fingerprint != expected_fingerprint:
            raise SshError("fingerprint confirmation does not match")
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self.known_hosts.parent.chmod(0o700)
        existing = self.known_hosts.read_text().splitlines() if self.known_hosts.exists() else []
        if scan.host_key not in existing:
            with self.known_hosts.open("a") as destination:
                destination.write(scan.host_key + "\n")
        self.known_hosts.chmod(0o600)

    async def check(self, runner: Runner) -> str:
        validate_runner(runner)
        code, stdout, stderr = await self.command(
            (*self.arguments(runner), "true"), None
        )
        if code:
            raise SshError(stderr.decode(errors="replace").strip() or "SSH check failed")
        return stdout.decode(errors="replace").strip()

    def arguments(self, runner: Runner) -> tuple[str, ...]:
        validate_runner(runner)
        return (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-i",
            runner.identity_file,
            "-p",
            str(runner.port),
            f"{runner.username}@{runner.host}",
        )

    async def execute(
        self, runner: Runner, script: str, *arguments: str
    ) -> tuple[int, bytes, bytes]:
        remote = shlex.join(("sh", "-c", script, "carlo", *arguments))
        return await self.command((*self.arguments(runner), remote), None)

    async def copy_file(self, runner: Runner, source: Path, destination: str) -> None:
        validate_runner(runner)
        command = (
            "scp",
            "-q",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-i",
            runner.identity_file,
            "-P",
            str(runner.port),
            str(source),
            f"{runner.username}@{runner.host}:{destination}",
        )
        code, _, stderr = await self.command(command, None)
        if code:
            raise SshError(stderr.decode(errors="replace").strip() or "SCP failed")


async def _command(
    arguments: tuple[str, ...], stdin: bytes | None = None
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(stdin)
    return process.returncode or 0, stdout, stderr
