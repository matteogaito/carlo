# Backend code index

## Project file browsing

The authenticated Project API reads the local working copy, including uncommitted files. Git ignore rules, hidden-path exclusions and symlink rejection apply both to discovery and reads; previews are bounded UTF-8 text.

- Inventory and exclusion policy: `carlo/project_files.py::list_files`
- Bounded reads and directory traversal protection: `carlo/project_files.py::read_file`
- Routes: `carlo/api.py::project_files`, `carlo/api.py::project_file`

## Execution pause and resume

Stopping an execution pauses its aggregate parent and active child. The worker checks task status before saving a technical plan, starting an attempt, or advancing a slice.

- API actions: `carlo/api.py::stop_task`, `carlo/api.py::resume_task`
- Active Pi cancellation: `carlo/worker.py::_run_implementation_with_stop`, `carlo/provider.py::PiProvider.stop_tasks`
- Worker guard: `carlo/orchestrator.py::ImplementationPipeline._require_running`
- Queue eligibility: `carlo/orchestrator.py::Orchestrator._eligible`

## Technical planning activity

The implementation worker records Pi activity while preparing a child's technical plan. These events let the task view show progress before local execution begins.

- Entry point: `carlo/orchestrator.py::ImplementationPipeline._prepare_technical_plan`
- Profile: `technical-plan`, seeded by Alembic with the feature planner's model and independent low reasoning
- Event mapping: `carlo/api.py::_planning_activity`
- Plan invocation: `carlo/planning.py::Planner.plan`

## Technical replan and slice checkpoints

The child task action stops the current attempt, queues a new technical plan from its approved feature, and restores the parent checkout to the last verified slice. Unverified edits are kept as a patch. A pending unapproved revision is superseded by the new plan. Each passing slice is committed before the next slice starts; file scope covers every slice of the child task.

- Action and event: `carlo/api.py::replan_technical_task`
- Restore and planner handoff: `carlo/orchestrator.py::ImplementationPipeline._restore_for_technical_replan`, `ImplementationPipeline._prepare_technical_plan`
- Patch archive: `carlo/git.py::GitWorkspace.archive_and_restore`
- Slice commit and scope: `carlo/orchestrator.py::ImplementationPipeline.run`

## Planner retries and inherited feature contract

Each planner call uses a fresh Pi session, including format retries. Technical slices retain the approved feature constraints; the final slice also carries its interfaces and acceptance criteria.

- Session isolation and format retries: `carlo/planning.py::Planner.plan`
- Feature contract: `carlo/orchestrator.py::ImplementationPipeline._prepare_technical_plan`

## Model thinking policy

Explicit profile effort enables reasoning in Pi for remote models even when a generic model catalog omits that capability. The omlx provider runs with thinking off.

- Catalog parsing: `carlo/model_providers.py::parse_openai_models`
- Effective profile: `carlo/model_providers.py::resolve_agent_profile`
- Pi model configuration: `carlo/pi_runtime.py::PiRuntimeSnapshotBuilder.materialize`

## Agent context limits

The model catalog or a model override supplies the context window; models without either cannot be selected. Each profile may set a power-of-two cap of at least 16k; runtime reserve and recent-context settings determine the usable input and the implementation context pack budget.

- Runtime policy: `carlo/model_providers.py::resolve_runtime_policy`
- Context pack allowance: `carlo/model_providers.py::context_pack_budget`
- Planner target: `carlo/model_providers.py::implementation_planning_target`
- Execution: `carlo/orchestrator.py::ImplementationPipeline.run`
- Settings API: `carlo/api.py::_profile_view`, `carlo/api.py::update_agent_profile`

## Planner skill isolation

Pi sessions disable global skill discovery while keeping Carlo's explicit profile skills and packages. The plan profile keeps Carlo's planning skill and excludes the Superpowers package, whose approval flow conflicts with Carlo's JSON planning contract. Other profiles keep their configured packages.

- Pi launch flags: `carlo/provider.py::PiProvider.run`, `PiProvider.open_conversation`
- Package resolution: `carlo/model_providers.py::resolve_agent_profile`
