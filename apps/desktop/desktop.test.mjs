import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

test("Tauri v2 desktop config reuses apps/web and enforces CSP without secrets", () => {
  const confPath = path.join(here, "src-tauri", "tauri.conf.json");
  const conf = JSON.parse(fs.readFileSync(confPath, "utf-8"));
  assert.equal(conf.productName, "ApplyPilot AI");
  assert.equal(conf.build.devUrl, "http://localhost:3000");
  assert.match(conf.app.security.csp, /default-src 'self'/);
  const serialized = JSON.stringify(conf);
  assert.doesNotMatch(serialized, /refresh_token|client_secret|GOOGLE_CLIENT/i);
});

test("Rust desktop shell exposes backend readiness command without token storage", () => {
  const mainRs = fs.readFileSync(path.join(here, "src-tauri", "src", "main.rs"), "utf-8");
  assert.match(mainRs, /check_backend_health/);
  assert.match(mainRs, /tauri_plugin_notification/);
  assert.doesNotMatch(mainRs, /refresh_token|localStorage/i);
});
