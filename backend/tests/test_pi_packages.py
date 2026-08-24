import json
from pathlib import Path

import pytest


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    ("source", "identity", "pinned"),
    [
        ("npm:ponytail", "npm:ponytail", False),
        ("npm:@scope/pippo", "npm:@scope/pippo", False),
        ("npm:@scope/pippo@1.2.3", "npm:@scope/pippo", True),
        ("git:github.com/owner/repo", "git:github.com/owner/repo", False),
        ("git:github.com/owner/repo@v1", "git:github.com/owner/repo", True),
    ],
)
def test_parse_package_source(source: str, identity: str, pinned: bool) -> None:
    from carlo.pi_packages import PackageSource, parse_package_source

    assert parse_package_source(source) == PackageSource(identity, pinned)


@pytest.mark.parametrize("source", ["", "../local", "/tmp/package", "file:local"])
def test_rejects_nonportable_package_sources(source: str) -> None:
    from carlo.pi_packages import PiPackageError, parse_package_source

    with pytest.raises(PiPackageError, match="npm or Git"):
        parse_package_source(source)


@pytest.mark.asyncio
async def test_install_uses_pi_staging_and_promotes_manifest(tmp_path: Path) -> None:
    from carlo.pi_packages import PiPackageManager

    calls = tmp_path / "calls.json"
    pi = executable(
        tmp_path / "pi",
        f"""
python3 -c 'import json,os,pathlib,sys; root=pathlib.Path(os.environ["PI_CODING_AGENT_DIR"])/"npm/node_modules/pippo"; (root/"extensions").mkdir(parents=True); (root/"skills/pippo").mkdir(parents=True); (root/"extensions/index.js").write_text("export default () => {{}}") ; (root/"skills/pippo/SKILL.md").write_text("---\\nname: pippo\\ndescription: test\\n---\\n"); (root/"package.json").write_text(json.dumps({{"name":"pippo","version":"1.4.0","pi":{{"extensions":["./extensions/index.js"],"skills":["./skills"]}}}})); pathlib.Path("{calls}").write_text(json.dumps(sys.argv[1:]))' "$@"
""",
    )
    manager = PiPackageManager(pi, tmp_path / "artifacts", tmp_path / "pi.lock")

    installed = await manager.install("npm:pippo")

    assert json.loads(calls.read_text()) == ["install", "npm:pippo", "--no-approve"]
    assert installed.identity == "npm:pippo"
    assert installed.resolved_version == "1.4.0"
    assert Path(installed.artifact_path, "package.json").is_file()
    assert installed.resources["skills"] == ["pippo"]
    assert installed.resources["extensions"] == ["extensions/index.js"]


@pytest.mark.asyncio
async def test_failed_install_does_not_replace_promoted_artifact(tmp_path: Path) -> None:
    from carlo.pi_packages import PiPackageError, PiPackageManager

    pi = executable(
        tmp_path / "pi",
        """
python3 -c 'import json,os,pathlib; root=pathlib.Path(os.environ["PI_CODING_AGENT_DIR"])/"npm/node_modules/pippo"; root.mkdir(parents=True); (root/"package.json").write_text(json.dumps({"name":"pippo","version":"1.4.0","pi":{"skills":[]}}))'
""",
    )
    manager = PiPackageManager(pi, tmp_path / "artifacts", tmp_path / "pi.lock")
    installed = await manager.install("npm:pippo")
    old = Path(installed.artifact_path)
    executable(pi, 'echo "registry unavailable" >&2\nexit 4\n')

    with pytest.raises(PiPackageError, match="registry unavailable"):
        await manager.install("npm:pippo")

    assert old.is_dir()
    assert json.loads((old / "package.json").read_text())["version"] == "1.4.0"
