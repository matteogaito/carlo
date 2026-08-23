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

1. Clearly propose each Task in chat.
2. Put the same proposals in `task_proposals`.
3. Wait for user confirmation; CARLO creates the real Tasks.

Each proposal contains:

- `title`: concise goal title;
- `megaprompt`: self-contained implementation input with goal, scope, relevant findings and decisions, evidence, files/symbols, acceptance criteria, dependencies, constraints, and validation commands;
- `depends_on`: titles of proposals that must finish first.

Do not create placeholder candidates, split work merely for organizational neatness, or include unrelated Discovery history. A Discovery may create zero, one, or many Tasks and remain open afterward.

## Common mistakes

- Generic advice without repository evidence: inspect the relevant flow first.
- Repeating the whole transcript: update concise structured state instead.
- Treating a hypothesis as a finding: label it and verify where practical.
- Sneaking implementation into diagnostics: stop and propose a Task.
- Producing an underspecified megaprompt: include the evidence already gathered so the planner need not repeat Discovery.
