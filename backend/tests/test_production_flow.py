from pathlib import Path
import base64
import os
import signal
import subprocess
import sys

import pytest

from carlo.config import Settings
from carlo.production import validate_production_settings


ROOT = Path(__file__).parents[2]


def test_debug_enables_verbose_timestamped_api_and_worker_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert Settings.from_env().log_level == "INFO"
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert Settings.from_env().log_level == "DEBUG"
    api = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging, uvicorn; from carlo.logging_config import configure_api_logging; "
            "uvicorn.Config('unused:app', log_config='logging.ini'); "
            "configure_api_logging('DEBUG'); "
            "logging.getLogger('carlo.api').debug('api probe')",
        ],
        cwd=ROOT / "backend",
        check=True,
        capture_output=True,
        text=True,
    )
    worker = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging; from carlo.logging_config import configure_logging; "
            "configure_logging('DEBUG'); logging.getLogger('carlo.worker').debug('worker probe')",
        ],
        cwd=ROOT / "backend",
        check=True,
        capture_output=True,
        text=True,
    )

    assert "T" in api.stderr.split(" api probe", 1)[0]
    assert "T" in worker.stderr.split(" worker probe", 1)[0]


def test_launchd_unload_waits_in_the_requested_domain(tmp_path: Path) -> None:
    state = tmp_path / "calls"
    state.write_text("0")
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "calls=$(cat \"$CARLO_TEST_STATE\")\n"
        "[ \"$1 $2\" = 'print gui/502/com.carlo.service' ] || exit 9\n"
        "[ \"$calls\" -ge 2 ] && exit 3\n"
        "echo $((calls + 1)) > \"$CARLO_TEST_STATE\"\n"
    )
    launchctl.chmod(0o755)

    result = subprocess.run(
        [
            "/bin/bash",
            str(ROOT / "scripts" / "wait-launchd-unloaded.sh"),
            "com.carlo.service",
            "gui/502",
        ],
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


def test_launchd_bootstrap_retries_transient_failure(tmp_path: Path) -> None:
    state = tmp_path / "calls"
    state.write_text("0")
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  bootstrap) calls=$(cat \"$CARLO_TEST_STATE\"); "
        "echo $((calls + 1)) > \"$CARLO_TEST_STATE\"; [ \"$calls\" -ge 1 ];;\n"
        "  print) exit 3;;\n"
        "  kickstart) exit 0;;\n"
        "  *) exit 9;;\n"
        "esac\n"
    )
    launchctl.chmod(0o755)

    result = subprocess.run(
        [
            "/bin/bash",
            str(ROOT / "scripts" / "launchd-bootstrap.sh"),
            "gui/502",
            str(tmp_path / "service.plist"),
            "com.carlo.service",
        ],
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
    assert "CARLO_WORKTREE_ROOT" not in configured
    assert "CHANGE_ME" in template

    makefile = (ROOT / "Makefile").read_text()
    assert "prod-api:" in makefile
    prod_recipe = makefile.split("prod-api:", 1)[1].split("\n\n", 1)[0]
    assert "--reload" not in prod_recipe


def test_deploy_is_cross_platform_and_unprivileged() -> None:
    makefile = (ROOT / "Makefile").read_text()
    deploy = (ROOT / "scripts" / "deploy.sh").read_text()
    runner = (ROOT / "scripts" / "carlo-service.sh").read_text()

    for target in ("deploy:", "status:", "logs:", "undeploy:"):
        assert target in makefile
    assert "install-mac:" not in makefile
    assert "status-mac:" not in makefile
    assert "uninstall-mac:" not in makefile
    assert "sudo" not in deploy
    assert "/Library/LaunchDaemons" not in deploy
    assert "Darwin" in deploy
    assert "Linux" in deploy
    assert "Library/LaunchAgents" in deploy
    assert '<key>LimitLoadToSessionType</key><string>Aqua</string>' in deploy
    assert "com.carlo.service.plist" in deploy
    assert 'mac_agent_plist="$mac_plist_dir/com.carlo.agent.plist"' in deploy
    assert 'launchctl bootout "gui/$service_uid/com.carlo.agent"' in deploy
    assert "write_mac_plist com.carlo.service" in deploy
    assert "write_mac_plist com.carlo.api" not in deploy
    assert "write_mac_plist com.carlo.worker" not in deploy
    assert "systemctl --user" in deploy
    assert "journalctl --user" in deploy
    assert "carlo.service" in deploy
    assert "write_linux_unit service" in deploy
    assert "write_linux_unit api" not in deploy
    assert "write_linux_unit worker" not in deploy
    assert 'runner="$install_root/scripts/carlo-service.sh"' in deploy
    assert "run-service.sh" not in runner
    assert "~/.local" not in runner
    assert 'CARLO_STATE_ROOT' in runner
    assert "ensure_encryption_key" in deploy
    assert "openssl rand -base64 32" in deploy


def test_carlo_service_stops_sibling_when_one_process_exits(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    scripts = install_root / "scripts"
    binaries = install_root / "backend" / ".venv" / "bin"
    scripts.mkdir(parents=True)
    binaries.mkdir(parents=True)
    runner = scripts / "carlo-service.sh"
    runner.write_bytes((ROOT / "scripts" / "carlo-service.sh").read_bytes())
    runner.chmod(0o755)
    (binaries / "uvicorn").write_text("#!/bin/sh\nsleep 0.2\nexit 7\n")
    (binaries / "python").write_text(
        "#!/bin/sh\n"
        "trap 'echo stopped > \"$CARLO_TEST_MARKER\"; exit 0' TERM INT\n"
        "while :; do sleep 0.1; done\n"
    )
    (binaries / "uvicorn").chmod(0o755)
    (binaries / "python").chmod(0o755)
    config_root = tmp_path / "carlo"
    config_root.mkdir()
    (config_root / ".env.production").write_text("")
    marker = tmp_path / "worker-stopped"
    env = os.environ | {
        "XDG_CONFIG_HOME": str(tmp_path),
        "CARLO_STATE_ROOT": str(tmp_path / "state"),
        "CARLO_TEST_MARKER": str(marker),
    }

    result = subprocess.run(
        [runner, "service"], env=env, capture_output=True, text=True, timeout=5
    )

    assert result.returncode != 0
    assert marker.read_text().strip() == "stopped"


def test_carlo_service_forces_stubborn_child_to_stop(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    scripts = install_root / "scripts"
    binaries = install_root / "backend" / ".venv" / "bin"
    scripts.mkdir(parents=True)
    binaries.mkdir(parents=True)
    runner = scripts / "carlo-service.sh"
    runner.write_bytes((ROOT / "scripts" / "carlo-service.sh").read_bytes())
    runner.chmod(0o755)
    (binaries / "uvicorn").write_text("#!/bin/sh\nsleep 0.2\nexit 7\n")
    child_pid = tmp_path / "child-pid"
    (binaries / "python").write_text(
        "#!/bin/sh\n"
        "echo $$ > \"$CARLO_TEST_CHILD_PID\"\n"
        "trap '' TERM INT\n"
        "while :; do sleep 0.1; done\n"
    )
    (binaries / "uvicorn").chmod(0o755)
    (binaries / "python").chmod(0o755)
    config_root = tmp_path / "carlo"
    config_root.mkdir()
    (config_root / ".env.production").write_text("")
    env = os.environ | {
        "XDG_CONFIG_HOME": str(tmp_path),
        "CARLO_STATE_ROOT": str(tmp_path / "state"),
        "CARLO_TEST_CHILD_PID": str(child_pid),
    }
    process = subprocess.Popen(
        [runner, "service"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        process.communicate(timeout=4)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise

    assert process.returncode != 0
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid.read_text()), 0)


def test_carlo_service_preserves_configured_artifact_root(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    scripts = install_root / "scripts"
    binaries = install_root / "backend" / ".venv" / "bin"
    scripts.mkdir(parents=True)
    binaries.mkdir(parents=True)
    runner = scripts / "carlo-service.sh"
    runner.write_bytes((ROOT / "scripts" / "carlo-service.sh").read_bytes())
    runner.chmod(0o755)
    marker = tmp_path / "artifact-root"
    (binaries / "python").write_text(
        "#!/bin/sh\nprintf '%s' \"$CARLO_ARTIFACT_ROOT\" > \"$CARLO_TEST_MARKER\"\n"
    )
    (binaries / "python").chmod(0o755)
    config_root = tmp_path / "config" / "carlo"
    config_root.mkdir(parents=True)
    configured = tmp_path / "configured-artifacts"
    (config_root / ".env.production").write_text(
        f"CARLO_ARTIFACT_ROOT={configured}\n"
    )

    result = subprocess.run(
        [runner, "check"],
        env=os.environ
        | {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "CARLO_STATE_ROOT": str(tmp_path / "state"),
            "CARLO_TEST_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text() == str(configured)


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
                frontend_dist=str(dist),
            )
        )
    validate_production_settings(
        Settings(
            app_origin="http://100.64.0.10:8000",
            artifact_root=str(tmp_path / "artifacts"),
            frontend_dist=str(dist),
            credential_encryption_key=base64.b64encode(b"k" * 32).decode(),
        )
    )
