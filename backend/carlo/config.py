import os
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "postgresql+psycopg:///carlov3"
DEFAULT_ARTIFACT_ROOT = ".carlo/artifacts"
DEFAULT_PI_EXECUTABLE = "pi"
DEFAULT_WORKTREE_ROOT = ".carlo/worktrees"


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    artifact_root: str = DEFAULT_ARTIFACT_ROOT
    pi_executable: str = DEFAULT_PI_EXECUTABLE
    worktree_root: str = DEFAULT_WORKTREE_ROOT
    max_attempts: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("CARLO_DATABASE_URL", DEFAULT_DATABASE_URL),
            artifact_root=os.getenv("CARLO_ARTIFACT_ROOT", DEFAULT_ARTIFACT_ROOT),
            pi_executable=os.getenv("CARLO_PI_EXECUTABLE", DEFAULT_PI_EXECUTABLE),
            worktree_root=os.getenv("CARLO_WORKTREE_ROOT", DEFAULT_WORKTREE_ROOT),
            max_attempts=int(os.getenv("CARLO_MAX_ATTEMPTS", "20")),
        )
