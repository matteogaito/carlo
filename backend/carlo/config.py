import os
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "postgresql+psycopg:///carlov3"
DEFAULT_ARTIFACT_ROOT = ".carlo/artifacts"
DEFAULT_PI_EXECUTABLE = "pi"


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    artifact_root: str = DEFAULT_ARTIFACT_ROOT
    pi_executable: str = DEFAULT_PI_EXECUTABLE

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("CARLO_DATABASE_URL", DEFAULT_DATABASE_URL),
            artifact_root=os.getenv("CARLO_ARTIFACT_ROOT", DEFAULT_ARTIFACT_ROOT),
            pi_executable=os.getenv("CARLO_PI_EXECUTABLE", DEFAULT_PI_EXECUTABLE),
        )
