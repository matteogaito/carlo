# Project Actions and SSH Runners Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an administrator-only Actions workspace that executes committed `carlo-actions.yaml` commands sequentially on one clean Git commit, locally or through a trusted SSH runner, with durable history, live redacted console output, cancellation, and restart recovery.

**Architecture:** Keep action parsing and execution outside `api.py`: `actions.py` owns the repository contract and preflight, `action_runner.py` owns the serialized lifecycle and process execution, and `ssh.py` owns native OpenSSH argument construction and trust. PostgreSQL stores runner/run/step state and existing `Event` rows drive realtime UI and Telegram; full console text stays in redacted artifact files. The existing worker runs the action loop beside, not inside, the implementation-task lock.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 async, PostgreSQL advisory locks, Alembic, psycopg 3, PyYAML, asyncio subprocesses, native Git/OpenSSH, React 19, TypeScript, Vite, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-19-project-actions-and-ssh-runners-design.md`

## Global Constraints

- Read action definitions only from committed root file `carlo-actions.yaml` using `git show HEAD:carlo-actions.yaml`.
- Require a clean target repository and record its exact full `HEAD` SHA; never commit, stash, reset, push, or synchronize pending source changes.
- Execute each YAML command as `shlex.split(command)` plus `asyncio.create_subprocess_exec`; never add an implicit shell.
- Execute commands sequentially and stop on the first non-zero exit code.
- Permit one action run globally through an action-specific PostgreSQL advisory lock; this lock remains independent of `IMPLEMENTATION_LOCK`.
- Never execute inside the project's ordinary checkout: use a detached local or SSH worktree at the recorded commit.
- Support only pre-existing filesystem SSH keys with strict host checking and explicit administrator fingerprint confirmation; no passwords, key upload, generation, or copying.
- Persist intent before external side effects and never automatically rerun a deployment after interruption or ambiguous remote state.
- Keep full durable console output in redacted artifacts, not PostgreSQL events; events carry byte offsets only.
- Snapshot dotenv content at enqueue, store no values in PostgreSQL, redact exact non-empty values before durable output, and remove transient secret files at terminal state.
- Keep all endpoints administrator-only now while retaining `project_id` and `requested_by_id` for later project membership authorization.
- Do not add first-class `act`, Ansible, rsync, schedules, matrices, action inputs, retries, or parallel action execution.

## File Structure

- Create `backend/carlo/actions.py`: YAML schema, command parsing, Git cleanliness/commit/catalog preflight, dotenv parsing, definition snapshots, and redaction.
- Create `backend/carlo/action_runner.py`: global action queue, local execution, console artifacts, cancellation, cleanup, and restart reconciliation.
- Create `backend/carlo/ssh.py`: SSH runner validation, host-key scan/confirmation, strict SSH/SCP argument vectors, and remote workspace operations.
- Modify `backend/carlo/models.py`: `Runner`, `ActionRun`, and `ActionStep` persistence models.
- Create `backend/alembic/versions/d1e2f3a4b5c6_project_actions.py`: matching schema and indexes, revising `c7d4f0a82e11`.
- Modify `backend/carlo/config.py`: dedicated known-hosts path and action timing settings.
- Modify `backend/carlo/api.py`: action catalog/run/console/cancel APIs and runner management APIs; retain existing authentication and event transport.
- Modify `backend/carlo/worker.py`: start the action loop beside implementation orchestration and Telegram.
- Modify `backend/carlo/telegram.py`: concise action lifecycle labels and run identity.
- Modify `backend/pyproject.toml`: add the single required YAML parser dependency.
- Create `backend/tests/test_actions.py`: catalog, Git gate, dotenv, redaction, and API tests.
- Create `backend/tests/test_action_runner.py`: local queue, steps, console, cancellation, cleanup, and recovery tests.
- Create `backend/tests/test_ssh.py`: key/trust/argument/remote lifecycle unit tests with a narrow command fake.
- Create `backend/tests/test_action_ssh_integration.py`: opt-in real SSH smoke test guarded by an environment variable.
- Modify existing backend test cleanup statements to include the new tables in foreign-key-safe order.
- Modify `frontend/src/api.ts`: action/runner contracts and HTTP methods.
- Create `frontend/src/ActionsView.tsx`: project action cards, confirmation dialog, run detail/console, history, and runner dialog.
- Modify `frontend/src/App.tsx`: Board/Actions navigation, action event refresh, and selected-run state.
- Modify `frontend/src/styles.css`: Actions workspace, accessible dialogs, console, responsive side panel, and reduced-motion rules.
- Modify `frontend/src/App.test.tsx`: navigation, grouping, preflight confirmation, console offsets, kill, and runner trust flows.
- Modify `.env.example`, `README.md`, and `Makefile`: configuration, YAML contract, runner setup, and opt-in smoke command.

---

### Task 1: Persist runners, action runs, and steps

**Files:**
- Modify: `backend/carlo/models.py`
- Create: `backend/alembic/versions/d1e2f3a4b5c6_project_actions.py`
- Modify: `backend/tests/test_security_models.py`
- Modify: every backend test fixture containing a literal `TRUNCATE` table list

**Interfaces:**
- Produces: ORM classes `Runner`, `ActionRun`, and `ActionStep` with the fields and status strings defined by the spec.
- Produces: `ActionRun.steps` ordered by `ActionStep.position` and project/requester relationships.
- Consumes: existing `Project`, `User`, `Event`, and `TimestampMixin` models.

- [ ] **Step 1: Write the failing persistence test**

Add a test that creates one project, admin, SSH runner, action run, and two steps, then reloads the run and verifies ordering and immutable snapshot data:

```python
run = ActionRun(
    project_id=project.id,
    requested_by_id=admin.id,
    action_key="deploy-dev",
    action_name="Deploy to Dev",
    definition={"commands": ["make test", "make deploy"]},
    runner_name="linux-build",
    status="queued",
    commit_sha="a" * 40,
    branch_name="main",
    origin="ssh://git@example/repo.git",
    env_file=".env.dev",
    env_names=["DEPLOY_TOKEN"],
    artifact_path="/tmp/actions/1/console.log",
)
run.steps = [
    ActionStep(position=2, command="make deploy", status="pending"),
    ActionStep(position=1, command="make test", status="pending"),
]
session.add(run)
await session.commit()
loaded = await session.get(ActionRun, run.id)
assert [step.position for step in loaded.steps] == [1, 2]
assert loaded.definition["commands"] == ["make test", "make deploy"]
```

- [ ] **Step 2: Run the focused test and confirm the models are missing**

Run: `cd backend && uv run pytest tests/test_security_models.py -q`

Expected: FAIL while importing `Runner`, `ActionRun`, or `ActionStep`.

- [ ] **Step 3: Add the minimal ORM models**

Use strings for lifecycle status to avoid PostgreSQL enum migration friction, JSONB for immutable definition/env-name lists, and database constraints for stable identities:

```python
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
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    action_key: Mapped[str] = mapped_column(String(120))
    action_name: Mapped[str] = mapped_column(String(160))
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    runner_name: Mapped[str] = mapped_column(String(80))
    runner_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), index=True)
    internal_stage: Mapped[str] = mapped_column(String(30), default="queued")
    commit_sha: Mapped[str] = mapped_column(String(64))
    branch_name: Mapped[str | None] = mapped_column(String(240))
    origin: Mapped[str | None] = mapped_column(Text)
    env_file: Mapped[str | None] = mapped_column(Text)
    env_names: Mapped[list[str]] = mapped_column(JSONB, default=list)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_step: Mapped[int | None] = mapped_column(Integer)
    process_group: Mapped[int | None] = mapped_column(Integer)
    workspace_path: Mapped[str | None] = mapped_column(Text)
    artifact_path: Mapped[str] = mapped_column(Text)
    secret_path: Mapped[str | None] = mapped_column(Text)
    log_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    recent_output: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    cleanup_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    steps: Mapped[list["ActionStep"]] = relationship(
        order_by="ActionStep.position", cascade="all, delete-orphan", passive_deletes=True
    )

class ActionStep(TimestampMixin, Base):
    __tablename__ = "action_steps"
    __table_args__ = (UniqueConstraint("run_id", "position"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("action_runs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    command: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    log_start: Mapped[int | None] = mapped_column(BigInteger)
    log_end: Mapped[int | None] = mapped_column(BigInteger)
```

Add explicit check constraints for the run/step status sets and indexes on `(status, requested_at, id)` and `(project_id, requested_at)` in the migration.

- [ ] **Step 4: Create, inspect, and apply the Alembic migration**

Create `d1e2f3a4b5c6_project_actions.py` with `revision = "d1e2f3a4b5c6"` and `down_revision = "c7d4f0a82e11"`, matching the model columns and constraints above.

Inspect the generated file to ensure downgrade drops `action_steps`, then `action_runs`, then `runners`, and no unrelated current schema change appears.

Run: `cd backend && CARLO_DATABASE_URL=postgresql+psycopg:///carlo_test uv run alembic upgrade head`

Expected: migration succeeds.

- [ ] **Step 5: Update fixture cleanup lists and run persistence tests**

Prepend `action_steps, action_runs, runners,` to each literal test `TRUNCATE` list so existing tests remain isolated.

Run: `cd backend && uv run pytest tests/test_security_models.py tests/test_persistence.py -q`

Expected: PASS. If `test_persistence.py` still hardcodes `Event(sequence=1)`, remove the explicit sequence and assert the generated positive sequence instead; do not delete production data.

- [ ] **Step 6: Commit the persistence slice**

```bash
git add backend/carlo/models.py backend/alembic/versions backend/tests
git commit -m "feat: persist project action runs"
```

### Task 2: Parse committed action catalogs and enforce Git/env preflight

**Files:**
- Modify: `backend/pyproject.toml`
- Create: `backend/carlo/actions.py`
- Create: `backend/tests/test_actions.py`

**Interfaces:**
- Produces: `ActionDefinition`, `ActionCatalog`, `ActionPreflight`, `ActionConfigError`.
- Produces: `async load_catalog(project: Project) -> ActionCatalog` and `async preflight(project: Project, action_key: str, artifact_root: Path) -> ActionPreflight`.
- Produces: `parse_dotenv(text: str) -> dict[str, str]`, `minimal_environment(values: Mapping[str, str]) -> dict[str, str]`, and `redact(text: str, secrets: Collection[str]) -> str`.
- Consumes: `Project.repository_path`; the API and runner use immutable `ActionPreflight` fields.

- [ ] **Step 1: Add PyYAML and failing parser tests**

Run: `cd backend && uv add pyyaml`

Test a valid mapping, unsupported `version`, unknown action fields, empty commands, shell syntax preservation as text, and an action defaulting to runner `local`:

```python
catalog = parse_catalog("""
version: 1
actions:
  test:
    name: Run tests
    commands: ["python -m pytest -q"]
""")
assert catalog.actions["test"].runner == "local"
assert catalog.actions["test"].argv == (("python", "-m", "pytest", "-q"),)
```

Run: `cd backend && uv run pytest tests/test_actions.py -q`

Expected: FAIL because `carlo.actions` does not exist.

- [ ] **Step 2: Implement strict schema and command parsing**

Use `yaml.safe_load`, exact key-set checks, stable action-key validation (`[a-z0-9][a-z0-9-]{0,119}`), and `shlex.split`:

```python
@dataclass(frozen=True, slots=True)
class ActionDefinition:
    key: str
    name: str
    runner: str
    env_file: str | None
    commands: tuple[str, ...]
    argv: tuple[tuple[str, ...], ...]

@dataclass(frozen=True, slots=True)
class ActionCatalog:
    commit_sha: str
    branch: str | None
    dirty_paths: tuple[str, ...]
    actions: dict[str, ActionDefinition]
    error: str | None = None
```

Reject command strings whose `shlex.split` result is empty. Do not reject literal `&&` or `|`; they remain ordinary argv values, consistent with the no-shell contract.

- [ ] **Step 3: Write failing real-Git preflight tests**

Create temporary repositories and verify:

```python
catalog = await load_catalog(project)
assert catalog.commit_sha == git(repository, "rev-parse", "HEAD")
assert catalog.actions["test"].commands == ("python check.py",)

(repository / "tracked.py").write_text("changed")
with pytest.raises(ActionConfigError, match="tracked.py"):
    await preflight(project, "test", artifact_root)
```

Also verify non-ignored untracked files block, ignored `.env.dev` does not, `git show HEAD:carlo-actions.yaml` ignores a modified working-copy YAML, and a credential-bearing HTTP origin is sanitized/rejected for SSH use.

Run: `cd backend && uv run pytest tests/test_actions.py -q`

Expected: parser tests pass and Git preflight tests fail.

- [ ] **Step 4: Implement the Git gate and immutable enqueue snapshot**

Use a private async `git -C <repository> ...` helper with fixed argv. Parse `git status --porcelain=v1 --untracked-files=all`, `rev-parse HEAD`, `symbolic-ref --short -q HEAD`, `remote get-url origin`, and `show HEAD:carlo-actions.yaml`. `preflight` must return:

```python
@dataclass(frozen=True, slots=True)
class ActionPreflight:
    definition: ActionDefinition
    commit_sha: str
    branch: str | None
    origin: str | None
    env_path: Path | None
    env_values: dict[str, str]
```

Resolve `env_file` beneath `repository.resolve()`, reject absolute/traversing/symlink escapes, require a regular file, and create no artifact yet; enqueue code will snapshot only after it has a persisted run ID.

- [ ] **Step 5: Implement and test dotenv/minimal env/redaction**

Accept comments, blank lines, optional `export `, single/double quoted values, and variable names matching `[A-Za-z_][A-Za-z0-9_]*`. Reject malformed lines and NUL bytes. Build the base environment from only `PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_ALL`, and `TERM` when present, then overlay action values. Redact longer secrets first:

```python
def redact(text: str, secrets: Collection[str]) -> str:
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text
```

Assert database-facing snapshots contain `env_names` only, while `minimal_environment` excludes `CARLO_DATABASE_URL` and Telegram variables.

- [ ] **Step 6: Run the parser/preflight suite and commit**

Run: `cd backend && uv run pytest tests/test_actions.py -q`

Expected: PASS.

```bash
git add backend/pyproject.toml backend/uv.lock backend/carlo/actions.py backend/tests/test_actions.py
git commit -m "feat: validate committed project actions"
```

### Task 3: Add catalog, enqueue, history, console, and cancel APIs

**Files:**
- Modify: `backend/carlo/api.py`
- Modify: `backend/tests/test_actions.py`

**Interfaces:**
- Produces: `GET /api/projects/{project_id}/actions`.
- Produces: `POST /api/projects/{project_id}/actions/{action_key}/runs`.
- Produces: `GET /api/action-runs`, `GET /api/action-runs/{run_id}`, `GET /api/action-runs/{run_id}/console?offset=N`, and `POST /api/action-runs/{run_id}/cancel`.
- Consumes: Task 1 models and Task 2 preflight/catalog functions.

- [ ] **Step 1: Write failing authenticated API tests**

Test catalog errors return HTTP 200 as project-scoped data, dirty preflight returns 409 with paths, a valid enqueue returns 201, creates all pending steps, snapshots the definition/SHA/env names and non-secret runner connection evidence, and writes a mode-600 secret snapshot without putting values in JSON or DB fields.

```python
response = await client.post(f"/api/projects/{project.id}/actions/test/runs")
assert response.status_code == 201
body = response.json()
assert body["status"] == "queued"
assert body["commit_sha"] == head
assert [step["status"] for step in body["steps"]] == ["pending", "pending"]
assert secret not in json.dumps(body)
```

Also test console offset validation, missing artifact behavior, pagination limit `1..100`, project filter, queued cancellation, and idempotent repeated cancellation.

- [ ] **Step 2: Run focused API tests and confirm 404 failures**

Run: `cd backend && uv run pytest tests/test_actions.py -q`

Expected: FAIL because action endpoints are absent.

- [ ] **Step 3: Add Pydantic views and enqueue transaction**

Keep endpoint functions thin. Preflight first; then create `ActionRun` plus `ActionStep` rows and an `action.queued` event in one transaction. Snapshot runner name, host, port, username, workspace root, and trusted fingerprint into `runner_snapshot`, never private-key content. After flush supplies `run.id`, atomically write `artifact_root/actions/<id>/environment` with mode `0o600`, set `secret_path`, and only then commit. If file creation fails, rollback the DB transaction and remove the partial file.

The event payload must contain only `run_id`, `project_id`, `action_key`, `runner`, and `commit_sha`.

- [ ] **Step 4: Implement safe console reads and cancellation intent**

Resolve the stored artifact path and verify it remains beneath `<artifact_root>/actions`; return at most 256 KiB with:

```json
{"offset": 0, "next_offset": 128, "text": "...", "eof": false}
```

For queued cancel, lock the row, mark the run `cancelled`, mark pending steps `skipped`, set timestamps, remove the secret snapshot, and emit `action.cancelled`. For running cancel, set only `cancel_requested_at` and emit `action.cancel_requested`; the worker performs termination.

- [ ] **Step 5: Run API tests and commit**

Run: `cd backend && uv run pytest tests/test_actions.py tests/test_auth.py tests/test_events.py -q`

Expected: PASS.

```bash
git add backend/carlo/api.py backend/tests/test_actions.py
git commit -m "feat: expose project action APIs"
```

### Task 4: Execute local actions under a separate global lock

**Files:**
- Create: `backend/carlo/action_runner.py`
- Create: `backend/tests/test_action_runner.py`
- Modify: `backend/carlo/worker.py`

**Interfaces:**
- Produces: `ACTION_LOCK`, `ActionOrchestrator.run_next() -> int | None`, and `ActionExecutor.run(run_id: int) -> None`.
- Consumes: Task 1 persistence and Task 2 `minimal_environment`, `parse_dotenv`, and `redact`.
- Produces events: `action.started`, `action.output_available`, `action.succeeded`, `action.failed`, `action.cancelled`, and `action.interrupted`.

- [ ] **Step 1: Write failing queue and separate-lock tests**

Create two queued runs and two `ActionOrchestrator.run_next()` calls. Hold the first runner and assert the second cannot enter. In the same test acquire/run an `Orchestrator` implementation task and assert one implementation and one action can be active together.

```python
first = asyncio.create_task(actions.run_next())
assert await asyncio.wait_for(entered.get(), 1) == first_run.id
second = asyncio.create_task(actions.run_next())
with pytest.raises(TimeoutError):
    await asyncio.wait_for(entered.get(), 0.1)
assert ACTION_LOCK != IMPLEMENTATION_LOCK
```

- [ ] **Step 2: Implement the minimal action queue**

Follow the existing `Orchestrator` pattern but use `pg_advisory_lock(ACTION_LOCK)`. Claim an existing `running` run first for reconciliation; otherwise claim the oldest queued run ordered by `requested_at, id` with `FOR UPDATE SKIP LOCKED`. Persist `running`, `started_at`, `internal_stage="preparing"`, and `action.started` before calling the executor.

- [ ] **Step 3: Write failing local execution tests**

Use a temporary Git repository with committed scripts and action YAML. Verify the executor:

- creates `<worktree_root>/actions/<run-id>` detached at the recorded SHA;
- executes commands in order with combined stdout/stderr;
- passes only the minimal parsed environment;
- writes commands, output, exit codes, and final result to `console.log`;
- redacts a printed dotenv value;
- marks later steps skipped after exit 7;
- treats missing executables as exit 127;
- removes worktree and secret snapshot at terminal state.

- [ ] **Step 4: Implement local execution and durable output**

Create process groups with `start_new_session=True`. Before starting a command, persist current step/status/log start. Stream chunks from `process.stdout`, redact before appending to the durable console, maintain a bounded final 16 KiB `recent_output`, and no more than four times per second persist `log_offset` plus `action.output_available`.

Use Git fixed argv:

```python
await run_exec("git", "-C", repository, "worktree", "add", "--detach", worktree, commit_sha)
...
await run_exec("git", "-C", repository, "worktree", "remove", "--force", worktree)
```

Do not use the existing `GitWorkspace.prepare`, because it creates implementation branches and checkpoints rather than detached action worktrees.

- [ ] **Step 5: Wire the action loop into the worker**

Construct one `ActionExecutor` and `ActionOrchestrator`. Run an `action_loop()` task beside Telegram while the existing main loop continues implementation orchestration. Each idle loop sleeps two seconds; exceptions are logged and retried without terminating the other loop.

- [ ] **Step 6: Run local execution tests and commit**

Run: `cd backend && uv run pytest tests/test_action_runner.py tests/test_executor.py -q`

Expected: PASS.

```bash
git add backend/carlo/action_runner.py backend/carlo/worker.py backend/tests/test_action_runner.py
git commit -m "feat: execute local project actions"
```

### Task 5: Implement Kill run and local restart reconciliation

**Files:**
- Modify: `backend/carlo/action_runner.py`
- Modify: `backend/tests/test_action_runner.py`

**Interfaces:**
- Produces: `ActionExecutor.cancel(run: ActionRun) -> None` and `ActionExecutor.reconcile(run_id: int) -> None`.
- Consumes: persisted `process_group`, `cancel_requested_at`, state file, console offset, and run/step records.

- [ ] **Step 1: Write failing cancellation tests**

Run a script that traps TERM and stays alive. Request cancellation through the API, assert TERM reaches the process group, advance a configurable short test grace period, and assert KILL follows. Verify active step `cancelled`, later steps `skipped`, run `cancelled`, and a second request changes nothing.

- [ ] **Step 2: Implement TERM-to-KILL cancellation**

Use `os.killpg(process_group, signal.SIGTERM)`, `asyncio.wait_for(process.wait(), timeout=cancel_grace_seconds)`, then `os.killpg(..., SIGKILL)` on timeout. Persist cancellation intent before signals and keep final run status `cancelled` even if cleanup fails.

- [ ] **Step 3: Write failing restart tests**

Cover three cases using a CARLO-owned state file in the run directory:

1. live PID/process group: resume waiting/tailing without rerunning the command;
2. terminal state file: collect exit code and finish remaining lifecycle;
3. absent process and no terminal state: mark `interrupted`, never enqueue a replacement.

- [ ] **Step 4: Implement local wrapper state and reconciliation**

Launch commands through a small committed Python module entry point, `python -m carlo.action_runner child ...`, which writes `process-group` and atomically replaces `state` JSON after exit. The wrapper still invokes the declared argv directly; it is not a shell. `reconcile` checks `os.killpg(pgid, 0)`, state JSON, and saved log offset in that order. A terminal run with `cleanup_pending=true` retries only cleanup; it never reruns commands or changes the recorded result.

- [ ] **Step 5: Run cancellation/recovery tests and commit**

Run: `cd backend && uv run pytest tests/test_action_runner.py -q`

Expected: PASS.

```bash
git add backend/carlo/action_runner.py backend/tests/test_action_runner.py
git commit -m "feat: cancel and recover local actions"
```

### Task 6: Manage SSH runners with explicit host trust

**Files:**
- Modify: `backend/carlo/config.py`
- Modify: `.env.example`
- Create: `backend/carlo/ssh.py`
- Create: `backend/tests/test_ssh.py`
- Modify: `backend/carlo/api.py`

**Interfaces:**
- Produces: `SshTransport.scan_host`, `confirm_host`, `check`, `run`, and `copy_file` using native OpenSSH.
- Produces admin endpoints: `GET/POST /api/runners`, `PATCH /api/runners/{id}`, `POST /api/runners/scan`, `POST /api/runners/{id}/trust`, and `POST /api/runners/{id}/test`.
- Consumes: `Settings.ssh_known_hosts`, persisted `Runner`, and authenticated admin dependency.

- [ ] **Step 1: Add settings and failing key validation tests**

Add defaults:

```python
ssh_known_hosts: str = ".carlo/ssh/known_hosts"
ssh_connect_timeout: int = 10
action_cancel_grace_seconds: int = 10
```

Test absolute regular readable identity paths, reject group/other permission bits (`mode & 0o077`), invalid hostname/username/port/workspace strings, and symlinks.

- [ ] **Step 2: Write failing SSH argv and host-scan tests**

With a fake subprocess recorder, assert every SSH command contains:

```text
-o BatchMode=yes
-o StrictHostKeyChecking=yes
-o UserKnownHostsFile=<configured path>
-o ConnectTimeout=<configured seconds>
-i <identity file>
-p <port>
```

Assert `ssh-keyscan -T <seconds> -p <port> <host>` output is fingerprinted with `ssh-keygen -lf -`, but is not appended until explicit confirmation.

- [ ] **Step 3: Implement trust and transport with fixed argv**

Validate host/user/workspace before argv construction. Write the dedicated known-hosts file with mode `0o600`; append the exact scanned line only after the API receives the expected fingerprint. On rescan mismatch, return stored and proposed fingerprints and keep the runner disabled until explicit replacement.

For remote execution, pass a constant `sh -c` bootstrap script as one argument and all dynamic values as positional parameters after `--`; never interpolate host, paths, commit, action key, or run ID into shell source.

- [ ] **Step 4: Add runner API tests and endpoints**

Test create returns a pending scan result without enabling, trust requires exact fingerprint, successful test updates timestamps, disabled runner blocks action preflight, and runner JSON never returns key contents. Only the identity path is returned.

- [ ] **Step 5: Run SSH/API tests and commit**

Run: `cd backend && uv run pytest tests/test_ssh.py tests/test_actions.py tests/test_auth.py -q`

Expected: PASS.

```bash
git add .env.example backend/carlo/config.py backend/carlo/ssh.py backend/carlo/api.py backend/tests/test_ssh.py backend/tests/test_actions.py
git commit -m "feat: manage trusted SSH runners"
```

### Task 7: Execute and recover exact commits on SSH runners

**Files:**
- Modify: `backend/carlo/action_runner.py`
- Modify: `backend/carlo/ssh.py`
- Modify: `backend/tests/test_ssh.py`
- Create: `backend/tests/test_action_ssh_integration.py`

**Interfaces:**
- Produces: remote prepare/start/tail/status/cancel/cleanup operations below `~/<workspace_root>/repositories` and `~/<workspace_root>/runs/<run-id>`.
- Consumes: immutable run origin/SHA/commands, trusted `Runner`, local mode-600 dotenv snapshot, and redactor.

- [ ] **Step 1: Write failing remote preparation tests**

Assert the transport creates or updates `~/.carlo/repositories/<project-key>.git`, fetches the target origin, verifies `<sha>^{commit}`, creates `runs/<id>/worktree` detached at exactly the run SHA, and fails before action commands when fetch cannot obtain it. Reject embedded origin credentials before SSH.

- [ ] **Step 2: Implement exact-commit remote preparation**

Use a retained bare repository and one isolated run directory. The remote account owns origin credentials. Use `git clone --mirror` only when the mirror is absent; otherwise `git remote set-url origin`, `git fetch --prune origin`, `git cat-file -e`, and `git worktree add --detach`.

- [ ] **Step 3: Write failing environment transfer and streaming tests**

Assert the normalized local snapshot is transferred through stdin or `scp` to a temporary remote path, changed to mode 600, and never appears in SSH argv. Verify remote raw output is fetched incrementally, redacted before appending locally, and deleted with the environment/worktree after terminal collection.

- [ ] **Step 4: Implement remote wrapper lifecycle**

Transfer the CARLO wrapper source/version for the run, then launch it detached. Store remote `console.log`, `state`, and `process-group` in the mode-700 run directory. Poll/tail using byte offsets. On SSH outage, persist `internal_stage="reconnecting"`, keep the run `running`, and retry that same run inside `ActionExecutor.run`; do not return from `ActionOrchestrator.run_next` or release `ACTION_LOCK` until remote state becomes known.

- [ ] **Step 5: Implement remote cancellation and restart reconciliation**

On pending cancellation, reconnect first, send TERM to the remote process group, wait ten seconds, then KILL. On restart:

- alive process: continue polling/tailing;
- terminal state: collect and finalize;
- unreachable runner: remain reconnecting and block queue;
- absent process without state: mark interrupted;
- cleanup failure: set `cleanup_pending=true` without rewriting the run result.

- [ ] **Step 6: Add an opt-in real SSH smoke test**

Guard with `CARLO_TEST_SSH_RUNNER`; when absent, skip. When configured, create temporary bare origin/source commits, execute two harmless commands remotely at the recorded commit, assert streamed/history output, construct a new executor instance, reconcile, and verify cleanup. Never use production projects or deployment commands.

- [ ] **Step 7: Run SSH suites and commit**

Run: `cd backend && uv run pytest tests/test_ssh.py tests/test_action_runner.py -q`

Optional: `cd backend && CARLO_TEST_SSH_RUNNER=1 uv run pytest tests/test_action_ssh_integration.py -q`

Expected: unit tests PASS; smoke test PASS when explicitly configured.

```bash
git add backend/carlo/action_runner.py backend/carlo/ssh.py backend/tests/test_ssh.py backend/tests/test_action_ssh_integration.py
git commit -m "feat: run project actions over SSH"
```

### Task 8: Build the Actions workspace and live console

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/ActionsView.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Produces: `ActionProject`, `ActionDefinition`, `ActionRun`, `ActionStep`, `Runner`, and API methods matching Tasks 3 and 6.
- Produces: `ActionsView` receiving `api`, `projects`, realtime `lastEvent`, and a shared error setter.
- Consumes: existing login, WebSocket reconnect, masthead, task detail visual language, and authenticated `request` helper.

- [ ] **Step 1: Write failing Board/Actions navigation and grouping tests**

Extend the fake `Api`, click Actions, and assert one card per project, its definitions grouped inside it, invalid catalog guidance, runner/last-result labels, and return to the unchanged six-column Board.

```tsx
await userEvent.click(screen.getByRole('button', { name: 'Actions' }))
expect(await screen.findByRole('heading', { name: 'ECADMO' })).toBeTruthy()
expect(screen.getByRole('button', { name: /Run Deploy to Dev/ })).toBeTruthy()
await userEvent.click(screen.getByRole('button', { name: 'Board' }))
expect(screen.getByRole('heading', { name: 'Not Ready' })).toBeTruthy()
```

- [ ] **Step 2: Add API types/methods and navigation shell**

Add `view: 'board' | 'actions'` state and peer `[BOARD] [ACTIONS]` buttons in the masthead. Load action catalogs/runs only while Actions is selected. Preserve Board creation controls only on Board; show Manage runners in Actions.

- [ ] **Step 3: Write failing confirmation/history/detail tests**

Assert Run opens an accessible dialog containing project, full commit, runner, env path, and ordered command rows. A dirty catalog disables confirmation and lists paths. Selecting a current or historic run opens a side panel with steps, Git evidence, runner, status, duration, console, and Kill button only for queued/running status.

- [ ] **Step 4: Implement project cards and run confirmation**

Use native `<dialog>` or the existing fixed-panel CSS pattern without adding a component library. Manage focus on open/close, close on Escape, provide labelled close buttons, and make detail full-screen below 900px.

- [ ] **Step 5: Write failing offset-console and Kill tests**

Fake `action.output_available` with `{run_id, offset}`. Assert the component asks `getActionConsole(runId, previousOffset)`, appends text once, resumes after rerender, and does not reload all history. Assert Kill requires confirmation and calls `cancelActionRun` once.

- [ ] **Step 6: Implement live console and realtime refresh**

Keep `{text, nextOffset}` per selected run. On matching action events, refresh run details; on `action.output_available`, fetch from the current offset. Render the console in a `<pre aria-live="polite">` with a user-controlled follow-output toggle, not forced scroll. Download uses the same authenticated console endpoint from offset zero and therefore remains redacted.

- [ ] **Step 7: Run frontend tests/build and commit**

Run: `cd frontend && npm test`

Run: `cd frontend && npm run build`

Expected: both PASS.

```bash
git add frontend/src/api.ts frontend/src/ActionsView.tsx frontend/src/App.tsx frontend/src/styles.css frontend/src/App.test.tsx
git commit -m "feat: add project Actions workspace"
```

### Task 9: Add the runner management and trust dialog

**Files:**
- Modify: `frontend/src/ActionsView.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: runner endpoints/types from Tasks 6 and 8.
- Produces: admin runner list/add/edit/test/enable/disable/trust replacement UI inside Actions.

- [ ] **Step 1: Write failing runner dialog tests**

Open Manage runners and assert built-in local plus SSH rows. Submit name/host/port/user/identity path/workspace, display scanned key type/fingerprint, require a separate Trust action, and show mismatch replacement confirmation. Assert no private-key content field exists.

- [ ] **Step 2: Implement the minimal accessible dialog**

Use standard form controls and the same dialog primitive as run confirmation. Newly created runners remain disabled until fingerprint trust and a successful connection test. Show `last_check_ok`, timestamp/error, stored fingerprint, and buttons Add, Test, Edit, Disable/Enable, Trust/Replace trust.

- [ ] **Step 3: Run frontend tests/build and commit**

Run: `cd frontend && npm test && npm run build`

Expected: PASS.

```bash
git add frontend/src/ActionsView.tsx frontend/src/App.test.tsx frontend/src/styles.css
git commit -m "feat: manage SSH runners in Actions"
```

### Task 10: Add notifications, documentation, and end-to-end verification

**Files:**
- Modify: `backend/carlo/telegram.py`
- Modify: `backend/tests/test_telegram.py`
- Modify: `README.md`
- Modify: `Makefile`
- Modify: `.env.example`

**Interfaces:**
- Consumes: existing `Event` notifier and all action APIs/runtime.
- Produces: concise action Telegram notifications and operator instructions.

- [ ] **Step 1: Write failing Telegram classification tests**

Assert queued/started/succeeded/cancelled are informational, failed/interrupted/runner-offline are blocking, identity renders as project/action run rather than `CARLO`, and payload never contains console text.

```python
event = Event(type="action.failed", payload={"run_id": 42, "project_id": 3, "action_key": "deploy-dev"})
message, severity = format_event(event)
assert severity == "blocking"
assert "deploy-dev #42" in message
assert "console" not in message.lower()
```

- [ ] **Step 2: Add labels and action identity formatting**

Extend `LABELS` and `BLOCKING_EVENTS` for action lifecycle types. When `task_id` is null and `run_id/action_key` exist, render `action-key #run-id`; retain `CARLO` fallback for unrelated global events.

- [ ] **Step 3: Document exact setup and repository contract**

Document:

- `CARLO_SSH_KNOWN_HOSTS`, connect timeout, and cancel grace settings;
- mode/ownership expectations for identity keys and known-hosts directory;
- strict `carlo-actions.yaml` example and one-command-per-line/no-shell behavior;
- clean committed/pushed SHA requirements for local/SSH;
- ignored dotenv snapshot/redaction limits;
- runner trust workflow and remote `~/.carlo` layout;
- action history, live console, Kill semantics, recovery, and cleanup-pending behavior;
- optional SSH smoke prerequisites.

Add `test-actions-ssh` to `Makefile`, guarded by the same explicit environment configuration used by the smoke test; do not include it in ordinary `make test`.

- [ ] **Step 4: Run migration consistency and complete test suite**

Run: `cd backend && uv run alembic check`

Run: `cd backend && uv run pytest -q`

Run: `cd frontend && npm test`

Run: `cd frontend && npm run build`

Expected: all commands PASS. Verify the backend suite uses `carlo_test`, not production `carlov3`.

- [ ] **Step 5: Perform a local manual smoke run**

In a temporary Git repository, commit `carlo-actions.yaml` with two harmless commands, add the project through CARLO, run the action, observe offset events/live redacted console, reopen history, then start a sleep action and Kill it. Confirm the normal project checkout is unchanged and no transient environment/worktree remains.

- [ ] **Step 6: Run production build/config validation**

Run: `make build`

Run with the configured production env: `make prod-check`

Expected: frontend production assets build and CARLO accepts all production settings without placeholders.

- [ ] **Step 7: Commit the integration slice**

```bash
git add backend/carlo/telegram.py backend/tests/test_telegram.py README.md Makefile .env.example
git commit -m "docs: operate project actions safely"
```

## Deferred by Design

- No password SSH authentication or browser-managed private keys; add only if filesystem-managed keys become operationally insufficient.
- No parallel action runs or per-runner queues; add only when the deliberate global serialization becomes a measured bottleneck.
- No `act`/Ansible abstraction; repository commands already support both executables.
- No automatic retention/deletion UI; add when artifact growth requires an explicit audited policy.
- No remote CARLO daemon; native OpenSSH plus run state files supply the required recovery boundary.
