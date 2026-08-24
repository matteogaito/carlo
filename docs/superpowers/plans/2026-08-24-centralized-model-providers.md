# Centralized Model Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PostgreSQL-backed CARLO Settings the source of truth for inference models and per-session Pi context/compaction configuration.

**Architecture:** CARLO stores OpenAI-compatible Model Providers, authenticated-encrypted credentials, discovered Models, and one Pi compaction policy. Before every Pi process, CARLO resolves a model selection and materializes an isolated, secret-free Pi agent directory; supported settings remain command-line arguments and credentials enter only through the child environment.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2, PostgreSQL, Alembic, `cryptography` AES-GCM, asyncio/stdlib HTTP, React, Vite, TypeScript.

**Spec:** `docs/superpowers/specs/2026-08-24-centralized-model-providers-design.md`

## Global Constraints

- `Model Provider` is an inference endpoint; `CodingAgentProvider` remains the external coding-agent harness.
- PostgreSQL is authoritative for every Pi runtime setting used by CARLO.
- CARLO must not mutate or delete the user's `~/.pi/agent` directory.
- API credentials are encrypted at rest and never returned, logged, placed in argv, or written to snapshots.
- Refresh active Model Providers every 15 minutes and retain unavailable Models instead of deleting them.
- Pi compaction defaults are `reserve = max(maxTokens, contextWindow × 10%)` and `keepRecent = contextWindow × 20%`.
- Existing Task, Discovery, Action, authentication, and legacy Agent Profile behavior remains compatible during migration.
- Do not add an OMLX-specific adapter; OMLX and OpenRouter use `openai-compatible`.
- Do not touch or commit `screenshots/`.

---

## File map

- `backend/carlo/model_providers.py`: credential encryption, catalog parsing/synchronization, selection resolution, and compaction calculation.
- `backend/carlo/pi_runtime.py`: atomic, per-session Pi snapshot materialization and managed snapshot cleanup.
- `backend/carlo/models.py`: Model Provider, Model, Pi settings, and model-selection foreign keys.
- `backend/carlo/provider.py`: provider-neutral resolved model data and Pi launch translation.
- `backend/carlo/api.py`: authenticated Settings CRUD/actions and Task model override.
- `backend/carlo/maintenance.py`: periodic Model Provider refresh using the existing maintenance loop.
- `backend/carlo/orchestrator.py`, `backend/carlo/discovery_runtime.py`, `backend/carlo/worker.py`, `backend/carlo/main.py`: resolve selections and inject the managed runtime into all Pi paths.
- `frontend/src/SettingsView.tsx`: Models and Coding Agents settings workspace.
- `frontend/src/api.ts`, `frontend/src/App.tsx`, `frontend/src/styles.css`: typed API, navigation, and responsive presentation.

### Task 1: Persistent model configuration and encrypted credentials

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/carlo/config.py`
- Modify: `backend/carlo/production.py`
- Modify: `backend/carlo/models.py`
- Create: `backend/carlo/model_providers.py`
- Create: `backend/alembic/versions/b7c8d9e0f1a2_model_providers.py`
- Modify: `.env.example`
- Test: `backend/tests/test_config.py`
- Create: `backend/tests/test_model_providers.py`
- Modify: `backend/tests/test_persistence.py`

**Interfaces:**
- Produces: `CredentialCipher.from_base64(value: str)`, `CredentialCipher.encrypt(secret: str) -> EncryptedCredential`, and `CredentialCipher.decrypt(ciphertext: bytes, nonce: bytes) -> str`.
- Produces: SQLAlchemy `ModelProvider`, `AvailableModel`, and `PiRuntimeSettings` records.
- Produces: nullable `AgentProfile.model_provider_id`, `AgentProfile.available_model_id`, and `Task.available_model_id` selections.

- [ ] **Step 1: Write failing configuration and encryption tests**

```python
def test_credential_key_requires_32_base64_bytes(monkeypatch):
    monkeypatch.setenv("CARLO_CREDENTIAL_ENCRYPTION_KEY", "bad")
    with pytest.raises(ValueError, match="32 base64-encoded bytes"):
        Settings.from_env()


def test_credential_cipher_round_trip_and_random_nonce():
    cipher = CredentialCipher(b"x" * 32)
    first = cipher.encrypt("sk-secret")
    second = cipher.encrypt("sk-secret")
    assert first != second
    assert cipher.decrypt(first.ciphertext, first.nonce) == "sk-secret"
    assert b"sk-secret" not in first.ciphertext
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `cd backend && uv run pytest tests/test_config.py tests/test_model_providers.py -q`

Expected: FAIL because the setting, dependency, and cipher do not exist.

- [ ] **Step 3: Add the minimum authenticated-encryption implementation**

Add `cryptography>=45` and parse one required production value:

```python
@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    ciphertext: bytes
    nonce: bytes


class CredentialCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("credential encryption key must contain 32 bytes")
        self._aes = AESGCM(key)

    @classmethod
    def from_base64(cls, value: str) -> "CredentialCipher": ...

    def encrypt(self, secret: str) -> EncryptedCredential:
        nonce = os.urandom(12)
        return EncryptedCredential(
            self._aes.encrypt(nonce, secret.encode(), b"carlo:model-provider:v1"),
            nonce,
        )
```

Use `CARLO_CREDENTIAL_ENCRYPTION_KEY=CHANGE_ME_BASE64_32_BYTES` in `.env.example`; production validation rejects missing/placeholders while tests and development may inject an explicit key.

- [ ] **Step 4: Add database tests for durable selections and constraints**

```python
provider = ModelProvider(name="Local OMLX", slug="omlx", kind="openai-compatible", base_url="http://127.0.0.1:11435/v1")
session.add(provider)
await session.flush()
model = AvailableModel(model_provider_id=provider.id, external_id="qwen", status="AVAILABLE")
session.add(model)
await session.flush()
provider.default_model_id = model.id
profile = AgentProfile(name="implementation", provider="pi", model_provider_id=provider.id)
task = Task(..., available_model_id=model.id)
await session.commit()
```

Assert uniqueness of provider slugs and `(model_provider_id, external_id)`, and reject Agent Profiles that set both provider-default and concrete-model foreign keys.

- [ ] **Step 5: Implement models and migration**

Create the three tables with the fields from the spec. Use `LargeBinary` for ciphertext/nonce, JSONB for compatibility/raw metadata, and a singleton check `pi_runtime_settings.id = 1`. Add nullable model-selection columns without removing legacy `agent_profiles.model`.

Seed:

```sql
INSERT INTO pi_runtime_settings
    (id, compaction_enabled, reserve_percent, keep_recent_percent)
VALUES (1, true, 10, 20);
```

- [ ] **Step 6: Apply the migration and run persistence tests**

Run: `cd backend && uv run alembic upgrade head`

Run: `cd backend && uv run pytest tests/test_config.py tests/test_model_providers.py tests/test_persistence.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add .env.example backend/pyproject.toml backend/uv.lock backend/carlo/config.py backend/carlo/production.py backend/carlo/models.py backend/carlo/model_providers.py backend/alembic/versions/b7c8d9e0f1a2_model_providers.py backend/tests/test_config.py backend/tests/test_model_providers.py backend/tests/test_persistence.py
git commit -m "feat: persist encrypted model providers"
```

### Task 2: OpenAI-compatible catalog synchronization

**Files:**
- Modify: `backend/carlo/model_providers.py`
- Modify: `backend/carlo/maintenance.py`
- Modify: `backend/carlo/worker.py`
- Test: `backend/tests/test_model_providers.py`
- Modify: `backend/tests/test_maintenance.py`

**Interfaces:**
- Consumes: `CredentialCipher`, `ModelProvider`, and `AvailableModel` from Task 1.
- Produces: `refresh_model_provider(factory, cipher, provider_id, *, now=None) -> ModelRefreshResult`.
- Produces: `refresh_due_model_providers(factory, cipher, *, now=None) -> int`.
- Produces: `ModelRefreshResult(provider_id: int, seen: int, unavailable: int, default_model_id: int | None)`.

- [ ] **Step 1: Write failing parser and synchronization tests**

```python
payload = {
    "object": "list",
    "data": [{"id": "qwen", "max_model_len": 65536}, {"id": "other"}],
}
entries = parse_openai_models(payload)
assert [(entry.external_id, entry.context_window) for entry in entries] == [
    ("qwen", 65536), ("other", None)
]
```

Test a second successful refresh omitting `qwen` marks it unavailable without deleting it. Test a one-model result selects it as default, a multi-model result retains an available default, and a transport failure preserves the previous catalog.

- [ ] **Step 2: Run tests and verify failure**

Run: `cd backend && uv run pytest tests/test_model_providers.py -q`

Expected: FAIL because discovery functions are absent.

- [ ] **Step 3: Implement bounded stdlib HTTP discovery**

Use `urllib.request` inside `asyncio.to_thread`, avoiding another HTTP runtime dependency. Enforce HTTP(S), a 15-second timeout, an 8 MiB response limit, at most 10,000 models, and bounded IDs/names. Send `Authorization: Bearer` only when a credential exists.

```python
async def fetch_openai_models(provider: ModelProvider, api_key: str) -> list[DiscoveredModel]:
    payload = await asyncio.to_thread(_fetch_json, f"{provider.base_url.rstrip('/')}/models", api_key)
    return parse_openai_models(payload)
```

Recognize `context_window`, `context_length`, `max_model_len`, and `max_tokens` when they are positive integers; do not invent missing limits.

- [ ] **Step 4: Implement transactional refresh and audit events**

Decrypt after loading the provider, fetch outside the write transaction, then upsert all returned models in one transaction. Persist `model_provider.refresh_completed`, `model_provider.refresh_failed`, and availability-change events with IDs/counts but no response bodies or credentials.

- [ ] **Step 5: Extend the existing maintenance loop**

Keep the hourly Pi npm update schedule unchanged. Add a 60-second loop tick and select providers whose `last_refresh_attempt_at` is missing or at least 15 minutes old:

```python
await refresh_due_model_providers(factory, cipher)
await update_pi_if_due(factory, npm_executable, pi_executable, lock_path)
await asyncio.sleep(60)
```

Inject the cipher from `worker.run()`; a provider refresh failure must not stop Task, Action, or Discovery loops.

- [ ] **Step 6: Run focused synchronization/maintenance tests**

Run: `cd backend && uv run pytest tests/test_model_providers.py tests/test_maintenance.py -q`

Expected: PASS, including due/not-due scheduling and failed-refresh isolation.

- [ ] **Step 7: Commit**

```bash
git add backend/carlo/model_providers.py backend/carlo/maintenance.py backend/carlo/worker.py backend/tests/test_model_providers.py backend/tests/test_maintenance.py
git commit -m "feat: synchronize model provider catalogs"
```

### Task 3: Administrator Settings APIs

**Files:**
- Modify: `backend/carlo/api.py`
- Create: `backend/tests/test_model_provider_api.py`
- Modify: `backend/tests/test_security_models.py`

**Interfaces:**
- Consumes: Task 1 persistence/cipher and Task 2 refresh operation.
- Produces: `/api/settings/model-providers`, `/api/settings/models`, `/api/settings/pi`, and model-selection updates.
- Produces: write-only `api_key` semantics; responses expose only `credential_configured` and `credential_hint`.

- [ ] **Step 1: Write failing admin API tests**

```python
created = client.post("/api/settings/model-providers", json={
    "name": "Local OMLX",
    "slug": "omlx",
    "kind": "openai-compatible",
    "base_url": "http://127.0.0.1:11435/v1",
    "api_key": "omlx-local",
})
assert created.status_code == 201
assert created.json()["credential_configured"] is True
assert "api_key" not in created.json()
assert "omlx-local" not in created.text
```

Also cover patch-without-key retention, explicit key replacement, delete refusal while referenced, model limit override, default selection, Pi percentages, Agent Profile provider/default selection, and Task concrete-model override.

- [ ] **Step 2: Run API tests and verify failure**

Run: `cd backend && uv run pytest tests/test_model_provider_api.py tests/test_security_models.py -q`

Expected: FAIL with missing routes/models.

- [ ] **Step 3: Add validated request/response contracts**

Use Pydantic boundary models with URL, slug, percentage, positive-token, and mutually-exclusive selection validation. Responses must be assembled explicitly; never use ORM serialization for encrypted fields.

Required endpoints:

```text
GET/POST              /api/settings/model-providers
PATCH/DELETE          /api/settings/model-providers/{id}
POST                  /api/settings/model-providers/{id}/refresh
GET                    /api/settings/models
PATCH                  /api/settings/models/{id}
GET/PATCH              /api/settings/pi
PATCH                  /api/agent-profiles/{name}
PATCH                  /api/tasks/{task_id}/model
```

- [ ] **Step 4: Implement events and conflict behavior**

Return `409` for referenced provider deletion, unavailable/missing-limit model selection, or provider-default selection without a valid default. Persist concise audit events and exclude ciphertext, nonce, raw credentials, and authorization data.

- [ ] **Step 5: Run API and existing auth tests**

Run: `cd backend && uv run pytest tests/test_model_provider_api.py tests/test_security_models.py tests/test_auth.py tests/test_production_api.py -q`

Expected: PASS and non-admin mutations remain forbidden.

- [ ] **Step 6: Commit**

```bash
git add backend/carlo/api.py backend/tests/test_model_provider_api.py backend/tests/test_security_models.py
git commit -m "feat: expose model settings APIs"
```

### Task 4: Immutable Pi runtime snapshots

**Files:**
- Create: `backend/carlo/pi_runtime.py`
- Modify: `backend/carlo/provider.py`
- Modify: `backend/tests/test_provider.py`
- Create: `backend/tests/test_pi_runtime.py`

**Interfaces:**
- Consumes: effective Model/Provider/Pi settings from Task 1.
- Produces: immutable `ResolvedModel` in `provider.py`.
- Produces: `PiRuntimeSnapshotBuilder.materialize(session_id: str, model: ResolvedModel) -> PiRuntimeSnapshot`.
- Produces: `PiRuntimeSnapshot(agent_dir: Path, model_pattern: str, environment: dict[str, str], manifest: dict[str, Any])`.

- [ ] **Step 1: Write failing compaction and snapshot tests**

```python
assert calculate_compaction(65536, 16384, 10, 20) == CompactionTokens(
    reserve_tokens=16384,
    keep_recent_tokens=13107,
)
snapshot = builder.materialize("DIMMELA-1-implementation-1", resolved)
assert json.loads((snapshot.agent_dir / "settings.json").read_text())["compaction"] == {
    "enabled": True,
    "reserveTokens": 16384,
    "keepRecentTokens": 13107,
}
assert "omlx-local" not in (snapshot.agent_dir / "models.json").read_text()
assert snapshot.environment == {"CARLO_PI_MODEL_API_KEY": "omlx-local"}
```

Assert `models.json` uses `$CARLO_PI_MODEL_API_KEY`, atomic replacement leaves no temporary file, a symlink session directory is rejected, and two models produce independent directories/settings.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `cd backend && uv run pytest tests/test_pi_runtime.py tests/test_provider.py -q`

Expected: FAIL because runtime snapshots do not exist.

- [ ] **Step 3: Add resolved-model and snapshot types**

```python
@dataclass(frozen=True, slots=True)
class ResolvedModel:
    model_provider_id: int
    available_model_id: int
    provider_slug: str
    base_url: str
    api: str
    external_id: str
    api_key: str
    context_window: int
    max_tokens: int
    compaction_enabled: bool
    reserve_tokens: int
    keep_recent_tokens: int
```

Write only the selected model/provider into the snapshot. Preserve current Pi compatibility flags from Model Provider JSON and use zero cost defaults.

- [ ] **Step 4: Integrate snapshots with both Pi launch modes**

Extend `AgentProfile` with `resolved_model: ResolvedModel | None = None`. In `run()` and `open_conversation()`:

- materialize before spawn;
- replace the legacy model argument with `snapshot.model_pattern`;
- set `PI_CODING_AGENT_DIR` and the secret environment value;
- merge Discovery environment without permitting it to overwrite reserved CARLO keys;
- keep the current legacy path when `resolved_model is None`.

Do not pass `--api-key`, because argv is observable.

- [ ] **Step 5: Add safe startup cleanup**

`cleanup_incomplete_snapshots()` removes only `.tmp-*` entries directly under the configured CARLO Pi runtime root after verifying the root is not a symlink. Completed snapshots remain audit evidence and may be atomically regenerated by session ID.

- [ ] **Step 6: Run provider/runtime tests**

Run: `cd backend && uv run pytest tests/test_pi_runtime.py tests/test_provider.py -q`

Expected: PASS; fake Pi asserts argv, environment, and snapshot contents.

- [ ] **Step 7: Commit**

```bash
git add backend/carlo/pi_runtime.py backend/carlo/provider.py backend/tests/test_pi_runtime.py backend/tests/test_provider.py
git commit -m "feat: isolate Pi runtime snapshots"
```

### Task 5: Resolve managed models across Task and Discovery workflows

**Files:**
- Modify: `backend/carlo/model_providers.py`
- Modify: `backend/carlo/orchestrator.py`
- Modify: `backend/carlo/discovery_runtime.py`
- Modify: `backend/carlo/api.py`
- Modify: `backend/carlo/worker.py`
- Modify: `backend/carlo/main.py`
- Modify: `backend/tests/fakes.py`
- Modify: `backend/tests/test_orchestration.py`
- Modify: `backend/tests/test_discovery_runtime.py`
- Modify: `backend/tests/test_end_to_end.py`

**Interfaces:**
- Consumes: `ResolvedModel`, snapshot-aware `PiProvider`, and selection foreign keys.
- Produces: `resolve_agent_profile(session, record, cipher, *, task_model_id=None) -> provider.AgentProfile`.
- Produces: persisted public `model_runtime` evidence in planning metadata, Attempts/Events, and Discovery state; secrets excluded.

- [ ] **Step 1: Write failing resolution tests**

```python
profile = await resolve_agent_profile(session, implementation, cipher)
assert profile.resolved_model.external_id == provider_default.external_id

overridden = await resolve_agent_profile(
    session, implementation, cipher, task_model_id=openrouter_model.id
)
assert overridden.resolved_model.external_id == openrouter_model.external_id
```

Cover inactive provider, unavailable concrete model, absent provider default, missing context, invalid compaction, and legacy `record.model` fallback.

- [ ] **Step 2: Run workflow tests and verify failure**

Run: `cd backend && uv run pytest tests/test_model_providers.py tests/test_orchestration.py tests/test_discovery_runtime.py -q`

Expected: FAIL because workflows still construct profiles from model strings.

- [ ] **Step 3: Implement one shared resolver**

Query Pi settings and the selected Model in the same AsyncSession. Concrete Task selection wins over Agent Profile concrete selection, which wins over Agent Profile provider default, which wins over the legacy model string. Decrypt only after all public validation succeeds.

- [ ] **Step 4: Route every Pi construction through the resolver**

Replace direct `AgentProfile(...)` creation in planning API, implementation/escalation/review orchestration, and Discovery runtime. Construct one `CredentialCipher` and one snapshot-aware `PiProvider` in both `worker.py` and `main.py`.

Persist public evidence shaped as:

```json
{
  "model_provider": "omlx",
  "model": "Qwen3.8-27B-oQ8e-fp16-mtp",
  "context_window": 65536,
  "max_tokens": 16384,
  "reserve_tokens": 16384,
  "keep_recent_tokens": 13107
}
```

- [ ] **Step 5: Verify restart and concurrency behavior**

Add a test that resumes a Discovery with its persisted concrete model evidence after the provider default changes. Add a test that two concurrent Discovery processes use different `PI_CODING_AGENT_DIR` values. Existing globally sequential implementation behavior must remain unchanged.

- [ ] **Step 6: Run backend workflow tests**

Run: `cd backend && uv run pytest tests/test_orchestration.py tests/test_discovery_runtime.py tests/test_end_to_end.py tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/carlo/model_providers.py backend/carlo/orchestrator.py backend/carlo/discovery_runtime.py backend/carlo/api.py backend/carlo/worker.py backend/carlo/main.py backend/tests/fakes.py backend/tests/test_orchestration.py backend/tests/test_discovery_runtime.py backend/tests/test_end_to_end.py
git commit -m "feat: run CARLO agents with managed models"
```

### Task 6: Settings workspace and model selection UI

**Files:**
- Create: `frontend/src/SettingsView.tsx`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: Task 3 Settings endpoints.
- Produces: `SettingsView` with `Models` and `Coding Agents` sections.
- Produces: Task implementation-model override control in the existing Task detail.

- [ ] **Step 1: Add failing typed UI tests**

Test navigation and semantics:

```tsx
fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
expect(await screen.findByRole('heading', { name: 'Model providers' })).toBeVisible()
expect(screen.getByText('API key configured')).toBeVisible()
expect(screen.queryByText('omlx-local')).not.toBeInTheDocument()
```

Also test provider creation modal, refresh, default-model selection, context override, Pi percentage preview, profile model selection, Task model override, errors, and mobile section navigation.

- [ ] **Step 2: Run frontend tests and verify failure**

Run: `cd frontend && npm test -- --run`

Expected: FAIL because Settings is absent.

- [ ] **Step 3: Add API types and methods**

Define `ModelProvider`, `AvailableModel`, `PiRuntimeSettings`, and model-selection types. Extend the `Api` interface and `httpApi`; update all test fakes so TypeScript remains exhaustive.

- [ ] **Step 4: Build the Settings workspace**

Use one responsive component with a small internal section switcher:

```tsx
<SettingsView api={api} event={lastEvent} setError={setError} />
```

Provider cards show health, endpoint, model count, default, and last refresh. Credential inputs always mean replace and never prefill. Model rows show effective/discovered/overridden limits and status. Pi settings show editable percentages and calculated examples for selected models.

- [ ] **Step 5: Add navigation and Task override**

Extend the main view union with `settings`, add the fourth navigation button, and ensure the mobile bottom navigation remains reachable. In Task detail, offer `Project/Profile default` plus available concrete Models; describe it as an implementation override.

- [ ] **Step 6: Validate tests and production build**

Run: `cd frontend && npm test -- --run`

Run: `cd frontend && npm run build`

Expected: all tests pass and Vite completes without type errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/SettingsView.tsx frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css frontend/src/App.test.tsx
git commit -m "feat: manage models from Settings"
```

### Task 7: Production setup, migration compatibility, and full verification

**Files:**
- Modify: `README.md`
- Modify: `Makefile`
- Modify: `.env.example`
- Modify: `backend/tests/test_production_flow.py`
- Modify: `backend/tests/test_end_to_end.py`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: documented encryption-key generation, first Model Provider setup, migration path, and verified production installation.

- [ ] **Step 1: Write failing production-flow assertions**

Assert production validation refuses a placeholder encryption key, `make install-mac` retains the configured key, Alembic upgrades an existing schema, and a legacy profile can run before being migrated.

- [ ] **Step 2: Run production tests and verify failure**

Run: `cd backend && uv run pytest tests/test_production_flow.py tests/test_end_to_end.py -q`

Expected: FAIL until documentation/setup and compatibility wiring are complete.

- [ ] **Step 3: Document exact setup**

Include a safe key-generation command that prints but does not write secrets:

```bash
openssl rand -base64 32
```

Document `Settings → Models → Add provider`, OMLX URL/key, Refresh, default model, Agent Profile selection, proportional compaction, Task overrides, and that CARLO ignores rather than deletes `~/.pi/agent`.

- [ ] **Step 4: Run migrations and the complete backend suite**

Run: `cd backend && uv run alembic upgrade head`

Run: `make test`

Expected: all backend, frontend, extension, migration, and production checks pass.

- [ ] **Step 5: Inspect secret leakage and generated artifacts**

Run:

```bash
rg -n "omlx-local|sk-[A-Za-z0-9]" .carlo backend frontend --glob '!*.lock'
```

Expected: no credential value in tracked files, snapshots, API fixtures, or logs; deliberately synthetic test literals appear only in tests.

- [ ] **Step 6: Verify repository state and final diff**

Run: `git diff --check`

Run: `git status --short --branch`

Expected: only intended files are changed; `screenshots/` remains untracked and untouched.

- [ ] **Step 7: Commit**

```bash
git add README.md Makefile .env.example backend/tests/test_production_flow.py backend/tests/test_end_to_end.py
git commit -m "docs: centralize Pi model configuration"
```

## Self-review

- Spec coverage: persistence, encryption, discovery, provider defaults, unavailable retention, proportional compaction, immutable snapshots, Task override, restart/concurrency, Settings UI, security, compatibility, and validation each map to a task.
- Placeholder scan: no `TBD`, deferred implementation step, or undefined “appropriate handling” remains.
- Type consistency: `ModelProvider`, `AvailableModel`, `PiRuntimeSettings`, `CredentialCipher`, `ResolvedModel`, `PiRuntimeSnapshot`, and `resolve_agent_profile` retain the same names and responsibilities across tasks.
