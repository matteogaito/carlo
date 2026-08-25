# Atomic Plan Subtasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn ordered planner output into real, sequential child tasks and complete the parent only after every child succeeds, while preventing context-limit retries and rendering one board card per child.

**Architecture:** Add a self-reference and order to the existing `Task` model, materialize children transactionally when the parent plan is approved, and teach the existing orchestrator to claim only the first unfinished sibling. Each child keeps the normal task lifecycle and starts its Git branch from the previous child's validated checkpoint; parent state is aggregated after each child finishes.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async, PostgreSQL/Alembic, pytest, React 19, TypeScript, Vitest, Testing Library, CSS.

**Spec:** `docs/superpowers/specs/2026-08-25-atomic-plan-subtasks-design.md`

## Global Constraints

- A child is a normal persisted `Task`, not a virtual UI record or a second workflow model.
- Children execute strictly in `subtask_position` order; later siblings cannot bypass an unfinished sibling.
- Parent approval creates all children and child plans in one database transaction.
- The parent never runs implementation when children exist and reaches `DONE` only when every child is `DONE`.
- Existing tasks whose plans contain no `implementation_tasks` retain current behavior.
- Reuse existing status, attempt, event, validation, profile, and worktree machinery; add no dependency.
- Preserve the current clipped-paper board style and show the parent title at the child's lower-right corner.

---

### Task 1: Persist Parent/Child Task Relationships

**Files:**
- Create: `backend/alembic/versions/a3b4c5d6e7f8_atomic_plan_subtasks.py`
- Modify: `backend/carlo/models.py:373-417`
- Test: `backend/tests/test_security_models.py`

**Interfaces:**
- Produces: `Task.parent_task_id: str | None`, `Task.subtask_position: int | None`, `Task.parent`, and ordered `Task.children`.
- Database invariant: `UNIQUE(parent_task_id, subtask_position)` and `subtask_position >= 0` when non-null.

- [ ] **Step 1: Write the failing persistence test**

Add a test that creates one parent and two children, reloads the parent, and asserts literal child order and back-references:

```python
parent = Task(id="CAR-1", project=project, sequence=1, title="Parent", goal="Goal")
first = Task(id="CAR-2", project=project, sequence=2, title="API", goal="API", parent=parent, subtask_position=0)
second = Task(id="CAR-3", project=project, sequence=3, title="UI", goal="UI", parent=parent, subtask_position=1)
session.add_all([parent, second, first])
await session.commit()
await session.refresh(parent, ["children"])
assert [child.id for child in parent.children] == ["CAR-2", "CAR-3"]
assert first.parent_task_id == "CAR-1"
```

The production mutation caught is a missing/incorrect self-reference or ordering.

- [ ] **Step 2: Run the test and verify RED**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_security_models.py::test_task_children_are_persisted_in_plan_order -q`

Expected: FAIL because `Task` has no `parent` or `subtask_position`.

- [ ] **Step 3: Add the model fields and relationships**

Implement the two columns and relationships directly on `Task`:

```python
parent_task_id: Mapped[str | None] = mapped_column(
    ForeignKey("tasks.id", ondelete="CASCADE"), index=True
)
subtask_position: Mapped[int | None] = mapped_column(Integer)
parent: Mapped["Task | None"] = relationship(
    remote_side="Task.id", back_populates="children"
)
children: Mapped[list["Task"]] = relationship(
    back_populates="parent",
    order_by="Task.subtask_position",
    cascade="all, delete-orphan",
    passive_deletes=True,
)
```

Extend `__table_args__` with the position uniqueness and non-negative check.

- [ ] **Step 4: Add the Alembic migration**

Use `down_revision = "f2a3b4c5d6e7"`. Add nullable columns, the self-FK with `ON DELETE CASCADE`, an index on `parent_task_id`, the unique constraint, and the check constraint. Downgrade removes them in reverse order.

- [ ] **Step 5: Run model and migration checks**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_security_models.py -q
cd backend && .venv/bin/python -m alembic check
```

Expected: PASS and `No new upgrade operations detected`.

- [ ] **Step 6: Commit**

```bash
git add backend/carlo/models.py backend/alembic/versions/a3b4c5d6e7f8_atomic_plan_subtasks.py backend/tests/test_security_models.py
git commit -m "feat: persist ordered task children"
```

### Task 2: Materialize Approved Plan Items as Real Tasks

**Files:**
- Modify: `backend/carlo/tasks.py`
- Modify: `backend/carlo/api.py:1738-1780,2208-2260`
- Test: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: `Task.parent_task_id`, `Task.subtask_position` from Task 1 and planner `metadata.implementation_tasks`.
- Produces: `materialize_plan_tasks(session: AsyncSession, parent: Task, plan: PlanRevision) -> list[Task]`.
- Produces API fields: `parent_task_id`, `parent_title`, `subtask_position`, `subtask_count`.

- [ ] **Step 1: Write the failing approval test**

Create a parent at `NOT_READY/AWAITING_APPROVAL` with a two-item plan. POST approval twice using the refreshed version and assert:

```python
children = (
    await session.scalars(
        select(Task).where(Task.parent_task_id == parent.id).order_by(Task.subtask_position)
    )
).all()
assert [(child.title, child.goal, child.status, child.stage) for child in children] == [
    ("Add API", "Implement the API contract.", TaskStatus.READY, TaskStage.QUEUED),
    ("Add UI", "Render the API result.", TaskStatus.READY, TaskStage.QUEUED),
]
assert len({child.id for child in children}) == 2
assert all(child.approved_plan_revision == 1 for child in children)
```

Also assert the second approval returns `409` or returns without adding rows. This catches partial/duplicate materialization.

- [ ] **Step 2: Run the test and verify RED**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_api.py::test_approval_atomically_materializes_ordered_real_subtasks -q`

Expected: FAIL because approval creates no children.

- [ ] **Step 3: Refactor task allocation without changing the public create flow**

Extract from `create_task` a non-committing allocator:

```python
async def allocate_task(
    session: AsyncSession,
    project: Project,
    title: str,
    goal: str,
    *,
    priority: int = 0,
    created_source: str = "web",
    parent: Task | None = None,
    subtask_position: int | None = None,
) -> Task:
    ...
```

It locks/uses the already loaded project sequence, writes the prompt file, adds the task and event, but does not commit. Keep `create_task` as the existing commit/rollback wrapper so current callers do not change.

- [ ] **Step 4: Implement transactional child materialization**

For each validated implementation item, allocate a child and add a one-task `PlanRevision` whose metadata copies parent resources and validation settings but contains only that implementation item. Set the child plan approved timestamp and state to `READY/QUEUED`. If children already exist, return them unchanged. On any failure, roll back and remove prompt files created during this approval.

Do not create children when `implementation_tasks` is absent or empty; approve the parent normally.

- [ ] **Step 5: Expose relationships through `_task_view`**

Load the parent title and child count with SQLAlchemy queries and add literal payload fields:

```python
"parent_task_id": task.parent_task_id,
"parent_title": parent_title,
"subtask_position": task.subtask_position,
"subtask_count": subtask_count,
```

After materializing children, leave the parent `IN_PROGRESS/IMPLEMENTING` as an aggregate and emit `subtasks.created` with child IDs. Do not queue the parent.

- [ ] **Step 6: Run the API tests and verify GREEN**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/carlo/tasks.py backend/carlo/api.py backend/tests/test_api.py
git commit -m "feat: create tasks from approved plan items"
```

### Task 3: Enforce Sequential Claiming and Aggregate Parent State

**Files:**
- Modify: `backend/carlo/orchestrator.py:48-175`
- Modify: `backend/carlo/git.py:30-58`
- Test: `backend/tests/test_executor.py`
- Test: `backend/tests/test_git.py`

**Interfaces:**
- Consumes: ordered child tasks from Task 2.
- Produces: `GitWorkspace.prepare(..., base_ref: str | None = None) -> Worktree`.
- Produces: `Orchestrator._sync_parent(session: AsyncSession, child: Task) -> None`.

- [ ] **Step 1: Write failing executor tests for strict order and parent completion**

Persist a parent with two queued children. Run the orchestrator twice and assert the literal sequence `CAR-2`, then `CAR-3`. After the first run assert parent is not `DONE`; after the second assert parent is `DONE`, `stage == COMPLETE`, and its checkpoint equals the second child's checkpoint.

Add a second test where the first child returns `failed`; assert `run_next()` does not claim the second child and the parent is `FAILED/BLOCKED`. Add a blocked outcome assertion where both child and parent remain `IN_PROGRESS/BLOCKED`. The production mutations caught are a missing predecessor predicate, accidentally claiming the aggregate parent, and premature parent completion.

- [ ] **Step 2: Run both tests and verify RED**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_executor.py -k 'subtask_order or parent_completion or failed_subtask' -q`

Expected: FAIL because ready siblings are currently independently claimable and parents are not synchronized.

- [ ] **Step 3: Add the predecessor condition to `_claim_next`**

Use a correlated `NOT EXISTS` against an aliased sibling task:

```python
earlier = aliased(Task)
no_unfinished_predecessor = ~exists().where(
    earlier.parent_task_id == Task.parent_task_id,
    earlier.subtask_position < Task.subtask_position,
    earlier.status != TaskStatus.DONE,
)
```

Apply it only to children; standalone tasks remain eligible. Exclude aggregate
parents (tasks for which a child exists) from both the active-task recovery query
and the ready-task query, so the parent's `IN_PROGRESS` status never launches an
implementation session. If a child is failed or blocked, later siblings remain
unclaimable.

- [ ] **Step 4: Synchronize the parent in `_finish`**

After setting the child outcome, lock its parent and query all siblings. Set the parent to:

- `DONE/COMPLETE` and final checkpoint when every child is `DONE`;
- `FAILED/BLOCKED` when any child is `FAILED`;
- `IN_PROGRESS/BLOCKED` when any child is `IN_PROGRESS/BLOCKED`;
- `IN_PROGRESS/IMPLEMENTING` otherwise.

Increment the parent version and emit `subtasks.completed` or `subtasks.blocked` only on an actual aggregate transition.

- [ ] **Step 5: Write and fail the Git ancestry test**

Prepare/checkpoint a first worktree, then prepare a second with `base_ref=first_checkpoint`. Assert:

```python
assert git(second.path, "merge-base", "--is-ancestor", first_checkpoint, "HEAD") == ""
```

Run: `backend/.venv/bin/python -m pytest backend/tests/test_git.py::test_child_worktree_starts_from_previous_checkpoint -q`

Expected: FAIL because `prepare` always branches from the integration branch.

- [ ] **Step 6: Add `base_ref` and pass the predecessor checkpoint**

Set `start_ref = base_ref or self.integration_branch` when creating a new branch. Before preparing a child worktree, load its immediately preceding sibling; require a non-null validated checkpoint and pass it as `base_ref`. The first child uses the integration branch.

- [ ] **Step 7: Run executor, Git, and orchestration suites**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_executor.py backend/tests/test_git.py backend/tests/test_orchestration.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/carlo/orchestrator.py backend/carlo/git.py backend/tests/test_executor.py backend/tests/test_git.py
git commit -m "feat: execute plan subtasks sequentially"
```

### Task 4: Prevent Context-Limit Retry Loops

**Files:**
- Modify: `backend/carlo/pi_runtime.py:83-123`
- Modify: `backend/carlo/orchestrator.py:48-82`
- Modify: `backend/carlo/provider.py`
- Test: `backend/tests/test_pi_runtime.py`
- Test: `backend/tests/test_executor.py`
- Test: `backend/tests/test_provider.py`

**Interfaces:**
- Produces: `runtime_context_window(context_window: int) -> int`, using 90% of the provider ceiling while never dropping below `max_tokens + 1`.
- Produces: `ContextLimitError(ProviderError)` for provider messages containing the verified prompt-too-long/context-window failure.

- [ ] **Step 1: Write failing safety-headroom and non-retry tests**

Assert a discovered 65,536-token model writes `58_982` to `models.json` while `manifest.json` preserves `65_536`. Add an orchestrator runner that raises `ContextLimitError` and assert one invocation, one interruption/context event, and immediate failed child state rather than three identical retries.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_pi_runtime.py::test_runtime_context_window_keeps_provider_headroom backend/tests/test_executor.py::test_context_limit_failure_is_not_retried_unchanged -q
```

Expected: FAIL because the exact provider ceiling is advertised and all exceptions follow generic interruption recovery.

- [ ] **Step 3: Implement conservative runtime context and typed error classification**

Write the advertised model `contextWindow` as `int(real_window * 0.9)` after ensuring it remains greater than `maxTokens`. Keep the real value in the manifest and settings API. When parsing a provider error message, raise `ContextLimitError` only for `Prompt too long` plus `context window`; preserve other provider errors unchanged.

- [ ] **Step 4: Handle context failures once in `Orchestrator.run_next`**

Catch `ContextLimitError` before the generic exception branch, record `execution.context_limit` with the truncated safe error, finish the child as failed, and return its ID. Do not restart the same prompt.

- [ ] **Step 5: Run provider/runtime/executor tests and verify GREEN**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_provider.py backend/tests/test_pi_runtime.py backend/tests/test_executor.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/carlo/pi_runtime.py backend/carlo/provider.py backend/carlo/orchestrator.py backend/tests/test_pi_runtime.py backend/tests/test_provider.py backend/tests/test_executor.py
git commit -m "fix: stop tasks before provider context overflow"
```

### Task 5: Render Child Cards with Parent References

**Files:**
- Modify: `frontend/src/api.ts:52-90`
- Modify: `frontend/src/App.tsx:180-215,400-530`
- Modify: `frontend/src/styles.css:82-92`
- Test: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes API fields from Task 2.
- Produces `visibleTasks(tasks: Task[]): Task[]`, filtering parents whose `subtask_count > 0`.

- [ ] **Step 1: Write the failing board test**

Render one parent and two children in different statuses. Assert the parent card is absent, both child buttons exist, and each contains the parent title:

```typescript
expect(screen.queryByRole('button', { name: /CAR-1 Parent task/ })).toBeNull()
expect(screen.getByRole('button', { name: /CAR-2 Add API.*Parent task/ })).toBeTruthy()
expect(screen.getByRole('button', { name: /CAR-3 Add UI.*Parent task/ })).toBeTruthy()
expect(screen.getAllByText('Parent task')).toHaveLength(2)
```

Also retain a standalone task assertion. This catches rendering the aggregate parent or losing the relationship label.

- [ ] **Step 2: Run the test and verify RED**

Run: `cd frontend && npm test -- --run src/App.test.tsx`

Expected: FAIL because relationship fields and filtering do not exist.

- [ ] **Step 3: Extend the TypeScript contract and board filter**

Add nullable relationship fields to `Task`. Filter only parents with `subtask_count > 0`; never filter children or standalone tasks. Inside each child button render:

```tsx
{task.parent_title && <span className="task-parent">{task.parent_title}</span>}
```

Include `parent_title` in the button `aria-label`. In task detail, show `Part of {parent_title}` directly below the title when present.

- [ ] **Step 4: Add the restrained lower-right treatment**

Keep the card grid and clipped corner. Add:

```css
.task-parent {
  align-self: end;
  justify-self: end;
  max-width: 90%;
  overflow: hidden;
  color: #657169;
  font: 700 9px ui-monospace, monospace;
  letter-spacing: .04em;
  text-overflow: ellipsis;
  white-space: nowrap;
}
```

Do not add icons, colors, animation, or a second card style.

- [ ] **Step 5: Run frontend tests and build**

Run:

```bash
cd frontend && npm test -- --run src/App.test.tsx
cd frontend && npm run build
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css frontend/src/App.test.tsx
git commit -m "feat: show atomic subtasks on the board"
```

### Task 6: End-to-End Atomic Subtask Flow

**Files:**
- Modify: `backend/tests/test_end_to_end.py`
- Modify: `README.md`

**Interfaces:**
- Consumes all prior task contracts.
- Proves the user-visible workflow from planning approval through ordered child completion and parent aggregation.

- [ ] **Step 1: Write the failing end-to-end scenario**

Have the fake planner return two implementation tasks and make the provider record implementation session IDs. Approve the parent, run the worker twice, and assert:

```python
assert implementation_sessions == [
    "CAR-2-implementation-1",
    "CAR-3-implementation-1",
]
assert parent.status == TaskStatus.DONE
assert [child.status for child in parent.children] == [TaskStatus.DONE, TaskStatus.DONE]
```

Assert the second child branch contains the first child checkpoint as an ancestor.

- [ ] **Step 2: Run the scenario and verify RED if any integration gap remains**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_end_to_end.py::test_parent_completes_after_ordered_atomic_plan_subtasks -q`

Expected before final integration: FAIL at the first missing cross-component contract; after Tasks 1-5 it may already pass, which is acceptable only if the test was first run against the pre-feature commit during execution.

- [ ] **Step 3: Make only integration corrections exposed by the test**

Correct mismatched field names, relationship loading, transaction boundaries, or session IDs in the owning module. Do not add a coordinator class or a second subtask API.

- [ ] **Step 4: Document behavior**

Add a concise README paragraph: approved structured plans become ordered task cards; each completes before the next starts; the parent completes after all children; context-limit failures stop the child without unchanged retries.

- [ ] **Step 5: Run complete verification**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests -q
cd frontend && npm test -- --run
cd frontend && npm run build
git diff --check
```

Expected: all tests and build pass. PostgreSQL must be running with access to `carlov3_test`; if unavailable, report database-backed tests as unverified rather than claiming success.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_end_to_end.py README.md
git commit -m "test: verify atomic plan subtask flow"
```
