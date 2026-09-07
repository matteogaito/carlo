# Pi Project Sandbox Design

## Goal

Enable the `pi-sandbox` Pi extension for every Carlo project. Pi may read and write project files only inside the repository or task worktree from which Carlo launches it. Network access remains unrestricted.

## Package Management

Carlo installs the unpinned package source `npm:pi-sandbox` through its existing `PiPackageManager` and marks it as a default package inherited by every agent profile. The existing weekly managed-package refresh keeps it updated. Carlo does not invoke `srt` directly; `pi-sandbox` manages its sandbox runtime internally.

## Configuration

Carlo already launches Pi with `cwd` set to the project repository or task worktree. In `pi-sandbox`, `.` therefore identifies the complete permitted project boundary.

Carlo sets a session-specific `PI_CODING_AGENT_DIR`, so `PiRuntimeSnapshotBuilder` writes the same `sandbox.json` into every session snapshot rather than relying on `~/.pi/agent/sandbox.json`.

The generated configuration:

- sets `enabled` and `sandboxUserShell` to `true`;
- sets a short permission timeout so unattended denied operations abort promptly;
- sets `filesystem.allowRead` and `filesystem.allowWrite` to `["."]`;
- denies reads from `/Users`, `/home`, and Carlo's worktree root, with `.` re-allowing only the current project;
- hard-denies writes to `.pi/sandbox.json` so Pi cannot change its own policy;
- sets `network.allowedDomains` to `["*"]` and `network.deniedDomains` to `[]`;
- leaves Apple Events and browser-process launching disabled;
- does not grant additional Unix-socket access.

Because `pi-sandbox` merges project-local `.pi/sandbox.json` settings and lets local scalar values override global values, Carlo rejects a project containing that file before starting Pi. This prevents repository content from disabling or widening the mandatory policy.

`sudo` remains disabled. Per-project elevation is deferred.

## Enforcement

`pi-sandbox` intercepts Pi's `read`, `write`, and `edit` tools. It runs `bash` commands and their descendants under macOS Seatbelt or Linux bubblewrap. A denied operation fails without an unsandboxed retry.

The worker fails closed if the extension, its required `rg` executable, the generated configuration, or the project boundary is unavailable.

## Verification

Tests must prove that:

- the unpinned package is installed, global, and refreshed by the existing maintenance flow;
- every runtime snapshot contains the mandatory configuration;
- both JSON and RPC sessions load the extension;
- a project-local sandbox configuration is rejected;
- Pi can read and write inside its current project;
- Pi cannot read or write a sibling project or use `sudo`;
- an outbound network request remains permitted.

The complete backend suite must remain green. OS-level integration checks may skip only when the host platform lacks the sandbox primitive; production startup must still fail closed.

## Deferred Work

Network allowlists, per-project sandbox exceptions, `sudo` opt-in, browser automation, containers, and virtual machines are not included.
