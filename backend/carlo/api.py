import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .domain import InvalidTransition, TaskStage, TaskStatus, transition
from .models import AgentProfile as AgentProfileRecord
from .models import Event, PlanRevision, Project, Task
from .provider import AgentProfile, CodingAgentProvider, ProviderError


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


def create_app(
    session_factory: async_sessionmaker[AsyncSession],
    provider: CodingAgentProvider,
) -> FastAPI:
    app = FastAPI(title="CARLO v3")

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
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

    @app.get("/api/projects")
    async def list_projects(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        projects = (await session.scalars(select(Project).order_by(Project.key))).all()
        return [_project_view(project) for project in projects]

    @app.post("/api/tasks", status_code=status.HTTP_201_CREATED)
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

    @app.get("/api/tasks")
    async def list_tasks(
        session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        tasks = (
            await session.scalars(
                select(Task).order_by(Task.priority.desc(), Task.created_at, Task.id)
            )
        ).unique().all()
        return [await _task_view(session, task) for task in tasks]

    @app.get("/api/tasks/{task_id}")
    async def get_task(
        task_id: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, Any]:
        return await _task_view(session, await _task_or_404(session, task_id))

    @app.post("/api/tasks/{task_id}/plan")
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
        session_id = f"{task.id}-plan-{task.version}"
        try:
            result = await provider.run(
                _provider_profile(profile),
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

    @app.post("/api/tasks/{task_id}/approve")
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
            task.status, task.stage = transition(task.status, task.stage, "approve")
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


async def _profile(session: AsyncSession, name: str) -> AgentProfileRecord:
    profile = await session.scalar(
        select(AgentProfileRecord).where(AgentProfileRecord.name == name)
    )
    if profile is None:
        profile = AgentProfileRecord(name=name, provider="pi")
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


async def _task_view(session: AsyncSession, task: Task) -> dict[str, Any]:
    plan = await session.scalar(
        select(PlanRevision)
        .where(PlanRevision.task_id == task.id)
        .order_by(PlanRevision.revision.desc())
        .limit(1)
    )
    return {
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
