import asyncio
import os
import re
import shlex
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml

from .models import Project


class ActionConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    key: str
    name: str
    runner: str
    env_file: str | None
    commands: tuple[str, ...]
    argv: tuple[tuple[str, ...], ...]

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "runner": self.runner,
            "env_file": self.env_file,
            "commands": list(self.commands),
        }


@dataclass(frozen=True, slots=True)
class ActionCatalog:
    commit_sha: str
    branch: str | None
    dirty_paths: tuple[str, ...]
    actions: dict[str, ActionDefinition]


@dataclass(frozen=True, slots=True)
class ActionPreflight:
    definition: ActionDefinition
    commit_sha: str
    branch: str | None
    origin: str | None
    env_path: Path | None
    env_values: dict[str, str]


_ACTION_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,119}")
_RUNNER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BASE_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM")


def parse_catalog(text: str) -> dict[str, ActionDefinition]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ActionConfigError(f"invalid YAML: {error}") from error
    if not isinstance(document, dict):
        raise ActionConfigError("catalog must be a mapping")
    unknown = set(document) - {"version", "actions"}
    if unknown:
        raise ActionConfigError(f"unknown root field: {sorted(unknown)[0]}")
    if document.get("version") != 1:
        raise ActionConfigError("unsupported version; expected 1")
    raw_actions = document.get("actions")
    if not isinstance(raw_actions, dict):
        raise ActionConfigError("actions must be a mapping")

    actions: dict[str, ActionDefinition] = {}
    for key, raw in raw_actions.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ActionConfigError(f"invalid action key: {key}")
        if not isinstance(raw, dict):
            raise ActionConfigError(f"action {key} must be a mapping")
        unknown = set(raw) - {"name", "runner", "env_file", "commands"}
        if unknown:
            raise ActionConfigError(f"action {key} has unknown field: {sorted(unknown)[0]}")
        name = raw.get("name")
        runner = raw.get("runner", "local")
        env_file = raw.get("env_file")
        commands = raw.get("commands")
        if not isinstance(name, str) or not name.strip():
            raise ActionConfigError(f"action {key} requires a non-empty name")
        if not isinstance(runner, str) or not _RUNNER_NAME.fullmatch(runner):
            raise ActionConfigError(f"action {key} has an invalid runner")
        if env_file is not None:
            if not isinstance(env_file, str) or not _safe_relative(env_file):
                raise ActionConfigError(f"action {key} env_file must be inside the project")
        if not isinstance(commands, list) or not commands:
            raise ActionConfigError(f"action {key} requires non-empty commands")
        parsed: list[tuple[str, ...]] = []
        normalized: list[str] = []
        for command in commands:
            if not isinstance(command, str) or "\n" in command or "\r" in command:
                raise ActionConfigError(f"action {key} commands must be one-line strings")
            try:
                arguments = tuple(shlex.split(command))
            except ValueError as error:
                raise ActionConfigError(f"action {key} has invalid command quoting") from error
            if not arguments:
                raise ActionConfigError(f"action {key} commands cannot be empty")
            normalized.append(command)
            parsed.append(arguments)
        actions[key] = ActionDefinition(
            key, name.strip(), runner, env_file, tuple(normalized), tuple(parsed)
        )
    return actions


async def load_catalog(project: Project) -> ActionCatalog:
    repository = Path(project.repository_path).resolve()
    if not repository.is_dir():
        raise ActionConfigError("project repository does not exist")
    commit_sha = await _git(repository, "rev-parse", "HEAD")
    branch = await _git(repository, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    status = await _git(
        repository, "status", "--porcelain=v1", "--untracked-files=all"
    )
    dirty_paths = tuple(
        _status_path(line) for line in status.splitlines() if len(line) >= 4
    )
    source = await _git(repository, "show", "HEAD:carlo-actions.yaml")
    return ActionCatalog(
        commit_sha=commit_sha,
        branch=branch or None,
        dirty_paths=dirty_paths,
        actions=parse_catalog(source),
    )


async def preflight(project: Project, action_key: str) -> ActionPreflight:
    catalog = await load_catalog(project)
    if catalog.dirty_paths:
        raise ActionConfigError(
            "repository has pending changes: " + ", ".join(catalog.dirty_paths)
        )
    try:
        definition = catalog.actions[action_key]
    except KeyError as error:
        raise ActionConfigError(f"action not found: {action_key}") from error
    repository = Path(project.repository_path).resolve()
    origin = await _git(repository, "remote", "get-url", "origin", check=False) or None
    if definition.runner != "local" and origin and _has_url_credentials(origin):
        raise ActionConfigError("SSH actions cannot use an origin with embedded credentials")

    env_path: Path | None = None
    env_values: dict[str, str] = {}
    if definition.env_file:
        candidate = repository / definition.env_file
        try:
            env_path = candidate.resolve(strict=True)
        except OSError as error:
            raise ActionConfigError(f"env_file not found: {definition.env_file}") from error
        if not env_path.is_relative_to(repository) or not env_path.is_file():
            raise ActionConfigError("env_file must be a regular file inside the project")
        env_values = parse_dotenv(env_path.read_text())
    return ActionPreflight(
        definition, catalog.commit_sha, catalog.branch, origin, env_path, env_values
    )


def parse_dotenv(text: str) -> dict[str, str]:
    if "\0" in text:
        raise ActionConfigError("dotenv contains a NUL byte")
    values: dict[str, str] = {}
    for number, original in enumerate(text.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ActionConfigError(f"invalid dotenv line {number}")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ActionConfigError(f"invalid dotenv line {number}")
        raw_value = raw_value.strip()
        if raw_value.startswith(("'", '"')):
            try:
                tokens = shlex.split(raw_value, posix=True)
            except ValueError as error:
                raise ActionConfigError(f"invalid dotenv line {number}") from error
            if len(tokens) != 1:
                raise ActionConfigError(f"invalid dotenv line {number}")
            value = tokens[0]
        else:
            value = raw_value
        values[name] = value
    return values


def minimal_environment(values: Mapping[str, str]) -> dict[str, str]:
    environment = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
    environment.update(values)
    return environment


def redact(text: str, secrets: Collection[str]) -> str:
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _status_path(line: str) -> str:
    path = line[3:]
    return path.rsplit(" -> ", 1)[-1]


def _has_url_credentials(origin: str) -> bool:
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and "@" in parsed.netloc


async def _git(repository: Path, *args: str, check: bool = True) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repository),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if check and process.returncode:
        message = stderr.decode(errors="replace").strip()
        raise ActionConfigError(message or f"git {' '.join(args)} failed")
    return stdout.decode(errors="replace").rstrip()
