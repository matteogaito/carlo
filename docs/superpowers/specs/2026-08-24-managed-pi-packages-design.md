# Managed Pi packages

## Purpose

CARLO must treat Pi packages as first-class managed resources instead of
hard-coding Superpowers, Ponytail, or any future package in Python. An
administrator registers an explicit Pi package source and decides whether it
is a global default. From that point CARLO installs, updates, validates,
snapshots, loads, and audits the package without another code change.

Pi packages are containers. Their `package.json` manifest may expose
extensions, skills, prompt templates, and themes together. CARLO must not
model a package and the skills inside it as mutually exclusive resources.

## Chosen approach

CARLO will use Pi's native package commands in a CARLO-owned staging agent
directory selected with `PI_CODING_AGENT_DIR`. It will not modify
`/Users/carlo/.pi/agent/settings.json` and will not implement a second npm/git
package resolver.

Alternatives rejected:

- passing remote `npm:` or `git:` sources to every session would make startup
  depend on the network and would not provide immutable task snapshots;
- extending the current hand-written Git updater would duplicate Pi's npm,
  Git, dependency, pinning, and package-manifest behavior;
- managing packages in the service user's ordinary Pi configuration would
  reintroduce mutable external state that CARLO cannot reconstruct from its
  database and artifacts.

## Package records

PostgreSQL stores one record per Pi package identity with at least:

- administrator-provided source (`npm:` or `git:`/supported URL);
- package identity derived from the source;
- whether it is enabled;
- whether it is a global default;
- pinning state;
- active resolved version or Git revision;
- active artifact path;
- resources discovered from the Pi manifest;
- last update attempt, result, and concise error;
- creation/update audit timestamps.

The source is unique by Pi package identity, not merely by the complete string,
so the same package cannot be registered twice under different refs. The first
vertical slice accepts npm and Git sources supported by Pi. Local filesystem
package sources are excluded because they do not provide a portable update or
recovery contract.

`PiPackage.is_default` is the single source of truth for global defaults;
profile additions refer to package records through an association table.
`PiRuntimeSettings.default_packages` remains only during one compatibility
release and is no longer authoritative after migration. Removing a package
from the global defaults affects only sessions created afterward. A package
needed by an already-persisted task snapshot remains retained on disk.

## Installation and updates

Adding a package performs an immediate staged installation:

1. validate source syntax and reject duplicates;
2. create a temporary CARLO-owned Pi agent directory;
3. run `pi install <source>` with `PI_CODING_AGENT_DIR` pointing to it;
4. inspect the resulting Pi settings, installed package, manifest, resolved
   version/revision, and declared resources;
5. reject packages without a valid Pi package structure;
6. atomically promote the staged artifact and database metadata;
7. leave the previous active artifact untouched until promotion succeeds.

Unpinned sources are refreshed weekly. Explicit npm versions and Git
tags/commits follow Pi's pinned behavior and are not moved automatically.
Maintenance is generic over active database records; adding `pippo` requires no
new descriptor or code constant.

Updates use the same staging and promotion process. If an update fails, CARLO
keeps serving the previous valid version and records the failure. A first
installation with no previous version remains unavailable and cannot be made a
default. The existing Pi maintenance lock prevents package mutation while
agent sessions are starting.

One concise Telegram notification summarizes the weekly package update cycle:
updated, unchanged, and failed packages. It must not emit one message per
package or retry every worker cycle. Existing failure backoff remains in
effect.

## Runtime snapshots

At session creation CARLO resolves:

1. enabled global default packages;
2. enabled profile-specific package additions;
3. standalone bundled or managed skills required by the profile or task.

Each selected package is loaded from its promoted immutable local artifact via
Pi's native package loader. Pi reads its manifest and exposes all declared
extensions, skills, prompts, and themes. Standalone CARLO skills continue to
use Pi's explicit skill-path mechanism.

The session snapshot records package database ID, original source, resolved
version/revision, artifact identity, and the resources declared by the
package. A running session never changes when maintenance promotes a newer
version.

The task UI shows loaded packages and their exact versions. Package-provided
skills are shown as available through that package, rather than pretending
they were separately installed or necessarily invoked. CARLO records a skill
as used only when Pi runtime evidence demonstrates its use.

## Historical plans and the DIMMELA-1 failure

Old Plan metadata may contain package names such as `ponytail` in its `skills`
array. During resolution CARLO normalizes a plan-provided resource name:

- if it matches a selected package identity or a skill supplied by that
  package, the package already satisfies it;
- if it matches a registered standalone skill, load that skill;
- otherwise fail with a precise unknown-resource error.

This compatibility applies only to Plan metadata. Invalid global or profile
configuration is not silently ignored. DIMMELA-1 can therefore be reworked or
resumed without regenerating its approved plan merely because Ponytail became
a managed package.

New planning output will expose `packages` and `skills` separately. CARLO will
continue accepting the older `skills`-only shape while historical plans exist.

## Settings UI

**Settings → Coding agents → Global Pi packages** provides:

- an Add package action accepting an explicit source;
- install/validation progress and errors;
- enabled and Default controls;
- source, pinning state, installed version/revision, update status, and
  discovered resource summary;
- Update now and Remove actions.

Removal first disables the package. Physical artifacts are retained while any
task/session snapshot references them. Destructive garbage collection is not
part of the first implementation.

Profile settings select additional registered packages. Global defaults remain
inherited and cannot be excluded by a profile.

## Recovery and security

PostgreSQL is authoritative for desired package configuration; promoted
artifacts and their manifests are the recoverable runtime state. At startup
CARLO verifies that every selected package's artifact and resolved identity are
present before admitting Pi sessions. Missing active artifacts trigger a
staged reinstall from the persisted source. If recovery fails and no old valid
artifact exists, affected new sessions are blocked with a concise actionable
error.

Pi package extensions execute code with the service user's permissions.
Adding, enabling, or changing a source is therefore admin-only, origin/CSRF
protected, validated, and audited. CARLO never executes an unpromoted package
inside a task repository. Logs and Telegram messages exclude credentials and
limit subprocess output.

## Migration and compatibility

The migration seeds Superpowers and Ponytail as enabled global defaults using
their current explicit Git sources. Their current known-good artifacts are
adopted when they pass the new validation; otherwise CARLO installs them into
the new layout before admitting sessions.

Existing profile model selections, standalone skills, task plans, task
snapshots, and provider credentials remain valid. The old hard-coded managed
package constants and resource descriptors are removed only after the seeded
records and generic maintenance path are active.

## Validation

Tests must cover:

- source validation, identity deduplication, and admin-only mutation;
- npm and Git staged installation through a fake Pi executable;
- atomic first install and update promotion;
- failed update retaining the previous version;
- pinned packages remaining unchanged;
- weekly aggregation and failure backoff;
- global defaults plus profile additions resolving generically;
- package manifest resources being exposed without separate skill install;
- immutable session revision evidence;
- restart recovery from database plus promoted artifacts;
- historical `skills: [ponytail]` normalization and DIMMELA-1 regression;
- settings UI add/default/update/error flows;
- migrations and the full existing CARLO suite.

Production smoke validation uses one real unpinned npm package and one Git
package, followed by a Pi session that reports the loaded package resources.
