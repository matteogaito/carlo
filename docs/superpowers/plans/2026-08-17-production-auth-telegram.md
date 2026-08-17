# Production Authentication and Telegram Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run CARLO over a trusted VPN with an in-app administrator login, durable session security, production-served frontend, and restart-safe Telegram event notifications.

**Architecture:** PostgreSQL stores users, project memberships, opaque sessions, login failures, and notification delivery state. FastAPI exposes public authentication endpoints and protects the existing API/WebSocket boundary; the built React SPA is served by the same process. A worker-side notifier consumes persisted CARLO events and calls Telegram's HTTPS `sendMessage` endpoint without blocking orchestration.

**Tech Stack:** Python 3.12 standard library (`hashlib.scrypt`, `secrets`, `urllib`), FastAPI, SQLAlchemy 2 asyncio, PostgreSQL, Alembic, React, TypeScript, Vitest.

## Global Constraints

- Deployment is HTTP only inside a trusted VPN; cookie `Secure` remains configurable for later HTTPS.
- The UI uses an application-owned username/password form, not HTTP Basic or proxy authentication.
- The first user is one global administrator; schema groundwork supports future project memberships without implementing speculative user-management UI.
- Telegram is outbound-only and has one destination.
- Real secrets are never committed; committed templates and the local production environment contain explicit placeholders.
- Authentication failures must not reveal whether a username exists.
- Telegram failures must never block or mutate task orchestration state.
- Use no new runtime dependency: standard-library password hashing and HTTP are sufficient.

---

### Task 1: Persist authentication and notification state

**Files:**
- Modify: `backend/carlo/config.py`
- Modify: `backend/carlo/models.py`
- Create: `backend/alembic/versions/<revision>_production_auth_notifications.py`
- Modify: `backend/tests/test_config.py`
- Create: `backend/tests/test_security_models.py`

**Interfaces:**
- Produces: `Settings.app_origin`, `cookie_secure`, `session_hours`, `frontend_dist`, `bind_host`, `port`, `bootstrap_admin_username`, `bootstrap_admin_password`, `telegram_bot_token`, `telegram_chat_id`, and `telegram_level`.
- Produces: ORM records `User`, `ProjectMembership`, `UserSession`, `LoginFailure`, `NotificationCursor`, and `NotificationDelivery`.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_production_settings_are_loaded(monkeypatch):
    monkeypatch.setenv("CARLO_APP_ORIGIN", "http://100.64.0.10:8000")
    monkeypatch.setenv("CARLO_COOKIE_SECURE", "false")
    monkeypatch.setenv("CARLO_SESSION_HOURS", "12")
    monkeypatch.setenv("CARLO_TELEGRAM_LEVEL", "blocking")
    settings = Settings.from_env()
    assert settings.app_origin == "http://100.64.0.10:8000"
    assert settings.cookie_secure is False
    assert settings.session_hours == 12
    assert settings.telegram_level == "blocking"

def test_invalid_boolean_is_rejected(monkeypatch):
    monkeypatch.setenv("CARLO_COOKIE_SECURE", "sometimes")
    with pytest.raises(ValueError, match="CARLO_COOKIE_SECURE"):
        Settings.from_env()
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `cd backend && uv run pytest tests/test_config.py -q`

Expected: failure because the production settings do not exist.

- [ ] **Step 3: Extend `Settings` with validated environment parsing**

```python
def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"

@dataclass(frozen=True, slots=True)
class Settings:
    # existing fields stay unchanged
    app_origin: str = "http://127.0.0.1:8000"
    cookie_secure: bool = False
    session_hours: int = 24
    frontend_dist: str = "../frontend/dist"
    bind_host: str = "127.0.0.1"
    port: int = 8000
    bootstrap_admin_username: str = "CHANGE_ME"
    bootstrap_admin_password: str = "CHANGE_ME"
    telegram_bot_token: str = "CHANGE_ME"
    telegram_chat_id: str = "CHANGE_ME"
    telegram_level: str = "all"
```

Reject non-positive session hours and ports outside `1..65535`; accept only
`all` and `blocking` Telegram levels.

- [ ] **Step 4: Write the schema test**

```python
async def test_security_schema_supports_admin_membership_sessions_and_delivery(factory):
    async with factory() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        project = Project(name="CARLO", key="CAR", repository_path="/repo")
        session.add_all([user, project])
        await session.flush()
        session.add(ProjectMembership(user_id=user.id, project_id=project.id, role="contributor"))
        session.add(UserSession(user_id=user.id, token_digest="a" * 64, expires_at=future))
        await session.commit()
        assert await session.scalar(select(func.count(ProjectMembership.id))) == 1
```

- [ ] **Step 5: Add the six ORM records and constraints**

Use normalized lowercase usernames with a unique constraint. Required fields:

```python
class User(TimestampMixin, Base):
    id: Mapped[int]
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(30), default="member")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class ProjectMembership(TimestampMixin, Base):
    __table_args__ = (UniqueConstraint("user_id", "project_id"),)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(30), default="contributor")

class UserSession(TimestampMixin, Base):
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class LoginFailure(Base):
    username: Mapped[str] = mapped_column(String(80), index=True)
    source_ip: Mapped[str] = mapped_column(String(80), index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class NotificationCursor(TimestampMixin, Base):
    destination: Mapped[str] = mapped_column(String(160), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger)

class NotificationDelivery(TimestampMixin, Base):
    __table_args__ = (UniqueConstraint("event_sequence", "destination"),)
    event_sequence: Mapped[int] = mapped_column(ForeignKey("events.sequence", ondelete="CASCADE"))
    destination: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 6: Generate and inspect the Alembic revision**

Run: `cd backend && uv run alembic revision --autogenerate -m "production auth notifications"`

Inspect the upgrade and downgrade ordering. The migration must create users
before memberships/sessions and events already exist before deliveries.

- [ ] **Step 7: Verify schema and migration drift**

Run: `cd backend && uv run pytest tests/test_config.py tests/test_security_models.py -q`

Run: `cd backend && uv run alembic upgrade head && uv run alembic check`

Expected: tests pass and Alembic reports no new upgrade operations.

- [ ] **Step 8: Commit**

```bash
git add backend/carlo/config.py backend/carlo/models.py backend/alembic/versions backend/tests/test_config.py backend/tests/test_security_models.py
git commit -m "feat: persist production security state"
```

---

### Task 2: Implement password, session, throttling, and admin bootstrap

**Files:**
- Create: `backend/carlo/auth.py`
- Create: `backend/carlo/admin.py`
- Create: `backend/tests/test_auth.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: `User`, `UserSession`, `LoginFailure`, and authentication settings from Task 1.
- Produces: `hash_password(password: str) -> str`, `verify_password(password: str, encoded: str) -> bool`, `authenticate(...) -> User | None`, `login(...) -> tuple[User, str]`, `resolve_session(...) -> User | None`, `revoke_session(...) -> None`, and `bootstrap_admin(...) -> User`.

- [ ] **Step 1: Write failing primitive tests**

```python
def test_password_hash_is_salted_and_verifiable():
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first != second
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("wrong", first)

async def test_login_returns_opaque_token_and_persists_only_digest(factory):
    user = await create_user(factory, "admin", "secret", role="admin")
    authenticated, token = await login(factory, "ADMIN", "secret", "100.64.0.2", 24)
    assert authenticated.id == user.id
    assert len(token) >= 40
    assert await stored_token(factory, token) is None
    assert await resolve_session(factory, token) == user
```

- [ ] **Step 2: Confirm tests fail**

Run: `cd backend && uv run pytest tests/test_auth.py -q`

Expected: import failure for `carlo.auth`.

- [ ] **Step 3: Implement standard-library password hashing**

```python
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1

def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"

def verify_password(password: str, encoded: str) -> bool:
    try:
        _, n, r, p, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return secrets.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False
```

- [ ] **Step 4: Implement durable opaque sessions and login throttling**

Normalize usernames with `strip().lower()`. Before verifying credentials, count
failures for the normalized username or source IP within 15 minutes. Five
failures return `LoginThrottled`; every failed login stores the generic failure.
Delete failures older than 24 hours during login. Generate tokens with
`secrets.token_urlsafe(32)` and store only `sha256(token).hexdigest()`.

```python
async def resolve_session(factory, token: str) -> User | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    async with factory() as session:
        record = await session.scalar(
            select(UserSession).where(
                UserSession.token_digest == digest,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > datetime.now(UTC),
            )
        )
        return None if record is None or not record.user.active else record.user
```

- [ ] **Step 5: Write bootstrap tests**

```python
async def test_bootstrap_admin_is_idempotent_and_does_not_replace_password(factory):
    first = await bootstrap_admin(factory, "admin", "first-password")
    second = await bootstrap_admin(factory, "admin", "different-password")
    assert second.id == first.id
    assert await authenticate(factory, "admin", "first-password") is not None
    assert await authenticate(factory, "admin", "different-password") is None
```

- [ ] **Step 6: Implement `python -m carlo.admin`**

`bootstrap_admin` rejects placeholder credentials, creates the first active
`admin`, and returns an existing admin unchanged. The module entrypoint loads
`Settings`, opens the engine, prints only the username, and disposes the engine.

- [ ] **Step 7: Add the bootstrap command and run tests**

```make
bootstrap-admin:
	cd backend && uv run python -m carlo.admin
```

Run: `cd backend && uv run pytest tests/test_auth.py -q`

Expected: all authentication and bootstrap tests pass.

- [ ] **Step 8: Commit**

```bash
git add backend/carlo/auth.py backend/carlo/admin.py backend/tests/test_auth.py Makefile
git commit -m "feat: add durable administrator sessions"
```

---

### Task 3: Protect HTTP/WebSocket APIs and serve the production SPA

**Files:**
- Modify: `backend/carlo/api.py`
- Modify: `backend/carlo/main.py`
- Modify: `backend/tests/test_api.py`
- Modify: `backend/tests/test_events.py`
- Create: `backend/tests/test_production_api.py`

**Interfaces:**
- Consumes: authentication functions from Task 2 and `Settings` from Task 1.
- Produces: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`; all prior `/api/*` endpoints and `/api/ws` require an active admin session.
- Produces: public SPA files from `Settings.frontend_dist`.

- [ ] **Step 1: Write failing API security tests**

```python
async def test_login_protects_api_and_logout_revokes_cookie(client, admin):
    assert (await client.get("/api/projects")).status_code == 401
    response = await client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
    assert response.status_code == 200
    assert response.json() == {"username": "admin", "role": "admin"}
    assert "httponly" in response.headers["set-cookie"].lower()
    assert (await client.get("/api/projects")).status_code == 200
    assert (await client.post("/api/auth/logout")).status_code == 204
    assert (await client.get("/api/projects")).status_code == 401

async def test_mutation_rejects_wrong_origin(authenticated_client):
    response = await authenticated_client.post(
        "/api/tasks", json={}, headers={"Origin": "http://attacker.invalid"}
    )
    assert response.status_code == 403
```

- [ ] **Step 2: Confirm protected API tests fail**

Run: `cd backend && uv run pytest tests/test_production_api.py -q`

Expected: `/api/projects` is currently public and auth routes return 404.

- [ ] **Step 3: Add auth endpoints and one reusable admin dependency**

```python
SESSION_COOKIE = "carlo_session"

async def require_admin(request: Request) -> User:
    user = await resolve_session(session_factory, request.cookies.get(SESSION_COOKIE, ""))
    if user is None:
        raise HTTPException(401, "authentication required")
    if user.role != "admin":
        raise HTTPException(403, "administrator access required")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") != settings.app_origin:
        raise HTTPException(403, "invalid request origin")
    return user
```

Define the existing routes on an `APIRouter(dependencies=[Depends(require_admin)])`
and include it after the public auth router. Login always returns the same 401
message for unknown users and wrong passwords. Set the cookie with `path=/`,
`httponly=True`, `samesite="lax"`, `secure=settings.cookie_secure`, and session
max age. Logout revokes the presented token before deleting the cookie.

- [ ] **Step 4: Authenticate WebSocket before acceptance**

Resolve `websocket.cookies[SESSION_COOKIE]`; close unauthenticated sockets with
code `4401` without streaming events. Update `test_events.py` to bootstrap and
log in before `websocket_connect`, and add a rejection assertion.

- [ ] **Step 5: Update existing API tests to authenticate**

Add a small test helper that inserts an admin, logs in through the public route,
and sends the configured Origin on mutations. Do not add an authentication-off
production switch. Existing planning and concurrency assertions remain intact.

- [ ] **Step 6: Serve built frontend files after API routes**

```python
dist = Path(settings.frontend_dist).resolve()
assets = dist / "assets"
if assets.is_dir():
    app.mount("/assets", StaticFiles(directory=assets), name="assets")

@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str):
    if path.startswith("api/") or not (dist / "index.html").is_file():
        raise HTTPException(404)
    candidate = (dist / path).resolve()
    if candidate.is_file() and candidate.is_relative_to(dist):
        return FileResponse(candidate)
    return FileResponse(dist / "index.html")
```

- [ ] **Step 7: Test SPA fallback and API 404 isolation**

Create a temporary `dist/index.html`, instantiate the app with that path, and
assert `/`, `/tasks/CAR-1`, and a static file are served while `/api/missing`
returns JSON 404 rather than the SPA.

- [ ] **Step 8: Run backend API tests**

Run: `cd backend && uv run pytest tests/test_api.py tests/test_events.py tests/test_production_api.py -q`

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add backend/carlo/api.py backend/carlo/main.py backend/tests/test_api.py backend/tests/test_events.py backend/tests/test_production_api.py
git commit -m "feat: protect CARLO with application login"
```

---

### Task 4: Add the in-application login experience

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `/api/auth/me`, `/api/auth/login`, and `/api/auth/logout` from Task 3.
- Produces: `User`, `AuthenticationRequired`, and `Api.me/login/logout`; unauthenticated users see only `LoginForm`.

- [ ] **Step 1: Write failing frontend tests**

```tsx
it('shows login before loading the board', async () => {
  render(<App api={unauthenticatedApi} />)
  expect(await screen.findByRole('heading', { name: 'Sign in to CARLO' })).toBeTruthy()
  await userEvent.type(screen.getByLabelText('Username'), 'admin')
  await userEvent.type(screen.getByLabelText('Password'), 'secret-password')
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  expect(await screen.findByRole('heading', { name: 'Not Ready' })).toBeTruthy()
})

it('logs out to the login form', async () => {
  render(<App api={authenticatedApi} />)
  await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
  expect(await screen.findByRole('heading', { name: 'Sign in to CARLO' })).toBeTruthy()
})
```

- [ ] **Step 2: Confirm frontend tests fail**

Run: `cd frontend && npm test`

Expected: `Api` has no auth methods and the login UI is absent.

- [ ] **Step 3: Extend the typed API client**

```typescript
export interface User { username: string; role: 'admin' | 'member' }
export class AuthenticationRequired extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...init, headers: { ... } })
  if (response.status === 401) throw new AuthenticationRequired()
  // preserve existing error handling
}
```

Add `me()`, `login(username, password)`, and `logout()` to `Api`. `logout`
accepts a 204 response without trying to parse JSON.

- [ ] **Step 4: Gate the board behind `LoginForm`**

Use three states: unresolved user (`undefined`), unauthenticated (`null`), and
authenticated `User`. Call `api.me()` once. Only start task loading and the
WebSocket after authentication. On any later `AuthenticationRequired`, return
to login. Add a visible username and `Sign out` button in the masthead.

- [ ] **Step 5: Style the login form with the existing design language**

Reuse current colors, typography, inputs, focus states, and error banner. Keep
the form keyboard accessible with explicit labels, autocomplete values
`username` and `current-password`, and a disabled submit state while signing in.

- [ ] **Step 6: Run frontend checks**

Run: `cd frontend && npm test && npm run build`

Expected: login/board tests pass and TypeScript/Vite build succeeds.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api.ts frontend/src/App.tsx frontend/src/App.test.tsx frontend/src/styles.css
git commit -m "feat: add CARLO login form"
```

---

### Task 5: Deliver persisted events to Telegram

**Files:**
- Create: `backend/carlo/telegram.py`
- Modify: `backend/carlo/worker.py`
- Create: `backend/tests/test_telegram.py`

**Interfaces:**
- Consumes: `Event`, `NotificationCursor`, `NotificationDelivery`, task/project relationships, and Telegram settings.
- Produces: `TelegramTransport.send(chat_id: str, text: str) -> None`, `TelegramNotifier.deliver_next() -> bool`, and `notification_loop(...) -> None`.

- [ ] **Step 1: Write formatting and filtering tests**

```python
def test_format_includes_task_and_severity():
    text, severity = format_event(event("planning.completed", "CAR-7", {"revision": 2}))
    assert severity == "info"
    assert "CAR-7" in text
    assert "Plan completed" in text

def test_failed_and_blocked_events_are_blocking():
    for name in ("planning.failed", "execution.stalled", "task.failed"):
        assert format_event(event(name))[1] == "blocking"
```

Unknown events are informational and humanized from their event type so the
`all` level really means all persisted CARLO events.

- [ ] **Step 2: Write delivery/recovery tests using a fake transport**

```python
async def test_delivery_is_not_duplicated_after_restart(factory):
    notifier = TelegramNotifier(factory, FakeTransport(), "token", "123", "all")
    await notifier.initialize_cursor()
    event = await add_event(factory, "planning.completed", task_id="CAR-1")
    assert await notifier.deliver_next() is True
    restarted = TelegramNotifier(factory, transport, "token", "123", "all")
    assert await restarted.deliver_next() is False
    assert transport.messages == [expected_message]

async def test_failure_retries_then_abandons_without_blocking_later_events(factory):
    transport = FakeTransport(failures=5)
    notifier = TelegramNotifier(factory, transport, "token", "123", "all", max_attempts=5)
    # advance test clock through bounded backoff
    assert await abandoned_delivery(factory, first_event)
    assert await notifier.deliver_next() is True  # later event can proceed
```

- [ ] **Step 3: Confirm notifier tests fail**

Run: `cd backend && uv run pytest tests/test_telegram.py -q`

Expected: import failure for `carlo.telegram`.

- [ ] **Step 4: Implement event classification and concise formatting**

Use an explicit label map for important events (`planning.completed`,
`plan.approved`, `execution.started`, `validation.passed`, `task.completed`,
`planning.failed`, `execution.stalled`, `escalation.started`, `task.failed`).
Derive severity from a set of blocking event types plus suffixes `.failed` and
`.blocked`. Truncate formatted messages below Telegram's 4096-character limit.

- [ ] **Step 5: Implement the HTTPS transport with the standard library**

```python
class TelegramTransport:
    async def send(self, token: str, chat_id: str, text: str) -> None:
        body = json.dumps({"chat_id": chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        await asyncio.to_thread(_send_request, request)
```

`_send_request` uses a finite timeout, verifies HTTP success and Telegram's JSON
`ok` field, and raises `TelegramError` with a redacted message that never embeds
the bot token. This follows Telegram's official HTTPS Bot API `sendMessage`
contract.

- [ ] **Step 6: Implement cursor, deduplication, and bounded retry**

On first enablement, initialize the destination cursor to the current maximum
event sequence so historical events are not flooded. For each later event:

1. create or load its unique delivery record;
2. mark filtered informational events `skipped` and advance the cursor;
3. send eligible events;
4. on success mark `sent` and advance the cursor in the same transaction;
5. on failure increment attempts and set exponential retry time;
6. after five failures mark `abandoned`, advance the cursor, and continue.

Use `SELECT ... FOR UPDATE` on the destination cursor so two worker processes
cannot deliver the same event concurrently.

- [ ] **Step 7: Run Telegram tests**

Run: `cd backend && uv run pytest tests/test_telegram.py -q`

Expected: formatting, filtering, retry, deduplication, and restart tests pass.

- [ ] **Step 8: Run notifier concurrently with the executor**

In `worker.run`, create a notification task only when token and chat ID are not
placeholders. The loop calls `deliver_next`, sleeps briefly when idle, logs
errors, and continues. Cancel and await it during worker shutdown. The existing
orchestrator loop remains unchanged.

- [ ] **Step 9: Commit**

```bash
git add backend/carlo/telegram.py backend/carlo/worker.py backend/tests/test_telegram.py
git commit -m "feat: notify administrator through Telegram"
```

---

### Task 6: Provide production configuration and end-to-end verification

**Files:**
- Create: `.env.production.example`
- Create locally but ignore: `.env.production`
- Modify: `.gitignore`
- Modify: `Makefile`
- Modify: `README.md`
- Create: `backend/tests/test_production_flow.py`

**Interfaces:**
- Consumes: all prior production settings, bootstrap command, API, worker, and frontend build.
- Produces: `make build`, `make prod-api`, `make bootstrap-admin`, documented two-process startup, and a placeholder production environment ready for local editing.

- [ ] **Step 1: Add the committed production template**

```dotenv
CARLO_DATABASE_URL=postgresql+psycopg:///carlov3
CARLO_ARTIFACT_ROOT=/ABSOLUTE/PATH/TO/carlov3-data/artifacts
CARLO_WORKTREE_ROOT=/ABSOLUTE/PATH/TO/carlov3-data/worktrees
CARLO_PI_EXECUTABLE=pi
CARLO_MAX_ATTEMPTS=20
CARLO_BIND_HOST=0.0.0.0
CARLO_PORT=8000
CARLO_APP_ORIGIN=http://VPN_IP_OR_HOSTNAME:8000
CARLO_FRONTEND_DIST=/ABSOLUTE/PATH/TO/carlov3/frontend/dist
CARLO_COOKIE_SECURE=false
CARLO_SESSION_HOURS=24
CARLO_BOOTSTRAP_ADMIN_USERNAME=CHANGE_ME
CARLO_BOOTSTRAP_ADMIN_PASSWORD=CHANGE_ME_MINIMUM_12_CHARACTERS
CARLO_TELEGRAM_BOT_TOKEN=CHANGE_ME
CARLO_TELEGRAM_CHAT_ID=CHANGE_ME
CARLO_TELEGRAM_LEVEL=all
```

Copy the same placeholder content to local `.env.production`. Ignore
`.env.production` explicitly while keeping `.env.production.example` tracked.

- [ ] **Step 2: Add production Make targets**

```make
build:
	cd frontend && npm run build

prod-api: build
	cd backend && uv run uvicorn carlo.main:app --host "$${CARLO_BIND_HOST:-127.0.0.1}" --port "$${CARLO_PORT:-8000}"
```

Keep worker as a separate process so API restarts do not interrupt task
execution. Neither production target uses `--reload`.

- [ ] **Step 3: Write the production-flow test**

The test creates schema, bootstraps an admin, builds a temporary SPA directory,
starts the ASGI app, verifies unauthenticated rejection, logs in, loads `/` and a
SPA fallback route, calls `/api/projects`, opens the authenticated WebSocket,
logs out, and verifies the session is revoked.

- [ ] **Step 4: Run the focused production flow**

Run: `cd backend && uv run pytest tests/test_production_flow.py -q`

Expected: one complete authenticated flow passes against PostgreSQL.

- [ ] **Step 5: Document exact installation and startup**

README instructions must say:

1. copy `.env.production.example` to `.env.production` and replace every
   `CHANGE_ME`, VPN origin, and absolute path;
2. load it with `set -a && source .env.production && set +a`;
3. run `make install`, `make migrate`, `make bootstrap-admin`, and `make build`;
4. start `make prod-api` and `make worker` in separate supervised processes;
5. message the bot once and obtain the single destination chat ID before
   enabling Telegram;
6. clear `CARLO_BOOTSTRAP_ADMIN_PASSWORD` from the populated file after the
   admin exists;
7. set `CARLO_COOKIE_SECURE=true` and change `CARLO_APP_ORIGIN` when HTTPS is
   introduced.

- [ ] **Step 6: Run full verification**

Run: `make test`

Expected: backend tests pass, Alembic reports no drift, frontend tests pass, and
the production Vite build succeeds.

Run: `git status --short`

Expected: only intended tracked changes; `.env.production` is absent because it
is ignored.

- [ ] **Step 7: Commit**

```bash
git add .env.production.example .gitignore Makefile README.md backend/tests/test_production_flow.py
git commit -m "docs: make VPN production setup reproducible"
```

---

## Final acceptance

- [ ] Run `make test` once more from a clean working tree.
- [ ] Run `cd backend && uv run alembic current` and confirm the new revision is current.
- [ ] Confirm `git check-ignore .env.production` succeeds.
- [ ] Confirm no committed file contains a real Telegram token or administrator password.
- [ ] Record Telegram live delivery as `UNVERIFIABLE` until the user replaces placeholders; do not claim a live message was sent.
