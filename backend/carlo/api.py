import asyncio
import json
import os
import re
from uuid import uuid4
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from .auth import InvalidCredentials, LoginThrottled, login, resolve_session, revoke_session
from .actions import ActionConfigError, load_catalog, preflight
from .config import Settings
from .model_providers import (
    CredentialCipher,
    ModelProviderError,
    calculate_compaction,
    model_runtime_evidence,
    refresh_model_provider,
    resolve_agent_profile,
)
from .domain import InvalidTransition, TaskStage, TaskStatus, transition
from .models import AgentProfile as AgentProfileRecord
from .maintenance import record_startup
from .models import (
    ActionRun,
    ActionStep,
    AvailableModel,
    Attempt,
    Escalation,
    Discovery,
    DiscoveryMessage,
    DiscoveryTurn,
    Event,
    PlanRevision,
    Project,
    ModelProvider,
    PiRuntimeSettings,
    Runner,
    Task,
    User,
    ValidationRun,
)
from .provider import AgentProfile, CodingAgentProvider, ProviderError
from .ssh import HostScan, SshError, SshTransport, validate_runner
from .telegram import TelegramNotifier, TelegramTransport, telegram_enabled
from .tasks import TaskCreationError, create_task as create_task_record


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    key: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,11}$")
    repository_path: str
    default_branch: str = "main"
    integration_branch: str = "carlo-Dev"
    validation_commands: list[str] = []


class TaskCreate(BaseModel):
    project_id: int
    title: str = Field(min_length=1, max_length=240)
    goal: str = Field(min_length=1, max_length=1024 * 1024)
    prompt_filename: str | None = Field(default=None, max_length=255)
    priority: int = 0
    created_source: str = "web"

    @field_validator("prompt_filename")
    @classmethod
    def markdown_filename(cls, value: str | None) -> str | None:
        if value is not None and Path(value).suffix.lower() != ".md":
            raise ValueError("prompt_filename must be a Markdown file")
        return value


class DiscoveryCreate(BaseModel):
    project_id: int
    title: str = Field(min_length=1, max_length=240)
    message: str = Field(min_length=1, max_length=1024 * 1024)


class DiscoveryMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=1024 * 1024)


class DiscoveryTaskCreate(BaseModel):
    proposal_ids: list[str] = []


class PlanningAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=1024 * 1024)


class Approval(BaseModel):
    revision: int
    version: int


class ProfileUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    permissions: dict[str, Any] | None = None
    default_skills: list[str] | None = None
    context_policy: dict[str, Any] | None = None
    active: bool | None = None
    model_provider_id: int | None = None
    available_model_id: int | None = None


class ModelProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")
    kind: str = Field(default="openai-compatible", pattern=r"^openai-compatible$")
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=16_384)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    refresh_interval_minutes: int = Field(default=15, ge=1, le=1440)

    @field_validator("base_url")
    @classmethod
    def http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")


class ModelProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9-]{0,79}$"
    )
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, min_length=1, max_length=16_384)
    compatibility: dict[str, Any] | None = None
    refresh_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    default_model_id: int | None = None
    active: bool | None = None

    @field_validator("base_url")
    @classmethod
    def optional_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")


class AvailableModelUpdate(BaseModel):
    context_window_override: int | None = Field(default=None, ge=1)
    max_tokens_override: int | None = Field(default=None, ge=1)


class PiRuntimeSettingsUpdate(BaseModel):
    compaction_enabled: bool | None = None
    reserve_percent: int | None = Field(default=None, ge=1, le=90)
    keep_recent_percent: int | None = Field(default=None, ge=1, le=90)


class TaskModelUpdate(BaseModel):
    available_model_id: int | None = None


class PlanMetadata(BaseModel):
    title: str | None = None
    description: str | None = None
    key_points: list[str] = Field(default_factory=list)
    implementation_tasks: list["ImplementationTask"] = Field(default_factory=list)
    skills: list[str]
    implementation_phases: list[str] = Field(default_factory=list)
    validation_commands: list[str]
    browser_validation: bool
    build_required: bool
    run_required: bool
    deployment_expected: bool
    risk_flags: list[str]
    affected_areas: list[str]


class ImplementationTask(BaseModel):
    title: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    intervention_points: list[str] = Field(default_factory=list)


class PlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_markdown: str = Field(min_length=1)
    plan_markdown: str = Field(min_length=1)
    metadata: PlanMetadata


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class RunnerCreate(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    identity_file: str
    workspace_root: str = ".carlo"


class RunnerUpdate(BaseModel):
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    identity_file: str | None = None
    workspace_root: str | None = None
    enabled: bool | None = None


class RunnerTrust(BaseModel):
    fingerprint: str = Field(min_length=1, max_length=160)


SESSION_COOKIE = "carlo_session"


async def recover_interrupted_reworks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tasks = (
            await session.scalars(
                select(Task).where(
                    Task.status == TaskStatus.NOT_READY,
                    Task.stage == TaskStage.PLANNING,
                    Task.planning_session_id.like("%-plan-rework-%"),
                    Task.planning_question.is_(None),
                )
            )
        ).all()
        for task in tasks:
            task.status = TaskStatus.FAILED
            task.stage = TaskStage.BLOCKED
            task.version += 1
            session.add(
                Event(
                    task=task,
                    type="task.rework.recovered_after_restart",
                    payload={},
                )
            )
        if tasks:
            await session.commit()


def create_app(
    session_factory: async_sessionmaker[AsyncSession],
    provider: CodingAgentProvider,
    settings: Settings | None = None,
    ssh_transport: SshTransport | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    credential_cipher = (
        CredentialCipher.from_base64(settings.credential_encryption_key)
        if settings.credential_encryption_key
        and "CHANGE_ME" not in settings.credential_encryption_key
        else None
    )
    ssh_transport = ssh_transport or SshTransport(
        Path(settings.ssh_known_hosts), connect_timeout=settings.ssh_connect_timeout
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await recover_interrupted_reworks(session_factory)
        if telegram_enabled(settings.telegram_bot_token, settings.telegram_chat_id):
            await TelegramNotifier(
                session_factory,
                TelegramTransport(),
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                settings.telegram_level,
            ).initialize_cursor()
        await record_startup(session_factory, settings.pi_executable)
        yield

    app = FastAPI(title="CARLO v3", lifespan=lifespan)

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def require_admin(request: Request) -> User:
        user = await resolve_session(
            session_factory, request.cookies.get(SESSION_COOKIE, "")
        )
        if user is None:
            raise HTTPException(401, "authentication required")
        if user.role != "admin":
            raise HTTPException(403, "administrator access required")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("origin") != settings.app_origin
        ):
            raise HTTPException(403, "invalid request origin")
        return user

    @app.post("/api/auth/login")
    async def login_user(
        payload: LoginPayload, request: Request, response: Response
    ) -> dict[str, str]:
        source_ip = request.client.host if request.client is not None else "unknown"
        try:
            user, token = await login(
                session_factory,
                payload.username,
                payload.password,
                source_ip,
                settings.session_hours,
            )
        except InvalidCredentials as error:
            raise HTTPException(401, "invalid credentials") from error
        except LoginThrottled as error:
            raise HTTPException(429, "try again later") from error
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return _user_view(user)

    @app.get("/api/auth/me")
    async def current_user(user: User = Depends(require_admin)) -> dict[str, str]:
        return _user_view(user)

    @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout_user(
        request: Request, user: User = Depends(require_admin)
    ) -> Response:
        del user
        await revoke_session(
            session_factory, request.cookies.get(SESSION_COOKIE, "")
        )
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    api = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])

    @api.post("/projects", status_code=status.HTTP_201_CREATED)
    async def create_project(
        payload: ProjectCreate, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        repository = str(Path(payload.repository_path).resolve())
        if not await _is_git_repository(repository):
            raise HTTPException(422, "repository_path must be a Git repository")
        project = Project(
            name=payload.name,
            key=payload.key,
            repository_path=repository,
            default_branch=payload.default_branch,
            integration_branch=payload.integration_branch,
            validation_commands=payload.validation_commands,
        )
        session.add(project)
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(409, "project key or repository already exists") from error
        await session.refresh(project)
        return _project_view(project)

    @api.get("/projects")
    async def list_projects(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        projects = (await session.scalars(select(Project).order_by(Project.key))).all()
        return [_project_view(project) for project in projects]

    @api.get("/runners")
    async def list_runners(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        runners = (await session.scalars(select(Runner).order_by(Runner.name))).all()
        return [
            {
                "id": None,
                "name": "local",
                "type": "local",
                "enabled": True,
                "last_check_ok": True,
            },
            *[_runner_view(runner) for runner in runners],
        ]

    @api.post("/runners", status_code=status.HTTP_201_CREATED)
    async def create_runner(
        payload: RunnerCreate, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        if payload.name == "local":
            raise HTTPException(409, "local is reserved")
        runner = Runner(**payload.model_dump(), enabled=False)
        try:
            validate_runner(runner)
            scan = await ssh_transport.scan_host(runner.host, runner.port)
        except SshError as error:
            raise HTTPException(422, str(error)) from error
        runner.host_key = scan.host_key
        runner.fingerprint = scan.fingerprint
        session.add(runner)
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(409, "runner name already exists") from error
        await session.refresh(runner)
        return _runner_view(runner)

    @api.patch("/runners/{runner_id}")
    async def update_runner(
        runner_id: int,
        payload: RunnerUpdate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        runner = await session.get(Runner, runner_id)
        if runner is None:
            raise HTTPException(404, "runner not found")
        connection_fields = {
            "host", "port", "username", "identity_file", "workspace_root"
        }
        changed_connection = bool(payload.model_fields_set & connection_fields)
        for field in payload.model_fields_set:
            setattr(runner, field, getattr(payload, field))
        try:
            validate_runner(runner)
            if payload.enabled is True and (
                not runner.fingerprint or runner.last_check_ok is not True
            ):
                raise SshError("runner must be trusted and tested before enabling")
            if changed_connection:
                scan = await ssh_transport.scan_host(runner.host, runner.port)
                runner.host_key = scan.host_key
                runner.fingerprint = scan.fingerprint
                runner.enabled = False
                runner.last_check_ok = None
                runner.last_checked_at = None
        except SshError as error:
            raise HTTPException(422, str(error)) from error
        await session.commit()
        await session.refresh(runner)
        return _runner_view(runner)

    @api.post("/runners/{runner_id}/trust")
    async def trust_runner(
        runner_id: int,
        payload: RunnerTrust,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        runner = await session.get(Runner, runner_id)
        if runner is None:
            raise HTTPException(404, "runner not found")
        if not runner.host_key or not runner.fingerprint or runner.last_checked_at is not None:
            raise HTTPException(409, "runner has no pending host scan")
        scan = HostScan(
            runner.host_key,
            runner.fingerprint,
            runner.host_key.split()[1],
        )
        try:
            await ssh_transport.confirm_host(scan, payload.fingerprint)
        except SshError as error:
            raise HTTPException(409, str(error)) from error
        await _check_runner(ssh_transport, runner)
        runner.enabled = runner.last_check_ok is True
        session.add(
            Event(type="runner.trusted", payload={"runner_id": runner.id, "name": runner.name})
        )
        await session.commit()
        await session.refresh(runner)
        return _runner_view(runner)

    @api.post("/runners/{runner_id}/test")
    async def test_runner_connection(
        runner_id: int, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        runner = await session.get(Runner, runner_id)
        if runner is None:
            raise HTTPException(404, "runner not found")
        await _check_runner(ssh_transport, runner)
        await session.commit()
        await session.refresh(runner)
        return _runner_view(runner)

    @api.get("/projects/{project_id}/actions")
    async def project_actions(
        project_id: int, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        try:
            catalog = await load_catalog(project)
        except ActionConfigError as error:
            return {
                "project_id": project.id,
                "commit_sha": None,
                "branch": None,
                "dirty_paths": [],
                "actions": [],
                "error": str(error),
            }
        return {
            "project_id": project.id,
            "commit_sha": catalog.commit_sha,
            "branch": catalog.branch,
            "dirty_paths": list(catalog.dirty_paths),
            "actions": [
                {"key": action.key, **action.snapshot()}
                for action in sorted(catalog.actions.values(), key=lambda item: item.key)
            ],
            "error": None,
        }

    @api.post(
        "/projects/{project_id}/actions/{action_key}/runs",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_action_run(
        project_id: int,
        action_key: str,
        user: User = Depends(require_admin),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        try:
            checked = await preflight(project, action_key)
        except ActionConfigError as error:
            raise HTTPException(409, str(error)) from error

        definition = checked.definition
        if definition.runner == "local":
            runner_snapshot: dict[str, Any] = {"type": "local", "name": "local"}
        else:
            runner = await session.scalar(
                select(Runner).where(Runner.name == definition.runner)
            )
            if runner is None or not runner.enabled or not runner.fingerprint:
                raise HTTPException(409, f"runner is not ready: {definition.runner}")
            runner_snapshot = {
                "type": "ssh",
                "name": runner.name,
                "host": runner.host,
                "port": runner.port,
                "username": runner.username,
                "identity_file": runner.identity_file,
                "workspace_root": runner.workspace_root,
                "fingerprint": runner.fingerprint,
                "host_key": runner.host_key,
            }

        run = ActionRun(
            project_id=project.id,
            requested_by_id=user.id,
            action_key=definition.key,
            action_name=definition.name,
            definition=definition.snapshot(),
            runner_name=definition.runner,
            runner_snapshot=runner_snapshot,
            status="queued",
            commit_sha=checked.commit_sha,
            branch_name=checked.branch,
            origin=checked.origin,
            env_file=definition.env_file,
            env_names=sorted(checked.env_values),
            artifact_path="pending",
            steps=[
                ActionStep(position=position, command=command)
                for position, command in enumerate(definition.commands, start=1)
            ],
        )
        session.add(run)
        await session.flush()
        run_root = (Path(settings.artifact_root).resolve() / "actions" / str(run.id))
        run.artifact_path = str(run_root / "console.log")
        secret_path: Path | None = None
        try:
            run_root.mkdir(parents=True, exist_ok=True)
            if checked.env_path is not None:
                secret_path = run_root / "environment"
                descriptor = os.open(
                    secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as output:
                    output.write(checked.env_path.read_bytes())
                run.secret_path = str(secret_path)
            session.add(
                Event(
                    type="action.queued",
                    payload={
                        "run_id": run.id,
                        "project_id": project.id,
                        "action_key": definition.key,
                        "runner": definition.runner,
                        "commit_sha": checked.commit_sha,
                    },
                )
            )
            await session.commit()
        except Exception:
            await session.rollback()
            if secret_path is not None:
                secret_path.unlink(missing_ok=True)
            raise
        return _action_run_view(run)

    @api.get("/action-runs")
    async def list_action_runs(
        project_id: int | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        statement = select(ActionRun).order_by(
            ActionRun.requested_at.desc(), ActionRun.id.desc()
        )
        if project_id is not None:
            statement = statement.where(ActionRun.project_id == project_id)
        runs = (await session.scalars(statement.limit(limit))).all()
        return [_action_run_view(run) for run in runs]

    @api.get("/action-runs/{run_id}")
    async def get_action_run(
        run_id: int, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        return _action_run_view(await _action_run_or_404(session, run_id))

    @api.get("/action-runs/{run_id}/console")
    async def get_action_console(
        run_id: int,
        offset: int = Query(default=0, ge=0),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        run = await _action_run_or_404(session, run_id)
        root = (Path(settings.artifact_root).resolve() / "actions").resolve()
        artifact = Path(run.artifact_path).resolve()
        if not artifact.is_relative_to(root):
            raise HTTPException(500, "invalid console artifact path")
        if not artifact.is_file():
            return {"offset": offset, "next_offset": offset, "text": "", "eof": True}
        size = artifact.stat().st_size
        if offset > size:
            raise HTTPException(416, "console offset exceeds file size")
        with artifact.open("rb") as source:
            source.seek(offset)
            data = source.read(256 * 1024)
        next_offset = offset + len(data)
        return {
            "offset": offset,
            "next_offset": next_offset,
            "text": data.decode(errors="replace"),
            "eof": next_offset >= size,
        }

    @api.post("/action-runs/{run_id}/cancel")
    async def cancel_action_run(
        run_id: int, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        run = await session.scalar(
            select(ActionRun).where(ActionRun.id == run_id).with_for_update()
        )
        if run is None:
            raise HTTPException(404, "action run not found")
        if run.status == "queued":
            now = datetime.now(UTC)
            run.status = "cancelled"
            run.internal_stage = "complete"
            run.cancel_requested_at = now
            run.finished_at = now
            for step in run.steps:
                step.status = "skipped"
                step.finished_at = now
            if run.secret_path:
                Path(run.secret_path).unlink(missing_ok=True)
                run.secret_path = None
            event_type = "action.cancelled"
        elif run.status == "running":
            if run.cancel_requested_at is None:
                run.cancel_requested_at = datetime.now(UTC)
            event_type = "action.cancel_requested"
        else:
            return _action_run_view(run)
        session.add(
            Event(
                type=event_type,
                payload={
                    "run_id": run.id,
                    "project_id": run.project_id,
                    "action_key": run.action_key,
                },
            )
        )
        await session.commit()
        return _action_run_view(run)

    @api.get("/agent-profiles")
    async def list_agent_profiles(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        profiles = (
            await session.scalars(
                select(AgentProfileRecord).order_by(AgentProfileRecord.name)
            )
        ).all()
        return [_profile_view(profile) for profile in profiles]

    @api.patch("/agent-profiles/{name}")
    async def update_agent_profile(
        name: str,
        payload: ProfileUpdate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        profile = await session.scalar(
            select(AgentProfileRecord).where(AgentProfileRecord.name == name)
        )
        if profile is None:
            raise HTTPException(404, "agent profile not found")
        selected_provider_id = (
            payload.model_provider_id
            if "model_provider_id" in payload.model_fields_set
            else profile.model_provider_id
        )
        selected_model_id = (
            payload.available_model_id
            if "available_model_id" in payload.model_fields_set
            else profile.available_model_id
        )
        if (
            "model_provider_id" in payload.model_fields_set
            and payload.model_provider_id is not None
        ):
            selected_model_id = None
        if (
            "available_model_id" in payload.model_fields_set
            and payload.available_model_id is not None
        ):
            selected_provider_id = None
        if selected_provider_id is not None and selected_model_id is not None:
            raise HTTPException(422, "choose a model provider default or a concrete model")
        if selected_provider_id is not None:
            provider_record = await session.get(ModelProvider, selected_provider_id)
            if provider_record is None or not provider_record.active:
                raise HTTPException(409, "model provider is unavailable")
        if selected_model_id is not None:
            await _selectable_model(session, selected_model_id)
        for field in payload.model_fields_set:
            setattr(profile, field, getattr(payload, field))
        if "model_provider_id" in payload.model_fields_set and payload.model_provider_id is not None:
            profile.available_model_id = None
            profile.model = None
        if "available_model_id" in payload.model_fields_set and payload.available_model_id is not None:
            profile.model_provider_id = None
            profile.model = None
        session.add(
            Event(type="agent_profile.updated", payload={"name": name, "fields": sorted(payload.model_fields_set)})
        )
        await session.commit()
        return _profile_view(profile)

    @api.get("/settings/model-providers")
    async def list_model_providers(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        providers = (
            await session.scalars(select(ModelProvider).order_by(ModelProvider.name))
        ).all()
        return [_model_provider_view(provider) for provider in providers]

    @api.post("/settings/model-providers", status_code=status.HTTP_201_CREATED)
    async def create_model_provider(
        payload: ModelProviderCreate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        if credential_cipher is None:
            raise HTTPException(503, "credential encryption is not configured")
        encrypted = credential_cipher.encrypt(payload.api_key)
        provider = ModelProvider(
            name=payload.name,
            slug=payload.slug,
            kind=payload.kind,
            base_url=payload.base_url,
            credential_ciphertext=encrypted.ciphertext,
            credential_nonce=encrypted.nonce,
            credential_hint=f"…{payload.api_key[-4:]}",
            compatibility=payload.compatibility,
            refresh_interval_minutes=payload.refresh_interval_minutes,
        )
        session.add(provider)
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(409, "model provider slug already exists") from error
        session.add(
            Event(
                type="model_provider.created",
                payload={"model_provider_id": provider.id, "slug": provider.slug},
            )
        )
        await session.commit()
        await session.refresh(provider)
        return _model_provider_view(provider)

    @api.patch("/settings/model-providers/{provider_id}")
    async def update_model_provider(
        provider_id: int,
        payload: ModelProviderUpdate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            raise HTTPException(404, "model provider not found")
        if "default_model_id" in payload.model_fields_set:
            if payload.default_model_id is None:
                provider.default_model_id = None
            else:
                model = await session.get(AvailableModel, payload.default_model_id)
                if model is None or model.model_provider_id != provider_id:
                    raise HTTPException(422, "default model must belong to this provider")
                if (
                    model.status != "AVAILABLE"
                    or model.effective_context_window is None
                    or model.effective_max_tokens is None
                ):
                    raise HTTPException(409, "default model is not selectable")
                provider.default_model_id = model.id
        if "api_key" in payload.model_fields_set:
            if payload.api_key is None:
                raise HTTPException(422, "api_key cannot be null")
            if credential_cipher is None:
                raise HTTPException(503, "credential encryption is not configured")
            encrypted = credential_cipher.encrypt(payload.api_key)
            provider.credential_ciphertext = encrypted.ciphertext
            provider.credential_nonce = encrypted.nonce
            provider.credential_hint = f"…{payload.api_key[-4:]}"
        for field in payload.model_fields_set - {"api_key", "default_model_id"}:
            setattr(provider, field, getattr(payload, field))
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(409, "model provider slug already exists") from error
        session.add(
            Event(
                type="model_provider.updated",
                payload={
                    "model_provider_id": provider_id,
                    "fields": sorted(payload.model_fields_set),
                },
            )
        )
        await session.commit()
        await session.refresh(provider)
        return _model_provider_view(provider)

    @api.post("/settings/model-providers/{provider_id}/refresh")
    async def refresh_model_provider_now(provider_id: int) -> dict[str, Any]:
        if credential_cipher is None:
            raise HTTPException(503, "credential encryption is not configured")
        try:
            result = await refresh_model_provider(
                session_factory, credential_cipher, provider_id
            )
        except ModelProviderError as error:
            raise HTTPException(502, str(error)) from error
        return {
            "provider_id": result.provider_id,
            "seen": result.seen,
            "unavailable": result.unavailable,
            "default_model_id": result.default_model_id,
        }

    @api.delete(
        "/settings/model-providers/{provider_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_model_provider(
        provider_id: int,
        session: AsyncSession = Depends(get_session),
    ) -> Response:
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            raise HTTPException(404, "model provider not found")
        profile_reference = await session.scalar(
            select(func.count())
            .select_from(AgentProfileRecord)
            .outerjoin(
                AvailableModel,
                AgentProfileRecord.available_model_id == AvailableModel.id,
            )
            .where(
                (AgentProfileRecord.model_provider_id == provider_id)
                | (AvailableModel.model_provider_id == provider_id)
            )
        )
        task_reference = await session.scalar(
            select(func.count())
            .select_from(Task)
            .join(AvailableModel, Task.available_model_id == AvailableModel.id)
            .where(AvailableModel.model_provider_id == provider_id)
        )
        if profile_reference or task_reference:
            raise HTTPException(409, "model provider is still referenced")
        provider.default_model_id = None
        await session.flush()
        await session.delete(provider)
        session.add(
            Event(
                type="model_provider.deleted",
                payload={"model_provider_id": provider_id, "slug": provider.slug},
            )
        )
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get("/settings/models")
    async def list_available_models(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        pi_settings = await _pi_settings(session)
        catalog = (
            await session.scalars(
                select(AvailableModel).order_by(
                    AvailableModel.model_provider_id, AvailableModel.external_id
                )
            )
        ).all()
        providers = {
            provider.id: provider
            for provider in (await session.scalars(select(ModelProvider))).all()
        }
        return [
            _available_model_view(model, providers[model.model_provider_id], pi_settings)
            for model in catalog
        ]

    @api.patch("/settings/models/{model_id}")
    async def update_available_model(
        model_id: int,
        payload: AvailableModelUpdate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        model = await session.get(AvailableModel, model_id)
        if model is None:
            raise HTTPException(404, "model not found")
        for field in payload.model_fields_set:
            setattr(model, field, getattr(payload, field))
        _validate_model_limits(model)
        session.add(
            Event(
                type="model.updated",
                payload={"model_id": model.id, "fields": sorted(payload.model_fields_set)},
            )
        )
        await session.commit()
        provider = await session.get(ModelProvider, model.model_provider_id)
        return _available_model_view(model, provider, await _pi_settings(session))

    @api.get("/settings/pi")
    async def get_pi_runtime_settings(
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        return _pi_settings_view(await _pi_settings(session))

    @api.patch("/settings/pi")
    async def update_pi_runtime_settings(
        payload: PiRuntimeSettingsUpdate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        pi_settings = await _pi_settings(session)
        for field in payload.model_fields_set:
            setattr(pi_settings, field, getattr(payload, field))
        models = (
            await session.scalars(
                select(AvailableModel).where(AvailableModel.status == "AVAILABLE")
            )
        ).all()
        try:
            for model in models:
                if (
                    model.effective_context_window is not None
                    and model.effective_max_tokens is not None
                ):
                    calculate_compaction(
                        model.effective_context_window,
                        model.effective_max_tokens,
                        pi_settings.reserve_percent,
                        pi_settings.keep_recent_percent,
                    )
        except ModelProviderError as error:
            await session.rollback()
            raise HTTPException(422, str(error)) from error
        session.add(
            Event(
                type="pi_settings.updated",
                payload={"fields": sorted(payload.model_fields_set)},
            )
        )
        await session.commit()
        return _pi_settings_view(pi_settings)

    @api.post("/tasks", status_code=status.HTTP_201_CREATED)
    async def create_task(
        payload: TaskCreate, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        try:
            task = await create_task_record(
                session,
                payload.project_id,
                payload.title,
                payload.goal,
                priority=payload.priority,
                created_source=payload.created_source,
            )
        except TaskCreationError as error:
            raise HTTPException(error.status_code, error.detail) from error
        return await _task_view(session, task)

    @api.patch("/tasks/{task_id}/model")
    async def update_task_model(
        task_id: str,
        payload: TaskModelUpdate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        task = await _task_or_404(session, task_id)
        if task.status in {TaskStatus.IN_PROGRESS, TaskStatus.TEST, TaskStatus.DONE}:
            raise HTTPException(409, "model cannot change after execution starts")
        if payload.available_model_id is not None:
            await _selectable_model(session, payload.available_model_id)
        task.available_model_id = payload.available_model_id
        task.version += 1
        session.add(
            Event(
                task=task,
                type="task.model_updated",
                payload={"available_model_id": payload.available_model_id},
            )
        )
        await session.commit()
        return await _task_view(session, task)

    @api.post("/discoveries", status_code=status.HTTP_201_CREATED)
    async def create_discovery(
        payload: DiscoveryCreate, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        project = await session.get(Project, payload.project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        profile = await _profile(session, "discovery")
        discovery = Discovery(
            project=project,
            title=payload.title,
            profile_id=profile.id,
            provider_session_id=f"discovery-{uuid4().hex}",
            state=_empty_discovery_state(),
            memory_path="pending",
        )
        session.add(discovery)
        await session.flush()
        discovery.memory_path = str(
            Path(settings.artifact_root).resolve()
            / "discoveries"
            / str(discovery.id)
            / "MEMORY.md"
        )
        message = DiscoveryMessage(
            discovery=discovery, sequence=1, role="user", content=payload.message
        )
        session.add(message)
        await session.flush()
        session.add_all(
            [
                DiscoveryTurn(discovery=discovery, input_message=message),
                Event(discovery=discovery, type="discovery.created", payload={}),
            ]
        )
        await session.commit()
        return await _discovery_view(session, discovery)

    @api.get("/discoveries")
    async def list_discoveries(
        project_id: int | None = None,
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        statement = select(Discovery).order_by(Discovery.last_active_at.desc(), Discovery.id.desc())
        if project_id is not None:
            statement = statement.where(Discovery.project_id == project_id)
        discoveries = (await session.scalars(statement)).unique().all()
        return [await _discovery_view(session, item, detail=False) for item in discoveries]

    @api.get("/discoveries/{discovery_id}")
    async def get_discovery(
        discovery_id: int, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        return await _discovery_view(session, await _discovery_or_404(session, discovery_id))

    @api.post("/discoveries/{discovery_id}/messages", status_code=status.HTTP_202_ACCEPTED)
    async def send_discovery_message(
        discovery_id: int,
        payload: DiscoveryMessageCreate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        discovery = await _discovery_or_404(session, discovery_id, lock=True)
        if discovery.status != "OPEN":
            raise HTTPException(409, "discovery is closed")
        sequence = await _next_discovery_sequence(session, discovery.id)
        message = DiscoveryMessage(
            discovery=discovery, sequence=sequence, role="user", content=payload.content
        )
        session.add(message)
        await session.flush()
        turn = DiscoveryTurn(discovery=discovery, input_message=message)
        discovery.last_active_at = datetime.now(UTC)
        session.add_all([turn, Event(discovery=discovery, type="discovery.message.queued", payload={})])
        await session.commit()
        return await _discovery_view(session, discovery)

    @api.post("/discoveries/{discovery_id}/stop")
    async def stop_discovery(
        discovery_id: int, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        discovery = await _discovery_or_404(session, discovery_id, lock=True)
        turn = await session.scalar(
            select(DiscoveryTurn)
            .where(DiscoveryTurn.discovery_id == discovery.id, DiscoveryTurn.status.in_(["QUEUED", "RUNNING"]))
            .order_by(DiscoveryTurn.id)
            .limit(1)
        )
        if turn is not None:
            if turn.status == "QUEUED":
                turn.status = "INTERRUPTED"
                turn.finished_at = datetime.now(UTC)
            else:
                turn.cancel_requested_at = datetime.now(UTC)
            session.add(Event(discovery=discovery, type="discovery.stop_requested", payload={"turn_id": turn.id}))
            await session.commit()
        return await _discovery_view(session, discovery)

    @api.post("/discoveries/{discovery_id}/tasks", status_code=status.HTTP_201_CREATED)
    async def create_discovery_tasks(
        discovery_id: int,
        payload: DiscoveryTaskCreate,
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        discovery = await _discovery_or_404(session, discovery_id, lock=True)
        proposals = [dict(item) for item in discovery.state.get("task_proposals", [])]
        selected = set(payload.proposal_ids)
        if selected and not selected.issubset({str(item.get("id")) for item in proposals}):
            raise HTTPException(422, "unknown task proposal")
        pending = [
            proposal
            for proposal in proposals
            if not proposal.get("created_task_id")
            and (not selected or str(proposal.get("id")) in selected)
        ]
        planned: list[PlanPayload] = []
        for proposal in pending:
            try:
                planned.append(
                    PlanPayload.model_validate(
                        {
                            "brief_markdown": proposal.get("brief_markdown"),
                            "plan_markdown": proposal.get("plan_markdown"),
                            "metadata": proposal.get("metadata"),
                        }
                    )
                )
            except ValueError as error:
                raise HTTPException(
                    409,
                    f"task proposal '{proposal.get('title', 'untitled')}' is not fully planned",
                ) from error
        created: list[Task] = []
        for proposal, approved_plan in zip(pending, planned, strict=True):
            try:
                task = await create_task_record(
                    session,
                    discovery.project_id,
                    str(proposal["title"]),
                    str(proposal["megaprompt"]),
                    created_source="discovery",
                )
            except (KeyError, TaskCreationError) as error:
                if isinstance(error, TaskCreationError):
                    raise HTTPException(error.status_code, error.detail) from error
                raise HTTPException(422, "invalid task proposal") from error
            task.status, task.stage = transition(task.status, task.stage, "plan")
            task.status, task.stage = transition(task.status, task.stage, "briefed")
            task.status, task.stage = transition(task.status, task.stage, "planned")
            task.status, task.stage = transition(task.status, task.stage, "approve")
            task.approved_plan_revision = 1
            task.version += 1
            now = datetime.now(UTC)
            session.add_all(
                [
                    PlanRevision(
                        task=task,
                        revision=1,
                        brief_markdown=approved_plan.brief_markdown,
                        plan_markdown=approved_plan.plan_markdown,
                        metadata_json=approved_plan.metadata.model_dump(),
                        approved_at=now,
                    ),
                    Event(task=task, type="planning.completed", payload={"revision": 1}),
                    Event(task=task, type="plan.approved", payload={"revision": 1}),
                ]
            )
            proposal["created_task_id"] = task.id
            created.append(task)
        discovery.state = {**discovery.state, "task_proposals": proposals}
        session.add(Event(discovery=discovery, type="discovery.tasks.created", payload={"task_ids": [task.id for task in created]}))
        await session.commit()
        return [await _task_view(session, task) for task in created]

    @api.post("/discoveries/{discovery_id}/close")
    async def close_discovery(
        discovery_id: int, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        discovery = await _discovery_or_404(session, discovery_id, lock=True)
        if discovery.status == "OPEN":
            now = datetime.now(UTC)
            discovery.status = "CLOSED"
            discovery.closed_at = now
            discovery.final_summary = str(discovery.state.get("summary") or "Discovery closed.")
            turns = (
                await session.scalars(
                    select(DiscoveryTurn).where(
                        DiscoveryTurn.discovery_id == discovery.id,
                        DiscoveryTurn.status.in_(["QUEUED", "RUNNING"]),
                    )
                )
            ).all()
            for turn in turns:
                turn.status = "INTERRUPTED"
                turn.finished_at = now
            session.add(Event(discovery=discovery, type="discovery.closed", payload={}))
            await session.commit()
        return await _discovery_view(session, discovery)

    @api.get("/tasks")
    async def list_tasks(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        tasks = (
            await session.scalars(
                select(Task).order_by(Task.priority.desc(), Task.created_at, Task.id)
            )
        ).unique().all()
        return [await _task_view(session, task) for task in tasks]

    @api.get("/tasks/{task_id}")
    async def get_task(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        return await _task_view(
            session, await _task_or_404(session, task_id), detail=True
        )

    @api.get("/events")
    async def list_events(
        after: int = 0, session: AsyncSession = Depends(get_session)
    ) -> list[dict[str, Any]]:
        events = (
            await session.scalars(
                select(Event)
                .where(Event.sequence > after)
                .order_by(Event.sequence)
                .limit(200)
            )
        ).all()
        return [_event_view(event) for event in events]

    @app.websocket("/api/ws")
    async def events_socket(websocket: WebSocket, after: int = 0) -> None:
        user = await resolve_session(
            session_factory, websocket.cookies.get(SESSION_COOKIE, "")
        )
        if user is None or user.role != "admin":
            await websocket.close(code=4401)
            return
        await websocket.accept()
        sequence = after
        try:
            while True:
                async with session_factory() as session:
                    events = (
                        await session.scalars(
                            select(Event)
                            .where(Event.sequence > sequence)
                            .order_by(Event.sequence)
                            .limit(200)
                        )
                    ).all()
                if not events:
                    await asyncio.sleep(0.5)
                    continue
                for event in events:
                    await websocket.send_json(_event_view(event))
                    sequence = event.sequence
        except WebSocketDisconnect:
            return

    async def continue_planning(
        task: Task, session: AsyncSession, instruction: str
    ) -> dict[str, Any]:
        profile = await _profile(session, "plan")
        try:
            provider_profile = await resolve_agent_profile(
                session,
                profile,
                credential_cipher,
                task_model_id=task.available_model_id,
            )
        except ModelProviderError as error:
            raise HTTPException(409, str(error)) from error
        if "carlo-planning" in provider_profile.skills:
            instruction = f"/skill:carlo-planning {instruction}"
        session_id = task.planning_session_id or f"{task.id}-plan"
        task.planning_session_id = session_id
        await session.commit()
        drafting_announced = False

        async def publish(provider_event: dict[str, Any]) -> None:
            nonlocal drafting_announced
            activity = _planning_activity(provider_event)
            if activity is None or (activity[0] == "planning.drafting" and drafting_announced):
                return
            drafting_announced |= activity[0] == "planning.drafting"
            session.add(Event(task=task, type=activity[0], payload=activity[1]))
            await session.commit()

        try:
            result = await provider.run(
                provider_profile,
                instruction,
                task.project.repository_path,
                session_id,
                on_event=publish,
            )
            raw = json.loads(result.output)
        except (ProviderError, ValueError) as error:
            session.add(
                Event(task=task, type="planning.failed", payload={"error": str(error)})
            )
            await session.commit()
            raise HTTPException(502, "planning provider failed") from error

        if result.used_skills:
            session.add(
                Event(
                    task=task,
                    type="planning.skills_used",
                    payload={"skills": list(result.used_skills)},
                )
            )
            await session.commit()

        question = raw.get("question") if isinstance(raw, dict) else None
        if isinstance(question, str) and question.strip():
            task.planning_question = {"text": question.strip()}
            task.version += 1
            session.add(Event(task=task, type="planning.question", payload=task.planning_question))
            await session.commit()
            return await _task_view(session, task, detail=True)
        try:
            output = PlanPayload.model_validate(raw)
        except ValueError as error:
            session.add(Event(task=task, type="planning.failed", payload={"error": str(error)}))
            await session.commit()
            raise HTTPException(502, "planning provider returned invalid output") from error

        revision = (
            await session.scalar(
                select(func.coalesce(func.max(PlanRevision.revision), 0) + 1).where(
                    PlanRevision.task_id == task.id
                )
            )
        ) or 1
        metadata = output.metadata.model_dump()
        metadata["planner_profile"] = {
            "name": profile.name,
            "provider": profile.provider,
            "model": provider_profile.model,
            "effort": provider_profile.effort,
            "tools": list(provider_profile.tools),
        }
        runtime_evidence = model_runtime_evidence(provider_profile)
        if runtime_evidence:
            metadata["model_runtime"] = runtime_evidence
        plan = PlanRevision(
            task=task,
            revision=revision,
            brief_markdown=output.brief_markdown,
            plan_markdown=output.plan_markdown,
            metadata_json=metadata,
        )
        task.status, task.stage = transition(task.status, task.stage, "planned")
        task.planning_question = None
        task.version += 1
        session.add_all(
            [plan, Event(task=task, type="planning.completed", payload={"revision": revision})]
        )
        await session.commit()
        return await _task_view(session, task, detail=True)

    @api.post("/tasks/{task_id}/plan")
    async def plan_task(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        task = await _task_or_404(session, task_id)
        try:
            task.status, task.stage = transition(task.status, task.stage, "plan")
            task.status, task.stage = transition(task.status, task.stage, "briefed")
        except InvalidTransition as error:
            raise HTTPException(409, str(error)) from error
        task.version += 1
        task.planning_session_id = f"{task.id}-plan"
        session.add(Event(task=task, type="planning.started", payload={}))
        await session.commit()
        return await continue_planning(task, session, _planning_instruction(task))

    @api.post("/tasks/{task_id}/plan/answer")
    async def answer_planning_question(
        task_id: str,
        payload: PlanningAnswer,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        task = await _task_or_404(session, task_id)
        if task.stage != TaskStage.PLANNING or not task.planning_question:
            raise HTTPException(409, "task is not waiting for a planning answer")
        question = task.planning_question["text"]
        task.planning_question = None
        task.version += 1
        session.add(Event(task=task, type="planning.answer", payload={"question": question}))
        await session.commit()
        return await continue_planning(
            task,
            session,
            f"The user answered your planning question.\nQuestion: {question}\nAnswer: {payload.answer}\nContinue repository analysis. Ask one further high-impact question if necessary; otherwise return the complete plan JSON.",
        )

    @api.post("/tasks/{task_id}/rework")
    async def rework_task(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        task = await session.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            raise HTTPException(404, "task not found")
        try:
            task.status, task.stage = transition(task.status, task.stage, "rework")
            task.status, task.stage = transition(task.status, task.stage, "briefed")
        except InvalidTransition as error:
            raise HTTPException(409, str(error)) from error
        cycle = int(
            await session.scalar(
                select(func.count(Event.sequence)).where(
                    Event.task_id == task.id, Event.type == "task.rework.started"
                )
            )
            or 0
        ) + 1
        previous_attempt = int(
            await session.scalar(
                select(func.coalesce(func.max(Attempt.number), 0)).where(
                    Attempt.task_id == task.id
                )
            )
            or 0
        )
        history = {
            "cycle": cycle,
            "previous_attempt": previous_attempt,
            "previous_branch": task.branch_name,
            "previous_worktree": task.worktree_path,
            "previous_checkpoint": task.checkpoint_sha,
        }
        task.approved_plan_revision = None
        task.active_profile_id = None
        task.branch_name = None
        task.worktree_path = None
        task.checkpoint_sha = None
        task.planning_cursor = None
        task.planning_question = None
        task.planning_session_id = f"{task.id}-plan-rework-{cycle}"
        task.version += 1
        session.add(Event(task=task, type="task.rework.started", payload=history))
        await session.commit()
        try:
            return await continue_planning(
                task,
                session,
                _planning_instruction(task, fresh_rework=True),
            )
        except HTTPException:
            task.status = TaskStatus.FAILED
            task.stage = TaskStage.BLOCKED
            task.version += 1
            session.add(
                Event(task=task, type="task.rework.planning_failed", payload={"cycle": cycle})
            )
            await session.commit()
            raise

    @api.post("/tasks/{task_id}/approve")
    async def approve_plan(
        task_id: str,
        payload: Approval,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        task = await _task_or_404(session, task_id)
        if task.version != payload.version:
            raise HTTPException(409, "task changed; refresh before approving")
        plan = await session.scalar(
            select(PlanRevision).where(
                PlanRevision.task_id == task_id,
                PlanRevision.revision == payload.revision,
            )
        )
        if plan is None:
            raise HTTPException(404, "plan revision not found")
        latest_revision = await session.scalar(
            select(func.max(PlanRevision.revision)).where(
                PlanRevision.task_id == task_id
            )
        )
        if payload.revision != latest_revision:
            raise HTTPException(409, "only the latest plan revision can be approved")
        try:
            action = (
                "approve_amendment"
                if task.status == TaskStatus.IN_PROGRESS
                and task.stage == TaskStage.BLOCKED
                else "approve"
            )
            task.status, task.stage = transition(task.status, task.stage, action)
        except InvalidTransition as error:
            raise HTTPException(409, str(error)) from error
        task.approved_plan_revision = payload.revision
        task.version += 1
        plan.approved_at = datetime.now(UTC)
        session.add(
            Event(task=task, type="plan.approved", payload={"revision": payload.revision})
        )
        await session.commit()
        return await _task_view(session, task)

    app.include_router(api)

    dist = Path(settings.frontend_dist).resolve()
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        index = dist / "index.html"
        if path.startswith("api/") or not index.is_file():
            raise HTTPException(404)
        candidate = (dist / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(index)

    return app


async def _is_git_repository(path: str) -> bool:
    if not Path(path).is_dir():
        return False
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        path,
        "rev-parse",
        "--is-inside-work-tree",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait() == 0


async def _task_or_404(session: AsyncSession, task_id: str) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


async def _discovery_or_404(
    session: AsyncSession, discovery_id: int, *, lock: bool = False
) -> Discovery:
    statement = select(Discovery).where(Discovery.id == discovery_id)
    if lock:
        statement = statement.with_for_update()
    discovery = await session.scalar(statement)
    if discovery is None:
        raise HTTPException(404, "discovery not found")
    return discovery


def _empty_discovery_state() -> dict[str, Any]:
    return {
        "summary": "",
        "findings": [],
        "decisions": [],
        "unresolved_questions": [],
        "inspected_resources": [],
        "commands": [],
        "task_proposals": [],
    }


async def _next_discovery_sequence(session: AsyncSession, discovery_id: int) -> int:
    return int(
        await session.scalar(
            select(func.coalesce(func.max(DiscoveryMessage.sequence), 0) + 1).where(
                DiscoveryMessage.discovery_id == discovery_id
            )
        )
        or 1
    )


async def _action_run_or_404(session: AsyncSession, run_id: int) -> ActionRun:
    run = await session.get(ActionRun, run_id)
    if run is None:
        raise HTTPException(404, "action run not found")
    return run


async def _profile(session: AsyncSession, name: str) -> AgentProfileRecord:
    profile = await session.scalar(
        select(AgentProfileRecord).where(AgentProfileRecord.name == name)
    )
    if profile is None:
        source = None
        if name == "discovery":
            source = await session.scalar(
                select(AgentProfileRecord).where(AgentProfileRecord.name == "plan")
            )
        profile = AgentProfileRecord(
            name=name,
            provider=source.provider if source else "pi",
            model=source.model if source else None,
            effort=source.effort if source else None,
            permissions={"tools": ["read", "bash", "grep", "find", "ls", "discovery_state"]}
            if name == "discovery"
            else {},
            default_skills=["carlo-planning"] if name == "plan" else ["carlo-discovery"] if name == "discovery" else [],
        )
        session.add(profile)
        await session.flush()
    return profile


def _planning_instruction(task: Task, *, fresh_rework: bool = False) -> str:
    contract = {
        "brief_markdown": "evidence-oriented repository understanding",
        "plan_markdown": "concrete implementation plan",
        "metadata": {
            "title": "short implementation title",
            "description": "concise description of the approved approach",
            "key_points": [],
            "implementation_tasks": [
                {
                    "title": "concise task title",
                    "prompt": "self-contained instruction for the implementation agent",
                    "intervention_points": [],
                }
            ],
            "skills": [],
            "implementation_phases": [],
            "validation_commands": [],
            "browser_validation": False,
            "build_required": False,
            "run_required": False,
            "deployment_expected": False,
            "risk_flags": [],
            "affected_areas": [],
        },
    }
    rework = (
        "This is a fresh rework. Start again from the original request and current "
        "repository; do not continue the failed implementation or treat its old plan "
        "as authoritative.\n"
        if fresh_rework
        else ""
    )
    prompt = f"Original Markdown request: {task.prompt_path}\n" if task.prompt_path else ""
    return (
        rework + f"Inspect the repository and plan task {task.id}: {task.goal}\n"
        f"{prompt}"
        f"Project policies: {json.dumps(task.project.policies)}\n"
        "If one high-impact answer is still required, return only "
        '{"question":"the single focused question"}. Ask no low-risk implementation questions. '
        "Return only JSON matching this shape:\n"
        f"{json.dumps(contract)}"
    )


def _planning_activity(
    event: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    kind = event.get("type")
    if kind == "agent_start":
        return "planning.exploring", {}
    if kind in {"tool_execution_start", "tool_execution_end"}:
        tool = str(event.get("toolName") or event.get("tool") or "tool")[:80]
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        detail = next(
            (str(args[key]) for key in ("path", "query", "command") if key in args),
            "",
        )[:500]
        payload: dict[str, Any] = {"tool": tool, "detail": detail}
        if kind == "tool_execution_end":
            payload["failed"] = bool(event.get("isError"))
        event_type = (
            "planning.tool.started"
            if kind == "tool_execution_start"
            else "planning.tool.completed"
        )
        return event_type, payload
    if kind == "message_update":
        return "planning.drafting", {}
    return None


def _project_view(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "key": project.key,
        "repository_path": project.repository_path,
        "default_branch": project.default_branch,
        "integration_branch": project.integration_branch,
        "validation_commands": project.validation_commands,
    }


async def _check_runner(transport: SshTransport, runner: Runner) -> None:
    runner.last_checked_at = datetime.now(UTC)
    try:
        await transport.check(runner)
    except SshError as error:
        runner.last_check_ok = False
        runner.last_check_error = str(error)[:500]
    else:
        runner.last_check_ok = True
        runner.last_check_error = None


def _runner_view(runner: Runner) -> dict[str, Any]:
    return {
        "id": runner.id,
        "name": runner.name,
        "type": "ssh",
        "host": runner.host,
        "port": runner.port,
        "username": runner.username,
        "identity_file": runner.identity_file,
        "workspace_root": runner.workspace_root,
        "fingerprint": runner.fingerprint,
        "pending_fingerprint": (
            runner.fingerprint if runner.last_checked_at is None else None
        ),
        "pending_key_type": (
            runner.host_key.split()[1]
            if runner.host_key and runner.last_checked_at is None
            else None
        ),
        "enabled": runner.enabled,
        "last_check_ok": runner.last_check_ok,
        "last_check_error": runner.last_check_error,
        "last_checked_at": (
            runner.last_checked_at.isoformat() if runner.last_checked_at else None
        ),
        "created_at": runner.created_at.isoformat(),
        "updated_at": runner.updated_at.isoformat(),
    }


def _profile_view(profile: AgentProfileRecord) -> dict[str, Any]:
    return {
        "name": profile.name,
        "provider": profile.provider,
        "model": profile.model,
        "effort": profile.effort,
        "permissions": profile.permissions,
        "default_skills": profile.default_skills,
        "context_policy": profile.context_policy,
        "active": profile.active,
        "model_provider_id": profile.model_provider_id,
        "available_model_id": profile.available_model_id,
    }


def _model_provider_view(provider: ModelProvider) -> dict[str, Any]:
    return {
        "id": provider.id,
        "name": provider.name,
        "slug": provider.slug,
        "kind": provider.kind,
        "base_url": provider.base_url,
        "credential_configured": provider.credential_ciphertext is not None,
        "credential_hint": provider.credential_hint,
        "compatibility": provider.compatibility,
        "refresh_interval_minutes": provider.refresh_interval_minutes,
        "last_refresh_attempt_at": provider.last_refresh_attempt_at.isoformat()
        if provider.last_refresh_attempt_at
        else None,
        "last_refresh_success_at": provider.last_refresh_success_at.isoformat()
        if provider.last_refresh_success_at
        else None,
        "last_refresh_status": provider.last_refresh_status,
        "last_refresh_error": provider.last_refresh_error,
        "default_model_id": provider.default_model_id,
        "active": provider.active,
        "created_at": provider.created_at.isoformat(),
        "updated_at": provider.updated_at.isoformat(),
    }


async def _pi_settings(session: AsyncSession) -> PiRuntimeSettings:
    settings = await session.get(PiRuntimeSettings, 1)
    if settings is None:
        settings = PiRuntimeSettings(id=1)
        session.add(settings)
        await session.flush()
    return settings


def _pi_settings_view(settings: PiRuntimeSettings) -> dict[str, Any]:
    return {
        "compaction_enabled": settings.compaction_enabled,
        "reserve_percent": settings.reserve_percent,
        "keep_recent_percent": settings.keep_recent_percent,
    }


def _validate_model_limits(model: AvailableModel) -> None:
    context_window = model.effective_context_window
    max_tokens = model.effective_max_tokens
    if context_window is not None and max_tokens is not None and max_tokens >= context_window:
        raise HTTPException(422, "max tokens must be smaller than context window")


async def _selectable_model(
    session: AsyncSession, model_id: int
) -> AvailableModel:
    model = await session.get(AvailableModel, model_id)
    if model is None:
        raise HTTPException(404, "model not found")
    if (
        model.status != "AVAILABLE"
        or model.effective_context_window is None
        or model.effective_max_tokens is None
    ):
        raise HTTPException(409, "model is not selectable")
    _validate_model_limits(model)
    return model


def _available_model_view(
    model: AvailableModel,
    provider: ModelProvider | None,
    settings: PiRuntimeSettings,
) -> dict[str, Any]:
    context_window = model.effective_context_window
    max_tokens = model.effective_max_tokens
    compaction = (
        calculate_compaction(
            context_window,
            max_tokens,
            settings.reserve_percent,
            settings.keep_recent_percent,
        )
        if context_window is not None and max_tokens is not None
        else None
    )
    return {
        "id": model.id,
        "model_provider_id": model.model_provider_id,
        "model_provider_name": provider.name if provider else None,
        "external_id": model.external_id,
        "display_name": model.display_name,
        "status": model.status,
        "discovered_context_window": model.discovered_context_window,
        "discovered_max_tokens": model.discovered_max_tokens,
        "context_window_override": model.context_window_override,
        "max_tokens_override": model.max_tokens_override,
        "effective_context_window": context_window,
        "effective_max_tokens": max_tokens,
        "reserve_tokens": compaction.reserve_tokens if compaction else None,
        "keep_recent_tokens": compaction.keep_recent_tokens if compaction else None,
        "input_modalities": model.input_modalities,
        "reasoning": model.reasoning,
        "selectable": model.status == "AVAILABLE"
        and context_window is not None
        and max_tokens is not None,
        "last_seen_at": model.last_seen_at.isoformat(),
    }


def _user_view(user: User) -> dict[str, str]:
    return {"username": user.username, "role": user.role}


async def _task_view(
    session: AsyncSession, task: Task, detail: bool = False
) -> dict[str, Any]:
    plan = await session.scalar(
        select(PlanRevision)
        .where(PlanRevision.task_id == task.id)
        .order_by(PlanRevision.revision.desc())
        .limit(1)
    )
    view = {
        "id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "goal": task.goal,
        "prompt_path": task.prompt_path,
        "status": task.status.value,
        "stage": task.stage.value,
        "priority": task.priority,
        "version": task.version,
        "approved_plan_revision": task.approved_plan_revision,
        "branch_name": task.branch_name,
        "worktree_path": task.worktree_path,
        "checkpoint_sha": task.checkpoint_sha,
        "available_model_id": task.available_model_id,
        "planning_question": task.planning_question,
        "used_skills": [],
        "plan": None
        if plan is None
        else {
            "revision": plan.revision,
            "brief_markdown": plan.brief_markdown,
            "plan_markdown": plan.plan_markdown,
            "metadata": plan.metadata_json,
            "approved_at": plan.approved_at,
        },
    }
    if not detail:
        return view
    skill_events = (
        await session.scalars(
            select(Event)
            .where(
                Event.task_id == task.id,
                Event.type.in_(("planning.skills_used", "agent.completed")),
            )
            .order_by(Event.sequence)
        )
    ).all()
    used_skills: list[str] = []
    for event in skill_events:
        values = event.payload.get("skills")
        if isinstance(values, list):
            used_skills.extend(skill for skill in values if isinstance(skill, str))
    view["used_skills"] = list(dict.fromkeys(used_skills))
    attempts = (
        await session.scalars(
            select(Attempt)
            .where(Attempt.task_id == task.id)
            .order_by(Attempt.number.desc())
        )
    ).all()
    validations = (
        await session.scalars(
            select(ValidationRun)
            .where(ValidationRun.task_id == task.id)
            .order_by(ValidationRun.created_at.desc())
        )
    ).all()
    escalations = (
        await session.scalars(
            select(Escalation)
            .where(Escalation.task_id == task.id)
            .order_by(Escalation.created_at.desc())
        )
    ).all()
    events = (
        await session.scalars(
            select(Event)
            .where(Event.task_id == task.id)
            .order_by(Event.sequence.desc())
            .limit(200)
        )
    ).all()
    view.update(
        attempts=[
            {
                "number": attempt.number,
                "outcome": attempt.outcome,
                "error_fingerprint": attempt.error_fingerprint,
                "progress": attempt.progress,
                "artifact_path": attempt.artifact_path,
                "created_at": attempt.created_at.isoformat(),
            }
            for attempt in attempts
        ],
        validations=[
            {
                "command": validation.command,
                "exit_code": validation.exit_code,
                "classification": validation.classification,
                "summary": validation.summary,
                "failure_count": validation.failure_count,
                "artifact_path": validation.artifact_path,
                "created_at": validation.created_at.isoformat(),
            }
            for validation in validations
        ],
        escalations=[
            {
                "reason": escalation.reason,
                "status": escalation.status,
                "diagnosis": escalation.diagnosis,
                "strategy": escalation.strategy,
                "created_at": escalation.created_at.isoformat(),
            }
            for escalation in escalations
        ],
        events=[_event_view(event) for event in events],
    )
    return view


def _event_view(event: Event) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "task_id": event.task_id,
        "discovery_id": event.discovery_id,
        "type": event.type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


async def _discovery_view(
    session: AsyncSession, discovery: Discovery, detail: bool = True
) -> dict[str, Any]:
    current_turn = await session.scalar(
        select(DiscoveryTurn)
        .where(DiscoveryTurn.discovery_id == discovery.id)
        .order_by(DiscoveryTurn.id.desc())
        .limit(1)
    )
    view: dict[str, Any] = {
        "id": discovery.id,
        "project_id": discovery.project_id,
        "title": discovery.title,
        "status": discovery.status,
        "state": discovery.state,
        "final_summary": discovery.final_summary,
        "last_active_at": discovery.last_active_at.isoformat(),
        "closed_at": discovery.closed_at.isoformat() if discovery.closed_at else None,
        "current_turn": None if current_turn is None else {
            "id": current_turn.id,
            "status": current_turn.status,
            "kind": current_turn.kind,
            "cancel_requested_at": current_turn.cancel_requested_at.isoformat() if current_turn.cancel_requested_at else None,
            "error": current_turn.error,
        },
    }
    if detail:
        messages = (
            await session.scalars(
                select(DiscoveryMessage)
                .where(DiscoveryMessage.discovery_id == discovery.id)
                .order_by(DiscoveryMessage.sequence)
            )
        ).all()
        view["messages"] = [
            {
                "id": message.id,
                "sequence": message.sequence,
                "role": message.role,
                "content": message.content,
                "metadata": message.metadata_json,
                "created_at": message.created_at.isoformat(),
            }
            for message in messages
        ]
    return view


def _action_run_view(run: ActionRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "action_key": run.action_key,
        "action_name": run.action_name,
        "definition": run.definition,
        "runner_name": run.runner_name,
        "runner_snapshot": run.runner_snapshot,
        "status": run.status,
        "internal_stage": run.internal_stage,
        "commit_sha": run.commit_sha,
        "branch_name": run.branch_name,
        "origin": run.origin,
        "env_file": run.env_file,
        "env_names": run.env_names,
        "requested_at": run.requested_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "cancel_requested_at": (
            run.cancel_requested_at.isoformat() if run.cancel_requested_at else None
        ),
        "current_step": run.current_step,
        "workspace_path": run.workspace_path,
        "artifact_path": run.artifact_path,
        "secret_path": run.secret_path,
        "log_offset": run.log_offset,
        "recent_output": run.recent_output,
        "exit_code": run.exit_code,
        "error": run.error,
        "cleanup_pending": run.cleanup_pending,
        "steps": [
            {
                "position": step.position,
                "command": step.command,
                "status": step.status,
                "started_at": step.started_at.isoformat() if step.started_at else None,
                "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                "exit_code": step.exit_code,
                "log_start": step.log_start,
                "log_end": step.log_end,
            }
            for step in run.steps
        ],
    }
