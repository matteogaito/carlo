import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from glob import has_magic
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .maintenance import pi_process_lock

NPM_NAME = re.compile(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+\Z", re.IGNORECASE)
SAFE_VERSION = re.compile(r"[A-Za-z0-9._-]{1,255}\Z")


class PiPackageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PackageSource:
    identity: str
    pinned: bool


@dataclass(frozen=True, slots=True)
class InstalledPiPackage:
    identity: str
    source: str
    resolved_version: str
    artifact_path: str
    resources: dict[str, list[str]]


def parse_package_source(source: str) -> PackageSource:
    if source.startswith("npm:"):
        value = source[4:]
        split_at = value.rfind("@")
        if value.startswith("@") and split_at == 0:
            split_at = -1
        name = value[:split_at] if split_at > value.rfind("/") else value
        pinned = name != value
        if not NPM_NAME.fullmatch(name):
            raise PiPackageError("package source must be an npm or Git source")
        return PackageSource(f"npm:{name}", pinned)
    if source.startswith("git:") or source.startswith(
        ("https://", "http://", "ssh://", "git://")
    ):
        split_at = source.rfind("@")
        boundary = max(source.rfind("/"), source.rfind(":"))
        pinned = split_at > boundary
        identity = source[:split_at] if pinned else source
        if not identity or any(character.isspace() for character in identity):
            raise PiPackageError("package source must be an npm or Git source")
        return PackageSource(identity, pinned)
    raise PiPackageError("package source must be an npm or Git source")


class PiPackageManager:
    def __init__(self, pi_executable: Path, root: Path, lock_path: Path) -> None:
        self.pi_executable = pi_executable
        self.root = root
        self.lock_path = lock_path

    async def install(self, source: str) -> InstalledPiPackage:
        parsed = parse_package_source(source)
        if self.root.is_symlink():
            raise PiPackageError("Pi package root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        agent_dir = staging / "agent"
        try:
            async with pi_process_lock(self.lock_path):
                try:
                    process = await asyncio.create_subprocess_exec(
                        str(self.pi_executable),
                        "install",
                        source,
                        "--no-approve",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env={**os.environ, "PI_CODING_AGENT_DIR": str(agent_dir)},
                    )
                    stdout, stderr = await process.communicate()
                except OSError as error:
                    raise PiPackageError(str(error)) from error
            if process.returncode:
                message = (stderr or stdout).decode(errors="replace").strip()[-500:]
                raise PiPackageError(message or f"Pi exited with {process.returncode}")
            package_root = self._locate_package(agent_dir, parsed)
            manifest = self._manifest(package_root)
            version = await self._resolved_version(package_root, manifest, parsed)
            resources = self._resources(package_root, manifest)
            relative = package_root.relative_to(agent_dir)
            target = self.root / self._safe_identity(parsed.identity) / version
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise PiPackageError("promoted Pi package must not be a symlink")
            if not target.exists():
                os.replace(agent_dir, target)
            promoted = target / relative
            if not promoted.is_dir():
                raise PiPackageError("promoted Pi package is unavailable")
            return InstalledPiPackage(
                parsed.identity, source, version, str(promoted), resources
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _locate_package(agent_dir: Path, source: PackageSource) -> Path:
        if source.identity.startswith("npm:"):
            root = agent_dir / "npm" / "node_modules" / source.identity[4:]
            if root.is_dir() and not root.is_symlink():
                return root
        else:
            candidates = [
                metadata.parent
                for metadata in (agent_dir / "git").rglob(".git")
                if metadata.is_dir()
                and not metadata.parent.is_symlink()
                and (metadata.parent / "package.json").is_file()
            ]
            if len(candidates) == 1:
                return candidates[0]
        raise PiPackageError("Pi did not install the requested package")

    @staticmethod
    def _manifest(package_root: Path) -> dict[str, Any]:
        try:
            value = json.loads((package_root / "package.json").read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise PiPackageError("installed package has no valid package.json") from error
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise PiPackageError("installed package has no valid package name")
        pi = value.get("pi")
        if pi is not None and not isinstance(pi, dict):
            raise PiPackageError("installed package has an invalid Pi manifest")
        return value

    @staticmethod
    async def _resolved_version(
        package_root: Path, manifest: dict[str, Any], source: PackageSource
    ) -> str:
        if source.identity.startswith("npm:"):
            version = manifest.get("version")
            if isinstance(version, str) and SAFE_VERSION.fullmatch(version):
                return version
            raise PiPackageError("npm package has no valid version")
        process = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=package_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        revision = stdout.decode(errors="replace").strip()
        if process.returncode or not re.fullmatch(r"[0-9a-f]{40}", revision):
            message = stderr.decode(errors="replace").strip()[-500:]
            raise PiPackageError(message or "Git package has no valid revision")
        return revision

    @staticmethod
    def _resources(
        package_root: Path, manifest: dict[str, Any]
    ) -> dict[str, list[str]]:
        declared = manifest.get("pi") or {}
        result: dict[str, list[str]] = {
            "extensions": [],
            "skills": [],
            "prompts": [],
            "themes": [],
        }
        for kind in result:
            entries = declared.get(kind)
            if entries is None:
                conventional = package_root / kind
                entries = [f"./{kind}"] if conventional.exists() else []
            if not isinstance(entries, list) or not all(
                isinstance(entry, str) for entry in entries
            ):
                raise PiPackageError(f"invalid Pi {kind} manifest")
            exclusions = [entry[1:].removeprefix("./") for entry in entries if entry.startswith("!")]
            for entry in (entry for entry in entries if not entry.startswith("!")):
                clean = entry.removeprefix("./")
                matches = list(package_root.glob(clean)) if has_magic(clean) else [package_root / clean]
                matches = [
                    path for path in matches
                    if not any(path.relative_to(package_root).match(pattern) for pattern in exclusions)
                ]
                if not matches:
                    raise PiPackageError(f"Pi package resource is missing: {clean}")
                for candidate in matches:
                    path = candidate.resolve()
                    try:
                        path.relative_to(package_root.resolve())
                    except ValueError as error:
                        raise PiPackageError("Pi package resource escapes its root") from error
                    if not path.exists():
                        raise PiPackageError(f"Pi package resource is missing: {clean}")
                    if kind == "skills":
                        skill_files = [path] if path.is_file() and path.name == "SKILL.md" else path.rglob("SKILL.md")
                        result[kind].extend(skill.parent.name for skill in skill_files)
                    elif path.is_dir():
                        suffixes = {
                            "extensions": {".js", ".ts"},
                            "prompts": {".md"},
                            "themes": {".json"},
                        }[kind]
                        result[kind].extend(
                            str(item.relative_to(package_root))
                            for item in path.rglob("*")
                            if item.is_file() and item.suffix in suffixes
                        )
                    else:
                        result[kind].append(str(path.relative_to(package_root)))
        return {key: list(dict.fromkeys(values)) for key, values in result.items()}

    @staticmethod
    def _safe_identity(identity: str) -> str:
        readable = re.sub(r"[^A-Za-z0-9._-]+", "-", identity).strip("-")[:60]
        digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
        return f"{readable}-{digest}"
