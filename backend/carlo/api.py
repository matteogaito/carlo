import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from .auth import InvalidCredentials, LoginThrottled, login, resolve_session, revoke_session
from .actions import ActionConfigError, load_catalog, preflight
from .config import Settings
from .domain import InvalidTransition, TaskStage, TaskStatus, transition
from .models import AgentProfile as AgentProfileRecord
from .models import (
    ActionRun,
    ActionStep,
    Attempt,
    Escalation,
    Event,
    PlanRevision,
    Project,
    Runner,
    Task,
    User,
    ValidationRun,
)
from .provider import AgentProfile, CodingAgentProvider, ProviderError
from .ssh import HostScan, SshError, SshTransport, validate_runner


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
    goal: str = Field(min_length=1)
    priority: int = 0
    created_source: str = "web"


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


class PlanMetadata(BaseModel):
    skills: list[str]
    validation_commands: list[str]
    browser_validation: bool
    build_required: bool
    run_required: bool
    deployment_expected: bool
    risk_flags: list[str]
    affected_areas: list[str]


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


def create_app(
    session_factory: async_sessionmaker[AsyncSession],
    provider: CodingAgentProvider,
    settings: Settings | None = None,
    ssh_transport: SshTransport | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    ssh_transport = ssh_transport or SshTransport(
        Path(settings.ssh_known_hosts), connect_timeout=settings.ssh_connect_timeout
    )
    app = FastAPI(title="CARLO v3")

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
        for field in payload.model_fields_set:
            setattr(profile, field, getattr(payload, field))
        session.add(
            Event(type="agent_profile.updated", payload={"name": name, "fields": sorted(payload.model_fields_set)})
        )
        await session.commit()
        return _profile_view(profile)

    @api.post("/tasks", status_code=status.HTTP_201_CREATED)
    async def create_task(
        payload: TaskCreate, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        project = await session.scalar(
            select(Project).where(Project.id == payload.project_id).with_for_update()
        )
        if project is None:
            raise HTTPException(404, "project not found")
        sequence = project.next_task_sequence
        project.next_task_sequence += 1
        task = Task(
            id=f"{project.key}-{sequence}",
            project=project,
            sequence=sequence,
            title=payload.title,
            goal=payload.goal,
            priority=payload.priority,
            created_source=payload.created_source,
        )
        session.add_all(
            [task, Event(task=task, type="task.created", payload={"source": payload.created_source})]
        )
        await session.commit()
        return await _task_view(session, task)

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
        session.add(Event(task=task, type="planning.started", payload={}))
        profile = await _profile(session, "plan")
        await session.commit()

        instruction = _planning_instruction(task)
        provider_profile = _provider_profile(profile)
        if "carlo-planning" in provider_profile.skills:
            instruction = f"/skill:carlo-planning {instruction}"
        session_id = f"{task.id}-plan-{task.version}"
        try:
            result = await provider.run(
                provider_profile,
                instruction,
                task.project.repository_path,
                session_id,
            )
            output = PlanPayload.model_validate_json(result.output)
        except (ProviderError, ValueError) as error:
            session.add(
                Event(task=task, type="planning.failed", payload={"error": str(error)})
            )
            await session.commit()
            raise HTTPException(502, "planning provider failed") from error

        revision = (
            await session.scalar(
                select(func.coalesce(func.max(PlanRevision.revision), 0) + 1).where(
                    PlanRevision.task_id == task.id
                )
            )
        ) or 1
        plan = PlanRevision(
            task=task,
            revision=revision,
            brief_markdown=output.brief_markdown,
            plan_markdown=output.plan_markdown,
            metadata_json=output.metadata.model_dump(),
        )
        task.status, task.stage = transition(task.status, task.stage, "planned")
        task.version += 1
        session.add_all(
            [plan, Event(task=task, type="planning.completed", payload={"revision": revision})]
        )
        await session.commit()
        return await _task_view(session, task)

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
            default_skills=["carlo-planning"] if name == "plan" else [],
        )
        session.add(profile)
        await session.flush()
    return profile


def _provider_profile(record: AgentProfileRecord) -> AgentProfile:
    tools = record.permissions.get("tools") or ["read", "grep", "find", "ls"]
    return AgentProfile(
        name=record.name,
        model=record.model,
        effort=record.effort,
        tools=tuple(tools),
        skills=tuple(record.default_skills),
    )


def _planning_instruction(task: Task) -> str:
    contract = {
        "brief_markdown": "evidence-oriented repository understanding",
        "plan_markdown": "concrete implementation plan",
        "metadata": {
            "skills": [],
            "validation_commands": [],
            "browser_validation": False,
            "build_required": False,
            "run_required": False,
            "deployment_expected": False,
            "risk_flags": [],
            "affected_areas": [],
        },
    }
    return (
        f"Inspect the repository and plan task {task.id}: {task.goal}\n"
        f"Project policies: {json.dumps(task.project.policies)}\n"
        "Return only JSON matching this shape:\n"
        f"{json.dumps(contract)}"
    )


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
        "status": task.status.value,
        "stage": task.stage.value,
        "priority": task.priority,
        "version": task.version,
        "approved_plan_revision": task.approved_plan_revision,
        "branch_name": task.branch_name,
        "worktree_path": task.worktree_path,
        "checkpoint_sha": task.checkpoint_sha,
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
        "type": event.type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


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
