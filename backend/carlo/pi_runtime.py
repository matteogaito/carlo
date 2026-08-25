import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .provider import ResolvedModel, ResolvedPiPackage

_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_.-]{1,200}\Z")


@dataclass(frozen=True, slots=True)
class PiRuntimeSnapshot:
    agent_dir: Path
    model_pattern: str
    environment: dict[str, str]
    manifest: dict[str, Any]
    packages: tuple[ResolvedPiPackage, ...]


class PiRuntimeSnapshotBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def materialize(
        self,
        session_id: str,
        model: ResolvedModel,
        packages: tuple[ResolvedPiPackage, ...] = (),
    ) -> PiRuntimeSnapshot:
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid Pi session ID")
        if self.root.is_symlink():
            raise ValueError("Pi runtime root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        agent_dir = self.root / session_id
        if agent_dir.is_symlink():
            raise ValueError("Pi runtime session must not be a symlink")
        agent_dir.mkdir(exist_ok=True)

        manifest_path = agent_dir / "manifest.json"
        if manifest_path.is_file():
            previous = json.loads(manifest_path.read_text())
            packages = tuple(
                ResolvedPiPackage(
                    int(item["id"]),
                    str(item["identity"]),
                    str(item["source"]),
                    str(item["version"]),
                    str(item["artifact_path"]),
                    dict(item.get("resources") or {}),
                )
                for item in previous.get("packages", [])
            )

        manifest = {
            "model_provider_id": model.model_provider_id,
            "available_model_id": model.available_model_id,
            "provider": model.provider_slug,
            "model": model.external_id,
            "context_window": model.context_window,
            "max_tokens": model.max_tokens,
            "compaction": {
                "enabled": model.compaction_enabled,
                "reserve_tokens": model.reserve_tokens,
                "keep_recent_tokens": model.keep_recent_tokens,
            },
            "packages": [
                {
                    "id": package.package_id,
                    "identity": package.identity,
                    "source": package.source,
                    "version": package.version,
                    "artifact_path": package.artifact_path,
                    "resources": package.resources,
                }
                for package in packages
            ],
        }
        self._write_json(
            agent_dir / "models.json",
            {
                "providers": {
                    model.provider_slug: {
                        "baseUrl": model.base_url,
                        "api": model.api,
                        "apiKey": "$CARLO_PI_MODEL_API_KEY"
                        if model.api_key
                        else "carlo-keyless",
                        "compat": model.compatibility,
                        "models": [
                            {
                                "id": model.external_id,
                                "name": model.display_name,
                                "input": list(model.input_modalities),
                                "reasoning": model.reasoning,
                                "contextWindow": model.context_window,
                                "maxTokens": model.max_tokens,
                                "cost": {
                                    "input": 0,
                                    "output": 0,
                                    "cacheRead": 0,
                                    "cacheWrite": 0,
                                },
                            }
                        ],
                    }
                }
            },
        )
        self._write_json(
            agent_dir / "settings.json",
            {
                "compaction": {
                    "enabled": model.compaction_enabled,
                    "reserveTokens": model.reserve_tokens,
                    "keepRecentTokens": model.keep_recent_tokens,
                }
            },
        )
        self._write_json(agent_dir / "manifest.json", manifest)
        return PiRuntimeSnapshot(
            agent_dir=agent_dir,
            model_pattern=f"{model.provider_slug}/{model.external_id}",
            environment={"CARLO_PI_MODEL_API_KEY": model.api_key}
            if model.api_key
            else {},
            manifest=manifest,
            packages=packages,
        )

    def cleanup_temporary_files(self) -> None:
        if self.root.is_symlink():
            raise ValueError("Pi runtime root must not be a symlink")
        if not self.root.exists():
            return
        for path in self.root.glob(".tmp-*"):
            if path.is_file() or path.is_symlink():
                path.unlink()

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".tmp-{path.name}-{os.getpid()}")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        os.replace(temporary, path)
