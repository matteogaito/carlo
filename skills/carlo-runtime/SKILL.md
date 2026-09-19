---
name: carlo-runtime
description: Use when Pi runs commands, builds, tests, or diagnostics inside Carlo.
---

# CARLO Runtime

Pi runs inside a project-scoped filesystem sandbox. Treat the current project directory as the durable read/write boundary.

## Files and commands

- Keep logs, caches, temporary files, build products, DerivedData, result
  bundles, and package checkouts inside the project. Prefer existing ignored
  project paths; otherwise use a hidden project-local scratch directory and
  remove it after validation.
- Never write explicitly to `/tmp`, the home directory, or another project.
  Do not assume `TMPDIR` relocates APIs that choose a system temp directory.
- Never use `sudo`, weaken the outer sandbox, or edit sandbox policy. When a
  required operation remains blocked, report the exact path or service.
- Prefer the repository's existing build and test commands.

## Xcode and Swift

An outer macOS sandbox can conflict with Swift's nested macro sandbox. When
`swift-plugin-server` produces a malformed response or macros such as
`@Observable` cannot load, run Xcode with:

```sh
xcodebuild ... OTHER_SWIFT_FLAGS='$(inherited) -disable-sandbox'
```

This disables only Swift's subprocess sandbox; Carlo's outer sandbox remains.
Keep `-derivedDataPath` and any captured output inside the project. Select an
explicit destination when known. Simulator-service warnings are not source
failures for a macOS-only build; genuine simulator tests may still require a
reported runtime boundary.
