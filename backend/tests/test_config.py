from carlo.config import Settings


def test_default_database_url_is_a_string(monkeypatch) -> None:
    monkeypatch.delenv("CARLO_DATABASE_URL", raising=False)
    assert Settings.from_env().database_url == "postgresql+psycopg:///carlov3"
