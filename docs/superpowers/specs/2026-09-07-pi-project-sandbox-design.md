# Pi Project Sandbox Design

## Goal

Run every Pi session inside an operating-system sandbox comparable to the workspace-write isolation used by coding agents such as Codex and Claude Code. The project repository or task worktree is Pi's only writable user-data filesystem boundary. Pi must not inspect other projects or user files, modify Carlo's installation, or control macOS services.

## Scope

The policy applies centrally to every `PiProvider` launch, including planning, Discovery, implementation, retries, and persistent RPC conversations. Existing profile tool lists remain capability hints; they are not the security boundary.

`sudo` is always forbidden. A future opt-in mechanism is explicitly out of scope.

## Sandbox Boundary

Carlo resolves the supplied working directory to a real, existing directory before launching Pi. That resolved directory is the project boundary.

The macOS process is launched through `/usr/bin/sandbox-exec` with a generated Seatbelt profile that:

- permits reading and writing beneath the resolved project boundary;
- permits only explicitly enumerated read access needed to start Pi and load the selected system toolchain, managed skills, packages, and runtime configuration;
- permits access only to the exact Carlo-owned runtime and session paths assigned to the current Pi session;
- permits outbound network access required by the configured model provider;
- permits child processes needed by project build and test commands;
- denies `sudo` and `launchctl` execution;
- denies signalling unrelated processes;
- denies filesystem access to other projects and user-data paths.

Unlike an interactive coding agent, Carlo cannot safely ask for elevation while Pi runs unattended. A denied operation therefore fails immediately; there is no approval or unsandboxed fallback path.

Paths inserted into a Seatbelt profile are escaped as data after symlink resolution. Missing `sandbox-exec`, an invalid boundary, or sandbox startup failure is fatal: Carlo must not launch Pi without confinement.

## Data Flow

`PiProvider.run` and `PiProvider.open_conversation` both build their existing Pi argument list, then pass it with the resolved working directory to one shared sandbox-command builder. Context-compaction retries reuse the same boundary and policy. Carlo remains outside the sandbox and retains responsibility for session lifecycle and termination.

## Error Handling

Sandbox-policy failures become `ProviderError` messages that identify the rejected boundary or unavailable sandbox without exposing model credentials. A denied operation appears to Pi as an ordinary permission failure and cannot trigger an unsandboxed retry.

## Verification

Provider tests must prove both JSON and RPC launches use the sandbox wrapper. A macOS integration probe must prove that a sandboxed child can create and read a file inside its project, while attempts to read or write a sibling project, execute `sudo` or `launchctl`, and signal an unrelated process fail.

Existing provider tests and the complete backend test suite must continue to pass. Tests on non-macOS hosts may skip only the Seatbelt integration probe; production launch must still fail closed when the configured sandbox executable is unavailable.

## Deferred Work

Per-project permission to use `sudo`, container or VM isolation, and configurable sandbox exceptions are not included. They should be designed separately only if a concrete project cannot operate within this boundary.
