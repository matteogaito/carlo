"""Conservative approval boundary for planner corrections."""

from typing import Any

from .api import ImplementationTask


def within_approved_scope(original: dict[str, Any], revised: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        before = ImplementationTask.model_validate(original)
        after = ImplementationTask.model_validate(revised)
    except ValueError as error:
        return False, f"revised package does not match the work package schema: {error}"
    if (
        before.id != after.id
        or before.position != after.position
        or before.objective != after.objective
        or before.interfaces != after.interfaces
        or before.verification != after.verification
        or after.budget.max_tool_calls > before.budget.max_tool_calls
        or not set(before.constraints).issubset(after.constraints)
    ):
        return False, "revised package changes id/position/objective/interfaces/verification/constraints/budget beyond what was approved"
    permitted = {item.path: item.mode for item in before.files}
    if not all(
        item.path in permitted
        and (permitted[item.path] == item.mode or permitted[item.path] == "edit" and item.mode == "read_only")
        for item in after.files
    ):
        return False, "revised package touches files outside the approved scope"
    return True, None


def within_approved_scope_split(
    original: dict[str, Any], packages: list[dict[str, Any]]
) -> tuple[bool, str | None]:
    """Whether a proposed split of one work package into several stays inside
    what was already approved: every file touched by every new package must
    have been declared (with a compatible mode) on the original package, and
    no new package may request more tool-call budget than the original had.
    """
    try:
        before = ImplementationTask.model_validate(original)
        afters = [ImplementationTask.model_validate(package) for package in packages]
    except ValueError as error:
        return False, f"one or more proposed packages do not match the work package schema: {error}"
    if not afters:
        return False, "no packages were proposed"
    permitted = {item.path: item.mode for item in before.files}
    for after in afters:
        if after.budget.max_tool_calls > before.budget.max_tool_calls:
            return False, f"package '{after.id}' requests more tool calls than the original package had"
        bad_files = [
            item.path
            for item in after.files
            if item.path not in permitted
            or not (permitted[item.path] == item.mode or permitted[item.path] == "edit" and item.mode == "read_only")
        ]
        if bad_files:
            return False, f"package '{after.id}' touches files outside the approved scope: {bad_files}"
    return True, None
