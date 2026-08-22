# Persistent Discovery and conversational planning

## Goal

Add a persistent, repository-aware Discovery workspace alongside CARLO's
existing Task board and Actions workspace.

The product distinction is:

```text
Discovery = understand
Task      = change
```

A Discovery is a long-lived conversation with a strong Pi coding agent. It can
inspect a project, run controlled diagnostics, retain knowledge, and prepare
one or more complete Task megaprompts. It does not modify source code. Users who
already know the desired change can still create a Task directly; its planner
becomes conversational only when important information is missing.

This feature preserves existing Task identities, lifecycle, execution queue,
Git behavior, Actions, authentication, Telegram notifications, and API clients.

## Product flows

### Discovery flow

1. The user opens Discoveries and selects a project.
2. The user starts a conversation. The first message automatically supplies a
   short editable title; creating an empty Discovery is unnecessary.
3. CARLO starts or resumes the Discovery's Pi RPC session.
4. Pi explores the repository, performs safe diagnostics, discusses findings,
   and incrementally updates structured state plus `MEMORY.md`.
5. When concrete changes emerge, Pi asks only for decisions it cannot resolve
   from the repository or conversation.
6. Pi presents one or more Task proposals. Each contains a title, a complete
   megaprompt, and optional dependencies on other proposals.
7. The user may continue the conversation to revise proposals, create one
   proposal, or create the complete group.
8. CARLO creates ordinary Tasks through the existing Task creation path. The
   Discovery remains `OPEN` and may later produce more Tasks.
9. The user explicitly closes the Discovery. Pi generates a short final
   summary, CARLO persists it, shuts down the session, and marks the Discovery
   `CLOSED` and read-only.

There is no Task candidate domain object or intermediate Kanban state. Proposal
data is part of the assistant message that presents it. Created Tasks do not
require an origin-Discovery foreign key; their megaprompts are self-contained.

### Direct Task flow

Direct Task creation remains available from the Board.

1. The user supplies a title and megaprompt as text or Markdown upload.
2. The user starts planning.
3. Pi inspects the repository before deciding whether clarification is needed.
4. If the goal is sufficiently defined, Pi produces the Brief and Plan.
5. If a high-impact decision is missing, Pi asks one focused question in the
   Task detail view. User answers are persisted and planning continues in the
   same Pi RPC session.
6. The Task remains `Not Ready` until a Plan revision is produced and approved.

The planner does not ask about local details it can determine from repository
evidence. Existing Tasks without a conversational planning session continue to
work and may start one when planning resumes.

## Runtime architecture

Extend the existing `CodingAgentProvider` abstraction with a conversational
session contract alongside the current one-shot `run()` operation. The Pi
provider implements this contract using Pi's documented JSONL RPC mode over
stdin/stdout.

The contract supports:

- opening or resuming a named persisted session;
- sending a prompt to an idle session;
- receiving streamed provider events;
- aborting the active turn;
- compacting and inspecting session state;
- closing the process without deleting its persisted session.

Planning, implementation, escalation, and review may keep using the existing
one-shot provider call where conversational behavior is unnecessary. Discovery
and clarification-stage Task planning use the RPC contract. Provider-neutral
domain records contain no Pi-specific internal state beyond an opaque provider
session ID, session artifact reference, and provider cursor.

### Process ownership and concurrency

The CARLO worker owns live RPC processes. Discovery work is independent of the
globally serialized implementation queue and the action queue.

- Discoveries may run while Tasks and Actions run.
- Different Discoveries may respond concurrently.
- A Discovery has at most one active turn; later user messages are queued in
  order.
- Each project may have at most three live Discovery RPC processes.
- When a fourth session needs to start, the least recently active idle process
  is closed gracefully after Pi has persisted its session.
- A streaming process is never killed to satisfy the cap. The new session waits
  until a process becomes idle.
- There is no arbitrary idle timeout in the first version.
- Task-planning conversations reuse the RPC machinery but do not count against
  the per-project Discovery limit because they are short-lived and tied to an
  explicit planning action.

The live-process registry is intentionally in memory. PostgreSQL and session
artifacts remain authoritative; rebuilding the registry after restart is safe.

## Persistent model

### Discovery

A Discovery stores:

- generated identifier and project foreign key;
- editable title;
- status `OPEN` or `CLOSED`;
- selected Agent Profile foreign key;
- opaque provider session ID and session artifact path;
- provider entry cursor;
- structured state as JSONB;
- generated `MEMORY.md` path;
- final summary;
- created, updated, last-active, and closed timestamps.

Structured state contains the current summary, findings, decisions, unresolved
questions, inspected resources, command history, and current Task proposals.
The shape is validated at the API/runtime boundary and remains small enough to
render without reading the full transcript.

### DiscoveryMessage

The append-only transcript stores ordered messages with:

- Discovery and monotonically increasing sequence;
- role `user`, `assistant`, `tool`, or `system`;
- Markdown/text content;
- provider entry ID where available;
- typed metadata for tool calls, command completion, proposal presentation,
  interruption, and errors;
- creation timestamp.

The transcript is never replaced by compaction. Assistant text deltas are
coalesced before persistence instead of creating one database row per token.
Tool output is persisted as a completed transcript entry and may be mirrored to
an artifact when it exceeds the bounded inline size.

### DiscoveryTurn

Each user or lifecycle request has a durable execution record:

- Discovery and input message;
- kind `CHAT` or `CLOSE`;
- status `QUEUED`, `RUNNING`, `COMPLETED`, `INTERRUPTED`, or `FAILED`;
- provider request/cursor evidence;
- cancellation request and attempt timestamps;
- concise error and completion timestamps.

Queue and recovery decisions use these records rather than process memory.

### Events

The existing persisted event stream gains an optional `discovery_id` foreign
key. Discovery lifecycle, turn, tool, proposal, Task creation, recovery, and
closure events use the same WebSocket replay channel as existing Task and
Action events. Existing nullable `task_id` semantics remain compatible.

## Memory and compaction

The complete transcript lives in PostgreSQL. CARLO also writes an agent-facing
projection to:

```text
<CARLO_ARTIFACT_ROOT>/discoveries/<DISCOVERY-ID>/MEMORY.md
```

`MEMORY.md` contains:

- current summary;
- findings with repository evidence;
- decisions and rationale;
- unresolved questions;
- inspected files/symbols;
- diagnostic commands and outcomes;
- current Task proposals and dependencies.

The file is written to a sibling temporary file and atomically replaced after
each completed turn. It never lives inside the target project repository.

Pi auto-compaction remains enabled. CARLO also reads RPC session statistics and
may request Pi compaction when context usage crosses Pi's reported safe limit.
Before or after compaction, CARLO retains the original transcript and refreshes
structured state plus `MEMORY.md`. A resumed or reconstructed session always
receives the current memory and the pending user input.

## Restart and failure recovery

Recovery is automatic:

1. On worker startup, CARLO finds Discovery turns left `RUNNING`.
2. It opens the stored Pi session and reads entries after the persisted cursor.
3. Newly persisted Pi entries are reconciled into transcript rows by stable
   provider entry ID.
4. If the requested turn completed, CARLO finalizes it without prompting again.
5. If it was incomplete but resumable, CARLO sends a recovery instruction with
   `MEMORY.md`, the pending input, and the last stable entry evidence.
6. If the session file is missing or invalid, CARLO creates a replacement
   session from `MEMORY.md`, unresolved questions, and recent transcript
   messages. The original transcript remains untouched.

Unique constraints on provider entry IDs and turn completion make reconciliation
idempotent. A provider crash triggers one immediate process restart; Pi's own
auto-retry handles transient model errors. Repeated failure marks the turn
`FAILED`, keeps the Discovery `OPEN`, and reports a recoverable error to the UI.

Stopping a turn sends RPC `abort`, preserves any partial assistant output as an
interrupted transcript entry, marks the turn `INTERRUPTED`, and leaves the
Discovery available for the next message.

## Discovery skill and technical guard

Add a versioned `skills/carlo-discovery/SKILL.md`. It instructs Pi to:

- understand before proposing change;
- inspect repository evidence rather than guessing;
- distinguish findings, decisions, assumptions, and open questions;
- ask only questions that block a useful Task megaprompt;
- avoid turning every observation into a Task;
- prepare implementation-oriented megaprompts containing goal, scope, findings,
  decisions, evidence, relevant files, acceptance criteria, dependencies, and
  useful validation commands;
- update structured memory every completed turn;
- respond conversationally in Markdown after updating state;
- never claim a command or inspection occurred when it did not.

Add a Pi extension loaded explicitly for Discovery sessions. It:

- blocks built-in `edit` and `write` tools by excluding them from the session;
- intercepts `bash` tool calls and rejects destructive commands, shell
  redirection, command substitution, backgrounding, unsafe chaining, and Git
  mutating subcommands;
- permits read-only Git inspection and a narrow diagnostic command catalog;
- permits project-declared test, lint, typecheck, and build commands;
- exposes a `discovery_state` tool whose validated arguments carry the complete
  updated structured state and optional Task proposals;
- emits enough tool events for CARLO to persist commands, files, outcomes, and
  proposal metadata.

Allowed diagnostics may create ordinary ignored caches or build products, but
may not intentionally modify tracked source. CARLO compares Git status around
diagnostic commands and stops the turn with a policy violation if tracked state
changes. Explicit source mutation belongs in a Task, never an implicit
Discovery mode switch.

## Task proposal contract

The `discovery_state` tool accepts zero or more proposals. Each proposal has:

- stable ID scoped to the assistant message;
- Task title;
- complete Markdown megaprompt;
- optional proposal IDs it depends on.

The UI renders proposal cards below the assistant message. The user revises
them by continuing the conversation. `Create task` creates one proposal;
`Create all` creates the current uncreated proposals in dependency order.

Creation calls a shared backend Task creation function also used by the Board
endpoint. It allocates normal Jira-like IDs, writes normal prompt files, emits
normal Task events, and leaves Tasks `Not Ready`. A created proposal is marked
in message metadata so repeated confirmation is idempotent. Partial batch
failure reports exactly which Tasks were created and leaves remaining proposals
available; it never silently duplicates completed creation.

## Conversational Task planning

The planning Agent Profile remains `plan`, using the existing
`carlo-planning` skill and strong configured model. Planning gains a persisted
conversation tied to the Task without exposing Discovery UI concepts.

The planning skill/output contract gains two valid outcomes:

1. `QUESTION`: one focused question plus the repository evidence that makes the
   decision relevant;
2. `PLAN_READY`: the existing Brief, Plan, and structured metadata contract.

CARLO persists questions and user answers, resumes the same RPC planning
session, and creates `PlanRevision` only for `PLAN_READY`. Existing approval and
Ready transitions remain unchanged. Provider failure keeps the Task recoverable
in `Not Ready`; old Tasks and existing Plan revisions require no conversion.

## API

Add authenticated administrator endpoints for:

- listing Discoveries with project/status filters;
- creating a Discovery from project plus first message;
- reading Discovery detail and paginated transcript;
- posting a message;
- stopping the current turn;
- creating one or all Tasks from a proposal message;
- closing a Discovery;
- updating the title;
- posting an answer to an outstanding Task planning question.

Closed Discoveries remain readable. Message posting, proposal creation, title
changes, and repeated close are rejected after closure. There is no delete or
reopen endpoint in the initial version.

Request validation bounds title, message, proposal, and inline tool-output
sizes. Repository paths are always taken from persisted Projects, never from
Discovery request payloads.

## Realtime behavior

The existing WebSocket remains the single frontend realtime channel. Discovery
events include lifecycle state, current turn, assistant text deltas, tool start
and completion, state updates, proposal availability, failures, and Task
creation.

The browser applies assistant deltas locally and refreshes authoritative detail
at completion or reconnect. Reconnect uses the existing event sequence replay;
the final transcript remains authoritative if transient deltas were missed.
Dynamic chat responses are never cached by the service worker.

## User interface

Add `Discoveries` to the primary navigation without changing the Task Board or
Actions behavior.

### Desktop

```text
┌──────────────┬────────────────────────────────────┬──────────────────┐
│ Discoveries  │ Project / title / OPEN             │ Context          │
│              │                                    │ Findings         │
│ ● current    │ User and assistant conversation    │ Decisions        │
│ ○ older      │ Tool output collapsed              │ Open questions   │
│ □ closed     │ Task proposal cards                │ Files / commands │
│              │                                    │ Created tasks    │
│ + New        │ Message composer                   │                  │
└──────────────┴────────────────────────────────────┴──────────────────┘
```

The conversation is the widest and primary region. The context panel displays
structured state and does not duplicate the transcript. Command output is
collapsed by default. Closing and stopping are explicit, separate actions.

### Mobile

Mobile is a distinct layout rather than a compressed desktop grid:

- authenticated launch restores the last opened Discovery when available;
- a bottom navigation switches between Tasks, Discoveries, and Actions;
- Discovery opens as a fullscreen chat with a compact sticky status header;
- the composer remains above the safe-area inset and reachable when the
  software keyboard is visible;
- transcript list and context become separate full-screen sheets;
- tool output uses expandable blocks;
- code blocks scroll horizontally without widening the viewport;
- controls have at least 44-pixel touch targets;
- streaming respects reduced-motion and maintains the user's scroll position;
- closed Discoveries show a read-only composer replacement and final summary.

The existing CARLO pine/brass operational identity remains. The memorable
Discovery signature is a narrow evidence rail beside assistant messages that
marks repository reads, commands, decisions, and Task proposals without making
the chat resemble a terminal.

Use `react-markdown` for safe basic Markdown rendering rather than maintaining a
custom parser. Raw HTML remains disabled. No syntax-highlighting dependency is
needed initially; readable monospaced fenced blocks and horizontal scrolling
meet the first slice.

## Progressive Web App

Implement the PWA without a Vite PWA plugin:

- a standards-compliant manifest with CARLO name, short name, standalone
  display, theme/background colors, start URL, and generated 192/512 icons;
- Apple touch icon, mobile-web-app metadata, and safe-area viewport support;
- a small versioned service worker registered by the frontend;
- cache-first for immutable hashed assets;
- network-first for navigation with cached application-shell fallback;
- no service-worker caching for `/api`, WebSocket traffic, authentication, chat,
  events, or other dynamic data;
- an update event shown as an `Update available` action that activates the
  waiting worker and reloads after controller change;
- offline UI that can open the cached shell and clearly reports that live CARLO
  data requires network access.

The service worker includes a no-op-safe push event entry point and notification
click routing so future Web Push subscriptions do not require replacing it.
Subscription persistence and delivery are outside this slice. Telegram remains
the operational notification and intervention channel.

## Error handling and observability

Persist and emit structured events for Discovery creation, session start,
suspension, restoration, turn queue/start/completion/interruption/failure, tool
policy rejection, compaction, proposal presentation, Task creation, recovery,
and closure.

The primary UI shows concise state and recovery guidance. Raw provider stderr
and oversized command output remain artifact evidence rather than primary chat
content. Secrets from environment/configuration are not intentionally loaded
into context; tool output follows the existing redaction principles.

## Validation

Backend tests cover:

- creating and listing an `OPEN` Discovery;
- persistent multi-turn conversation and structured memory;
- allowed and rejected tool execution;
- aborting and resuming a turn;
- LRU suspension at the fourth live Discovery per project;
- worker restart reconciliation without duplicate messages;
- creating one Task and multiple Tasks from proposals;
- continuing a Discovery after Task creation;
- closing with final summary and read-only consultation;
- direct Task planning questions followed by a Plan;
- compatibility with existing Task transitions and execution serialization.

Frontend tests cover:

- Discovery navigation, conversation, streaming updates, proposals, and close;
- mobile chat/context navigation and accessible controls;
- Task planning question/answer flow;
- service-worker update notification and offline fallback contract;
- manifest presence and install metadata.

Final validation runs the existing `make test`, Alembic consistency check,
frontend production build, and a browser smoke test at desktop and smartphone
viewports. RPC recovery is exercised against a controlled fake subprocess;
one local Pi smoke test verifies the installed RPC protocol and Discovery guard
when credentials are available.

## Deliberate initial limits

- no Discovery deletion;
- no status transition from `CLOSED` back to `OPEN`;
- no source mutation or approval-to-mutate inside Discovery;
- no Web Push subscription backend;
- no syntax highlighting package;
- no per-user project permissions beyond the existing administrator-only API;
- no configurable process-cap UI; the initial cap is three per project.

These limits do not block future extension because transcript, session identity,
structured state, project ownership, and provider-neutral runtime boundaries are
already durable.
