import pytest

from carlo.config import Settings


def test_default_database_url_is_a_string(monkeypatch) -> None:
    monkeypatch.delenv("CARLO_DATABASE_URL", raising=False)
    assert Settings.from_env().database_url == "postgresql+psycopg:///carlov3"


def test_worker_settings_are_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("CARLO_WORKTREE_ROOT", "/tmp/carlo-worktrees")
    monkeypatch.setenv("CARLO_MAX_ATTEMPTS", "9")
    monkeypatch.setenv("CARLO_ACTION_CANCEL_GRACE_SECONDS", "7")
    monkeypatch.setenv("CARLO_NPM_EXECUTABLE", "/opt/homebrew/bin/npm")
    settings = Settings.from_env()
    assert settings.worktree_root == "/tmp/carlo-worktrees"
    assert settings.max_attempts == 9
    assert settings.action_cancel_grace_seconds == 7
    assert settings.npm_executable == "/opt/homebrew/bin/npm"


def test_production_settings_are_loaded(monkeypatch) -> None:
    monkeypatch.setenv("CARLO_APP_ORIGIN", "http://100.64.0.10:8000")
    monkeypatch.setenv("CARLO_COOKIE_SECURE", "true")
    monkeypatch.setenv("CARLO_SESSION_HOURS", "12")
    monkeypatch.setenv("CARLO_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("CARLO_PORT", "8080")
    monkeypatch.setenv("CARLO_TELEGRAM_LEVEL", "blocking")

    settings = Settings.from_env()

    assert settings.app_origin == "http://100.64.0.10:8000"
    assert settings.cookie_secure is True
    assert settings.session_hours == 12
    assert settings.bind_host == "0.0.0.0"
    assert settings.port == 8080
    assert settings.telegram_level == "blocking"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("CARLO_COOKIE_SECURE", "sometimes", "CARLO_COOKIE_SECURE"),
        ("CARLO_SESSION_HOURS", "0", "CARLO_SESSION_HOURS"),
        ("CARLO_PORT", "70000", "CARLO_PORT"),
        ("CARLO_TELEGRAM_LEVEL", "verbose", "CARLO_TELEGRAM_LEVEL"),
    ],
)
def test_invalid_production_settings_are_rejected(
    monkeypatch, name: str, value: str, message: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings.from_env()
