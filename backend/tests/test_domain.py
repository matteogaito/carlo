import pytest

from carlo.domain import InvalidTransition, TaskStage, TaskStatus, transition


def test_approved_plan_makes_task_ready() -> None:
    assert transition(
        TaskStatus.NOT_READY, TaskStage.AWAITING_APPROVAL, "approve"
    ) == (TaskStatus.READY, TaskStage.QUEUED)


def test_local_success_completes_task() -> None:
    assert transition(
        TaskStatus.IN_PROGRESS, TaskStage.VALIDATING, "validated"
    ) == (TaskStatus.DONE, TaskStage.COMPLETE)


def test_unknown_transition_is_rejected() -> None:
    with pytest.raises(InvalidTransition):
        transition(TaskStatus.NOT_READY, TaskStage.CREATED, "validated")


def test_approved_amendment_resumes_implementation() -> None:
    assert transition(
        TaskStatus.IN_PROGRESS, TaskStage.BLOCKED, "approve_amendment"
    ) == (TaskStatus.IN_PROGRESS, TaskStage.IMPLEMENTING)
