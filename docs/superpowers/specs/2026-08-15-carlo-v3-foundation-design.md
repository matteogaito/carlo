# CARLO v3 Foundation Design

## Goal

Build the first coherent CARLO v3 vertical slice: projects, goal-oriented tasks,
repository-aware planning through Pi, explicit plan approval, one globally
serialized implementation, Git isolation and checkpoints, executable local
validation, progress-aware retries, stalled-loop escalation, persistent audit
state, and a simple realtime Kanban UI.

CARLO means Coding Agent Runtime Lifetime Orchestrator. Its operating principle
is **slow but relentless**: optimize for correctness, recovery, and eventual
completion rather than throughput.

## Delivery boundary

This slice builds the foundation spanning priorities 1–15 from the CARLO v3
brief. The user-approved temporary completion rule overrides activation of the
review, merge, and integration gates:

> A task becomes Done when its declared local validation commands pass.

Strong final review, merge into `carlo-Dev`, the Test-stage integration run, and
automatic integration-failure follow-ups remain represented as future lifecycle
capabilities but are not active completion gates in this slice. Skill editing,
skill revision management, and evolutionary skill proposals are later slices.

## Architecture

CARLO is a modular monolith with three runtime processes:

- a FastAPI HTTP/WebSocket application;
- an asyncio orchestration worker;
- a React/Vite/TypeScript browser client.

PostgreSQL is the canonical store for domain and orchestration state. Git is the
canonical store for source changes, branches, worktrees, and checkpoints. Pi is
an external coding-agent provider invoked across a subprocess boundary. The
frontend communicates only through stable HTTP and WebSocket contracts.

No workflow engine, task queue, Redis instance, microservice split, or embedded
LLM SDK is introduced. PostgreSQL and explicit Python orchestration are enough
for the approved single-host foundation.

The backend uses typed Python, FastAPI, SQLAlchemy 2 async, Alembic, psycopg 3,
and PostgreSQL. The frontend uses React, Vite, and TypeScript. Configuration,
including the database URL and provider model assignments, comes from environment
variables with documented local defaults.

## Domain model

### Project

A project records its name, unique short key, repository path, default branch,
integration branch (default `carlo-Dev`), validation commands, provider/profile
configuration, and policy text. Repository paths and Git readiness are validated
before work begins.

### Task

A task has a permanent Jira-like ID formed from the project key and a monotonic
project sequence. It stores a title, goal, priority, source, user-visible status,
internal stage, approved plan revision, branch/worktree resources, current
checkpoint, current provider/profile, and timestamps.

User-visible statuses are `NOT_READY`, `READY`, `IN_PROGRESS`, `TEST`, `DONE`,
and `FAILED`. The first slice actively uses `NOT_READY`, `READY`, `IN_PROGRESS`,
`DONE`, and `FAILED`; `TEST` is reserved for the later integration gate.

Internal stages are explicit and may include `briefing`, `planning`,
`awaiting_approval`, `preparing_git`, `implementing`, `validating`, `escalating`,
`blocked`, and `complete`. Domain transition functions, not API handlers or UI
components, enforce valid changes.

### Planning and execution evidence

Briefs and Plans are immutable revisions. A Plan contains Markdown plus separate
structured metadata: recommended skills, validation commands, browser/build/run/
deployment expectations, risk flags, and affected project areas. Approval pins
one Plan revision and moves the task to Ready.

Attempts record provider session identity, profile, instruction, outcome,
checkpoint, Git diff reference, error fingerprint, progress measurements, and
artifact paths. Validation runs record commands, exit status, summarized output,
classification, and timestamps. Events form an append-only audit stream used by
the UI and recovery logic.

## Provider boundary

`CodingAgentProvider` defines the capabilities CARLO needs: start, instruct,
stream events, inspect status, stop, reconstruct, and collect final output. It
accepts a provider-neutral agent profile containing model, effort, permissions,
tools, skills, repository access, and context policy.

`PiProvider` translates a profile into the installed `pi` CLI. It uses explicit
session IDs and a CARLO-owned session directory. Brief and Plan sessions receive
read-only repository tools. Implementation sessions receive the tools permitted
inside the task worktree. Essential state is copied into PostgreSQL and task
artifacts; Pi session memory is never authoritative.

The initial configurable profiles are `brief`, `plan`, `implementation`, and
`escalation`. Review and skill-evolution profiles are added when their workflows
become active.

## Planning workflow

Planning may run concurrently for multiple tasks because it does not mutate
project source. Pi first builds an evidence-oriented Brief by inspecting the
repository, architecture, existing patterns, tests, policies, dependencies,
risks, assumptions, and concrete paths/symbols. It then creates a Plan optimized
for a potentially weaker implementation model.

The Plan resolves high-impact decisions, intervention points, invariants,
implementation order, validation commands, error/logging expectations, and
stopping conditions without prescribing brittle line-by-line code. The planner
may revisit the repository while refining either artifact.

The task remains Not Ready until the user approves a Plan revision. Approval is
an explicit API action and audit event.

## Globally sequential execution

The worker holds a dedicated PostgreSQL advisory lock while selecting or running
implementation work. This native database lock guarantees that only one CARLO
worker can implement at once, while planning endpoints and planner processes
remain concurrent.

The next Ready task is selected deterministically by priority, creation time, and
ID. Before invoking Pi, CARLO creates a task branch from the project's current
integration branch and a dedicated worktree. The branch name is
`<TASK-ID>-<short-kebab-description>`; the task ID remains the database identity.

CARLO creates Git checkpoint commits after known-good validation improvements.
Each checkpoint is linked to its validation evidence. A worsening experiment can
be rolled back to the last known-good checkpoint before a new strategy begins.

## Retry, progress, and escalation

Retries are progress-driven rather than count-driven. CARLO compares validation
results, error fingerprints, Git diffs, and attempt summaries. Fewer failures,
later build progress, a newly isolated root cause, or a newly passing validation
step count as measurable progress.

Repeated fingerprints with materially identical diffs, repeated strategies with
the same outcome, two-state oscillation, or repeated rollback/reapply cycles
count as stalled behavior. A configurable absolute ceiling remains a safety net,
not the normal stopping rule.

When stalled, the escalation profile receives the goal, approved Plan, Brief,
current diff and checkpoint, validation evidence, fingerprints, concise attempt
history, blocker, and selected skills. It returns a structured diagnosis and next
strategy for the implementation agent. If progress remains impossible, the task
becomes Failed and requires user action.

If implementation discovers a major change to scope, architecture, dependencies,
public APIs, data model, migrations, production behavior, deployment, assumptions,
or significant unplanned areas, execution pauses. The planning profile produces
an immutable Plan Amendment and the task remains blocked until the user approves,
edits, or rejects it. Minor local adaptations require no amendment.

## Validation and completion

Validation runs only commands declared in approved Plan metadata or project
configuration. Each result is classified as `VERIFIED`, `PARTIALLY_VERIFIED`, or
`UNVERIFIABLE`; CARLO never records an unperformed check as successful.

For this slice, all required local commands passing moves the task directly from
In Progress to Done and releases the global executor. A failed command feeds the
progress/retry loop. Missing infrastructure, credentials, hardware, or services
is recorded as evidence and may require user intervention rather than being
silently ignored.

## Recovery and failure handling

Important intent is persisted before external side effects. On startup, the
worker acquires the advisory lock and reconciles any active task against its
database stage, worktree, branch, checkpoint, provider session, and latest
validation. It then resumes the next idempotent action instead of assuming the Pi
process survived.

Process exits, malformed provider output, Git conflicts, invalid repository
state, database failures, and WebSocket disconnects produce structured audit
events. Raw output is stored as bounded artifacts; the main UI shows summaries
and links to detail.

## API and realtime boundary

HTTP endpoints cover project and task CRUD, planning, Plan approval, execution
actions, validation evidence, attempts, and event history. Commands validate the
current task version/stage to reject stale or duplicate actions.

The WebSocket emits typed task, provider, validation, and planning events. Event
sequence numbers let a reconnecting client fetch missed events over HTTP before
continuing live updates. WebSocket delivery is a view optimization, not a source
of truth.

## User interface

The main view is a six-column Kanban: Not Ready, Ready, In Progress, Test, Done,
and Failed. Cards show task ID, title, internal stage, and the smallest useful
progress/evidence summary. Test may remain empty until integration gates are
activated.

Task detail shows goal, Brief, Plan revisions, approval, internal stage,
implementation progress, current provider/profile, Git resources, checkpoint,
validation, attempts, escalation, audit events, and raw logs in a secondary view.
The primary view explains what CARLO is doing and why instead of resembling a
terminal. Narrow layouts use horizontal Kanban scrolling and a single-column
detail view.

## Testing strategy

Backend tests use real domain objects and PostgreSQL integration where database
locking or persistence matters. Temporary Git repositories exercise branch,
worktree, checkpoint, and recovery behavior. A fake provider implements the same
boundary for deterministic orchestration tests; a small opt-in Pi smoke test
checks the installed CLI contract without spending model calls in the normal
suite.

Required scenarios include task transitions, concurrent planning, global
execution serialization, restart recovery, worktree lifecycle, checkpoint
rollback, stalled-loop detection, escalation handoff, validation evidence, and
Done-on-local-success. Frontend tests cover board mapping and user actions;
TypeScript type-check and production build are mandatory.

## Explicitly deferred

- mandatory strong final review;
- automatic merge into `carlo-Dev`;
- integration pipeline execution and merge revert;
- automatic integration-failure follow-up tasks;
- skill editing, revision pinning, and evolutionary proposals;
- future channel adapters;
- drag-and-drop Kanban and other nonessential UI behavior.

These features should be added only when their lifecycle gates become active.
