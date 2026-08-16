import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum


class TaskStatus(StrEnum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    TEST = "TEST"
    DONE = "DONE"
    FAILED = "FAILED"


class TaskStage(StrEnum):
    CREATED = "created"
    BRIEFING = "briefing"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    PREPARING_GIT = "preparing_git"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    ESCALATING = "escalating"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidationSnapshot:
    failures: int
    completed_steps: int


@dataclass(frozen=True, slots=True)
class Progress:
    is_progress: bool
    reason: str


@dataclass(frozen=True, slots=True)
class AttemptSignal:
    fingerprint: str
    diff_hash: str
    strategy: str


@dataclass(frozen=True, slots=True)
class Stalled:
    reason: str


_TRANSITIONS = {
    (TaskStatus.NOT_READY, TaskStage.CREATED, "plan"): (
        TaskStatus.NOT_READY,
        TaskStage.BRIEFING,
    ),
    (TaskStatus.NOT_READY, TaskStage.BRIEFING, "briefed"): (
        TaskStatus.NOT_READY,
        TaskStage.PLANNING,
    ),
    (TaskStatus.NOT_READY, TaskStage.PLANNING, "planned"): (
        TaskStatus.NOT_READY,
        TaskStage.AWAITING_APPROVAL,
    ),
    (TaskStatus.NOT_READY, TaskStage.AWAITING_APPROVAL, "approve"): (
        TaskStatus.READY,
        TaskStage.QUEUED,
    ),
    (TaskStatus.READY, TaskStage.QUEUED, "start"): (
        TaskStatus.IN_PROGRESS,
        TaskStage.PREPARING_GIT,
    ),
    (TaskStatus.IN_PROGRESS, TaskStage.VALIDATING, "validated"): (
        TaskStatus.DONE,
        TaskStage.COMPLETE,
    ),
    (TaskStatus.IN_PROGRESS, TaskStage.BLOCKED, "approve_amendment"): (
        TaskStatus.IN_PROGRESS,
        TaskStage.IMPLEMENTING,
    ),
}


def transition(
    status: TaskStatus, stage: TaskStage, action: str
) -> tuple[TaskStatus, TaskStage]:
    try:
        return _TRANSITIONS[status, stage, action]
    except KeyError as error:
        raise InvalidTransition(f"Cannot {action} from {status}/{stage}") from error


def assess_progress(
    previous: ValidationSnapshot, current: ValidationSnapshot
) -> Progress:
    if current.failures < previous.failures:
        return Progress(True, "fewer_failures")
    if current.completed_steps > previous.completed_steps:
        return Progress(True, "validation_advanced")
    return Progress(False, "no_validation_improvement")


def detect_stall(history: list[AttemptSignal]) -> Stalled | None:
    if len(history) >= 4 and history[-4:-2] == history[-2:]:
        return Stalled("oscillation")
    if len(history) >= 2 and history[-1] == history[-2]:
        return Stalled("repeated_outcome")
    return None


def fingerprint(exit_code: int, output: str) -> str:
    normalized = re.sub(r"(?:/[^\s:]+)+", "<path>", output)
    normalized = re.sub(r":\d+", ":<n>", normalized)
    normalized = " ".join(normalized.lower().split())
    return hashlib.sha256(f"{exit_code}:{normalized}".encode()).hexdigest()
