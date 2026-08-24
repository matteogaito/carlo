import importlib
import importlib.util
import json
from pathlib import Path


def test_pi_runtime_snapshot_is_isolated_atomic_and_secret_free(tmp_path: Path) -> None:
    assert importlib.util.find_spec("carlo.pi_runtime") is not None, (
        "managed Pi snapshots are missing"
    )
    runtime = importlib.import_module("carlo.pi_runtime")
    provider = importlib.import_module("carlo.provider")
    assert hasattr(provider, "ResolvedModel"), "resolved model contract is missing"
    resolved = provider.ResolvedModel(
        model_provider_id=1,
        available_model_id=2,
        provider_slug="omlx",
        base_url="http://127.0.0.1:11435/v1",
        api="openai-completions",
        external_id="qwen",
        display_name="Qwen local",
        api_key="omlx-local",
        compatibility={"supportsDeveloperRole": False},
        input_modalities=("text",),
        reasoning=True,
        context_window=65_536,
        max_tokens=16_384,
        compaction_enabled=True,
        reserve_tokens=16_384,
        keep_recent_tokens=13_107,
    )

    snapshot = runtime.PiRuntimeSnapshotBuilder(tmp_path / "runtime").materialize(
        "DIMMELA-1-implementation-1", resolved
    )

    assert snapshot.model_pattern == "omlx/qwen"
    assert snapshot.environment == {"CARLO_PI_MODEL_API_KEY": "omlx-local"}
    assert snapshot.agent_dir == tmp_path / "runtime" / "DIMMELA-1-implementation-1"
    models = json.loads((snapshot.agent_dir / "models.json").read_text())
    assert models["providers"]["omlx"]["apiKey"] == "$CARLO_PI_MODEL_API_KEY"
    assert models["providers"]["omlx"]["models"][0]["contextWindow"] == 65_536
    settings = json.loads((snapshot.agent_dir / "settings.json").read_text())
    assert settings == {
        "compaction": {
            "enabled": True,
            "reserveTokens": 16_384,
            "keepRecentTokens": 13_107,
        }
    }
    assert "omlx-local" not in "".join(
        file.read_text() for file in snapshot.agent_dir.iterdir() if file.is_file()
    )
    assert not list(snapshot.agent_dir.glob(".tmp-*"))
    assert snapshot.manifest["context_window"] == 65_536
    assert "api_key" not in snapshot.manifest


def test_pi_runtime_cleanup_only_removes_its_temporary_files(tmp_path: Path) -> None:
    runtime = importlib.import_module("carlo.pi_runtime")
    root = tmp_path / "runtime"
    root.mkdir()
    temporary = root / ".tmp-models.json-stale"
    snapshot = root / "completed-session"
    temporary.write_text("partial")
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text("{}")

    runtime.PiRuntimeSnapshotBuilder(root).cleanup_temporary_files()

    assert not temporary.exists()
    assert (snapshot / "manifest.json").exists()


def test_pi_runtime_uses_non_secret_placeholder_for_keyless_provider(
    tmp_path: Path,
) -> None:
    runtime = importlib.import_module("carlo.pi_runtime")
    provider = importlib.import_module("carlo.provider")
    model = provider.ResolvedModel(
        model_provider_id=1,
        available_model_id=2,
        provider_slug="omlx",
        base_url="http://127.0.0.1:11435/v1",
        api="openai-completions",
        external_id="qwen",
        display_name="Qwen",
        api_key=None,
        compatibility={},
        input_modalities=("text",),
        reasoning=True,
        context_window=65_536,
        max_tokens=16_384,
        compaction_enabled=True,
        reserve_tokens=16_384,
        keep_recent_tokens=13_107,
    )

    snapshot = runtime.PiRuntimeSnapshotBuilder(tmp_path).materialize("keyless", model)

    assert snapshot.environment == {}
    configured = json.loads((snapshot.agent_dir / "models.json").read_text())
    assert configured["providers"]["omlx"]["apiKey"] == "carlo-keyless"
