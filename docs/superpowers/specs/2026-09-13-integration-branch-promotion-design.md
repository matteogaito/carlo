# Integration Branch Promotion Design

## Goal

Make every new parent task start from all previously completed work without
merging arbitrary task branches. Carlo promotes only a successfully completed
parent task into the project's configured `integration_branch`.

## Lifecycle

All subtasks continue to work sequentially on their shared parent-task branch.
When the last active subtask validates, Carlo already marks the parent `DONE`
and copies the last subtask checkpoint to the parent. At that boundary Carlo
fast-forwards `Project.integration_branch` to the parent's validated checkpoint.

A standalone task is promoted when it validates directly. A failed, blocked,
superseded, interrupted, or partially completed task is never promoted.

After promotion, the repository remains checked out on the completed task
branch for inspection. The next parent task uses the updated integration branch
as its base through the existing `GitWorkspace.prepare` flow.

## Git safety

Promotion is ancestry-only:

1. Verify the repository is the registered direct checkout.
2. Verify the task checkpoint exists.
3. Require the current integration tip to be an ancestor of the checkpoint.
4. Update the local integration ref with a fast-forward operation.

Carlo does not enumerate or merge other branches, create merge commits, switch
the checkout, push remotes, resolve conflicts, force-update refs, stash changes,
or alter the working tree. A divergent integration branch blocks promotion and
records a clear Git error; the completed implementation remains intact on its
task branch.

Promotion and the parent `DONE` transition form one orchestration outcome: a
parent must not be reported as fully completed until its checkpoint has been
promoted. A transient interruption may retry the idempotent fast-forward.

## Existing projects

The branch name remains project-configurable. Existing projects using
`carlo-Dev` keep that name; projects configured with `dev` use `dev`. No branch
is renamed and no historical task branch is merged during deployment.

The first new completion after deployment promotes only that completed parent.
If its branch was originally based on an older integration tip, the
fast-forward succeeds when ancestry is intact. Divergence requires explicit
human reconciliation before later tasks can start from a single trustworthy
base.

## Observability

Successful promotion emits an event containing the integration branch and
checkpoint SHA. Refusal emits the precise Git reason and leaves both refs
unchanged. Existing task diagnostics expose these events; no new diagnostics
subsystem is needed.

## Verification

Tests cover parent completion promotion, standalone-task promotion, new parent
branch creation from the promoted tip, no promotion before all subtasks pass,
no promotion for failed or superseded work, idempotent retry, divergence
refusal, unchanged checkout branch, and absence of merge commits or remote
pushes.
