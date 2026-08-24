from carlo import models
from carlo.models import AgentProfile, ModelProvider, PiRuntimeSettings


def test_profile_model_schema_keeps_only_concrete_selection() -> None:
    assert "default_model_id" not in ModelProvider.__table__.columns
    assert "model_provider_id" not in AgentProfile.__table__.columns
    assert "model" not in AgentProfile.__table__.columns
    assert "available_model_id" in AgentProfile.__table__.columns


def test_pi_resources_have_global_defaults_and_profile_additions() -> None:
    assert "default_packages" in PiRuntimeSettings.__table__.columns
    assert "default_skills" in PiRuntimeSettings.__table__.columns
    assert "default_packages" in AgentProfile.__table__.columns


def test_managed_pi_package_schema() -> None:
    assert hasattr(models, "PiPackage")
    assert hasattr(models, "AgentProfilePackage")
    package = models.PiPackage
    assert {"source", "identity", "enabled", "pinned", "is_default"}.issubset(
        package.__table__.columns.keys()
    )
    assert {
        "active_version",
        "active_artifact_path",
        "resources",
        "last_update_status",
        "last_update_error",
    }.issubset(package.__table__.columns.keys())
    assert models.AgentProfilePackage.__table__.c.package_id.foreign_keys
