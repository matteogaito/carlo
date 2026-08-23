from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
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


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(30), default="member")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProjectMembership(TimestampMixin, Base):
    __tablename__ = "project_memberships"
    __table_args__ = (UniqueConstraint("user_id", "project_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(30), default="contributor")


class UserSession(TimestampMixin, Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(lazy="joined")


class Runner(TimestampMixin, Base):
    __tablename__ = "runners"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    host: Mapped[str] = mapped_column(String(253))
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(80))
    identity_file: Mapped[str] = mapped_column(Text)
    workspace_root: Mapped[str] = mapped_column(Text, default=".carlo")
    host_key: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str | None] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_check_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionRun(TimestampMixin, Base):
    __tablename__ = "action_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'cancelled', 'interrupted')",
            name="ck_action_runs_status",
        ),
        Index("ix_action_runs_queue", "status", "requested_at", "id"),
        Index("ix_action_runs_project_history", "project_id", "requested_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    requested_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    action_key: Mapped[str] = mapped_column(String(120))
    action_name: Mapped[str] = mapped_column(String(160))
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    runner_name: Mapped[str] = mapped_column(String(80))
    runner_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    internal_stage: Mapped[str] = mapped_column(String(30), default="queued")
    commit_sha: Mapped[str] = mapped_column(String(64))
    branch_name: Mapped[str | None] = mapped_column(String(240))
    origin: Mapped[str | None] = mapped_column(Text)
    env_file: Mapped[str | None] = mapped_column(Text)
    env_names: Mapped[list[str]] = mapped_column(JSONB, default=list)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_step: Mapped[int | None] = mapped_column(Integer)
    process_group: Mapped[int | None] = mapped_column(BigInteger)
    workspace_path: Mapped[str | None] = mapped_column(Text)
    artifact_path: Mapped[str] = mapped_column(Text)
    secret_path: Mapped[str | None] = mapped_column(Text)
    log_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    recent_output: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    cleanup_pending: Mapped[bool] = mapped_column(Boolean, default=False)

    steps: Mapped[list["ActionStep"]] = relationship(
        order_by="ActionStep.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class ActionStep(TimestampMixin, Base):
    __tablename__ = "action_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "position"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'cancelled', 'skipped')",
            name="ck_action_steps_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("action_runs.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    command: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    log_start: Mapped[int | None] = mapped_column(BigInteger)
    log_end: Mapped[int | None] = mapped_column(BigInteger)


class LoginFailure(Base):
    __tablename__ = "login_failures"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(String(80), index=True)
    source_ip: Mapped[str] = mapped_column(String(80), index=True)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
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
    prompt_path: Mapped[str | None] = mapped_column(Text)
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

    project: Mapped[Project] = relationship(back_populates="tasks", lazy="selectin")
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


class NotificationCursor(TimestampMixin, Base):
    __tablename__ = "notification_cursors"

    destination: Mapped[str] = mapped_column(String(160), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger)


class NotificationDelivery(TimestampMixin, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (UniqueConstraint("event_sequence", "destination"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_sequence: Mapped[int] = mapped_column(
        ForeignKey("events.sequence", ondelete="CASCADE"), index=True
    )
    destination: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
