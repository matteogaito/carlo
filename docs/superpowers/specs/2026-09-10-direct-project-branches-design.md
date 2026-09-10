# Direct Project Branches Design

## Goal

CARLO performs implementation work directly in each project's registered Git
repository. It does not create implementation worktrees outside or inside the
project. One globally serialized implementation task owns the checkout at a
time.

## Branch model

Each approved parent task owns one branch shared by all of its active subtasks.
The branch name is:

```text
<PARENT_TASK_ID>_<compact-parent-description>
```

For example:

```text
PHOTODIGGER-1_implementskeleton
```

The description is derived deterministically from the parent title, normalized
to lowercase ASCII alphanumeric characters, and truncated so the complete Git
branch name stays within CARLO's existing database limit. A standalone task
uses its own task ID and title as the parent identity.

The first subtask creates the branch from the project's integration branch.
Every later subtask checks out the same branch and starts from the validated
checkpoint left by its predecessor. Each validated subtask creates a checkpoint
commit on that branch. Retry keeps the same checkout, branch, approved plan,
and checkpoint.

## Checkout safety

The registered `Project.repository_path` is the execution directory and the
only source checkout CARLO mutates. Before creating or switching to a parent
branch, CARLO requires the checkout to be clean. Unexpected local changes block
execution and are never stashed, reset, overwritten, or committed.

Once CARLO has started the parent branch, its uncommitted changes are recognized
as task state and may be resumed by the next attempt or Retry. The checkout is
left on the parent branch after completion so the user can inspect it. CARLO
does not merge or switch back automatically.

The existing global implementation lock remains the concurrency boundary. It
prevents two implementation tasks from switching or editing project checkouts
at the same time.

## Sandbox boundary

Pi runs with the registered project directory as its working directory. The
project-local `.git` directory therefore stays within the same filesystem
boundary as the source. Pi receives read and write access to the project only,
plus the existing exact read-only exceptions for managed skill packages.

No Git metadata, implementation checkout, temporary source tree, or build
output needs to live outside the registered project directory. Network policy
does not change.

## Storage changes

`GitWorkspace` stops creating and validating implementation worktree paths. It
instead owns branch preparation, checkout validation, checkpointing, diff
hashing, and restoration directly against `Project.repository_path`.

Implementation tasks persist the shared parent branch in `branch_name` and the
registered repository path in `worktree_path` for API compatibility during the
migration. New orchestration code treats that field as an execution path, not a
Git worktree.

The global `CARLO_WORKTREE_ROOT` setting is removed from implementation and
action execution. Project actions that need an isolated directory use
`<repository>/.carlo/actions/<run-id>`, keeping their files inside the project.
CARLO adds `.carlo/` to the repository's local `.git/info/exclude`; it does not
modify the project's committed `.gitignore`.

## Existing task migration

PHOTODIGGER-12 must retain its completed implementation. Migration will:

1. verify the main PhotoDigger checkout has no unrelated local changes;
2. exclude generated `.carlo` state locally;
3. checkpoint the relevant source and test changes on the existing task branch
   without committing generated build caches;
4. unregister the old worktree only after the checkpoint exists;
5. check out the shared parent branch in the registered project directory;
6. update the task family's persisted execution path and branch; and
7. resume PHOTODIGGER-12 through Retry without rerunning planning.

If any safety check fails, migration stops with both the old worktree and its
branch intact.

## Failure handling

- Dirty checkout before first ownership: block without mutation.
- Wrong checked-out branch during an active family: switch only when the
  checkout is clean; otherwise block.
- Missing predecessor checkpoint: keep the existing blocking behavior.
- Branch history not containing the predecessor checkpoint: block instead of
  resetting or force-updating.
- Git command failure: record the exact command error and preserve disk state.

## Verification

Tests cover deterministic parent branch naming, all subtasks sharing one
branch, sequential checkpoint ancestry, standalone tasks, dirty-checkout
refusal, Retry reuse, project-local sandbox access to `.git`, project-local
action directories, legacy task migration safety, and the absence of new
implementation worktrees. The full backend, frontend, extension, migration,
and production build checks must remain green.
