from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_carlo_planning_skill_has_pi_contract() -> None:
    content = (ROOT / "skills" / "carlo-planning" / "SKILL.md").read_text()

    assert content.startswith("---\nname: carlo-planning\n")
    for required in (
        "brief_markdown",
        "plan_markdown",
        "validation_commands",
        "browser_validation",
        "affected_areas",
        "Major deviations",
        "Final review",
    ):
        assert required in content
