import json
from pathlib import Path
from typing import Any

import pytest

from carlo.pi_runtime import PiRuntimeSnapshotBuilder
from carlo.provider import AgentProfile, PiProvider, ProviderError, ResolvedModel


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


@pytest.mark.asyncio
async def test_pi_provider_uses_explicit_read_only_session(tmp_path: Path) -> None:
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

    result = await provider.run(
        profile, "Inspect the repo", str(tmp_path), "CAR-1-plan-1", collect
    )

    assert json.loads(result.output)["plan_markdown"] == "P"
    assert seen == ["agent_start", "tool_execution_start", "final"]
    assert [event["type"] for event in result.events] == seen
    argv = result.events[-1]["argv"]
    assert argv == [
        "--mode", "json", "--print", "--approve", "--session-id", "CAR-1-plan-1",
        "--session-dir", str(sessions), "--model", "openai/gpt-5",
        "--thinking", "high", "--tools", "read,grep,find,ls",
        "--skill", str(skill_root / "carlo-planning"),
        "Inspect the repo",
    ]


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
    frontend = root / "checkouts" / "frontend-design" / ("b" * 40) / "skills" / "frontend-design"
    superpowers.mkdir(parents=True)
    frontend.mkdir(parents=True)
    (frontend / "SKILL.md").write_text("---\nname: frontend-design\n---\n")
    manifest = root / "revisions.json"
    manifest.write_text(json.dumps({"superpowers": "a" * 40, "frontend-design": "b" * 40}))
    provider = PiProvider(
        str(executable),
        tmp_path / "sessions",
        resource_root=root,
        managed_packages=("superpowers",),
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
        AgentProfile("plan", None, None, (), ("frontend-design",)),
        "Plan the frontend",
        str(tmp_path),
        "CAR-4-plan",
    )

    argv = result.events[-1]["argv"]
    assert argv[:2] == ["-e", str(superpowers)]
    assert ["--skill", str(frontend)] == argv[argv.index("--skill"):argv.index("--skill") + 2]
    assert result.resource_revisions == {
        "superpowers": "a" * 40,
        "frontend-design": "b" * 40,
    }


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
        "--thinking", "high", "--tools", "read,bash,grep,find,ls",
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
