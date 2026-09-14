# Discovery Subtask Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every task created from a Discovery an aggregate parent with ordered, context-bounded implementation subtasks.

**Architecture:** Require Discovery proposals to carry the existing `metadata.implementation_tasks` contract, then reuse the existing plan-approval child materialization path when creating approved tasks. Keep the parent as an aggregate and queue only its children.

**Tech Stack:** Python, FastAPI, SQLAlchemy async, PostgreSQL, pytest, React, TypeScript, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-25-atomic-plan-subtasks-design.md`

## Global Constraints

- Every Discovery-created parent must contain at least one self-contained implementation task.
- Each implementation task becomes a real ordered child with its own Pi session and context window.
- Reuse the existing child-task model, state transitions, ordering, validation metadata, and board rendering.
- The operation creates the parent and all children before reporting success.
- Add no dependency and retain the Discovery's read-only repository policy.

---

### Task 1: Materialize Discovery Plans as Parent/Child Tasks

**Files:**
- Modify: `skills/carlo-discovery/SKILL.md`
- Modify: `backend/carlo/api.py`
- Modify: `backend/tests/test_discovery_api.py`
- Modify: `frontend/src/DiscoveriesView.tsx`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: `PlanMetadata.implementation_tasks` and the existing approved-plan child representation.
- Produces: one aggregate parent and one ordered child per Discovery implementation task.

- [ ] **Step 1: Write failing API and UI tests**

Add two literal implementation tasks to a Discovery proposal. Assert the creation response is an `IN_PROGRESS` parent and `/api/tasks` contains two `READY/queued` children in order. Assert a proposal without implementation tasks cannot be created and is not Ready in the UI.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_discovery_api.py -q
cd frontend && npm test -- --run src/App.test.tsx -t "Discovery proposals"
```

Expected: the parent is currently queued directly and no children exist.

- [ ] **Step 3: Reuse child materialization**

Extract the existing child allocation block from plan approval into one private helper in `backend/carlo/api.py`. Call it from both normal approval and Discovery creation. Reject Discovery plans whose `implementation_tasks` list is empty.

- [ ] **Step 4: Require executable proposals**

Make the planning-compatible Discovery contract explicitly require at least one item shaped as:

```json
{"title":"Focused subtask","prompt":"Self-contained implementation instruction.","intervention_points":["path:symbol"]}
```

For already persisted proposals, derive one focused subtask per `implementation_phases` entry using the existing megaprompt, plan, and affected areas.

- [ ] **Step 5: Verify the focused and complete suites**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_discovery_api.py backend/tests/test_api.py -q
cd frontend && npm test && npm run build
make test
```

Expected: all commands pass and Discovery-created parents expose ordered children.

- [ ] **Step 6: Commit**

```bash
git add skills/carlo-discovery/SKILL.md backend/carlo/api.py backend/tests/test_discovery_api.py frontend/src/DiscoveriesView.tsx frontend/src/App.test.tsx docs/superpowers/plans/2026-09-14-discovery-subtask-handoff.md
git commit -m "feat: create subtasks from discovery plans"
```
