import asyncio
import json
import logging
import os
import socket
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

import fcntl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import AgentProfilePackage, Event, PiPackage
from .model_providers import (
    CredentialCipher,
    DiscoveredModel,
    fetch_openai_models,
    refresh_due_model_providers,
)

logger = logging.getLogger("carlo.maintenance")
UPDATE_INTERVAL = timedelta(days=7)
FAILURE_RETRY_INTERVAL = timedelta(hours=1)
RESOURCE_NAME = re.compile(r"[a-z0-9-]{1,80}\Z")
GIT_REVISION = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True, slots=True)
class ManagedPiResource:
    name: str
    repository: str
    required_paths: tuple[str, ...]


MANAGED_PI_RESOURCES = (
    ManagedPiResource(
        "frontend-design",
        "https://github.com/anthropics/skills.git",
        ("skills/frontend-design/SKILL.md",),
    ),
)


@dataclass(frozen=True, slots=True)
class PackageRefreshSummary:
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    failed: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "updated": list(self.updated),
            "unchanged": list(self.unchanged),
            "failed": list(self.failed),
            "summary": (
                f"{len(self.updated)} updated · {len(self.unchanged)} unchanged · "
                f"{len(self.failed)} failed"
            ),
        }


async def refresh_managed_pi_packages(
    factory: async_sessionmaker[AsyncSession],
    manager: object,
    now: datetime | None = None,
) -> PackageRefreshSummary:
    now = now or datetime.now(UTC)
    async with factory() as session:
        packages = list(
            await session.scalars(
                select(PiPackage).where(PiPackage.enabled.is_(True)).order_by(PiPackage.identity)
            )
        )
    updated: list[str] = []
    unchanged: list[str] = []
    failed: list[str] = []
    for package in packages:
        active = bool(
            package.active_artifact_path
            and Path(package.active_artifact_path).is_dir()
        )
        recent_failure = package.last_update_status == "FAILED" and package.last_update_attempt_at and now - package.last_update_attempt_at < FAILURE_RETRY_INTERVAL
        recent_success = package.last_update_success_at and now - package.last_update_success_at < UPDATE_INTERVAL
        if (package.pinned and active) or recent_failure or (active and recent_success):
            unchanged.append(package.identity)
            continue
        try:
            installed = await manager.install(package.source)  # type: ignore[attr-defined]
        except Exception as error:
            async with factory() as session:
                current = await session.get(PiPackage, package.id)
                if current is not None:
                    current.last_update_attempt_at = now
                    current.last_update_status = "FAILED"
                    current.last_update_error = str(error)[-500:]
                    await session.commit()
            failed.append(package.identity)
            continue
        async with factory() as session:
            current = await session.get(PiPackage, package.id)
            if current is not None:
                current.active_version = installed.resolved_version
                current.active_artifact_path = installed.artifact_path
                current.resources = installed.resources
                current.last_update_attempt_at = now
                current.last_update_success_at = now
                current.last_update_status = "SUCCESS"
                current.last_update_error = None
                await session.commit()
        updated.append(f"{package.identity}@{installed.resolved_version}")
    summary = PackageRefreshSummary(tuple(updated), tuple(unchanged), tuple(failed))
    if updated or failed:
        async with factory() as session:
            session.add(
                Event(
                    type="pi.packages_update_completed",
                    payload=summary.as_payload(),
                    created_at=now,
                )
            )
            await session.commit()
    return summary


async def ensure_managed_pi_packages(
    factory: async_sessionmaker[AsyncSession], manager: object
) -> None:
    await refresh_managed_pi_packages(factory, manager)
    async with factory() as session:
        packages = list(
            await session.scalars(
                select(PiPackage)
                .outerjoin(
                    AgentProfilePackage,
                    AgentProfilePackage.package_id == PiPackage.id,
                )
                .where(
                    PiPackage.enabled.is_(True),
                    (PiPackage.is_default.is_(True))
                    | (AgentProfilePackage.package_id.is_not(None)),
                )
                .distinct()
            )
        )
    missing = sorted(
        package.identity
        for package in packages
        if not package.active_artifact_path
        or not Path(package.active_artifact_path).is_dir()
    )
    if missing:
        raise RuntimeError(f"managed Pi packages are unavailable: {', '.join(missing)}")


def _resource_manifest_complete(
    root: Path, resources: tuple[ManagedPiResource, ...]
) -> bool:
    try:
        revisions = json.loads((root / "revisions.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(revisions, dict):
        return False
    for resource in resources:
        revision = revisions.get(resource.name)
        if not isinstance(revision, str) or not GIT_REVISION.fullmatch(revision):
            return False
        checkout = root / "checkouts" / resource.name / revision
        if checkout.is_symlink() or not all(
            (checkout / required_path).is_file()
            for required_path in resource.required_paths
        ):
            return False
    return True


@asynccontextmanager
async def pi_process_lock(path: Path, *, exclusive: bool = True) -> AsyncIterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        while True:
            try:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


async def record_startup(
    factory: async_sessionmaker[AsyncSession],
    pi_executable: str,
    *,
    hostname: str | None = None,
    event_type: str = "system.started",
) -> None:
    async with factory() as session:
        session.add(
            Event(
                type=event_type,
                payload={
                    "hostname": hostname or socket.gethostname(),
                    "pi_version": await _version(pi_executable),
                },
            )
        )
        await session.commit()


async def update_pi_if_due(
    factory: async_sessionmaker[AsyncSession],
    npm_executable: str,
    pi_executable: str,
    lock_path: Path,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    if not await _update_due(factory, now):
        return False

    async with pi_process_lock(lock_path):
        if not await _update_due(factory, now):
            return False
        before = await _version(pi_executable)
        try:
            process = await asyncio.create_subprocess_exec(
                npm_executable,
                "install",
                "-g",
                "--ignore-scripts",
                "@earendil-works/pi-coding-agent",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            exit_code = process.returncode
            error = (stderr or stdout).decode(errors="replace").strip()[-500:]
        except OSError as exc:
            exit_code = None
            error = str(exc)[-500:]
        if exit_code:
            event = Event(
                type="pi.update_failed",
                payload={
                    "exit_code": exit_code,
                    "error": error,
                },
                created_at=now,
            )
        elif exit_code is None:
            event = Event(
                type="pi.update_failed",
                payload={"exit_code": None, "error": error},
                created_at=now,
            )
        else:
            event = Event(
                type="pi.update_completed",
                payload={
                    "version_before": before,
                    "version_after": await _version(pi_executable),
                },
                created_at=now,
            )
        async with factory() as session:
            session.add(event)
            await session.commit()
        return True


async def update_pi_resources_if_due(
    factory: async_sessionmaker[AsyncSession],
    git_executable: str,
    root: Path,
    lock_path: Path,
    *,
    now: datetime | None = None,
    resources: tuple[ManagedPiResource, ...] = MANAGED_PI_RESOURCES,
) -> bool:
    now = now or datetime.now(UTC)
    complete = _resource_manifest_complete(root, resources)
    if not await _resource_update_due(factory, now, force=not complete):
        return False
    async with pi_process_lock(lock_path):
        complete = _resource_manifest_complete(root, resources)
        if not await _resource_update_due(factory, now, force=not complete):
            return False
        try:
            revisions = {
                resource.name: await _update_resource(
                    git_executable, root, resource
                )
                for resource in resources
            }
            temporary = root / f".revisions-{os.getpid()}.json"
            temporary.write_text(json.dumps(revisions, indent=2) + "\n")
            os.replace(temporary, root / "revisions.json")
            event = Event(
                type="pi.resources_updated",
                payload={
                    "resources": revisions,
                    "summary": ", ".join(
                        f"{name}@{revision[:7]}"
                        for name, revision in revisions.items()
                    ),
                },
                created_at=now,
            )
        except (OSError, RuntimeError, ValueError) as error:
            event = Event(
                type="pi.resources_update_failed",
                payload={"error": str(error)[-500:]},
                created_at=now,
            )
        async with factory() as session:
            session.add(event)
            await session.commit()
        return True


async def ensure_pi_resources(
    factory: async_sessionmaker[AsyncSession],
    git_executable: str,
    root: Path,
    lock_path: Path,
) -> None:
    await update_pi_resources_if_due(factory, git_executable, root, lock_path)
    if not _resource_manifest_complete(root, MANAGED_PI_RESOURCES):
        raise RuntimeError("managed Pi resources are unavailable")


async def maintenance_loop(
    factory: async_sessionmaker[AsyncSession],
    npm_executable: str,
    pi_executable: str,
    lock_path: Path,
    credential_cipher: CredentialCipher | None = None,
    resource_root: Path | None = None,
    package_manager: object | None = None,
) -> None:
    while True:
        await run_maintenance_cycle(
            factory,
            npm_executable,
            pi_executable,
            lock_path,
            credential_cipher,
            resource_root=resource_root,
            package_manager=package_manager,
        )
        await asyncio.sleep(60)


async def run_maintenance_cycle(
    factory: async_sessionmaker[AsyncSession],
    npm_executable: str,
    pi_executable: str,
    lock_path: Path,
    credential_cipher: CredentialCipher | None,
    *,
    resource_root: Path | None = None,
    package_manager: object | None = None,
    now: datetime | None = None,
    model_fetcher=fetch_openai_models,
) -> None:
    try:
        await refresh_due_model_providers(
            factory,
            credential_cipher,
            now=now,
            fetcher=model_fetcher,
        )
    except Exception:
        logger.exception("model provider refresh cycle failed")
    try:
        await update_pi_if_due(
            factory, npm_executable, pi_executable, lock_path, now=now
        )
    except Exception:
        logger.exception("weekly Pi update cycle failed")
    if resource_root is not None:
        try:
            await update_pi_resources_if_due(
                factory, "git", resource_root, lock_path, now=now
            )
        except Exception:
            logger.exception("weekly Pi resource update cycle failed")
    if package_manager is not None:
        try:
            await refresh_managed_pi_packages(factory, package_manager, now=now)
        except Exception:
            logger.exception("weekly Pi package update cycle failed")


async def _update_due(
    factory: async_sessionmaker[AsyncSession], now: datetime
) -> bool:
    async with factory() as session:
        last_success = await session.scalar(
            select(func.max(Event.created_at)).where(Event.type == "pi.update_completed")
        )
        last_failure = await session.scalar(
            select(func.max(Event.created_at)).where(Event.type == "pi.update_failed")
        )
    if last_success is not None and now - last_success < UPDATE_INTERVAL:
        return False
    return last_failure is None or now - last_failure >= FAILURE_RETRY_INTERVAL


async def _resource_update_due(
    factory: async_sessionmaker[AsyncSession], now: datetime, *, force: bool = False
) -> bool:
    async with factory() as session:
        last_success = await session.scalar(
            select(func.max(Event.created_at)).where(
                Event.type == "pi.resources_updated"
            )
        )
        last_failure = await session.scalar(
            select(func.max(Event.created_at)).where(
                Event.type == "pi.resources_update_failed"
            )
        )
    if last_failure is not None and now - last_failure < FAILURE_RETRY_INTERVAL:
        return False
    return force or last_success is None or now - last_success >= UPDATE_INTERVAL


async def _update_resource(
    git_executable: str, root: Path, resource: ManagedPiResource
) -> str:
    if not RESOURCE_NAME.fullmatch(resource.name):
        raise ValueError("invalid managed Pi resource name")
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".resource-", dir=root))
    checkout = temporary / resource.name
    try:
        await _git(
            git_executable,
            "clone",
            "--depth",
            "1",
            resource.repository,
            str(checkout),
        )
        await _verify_resource(git_executable, checkout, resource.required_paths)
        revision = await _git(git_executable, "rev-parse", "HEAD", cwd=checkout)
        target = root / "checkouts" / resource.name / revision
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("managed Pi resource checkout must not be a symlink")
        if not target.exists():
            checkout.replace(target)
        await _verify_resource(git_executable, target, resource.required_paths)
        return revision
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


async def _verify_resource(
    git_executable: str, checkout: Path, required_paths: tuple[str, ...]
) -> None:
    for required_path in required_paths:
        if not (checkout / required_path).is_file():
            raise RuntimeError(f"managed Pi resource is missing {required_path}")
    await _git(git_executable, "rev-parse", "HEAD", cwd=checkout)


async def _git(
    executable: str, *arguments: str, cwd: Path | None = None
) -> str:
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        message = (stderr or stdout).decode(errors="replace").strip()[-500:]
        raise RuntimeError(message or f"git exited with {process.returncode}")
    return stdout.decode(errors="replace").strip()


async def _version(executable: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        if process.returncode == 0 and stdout.strip():
            return stdout.decode(errors="replace").strip()[:120]
    except (OSError, TimeoutError):
        pass
    return "unknown"
