/** PRD-084 PAGE-116 exact client, authorization owner, lifecycle, and privacy tests. */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { localEndpointCreateFromFormData } from "../../src/features/local-models/bindLocalEndpoints.js";
import {
  createLocalEndpointPageState,
  localEndpointPageOwner,
} from "../../src/features/local-models/localEndpointPageState.js";
import { renderLocalEndpoints } from "../../src/features/local-models/renderLocalEndpoints.js";
import { localizeConsoleStaticHtml } from "../../src/i18n/localizeConsoleStaticHtml.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createLocalEndpoint,
  listLocalEndpoints,
  normalizeLocalEndpointCreateRequest,
  normalizeLocalEndpointList,
  testLocalEndpoint,
} from "../../src/lib/localEndpoints.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const TENANT_ID = "tenant_local_endpoint_a";
const ENDPOINT_ID = `lme_${"a".repeat(32)}`;
const READ = [PERMISSIONS.LOCAL_ENDPOINT_READ];
const ALL = [
  PERMISSIONS.LOCAL_ENDPOINT_READ,
  PERMISSIONS.LOCAL_ENDPOINT_MANAGE,
  PERMISSIONS.LOCAL_ENDPOINT_TEST,
];

function context(permissionCodes = ALL, fingerprint = "local_endpoint_authz_a") {
  return withPermissionSnapshot({
    tenant: { tenant_id: TENANT_ID, name: "Local tenant" },
    user: {
      tenant_id: TENANT_ID,
      user_id: "usr_local_endpoint_a",
      role: "developer",
      display_name: "Local developer",
      email: "local@example.test",
    },
    session: { session_id: "ses_local_endpoint_a", session_revision: 1 },
  }, permissionCodes, { permissionRevision: 19, authorizationFingerprint: fingerprint });
}

function shell(auth = context()) {
  return {
    state: "ok",
    activeNavigationItemId: "local-models",
    navigation: {
      active_navigation_item_id: "local-models",
      permission_codes: auth.permission_codes,
      permission_revision: auth.permission_revision,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: [{
        id: "local-models",
        target_section: "local-models",
        label: "本地模型",
        group: "扩展层",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.LOCAL_ENDPOINT_READ],
      }],
    },
  };
}

function initialModel(overrides = {}) {
  return {
    model_id: "mdl_local_chat_a",
    internal_model_id: "local-chat",
    provider_model_id: "served-model",
    display_name: "Local chat",
    capabilities: { chat: true, stream: true },
    context_window: 32768,
    input_price_per_1k: 0,
    output_price_per_1k: 0,
    cache_price_per_1k: 0,
    currency: "CNY",
    status: "active",
    ...overrides,
  };
}

function endpoint(overrides = {}) {
  return {
    local_endpoint_id: ENDPOINT_ID,
    tenant_id: TENANT_ID,
    provider_id: "prv_local_endpoint_a",
    provider_code: "local_inference",
    name: "Local inference",
    engine_kind: "vllm",
    endpoint_hint: "https://[private]/v1",
    auth_type: "bearer",
    status: "active",
    health_status: "healthy",
    last_tested_at: "2026-08-16T08:00:00Z",
    revision: 1,
    catalog_revision: 2,
    catalog_status: "ready",
    last_discovered_at: "2026-08-16T08:00:00Z",
    discovered_model_count: 3,
    credential_configured: true,
    initial_model: initialModel(),
    ...overrides,
  };
}

function listPayload(overrides = {}) {
  return {
    local_endpoints: [endpoint()],
    request_id: "req_local_list_a",
    trace_id: "trc_local_list_a",
    ...overrides,
  };
}

function createRequest(overrides = {}) {
  return {
    provider_code: "local_inference",
    name: "Local inference",
    engine_kind: "vllm",
    base_url: "https://inference.internal.example/v1",
    auth_type: "bearer",
    bearer_token: "one-use-token-canary",
    initial_model: {
      internal_model_id: "local-chat",
      provider_model_id: "served-model",
      display_name: "Local chat",
      capabilities: { chat: true, stream: true },
      context_window: 32768,
      input_price_per_1k: 0,
      output_price_per_1k: 0,
      cache_price_per_1k: 0,
      currency: "CNY",
    },
    ...overrides,
  };
}

function testPayload(overrides = {}) {
  return {
    local_endpoint: endpoint({ catalog_revision: 3, discovered_model_count: 4 }),
    test_result: {
      status: "success",
      http_status: 200,
      latency_ms: 42,
      error_code: null,
      discovered_model_count: 4,
      catalog_truncated: false,
      checked_at: "2026-08-16T08:01:00Z",
    },
    request_id: "req_local_test_a",
    trace_id: "trc_local_test_a",
    ...overrides,
  };
}

function jsonResponse(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
      ...headers,
    },
  });
}

test("PAGE-116 owner requires exact read navigation and projects manage/test independently", () => {
  const all = context();
  const owner = localEndpointPageOwner(all, shell(all));
  assert.ok(owner);
  assert.equal(owner.canManage, true);
  assert.equal(owner.canTest, true);

  const reader = context(READ, "local_endpoint_reader");
  assert.equal(localEndpointPageOwner(reader, shell(reader)).canManage, false);
  assert.equal(localEndpointPageOwner(reader, shell(reader)).canTest, false);

  const manageOnly = context([PERMISSIONS.LOCAL_ENDPOINT_MANAGE], "local_endpoint_manage_only");
  assert.equal(localEndpointPageOwner(manageOnly, shell(manageOnly)), null);
  assert.equal(localEndpointPageOwner(all, {
    ...shell(all),
    navigation: { ...shell(all).navigation, permission_revision: 18 },
  }), null);
});

test("GET client enforces exact tenant DTO, safe hint, no-store, no cookies, and no query", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(listPayload());
  };
  const result = await listLocalEndpoints({
    controlApiUrl: "http://control.local",
    token: "fixture-session-token",
    tenantId: TENANT_ID,
  });
  assert.equal(result.local_endpoints[0].endpoint_hint, "https://[private]/v1");
  assert.equal(calls[0].url, "http://control.local/local-endpoints");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.redirect, "error");
  assert.equal(calls[0].options.headers.Authorization, "Bearer fixture-session-token");

  assert.equal(normalizeLocalEndpointList({
    ...listPayload(),
    local_endpoints: [endpoint({ tenant_id: "tenant_other" })],
  }, TENANT_ID), null);
  assert.equal(normalizeLocalEndpointList({ ...listPayload(), endpoint_url: "https://secret.example/v1" }, TENANT_ID), null);
  assert.equal(normalizeLocalEndpointList({
    ...listPayload(),
    local_endpoints: [endpoint({ endpoint_hint: "https://host.internal.example/v1" })],
  }, TENANT_ID), null);
});

test("client rejects header drift and success DTOs containing URL, token, or unknown fields", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async () => jsonResponse(listPayload(), 200, { "X-Request-ID": "req_drift" });
  await assert.rejects(
    listLocalEndpoints({ controlApiUrl: "http://control.local", token: "fixture-token", tenantId: TENANT_ID }),
    (error) => error instanceof AuthApiError && error.code === "local_endpoint.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse({
    ...listPayload(),
    local_endpoints: [{ ...endpoint(), bearer_token: "response-token-canary" }],
  });
  await assert.rejects(
    listLocalEndpoints({ controlApiUrl: "http://control.local", token: "fixture-token", tenantId: TENANT_ID }),
    (error) => error.code === "local_endpoint.response_invalid",
  );
});

test("create and test clients emit exact request bodies and validate exact response envelopes", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return String(url).endsWith("/test")
      ? jsonResponse(testPayload())
      : jsonResponse({
        local_endpoint: endpoint(),
        created: true,
        request_id: "req_local_create_a",
        trace_id: "trc_local_create_a",
      }, 201);
  };
  const request = createRequest();
  const created = await createLocalEndpoint({
    controlApiUrl: "http://control.local",
    token: "fixture-token",
    tenantId: TENANT_ID,
    request,
    requestId: "req_console_create_a",
  });
  assert.equal(created.created, true);
  assert.deepEqual(JSON.parse(calls[0].options.body), request);
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_console_create_a");
  assert.deepEqual(Object.keys(JSON.parse(calls[0].options.body)).sort(), [
    "auth_type", "base_url", "bearer_token", "engine_kind", "initial_model", "name", "provider_code",
  ]);

  await testLocalEndpoint({
    controlApiUrl: "http://control.local",
    token: "fixture-token",
    tenantId: TENANT_ID,
    localEndpointId: ENDPOINT_ID,
  });
  assert.equal(calls[1].url, `http://control.local/local-endpoints/${ENDPOINT_ID}/test`);
  assert.equal(calls[1].options.body, "{}");

  const none = createRequest({ auth_type: "none" });
  delete none.bearer_token;
  assert.deepEqual(Object.keys(normalizeLocalEndpointCreateRequest(none)).sort(), [
    "auth_type", "base_url", "engine_kind", "initial_model", "name", "provider_code",
  ]);
  assert.throws(() => normalizeLocalEndpointCreateRequest({ ...request, tenant_id: TENANT_ID }));
});

test("create client rejects disabled capability entries", () => {
  const request = createRequest();
  request.initial_model.capabilities = { chat: true, stream: false };

  assert.throws(
    () => normalizeLocalEndpointCreateRequest(request),
    (error) => error instanceof AuthApiError
      && error.code === "local_endpoint.request_invalid"
      && error.status === 422,
  );
});

test("form parsing removes bearer_token for none and reads only the closed initial model intent", () => {
  const form = new FormData();
  form.set("provider_code", "local_inference");
  form.set("name", "Local inference");
  form.set("engine_kind", "ollama");
  form.set("base_url", "http://ollama.internal.example:11434/v1");
  form.set("auth_type", "none");
  form.set("bearer_token", "must-not-survive-none");
  form.set("internal_model_id", "local-chat");
  form.set("provider_model_id", "served-model");
  form.set("display_name", "Local chat");
  form.append("capabilities", "chat");
  form.append("capabilities", "stream");
  form.set("context_window", "32768");
  form.set("input_price_per_1k", "0");
  form.set("output_price_per_1k", "0.250000");
  form.set("cache_price_per_1k", "0.125000");
  form.set("currency", "CNY");
  form.set("resolved_ip", "192.0.2.1");
  const request = localEndpointCreateFromFormData(form);
  assert.equal(Object.hasOwn(request, "bearer_token"), false);
  assert.equal(Object.hasOwn(request, "resolved_ip"), false);
  assert.deepEqual(request.initial_model.capabilities, { chat: true, stream: true });
});

test("state keeps owner/generation/controller private, merges create/test, and never snapshots input secrets", () => {
  const auth = context();
  const owner = localEndpointPageOwner(auth, shell(auth));
  const cleared = [];
  const state = createLocalEndpointPageState({
    clearDom: () => cleared.push("cleared"),
    createRequestId: () => "req_console_create_state",
  });
  assert.equal(state.enter(owner), true);
  const load = state.beginLoad(owner, 1);
  assert.ok(load.controller instanceof AbortController);
  assert.equal(state.acceptLoad(load, listPayload(), owner, 1), true);
  state.finishLoad(load);
  assert.equal(state.snapshot().items.length, 1);

  assert.equal(state.openCreate(owner), true);
  const create = state.beginCreate(owner);
  assert.equal(create.requestId, "req_console_create_state");
  assert.equal(state.acceptCreate(create, {
    local_endpoint: endpoint({ name: "Created endpoint" }),
    created: true,
    request_id: "req_created_state",
    trace_id: "trc_created_state",
  }, owner), true);
  state.finishCreate(create);
  assert.equal(state.snapshot().dialog.open, false);

  const probe = state.beginTest(owner, ENDPOINT_ID);
  assert.ok(probe.controller instanceof AbortController);
  assert.equal(state.acceptTest(probe, testPayload(), owner), true);
  state.finishTest(probe);
  const serialized = JSON.stringify(state.snapshot());
  assert.match(serialized, /https:\/\/\[private\]\/v1/);
  assert.doesNotMatch(serialized, /inference\.internal|one-use-token-canary|req_console_create_state/);
  assert.equal(state.snapshot().items[0].catalog_revision, 3);

  const pending = state.beginLoad(owner, 2);
  const reduced = context(READ, "local_endpoint_reduced");
  state.enter(localEndpointPageOwner(reduced, shell(reduced)));
  assert.equal(pending.controller.signal.aborted, true);
  assert.equal(state.snapshot().items.length, 0);
  assert.ok(cleared.length >= 1);
});

test("renderer shows safe hint and capability commands only, escaping backend display text", () => {
  const auth = context();
  const owner = localEndpointPageOwner(auth, shell(auth));
  const state = createLocalEndpointPageState({ createRequestId: () => "req_render" });
  state.enter(owner);
  const operation = state.beginLoad(owner, 1);
  state.acceptLoad(operation, listPayload({
    local_endpoints: [endpoint({ name: '<img src=x onerror="alert(1)">' })],
  }), owner, 1);
  state.finishLoad(operation);
  const html = renderLocalEndpoints(state.snapshot(), auth, shell(auth));
  assert.match(html, /data-local-endpoint-root/);
  assert.match(html, /https:\/\/\[private\]\/v1/);
  assert.match(html, /data-local-endpoint-open-create/);
  assert.match(html, /data-local-endpoint-test/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /bearer_token|resolved_ip|KeyPool/);

  state.openCreate(owner);
  const dialogHtml = renderLocalEndpoints(state.snapshot(), auth, shell(auth));
  assert.doesNotMatch(dialogHtml, /模型别名|上游模型名称|内部模型 ID|上下文/u);
  assert.match(dialogHtml, /模型名称/);
  assert.match(dialogHtml, /显示名称/);
  const englishDialog = localizeConsoleStaticHtml(dialogHtml, { locale: "en-US" });
  assert.doesNotMatch(englishDialog, /Model alias|Upstream model name|Internal model ID|Context window/u);
  assert.match(englishDialog, /Model name/);

  const reader = context(READ, "local_endpoint_render_reader");
  const readHtml = renderLocalEndpoints(state.snapshot(), reader, shell(reader));
  assert.doesNotMatch(readHtml, /data-local-endpoint-open-create|data-local-endpoint-test/);
});

test("main, navigation, privacy, and responsive source contracts are wired without Storage", async () => {
  const [main, client, state, binding, renderer, css, messages] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/localEndpoints.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/local-models/localEndpointPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/local-models/bindLocalEndpoints.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/local-models/renderLocalEndpoints.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/i18n/messages.js", import.meta.url), "utf8"),
  ]);
  assert.match(main, /loadLocalEndpointsState\(consoleShellState, refreshId\)/);
  assert.match(main, /case "local-models":[\s\S]*load\("localEndpointsState", \(\) => loadLocalEndpointsState\(consoleShellState, refreshId\)\)/);
  const activeLoader = main.slice(main.indexOf('case "local-models":'), main.indexOf('case "private-gateways":'));
  assert.doesNotMatch(activeLoader, /loadProvidersState/);
  assert.match(main, /handleLocalEndpointAuthenticationFailure/);
  assert.match(main, /localEndpointPageState\.clear\(\)/);
  assert.match(main, /isLoadCurrent\(operation, currentOwner, refreshId\)/);
  assert.match(messages, /nav\.local-models/);
  assert.match(css, /\.local-endpoint-table-scroll[\s\S]+overflow-x: auto/);
  assert.match(css, /@media \(max-width: 390px\)/);
  assert.doesNotMatch(`${client}\n${state}\n${binding}\n${renderer}`, /localStorage|sessionStorage/);
  const snapshotSource = state.slice(state.indexOf("function snapshot()"), state.indexOf("function owns(owner)"));
  assert.doesNotMatch(snapshotSource, /base_url|bearer_token|controller|requestId|ownerKey/);
});
