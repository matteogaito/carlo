# Managed Pi Packages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace CARLO's hard-coded Pi resources with database-managed npm/Git packages that Pi installs atomically, updates weekly, and loads reproducibly in every selected session.

**Architecture:** PostgreSQL stores desired package sources and global/profile selection while a focused `PiPackageManager` invokes Pi's native package commands inside a staging `PI_CODING_AGENT_DIR`. Successful staging directories are promoted to immutable artifacts; profile resolution selects database records and `PiProvider` loads their promoted local roots through Pi's package loader. Old artifacts remain available to historical task snapshots, and a failed refresh leaves the active version untouched.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2, PostgreSQL/JSONB, Alembic, asyncio subprocesses, React, TypeScript, Vitest, Pi CLI.

**Spec:** `docs/superpowers/specs/2026-08-24-managed-pi-packages-design.md`

## Global Constraints

- Package sources are explicit Pi-compatible npm or Git sources; local filesystem sources are rejected.
- Unpinned packages update weekly; explicit npm versions and Git refs remain pinned.
- Package installation and refresh use Pi's native commands in a CARLO-owned staging agent directory.
- Failed updates retain the previous active artifact and produce one concise aggregate Telegram notification.
- Global defaults are inherited by every profile and cannot be excluded locally.
- Running and historical sessions retain their exact package artifact/version evidence.
- Package extensions execute code, so every package mutation is admin-only, origin-protected, validated, and audited.
- Existing tasks, profiles, standalone skills, model selections, and historical `skills: [ponytail]` plans remain compatible.
- Do not add another agent framework or a custom npm/Git dependency resolver.

---

### Task 1: Restore Historical Plan Compatibility

**Files:**
- Modify: `backend/carlo/model_providers.py`
- Modify: `backend/carlo/orchestrator.py`
- Test: `backend/tests/test_model_providers.py`
- Test: `backend/tests/test_orchestration.py`

**Interfaces:**
- Consumes: existing `resolve_agent_profile(..., extra_skills: tuple[str, ...])` and `PlanRevision.metadata_json["skills"]`.
- Produces: `partition_plan_resources(names, package_names, skill_names) -> tuple[tuple[str, ...], tuple[str, ...]]`, returning package requests first and standalone skill requests second.

- [ ] **Step 1: Add the DIMMELA-1 regression test**

Extend `test_agent_profile_resolution_uses_concrete_model_and_task_override`, whose fixture already creates the provider, model, runtime defaults, and `plan_profile`. Resolve that profile once more with the historical Plan value:

```python
historical = await module.resolve_agent_profile(
    session, plan_profile, cipher, extra_skills=("ponytail",)
)

assert historical.packages == ("superpowers", "ponytail")
assert "ponytail" not in historical.skills
```

- [ ] **Step 2: Run the regression and verify the reported failure**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_model_providers.py -q
```

Expected: FAIL with `agent profile has unknown skills: ponytail`.

- [ ] **Step 3: Implement the minimal compatibility partition**

Add the pure helper and apply it only to `extra_skills`; stored global/profile skill configuration must continue to reject package names:

```python
def partition_plan_resources(
    names: tuple[str, ...],
    package_names: set[str],
    skill_names: set[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    packages = tuple(dict.fromkeys(name for name in names if name in package_names))
    skills = tuple(dict.fromkeys(name for name in names if name in skill_names))
    unknown = sorted(set(names) - package_names - skill_names)
    if unknown:
        raise ModelProviderError(
            f"plan has unknown resources: {', '.join(unknown)}"
        )
    return packages, skills
```

Union the returned package requests into `packages` and only the returned standalone skills into `skills`.

- [ ] **Step 4: Verify resolver and orchestration behavior**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_model_providers.py tests/test_orchestration.py -q
```

Expected: PASS, including an orchestration assertion that the provider receives Ponytail in `profile.packages` and not `profile.skills`.

- [ ] **Step 5: Commit the production regression fix**

```bash
git add backend/carlo/model_providers.py backend/carlo/orchestrator.py backend/tests/test_model_providers.py backend/tests/test_orchestration.py
git commit -m "fix: normalize historical plan resources"
```

---

### Task 2: Persist Generic Pi Package Configuration

**Files:**
- Create: `backend/alembic/versions/f2a3b4c5d6e7_pi_package_catalog.py`
- Modify: `backend/carlo/models.py`
- Modify: `backend/tests/test_migrations.py`
- Create: `backend/tests/test_pi_packages.py`

**Interfaces:**
- Produces: `PiPackage` and `AgentProfilePackage` ORM records.
- Produces: `PiPackage.active_artifact_path`, `PiPackage.active_version`, and `PiPackage.resources` as the runtime lookup contract.
- Preserves: API-facing names `default_packages` during the transition; later tasks translate them to package identities.

- [ ] **Step 1: Write schema assertions before adding models**

```python
def test_managed_pi_package_schema() -> None:
    assert {"source", "identity", "enabled", "pinned", "is_default"}.issubset(
        PiPackage.__table__.columns.keys()
    )
    assert {
        "active_version",
        "active_artifact_path",
        "resources",
        "last_update_status",
        "last_update_error",
    }.issubset(PiPackage.__table__.columns.keys())
    assert AgentProfilePackage.__table__.c.package_id.foreign_keys
```

Add a migration-chain assertion that Alembic has exactly one head and that the new revision follows `e1f2a3b4c5d6`.

- [ ] **Step 2: Verify the schema test fails**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_migrations.py -q
```

Expected: FAIL because `PiPackage` and association models do not exist.

- [ ] **Step 3: Add the package catalog models**

Use one package table plus one profile association table; `PiPackage.is_default` is the only global-default source of truth. Do not introduce a revision table because immutable artifact directories and event evidence already preserve old revisions:

```python
class PiPackage(TimestampMixin, Base):
    __tablename__ = "pi_packages"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    identity: Mapped[str] = mapped_column(String(255), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    active_version: Mapped[str | None] = mapped_column(String(255))
    active_artifact_path: Mapped[str | None] = mapped_column(Text)
    resources: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_update_attempt_at: Mapped[datetime | None]
    last_update_success_at: Mapped[datetime | None]
    last_update_status: Mapped[str] = mapped_column(String(20), default="NEVER")
    last_update_error: Mapped[str | None] = mapped_column(Text)
```

Association rows use composite primary keys and `ON DELETE CASCADE`; package deletion remains disabled at the API until reference-safe garbage collection exists.

- [ ] **Step 4: Add and exercise the Alembic migration**

The migration must:

1. create the two tables and indexes;
2. seed `git:github.com/obra/superpowers` and `git:github.com/DietrichGebert/ponytail`;
3. mark both enabled and default when present in the existing singleton defaults;
4. translate existing profile package names into association rows;
5. retain the old JSON columns for one compatibility release, with the new association tables authoritative after bootstrap.

Run:

```bash
cd backend && CARLO_DATABASE_URL=postgresql+psycopg:///carlov3_merge_test UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run alembic upgrade head
cd backend && CARLO_DATABASE_URL=postgresql+psycopg:///carlov3_merge_test UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run alembic check
```

Expected: upgrade succeeds and Alembic reports `No new upgrade operations detected.`

- [ ] **Step 5: Verify persistence and commit**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_migrations.py tests/test_pi_packages.py -q
```

Expected: PASS.

```bash
git add backend/carlo/models.py backend/alembic/versions/f2a3b4c5d6e7_pi_package_catalog.py backend/tests/test_migrations.py backend/tests/test_pi_packages.py
git commit -m "feat: persist managed Pi packages"
```

---

### Task 3: Install and Promote Packages Through Pi

**Files:**
- Create: `backend/carlo/pi_packages.py`
- Modify: `backend/carlo/config.py`
- Modify: `backend/tests/test_pi_packages.py`

**Interfaces:**
- Produces: `PackageSource(identity: str, pinned: bool)` and `parse_package_source(source: str) -> PackageSource`.
- Produces: `InstalledPiPackage(identity, source, resolved_version, artifact_path, resources)`.
- Produces: `PiPackageManager.install(source: str) -> InstalledPiPackage`.
- Consumes: Pi executable, artifact root, and the shared `pi-runtime.lock`.

- [ ] **Step 1: Write source-boundary tests**

Cover these exact inputs:

```python
@pytest.mark.parametrize(
    ("source", "identity", "pinned"),
    [
        ("npm:ponytail", "npm:ponytail", False),
        ("npm:@scope/pippo", "npm:@scope/pippo", False),
        ("npm:@scope/pippo@1.2.3", "npm:@scope/pippo", True),
        ("git:github.com/owner/repo", "git:github.com/owner/repo", False),
        ("git:github.com/owner/repo@v1", "git:github.com/owner/repo", True),
    ],
)
def test_parse_package_source(source: str, identity: str, pinned: bool) -> None:
    assert parse_package_source(source) == PackageSource(identity, pinned)


@pytest.mark.parametrize("source", ["", "../local", "/tmp/package", "file:local"])
def test_rejects_nonportable_package_sources(source: str) -> None:
    with pytest.raises(PiPackageError, match="npm or Git"):
        parse_package_source(source)
```

- [ ] **Step 2: Write a fake-Pi staged-install test**

Create an executable fixture that reads `PI_CODING_AGENT_DIR`, writes a Pi-compatible package root containing:

```json
{
  "name": "pippo",
  "version": "1.4.0",
  "pi": {
    "extensions": ["./extensions/index.js"],
    "skills": ["./skills"]
  }
}
```

The test must assert that `PiPackageManager.install("npm:pippo")` invokes:

```text
pi install npm:pippo --no-approve
```

and returns a promoted immutable directory containing the manifest and resources `extensions/index.js` and `skills/pippo/SKILL.md`.

- [ ] **Step 3: Run the package tests and verify failure**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_pi_packages.py -q
```

Expected: FAIL because `carlo.pi_packages` does not exist.

- [ ] **Step 4: Implement staging with the standard library**

Implement the manager with `tempfile.mkdtemp`, `asyncio.create_subprocess_exec`, `json`, `Path`, and `os.replace`; add no package-management dependency. Its command environment must set only the staging agent directory while preserving the service process environment:

```python
environment = {
    **os.environ,
    "PI_CODING_AGENT_DIR": str(staging_agent_dir),
}
process = await asyncio.create_subprocess_exec(
    self.pi_executable,
    "install",
    source,
    "--no-approve",
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env=environment,
)
```

Inspect only `agent/npm/node_modules/<package>` or `agent/git/<package>` roots written by Pi. Require `package.json`, parse its optional `pi` object, and verify every declared local path remains inside the package root. Promote to:

```text
<artifact-root>/pi-packages/<safe-identity>/<resolved-version-or-revision>/
```

Reject symlinked roots and path traversal. Cap captured error output at 500 characters.

- [ ] **Step 5: Add atomic failure and duplicate tests**

The failed-update test installs version `1.4.0`, changes the fake Pi executable to exit `4`, and asserts the previously returned artifact remains intact. A duplicate identity test must reject `npm:pippo@1.4.0` when `npm:pippo` is already registered.

- [ ] **Step 6: Verify and commit the manager**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_pi_packages.py -q
```

Expected: PASS.

```bash
git add backend/carlo/pi_packages.py backend/carlo/config.py backend/tests/test_pi_packages.py
git commit -m "feat: install Pi packages atomically"
```

---

### Task 4: Make Maintenance Database-Driven and Recoverable

**Files:**
- Modify: `backend/carlo/maintenance.py`
- Modify: `backend/carlo/main.py`
- Modify: `backend/carlo/worker.py`
- Modify: `backend/carlo/telegram.py`
- Modify: `backend/tests/test_maintenance.py`
- Modify: `backend/tests/test_production_api.py`
- Modify: `backend/tests/test_telegram.py`

**Interfaces:**
- Produces: `refresh_managed_pi_packages(factory, manager, now) -> PackageRefreshSummary`.
- Produces: `ensure_managed_pi_packages(factory, manager) -> None`.
- `PackageRefreshSummary` contains immutable tuples `updated`, `unchanged`, and `failed` and a concise `as_payload()` result.

- [ ] **Step 1: Write the previous-version retention test**

Persist an enabled unpinned package with a valid old artifact, make the fake Pi installation fail, and assert:

```python
assert package.active_version == "1.4.0"
assert package.active_artifact_path == str(old_artifact)
assert package.last_update_status == "FAILED"
assert "registry unavailable" in package.last_update_error
```

Add a second call five minutes later and assert no subprocess call occurs because the existing one-hour failure backoff applies.

- [ ] **Step 2: Write aggregate-event and pinning tests**

With one updated package, one unchanged pinned package, and one failed package, assert exactly one event:

```python
assert event.type == "pi.packages_update_completed"
assert event.payload == {
    "updated": ["npm:pippo@1.5.0"],
    "unchanged": ["npm:frozen@2.0.0"],
    "failed": ["git:github.com/owner/broken"],
    "summary": "1 updated · 1 unchanged · 1 failed",
}
```

- [ ] **Step 3: Verify the maintenance tests fail**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_maintenance.py tests/test_telegram.py -q
```

Expected: FAIL because maintenance still iterates `MANAGED_PI_RESOURCES`.

- [ ] **Step 4: Replace descriptor maintenance with package rows**

Delete `ManagedPiResource`, `MANAGED_PI_RESOURCES`, `_update_resource`, and the hard-coded manifest completeness logic after the generic package path is covered. Query enabled packages, skip pinned packages with an active artifact, honor the seven-day success and one-hour failure intervals, and promote each successful result in its own short database transaction.

`ensure_managed_pi_packages` must verify every enabled package selected globally or by a profile. It attempts recovery when the active artifact is missing and raises:

```text
managed Pi packages are unavailable: <identity>, <identity>
```

only when no valid old artifact can serve the affected sessions.

- [ ] **Step 5: Wire API and worker startup to the generic readiness gate**

Construct one `PiPackageManager` from `settings.pi_executable`, `settings.artifact_root`, and the existing lock path. Both FastAPI lifespan bootstrap and worker bootstrap call `ensure_managed_pi_packages` before accepting Pi work. The maintenance loop receives the same manager and does not know any package names.

- [ ] **Step 6: Replace Telegram resource events with one package summary**

Map `pi.packages_update_completed` to title `Pi packages updated` and fields `summary`, `updated`, `unchanged`, `failed`. Remove the old per-descriptor `pi.resources_updated` and `pi.resources_update_failed` notification mappings after migration tests prove no active producer remains.

- [ ] **Step 7: Verify recovery, backoff, startup, and Telegram**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_maintenance.py tests/test_production_api.py tests/test_telegram.py -q
```

Expected: PASS with one notification event per cycle.

- [ ] **Step 8: Commit maintenance**

```bash
git add backend/carlo/maintenance.py backend/carlo/main.py backend/carlo/worker.py backend/carlo/telegram.py backend/tests/test_maintenance.py backend/tests/test_production_api.py backend/tests/test_telegram.py
git commit -m "feat: maintain Pi packages generically"
```

---

### Task 5: Resolve and Snapshot Database Packages in Pi Sessions

**Files:**
- Modify: `backend/carlo/model_providers.py`
- Modify: `backend/carlo/provider.py`
- Modify: `backend/carlo/pi_runtime.py`
- Modify: `backend/carlo/discovery_runtime.py`
- Modify: `backend/tests/test_model_providers.py`
- Modify: `backend/tests/test_provider.py`
- Modify: `backend/tests/test_discovery_runtime.py`

**Interfaces:**
- Replaces: `AgentProfile.packages: tuple[str, ...]` with `AgentProfile.packages: tuple[ResolvedPiPackage, ...]`.
- Produces: `ResolvedPiPackage(package_id, identity, source, version, artifact_path, resources)`.
- Preserves: `AgentResult.resource_revisions`, now keyed by package identity and populated from resolved database evidence rather than `revisions.json`.

- [ ] **Step 1: Write a dynamic package resolution test**

Persist `npm:pippo`, mark it default, and attach `git:github.com/owner/profile-tools` only to the implementation profile. Assert:

```python
assert [package.identity for package in resolved.packages] == [
    "npm:pippo",
    "git:github.com/owner/profile-tools",
]
assert resolved.packages[0].version == "1.5.0"
assert resolved.packages[0].artifact_path == str(pippo_artifact)
```

No test or production constant may contain a set of allowed package names.

- [ ] **Step 2: Write provider command and snapshot evidence tests**

Run the provider with two resolved packages and assert Pi receives their immutable local roots as repeated extension/package sources:

```python
assert argv[:4] == [
    "-e", str(pippo_artifact),
    "-e", str(profile_tools_artifact),
]
assert result.resource_revisions == {
    "npm:pippo": "1.5.0",
    "git:github.com/owner/profile-tools": "a" * 40,
}
```

Also assert the runtime manifest stores source, resolved version, and resource names for both packages.

- [ ] **Step 3: Verify tests fail against name-based snapshots**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_model_providers.py tests/test_provider.py tests/test_discovery_runtime.py -q
```

Expected: FAIL because profile resolution still uses `MANAGED_PROFILE_PACKAGES` and the provider still reads `revisions.json`.

- [ ] **Step 4: Add the resolved package value type**

```python
@dataclass(frozen=True, slots=True)
class ResolvedPiPackage:
    package_id: int
    identity: str
    source: str
    version: str
    artifact_path: str
    resources: dict[str, tuple[str, ...]]
```

`resolve_agent_profile` queries global associations plus profile associations in deterministic identity order, rejects disabled/unavailable records, and returns resolved values. Package-provided skills satisfy compatible historical plan requests by matching the declared `resources["skills"]` names.

- [ ] **Step 5: Remove provider package constants and manifest lookup**

Delete `managed_packages`, package filtering in `_resource_snapshot`, and the package portion of `resource_manifest`. `_package_arguments` validates each artifact path is absolute, exists, is a directory, and is not a symlink before adding `-e <artifact_path>`. Keep the standalone managed skill snapshot path for `frontend-design` until it is migrated as its own registered package or standalone skill source.

- [ ] **Step 6: Persist immutable package evidence**

Extend `PiRuntimeSnapshotBuilder.materialize` with a `packages` argument and write this exact manifest shape:

```json
{
  "packages": [
    {
      "id": 12,
      "identity": "npm:pippo",
      "source": "npm:pippo",
      "version": "1.5.0",
      "artifact_path": "/absolute/artifact/path",
      "resources": {"skills": ["pippo"]}
    }
  ]
}
```

Never rewrite a session's existing manifest with a newer package revision.

- [ ] **Step 7: Verify provider and Discovery compatibility**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_model_providers.py tests/test_provider.py tests/test_discovery_runtime.py -q
```

Expected: PASS with no hard-coded Superpowers/Ponytail construction in `main.py`, `worker.py`, or Discovery fallback profiles.

- [ ] **Step 8: Commit runtime package resolution**

```bash
git add backend/carlo/model_providers.py backend/carlo/provider.py backend/carlo/pi_runtime.py backend/carlo/discovery_runtime.py backend/tests/test_model_providers.py backend/tests/test_provider.py backend/tests/test_discovery_runtime.py
git commit -m "feat: snapshot database Pi packages"
```

---

### Task 6: Add the Admin Package API and Settings UI

**Files:**
- Modify: `backend/carlo/api.py`
- Modify: `backend/tests/test_model_provider_api.py`
- Modify: `backend/tests/test_production_api.py`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/SettingsView.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Produces: `GET /api/settings/pi-packages`.
- Produces: `POST /api/settings/pi-packages` with `{source, is_default}`.
- Produces: `PATCH /api/settings/pi-packages/{id}` with `{enabled?, is_default?}`.
- Produces: `POST /api/settings/pi-packages/{id}/update`.
- Produces: `DELETE /api/settings/pi-packages/{id}` as disable-only while referenced.

- [ ] **Step 1: Write API authorization and lifecycle tests**

Test that anonymous and non-admin package mutations are rejected. With an admin session, create:

```json
{"source": "npm:pippo", "is_default": true}
```

and assert the response includes:

```json
{
  "identity": "npm:pippo",
  "source": "npm:pippo",
  "enabled": true,
  "is_default": true,
  "pinned": false,
  "active_version": "1.5.0",
  "last_update_status": "SUCCESS"
}
```

Test duplicate identity returns 409, invalid/local source returns 422, and a failed first installation returns 502 without creating an enabled/default record.

- [ ] **Step 2: Verify API tests fail**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_model_provider_api.py tests/test_production_api.py -q
```

Expected: FAIL with missing `/api/settings/pi-packages` routes.

- [ ] **Step 3: Implement thin API orchestration**

Inject the shared `PiPackageManager` into `create_app` as an optional dependency used by package routes. Route handlers validate Pydantic payloads, invoke the manager/service, persist one audit `Event`, and return `_pi_package_view(package)`. They must not construct npm/Git commands themselves.

Keep `GET /settings/packages` as a compatibility alias for one release, returning the registered enabled package catalog rather than constants. Update profile writes to resolve submitted identities to package records and association rows.

- [ ] **Step 4: Write the Settings UI test**

Assert the UI can:

1. open **Coding agents**;
2. click **Add package**;
3. submit `npm:pippo` with Default enabled;
4. show `pippo`, `1.5.0`, `Unpinned`, and `SUCCESS`;
5. invoke **Update now**;
6. show package-provided skills as read-only resources;
7. keep the package checked and disabled inside every profile because it is globally inherited.

The API mock must assert exact request payloads and a visible `Saved ✓` or install error state.

- [ ] **Step 5: Verify the frontend test fails**

Run:

```bash
cd frontend && npm test -- --run
```

Expected: FAIL because no add-package dialog or dynamic package API exists.

- [ ] **Step 6: Implement the vertical package editor**

Replace the static package checkbox ledger in `DefaultResources` with package cards and a compact modal. Each card displays source, version/revision, pinning, last update status, Default toggle, Update now, and Disable. Use existing modal, status-chip, form, and API error patterns; add no UI dependency.

Profile cards consume the same package list. Default packages remain checked/disabled while explicit profile selections are preserved independently, matching the existing inherited-resource regression behavior.

- [ ] **Step 7: Verify backend and frontend package management**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_model_provider_api.py tests/test_production_api.py -q
cd frontend && npm test -- --run
cd frontend && npm run build
```

Expected: all commands pass.

- [ ] **Step 8: Commit API and UI**

```bash
git add backend/carlo/api.py backend/tests/test_model_provider_api.py backend/tests/test_production_api.py frontend/src/api.ts frontend/src/SettingsView.tsx frontend/src/styles.css frontend/src/App.test.tsx
git commit -m "feat: manage Pi packages in Settings"
```

---

### Task 7: Separate Planning Metadata and Finish Audit Evidence

**Files:**
- Modify: `skills/carlo-planning/SKILL.md`
- Modify: `skills/carlo-discovery/SKILL.md`
- Modify: `backend/carlo/planning.py`
- Modify: `backend/carlo/api.py`
- Modify: `backend/carlo/orchestrator.py`
- Modify: `backend/tests/test_planning.py`
- Modify: `backend/tests/test_api.py`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `README.md`

**Interfaces:**
- Planning metadata produces distinct `packages: list[str]` and `skills: list[str]` arrays.
- Task detail produces `loaded_packages` entries with identity, source, and exact version plus `used_skills` based only on runtime evidence.
- Historical `skills`-only plan metadata remains accepted by Task 1/Task 5 normalization.

- [ ] **Step 1: Write planning-contract tests**

Assert the planning skill and parser require this structured metadata:

```json
{
  "packages": ["npm:@dietrichgebert/ponytail"],
  "skills": ["frontend-design"],
  "validation_commands": ["make test"]
}
```

Add a parser test proving an old payload with `"skills": ["ponytail"]` still loads and is normalized during execution rather than rejected during plan display.

- [ ] **Step 2: Write task-detail evidence tests**

Persist an `agent.completed` event containing package evidence and a Pi read event for one `SKILL.md`. Assert:

```python
assert view["loaded_packages"] == [
    {
        "identity": "npm:pippo",
        "source": "npm:pippo",
        "version": "1.5.0",
    }
]
assert view["used_skills"] == ["pippo"]
```

Merely loading a package with six declared skills must not place all six names in `used_skills`.

- [ ] **Step 3: Verify contract tests fail**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_planning.py tests/test_api.py -q
```

Expected: FAIL because planning metadata and task detail do not yet expose package evidence separately.

- [ ] **Step 4: Update planning and Discovery contracts**

Teach both CARLO skills that packages are containers loaded by source/identity while standalone skills are explicit instructional resources. The planner may recommend only package identities present in CARLO's supplied catalog. Update structured-output validation to retain both arrays without a second LLM call.

- [ ] **Step 5: Persist and render honest runtime evidence**

Store resolved package evidence in `agent.completed` and `planning.skills_used` payloads. Keep `_used_skills` evidence-based: explicit `/skill:name` invocation or a successful read/tool reference to the exact resolved `SKILL.md`; do not infer usage from package availability.

Render **Loaded packages** separately from **Skills used** in Task detail. Package rows show exact resolved version/revision and source. Keep Markdown rendering and existing responsive task panel behavior unchanged.

- [ ] **Step 6: Update the README architecture and operations**

Document explicit source entry, weekly automatic unpinned updates, pinned behavior, previous-version fallback, aggregate Telegram notification, and the distinction between package availability and actual skill use. Remove text claiming Superpowers/Ponytail are hard-coded maintenance resources.

- [ ] **Step 7: Run focused contracts and frontend verification**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_planning.py tests/test_api.py -q
cd frontend && npm test -- --run
cd frontend && npm run build
```

Expected: PASS.

- [ ] **Step 8: Commit planning and evidence changes**

```bash
git add skills/carlo-planning/SKILL.md skills/carlo-discovery/SKILL.md backend/carlo/planning.py backend/carlo/api.py backend/carlo/orchestrator.py backend/tests/test_planning.py backend/tests/test_api.py frontend/src/App.tsx frontend/src/App.test.tsx README.md
git commit -m "feat: audit Pi package and skill usage"
```

---

### Task 8: Full Migration, Recovery, and Production Smoke Verification

**Files:**
- Modify: `backend/tests/test_end_to_end.py`
- Modify: `backend/tests/test_persistence.py`
- Modify: `Makefile`
- Modify: `README.md`

**Interfaces:**
- Consumes all previous tasks.
- Produces one end-to-end proof that package configuration survives restart and a historical task can execute with its approved plan.

- [ ] **Step 1: Add restart and historical-task end-to-end tests**

The restart test must:

1. register and install `npm:pippo` through fake Pi;
2. create a Task whose approved historical Plan contains `skills: ["pippo"]`;
3. dispose and recreate engine, factories, manager, provider, and orchestrator;
4. execute the Task;
5. assert Pi receives the promoted package root;
6. assert the Task becomes Done after its declared local validation command;
7. assert exact package evidence remains queryable from Task detail.

The failed-refresh branch updates fake Pi to exit nonzero before restart and proves the old artifact still executes the Task.

- [ ] **Step 2: Run the new tests and fix only integration defects**

Run:

```bash
cd backend && UV_CACHE_DIR=/private/tmp/carlo-uv-cache uv run pytest tests/test_end_to_end.py tests/test_persistence.py -q
```

Expected: PASS. Do not add behavior beyond the approved specification when resolving integration failures.

- [ ] **Step 3: Add an opt-in real Pi package smoke target**

Add `make smoke-pi-packages` that requires `CARLO_SMOKE_NPM_PACKAGE` and `CARLO_SMOKE_GIT_PACKAGE`, uses a temporary CARLO artifact directory, installs both through the production manager, launches a Pi `Reply exactly: OK` session, and leaves production PostgreSQL and `/Users/carlo/.pi/agent` untouched. The default `make test` must not access the network.

- [ ] **Step 4: Run the full deterministic suite**

Run:

```bash
CARLO_DATABASE_URL=postgresql+psycopg:///carlov3_merge_test UV_CACHE_DIR=/private/tmp/carlo-uv-cache make test
```

Expected:

- extension/PWA tests pass;
- Python compilation passes;
- all backend tests pass;
- `alembic check` reports no new operations;
- all frontend tests pass;
- TypeScript and Vite production build pass.

- [ ] **Step 5: Run whitespace and repository checks**

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors; only intended implementation files plus the user-owned `screenshots/` directory appear.

- [ ] **Step 6: Request a correctness review**

Review specifically for package-source parsing, subprocess environment isolation, path traversal/symlink handling, atomic promotion, database/filesystem recovery ordering, historical plan compatibility, and accidental credential/log exposure. Resolve every Critical or Important finding with a failing regression test first.

- [ ] **Step 7: Re-run the full suite after review changes**

```bash
CARLO_DATABASE_URL=postgresql+psycopg:///carlov3_merge_test UV_CACHE_DIR=/private/tmp/carlo-uv-cache make test
```

Expected: complete success from fresh output after the final code change.

- [ ] **Step 8: Commit final integration changes**

```bash
git add Makefile README.md backend/tests/test_end_to_end.py backend/tests/test_persistence.py
git commit -m "test: verify managed Pi package recovery"
```
