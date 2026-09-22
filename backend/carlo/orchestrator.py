import asyncio
import hashlib
import json
import re
import shlex
from datetime import UTC, datetime
from dataclasses import asdict, dataclass, replace
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .domain import (
    AttemptSignal,
    TaskStage,
    TaskStatus,
    ValidationSnapshot,
    assess_progress,
    detect_stall,
    fingerprint,
    transition,
)
from .git import Checkout, GitError, GitWorkspace, parent_branch_name
from .models import (
    AgentProfile as AgentProfileRecord,
    Attempt,
    Escalation,
    Event,
    PlanRevision,
    Task,
    ValidationRun,
)
from .model_providers import (
    CredentialCipher,
    model_runtime_evidence,
    resolve_agent_profile,
)
from .provider import AgentProfile, AgentResult, CodingAgentProvider, ContextLimitError
from .context_pack import ContextPackBudgetExceeded, ContextPackError, build_context_pack
from .execution_telemetry import ExecutionTelemetry, ToolBudgetExceeded
from .provider import PiProvider
from .planning import FeatureTask, ImplementationTask, Planner, PlanningError, PlanningQuestion, PlanningRequest
from .work_package_escalation import within_approved_scope
from .api import work_package_example

IMPLEMENTATION_LOCK = 1_128_352_847
MAX_INTERRUPTS = 3
EMPTY_DIFF_HASH = hashlib.sha256(b"").hexdigest()


class TaskStopRequested(BaseException):
    """Raised inside the Pi stream handler when the task was stopped mid-run."""
TaskRunner = Callable[[str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ValidationBatch:
    passed: bool
    snapshot: ValidationSnapshot
    fingerprint: str
    summary: str


class Orchestrator:
    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        runner: TaskRunner,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.runner = runner

    async def run_next(self) -> str | None:
        async with self.engine.connect() as lock_connection:
            await lock_connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": IMPLEMENTATION_LOCK}
            )
            await lock_connection.commit()
            try:
                task_id = await self._claim_next()
                if task_id is None:
                    return None
                try:
                    outcome = await self.runner(task_id)
                except ContextLimitError as error:
                    await self._context_limit(task_id, error)
                    await self._finish(task_id, "failed")
                    return task_id
                except GitError as error:
                    await self._interrupt(task_id, error)
                    await self._finish(task_id, "failed")
                    return task_id
                except TaskStopRequested:
                    return task_id
                except Exception as error:
                    interruptions = await self._interrupt(task_id, error)
                    if interruptions >= MAX_INTERRUPTS:
                        await self._finish(task_id, "failed")
                        return task_id
                    raise
                if not await self._still_running(task_id):
                    return task_id
                await self._finish(task_id, outcome)
                return task_id
            finally:
                await lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": IMPLEMENTATION_LOCK},
                )
                await lock_connection.commit()

    async def _context_limit(self, task_id: str, error: ContextLimitError) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            session.add(
                Event(
                    task=task,
                    type="execution.context_limit",
                    payload={"error": str(error)[:500]},
                )
            )
            await session.commit()

    async def _claim_next(self) -> str | None:
        async with self.session_factory() as session:
            active_tasks = (
                await session.scalars(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.IN_PROGRESS,
                        Task.superseded_at.is_(None),
                    )
                    .order_by(Task.created_at, Task.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            active = None
            for candidate in active_tasks:
                if await self._eligible(session, candidate):
                    active = candidate
                    break
            if active is not None:
                if active.stage == TaskStage.BLOCKED:
                    return None
                session.add(Event(task=active, type="recovery.resumed", payload={}))
                await session.commit()
                return active.id
            ready_tasks = (
                await session.scalars(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.READY,
                        Task.stage == TaskStage.QUEUED,
                        Task.superseded_at.is_(None),
                    )
                    .order_by(Task.priority.desc(), Task.created_at, Task.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            task = None
            for candidate in ready_tasks:
                if await self._eligible(session, candidate):
                    task = candidate
                    break
            if task is None:
                return None
            task.status, task.stage = transition(task.status, task.stage, "start")
            task.version += 1
            session.add(Event(task=task, type="execution.started", payload={}))
            await session.commit()
            return task.id

    async def _still_running(self, task_id: str) -> bool:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            return (
                task is not None
                and task.status == TaskStatus.IN_PROGRESS
                and task.superseded_at is None
            )

    @staticmethod
    async def _eligible(session: AsyncSession, task: Task) -> bool:
        if task.superseded_at is not None:
            return False
        if await session.scalar(
            select(func.count(Task.id)).where(
                Task.parent_task_id == task.id,
                Task.superseded_at.is_(None),
            )
        ):
            return False
        if task.parent_task_id is None:
            return await Orchestrator._root_unblocked(session, task)
        parent = await session.get(Task, task.parent_task_id)
        if parent is None or (parent.status, parent.stage) != (
            TaskStatus.IN_PROGRESS,
            TaskStage.IMPLEMENTING,
        ):
            return False
        if not await Orchestrator._root_unblocked(session, parent):
            return False
        unfinished = await session.scalar(
            select(func.count(Task.id)).where(
                Task.parent_task_id == task.parent_task_id,
                Task.superseded_at.is_(None),
                Task.subtask_position < task.subtask_position,
                Task.status != TaskStatus.DONE,
            )
        )
        return not unfinished

    @staticmethod
    async def _root_unblocked(session: AsyncSession, root: Task) -> bool:
        for dep_id in root.depends_on_task_ids:
            dependency = await session.get(Task, dep_id)
            if dependency is None or dependency.status != TaskStatus.DONE:
                return False
        blocked_ahead = await session.scalar(
            select(func.count(Task.id)).where(
                Task.project_id == root.project_id,
                Task.parent_task_id.is_(None),
                Task.id != root.id,
                Task.superseded_at.is_(None),
                Task.sequence < root.sequence,
                Task.status != TaskStatus.DONE,
            )
        )
        return not blocked_ahead

    async def _finish(self, task_id: str, outcome: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise RuntimeError(f"active task {task_id} disappeared")
            if outcome == "validated":
                if task.branch_name is not None or task.worktree_path is not None:
                    checkpoint = task.checkpoint_sha
                    if checkpoint is None:
                        raise GitError("validated task has no checkpoint")
                    target = await self._promotion_target(session, task)
                    if target is not None:
                        workspace = GitWorkspace(
                            Path(task.project.repository_path),
                            task.project.integration_branch,
                        )
                        try:
                            await workspace.promote(checkpoint)
                        except GitError as error:
                            task.status = TaskStatus.IN_PROGRESS
                            task.stage = TaskStage.BLOCKED
                            task.version += 1
                            session.add(
                                Event(
                                    task=task,
                                    type="git.integration_promotion_failed",
                                    payload={
                                        "integration_branch": task.project.integration_branch,
                                        "error": str(error),
                                    },
                                )
                            )
                            await self._sync_parent(session, task)
                            await session.commit()
                            return
                        target.checkpoint_sha = checkpoint
                        session.add(
                            Event(
                                task=target,
                                type="git.integration_promoted",
                                payload={
                                    "integration_branch": task.project.integration_branch,
                                    "checkpoint": checkpoint,
                                },
                            )
                        )
                task.stage = TaskStage.VALIDATING
                task.status, task.stage = transition(
                    task.status, task.stage, "validated"
                )
                event_type = "execution.completed"
            elif outcome == "blocked":
                task.status = TaskStatus.IN_PROGRESS
                task.stage = TaskStage.BLOCKED
                event_type = "execution.blocked"
            else:
                task.status = TaskStatus.FAILED
                task.stage = TaskStage.BLOCKED
                event_type = "execution.failed"
            task.version += 1
            session.add(Event(task=task, type=event_type, payload={"outcome": outcome}))
            await self._sync_parent(session, task)
            await session.commit()

    @staticmethod
    async def _promotion_target(
        session: AsyncSession, task: Task
    ) -> Task | None:
        if task.parent_task_id is None:
            return task
        parent = await session.scalar(
            select(Task).where(Task.id == task.parent_task_id).with_for_update()
        )
        if parent is None:
            raise GitError("parent task is missing")
        unfinished = await session.scalar(
            select(func.count(Task.id)).where(
                Task.parent_task_id == parent.id,
                Task.superseded_at.is_(None),
                Task.id != task.id,
                Task.status != TaskStatus.DONE,
            )
        )
        return parent if not unfinished else None

    @staticmethod
    async def _sync_parent(session: AsyncSession, child: Task) -> None:
        if child.parent_task_id is None or child.superseded_at is not None:
            return
        parent = await session.scalar(
            select(Task).where(Task.id == child.parent_task_id).with_for_update()
        )
        if parent is None:
            return
        siblings = list(
            (
                await session.scalars(
                    select(Task)
                    .where(
                        Task.parent_task_id == parent.id,
                        Task.superseded_at.is_(None),
                    )
                    .order_by(Task.subtask_position)
                )
            ).all()
        )
        previous = (parent.status, parent.stage)
        event_type = None
        if siblings and all(sibling.status == TaskStatus.DONE for sibling in siblings):
            parent.status, parent.stage = TaskStatus.DONE, TaskStage.COMPLETE
            parent.checkpoint_sha = siblings[-1].checkpoint_sha
            event_type = "subtasks.completed"
        elif any(sibling.status == TaskStatus.FAILED for sibling in siblings):
            parent.status, parent.stage = TaskStatus.FAILED, TaskStage.BLOCKED
            event_type = "subtasks.blocked"
        elif any(sibling.stage == TaskStage.BLOCKED for sibling in siblings):
            parent.status, parent.stage = TaskStatus.IN_PROGRESS, TaskStage.BLOCKED
            event_type = "subtasks.blocked"
        else:
            parent.status, parent.stage = TaskStatus.IN_PROGRESS, TaskStage.IMPLEMENTING
        if previous != (parent.status, parent.stage):
            parent.version += 1
            if event_type:
                session.add(Event(task=parent, type=event_type, payload={"child": child.id}))

    async def _interrupt(self, task_id: str, error: Exception) -> int:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return 0
            cycle_start = int(
                await session.scalar(
                    select(func.coalesce(func.max(Event.sequence), 0)).where(
                        Event.task_id == task_id,
                        Event.type == "task.rework.started",
                    )
                )
                or 0
            )
            interruptions = (
                await session.scalar(
                    select(func.count(Event.sequence)).where(
                        Event.task_id == task_id,
                        Event.type == "execution.interrupted",
                        Event.sequence > cycle_start,
                    )
                )
                or 0
            ) + 1
            task.version += 1
            session.add(
                Event(
                    task=task,
                    type="execution.interrupted",
                    payload={
                        "error_type": type(error).__name__,
                        "error": str(error)[:500],
                        "interruption": interruptions,
                        "limit": MAX_INTERRUPTS,
                    },
                )
            )
            await session.commit()
            return interruptions


class ImplementationPipeline:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: CodingAgentProvider,
        artifact_root: Path,
        max_attempts: int = 20,
        credential_cipher: CredentialCipher | None = None,
        context_pack_budget_tokens: int = 32_000,
        request_diagnostics: bool = False,
        max_escalations: int = 3,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.artifact_root = artifact_root
        self.max_attempts = max_attempts
        self.credential_cipher = credential_cipher
        self.context_pack_budget_tokens = context_pack_budget_tokens
        self.request_diagnostics = request_diagnostics
        self.max_escalations = max_escalations

    async def run(self, task_id: str, prior_history: list[AttemptSignal] | None = None) -> str:
        task, plan, implementation, escalation, coder_expert = await self._context(task_id)
        repository = Path(task.project.repository_path).resolve()
        workspace = GitWorkspace(repository, task.project.integration_branch)
        branch_owner_id, branch_owner_title = task.id, task.title
        if task.parent_task_id is not None:
            async with self.session_factory() as session:
                parent = await session.get(Task, task.parent_task_id)
                if parent is None:
                    raise GitError("parent task is missing")
                branch_owner_id, branch_owner_title = parent.id, parent.title
        _, previous_cycle_attempt, focused_retry = (
            await self._rework_context(task_id)
        )
        retry_feedback = (
            await self._latest_validation_feedback(task_id) if focused_retry else None
        )
        base_ref = None
        if task.parent_task_id is not None and task.subtask_position:
            async with self.session_factory() as session:
                predecessor = await session.scalar(
                    select(Task).where(
                        Task.parent_task_id == task.parent_task_id,
                        Task.superseded_at.is_(None),
                        Task.subtask_position == task.subtask_position - 1,
                    )
                )
                if predecessor is None or predecessor.checkpoint_sha is None:
                    raise GitError("previous subtask has no validated checkpoint")
                base_ref = predecessor.checkpoint_sha
        expected_branch = parent_branch_name(branch_owner_id, branch_owner_title)
        if task.worktree_path and Path(task.worktree_path).resolve() != repository:
            raise GitError("saved checkout is outside the registered project")
        if task.branch_name and task.branch_name != expected_branch:
            raise GitError("saved checkout does not use the parent task branch")
        worktree = await workspace.prepare(
            branch_owner_id, branch_owner_title, base_ref=base_ref
        )
        if task.worktree_path != str(worktree.path) or task.branch_name != worktree.branch:
            async with self.session_factory() as session:
                current = await session.get(Task, task_id)
                if current is None:
                    return "failed"
                current.branch_name = worktree.branch
                current.worktree_path = str(worktree.path)
                current.stage = TaskStage.IMPLEMENTING
                session.add(
                    Event(
                        task=current,
                        type="git.checkout_prepared",
                        payload={"branch": worktree.branch, "path": str(worktree.path)},
                    )
                )
                await session.commit()

        if task.parent_task_id is not None:
            packages = plan.metadata_json.get("implementation_tasks") or []
            if len(packages) == 1 and "files" not in packages[0]:
                technical = await self._prepare_technical_plan(task, plan, worktree)
                if technical == "blocked":
                    return "blocked"
                if technical is None:
                    return "failed"
                plan = technical

        implementation_profile = await self._resolve_profile(
            implementation.id,
            task.available_model_id,
            tuple(
                resource
                for resource in (
                    *plan.metadata_json.get("packages", []),
                    *plan.metadata_json.get("skills", []),
                )
                if isinstance(resource, str)
            ),
        )
        escalation_profile = await self._resolve_profile(escalation.id)
        await self._record_model_runtime(
            task_id, model_runtime_evidence(implementation_profile)
        )

        previous: ValidationSnapshot | None = None
        history: list[AttemptSignal] = list(prior_history or [])
        strategy = "approved_plan"
        if task.stage == TaskStage.VALIDATING:
            async with self.session_factory() as session:
                interrupted = await session.scalar(
                    select(Attempt)
                    .where(Attempt.task_id == task_id)
                    .order_by(Attempt.number.desc())
                    .limit(1)
                )
            if interrupted is None:
                return "failed"
            recovered = await self._validate(
                task,
                plan,
                worktree,
                interrupted.id,
                interrupted.number,
            )
            if recovered.passed:
                checkpoint = await workspace.checkpoint(
                    worktree, f"attempt {interrupted.number} recovered and validated"
                )
                await self._finish_attempt(
                    task_id,
                    interrupted.id,
                    "verified",
                    checkpoint,
                    None,
                    recovered,
                )
                next_plan = (
                    await self._advance_technical_slice(task_id, plan, interrupted.number)
                    if task.parent_task_id is not None else None
                )
                if next_plan is not None:
                    return await self.run(task_id, [])
                return "validated"
            previous = recovered.snapshot
            retry_feedback = recovered.summary[-6_000:]
            focused_retry = True
        start = await self._next_attempt_number(task_id)
        attempts_used = max(0, start - 1 - previous_cycle_attempt)
        attempts_left = max(0, self.max_attempts - attempts_used)
        had_attempt_capacity = attempts_left > 0
        async with self.session_factory() as session:
            latest_revision = await session.scalar(
                select(Event)
                .where(
                    Event.task_id == task_id,
                    Event.type.in_(("escalation.package_revised", "planning.technical.slice_advanced")),
                )
                .order_by(Event.sequence.desc())
                .limit(1)
            )
            local_cycle_attempt = max(
                previous_cycle_attempt,
                int(latest_revision.payload.get("previous_attempt", 0))
                if latest_revision else 0,
            )
            cycle_outcomes = list(await session.scalars(
                select(Attempt.outcome)
                .where(
                    Attempt.task_id == task_id,
                    Attempt.number > local_cycle_attempt,
                )
                .order_by(Attempt.number)
            ))
        productive_attempts_used = sum(
            outcome != "budget_exceeded" for outcome in cycle_outcomes
        )
        productive_attempts_left = (
            min(attempts_left, max(0, 3 - productive_attempts_used))
            if task.parent_task_id is not None else attempts_left
        )
        consecutive_budget_overruns = 0
        for outcome in reversed(cycle_outcomes):
            if outcome != "budget_exceeded":
                break
            consecutive_budget_overruns += 1
        packages = plan.metadata_json.get("implementation_tasks", [])
        recovery_checkpoint: str | None = None
        number = start
        while (
            attempts_left > 0
            and productive_attempts_left > 0
            and consecutive_budget_overruns < 3
        ):
            attempts_left -= 1
            current_number = number
            number += 1
            is_recovery_attempt = recovery_checkpoint is not None
            runtime_instruction = (
                self._context_recovery_instruction(task, recovery_checkpoint)
                if recovery_checkpoint
                else self._focused_retry_instruction(task, retry_feedback)
                if focused_retry
                else self._implementation_instruction(task, plan, strategy)
            )
            if task.parent_task_id is not None:
                try:
                    if not isinstance(packages, list) or len(packages) != 1:
                        raise ContextPackError("subtask has no single complete work package; replan required")
                    context_brief = plan.brief_markdown
                    if plan.metadata_json.get("feature_task"):
                        context_brief += (
                            f"\n\nTechnical evidence:\n{plan.metadata_json.get('technical_brief', '')}"
                            f"\n\nTechnical plan:\n{plan.plan_markdown}"
                        )
                        predecessors = plan.metadata_json.get("verified_predecessors") or []
                        if predecessors:
                            context_brief += f"\n\nVerified predecessors:\n{json.dumps(predecessors, ensure_ascii=False)}"
                    if is_recovery_attempt or focused_retry:
                        pack = build_context_pack(
                            worktree.path, packages[0],
                            max_tokens=self.context_pack_budget_tokens,
                            brief=context_brief,
                            include_file_contents=False,
                        )
                    else:
                        pack = build_context_pack(
                            worktree.path, packages[0],
                            max_tokens=self.context_pack_budget_tokens,
                            brief=context_brief,
                        )
                        runtime_instruction = f"Current strategy: {strategy}"
                except ContextPackError as error:
                    await self._record_context_pack_replan(task_id, error)
                    return "failed"
                instruction = (
                    f"{pack}\n"
                    f"Runtime state:\n{runtime_instruction}\n"
                    "Work only inside the project checkout. Run no undeclared deployment commands."
                )
            else:
                instruction = runtime_instruction
            attempt_id = await self._start_attempt(
                task_id, implementation.id, current_number, instruction
            )
            meter = None
            if task.parent_task_id is not None:
                package = packages[0]
                meter = ExecutionTelemetry(
                    worktree.path,
                    {file["path"] for file in package["files"]},
                    package.get("budget", {}).get("max_tool_calls", 30),
                )
            step_handler = self._pi_step_handler(task_id)

            async def on_event(event: dict[str, Any]) -> None:
                if meter is not None:
                    old_reads = len(meter.outside_reads)
                    was_soft = meter.soft_budget_exceeded
                    meter.observe(event)
                    if not was_soft and meter.soft_budget_exceeded:
                        await self._record_soft_tool_budget(
                            task_id, current_number, meter.tool_calls, meter.estimated_tool_calls
                        )
                    for path in meter.outside_reads[old_reads:]:
                        await self._record_outside_read(task_id, current_number, path)
                await step_handler(event)

            try:
                args = (implementation_profile, instruction, str(worktree.path), f"{task_id}-implementation-{current_number}")
                if meter is not None and isinstance(self.provider, PiProvider):
                    result = await self.provider.run(
                        *args, on_event=on_event,
                        max_tool_calls=meter.hard_tool_calls,
                        request_diagnostics=self.request_diagnostics,
                    )
                else:
                    result = await self.provider.run(*args, on_event=on_event)
            except ToolBudgetExceeded as error:
                await self._mark_attempt_outcome(task_id, attempt_id, "budget_exceeded", str(error))
                if meter is not None:
                    await self._record_session_metrics(task_id, current_number, meter.summary("budget_exceeded"))
                history.append(AttemptSignal("tool_budget_exceeded", await workspace.diff_hash(worktree), strategy))
                focused_retry = True
                consecutive_budget_overruns += 1
                if consecutive_budget_overruns >= 3:
                    break
                continue
            except ContextLimitError as error:
                productive_attempts_left -= 1
                was_recovery = recovery_checkpoint is not None
                recovery_checkpoint = await workspace.checkpoint(
                    worktree, f"attempt {current_number} reached context limit"
                )
                await self._record_context_limit(
                    task_id, attempt_id, current_number, recovery_checkpoint, error
                )
                if meter is not None:
                    await self._record_session_metrics(task_id, current_number, meter.summary("failed"))
                if was_recovery:
                    return "failed"
                continue
            except Exception:
                if meter is not None:
                    await self._record_session_metrics(task_id, current_number, meter.summary("failed"))
                raise
            productive_attempts_left -= 1
            consecutive_budget_overruns = 0
            recovery_checkpoint = None
            focused_retry = False
            retry_feedback = None
            await self._record_provider_result(task_id, attempt_id, current_number, result)
            deviation = major_deviation(result.output)
            if deviation:
                await self._block_for_amendment(
                    task_id, plan, attempt_id, deviation
                )
                if meter is not None:
                    await self._record_session_metrics(task_id, current_number, meter.summary("escalated"))
                return "blocked"
            if (
                task.parent_task_id is not None
                and packages[0].get("changes")
                and not is_recovery_attempt
                and await workspace.diff_hash(worktree) == EMPTY_DIFF_HASH
            ):
                await self._mark_attempt_outcome(
                    task_id, attempt_id, "no_progress", "provider completed without repository changes"
                )
                if meter is not None:
                    await self._record_session_metrics(task_id, current_number, meter.summary("no_progress"))
                history.append(AttemptSignal("no_repository_changes", EMPTY_DIFF_HASH, strategy))
                focused_retry = True
                continue
            await self._set_stage(task_id, TaskStage.VALIDATING)
            batch = await self._validate(task, plan, worktree, attempt_id, current_number)
            if batch.passed:
                checkpoint = await workspace.checkpoint(
                    worktree, f"attempt {current_number} validated"
                )
                await self._finish_attempt(
                    task_id, attempt_id, "verified", checkpoint, None, batch
                )
                next_plan = (
                    await self._advance_technical_slice(task_id, plan, current_number)
                    if task.parent_task_id is not None else None
                )
                if next_plan is not None:
                    if meter is not None:
                        await self._record_session_metrics(task_id, current_number, meter.summary("slice_done"))
                    return await self.run(task_id, [])
                if meter is not None:
                    await self._record_session_metrics(task_id, current_number, meter.summary("done"))
                return "validated"

            diff_hash = await workspace.diff_hash(worktree)
            signal = AttemptSignal(batch.fingerprint, diff_hash, strategy)
            history.append(signal)
            await self._finish_attempt(
                task_id, attempt_id, "validation_failed", None, diff_hash, batch
            )
            if meter is not None:
                await self._record_session_metrics(task_id, current_number, meter.summary("failed"))
            if previous and assess_progress(previous, batch.snapshot).is_progress:
                checkpoint = await workspace.checkpoint(
                    worktree, f"attempt {current_number} improved validation"
                )
                await self._set_checkpoint(task_id, checkpoint)
            previous = batch.snapshot
            retry_feedback = batch.summary[-6_000:]
            focused_retry = True
            stalled = detect_stall(history)
            if stalled and task.parent_task_id is None:
                root_count = await self._escalation_count(task_id)
                if root_count >= 1 and coder_expert is not None:
                    await self._upgrade_model(task_id, coder_expert)
                strategy = await self._escalate(
                    task,
                    plan,
                    escalation_profile,
                    worktree,
                    stalled.reason,
                    history,
                    batch,
                )
            await self._set_stage(task_id, TaskStage.IMPLEMENTING)
        if task.parent_task_id is not None and (
            had_attempt_capacity or productive_attempts_used or consecutive_budget_overruns
        ):
            count = await self._work_package_escalation_count(task_id)
            if count < self.max_escalations:
                outcome = await self._escalate_work_package(
                    task, plan, escalation_profile, worktree, packages[0],
                    detect_stall(history).reason if detect_stall(history) else "local_attempts_exhausted", history,
                    escalation_count=count, coder_expert=coder_expert,
                )
                if outcome == "retry":
                    return await self.run(task_id, history)
                if outcome == "blocked":
                    return "blocked"
        return "failed"

    async def _prepare_technical_plan(
        self, task: Task, feature_plan: PlanRevision, worktree: Checkout,
    ) -> PlanRevision | str | None:
        """Resolve a feature outcome into ordered internal execution slices."""
        feature = FeatureTask.model_validate(feature_plan.metadata_json["implementation_tasks"][0])
        async with self.session_factory() as session:
            record = await session.scalar(select(AgentProfileRecord).where(AgentProfileRecord.name == "plan"))
            answer = await session.scalar(select(Event).where(
                Event.task_id == task.id, Event.type == "planning.technical.answer",
            ).order_by(Event.sequence.desc()).limit(1))
            predecessors = list((await session.scalars(select(Task).where(
                Task.parent_task_id == task.parent_task_id,
                Task.subtask_position < task.subtask_position,
                Task.status == TaskStatus.DONE,
                Task.superseded_at.is_(None),
            ).order_by(Task.subtask_position))).all())
        verified_predecessors = [
            {"task_id": previous.id, "title": previous.title, "outcome": previous.goal, "checkpoint": previous.checkpoint_sha}
            for previous in predecessors if previous.checkpoint_sha
        ]
        if record is None:
            raise RuntimeError("technical planning profile is missing")
        profile = await self._resolve_profile(record.id)
        profile = replace(profile, tools=tuple(tool for tool in profile.tools if tool in {"read", "grep", "find", "ls"}))
        handoff = json.dumps({
            "approved_parent_brief": feature_plan.brief_markdown,
            "approved_feature": feature.model_dump(),
            "verified_predecessors": verified_predecessors,
            "user_answer": answer.payload if answer else None,
            "instruction": (
                "Plan only this approved feature on the current checkout. Return one or more ordered technical "
                "execution slices. Split now if necessary; slices are internal to this child Task and must not "
                "need later sub-splitting. Together they must preserve the approved objective, interfaces, "
                "constraints, and acceptance criteria."
            ),
        }, ensure_ascii=False)
        async with self.session_factory() as session:
            session.add(Event(task_id=task.id, type="planning.technical.started", payload={"feature_id": feature.id}))
            await session.commit()
        try:
            result = await Planner(self.provider, profile).plan(PlanningRequest(
                task.project, task.title, task.goal,
                f"{task.id}-technical-plan-{feature_plan.revision}",
                handoff=handoff, technical_plan=True, repository_path=str(worktree.path),
            ))
            if isinstance(result, PlanningQuestion):
                async with self.session_factory() as session:
                    current = await session.get(Task, task.id)
                    current.planning_question = {"text": result.text}
                    session.add(Event(task=current, type="planning.technical.question", payload={"question": result.text}))
                    await session.commit()
                return "blocked"
            packages = result.metadata.implementation_tasks
            if not packages or any(not isinstance(package, ImplementationTask) for package in packages):
                raise PlanningError("technical plan did not return complete execution slices")
            if [package.position for package in packages] != list(range(len(packages))):
                raise PlanningError("technical slices are not zero-based and ordered")
            if any(not set(feature.constraints).issubset(package.constraints) for package in packages):
                raise PlanningError("technical slice weakened approved feature constraints")
            inherited_interfaces = {item for package in packages for item in package.interfaces}
            inherited_done_when = {item for package in packages for item in package.done_when}
            if not set(feature.interfaces).issubset(inherited_interfaces) or not set(feature.done_when).issubset(inherited_done_when):
                raise PlanningError("technical slices do not preserve approved interfaces or acceptance criteria")
        except (PlanningError, ValueError) as error:
            async with self.session_factory() as session:
                session.add(Event(task_id=task.id, type="planning.technical.failed", payload={"error": str(error)[:1000]}))
                await session.commit()
            return None
        async with self.session_factory() as session:
            current = await session.get(Task, task.id)
            revision = PlanRevision(
                task=current,
                revision=feature_plan.revision + 1,
                brief_markdown=feature_plan.brief_markdown,
                plan_markdown=result.plan_markdown,
                metadata_json={
                    **feature_plan.metadata_json,
                    "implementation_tasks": [packages[0].model_dump()],
                    "technical_packages": [package.model_dump() for package in packages],
                    "technical_package_index": 0,
                    "skills": result.metadata.skills,
                    "packages": result.metadata.packages,
                    "feature_task": feature.model_dump(),
                    "technical_brief": result.brief_markdown,
                    "verified_predecessors": verified_predecessors,
                },
                approved_at=datetime.now(UTC),
            )
            current.approved_plan_revision = revision.revision
            current.version += 1
            session.add_all([revision, Event(
                task=current,
                type="planning.technical.completed",
                payload={"revision": revision.revision, "slices": len(packages)},
            )])
            await session.commit()
            return revision

    async def _advance_technical_slice(
        self, task_id: str, plan: PlanRevision, previous_attempt: int,
    ) -> PlanRevision | None:
        slices = plan.metadata_json.get("technical_packages") or []
        index = int(plan.metadata_json.get("technical_package_index", 0))
        next_index = index + 1
        if not isinstance(slices, list) or next_index >= len(slices):
            return None
        async with self.session_factory() as session:
            current = await session.get(Task, task_id)
            if current is None:
                raise RuntimeError("task disappeared while advancing technical slice")
            revision_number = int(await session.scalar(
                select(func.coalesce(func.max(PlanRevision.revision), 0) + 1).where(
                    PlanRevision.task_id == task_id
                )
            ) or 1)
            revision = PlanRevision(
                task=current,
                revision=revision_number,
                brief_markdown=plan.brief_markdown,
                plan_markdown=plan.plan_markdown,
                metadata_json={
                    **plan.metadata_json,
                    "implementation_tasks": [slices[next_index]],
                    "technical_package_index": next_index,
                },
                approved_at=datetime.now(UTC),
            )
            current.approved_plan_revision = revision_number
            current.stage = TaskStage.IMPLEMENTING
            current.version += 1
            session.add_all([
                revision,
                Event(
                    task=current,
                    type="planning.technical.slice_advanced",
                    payload={
                        "revision": revision_number,
                        "slice": next_index,
                        "total_slices": len(slices),
                        "previous_attempt": previous_attempt,
                    },
                ),
            ])
            await session.commit()
            return revision

    async def _work_package_escalation_count(self, task_id: str) -> int:
        async with self.session_factory() as session:
            retry = await session.scalar(
                select(Event.created_at)
                .where(Event.task_id == task_id, Event.type == "task.retry.started")
                .order_by(Event.sequence.desc())
                .limit(1)
            )
            query = select(func.count(Escalation.id)).where(
                Escalation.task_id == task_id,
                Escalation.reason == "work_package",
            )
            if retry is not None:
                query = query.where(Escalation.created_at >= retry)
            return int(await session.scalar(query) or 0)

    async def _latest_validation_feedback(self, task_id: str) -> str | None:
        async with self.session_factory() as session:
            run = await session.scalar(
                select(ValidationRun)
                .where(ValidationRun.task_id == task_id, ValidationRun.exit_code != 0)
                .order_by(ValidationRun.id.desc())
                .limit(1)
            )
        return run.summary[-6_000:] if run and run.summary else None

    async def _escalation_count(self, task_id: str) -> int:
        async with self.session_factory() as session:
            return int(await session.scalar(select(func.count(Escalation.id)).where(
                Escalation.task_id == task_id,
            )) or 0)

    async def _upgrade_model(self, task_id: str, coder_expert: AgentProfileRecord) -> None:
        if coder_expert.available_model_id is None:
            return
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.available_model_id == coder_expert.available_model_id:
                return
            task.available_model_id = coder_expert.available_model_id
            task.version += 1
            session.add(Event(task=task, type="escalation.model_upgraded", payload={
                "profile": coder_expert.name,
                "available_model_id": coder_expert.available_model_id,
            }))
            await session.commit()

    async def _escalate_work_package(
        self, task: Task, plan: PlanRevision, profile: AgentProfile,
        checkout: Checkout, package: dict[str, Any], reason: str,
        history: list[AttemptSignal], *,
        escalation_count: int = 0,
        coder_expert: AgentProfileRecord | None = None,
    ) -> str:
        workspace = GitWorkspace(checkout.path, task.project.integration_branch)
        diff = await workspace._git("diff", "HEAD", "--", cwd=checkout.path)
        status = await workspace._git("status", "--short", cwd=checkout.path)
        async with self.session_factory() as session:
            outside = (await session.scalars(select(Event).where(
                Event.task_id == task.id,
                Event.type == "execution.outside_package_read",
            ))).all()
            validations = (await session.scalars(select(ValidationRun).where(
                ValidationRun.task_id == task.id,
            ).order_by(ValidationRun.created_at.desc()).limit(4))).all()
            evidence = {
                "package": package,
                "reason": reason,
                "diff": diff[:16_000],
                "git_status": status[:2_000],
                "test_errors": [run.summary[-4_000:] for run in validations],
                "outside_reads": [event.payload.get("path") for event in outside[-40:]],
                "history": [asdict(item) for item in history],
            }
            record = Escalation(task_id=task.id, reason="work_package", evidence=evidence)
            session.add_all([record, Event(task_id=task.id, type="escalation.started", payload={"reason": reason})])
            await session.commit()
            escalation_id = record.id
        package_example = work_package_example()
        package_example["budget"]["max_tool_calls"] = package.get("budget", {}).get("max_tool_calls", 30)
        instruction = (
            "Diagnose this failed technical slice. Return JSON with action revise or blocked; "
            "for revise include one complete package under \"package\"; include diagnosis. "
            "Never split this slice: needing a sub-split means the technical plan was undersized and must be replanned. "
            "Preserve the approved objective, "
            "interfaces, constraints, verification, and file scope unless human approval is needed. "
            "Existing uncommitted changes were generated by earlier attempts and may be corrected or "
            "reverted without human approval, including removing their out-of-scope additions. "
            "Failing tests are not a human blocker. Use blocked only for a missing product decision, "
            "credential, external dependency, or required scope expansion. "
            "A revised slice may not raise its planning estimate above the original slice estimate. "
            "Every package (revise or split) must match exactly this shape, with no other fields:\n"
            + json.dumps(package_example)
            + "\n"
            + json.dumps(evidence)
        )
        try:
            result = await self.provider.run(
                profile, instruction, str(checkout.path), f"{task.id}-work-package-escalation-{escalation_id}"
            )
            output = json.loads(result.output)
            if not isinstance(output, dict) or output.get("action") not in {"revise", "blocked"}:
                raise ValueError("planner returned no valid escalation action")
        except (ValueError, RuntimeError) as error:
            async with self.session_factory() as session:
                record = await session.get(Escalation, escalation_id)
                record.status = "failed"
                record.diagnosis = str(error)[:500]
                session.add(Event(task_id=task.id, type="escalation.failed", payload={"error": str(error)[:500]}))
                await session.commit()
            return "failed"
        action = output["action"]
        revised = output.get("package") if action == "revise" else None
        automatic, automatic_rejected_reason = (
            within_approved_scope(package, revised)
            if isinstance(revised, dict)
            else (False, "planner returned no 'package' for a revise action")
        )
        async with self.session_factory() as session:
            current = await session.get(Task, task.id)
            record = await session.get(Escalation, escalation_id)
            record.status = "completed"
            record.diagnosis = str(output.get("diagnosis", ""))[:2_000]
            record.strategy = action
            if action == "blocked":
                session.add(Event(task=current, type="escalation.human_required", payload={"reason": record.diagnosis}))
                await session.commit()
                return "blocked"
            revision = int(await session.scalar(select(func.coalesce(func.max(PlanRevision.revision), 0) + 1).where(
                PlanRevision.task_id == task.id,
            )) or 1)
            previous_attempt = int(await session.scalar(select(func.coalesce(func.max(Attempt.number), 0)).where(
                Attempt.task_id == task.id,
            )) or 0)
            metadata = {**plan.metadata_json, "implementation_tasks": [revised] if revised else output.get("packages", []), "escalation_source": escalation_id}
            amendment = PlanRevision(
                task=current, revision=revision, brief_markdown=plan.brief_markdown,
                plan_markdown=plan.plan_markdown, metadata_json=metadata,
                approved_at=datetime.now(UTC) if automatic else None,
            )
            if automatic:
                current.approved_plan_revision = revision
            else:
                current.stage = TaskStage.BLOCKED
            current.version += 1
            session.add_all([amendment, Event(task=current,
                type="escalation.package_revised" if automatic else "escalation.approval_required",
                payload={
                    "revision": revision, "action": action, "automatic": automatic,
                    "previous_attempt": previous_attempt,
                    **({} if automatic else {"rejected_reason": automatic_rejected_reason}),
                },
            )])
            if automatic and coder_expert is not None:
                await session.flush()
                if coder_expert.available_model_id is not None and current.available_model_id != coder_expert.available_model_id:
                    current.available_model_id = coder_expert.available_model_id
                    session.add(Event(task=current, type="escalation.model_upgraded", payload={
                        "profile": coder_expert.name,
                        "available_model_id": coder_expert.available_model_id,
                    }))
            await session.commit()
        return "retry" if automatic else "blocked"

    async def _record_outside_read(self, task_id: str, attempt: int, path: str) -> None:
        async with self.session_factory() as session:
            session.add(Event(task_id=task_id, type="execution.outside_package_read", payload={"attempt": attempt, "path": path}))
            await session.commit()

    async def _record_soft_tool_budget(
        self, task_id: str, attempt: int, tool_calls: int, estimated_tool_calls: int,
    ) -> None:
        async with self.session_factory() as session:
            session.add(Event(
                task_id=task_id,
                type="execution.soft_tool_budget_exceeded",
                payload={
                    "attempt": attempt,
                    "tool_calls": tool_calls,
                    "soft_limit": 50,
                    "estimated_tool_calls": estimated_tool_calls,
                },
            ))
            await session.commit()

    async def _record_session_metrics(self, task_id: str, attempt: int, summary: dict[str, Any]) -> None:
        async with self.session_factory() as session:
            session.add(Event(task_id=task_id, type="execution.session_metrics", payload={"attempt": attempt, **summary}))
            await session.commit()

    async def _mark_attempt_outcome(self, task_id: str, attempt_id: int, outcome: str, reason: str) -> None:
        async with self.session_factory() as session:
            attempt = await session.get(Attempt, attempt_id)
            if attempt is not None:
                attempt.outcome = outcome
            session.add(Event(task_id=task_id, type=f"execution.{outcome}", payload={"reason": reason[:500]}))
            await session.commit()

    async def _record_context_pack_replan(self, task_id: str, error: ContextPackError) -> None:
        payload: dict[str, Any] = {"reason": str(error)[:500]}
        if isinstance(error, ContextPackBudgetExceeded):
            payload.update(
                estimated_tokens=error.estimated_tokens,
                budget_tokens=error.max_tokens,
            )
        async with self.session_factory() as session:
            session.add(Event(task_id=task_id, type="context_pack.replan_required", payload=payload))
            await session.commit()

    async def _context(
        self, task_id: str
    ) -> tuple[Task, PlanRevision, AgentProfileRecord, AgentProfileRecord]:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.approved_plan_revision is None:
                raise RuntimeError("task has no approved plan")
            plan = await session.scalar(
                select(PlanRevision).where(
                    PlanRevision.task_id == task_id,
                    PlanRevision.revision == task.approved_plan_revision,
                )
            )
            profiles = {
                profile.name: profile
                for profile in (
                    await session.scalars(
                        select(AgentProfileRecord).where(
                            AgentProfileRecord.name.in_(
                                ("implementation", "escalation", "coder-expert")
                            )
                        )
                    )
                ).all()
            }
            if plan is None or not {"implementation", "escalation"}.issubset(profiles):
                raise RuntimeError("plan or agent profiles are missing")
            return (
                task,
                plan,
                profiles["implementation"],
                profiles["escalation"],
                profiles.get("coder-expert"),
            )

    async def _resolve_profile(
        self,
        profile_id: int,
        task_model_id: int | None = None,
        extra_skills: tuple[str, ...] = (),
    ) -> AgentProfile:
        async with self.session_factory() as session:
            record = await session.get(AgentProfileRecord, profile_id)
            if record is None:
                raise RuntimeError("agent profile is missing")
            return await resolve_agent_profile(
                session,
                record,
                self.credential_cipher,
                task_model_id=task_model_id,
                extra_skills=extra_skills,
                default_tools=("read", "bash", "edit", "write", "grep", "find", "ls"),
            )

    async def _record_model_runtime(
        self, task_id: str, evidence: dict[str, Any] | None
    ) -> None:
        if evidence is None:
            return
        async with self.session_factory() as session:
            session.add(
                Event(task_id=task_id, type="model.runtime_selected", payload=evidence)
            )
            await session.commit()

    async def _next_attempt_number(self, task_id: str) -> int:
        async with self.session_factory() as session:
            maximum = await session.scalar(
                select(func.coalesce(func.max(Attempt.number), 0)).where(
                    Attempt.task_id == task_id
                )
            )
            return int(maximum or 0) + 1

    async def _rework_context(self, task_id: str) -> tuple[int, int, bool]:
        async with self.session_factory() as session:
            event = await session.scalar(
                select(Event)
                .where(
                    Event.task_id == task_id,
                    Event.type.in_(("task.rework.started", "task.retry.started")),
                )
                .order_by(Event.sequence.desc())
                .limit(1)
            )
            if event is None:
                return 0, 0, False
            return (
                int(event.payload.get("cycle", 0)),
                int(event.payload["previous_attempt"]),
                event.type == "task.retry.started"
                and not event.payload.get("fresh_checkout", False),
            )

    async def _start_attempt(
        self, task_id: str, profile_id: int, number: int, instruction: str
    ) -> int:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise RuntimeError("task disappeared")
            task.active_profile_id = profile_id
            task.stage = TaskStage.IMPLEMENTING
            attempt = Attempt(
                task_id=task_id,
                number=number,
                profile_id=profile_id,
                provider_session_id=f"{task_id}-implementation-{number}",
                instruction=instruction,
            )
            session.add_all(
                [attempt, Event(task=task, type="implementation.attempt_started", payload={"number": number})]
            )
            await session.commit()
            return attempt.id

    def _pi_step_handler(self, task_id: str):
        """Stream Pi step events into the events table for live UI visibility."""

        async def on_event(event: dict) -> None:
            kind = event.get("type")
            summary: str | None = None
            if isinstance(event.get("output"), str) and event["output"].strip():
                summary = event["output"].strip()
            message = event.get("message")
            if isinstance(message, dict):
                if message.get("stopReason") == "error" and isinstance(
                    message.get("errorMessage"), str
                ):
                    summary = message["errorMessage"]
                elif isinstance(message.get("content"), list):
                    for part in message["content"]:
                        if not isinstance(part, dict):
                            continue
                        text = part.get("text") or part.get("reasoning")
                        if isinstance(text, str) and text.strip():
                            summary = text.strip()
                            break
            if summary is None and kind != "agent_end":
                return
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                if task is None or task.status != TaskStatus.IN_PROGRESS:
                    raise TaskStopRequested()
                session.add(
                    Event(
                        task_id=task_id,
                        type="pi.step",
                        payload={"kind": kind, "summary": (summary or "").replace("\x00", "")},
                    )
                )
                await session.commit()

        return on_event

    async def _validate(
        self,
        task: Task,
        plan: PlanRevision,
        worktree: Checkout,
        attempt_id: int,
        attempt_number: int,
    ) -> ValidationBatch:
        if task.parent_task_id is None:
            commands = plan.metadata_json.get("validation_commands") or task.project.validation_commands
        else:
            packages = plan.metadata_json.get("implementation_tasks") or []
            commands = list(packages[0].get("verification", {}).get("commands", [])) if len(packages) == 1 else []
            technical_packages = plan.metadata_json.get("technical_packages") or []
            technical_index = int(plan.metadata_json.get("technical_package_index", 0))
            final_technical_slice = not technical_packages or technical_index >= len(technical_packages) - 1
            async with self.session_factory() as session:
                later = await session.scalar(select(func.count(Task.id)).where(
                    Task.parent_task_id == task.parent_task_id,
                    Task.superseded_at.is_(None),
                    Task.subtask_position > task.subtask_position,
                ))
                if not later and final_technical_slice:
                    parent = await session.get(Task, task.parent_task_id)
                    parent_plan = await session.scalar(select(PlanRevision).where(
                        PlanRevision.task_id == parent.id,
                        PlanRevision.revision == parent.approved_plan_revision,
                    )) if parent else None
                    if parent_plan:
                        commands.extend(parent_plan.metadata_json.get("validation_commands") or task.project.validation_commands)
            commands = list(dict.fromkeys(commands))
        if not commands:
            return ValidationBatch(
                False,
                ValidationSnapshot(1, 0),
                fingerprint(1, "no validation commands declared"),
                "No validation commands declared",
            )
        failures = 0
        completed = 0
        summaries: list[str] = []
        for index, command in enumerate(commands, start=1):
            artifact = self.artifact_root / task.id / f"attempt-{attempt_number}" / f"validation-{index}.log"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            try:
                arguments = shlex.split(command)
                if not arguments:
                    raise ValueError("empty validation command")
                process = await asyncio.create_subprocess_exec(
                    *arguments,
                    cwd=worktree.path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await process.communicate()
                output = stdout.decode(errors="replace")
                exit_code = process.returncode or 0
                missing_tests = _missing_requested_xcode_tests(arguments, output)
                if exit_code == 0 and missing_tests:
                    output += "\nCARLO: requested tests did not run: " + ", ".join(missing_tests)
                    exit_code = 1
                classification = "VERIFIED" if exit_code == 0 else "PARTIALLY_VERIFIED"
            except (FileNotFoundError, ValueError) as error:
                output = str(error)
                exit_code = 127
                classification = "UNVERIFIABLE"
            artifact.write_text(output)
            summary = output[-4000:]
            summaries.append(summary)
            failure_count = 0 if exit_code == 0 else _failure_count(output)
            failures += failure_count
            completed += int(exit_code == 0)
            async with self.session_factory() as session:
                session.add(
                    ValidationRun(
                        task_id=task.id,
                        attempt_id=attempt_id,
                        command=command,
                        exit_code=exit_code,
                        classification=classification,
                        summary=summary,
                        artifact_path=str(artifact),
                        failure_count=failure_count,
                    )
                )
                await session.commit()
        combined = "\n".join(summaries)
        return ValidationBatch(
            failures == 0,
            ValidationSnapshot(failures, completed),
            fingerprint(0 if failures == 0 else 1, combined),
            combined[-4000:],
        )

    async def _finish_attempt(
        self,
        task_id: str,
        attempt_id: int,
        outcome: str,
        checkpoint: str | None,
        diff_hash: str | None,
        batch: ValidationBatch,
    ) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            attempt = await session.get(Attempt, attempt_id)
            if task is None or attempt is None:
                raise RuntimeError("attempt disappeared")
            attempt.outcome = outcome
            attempt.checkpoint_sha = checkpoint
            attempt.diff_hash = diff_hash
            attempt.error_fingerprint = None if batch.passed else batch.fingerprint
            attempt.progress = {
                "failures": batch.snapshot.failures,
                "completed_steps": batch.snapshot.completed_steps,
            }
            if checkpoint:
                task.checkpoint_sha = checkpoint
            await session.commit()

    async def _record_context_limit(
        self,
        task_id: str,
        attempt_id: int,
        attempt_number: int,
        checkpoint: str,
        error: ContextLimitError,
    ) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            attempt = await session.get(Attempt, attempt_id)
            if task is None or attempt is None:
                raise RuntimeError("task or attempt disappeared")
            attempt.outcome = "context_limit"
            attempt.checkpoint_sha = checkpoint
            task.checkpoint_sha = checkpoint
            session.add(
                Event(
                    task=task,
                    type="execution.context_limit",
                    payload={
                        "attempt": attempt_number,
                        "checkpoint": checkpoint,
                        "error": str(error)[:500],
                        "recovery": "new_session",
                    },
                )
            )
            await session.commit()

    async def _record_provider_result(
        self,
        task_id: str,
        attempt_id: int,
        attempt_number: int,
        result: AgentResult,
    ) -> None:
        artifact = (
            self.artifact_root
            / task_id
            / f"attempt-{attempt_number}"
            / "provider.json"
        )
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps(
                {
                    "session_id": result.session_id,
                    "exit_code": result.exit_code,
                    "output": result.output,
                    "events": result.events,
                },
                default=str,
            )
        )
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            attempt = await session.get(Attempt, attempt_id)
            if task is None or attempt is None:
                raise RuntimeError("task or attempt disappeared")
            attempt.artifact_path = str(artifact)
            session.add(
                Event(
                    task=task,
                    type="agent.completed",
                    payload={
                        "session_id": result.session_id,
                        "event_count": len(result.events),
                        "skills": list(result.used_skills),
                        "revisions": result.resource_revisions,
                        "packages": result.loaded_packages,
                    },
                )
            )
            await session.commit()

    async def _set_stage(self, task_id: str, stage: TaskStage) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise RuntimeError("task disappeared")
            task.stage = stage
            await session.commit()

    async def _set_checkpoint(self, task_id: str, checkpoint: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise RuntimeError("task disappeared")
            task.checkpoint_sha = checkpoint
            session.add(
                Event(task=task, type="git.checkpoint_created", payload={"sha": checkpoint})
            )
            await session.commit()

    async def _block_for_amendment(
        self,
        task_id: str,
        plan: PlanRevision,
        attempt_id: int,
        deviation: dict[str, str],
    ) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            attempt = await session.get(Attempt, attempt_id)
            if task is None or attempt is None:
                raise RuntimeError("task or attempt disappeared")
            revision = (
                await session.scalar(
                    select(func.coalesce(func.max(PlanRevision.revision), 0) + 1).where(
                        PlanRevision.task_id == task_id
                    )
                )
            ) or 1
            amendment = PlanRevision(
                task=task,
                revision=revision,
                brief_markdown=plan.brief_markdown,
                plan_markdown=(
                    f"{plan.plan_markdown}\n\n## Proposed amendment\n"
                    f"{deviation['summary']}\n\n{deviation['reason']}"
                ),
                metadata_json={**plan.metadata_json, "amendment": deviation},
            )
            attempt.outcome = "major_deviation"
            task.stage = TaskStage.BLOCKED
            task.version += 1
            session.add_all(
                [
                    amendment,
                    Event(
                        task=task,
                        type="plan.amendment_proposed",
                        payload={"revision": revision, **deviation},
                    ),
                ]
            )
            await session.commit()

    async def _escalate(
        self,
        task: Task,
        plan: PlanRevision,
        profile: AgentProfile,
        worktree: Checkout,
        reason: str,
        history: list[AttemptSignal],
        batch: ValidationBatch,
    ) -> str:
        evidence: dict[str, Any] = {
            "reason": reason,
            "goal": task.goal,
            "brief": plan.brief_markdown,
            "plan": plan.plan_markdown,
            "checkpoint": task.checkpoint_sha,
            "validation": batch.summary,
            "history": [asdict(signal) for signal in history],
            "skills": plan.metadata_json.get("skills", []),
        }
        async with self.session_factory() as session:
            escalation = Escalation(task_id=task.id, reason=reason, evidence=evidence)
            session.add(escalation)
            await session.commit()
            escalation_id = escalation.id
        instruction = (
            "Diagnose the stalled implementation and return JSON with diagnosis and strategy.\n"
            + json.dumps(evidence)
        )
        result = await self.provider.run(
            profile,
            instruction,
            str(worktree.path),
            f"{task.id}-escalation-{escalation_id}",
        )
        output = json.loads(result.output)
        strategy = str(output["strategy"])
        async with self.session_factory() as session:
            record = await session.get(Escalation, escalation_id)
            current = await session.get(Task, task.id)
            if record is None or current is None:
                raise RuntimeError("escalation disappeared")
            record.diagnosis = str(output["diagnosis"])
            record.strategy = strategy
            record.status = "completed"
            session.add(
                Event(task=current, type="escalation.completed", payload={"reason": reason})
            )
            await session.commit()
        return strategy

    @staticmethod
    def _implementation_instruction(
        task: Task, plan: PlanRevision, strategy: str
    ) -> str:
        if task.parent_task_id is not None:
            return (
                f"Implement {task.id}: {task.goal}\n\n"
                f"Current strategy: {strategy}\n"
                "Work only inside this worktree. Run no undeclared deployment commands."
            )
        implementation_tasks = plan.metadata_json.get("implementation_tasks", [])
        return (
            f"Implement {task.id}: {task.goal}\n\n"
            f"Approved plan:\n{plan.plan_markdown}\n\n"
            f"Implementation tasks:\n{json.dumps(implementation_tasks)}\n\n"
            f"Current strategy: {strategy}\n"
            "Work only inside this worktree. Run no undeclared deployment commands."
        )

    @staticmethod
    def _context_recovery_instruction(task: Task, checkpoint: str) -> str:
        return (
            f"Continue only subtask {task.id}: {task.goal}\n\n"
            "The previous session reached its context limit. Its work is saved in "
            f"the current worktree at checkpoint {checkpoint}. Inspect the existing "
            "files and recent Git history, then finish this subtask from that state. "
            "Do not restart the broader plan. Run the repository's relevant tests."
        )

    @staticmethod
    def _focused_retry_instruction(task: Task, validation_feedback: str | None = None) -> str:
        checkpoint = f" at checkpoint {task.checkpoint_sha}" if task.checkpoint_sha else ""
        feedback = (
            f"\n\nThe previous validation failed. Fix these exact failures before rerunning it:\n{validation_feedback}"
            if validation_feedback else ""
        )
        return (
            f"Retry only subtask {task.id}: {task.goal}\n\n"
            f"Continue from the existing worktree{checkpoint}. Inspect the current "
            "files and recent Git history, fix the failed implementation, and run the "
            "repository's relevant tests. Do not restart or request the broader plan."
            f"{feedback}"
        )


def _failure_count(output: str) -> int:
    match = re.search(r"(\d+)\s+failed", output, re.IGNORECASE)
    return int(match.group(1)) if match else 1


def _missing_requested_xcode_tests(arguments: list[str], output: str) -> list[str]:
    if not arguments or Path(arguments[0]).name != "xcodebuild" or "test" not in arguments:
        return []
    selectors: list[str] = []
    for index, argument in enumerate(arguments):
        if argument.startswith("-only-testing:"):
            selectors.append(argument.split(":", 1)[1])
        elif argument == "-only-testing" and index + 1 < len(arguments):
            selectors.append(arguments[index + 1])
    suite_markers = re.findall(
        r"^\s*Test Suite ['\"]([^'\"]+)['\"] (?:started|passed|failed)\b",
        output,
        re.IGNORECASE | re.MULTILINE,
    )
    case_markers = re.findall(
        r"^\s*Test Case ['\"]([^'\"]+)['\"] (?:started|passed|failed)\b",
        output,
        re.IGNORECASE | re.MULTILINE,
    )
    case_runs: set[tuple[str, str]] = set()
    for marker in case_markers:
        if marker.startswith("-[") and marker.endswith("]") and " " in marker:
            qualified, method = marker[2:-1].split(" ", 1)
            case_runs.add((qualified.rsplit(".", 1)[-1], method))
    missing: list[str] = []
    for selector in selectors:
        components = selector.split("/")[1:]
        if not components:
            continue
        suite = components[0]
        method = components[1] if len(components) > 1 else None
        ran = (
            (suite, method) in case_runs
            if method
            else any(marker == suite or marker.endswith(f".{suite}") for marker in suite_markers)
            or any(case_suite == suite for case_suite, _ in case_runs)
        )
        if not ran:
            missing.append(selector)
    return missing


def major_deviation(output: str) -> dict[str, str] | None:
    try:
        value = json.loads(output).get("major_deviation")
    except (AttributeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    summary, reason = value.get("summary"), value.get("reason")
    if not isinstance(summary, str) or not isinstance(reason, str):
        return None
    return {"summary": summary, "reason": reason}
