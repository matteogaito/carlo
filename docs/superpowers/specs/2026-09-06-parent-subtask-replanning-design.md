# Parent subtask replanning

## Problem

CARLO materializes a top-level plan into ordered child Tasks only once. After
approval, the parent cannot ask the Plan agent to reconsider those boundaries.
This leaves an oversized child in place even when attempts or context-limit
events prove that its scope does not fit a reliable agent session.

The parent Task needs an explicit replanning action that proposes a better set
of context-sized children, preserves the old execution history, and changes the
active children only after human approval.

## Chosen approach

Add a `Replan subtasks` action to aggregate parent Tasks. Replanning creates a
new parent plan revision through the existing Plan agent and approval flow. The
current children remain active and visible while planning is in progress. When
the new revision is approved, CARLO atomically supersedes the current children
and creates a replacement ordered set from the new
`metadata.implementation_tasks`.

Old children are retained with their attempts, events, artifacts, branches,
worktrees, and plans. They are excluded from scheduling, aggregation, active
subtask counts, Resume, predecessor lookup, and the execution board.

This is preferable to deleting children, which destroys evidence, or rewriting
children in place, which makes old attempts appear to belong to a different
goal.

## Eligibility and safety

Replanning is available only when:

- the selected Task is top-level and has active children;
- no active child is `IN_PROGRESS`, `TEST`, or `DONE`;
- the parent is not already briefing or planning.

An aggregate parent awaiting approval may be replanned again. Its pending plan
revision remains as unapproved history and the new revision becomes the only
approvable proposal; active children are still unchanged until approval.

Failed, blocked, not-ready, and queued children may be superseded. Rejecting a
parent with completed children avoids ambiguous Git ancestry: CARLO does not
yet have a safe way to transplant completed child checkpoints into a new task
graph. A parent that has completed children continues to use child-level rework.

The backend enforces these rules under row locks and returns `409` when the
state changes before the request arrives. The UI exposes backend-derived
`replan_allowed` rather than reproducing the eligibility rules.

While a parent is being replanned or awaiting approval, its current children
must not be claimed. The orchestrator considers a child eligible only while its
parent is `IN_PROGRESS/IMPLEMENTING` and the child has not been superseded.

## Data model

`Task` gains nullable `superseded_at`. A null value identifies the active task
graph. Existing rows migrate with null values.

The current uniqueness constraint on `(parent_task_id, subtask_position)` is
replaced with a PostgreSQL partial unique index covering rows where
`superseded_at IS NULL`. Superseded generations retain their original positions
while a new active generation can reuse positions starting at zero.

No separate generation table or parent generation counter is needed. The
approved parent plan revision and `subtasks.replanned` event identify each
replacement, while `superseded_at` provides the only predicate needed by the
scheduler.

Task API payloads add:

- `superseded_at`, primarily for filtering historical children;
- `replan_allowed`, true only for an eligible aggregate parent.

## Replanning flow

`POST /api/tasks/{task_id}/replan` locks the parent and its active children,
checks eligibility, moves the parent into the existing planning states, assigns
a fresh session ID such as `{task_id}-replan-{cycle}`, and records
`task.replan.started`. Its payload stores the parent's prior status and stage so
failure or process-restart recovery can restore them exactly.

The planning instruction contains:

- the original parent goal and current approved plan;
- each active child's ID, title, goal, status, attempt count, validation
  failures, and context-limit evidence;
- an instruction to replace the current partition with the smallest useful set
  of independently verifiable outcomes that each fit one agent context;
- an instruction to keep coupled work together and avoid validation-only or
  organizational tasks.

The existing `continue_planning` function handles Plan-agent execution,
questions, activity events, validation, and revision persistence. Plans created
from a replan session receive `metadata.replan = true` so approval can select
the replacement path without adding another persistent mode field.

The result remains `AWAITING_APPROVAL`. Until approval, no child is modified.
If planning fails, the parent returns to its previous aggregate state and the
old children retain their prior eligibility; `task.replan.failed` records the
error.

## Approval and replacement

Approval of a latest plan revision with `metadata.replan = true` performs one
transaction:

1. Lock the parent and active children and repeat the child-status safety check;
   the parent itself must now be `NOT_READY/AWAITING_APPROVAL`.
2. Set `superseded_at` on every active child.
3. Create the replacement children through the existing child materialization
   code, with fresh task IDs and positions starting at zero.
4. Restore the parent to `IN_PROGRESS/IMPLEMENTING`.
5. Record `subtasks.replanned` with the plan revision, old child IDs, and new
   child IDs.

If any step fails, the transaction rolls back and the old graph stays active.
Ordinary first approval and amendment approval retain their existing behavior.

## Orchestrator and API queries

Every query that reasons about child execution adds
`Task.superseded_at.is_(None)`:

- aggregate detection and child eligibility;
- earlier-sibling and predecessor checkpoint lookup;
- parent synchronization after a child finishes;
- Resume selection;
- parent `subtask_count` and replanning eligibility.

The task list continues returning historical children so direct task links and
event-driven refresh remain valid. The frontend removes rows with
`superseded_at` from board and goal/execution navigation. A superseded task can
still be fetched directly by ID for audit and troubleshooting.

## Interface

The parent detail action bar shows `Replan subtasks` when `replan_allowed` is
true. Activating it uses the normal busy/error behavior and changes the detail
view to live planning. The existing Brief, Plan, planning question, and approval
components are reused.

The approval label becomes `Approve replan → Replace subtasks` when the latest
plan has `metadata.replan = true`. This makes the replacement explicit at the
irreversible decision point. No additional modal or configuration UI is added.

## Failure and recovery invariants

- Starting a replan never modifies or supersedes children.
- Approval replaces all active children or none.
- A superseded child can never be claimed or affect parent aggregation.
- A parent cannot be replanned while child work is running or completed.
- Old attempts, events, artifacts, task IDs, positions, and plans remain intact.
- Startup recovery extends the existing rework recovery query to interrupted
  replan sessions. It restores the status and stage stored in
  `task.replan.started` and records `task.replan.recovered_after_restart`.
- Repeated approval cannot create a second replacement generation because the
  normal optimistic task version and latest-revision checks still apply.

## Validation

Backend tests must prove:

- the endpoint rejects leaf parents, nested children, active work, completed
  work, and stale state;
- the Plan agent receives current child scope and context-failure evidence;
- failed planning leaves children unchanged with their prior eligibility;
- approval atomically supersedes old children and creates fresh ordered tasks;
- old history remains queryable;
- scheduler claim order, predecessor lookup, Resume, and parent aggregation
  ignore superseded children;
- ordinary initial plan approval is unchanged;
- the migration preserves all existing Tasks and permits reused active
  positions.

Frontend tests must prove that the action appears only from
`replan_allowed`, invokes the new endpoint, changes the approval label for a
replan revision, and hides superseded children from both board levels. The full
backend suite, frontend suite, build, and Alembic check must pass.

## Deferred scope

Replanning after one or more children have completed is deferred until CARLO
can define and validate the Git checkpoint that should seed the replacement
graph. A dedicated historical-generation browser is also deferred; direct task
detail, events, and database history preserve the evidence required today.
