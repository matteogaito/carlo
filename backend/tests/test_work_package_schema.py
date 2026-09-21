import pytest
from pydantic import ValidationError

from carlo.api import ImplementationTask, PlanMetadata, _proposal_implementation_tasks


def package(position: int = 0) -> dict:
    return {
        "id": f"wp-{position + 1}",
        "title": "Update importer",
        "position": position,
        "objective": "Imported photos are placed by metadata date.",
        "files": [
            {
                "path": "src/importer.py", "mode": "edit",
                "ranges": [{"start": 10, "end": 30}], "symbols": ["import_photos"],
                "reason": "This owns date placement.",
            }
        ],
        "interfaces": ["import_photos(source: Path) -> list[Path] preserves order"],
        "changes": {"src/importer.py": "Use the metadata date, then copy into YYYY/MM."},
        "constraints": ["Do not add dependencies."],
        "verification": {"commands": ["pytest -q tests/test_importer.py --tb=short"], "success": "All targeted tests pass."},
        "done_when": ["Date placement and collision tests pass."],
        "budget": {"max_tool_calls": 20},
    }


def test_work_package_accepts_complete_contract() -> None:
    item = ImplementationTask.model_validate(package())
    assert item.files[0].ranges[0].start == 10
    assert item.budget.max_tool_calls == 20
    assert ImplementationTask.model_validate(
        {**package(), "budget": {"max_tool_calls": 50}}
    ).budget.max_tool_calls == 50
    without_budget = package()
    without_budget.pop("budget")
    assert ImplementationTask.model_validate(without_budget).budget.max_tool_calls == 30


@pytest.mark.parametrize("change", [
    lambda p: p.pop("objective"),
    lambda p: p.update(files=[]),
    lambda p: p.update(changes={"other.py": "Edit it"}),
    lambda p: p["files"][0].update(path="../outside.py"),
    lambda p: p["files"][0].update(ranges=[{"start": 30, "end": 10}]),
    lambda p: p.update(budget={"max_tool_calls": 0}),
    lambda p: p.update(budget={"max_tool_calls": 51}),
])
def test_work_package_rejects_incomplete_or_unsafe_contract(change) -> None:
    item = package()
    change(item)
    with pytest.raises(ValidationError):
        ImplementationTask.model_validate(item)


def test_plan_rejects_duplicate_ids_and_out_of_order_positions() -> None:
    base = {
        "skills": [], "validation_commands": [], "browser_validation": False,
        "build_required": False, "run_required": False,
        "deployment_expected": False, "risk_flags": [], "affected_areas": [],
    }
    with pytest.raises(ValidationError):
        PlanMetadata.model_validate({**base, "implementation_tasks": [package(1)]})
    with pytest.raises(ValidationError):
        PlanMetadata.model_validate({**base, "implementation_tasks": [package(), {**package(1), "id": "wp-1"}]})


def test_discovery_does_not_invent_packages_from_phase_titles() -> None:
    assert _proposal_implementation_tasks(
        {"megaprompt": "Build importer"},
        {"implementation_phases": ["Build importer"], "affected_areas": ["src/importer.py"]},
    ) == []
