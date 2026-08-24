# CARLO v3

**Coding Agent Runtime Lifetime Orchestrator** — slow but relentless.

CARLO plans coding goals through Pi, waits for user approval, then executes one
implementation globally until local validation passes or human input is needed.
PostgreSQL owns lifecycle state; Git owns source changes and checkpoints.

## Current vertical slice

- Git-backed projects and permanent IDs such as `CAR-1`;
- concurrent repository-aware Brief/Plan sessions through Pi using the bundled
  `carlo-planning` skill;
- persistent repository-aware Discoveries through Pi RPC, with complete chat
  and tool transcript, structured findings/decisions, restart recovery, and an
  atomic `MEMORY.md` projection;
- direct creation of one or many ordinary Tasks from Discovery proposals;
- focused planner questions for Tasks whose goal is not yet sufficiently clear;
- explicit Plan and Plan Amendment approval;
- one globally serialized implementation via PostgreSQL advisory lock;
- dedicated branch/worktree and validation-linked checkpoint commits;
- progress-aware retries, repeated-loop detection, GPT-profile escalation;
- restart recovery from persisted task, attempt, Git, and validation state;
- failed-task Rework from the original request, with fresh Pi planning and a
  clean numbered worktree while preserving prior evidence;
- HTTP API, persisted WebSocket event replay, six-column React Kanban, Actions,
  and a streaming desktop/mobile Discovery chat;
- installable PWA with standalone mode, explicit frontend updates, offline app
  shell, and network-only API/chat traffic;
- in-application administrator login with durable revocable sessions;
- restart-safe Telegram notifications for informational and blocking events;
- encrypted, CARLO-managed OpenAI-compatible model providers, synchronized
  catalogs, proportional Pi compaction, and per-session runtime snapshots.

For now, a task becomes **Done when every approved local validation command
passes**. Strong final review, merge into `carlo-Dev`, and integration pipelines
are intentionally deferred.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- PostgreSQL
- Node.js and npm
- Git
- the `pi` CLI; CARLO supplies model definitions and credentials to each session

## Local setup

```bash
createdb carlov3
createdb carlov3_test
cp .env.example .env
# Replace the key placeholder with: openssl rand -base64 32
set -a && source .env && set +a
make install
make migrate
make bootstrap-admin
```

The databases already exist on the original development host. `carlov3_test` is
destructive test-only storage; never point `CARLO_DATABASE_URL` at production.

After login, open **Settings → Models**, add each OpenAI-compatible endpoint,
and refresh its catalog. Under **Coding agents**, assign one concrete
`provider — model` pair to `plan`, `implementation`, `discovery`, and the other
profiles. There is no provider default; profiles without a concrete model
remain unconfigured.
Catalog models remain selectable when an OpenAI-compatible endpoint omits token
metadata: CARLO falls back to a 65,536-token context and 16,384-token output.
Per-model overrides are optional.

CARLO encrypts provider keys in PostgreSQL using
`CARLO_CREDENTIAL_ENCRYPTION_KEY`. Never rotate that key without first
re-encrypting the stored credentials. Pi receives only the selected model in an
isolated snapshot below `CARLO_ARTIFACT_ROOT/pi-runtime/<session-id>`; the API
key exists only in the child process environment. Personal
`~/.pi/agent/models.json` and `settings.json` do not affect managed sessions.
CARLO also keeps Superpowers, Ponytail, and Anthropic's `frontend-design`
repository below `CARLO_ARTIFACT_ROOT/pi-resources`, updates them weekly under
the same exclusive Pi maintenance lock, and sends one concise Telegram result.
Under **Coding agents → Default resources**, global packages and skills are
loaded by every profile and cannot be excluded locally; each profile may add
more managed resources. Each Task records the exact resource commits loaded by
its Pi session.

Use separate terminals:

```bash
set -a && source .env && set +a && make api
set -a && source .env && set +a && make worker
make ui
```

Open `http://localhost:5173`. Add a project whose repository already has a
committed `carlo-Dev` branch. The Task flow is:

```text
Create Task → live Pi planning → zero or more focused questions → Brief/Plan review → explicit approval → Ready
```

Task creation starts planning automatically. The half-screen Task panel shows
repository activity over WebSocket, renders Goal, Brief, and Plan as Markdown,
and can be resized from its left edge. Closing it does not cancel or delete the
Task; its state and planning activity remain available from the Board.

## Discoveries

Use **Discoveries** when the change is not clear yet. Pick a project and start a
conversation; Pi can inspect the repository, Git history, logs, and declared
diagnostic commands, but the bundled extension blocks source writes, commits,
destructive shell syntax, and undeclared commands. Discovery turns run beside
Task execution and Actions. While Pi works, the chat shows its current safe
repository activity—such as the file being read or command being run—without
exposing reasoning or raw tool output. The central chat and right-side Context
panel remain the primary desktop layout.

PostgreSQL preserves the full user/assistant/tool transcript and structured
state. Pi's resumable JSONL lives below `CARLO_ARTIFACT_ROOT/pi-sessions`; CARLO
also writes `CARLO_ARTIFACT_ROOT/discoveries/<id>/MEMORY.md` atomically. After a
restart, running turns return to the durable queue and reuse the same Pi session
identity. CARLO keeps at most three live Pi Discovery processes per project and
evicts only the least-recently-used idle process.

When the conversation has produced a concrete change, Pi prepares each Task's
self-contained megaprompt, Brief, Plan, structured phases, and validation
metadata while the repository context is still available. The Context panel
previews that handoff. **Create Ready task** or **Create all Ready tasks** is the
user's approval: CARLO persists the approved plan and queues the Task directly,
without repeating planning. Incomplete proposals cannot be created. Closing a
Discovery archives it read-only; it is not deleted.

The runtime uses [`skills/carlo-discovery`](skills/carlo-discovery/) for the
conversation contract. [`skills/carlo-ui-design`](skills/carlo-ui-design/)
stores CARLO's durable visual reference and UI rules for later Pi-driven work.

## Verification

```bash
set -a && source .env && set +a && make test
```

The suite uses real PostgreSQL advisory locks and temporary Git repositories.
Provider calls use deterministic fakes; the installed Pi boundary has a separate
smoke check because model access may require credentials and incur cost.

## VPN production setup

CARLO can serve the built React application itself, so Nginx is optional for
the initial HTTP-over-VPN deployment.

```bash
cp .env.production.example .env.production
```

Edit `.env.production` and replace every `CHANGE_ME`, `VPN_IP_OR_HOSTNAME`, and
`/ABSOLUTE/PATH`. `CARLO_APP_ORIGIN` must exactly match the URL opened in the
browser. Generate `CARLO_CREDENTIAL_ENCRYPTION_KEY` with
`openssl rand -base64 32`. Then install, migrate, bootstrap the only
administrator, and build:

```bash
set -a && source .env.production && set +a
make install
make migrate
make bootstrap-admin
make build
```

After bootstrap, remove the real value of `CARLO_BOOTSTRAP_ADMIN_PASSWORD` from
the populated file; CARLO never changes an existing administrator implicitly.

Run the API and worker as two separately supervised processes:

```bash
set -a && source .env.production && set +a && make prod-api
set -a && source .env.production && set +a && make worker
```

Open the configured VPN URL. The API, SPA, and WebSocket share the same origin.
When HTTPS is added, change the origin to `https://...` and set
`CARLO_COOKIE_SECURE=true`.

The built frontend is a PWA. On iPhone/iPad choose **Share → Add to Home
Screen**; on supported desktop browsers use the install action in the address
bar. Dynamic `/api` traffic, WebSockets, and Discovery chat are never served
from the service-worker cache. When a new frontend is available CARLO shows an
explicit **Update now** banner instead of refreshing during a conversation.

### macOS boot service

On macOS the complete production setup can instead be installed as two system
`LaunchDaemon` jobs. They run at boot as user `carlo`, even when nobody has
logged in. The installer uses an existing `carlo` account with home
`/Users/carlo`, or creates a hidden service account when it is absent:

```bash
sudo make install-mac
make status-mac
make logs-mac
```

On first install, `.env.production` is copied to
`/Users/carlo/.config/carlo/.env.production` with mode `600`. Later installs
preserve that file; edit it there and rerun `sudo make install-mac`. The command
installs dependencies, builds the UI, migrates PostgreSQL, bootstraps the admin,
and installs/starts the API and worker daemons. With the default local database
URL it also creates the PostgreSQL login `carlo`, assigns ownership of the
dedicated `carlov3` database to it, and applies migrations as that account.
Authenticated `localhost` and `127.0.0.1` URLs receive the same ownership
repair; the API, worker, and migrations still run as `carlo`, never as root.
If the encryption key is missing or still a placeholder, `install-mac`
generates it once in that protected file. Existing real keys are never changed.

```bash
make uninstall-mac
```

Uninstalling stops and removes only the daemon definitions. Application files,
configuration, PostgreSQL data, artifacts, and logs are preserved.

### Telegram

Create a bot with BotFather, send that bot one message, and put its token and
your single destination chat ID in `.env.production`. `CARLO_TELEGRAM_LEVEL=all`
sends informational and blocking events; use `blocking` for intervention-only
messages. Placeholder values disable Telegram cleanly. Delivery uses Telegram's
official HTTPS `sendMessage` API, is deduplicated across restarts, and never
blocks task execution.

## Runtime shape

```text
React PWA ──HTTP/WebSocket──> FastAPI ──SQLAlchemy──> PostgreSQL
                              │                      │
                              └── persisted events ──┘

async worker ──global DB lock──> one task ──> Git worktree
                                      ├─────> Pi provider + isolated model snapshot
                                      └─────> local validation

async worker ──Discovery queue──> Pi RPC sessions (max 3/project)
                                  ├─────> read-only repository tools
                                  └─────> transcript + MEMORY.md
```

WebSocket delivery is resumable by event sequence. On restart the worker first
looks for an In Progress task, reconciles its stored worktree/checkpoint/stage,
and resumes it before considering Ready tasks. A task blocked on a Plan Amendment
freezes the implementation queue until the user approves it.

Design and implementation decisions are recorded in [`docs/superpowers`](docs/superpowers/).
