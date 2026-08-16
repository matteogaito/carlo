from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .domain import TaskStage, TaskStatus


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(12), unique=True)
    repository_path: Mapped[str] = mapped_column(Text, unique=True)
    default_branch: Mapped[str] = mapped_column(String(120), default="main")
    integration_branch: Mapped[str] = mapped_column(String(120), default="carlo-Dev")
    next_task_sequence: Mapped[int] = mapped_column(Integer, default=1)
    policies: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    validation_commands: Mapped[list[str]] = mapped_column(JSONB, default=list)

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class AgentProfile(TimestampMixin, Base):
    __tablename__ = "agent_profiles"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    provider: Mapped[str] = mapped_column(String(40), default="pi")
    model: Mapped[str | None] = mapped_column(String(160))
    effort: Mapped[str | None] = mapped_column(String(20))
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    default_skills: Mapped[list[str]] = mapped_column(JSONB, default=list)
    context_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("project_id", "sequence"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), default=TaskStatus.NOT_READY
    )
    stage: Mapped[TaskStage] = mapped_column(
        Enum(TaskStage, name="task_stage"), default=TaskStage.CREATED
    )
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_source: Mapped[str] = mapped_column(String(40), default="web")
    version: Mapped[int] = mapped_column(Integer, default=1)
    approved_plan_revision: Mapped[int | None] = mapped_column(Integer)
    branch_name: Mapped[str | None] = mapped_column(String(240))
    worktree_path: Mapped[str | None] = mapped_column(Text)
    checkpoint_sha: Mapped[str | None] = mapped_column(String(64))
    active_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_profiles.id")
    )

    project: Mapped[Project] = relationship(back_populates="tasks", lazy="joined")
    plans: Mapped[list["PlanRevision"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )


class PlanRevision(TimestampMixin, Base):
    __tablename__ = "plan_revisions"
    __table_args__ = (UniqueConstraint("task_id", "revision"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    brief_markdown: Mapped[str] = mapped_column(Text)
    plan_markdown: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[Task] = relationship(back_populates="plans")


class Attempt(TimestampMixin, Base):
    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("agent_profiles.id"))
    provider_session_id: Mapped[str | None] = mapped_column(String(160))
    instruction: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(40))
    checkpoint_sha: Mapped[str | None] = mapped_column(String(64))
    diff_hash: Mapped[str | None] = mapped_column(String(64))
    error_fingerprint: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    artifact_path: Mapped[str | None] = mapped_column(Text)


class ValidationRun(TimestampMixin, Base):
    __tablename__ = "validation_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("attempts.id", ondelete="SET NULL")
    )
    command: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    classification: Mapped[str] = mapped_column(String(30))
    summary: Mapped[str] = mapped_column(Text)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int | None] = mapped_column(Integer)


class Escalation(TimestampMixin, Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(String(80))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    diagnosis: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="started")


class Event(Base):
    __tablename__ = "events"

    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    task: Mapped[Task | None] = relationship(back_populates="events")
