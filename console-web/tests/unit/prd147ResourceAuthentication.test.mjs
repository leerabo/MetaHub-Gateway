import assert from "node:assert/strict";
import { File } from "node:buffer";
import test from "node:test";
import { authenticatedResourceFetch } from "../../src/lib/resourceAuthentication.js";
import * as projects from "../../src/lib/projects.js";
import * as environments from "../../src/lib/environments.js";
import * as providers from "../../src/lib/providers.js";
import * as models from "../../src/lib/models.js";
import * as keys from "../../src/lib/virtualKeys.js";
import * as files from "../../src/lib/files.js";
import * as catalog from "../../src/lib/providerCatalog.js";
import * as health from "../../src/lib/providerHealth.js";
import * as status from "../../src/lib/providerStatus.js";
import * as aliases from "../../src/lib/modelAliases.js";
import * as scores from "../../src/lib/modelQualityScores.js";
import * as experiments from "../../src/lib/experiments.js";
import * as dlp from "../../src/lib/dlp.js";

const BASE = "https://control.example.test";
const CSRF = "w".repeat(43);
const FILE_ID = `file_${"a".repeat(32)}`;
const originalFetch = globalThis.fetch;
const originalDocument = globalThis.document;
test.afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalDocument === undefined) delete globalThis.document;
  else globalThis.document = originalDocument;
});

test("workspace transient reads retry once without changing authentication or signal", async () => {
  const calls = [];
  const controller = new AbortController();
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) throw new TypeError("Failed to fetch");
    return new Response("{}");
  };
  await authenticatedResourceFetch(BASE + "/models", { authentication: "workspace", signal: controller.signal });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].options, calls[1].options);
  assert.equal(calls[1].options.credentials, "include");
  assert.equal(calls[1].options.signal, controller.signal);
});

test("transient recovery is bounded and never replays writes or non-network errors", async () => {
  for (const [method, error, expected] of [["GET", new TypeError("Failed to fetch"), 2], ["POST", new TypeError("Failed to fetch"), 1], ["GET", new TypeError("programming error"), 1]]) {
    let calls = 0;
    globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${CSRF}` };
    globalThis.fetch = async () => { calls += 1; throw error; };
    await assert.rejects(authenticatedResourceFetch(BASE, { authentication: "workspace", method }), error);
    assert.equal(calls, expected);
  }
});

test("leaving a page cancels a pending read recovery", async () => {
  const controller = new AbortController();
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new TypeError("Failed to fetch"); };
  const pending = authenticatedResourceFetch(BASE, { authentication: "workspace", signal: controller.signal });
  await Promise.resolve();
  controller.abort();
  await assert.rejects(pending);
  assert.equal(calls, 1);
});

test("CSRF-protected Workspace GET sends origin-only referrer without changing other requests", async () => {
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${CSRF}` };
  globalThis.fetch = async (_url, options) => { calls.push(options); return new Response("{}"); };
  await authenticatedResourceFetch(BASE + "/usage/export.csv", {
    authentication: "workspace", csrfRequired: true, referrerPolicy: "unsafe-url",
  });
  await authenticatedResourceFetch(BASE + "/usage/summary", { authentication: "workspace" });
  await authenticatedResourceFetch(BASE + "/budgets", { authentication: "workspace", method: "POST" });
  await authenticatedResourceFetch(BASE + "/usage/export.csv", { authentication: "legacy", token: "test-token", csrfRequired: true });
  assert.equal(calls[0].referrerPolicy, "origin");
  assert.equal(calls[0].headers["X-CSRF-Token"], CSRF);
  assert.equal(calls[0].headers.Origin, undefined);
  assert.equal(calls[0].headers.Referer, undefined);
  for (const call of calls.slice(1)) assert.equal(call.referrerPolicy, undefined);
});

const operations = [
  [dlp.listDlpRules, { status: "all" }, "GET", "/dlp/rules"],
  [dlp.createDlpRule, { name: "Protection", ruleType: "keyword", pattern: "example", action: "log_only" }, "POST", "/dlp/rules"],
  [dlp.updateDlpRule, { ruleId: "dlp_sample", status: "disabled" }, "PATCH", "/dlp/rules/dlp_sample"],
  [aliases.listModelAliases, {}, "GET", "/model-aliases"],
  [aliases.createModelAlias, { alias: "sample", defaultModelId: "model_sample" }, "POST", "/model-aliases"],
  [aliases.updateModelAlias, { modelAliasId: "alias_sample", status: "disabled", expectedRoutingPolicyVersion: 1 }, "PATCH", "/model-aliases/alias_sample"],
  [aliases.updateFallbackPolicy, { modelAliasId: "alias_sample", enabled: false, triggerConditions: ["upstream_timeout"], expectedRoutingPolicyVersion: 1 }, "PATCH", "/model-aliases/alias_sample/fallback"],
  [aliases.updateRoutingMode, { modelAliasId: "alias_sample", routingMode: "fixed", expectedVersion: 1 }, "PATCH", "/model-aliases/alias_sample/routing-mode"],
  [aliases.updateCacheAffinity, { modelAliasId: "alias_sample", status: "disabled", dataClass: null, minimumPrefixChars: 256, ttlSeconds: 60, expectedVersion: 0 }, "PATCH", "/model-aliases/alias_sample/cache-affinity"],
  [scores.listModelQualityScores, {}, "GET", "/model-quality-scores"],
  [scores.updateModelQualityScore, { modelId: "model_sample", manualQualityBps: 8000, expectedFeedbackVersion: 1 }, "PATCH", "/model-quality-scores"],
  [experiments.listExperiments, {}, "GET", "/experiments"],
  [health.listProviderHealth, {}, "GET", "/provider-health"],
  [health.probeProvider, { providerId: "provider_sample" }, "POST", "/providers/provider_sample/probe"],
  [status.listProviderStatuses, {}, "GET", "/status/providers"],
  [projects.listProjects, {}, "GET", "/projects"],
  [projects.createProject, { projectCode: "sample", name: "Sample" }, "POST", "/projects"],
  [projects.updateProject, { projectId: "prj_sample", name: "Renamed" }, "PATCH", "/projects/prj_sample"],
  [projects.listApplications, {}, "GET", "/applications"],
  [projects.createApplication, { projectId: "prj_sample", applicationCode: "sample", name: "Sample" }, "POST", "/applications"],
  [projects.updateApplication, { applicationId: "app_sample", status: "disabled" }, "PATCH", "/applications/app_sample"],
  [environments.listEnvironments, {}, "GET", "/environments"],
  [environments.createEnvironment, { code: "test", name: "Test" }, "POST", "/environments"],
  [environments.updateEnvironment, { environmentId: "env_sample", name: "Renamed" }, "PATCH", "/environments/env_sample"],
  [providers.listProviders, {}, "GET", "/providers"],
  [providers.listProviderKeys, { providerId: "provider_sample" }, "GET", "/providers/provider_sample/keys"],
  [providers.createProvider, { providerCode: "sample", name: "Sample" }, "POST", "/providers"],
  [providers.createProviderKey, { providerId: "provider_sample", name: "Sample" }, "POST", "/providers/provider_sample/keys"],
  [providers.updateProvider, { providerId: "provider_sample", status: "disabled" }, "PATCH", "/providers/provider_sample"],
  [providers.updateProviderKey, { providerKeyId: "pkey_sample", status: "disabled" }, "PATCH", "/provider-keys/pkey_sample"],
  [providers.testProviderConnection, { providerId: "provider_sample" }, "POST", "/providers/provider_sample/test"],
  [models.listModels, {}, "GET", "/models"],
  [models.createModel, { providerId: "provider_sample", internalModelId: "sample" }, "POST", "/models"],
  [models.updateModel, { modelId: "mdl_sample", displayName: "Renamed" }, "PATCH", "/models/mdl_sample"],
  [keys.listVirtualKeys, {}, "GET", "/virtual-keys"],
  [keys.createVirtualKey, { name: "Sample" }, "POST", "/virtual-keys"],
  [keys.updateVirtualKey, { virtualKeyId: "vkey_sample", name: "Renamed" }, "PATCH", "/virtual-keys/vkey_sample"],
  [keys.updateVirtualKeyLimits, { virtualKeyId: "vkey_sample", rpmLimit: 10 }, "PATCH", "/virtual-keys/vkey_sample/limits"],
  [keys.disableVirtualKey, { virtualKeyId: "vkey_sample" }, "DELETE", "/virtual-keys/vkey_sample"],
  [files.listFiles, {}, "GET", "/files"],
  [files.getFile, { fileId: FILE_ID }, "GET", `/files/${FILE_ID}`],
  [files.uploadFile, { file: new File(["test"], "sample.txt", { type: "text/plain" }) }, "POST", "/files"],
  [catalog.listProviderCatalog, {}, "GET", "/provider-catalog"],
  [catalog.getProviderCatalogCompatibility, { entryCode: "sample" }, "GET", "/provider-catalog/sample/compatibility"],
  [catalog.createProviderFromCatalog, { entryCode: "sample", version: "2026.09.08.1", regionCode: "cn-east",
    capabilityCodes: ["chat"], providerCode: "sample", name: "Sample", confirmed: true,
    idempotencyKey: "console:catalog:instantiate:test" }, "POST", "/providers/from-catalog"],
  [catalog.preflightCatalogProvider, { providerId: "provider_sample", timeoutSeconds: 5,
    idempotencyKey: "console:catalog:preflight:test" }, "POST", "/providers/provider_sample/preflight"],
];

test("PRD-147 observation filters cannot transmit owner selectors or endpoint fields", async () => {
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(new URL(url));
    return { ok: true, headers: new Headers(), json: async () => ({ state: "error" }) };
  };
  for (const operation of [health.listProviderHealth, status.listProviderStatuses]) {
    const result = await operation({ controlApiUrl: BASE, authentication: "workspace",
      filters: { tenant_id: "foreign", endpoint: "https://foreign.test", provider_id: "owned", limit: 20 } });
    assert.equal(result.state, "ok");
  }
  for (const url of urls) assert.deepEqual([...url.searchParams.keys()].sort(), ["limit", "provider_id"]);
});

for (const [operation, input, method, path] of operations) {
  for (const authentication of ["workspace", "legacy"]) {
    test(`PRD-147 ${operation.name} uses explicit ${authentication} authentication and cancellation`, async () => {
      const calls = [];
      const controller = new AbortController();
      globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${CSRF}` };
      globalThis.fetch = async (url, options) => {
        calls.push({ url, options });
        return responseFor(path, method);
      };
      await operation({ controlApiUrl: BASE, authentication, signal: controller.signal,
        ...(authentication === "legacy" ? { token: "legacy-test-session" } : {}),
        tenantId: "ignored-selector", ...input });
      assert.equal(calls.length, 1);
      const call = calls[0];
      const headers = new Headers(call.options.headers);
      assert.equal(new URL(call.url).origin, BASE);
      assert.equal(new URL(call.url).pathname, path);
      assert.equal(call.options.method, method);
      assert.equal(call.options.credentials, authentication === "workspace" ? "include" : "omit");
      assert.equal(call.options.cache, "no-store");
      assert.equal(call.options.redirect, "error");
      if ([models.createModel, aliases.createModelAlias].includes(operation)) {
        assert.ok(call.options.signal instanceof AbortSignal);
        assert.equal(call.options.signal.aborted, false);
      } else assert.equal(call.options.signal, controller.signal);
      assert.equal(call.options.token, undefined);
      assert.equal(call.options.authentication, undefined);
      assert.equal(headers.has("cookie"), false);
      assert.equal(headers.has("authorization"), authentication === "legacy");
      assert.equal(headers.has("x-csrf-token"), authentication === "workspace" && method !== "GET");
      if (headers.has("x-csrf-token")) assert.equal(headers.get("x-csrf-token") === CSRF, true);
      if (operation === files.uploadFile) {
        assert.equal(headers.has("content-type"), false);
        assert.deepEqual([...call.options.body.keys()], ["file"]);
      } else if (call.options.body) {
        const body = JSON.parse(call.options.body);
        for (const forbidden of ["token", "authentication", "tenant_id", "tenantId"]) {
          assert.equal(Object.hasOwn(body, forbidden), false);
        }
      }
    });
  }
}

for (const operation of [models.createModel, aliases.createModelAlias]) {
  for (const authentication of ["workspace", "legacy"]) {
    test(`${operation.name} ${authentication} deadline transport follows owner cancellation during response decoding`, async () => {
      const owner = new AbortController();
      let transportSignal;
      let finishBody;
      globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${CSRF}` };
      globalThis.fetch = async (_url, options) => {
        transportSignal = options.signal;
        return { ok: true, headers: new Headers(), json: () => new Promise((resolve) => { finishBody = resolve; }) };
      };
      const result = operation({ controlApiUrl: BASE, authentication, signal: owner.signal,
        ...(authentication === "legacy" ? { token: "legacy-test-session" } : {}),
        providerId: "provider_sample", internalModelId: "sample", alias: "sample" });
      await new Promise((resolve) => setImmediate(resolve));
      assert.equal(typeof finishBody, "function");
      owner.abort();
      await assert.rejects(result, { name: "AbortError" });
      assert.equal(transportSignal.aborted, true);
      finishBody({ state: "ok" });
      await assert.rejects(result, { name: "AbortError" });
    });
  }
}

test("PRD-147 every resource operation rejects mixed authentication before network", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new Error("unexpected network"); };
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${CSRF}` };
  for (const [operation, input] of operations) {
    await assert.rejects(operation({ controlApiUrl: BASE, authentication: "workspace",
      token: "legacy-test-session", ...input }), (error) => error.code === "workspace_session.credential_conflict");
  }
  assert.equal(calls, 0);
});

test("PRD-147 missing tokens do not fall back to ambient cookies", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new Error("unexpected network"); };
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${CSRF}` };
  for (const token of [undefined, null, "", {}, "bad value", "bad,value", "bad\nvalue"]) {
    assert.throws(() => authenticatedResourceFetch(BASE, { token }),
      (error) => error.code === "auth.token_missing");
  }
  assert.equal(calls, 0);
});

test("PRD-147 workspace writes require one valid Workspace CSRF cookie", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new Error("unexpected network"); };
  for (const cookie of ["", `__Host-metahub-global-csrf=${CSRF}`,
    `__Host-metahub-workspace-csrf=${CSRF}; __Host-metahub-workspace-csrf=${CSRF}`,
    "__Host-metahub-workspace-csrf=%00"]) {
    globalThis.document = { cookie };
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      assert.throws(() => authenticatedResourceFetch(BASE, { authentication: "workspace", method }),
        (error) => error.status === 403 && error.code === "workspace_session.csrf_invalid");
    }
  }
  assert.equal(calls, 0);
});

test("PRD-147 unknown domains and caller-supplied credential headers cannot bypass transport selection", () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new Error("unexpected network"); };
  for (const authentication of ["platform", "identity", "support", "", {}, null]) {
    assert.throws(() => authenticatedResourceFetch(BASE, { authentication }),
      (error) => error.code === "auth.credential_invalid");
  }
  for (const name of ["Authorization", "authorization", "Cookie", "Proxy-Authorization", "X-CSRF-Token"]) {
    assert.throws(() => authenticatedResourceFetch(BASE, { authentication: "workspace", headers: { [name]: "test" } }),
      (error) => error.code === "auth.credential_invalid");
  }
  assert.equal(calls, 0);
});

test("PRD-147 file uploads preserve cancellation instead of mapping it to a storage failure", async () => {
  const controller = new AbortController();
  let started;
  const ready = new Promise((resolve) => { started = resolve; });
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${CSRF}` };
  globalThis.fetch = (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    started();
  });
  const request = files.uploadFile({ controlApiUrl: BASE, authentication: "workspace",
    signal: controller.signal, file: new File(["test"], "sample.txt", { type: "text/plain" }) });
  const assertion = assert.rejects(request, (error) => error.name === "AbortError");
  await ready;
  controller.abort();
  await assertion;
});

test("PRD-147 all resource operations preserve cancellation while reading response bodies", async () => {
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${CSRF}` };
  for (const [operation, input] of operations) {
    const controller = new AbortController();
    let started;
    const reading = new Promise((resolve) => { started = resolve; });
    globalThis.fetch = async (_url, { signal }) => ({
      ok: true, status: 200,
      json: () => new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        started();
      }),
    });
    const request = operation({ controlApiUrl: BASE, authentication: "workspace", signal: controller.signal, ...input });
    const assertion = assert.rejects(request, (error) => error.name === "AbortError");
    await reading;
    controller.abort();
    await assertion;
  }
});

test("PRD-147 transport options cannot override credential isolation, caching or redirects", async () => {
  const calls = [];
  globalThis.fetch = async (_url, options) => { calls.push(options); return Response.json({}); };
  globalThis.document = { get cookie() { throw new Error("Read requests must not access CSRF cookies"); } };
  await authenticatedResourceFetch(BASE, { authentication: "workspace", method: "head", credentials: "omit",
    cache: "force-cache", redirect: "follow" });
  await authenticatedResourceFetch(BASE, { authentication: "legacy", token: "legacy-test-session",
    credentials: "include", cache: "force-cache", redirect: "follow" });
  assert.deepEqual(calls.map(({ method, credentials, cache, redirect }) => ({ method, credentials, cache, redirect })), [
    { method: "HEAD", credentials: "include", cache: "no-store", redirect: "error" },
    { method: "GET", credentials: "omit", cache: "no-store", redirect: "error" },
  ]);
});

function responseFor(path, method) {
  const envelope = { request_id: "req_resource", trace_id: "trc_resource" };
  if (path === "/experiments") return Response.json({ experiments: [], next_cursor: null, ...envelope },
    { headers: { "Cache-Control": "no-store, private" } });
  if (path === "/files" && method === "GET") {
    return Response.json({ files: [], next_cursor: null, ...envelope });
  }
  if (path.startsWith("/files")) {
    return Response.json({ file: {
      file_id: FILE_ID, original_name: "sample.txt", file_extension: "txt", media_type: "text/plain",
      size_bytes: 4, status: "failed", virus_scan_status: "not_configured", dlp_scan_status: "unavailable",
      dlp_action: null, match_count: 0, failure_error_code: "file.parse_failed", upload_request_id: "req_upload",
      extract: null, created_at: "2026-09-07T00:00:00Z", updated_at: "2026-09-07T00:00:00Z",
      completed_at: "2026-09-07T00:00:00Z",
    }, ...envelope });
  }
  return Response.json(envelope);
}
