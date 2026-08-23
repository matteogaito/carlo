---
name: carlo-discovery
description: Use when Pi conducts a persistent CARLO conversation to understand a repository, investigate behavior, or turn product discussion into one or more implementation-ready Tasks without modifying source code.
---

# CARLO Discovery

Understand before changing. Explore the real repository, preserve evidence, and help the user reach clear decisions. A Discovery is not an implementation Task.

## Workflow

1. Read the current Discovery memory and the newest user message.
2. Inspect only the repository areas needed to answer well. Prefer concrete files, symbols, Git evidence, logs, configuration, and existing patterns over guesses.
3. Use tests, lint, build, or diagnostics when they resolve a real question. Report what actually ran and its result.
4. Respond conversationally in Markdown. Explain conclusions plainly, distinguish evidence from inference, and ask only the next useful question.
5. Call `discovery_state` exactly once before finishing every turn. Send the complete current state, not a delta.

Discovery is read-only. Never use edit/write tools, modify source, commit, install dependencies, alter services, or bypass a blocked command. If mutation is necessary, explain why and propose a Task.

## State contract

Keep these fields concise and cumulative:

- `summary`: current understanding sufficient to resume later;
- `findings`: evidence-backed facts with paths, symbols, or command results;
- `decisions`: choices explicitly agreed with the user;
- `unresolved_questions`: only questions that still affect the outcome;
- `inspected_resources`: repository paths and relevant external resources read;
- `commands`: commands actually executed plus material result;
- `task_proposals`: current implementation-ready proposals, or an empty list.

Do not use memory as a substitute for the transcript. Correct stale state when later evidence disproves it.

## Task handoff

When a concrete change emerges, identify distinct Tasks and ask only for missing high-impact information. Once sufficient:

1. Build an evidence-oriented Brief and implementation Plan while repository
   context is already available in the Discovery.
2. Clearly propose each fully planned Task in chat.
3. Put the same proposals in `task_proposals`.
4. Wait for user confirmation; CARLO creates and approves the real Tasks.

Each proposal contains:

- `title`: concise goal title;
- `megaprompt`: self-contained implementation input with goal, scope, relevant findings and decisions, evidence, files/symbols, acceptance criteria, dependencies, constraints, and validation commands;
- `depends_on`: titles of proposals that must finish first;
- `brief_markdown`: repository understanding, evidence, constraints, risks,
  assumptions, relevant files and symbols;
- `plan_markdown`: ordered, technical implementation strategy with invariants,
  error handling, validation and stopping conditions;
- `metadata`: `skills`, concise ordered `implementation_phases`, verified
  `validation_commands`, the four validation/deployment booleans, `risk_flags`,
  and `affected_areas`, using the same contract as CARLO planning.

Only emit a proposal after high-impact questions are resolved. The plan should
shift costly decisions upstream without brittle line-by-line pseudocode. The
user's Create action approves the displayed plans, so incomplete proposals must
remain questions in the conversation instead of entering `task_proposals`.

Do not create placeholder candidates, split work merely for organizational neatness, or include unrelated Discovery history. A Discovery may create zero, one, or many Tasks and remain open afterward.

## Common mistakes

- Generic advice without repository evidence: inspect the relevant flow first.
- Repeating the whole transcript: update concise structured state instead.
- Treating a hypothesis as a finding: label it and verify where practical.
- Sneaking implementation into diagnostics: stop and propose a Task.
- Producing an underspecified handoff: include the evidence and complete plan so
  the Task does not repeat Discovery or planning.
