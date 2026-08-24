from pathlib import Path
import base64
import os
import subprocess

import pytest

from carlo.config import Settings
from carlo.production import validate_production_settings


ROOT = Path(__file__).parents[2]


def test_launchd_unload_waits_until_service_disappears(tmp_path: Path) -> None:
    state = tmp_path / "calls"
    state.write_text("0")
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "calls=$(cat \"$CARLO_TEST_STATE\")\n"
        "[ \"$1 $2\" = 'print system/com.carlo.api' ] || exit 9\n"
        "[ \"$calls\" -ge 2 ] && exit 3\n"
        "echo $((calls + 1)) > \"$CARLO_TEST_STATE\"\n"
    )
    launchctl.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "wait-launchd-unloaded.sh"), "com.carlo.api"],
        env={
            **os.environ,
            "CARLO_LAUNCHCTL_BIN": str(launchctl),
            "CARLO_SLEEP_BIN": "/usr/bin/true",
            "CARLO_TEST_STATE": str(state),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert state.read_text().strip() == "2"


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
        "CARLO_CREDENTIAL_ENCRYPTION_KEY",
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


def test_macos_installer_creates_boot_daemons_for_service_user() -> None:
    makefile = (ROOT / "Makefile").read_text()
    installer = (ROOT / "scripts" / "install-mac.sh").read_text()
    runner = (ROOT / "scripts" / "run-mac-service.sh").read_text()

    for target in ("install-mac:", "status-mac:", "logs-mac:", "uninstall-mac:"):
        assert target in makefile
    assert "EUID" in installer
    assert "/Library/LaunchDaemons/com.carlo.api.plist" in installer
    assert "/Library/LaunchDaemons/com.carlo.worker.plist" in installer
    assert "<key>UserName</key><string>carlo</string>" in installer
    assert "launchctl bootstrap system" in installer
    assert "REASSIGN OWNED" in installer
    assert "/Users/carlo/.config/carlo/.env.production" in runner
    assert "source" not in installer
    assert "existing_hidden" not in installer
    assert "<key>GroupName</key><string>$service_group</string>" in installer
    assert '<key>PATH</key><string>$(/usr/bin/dirname "$NPM_BIN"):$SERVICE_HOME/.npm-global/bin:' in installer
    assert "ensure_encryption_key" in installer
    assert "/usr/bin/openssl rand -base64 32" in installer


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
    with pytest.raises(ValueError, match="CARLO_CREDENTIAL_ENCRYPTION_KEY"):
        validate_production_settings(
            Settings(
                app_origin="http://100.64.0.10:8000",
                artifact_root=str(tmp_path / "artifacts"),
                worktree_root=str(tmp_path / "worktrees"),
                frontend_dist=str(dist),
            )
        )
    validate_production_settings(
        Settings(
            app_origin="http://100.64.0.10:8000",
            artifact_root=str(tmp_path / "artifacts"),
            worktree_root=str(tmp_path / "worktrees"),
            frontend_dist=str(dist),
            credential_encryption_key=base64.b64encode(b"k" * 32).decode(),
        )
    )
