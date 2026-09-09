import assert from "node:assert/strict";
import test from "node:test";

import { renderVirtualKeyPanel } from "../../src/features/virtual-keys/renderVirtualKeyPanel.js";
import { renderConsumers } from "../../src/features/virtual-keys/renderConsumers.js";
import { createConsumer, listConsumers, updateConsumer } from "../../src/lib/consumers.js";
import { createVirtualKey, rotateVirtualKey } from "../../src/lib/virtualKeys.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const directory = { state: "ok", limit: 50, offset: 0, total: 2, consumers: [
  { consumer_id: "con_1", display_name: '<img src=x onerror="fail">', department: "Finance & Ops", status: "active", revision: 1, key_count: 2 },
  { consumer_id: "con_2", display_name: "Disabled Employee", department: "Sales", status: "disabled", revision: 3, key_count: 1 },
] };

test("consumer directory has only profile fields and escapes every user-supplied value", () => {
  const html = renderConsumers(directory);
  assert.match(html, /create-consumer-form/);
  assert.match(html, /data-revision="3"/);
  assert.match(html, /type="checkbox"/);
  assert.match(html, /&lt;img src=x onerror=&quot;fail&quot;&gt;/);
  assert.doesNotMatch(html, /<img|name="password"|name="email"|name="role"/);
  assert.match(html, /data-consumer-page="0" disabled/);
  assert.match(html, /data-consumer-page="50" disabled/);
});

test("only user managers see employee directory and assignment controls", () => {
  const state = { state: "ok", virtual_keys: [], consumerDirectory: directory };
  const admin = withPermissionSnapshot({ user: { role: "admin" } }, [
    PERMISSIONS.USER_MANAGE, PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE,
  ]);
  const html = renderVirtualKeyPanel(state, admin);
  assert.match(html, /name="consumerId"/);
  assert.match(html, /option value="con_1"/);
  assert.doesNotMatch(html, /option value="con_2"/);
  const developer = withPermissionSnapshot({ user: { role: "developer" } }, [
    PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE,
  ]);
  const technicalHtml = renderVirtualKeyPanel(state, developer);
  assert.doesNotMatch(technicalHtml, /consumer-form|consumerId|Disabled Employee|Finance/);
  const failed = renderConsumers({ state: "error", message: "Directory unavailable" });
  assert.match(failed, /role="alert"/);
  assert.doesNotMatch(failed, /暂无使用者/);
});

test("consumer APIs send profile and revision only, keys send authoritative consumer id", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return { ok: true, headers: new Map(), json: async () => directory };
  });
  const options = { controlApiUrl: "https://control.example.test", token: "unit-test-token" };
  await listConsumers({ ...options, offset: 50 });
  await createConsumer({ ...options, displayName: "Employee", department: "Finance", email: "ignored@example.test" });
  await updateConsumer({ ...options, consumerId: "con_1", displayName: "Employee", department: "Finance", status: "disabled", revision: 4 });
  await createVirtualKey({ ...options, name: "Tool", consumerId: "con_1" });
  await rotateVirtualKey({ ...options, virtualKeyId: "vk_1", expectedRevision: 3 });
  assert.match(calls[0].url, /limit=50&offset=50$/);
  assert.deepEqual(JSON.parse(calls[1].options.body), { display_name: "Employee", department: "Finance" });
  assert.equal(JSON.parse(calls[2].options.body).revision, 4);
  assert.equal(JSON.parse(calls[3].options.body).consumer_id, "con_1");
  assert.deepEqual(JSON.parse(calls[4].options.body), { expected_revision: 3 });
});
