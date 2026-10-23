import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";

import { initializeSystemUpdates } from "../../src/features/platform/systemUpdates.js";

function mount(t, fetcher) {
  const originalObserver = globalThis.MutationObserver;
  const button = {};
  const attributes = new Map();
  const panel = {
    dataset: {}, isConnected: true, innerHTML: "加载系统升级状态…",
    setAttribute: (name, value) => attributes.set(name, value),
    querySelector: (selector) => selector === "[data-update-refresh]" ? button : null,
  };
  let observed;
  globalThis.MutationObserver = class {
    constructor(callback) { observed = callback; }
    observe() {}
  };
  t.after(() => { globalThis.MutationObserver = originalObserver; });
  t.mock.method(globalThis, "fetch", fetcher);
  initializeSystemUpdates({ querySelector: () => panel }, "https://control.test");
  return { panel, button, attributes, notify: () => observed() };
}

function statusResponse() {
  return { ok: true, json: async () => ({ current_version: "2.0.0", enabled: false, releases: [], jobs: [] }) };
}

test("an existing upgrade panel times out and retries its GET without submitting an install", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const requests = [];
  const ui = mount(t, async (url, options) => {
    requests.push({ url, options });
    if (requests.length > 1) return statusResponse();
    return new Promise((resolve, reject) => options.signal.addEventListener("abort", () => {
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true }));
  });
  ui.notify();
  assert.equal(requests.length, 1);
  assert.equal(ui.attributes.get("aria-busy"), "true");
  t.mock.timers.tick(8000);
  await setImmediate();
  assert.match(ui.panel.innerHTML, /读取升级状态超时/u);
  assert.match(ui.panel.innerHTML, /role="alert"/u);
  assert.equal(ui.attributes.get("aria-busy"), "false");
  await ui.button.onclick();
  assert.match(ui.panel.innerHTML, /系统版本 2\.0\.0/u);
  assert.equal(ui.attributes.get("aria-busy"), "false");
  assert.equal(requests.length, 2);
  assert.ok(requests.every(({ url, options }) => url === "https://control.test/platform/system-updates"
    && !options.method && options.credentials === "include" && !options.body));
});

test("the status deadline also covers a stalled response body", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const ui = mount(t, async (url, options) => ({
    ok: true,
    json: () => new Promise((resolve, reject) => options.signal.addEventListener("abort", () => {
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true })),
  }));
  await setImmediate();
  t.mock.timers.tick(8000);
  await setImmediate();
  assert.match(ui.panel.innerHTML, /读取升级状态超时/u);
  assert.equal(ui.attributes.get("aria-busy"), "false");
});

test("a late failed read does not replace a detached page", async (t) => {
  let finish;
  const ui = mount(t, () => new Promise((resolve) => { finish = resolve; }));
  ui.panel.isConnected = false;
  finish({ ok: false });
  await setImmediate();
  assert.equal(ui.panel.innerHTML, "加载系统升级状态…");
});

test("a failed service read offers retry without exposing its response payload", async (t) => {
  const ui = mount(t, async () => ({ ok: false, json: async () => ({ secret: "internal-path" }) }));
  await setImmediate();
  assert.match(ui.panel.innerHTML, /重试读取状态/u);
  assert.doesNotMatch(ui.panel.innerHTML, /internal-path/u);
  assert.equal(ui.attributes.get("aria-busy"), "false");
});
