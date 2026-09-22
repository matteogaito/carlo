"""Small, content-free counters for one Pi implementation session."""

import time
from pathlib import Path
from typing import Any


class ToolBudgetExceeded(RuntimeError):
    pass


class ExecutionTelemetry:
    def __init__(
        self,
        repository: Path,
        package_files: set[str],
        estimated_tool_calls: int,
        *,
        soft_tool_calls: int = 50,
        hard_tool_calls: int = 120,
    ) -> None:
        if not 1 <= estimated_tool_calls <= soft_tool_calls < hard_tool_calls:
            raise ValueError("tool-call thresholds must satisfy 1 <= estimate <= soft < hard")
        self.repository = repository.resolve()
        self.package_files = package_files
        self.estimated_tool_calls = estimated_tool_calls
        self.soft_tool_calls = soft_tool_calls
        self.hard_tool_calls = hard_tool_calls
        self.soft_budget_exceeded = False
        self.started = time.monotonic()
        self.tool_calls = 0
        self.model_calls = 0
        self.compactions = 0
        self.peak_prompt_tokens = 0
        self.generated_tokens = 0
        self.outside_reads: list[str] = []

    def observe(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "tool_execution_start":
            self.tool_calls += 1
            if event.get("toolName") == "read":
                args = event.get("args") or {}
                name = args.get("path") or args.get("file_path") if isinstance(args, dict) else None
                if isinstance(name, str):
                    path = (self.repository / name).resolve()
                    relative = str(path.relative_to(self.repository)) if path.is_relative_to(self.repository) else str(path)
                    if relative not in self.package_files:
                        self.outside_reads.append(relative)
            if self.tool_calls > self.soft_tool_calls:
                self.soft_budget_exceeded = True
            if self.tool_calls > self.hard_tool_calls:
                raise ToolBudgetExceeded(f"hard tool-call safety limit exceeded ({self.hard_tool_calls})")
        elif kind in {"session_compact", "context_compaction_retry"}:
            self.compactions += 1
        elif kind == "message_end":
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return
            usage = message.get("usage")
            if not isinstance(usage, dict):
                return
            self.model_calls += 1
            self.peak_prompt_tokens = max(self.peak_prompt_tokens, int(usage.get("input") or 0))
            self.generated_tokens += int(usage.get("output") or 0)

    def summary(self, outcome: str) -> dict[str, Any]:
        return {
            "duration_seconds": round(time.monotonic() - self.started, 3),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "estimated_tool_calls": self.estimated_tool_calls,
            "soft_tool_calls": self.soft_tool_calls,
            "hard_tool_calls": self.hard_tool_calls,
            "soft_budget_exceeded": self.soft_budget_exceeded,
            "outside_reads": self.outside_reads,
            "compactions": self.compactions,
            "peak_prompt_tokens": self.peak_prompt_tokens,
            "generated_tokens": self.generated_tokens,
            "outcome": outcome,
        }


def summarize_sessions(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes: dict[str, int] = {}
    for item in sessions:
        outcome = str(item["outcome"])
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {
        "sessions": len(sessions),
        "duration_seconds": round(sum(item["duration_seconds"] for item in sessions), 3),
        "model_calls": sum(item["model_calls"] for item in sessions),
        "tool_calls": sum(item["tool_calls"] for item in sessions),
        "outside_reads": sum(len(item["outside_reads"]) for item in sessions),
        "compactions": sum(item["compactions"] for item in sessions),
        "peak_prompt_tokens": max((item["peak_prompt_tokens"] for item in sessions), default=0),
        "generated_tokens": sum(item["generated_tokens"] for item in sessions),
        "outcomes": outcomes,
    }
