# Prompt — Implement `carlo-planning` Skill for Pi

Create a Pi skill named:

`carlo-planning`

with its main instructions in:

`skills/carlo-planning/SKILL.md`

## Purpose

This skill defines how Pi must produce high-quality implementation plans for CARLO.

The planning session will use a strong GPT model, initially GPT-5.6 Sol, but the skill must remain model-agnostic.

The implementation model that will later execute the plan may be a smaller/local coding model such as KAT.

Therefore the planner should deliberately move difficult, high-impact reasoning upstream so the implementation model receives a concrete, technically useful plan.

---

## Core principles

The planner must:

- inspect the repository before proposing the plan
- understand the existing architecture and conventions
- inspect relevant source files, interfaces, tests, configuration, and project instructions
- reuse existing abstractions and patterns where possible
- avoid unnecessary dependencies
- avoid unnecessary architectural changes
- avoid overengineering
- keep the task goal and approved scope central
- explicitly identify important technical assumptions
- identify risks and unknowns before implementation
- optimize the plan for the capabilities of the target implementation model

The plan must be technical and implementation-oriented.

Do not produce a generic project-management checklist.

---

## Repository discovery

Before finalizing the plan, inspect enough of the repository to understand:

- relevant modules
- likely files involved
- important classes/functions/interfaces/symbols
- existing implementation patterns
- test structure
- build/run workflow
- lint/typecheck tooling
- relevant dependencies
- logging/error-handling conventions
- project-specific documentation/instructions
- Git/project structure where relevant

Do not blindly read the entire repository.

Explore selectively and deeply enough to make the plan reliable.

If something important is unclear, inspect more repository context before making assumptions.

---

## Brief

Before or as part of planning, construct a concise internal/explicit Brief containing:

- task goal
- relevant architecture
- relevant repository areas
- important files/modules/symbols
- existing patterns to reuse
- constraints
- assumptions
- risks
- unknowns
- project-specific requirements
- validation capabilities available in the repository

Keep the Brief evidence-oriented and reference concrete repository locations where useful.

---

## Planning for a weaker/local implementation model

Assume the implementation model may be less capable than the planner.

Resolve high-impact decisions in the plan whenever reasonable.

The plan should tell the implementation model:

- where to intervene
- which modules/files/symbols are likely involved
- which existing patterns to follow
- which abstractions to reuse
- what must not be broken
- which order of changes is recommended
- what tests should be added or updated
- how to validate each phase
- which build/run commands are relevant
- when browser validation is required
- what logging/error-handling expectations apply
- whether documentation/changelog/migrations/deployment are affected

Do not leave major architecture decisions to the smaller implementation model unless they genuinely depend on discoveries that can only happen during implementation.

At the same time, do not over-specify low-risk details such as:

- exact local variable names
- trivial refactors
- exact line numbers
- brittle pseudocode
- equivalent low-level implementation choices

---

## Plan structure

Produce a plan made of clear implementation phases.

Each phase should normally include:

### Objective
What this phase must achieve.

### Relevant areas
Files/modules/classes/functions/interfaces likely involved.

### Approach
How to implement the phase and which existing patterns to follow.

### Invariants / constraints
What must remain true.

### Expected outcome
What should exist or work when the phase is complete.

### Validation
How the implementation agent must verify success.

Example:

```markdown
## Phase 2 — Order synchronization

### Relevant areas
- `backend/integrations/vinted/adapter.py`
- `backend/orders/services/importer.py`
- `tests/integrations/vinted/`

### Approach
- implement Vinted order ingestion through the existing marketplace adapter abstraction
- reuse the existing order importer instead of introducing another persistence path
- keep marketplace-specific state mapping inside the adapter
- preserve idempotency using the existing external order identifier
- reuse the project's existing retry mechanism for remote calls

### Invariants
- no duplicate orders from repeated remote events
- marketplace-specific rules must not leak into the core order domain

### Validation
- mapping tests
- duplicate-ingestion test
- retry/failure tests
- run synchronization with available fixtures or sandbox
```

---

## Validation requirements

Every implementation plan must consider real executable validation.

Where applicable, identify:

- compilation/build
- type checking
- linting
- unit tests
- integration tests
- application startup
- runtime smoke tests
- CLI execution
- browser validation
- console errors
- network failures
- relevant user flows

"Tests pass" is not automatically equivalent to "the feature works".

Prefer validation as close as possible to real usage.

If full validation is impossible because of missing credentials, infrastructure, hardware, or external services, state that clearly.

---

## Browser validation

For web-facing changes, explicitly determine whether browser validation is needed.

When applicable, the plan should require the implementation agent to:

- start the application
- open the relevant page/flow
- validate the feature
- inspect browser console errors
- inspect failed network requests
- exercise the relevant user path

Do not require browser validation for tasks where it adds no value.

---

## Logging and error handling

The plan must consider:

- meaningful error handling
- structured/useful logging where appropriate
- avoiding silent failures
- avoiding noisy or redundant logging
- avoiding leakage of sensitive data

Do not invent a new logging architecture when the repository already has one.

---

## Documentation and changelog

Determine whether the task requires:

- changelog update
- user-facing documentation
- developer documentation
- API documentation
- migration notes

Do not force documentation updates where they are irrelevant.

---

## Skills for implementation

Recommend the skill set the implementation coding agent should use.

Examples:

- `carlo-implementation`
- `carlo-testing`
- `carlo-debugging`
- `carlo-browser-validation`
- `python-backend`
- `typescript-node`
- `frontend-design`
- `postgres`
- `docker`
- `kubernetes`
- `qt6-desktop`

Select only skills relevant to the task.

Avoid overloading smaller models with unnecessary skills.

---

## Structured metadata

Alongside the human-readable plan, provide structured metadata that CARLO can persist and consume without another LLM call.

Include at least:

```yaml
recommended_skills:
  - carlo-implementation
  - carlo-testing

validation:
  tests: true
  build: true
  run: true
  browser: false

affected_areas: []

requires_deployment: false

major_risks: []
```

Extend this schema only when it materially helps execution.

---

## Plan revisions

The plan may be revised after user feedback.

When revising:

- preserve the task goal
- incorporate explicit user constraints
- explain meaningful changes
- avoid silently changing architecture or scope
- keep previous revisions understandable
- make the new plan self-contained

If user feedback implies a major scope or architecture change, call it out explicitly.

---

## Major deviations during implementation

The implementation model may make small local adjustments.

Major changes should not be silently improvised.

Examples of major deviation:

- changing task scope
- replacing the planned architecture
- adding major dependencies
- changing public APIs
- changing the data model substantially
- changing migration strategy
- touching important areas outside the approved plan
- changing production/deployment behavior

The plan should make major decision boundaries clear enough that CARLO can request replanning/user approval if necessary.

---

## Anti-patterns

Do not produce plans like:

```text
1. Analyze the code.
2. Implement the feature.
3. Add tests.
4. Verify it works.
```

This is too generic.

Also avoid plans that attempt to pre-write the entire patch as pseudocode.

The desired level is:

> concrete enough to guide a weaker coding model, flexible enough to survive normal implementation discoveries.

---

## Final self-review

Before marking a plan ready, review it critically.

Check:

- Did I actually inspect the relevant repository areas?
- Does the plan solve the user's actual goal?
- Have I reused existing project patterns where possible?
- Have I made high-impact decisions explicit?
- Is the plan concrete enough for the target implementation model?
- Did I avoid unnecessary architecture or dependencies?
- Are validation steps specific and executable?
- Did I identify important risks and assumptions?
- Is any major ambiguity still unresolved?
- Would a smaller coding model know where to start and how to know when it is done?

If not, improve the plan before returning it.

---

## Guiding principle

The purpose of `carlo-planning` is not to produce an impressive-looking document.

Its purpose is to make downstream implementation by a local coding model more reliable.

Use the strong planning model's reasoning capacity where it creates the most leverage:

> understand deeply, decide difficult things upstream, give the implementation model a precise path, and define how success will be proven.
