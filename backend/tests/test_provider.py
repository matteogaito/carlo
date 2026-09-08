import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from carlo.pi_runtime import PiRuntimeSnapshotBuilder
from carlo.provider import (
    AgentProfile,
    ContextLimitError,
    PiProvider,
    ProviderError,
    ResolvedModel,
    ResolvedPiPackage,
)


def _resolved_model() -> ResolvedModel:
    return ResolvedModel(
        model_provider_id=1,
        available_model_id=2,
        provider_slug="omlx",
        base_url="http://127.0.0.1:11435/v1",
        api="openai-completions",
        external_id="qwen",
        display_name="Qwen",
        api_key="secret-local-key",
        compatibility={},
        input_modalities=("text",),
        reasoning=True,
        context_window=65_536,
        max_tokens=16_384,
        compaction_enabled=True,
        reserve_tokens=16_384,
        keep_recent_tokens=13_107,
    )


def _context_retry_provider(tmp_path: Path, *, always_fail: bool = False) -> tuple[PiProvider, Path]:
    executable = tmp_path / "fake-pi"
    calls = tmp_path / "calls.jsonl"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"calls = pathlib.Path({str(calls)!r})\n"
        "with calls.open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "attempt = len(calls.read_text().splitlines())\n"
        f"if attempt == 1 or {always_fail!r}:\n"
        " print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', "
        "'content': [], 'stopReason': 'error', 'errorMessage': "
        "'Prompt too long: context window exceeded'}}))\n"
        "else:\n"
        " print(json.dumps({'type': 'final', 'output': 'done'}))\n"
    )
    executable.chmod(0o755)
    return PiProvider(str(executable), tmp_path / "sessions"), calls


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", ["run", "open_conversation"])
async def test_pi_provider_rejects_project_local_sandbox_configuration(
    tmp_path: Path, launch: str
) -> None:
    project = tmp_path / "project"
    (project / ".pi").mkdir(parents=True)
    (project / ".pi" / "sandbox.json").write_text("{}")
    marker = tmp_path / "started"
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).touch()\n"
        "print('{\"type\": \"final\", \"output\": \"done\"}')\n"
    )
    executable.chmod(0o755)
    provider = PiProvider(str(executable), tmp_path / "sessions")
    profile = AgentProfile("plan", None, None, (), ())

    with pytest.raises(
        ProviderError, match="project-local Pi sandbox configuration is forbidden"
    ):
        if launch == "run":
            await provider.run(profile, "Plan", str(project), "CAR-sandbox")
        else:
            await provider.open_conversation(profile, str(project), "CAR-sandbox")

    assert not marker.exists()


@pytest.mark.asyncio
async def test_pi_provider_resumes_once_after_context_compaction(tmp_path: Path) -> None:
    provider, calls_path = _context_retry_provider(tmp_path)
    seen: list[str] = []

    async def collect(event: dict[str, Any]) -> None:
        seen.append(str(event["type"]))

    result = await provider.run(
        AgentProfile("implementation", None, None, (), ()),
        "Implement everything",
        str(tmp_path),
        "CAR-1-implementation-1",
        collect,
    )

    assert result.output == "done"
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert len(calls) == 2
    assert "Implement everything" in calls[0]
    assert "Continue from the compacted session and finish the current task." in calls[1]
    assert "Implement everything" not in calls[1]
    assert all("CAR-1-implementation-1" in call for call in calls)
    assert "context_compaction_retry" in seen


@pytest.mark.asyncio
async def test_pi_provider_retries_context_compaction_only_once(tmp_path: Path) -> None:
    provider, calls_path = _context_retry_provider(tmp_path, always_fail=True)

    with pytest.raises(ContextLimitError):
        await provider.run(
            AgentProfile("implementation", None, None, (), ()),
            "Implement",
            str(tmp_path),
            "CAR-2-implementation-1",
        )

    assert len(calls_path.read_text().splitlines()) == 2


@pytest.mark.asyncio
async def test_pi_provider_resolves_project_boundary_in_successful_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_link = tmp_path / "project-link"
    project_link.symlink_to(project, target_is_directory=True)
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'type': 'agent_start'}))\n"
        "print(json.dumps({'type': 'tool_execution_start', 'toolName': 'read', 'args': {'path': 'README.md'}}))\n"
        "print(json.dumps({'type': 'final', 'output': json.dumps({'brief_markdown': 'B', 'plan_markdown': 'P', 'metadata': {}}), 'argv': sys.argv[1:]}))\n"
    )
    executable.chmod(0o755)
    sessions = tmp_path / "sessions"
    skill_root = tmp_path / "skills"
    (skill_root / "carlo-planning").mkdir(parents=True)
    provider = PiProvider(str(executable), sessions, skill_root)
    profile = AgentProfile(
        name="plan",
        model="openai/gpt-5",
        effort="high",
        tools=("read", "grep", "find", "ls"),
        skills=("carlo-planning",),
    )

    seen: list[str] = []

    async def collect(event: dict[str, Any]) -> None:
        seen.append(str(event["type"]))

    original_create_subprocess_exec = asyncio.create_subprocess_exec
    subprocess_cwds: list[str] = []

    async def capture_subprocess_cwd(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        subprocess_cwds.append(kwargs["cwd"])
        return await original_create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_subprocess_cwd)

    result = await provider.run(
        profile, "Inspect the repo", str(project_link), "CAR-1-plan-1", collect
    )

    assert json.loads(result.output)["plan_markdown"] == "P"
    assert seen == ["agent_start", "tool_execution_start", "final"]
    assert [event["type"] for event in result.events] == seen
    assert subprocess_cwds == [str(project.resolve())]
    argv = result.events[-1]["argv"]
    assert argv == [
        "--mode", "json", "--print", "--approve", "--session-id", "CAR-1-plan-1",
        "--session-dir", str(sessions), "--model", "openai/gpt-5",
        "--thinking", "high", "--tools", "read",
        "--skill", str(skill_root / "carlo-planning"),
        "Inspect the repo",
    ]


@pytest.mark.asyncio
async def test_pi_provider_reports_assistant_api_error(tmp_path: Path) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'message_end', 'message': {"
        "'role': 'assistant', 'content': [], 'stopReason': 'error', "
        "'errorMessage': '400: function tools require /v1/responses'}}))\n"
    )
    executable.chmod(0o755)
    provider = PiProvider(str(executable), tmp_path / "sessions")

    with pytest.raises(
        ProviderError, match=r"400: function tools require /v1/responses"
    ):
        await provider.run(
            AgentProfile("plan", None, None, (), ()),
            "Plan",
            str(tmp_path),
            "CAR-2-plan",
        )


@pytest.mark.asyncio
async def test_pi_debug_writes_replay_without_api_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'final', 'output': 'done'}))\n"
    )
    executable.chmod(0o755)
    runtime_root = tmp_path / "runtime"
    provider = PiProvider(
        str(executable),
        tmp_path / "sessions",
        runtime_builder=PiRuntimeSnapshotBuilder(runtime_root),
        debug=True,
    )
    profile = AgentProfile(
        "plan", None, None, (), (), resolved_model=_resolved_model()
    )

    with caplog.at_level(logging.DEBUG, logger="carlo.provider"):
        await provider.run(
            profile, "Inspect this repository", str(tmp_path), "CAR-3-plan"
        )

    debug_dir = runtime_root / "CAR-3-plan"
    assert (debug_dir / "instruction.md").read_text() == "Inspect this repository"
    replay = (debug_dir / "replay.sh").read_text()
    assert "secret-local-key" not in replay
    assert "CARLO_PI_MODEL_API_KEY" in replay
    assert "instruction.md" in replay
    assert "Pi debug launch session=CAR-3-plan" in caplog.text
    assert "secret-local-key" not in caplog.text


@pytest.mark.asyncio
async def test_pi_provider_loads_managed_superpowers_and_frontend_skill(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'type': 'final', 'output': 'done', 'argv': sys.argv[1:]}))\n"
    )
    executable.chmod(0o755)
    root = tmp_path / "managed"
    superpowers = root / "checkouts" / "superpowers" / ("a" * 40)
    ponytail = root / "checkouts" / "ponytail" / ("c" * 40)
    frontend = root / "checkouts" / "frontend-design" / ("b" * 40) / "skills" / "frontend-design"
    superpowers.mkdir(parents=True)
    ponytail.mkdir(parents=True)
    frontend.mkdir(parents=True)
    (frontend / "SKILL.md").write_text("---\nname: frontend-design\n---\n")
    manifest = root / "revisions.json"
    manifest.write_text(json.dumps({"superpowers": "a" * 40, "frontend-design": "b" * 40, "ponytail": "c" * 40}))
    provider = PiProvider(
        str(executable),
        tmp_path / "sessions",
        resource_root=root,
        managed_packages=("superpowers", "ponytail"),
        managed_skills={"frontend-design": "skills/frontend-design"},
        resource_manifest=manifest,
    )
    replacement_manifest = json.dumps({"superpowers": "c" * 40})
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(manifest)!r}).write_text({replacement_manifest!r})\n"
        "print(json.dumps({'type': 'final', 'output': 'done', 'argv': sys.argv[1:]}))\n"
    )

    result = await provider.run(
        AgentProfile(
            "plan",
            None,
            None,
            (),
            ("frontend-design",),
            packages=("superpowers", "ponytail"),
        ),
        "Plan the frontend",
        str(tmp_path),
        "CAR-4-plan",
    )

    argv = result.events[-1]["argv"]
    assert argv[:4] == ["-e", str(superpowers), "-e", str(ponytail)]
    assert ["--skill", str(frontend)] == argv[argv.index("--skill"):argv.index("--skill") + 2]
    assert result.resource_revisions == {
        "superpowers": "a" * 40,
        "frontend-design": "b" * 40,
        "ponytail": "c" * 40,
    }


@pytest.mark.asyncio
async def test_pi_provider_loads_only_packages_selected_by_the_profile(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'type': 'final', 'output': 'done', 'argv': sys.argv[1:]}))\n"
    )
    executable.chmod(0o755)
    root = tmp_path / "managed"
    superpowers = root / "checkouts" / "superpowers" / ("a" * 40)
    ponytail = root / "checkouts" / "ponytail" / ("b" * 40)
    superpowers.mkdir(parents=True)
    ponytail.mkdir(parents=True)
    manifest = root / "revisions.json"
    manifest.write_text(
        json.dumps({"superpowers": "a" * 40, "ponytail": "b" * 40})
    )
    provider = PiProvider(
        str(executable),
        tmp_path / "sessions",
        resource_root=root,
        managed_packages=("superpowers", "ponytail"),
        resource_manifest=manifest,
    )

    result = await provider.run(
        AgentProfile("plan", None, None, (), (), packages=("ponytail",)),
        "Plan",
        str(tmp_path),
        "CAR-5-plan",
    )

    argv = result.events[-1]["argv"]
    assert ["-e", str(ponytail)] == argv[:2]
    assert str(superpowers) not in argv


@pytest.mark.asyncio
async def test_pi_provider_loads_resolved_package_artifacts(tmp_path: Path) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'type': 'final', 'output': 'done', 'argv': sys.argv[1:]}))\n"
    )
    executable.chmod(0o755)
    first = tmp_path / "pippo"
    second = tmp_path / "tools"
    first.mkdir()
    second.mkdir()
    packages = (
        ResolvedPiPackage(1, "npm:pippo", "npm:pippo", "1.5.0", str(first), {"skills": ["pippo"]}),
        ResolvedPiPackage(2, "git:tools", "git:tools", "a" * 40, str(second), {"skills": []}),
    )
    provider = PiProvider(str(executable), tmp_path / "sessions")

    result = await provider.run(
        AgentProfile("implementation", None, None, (), (), packages=packages),
        "Implement",
        str(tmp_path),
        "CAR-8-implementation",
    )

    assert result.events[-1]["argv"][:4] == ["-e", str(first), "-e", str(second)]
    assert result.loaded_packages == {"npm:pippo": "1.5.0", "git:tools": "a" * 40}


def test_pi_provider_omits_unsandboxed_builtin_filesystem_tools(tmp_path: Path) -> None:
    provider = PiProvider("pi", tmp_path / "sessions")

    arguments = provider._profile_arguments(
        AgentProfile("implementation", None, None, ("read", "bash", "grep", "find", "ls"), ())
    )

    assert arguments == ["--tools", "read,bash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", ["run", "open_conversation"])
async def test_pi_provider_requires_rg_for_resolved_sandbox_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch: str
) -> None:
    package_root = tmp_path / "pi-sandbox"
    package_root.mkdir()
    marker = tmp_path / "started"
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).touch()\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    provider = PiProvider(str(executable), tmp_path / "sessions")
    profile = AgentProfile(
        "implementation",
        None,
        None,
        (),
        (),
        packages=(
            ResolvedPiPackage(1, "npm:pi-sandbox", "npm:pi-sandbox", "1.0.0", str(package_root), {}),
        ),
    )

    with pytest.raises(ProviderError, match="pi-sandbox requires rg on PATH"):
        if launch == "run":
            await provider.run(profile, "Implement", str(tmp_path), "CAR-rg")
        else:
            await provider.open_conversation(profile, str(tmp_path), "CAR-rg")

    assert not marker.exists()


@pytest.mark.asyncio
async def test_pi_provider_rejects_selected_managed_skill_missing_from_snapshot(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'final', 'output': 'done'}))\n"
    )
    executable.chmod(0o755)
    root = tmp_path / "managed"
    root.mkdir()
    manifest = root / "revisions.json"
    manifest.write_text("{}")
    provider = PiProvider(
        str(executable),
        tmp_path / "sessions",
        resource_root=root,
        managed_skills={"frontend-design": "skills/frontend-design"},
        resource_manifest=manifest,
    )

    with pytest.raises(ProviderError, match="managed Pi skill is unavailable"):
        await provider.run(
            AgentProfile("plan", None, None, (), ("frontend-design",)),
            "Plan",
            str(tmp_path),
            "CAR-6-plan",
        )


@pytest.mark.asyncio
async def test_pi_provider_reports_only_skills_actually_invoked_or_read(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'tool_execution_start', 'toolCallId': 'skill-ok', "
        "'toolName': 'read', 'args': {'path': '/pi/skills/frontend-design/SKILL.md'}}))\n"
        "print(json.dumps({'type': 'tool_execution_end', 'toolCallId': 'skill-ok', "
        "'toolName': 'read', 'isError': False}))\n"
        "print(json.dumps({'type': 'tool_execution_start', 'toolCallId': 'skill-failed', "
        "'toolName': 'read', 'args': {'path': '/pi/skills/database/SKILL.md'}}))\n"
        "print(json.dumps({'type': 'tool_execution_end', 'toolCallId': 'skill-failed', "
        "'toolName': 'read', 'isError': True}))\n"
        "print(json.dumps({'type': 'tool_execution_start', 'toolName': 'read', "
        "'args': {'path': '/repo/README.md'}}))\n"
        "print(json.dumps({'type': 'final', 'output': 'done'}))\n"
    )
    executable.chmod(0o755)
    provider = PiProvider(str(executable), tmp_path / "sessions")

    result = await provider.run(
        AgentProfile("plan", None, None, (), ("carlo-planning",)),
        "/skill:carlo-planning Plan the change",
        str(tmp_path),
        "CAR-2-plan",
    )

    assert result.used_skills == ("carlo-planning", "frontend-design")

    malformed = await provider.run(
        AgentProfile("plan", None, None, (), ()),
        "/skill:   Plan the change",
        str(tmp_path),
        "CAR-3-plan",
    )
    assert malformed.used_skills == ("frontend-design",)


@pytest.mark.asyncio
async def test_pi_provider_accepts_json_events_larger_than_asyncio_default(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'tool_execution_end', 'result': 'x' * 70000}))\n"
        "print(json.dumps({'type': 'final', 'output': 'plan ready'}))\n"
    )
    executable.chmod(0o755)
    provider = PiProvider(str(executable), tmp_path / "sessions")

    result = await provider.run(
        AgentProfile("plan", None, None, (), ()),
        "Plan",
        str(tmp_path),
        "CAR-2-plan-1",
    )

    assert result.output == "plan ready"
    assert len(result.events[0]["result"]) == 70000


@pytest.mark.asyncio
async def test_pi_provider_rejects_a_json_event_larger_than_four_mebibytes(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'tool_execution_end', 'result': 'x' * (4 * 1024 * 1024)}))\n"
    )
    executable.chmod(0o755)
    provider = PiProvider(str(executable), tmp_path / "sessions")

    with pytest.raises(ProviderError, match="larger than 4 MiB"):
        await provider.run(
            AgentProfile("plan", None, None, (), ()),
            "Plan",
            str(tmp_path),
            "CAR-3-plan-1",
        )


@pytest.mark.asyncio
async def test_pi_provider_streams_a_persisted_rpc_conversation(tmp_path: Path) -> None:
    assert hasattr(PiProvider, "open_conversation"), "RPC conversations are missing"
    executable = Path(__file__).parent / "fixtures" / "fake_pi_rpc.py"
    executable.chmod(0o755)
    sessions = tmp_path / "sessions"
    skill = tmp_path / "skill"
    skill.mkdir()
    guard = tmp_path / "guard.mjs"
    guard.write_text("export default () => {}")
    provider = PiProvider(str(executable), sessions)
    profile = AgentProfile(
        name="discovery",
        model="openai/gpt-5.6-sol",
        effort="high",
        tools=("read", "bash", "grep", "find", "ls"),
        skills=(str(skill),),
    )

    session = await provider.open_conversation(
        profile,
        str(tmp_path),
        "discovery-42",
        extensions=(guard,),
        environment={"CARLO_DISCOVERY_COMMANDS": '["make verify"]'},
    )
    events = [event async for event in session.prompt("Inspect auth")]
    assert [event.type for event in events] == [
        "agent_start",
        "message_update",
        "message_update",
        "tool_execution_start",
        "tool_execution_end",
        "agent_end",
    ]
    assert "".join(str(event.payload.get("delta", "")) for event in events) == "Repository inspected."

    state = await session.get_state()
    assert state.session_id == "discovery-42"
    assert state.session_file == "/sessions/discovery-42.jsonl"
    assert state.context_percent == 12.5
    assert state.raw["discoveryCommands"] == '["make verify"]'
    assert state.raw["argv"] == [
        "--mode", "rpc", "--approve", "--session-id", "discovery-42",
        "--session-dir", str(sessions), "--model", "openai/gpt-5.6-sol",
        "--thinking", "high", "--tools", "read,bash",
        "--skill", str(skill), "--extension", str(guard),
    ]
    entries, cursor = await session.get_entries()
    assert entries == [{"id": "entry-1", "type": "message"}]
    assert cursor == "entry-1"
    assert await session.compact("Keep decisions") == "Compact memory"
    await session.abort()
    await session.close()
    assert provider.status("discovery-42") == "idle"


@pytest.mark.asyncio
async def test_pi_provider_uses_managed_model_snapshot_for_all_sessions(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'type': 'final', 'output': 'done', "
        "'argv': sys.argv[1:], 'agentDir': os.getenv('PI_CODING_AGENT_DIR'), "
        "'hasKey': bool(os.getenv('CARLO_PI_MODEL_API_KEY'))}))\n"
    )
    executable.chmod(0o755)
    runtime_root = tmp_path / "runtime"
    provider = PiProvider(
        str(executable),
        tmp_path / "sessions",
        runtime_builder=PiRuntimeSnapshotBuilder(runtime_root),
    )
    profile = AgentProfile("implementation", "legacy/model", None, (), (), _resolved_model())

    result = await provider.run(profile, "Implement", str(tmp_path), "CAR-9-implementation")
    final = result.events[-1]
    assert final["hasKey"] is True
    assert final["agentDir"] == str(runtime_root / "CAR-9-implementation")
    assert final["argv"][final["argv"].index("--model") + 1] == "omlx/qwen"
    assert "secret-local-key" not in json.dumps(final["argv"])

    rpc_executable = Path(__file__).parent / "fixtures" / "fake_pi_rpc.py"
    rpc_executable.chmod(0o755)
    provider.executable = str(rpc_executable)
    session = await provider.open_conversation(
        profile, str(tmp_path), "discovery-99"
    )
    state = await session.get_state()
    assert state.raw["hasModelKey"] is True
    assert state.raw["agentDir"] == str(runtime_root / "discovery-99")
    assert state.raw["argv"][state.raw["argv"].index("--model") + 1] == "omlx/qwen"
    await session.close()
