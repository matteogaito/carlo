# CARLO v3

**Coding Agent Runtime Lifetime Orchestrator** — slow but relentless.

CARLO plans coding goals through Pi, waits for user approval, then executes one
implementation globally until local validation passes or human input is needed.
PostgreSQL owns lifecycle state; Git owns source changes and checkpoints.

## Current vertical slice

- Git-backed projects and permanent IDs such as `CAR-1`;
- concurrent repository-aware Brief/Plan sessions through Pi;
- explicit Plan and Plan Amendment approval;
- one globally serialized implementation via PostgreSQL advisory lock;
- dedicated branch/worktree and validation-linked checkpoint commits;
- progress-aware retries, repeated-loop detection, GPT-profile escalation;
- restart recovery from persisted task, attempt, Git, and validation state;
- HTTP API, persisted WebSocket event replay, and six-column React Kanban;
- configurable provider/model/effort/tool profiles.

For now, a task becomes **Done when every approved local validation command
passes**. Strong final review, merge into `carlo-Dev`, and integration pipelines
are intentionally deferred.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- PostgreSQL
- Node.js and npm
- Git
- the `pi` CLI, plus credentials/models configured for Pi

## Local setup

```bash
createdb carlov3
createdb carlov3_test
cp .env.example .env
set -a && source .env && set +a
make install
make migrate
```

The databases already exist on the original development host. `carlov3_test` is
destructive test-only storage; never point `CARLO_DATABASE_URL` at production.

Configure the four seeded profiles after starting the API. Model assignments are
data, not hardcoded domain behavior:

```bash
curl -X PATCH http://localhost:8000/api/agent-profiles/plan \
  -H 'content-type: application/json' \
  -d '{"model":"openai/gpt-5","effort":"high"}'

curl -X PATCH http://localhost:8000/api/agent-profiles/implementation \
  -H 'content-type: application/json' \
  -d '{"model":"your-kat-model","effort":"high"}'
```

Use separate terminals:

```bash
set -a && source .env && set +a && make api
set -a && source .env && set +a && make worker
make ui
```

Open `http://localhost:5173`. Add a project whose repository already has a
committed `carlo-Dev` branch, create a task, build its Brief/Plan, approve it, and
let the worker claim it.

## Verification

```bash
set -a && source .env && set +a && make test
```

The suite uses real PostgreSQL advisory locks and temporary Git repositories.
Provider calls use deterministic fakes; the installed Pi boundary has a separate
smoke check because model access may require credentials and incur cost.

## Runtime shape

```text
React ──HTTP/WebSocket──> FastAPI ──SQLAlchemy──> PostgreSQL
                              │                      │
                              └── persisted events ──┘

async worker ──global DB lock──> one task ──> Git worktree
                                      ├─────> Pi provider
                                      └─────> local validation
```

WebSocket delivery is resumable by event sequence. On restart the worker first
looks for an In Progress task, reconciles its stored worktree/checkpoint/stage,
and resumes it before considering Ready tasks. A task blocked on a Plan Amendment
freezes the implementation queue until the user approves it.

Design and implementation decisions are recorded in [`docs/superpowers`](docs/superpowers/).
