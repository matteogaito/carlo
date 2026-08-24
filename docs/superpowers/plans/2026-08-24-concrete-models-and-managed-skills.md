# Concrete Models and Managed Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every CARLO profile select one concrete provider/model pair and keep Superpowers plus Anthropic frontend-design available to Pi through a weekly CARLO-managed update.

**Architecture:** PostgreSQL keeps only the concrete `available_model_id` selection. The existing Pi runtime resolver and snapshot builder consume that selection. The existing weekly maintenance lock also updates two CARLO-managed Git checkouts, while Pi receives only the requested package or skill paths.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, React, TypeScript, Pi CLI, Git.

**Spec:** `docs/superpowers/specs/2026-08-24-concrete-profile-model-selection-design.md`

## Global Constraints

- A provider is only a connection and model catalog; it has no default model.
- Agent profiles select only concrete `AvailableModel` rows.
- UI labels use `provider-slug — external-model-id`.
- Running sessions retain the skill revisions they started with.
- Failed third-party updates retain the previous usable checkout and emit one concise event.
- No new Python or frontend dependency.

---

### Task 1: Concrete model schema and migration

**Files:**
- Create: `backend/alembic/versions/c8d9e0f1a2b3_concrete_profile_models.py`
- Modify: `backend/carlo/models.py`
- Test: `backend/tests/test_migrations.py`

**Interfaces:**
- Produces: `AgentProfile.available_model_id` as the only persisted profile model selection.
- Removes: `ModelProvider.default_model_id`, `AgentProfile.model_provider_id`, and the legacy `AgentProfile.model` string.

- [ ] **Step 1: Write a migration test that inserts a provider default and provider-only profile at the previous revision, upgrades to head, and asserts the profile points to the former concrete model. Add a second provider without a default and assert its profile remains unconfigured.**

- [ ] **Step 2: Run the migration test and verify it fails because the new revision does not exist.**

Run: `cd backend && uv run pytest tests/test_migrations.py -q`

- [ ] **Step 3: Add the Alembic revision. Before dropping columns, execute:**

```sql
UPDATE agent_profiles AS profile
SET available_model_id = provider.default_model_id
FROM model_providers AS provider
WHERE profile.model_provider_id = provider.id
  AND profile.available_model_id IS NULL
  AND provider.default_model_id IS NOT NULL
```

Then drop `ck_agent_profiles_one_model_selection`, `agent_profiles.model_provider_id`, `agent_profiles.model`, and `model_providers.default_model_id` with their foreign keys. The downgrade restores nullable legacy columns and the check constraint without inventing lost defaults.

- [ ] **Step 4: Remove the obsolete ORM fields, relationships, and check constraint.**

- [ ] **Step 5: Run the migration test and model tests.**

Run: `cd backend && uv run pytest tests/test_migrations.py tests/test_models.py -q`

- [ ] **Step 6: Commit the schema slice.**

```bash
git add backend/alembic/versions/c8d9e0f1a2b3_concrete_profile_models.py backend/carlo/models.py backend/tests/test_migrations.py
git commit -m "refactor: persist concrete profile models"
```

### Task 2: Concrete model API and runtime

**Files:**
- Modify: `backend/carlo/model_providers.py`
- Modify: `backend/carlo/api.py`
- Modify: `backend/carlo/admin.py`
- Modify: `backend/carlo/discovery_runtime.py`
- Test: `backend/tests/test_model_providers.py`
- Test: `backend/tests/test_model_provider_api.py`
- Test: `backend/tests/test_discovery_runtime.py`

**Interfaces:**
- Consumes: `AgentProfile.available_model_id`.
- Produces: provider JSON without `default_model_id`; profile JSON and updates with only `available_model_id`; `resolve_agent_profile(...)` that rejects an unconfigured profile.

- [ ] **Step 1: Change API and resolver tests to reject provider-only selection, omit provider defaults, preserve concrete task overrides, and expect `ModelProviderError("agent profile has no managed model")` when no concrete model is configured.**

- [ ] **Step 2: Run the focused tests and verify failures reference the obsolete fields or fallback.**

Run: `cd backend && uv run pytest tests/test_model_providers.py tests/test_model_provider_api.py tests/test_discovery_runtime.py -q`

- [ ] **Step 3: Remove `default_model_id` from refresh results and delete all implicit selection during catalog refresh. Resolve only `task_model_id or record.available_model_id`; fail if absent.**

- [ ] **Step 4: Remove legacy fields from Pydantic update/view contracts. On provider deletion, detect profile references only by joining `AgentProfile.available_model_id` through `AvailableModel.model_provider_id`.**

- [ ] **Step 5: When creating the discovery profile from the plan profile, copy `available_model_id`, not a provider or legacy model string. Keep task/discovery evidence as provider slug plus external model ID.**

- [ ] **Step 6: Run the focused tests and the backend API suite.**

Run: `cd backend && uv run pytest tests/test_model_providers.py tests/test_model_provider_api.py tests/test_discovery_runtime.py tests/test_api.py -q`

- [ ] **Step 7: Commit the runtime slice.**

```bash
git add backend/carlo/model_providers.py backend/carlo/api.py backend/carlo/admin.py backend/carlo/discovery_runtime.py backend/tests
git commit -m "refactor: resolve concrete agent models"
```

### Task 3: Concrete Coding agents selector

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/SettingsView.tsx`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: provider and available-model catalogs.
- Produces: a required concrete-model selector grouped by provider and saving only `{ available_model_id }`.

- [ ] **Step 1: Update the frontend test to assert Models has no `Default` or `Use as default`, Coding agents shows `omlx — Qwen3.8-27B-oQ8e-fp16-mtp`, and saving sends only `available_model_id`.**

- [ ] **Step 2: Run the frontend test and verify it fails on the old default controls and payload.**

Run: `cd frontend && npm test -- --run src/App.test.tsx`

- [ ] **Step 3: Remove obsolete fields from TypeScript API types and request payloads. Simplify `ModelRow` by deleting default props and controls.**

- [ ] **Step 4: Render one `<optgroup>` per active provider. Include only selectable catalog models and label every option `${provider.slug} — ${model.external_id}`. Save `{ available_model_id: Number(value) }`.**

- [ ] **Step 5: Run frontend tests and production build.**

Run: `cd frontend && npm test -- --run && npm run build`

- [ ] **Step 6: Commit the UI slice.**

```bash
git add frontend/src/api.ts frontend/src/SettingsView.tsx frontend/src/App.test.tsx
git commit -m "refactor: select concrete coding agent models"
```

### Task 4: CARLO-managed external skills

**Files:**
- Modify: `backend/carlo/config.py`
- Modify: `backend/carlo/maintenance.py`
- Modify: `backend/carlo/provider.py`
- Modify: `backend/carlo/pi_runtime.py`
- Modify: `backend/carlo/telegram.py`
- Test: `backend/tests/test_maintenance.py`
- Test: `backend/tests/test_provider.py`

**Interfaces:**
- Produces: managed checkouts for `https://github.com/obra/superpowers.git` and `https://github.com/anthropics/skills.git` under CARLO's artifact root.
- Produces: one `pi.resources_updated` or `pi.resources_update_failed` event with repository commit IDs.
- Consumes: the existing exclusive `pi_process_lock`, so updates cannot mutate resources used by a live Pi session.

- [ ] **Step 1: Write tests with local Git repositories proving the weekly updater clones missing sources, fast-forwards existing sources, verifies `skills/frontend-design/SKILL.md` and the Superpowers Pi package, records commit IDs, skips a second update within seven days, and retains the previous checkout on failure.**

- [ ] **Step 2: Write a provider test proving the Pi command receives the managed Superpowers package and explicit frontend-design skill while unrelated Anthropic skills are absent.**

- [ ] **Step 3: Run the tests and verify they fail because managed resources do not exist.**

Run: `cd backend && uv run pytest tests/test_maintenance.py tests/test_provider.py -q`

- [ ] **Step 4: Add a small Git updater using `asyncio.create_subprocess_exec`. Use fixed official repository URLs, `git clone` for a missing checkout and `git pull --ff-only` for an existing clean checkout. Run it inside the existing exclusive Pi lock after the Pi npm update.**

- [ ] **Step 5: Pass the managed Superpowers checkout as a Pi package/extension path and `anthropics/skills/skills/frontend-design` as the only Anthropic skill path. Persist the resolved commit IDs in the session manifest and execution evidence.**

- [ ] **Step 6: Add concise Telegram formatting for one success/failure event; do not notify once per repository.**

- [ ] **Step 7: Run the focused tests.**

Run: `cd backend && uv run pytest tests/test_maintenance.py tests/test_provider.py tests/test_telegram.py -q`

- [ ] **Step 8: Commit the managed-resource slice.**

```bash
git add backend/carlo/config.py backend/carlo/maintenance.py backend/carlo/provider.py backend/carlo/pi_runtime.py backend/carlo/telegram.py backend/tests
git commit -m "feat: manage Pi skills with CARLO"
```

### Task 5: Full verification and documentation

**Files:**
- Modify: `README.md`
- Modify: `.env.example` only if the managed resource root needs an operator-visible override.

**Interfaces:**
- Produces: documented model selection and weekly skill update behavior.

- [ ] **Step 1: Document that providers only expose catalogs, profiles select concrete models, and CARLO updates the two official skill sources weekly. Include the managed checkout location and Telegram event behavior.**

- [ ] **Step 2: Upgrade a disposable PostgreSQL database to Alembic head.**

Run: `cd backend && CARLO_DATABASE_URL=postgresql+psycopg:///carlov3_merge_test uv run alembic upgrade head`

- [ ] **Step 3: Run the full project checks.**

Run: `make test`

- [ ] **Step 4: Inspect `git diff --check` and `git status --short`; preserve the user's untracked `screenshots/` directory.**

- [ ] **Step 5: Commit documentation or final corrections.**

```bash
git add README.md .env.example
git commit -m "docs: explain managed Pi models and skills"
```
