import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

test("Discovery skill defines the state and task handoff contracts", async () => {
  const skill = await readFile("skills/carlo-discovery/SKILL.md", "utf8");
  for (const required of ["discovery_state", "findings", "decisions", "unresolved_questions", "megaprompt", "brief_markdown", "plan_markdown", "implementation_phases"]) {
    assert.match(skill, new RegExp(required));
  }
});

test("CARLO UI skill keeps the saved visual reference discoverable", async () => {
  const skill = await readFile("skills/carlo-ui-design/SKILL.md", "utf8");
  assert.match(skill, /reference\.png/);
  assert.match(skill, /mobile-first/);
  assert.match(skill, /pill/i);
});
