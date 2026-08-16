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
    provider = PiProvider(str(executable), sessions)
    profile = AgentProfile(
        name="plan",
        model="openai/gpt-5",
        effort="high",
        tools=("read", "grep", "find", "ls"),
        skills=(),
    )

    result = await provider.run(profile, "Inspect the repo", str(tmp_path), "CAR-1-plan-1")

    assert json.loads(result.output)["plan_markdown"] == "P"
    argv = result.events[0]["argv"]
    assert argv == [
        "--mode", "json", "--print", "--approve", "--session-id", "CAR-1-plan-1",
        "--session-dir", str(sessions), "--model", "openai/gpt-5",
        "--thinking", "high", "--tools", "read,grep,find,ls",
        "Inspect the repo",
    ]
