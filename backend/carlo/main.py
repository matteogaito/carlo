from pathlib import Path

from .api import create_app
from .config import Settings
from .db import make_engine, make_session_factory
from .provider import PiProvider
from .pi_runtime import PiRuntimeSnapshotBuilder

settings = Settings.from_env()
engine = make_engine(settings)
resource_root = Path(settings.artifact_root) / "pi-resources"
app = create_app(
    make_session_factory(engine),
    PiProvider(
        settings.pi_executable,
        Path(settings.artifact_root) / "pi-sessions",
        runtime_builder=PiRuntimeSnapshotBuilder(
            Path(settings.artifact_root) / "pi-runtime"
        ),
        resource_root=resource_root,
        managed_packages=("superpowers",),
        managed_skills={"frontend-design": "skills/frontend-design"},
        resource_manifest=resource_root / "revisions.json",
    ),
    settings,
)
