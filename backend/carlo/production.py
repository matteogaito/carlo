from pathlib import Path
from urllib.parse import urlparse

from .config import Settings


def validate_production_settings(settings: Settings) -> None:
    fields = {
        "CARLO_APP_ORIGIN": settings.app_origin,
        "CARLO_ARTIFACT_ROOT": settings.artifact_root,
        "CARLO_WORKTREE_ROOT": settings.worktree_root,
        "CARLO_FRONTEND_DIST": settings.frontend_dist,
    }
    for name, value in fields.items():
        if any(marker in value.upper() for marker in ("CHANGE_ME", "/ABSOLUTE/", "VPN_IP_OR_HOSTNAME")):
            raise ValueError(f"{name} still contains a placeholder")

    origin = urlparse(settings.app_origin)
    if origin.scheme not in {"http", "https"} or not origin.netloc:
        raise ValueError("CARLO_APP_ORIGIN must be an absolute HTTP(S) origin")
    for name in ("artifact_root", "worktree_root", "frontend_dist"):
        if not Path(getattr(settings, name)).is_absolute():
            raise ValueError(f"CARLO_{name.upper()} must be an absolute path")
    if not (Path(settings.frontend_dist) / "index.html").is_file():
        raise ValueError("frontend build is missing; run make build")
    if not settings.credential_encryption_key or "CHANGE_ME" in settings.credential_encryption_key:
        raise ValueError("CARLO_CREDENTIAL_ENCRYPTION_KEY is not configured")


def main() -> None:
    validate_production_settings(Settings.from_env())
    print("Production configuration is valid")


if __name__ == "__main__":
    main()
