from carlo.config import Settings


def test_default_database_url_is_a_string(monkeypatch) -> None:
    monkeypatch.delenv("CARLO_DATABASE_URL", raising=False)
    assert Settings.from_env().database_url == "postgresql+psycopg:///carlov3"


def test_worker_settings_are_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("CARLO_WORKTREE_ROOT", "/tmp/carlo-worktrees")
    monkeypatch.setenv("CARLO_MAX_ATTEMPTS", "9")
    settings = Settings.from_env()
    assert settings.worktree_root == "/tmp/carlo-worktrees"
    assert settings.max_attempts == 9
