# Pi Context Recovery and Planning Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover long Pi runs after compaction, create smaller implementation Tasks, and use one configurable Plan profile for both direct planning and Discovery.

**Architecture:** `PiProvider` owns a single resume attempt because it owns Pi session persistence; the orchestrator keeps its existing provider-neutral failure contract. Plan and Discovery remain separate workflows but resolve the same `plan` database profile with an explicit workflow skill. Existing planner child-task creation is reused and guided toward independently verifiable outcomes.

**Tech Stack:** Python 3.14, asyncio, Pi CLI JSON/RPC modes, SQLAlchemy, PostgreSQL/Alembic, pytest, Markdown skills, Node contract tests.

**Spec:** `docs/superpowers/specs/2026-09-05-pi-context-and-planning-profile-design.md`

## Global Constraints

- Retry a context failure at most once and resume the same saved Pi session.
- Never repeat the original implementation prompt during recovery.
- Keep Plan and Discovery behavior distinct while sharing model/profile configuration.
- Keep Discovery read-only and preserve existing Discovery records.
- Add no dependency and no arbitrary prompt/file-count limit.

---

### Task 1: Start Pi compaction with provider headroom

**Files:**
- Modify: `backend/carlo/pi_runtime.py:runtime_context_window`
- Test: `backend/tests/test_pi_runtime.py:test_pi_runtime_snapshot_is_isolated_atomic_and_secret_free`

**Interfaces:**
- Consumes: `runtime_context_window(context_window: int, max_tokens: int) -> int`
- Produces: a Pi-advertised context window equal to 75% of the real provider window, while still exceeding `max_tokens`

- [ ] **Step 1: Change the existing expectation first**

```python
assert models["providers"]["omlx"]["models"][0]["contextWindow"] == 49_152
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/test_pi_runtime.py::test_pi_runtime_snapshot_is_isolated_atomic_and_secret_free -q`

Expected: FAIL because the current value is `58_982`.

- [ ] **Step 3: Apply the smallest runtime change**

```python
def runtime_context_window(context_window: int, max_tokens: int) -> int:
    return max(max_tokens + 1, int(context_window * 0.75))
```

- [ ] **Step 4: Verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/test_pi_runtime.py -q`

Expected: all runtime snapshot tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/carlo/pi_runtime.py backend/tests/test_pi_runtime.py
git commit -m "fix: compact Pi sessions with more headroom"
```

### Task 2: Resume once after Pi compacts a failed prompt

**Files:**
- Modify: `backend/carlo/provider.py:PiProvider.run`
- Test: `backend/tests/test_provider.py`

**Interfaces:**
- Consumes: `_final_output(events: tuple[dict[str, Any], ...]) -> str`, which raises `ContextLimitError`
- Produces: `PiProvider.run(...) -> AgentResult` after zero or one context recovery; the second invocation uses the same `session_id` and `Continue from the compacted session and finish the current task.`

- [ ] **Step 1: Add a fake-Pi regression test**

Create a temporary executable which records `sys.argv`, emits a `Prompt too long` assistant error on its first invocation, and emits `{"type":"final","output":"done"}` on its second invocation. Assert:

```python
result = await provider.run(profile, "Implement everything", str(tmp_path), "CAR-1-implementation-1")
assert result.output == "done"
calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
assert len(calls) == 2
assert "Implement everything" in calls[0]
assert "Continue from the compacted session and finish the current task." in calls[1]
assert "Implement everything" not in calls[1]
assert all("CAR-1-implementation-1" in call for call in calls)
```

- [ ] **Step 2: Verify RED**

Run: `cd backend && .venv/bin/python -m pytest tests/test_provider.py::test_pi_provider_resumes_once_after_context_compaction -q`

Expected: FAIL with `ContextLimitError` after one invocation.

- [ ] **Step 3: Refactor one subprocess execution into a private helper**

Add `_run_once(...) -> tuple[dict[str, str], tuple[dict[str, Any], ...], int]` only to avoid duplicating the existing subprocess loop. Preserve process registration, stderr handling, package metadata, event callbacks, and malformed-event errors exactly.

- [ ] **Step 4: Add the one-retry loop in `run`**

Use these fixed prompts and bounds:

```python
prompts = (
    instruction,
    "Continue from the compacted session and finish the current task.",
)
```

Call `_final_output` after each invocation. Catch only `ContextLimitError`; re-raise it after the second failure. Before the second invocation, publish this synthetic event when a callback exists:

```python
await on_event({
    "type": "context_compaction_retry",
    "output": "Resuming compacted Pi session",
})
```

Return one `AgentResult` containing the successful invocation's final output and the combined events from both invocations.

- [ ] **Step 5: Add the retry-ceiling test**

Use a fake executable which always emits the context error:

```python
with pytest.raises(ContextLimitError):
    await provider.run(profile, "Implement", str(tmp_path), "CAR-2-implementation-1")
assert len(calls_path.read_text().splitlines()) == 2
```

- [ ] **Step 6: Verify GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/test_provider.py -q`

Expected: all provider tests pass.

- [ ] **Step 7: Commit**

```bash
git add backend/carlo/provider.py backend/tests/test_provider.py
git commit -m "fix: resume Pi after context compaction"
```

### Task 3: Make planning produce context-sized child Tasks

**Files:**
- Modify: `skills/carlo-planning/SKILL.md:Plan contract`
- Test: `extensions/skill-contract.test.mjs`

**Interfaces:**
- Consumes: the existing `metadata.implementation_tasks` contract
- Produces: one child Task per independently verifiable outcome using the existing approval flow

- [ ] **Step 1: Add the contract regression**

Extend the planning-skill test to require the phrases `independently verifiable outcome`, `distinct validation loop`, and `separate implementation task` from `skills/carlo-planning/SKILL.md`.

- [ ] **Step 2: Verify RED**

Run: `node --test extensions/skill-contract.test.mjs`

Expected: FAIL because the sizing contract is absent.

- [ ] **Step 3: Add the minimum planning instruction**

Under `metadata.implementation_tasks`, require the planner to create a separate item when work crosses independently testable subsystems or distinct validation loops. Keep tightly coupled files together and prohibit splitting by arbitrary file count, prose length, or organizational neatness.

- [ ] **Step 4: Verify GREEN**

Run: `node --test extensions/skill-contract.test.mjs`

Expected: both skill contract tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/carlo-planning/SKILL.md extensions/skill-contract.test.mjs
git commit -m "fix: size implementation tasks for one context"
```

### Task 4: Share one Plan profile across Plan and Discovery

**Files:**
- Modify: `backend/carlo/model_providers.py:resolve_agent_profile`
- Modify: `backend/carlo/discovery_runtime.py:DiscoveryRuntime._run`
- Modify: `backend/carlo/api.py:create_discovery`, `_profile`
- Create: `backend/alembic/versions/b4c5d6e7f8a9_unify_planning_profiles.py`
- Test: `backend/tests/test_model_providers.py`
- Test: `backend/tests/test_discovery_runtime.py`
- Test: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: `resolve_agent_profile(..., workflow_skill: str | None = None)` and the existing `plan` profile row
- Produces: explicit mode-specific core skills while preserving shared model, effort, packages, and non-core skills

- [ ] **Step 1: Add resolver tests first**

Create a Plan record whose defaults include `carlo-planning` and `frontend-design`, then resolve it with `workflow_skill="carlo-discovery"`. Assert:

```python
assert "carlo-discovery" in resolved.skills
assert "frontend-design" in resolved.skills
assert "carlo-planning" not in resolved.skills
```

The existing Plan resolution test must continue to assert `carlo-planning` is present.

- [ ] **Step 2: Verify resolver RED**

Run: `cd backend && .venv/bin/python -m pytest tests/test_model_providers.py -q`

Expected: FAIL because `workflow_skill` is not accepted.

- [ ] **Step 3: Add the explicit workflow-skill override**

Add `workflow_skill: str | None = None` to `resolve_agent_profile`. When supplied, remove `carlo-planning` and `carlo-discovery` from stored/default skills and prepend only the supplied workflow skill. Validate the resulting list through the existing known-skill check. Calls without the override keep current behavior.

- [ ] **Step 4: Make Discovery resolve the Plan profile in Discovery mode**

Change new Discovery creation from `_profile(session, "discovery")` to `_profile(session, "plan")`. In `DiscoveryRuntime._run`, pass `workflow_skill="carlo-discovery"` while retaining Discovery's current tools, RPC mode, and guard extension.

- [ ] **Step 5: Add API and runtime regressions**

Assert a newly created Discovery references the `plan` profile. Assert the provider profile received by `DiscoveryRuntime` is named `plan`, includes `carlo-discovery`, excludes `carlo-planning`, and retains a shared non-core skill.

- [ ] **Step 6: Verify API/runtime GREEN**

Run: `cd backend && .venv/bin/python -m pytest tests/test_model_providers.py tests/test_discovery_runtime.py tests/test_api.py -q`

Expected: all selected tests pass.

- [ ] **Step 7: Add the data migration**

Create an Alembic revision after the current head. In `upgrade()`:

1. Find the `plan` profile ID.
2. Repoint `discoveries.profile_id`, `tasks.active_profile_id`, and `attempts.profile_id` from `brief` or `discovery` to `plan` where those references exist.
3. Remove package associations for `brief` and `discovery`.
4. Delete the two redundant profile rows.

In `downgrade()`, recreate `brief` and `discovery` by copying provider, effort, model, permissions, context policy, packages, and shared defaults from `plan`, then restore required core skills. Historical references remain on `plan` because their former ownership cannot be reconstructed safely.

- [ ] **Step 8: Verify migration and profile catalog**

Run: `cd backend && .venv/bin/alembic upgrade head && .venv/bin/alembic check`

Then query a test database and assert only `plan`, `implementation`, and `escalation` remain in `agent_profiles`, while every Discovery has a valid profile reference.

- [ ] **Step 9: Commit**

```bash
git add backend/carlo/model_providers.py backend/carlo/discovery_runtime.py backend/carlo/api.py backend/alembic/versions backend/tests/test_model_providers.py backend/tests/test_discovery_runtime.py backend/tests/test_api.py
git commit -m "refactor: share Plan profile with Discovery"
```

### Task 5: Integrated verification and installation

**Files:**
- Verify all files above

**Interfaces:**
- Consumes: Tasks 1–4
- Produces: tested source and updated local CARLO services

- [ ] **Step 1: Run the full repository check**

Run: `env UV_CACHE_DIR=/tmp/carlo-uv-cache-codex make test`

Expected: extension tests, Python compilation, backend tests, Alembic check, frontend tests, and frontend build all pass.

- [ ] **Step 2: Inspect the final diff**

Run: `git diff --check` and `git status --short`.

Expected: no whitespace errors and only intended files changed.

- [ ] **Step 3: Install and restart local CARLO**

Run: `sudo make install-mac`

Expected: production check succeeds and launchd restarts API and worker.

- [ ] **Step 4: Verify production state**

Confirm the installed provider/runtime/profile code matches the committed source, API and worker processes are running, Alembic reports the new head, and `agent_profiles` contains only the three operational profiles.
