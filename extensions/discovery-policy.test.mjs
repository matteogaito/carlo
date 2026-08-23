import test from "node:test";
import assert from "node:assert/strict";

import { allowedDiscoveryCommand } from "./discovery-policy.mjs";

test("allows repository inspection and validation commands", () => {
  assert.equal(allowedDiscoveryCommand("git status --short", []), true);
  assert.equal(allowedDiscoveryCommand("pytest -q", []), true);
  assert.equal(allowedDiscoveryCommand("make verify", ["make verify"]), true);
});

test("blocks mutation and shell composition", () => {
  assert.equal(allowedDiscoveryCommand("git reset --hard", []), false);
  assert.equal(allowedDiscoveryCommand("pytest -q > result", []), false);
  assert.equal(allowedDiscoveryCommand("python -c 'open(\"x\", \"w\")'", []), false);
});
