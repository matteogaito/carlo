# Production authentication and Telegram notifications

## Scope

Make the current CARLO vertical slice usable over a trusted VPN without a
development server. Add an in-application login, one bootstrap administrator,
the minimum authorization model needed for future project-scoped users, and
outbound Telegram notifications. HTTPS and user-management screens remain out
of scope.

## Production runtime

- Build the React application once and serve its static files from FastAPI,
  including SPA route fallback.
- Run API and worker as separate long-lived processes without auto-reload.
- Bind to a configurable host and port; the intended deployment is HTTP inside
  a trusted VPN.
- Add a production environment template containing only real CARLO settings.
  Secret values use explicit placeholders and the file is safe to commit as a
  template. A populated local environment file remains ignored by Git.

## Authentication

- The React application presents a username/password login form.
- FastAPI verifies a password hash stored in PostgreSQL and creates an opaque,
  expiring server-side session.
- The browser receives only an `HttpOnly`, `SameSite=Lax` session cookie.
  `Secure` is configurable and defaults off for the initial VPN-only HTTP
  deployment.
- Logout revokes the server-side session. Disabled users and expired or revoked
  sessions are rejected by HTTP APIs and WebSockets.
- Login failures return one generic response and are throttled per username and
  source address using persisted recent failures.
- Mutating cookie-authenticated requests validate their `Origin` against the
  configured application origin.
- Passwords use a salted memory-hard standard-library hash. Session tokens are
  random and only their digest is stored.

## Bootstrap and authorization

- A startup command creates the initial administrator from environment
  placeholders if no administrator exists. It is idempotent and never changes
  an existing password implicitly.
- `users` stores identity, password hash, active state, and global role.
- The initial global role is `admin`, which can perform every CARLO action.
- `project_memberships` associates a user with a project and a project role.
  It is schema and domain groundwork only; no multi-user management UI or
  project-member behavior is implemented in this slice.
- Authorization is enforced at the API boundary through one reusable current
  user/admin dependency, leaving task-domain logic independent of channels.

## Telegram notifications

- Telegram is outbound-only and targets one configured chat ID.
- A subscriber translates persisted CARLO events into concise messages that
  include project/task identity, event type, severity, and useful context.
- Two severities are supported: `info` and `blocking`. The production template
  defaults to both and allows selecting `blocking` only.
- Relevant informational events include planning completion, plan approval,
  execution start, validation success, and task completion. Blocking events
  include planning/provider errors, stalled execution, escalation, failed
  validation requiring intervention, and failed tasks.
- Delivery uses Telegram's HTTPS Bot API. Failures are recorded and retried with
  a small bounded backoff, but never block orchestration or change task state.
- Each persisted event is delivered at most once per Telegram destination by a
  database delivery record, so restarts can safely resume pending notifications.
- Missing placeholder configuration disables Telegram cleanly and logs why.

## Configuration

The production template exposes:

- PostgreSQL URL and absolute artifact/worktree paths;
- Pi executable and attempt ceiling;
- bind host, port, application origin, and cookie security mode;
- bootstrap admin username/password placeholders and session lifetime;
- Telegram bot-token/chat-ID placeholders and notification level.

Secrets are never committed with real values or returned by APIs. Startup fails
with a clear error when required production authentication values still contain
placeholders. Telegram placeholders only disable Telegram so CARLO can start.

## Failure handling and recovery

- Database state remains authoritative for users, sessions, events, and Telegram
  delivery attempts.
- Restarting API or worker does not invalidate valid sessions or duplicate
  successful notifications.
- Telegram outages do not affect task execution.
- The UI redirects unauthenticated users to login and returns to the Kanban after
  successful authentication.

## Verification

- Migration tests cover users, memberships, sessions, login failures, and
  Telegram deliveries.
- API tests cover login/logout, protected CRUD/actions, admin access, origin
  checks, disabled/expired sessions, and authenticated WebSockets.
- Telegram tests use a fake transport and cover formatting, severity filtering,
  retry, deduplication, disabled configuration, and restart recovery.
- Frontend tests cover login and authenticated Kanban loading.
- A production smoke test builds the frontend, bootstraps an admin, starts the
  non-reload API, logs in, loads the SPA, and verifies a protected endpoint.

## Deferred

- HTTPS termination and reverse-proxy configuration;
- inbound Telegram commands;
- multiple Telegram recipients;
- user and membership management UI;
- non-admin project-scoped action enforcement;
- password reset, MFA, SSO, and external identity providers.
