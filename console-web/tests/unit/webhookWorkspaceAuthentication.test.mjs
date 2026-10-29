import assert from "node:assert/strict";
import test, { afterEach } from "node:test";
import * as webhooks from "../../src/lib/webhooks.js";

const originalFetch = globalThis.fetch;
const originalDocument = globalThis.document;
const csrf = "c".repeat(48);
afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalDocument === undefined) delete globalThis.document;
  else globalThis.document = originalDocument;
});

const operations = [
  ["listWebhooks", {}, "GET"],
  ["createWebhook", { name: "Test", endpointUrl: "https://receiver.example.test/events", eventTypes: ["webhook.test"] }, "POST"],
  ["testWebhook", { webhookEndpointId: "wh_example" }, "POST"],
  ["listWebhookDeliveries", { webhookEndpointId: "wh_example" }, "GET"],
  ["replayWebhook", { webhookEndpointId: "wh_example", webhookDeliveryId: "whd_example", idempotencyKey: "console_webhook_example" }, "POST"],
  ["rotateWebhookSecret", { webhookEndpointId: "wh_example" }, "POST"],
  ["listWebhookDeadLetters", {}, "GET"],
  ["replayWebhookDelivery", { webhookDeliveryId: "whd_example", idempotencyKey: "console_0123456789abcdef" }, "POST"],
  ["archiveWebhookDeadLetter", { deadLetterEventId: "whdl_example" }, "POST"],
];

for (const [name, input, method] of operations) {
  test(`${name} uses explicit Workspace authentication without a visible session cookie`, async () => {
    globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${csrf}` };
    const calls = [];
    globalThis.fetch = async (url, options) => {
      calls.push({ url, options });
      return new Response("{}", { status: 200 });
    };
    const controller = new AbortController();
    await webhooks[name]({ controlApiUrl: "https://control.example.test", authentication: "workspace", signal: controller.signal, ...input });
    assert.equal(calls.length, 1);
    const { options } = calls[0];
    assert.equal(options.method, method);
    assert.equal(options.credentials, "include");
    assert.equal(options.redirect, "error");
    assert.equal(options.signal, controller.signal);
    assert.equal(options.headers.Authorization, undefined);
    assert.equal(options.headers["X-CSRF-Token"], method === "GET" ? undefined : csrf);
  });
}

test("Webhook authentication rejects mixed domains and missing CSRF before any request", async () => {
  globalThis.document = { cookie: "" };
  let calls = 0;
  globalThis.fetch = async () => { calls++; return new Response("{}"); };
  await assert.rejects(webhooks.listWebhooks({ controlApiUrl: "https://control.example.test", authentication: "workspace", token: "legacy-token" }),
    error => error.code === "workspace_session.credential_conflict");
  await assert.rejects(webhooks.rotateWebhookSecret({ controlApiUrl: "https://control.example.test", authentication: "workspace", webhookEndpointId: "wh_example" }),
    error => error.code === "workspace_session.csrf_invalid");
  await assert.rejects(webhooks.listWebhooks({ controlApiUrl: "https://control.example.test" }),
    error => error.code === "auth.token_missing");
  assert.equal(calls, 0);
});

test("Webhook legacy callers retain Bearer transport even when Workspace CSRF is visible", async () => {
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${csrf}` };
  let options;
  globalThis.fetch = async (_url, value) => { options = value; return new Response("{}"); };
  await webhooks.listWebhooks({ controlApiUrl: "https://control.example.test", token: "legacy-token" });
  assert.equal(options.credentials, "omit");
  assert.equal(options.headers.Authorization, "Bearer legacy-token");
  assert.equal(options.headers["X-CSRF-Token"], undefined);
});
