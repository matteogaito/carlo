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


_TRANSITIONS = {
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
}


def transition(
    status: TaskStatus, stage: TaskStage, action: str
) -> tuple[TaskStatus, TaskStage]:
    try:
        return _TRANSITIONS[status, stage, action]
    except KeyError as error:
        raise InvalidTransition(f"Cannot {action} from {status}/{stage}") from error
