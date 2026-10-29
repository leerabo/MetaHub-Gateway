/** PRD-085 Browser 驱动契约测试；固定网络、证据、owner 清理与总体未完成边界。 */

import assert from "node:assert/strict";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BrowserAcceptanceFailure,
  HARNESS_ORIGIN,
  SCREENSHOTS,
  VIEWPORTS,
  abortHarness,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyRequest,
  createDriverDirectory,
  createIsolatedBrowserRuntime,
  createTracker,
  parseArguments,
  recordResponseStatus,
  removeDriverDirectory,
  removeIsolatedBrowserRuntime,
  requestContractsExact,
  safeRunId,
  validateCompletionFacts,
} from "../../../scripts/acceptance/prd085-browser-acceptance.mjs";

// 源文件路径只用于静态审计正式驱动与 Harness，测试不会启动浏览器或生成验收证据。
const DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const DRIVER = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd085-browser-acceptance.mjs");
const HARNESS = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd085-browser-harness.py");
const SHARED_DRIVER = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd066-browser-acceptance.mjs");


/** 构造无溢出、无重叠且内容受视口约束的布局事实。 */
function layout(width, height) {
  return {
    width,
    height,
    horizontalOverflow: false,
    textOverflow: false,
    overlapCount: 0,
    contentBounded: true,
  };
}


/** 构造不含敏感 DOM 且像素非空的截图结果替身。 */
function screenshot(width, height) {
  return { layout: layout(width, height), nonblank: true, noSensitiveDom: true };
}


/** 构造授权、拒绝、撤权和第二租户隔离均成立的完整验收输入。 */
function completeInputs() {
  return {
    authorized: {
      navigationVisible: true,
      pageVisible: true,
      desktop: screenshot(1280, 720),
      mobile: screenshot(390, 844),
      snapshot: { steps: 7, completed: 2, count: true, rate: true },
      root: true,
      navigation: true,
      dom: true,
      lateDiscarded: true,
      permissionDeniedCleared: true,
    },
    authentication: {
      sessionExpiredCleared: true,
      lateAuthFailureDiscarded: true,
    },
    unauthorized: { navigation: true, page: true, apiPrefetchAbsent: true },
    secondary: {
      navigationVisible: true,
      independent: true,
      previous: true,
      desktop: screenshot(1280, 720),
      mobile: screenshot(390, 844),
      snapshot: { steps: 7, completed: 1, count: true, rate: true, initial: true },
      storageCleared: true,
    },
    registration: { status: 200 },
    tracker: {
      externalRequests: 0,
      forbiddenQueries: 0,
      consoleErrors: 0,
      pageErrors: 0,
      loginStatuses: [200, 200, 200, 200, 200, 200],
      logoutStatuses: [200, 200, 401, 200, 200],
      funnelGetCount: 9,
      funnelStatuses: [200, 200, 200, 403, 200, 401, 200, 401, 200],
      noStoreResponses: true,
      noStoreObserved: 9,
      requestContractExact: true,
    },
  };
}


test("PAGE-117 freezes run IDs, dual viewports, and the artifact whitelist", () => {
  assert.deepEqual(parseArguments(["--run-id", "browser-contract"]), { runId: "browser-contract" });
  assert.equal(safeRunId("final-20260817"), "final-20260817");
  assert.throws(() => safeRunId("../escape"));
  assert.deepEqual(VIEWPORTS.desktop, { name: "desktop-1280x720", width: 1280, height: 720 });
  assert.deepEqual(VIEWPORTS.mobile, { name: "mobile-390x844", width: 390, height: 844 });
  assert.deepEqual(SCREENSHOTS, [
    "page-117-growth-analytics-desktop-1280x720.png",
    "page-117-growth-analytics-mobile-390x844.png",
    "page-117-initial-state-desktop-1280x720.png",
    "page-117-tenant-isolation-mobile-390x844.png",
  ]);
});


test("network guard admits only loopback and freezes the query-free funnel route", () => {
  assert.equal(browserRequestAllowed("http://localhost:5173/#growth-analytics").allowed, true);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/product-analytics/funnel").allowed, true);
  assert.equal(browserRequestAllowed("http://localhost:8000/health").allowed, true);
  assert.equal(browserRequestAllowed(`${HARNESS_ORIGIN}/health`).allowed, true);
  assert.equal(browserRequestAllowed("https://example.com/").external, true);
  assert.equal(
    browserRequestAllowed("http://localhost:8001/product-analytics/funnel?window=all").forbiddenQuery,
    true,
  );
  assert.equal(
    classifyRequest("http://localhost:8001/product-analytics/funnel", "GET"),
    "product_analytics_funnel",
  );
  assert.notEqual(
    classifyRequest("http://localhost:8001/product-analytics/funnel", "POST"),
    "product_analytics_funnel",
  );
});


test("paused funnel responses are recorded once across Fetch and Network events", () => {
  const tracker = createTracker({
    adminPassword: "private-admin",
    rolePassword: "private-role",
    stopToken: "private-stop",
    driverOwner: "private-owner",
  });
  tracker.requestMethods.set("network-1", "GET");
  tracker.requestCategories.set("network-1", "product_analytics_funnel");
  recordResponseStatus(tracker, {
    requestId: "network-1",
    method: "GET",
    url: "http://localhost:8001/product-analytics/funnel",
    status: 200,
    headers: [{ name: "Cache-Control", value: "no-store, private" }],
  });
  recordResponseStatus(tracker, {
    requestId: "network-1",
    url: "http://localhost:8001/product-analytics/funnel",
    status: 200,
    headers: { "Cache-Control": "no-store, private" },
  });
  assert.deepEqual(tracker.funnelStatuses, [200]);
  assert.equal(tracker.noStoreObserved, 1);
  assert.equal(tracker.noStoreResponses, true);
});


test("completion requires authorization, revocation, isolation, layout, and exact requests", () => {
  const inputs = completeInputs();
  assert.equal(requestContractsExact(inputs.tracker), true);
  const facts = buildCompletionFacts(inputs);
  assert.equal(validateCompletionFacts(facts), facts);
  assert.equal(facts.all_108_prds_complete, false);
  assert.equal(facts.desktop.seven_stages, true);
  assert.equal(facts.mobile.tenant_isolation_visible, true);
  assert.equal(facts.lifecycle.unauthorized_api_prefetch_absent, true);
  assert.equal(facts.lifecycle.dynamic_revocation_dom_cleared, true);
  assert.equal(facts.lifecycle.late_response_discarded, true);
  assert.equal(facts.lifecycle.permission_denied_dom_cleared, true);
  assert.equal(facts.lifecycle.session_expired_dom_cleared, true);
  assert.equal(facts.lifecycle.late_auth_failure_discarded, true);
  assert.equal(facts.lifecycle.previous_tenant_dom_cleared, true);
  assert.equal(facts.network.no_response_body_capture, true);
  assert.deepEqual(facts.screenshots, SCREENSHOTS);

  inputs.tracker.funnelGetCount = 4;
  assert.equal(requestContractsExact(inputs.tracker), false);
  assert.throws(() => validateCompletionFacts(buildCompletionFacts(inputs)));
});


test("driver owner marker guards creation rollback and remains until child cleanup succeeds", async () => {
  const created = [];
  const removed = [];
  const runId = `unit-owner-${process.pid}-${Date.now()}`;
  await assert.rejects(
    createDriverDirectory(runId, "a".repeat(64), {
      async mkdirOperation(target) { created.push(target); },
      async writeOperation() { throw new Error("injected"); },
      async readOperation() { return ""; },
      async removeOperation(target, options) { removed.push({ target, options }); },
    }),
    /injected/,
  );
  assert.equal(created.length, 6);
  assert.deepEqual(removed, [{ target: created[1], options: { recursive: true, force: false } }]);

  const cleanupCalls = [];
  const fakeDirectory = path.resolve(
    DIRECTORY,
    `../../../.tmp/prd085-browser-driver/unit-cleanup-${process.pid}-${Date.now()}`,
  );
  await assert.rejects(
    removeDriverDirectory(fakeDirectory, "b".repeat(64), {
      existsOperation: () => true,
      async lstatOperation() {
        return { isDirectory: () => true, isSymbolicLink: () => false };
      },
      async readOperation() { return "b".repeat(64); },
      async listOperation() { return ["profile", ".prd085-browser-driver-owner"]; },
      async removeOperation(target, options) {
        cleanupCalls.push({ kind: "child", target, options });
        // 模拟 Windows Profile 仍被占用，确认失败发生时不会提前删除 owner marker。
        throw new Error("locked");
      },
      async unlinkOperation(target) { cleanupCalls.push({ kind: "marker", target }); },
      async rmdirOperation(target) { cleanupCalls.push({ kind: "directory", target }); },
    }),
    /locked/,
  );
  assert.equal(cleanupCalls.length, 1);
  assert.equal(cleanupCalls[0].kind, "child");
  assert.equal(path.basename(cleanupCalls[0].target), "profile");
  assert.deepEqual(cleanupCalls[0].options, {
    recursive: true,
    force: false,
    maxRetries: 10,
    retryDelay: 200,
  });
});


test("harness abort reports cleanup failure without throwing over the primary coordinate", async () => {
  const running = { child: { exitCode: null }, closed: new Promise(() => {}) };
  const stopped = await abortHarness(running, {}, {
    // 协调请求与进程树终止同时失败时，只返回固定 false，不能抛出并覆盖主坐标。
    async postOperation() { throw new Error("injected"); },
    async terminateOperation() { throw new Error("injected"); },
    timeoutMs: 0,
  });
  assert.equal(stopped, false);

  const alreadyClosed = await abortHarness(
    { child: { exitCode: 0 }, closed: Promise.resolve({ code: 0 }) },
    {},
  );
  assert.equal(alreadyClosed, true);
});


test("isolated Chrome runtime hard-links required files and excludes the update handler", async () => {
  const suffix = `unit-runtime-${process.pid}-${Date.now()}`;
  const source = path.resolve(DIRECTORY, `../../../.tmp/${suffix}-source`);
  const owner = "c".repeat(64);
  let runtime = null;
  try {
    await mkdir(path.join(source, "version"), { recursive: true });
    await writeFile(path.join(source, "chrome.exe"), "chrome", "ascii");
    await writeFile(path.join(source, "debug.log"), "mutable", "ascii");
    await writeFile(path.join(source, "version", "chrome.dll"), "library", "ascii");
    await writeFile(path.join(source, "version", "os_update_handler.exe"), "updater", "ascii");
    runtime = await createIsolatedBrowserRuntime(suffix, owner, { sourceRoot: source });
    assert.equal(await readFile(path.join(runtime, "chrome.exe"), "ascii"), "chrome");
    assert.equal(await readFile(path.join(runtime, "version", "chrome.dll"), "ascii"), "library");
    await assert.rejects(readFile(path.join(runtime, "debug.log")), { code: "ENOENT" });
    await assert.rejects(
      readFile(path.join(runtime, "version", "os_update_handler.exe")),
      { code: "ENOENT" },
    );
    await removeIsolatedBrowserRuntime(runtime, owner);
    runtime = null;
  } finally {
    // 单测仅清理自己创建的精确目录，正式 owner 清理失败时仍保留 marker 供诊断。
    if (runtime) await removeIsolatedBrowserRuntime(runtime, owner);
    await rm(source, { recursive: true, force: true });
  }
});


test("sources prohibit escalation, body capture, private URLs, and stale PRD identifiers", async () => {
  const [driver, harness, sharedDriver] = await Promise.all([
    readFile(DRIVER, "utf8"),
    readFile(HARNESS, "utf8"),
    readFile(SHARED_DRIVER, "utf8"),
  ]);
  assert.match(driver, /all_108_prds_complete: false/);
  assert.match(harness, /EXPECTED_MIGRATION_COUNT = 78/);
  assert.match(harness, /EXPECTED_MIGRATION_VERSION = "0078"/);
  assert.match(driver, /unauthorized_api_prefetch_absent/);
  assert.match(driver, /dynamic_revocation_dom_cleared/);
  assert.match(driver, /previous_tenant_dom_cleared/);
  assert.match(driver, /secrets: privateValues/);
  assert.match(driver, /count:metrics\.some\(\(item\)=>\/\\\\d\//);
  assert.doesNotMatch(`${driver}\n${harness}`, /sandbox_permissions/);
  assert.match(driver, /EXCLUDED_BROWSER_RUNTIME_FILES = new Set\(\["debug\.log", "os_update_handler\.exe"\]\)/);
  assert.doesNotMatch(driver, /responseBody|Network\.getResponseBody/);
  assert.doesNotMatch(driver, /https:\/\/[^"']+\.invalid\/private|Bearer-[A-Za-z0-9_-]{16,}/);
  assert.doesNotMatch(`${driver}\n${harness}`, /PRD-084|prd084|page-116/);
  assert.match(sharedDriver, /--disable-features=[^"\r\n]*OsUpdateHandlerWin/);
});


test("failure coordinates remain fixed and do not include content", () => {
  const failure = new BrowserAcceptanceFailure("browser.contract", "invalid");
  assert.equal(failure.stage, "browser.contract");
  assert.equal(failure.code, "invalid");
  assert.equal(failure.message, "browser.contract:invalid");
});
