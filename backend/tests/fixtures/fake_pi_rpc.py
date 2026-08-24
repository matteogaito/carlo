#!/usr/bin/env python3
import json
import os
import sys


def send(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


session_id = sys.argv[sys.argv.index("--session-id") + 1]
for line in sys.stdin:
    command = json.loads(line)
    command_id = command.get("id")
    kind = command["type"]
    if kind == "prompt":
        send({"id": command_id, "type": "response", "command": kind, "success": True})
        send({"type": "agent_start"})
        send({"type": "message_update", "delta": "Repository "})
        send({"type": "message_update", "delta": "inspected."})
        send({"type": "tool_execution_start", "toolName": "read", "args": {"path": "README.md"}})
        send({"type": "tool_execution_end", "toolName": "read", "result": "CARLO"})
        send({"type": "agent_end"})
    elif kind == "get_state":
        send({
            "id": command_id,
            "type": "response",
            "command": kind,
            "success": True,
            "data": {
                "sessionId": session_id,
                "sessionFile": f"/sessions/{session_id}.jsonl",
                "isStreaming": False,
                "contextUsage": {"percent": 12.5},
                "argv": sys.argv[1:],
                "discoveryCommands": os.getenv("CARLO_DISCOVERY_COMMANDS"),
                "agentDir": os.getenv("PI_CODING_AGENT_DIR"),
                "hasModelKey": bool(os.getenv("CARLO_PI_MODEL_API_KEY")),
            },
        })
    elif kind == "get_entries":
        send({
            "id": command_id,
            "type": "response",
            "command": kind,
            "success": True,
            "data": {"entries": [{"id": "entry-1", "type": "message"}], "leafId": "entry-1"},
        })
    elif kind == "compact":
        send({
            "id": command_id,
            "type": "response",
            "command": kind,
            "success": True,
            "data": {"summary": "Compact memory"},
        })
    elif kind == "abort":
        send({"id": command_id, "type": "response", "command": kind, "success": True})
    else:
        send({"id": command_id, "type": "response", "command": kind, "success": False, "error": "unsupported"})
