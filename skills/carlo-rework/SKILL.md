---
name: carlo-rework
description: Use when Pi discusses a failed CARLO Task with the user to diagnose why it failed and agree a fix, without modifying source code.
---

# CARLO Rework

A failed Task already has evidence: the approved plan, what each attempt tried, and the diagnosis from automatic escalation. Read it, explain it plainly, and agree a fix with the user before proposing anything.

## Workflow

1. Read the failure context given in the first message: task goal, current plan, attempt outcomes, escalation diagnosis and evidence (diff, test errors).
2. Explain in your own words why it failed. Distinguish what is certain (from evidence) from what is your inference.
3. Discuss the fix with the user. Ask only the questions that change the outcome.
4. Decide the right shape for the fix:
   - `revise_task`: the task's own work package needs different instructions, constraints, or file scope, but the task itself is still the right unit of work.
   - `revise_parent`: the task's scope was wrong from the start and needs to be re-split into multiple smaller tasks under its parent.
5. Call `task_fix_proposal` only after the user has explicitly agreed to a specific fix. Never call it to "checkpoint" an idea the user hasn't confirmed.

Rework is read-only. Never use edit/write tools, modify source, commit, install dependencies, or bypass a blocked command. If you need to inspect current file contents to ground the diagnosis, read them — this is the same read-only repository access as `carlo-discovery`.

## Proposal contract

`task_fix_proposal` takes:

- `action`: `revise_task` or `revise_parent`;
- `summary`: one paragraph explaining the fix and why, for the user;
- `brief_markdown` / `plan_markdown`: updated understanding and plan for the affected task(s);
- `package` (for `revise_task`): one complete work package in the same shape as CARLO planning uses (`id`, `title`, `position`, `objective`, `files`, `interfaces`, `changes`, `constraints`, `verification`, `done_when`, `budget`);
- `packages` (for `revise_parent`): the complete replacement set of work packages for the parent task.

A work package must describe every file it touches with a reason, and `changes` must cover exactly the files marked `edit`/`create`. Set `budget.max_tool_calls` from the evidence: 15–20 for small local work, 25–35 for several files or compile/test iteration, and 40–50 for costly build systems, migrations, or integration work. If it needs more than 50, use `revise_parent` with smaller packages.

## Common mistakes

- Proposing a fix before the user has agreed to it in chat.
- Repeating the whole diagnosis instead of the parts that are still relevant to the open question.
- Choosing `revise_task` when the real problem is that the task combined too much scope for any single budget — that calls for `revise_parent`.
- Leaving `changes` or `files` inconsistent with each other in the proposed package(s).
