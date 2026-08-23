import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

test("PWA manifest and service worker keep dynamic CARLO data network-only", async () => {
  const manifest = JSON.parse(await readFile("frontend/public/manifest.webmanifest", "utf8"));
  assert.equal(manifest.display, "standalone");
  assert.ok(manifest.icons.some((icon) => icon.sizes === "192x192"));
  const worker = await readFile("frontend/public/sw.js", "utf8");
  assert.match(worker, /\/api\//);
  assert.match(worker, /fetch\(request\)/);
  assert.doesNotMatch(worker, /caches\.match\(request\).*\/api/s);
  const index = await readFile("frontend/index.html", "utf8");
  assert.match(index, /manifest\.webmanifest/);
  assert.match(index, /apple-touch-icon/);
});
