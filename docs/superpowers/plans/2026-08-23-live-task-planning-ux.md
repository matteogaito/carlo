# Live Task Planning UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Task creation and planning into a visible guided flow with a vertical creation modal, real Pi activity over WebSocket, Markdown rendering, and a half-screen resizable Task panel.

**Architecture:** Keep the existing Task lifecycle and synchronous planning endpoints. Extend the provider-neutral `run()` boundary with an optional event callback, persist a small safe subset of Pi lifecycle/tool events through the existing `Event` table, and let the existing WebSocket refresh the open Task panel while planning runs. Reuse `react-markdown`; add no dependency, schema, provider, or planning engine.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2, asyncio subprocesses, Pi JSON event mode, React 19, TypeScript 7, `react-markdown`, Vitest, React Testing Library, CSS pointer interactions.

**Spec:** User-approved feedback from 2026-08-23 and visual reproduction `screenshots/broken_task.png`.

## Implementation status — 2026-08-23

Implemented on `main`: ordered provider event streaming, bounded persisted
planning activity, automatic Task planning, vertical creation form, live
planner guidance, Markdown Goal/Brief/Plan rendering, accessible half-screen
Task resizing, and live Discovery tool activity without changing its layout.
The final regression also repaired the worker module indentation that prevented
installed Discovery turns from running and added whole-package byte compilation
to `make test`.

Verification: 5 Node contract checks, whole-package Python compilation, 87
backend tests, clean Alembic check, 10 frontend tests, and Vite production build.
Browser automation was unavailable, so pointer resizing is covered by automated
interaction tests but not claimed as browser-visual validation.

## Global Constraints

- A Task remains `NOT_READY` until its generated plan is explicitly approved.
- Creating a Task automatically starts its existing Pi planning session.
- Planning activity shown in the UI must come from real Pi events; never simulate progress.
- Persist only bounded event metadata: phase, tool name, relevant path/query/command, and success/failure. Never persist raw tool output or reasoning.
- Reuse the existing `CodingAgentProvider`, `Event`, `/api/ws`, Task detail response, planning question endpoint, and approval endpoint.
- Keep planning concurrent and independent from the global implementation lock.
- Desktop Task detail starts at 50% viewport width and is pointer/keyboard resizable; mobile remains fullscreen.
- Add no dependency and no database migration.

---

### Task 1: Stream provider events while Pi is running

**Files:**
- Modify: `backend/carlo/provider.py`
- Modify: `backend/tests/fakes.py`
- Modify: `backend/tests/test_provider.py`

**Interfaces:**
- Produces: `AgentEventHandler = Callable[[dict[str, Any]], Awaitable[None]]`.
- Changes: `CodingAgentProvider.run(..., on_event: AgentEventHandler | None = None) -> AgentResult`.
- Guarantees: callbacks are awaited in stdout order; `AgentResult.events` and `_final_output()` retain current behavior.

- [ ] **Step 1: Write the failing provider test**

Extend the fake executable in `test_pi_provider_uses_explicit_read_only_session` to print an `agent_start`, a `tool_execution_start`, and the existing `final` record. Pass an async collector:

```python
seen: list[str] = []

async def collect(event: dict[str, Any]) -> None:
    seen.append(str(event["type"]))

result = await provider.run(
    profile, "Inspect the repo", str(tmp_path), "CAR-1-plan-1", collect
)

assert seen == ["agent_start", "tool_execution_start", "final"]
assert [event["type"] for event in result.events] == seen
assert json.loads(result.output)["plan_markdown"] == "P"
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd backend && uv run pytest tests/test_provider.py::test_pi_provider_uses_explicit_read_only_session -q
```

Expected: FAIL because `run()` does not accept the callback.

- [ ] **Step 3: Implement ordered line streaming**

Add the callback type and optional final parameter to the protocol, `PiProvider`, and `FakeProvider`. Replace `process.communicate()` with concurrent stderr draining plus line-by-line stdout parsing:

```python
stderr_task = asyncio.create_task(process.stderr.read())
events: list[dict[str, Any]] = []
while line := await process.stdout.readline():
    event = json.loads(line)
    if not isinstance(event, dict):
        raise ProviderError("Pi returned a non-object JSON event")
    events.append(event)
    if on_event:
        await on_event(event)
return_code = await process.wait()
stderr = await stderr_task
```

On malformed JSON, terminate the still-running process before raising `ProviderError`. Preserve process cleanup, stderr error reporting, `tuple(events)`, and `_final_output()`.

- [ ] **Step 4: Verify GREEN**

```bash
cd backend && uv run pytest tests/test_provider.py -q
```

Expected: all provider tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/carlo/provider.py backend/tests/fakes.py backend/tests/test_provider.py
git commit -m "feat: stream coding agent run events"
```

---

### Task 2: Publish safe planning activity through existing events

**Files:**
- Modify: `backend/carlo/api.py`
- Modify: `backend/tests/fakes.py`
- Modify: `backend/tests/test_api.py`

**Interfaces:**
- Produces persisted Task events `planning.exploring`, `planning.tool.started`, `planning.tool.completed`, and one `planning.drafting` per provider run.
- Produces: `_planning_activity(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None`.
- Consumes: Task 1 `on_event` callback and existing `Event(task=..., type=..., payload=...)`.

- [ ] **Step 1: Write the failing API test**

Give `FakeProvider` optional ordered events and make it call `on_event` before returning. In `test_task_stays_not_ready_until_plan_is_approved`, use:

```python
provider.events = (
    {"type": "agent_start"},
    {"type": "tool_execution_start", "toolName": "read", "args": {"path": "README.md"}},
    {"type": "tool_execution_end", "toolName": "read", "isError": False, "result": "do not persist"},
    {"type": "message_update", "delta": "{"},
    {"type": "message_update", "delta": '"brief_markdown"'},
)
```

After planning, assert the detailed Task events contain exactly one drafting event, tool events contain `{"tool": "read", "detail": "README.md"}`, and no event payload contains `do not persist`.

- [ ] **Step 2: Verify RED**

```bash
cd backend && uv run pytest tests/test_api.py::test_task_stays_not_ready_until_plan_is_approved -q
```

Expected: FAIL because provider events are not persisted during planning.

- [ ] **Step 3: Add the minimal safe event adapter**

Map only supported provider records:

```python
def _planning_activity(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    kind = event.get("type")
    if kind == "agent_start":
        return "planning.exploring", {}
    if kind in {"tool_execution_start", "tool_execution_end"}:
        tool = str(event.get("toolName") or event.get("tool") or "tool")[:80]
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        detail = next((str(args[key]) for key in ("path", "query", "command") if key in args), "")[:500]
        payload = {"tool": tool, "detail": detail}
        if kind == "tool_execution_end":
            payload["failed"] = bool(event.get("isError"))
        return ("planning.tool.started" if kind.endswith("start") else "planning.tool.completed"), payload
    if kind == "message_update":
        return "planning.drafting", {}
    return None
```

Inside `continue_planning`, create a callback that ignores repeated `planning.drafting`, adds the mapped `Event`, and commits immediately. Pass it to `provider.run(..., on_event=publish)`. Do not persist deltas, tool results, prompt content, or model reasoning.

- [ ] **Step 4: Verify GREEN and realtime compatibility**

```bash
cd backend && uv run pytest tests/test_api.py tests/test_events.py -q
```

Expected: API and WebSocket replay tests pass without schema changes.

- [ ] **Step 5: Commit**

```bash
git add backend/carlo/api.py backend/tests/fakes.py backend/tests/test_api.py
git commit -m "feat: publish live planning activity"
```

---

### Task 3: Make Task creation a vertical automatic planning flow

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Changes: `CreateStrip` receives `onTaskCreated(task: Task): Promise<void>`.
- Behavior: successful creation closes/reset the creation modal, opens Task detail immediately, and calls `api.startPlanning(task.id)`.
- Consumes: Task 2 activity events via the existing `App.events()` refresh path.

- [ ] **Step 1: Write failing frontend tests**

Extend the upload test with a `startPlanning` spy returning a Task in `planning` stage:

```tsx
expect(startPlanning).toHaveBeenCalledWith('ECA-1')
expect(await screen.findByRole('complementary', { name: 'ECA-1 details' })).toBeTruthy()
expect(screen.getByText(/Pi is planning/i)).toBeTruthy()
```

Add a structure assertion that the creation form has class `task-create-form` and computed `display: grid`, catching the current `.create-strip form` specificity regression.

- [ ] **Step 2: Verify RED**

```bash
cd frontend && npm test -- App.test.tsx
```

Expected: FAIL because creation does not start planning/open details and the modal form computes as flex.

- [ ] **Step 3: Fix the root CSS selector and connect automatic planning**

Change the project-only rule:

```css
.create-strip > form { display: flex; gap: 10px; align-items: end; padding-left: 12px; }
.task-create-form { display: grid; }
```

In `App`, add:

```tsx
async function beginPlanning(task: Task) {
  setSelected(task)
  await act(() => api.startPlanning(task.id))
}
```

Pass it to `CreateStrip`. In `submitTask`, retain the created Task, reset and close the modal, then await `onTaskCreated(created)`. The Task remains durable if planning fails; report the existing API error without deleting it.

- [ ] **Step 4: Render real live activity in Task detail**

While `task.stage === 'planning'`, render an `aria-live="polite"` section headed `Pi is planning` and map the newest planning events to plain labels:

```tsx
planning.exploring       -> Exploring repository
planning.tool.started    -> `${tool} ${detail}`
planning.tool.completed  -> `${tool} completed` or `${tool} failed`
planning.drafting        -> Drafting Brief and Plan
```

Keep the existing question form, plan approval, Activity audit list, and manual `Build Brief & Plan` recovery button. WebSocket events already refresh `selected`; add no polling.

- [ ] **Step 5: Verify GREEN**

```bash
cd frontend && npm test -- App.test.tsx
```

Expected: Task upload, automatic start, question answering, and Board tests pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/App.tsx frontend/src/styles.css frontend/src/App.test.tsx
git commit -m "feat: guide task creation into live planning"
```

---

### Task 4: Render Markdown and resize Task detail from 50%

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Produces: `TaskMarkdown({ children }: { children: string }): JSX.Element`, backed by installed `react-markdown`.
- Behavior: desktop Task detail defaults to `50vw`, clamps between `360px` and `window.innerWidth - 320`, and resizes from its left separator with pointer or arrow keys.
- Mobile behavior: existing fullscreen Task detail remains `width: 100%`; resize separator is hidden below `900px`.

- [ ] **Step 1: Write failing Markdown and resize tests**

Use the existing Task whose Brief is `# Brief` and Plan is `# Plan`. Assert rendered headings exist rather than literal preformatted text:

```tsx
expect(screen.getByRole('heading', { name: 'Brief', level: 1 })).toBeTruthy()
expect(screen.getByRole('heading', { name: 'Plan', level: 1 })).toBeTruthy()
```

Get the separator and verify default width and keyboard resizing:

```tsx
const detail = screen.getByRole('complementary', { name: 'CAR-1 details' })
const separator = screen.getByRole('separator', { name: 'Resize task details' })
expect(detail.style.width).toBe('512px') // jsdom default innerWidth: 1024
await userEvent.type(separator, '{ArrowLeft}')
expect(detail.style.width).toBe('544px')
```

- [ ] **Step 2: Verify RED**

```bash
cd frontend && npm test -- App.test.tsx
```

Expected: FAIL because Brief/Plan use `<pre>` and no resize separator exists.

- [ ] **Step 3: Reuse the installed Markdown renderer**

Import `Markdown` from `react-markdown` and render Goal, Brief, and Plan through:

```tsx
function TaskMarkdown({ children }: { children: string }) {
  return <div className="task-markdown"><Markdown>{children}</Markdown></div>
}
```

Style headings, lists, links, inline code, tables, and fenced code within `.task-markdown`. Fenced code must scroll horizontally and never widen the panel.

- [ ] **Step 4: Add the minimal accessible resize separator**

Store `detailWidth` in `App`, initialized from `Math.round(window.innerWidth / 2)`, and pass width/update props to `TaskDetail`. Add a left-edge separator with pointer capture. Width is calculated as `window.innerWidth - event.clientX` and clamped:

```tsx
const clampDetailWidth = (width: number) =>
  Math.min(Math.max(width, 360), window.innerWidth - 320)
```

`ArrowLeft` adds 32 px; `ArrowRight` subtracts 32 px. Give the separator `role="separator"`, `aria-orientation="vertical"`, `aria-label="Resize task details"`, and `tabIndex={0}`.

Update desktop grid layout to `grid-template-columns: minmax(0, 1fr) auto`. On mobile force Task detail to `width: 100% !important; min-width: 0; max-width: none` and hide the separator.

- [ ] **Step 5: Verify GREEN and production build**

```bash
cd frontend && npm test -- App.test.tsx
cd frontend && npm test
cd frontend && npm run build
```

Expected: all frontend tests pass and Vite builds without TypeScript errors.

- [ ] **Step 6: Manual responsive verification**

Run CARLO locally and verify at desktop and phone widths:

1. Open `screenshots/broken_task.png` beside the live app.
2. Create a Markdown Task: the form stacks vertically without clipping.
3. Submit: Task detail opens immediately and real planning activity advances.
4. Answer a planner question and confirm the same panel resumes live activity.
5. Confirm Goal, Brief, Plan, lists, headings, and code blocks render correctly.
6. Drag the left edge both directions; Board remains usable and the panel stays within bounds.
7. At width below 900 px, confirm fullscreen detail, no resize handle, and horizontally scrolling code.
8. Approve the plan and confirm the card moves from `Not Ready` to `Ready`.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/App.tsx frontend/src/styles.css frontend/src/App.test.tsx
git commit -m "feat: render and resize task planning detail"
```

---

### Task 5: Show real Discovery exploration activity

**Files:**
- Modify: `backend/carlo/discovery_runtime.py`
- Modify: `backend/tests/test_discovery_runtime.py`
- Modify: `frontend/src/DiscoveriesView.tsx`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Produces: safe `discovery.tool.started` and `discovery.tool.completed` events.
- Preserves: the existing central chat and right-side Context layout.

- [ ] **Step 1: Write failing runtime and UI tests**

Assert a `read` tool emits `{"tool": "read", "detail": "src/ingest.py"}`
while its result body is absent from events. Feed that event to
`DiscoveriesView` and assert the running indicator displays
`read · src/ingest.py`.

- [ ] **Step 2: Verify RED**

```bash
cd backend && uv run pytest tests/test_discovery_runtime.py -q
cd frontend && npm test -- App.test.tsx
```

Expected: both fail because Discovery exposes only a generic running label.

- [ ] **Step 3: Emit and render bounded activity**

On tool start, persist tool name plus the first available `path`, `query`, or
`command`, capped at 500 characters. On completion persist only tool name and
failure flag. Update the existing thinking row through WebSocket events; switch
to `Pi is drafting a response…` after the first assistant delta and clear it at
the terminal turn event.

- [ ] **Step 4: Verify GREEN and commit**

```bash
cd backend && uv run pytest tests/test_discovery_runtime.py -q
cd frontend && npm test -- App.test.tsx
git add backend/carlo/discovery_runtime.py backend/tests/test_discovery_runtime.py frontend/src/DiscoveriesView.tsx frontend/src/App.test.tsx
git commit -m "feat: show live discovery tool activity"
```

---

### Task 6: Full regression and release note

**Files:**
- Modify: `README.md`

**Interfaces:**
- Documents: automatic live planning, explicit approval, and resizable Markdown detail.

- [ ] **Step 1: Update the concise Task workflow documentation**

Document this exact sequence:

```text
Create Task → live Pi planning → zero or more focused questions → Brief/Plan review → explicit approval → Ready
```

State that closing the panel does not cancel or delete the Task and that planning activity is persisted in Task history.

- [ ] **Step 2: Run the full project verification**

```bash
make test
```

Expected: backend, frontend, migration consistency, and production build checks all pass.

- [ ] **Step 3: Inspect the final diff**

```bash
git status --short
git diff --check
git diff --stat
```

Expected: only the files named in this plan are changed; no whitespace errors, generated artifacts, screenshots, secrets, or migration files are added.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: explain live task planning"
```
