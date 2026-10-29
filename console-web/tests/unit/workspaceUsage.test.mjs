import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { getUsageSummary, listUsageRecords, requestUsageCsvExport,
  getUsageExportStatus, downloadUsageExport, pollUsageExportUntilReady } from "../../src/lib/usage.js";
import { renderUsagePanel } from "../../src/features/usage/renderUsagePanel.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const base = { controlApiUrl: "https://control.example.test", authentication: "workspace" };
const jobId = "uex_" + "a".repeat(32);

function documentCookie(t) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    cookie: "__Host-metahub-workspace-csrf=" + "c".repeat(43),
  } });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, "document", previous);
    else delete globalThis.document;
  });
}

test("usage reads and export use only Workspace cookies with explicit write CSRF", async (t) => {
  documentCookie(t);
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: new URL(url), options });
    return new Response(String(url).endsWith("download") || String(url).includes("export.csv")
      ? "id\r\n1\r\n" : JSON.stringify({ export_job: { usage_export_job_id: jobId, status: "completed" } }),
    { status: 200, headers: { "Content-Type": String(url).includes("export.csv") || String(url).endsWith("download") ? "text/csv" : "application/json" } });
  });
  const controller = new AbortController();
  await getUsageSummary({ ...base, consumerId: "con_1", signal: controller.signal });
  await listUsageRecords({ ...base, consumerId: "con_1", signal: controller.signal });
  await requestUsageCsvExport({ ...base, consumerId: "con_1" });
  await getUsageExportStatus({ ...base, usageExportJobId: jobId });
  await downloadUsageExport({ ...base, usageExportJobId: jobId });
  for (const { url, options } of calls) {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, undefined);
    assert.equal(url.searchParams.has("token"), false);
  }
  for (const call of calls.slice(0, 3)) assert.equal(call.url.searchParams.get("consumer_id"), "con_1");
  assert.equal(calls[0].options.signal, controller.signal);
  assert.equal(calls[1].options.signal, controller.signal);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], "c".repeat(43));
  assert.equal(calls[3].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[4].options.headers["X-CSRF-Token"], "c".repeat(43));
  assert.equal(calls[2].options.referrerPolicy, "origin");
  assert.equal(calls[4].options.referrerPolicy, "origin");
  for (const index of [0, 1, 3]) assert.equal(calls[index].options.referrerPolicy, undefined);
});

test("export polling carries Workspace transport through the final download", async (t) => {
  documentCookie(t);
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push(options);
    return new Response(String(url).endsWith("download") ? "id\r\n" : JSON.stringify({
      export_job: { usage_export_job_id: jobId, status: "completed" },
    }), { status: 200, headers: { "Content-Type": String(url).endsWith("download") ? "text/csv" : "application/json" } });
  });
  const result = await pollUsageExportUntilReady({ ...base, usageExportJobId: jobId });
  assert.equal(result.kind, "download");
  assert.equal(calls.length, 2);
  assert.ok(calls.every((call) => call.credentials === "include" && !call.headers.Authorization));
  await assert.rejects(getUsageSummary({ ...base, token: "mixed-token" }),
    (error) => error.code === "workspace_session.credential_conflict");
});

test("usage shows immutable consumer attribution and protects financial DOM", () => {
  const context = withPermissionSnapshot({ user: { role: "admin" } }, [PERMISSIONS.BILLING_USAGE_READ]);
  const state = { state: "ok", filters: { consumerId: 'con_"<img>' }, summary: {
    by_consumer: [{ consumer_id: "con_<img>", total_tokens: 30, request_count: 2, cost_cny: 0.25, cost_usd: 0.25 },
      { consumer_id: null, total_tokens: 1, request_count: 1 }],
  } };
  const html = renderUsagePanel(state, context);
  assert.match(html, /按使用者用量/);
  assert.match(html, /未绑定使用者/);
  assert.match(html, /value="con_&quot;&lt;img&gt;"/);
  assert.doesNotMatch(html, /<img>/);
  assert.equal(renderUsagePanel(state, withPermissionSnapshot({ user: { role: "viewer" } }, [])), "");
});

test("usage main binds reads, filters, pagination and export to resource lifecycle", () => {
  const source = readFileSync(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const loader = source.slice(source.indexOf("async function loadUsageState()"), source.indexOf("async function loadFinOpsState("));
  assert.match(loader, /loadResourceState\("usage"/);
  assert.doesNotMatch(loader, /authSession\.token|clearAuthSession/);
  const events = source.slice(source.indexOf('const usageFilterForm ='), source.indexOf('bindFinOpsInteractions(consoleShellState);'));
  assert.doesNotMatch(events, /authSession\.token/);
  for (const name of ["usageFilterBinding", "usageResetBinding", "usagePrevBinding", "usageNextBinding"]) {
    assert.ok(events.includes(`isBindingCurrent(${name})`));
  }
  assert.match(events, /beginMutation\(usageExportBinding\)/);
  assert.match(events, /controller = operation\.controller/);
  assert.match(events, /finishMutation\(operation\)/);
});
