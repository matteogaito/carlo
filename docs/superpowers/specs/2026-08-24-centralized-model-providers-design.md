# Centralized Model Providers and Pi runtime settings

## Goal

Make CARLO the sole source of truth for the model configuration used by Pi
processes launched by CARLO. Administrators configure inference endpoints,
encrypted credentials, discovered models, context limits, and compaction from
the CARLO web application. They no longer edit Pi `models.json` or
`settings.json` files for worker operation.

The user's independently launched Pi remains separate. CARLO does not delete
or mutate `~/.pi/agent`; worker processes point at CARLO-generated runtime
snapshots and therefore cannot inherit stale personal settings.

## Product structure

Add one top-level **Settings** workspace with two sections:

```text
Settings
├── Models
│   ├── Model Providers
│   └── Models
└── Coding Agents
    └── Pi
```

`Model Provider` means an inference endpoint. It is distinct from CARLO's
existing `CodingAgentProvider`, which is the agent harness such as Pi.

The first Model Provider kind is `openai-compatible`. A provider stores a
display name, stable slug, base URL, encrypted API key, compatibility settings,
refresh state, default model, and active state. OMLX and OpenRouter are ordinary
instances of this same kind; neither needs provider-specific orchestration.

## Persistence

### ModelProvider

Store:

- name and stable slug;
- kind, initially `openai-compatible`;
- base URL and optional non-secret compatibility configuration;
- encrypted API credential, nonce, encryption-key version, and masked hint;
- synchronization interval and timestamps;
- last synchronization outcome and concise error;
- default discovered model;
- active state and audit timestamps.

Credentials are encrypted with authenticated encryption using
`CARLO_CREDENTIAL_ENCRYPTION_KEY`. Plaintext is accepted only at the API trust
boundary, encrypted before persistence, never returned by APIs, and never
written to logs or generated files. Updating a provider without supplying a new
credential retains the existing credential.

### Model

Store one durable row per `(model_provider_id, external_id)`:

- external ID and display name;
- availability (`AVAILABLE` or `UNAVAILABLE`);
- discovered context window and output limit when supplied by the endpoint;
- administrator overrides for context window and output limit;
- input modalities, reasoning support, compatibility metadata, and bounded raw
  discovery metadata;
- first-seen, last-seen, and audit timestamps.

The effective context and output limits use administrator overrides first and
discovered values second. A model without a verified effective context window
is visible but cannot be selected for a new CARLO run.

Agent Profiles may reference either a concrete Model or the default model of a
Model Provider. Task-level overrides reference a concrete Model. Existing
string-valued Agent Profile models are migrated when they match a discovered
provider/model pair and remain readable during the compatibility transition.

### Pi settings

Persist one CARLO-owned Pi runtime policy:

- automatic compaction enabled;
- reserve-context percentage, default `10`;
- keep-recent percentage, default `20`;
- optional operational settings already supported by CARLO's Pi provider.

For a selected model:

```text
reserveTokens    = max(effectiveMaxTokens, effectiveContextWindow * 10%)
keepRecentTokens = effectiveContextWindow * 20%
```

Values are rounded to whole tokens and validated before a run. CARLO rejects a
configuration where the resulting compaction threshold cannot safely retain
the requested recent context. The Settings UI shows the percentages and the
calculated token values for each selectable model.

## Discovery and synchronization

The existing worker maintenance loop refreshes active Model Providers every 15
minutes. The Settings UI also exposes **Refresh now**.

For `openai-compatible`, CARLO requests `GET <baseUrl>/models` using the stored
credential and imports every valid model object. The standard model ID is
required. Common optional context and output-limit fields are recognized;
unknown bounded metadata is retained for inspection without becoming domain
logic.

Synchronization is transactional:

- seen models are inserted or updated and marked available;
- previously known but absent models become unavailable, never deleted;
- a successful single-model catalog automatically makes that model the
  provider default;
- a multi-model catalog keeps the existing available default or requires an
  explicit administrator selection;
- a failed refresh preserves the last good catalog and records the concise
  failure;
- routine successes do not notify Telegram; repeated refresh failures may emit
  one deduplicated operational notification.

Agent Profiles configured for a provider default follow changes to that
default. Concrete Task model selections never change silently. A running or
persisted session retains its original runtime snapshot even if later catalog
refreshes change availability.

## Pi runtime snapshots

Before launching Pi, CARLO resolves the Agent Profile and optional Task model
override into one immutable runtime snapshot. The snapshot contains only the
selected provider/model plus the effective Pi settings:

```text
<artifact-root>/pi-runtime/<session-id>/
├── models.json
└── settings.json
```

CARLO passes supported process options directly (`--model`, `--thinking`,
`--tools`, `--skill`, and `--extension`). Pi has no CLI flags for custom model
definitions or compaction, so CARLO points `PI_CODING_AGENT_DIR` at the snapshot
directory.

`models.json` refers to a session-only environment variable for the API key.
CARLO decrypts the credential immediately before process creation and supplies
it only in the child environment. The secret does not appear in command-line
arguments, snapshot files, events, or persisted artifacts.

The snapshot manifest recorded with the CARLO session includes the selected
Model and Model Provider IDs, public configuration, effective context/output
limits, compaction values, and configuration revision. It excludes secrets.
This makes task and Discovery recovery deterministic and auditable.

At startup CARLO removes incomplete and unreferenced snapshot directories under
its own artifact root and reconstructs required snapshots from PostgreSQL.
CARLO never recursively cleans the user's Pi directory. Since worker processes
always receive `PI_CODING_AGENT_DIR`, static personal Pi settings are ignored by
construction.

## Runtime behavior and recovery

The provider-neutral Agent Profile passed to a coding-agent provider includes a
resolved model selection and runtime context policy. The Pi provider translates
that selection into Pi arguments, environment, and snapshot files. Domain and
API code do not depend on Pi's `models.json` representation.

New sessions are blocked with a concise actionable error when:

- the Model Provider is inactive;
- its credential cannot be decrypted;
- the selected concrete model is unavailable;
- a provider default is required but not selected;
- context/output limits are missing or invalid.

Existing live sessions continue with their initial snapshot. After restart,
CARLO recreates the same public snapshot from persisted session evidence and
the referenced model revision. Credential rotation uses the current credential
without changing the pinned model configuration.

## API and UI

Authenticated administrator APIs support:

- Model Provider CRUD with write-only credentials;
- connection testing and explicit refresh;
- model listing, availability filtering, and metadata overrides;
- provider-default selection;
- Pi compaction-policy editing and calculated previews;
- Agent Profile model selection;
- optional concrete Task model override before a run.

The Settings UI uses existing CARLO cards, modals, realtime events, and mobile
navigation patterns. Provider cards emphasize health, endpoint, model count,
default model, and last refresh. Model rows show availability, context,
max-output, source/override state, and affected profiles. Secrets use replace
semantics rather than a reveal action.

## Security and observability

- Require a valid encryption key in production before credential writes or
  Model Provider execution.
- Use authenticated encryption with a fresh nonce per credential update.
- Redact authorization headers, environment values, and provider response
  bodies from normal logs.
- Bound `/models` response size, model count, string lengths, and raw metadata.
- Apply request timeouts and reject non-HTTP(S) endpoints. Existing private/local
  endpoints remain supported because OMLX is a primary use case.
- Persist provider-created, credential-updated, refresh-started/completed/failed,
  model-availability-changed, Pi-settings-updated, and snapshot-created events.

## Compatibility and rollout

The migration does not change existing Task identities, plans, events, or Git
lifecycle. Seed an OMLX Model Provider from the current implementation profile
and available Pi model configuration when values are discoverable; otherwise
leave an explicit setup requirement in Settings. Do not import plaintext API
keys into PostgreSQL automatically.

Until a profile is migrated, its legacy model string continues through the
existing Pi path. Once a profile selects a CARLO Model or Model Provider, all
new sessions use managed snapshots. Remove the legacy path only after every
active profile has migrated and recovery tests cover the managed path.

## Validation

Cover at minimum:

- credential encryption, masking, replacement, wrong-key failure, and no
  plaintext leakage;
- successful, failed, bounded, and repeated `/models` synchronization;
- unavailable-model retention and provider-default rules;
- context/output overrides and proportional compaction calculations;
- immutable snapshot generation without secrets;
- Pi process arguments, environment, and `PI_CODING_AGENT_DIR` isolation;
- concurrent sessions using different model contexts;
- restart reconstruction and stale snapshot cleanup;
- legacy Agent Profile compatibility and migration;
- Settings APIs, responsive UI, and production build.
