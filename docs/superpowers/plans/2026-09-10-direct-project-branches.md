# Direct Project Branches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-subtask implementation worktrees with one direct project checkout and one parent-task branch shared sequentially by every subtask.

**Architecture:** `GitWorkspace` will manage the registered repository directly and enforce clean, non-destructive branch switching. `ImplementationPipeline` will derive one branch identity from the parent task and reuse it for every child, while local action worktrees and Carlo-owned prompts live under the project-local ignored `.carlo` directory. Deployment changes Carlo only and does not mutate registered projects.

**Tech Stack:** Python 3.14, asyncio subprocesses, Git CLI, SQLAlchemy 2, pytest, Bash deployment scripts.

**Spec:** `docs/superpowers/specs/2026-09-10-direct-project-branches-design.md`

## Global Constraints

- Only one implementation task runs globally; the registered project checkout is exclusive while it runs.
- The branch format is `<PARENT_TASK_ID>_<compact-parent-description>`; `PHOTODIGGER-1` plus title `Implement skeleton` becomes `PHOTODIGGER-1_implementskeleton`.
- All active subtasks of one parent share the same branch and build consecutively on validated checkpoints.
- Unexpected user changes are never stashed, reset, overwritten, removed, or committed.
- Pi filesystem writes stay inside the registered project directory; managed package directories remain read-only exceptions.
- CARLO does not merge or switch away from the parent branch after completion.
- No new dependency or external process supervisor is introduced.

---

### Task 1: Replace implementation worktrees with a guarded direct checkout

**Files:**
- Modify: `backend/carlo/git.py`
- Modify: `backend/tests/test_git.py`

**Interfaces:**
- Produces: `Checkout(branch: str, path: Path)`.
- Produces: `parent_branch_name(task_id: str, title: str) -> str`.
- Produces: `GitWorkspace(repository: Path, integration_branch: str)`.
- Produces: `prepare(task_id: str, title: str, *, base_ref: str | None = None) -> Checkout`.
- Preserves: `checkpoint`, `diff_hash`, and `restore`, now accepting `Checkout`.

- [ ] **Step 1: Write failing branch-name and direct-checkout tests**

Replace worktree expectations with tests containing these assertions:

```python
assert parent_branch_name("PHOTODIGGER-1", "Implement skeleton") == (
    "PHOTODIGGER-1_implementskeleton"
)

workspace = GitWorkspace(repository, "carlo-Dev")
checkout = await workspace.prepare("CAR-1", "Login Flow")
assert checkout.path == repository.resolve()
assert checkout.branch == "CAR-1_loginflow"
assert git(repository, "branch", "--show-current") == "CAR-1_loginflow"
assert ".carlo/" in (repository / ".git" / "info" / "exclude").read_text().splitlines()
```

Add a second subtask preparation using the same owner ID and a predecessor
checkpoint; assert it returns the same branch and that the checkpoint is an
ancestor of `HEAD`.

- [ ] **Step 2: Write failing dirty-checkout and ancestry tests**

```python
(repository / "user-note.txt").write_text("mine\n")
with pytest.raises(GitError, match="uncommitted changes"):
    await GitWorkspace(repository, "carlo-Dev").prepare("CAR-2", "Safe branch")
assert (repository / "user-note.txt").read_text() == "mine\n"
```

Create an existing parent branch that does not contain `base_ref`; assert
`prepare` raises `GitError("parent branch does not contain the previous subtask checkpoint")`
without resetting either branch.

- [ ] **Step 3: Run the Git tests and verify RED**

Run:

```bash
cd backend && uv run pytest tests/test_git.py -q
```

Expected: failures because `GitWorkspace` still requires `worktree_root`, creates
hyphenated per-task branches, and returns an external worktree.

- [ ] **Step 4: Implement the direct-checkout contract**

Use this shape in `backend/carlo/git.py`:

```python
@dataclass(frozen=True, slots=True)
class Checkout:
    branch: str
    path: Path


def parent_branch_name(task_id: str, title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    description = re.sub(r"[^a-z0-9]+", "", ascii_title.lower())[:60] or "task"
    return f"{task_id}_{description}"[:240]
```

`GitWorkspace.prepare` must perform these operations in order:

1. verify `repository/.git` is a directory;
2. append `.carlo/` once to `.git/info/exclude`;
3. ensure the integration branch exists;
4. return immediately when the expected branch is already checked out;
5. reject a dirty checkout before any switch;
6. `git switch` an existing expected branch or `git switch -c` a missing one;
7. when `base_ref` is supplied, require `git merge-base --is-ancestor base_ref HEAD`.

Update `_validate` to require `checkout.path.resolve() == repository` and the
expected branch to be currently checked out. Delete `worktree_root`, `git
worktree add`, and rework-cycle branch suffixes.

- [ ] **Step 5: Run the Git tests and verify GREEN**

```bash
cd backend && uv run pytest tests/test_git.py -q
```

Expected: all Git tests pass and no test creates a sibling implementation
worktree directory.

- [ ] **Step 6: Commit the Git boundary**

```bash
git add backend/carlo/git.py backend/tests/test_git.py
git commit -m "refactor: use direct project checkout"
```

---

### Task 2: Share the parent branch across sequential subtasks

**Files:**
- Modify: `backend/carlo/orchestrator.py`
- Modify: `backend/carlo/worker.py`
- Modify: `backend/tests/test_orchestration.py`

**Interfaces:**
- Consumes: `Checkout`, `GitWorkspace(repository, integration_branch)`, and `parent_branch_name` from Task 1.
- Produces: `ImplementationPipeline` without a `worktree_root` constructor argument.
- Persists: every child uses the same `branch_name` and `worktree_path == Project.repository_path`.

- [ ] **Step 1: Write the failing shared-parent-branch test**

Create an approved parent with two ordered children. Run the first child through
validation, then start the second. Assert:

```python
assert first.branch_name == second.branch_name
assert first.branch_name == "CAR-1_parentfeature"
assert first.worktree_path == str(repository.resolve())
assert second.worktree_path == str(repository.resolve())
assert git(repository, "merge-base", "--is-ancestor", first.checkpoint_sha, "HEAD") == ""
```

Also assert a standalone task uses its own ID and title for the same naming
rule.

- [ ] **Step 2: Write the failing Retry and wrong-branch tests**

For a failed child with saved direct-checkout state, call Retry and assert the
pipeline reuses the parent branch and approved plan. Switch a clean repository
to another branch and assert CARLO switches back; add an uncommitted file and
assert it blocks without mutation.

- [ ] **Step 3: Run the orchestration tests and verify RED**

```bash
cd backend && uv run pytest tests/test_orchestration.py -q
```

Expected: failures because children currently derive separate task branches and
the pipeline still validates paths against the global worktree root.

- [ ] **Step 4: Implement parent branch context in the pipeline**

In `ImplementationPipeline.run`, load the active parent when
`task.parent_task_id` is set and derive:

```python
branch_owner_id = parent.id if parent else task.id
branch_owner_title = parent.title if parent else task.title
workspace = GitWorkspace(
    Path(task.project.repository_path),
    task.project.integration_branch,
)
```

Keep the existing predecessor lookup. Pass its checkpoint as `base_ref`, call
`workspace.prepare(branch_owner_id, branch_owner_title, base_ref=base_ref)`,
and persist the returned branch and repository path on each child. Existing
saved state must be validated against the registered repository before reuse.

Remove `worktree_root` from `ImplementationPipeline.__init__` and its
construction in `worker.py`. Retain the global orchestration lock unchanged.

- [ ] **Step 5: Run orchestration and API Retry tests**

```bash
cd backend && uv run pytest tests/test_orchestration.py tests/test_api.py -q
```

Expected: all tests pass; Retry still avoids planning and every sibling uses
the parent branch.

- [ ] **Step 6: Commit shared branch orchestration**

```bash
git add backend/carlo/orchestrator.py backend/carlo/worker.py backend/tests/test_orchestration.py
git commit -m "feat: share parent task branch"
```

---

### Task 3: Keep action workspaces inside their project

**Files:**
- Modify: `backend/carlo/action_runner.py`
- Modify: `backend/carlo/worker.py`
- Modify: `backend/tests/test_action_runner.py`

**Interfaces:**
- Produces: `ActionExecutor(session_factory, artifact_root, cancel_grace_seconds, ssh_transport)` without `worktree_root`.
- Produces: local action path `<Project.repository_path>/.carlo/actions/<run-id>`.

- [ ] **Step 1: Write the failing project-local action test**

Run a local action and assert:

```python
assert run.worktree_path == str(repository / ".carlo" / "actions" / str(run.id))
assert not (external_worktree_root / "actions" / str(run.id)).exists()
assert ".carlo/" in (repository / ".git" / "info" / "exclude").read_text().splitlines()
```

Retain assertions that cleanup unregisters and removes the detached action
worktree after success, failure, or cancellation.

- [ ] **Step 2: Run the action tests and verify RED**

```bash
cd backend && uv run pytest tests/test_action_runner.py -q
```

Expected: failure because `ActionExecutor` currently builds paths from the
global `worktree_root`.

- [ ] **Step 3: Implement the project-local action path**

Resolve the workspace from the trusted project path:

```python
repository = Path(project.repository_path).resolve()
workspace_root = repository / ".carlo" / "actions"
workspace = (workspace_root / str(run.id)).resolve()
if not workspace.is_relative_to(workspace_root):
    raise RuntimeError("invalid action workspace path")
```

Ensure `.carlo/` is present once in `.git/info/exclude` before `git worktree
add --detach`. Remove the constructor field and the corresponding argument in
`worker.py`; preserve remote-runner behavior.

- [ ] **Step 4: Run the action tests and verify GREEN**

```bash
cd backend && uv run pytest tests/test_action_runner.py -q
```

Expected: all action lifecycle tests pass.

- [ ] **Step 5: Commit project-local actions**

```bash
git add backend/carlo/action_runner.py backend/carlo/worker.py backend/tests/test_action_runner.py
git commit -m "refactor: keep actions inside projects"
```

---

### Task 4: Remove the global worktree setting and preserve configured roots

**Files:**
- Modify: `backend/carlo/config.py`
- Modify: `backend/carlo/production.py`
- Modify: `backend/tests/test_config.py`
- Modify: `backend/tests/test_production_flow.py`
- Modify: `.env.example`
- Modify: `.env.production.example`
- Modify: `scripts/carlo-service.sh`
- Modify: `README.md`

**Interfaces:**
- Removes: `Settings.worktree_root`, `DEFAULT_WORKTREE_ROOT`, and `CARLO_WORKTREE_ROOT`.
- Preserves: configured `CARLO_ARTIFACT_ROOT`; falls back to `$CARLO_STATE_ROOT/artifacts` only when absent.

- [ ] **Step 1: Write failing configuration and service-runner tests**

Delete `CARLO_WORKTREE_ROOT` from required production keys and assert:

```python
settings = Settings.from_env()
assert not hasattr(settings, "worktree_root")
assert "CARLO_WORKTREE_ROOT" not in (ROOT / ".env.production.example").read_text()
```

Add a runner contract assertion that it does not overwrite an artifact root
loaded from `.env.production`:

```python
runner = (ROOT / "scripts" / "carlo-service.sh").read_text()
assert 'CARLO_ARTIFACT_ROOT="${CARLO_ARTIFACT_ROOT:-$STATE_ROOT/artifacts}"' in runner
assert "CARLO_WORKTREE_ROOT" not in runner
```

- [ ] **Step 2: Run configuration tests and verify RED**

```bash
cd backend && uv run pytest tests/test_config.py tests/test_production_flow.py -q
```

Expected: failures because settings and templates still require the global root
and the service runner currently overwrites configured paths.

- [ ] **Step 3: Remove the setting and fix the runner**

Delete the worktree setting from `config.py`, production validation, both env
templates, README instructions, and worker construction. In
`carlo-service.sh`, replace unconditional root assignment with:

```bash
export CARLO_ARTIFACT_ROOT="${CARLO_ARTIFACT_ROOT:-$STATE_ROOT/artifacts}"
```

Do not export `CARLO_WORKTREE_ROOT`. This also removes the root mismatch that
caused PHOTODIGGER-12 to fail after `agent.completed`.

- [ ] **Step 4: Run configuration and production tests**

```bash
cd backend && uv run pytest tests/test_config.py tests/test_production_flow.py -q
```

Expected: all tests pass and no runtime file references `CARLO_WORKTREE_ROOT`.

- [ ] **Step 5: Commit configuration cleanup**

```bash
git add backend/carlo/config.py backend/carlo/production.py backend/carlo/worker.py backend/tests/test_config.py backend/tests/test_production_flow.py .env.example .env.production.example scripts/carlo-service.sh README.md
git commit -m "refactor: remove global worktree root"
```

---

### Task 5: Verify the project sandbox and keep Carlo metadata ignored

**Files:**
- Modify: `backend/tests/test_orchestration.py`
- Modify: `backend/tests/test_pi_runtime.py`
- Modify: `backend/carlo/tasks.py`
- Modify: `backend/tests/test_tasks.py`
- Modify: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: direct repository checkout and parent branch behavior from Tasks 1–4.
- Produces: implementation working directory equal to the registered repository.
- Produces: Carlo-owned prompts under the ignored `.carlo/prompts` directory.

- [ ] **Step 1: Add the project-boundary regression tests**

Assert the implementation provider runs from the registered repository,
the sandbox writes only to `.`, and managed skill packages are exact read-only
exceptions. Assert new task prompts are stored below `.carlo/prompts`.

- [ ] **Step 2: Run focused sandbox and prompt tests**

```bash
cd backend && uv run pytest tests/test_pi_runtime.py tests/test_orchestration.py tests/test_tasks.py tests/test_api.py tests/test_end_to_end.py -q
```

Expected: all tests pass and creating a task does not dirty the project before
its first direct checkout.

- [ ] **Step 3: Commit final regressions**

```bash
git add backend/carlo/tasks.py backend/tests/test_tasks.py backend/tests/test_api.py backend/tests/test_orchestration.py backend/tests/test_pi_runtime.py
git commit -m "test: cover project-local task execution"
```

---

### Task 6: Final review and production evidence

**Files:**
- Review: all files changed by Tasks 1–5

**Interfaces:**
- Consumes: all preceding task deliverables.
- Produces: verified source and deployed Carlo service.

- [ ] **Step 1: Run focused safety checks**

```bash
cd backend && uv run pytest tests/test_git.py tests/test_orchestration.py tests/test_action_runner.py tests/test_pi_runtime.py tests/test_config.py tests/test_production_flow.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run the complete repository verification**

```bash
make test
git diff --check
```

Expected: extension tests, Python compilation, all backend tests, Alembic
consistency, frontend tests, and frontend production build pass with no
whitespace errors.

- [ ] **Step 3: Verify runtime and repository containment**

```bash
make status
```

Expected: exactly one Carlo service is running and its HTTP endpoint responds;
deployment performs no mutation in any registered project.

- [ ] **Step 4: Commit any final documentation-only correction**

Only when Step 3 reveals a documentation mismatch:

```bash
git add README.md docs/superpowers/specs/2026-09-10-direct-project-branches-design.md
git commit -m "docs: clarify direct project execution"
```
