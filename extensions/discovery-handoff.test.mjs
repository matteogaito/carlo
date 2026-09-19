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

test("Discovery tool accepts complete local-worker packages and existing proposal identity", async () => {
  const { default: discoveryGuard } = await import("./carlo-discovery-guard.mjs");
  let tool;
  discoveryGuard({ on() {}, registerTool(value) { tool = value; } });

  const proposal = tool.parameters.properties.task_proposals.items.properties;
  assert.ok(proposal.id);
  assert.ok(proposal.created_task_id);
  const workPackage = proposal.metadata.properties.implementation_tasks.items.properties;
  for (const field of ["id", "title", "position", "objective", "files", "interfaces", "changes", "constraints", "verification", "done_when", "budget"]) {
    assert.ok(workPackage[field], `missing work-package field: ${field}`);
  }
  const file = workPackage.files.items.properties;
  for (const field of ["path", "mode", "ranges", "symbols", "reason"]) {
    assert.ok(file[field], `missing package-file field: ${field}`);
  }
  assert.ok(workPackage.verification.properties.commands);
  assert.ok(workPackage.verification.properties.success);
  assert.ok(workPackage.budget.properties.max_tool_calls);
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
