import test from "node:test";
import assert from "node:assert/strict";
import { registerHooks } from "node:module";

const typeStub = `export const Type = {
  String: () => ({ type: "string" }),
  Boolean: () => ({ type: "boolean" }),
  Integer: () => ({ type: "integer" }),
  Array: (items) => ({ type: "array", items }),
  Object: (properties) => ({ type: "object", properties }),
  Record: (_keys, values) => ({ type: "object", additionalProperties: values }),
  Literal: (value) => ({ const: value }),
  Union: (items) => ({ anyOf: items }),
  Optional: (schema) => schema,
};`;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@earendil-works/pi-ai") {
      return { url: `data:text/javascript,${encodeURIComponent(typeStub)}`, shortCircuit: true };
    }
    return nextResolve(specifier, context);
  },
});

test("Discovery tool captures candidates while Carlo owns work packages", async () => {
  const { default: discoveryGuard } = await import("./carlo-discovery-guard.mjs");
  let tool;
  discoveryGuard({ on() {}, registerTool(value) { tool = value; } });

  const proposal = tool.parameters.properties.task_proposals.items.properties;
  assert.ok(proposal.id);
  assert.equal(proposal.created_task_id, undefined);
  for (const field of ["title", "megaprompt", "depends_on"]) assert.ok(proposal[field]);
  assert.equal(proposal.metadata, undefined);
  assert.equal(proposal.plan_markdown, undefined);
});

test("Rework tool accepts a task-level revision or a parent-level split", async () => {
  const { default: reworkGuard } = await import("./carlo-rework-guard.mjs");
  let tool;
  const blocked = [];
  reworkGuard({
    on(event, handler) { if (event === "tool_call") blocked.push(handler); },
    registerTool(value) { tool = value; },
  });

  assert.deepEqual(tool.parameters.properties.action, { anyOf: [{ const: "revise_task" }, { const: "revise_parent" }] });
  assert.ok(tool.parameters.properties.summary);
  assert.ok(tool.parameters.properties.brief_markdown);
  assert.ok(tool.parameters.properties.plan_markdown);
  for (const field of ["id", "title", "position", "objective", "files", "interfaces", "changes", "constraints", "verification", "done_when", "budget"]) {
    assert.ok(tool.parameters.properties.package.properties[field], `missing package field: ${field}`);
    assert.ok(tool.parameters.properties.packages.items.properties[field], `missing packages field: ${field}`);
  }

  const editBlock = await blocked[0]({ toolName: "edit", input: {} });
  assert.equal(editBlock.block, true);
  const bashBlock = await blocked[0]({ toolName: "bash", input: { command: "rm -rf ." } });
  assert.equal(bashBlock.block, true);
});
