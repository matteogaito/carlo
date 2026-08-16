# CARLO v3 Foundation Design

## Goal

Build priorities 1–15 as one working vertical slice: create projects and tasks,
plan through Pi with repository access, approve a Plan, execute one task globally,
validate it locally, recover after restart, and show progress in a realtime UI.

CARLO is **slow but relentless**: correctness and recovery beat throughput.

## Approved scope

Temporary completion rule: **all declared local validations pass → Done**.

Branch, worktree, checkpoints, retries, stalled-loop detection, and escalation are
included. Strong final review, merge to `carlo-Dev`, integration pipelines, skill
management/evolution, and channel adapters are deferred. Their future states do
not block this slice.

## Architecture

- Backend: typed Python, FastAPI, SQLAlchemy 2 async, Alembic, psycopg 3.
- Database: local PostgreSQL database `carlov3`, configured by environment URL.
- Worker: explicit asyncio orchestration; no Celery, Redis, or workflow engine.
- Frontend: React, Vite, TypeScript; HTTP commands plus WebSocket events.
- Source state: Git branches, worktrees, and checkpoint commits.
- Agent boundary: provider-neutral `CodingAgentProvider`; initial `PiProvider`
  invokes the installed `pi` CLI with persisted session IDs and artifacts.

PostgreSQL is authoritative for lifecycle state. Git is authoritative for source
changes. Pi session memory is never required for recovery.

## Core records

- Project: name, unique key, repository, default/integration branches, validation
  commands, policies, provider profiles.
- Task: permanent ID, project, goal, priority, status/stage, approved Plan,
  branch/worktree/checkpoint, active profile, timestamps.
- Brief and Plan: immutable Markdown revisions; Plan metadata separately stores
  skills, validations, browser/build/run expectations, risks, and affected areas.
- Attempt, validation, escalation, and event: structured evidence plus bounded
  artifact/log references.

Visible statuses are Not Ready, Ready, In Progress, Test, Done, and Failed. Test
is reserved for the deferred integration gate. Internal stages explain the real
workflow without adding Kanban columns.

## Workflow

1. A task starts Not Ready.
2. Pi inspects the repository and creates an evidence-based Brief and concrete
   Plan. Multiple tasks may plan concurrently.
3. User approval pins a Plan revision and moves the task to Ready.
4. One worker acquires a PostgreSQL advisory lock and selects the next Ready task
   by priority, creation time, then ID.
5. CARLO creates `<TASK-ID>-<short-description>` from `carlo-Dev` plus a dedicated
   worktree, then Pi implements inside it.
6. CARLO runs only declared validation commands and checkpoints improvements.
7. Progress permits further attempts. Repeated errors/strategies or oscillation
   trigger the strong escalation profile with summarized evidence.
8. All required local checks pass → Done. An unrecoverable blocker → Failed.

A major change to scope, architecture, dependencies, public APIs, data model,
migrations, production behavior, or deployment pauses execution. Pi proposes a
Plan Amendment; only the user can approve it.

## Recovery and realtime

Intent is persisted before external side effects. On startup, the worker
reconciles the active database stage with Git, checkpoint, Pi session, and latest
validation, then resumes the next idempotent action.

Events are stored before WebSocket publication and carry sequence numbers. After
a disconnect, the frontend fetches missed events over HTTP. Raw logs remain a
secondary diagnostic view.

## UI

The main page is the approved six-column Kanban. Cards show ID, title, internal
stage, and a short evidence/progress summary. Task detail shows Goal, Brief, Plan,
approval, agent/profile, Git state, validations, attempts, escalation, events,
and raw logs. Narrow screens use horizontal board scrolling.

## Verification

Tests must prove domain transitions, concurrent planning, global execution
serialization, restart recovery, worktree/checkpoint behavior, stalled-loop
detection, escalation handoff, and Done-on-local-success. Temporary Git repos and
real PostgreSQL cover integration boundaries; a fake provider keeps normal tests
deterministic. An opt-in Pi smoke test verifies the CLI contract. Frontend
type-check, tests, and production build must pass.
