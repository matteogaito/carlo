import asyncio
import json
import re
import shlex
from dataclasses import asdict, dataclass
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
from .git import GitError, GitWorkspace, Worktree
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

IMPLEMENTATION_LOCK = 1_128_352_847
MAX_INTERRUPTS = 3


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
                .where(Task.status == TaskStatus.IN_PROGRESS)
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
                .where(Task.status == TaskStatus.READY, Task.stage == TaskStage.QUEUED)
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
            return task is not None and task.status == TaskStatus.IN_PROGRESS

    @staticmethod
    async def _eligible(session: AsyncSession, task: Task) -> bool:
        if await session.scalar(
            select(func.count(Task.id)).where(Task.parent_task_id == task.id)
        ):
            return False
        if task.parent_task_id is None:
            return True
        unfinished = await session.scalar(
            select(func.count(Task.id)).where(
                Task.parent_task_id == task.parent_task_id,
                Task.subtask_position < task.subtask_position,
                Task.status != TaskStatus.DONE,
            )
        )
        return not unfinished

    async def _finish(self, task_id: str, outcome: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise RuntimeError(f"active task {task_id} disappeared")
            if outcome == "validated":
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
    async def _sync_parent(session: AsyncSession, child: Task) -> None:
        if child.parent_task_id is None:
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
                    .where(Task.parent_task_id == parent.id)
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
        worktree_root: Path,
        artifact_root: Path,
        max_attempts: int = 20,
        credential_cipher: CredentialCipher | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.worktree_root = worktree_root
        self.artifact_root = artifact_root
        self.max_attempts = max_attempts
        self.credential_cipher = credential_cipher

    async def run(self, task_id: str) -> str:
        task, plan, implementation, escalation = await self._context(task_id)
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
        workspace = GitWorkspace(
            Path(task.project.repository_path),
            self.worktree_root,
            task.project.integration_branch,
        )
        rework_cycle, previous_cycle_attempt = await self._rework_context(task_id)
        base_ref = None
        if task.parent_task_id is not None and task.subtask_position:
            async with self.session_factory() as session:
                predecessor = await session.scalar(
                    select(Task).where(
                        Task.parent_task_id == task.parent_task_id,
                        Task.subtask_position == task.subtask_position - 1,
                    )
                )
                if predecessor is None or predecessor.checkpoint_sha is None:
                    raise GitError("previous subtask has no validated checkpoint")
                base_ref = predecessor.checkpoint_sha
        if task.worktree_path and task.branch_name:
            worktree = Worktree(task.branch_name, Path(task.worktree_path))
        else:
            worktree = await workspace.prepare(
                task.id, task.title, rework_cycle=rework_cycle, base_ref=base_ref
            )
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
                        type="git.worktree_prepared",
                        payload={"branch": worktree.branch, "path": str(worktree.path)},
                    )
                )
                await session.commit()

        previous: ValidationSnapshot | None = None
        history: list[AttemptSignal] = []
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
                return "validated"
            previous = recovered.snapshot
        start = await self._next_attempt_number(task_id)
        attempts_used = max(0, start - 1 - previous_cycle_attempt)
        attempts_left = max(0, self.max_attempts - attempts_used)
        for number in range(start, start + attempts_left):
            instruction = self._implementation_instruction(task, plan, strategy)
            attempt_id = await self._start_attempt(
                task_id, implementation.id, number, instruction
            )
            result = await self.provider.run(
                implementation_profile,
                instruction,
                str(worktree.path),
                f"{task_id}-implementation-{number}",
                on_event=self._pi_step_handler(task_id),
            )
            await self._record_provider_result(task_id, attempt_id, number, result)
            deviation = major_deviation(result.output)
            if deviation:
                await self._block_for_amendment(
                    task_id, plan, attempt_id, deviation
                )
                return "blocked"
            await self._set_stage(task_id, TaskStage.VALIDATING)
            batch = await self._validate(task, plan, worktree, attempt_id, number)
            if batch.passed:
                checkpoint = await workspace.checkpoint(
                    worktree, f"attempt {number} validated"
                )
                await self._finish_attempt(
                    task_id, attempt_id, "verified", checkpoint, None, batch
                )
                return "validated"

            diff_hash = await workspace.diff_hash(worktree)
            signal = AttemptSignal(batch.fingerprint, diff_hash, strategy)
            history.append(signal)
            await self._finish_attempt(
                task_id, attempt_id, "validation_failed", None, diff_hash, batch
            )
            if previous and assess_progress(previous, batch.snapshot).is_progress:
                checkpoint = await workspace.checkpoint(
                    worktree, f"attempt {number} improved validation"
                )
                await self._set_checkpoint(task_id, checkpoint)
            previous = batch.snapshot
            stalled = detect_stall(history)
            if stalled:
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
        return "failed"

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
                            AgentProfileRecord.name.in_(("implementation", "escalation"))
                        )
                    )
                ).all()
            }
            if plan is None or set(profiles) != {"implementation", "escalation"}:
                raise RuntimeError("plan or agent profiles are missing")
            return task, plan, profiles["implementation"], profiles["escalation"]

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

    async def _rework_context(self, task_id: str) -> tuple[int, int]:
        async with self.session_factory() as session:
            event = await session.scalar(
                select(Event)
                .where(
                    Event.task_id == task_id,
                    Event.type == "task.rework.started",
                )
                .order_by(Event.sequence.desc())
                .limit(1)
            )
            if event is None:
                return 0, 0
            return int(event.payload["cycle"]), int(event.payload["previous_attempt"])

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
        worktree: Worktree,
        attempt_id: int,
        attempt_number: int,
    ) -> ValidationBatch:
        commands = plan.metadata_json.get("validation_commands") or task.project.validation_commands
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
        worktree: Worktree,
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
        implementation_tasks = plan.metadata_json.get("implementation_tasks", [])
        return (
            f"Implement {task.id}: {task.goal}\n\n"
            f"Approved plan:\n{plan.plan_markdown}\n\n"
            f"Implementation tasks:\n{json.dumps(implementation_tasks)}\n\n"
            f"Current strategy: {strategy}\n"
            "Work only inside this worktree. Run no undeclared deployment commands."
        )


def _failure_count(output: str) -> int:
    match = re.search(r"(\d+)\s+failed", output, re.IGNORECASE)
    return int(match.group(1)) if match else 1


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
