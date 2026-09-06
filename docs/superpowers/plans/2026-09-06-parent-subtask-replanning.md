# Parent Subtask Replanning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an aggregate parent Task ask the Plan agent for a safer child partition and atomically replace its active children after approval without deleting history.

**Architecture:** Active children have `superseded_at IS NULL`; approved replans timestamp the old set and create fresh children in one transaction. The existing planning and approval flows are reused, while the API derives eligibility and the scheduler ignores superseded children.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy, PostgreSQL/Alembic, pytest, React 19, TypeScript, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-06-parent-subtask-replanning-design.md`

## Global Constraints

- Never delete or rewrite old child Tasks, attempts, events, artifacts, branches, worktrees, or plans.
- Replanning requires a top-level parent with active children and rejects any active child in `IN_PROGRESS`, `TEST`, or `DONE`.
- Starting or failing planning cannot supersede children; replacement happens only during approval.
- Superseded children never participate in scheduling, Resume, predecessor lookup, aggregation, or active counts.
- Add no dependency and preserve ordinary planning, approval, rework, and child execution behavior.

---

### Task 1: Persist active and superseded child sets

**Files:**
- Modify: `backend/carlo/models.py:Task`
- Create: `backend/alembic/versions/d6e7f8a9b0c1_superseded_subtasks.py`
- Modify: `backend/tests/test_security_models.py`
- Modify: `backend/tests/test_planning_profile_migration.py`

**Interfaces:**
- Produces: `Task.superseded_at: datetime | None`
- Produces: partial unique index `uq_tasks_active_parent_position`
- Consumes: existing `parent_task_id` and `subtask_position`

- [ ] **Step 1: Write the failing persistence test**

Add a test that creates two children at position zero under one parent, with the
older child carrying `superseded_at=datetime.now(UTC)`. Flush both and assert
that both rows persist. Then add a second active position-zero child and assert
that PostgreSQL raises `IntegrityError`.

- [ ] **Step 2: Verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/test_security_models.py::test_only_one_active_child_can_hold_a_parent_position -q`

Expected: FAIL because `Task` has no `superseded_at` and the existing ordinary
unique constraint rejects the historical row.

- [ ] **Step 3: Add the model field and partial index**

Replace the existing two-column `UniqueConstraint` with:

```python
Index(
    "uq_tasks_active_parent_position",
    "parent_task_id",
    "subtask_position",
    unique=True,
    postgresql_where=text("superseded_at IS NULL"),
)
```

Add:

```python
superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 4: Add the Alembic migration**

Create revision `d6e7f8a9b0c1` after `c5d6e7f8a9b0`. Its upgrade adds the
nullable column, drops `uq_tasks_parent_position`, and creates the partial
unique index. Downgrade drops the index and column and restores
`uq_tasks_parent_position`; before restoration it must raise a clear database
error if duplicate historical positions exist rather than deleting rows.

- [ ] **Step 5: Extend the migration regression**

Update `test_planning_profile_migration.py` or add a focused migration test that
invokes the new migration through `MigrationContext`, asserts all existing Task
IDs remain, supersedes one child, and inserts a replacement at the same
position.

- [ ] **Step 6: Verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/test_security_models.py tests/test_planning_profile_migration.py -q`

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add backend/carlo/models.py backend/alembic/versions/d6e7f8a9b0c1_superseded_subtasks.py backend/tests/test_security_models.py backend/tests/test_planning_profile_migration.py
git commit -m "feat: retain superseded subtasks"
```

### Task 2: Start and recover parent replanning

**Files:**
- Modify: `backend/carlo/domain.py`
- Modify: `backend/carlo/api.py:recover_interrupted_reworks`, `continue_planning`, task routes, `_task_view`
- Modify: `backend/tests/test_api.py`

**Interfaces:**
- Produces: `POST /api/tasks/{task_id}/replan -> Task`
- Produces: task payload field `replan_allowed: bool`
- Produces: plan metadata field `replan: true`
- Consumes: active children where `Task.superseded_at.is_(None)`

- [ ] **Step 1: Write endpoint eligibility tests**

Create a parent with queued children and assert its detail payload has
`replan_allowed == true`. Assert `POST /replan` rejects with `409` for a leaf,
a nested child, and a parent whose active child is independently set to each of
`IN_PROGRESS`, `TEST`, and `DONE`.

- [ ] **Step 2: Write the planning-input regression**

Use the existing fake planning provider. Give one child attempts, a failed
validation, and an `execution.context_limit` event. Call `/replan` and assert
the provider instruction contains the child ID/title/goal, attempt and failure
counts, context-limit evidence, and these constraints:

```text
Each replacement task must be an independently verifiable outcome that fits one agent context.
Do not create validation-only tasks. Keep tightly coupled work together.
```

Assert the returned plan is awaiting approval, contains
`metadata.replan == true`, and leaves every child unchanged.

- [ ] **Step 3: Verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/test_api.py -k replan -q`

Expected: FAIL because the route and payload field do not exist.

- [ ] **Step 4: Add domain transitions and derived eligibility**

Add `replan` transitions from aggregate states
`IN_PROGRESS/IMPLEMENTING`, `IN_PROGRESS/BLOCKED`, `FAILED/BLOCKED`, and
`READY/QUEUED` to `NOT_READY/BRIEFING`. Implement one private eligibility
function that locks or reads active children as requested and returns false for
the prohibited child statuses.

- [ ] **Step 5: Add the route using the existing planner**

The route locks the parent and active children, saves prior status/stage in
`task.replan.started`, applies `replan` then `briefed`, assigns
`{task.id}-replan-{cycle}`, and calls `continue_planning` with a compact summary
of child evidence. Do not include raw event payloads or tool output.

In `continue_planning`, set `metadata["replan"] = True` when the current session
ID contains `-replan-`. This also survives planner questions handled by the
existing `/plan/answer` route.

- [ ] **Step 6: Add failure and restart recovery tests**

Make the provider fail and assert the endpoint restores the exact prior parent
status/stage, records `task.replan.failed`, and changes no child. Add an
interrupted replan Task with a `task.replan.started` event, call the startup
recovery function, and assert it restores the saved state and records
`task.replan.recovered_after_restart`.

- [ ] **Step 7: Implement shared recovery**

Extend `recover_interrupted_reworks` to recognize `%-replan-%` sessions. Read
the latest `task.replan.started` payload for each task and restore the stored
enum values. Keep existing rework recovery unchanged.

- [ ] **Step 8: Verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/test_api.py -k 'replan or interrupted_rework' -q`

Expected: all selected tests pass.

- [ ] **Step 9: Commit**

```bash
git add backend/carlo/domain.py backend/carlo/api.py backend/tests/test_api.py
git commit -m "feat: plan replacement subtasks"
```

### Task 3: Replace children atomically and isolate scheduling

**Files:**
- Modify: `backend/carlo/api.py:approve_plan`, `resume_task`, `_task_view`
- Modify: `backend/carlo/orchestrator.py:Orchestrator._eligible`, `_sync_parent`, `ImplementationPipeline.run`
- Modify: `backend/tests/test_api.py`
- Modify: `backend/tests/test_executor.py`
- Modify: `backend/tests/test_orchestration.py`

**Interfaces:**
- Consumes: latest approved plan with `metadata.replan is True`
- Produces: `subtasks.replanned` event with `old_children` and `new_children`
- Produces: task payload field `superseded_at: str | None`

- [ ] **Step 1: Write the approval replacement test**

Start with a parent and two active children carrying attempts/events. Store a
latest unapproved replan revision with three implementation tasks. Approve it
and assert in one committed result:

```python
assert all(child.superseded_at is not None for child in old_children)
assert [child.subtask_position for child in new_children] == [0, 1, 2]
assert all(child.id not in old_ids for child in new_children)
assert old_attempt.task_id == old_children[0].id
assert parent.status == TaskStatus.IN_PROGRESS
assert parent.stage == TaskStage.IMPLEMENTING
```

Assert `subtasks.replanned` contains both ID lists and a repeated stale approval
returns `409` without creating more children.

- [ ] **Step 2: Verify replacement RED**

Run: `cd backend && .venv/bin/python -m pytest tests/test_api.py -k approve_replan -q`

Expected: FAIL because approval currently reuses any existing children.

- [ ] **Step 3: Reuse child materialization in approval**

Keep child construction inside `approve_plan` and use the existing field
assignments for both branches. For a replan revision, require the parent to be
`NOT_READY/AWAITING_APPROVAL`, repeat the prohibited child-status check,
timestamp active children with one `now`, and create fresh children before
commit. Ordinary approval keeps its idempotent existing-child behavior.

- [ ] **Step 4: Write scheduler isolation tests**

Add one focused test for each behavior:

- a ready superseded child is never claimed;
- a current child is not claimed while its parent is planning or awaiting
  approval;
- earlier superseded siblings do not block the first replacement child;
- predecessor lookup selects only the preceding active sibling;
- completing a replacement child aggregates only active siblings;
- Resume selects only an unfinished active child;
- parent `subtask_count` counts only active children.

- [ ] **Step 5: Verify scheduler RED**

Run: `cd backend && .venv/bin/python -m pytest tests/test_executor.py tests/test_orchestration.py tests/test_api.py -k 'superseded or replanning_parent or active_subtask_count' -q`

Expected: FAIL because existing queries include every historical child.

- [ ] **Step 6: Apply the active-child predicate everywhere**

Add `Task.superseded_at.is_(None)` to aggregate detection, sibling order,
predecessor lookup, parent synchronization, Resume, active count, and approval
queries. In `_eligible`, also load the parent and require
`IN_PROGRESS/IMPLEMENTING` before permitting a child claim.

- [ ] **Step 7: Verify GREEN and existing ordering**

Run: `cd backend && .venv/bin/python -m pytest tests/test_executor.py tests/test_orchestration.py tests/test_api.py -q`

Expected: all selected tests pass, including original ordered execution tests.

- [ ] **Step 8: Commit**

```bash
git add backend/carlo/api.py backend/carlo/orchestrator.py backend/tests/test_api.py backend/tests/test_executor.py backend/tests/test_orchestration.py
git commit -m "feat: activate replanned subtask sets"
```

### Task 4: Add the parent action to the board

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: `Task.replan_allowed`, `Task.superseded_at`, `Plan.metadata.replan`
- Produces: `Api.replanTask(id: string): Promise<Task>`

- [ ] **Step 1: Write failing UI tests**

Assert that a selected parent with `replan_allowed: true` renders
`Replan subtasks`, clicking calls `replanTask(parent.id)`, and a leaf or
ineligible parent does not render the action. Add a replan plan and assert the
approval label is `Approve replan → Replace subtasks`. Include a superseded
child in `listTasks` and assert its title is absent from both board levels.

- [ ] **Step 2: Verify RED**

Run: `cd frontend && npm test`

Expected: FAIL because the API method, fields, action, and filter are absent.

- [ ] **Step 3: Add API types and method**

Extend `Task` with:

```typescript
superseded_at?: string | null
replan_allowed?: boolean
```

Add `replanTask` to the `Api` interface, fake API, and HTTP implementation using
`POST /api/tasks/${id}/replan`.

- [ ] **Step 4: Add the action and active-board filter**

Pass a `replan` callback into `TaskDetail`. Render the action only from
`task.replan_allowed`. Select the approval text from
`task.plan?.metadata.replan`. Filter `visibleTasks` or `boardTasks` so any Task
with `superseded_at` is omitted at both board levels while direct detail fetch
continues to work.

- [ ] **Step 5: Verify GREEN and build**

Run: `cd frontend && npm test && npm run build`

Expected: all frontend tests pass and Vite builds successfully.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api.ts frontend/src/App.tsx frontend/src/App.test.tsx
git commit -m "feat: add parent subtask replan action"
```

### Task 5: Integrated verification and installation

**Files:**
- Verify all files above

**Interfaces:**
- Produces: an installed CARLO release with the new database revision and active services

- [ ] **Step 1: Run the full repository suite**

Run: `env UV_CACHE_DIR=/tmp/carlo-uv-cache-codex make test`

Expected: skill contracts, Python compilation, all backend tests, Alembic check,
frontend tests, TypeScript, and Vite build pass.

- [ ] **Step 2: Review the final diff**

Run: `git diff --check && git status --short && git diff main...HEAD --stat`

Expected: no whitespace errors and only planned files changed.

- [ ] **Step 3: Request code review**

Review `main...HEAD` against the design spec. Fix every Critical or Important
finding and rerun the affected tests.

- [ ] **Step 4: Integrate using the approved branch-finishing option**

Follow `superpowers:finishing-a-development-branch`; do not merge or push until
the user selects the integration option.

- [ ] **Step 5: Install only after integration**

Run: `sudo make install-mac`, then verify the new Alembic revision, API and
worker processes, and the `Replan subtasks` action on an eligible parent.
