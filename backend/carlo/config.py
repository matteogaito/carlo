import os
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "postgresql+psycopg:///carlov3"
DEFAULT_ARTIFACT_ROOT = ".carlo/artifacts"
DEFAULT_PI_EXECUTABLE = "pi"
DEFAULT_WORKTREE_ROOT = ".carlo/worktrees"


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def _positive_integer(name: str, default: int, maximum: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1 or (maximum is not None and value > maximum):
        limit = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be positive{limit}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    artifact_root: str = DEFAULT_ARTIFACT_ROOT
    pi_executable: str = DEFAULT_PI_EXECUTABLE
    worktree_root: str = DEFAULT_WORKTREE_ROOT
    max_attempts: int = 20
    action_cancel_grace_seconds: int = 10
    app_origin: str = "http://127.0.0.1:8000"
    cookie_secure: bool = False
    session_hours: int = 24
    frontend_dist: str = "../frontend/dist"
    bind_host: str = "127.0.0.1"
    port: int = 8000
    bootstrap_admin_username: str = "CHANGE_ME"
    bootstrap_admin_password: str = "CHANGE_ME"
    telegram_bot_token: str = "CHANGE_ME"
    telegram_chat_id: str = "CHANGE_ME"
    telegram_level: str = "all"

    @classmethod
    def from_env(cls) -> "Settings":
        telegram_level = os.getenv("CARLO_TELEGRAM_LEVEL", "all").lower()
        if telegram_level not in {"all", "blocking"}:
            raise ValueError("CARLO_TELEGRAM_LEVEL must be all or blocking")
        return cls(
            database_url=os.getenv("CARLO_DATABASE_URL", DEFAULT_DATABASE_URL),
            artifact_root=os.getenv("CARLO_ARTIFACT_ROOT", DEFAULT_ARTIFACT_ROOT),
            pi_executable=os.getenv("CARLO_PI_EXECUTABLE", DEFAULT_PI_EXECUTABLE),
            worktree_root=os.getenv("CARLO_WORKTREE_ROOT", DEFAULT_WORKTREE_ROOT),
            max_attempts=_positive_integer("CARLO_MAX_ATTEMPTS", 20),
            action_cancel_grace_seconds=_positive_integer(
                "CARLO_ACTION_CANCEL_GRACE_SECONDS", 10
            ),
            app_origin=os.getenv("CARLO_APP_ORIGIN", "http://127.0.0.1:8000").rstrip("/"),
            cookie_secure=_boolean("CARLO_COOKIE_SECURE", False),
            session_hours=_positive_integer("CARLO_SESSION_HOURS", 24),
            frontend_dist=os.getenv("CARLO_FRONTEND_DIST", "../frontend/dist"),
            bind_host=os.getenv("CARLO_BIND_HOST", "127.0.0.1"),
            port=_positive_integer("CARLO_PORT", 8000, 65535),
            bootstrap_admin_username=os.getenv(
                "CARLO_BOOTSTRAP_ADMIN_USERNAME", "CHANGE_ME"
            ),
            bootstrap_admin_password=os.getenv(
                "CARLO_BOOTSTRAP_ADMIN_PASSWORD", "CHANGE_ME"
            ),
            telegram_bot_token=os.getenv("CARLO_TELEGRAM_BOT_TOKEN", "CHANGE_ME"),
            telegram_chat_id=os.getenv("CARLO_TELEGRAM_CHAT_ID", "CHANGE_ME"),
            telegram_level=telegram_level,
        )
