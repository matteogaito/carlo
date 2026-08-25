from pathlib import Path

from .api import create_app
from .config import Settings
from .db import make_engine, make_session_factory
from .maintenance import ensure_managed_pi_packages, ensure_pi_resources
from .pi_packages import PiPackageManager
from .pi_runtime import PiRuntimeSnapshotBuilder
from .provider import PiProvider

settings = Settings.from_env()
engine = make_engine(settings)
session_factory = make_session_factory(engine)
resource_root = Path(settings.artifact_root) / "pi-resources"
lock_path = Path(settings.artifact_root) / "pi-runtime.lock"
package_manager = PiPackageManager(
    Path(settings.pi_executable),
    Path(settings.artifact_root) / "pi-packages",
    lock_path,
)


async def bootstrap_resources() -> None:
    await ensure_pi_resources(session_factory, "git", resource_root, lock_path)
    await ensure_managed_pi_packages(session_factory, package_manager)


app = create_app(
    session_factory,
    PiProvider(
        settings.pi_executable,
        Path(settings.artifact_root) / "pi-sessions",
        runtime_builder=PiRuntimeSnapshotBuilder(
            Path(settings.artifact_root) / "pi-runtime"
        ),
        resource_root=resource_root,
        managed_skills={"frontend-design": "skills/frontend-design"},
        resource_manifest=resource_root / "revisions.json",
    ),
    settings,
    resource_bootstrap=bootstrap_resources,
    package_manager=package_manager,
)
