# Atomic plan subtasks

## Problem

CARLO planning already returns ordered `metadata.implementation_tasks`, but the
implementation pipeline serializes all of them into one prompt and runs one
agent session for the parent task. Production recorded two provider failures at
68,421 and 65,682 tokens against a 65,536-token context window. The existing
compaction settings did not prevent those requests.

CARLO must turn planned implementation tasks into independently managed tasks,
execute them one at a time in plan order, and complete the parent only after
every child completes.

## Data model

`Task` gains two nullable fields:

- `parent_task_id`, a self-referencing foreign key to `tasks.id`;
- `subtask_position`, the child's zero-based position in the approved plan.

The database enforces uniqueness of `(parent_task_id, subtask_position)`.
Top-level tasks keep both fields null. A child is a normal `Task`: it owns its
status, stage, attempts, events, validation runs, provider sessions, branch,
worktree, checkpoint, and rework history.

The parent remains the owner of the approved plan and acts only as an aggregate
after children are created. It does not run an implementation agent itself.

## Materializing an approved plan

Approving a top-level task with `implementation_tasks` atomically creates one
child `Task` per ordered plan item. Each child receives:

- a normal project task ID allocated through the existing project sequence;
- the implementation task title;
- the self-contained implementation prompt as its goal;
- the parent's priority, selected model, and approved planning resources;
- an automatically approved one-task plan derived from the parent revision;
- `parent_task_id` and `subtask_position`.

The operation is idempotent: retrying approval cannot create duplicate children.
All children are visible immediately, but only the earliest unfinished child is
eligible for execution. A plan without implementation tasks retains the current
single-task behavior for backward compatibility.

## Ordered atomic execution

The orchestrator may claim a child only when every earlier sibling is `DONE`.
Later siblings remain queued and cannot bypass a failed, blocked, or unfinished
sibling. CARLO's existing global implementation lock continues to ensure that
only one implementation task runs at a time system-wide.

Each child uses a distinct Pi session and completes its full existing lifecycle:
claim, isolated worktree preparation, implementation attempts, validation,
checkpoint, and terminal state. Child N starts from child N-1's validated
checkpoint, so its branch contains all earlier completed work while preserving
separate task evidence and recovery.

When a child reaches a terminal state, CARLO updates the parent in the same
database transaction:

- all children `DONE`: parent becomes `DONE` and records the final checkpoint;
- any child failed or blocked: parent becomes blocked and later children remain
  ineligible;
- otherwise: parent remains in progress.

Reworking a failed child uses the existing task rework flow. Once it completes,
the next sibling becomes eligible automatically. Replanning the parent is not
allowed after child execution has started because it would invalidate ordered
branch ancestry.

## Context safety

Task decomposition is the primary context boundary: every child starts a fresh
Pi session containing only its own prompt and the minimal parent-plan constraints
needed to implement it.

CARLO also advertises a conservative context window to Pi rather than the
provider's exact hard ceiling. This safety headroom makes automatic compaction
run before tool schemas and the next request push the provider over its limit.
The provider's real discovered limit remains visible in settings. A prompt-too-
long provider error is recorded as a context failure and blocks that child; it
is not retried unchanged.

## API

Task payloads add:

- `parent_task_id`;
- `parent_title`;
- `subtask_position`;
- `subtask_count` for parents.

Task list and detail endpoints continue returning ordinary task payloads. The
API derives aggregate parent state from persisted children and exposes their
relationship without introducing a separate subtask API.

## Board interface

When a parent has children, the board omits the parent card and renders one card
per child in its current status column. Each child card keeps the existing task
identity, title, stage, running rail, and checkpoint. A quiet utility label at
the bottom right shows the parent title. It is part of the card's accessible
label and remains readable at narrow widths.

Opening a child shows its normal task detail plus a link-style reference to the
parent. Parents without children render exactly as they do today.

The visual treatment reuses CARLO's clipped paper cards, typography, and palette;
the parent reference is structural information rather than a new decorative
card style.

## Failure and recovery invariants

- Approval creates either all children and their plans or none.
- A child can never run before an earlier sibling is `DONE`.
- A failed child cannot be skipped implicitly.
- Restart recovery resumes the active child before considering another task.
- Parent completion requires every persisted child to be `DONE`.
- Existing top-level tasks and plans remain executable without migration-time
  child generation.

## Validation

Backend tests will prove atomic child creation, approval idempotency, strict
claim order, blocked-sibling behavior, checkpoint ancestry, parent aggregation,
restart recovery, and unchanged legacy execution. Provider tests will prove the
context safety headroom and non-retry behavior for prompt-too-long errors.

Frontend tests will prove that parent cards are hidden when children exist,
every child is rendered in its own status column, the parent title appears at
the lower right, and standalone tasks remain unchanged. The frontend build and
the relevant backend suites must pass; database-backed integration tests require
the local PostgreSQL test database.
