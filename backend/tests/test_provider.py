import json
from pathlib import Path

import pytest

from carlo.provider import AgentProfile, PiProvider


@pytest.mark.asyncio
async def test_pi_provider_uses_explicit_read_only_session(tmp_path: Path) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
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

    result = await provider.run(profile, "Inspect the repo", str(tmp_path), "CAR-1-plan-1")

    assert json.loads(result.output)["plan_markdown"] == "P"
    argv = result.events[0]["argv"]
    assert argv == [
        "--mode", "json", "--print", "--approve", "--session-id", "CAR-1-plan-1",
        "--session-dir", str(sessions), "--model", "openai/gpt-5",
        "--thinking", "high", "--tools", "read,grep,find,ls",
        "--skill", str(skill_root / "carlo-planning"),
        "Inspect the repo",
    ]


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
