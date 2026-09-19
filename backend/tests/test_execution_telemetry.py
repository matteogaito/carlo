from pathlib import Path

import pytest

from carlo.execution_telemetry import ExecutionTelemetry, ToolBudgetExceeded, summarize_sessions


def test_tool_budget_stops_at_limit_and_records_outside_reads(tmp_path: Path) -> None:
    telemetry = ExecutionTelemetry(tmp_path, {"src/owned.py"}, max_tool_calls=2)
    telemetry.observe({"type": "tool_execution_start", "toolName": "read", "args": {"path": "src/owned.py"}})
    telemetry.observe({"type": "tool_execution_start", "toolName": "read", "args": {"path": "src/other.py"}})
    assert telemetry.outside_reads == ["src/other.py"]
    with pytest.raises(ToolBudgetExceeded):
        telemetry.observe({"type": "tool_execution_start", "toolName": "bash", "args": {"command": "true"}})
    assert telemetry.tool_calls == 3


def test_session_usage_counts_model_calls_and_compactions(tmp_path: Path) -> None:
    telemetry = ExecutionTelemetry(tmp_path, set(), max_tool_calls=20)
    telemetry.observe({"type": "message_end", "message": {"role": "assistant", "usage": {"input": 100, "output": 12}}})
    telemetry.observe({"type": "message_end", "message": {"role": "assistant", "usage": {"input": 80, "output": 8}}})
    telemetry.observe({"type": "session_compact"})
    assert telemetry.summary("done")["model_calls"] == 2
    assert telemetry.summary("done")["peak_prompt_tokens"] == 100
    assert telemetry.summary("done")["generated_tokens"] == 20
    assert telemetry.summary("done")["compactions"] == 1


def test_parent_summary_adds_sessions_and_keeps_peak() -> None:
    assert summarize_sessions([
        {"duration_seconds": 2.5, "model_calls": 2, "tool_calls": 3, "outside_reads": ["x"], "compactions": 0, "peak_prompt_tokens": 100, "generated_tokens": 10, "outcome": "failed"},
        {"duration_seconds": 1.5, "model_calls": 1, "tool_calls": 2, "outside_reads": [], "compactions": 1, "peak_prompt_tokens": 80, "generated_tokens": 5, "outcome": "done"},
    ]) == {
        "sessions": 2, "duration_seconds": 4.0, "model_calls": 3,
        "tool_calls": 5, "outside_reads": 1, "compactions": 1,
        "peak_prompt_tokens": 100, "generated_tokens": 15,
        "outcomes": {"failed": 1, "done": 1},
    }
