# Project actions and SSH runners

## Goal

Add an Actions workspace where an administrator can run repository-defined,
multi-step operational commands and follow their output like a Jenkins console.
Actions may run locally or on a configured SSH runner. Every run is tied to one
clean Git commit of the target project, is globally serialized with other action
runs, survives CARLO restarts, and retains an auditable history.

This subsystem is independent of the globally serialized implementation queue.
At most one action and one implementation task may run at the same time.

## Scope

The first slice includes:

- project action discovery from `carlo-actions.yaml` at the project root;
- sequential commands with stop-on-first-failure behavior;
- optional per-action dotenv files;
- local and SSH execution at an exact project commit;
- one global action queue;
- persistent runs, steps, console logs, events, and restart reconciliation;
- live console output, run history, and Kill run;
- administrator-managed SSH runner metadata and host trust;
- existing Telegram event notifications for action lifecycle events.

It is a generic command runner. `act` is one command an action may invoke, not a
special CARLO provider or required dependency.

## Repository contract

CARLO reads the action catalog from the committed version of
`carlo-actions.yaml`, never from an uncommitted filesystem copy. The initial
schema is deliberately small:

```yaml
version: 1

actions:
  deploy-dev:
    name: Deploy to Dev
    runner: linux-build
    env_file: .env.dev
    commands:
      - make test
      - docker build -t ecadmo-api .
      - act workflow_dispatch -W pipelines/deploy.yml -j deploy

  run-tests:
    name: Run tests
    commands:
      - make test
```

`actions` is a mapping whose stable key identifies the action inside the
project. `name` and a non-empty `commands` list are required. `runner` defaults
to the built-in `local` runner. `env_file` is optional and must be a relative
path contained by the project root. Unknown fields and unsupported schema
versions are rejected with a useful project-level error.

Each command is one YAML string. CARLO uses `shlex.split()` and executes the
resulting argument vector without an implicit local shell. Shell operators such
as `&&`, pipes, redirects, and substitutions are not interpreted. Projects that
need shell behavior call a committed script explicitly.

The API validates and returns catalog errors without persisting action
definitions. A run stores an immutable snapshot of the selected definition so
history remains meaningful after the YAML changes.

## Git gate

Pressing Run begins with a mandatory preflight against the target project:

1. the repository exists and `HEAD` resolves to a commit;
2. tracked and non-ignored untracked files contain no pending changes;
3. `git show HEAD:carlo-actions.yaml` parses and contains the selected action;
4. the optional `env_file` exists as a regular file inside the project root;
5. the selected runner is active and reachable;
6. an SSH runner can fetch the exact commit from the project's `origin`.

CARLO does not commit, stash, reset, or push the source repository. A dirty
repository blocks the run with the affected paths and asks the user to commit
or discard them. Git-ignored files such as `.env` do not make the repository
dirty.

The run records project ID, action key, branch when available, full commit SHA,
sanitized origin identity, action snapshot, and requesting user. Embedded Git
passwords or tokens are rejected for SSH runs and are never stored or placed in
command arguments. Commands never run in the project's ordinary checkout. Local
runs use a detached worktree at the recorded SHA. SSH runs use a detached
worktree created from a runner-local bare mirror.

## Persistent records

Add three records through an Alembic migration.

### Runner

An SSH runner stores:

- unique display name used by `runner` in project YAML;
- host, port, and remote username;
- absolute local identity-file path;
- remote workspace root relative to the remote user's home, default `.carlo`;
- trusted host-key fingerprint and enabled state;
- last connection-check result and timestamps.

The built-in `local` runner is not editable and needs no database row.

### ActionRun

An action run stores:

- project, action key/name, definition snapshot, and requester;
- runner identity and target Git evidence;
- queue position inputs and lifecycle status;
- requested, started, finished, and cancellation timestamps;
- current step, process-group evidence, workspace location, and log offset;
- local console artifact path, concise error, and final exit code.

Run statuses are `queued`, `running`, `succeeded`, `failed`, `cancelled`, and
`interrupted`. Recovery details such as reconnecting remain an internal stage so
the public status set stays small.

### ActionStep

Each command snapshot becomes an ordered step containing command text, status,
timestamps, exit code, and its console byte offsets. Step statuses are
`pending`, `running`, `succeeded`, `failed`, `cancelled`, and `skipped`.

Existing persisted `Event` records carry action lifecycle events with project
and run IDs in their payload and no task ID. Definitions and runs are project
scoped for future membership authorization.

## Runner configuration and trust

Runner management lives behind an administrator-only API and a Manage runners
dialog on the Actions page. The form accepts name, host, port, username,
identity-file path, workspace root, and enabled state.

CARLO supports only non-interactive SSH private keys already placed on the
service host. It never generates, uploads, copies, edits, returns, or stores a
private key. Before saving, the backend requires the identity path to be a
regular readable file with restrictive permissions. Password authentication is
not supported.

CARLO owns a dedicated known-hosts file under:

```text
/Users/carlo/.config/carlo/ssh/known_hosts
```

Creating a runner scans the remote host key with a bounded timeout and returns
the key type and fingerprint to the administrator. The administrator must
confirm the fingerprint before CARLO appends the exact key and tests
authentication with strict host checking. A changed host key blocks all runs;
CARLO shows the stored and proposed fingerprints and never replaces trust
automatically. This is explicit trust-on-first-use, and the UI tells the user to
verify the fingerprint out of band.

The backend invokes native OpenSSH tooling with fixed argument vectors,
`BatchMode=yes`, strict host checking, the CARLO known-hosts file, and the
configured identity. Host, user, paths, action keys, and run IDs are validated
before they are used in local or remote commands.

## Remote workspace

All remote state stays below the configured SSH user's home:

```text
~/.carlo/
├── repositories/
│   └── ECA.git/
└── runs/
    └── 42/
        ├── worktree/
        ├── environment
        ├── console.log
        ├── state
        └── process-group
```

The first run creates a bare mirror from the project's origin. Later runs fetch
the origin and create an isolated detached worktree at the recorded SHA. If the
runner cannot obtain that SHA, preflight fails before any declared command.
Repository credentials needed by the remote `git fetch` belong to the remote
runner account and are outside CARLO's credential management.

CARLO does not use `rsync`. At enqueue time it snapshots the selected dotenv
file into a protected transient run-secret file with mode `600`. This prevents a
queued run from silently observing later dotenv edits. The snapshot is the only
project-local file transferred separately. CARLO parses it locally, sends a
normalized environment file over the authenticated SSH channel with mode `600`,
and never places secret values in an SSH command line. Terminal cleanup removes
the local secret snapshot, worktree, and transferred environment; the bare Git
mirror is retained. Cleanup failure is recorded and retried without changing
the run result.

## Environment handling

The dotenv parser accepts ordinary `KEY=VALUE` files with blank lines, comments,
and quoted values. Invalid variable names or malformed lines block preflight.
The subprocess receives a minimal base environment plus the parsed variables;
CARLO's database, Telegram, and authentication secrets are not inherited by
action commands.

Values loaded from `env_file` are treated as sensitive. Before console data is
written locally, persisted, or sent to the browser, exact values are replaced
with `***`. Empty values are ignored by the redactor. The UI warns that CARLO
cannot redact transformed, encoded, or indirectly fetched secrets and that
trusted project commands must not print credentials.

The database stores only the dotenv path and variable names, never values. The
protected transient snapshot is required for queued execution and restart
recovery but is deleted once the run reaches a terminal state.

## Execution and global serialization

The existing worker runs an action-queue loop alongside implementation
orchestration and notifications. A separate PostgreSQL advisory lock permits
only one action run globally, including across multiple CARLO worker processes.
Queue order is request time then run ID. Actions do not acquire or block the
implementation advisory lock.

CARLO persists intent before every external side effect. It prepares all step
records before starting the first command. Commands run sequentially. A zero
exit status marks a step succeeded; any non-zero status marks it failed and all
remaining steps skipped. A run succeeds only when every step exits zero. Output
parsing never overrides an exit status.

Local and remote execution use a small CARLO-owned wrapper which writes ordered
combined stdout/stderr, current step, exit status, and process-group ID to the
run directory. The remote wrapper is transferred and versioned by CARLO for the
run; it is not a persistent daemon.

## Console and artifacts

Every run has a redacted append-only `console.log` below the CARLO artifact
root. It contains CARLO lifecycle lines, each command, combined stdout/stderr,
step exit status, and final result. The database retains only structured
metadata and a bounded recent tail.

The worker batches `action.output_available` events containing run ID and latest
byte offset instead of copying console text into the event table. The browser
uses an authenticated offset-based endpoint to fetch new redacted bytes. This
keeps WebSocket updates live and replayable without duplicating full logs in
PostgreSQL. Reopening a historical run reads the same artifact from offset zero.

The remote reconnect mechanism requires a temporary raw console file in the
run's mode-`700` workspace. It is readable only by the configured runner user,
is never exposed through CARLO, and is deleted after CARLO has collected and
redacted the terminal log. If cleanup is delayed by an outage, the UI reports
that sensitive remote cleanup remains pending. CARLO's durable artifacts are
always redacted. Telegram receives concise queued, started, succeeded, failed,
cancelled, interrupted, and runner-offline events, not console content.

## Kill run

Kill run is available for queued and running actions and requires confirmation.
For a queued run, CARLO marks the run cancelled and every step skipped without
starting external work.

For a running command, the API records cancellation intent. The worker sends
`TERM` to the recorded local or remote process group, waits up to ten seconds,
then sends `KILL` if needed. The active step becomes cancelled and later steps
become skipped. The run remains cancelled even if cleanup subsequently fails.
Kill requests are idempotent and remain pending through temporary SSH outages.

## Restart and connection recovery

The wrapper's log, state, and process-group files make an action independent of
one SSH channel. The worker tails console output while connected. After a CARLO
restart it reconciles any running ActionRun:

- if the process is alive, resume tailing from the stored byte offset;
- if it completed, collect its final state and continue lifecycle handling;
- if the host is temporarily unreachable, keep the run active in a reconnecting
  stage and do not start the next action;
- if the process is absent without a terminal state, mark the run interrupted;
- if cancellation was requested, deliver it before any further queue work.

CARLO never automatically reruns a deployment after ambiguity or interruption.
The user may start a new run from the historical record.

## HTTP and realtime boundary

Add project-scoped endpoints to:

- list committed action definitions and catalog errors;
- list runs with pagination and filters;
- create a run after Git and runner preflight;
- read run and step details;
- fetch console output from a byte offset;
- request Kill run.

Add administrator endpoints to list, create, update, enable/disable, scan trust,
confirm trust, and test runners. APIs return domain views rather than exposing
database or subprocess internals.

All current endpoints remain protected by the administrator session. The domain
and schema retain project IDs so future project members can list and trigger
actions only for their memberships. Runner management and host trust always
remain administrator-only.

## User interface

The masthead gains two peer navigation buttons:

```text
[ BOARD ] [ ACTIONS ]
```

The Board remains the existing Kanban. Actions shows one card per project. Each
project card displays repository health, committed SHA, catalog errors, every
action, its runner, last result and duration, and a Run button. Projects without
a valid catalog explain how to add or fix `carlo-actions.yaml`.

Run always opens a confirmation dialog showing project, action, commit, runner,
dotenv path, and ordered commands. A dirty project disables confirmation and
shows the pending paths.

Selecting a run opens a side panel consistent with task detail. It contains
status, Git evidence, runner, step timeline, elapsed time, live console, raw-log
download of the same redacted artifact, and Kill run. Historical output uses the
same console component. Keyboard focus, escape/close behavior, reduced motion,
mobile full-screen detail, and clear queued/reconnecting/empty/error states are
required.

Manage runners is an admin dialog within Actions, not a third main navigation
section. It shows local and SSH runners, online state, last test, fingerprint,
and Add, Test, Edit, Disable, and Trust replacement actions. Secret key contents
never enter the browser.

## Failure behavior

- Missing or malformed YAML disables only that project's actions.
- Dirty Git state, missing dotenv, unavailable commit, runner disabled/offline,
  or failed host verification prevents external commands from starting.
- A missing executable is a normal failed step with exit code `127`.
- A non-zero command exit stops the action and preserves all evidence.
- WebSocket loss does not stop a run; the browser resumes by event sequence and
  console offset.
- Database persistence failure prevents the corresponding external side effect.
- Cleanup and notification failures do not rewrite a successful or failed run.
- Unknown remote state blocks the action queue rather than risking a second
  deployment.

## Verification

Backend tests cover:

- YAML schema, one-line command parsing, unknown fields, and path containment;
- clean/dirty repository gates and committed-definition snapshots;
- dotenv parsing, minimal environments, redaction, and absence of stored values;
- action lifecycle, step ordering, strict exit-code success, queue ordering, and
  the separate global advisory lock;
- local detached worktrees and cleanup;
- SSH argument construction, key-file checks, host scan/confirmation/change,
  exact-commit fetch, remote worktree preparation, and environment transfer;
- live output offsets, artifact replay, Kill run, TERM-to-KILL escalation, and
  idempotent cancellation;
- worker restart, SSH reconnect, completed-run collection, missing-process
  interruption, and queue blocking while remote state is unknown;
- administrator authorization and future project-scope boundaries;
- Telegram action event classification.

SSH integration tests use a disposable local SSH server or container and real
Git repositories; normal unit tests use a narrow transport fake. Tests never
run deployment commands or use the production database.

Frontend tests cover Board/Actions navigation, project grouping, invalid
catalogs, confirmation and dirty-state blocking, live console offset updates,
history, Kill run, and runner trust flows. Production type-check and build remain
mandatory.

An opt-in smoke test creates a temporary remote Git repository and SSH runner,
runs a multi-step action at an exact commit, observes streamed output, and
verifies history after worker restart.

## Deferred

- password-based SSH authentication;
- private-key upload, generation, or storage by CARLO;
- parallel action runs or per-runner concurrency;
- manual branch/ref selection and execution of dirty or unpushed source;
- automatic Git push, filesystem synchronization, or `rsync`;
- action inputs, schedules, approvals by environment, dependencies, matrices,
  retries, and continue-on-error;
- an installed remote CARLO daemon;
- Ansible-specific integration; projects may invoke `ansible-playbook` normally;
- deployment-specific semantics and first-class `act` workflow parsing;
- automatic retention policies and artifact deletion UI.
