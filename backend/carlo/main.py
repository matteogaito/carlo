from pathlib import Path

from .api import create_app
from .config import Settings
from .db import make_engine, make_session_factory
from .provider import PiProvider

settings = Settings.from_env()
engine = make_engine(settings)
app = create_app(
    make_session_factory(engine),
    PiProvider(settings.pi_executable, Path(settings.artifact_root) / "pi-sessions"),
    settings,
)
