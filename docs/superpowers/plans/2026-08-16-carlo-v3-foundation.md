# CARLO v3 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver projects and goal tasks from repository-aware Pi planning through approval, globally serialized implementation, local validation, recovery, and a realtime Kanban.

**Architecture:** A typed FastAPI/SQLAlchemy modular monolith stores durable state in PostgreSQL and runs orchestration in an asyncio worker. Git owns source checkpoints, Pi sits behind a small provider protocol, and React consumes only HTTP/WebSocket contracts.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 async, Alembic, psycopg 3, PostgreSQL 17, pytest, React, Vite, TypeScript, Vitest.

## Global Constraints

- CARLO is slow but relentless: correctness, evidence, and recovery beat throughput.
- Planning may run concurrently; implementation is globally serialized.
- The approved temporary completion rule is: all declared local validations pass → Done.
- PostgreSQL and Git are authoritative; Pi session memory is not.
- No Celery, Redis, workflow engine, microservices, drag-and-drop, review gate, merge gate, or integration pipeline in this slice.
- Production code is written only after its focused test has failed for the expected reason.

---

## File map

- `backend/pyproject.toml`: Python package, runtime, and test dependencies.
- `backend/alembic.ini`, `backend/alembic/`: one initial PostgreSQL migration.
- `backend/carlo/config.py`: environment settings only.
- `backend/carlo/db.py`: async engine/session construction.
- `backend/carlo/domain.py`: statuses, stages, transition rules, progress verdicts.
- `backend/carlo/models.py`: SQLAlchemy persistence records.
- `backend/carlo/provider.py`: provider protocol and Pi subprocess adapter.
- `backend/carlo/git.py`: branch, worktree, checkpoint operations.
- `backend/carlo/orchestrator.py`: planning, global executor, validation, retry, escalation, recovery.
- `backend/carlo/api.py`: HTTP and WebSocket contracts.
- `backend/carlo/main.py`: FastAPI application lifecycle.
- `backend/carlo/worker.py`: dedicated orchestration worker entrypoint.
- `backend/tests/`: focused checks plus test-only fake provider.
- `frontend/src/api.ts`: typed HTTP/WebSocket boundary.
- `frontend/src/App.tsx`: Kanban and task detail.
- `frontend/src/styles.css`: responsive visual system.
- `frontend/src/App.test.tsx`: board/action behavior.
- `README.md`, `.env.example`, `Makefile`: local setup and shortest useful commands.

### Task 1: Persistent domain foundation

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/carlo/{__init__,config,db,domain,models}.py`
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/versions/0001_foundation.py`
- Test: `backend/tests/test_domain.py`
- Test: `backend/tests/test_persistence.py`

**Interfaces:**
- Produces: `TaskStatus`, `TaskStage`, `transition(status, stage, action)`, `Settings.from_env()`, `async_session()`.
- Produces tables: `projects`, `agent_profiles`, `tasks`, `plan_revisions`, `attempts`, `validation_runs`, `escalations`, `events`.

- [ ] **Step 1: Add the failing transition test**

```python
def test_approved_plan_makes_task_ready():
    assert transition(TaskStatus.NOT_READY, TaskStage.AWAITING_APPROVAL, "approve") == (
        TaskStatus.READY,
        TaskStage.QUEUED,
    )

def test_local_success_completes_task():
    assert transition(TaskStatus.IN_PROGRESS, TaskStage.VALIDATING, "validated") == (
        TaskStatus.DONE,
        TaskStage.COMPLETE,
    )
```

- [ ] **Step 2: Run RED**

Run: `cd backend && uv run pytest tests/test_domain.py -q`
Expected: collection fails because `carlo.domain` does not exist.

- [ ] **Step 3: Implement the minimum transition table**

```python
TRANSITIONS = {
    (TaskStatus.NOT_READY, TaskStage.AWAITING_APPROVAL, "approve"):
        (TaskStatus.READY, TaskStage.QUEUED),
    (TaskStatus.READY, TaskStage.QUEUED, "start"):
        (TaskStatus.IN_PROGRESS, TaskStage.PREPARING_GIT),
    (TaskStatus.IN_PROGRESS, TaskStage.VALIDATING, "validated"):
        (TaskStatus.DONE, TaskStage.COMPLETE),
}
```

Unknown transitions raise `InvalidTransition`; no endpoint writes statuses directly.

- [ ] **Step 4: Run GREEN**

Run: `cd backend && uv run pytest tests/test_domain.py -q`
Expected: PASS.

- [ ] **Step 5: Add a failing persistence round-trip**

Create a Project and Task through a real async PostgreSQL session, commit, reopen
the session, and assert the permanent ID `CAR-1`, status, and goal survive.

- [ ] **Step 6: Run RED, add models and migration, then run GREEN**

Run RED: `cd backend && uv run pytest tests/test_persistence.py -q`
Expected: FAIL because tables/models are absent.

Implement typed SQLAlchemy models with PostgreSQL enums/JSONB, foreign keys,
unique `(project_id, sequence)`, event sequence ordering, UTC timestamps, and an
initial Alembic migration matching metadata exactly.

`agent_profiles` stores provider, model, effort, permissions, default skills,
context policy, and active flag. Seed configurable `brief`, `plan`,
`implementation`, and `escalation` profiles in the migration; tasks/attempts
reference the exact profile used.

Run GREEN: `cd backend && uv run alembic upgrade head && uv run pytest tests/test_persistence.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend
git commit -m "feat: add persistent CARLO domain"
```

### Task 2: Project, task, planning, and approval API

**Files:**
- Create: `backend/carlo/provider.py`
- Create: `backend/carlo/api.py`
- Create: `backend/carlo/main.py`
- Test: `backend/tests/test_api.py`
- Test: `backend/tests/test_provider.py`

**Interfaces:**
- Produces: `CodingAgentProvider.start(profile, instruction, cwd, session_id) -> AgentRun`.
- Produces: `PiProvider` JSON-mode subprocess implementation; `tests/fakes.py`
  supplies deterministic `FakeProvider` only to tests.
- Produces HTTP: `POST/GET /api/projects`, `POST/GET /api/tasks`, `POST /api/tasks/{id}/plan`, `POST /api/tasks/{id}/approve`.

- [ ] **Step 1: Write failing API tests**

```python
async def test_task_stays_not_ready_until_plan_is_approved(client):
    project = await create_project(client)
    task = (await client.post("/api/tasks", json={
        "project_id": project["id"], "title": "Login", "goal": "Add login"
    })).json()
    assert task["status"] == "NOT_READY"
    await client.post(f'/api/tasks/{task["id"]}/plan')
    planned = (await client.get(f'/api/tasks/{task["id"]}')).json()
    assert planned["stage"] == "awaiting_approval"
    approved = (await client.post(f'/api/tasks/{task["id"]}/approve', json={
        "revision": planned["plan"]["revision"]
    })).json()
    assert approved["status"] == "READY"
```

- [ ] **Step 2: Run RED**

Run: `cd backend && uv run pytest tests/test_api.py -q`
Expected: FAIL because the application/routes do not exist.

- [ ] **Step 3: Implement CRUD and approval**

Use Pydantic request/response models in `api.py`, optimistic task version checks,
repository/key validation, immutable plan revisions, and one audit event per
accepted command. Return `409` for stale/invalid lifecycle actions.

- [ ] **Step 4: Define and test the provider boundary**

The failing provider test supplies a temporary executable that emits JSON lines;
assert `PiProvider` streams them, records exit status, uses `--mode json`, an
explicit `--session-id`, `--session-dir`, profile model/thinking/tool flags, and
the supplied working directory. Planning profiles allow only read tools.

- [ ] **Step 5: Implement repository-aware planning**

Build Brief and Plan instructions from task, project policy, repository path, and
required JSON output contract. Persist Brief Markdown, Plan Markdown, and metadata
without another model call. `FakeProvider` returns valid fixtures for API tests.

- [ ] **Step 6: Run GREEN**

Run: `cd backend && uv run pytest tests/test_api.py tests/test_provider.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend
git commit -m "feat: add Pi planning and approval"
```

### Task 3: Git lifecycle and globally serialized executor

**Files:**
- Create: `backend/carlo/git.py`
- Create: `backend/carlo/orchestrator.py`
- Create: `backend/carlo/worker.py`
- Test: `backend/tests/test_git.py`
- Test: `backend/tests/test_executor.py`

**Interfaces:**
- Produces: `GitWorkspace.prepare(task_id, title) -> Worktree`, `checkpoint(message) -> sha`, `restore(sha)`.
- Produces: `Orchestrator.plan(task_id)`, `run_next() -> task_id | None`, `recover()`.
- Uses one dedicated connection holding `pg_try_advisory_lock(1128352847)` for the full implementation run.

- [ ] **Step 1: Write the failing real-Git lifecycle test**

Initialize a temporary repository with `main` and `carlo-Dev`, call `prepare`, and
assert the branch is `CAR-1-login`, its base is `carlo-Dev`, and its worktree is
separate. Modify a file, checkpoint it, modify it again, restore, and assert the
checkpoint content returns.

- [ ] **Step 2: Run RED, implement `GitWorkspace`, run GREEN**

Run RED: `cd backend && uv run pytest tests/test_git.py -q`
Expected: FAIL because `carlo.git` is absent.

Use `asyncio.create_subprocess_exec("git", ...)`, never a shell string. Validate
repository/worktree paths and command exits. Run GREEN with the same command.

- [ ] **Step 3: Write the failing serialization test**

Create two Ready tasks and two orchestrators using separate DB connections.
Block the first fake implementation run, start both `run_next()` calls, and assert
only one provider implementation starts. Release it and assert the second starts
only after the first task reaches Done.

- [ ] **Step 4: Run RED, implement advisory-lock executor, run GREEN**

Run RED: `cd backend && uv run pytest tests/test_executor.py -q`
Expected: FAIL because no executor exists.

Inside the lock: select one Ready task deterministically, persist `start`, prepare
Git, invoke implementation, then validate. Release the lock on every exit path.
Planning never acquires this lock.

Run GREEN: `cd backend && uv run pytest tests/test_executor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat: serialize implementation with Git isolation"
```

### Task 4: Validation, progress-aware retry, escalation, and recovery

**Files:**
- Modify: `backend/carlo/domain.py`
- Modify: `backend/carlo/orchestrator.py`
- Test: `backend/tests/test_orchestration.py`

**Interfaces:**
- Produces: `assess_progress(previous, current) -> Progress | Stalled`.
- Produces: `fingerprint(exit_code, output) -> str`, stable after path/number normalization.
- Produces: `Orchestrator.recover()` and structured escalation evidence.

- [ ] **Step 1: Write failing progress/stall tests**

```python
def test_fewer_failures_is_progress():
    assert assess_progress(result(failures=7), result(failures=2)).is_progress

def test_repeated_fingerprint_and_diff_is_stalled():
    history = [attempt("abc", "diff1"), attempt("abc", "diff1")]
    assert detect_stall(history).reason == "repeated_outcome"

def test_two_state_oscillation_is_stalled():
    assert detect_stall([attempt("a", "1"), attempt("b", "2"),
                         attempt("a", "1"), attempt("b", "2")]).reason == "oscillation"
```

- [ ] **Step 2: Run RED, implement minimal pure functions, run GREEN**

Run: `cd backend && uv run pytest tests/test_orchestration.py -q`
Expected RED before implementation and PASS afterward.

- [ ] **Step 3: Add failing orchestration scenarios**

Tests prove: commands run without `shell=True`; outputs and classifications are
persisted; improvement creates a checkpoint and retries; stall invokes escalation
with goal/Plan/Brief/diff/checkpoint/results/history/skills; major deviation stores
an amendment and pauses; validation success moves directly to Done.

- [ ] **Step 4: Implement the loop and run GREEN**

Keep the loop in one orchestration method with small pure helpers. Cap captured
stdout/stderr while writing full output to task artifacts. Reject validation
commands not present in approved Plan/project configuration.

Run: `cd backend && uv run pytest tests/test_orchestration.py -q`
Expected: PASS.

- [ ] **Step 5: Add failing restart recovery test**

Persist an In Progress task at `validating` with a real worktree/checkpoint, create
a new Orchestrator instance, call `recover`, and assert it resumes validation
without rerunning implementation or requiring the old provider process.

- [ ] **Step 6: Implement reconciliation and run the backend suite**

Run: `cd backend && uv run pytest -q`
Expected: PASS, including recovery and serialization.

- [ ] **Step 7: Commit**

```bash
git add backend
git commit -m "feat: add relentless validation and recovery"
```

### Task 5: Realtime Kanban and task detail

**Files:**
- Create: `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/index.html`
- Create: `frontend/src/{main,api,App}.tsx`
- Create: `frontend/src/styles.css`
- Create: `frontend/src/App.test.tsx`
- Modify: `backend/carlo/api.py`
- Test: `backend/tests/test_events.py`

**Interfaces:**
- Produces HTTP `GET /api/tasks`, `GET /api/tasks/{id}`, `GET /api/events?after=`.
- Produces WebSocket `/api/ws?after=` with `{sequence, type, task_id, payload, created_at}`.
- Frontend `Api` exposes `listTasks`, `getTask`, `approvePlan`, `startPlanning`, and `events`.

- [ ] **Step 1: Write failing persisted-event/reconnect test**

Create events 1–3, connect with `after=1`, and assert events 2–3 arrive in order.
Disconnect, create event 4, fetch `GET /api/events?after=3`, and assert event 4.

- [ ] **Step 2: Run RED, implement event fan-out, run GREEN**

Run: `cd backend && uv run pytest tests/test_events.py -q`
Expected RED before routes/hub and PASS afterward. PostgreSQL remains the source;
an in-process asyncio condition only wakes connected clients.

- [ ] **Step 3: Write failing UI test**

```tsx
it('maps tasks to the six fixed columns and opens detail', async () => {
  render(<App api={fakeApi(tasks)} />)
  expect(await screen.findByText('Not Ready')).toBeVisible()
  expect(screen.getByText('CAR-1')).toBeVisible()
  await userEvent.click(screen.getByText('CAR-1'))
  expect(await screen.findByText('Validation')).toBeVisible()
})
```

- [ ] **Step 4: Run RED, build the approved UI, run GREEN**

Run RED: `cd frontend && npm test -- --run`
Expected: FAIL because `App` is absent.

Build the six fixed columns, compact cards, connection/executor indicator, and
task detail sections from the approved ASCII layout. Include minimal project/task
creation, Plan generation, and approval controls so the complete flow is usable.
Use semantic buttons, headings, focus styles, accessible labels, responsive
horizontal scrolling, and plain CSS; add no component or drag library.

Run GREEN: `cd frontend && npm test -- --run && npm run build`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend frontend
git commit -m "feat: add realtime CARLO Kanban"
```

### Task 6: End-to-end local operation

**Files:**
- Create: `.env.example`
- Create: `Makefile`
- Create: `README.md`
- Modify: `backend/tests/test_executor.py`

**Interfaces:**
- Produces: `make migrate`, `make api`, `make worker`, `make ui`, `make test`.

- [ ] **Step 1: Add the failing end-to-end test**

Using real PostgreSQL, a temporary Git repository, and `FakeProvider`: create a
project/task through HTTP, plan, approve, run the worker, validate a real command,
and assert the API reports Done with branch, worktree, checkpoint, attempts,
validation evidence, and ordered events.

- [ ] **Step 2: Run RED, add only missing wiring, run GREEN**

Run: `cd backend && uv run pytest tests/test_executor.py -k end_to_end -q`
Expected RED until application lifecycle and worker wiring are complete, then PASS.

- [ ] **Step 3: Document and automate local setup**

Document PostgreSQL `carlov3`, environment variables, migrations, API/worker/UI
commands, Pi profile/model configuration, test DB isolation, recovery behavior,
known deferred gates, and the Done-on-local-tests rule. The Makefile only wraps
the exact documented commands.

- [ ] **Step 4: Run full verification**

```bash
make test
cd backend && uv run alembic check
cd frontend && npm run build
```

Expected: backend and frontend tests pass, Alembic reports no new upgrade
operations, and Vite produces a clean production build.

- [ ] **Step 5: Pi smoke check**

Run the opt-in provider test with Pi offline and the harmless read-only prompt.
If credentials/model access are unavailable, record it as `UNVERIFIABLE`; do not
misreport it as passed.

- [ ] **Step 6: Commit**

```bash
git add .env.example Makefile README.md backend frontend
git commit -m "docs: make CARLO v3 runnable locally"
```
