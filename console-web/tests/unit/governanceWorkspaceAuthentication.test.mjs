import assert from "node:assert/strict";
import test, { afterEach } from "node:test";
import { getPermissions, replaceRolePermissions } from "../../src/lib/permissions.js";
import { listCostAnomalies, applyCostAnomalyAction } from "../../src/lib/costAnomalies.js";
import { getCurrentPolicySnapshot, listPolicySnapshots, publishPolicySnapshot, rollbackPolicySnapshot } from "../../src/lib/policySnapshots.js";

const originalFetch = globalThis.fetch;
const originalDocument = globalThis.document;
const csrf = "c".repeat(48);
afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalDocument === undefined) delete globalThis.document;
  else globalThis.document = originalDocument;
});

const operations = [
  [getPermissions, {}, "GET"],
  [replaceRolePermissions, { roleId: "role_example", expectedRevision: 1, permissionCodes: [] }, "PATCH"],
  [listCostAnomalies, {}, "GET"],
  [applyCostAnomalyAction, { anomalyId: `can_${"a".repeat(32)}`, action: "acknowledge", idempotencyKey: "anomaly_stable_key" }, "PATCH"],
  [getCurrentPolicySnapshot, {}, "GET"],
  [listPolicySnapshots, {}, "GET"],
  [publishPolicySnapshot, { reason: "Fixture publication", headEtag: '"0"', candidateEtag: `"${"a".repeat(64)}"`, requestId: "req_publish_fixture" }, "POST"],
  [rollbackPolicySnapshot, { policySnapshotId: "psn_fixture", reason: "Fixture rollback", headEtag: '"2"', requestId: "req_rollback_fixture" }, "POST"],
];

for (const [operation, input, method] of operations) {
  test(`${operation.name} preserves Workspace cookies, CSRF, cancellation and conditional headers`, async () => {
    globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${csrf}` };
    const calls = [];
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response("{}"); };
    const controller = new AbortController();
    await operation({ controlApiUrl: "https://control.test", authentication: "workspace", signal: controller.signal, ...input });
    assert.equal(calls.length, 1);
    const { options } = calls[0];
    const headers = new Headers(options.headers);
    assert.equal(options.method, method);
    assert.equal(options.credentials, "include");
    assert.equal(options.signal, controller.signal);
    assert.equal(options.redirect, "error");
    assert.equal(headers.has("Authorization"), false);
    assert.equal(headers.get("X-CSRF-Token"), method === "GET" ? null : csrf);
    if (input.headEtag) assert.equal(headers.get("If-Match"), input.headEtag);
    if (input.candidateEtag) assert.equal(headers.get("If-Policy-Candidate-Match"), input.candidateEtag);
    if (input.idempotencyKey) assert.equal(headers.get("Idempotency-Key"), input.idempotencyKey);
    if (input.requestId) assert.equal(headers.get("X-Request-ID"), input.requestId);
  });

  test(`${operation.name} rejects mixed credentials and missing authentication before fetch`, async () => {
    globalThis.document = { cookie: "" };
    let calls = 0;
    globalThis.fetch = async () => { calls++; return new Response("{}"); };
    const options = { controlApiUrl: "https://control.test", ...input };
    await assert.rejects(operation({ ...options, authentication: "workspace", token: "legacy-token" }),
      error => error.code === "workspace_session.credential_conflict");
    await assert.rejects(operation(options), error => error.code === "auth.token_missing");
    if (method !== "GET") {
      await assert.rejects(operation({ ...options, authentication: "workspace" }),
        error => error.code === "workspace_session.csrf_invalid");
    }
    assert.equal(calls, 0);
  });
}
