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
    assert models["providers"]["omlx"]["models"][0]["contextWindow"] == 49_152
    settings = json.loads((snapshot.agent_dir / "settings.json").read_text())
    assert settings == {
        "compaction": {
            "enabled": True,
            "reserveTokens": 16_384,
            "keepRecentTokens": 13_107,
        }
    }
    sandbox = json.loads((snapshot.agent_dir / "sandbox.json").read_text())
    assert sandbox == {
        "enabled": True,
        "sandboxUserShell": True,
        "permissionPromptTimeoutSeconds": 1,
        "allowBrowserProcess": False,
        "network": {
            "allowedDomains": ["*"],
            "deniedDomains": [],
        },
        "filesystem": {
            "denyRead": [
                "/Users",
                "/home",
                "/tmp",
                "/private/tmp",
                "/Volumes",
                "/usr/local/var/carlo",
            ],
            "allowRead": ["."],
            "allowWrite": ["."],
            "denyWrite": [".pi/sandbox.json"],
        },
    }
    assert "omlx-local" not in "".join(
        file.read_text() for file in snapshot.agent_dir.iterdir() if file.is_file()
    )
    assert not list(snapshot.agent_dir.glob(".tmp-*"))
    assert snapshot.manifest["context_window"] == 65_536
    assert "api_key" not in snapshot.manifest

    frozen_root = tmp_path / "frozen-package"
    sandbox_root = tmp_path / "sandbox-package"
    frozen_root.mkdir()
    sandbox_root.mkdir()
    frozen = provider.ResolvedPiPackage(
        9, "npm:frozen", "npm:frozen", "2.0.0", str(frozen_root), {"skills": ["frozen"]}
    )
    current_sandbox = provider.ResolvedPiPackage(
        10, "npm:pi-sandbox", "npm:pi-sandbox", "1.0.0", str(sandbox_root), {}
    )
    resumed_runtime = runtime.PiRuntimeSnapshotBuilder(tmp_path / "resumed-runtime")
    resumed_runtime.materialize(
        "DIMMELA-1-implementation-1", resolved, (frozen,)
    )
    resumed = resumed_runtime.materialize(
        "DIMMELA-1-implementation-1", resolved, (current_sandbox,)
    )
    assert resumed.packages == (frozen, current_sandbox)

    updated_sandbox = provider.ResolvedPiPackage(
        11, "npm:pi-sandbox", "npm:pi-sandbox", "2.0.0", str(sandbox_root), {}
    )
    resumed = resumed_runtime.materialize(
        "DIMMELA-1-implementation-1", resolved, (updated_sandbox,)
    )
    assert resumed.packages == (frozen, updated_sandbox)
    assert resumed_runtime.materialize(
        "DIMMELA-1-implementation-1", resolved
    ).packages == (frozen, updated_sandbox)


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


def test_pi_runtime_allows_resolved_packages_read_only(tmp_path: Path) -> None:
    runtime = importlib.import_module("carlo.pi_runtime")
    provider = importlib.import_module("carlo.provider")
    package_root = tmp_path / "managed-package"
    package_root.mkdir()
    model = provider.ResolvedModel(
        model_provider_id=1,
        available_model_id=2,
        provider_slug="omlx",
        base_url="http://127.0.0.1:11435/v1",
        api="openai-completions",
        external_id="qwen",
        display_name="Qwen local",
        api_key="",
        compatibility={},
        input_modalities=("text",),
        reasoning=True,
        context_window=65_536,
        max_tokens=16_384,
        compaction_enabled=True,
        reserve_tokens=16_384,
        keep_recent_tokens=13_107,
    )
    package = provider.ResolvedPiPackage(
        1, "git:skills", "git:skills", "abc", str(package_root), {"skills": ["tdd"]}
    )

    snapshot = runtime.PiRuntimeSnapshotBuilder(tmp_path / "runtime").materialize(
        "CAR-1-implementation-1", model, (package,)
    )
    filesystem = json.loads((snapshot.agent_dir / "sandbox.json").read_text())[
        "filesystem"
    ]

    assert filesystem["allowRead"] == [".", str(package_root.resolve())]
    assert filesystem["allowWrite"] == ["."]


def test_pi_runtime_sandboxes_to_absolute_project_and_reads_gitconfig(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = importlib.import_module("carlo.pi_runtime")
    provider = importlib.import_module("carlo.provider")
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    user_temp = tmp_path / "user-temp"
    user_temp.mkdir()
    monkeypatch.setattr(runtime.Path, "home", lambda: home)
    monkeypatch.setattr(runtime.tempfile, "gettempdir", lambda: str(user_temp))
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

    snapshot = runtime.PiRuntimeSnapshotBuilder(tmp_path / "runtime").materialize(
        "CAR-1-implementation-1", model, project=project
    )
    filesystem = json.loads((snapshot.agent_dir / "sandbox.json").read_text())[
        "filesystem"
    ]

    assert filesystem["allowRead"] == [str(project.resolve()), str(home / ".gitconfig")]
    assert filesystem["allowWrite"] == [str(project.resolve()), str(user_temp.resolve())]


def test_pi_runtime_allows_extra_paths_read_only(tmp_path: Path) -> None:
    runtime = importlib.import_module("carlo.pi_runtime")
    provider = importlib.import_module("carlo.provider")
    project = tmp_path / "project"
    project.mkdir()
    uploads = tmp_path / "uploads"
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

    snapshot = runtime.PiRuntimeSnapshotBuilder(tmp_path / "runtime").materialize(
        "CAR-1-implementation-1", model, project=project, read_paths=(uploads,)
    )
    filesystem = json.loads((snapshot.agent_dir / "sandbox.json").read_text())[
        "filesystem"
    ]

    assert str(uploads.resolve()) in filesystem["allowRead"]
    assert str(uploads.resolve()) not in filesystem["allowWrite"]


def test_pi_runtime_advertises_image_input_for_openai_gpt_models(tmp_path: Path) -> None:
    runtime = importlib.import_module("carlo.pi_runtime")
    provider = importlib.import_module("carlo.provider")
    model = provider.ResolvedModel(
        model_provider_id=1,
        available_model_id=2,
        provider_slug="openai",
        base_url="https://api.openai.com/v1",
        api="openai-responses",
        external_id="gpt-5.6-sol",
        display_name="GPT",
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

    snapshot = runtime.PiRuntimeSnapshotBuilder(tmp_path / "runtime").materialize(
        "discovery-1", model
    )
    configured = json.loads((snapshot.agent_dir / "models.json").read_text())

    assert configured["providers"]["openai"]["models"][0]["input"] == [
        "text",
        "image",
    ]


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
