# Unified Discovery Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Discovery preview and create Tasks from the same Carlo plan used by direct Task planning, with an inspectable parent–child tree and test-gated completion.

**Architecture:** Extract the existing plan-profile invocation and `PlanPayload` validation from the API handler into one callable planning service. Discovery supplies parent candidates and repository evidence; the service produces canonical drafts before creation. Both creation routes approve the exact reviewed draft and materialize children through one helper. Implementation prompts include the stable parent Brief; child checks run at each child, and final integration checks run before the last child completes.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, Pi provider, React 19, TypeScript, pytest, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-20-unified-discovery-planning-design.md`

## Global Constraints

- Discovery can propose one or many parent Tasks; each parent can have one or many ordered children. No fixed count or file-count threshold.
- The `plan` profile and Carlo planning skill produce all canonical `PlanPayload` drafts, including Discovery drafts. OMLX is unchanged.
- Creation persists exactly the reviewed draft; stale, invalid, or failed drafts cannot be created.
- The parent Brief precedes each child's variable context and counts toward its context budget.
- Failed or unavailable tests block completion. Budget exhaustion remains an exceptional recovery path.
- Preserve existing created Tasks and plan revisions; pending legacy Discovery proposals need canonical drafts.
- No new dependency or migration unless a task proves one necessary.

## Review Focus

1. A Discovery candidate changes while its planner run is in flight: discard the old result. Covered in Task 2.
2. One selected parent depends on an uncreated parent: reject without creating a dangling dependency. Covered in Task 3.
3. `Create all` contains a dependency cycle or duplicate request: reject the cycle; repeated creation returns the existing Tasks. Covered in Task 3.
4. A child passes local checks while final integration checks fail: neither child nor parent completes. Covered in Task 4.
5. A large Brief plus file context exceeds the pack budget: stop before invoking Pi and report replan evidence. Covered in Task 4.

---

### Task 1: One canonical planner for direct Tasks and candidates

**Files:**
- Create: `backend/carlo/planning.py`
- Modify: `backend/carlo/api.py:216-313,2287-2425`
- Modify: `backend/carlo/context_pack.py:8`
- Test: `backend/tests/test_api.py`
- Test: `backend/tests/test_discovery_api.py`

**Interfaces:**
- Consumes: `CodingAgentProvider.run`, `resolve_agent_profile`, existing `PlanPayload` contract and `carlo-planning` skill.
- Produces: `Planner.plan(request: PlanningRequest, on_event: AgentEventHandler | None = None) -> PlanPayload | PlanningQuestion`; `PlanningRequest` carries project, title, goal, session ID, optional prompt path, optional Discovery handoff, and optional selected model ID.

- [ ] **Step 1: Write the failing parity test.** Use `FakeProvider` to plan the same goal through a direct Task and a Discovery candidate. Assert both calls use the resolved `plan` profile and both reject the same malformed `implementation_tasks`. Add a question response to prove a direct Task still enters its existing question state.

```python
assert direct_profile.name == discovery_profile.name == "plan"
assert direct_error == discovery_error
assert task.planning_question == {"text": "Which format?"}
```

- [ ] **Step 2: Run the targeted tests and confirm the new parity assertion fails.**

```text
cd backend && .venv/bin/python -m pytest tests/test_api.py tests/test_discovery_api.py -q --tb=short
```

- [ ] **Step 3: Move the `PlanPayload`/package schema and provider parse–repair loop from `continue_planning` into `planning.py`.** Preserve the three validation attempts, skill invocation, runtime evidence, and question result. Build the planning instruction from `PlanningRequest`, with Discovery evidence appended after the common contract; direct Tasks pass an empty handoff. Keep DB state transitions and events in API callers.

```python
@dataclass(frozen=True)
class PlanningRequest:
    project: Project
    title: str
    goal: str
    session_id: str
    prompt_path: str | None = None
    handoff: str = ""
    model_id: int | None = None

@dataclass(frozen=True)
class PlanningQuestion:
    text: str

# Both callers await the same Planner.plan(request, on_event).
```

- [ ] **Step 4: Run the targeted tests, then the planning-profile tests.**

```text
cd backend && .venv/bin/python -m pytest tests/test_api.py tests/test_discovery_api.py tests/test_planning_profile_migration.py -q --tb=short
```

### Task 2: Canonical drafts after Discovery conversation

**Files:**
- Modify: `skills/carlo-discovery/SKILL.md`
- Modify: `skills/carlo-planning/SKILL.md`
- Modify: `extensions/carlo-discovery-guard.mjs`
- Modify: `backend/carlo/discovery_runtime.py:325-409,557-575`
- Modify: `backend/carlo/api.py:483-510`
- Test: `backend/tests/test_discovery_runtime.py`
- Test: `backend/tests/test_discovery_api.py`
- Test: `extensions/discovery-handoff.test.mjs`

**Interfaces:**
- Consumes: `Planner.plan(PlanningRequest)` from Task 1 and current `Discovery.state` JSON.
- Produces: each pending proposal stores `plan_draft: PlanPayload.model_dump()`, `draft_source: str` (SHA-256 of candidate fields and planning handoff), and `planning_error: str | None`; the backend marks stale drafts with `validation_error`. Existing `id`, `title`, `megaprompt`, and `depends_on` remain.

- [ ] **Step 1: Write a failing Discovery test.** Submit a `discovery_state` with two parent candidates and no hand-authored plan. Assert the runtime invokes the canonical planner once per candidate, records two drafts, and marks each ready only after `PlanPayload` validation. Change one candidate while an old run is in flight and assert its old result is discarded; unchanged draft survives. Test a pending legacy proposal without `draft_source` as unready.

```python
assert [item["plan_draft"]["metadata"]["implementation_tasks"][0]["title"]
        for item in discovery.state["task_proposals"]] == ["API", "UI"]
assert changed["draft_source"] != old_source
assert unchanged["draft_source"] == old_unchanged_source
```

- [ ] **Step 2: Run the tests and confirm failure before changing runtime code.**

```text
cd backend && .venv/bin/python -m pytest tests/test_discovery_runtime.py tests/test_discovery_api.py -q --tb=short
node --test extensions/discovery-handoff.test.mjs
```

- [ ] **Step 3: Change Discovery's tool contract to emit parent candidates and evidence, then plan them with `Planner`.** Trigger planning in the worker after the conversation state is saved; emit proposal planning events so the UI can refresh. Fingerprint normalized candidate goal/dependencies plus relevant Discovery decisions and findings with `hashlib.sha256`; save a result only when its fingerprint still matches. A `PlanningQuestion` becomes a visible Discovery clarification and leaves that candidate unready. Remove the Discovery skill's instruction to author `brief_markdown`, `plan_markdown`, and work packages; require explicit parent boundaries and source evidence. In `carlo-planning`, remove “smallest useful number” and require a final tree review for omitted work, interfaces, dependencies, and tests that depend on future work.

```python
source = hashlib.sha256(json.dumps(source_data, sort_keys=True).encode()).hexdigest()
if source == current_candidate_source:
    proposal["plan_draft"] = result.model_dump()
    proposal["draft_source"] = source
```

- [ ] **Step 4: Run the targeted tests and verify a failed plan cannot enable Create.**

```text
cd backend && .venv/bin/python -m pytest tests/test_discovery_runtime.py tests/test_discovery_api.py -q --tb=short
node --test extensions/discovery-handoff.test.mjs
```

### Task 3: Approve the exact reviewed plan through one path

**Files:**
- Modify: `backend/carlo/api.py:417-473,1959-2056,2782-2872`
- Modify: `backend/carlo/tasks.py:18-73`
- Test: `backend/tests/test_discovery_api.py`
- Test: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: validated `PlanPayload`, current `Discovery.state` draft/source, existing `Task` and `PlanRevision` models.
- Produces: `approve_plan_revision(session: AsyncSession, task: Task, plan: PlanRevision) -> list[Task]`, used by direct approval and Discovery creation.

- [ ] **Step 1: Write failing API tests.** Create two parent proposals with children, approve one, then retry Create and assert no duplicate parent or children. Reject creation when the requested parent depends on an uncreated proposal or when `Create all` has a cycle. Reject a stale `draft_source`; compare the displayed draft JSON with the persisted parent revision and materialized child titles/objectives.

```python
assert persisted.metadata_json == preview["metadata"]
assert [child.title for child in children] == [item["title"] for item in preview["metadata"]["implementation_tasks"]]
assert retry_ids == first_ids
```

- [ ] **Step 2: Run the targeted tests and confirm failure.**

```text
cd backend && .venv/bin/python -m pytest tests/test_discovery_api.py tests/test_api.py -q --tb=short
```

- [ ] **Step 3: Extract direct approval's state transition and child materialization into the shared helper.** Discovery creation validates every draft and its current source fingerprint before any Task is created. Resolve proposal dependencies by stable proposal ID; topologically order selected parents and reject cycles and missing uncreated parents. Change `create_task_record` so the batch can flush parent records without committing each one; commit the batch and `created_task_id` markers once, and clean up newly written prompt files on rollback. Do not silently skip a missing dependency. Keep completed historical proposals untouched.

```python
if proposal.get("draft_source") != fingerprint(proposal, discovery.state):
    raise HTTPException(409, "proposal plan is stale; replan in Discovery")
approved = PlanPayload.model_validate(proposal["plan_draft"])
children = await approve_plan_revision(session, parent, revision)
```

- [ ] **Step 4: Run API tests and verify direct approval and Discovery creation use the helper.**

```text
cd backend && .venv/bin/python -m pytest tests/test_discovery_api.py tests/test_api.py -q --tb=short
```

### Task 4: Shared Brief and test-gated child completion

**Files:**
- Modify: `backend/carlo/context_pack.py:68-141`
- Modify: `backend/carlo/orchestrator.py:537-565,1065-1130,320-357`
- Test: `backend/tests/test_context_pack.py`
- Test: `backend/tests/test_orchestration.py`

**Interfaces:**
- Consumes: copied child `PlanRevision.brief_markdown`, each child's `implementation_tasks[0].verification.commands`, parent's `metadata.validation_commands`.
- Produces: stable Brief-first implementation instruction; child completion requires its checks, and the final child additionally requires the parent's integration checks.

- [ ] **Step 1: Write failing tests.** Compare two sibling instructions: both start with identical parent Brief and differ afterward. Prove the Brief is included in the pack budget. Prove child A can pass its local check while child B's final integration command fails, leaving B and the parent incomplete; a later attempt that fixes the error completes both. Assert a missing/unrunnable command blocks rather than passes.

```python
assert first_instruction.startswith("Parent Brief:\n" + brief)
assert second_instruction.startswith("Parent Brief:\n" + brief)
assert parent.status != TaskStatus.DONE
assert final_attempt.outcome == "verified"
```

- [ ] **Step 2: Run the focused tests and confirm failure.**

```text
cd backend && .venv/bin/python -m pytest tests/test_context_pack.py tests/test_orchestration.py -q --tb=short
```

- [ ] **Step 3: Put the copied Brief before variable package content and count both toward the configured context budget.** For a child, select its `verification.commands`; if it is the last active sibling, append the parent's integration commands. Reuse `_validate` and its recorded `ValidationRun`s. A failing integration command follows the existing validation-failure retry path; `_sync_parent` only sees `DONE` after the last child has passed all required commands. Keep standalone Task validation unchanged.

```python
commands = list(package["verification"]["commands"])
if is_last_active_child:
    commands.extend(parent_plan.metadata_json["validation_commands"])
commands = list(dict.fromkeys(commands))
```

- [ ] **Step 4: Run the focused tests, then the full backend suite.**

```text
cd backend && .venv/bin/python -m pytest tests/test_context_pack.py tests/test_orchestration.py -q --tb=short
cd backend && .venv/bin/python -m pytest -q --tb=short
```

### Task 5: Review the parent–child forest in Discovery

**Files:**
- Modify: `frontend/src/api.ts:125-135`
- Modify: `frontend/src/DiscoveriesView.tsx:122-223`
- Modify: `frontend/src/styles.css:325-344`
- Test: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: proposal `plan_draft`, `draft_source`, `planning_error`, `depends_on`, and ordered `metadata.implementation_tasks` from Task 2.
- Produces: visible parent–child preview with per-parent and all-ready Create controls.

- [ ] **Step 1: Write failing UI tests.** Render two parent candidates, one with one child and one with three. Assert parent title, child count/order, dependency, expanded child objective/files/checks, and disabled Create for a stale or failed plan. A single-parent Create passes only its ID; Create all passes no selection.

```tsx
expect(within(chat).getByText('3 subtasks')).toBeTruthy()
expect(within(chat).getByText('Depends on: Archive')).toBeTruthy()
expect(within(chat).getByText('Run parser tests')).toBeTruthy()
```

- [ ] **Step 2: Run the UI test and confirm failure.**

```text
cd frontend && npm test -- --run src/App.test.tsx
```

- [ ] **Step 3: Render the actual canonical draft in native `<details>` rows in the main chat.** Keep the existing Context pane for full Brief/Plan. Show a precise state for planning, ready, failed, stale, and created proposals. Use `implementation_tasks`, not `implementation_phases`, for children; use the existing `DiscoveryProposal` type and CSS vocabulary. Make controls follow the server's dependency and draft-readiness rules, and retain server validation as the authority.

```tsx
<details className="proposal-parent">
  <summary>{proposal.title} · {tasks.length} subtasks</summary>
  <ol>{tasks.map(task => <li key={task.id}><details><summary>{task.title}</summary><p>{task.objective}</p></details></li>)}</ol>
</details>
```

- [ ] **Step 4: Run UI tests and build.**

```text
cd frontend && npm test -- --run src/App.test.tsx
cd frontend && npm run build
```

### Final verification

- [ ] Run backend tests, extension tests, frontend tests and build using the repo commands, then inspect `git diff --check` and the full diff against the spec.

```text
cd backend && .venv/bin/python -m pytest -q --tb=short
node --test extensions/*.test.mjs
cd frontend && npm test
cd frontend && npm run build
git diff --check
```
