"""The single plan contract and provider loop used by Tasks and Discovery."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import Project
from .provider import AgentEventHandler, AgentProfile, AgentResult, CodingAgentProvider, ProviderError


class PlanningError(ValueError):
    pass


@dataclass(frozen=True)
class PlanningRequest:
    project: Project
    title: str
    goal: str
    session_id: str
    prompt_path: str | None = None
    handoff: str = ""
    model_id: int | None = None
    instruction: str | None = None
    one_package: bool = False


@dataclass(frozen=True)
class PlanningQuestion:
    text: str


def proposal_source(proposal: dict[str, Any], state: dict[str, Any]) -> str:
    evidence = {
        "title": proposal.get("title"),
        "megaprompt": proposal.get("megaprompt"),
        "depends_on": proposal.get("depends_on", []),
        "summary": state.get("summary"),
        "findings": state.get("findings", []),
        "decisions": state.get("decisions", []),
        "unresolved_questions": state.get("unresolved_questions", []),
        "inspected_resources": state.get("inspected_resources", []),
        "commands": state.get("commands", []),
    }
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Planner:
    def __init__(self, provider: CodingAgentProvider, profile: AgentProfile | None):
        self.provider = provider
        self.profile = profile
        self.last_result: AgentResult | None = None

    async def plan(
        self,
        request: PlanningRequest,
        on_event: AgentEventHandler | None = None,
        on_retry: AgentEventHandler | None = None,
    ) -> "PlanPayload | PlanningQuestion":
        instruction = request.instruction or planning_instruction(request)
        if self.profile is not None and "carlo-planning" in self.profile.skills:
            instruction = f"/skill:carlo-planning {instruction}"
        for attempt in range(3):
            try:
                self.last_result = await self.provider.run(
                    self.profile, instruction, request.project.repository_path,
                    request.session_id, on_event=on_event,
                )
                raw = json.loads(self.last_result.output)
                question = raw.get("question") if isinstance(raw, dict) else None
                if isinstance(question, str) and question.strip():
                    return PlanningQuestion(question.strip())
                output = PlanPayload.model_validate(raw)
                if not output.metadata.implementation_tasks:
                    raise ValueError("metadata.implementation_tasks must contain at least one complete work package")
                if request.one_package and len(output.metadata.implementation_tasks) != 1:
                    raise ValueError("a subtask regeneration must contain exactly one complete work package")
                if not request.one_package and not (output.metadata.validation_commands or request.project.validation_commands):
                    raise ValueError("parent plan needs final integration validation commands")
                return output
            except ProviderError as error:
                raise PlanningError(str(error)) from error
            except ValueError as error:
                if attempt == 2:
                    raise PlanningError(f"planner returned incomplete work packages: {error}") from error
                if on_retry:
                    await on_retry({"attempt": attempt + 1, "error": str(error)})
                instruction = (
                    "The previous plan is invalid. Regenerate the entire JSON plan with complete "
                    "metadata.implementation_tasks work packages; do not return a partial patch. "
                    f"Validation errors:\n{error}"
                )
        raise AssertionError("unreachable")


class PlanMetadata(BaseModel):
    title: str | None = None
    description: str | None = None
    key_points: list[str] = Field(default_factory=list)
    implementation_tasks: list["ImplementationTask"] = Field(default_factory=list)
    skills: list[str]
    packages: list[str] = Field(default_factory=list)
    implementation_phases: list[str] = Field(default_factory=list)
    validation_commands: list[str]
    browser_validation: bool
    build_required: bool
    run_required: bool
    deployment_expected: bool
    risk_flags: list[str]
    affected_areas: list[str]

    @model_validator(mode="after")
    def ordered_packages(self) -> "PlanMetadata":
        ids = [item.id for item in self.implementation_tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("implementation_tasks ids must be unique")
        if [item.position for item in self.implementation_tasks] != list(range(len(ids))):
            raise ValueError("implementation_tasks positions must be zero-based and ordered")
        return self


class FileRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered(self) -> "FileRange":
        if self.end < self.start:
            raise ValueError("range end must be at least start")
        return self


class PackageFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    mode: Literal["edit", "read_only", "create"]
    ranges: list[FileRange] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def project_relative_path(cls, value: str) -> str:
        if value.startswith("/") or any(part in {".", "..", ""} for part in value.split("/")):
            raise ValueError("file path must be project-relative and cannot traverse directories")
        return value


class PackageVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commands: list[str] = Field(min_length=1)
    success: str = Field(min_length=1)


class PackageBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int = Field(default=20, ge=1, le=30)


class ImplementationTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    position: int = Field(ge=0)
    objective: str = Field(min_length=1)
    files: list[PackageFile] = Field(min_length=1)
    interfaces: list[str] = Field(min_length=1)
    changes: dict[str, str] = Field(min_length=1)
    constraints: list[str]
    verification: PackageVerification
    done_when: list[str] = Field(min_length=1)
    budget: PackageBudget = Field(default_factory=PackageBudget)

    @model_validator(mode="after")
    def changes_match_editable_files(self) -> "ImplementationTask":
        editable = {item.path for item in self.files if item.mode in {"edit", "create"}}
        if set(self.changes) != editable or any(not value.strip() for value in self.changes.values()):
            raise ValueError("changes must describe every edit/create file and no other file")
        return self


class PlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_markdown: str = Field(min_length=1)
    plan_markdown: str = Field(min_length=1)
    metadata: PlanMetadata



def work_package_example() -> dict[str, Any]:
    """The exact shape a single `implementation_tasks` entry must have.

    Shared by the planning prompt and the escalation prompt so a diagnosing
    model is never left to guess field names for the package it must emit.
    """
    return {
        "id": "stable id",
        "title": "concise task title",
        "position": 0,
        "objective": "one to three sentence observable outcome",
        "files": [
            {
                "path": "project-relative path",
                "mode": "edit | read_only | create",
                "reason": "why this file is needed",
            }
        ],
        "interfaces": [],
        "changes": {"project-relative path": "concrete instruction"},
        "constraints": [],
        "verification": {"commands": [], "success": "success criterion"},
        "done_when": [],
        "budget": {"max_tool_calls": 20},
    }


def planning_instruction(request: PlanningRequest, *, fresh_rework: bool = False) -> str:
    contract = {
        "brief_markdown": "evidence-oriented repository understanding",
        "plan_markdown": "concrete implementation plan",
        "metadata": {
            "title": "short implementation title",
            "description": "concise description of the approved approach",
            "key_points": [],
            "implementation_tasks": [work_package_example()],
            "skills": [],
            "implementation_phases": [],
            "validation_commands": [],
            "browser_validation": False,
            "build_required": False,
            "run_required": False,
            "deployment_expected": False,
            "risk_flags": [],
            "affected_areas": [],
        },
    }
    rework = (
        "This is a fresh rework. Start again from the original request and current "
        "repository; do not continue the failed implementation or treat its old plan "
        "as authoritative.\n"
        if fresh_rework
        else ""
    )
    prompt = f"Original Markdown request: {request.prompt_path}\n" if request.prompt_path else ""
    return (
        rework + f"Inspect the repository and plan task {request.title}: {request.goal}\n"
        f"{prompt}"
        f"Project policies: {json.dumps(request.project.policies)}\n"
        f"Project validation commands: {json.dumps(request.project.validation_commands)}\n"
        "If one high-impact answer is still required, return only "
        '{"question":"the single focused question"}. Ask no low-risk implementation questions. '
        "A parent plan must have runnable final integration validation commands, from its metadata or the project configuration. "
        "Return only JSON matching this shape:\n"
        f"{json.dumps(contract)}"
        + (f"\nDiscovery handoff:\n{request.handoff}" if request.handoff else "")
    )
