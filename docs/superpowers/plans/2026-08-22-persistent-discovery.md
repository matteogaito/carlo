# Persistent Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent repository-aware Discoveries, recoverable Pi RPC conversations, conversational Task planning, a mobile-first chat UI, and an installable PWA without changing the existing Task execution lifecycle.

**Architecture:** Extend the existing provider boundary with an RPC conversation session. PostgreSQL owns transcript and turn recovery, Pi session JSONL plus an atomic `MEMORY.md` own agent continuity, and the existing worker owns live processes with a three-per-project idle LRU cap. Discovery produces ordinary Task megaprompts; direct Task planning uses the same short-lived RPC machinery for clarification questions.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2, PostgreSQL, Alembic, asyncio subprocesses, Pi JSONL RPC, React 19, TypeScript 7, Vite 8, Vitest, React Testing Library, service worker APIs.

**Spec:** `docs/superpowers/specs/2026-08-22-persistent-discovery-design.md`

## Implementation status — 2026-08-23

Implemented on `main`: persistence and migration, shared Task creation, Pi RPC
provider, technical read-only guard, `carlo-discovery` skill, Discovery API and
Task handoff, restart-safe worker with three-per-project idle LRU, structured
memory/transcript, focused Task-planning questions, desktop/mobile chat UI,
oMLX-inspired `carlo-ui-design` skill, and installable PWA.

Verification: 5 Node contract checks, 87 backend tests, Alembic clean check, 8
frontend tests, Vite production build, and a real no-model Pi RPC startup smoke
that loaded the Discovery extension/tool. Automated browser control was not
available in this session, so no browser screenshot is claimed as validation.

## Global Constraints

- Discovery never enables Pi `edit` or `write` and never silently becomes implementation.
- Discovery work runs independently of the global implementation and action locks.
- At most three live Discovery RPC processes per project; never evict a streaming process.
- PostgreSQL transcript is append-only and is never replaced by compaction.
- `MEMORY.md` lives under CARLO artifacts, never in the target repository.
- Generated Tasks use the existing Task lifecycle and contain self-sufficient megaprompts.
- Existing Tasks, Plans, Actions, authentication, Telegram, and API payloads remain compatible.
- API/WebSocket/chat data is never cached by the PWA service worker.
- Preserve unrelated and pre-existing worktree changes; do not stage them into implementation checkpoints.

---

### Task 1: Persistent Discovery schema

**Files:**
- Modify: `backend/carlo/models.py`
- Create: `backend/alembic/versions/f6b7c8d9e0f1_persistent_discoveries.py`
- Modify: `backend/tests/test_persistence.py`
- Modify: `backend/tests/test_security_models.py`

**Interfaces:**
- Produces: `Discovery`, `DiscoveryMessage`, and `DiscoveryTurn` SQLAlchemy models.
- Produces: nullable `Event.discovery_id` for shared realtime/audit history.
- Produces: nullable Task planning session/cursor/current-question fields used by Task 7.
- Consumes: existing `Project`, `AgentProfile`, `Event`, and timestamp conventions.

- [ ] **Step 1: Write failing persistence tests**

Add a real-database test that inserts one project, Discovery, ordered transcript,
turn, and Discovery event, then loads them through a new session:

```python
discovery = Discovery(
    project=project,
    title="Vinted direction",
    status="OPEN",
    state={"summary": "", "findings": [], "decisions": [],
           "unresolved_questions": [], "inspected_resources": [],
           "commands": [], "proposals": []},
    provider_session_id="discovery-1",
    memory_path="/tmp/discoveries/1/MEMORY.md",
)
message = DiscoveryMessage(discovery=discovery, sequence=1, role="user", content="Explore Vinted")
turn = DiscoveryTurn(discovery=discovery, input_message=message, kind="CHAT", status="QUEUED")
event = Event(discovery=discovery, type="discovery.created", payload={})
```

Assert status, ordering, JSON state, nullable Task event compatibility, nullable
planning fields, and cascade deletion. The production break caught is missing
durable ownership or incorrect ordering/cascade constraints.

- [ ] **Step 2: Verify RED**

Run:

```bash
cd backend && uv run pytest tests/test_persistence.py tests/test_security_models.py -q
```

Expected: import/table failures because Discovery records do not exist.

- [ ] **Step 3: Add minimal models and migration**

Use string statuses with database checks rather than new PostgreSQL enum types:

```python
class Discovery(TimestampMixin, Base):
    __tablename__ = "discoveries"
    __table_args__ = (CheckConstraint("status IN ('OPEN', 'CLOSED')", name="ck_discoveries_status"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(10), default="OPEN", index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("agent_profiles.id"))
    provider_session_id: Mapped[str] = mapped_column(String(160), unique=True)
    session_path: Mapped[str | None] = mapped_column(Text)
    provider_cursor: Mapped[str | None] = mapped_column(String(160))
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    memory_path: Mapped[str] = mapped_column(Text)
    final_summary: Mapped[str | None] = mapped_column(Text)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

`DiscoveryMessage` has a unique `(discovery_id, sequence)` pair, roles limited
to `user/assistant/tool/system`, optional provider entry ID with a partial-safe
unique constraint, JSONB metadata, and ordered relationship. `DiscoveryTurn`
has a unique input-message reference, checked kind/status strings, cancellation,
attempt, error, and completion fields. Add `Event.discovery_id` plus relationship.

The Alembic migration creates these tables/indexes and adds the nullable event
column. Its downgrade removes only the new records and column.

- [ ] **Step 4: Verify GREEN and migration consistency**

Run:

```bash
cd backend && uv run pytest tests/test_persistence.py tests/test_security_models.py -q
cd backend && uv run alembic upgrade head
cd backend && uv run alembic check
```

Expected: tests pass and no new upgrade operations are detected.

- [ ] **Step 5: Checkpoint**

Record `git status --short` and keep the migration/model diff isolated from
pre-existing changes. Do not stage unrelated files.

---

### Task 2: Shared ordinary Task creation

**Files:**
- Create: `backend/carlo/tasks.py`
- Modify: `backend/carlo/api.py`
- Modify: `backend/tests/test_api.py`

**Interfaces:**
- Produces: `async create_task(session, project_id, title, goal, priority=0, created_source="web") -> Task`.
- Consumes: existing project sequence lock, prompt filename rules, `slug()`, and `task.created` event.
- Used later by: Discovery proposal confirmation API.

- [ ] **Step 1: Write a failing shared-creation test**

Move behavior assertions to the production helper boundary: two calls allocate
`ECA-1` and `ECA-2`, write distinct prompt files, and return ordinary
`NOT_READY/created` Tasks. Keep the existing endpoint test to prove it delegates
without changing its response.

The production break caught is a Discovery-only Task path that drifts from
Board Task identity or prompt behavior.

- [ ] **Step 2: Verify RED**

```bash
cd backend && uv run pytest tests/test_api.py -k 'task_creation' -q
```

Expected: failure because `carlo.tasks.create_task` does not exist.

- [ ] **Step 3: Extract the existing implementation without changing behavior**

```python
async def create_task(
    session: AsyncSession,
    project_id: int,
    title: str,
    goal: str,
    *,
    priority: int = 0,
    created_source: str = "web",
) -> Task:
    """Allocate identity, persist the normal prompt artifact, and flush the Task."""
```

The helper locks the Project, bounds UTF-8 content to 1 MiB, writes with mode
`x`, adds the Task plus event, commits, removes the prompt if commit fails, and
returns the Task. Preserve current HTTP error mapping by raising small typed
`TaskCreationError(status_code, detail)` values handled by the endpoint.

- [ ] **Step 4: Verify GREEN**

```bash
cd backend && uv run pytest tests/test_api.py -q
```

Expected: all API tests pass with unchanged response fields.

- [ ] **Step 5: Checkpoint**

Review `backend/carlo/api.py` to confirm there is one sequence/prompt creation
implementation.

---

### Task 3: Provider-neutral RPC conversation session

**Files:**
- Modify: `backend/carlo/provider.py`
- Modify: `backend/tests/fakes.py`
- Create: `backend/tests/fixtures/fake_pi_rpc.py`
- Modify: `backend/tests/test_provider.py`

**Interfaces:**
- Produces: `ConversationEvent`, `ConversationState`, and `ConversationSession` protocol.
- Produces: `CodingAgentProvider.open_conversation(...) -> ConversationSession`.
- Produces Pi implementation methods `prompt`, `abort`, `get_state`, `get_entries`, `compact`, and `close`.
- Keeps existing: `CodingAgentProvider.run()` and `PiProvider.run()` unchanged for current callers.

- [ ] **Step 1: Write failing protocol tests against a real fake subprocess**

The fixture speaks strict LF-delimited JSONL. It acknowledges `prompt`, emits
`agent_start`, assistant deltas, one tool event, `agent_end`, answers
`get_state/get_entries`, acknowledges `abort`, and exits on EOF.

Test that:

```python
session = await provider.open_conversation(
    profile, str(repo), "discovery-42", extensions=(guard,),
)
events = [event async for event in session.prompt("Inspect auth")]
assert [event.type for event in events][-1] == "agent_end"
assert (await session.get_state()).session_id == "discovery-42"
await session.close()
```

Also assert the spawned argv contains `--mode rpc`, stable session ID/directory,
model, effort, tools, skills, and extension; malformed JSON terminates with
`ProviderError`. This catches framing, command correlation, and lost session
configuration.

- [ ] **Step 2: Verify RED**

```bash
cd backend && uv run pytest tests/test_provider.py -q
```

Expected: missing conversation API.

- [ ] **Step 3: Implement the minimal RPC client**

Use `asyncio.create_subprocess_exec` with pipes. `PiRpcSession` owns:

```python
class ConversationSession(Protocol):
    session_id: str
    async def prompt(self, message: str) -> AsyncIterator[ConversationEvent]: ...
    async def abort(self) -> None: ...
    async def get_state(self) -> ConversationState: ...
    async def get_entries(self, since: str | None = None) -> tuple[list[dict[str, Any]], str | None]: ...
    async def compact(self, instructions: str) -> str: ...
    async def close(self) -> None: ...
```

Read bytes with `readuntil(b"\n")`, strip only a trailing `\r`, decode JSON,
route `response` objects by request ID, and yield agent events for the active
prompt until `agent_end`. Serialize commands through one lock. EOF or stderr
failure fails pending requests and removes the process from `PiProvider`.

- [ ] **Step 4: Verify GREEN and old provider compatibility**

```bash
cd backend && uv run pytest tests/test_provider.py tests/test_end_to_end.py -q
```

Expected: RPC tests and existing one-shot flow pass.

- [ ] **Step 5: Checkpoint**

Confirm existing one-shot command snapshots did not gain RPC-only flags.

---

### Task 4: Discovery skill and Pi technical guard

**Files:**
- Create: `skills/carlo-discovery/SKILL.md`
- Create: `extensions/discovery-policy.mjs`
- Create: `extensions/carlo-discovery-guard.mjs`
- Create: `extensions/discovery-policy.test.mjs`
- Modify: `Makefile`

**Interfaces:**
- Produces: Pi extension loaded explicitly for Discovery sessions.
- Produces: `allowedDiscoveryCommand(command, declaredCommands) -> boolean`.
- Produces: `discovery_state` tool arguments matching the structured state contract.
- Consumes: declared project validation commands supplied through `CARLO_DISCOVERY_COMMANDS` JSON.

- [ ] **Step 1: Write failing Node policy tests**

Use `node:test` and literal cases:

```javascript
assert.equal(allowedDiscoveryCommand("git status --short", []), true)
assert.equal(allowedDiscoveryCommand("git reset --hard", []), false)
assert.equal(allowedDiscoveryCommand("pytest -q", []), true)
assert.equal(allowedDiscoveryCommand("make verify", ["make verify"]), true)
assert.equal(allowedDiscoveryCommand("pytest -q > result", []), false)
assert.equal(allowedDiscoveryCommand("python -c 'open(\"x\",\"w\")'", []), false)
```

The break caught is any policy regression that lets a mutation route through
the only mutable built-in tool.

- [ ] **Step 2: Verify RED**

```bash
node --test extensions/discovery-policy.test.mjs
```

Expected: module-not-found failure.

- [ ] **Step 3: Implement guard and skill**

Reject shell control syntax before prefix matching. Permit exact declared
commands plus documented safe families: Git `status/log/diff/show/rev-parse`,
test/lint/build commands for uv/pytest/npm/pnpm/yarn/go/cargo/make, and process
inspection without kill operations.

The extension intercepts `tool_call`: block `edit/write`; validate `bash`; and
register `discovery_state` with a strict schema containing full summary,
findings, decisions, unresolved questions, inspected resources, commands, and
proposals. Tool execution returns a short acknowledgement; CARLO obtains the
arguments from streamed tool events.

The skill defines evidence discipline, memory updates, conversational Markdown,
focused questions, and complete megaprompt content. It never instructs Pi to
modify source.

- [ ] **Step 4: Verify GREEN and add it to the default suite**

```bash
node --test extensions/discovery-policy.test.mjs
make test
```

Add the Node command to `make test` before frontend tests. No npm dependency is
needed for policy tests.

- [ ] **Step 5: Checkpoint**

Manually inspect the extension allowlist for general interpreters, redirects,
Git mutation, and subprocess escape hatches; none may be present.

---

### Task 5: Discovery API and proposal-to-Task flow

**Files:**
- Create: `backend/carlo/discovery.py`
- Modify: `backend/carlo/api.py`
- Modify: `backend/tests/test_api.py`
- Create: `backend/tests/test_discovery_api.py`

**Interfaces:**
- Produces request models for create/message/title/proposal actions.
- Produces `_discovery_view()` and `_discovery_message_view()`.
- Produces endpoints under `/api/discoveries`.
- Consumes `carlo.tasks.create_task()` for direct real Task creation.

- [ ] **Step 1: Write failing API lifecycle tests**

Against the real FastAPI app and PostgreSQL, cover separately:

- create from `project_id + first_message` returns `OPEN`, generated title, user
  message, and queued turn;
- list filters open/closed and project;
- append message queues a new turn only when no unconsumed duplicate exists;
- close queues a `CLOSE` turn and does not erase transcript;
- closed detail remains readable and mutations return 409;
- proposal confirmation creates one normal Task;
- create-all creates N Tasks in proposal dependency order and is idempotent.

Proposal test metadata uses a literal assistant message payload and asserts
actual Tasks/prompt files, not a mocked creation method.

- [ ] **Step 2: Verify RED**

```bash
cd backend && uv run pytest tests/test_discovery_api.py -q
```

Expected: 404 for missing routes.

- [ ] **Step 3: Implement minimal API/domain helpers**

`new_discovery_state()` returns the one canonical empty state. Generate the
title from the first non-empty line, normalized and bounded to 80 characters;
no LLM call.

Endpoints:

```text
POST   /api/discoveries
GET    /api/discoveries
GET    /api/discoveries/{id}
PATCH  /api/discoveries/{id}
POST   /api/discoveries/{id}/messages
POST   /api/discoveries/{id}/stop
POST   /api/discoveries/{id}/close
POST   /api/discoveries/{id}/proposals/{proposal_id}/tasks
POST   /api/discoveries/{id}/proposals/tasks
```

Creation writes the first user transcript row and queued turn in one
transaction. Proposal endpoints lock the assistant message, validate the
proposal is current and uncreated, call shared Task creation, and store created
Task IDs in message metadata. Emit Discovery events through the existing Event
table.

- [ ] **Step 4: Verify GREEN**

```bash
cd backend && uv run pytest tests/test_discovery_api.py tests/test_api.py -q
```

- [ ] **Step 5: Checkpoint**

Inspect route ordering so `/api/tasks/{task_id}` and SPA fallback behavior stay
unchanged.

---

### Task 6: Worker runtime, memory, LRU, streaming, and recovery

**Files:**
- Create: `backend/carlo/discovery_runtime.py`
- Modify: `backend/carlo/worker.py`
- Modify: `backend/tests/fakes.py`
- Create: `backend/tests/test_discovery_runtime.py`

**Interfaces:**
- Produces: `DiscoveryRuntime.run_forever()` and `close()`.
- Produces: `DiscoverySessionPool.acquire(project_id, discovery_id, ...)` with cap 3.
- Produces: `write_memory(path, state)` atomic Markdown projection.
- Consumes: conversation provider, Discovery rows/turns/messages/events, project validation commands.

- [ ] **Step 1: Write failing runtime behavior tests**

Use a provider fake that emits real conversation events and records live
session opens/closes. Separate tests catch:

1. queued message becomes running/completed with streamed assistant transcript;
2. `discovery_state` arguments update JSON state and exact `MEMORY.md` sections;
3. tool calls become collapsed tool transcript entries with command/output;
4. fourth session closes the least-recent idle session but never an active one;
5. cancellation calls abort and leaves the Discovery open;
6. worker restart reconciles provider entry IDs without duplicate messages;
7. a close turn persists final summary, marks `CLOSED`, and closes the process;
8. a failed session remains resumable by the next queued message.

The recovery test uses two runtime instances over one database and one session
artifact directory; it is not a mock call-count test.

- [ ] **Step 2: Verify RED**

```bash
cd backend && uv run pytest tests/test_discovery_runtime.py -q
```

Expected: missing runtime.

- [ ] **Step 3: Implement session pool and turn dispatcher**

The runtime polls queued/recoverable turns, claims with `FOR UPDATE SKIP LOCKED`,
and launches one asyncio Task per Discovery. A keyed in-memory set prevents two
local turns for the same Discovery. The pool tracks project, last-use monotonic
time, busy flag, and conversation session. On acquire at cap it closes the
oldest idle entry or waits on a condition until one becomes idle.

- [ ] **Step 4: Persist events and atomic memory**

Coalesce assistant deltas in memory and periodically emit lightweight realtime
events; persist one final assistant row. Persist tool completion rows, bounded
inline output, and artifact path for oversized output. Apply the latest valid
`discovery_state` call as a full replacement of validated state.

Memory writing uses only stdlib:

```python
temporary = path.with_suffix(".tmp")
temporary.write_text(render_memory(state), encoding="utf-8")
temporary.replace(path)
```

Ensure directory/path resolution remains inside the configured Discovery
artifact root.

- [ ] **Step 5: Implement recovery and cancellation**

At startup change orphaned `RUNNING` turns to a recoverable claim state, open
the stored session, call `get_entries(since=cursor)`, and insert unseen entry
IDs idempotently. Complete recovered `agent_end`; otherwise send one recovery
prompt containing memory plus the pending input. Observe `cancel_requested_at`
during streaming and call RPC abort.

- [ ] **Step 6: Wire the worker and verify GREEN**

Start one `DiscoveryRuntime.run_forever()` background task beside action,
Telegram, maintenance, and implementation loops; cancel and close it during
worker shutdown.

```bash
cd backend && uv run pytest tests/test_discovery_runtime.py tests/test_executor.py tests/test_action_runner.py -q
```

- [ ] **Step 7: Checkpoint**

Verify the Discovery runtime never acquires `IMPLEMENTATION_LOCK` or the action
lock and all process cleanup paths close pipes/tasks.

---

### Task 7: Conversational direct-Task planning

**Files:**
- Modify: `skills/carlo-planning/SKILL.md`
- Modify: `backend/carlo/api.py`
- Modify: `backend/tests/test_api.py`
- Modify: `backend/tests/test_provider.py`

**Interfaces:**
- Produces: planning output discriminated by `outcome: QUESTION | PLAN_READY`.
- Produces: `POST /api/tasks/{task_id}/planning-answer`.
- Adds nullable Task planning session/cursor/current-question fields.
- Keeps existing `POST /api/tasks/{task_id}/plan` and approval contract.

- [ ] **Step 1: Write failing question/answer flow tests**

Configure a conversation provider sequence: first response returns a literal
`QUESTION`, second returns `PLAN_READY`. Assert:

- first planning call leaves Task `NOT_READY/planning` and returns one question;
- answer is persisted as Task event and resumes the same provider session ID;
- second response creates Plan revision 1 and transitions to
  `NOT_READY/awaiting_approval`;
- an already sufficient Task goes directly to `PLAN_READY`;
- existing plan approval still reaches `READY/queued`.

- [ ] **Step 2: Verify RED**

```bash
cd backend && uv run pytest tests/test_api.py -k 'planning_question or plan' -q
```

- [ ] **Step 3: Extend the planning contract and API**

The planning instruction requires exactly one JSON object:

```json
{"outcome":"QUESTION","question":"...","reason":"...","evidence":["path:symbol"]}
```

or:

```json
{"outcome":"PLAN_READY","brief_markdown":"...","plan_markdown":"...","metadata":{}}
```

Use a temporary RPC conversation session with stable ID `<TASK-ID>-plan`; close
the process after each question or completed plan while retaining Pi's session
file. Persist question/answer events and expose them as `planning_messages` in
Task detail. Clear current question only after accepting an answer.

- [ ] **Step 4: Verify GREEN and backward compatibility**

```bash
cd backend && uv run pytest tests/test_api.py tests/test_end_to_end.py tests/test_executor.py -q
```

- [ ] **Step 5: Checkpoint**

Confirm implementation workers still consume only approved `PlanRevision` and
never read planning conversation events directly.

---

### Task 8: Discovery frontend and mobile-first navigation

**Files:**
- Create: `frontend/src/DiscoveriesView.tsx`
- Create: `frontend/src/Markdown.tsx`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`

**Interfaces:**
- Produces Discovery/Message/Turn/state/proposal TypeScript types and API methods.
- Produces `DiscoveriesView` driven by existing WebSocket events.
- Produces Task planning question composer in `TaskDetail`.
- Consumes: `react-markdown` with raw HTML disabled.

- [ ] **Step 1: Write failing user-flow tests**

Add behavior tests for:

- primary `Discoveries` navigation and first-message creation;
- selecting an open/closed conversation and rendering transcript;
- sending a message, showing streaming status, and stopping;
- expanding command output;
- creating one and all proposal Tasks;
- continuing to message after Task creation;
- closing and read-only consultation;
- answering a Task planning question;
- mobile context button and accessible sheet/dialog semantics.

Assert visible user outcomes and real component state; mock only HTTP/WebSocket
at the existing `Api` boundary.

- [ ] **Step 2: Verify RED**

```bash
cd frontend && npm test -- --run src/App.test.tsx
```

Expected: Discoveries navigation missing.

- [ ] **Step 3: Add API types and Markdown rendering**

Extend `Event` with `discovery_id`. Add Api methods matching backend routes.
Install `react-markdown` only; render code with a scrollable `<pre><code>` and
do not enable raw HTML or plugin ecosystems.

- [ ] **Step 4: Build the mobile-first Discovery view**

Base CSS is the phone layout: fullscreen chat, 44px controls, sticky compact
header, composer above `env(safe-area-inset-bottom)`, bottom navigation, and
full-screen transcript/context sheets. At `min-width: 901px`, switch to the
three-column list/chat/context workspace.

Reuse existing CARLO colors and typography. Add the evidence rail only to
assistant messages containing tool, decision, or proposal metadata. Command
outputs use native `<details>` for collapse and accessibility.

Persist last Discovery ID in `localStorage`; ignore missing/closed IDs safely.

- [ ] **Step 5: Add Task planning answer UI**

Task detail shows the focused question, reason/evidence, previous planning
messages, and one textarea/button. After answer, show planning activity until
the next question or Plan arrives.

- [ ] **Step 6: Verify GREEN and build**

```bash
cd frontend && npm test
cd frontend && npm run build
```

- [ ] **Step 7: Checkpoint**

Inspect at 390px and 1280px: no horizontal page overflow, code scroll stays
inside messages, composer remains reachable, and desktop chat remains primary.

---

### Task 9: Installable PWA with safe caching and updates

**Files:**
- Create: `frontend/public/manifest.webmanifest`
- Create: `frontend/public/sw.js`
- Create: `frontend/public/offline.html`
- Create: `frontend/public/icons/carlo-192.png`
- Create: `frontend/public/icons/carlo-512.png`
- Create: `frontend/public/icons/apple-touch-icon.png`
- Create: `frontend/src/pwa.ts`
- Create: `frontend/src/pwa.test.ts`
- Modify: `frontend/src/main.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/index.html`
- Modify: `frontend/vite.config.ts`

**Interfaces:**
- Produces `registerPwa(onUpdate) -> cleanup` and update activation callback.
- Produces manifest/install metadata and versioned service worker.
- Never intercepts `/api/*` or WebSocket traffic.

- [ ] **Step 1: Write failing PWA behavior tests**

Test registration with a fake `navigator.serviceWorker`: waiting worker triggers
the update callback, activation posts `SKIP_WAITING`, and controller change
reloads once. Add a production-build contract test that reads built manifest
and service worker and asserts API bypass plus navigation fallback behavior.

- [ ] **Step 2: Verify RED**

```bash
cd frontend && npm test -- --run src/pwa.test.ts
```

- [ ] **Step 3: Implement manifest, icons, and registration**

Generate simple CARLO pine/brass icons in 192, 512, and 180 sizes. Add manifest,
Apple metadata, `viewport-fit=cover`, and registration only in production.

`registerPwa` listens for `updatefound`, waiting/install state, and
`controllerchange`. App displays one `Update available` banner action.

- [ ] **Step 4: Implement conservative service worker**

Use versioned shell and runtime caches. Installation caches offline shell and
manifest. Fetch handler returns immediately for non-GET, `/api/`, and requests
whose upgrade header is `websocket`. Hashed `/assets/` use cache-first;
navigation uses network-first then cached shell/offline page. Activate deletes
old CARLO caches. Push and notification-click handlers are safe placeholders.

- [ ] **Step 5: Verify GREEN and production artifacts**

```bash
cd frontend && npm test
cd frontend && npm run build
test -f frontend/dist/manifest.webmanifest
test -f frontend/dist/sw.js
```

- [ ] **Step 6: Checkpoint**

Review `sw.js`: no response from `/api` can enter any cache, and update cache
names differ when the service worker changes.

---

### Task 10: Recovery, browser smoke, documentation, and full regression

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `.env.production.example`
- Modify: `backend/tests/test_end_to_end.py`
- Modify: `backend/tests/test_production_flow.py`

**Interfaces:**
- Documents Discovery artifacts, process cap, Pi requirements, PWA update, and recovery.
- Verifies one complete real CARLO vertical slice without replacing focused tests.

- [ ] **Step 1: Write the failing end-to-end test**

Create project and Discovery through API, run a fake provider turn with one
repository tool and proposal, create two real Tasks, continue the Discovery,
close it, reconstruct the app/session factory, and assert the closed transcript,
memory, Tasks, prompt files, and events remain readable.

The production breaks caught are cross-layer wiring loss and restart state that
focused unit tests cannot see.

- [ ] **Step 2: Verify RED**

```bash
cd backend && uv run pytest tests/test_end_to_end.py -k discovery -q
```

- [ ] **Step 3: Complete wiring and documentation**

Document:

- `discovery` Agent Profile defaults to the current `plan` profile settings;
- artifact/session/memory paths;
- safe command policy and project validation commands;
- maximum three live Discovery sessions per project;
- worker/Pi restart behavior;
- mobile/PWA installation and update behavior;
- deliberate exclusions: reopen/delete, mutation, Web Push subscriptions.

Add environment values only if runtime truly needs them; keep the process cap a
code default of three rather than speculative configuration UI.

- [ ] **Step 4: Verify the complete system**

Run:

```bash
make test
git -c safe.directory=/Users/Shared/Projects/codex/carlov3 diff --check
/bin/bash -n scripts/install-mac.sh scripts/run-mac-service.sh
```

Expected: backend, Alembic, extension policy, frontend tests, and production
build all pass with no whitespace or shell syntax errors.

- [ ] **Step 5: Browser smoke**

Run CARLO against the local database and production frontend. At desktop and
390px viewport verify login, create/open Discovery, send/stream/stop, context
sheet, proposal confirmation, direct Task planning question, close/read-only,
service-worker update, and offline-shell display. Inspect console and failed
network requests.

- [ ] **Step 6: Final review**

Review the complete diff against every section of the spec. Confirm no Task
execution behavior, global lock, Telegram notification, action runner, or auth
boundary was weakened. Report any Pi credential-dependent smoke check honestly
as `PARTIALLY_VERIFIED` rather than claiming it ran.
