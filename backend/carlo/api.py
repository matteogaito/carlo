import asyncio
import hmac
import json
import os
import re
import shutil
from uuid import uuid4
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import delete, func, or_, select
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
    REQUIRED_PROFILE_SKILLS,
    available_profile_packages,
    available_profile_skills,
    calculate_compaction,
    model_runtime_evidence,
    refresh_model_provider,
    resolve_agent_profile,
    validate_runtime_policy,
)
from .domain import InvalidTransition, TaskStage, TaskStatus, transition
from .git import parent_branch_name
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
    PiPackage,
    AgentProfilePackage,
    Runner,
    Task,
    User,
    ValidationRun,
)
from .provider import AgentProfile, CodingAgentProvider, ProviderError
from .pi_packages import PiPackageError, PiPackageManager, parse_package_source
from .ssh import HostScan, SshError, SshTransport, validate_runner
from .telegram import TelegramNotifier, TelegramTransport, telegram_enabled


from .tasks import TaskCreationError, create_task as create_task_record
from .planning import (
    ImplementationTask,
    PlanMetadata,
    PlanPayload,
    Planner,
    PlanningError,
    PlanningQuestion,
    PlanningRequest,
    planning_instruction,
    proposal_source,
)


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


class TaskReorder(BaseModel):
    task_ids: list[str] = Field(min_length=1)


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
    effort: str | None = None
    permissions: dict[str, Any] | None = None
    default_skills: list[str] | None = None
    default_packages: list[str] | None = None
    context_policy: dict[str, Any] | None = None
    active: bool | None = None
    available_model_id: int | None = None


class ModelProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")
    kind: str = Field(default="openai-compatible", pattern=r"^openai-compatible$")
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, min_length=1, max_length=16_384)
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
    default_packages: list[str] | None = None
    default_skills: list[str] | None = None


class PiPackageCreate(BaseModel):
    source: str = Field(min_length=1, max_length=2048)
    is_default: bool = False


class PiPackageUpdate(BaseModel):
    enabled: bool | None = None
    is_default: bool | None = None


class TaskModelUpdate(BaseModel):
    available_model_id: int | None = None


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
                    or_(
                        Task.planning_session_id.like("%-plan-rework-%"),
                        Task.planning_session_id.like("%-replan-%"),
                    ),
                    Task.planning_question.is_(None),
                )
            )
        ).all()
        for task in tasks:
            if task.planning_session_id and "-replan-" in task.planning_session_id:
                await _restore_replan(
                    session, task, "task.replan.recovered_after_restart"
                )
                continue
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


async def _restore_replan(
    session: AsyncSession,
    task: Task,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    started = await session.scalar(
        select(Event)
        .where(Event.task_id == task.id, Event.type == "task.replan.started")
        .order_by(Event.sequence.desc())
        .limit(1)
    )
    try:
        task.status = TaskStatus(started.payload["previous_status"])
        task.stage = TaskStage(started.payload["previous_stage"])
    except (AttributeError, KeyError, ValueError):
        task.status, task.stage = TaskStatus.FAILED, TaskStage.BLOCKED
    task.version += 1
    session.add(Event(task=task, type=event_type, payload=payload or {}))


async def _active_children(
    session: AsyncSession, task_id: str, *, lock: bool = False
) -> list[Task]:
    statement = (
        select(Task)
        .where(Task.parent_task_id == task_id, Task.superseded_at.is_(None))
        .order_by(Task.subtask_position)
    )
    if lock:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


async def _task_delete_blocker(session: AsyncSession, task: Task, children: list[Task]) -> str | None:
    if task.parent_task_id is not None:
        return "only parent Tasks can be deleted"
    if any(item.status in {TaskStatus.IN_PROGRESS, TaskStatus.TEST} or item.stage == TaskStage.PLANNING for item in [task, *children]):
        return "stop active Task work before deleting it"
    ids = {item.id for item in [task, *children]}
    dependent = await session.scalar(select(Task.id).where(
        Task.id.not_in(ids),
        or_(*(Task.depends_on_task_ids.contains([item_id]) for item_id in ids)),
    ).limit(1))
    if dependent:
        return "another Task depends on this Task or one of its subtasks"
    return None


async def _materialize_plan_subtasks(
    session: AsyncSession,
    task: Task,
    plan: PlanRevision,
    items: list[dict[str, Any]],
    *,
    start_position: int = 0,
) -> list[Task]:
    project = await session.scalar(
        select(Project).where(Project.id == task.project_id).with_for_update()
    )
    if project is None:
        raise HTTPException(404, "project not found")
    children: list[Task] = []
    for position, item in enumerate(items, start=start_position):
        sequence = project.next_task_sequence
        project.next_task_sequence += 1
        child = Task(
            id=f"{project.key}-{sequence}",
            project=project,
            sequence=sequence,
            title=str(item["title"]),
            goal=str(item["objective"]),
            priority=task.priority,
            created_source="plan",
            status=TaskStatus.READY,
            stage=TaskStage.QUEUED,
            approved_plan_revision=1,
            available_model_id=task.available_model_id,
            parent=task,
            subtask_position=position,
        )
        child_metadata = {
            **plan.metadata_json,
            "replan": False,
            "title": str(item["title"]),
            "description": str(item["objective"]),
            "implementation_tasks": [item],
            "implementation_phases": [str(item["title"])],
        }
        session.add_all(
            [
                child,
                PlanRevision(
                    task=child,
                    revision=1,
                    brief_markdown=plan.brief_markdown,
                    plan_markdown=str(item["objective"]),
                    metadata_json=child_metadata,
                    approved_at=plan.approved_at,
                ),
                Event(task=child, type="task.created", payload={"source": "plan"}),
                Event(task=child, type="plan.approved", payload={"revision": 1}),
            ]
        )
        children.append(child)
    task.status = TaskStatus.IN_PROGRESS
    task.stage = TaskStage.IMPLEMENTING
    return children


async def approve_plan_revision(
    session: AsyncSession,
    task: Task,
    plan: PlanRevision,
    *,
    existing: list[Task] | None = None,
    is_replan: bool = False,
) -> list[Task]:
    """Approve a stored plan and materialize its children for either entry route."""
    action = (
        "approve_amendment" if task.status == TaskStatus.IN_PROGRESS and task.stage == TaskStage.BLOCKED
        else "approve_fix" if task.status == TaskStatus.FAILED and task.stage == TaskStage.BLOCKED
        else "approve"
    )
    task.status, task.stage = transition(task.status, task.stage, action)
    task.approved_plan_revision = plan.revision
    task.version += 1
    plan.approved_at = datetime.now(UTC)
    children: list[Task] = []
    old_children: list[Task] = []
    items = plan.metadata_json.get("implementation_tasks") or []
    if task.parent_task_id is None and items:
        if existing and not is_replan:
            children = list(existing)
        else:
            if is_replan:
                old_children = list(existing or [])
                for child in old_children:
                    child.superseded_at = plan.approved_at
            children = await _materialize_plan_subtasks(session, task, plan, items)
    session.add(Event(task=task, type="plan.approved", payload={"revision": plan.revision}))
    if old_children:
        session.add(Event(task=task, type="subtasks.replanned", payload={
            "revision": plan.revision,
            "old_children": [child.id for child in old_children],
            "new_children": [child.id for child in children],
        }))
    elif children:
        session.add(Event(task=task, type="subtasks.created", payload={"children": [child.id for child in children]}))
    return children


def _proposal_implementation_tasks(
    proposal: dict[str, Any], metadata: dict[str, Any]
) -> list[dict[str, Any]]:
    items = metadata.get("implementation_tasks")
    return items if isinstance(items, list) else []


def validate_task_proposal(proposal: dict[str, Any], state: dict[str, Any] | None = None) -> tuple[PlanPayload | None, str | None]:
    """Validate a Discovery task_proposal the same way create_discovery_tasks
    does, without side effects — returns (plan, None) if valid, or (None,
    human-readable detail) if not. Shared so the check can run as soon as a
    proposal is produced, not only when a human clicks Create task.
    """
    try:
        if state is not None:
            if proposal.get("planning_error"):
                raise ValueError(str(proposal["planning_error"]))
            if proposal.get("draft_source") != proposal_source(proposal, state):
                raise ValueError("proposal plan is stale or pending")
            approved_plan = PlanPayload.model_validate(proposal.get("plan_draft"))
            if not approved_plan.metadata.implementation_tasks:
                raise ValueError("implementation_tasks must not be empty")
            return approved_plan, None
        metadata = dict(proposal.get("metadata") or {})
        metadata["implementation_tasks"] = _proposal_implementation_tasks(proposal, metadata)
        approved_plan = PlanPayload.model_validate(
            {
                "brief_markdown": proposal.get("brief_markdown"),
                "plan_markdown": proposal.get("plan_markdown"),
                "metadata": metadata,
            }
        )
        if not approved_plan.metadata.implementation_tasks:
            raise ValueError("implementation_tasks must not be empty")
        return approved_plan, None
    except ValueError as error:
        detail = (
            "; ".join(
                f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                for item in error.errors(include_input=False, include_url=False)[:5]
            )
            if isinstance(error, ValidationError) else str(error)
        )
        return None, detail


async def validate_fix_proposal(
    session: AsyncSession, task: Task, proposal: dict[str, Any]
) -> tuple[PlanPayload | None, str | None]:
    """Validate a task-rework fix_proposal the same way apply_discovery_fix
    applies it, without side effects — returns (plan, None) if valid, or
    (None, human-readable detail) otherwise. Shared so the check can run as
    soon as Pi proposes a fix, not only when the human clicks "Applica e
    riprendi".
    """
    action = proposal.get("action") if isinstance(proposal, dict) else None
    if action not in {"revise_task", "revise_parent"}:
        return None, "action must be revise_task or revise_parent"
    target = task
    if action == "revise_parent":
        if task.parent_task_id is None:
            return None, "task has no parent to revise"
        target = await session.get(Task, task.parent_task_id)
        if target is None:
            return None, "parent task not found"
    items = (
        [proposal.get("package")]
        if action == "revise_task"
        else list(proposal.get("packages") or [])
    )
    current_plan = (
        await session.scalar(
            select(PlanRevision).where(
                PlanRevision.task_id == target.id,
                PlanRevision.revision == target.approved_plan_revision,
            )
        )
        if target.approved_plan_revision
        else None
    )
    try:
        approved_plan = PlanPayload.model_validate(
            {
                "brief_markdown": proposal.get("brief_markdown"),
                "plan_markdown": proposal.get("plan_markdown"),
                "metadata": {
                    **(current_plan.metadata_json if current_plan else {}),
                    "implementation_tasks": items,
                    "replan": action == "revise_parent",
                },
            }
        )
        if not approved_plan.metadata.implementation_tasks:
            raise ValueError("fix proposal has no work packages")
        return approved_plan, None
    except ValueError as error:
        detail = (
            "; ".join(
                f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                for item in error.errors(include_input=False, include_url=False)[:5]
            )
            if isinstance(error, ValidationError) else str(error)
        )
        return None, detail


async def _queue_proposal_fix_request(
    session: AsyncSession, discovery: Discovery, proposal: dict[str, Any], detail: str | None
) -> bool:
    """When a user tries to create a task from an invalid proposal, ask Pi to
    fix it in the same Discovery instead of leaving the human to relay the
    validation error by hand. Returns whether a request was actually queued
    (a closed Discovery cannot accept a new turn).
    """
    content = (
        f"La proposta \"{proposal.get('title', 'senza titolo')}\" non è creabile: {detail}. "
        "Correggila e richiama discovery_state con lo stato completo aggiornato."
    )
    return await _queue_discovery_fix_request(session, discovery, content)


async def _queue_fix_proposal_fix_request(
    session: AsyncSession, discovery: Discovery, detail: str | None
) -> bool:
    """Same self-healing pattern as `_queue_proposal_fix_request`, for the
    task-rework chat: when "Applica e riprendi" fails validation, ask Pi to
    correct its `task_fix_proposal` instead of leaving the human to relay a
    Pydantic error by hand.
    """
    content = (
        f"La proposta di correzione non è applicabile: {detail}. "
        "Correggila e richiama task_fix_proposal con il pacchetto/i pacchetti completi e corretti."
    )
    return await _queue_discovery_fix_request(session, discovery, content)


async def _queue_discovery_fix_request(
    session: AsyncSession, discovery: Discovery, content: str
) -> bool:
    if discovery.status != "OPEN":
        return False
    sequence = max((message.sequence for message in discovery.messages), default=0) + 1
    message = DiscoveryMessage(discovery=discovery, sequence=sequence, role="system", content=content)
    session.add(message)
    await session.flush()
    session.add_all(
        [
            DiscoveryTurn(discovery=discovery, input_message=message),
            Event(discovery=discovery, type="discovery.message.queued", payload={}),
        ]
    )
    discovery.last_active_at = datetime.now(UTC)
    await session.commit()
    return True


def _replan_allowed(task: Task, children: list[Task]) -> bool:
    return bool(
        task.parent_task_id is None
        and children
        and (task.status, task.stage)
        in {
            (TaskStatus.IN_PROGRESS, TaskStage.IMPLEMENTING),
            (TaskStatus.IN_PROGRESS, TaskStage.BLOCKED),
            (TaskStatus.FAILED, TaskStage.BLOCKED),
            (TaskStatus.READY, TaskStage.QUEUED),
            (TaskStatus.NOT_READY, TaskStage.AWAITING_APPROVAL),
        }
        and all(
            child.status
            not in {TaskStatus.IN_PROGRESS, TaskStatus.TEST, TaskStatus.DONE}
            for child in children
        )
    )


def create_app(
    session_factory: async_sessionmaker[AsyncSession],
    provider: CodingAgentProvider,
    settings: Settings | None = None,
    ssh_transport: SshTransport | None = None,
    resource_bootstrap: Callable[[], Awaitable[None]] | None = None,
    package_manager: PiPackageManager | None = None,
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
        if resource_bootstrap is not None:
            await resource_bootstrap()
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

    async def require_diagnostics_token(request: Request) -> None:
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "diagnostics are only available on localhost")
        authorization = request.headers.get("authorization", "")
        expected = settings.diagnostics_token
        if (
            not expected
            or "CHANGE_ME" in expected
            or not authorization.startswith("Bearer ")
            or not hmac.compare_digest(authorization[7:], expected)
        ):
            raise HTTPException(401, "invalid diagnostics token")

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
    diagnostics = APIRouter(
        prefix="/api/diagnostics", dependencies=[Depends(require_diagnostics_token)]
    )

    @diagnostics.get("/tasks/{task_id}/events")
    async def diagnostic_task_events(
        task_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        await _task_or_404(session, task_id)
        events = (
            await session.scalars(
                select(Event)
                .where(Event.task_id == task_id, Event.sequence > after)
                .order_by(Event.sequence)
                .limit(limit)
            )
        ).all()
        return [_event_view(event) for event in events]

    @diagnostics.get("/tasks/{task_id}/logs")
    async def diagnostic_task_logs(
        task_id: str,
        tail: int = Query(default=16000, ge=1, le=1_000_000),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, list[dict[str, Any]]]:
        await _task_or_404(session, task_id)
        attempts = (
            await session.scalars(
                select(Attempt)
                .where(Attempt.task_id == task_id)
                .order_by(Attempt.number.desc())
            )
        ).all()
        validations = (
            await session.scalars(
                select(ValidationRun)
                .where(ValidationRun.task_id == task_id)
                .order_by(ValidationRun.created_at.desc())
            )
        ).all()
        root = Path(settings.artifact_root).resolve()
        return {
            "attempts": [
                {
                    "number": attempt.number,
                    "outcome": attempt.outcome,
                    "artifact_path": attempt.artifact_path,
                    "content": _artifact_tail(attempt.artifact_path, root, tail),
                }
                for attempt in attempts
            ],
            "validations": [
                {
                    "command": validation.command,
                    "exit_code": validation.exit_code,
                    "classification": validation.classification,
                    "artifact_path": validation.artifact_path,
                    "content": _artifact_tail(validation.artifact_path, root, tail),
                }
                for validation in validations
            ],
        }

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

    @api.get("/settings/skills")
    async def list_skills() -> list[dict[str, Any]]:
        return _skill_catalog(_pi_resource_revisions(settings))

    @api.get("/settings/packages")
    async def list_packages(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        packages = list(
            await session.scalars(
                select(PiPackage).where(PiPackage.enabled.is_(True)).order_by(PiPackage.identity)
            )
        )
        return [
            {"name": package.identity, "revision": package.active_version}
            for package in packages
        ]

    @api.get("/settings/pi-packages")
    async def list_pi_packages(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        packages = list(await session.scalars(select(PiPackage).order_by(PiPackage.identity)))
        return [_pi_package_view(package) for package in packages]

    @api.post("/settings/pi-packages", status_code=status.HTTP_201_CREATED)
    async def create_pi_package(
        payload: PiPackageCreate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        if package_manager is None:
            raise HTTPException(503, "Pi package management is unavailable")
        try:
            parsed = parse_package_source(payload.source)
        except PiPackageError as error:
            raise HTTPException(422, str(error)) from error
        if await session.scalar(select(PiPackage.id).where(PiPackage.identity == parsed.identity)):
            raise HTTPException(409, "Pi package identity already exists")
        try:
            installed = await package_manager.install(payload.source)
        except PiPackageError as error:
            raise HTTPException(502, str(error)) from error
        package = PiPackage(
            source=payload.source,
            identity=parsed.identity,
            enabled=True,
            pinned=parsed.pinned,
            is_default=payload.is_default,
            active_version=installed.resolved_version,
            active_artifact_path=installed.artifact_path,
            resources=installed.resources,
            last_update_attempt_at=datetime.now(UTC),
            last_update_success_at=datetime.now(UTC),
            last_update_status="SUCCESS",
        )
        try:
            session.add(package)
            await session.flush()
            session.add(Event(type="pi.package.created", payload={"package_id": package.id, "identity": package.identity}))
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(409, "Pi package identity already exists") from error
        return _pi_package_view(package)

    @api.patch("/settings/pi-packages/{package_id}")
    async def update_pi_package(
        package_id: int,
        payload: PiPackageUpdate,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        package = await session.get(PiPackage, package_id)
        if package is None:
            raise HTTPException(404, "Pi package not found")
        if payload.enabled is not None:
            package.enabled = payload.enabled
            if not payload.enabled:
                package.is_default = False
        if payload.is_default is not None:
            if payload.is_default and (
                not package.enabled
                or not package.active_artifact_path
                or not Path(package.active_artifact_path).is_dir()
            ):
                raise HTTPException(422, "only an installed enabled package can be default")
            package.is_default = payload.is_default
        session.add(Event(type="pi.package.updated", payload={"package_id": package.id, "fields": sorted(payload.model_fields_set)}))
        await session.commit()
        return _pi_package_view(package)

    @api.post("/settings/pi-packages/{package_id}/update")
    async def refresh_pi_package(
        package_id: int,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        if package_manager is None:
            raise HTTPException(503, "Pi package management is unavailable")
        package = await session.get(PiPackage, package_id)
        if package is None:
            raise HTTPException(404, "Pi package not found")
        try:
            installed = await package_manager.install(package.source)
        except PiPackageError as error:
            package.last_update_attempt_at = datetime.now(UTC)
            package.last_update_status = "FAILED"
            package.last_update_error = str(error)[-500:]
            await session.commit()
            raise HTTPException(502, str(error)) from error
        package.active_version = installed.resolved_version
        package.active_artifact_path = installed.artifact_path
        package.resources = installed.resources
        package.last_update_attempt_at = datetime.now(UTC)
        package.last_update_success_at = datetime.now(UTC)
        package.last_update_status = "SUCCESS"
        package.last_update_error = None
        session.add(Event(type="pi.package.refreshed", payload={"package_id": package.id, "version": package.active_version}))
        await session.commit()
        return _pi_package_view(package)

    @api.delete(
        "/settings/pi-packages/{package_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_pi_package(
        package_id: int,
        session: AsyncSession = Depends(get_session),
    ) -> Response:
        package = await session.get(PiPackage, package_id)
        if package is None:
            raise HTTPException(404, "Pi package not found")
        assigned = await session.scalar(
            select(AgentProfilePackage.package_id).where(
                AgentProfilePackage.package_id == package.id
            )
        )
        legacy_assignment = await session.scalar(
            select(AgentProfileRecord.id).where(
                AgentProfileRecord.default_packages.contains([package.identity])
            )
        )
        if assigned is not None or legacy_assignment is not None:
            raise HTTPException(409, "Pi package is assigned to an agent profile")
        runtime = await session.get(PiRuntimeSettings, 1)
        if runtime is not None:
            runtime.default_packages = [
                identity
                for identity in runtime.default_packages
                if identity != package.identity
            ]
        session.add(Event(type="pi.package.deleted", payload={"package_id": package.id, "identity": package.identity}))
        await session.delete(package)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
        selected_model_id = (
            payload.available_model_id
            if "available_model_id" in payload.model_fields_set
            else profile.available_model_id
        )
        if selected_model_id is not None:
            await _selectable_model(session, selected_model_id)
        if "context_policy" in payload.model_fields_set:
            if payload.context_policy is None:
                raise HTTPException(422, "context_policy cannot be null")
            try:
                validate_runtime_policy(payload.context_policy)
            except ModelProviderError as error:
                raise HTTPException(422, str(error)) from error
        if "default_packages" in payload.model_fields_set:
            if payload.default_packages is None:
                raise HTTPException(422, "default_packages cannot be null")
            package_rows = list(
                await session.scalars(
                    select(PiPackage).where(
                        PiPackage.enabled.is_(True),
                        PiPackage.identity.in_(payload.default_packages),
                    )
                )
            )
            unknown_packages = sorted(
                set(payload.default_packages) - {package.identity for package in package_rows}
            )
            if unknown_packages:
                raise HTTPException(
                    422, f"unknown packages: {', '.join(unknown_packages)}"
                )
            payload.default_packages = list(dict.fromkeys(payload.default_packages))
            await session.execute(
                delete(AgentProfilePackage).where(
                    AgentProfilePackage.agent_profile_id == profile.id
                )
            )
            session.add_all(
                AgentProfilePackage(agent_profile_id=profile.id, package_id=package.id)
                for package in package_rows
                if not package.is_default
            )
        if "default_skills" in payload.model_fields_set and payload.default_skills is None:
            raise HTTPException(422, "default_skills cannot be null")
        if payload.default_skills is not None:
            known = available_profile_skills()
            unknown = sorted(set(payload.default_skills) - known)
            if unknown:
                raise HTTPException(422, f"unknown skills: {', '.join(unknown)}")
            payload.default_skills = list(
                dict.fromkeys(
                    (*REQUIRED_PROFILE_SKILLS.get(name, ()), *payload.default_skills)
                )
            )
        for field in payload.model_fields_set:
            setattr(profile, field, getattr(payload, field))
        profile.default_skills = list(
            dict.fromkeys(
                (*REQUIRED_PROFILE_SKILLS.get(name, ()), *profile.default_skills)
            )
        )
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
        encrypted = None
        if payload.api_key:
            if credential_cipher is None:
                raise HTTPException(503, "credential encryption is not configured")
            encrypted = credential_cipher.encrypt(payload.api_key)
        provider = ModelProvider(
            name=payload.name,
            slug=payload.slug,
            kind=payload.kind,
            base_url=payload.base_url,
            credential_ciphertext=encrypted.ciphertext if encrypted else None,
            credential_nonce=encrypted.nonce if encrypted else None,
            credential_hint=f"…{payload.api_key[-4:]}" if payload.api_key else None,
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
        if "api_key" in payload.model_fields_set:
            if payload.api_key is None:
                raise HTTPException(422, "api_key cannot be null")
            if credential_cipher is None:
                raise HTTPException(503, "credential encryption is not configured")
            encrypted = credential_cipher.encrypt(payload.api_key)
            provider.credential_ciphertext = encrypted.ciphertext
            provider.credential_nonce = encrypted.nonce
            provider.credential_hint = f"…{payload.api_key[-4:]}"
        for field in payload.model_fields_set - {"api_key"}:
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
                AvailableModel.model_provider_id == provider_id
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
        for field, available in (
            ("default_packages", available_profile_packages()),
            ("default_skills", available_profile_skills()),
        ):
            if field not in payload.model_fields_set:
                continue
            value = getattr(payload, field)
            if value is None:
                raise HTTPException(422, f"{field} cannot be null")
            unknown = sorted(set(value) - available)
            if unknown:
                raise HTTPException(422, f"unknown {field}: {', '.join(unknown)}")
            setattr(payload, field, list(dict.fromkeys(value)))
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
        profile = await _profile(session, "plan")
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

    @api.post(
        "/discoveries/{discovery_id}/messages/attachments",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def send_discovery_message_with_attachments(
        discovery_id: int,
        content: str = Form(default="", max_length=1024 * 1024),
        files: list[UploadFile] = File(...),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        discovery = await _discovery_or_404(session, discovery_id, lock=True)
        if discovery.status != "OPEN":
            raise HTTPException(409, "discovery is closed")
        if not files or len(files) > 10:
            raise HTTPException(422, "attach between 1 and 10 files")

        allowed = {
            ".json", ".yaml", ".yml", ".png", ".jpg", ".jpeg",
            ".gif", ".webp", ".heic", ".heif",
        }
        folder = (
            Path(settings.artifact_root).resolve()
            / "discoveries"
            / str(discovery.id)
            / "attachments"
        )
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        attachments: list[dict[str, str]] = []
        try:
            for upload in files:
                name = re.sub(
                    r"[^A-Za-z0-9._-]", "_", Path(upload.filename or "file").name
                ) or "file"
                suffix = Path(name).suffix.lower()
                if suffix not in allowed:
                    raise HTTPException(422, f"unsupported attachment type: {suffix or name}")
                path = folder / f"{uuid4().hex}-{name}"
                saved.append(path)
                size = 0
                with path.open("xb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > 20 * 1024 * 1024:
                            raise HTTPException(422, f"attachment too large: {name}")
                        output.write(chunk)
                if not size:
                    raise HTTPException(422, f"empty attachment: {name}")
                attachments.append(
                    {
                        "name": name,
                        "stored_name": path.name,
                        "path": str(path),
                        "content_type": upload.content_type or "application/octet-stream",
                        "kind": "image" if suffix not in {".json", ".yaml", ".yml"} else "file",
                    }
                )
        except Exception:
            for path in [*saved, *folder.glob("*.tmp")]:
                path.unlink(missing_ok=True)
            raise

        message_content = content.strip() or "Allegati: " + ", ".join(
            item["name"] for item in attachments
        )
        sequence = await _next_discovery_sequence(session, discovery.id)
        message = DiscoveryMessage(
            discovery=discovery,
            sequence=sequence,
            role="user",
            content=message_content,
            metadata_json={"attachments": attachments},
        )
        session.add(message)
        await session.flush()
        discovery.last_active_at = datetime.now(UTC)
        session.add_all(
            [
                DiscoveryTurn(discovery=discovery, input_message=message),
                Event(
                    discovery=discovery,
                    type="discovery.message.queued",
                    payload={"attachments": len(attachments)},
                ),
            ]
        )
        await session.commit()
        return await _discovery_view(session, discovery)

    @api.post("/discoveries/{discovery_id}/screenshots", status_code=status.HTTP_202_ACCEPTED)
    async def upload_discovery_screenshot(
        discovery_id: int,
        file: UploadFile = File(...),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        discovery = await _discovery_or_404(session, discovery_id, lock=True)
        if discovery.status != "OPEN":
            raise HTTPException(409, "discovery is closed")
        raw = await file.read()
        if not raw:
            raise HTTPException(422, "empty screenshot")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "image.png").name) or "image.png"
        folder = (
            Path(settings.artifact_root).resolve()
            / "discoveries"
            / str(discovery.id)
            / "attachments"
        )
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uuid4().hex}-{name}"
        path.write_bytes(raw)
        sequence = await _next_discovery_sequence(session, discovery.id)
        message = DiscoveryMessage(
            discovery=discovery,
            sequence=sequence,
            role="user",
            content=f"Ho allegato uno screenshot: `{name}`. Leggilo con il tool read prima di rispondere.",
            metadata_json={"image_path": str(path), "image_name": name},
        )
        session.add(message)
        await session.flush()
        turn = DiscoveryTurn(discovery=discovery, input_message=message)
        discovery.last_active_at = datetime.now(UTC)
        session.add_all(
            [turn, Event(discovery=discovery, type="discovery.screenshot.queued", payload={"name": name})]
        )
        await session.commit()
        return await _discovery_view(session, discovery)

    @api.get("/discoveries/{discovery_id}/screenshots/{file_name}")
    async def get_discovery_screenshot(
        discovery_id: int, file_name: str, session: AsyncSession = Depends(get_session)
    ) -> Response:
        discovery = await _discovery_or_404(session, discovery_id)
        base = (
            Path(settings.artifact_root).resolve()
            / "discoveries"
            / str(discovery.id)
            / "attachments"
        )
        resolved = (base / file_name).resolve()
        if not str(resolved).startswith(str(base)) or not resolved.is_file():
            raise HTTPException(404, "screenshot not found")
        return FileResponse(resolved)

    @api.get("/discoveries/{discovery_id}/attachments/{file_name}")
    async def get_discovery_attachment(
        discovery_id: int, file_name: str, session: AsyncSession = Depends(get_session)
    ) -> Response:
        discovery = await _discovery_or_404(session, discovery_id)
        base = (
            Path(settings.artifact_root).resolve()
            / "discoveries"
            / str(discovery.id)
            / "attachments"
        )
        resolved = (base / file_name).resolve()
        if not resolved.is_relative_to(base) or not resolved.is_file():
            raise HTTPException(404, "attachment not found")
        return FileResponse(resolved, headers={"X-Content-Type-Options": "nosniff"})

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
        if any(not isinstance(item.get("id"), str) or not item["id"].strip() for item in proposals):
            raise HTTPException(422, "task proposal id is required")
        by_id = {str(item.get("id")): item for item in proposals}
        if len(by_id) != len(proposals):
            raise HTTPException(422, "duplicate task proposal id")
        selected = set(payload.proposal_ids) if payload.proposal_ids else set(by_id)
        if not selected.issubset(by_id):
            raise HTTPException(422, "unknown task proposal")
        # Accept legacy title references only when unambiguous; new proposals use stable IDs.
        by_title = {str(item.get("title")): item for item in proposals
                    if sum(other.get("title") == item.get("title") for other in proposals) == 1}
        dependencies: dict[str, list[str]] = {}
        for proposal_id in selected:
            proposal = by_id[proposal_id]
            if not isinstance(proposal.get("depends_on", []), list):
                raise HTTPException(422, "task proposal dependencies must be a list")
            resolved: list[str] = []
            for reference in proposal.get("depends_on", []):
                target = by_id.get(reference) or by_title.get(reference)
                if target is None:
                    raise HTTPException(422, f"unknown dependency: {reference}")
                target_id = str(target["id"])
                if target_id not in selected and not target.get("created_task_id"):
                    raise HTTPException(409, f"dependency {reference} must be created first")
                resolved.append(target_id)
            dependencies[proposal_id] = resolved
        ordered: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(proposal_id: str) -> None:
            if proposal_id in visiting:
                raise HTTPException(409, "task proposal dependency cycle")
            if proposal_id in visited:
                return
            visiting.add(proposal_id)
            for dependency in dependencies[proposal_id]:
                if dependency in selected:
                    visit(dependency)
            visiting.remove(proposal_id)
            visited.add(proposal_id)
            ordered.append(proposal_id)

        for proposal_id in [str(item["id"]) for item in proposals if str(item["id"]) in selected]:
            visit(proposal_id)
        plans: dict[str, PlanPayload] = {}
        for proposal_id in ordered:
            proposal = by_id[proposal_id]
            if proposal.get("created_task_id"):
                continue
            plan, detail = validate_task_proposal(proposal, discovery.state)
            if plan is None:
                raise HTTPException(409, f"task proposal '{proposal.get('title', 'untitled')}' is not fully planned: {detail}")
            plans[proposal_id] = plan
        created: list[Task] = []
        prompt_paths: list[Path] = []
        try:
            for proposal_id in ordered:
                proposal = by_id[proposal_id]
                if proposal.get("created_task_id"):
                    continue
                plan_payload = plans[proposal_id]
                task = await create_task_record(
                    session, discovery.project_id, str(proposal["title"]),
                    str(proposal["megaprompt"]), created_source="discovery", commit=False,
                )
                prompt_paths.append(Path(discovery.project.repository_path) / str(task.prompt_path))
                task.status, task.stage = transition(task.status, task.stage, "plan")
                task.status, task.stage = transition(task.status, task.stage, "briefed")
                task.status, task.stage = transition(task.status, task.stage, "planned")
                revision = PlanRevision(
                    task=task, revision=1,
                    brief_markdown=plan_payload.brief_markdown,
                    plan_markdown=plan_payload.plan_markdown,
                    metadata_json=plan_payload.metadata.model_dump(),
                )
                session.add_all([revision, Event(task=task, type="planning.completed", payload={"revision": 1})])
                await approve_plan_revision(session, task, revision)
                proposal["created_task_id"] = task.id
                created.append(task)
            for proposal_id in ordered:
                proposal = by_id[proposal_id]
                if not proposal.get("created_task_id"):
                    continue
                task = await session.get(Task, proposal["created_task_id"])
                if task:
                    task.depends_on_task_ids = [by_id[dependency]["created_task_id"] for dependency in dependencies[proposal_id]]
            discovery.state = {**discovery.state, "task_proposals": proposals}
            session.add(Event(discovery=discovery, type="discovery.tasks.created", payload={"task_ids": [task.id for task in created]}))
            await session.commit()
        except Exception as error:
            await session.rollback()
            for path in prompt_paths:
                path.unlink(missing_ok=True)
            if isinstance(error, TaskCreationError):
                raise HTTPException(error.status_code, error.detail) from error
            raise
        ids = [by_id[proposal_id]["created_task_id"] for proposal_id in ordered]
        tasks = [await session.get(Task, task_id) for task_id in ids]
        return [await _task_view(session, task) for task in tasks if task is not None]

    @api.post("/tasks/{task_id}/rework-chat", status_code=status.HTTP_201_CREATED)
    async def rework_chat(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        task = await _task_or_404(session, task_id, lock=True)
        if not _task_is_stuck(task):
            raise HTTPException(409, "task is not stuck")
        existing = await session.scalar(
            select(Discovery)
            .where(Discovery.task_id == task_id, Discovery.status == "OPEN")
            .order_by(Discovery.id.desc())
        )
        if existing is not None:
            return await _discovery_view(session, existing)
        project = await session.get(Project, task.project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        profile = await _profile(session, "escalation")
        discovery = Discovery(
            project=project,
            task_id=task.id,
            title=f"Fix: {task.title}",
            profile_id=profile.id,
            provider_session_id=f"discovery-{uuid4().hex}",
            state={},
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
            discovery=discovery,
            sequence=1,
            role="user",
            content="Il task è fallito. Mostrami la diagnosi e proponi una soluzione.",
        )
        session.add(message)
        await session.flush()
        session.add_all(
            [
                DiscoveryTurn(discovery=discovery, input_message=message),
                Event(discovery=discovery, type="discovery.created", payload={"task_id": task.id}),
            ]
        )
        await session.commit()
        return await _discovery_view(session, discovery)

    @api.post("/discoveries/{discovery_id}/apply-fix")
    async def apply_discovery_fix(
        discovery_id: int, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        discovery = await _discovery_or_404(session, discovery_id, lock=True)
        if discovery.task_id is None:
            raise HTTPException(409, "discovery is not a task rework chat")
        proposal = discovery.state.get("fix_proposal")
        if not isinstance(proposal, dict) or proposal.get("action") not in {"revise_task", "revise_parent"}:
            raise HTTPException(409, "no fix proposal is ready to apply")
        task = await _task_or_404(session, discovery.task_id, lock=True)
        approved_plan, detail = await validate_fix_proposal(session, task, proposal)
        if approved_plan is None:
            asked_pi = await _queue_fix_proposal_fix_request(session, discovery, detail)
            raise HTTPException(
                409,
                f"fix proposal is not valid: {detail}"
                + (" — ho chiesto a Pi di correggerla, guarda la chat." if asked_pi else ""),
            )
        action = proposal["action"]
        target = task if action == "revise_task" else await _task_or_404(session, task.parent_task_id, lock=True)
        if not _task_is_stuck(target):
            raise HTTPException(409, "target task is not stuck")
        revision = int(await session.scalar(select(func.coalesce(func.max(PlanRevision.revision), 0) + 1).where(
            PlanRevision.task_id == target.id,
        )) or 1)
        now = datetime.now(UTC)
        plan = PlanRevision(
            task=target,
            revision=revision,
            brief_markdown=approved_plan.brief_markdown,
            plan_markdown=approved_plan.plan_markdown,
            metadata_json={**approved_plan.metadata.model_dump(), "fix_source": discovery.id},
            approved_at=now,
        )
        session.add(plan)
        await session.flush()
        fix_action = (
            "approve_amendment" if target.status == TaskStatus.IN_PROGRESS else "approve_fix"
        )
        target.status, target.stage = transition(target.status, target.stage, fix_action)
        target.approved_plan_revision = revision
        target.version += 1
        children: list[Task] = []
        if action == "revise_parent":
            old_children = await _active_children(session, target.id, lock=True)
            for child in old_children:
                child.superseded_at = now
            children = await _materialize_plan_subtasks(
                session, target, plan, approved_plan.metadata.model_dump()["implementation_tasks"]
            )
        session.add(Event(
            task=target,
            type="task.fix_applied",
            payload={
                "revision": revision, "action": action, "discovery_id": discovery.id,
                "new_children": [child.id for child in children],
            },
        ))
        discovery.status = "CLOSED"
        discovery.closed_at = now
        await session.commit()
        return await _task_view(session, target)

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
            folder = (
                Path(settings.artifact_root).resolve()
                / "discoveries"
                / str(discovery.id)
                / "attachments"
            )
            shutil.rmtree(folder, ignore_errors=True)
        return await _discovery_view(session, discovery)

    @api.delete("/discoveries/{discovery_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_discovery(
        discovery_id: int, session: AsyncSession = Depends(get_session)
    ) -> Response:
        discovery = await _discovery_or_404(session, discovery_id, lock=True)
        active = await session.scalar(select(func.count(DiscoveryTurn.id)).where(
            DiscoveryTurn.discovery_id == discovery_id,
            DiscoveryTurn.status.in_(["QUEUED", "RUNNING"]),
        ))
        if active:
            raise HTTPException(409, "stop or close the active Discovery before deleting it")
        await session.delete(discovery)
        await session.commit()
        shutil.rmtree(Path(settings.artifact_root).resolve() / "discoveries" / str(discovery_id), ignore_errors=True)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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

    @api.post("/tasks/reorder")
    async def reorder_tasks(
        payload: TaskReorder, session: AsyncSession = Depends(get_session)
    ) -> list[dict[str, Any]]:
        tasks = (
            await session.scalars(
                select(Task).where(Task.id.in_(payload.task_ids)).with_for_update()
            )
        ).all()
        by_id = {task.id: task for task in tasks}
        if set(by_id) != set(payload.task_ids):
            raise HTTPException(404, "unknown task id in reorder list")
        total = len(payload.task_ids)
        for index, task_id in enumerate(payload.task_ids):
            by_id[task_id].priority = total - index
        await session.commit()
        return [await _task_view(session, by_id[task_id]) for task_id in payload.task_ids]

    @api.get("/tasks/{task_id}")
    async def get_task(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        return await _task_view(
            session, await _task_or_404(session, task_id), detail=True
        )

    @api.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_task(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> Response:
        task = await _task_or_404(session, task_id, lock=True)
        children = list((await session.scalars(select(Task).where(Task.parent_task_id == task.id).with_for_update())).all())
        blocker = await _task_delete_blocker(session, task, children)
        if blocker:
            raise HTTPException(409, blocker)
        removed = [task, *children]
        repository = Path(task.project.repository_path).resolve()
        prompts = [repository / item.prompt_path for item in removed if item.prompt_path]
        discoveries = (await session.scalars(
            select(Discovery)
            .where(Discovery.state["task_proposals"].contains([{"created_task_id": task.id}]))
            .with_for_update()
        )).all()
        blocker = await _task_delete_blocker(session, task, children)
        if blocker:
            raise HTTPException(409, blocker)
        for discovery in discoveries:
            proposals = [dict(proposal) for proposal in discovery.state.get("task_proposals", [])]
            for proposal in proposals:
                if proposal.get("created_task_id") == task.id:
                    proposal.pop("created_task_id")
            discovery.state = {**discovery.state, "task_proposals": proposals}
        await session.delete(task)
        await session.commit()
        for prompt in prompts:
            try:
                if prompt.resolve().is_relative_to(repository / ".carlo" / "prompts"):
                    prompt.unlink(missing_ok=True)
            except OSError:
                pass
        for item in removed:
            shutil.rmtree(Path(settings.artifact_root).resolve() / item.id, ignore_errors=True)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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

        async def retry(payload: dict[str, Any]) -> None:
            session.add(Event(task=task, type="planning.package_retry", payload=payload))
            await session.commit()

        planner = Planner(provider, provider_profile)
        try:
            output = await planner.plan(
                PlanningRequest(
                    task.project, task.title, task.goal, session_id,
                    prompt_path=task.prompt_path, model_id=task.available_model_id,
                    instruction=instruction, one_package=task.parent_task_id is not None,
                ),
                on_event=publish, on_retry=retry,
            )
        except PlanningError as error:
            session.add(Event(task=task, type="planning.failed", payload={"error": str(error)}))
            await session.commit()
            raise HTTPException(502, str(error)) from error
        result = planner.last_result
        if result and (result.used_skills or result.resource_revisions):
            session.add(Event(task=task, type="planning.skills_used", payload={
                "session_id": result.session_id, "skills": list(result.used_skills),
                "revisions": result.resource_revisions, "packages": result.loaded_packages,
            }))
            await session.commit()
        if isinstance(output, PlanningQuestion):
            task.planning_question = {"text": output.text}
            task.version += 1
            session.add(Event(task=task, type="planning.question", payload=task.planning_question))
            await session.commit()
            return await _task_view(session, task, detail=True)

        revision = (
            await session.scalar(
                select(func.coalesce(func.max(PlanRevision.revision), 0) + 1).where(
                    PlanRevision.task_id == task.id
                )
            )
        ) or 1
        metadata = output.metadata.model_dump()
        if task.planning_session_id and "-replan-" in task.planning_session_id:
            metadata["replan"] = True
            if task.parent_task_id is not None:
                metadata["legacy_regeneration"] = True
        runtime_evidence = model_runtime_evidence(provider_profile)
        metadata["planner_profile"] = {
            "name": profile.name,
            "provider": profile.provider,
            "model": runtime_evidence["model"] if runtime_evidence else None,
            "effort": provider_profile.effort,
            "tools": list(provider_profile.tools),
        }
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
        if task.parent_task_id is not None and task.stage == TaskStage.BLOCKED and task.planning_question:
            parent = await session.scalar(select(Task).where(Task.id == task.parent_task_id).with_for_update())
            if parent is None:
                raise HTTPException(409, "subtask parent is missing")
            question = task.planning_question["text"]
            task.planning_question = None
            task.status, task.stage = TaskStatus.READY, TaskStage.QUEUED
            task.version += 1
            parent.status, parent.stage = TaskStatus.IN_PROGRESS, TaskStage.IMPLEMENTING
            parent.version += 1
            session.add(Event(task=task, type="planning.technical.answer", payload={
                "question": question, "answer": payload.answer,
            }))
            await session.commit()
            return await _task_view(session, task)
        if task.stage != TaskStage.PLANNING or not task.planning_question:
            raise HTTPException(409, "task is not waiting for a planning answer")
        question = task.planning_question["text"]
        task.planning_question = None
        task.version += 1
        session.add(Event(task=task, type="planning.answer", payload={"question": question}))
        await session.commit()
        try:
            return await continue_planning(
                task,
                session,
                f"The user answered your planning question.\nQuestion: {question}\nAnswer: {payload.answer}\nContinue repository analysis. Ask one further high-impact question if necessary; otherwise return the complete plan JSON.",
            )
        except HTTPException as error:
            if task.planning_session_id and "-replan-" in task.planning_session_id:
                await _restore_replan(
                    session,
                    task,
                    "task.replan.failed",
                    {"error": str(error.detail)},
                )
                await session.commit()
            raise

    @api.post("/tasks/{task_id}/replan")
    async def replan_subtasks(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        task = await session.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            raise HTTPException(404, "task not found")
        children = await _active_children(session, task.id, lock=True)
        if not _replan_allowed(task, children):
            raise HTTPException(409, "task cannot replan its current subtasks")
        previous_status, previous_stage = task.status, task.stage
        task.status, task.stage = transition(task.status, task.stage, "replan")
        task.status, task.stage = transition(task.status, task.stage, "briefed")
        cycle = int(
            await session.scalar(
                select(func.count(Event.sequence)).where(
                    Event.task_id == task.id, Event.type == "task.replan.started"
                )
            )
            or 0
        ) + 1
        task.planning_session_id = f"{task.id}-replan-{cycle}"
        task.planning_cursor = None
        task.planning_question = None
        task.version += 1
        session.add(
            Event(
                task=task,
                type="task.replan.started",
                payload={
                    "cycle": cycle,
                    "previous_status": previous_status.value,
                    "previous_stage": previous_stage.value,
                    "children": [child.id for child in children],
                },
            )
        )
        instruction = await _replan_instruction(session, task, children)
        await session.commit()
        try:
            return await continue_planning(task, session, instruction)
        except HTTPException as error:
            await _restore_replan(
                session,
                task,
                "task.replan.failed",
                {"cycle": cycle, "error": str(error.detail)},
            )
            await session.commit()
            raise

    @api.post("/tasks/{task_id}/regenerate-plan")
    async def regenerate_plan(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        task = await _task_or_404(session, task_id, lock=True)
        if task.parent_task_id is None:
            return await replan_subtasks(task_id, session)
        if task.status == TaskStatus.DONE or (task.status == TaskStatus.IN_PROGRESS and task.stage != TaskStage.BLOCKED):
            raise HTTPException(409, "cannot regenerate a completed or running subtask")
        plan = await session.scalar(select(PlanRevision).where(
            PlanRevision.task_id == task.id,
            PlanRevision.revision == task.approved_plan_revision,
        ))
        items = plan.metadata_json.get("implementation_tasks", []) if plan else []
        if len(items) != 1 or not isinstance(items[0], dict) or "prompt" not in items[0]:
            raise HTTPException(409, "subtask does not have a legacy plan")
        previous_status, previous_stage = task.status, task.stage
        try:
            task.status, task.stage = transition(task.status, task.stage, "replan")
            task.status, task.stage = transition(task.status, task.stage, "briefed")
        except InvalidTransition as error:
            raise HTTPException(409, str(error)) from error
        task.planning_session_id = f"{task.id}-replan-{task.version + 1}"
        task.planning_cursor = None
        task.planning_question = None
        task.version += 1
        session.add(Event(task=task, type="task.replan.started", payload={
            "previous_status": previous_status.value,
            "previous_stage": previous_stage.value,
            "legacy_regeneration": True,
        }))
        await session.commit()
        instruction = (
            "Regenerate this legacy subtask as exactly one complete work package. "
            "Preserve its approved goal and do not replan completed siblings. "
            f"Previous plan:\n{plan.plan_markdown[:8000]}\n\n"
            + _planning_instruction(task)
        )
        try:
            return await continue_planning(task, session, instruction)
        except HTTPException as error:
            await _restore_replan(session, task, "task.replan.failed", {"error": str(error.detail)})
            await session.commit()
            raise

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

    @api.post("/tasks/{task_id}/retry")
    async def retry_subtask(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        task = await session.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            raise HTTPException(404, "task not found")
        if (
            task.parent_task_id is None
            or task.approved_plan_revision is None
        ):
            raise HTTPException(409, "subtask has no reusable execution state")
        try:
            task.status, task.stage = transition(task.status, task.stage, "retry")
        except InvalidTransition as error:
            raise HTTPException(409, str(error)) from error
        parent = await session.scalar(
            select(Task).where(Task.id == task.parent_task_id).with_for_update()
        )
        if parent is None:
            raise HTTPException(409, "subtask parent is missing")
        previous_branch = task.branch_name
        previous_worktree = task.worktree_path
        previous_checkpoint = task.checkpoint_sha
        fresh_checkout = (
            not task.worktree_path
            or not task.branch_name
            or Path(task.worktree_path).resolve()
            != Path(task.project.repository_path).resolve()
            or task.branch_name != parent_branch_name(parent.id, parent.title)
        )
        previous_attempt = int(
            await session.scalar(
                select(func.coalesce(func.max(Attempt.number), 0)).where(
                    Attempt.task_id == task.id
                )
            )
            or 0
        )
        task.active_profile_id = None
        if fresh_checkout:
            task.branch_name = None
            task.worktree_path = None
            task.checkpoint_sha = None
        task.version += 1
        parent.status, parent.stage = TaskStatus.IN_PROGRESS, TaskStage.IMPLEMENTING
        parent.version += 1
        session.add(
            Event(
                task=task,
                type="task.retry.started",
                payload={
                    "previous_attempt": previous_attempt,
                    "branch": task.branch_name,
                    "worktree": task.worktree_path,
                    "checkpoint": task.checkpoint_sha,
                    "fresh_checkout": fresh_checkout,
                    "previous_branch": previous_branch,
                    "previous_worktree": previous_worktree,
                    "previous_checkpoint": previous_checkpoint,
                },
            )
        )
        session.add(
            Event(task=parent, type="subtasks.resumed", payload={"child": task.id})
        )
        await session.commit()
        return await _task_view(session, task)

    @api.post("/tasks/{task_id}/stop")
    async def stop_task(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        task = await session.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            raise HTTPException(404, "task not found")
        try:
            task.status, task.stage = transition(task.status, task.stage, "stop")
        except InvalidTransition as error:
            raise HTTPException(409, str(error)) from error
        if task.parent_task_id is None:
            session.add(
                Event(
                    task=task,
                    type="task.stopped",
                    payload={"reset": "ready"},
                )
            )
        await session.commit()
        return await _task_view(session, task)

    @api.post("/tasks/{task_id}/hold")
    async def hold_task(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        task = await session.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            raise HTTPException(404, "task not found")
        try:
            task.status, task.stage = transition(task.status, task.stage, "hold")
        except InvalidTransition as error:
            raise HTTPException(409, str(error)) from error
        session.add(Event(task=task, type="task.held", payload={"reset": "not_ready"}))
        await session.commit()
        return await _task_view(session, task)

    @api.post("/tasks/{task_id}/resume")
    async def resume_task(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        task = await session.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            raise HTTPException(404, "task not found")
        if task.status != TaskStatus.READY:
            raise HTTPException(409, "aggregate task is not ready")
        child = await session.scalar(
            select(Task)
            .where(
                Task.parent_task_id == task.id,
                Task.superseded_at.is_(None),
                Task.status != TaskStatus.DONE,
            )
            .order_by(Task.subtask_position)
            .limit(1)
            .with_for_update()
        )
        if child is None:
            raise HTTPException(409, "aggregate task has no unfinished subtasks")
        if child.status != TaskStatus.READY:
            raise HTTPException(409, f"{child.id} is not ready")
        child.stage = TaskStage.QUEUED
        child.version += 1
        task.status, task.stage = TaskStatus.IN_PROGRESS, TaskStage.IMPLEMENTING
        task.version += 1
        session.add(Event(task=task, type="subtasks.resumed", payload={"child": child.id}))
        await session.commit()
        return await _task_view(session, task)

    @api.post("/tasks/{task_id}/approve")
    async def approve_plan(
        task_id: str,
        payload: Approval,
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        task = await _task_or_404(session, task_id, lock=True)
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
        is_replan = plan.metadata_json.get("replan") is True and not plan.metadata_json.get("legacy_regeneration")
        items = plan.metadata_json.get("implementation_tasks") or []
        if is_replan and not items:
            raise HTTPException(409, "replan must contain replacement subtasks")
        existing = await _active_children(session, task.id, lock=is_replan)
        if is_replan and (
            task.parent_task_id is not None
            or not existing
            or any(
                child.status
                in {TaskStatus.IN_PROGRESS, TaskStatus.TEST, TaskStatus.DONE}
                for child in existing
            )
        ):
            raise HTTPException(409, "subtasks changed and cannot be replaced")
        try:
            await approve_plan_revision(session, task, plan, existing=existing, is_replan=is_replan)
        except InvalidTransition as error:
            raise HTTPException(409, str(error)) from error
        await session.commit()
        return await _task_view(session, task)

    app.include_router(api)
    app.include_router(diagnostics)

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


async def _task_or_404(
    session: AsyncSession, task_id: str, *, lock: bool = False
) -> Task:
    if lock:
        task = await session.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
    else:
        task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


def _task_is_stuck(task: Task) -> bool:
    """Whether a task is blocked and has no other automatic path forward:
    FAILED/BLOCKED (exhausted retries/escalations), or IN_PROGRESS/BLOCKED with
    no pending amendment to approve (an escalation asked for a human directly).
    """
    if task.stage != TaskStage.BLOCKED:
        return False
    if task.status == TaskStatus.FAILED:
        return True
    return task.status == TaskStatus.IN_PROGRESS


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
        profile = AgentProfileRecord(
            name=name,
            provider="pi",
            effort=None,
            available_model_id=None,
            permissions={},
            default_skills=list(REQUIRED_PROFILE_SKILLS.get(name, ()))
            + (["frontend-design"] if name == "plan" else []),
            default_packages=[],
        )
        session.add(profile)
        await session.flush()
    return profile


def work_package_example() -> dict[str, Any]:
    from .planning import work_package_example as example
    return example()


def _planning_instruction(task: Task, *, fresh_rework: bool = False) -> str:
    return planning_instruction(
        PlanningRequest(task.project, task.title, task.goal,
                        task.planning_session_id or f"{task.id}-plan",
                        prompt_path=task.prompt_path),
        fresh_rework=fresh_rework,
    )


async def _replan_instruction(
    session: AsyncSession, task: Task, children: list[Task]
) -> str:
    approved_plan = (
        await session.scalar(
            select(PlanRevision).where(
                PlanRevision.task_id == task.id,
                PlanRevision.revision == task.approved_plan_revision,
            )
        )
        if task.approved_plan_revision
        else None
    )
    current_plan = approved_plan.plan_markdown[:8000] if approved_plan else "Unavailable"
    lines: list[str] = []
    for child in children:
        attempts = int(
            await session.scalar(
                select(func.count(Attempt.id)).where(Attempt.task_id == child.id)
            )
            or 0
        )
        failures = int(
            await session.scalar(
                select(func.coalesce(func.sum(ValidationRun.failure_count), 0)).where(
                    ValidationRun.task_id == child.id
                )
            )
            or 0
        )
        context_limits = int(
            await session.scalar(
                select(func.count(Event.sequence)).where(
                    Event.task_id == child.id,
                    Event.type == "execution.context_limit",
                )
            )
            or 0
        )
        lines.append(
            f"- {child.id} | {child.title} | {child.status.value} | attempts: "
            f"{attempts} | validation failures: {failures} | context-limit events: "
            f"{context_limits}\n  goal: {child.goal}"
        )
    return (
        f"Current approved plan:\n{current_plan}\n\n"
        "Replace the current subtask partition using this execution evidence:\n"
        + "\n".join(lines)
        + "\nEach replacement task must be an independently verifiable outcome that "
        "fits one agent context. Do not create validation-only tasks. Keep tightly "
        "coupled work together. Use the smallest useful number of tasks.\n"
        + _planning_instruction(task)
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
        "effort": profile.effort,
        "permissions": profile.permissions,
        "default_skills": profile.default_skills,
        "default_packages": profile.default_packages,
        "required_skills": list(REQUIRED_PROFILE_SKILLS.get(profile.name, ())),
        "context_policy": profile.context_policy,
        "active": profile.active,
        "available_model_id": profile.available_model_id,
    }


def _pi_package_view(package: PiPackage) -> dict[str, Any]:
    return {
        "id": package.id,
        "source": package.source,
        "identity": package.identity,
        "enabled": package.enabled,
        "pinned": package.pinned,
        "is_default": package.is_default,
        "active_version": package.active_version,
        "resources": package.resources,
        "last_update_status": package.last_update_status,
        "last_update_error": package.last_update_error,
        "last_update_attempt_at": package.last_update_attempt_at.isoformat()
        if package.last_update_attempt_at else None,
        "last_update_success_at": package.last_update_success_at.isoformat()
        if package.last_update_success_at else None,
    }


def _skill_catalog(revisions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    revisions = revisions or {}
    bundled = {
        path.parent.name
        for path in (Path(__file__).resolve().parents[2] / "skills").glob("*/SKILL.md")
    }
    return [
        {
            "name": name,
            "source": "carlo" if name in bundled else "managed",
            "revision": revisions.get(name),
            "required_profiles": sorted(
                profile
                for profile, required in REQUIRED_PROFILE_SKILLS.items()
                if name in required
            ),
        }
        for name in sorted(available_profile_skills())
    ]


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
        "default_packages": settings.default_packages,
        "default_skills": settings.default_skills,
    }


def _pi_resource_revisions(settings: Settings) -> dict[str, str]:
    manifest = Path(settings.artifact_root) / "pi-resources" / "revisions.json"
    try:
        value = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        name: revision
        for name, revision in value.items()
        if isinstance(name, str)
        and isinstance(revision, str)
        and re.fullmatch(r"[0-9a-f]{40}", revision)
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
    try:
        _validate_model_limits(model)
        settings = await _pi_settings(session)
        calculate_compaction(
            model.effective_context_window,
            model.effective_max_tokens,
            settings.reserve_percent,
            settings.keep_recent_percent,
        )
    except (HTTPException, ModelProviderError) as error:
        raise HTTPException(409, "model is not selectable") from error
    return model


def _available_model_view(
    model: AvailableModel,
    provider: ModelProvider | None,
    settings: PiRuntimeSettings,
) -> dict[str, Any]:
    context_window = model.effective_context_window
    max_tokens = model.effective_max_tokens
    try:
        compaction = calculate_compaction(
            context_window,
            max_tokens,
            settings.reserve_percent,
            settings.keep_recent_percent,
        )
    except ModelProviderError:
        compaction = None
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
        "selectable": model.status == "AVAILABLE" and compaction is not None,
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
    parent_title = None
    if task.parent_task_id:
        parent_title = await session.scalar(
            select(Task.title).where(Task.id == task.parent_task_id)
        )
    children = await _active_children(session, task.id)
    subtask_count = len(children)
    await session.refresh(task, attribute_names=["updated_at"])
    view = {
        "id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "goal": task.goal,
        "prompt_path": task.prompt_path,
        "status": task.status.value,
        "stage": task.stage.value,
        "updated_at": task.updated_at.isoformat(),
        "priority": task.priority,
        "version": task.version,
        "approved_plan_revision": task.approved_plan_revision,
        "branch_name": task.branch_name,
        "worktree_path": task.worktree_path,
        "checkpoint_sha": task.checkpoint_sha,
        "available_model_id": task.available_model_id,
        "planning_question": task.planning_question,
        "parent_task_id": task.parent_task_id,
        "parent_title": parent_title,
        "subtask_position": task.subtask_position,
        "subtask_count": subtask_count,
        "superseded_at": task.superseded_at.isoformat()
        if task.superseded_at
        else None,
        "replan_allowed": _replan_allowed(task, children),
        "used_skills": [],
        "skill_revisions": {},
        "loaded_packages": {},
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
    all_children = (await session.scalars(select(Task).where(Task.parent_task_id == task.id))).all()
    view["delete_allowed"] = await _task_delete_blocker(session, task, list(all_children)) is None
    from .execution_telemetry import summarize_sessions

    measured_ids = [task.id]
    if task.parent_task_id is None:
        measured_ids.extend((await session.scalars(select(Task.id).where(Task.parent_task_id == task.id))).all())
    metric_events = (
        await session.scalars(
            select(Event).where(
                Event.task_id.in_(measured_ids),
                Event.type == "execution.session_metrics",
            )
        )
    ).all()
    view["execution_summary"] = summarize_sessions([event.payload for event in metric_events])
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
    skill_revisions: dict[str, list[str]] = {}
    loaded_packages: dict[str, list[str]] = {}
    for event in skill_events:
        values = event.payload.get("skills")
        if isinstance(values, list):
            used_skills.extend(skill for skill in values if isinstance(skill, str))
        packages = event.payload.get("packages")
        package_names = set(packages) if isinstance(packages, dict) else set()
        revisions = event.payload.get("revisions")
        if isinstance(revisions, dict):
            for name, revision in revisions.items():
                if isinstance(name, str) and isinstance(revision, str) and name not in package_names:
                    values = skill_revisions.setdefault(name, [])
                    if revision not in values:
                        values.append(revision)
        if isinstance(packages, dict):
            for name, revision in packages.items():
                if isinstance(name, str) and isinstance(revision, str):
                    values = loaded_packages.setdefault(name, [])
                    if revision not in values:
                        values.append(revision)
    view["used_skills"] = list(dict.fromkeys(used_skills))
    view["skill_revisions"] = skill_revisions
    view["loaded_packages"] = loaded_packages
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


def _artifact_tail(path_value: str | None, root: Path, limit: int) -> str | None:
    if not path_value:
        return None
    path = Path(path_value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    with path.open("rb") as artifact:
        artifact.seek(0, 2)
        artifact.seek(max(artifact.tell() - limit, 0))
        return artifact.read().decode(errors="replace")


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
        "task_id": discovery.task_id,
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
