import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

test("Discovery skill defines the state and task handoff contracts", async () => {
  const skill = await readFile("skills/carlo-discovery/SKILL.md", "utf8");
  for (const required of ["discovery_state", "findings", "decisions", "unresolved_questions", "task_proposals", "megaprompt", "depends_on", "canonical planning profile", "child tree"]) {
    assert.match(skill, new RegExp(required));
  }
  assert.match(skill, /dependent outcomes.*one parent/i);
});

test("Rework skill defines the fix proposal contract", async () => {
  const skill = await readFile("skills/carlo-rework/SKILL.md", "utf8");
  for (const required of ["task_fix_proposal", "revise_task", "revise_parent", "objective", "files", "changes", "constraints", "verification", "done_when", "budget"]) {
    assert.match(skill, new RegExp(required));
  }
});

test("Planning skill distinguishes feature outcomes from technical packages", async () => {
  const skill = await readFile("skills/carlo-planning/SKILL.md", "utf8");
  for (const required of ["feature plan", "technical plan", "Do not prescribe files", "current checkout", "15,000–20,000", "max_tool_calls", "objective", "interfaces", "changes", "constraints", "verification", "done_when"]) {
    assert.match(skill, new RegExp(required));
  }
});

test("CARLO UI skill keeps the saved visual reference discoverable", async () => {
  const skill = await readFile("skills/carlo-ui-design/SKILL.md", "utf8");
  assert.match(skill, /reference\.png/);
  assert.match(skill, /mobile-first/);
  assert.match(skill, /pill/i);
});
