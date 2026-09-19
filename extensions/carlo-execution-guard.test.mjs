import assert from "node:assert/strict";
import test from "node:test";
import guard from "./carlo-execution-guard.mjs";

test("blocks the next tool before it executes", () => {
  const handlers = {};
  guard({ on(name, handler) { handlers[name] = handler; } }, { maxToolCalls: 2 });
  const call = { toolName: "read", input: { path: "README.md" } };
  assert.equal(handlers.tool_call(call), undefined);
  assert.equal(handlers.tool_call(call), undefined);
  let aborted = false;
  assert.deepEqual(handlers.tool_call(call, { abort() { aborted = true; } }), {
    block: true, reason: "Carlo tool-call budget exceeded (2)",
  });
  assert.equal(aborted, true);
});

test("request diagnostics omit content and credentials", () => {
  const oldFlag = process.env.CARLO_PI_REQUEST_DIAGNOSTICS;
  process.env.CARLO_PI_REQUEST_DIAGNOSTICS = "true";
  const handlers = {};
  const output = [];
  const originalWrite = process.stderr.write;
  process.stderr.write = (line) => { output.push(line); return true; };
  try {
    guard({ on(name, handler) { handlers[name] = handler; } }, { maxToolCalls: 1 });
    handlers.before_provider_request({ payload: {
      messages: [{ role: "user", content: "secret message" }],
      tools: [{ name: "read" }],
      model: "local", max_tokens: 100, api_key: "secret-key",
      chat_template_kwargs: { enable_thinking: false, secret: "secret-nested" },
    } });
  } finally {
    process.stderr.write = originalWrite;
    if (oldFlag === undefined) delete process.env.CARLO_PI_REQUEST_DIAGNOSTICS;
    else process.env.CARLO_PI_REQUEST_DIAGNOSTICS = oldFlag;
  }
  assert.equal(output.length, 1);
  assert.equal(output[0].includes("secret"), false);
  const data = JSON.parse(output[0].replace("CARLO_PI_REQUEST_DIAGNOSTIC ", ""));
  assert.equal(data.blocks[0].role, "user");
  assert.equal(data.blocks[0].length, 14);
  assert.equal(data.tools, 1);
  assert.equal(data.params.model, "local");
  assert.deepEqual(data.params.chat_template_kwargs, { enable_thinking: false });
});
