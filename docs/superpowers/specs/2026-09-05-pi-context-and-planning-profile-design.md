# Pi Context Recovery and Planning Profile

## Goal

Keep long Pi implementation runs from failing at the context boundary, produce smaller independently verifiable child tasks, and expose one planning-agent configuration without removing the distinct Plan and Discovery workflows.

## Context handling

CARLO currently gives Pi a context window equal to 90% of the provider limit and enables Pi compaction. `PHOTODIGGER-2-implementation-22` still reached 66,066 tokens, received `Prompt too long`, and only then wrote a compaction entry. CARLO treated the provider error as terminal even though the saved session had been compacted.

CARLO will advertise 75% of the provider context window to Pi so Pi starts its existing automatic compaction with more headroom. If a run still ends with `Prompt too long`, the provider will resume the same saved session exactly once with a short continuation message. It will not repeat the original implementation prompt. A second context failure remains terminal and uses the existing `execution.context_limit` event.

The retry remains inside `PiProvider`, where the session directory and Pi command are known. The orchestrator continues to receive one final `AgentResult` or one `ContextLimitError`; it does not own Pi-specific session recovery.

## Task sizing

The existing planner already creates one child Task for each `metadata.implementation_tasks` item. The planning skill will require each item to represent one independently verifiable outcome and split work when it crosses subsystems or needs distinct validation loops. A task may touch several tightly related files, so CARLO will not reject plans using an arbitrary character or file-count limit.

The implementation prompt format stays unchanged. The failure was caused by accumulated tool output during a two-hour run, not by the initial prompt size.

## Planning profile

CARLO will keep two workflows:

- Plan is a one-shot, machine-readable planning run for Tasks created directly from the Board.
- Discovery is a persistent read-only conversation that may produce zero or more already planned Tasks.

They will share the existing `plan` agent profile for model, effort, packages, and user-selected skills. The runtime selects the workflow skill explicitly: `carlo-planning` for Plan and `carlo-discovery` plus the read-only guard for Discovery. Discovery-created Tasks continue to bypass a second planning run.

The unused `brief` profile and the separate `discovery` profile will be removed. A migration will repoint existing Discovery rows to `plan` before deleting those profiles. Historical Task and Attempt references must remain valid; the migration will only delete a profile after all foreign-key references have been handled or proven absent.

The settings UI will show the remaining operational profiles: `plan`, `implementation`, and `escalation`.

## Failure handling

Only the provider error already recognized as `ContextLimitError` triggers context recovery. Other provider failures remain unchanged. The retry count is fixed at one to prevent loops and repeated side effects. Resuming the same session preserves Pi's compacted history and the worktree changes already made.

If the session was not saved or cannot be resumed, the retry failure is reported through the existing context-limit path. CARLO will emit an event when it retries after compaction so the UI and logs explain the extra run.

## Verification

- Provider test: a first run writes a compaction and returns `Prompt too long`; the second invocation resumes the same session with the continuation message and succeeds.
- Provider test: two context failures produce one final `ContextLimitError` and only one retry.
- Runtime test: a 65,536-token provider window is advertised to Pi as 49,152 tokens.
- API and Discovery tests: new Discoveries use the `plan` profile while loading only the Discovery workflow skill and guard.
- Migration test or database check: existing Discovery references survive removal of `brief` and `discovery`.
- Planning-skill behavioral contract: multi-subsystem work is divided into independently verifiable implementation tasks.
- Full repository test and production build before installation.
