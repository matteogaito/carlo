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
                "files": [{"path": "a.py", "mode": "edit", "reason": "implementation"}],
                "interfaces": ["existing API"], "changes": {"a.py": "Implement"},
                "constraints": [], "verification": {"commands": ["pytest -q"], "success": "passes"},
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
