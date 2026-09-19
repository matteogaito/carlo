from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_carlo_planning_skill_has_pi_contract() -> None:
    content = (ROOT / "skills" / "carlo-planning" / "SKILL.md").read_text()

    assert content.startswith("---\nname: carlo-planning\n")
    for required in (
        "brief_markdown",
        "plan_markdown",
        "implementation_phases",
        "validation_commands",
        "browser_validation",
        "affected_areas",
        "Major deviations",
        "Final review",
    ):
        assert required in content


def test_carlo_runtime_skill_teaches_project_sandbox_constraints() -> None:
    content = (ROOT / "skills" / "carlo-runtime" / "SKILL.md").read_text()

    for required in (
        "project directory",
        "/tmp",
        "OTHER_SWIFT_FLAGS",
        "-disable-sandbox",
        "swift-plugin-server",
        "sudo",
    ):
        assert required in content
