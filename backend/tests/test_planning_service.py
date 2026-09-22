import json

import pytest

from carlo.planning import Planner, PlanningError, PlanningQuestion, PlanningRequest
from carlo.models import Project
from tests.fakes import FakeProvider


@pytest.mark.asyncio
async def test_planner_uses_same_contract_for_task_and_discovery():
    from carlo.api import PlanPayload

    valid = PlanPayload.model_validate({
        "brief_markdown": "Brief", "plan_markdown": "Plan",
        "metadata": {
            "skills": [], "validation_commands": ["pytest -q"],
            "browser_validation": False, "build_required": False,
            "run_required": False, "deployment_expected": False,
            "risk_flags": [], "affected_areas": [],
                "implementation_tasks": [{
                    "id": "one", "title": "One", "position": 0, "objective": "Deliver one",
                    "interfaces": ["existing API"], "constraints": [],
                    "done_when": ["works"],
            }],
        },
    })
    project = Project(name="Test", key="TEST", repository_path="/tmp", policies={})
    provider = FakeProvider(valid.model_dump_json())
    planner = Planner(provider, profile=None)
    direct = await planner.plan(PlanningRequest(project, "One", "Deliver one", "task-plan"))
    discovery = await planner.plan(PlanningRequest(project, "One", "Deliver one", "discovery-plan", handoff="Decision: use API"))
    assert direct == discovery == valid
    assert all("Inspect the repository" in call[1] for call in provider.calls)
    assert "Decision: use API" in provider.calls[1][1]

    provider.output = json.dumps({"question": "Which format?"})
    assert await planner.plan(PlanningRequest(project, "One", "Deliver one", "question")) == PlanningQuestion("Which format?")

    missing_final = valid.model_dump()
    missing_final["metadata"]["validation_commands"] = []
    provider.output = json.dumps(missing_final)
    with pytest.raises(PlanningError, match="final integration validation"):
        await planner.plan(PlanningRequest(project, "One", "Deliver one", "untested"))


@pytest.mark.asyncio
async def test_technical_planner_uses_current_checkout_and_requires_a_complete_package(tmp_path):
    from carlo.api import PlanPayload

    project = Project(name="Test", key="TEST", repository_path="/old/repo", policies={})
    technical = PlanPayload.model_validate({
        "brief_markdown": "Shared feature brief", "plan_markdown": "Edit the importer and test it.",
        "metadata": {
            "skills": [], "validation_commands": [],
            "browser_validation": False, "build_required": False,
            "run_required": False, "deployment_expected": False,
            "risk_flags": [], "affected_areas": [],
            "implementation_tasks": [{
                "id": "feature-1", "title": "Import by date", "position": 0,
                "objective": "Imported photos appear under their metadata date.",
                "files": [{"path": "importer.py", "mode": "edit", "reason": "owns import"}],
                "interfaces": ["import_photo returns the destination path"],
                "changes": {"importer.py": "Use metadata date"},
                "constraints": [],
                "verification": {"commands": ["pytest -q tests/test_importer.py"], "success": "passes"},
                "done_when": ["Date and collision tests pass"],
            }],
        },
    })
    provider = FakeProvider(technical.model_dump_json())
    result = await Planner(provider, None).plan(PlanningRequest(
        project, "Import by date", "Place by metadata date", "technical-1",
        handoff="Parent brief and verified predecessor contract", one_package=True,
        repository_path=str(tmp_path),
    ))
    assert result == technical
    assert provider.calls[0][2] == str(tmp_path)
    assert "Parent brief and verified predecessor contract" in provider.calls[0][1]


@pytest.mark.asyncio
async def test_feature_planner_rejects_premature_file_scoped_children():
    project = Project(name="Test", key="TEST", repository_path="/tmp", policies={}, validation_commands=["pytest -q"])
    output = {
        "brief_markdown": "Brief", "plan_markdown": "Plan",
        "metadata": {
            "skills": [], "validation_commands": ["pytest -q"],
            "browser_validation": False, "build_required": False,
            "run_required": False, "deployment_expected": False,
            "risk_flags": [], "affected_areas": [],
            "implementation_tasks": [{
                "id": "one", "title": "One", "position": 0, "objective": "Deliver one",
                "files": [{"path": "a.py", "mode": "edit", "reason": "implementation"}],
                "interfaces": ["existing API"], "changes": {"a.py": "Implement"},
                "constraints": [], "verification": {"commands": ["pytest -q"], "success": "passes"},
                "done_when": ["works"],
            }],
        },
    }
    provider = FakeProvider(json.dumps(output))
    with pytest.raises(PlanningError, match="feature-level"):
        await Planner(provider, None).plan(PlanningRequest(
            project, "One", "Deliver one", "feature-plan", handoff="Approved parent decision",
        ))
    assert len(provider.calls) == 3
    assert all("Approved parent decision" in call[1] for call in provider.calls)
