# Integration Branch Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote each successfully completed parent or standalone task into the project's configured integration branch so every later parent starts from all accepted work.

**Architecture:** Add one atomic, ancestry-checked `GitWorkspace.promote()` operation that moves only the local integration ref and never switches the checkout. Call it from `Orchestrator` immediately before the completion transition; promotion failure leaves the implementation intact and moves the active task to `BLOCKED` with a diagnostic event.

**Tech Stack:** Python 3.14, asyncio subprocesses, Git CLI, SQLAlchemy 2, pytest.

**Spec:** `docs/superpowers/specs/2026-09-13-integration-branch-promotion-design.md`

## Global Constraints

- Promote only validated standalone tasks or parents whose active subtasks have all validated.
- Use `Project.integration_branch`; do not rename existing integration branches.
- Require the current integration tip to be an ancestor of the validated checkpoint.
- Update only the local integration ref; do not merge, switch, push, force-update, stash, reset, or enumerate other branches.
- Failed, blocked, interrupted, partial, and superseded work must never be promoted.
- Keep the repository checked out on the completed task branch.
- A promotion error must block visibly and must not produce a retry loop.
- Add no dependency, schema migration, endpoint, or UI control.

---

### Task 1: Add atomic fast-forward promotion to GitWorkspace

**Files:**
- Modify: `backend/carlo/git.py`
- Test: `backend/tests/test_git.py`

**Interfaces:**
- Consumes: `GitWorkspace.repository`, `GitWorkspace.integration_branch`, and a validated commit SHA.
- Produces: `async GitWorkspace.promote(checkpoint: str) -> str`, returning the promoted checkpoint SHA.
- Raises: `GitError("integration branch has diverged from the validated checkpoint")` when fast-forward ancestry is absent.

- [ ] **Step 1: Write the failing promotion tests**

Append focused tests to `backend/tests/test_git.py`:

```python
@pytest.mark.asyncio
async def test_promote_fast_forwards_integration_without_switching(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Feature")
    (repository / "feature.txt").write_text("done\n")
    checkpoint = await workspace.checkpoint(checkout, "validated")

    promoted = await workspace.promote(checkpoint)

    assert promoted == checkpoint
    assert git(repository, "rev-parse", "carlo-Dev") == checkpoint
    assert git(repository, "branch", "--show-current") == "CAR-1_feature"
    assert git(repository, "rev-list", "--merges", "carlo-Dev") == ""


@pytest.mark.asyncio
async def test_promote_is_idempotent(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Feature")
    checkpoint = await workspace.checkpoint(checkout, "validated")

    assert await workspace.promote(checkpoint) == checkpoint
    assert await workspace.promote(checkpoint) == checkpoint


@pytest.mark.asyncio
async def test_promote_refuses_diverged_integration_branch(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Feature")
    (repository / "feature.txt").write_text("done\n")
    checkpoint = await workspace.checkpoint(checkout, "validated")
    old_integration = git(repository, "rev-parse", "carlo-Dev")
    git(repository, "switch", "carlo-Dev")
    (repository / "other.txt").write_text("other\n")
    git(repository, "add", "other.txt")
    git(repository, "commit", "-m", "diverged")
    diverged = git(repository, "rev-parse", "HEAD")
    git(repository, "switch", "CAR-1_feature")

    with pytest.raises(
        GitError,
        match="integration branch has diverged from the validated checkpoint",
    ):
        await workspace.promote(checkpoint)

    assert old_integration != diverged
    assert git(repository, "rev-parse", "carlo-Dev") == diverged
    assert git(repository, "branch", "--show-current") == "CAR-1_feature"
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
cd backend && uv run pytest tests/test_git.py -q
```

Expected: the three new tests fail because `GitWorkspace` has no `promote` method.

- [ ] **Step 3: Implement the minimal Git primitive**

Add this method to `GitWorkspace` in `backend/carlo/git.py`:

```python
async def promote(self, checkpoint: str) -> str:
    if not (self.repository / ".git").is_dir():
        raise GitError("registered project is not a direct Git checkout")
    await self._git("rev-parse", "--git-dir", cwd=self.repository)
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", checkpoint):
        raise GitError("invalid checkpoint SHA")
    await self._git("cat-file", "-e", f"{checkpoint}^{{commit}}", cwd=self.repository)
    await self._ensure_integration_branch()
    current = await self._git(
        "rev-parse", f"refs/heads/{self.integration_branch}", cwd=self.repository
    )
    if await self._git_status(
        "merge-base", "--is-ancestor", current, checkpoint, cwd=self.repository
    ):
        raise GitError(
            "integration branch has diverged from the validated checkpoint"
        )
    await self._git(
        "update-ref",
        f"refs/heads/{self.integration_branch}",
        checkpoint,
        current,
        cwd=self.repository,
    )
    return checkpoint
```

`git update-ref <ref> <new> <old>` makes the ref update atomic and naturally supports the idempotent case where `current == checkpoint`. It does not touch the index, worktree, or checked-out branch.

- [ ] **Step 4: Run the Git tests to verify GREEN**

Run:

```bash
cd backend && uv run pytest tests/test_git.py -q
```

Expected: all Git tests pass.

- [ ] **Step 5: Commit the Git primitive**

```bash
git add backend/carlo/git.py backend/tests/test_git.py
git commit -m "feat: fast-forward integration branch"
```

---

### Task 2: Promote validated task families before marking them complete

**Files:**
- Modify: `backend/carlo/orchestrator.py`
- Test: `backend/tests/test_orchestration.py`

**Interfaces:**
- Consumes: `GitWorkspace.promote(checkpoint: str) -> str` from Task 1.
- Produces: `async Orchestrator._promotion_target(session: AsyncSession, task: Task) -> Task | None`.
- Emits: `git.integration_promoted` with `integration_branch` and `checkpoint`.
- Emits on refusal: `git.integration_promotion_failed` with `integration_branch` and `error`.

- [ ] **Step 1: Add the small Git helper used by completion tests**

Add these beside the imports in `backend/tests/test_orchestration.py`, and add
`TaskStage` and `TaskStatus` to the existing `carlo.domain` import:

```python
def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository_at(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("base\n")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "base")
    git(path, "branch", "carlo-Dev")
    return path


async def empty_orchestration_store():
    engine = create_async_engine("postgresql+psycopg:///carlo_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE events, validation_runs, escalations, attempts, "
                "plan_revisions, tasks, projects, agent_profiles "
                "RESTART IDENTITY CASCADE"
            )
        )
    return engine, async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
```

- [ ] **Step 2: Write the failing standalone completion test**

Append this test to `backend/tests/test_orchestration.py`:

```python
@pytest.mark.asyncio
async def test_standalone_completion_promotes_integration_branch(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    checkout = await GitWorkspace(repository, "carlo-Dev").prepare("CAR-1", "Feature")
    (repository / "feature.txt").write_text("done\n")
    checkpoint = await GitWorkspace(repository, "carlo-Dev").checkpoint(
        checkout, "validated"
    )

    engine, factory = await empty_orchestration_store()
    async with factory() as session:
        project = Project(
            name="CARLO", key="CAR", repository_path=str(repository),
            integration_branch="carlo-Dev",
        )
        task = Task(
            id="CAR-1", project=project, sequence=1, title="Feature", goal="Feature",
            status=TaskStatus.IN_PROGRESS, stage=TaskStage.VALIDATING,
            branch_name=checkout.branch, worktree_path=str(repository),
            checkpoint_sha=checkpoint,
        )
        session.add_all([project, task])
        await session.commit()

    async def unused_runner(task_id: str) -> str:
        raise AssertionError(task_id)

    await Orchestrator(engine, factory, unused_runner)._finish("CAR-1", "validated")

    async with factory() as session:
        task = await session.get(Task, "CAR-1")
        events = (await session.scalars(select(Event).where(Event.task_id == "CAR-1"))).all()
        assert task is not None
        assert task.status == TaskStatus.DONE
        assert git(repository, "rev-parse", "carlo-Dev") == checkpoint
        assert git(repository, "branch", "--show-current") == "CAR-1_feature"
        assert any(
            event.type == "git.integration_promoted"
            and event.payload == {
                "integration_branch": "carlo-Dev", "checkpoint": checkpoint,
            }
            for event in events
        )
    next_checkout = await GitWorkspace(repository, "carlo-Dev").prepare(
        "CAR-2", "Next Feature"
    )
    assert next_checkout.branch == "CAR-2_nextfeature"
    assert git(repository, "merge-base", "--is-ancestor", checkpoint, "HEAD") == ""
    await engine.dispose()
```

- [ ] **Step 3: Write the failing parent-family boundary test**

Append a direct completion-boundary test. Create its repository and commits
with these exact calls:

```python
repository = repository_at(tmp_path / "repo")
workspace = GitWorkspace(repository, "carlo-Dev")
checkout = await workspace.prepare("CAR-1", "Parent Feature")
(repository / "first.txt").write_text("first\n")
first_checkpoint = await workspace.checkpoint(checkout, "first validated")
(repository / "second.txt").write_text("second\n")
second_checkpoint = await workspace.checkpoint(checkout, "second validated")
engine, factory = await empty_orchestration_store()
```

Insert this task state in one session:

```python
project = Project(
    name="CARLO", key="CAR", repository_path=str(repository),
    integration_branch="carlo-Dev",
)
parent = Task(
    id="CAR-1", project=project, sequence=1, title="Parent Feature", goal="Parent",
    status=TaskStatus.IN_PROGRESS, stage=TaskStage.IMPLEMENTING,
)
first = Task(
    id="CAR-2", project=project, parent_task_id="CAR-1", subtask_position=0,
    sequence=2, title="First", goal="First", status=TaskStatus.DONE,
    stage=TaskStage.COMPLETE, branch_name=checkout.branch,
    worktree_path=str(repository), checkpoint_sha=first_checkpoint,
)
second = Task(
    id="CAR-3", project=project, parent_task_id="CAR-1", subtask_position=1,
    sequence=3, title="Second", goal="Second", status=TaskStatus.IN_PROGRESS,
    stage=TaskStage.VALIDATING, branch_name=checkout.branch,
    worktree_path=str(repository), checkpoint_sha=second_checkpoint,
)
session.add_all([project, parent, first, second])
await session.commit()
```

Before completion, assert `git(repository, "rev-parse", "carlo-Dev") !=
second_checkpoint`. Then execute and reload the state with:

```python
async def unused_runner(task_id: str) -> str:
    raise AssertionError(task_id)

await Orchestrator(engine, factory, unused_runner)._finish("CAR-3", "validated")

async with factory() as session:
    parent = await session.get(Task, "CAR-1")
    first = await session.get(Task, "CAR-2")
    second = await session.get(Task, "CAR-3")
    promotions = (await session.scalars(
        select(Event).where(Event.type == "git.integration_promoted")
    )).all()
```

Assert:

```python
assert first.status == TaskStatus.DONE
assert second.status == TaskStatus.DONE
assert parent.status == TaskStatus.DONE
assert parent.checkpoint_sha == second_checkpoint
assert git(repository, "rev-parse", "carlo-Dev") == second_checkpoint
assert [(event.task_id, event.payload) for event in promotions] == [
    ("CAR-1", {
        "integration_branch": "carlo-Dev", "checkpoint": second_checkpoint,
    })
]
```

- [ ] **Step 4: Write the failing divergence test**

Create the standalone repository and divergence with these exact calls:

```python
repository = repository_at(tmp_path / "repo")
workspace = GitWorkspace(repository, "carlo-Dev")
checkout = await workspace.prepare("CAR-1", "Feature")
(repository / "feature.txt").write_text("done\n")
checkpoint = await workspace.checkpoint(checkout, "validated")
git(repository, "switch", "carlo-Dev")
(repository / "other.txt").write_text("other\n")
git(repository, "add", "other.txt")
git(repository, "commit", "-m", "diverged")
diverged_sha = git(repository, "rev-parse", "HEAD")
git(repository, "switch", checkout.branch)
engine, factory = await empty_orchestration_store()
```

Insert these records, call `_finish`, then assert:

```python
async with factory() as session:
    project = Project(
        name="CARLO", key="CAR", repository_path=str(repository),
        integration_branch="carlo-Dev",
    )
    task = Task(
        id="CAR-1", project=project, sequence=1, title="Feature", goal="Feature",
        status=TaskStatus.IN_PROGRESS, stage=TaskStage.VALIDATING,
        branch_name=checkout.branch, worktree_path=str(repository),
        checkpoint_sha=checkpoint,
    )
    session.add_all([project, task])
    await session.commit()

async def unused_runner(task_id: str) -> str:
    raise AssertionError(task_id)

orchestrator = Orchestrator(engine, factory, unused_runner)
await orchestrator._finish("CAR-1", "validated")

async with factory() as session:
    task = await session.get(Task, "CAR-1")
    events = (await session.scalars(
        select(Event).where(Event.task_id == "CAR-1")
    )).all()
```

```python
assert task.status == TaskStatus.IN_PROGRESS
assert task.stage == TaskStage.BLOCKED
assert git(repository, "rev-parse", "carlo-Dev") == diverged_sha
assert git(repository, "rev-parse", task.branch_name) == task.checkpoint_sha
assert any(
    event.type == "git.integration_promotion_failed"
    and "diverged" in event.payload["error"]
    for event in events
)
```

Call `run_next()` once more and assert it returns `None`; this proves the worker does not loop on the blocked promotion.

Extend the existing parametrized
`test_context_limit_rolls_over_to_a_focused_subtask_attempt` by saving
`initial_integration = git(repository, "rev-parse", "carlo-Dev")` before
orchestration and adding:

```python
integration_tip = git(repository, "rev-parse", "carlo-Dev")
if recovery_succeeds:
    assert integration_tip == task.checkpoint_sha
else:
    assert task.checkpoint_sha
    assert integration_tip == initial_integration
```

This proves a partial checkpoint from a failed context-limit recovery is not
promoted. Existing task-selection tests remain the guard that superseded tasks
never reach `_finish`.

- [ ] **Step 5: Run the focused tests to verify RED**

Run:

```bash
cd backend && uv run pytest tests/test_orchestration.py -q
```

Expected: the new assertions fail because successful completion does not yet update the integration ref or emit promotion events.

- [ ] **Step 6: Implement completion-boundary promotion**

In `Orchestrator`, add a query helper with this behavior:

```python
@staticmethod
async def _promotion_target(session: AsyncSession, task: Task) -> Task | None:
    if task.parent_task_id is None:
        return task
    parent = await session.scalar(
        select(Task).where(Task.id == task.parent_task_id).with_for_update()
    )
    if parent is None:
        raise GitError("parent task is missing")
    unfinished = await session.scalar(
        select(func.count(Task.id)).where(
            Task.parent_task_id == parent.id,
            Task.superseded_at.is_(None),
            Task.id != task.id,
            Task.status != TaskStatus.DONE,
        )
    )
    return parent if not unfinished else None
```

At the start of the `outcome == "validated"` branch in `_finish`, preserve the
existing generic-runner behavior for tasks with neither `branch_name` nor
`worktree_path`. For Git-backed tasks, resolve the target. If one exists, use
the current task checkpoint as the validated family checkpoint and call:

```python
if task.branch_name is not None or task.worktree_path is not None:
    checkpoint = task.checkpoint_sha
    if checkpoint is None:
        raise GitError("validated task has no checkpoint")
    target = await self._promotion_target(session, task)
    if target is not None:
        workspace = GitWorkspace(
            Path(task.project.repository_path), task.project.integration_branch
        )
        try:
            await workspace.promote(checkpoint)
        except GitError as error:
            task.status = TaskStatus.IN_PROGRESS
            task.stage = TaskStage.BLOCKED
            task.version += 1
            session.add(
                Event(
                    task=task,
                    type="git.integration_promotion_failed",
                    payload={
                        "integration_branch": task.project.integration_branch,
                        "error": str(error),
                    },
                )
            )
            await self._sync_parent(session, task)
            await session.commit()
            return
        target.checkpoint_sha = checkpoint
        session.add(
            Event(
                task=target,
                type="git.integration_promoted",
                payload={
                    "integration_branch": task.project.integration_branch,
                    "checkpoint": checkpoint,
                },
            )
        )
```

Then retain the existing successful child transition and `_sync_parent` call. Do not change task selection, parent-branch naming, checkpoint creation, retries, API views, or deployment.

- [ ] **Step 7: Run focused orchestration and Git tests**

Run:

```bash
cd backend && uv run pytest tests/test_git.py tests/test_orchestration.py -q
```

Expected: all tests pass, including promotion, sequential parent branches, retries, and divergence blocking.

- [ ] **Step 8: Run the complete verification suite**

Run:

```bash
make test
```

Expected: backend, frontend, extension, build, migration, and production configuration checks all pass.

- [ ] **Step 9: Commit orchestration integration**

```bash
git add backend/carlo/orchestrator.py backend/tests/test_orchestration.py
git commit -m "feat: promote completed tasks to integration"
```
