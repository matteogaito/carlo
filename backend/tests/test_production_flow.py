from pathlib import Path

import pytest

from carlo.config import Settings
from carlo.production import validate_production_settings


ROOT = Path(__file__).parents[2]


def test_production_template_and_commands_are_complete() -> None:
    template = (ROOT / ".env.production.example").read_text()
    required = {
        "CARLO_DATABASE_URL",
        "CARLO_ARTIFACT_ROOT",
        "CARLO_WORKTREE_ROOT",
        "CARLO_APP_ORIGIN",
        "CARLO_FRONTEND_DIST",
        "CARLO_COOKIE_SECURE",
        "CARLO_BOOTSTRAP_ADMIN_USERNAME",
        "CARLO_BOOTSTRAP_ADMIN_PASSWORD",
        "CARLO_TELEGRAM_BOT_TOKEN",
        "CARLO_TELEGRAM_CHAT_ID",
        "CARLO_TELEGRAM_LEVEL",
    }
    configured = {
        line.split("=", 1)[0]
        for line in template.splitlines()
        if line and not line.startswith("#")
    }
    assert required <= configured
    assert "CHANGE_ME" in template

    makefile = (ROOT / "Makefile").read_text()
    assert "prod-api:" in makefile
    prod_recipe = makefile.split("prod-api:", 1)[1].split("\n\n", 1)[0]
    assert "--reload" not in prod_recipe


def test_production_validation_rejects_placeholders_and_missing_build(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="CARLO_APP_ORIGIN"):
        validate_production_settings(
            Settings(app_origin="http://VPN_IP_OR_HOSTNAME:8000")
        )

    settings = Settings(
        app_origin="http://100.64.0.10:8000",
        artifact_root=str(tmp_path / "artifacts"),
        worktree_root=str(tmp_path / "worktrees"),
        frontend_dist=str(tmp_path / "missing-dist"),
    )
    with pytest.raises(ValueError, match="frontend build"):
        validate_production_settings(settings)

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("CARLO")
    validate_production_settings(
        Settings(
            app_origin="http://100.64.0.10:8000",
            artifact_root=str(tmp_path / "artifacts"),
            worktree_root=str(tmp_path / "worktrees"),
            frontend_dist=str(dist),
        )
    )
