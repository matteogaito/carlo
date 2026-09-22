# Unified Carlo planning for Discovery and direct tasks

## Intent

Discovery is a conversation that gathers repository evidence, resolves product and technical decisions, and proposes actionable work. It may propose one parent Task or many. Each parent may have as many ordered implementation subtasks as the work requires. The user reviews the complete parent–child tree before creating anything.

The plan shown in Discovery must be produced by the same Carlo planning workflow and `plan` profile used for a directly created Task. Creating a proposal must persist exactly the reviewed plan; it must not silently generate a different one. A Task or subtask is complete only after its declared tests pass. Failed tests trigger another focused implementation attempt or an explicit blocked state, never completion.

## Current gap

Direct Tasks use `continue_planning` and the `plan` profile in `backend/carlo/api.py`. Discovery currently accepts a plan written inside `discovery_state`, validates its JSON shape, and creates and approves the Task and children directly in `create_discovery_tasks`. The two routes therefore share a data shape but not the planner. The Discovery UI shows parent titles and `implementation_phases`, not the actual `implementation_tasks` that become children.

The first PHOTODIGGER-69 proposal illustrates the gap: one child combined an archive, mounted-volume detection, application injection, migration, and tests across six files. Its 149-character Brief did not convey the architecture to the child. The schema accepted the proposal because it was structurally complete. This example motivates a semantic planning review, not a fixed minimum number of children or a rule based on file count.

## Planning flow

1. Discovery explores and discusses the repository. It proposes **parent candidates** with a title, goal, evidence and decisions, and dependencies on other candidates. It does not author a competing implementation plan.
2. For each candidate ready for implementation, Carlo runs the canonical planner using the configured `plan` profile. The planner receives the candidate goal, project policies, and a structured handoff from Discovery. The handoff includes relevant findings, decisions, inspected paths and symbols, constraints, and unresolved questions that affect implementation. It excludes the raw chat transcript.
3. The same planning service handles direct Tasks. It produces the existing `PlanPayload`: `brief_markdown`, `plan_markdown`, and ordered `metadata.implementation_tasks`. Both origins use the same planning skill, output validation, and correction loop. A direct Task persists the result as an unapproved `PlanRevision`; a Discovery candidate stores it as an unapproved draft in Discovery state.
4. Discovery displays the canonical draft as a forest of parent candidates and their ordered children. User feedback in the conversation invalidates affected drafts; if the affected set is unclear, all pending drafts are invalidated. Carlo replans invalidated candidates with the new decisions. A draft that fails planning or validation cannot be created.
5. `Create task` or `Create all tasks` persists the exact displayed draft as the parent's approved `PlanRevision` and materializes its children through the same approval operation used by direct Tasks. A single parent that depends on an uncreated parent cannot be created alone; `Create all tasks` creates dependencies first or rejects a cycle. Repeated Create requests cannot duplicate Tasks. Created Tasks retain `created_source="discovery"` for provenance; the planning path is otherwise identical.

The shared planner should be extracted from the current API handler, along with approval/materialization. No separate Discovery planner or new plan format is needed. Existing created Tasks and historical plans remain unchanged. Pending legacy Discovery proposals require canonical planning before their Create action is enabled.

## Plan quality contract

The Brief is the stable, bounded parent context: intended outcome, relevant architecture, existing interfaces, invariants, decisions, repository evidence, and acceptance criteria. It must contain enough substance to guide a new implementation session while excluding tool logs and the Discovery transcript. Carlo places this identical Brief before each child's variable work package and file context. This can enable prefix reuse between sibling prompts when the model and provider support it; cache hits are a possible benefit, not a correctness condition.

The planner chooses parent boundaries by distinct useful outcomes and child boundaries by independently verifiable progress. It may return one or many of either. It does not optimize for the fewest items. For each child it must state the starting dependency, concrete result, interfaces to preserve or expose, work to perform, and checks that can pass at that stage. It must review the entire proposed tree for omitted work, incompatible interfaces, circular or missing dependencies, and checks that require future work. Large prompt size is a separate context constraint; a small initial context pack is not evidence that implementation effort is small, especially when files do not yet exist.

Structural validation remains deterministic: required fields, ordering, unique IDs, valid paths, package completeness, and valid dependencies. The planner's review supplies the semantic judgment; no arbitrary minimum number of children or maximum file count is a substitute. The Discovery and direct-task planning instructions must express the same quality contract.

## Tests and completion

Each child has runnable verification appropriate to the outcome it leaves behind. Carlo executes the verification declared for that child, records failures, and resumes implementation from the current checkout and failure evidence. The parent plan also declares final integration checks; the final child must pass those checks before the parent can become `DONE`. Earlier children must leave the project in a state where their own checks pass. A plan that cannot identify such boundaries needs different children or a different parent boundary.

Exhausted retries, an unavailable test environment, or a required scope change leave the child and parent blocked with evidence. They never count as success. Tool-call budget exhaustion remains an exceptional recovery path, not a reason to weaken test gates or force a fixed number of subtasks. This design does not change OMLX.

## Discovery preview

The main Discovery conversation shows a proposal forest before the Create controls. Each parent row shows its goal, number of children, parent dependencies, and planning status. Expanding it shows ordered child titles and outcomes; expanding a child shows files, interfaces, constraints, and verification. Brief and full plan remain available in the existing context panel. The tree uses the actual `implementation_tasks` from the canonical draft, not `implementation_phases` as a substitute.

Native disclosure controls support keyboard use and keep a large forest scannable on narrow screens. `Create task` creates one reviewed parent with its children; `Create all tasks` creates all ready, mutually consistent parents. A planning error is shown on the affected parent, and its Create control is disabled. User feedback in chat can revise the tree before creation.

## Verification

- A Discovery candidate and a direct Task with equivalent input invoke the same planning service and `plan` profile, and both reject the same invalid plan.
- Discovery previews the exact ordered `implementation_tasks` that creation materializes; one and many parents and children render correctly.
- Editing a proposal through Discovery feedback invalidates its old draft, and creation cannot apply a stale or failed draft.
- Per-child failed tests prevent child completion and allow a focused retry. The parent becomes `DONE` only after all children and final integration checks pass.
- Existing created Tasks, direct-task planning, and Discovery creation continue to work without changing OMLX.
