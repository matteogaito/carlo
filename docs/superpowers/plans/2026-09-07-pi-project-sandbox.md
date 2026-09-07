# Pi Project Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable the unpinned `pi-sandbox` extension for every Pi session and confine file access to the session's current project while leaving internet access unrestricted.

**Architecture:** Seed `npm:pi-sandbox` into Carlo's existing managed package catalog as a global default. Write the mandatory `sandbox.json` into every session-specific `PI_CODING_AGENT_DIR`, and reject project-local overrides before either JSON or RPC Pi launch.

**Tech Stack:** Python 3.14, SQLAlchemy 2, Alembic, pytest, Pi managed packages, `pi-sandbox`.

**Spec:** `docs/superpowers/specs/2026-09-07-pi-project-sandbox-design.md`

## Global Constraints

- Apply the sandbox to every Pi profile and project.
- Permit file reads and writes only under Pi's current working directory, except runtime/toolchain reads required by the extension.
- Keep internet domains unrestricted.
- Keep `sudo`, Apple Events, browser launching, and additional Unix sockets disabled.
- Do not add a custom `srt` launcher or another sandbox dependency.
- Fail closed when the policy can be overridden or the sandbox cannot initialize.

---

### Task 1: Generate the mandatory Pi sandbox policy

**Files:**
- Modify: `backend/carlo/pi_runtime.py`
- Test: `backend/tests/test_pi_runtime.py`

**Interfaces:**
- Consumes: `PiRuntimeSnapshotBuilder.materialize(session_id, model, packages)` and its session-specific `agent_dir`.
- Produces: `<agent_dir>/sandbox.json`, read automatically by `pi-sandbox` through `PI_CODING_AGENT_DIR`.

- [ ] **Step 1: Write the failing runtime test**

Extend `test_pi_runtime_snapshot_is_isolated_atomic_and_secret_free` with a literal expected policy:

```python
    sandbox = json.loads((snapshot.agent_dir / "sandbox.json").read_text())
    assert sandbox == {
        "enabled": True,
        "sandboxUserShell": True,
        "permissionPromptTimeoutSeconds": 1,
        "allowBrowserProcess": False,
        "network": {
            "allowedDomains": ["*"],
            "deniedDomains": [],
        },
        "filesystem": {
            "denyRead": ["/Users", "/home", "/usr/local/var/carlo/worktrees"],
            "allowRead": ["."],
            "allowWrite": ["."],
            "denyWrite": [".pi/sandbox.json"],
        },
    }
```

This catches omission or weakening of the generated session policy.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_pi_runtime.py::test_pi_runtime_snapshot_is_isolated_atomic_and_secret_free -q
```

Expected: FAIL because `sandbox.json` does not exist.

- [ ] **Step 3: Write the minimal policy file**

In `PiRuntimeSnapshotBuilder.materialize`, use the existing `_write_json` helper:

```python
        self._write_json(
            agent_dir / "sandbox.json",
            {
                "enabled": True,
                "sandboxUserShell": True,
                "permissionPromptTimeoutSeconds": 1,
                "allowBrowserProcess": False,
                "network": {"allowedDomains": ["*"], "deniedDomains": []},
                "filesystem": {
                    "denyRead": ["/Users", "/home", "/usr/local/var/carlo/worktrees"],
                    "allowRead": ["."],
                    "allowWrite": ["."],
                    "denyWrite": [".pi/sandbox.json"],
                },
            },
        )
```

- [ ] **Step 4: Run the runtime tests and verify GREEN**

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_pi_runtime.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/carlo/pi_runtime.py backend/tests/test_pi_runtime.py
git commit -m "feat: generate Pi sandbox policy"
```

### Task 2: Reject project-local sandbox overrides

**Files:**
- Modify: `backend/carlo/provider.py`
- Test: `backend/tests/test_provider.py`

**Interfaces:**
- Consumes: the `cwd` supplied to `PiProvider.run` and `PiProvider.open_conversation`.
- Produces: `_project_boundary(cwd: str) -> str`, returning the resolved directory or raising `ProviderError` before Pi starts.

- [ ] **Step 1: Write failing JSON and RPC launch tests**

Add one parametrized test that invokes both launch paths with `<cwd>/.pi/sandbox.json` present and asserts:

```python
with pytest.raises(ProviderError, match="project-local Pi sandbox configuration is forbidden"):
    await launch(provider, profile, str(project))
assert not marker.exists()
```

Use a fake Pi executable that creates `marker` when started. This catches either launch path starting Pi before validating the boundary.

Add a second assertion to an existing successful launch test that a symlinked `cwd` is resolved before use.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_provider.py -k 'sandbox_configuration or resolves_project_boundary' -q
```

Expected: FAIL because both launch paths currently accept the local override and preserve the symlink spelling.

- [ ] **Step 3: Add the shared boundary check**

Add this single helper to `PiProvider`:

```python
    @staticmethod
    def _project_boundary(cwd: str) -> str:
        project = Path(cwd).resolve(strict=True)
        if not project.is_dir():
            raise ProviderError("Pi project boundary must be a directory")
        if (project / ".pi" / "sandbox.json").exists():
            raise ProviderError("project-local Pi sandbox configuration is forbidden")
        return str(project)
```

Call it once at the start of `run` and `open_conversation`, and use the returned value for subprocess `cwd` and debug replay generation.

- [ ] **Step 4: Run provider tests and verify GREEN**

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_provider.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/carlo/provider.py backend/tests/test_provider.py
git commit -m "fix: reject Pi sandbox overrides"
```

### Task 3: Seed pi-sandbox as an automatically updated global package

**Files:**
- Create: `backend/alembic/versions/e7f8a9b0c1d2_enable_pi_sandbox.py`
- Modify: `backend/tests/test_planning_profile_migration.py`

**Interfaces:**
- Consumes: existing `pi_packages` catalog and `ensure_managed_pi_packages` worker startup flow.
- Produces: an enabled, unpinned, default row with source and identity `npm:pi-sandbox`.

- [ ] **Step 1: Write the failing migration test**

Add `test_pi_sandbox_migration_seeds_global_unpinned_package` using the existing `load_migration` and `run_migration` helpers. Start from `Base.metadata.create_all`, run the new migration upgrade, and assert:

```python
package = await session.scalar(
    select(PiPackage).where(PiPackage.identity == "npm:pi-sandbox")
)
assert package is not None
assert package.source == "npm:pi-sandbox"
assert package.enabled is True
assert package.pinned is False
assert package.is_default is True
```

Run downgrade and assert only that package row is removed.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_planning_profile_migration.py::test_pi_sandbox_migration_seeds_global_unpinned_package -q
```

Expected: FAIL because the migration module does not exist.

- [ ] **Step 3: Add the data migration**

Create revision `e7f8a9b0c1d2`, down revision `d6e7f8a9b0c1`, with:

```python
def upgrade() -> None:
    op.execute(
        """
        INSERT INTO pi_packages (source, identity, enabled, pinned, is_default)
        VALUES ('npm:pi-sandbox', 'npm:pi-sandbox', true, false, true)
        ON CONFLICT (identity) DO UPDATE
        SET source = EXCLUDED.source,
            enabled = true,
            pinned = false,
            is_default = true
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM pi_packages WHERE identity = 'npm:pi-sandbox'")
```

The unpinned row is picked up by the existing startup installer and weekly refresh without new maintenance code.

- [ ] **Step 4: Run migration and package tests**

Run:

```bash
cd backend && .venv/bin/python -m pytest tests/test_planning_profile_migration.py tests/test_pi_packages.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/e7f8a9b0c1d2_enable_pi_sandbox.py backend/tests/test_planning_profile_migration.py
git commit -m "feat: enable pi-sandbox globally"
```

### Task 4: Verify installation and confinement end to end

**Files:**
- Modify only if a failing check reveals a defect in Tasks 1-3.

**Interfaces:**
- Consumes: migrated database, worker startup package installation, generated runtime policy.
- Produces: evidence that the installed extension confines Pi to the current project while internet access remains available.

- [ ] **Step 1: Run all backend tests**

```bash
cd backend && .venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Check migration consistency**

```bash
cd backend && .venv/bin/alembic check
```

Expected: `No new upgrade operations detected.`

- [ ] **Step 3: Install the migration and managed package**

Run the normal production installation only after tests pass:

```bash
sudo make install-mac
```

Expected: Alembic reaches `e7f8a9b0c1d2`; worker startup installs the current unpinned `pi-sandbox`; API and worker remain running.

- [ ] **Step 4: Run one real sandbox probe project**

Create a Carlo task whose working directory contains `inside.txt` and has a sibling directory containing `outside.txt`. Ask Pi to read both, create `created.txt`, run `sudo -n true`, and fetch `https://example.com`.

Expected:

- `inside.txt` is readable and `created.txt` is created inside the project;
- sibling `outside.txt` is denied;
- `sudo` is denied;
- the HTTPS request succeeds;
- no permission wait exceeds the configured one-second timeout.

- [ ] **Step 5: Record final repository state**

```bash
git status --short
git log -5 --oneline
```

Expected: only the user's pre-existing untracked screenshot remains; sandbox changes are committed.
