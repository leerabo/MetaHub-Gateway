/** PRD-080 Browser 验收器的路径、完成信号、网络与隐私合同。 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { PassThrough } from "node:stream";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import {
  ALLOWED_ORIGINS,
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  DEBUG_PORT,
  DRIVER_ROOT,
  HARNESS_CONSOLE_URL,
  HARNESS_ORIGIN,
  OPERABLE_DIALOG_CONTROL_SELECTOR,
  SCREENSHOTS,
  VIEWPORTS,
  browserProcessIdFromProcessInfo,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyRequest,
  closePrd080Browser,
  directChild,
  parseArguments,
  safeRunId,
  validateCompletionFacts,
} from "../../../scripts/acceptance/prd080-browser-acceptance.mjs";

const TEST_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(TEST_DIRECTORY, "..", "..", "..");

function layout(width, height) {
  return Object.freeze({
    width,
    height,
    root_visible: true,
    root_horizontal_bounds: true,
    capture_layer_viewport_intersection: true,
    horizontal_overflow: false,
    text_overflow: false,
    overlap_count: 0,
  });
}

function pixels() {
  return Object.freeze({ non_blank_pixels: true });
}

function completeFacts() {
  return buildCompletionFacts({
    listDesktop: { layout: layout(1280, 720), pixels: pixels() },
    approvedDesktop: { layout: layout(1280, 720), pixels: pixels() },
    listMobile: { layout: layout(390, 844), pixels: pixels() },
    rejectedMobile: { layout: layout(390, 844), pixels: pixels() },
    requestScopeCreated: true,
    timeScopeCreated: true,
    listRowsExact: true,
    selfReviewBlocked: true,
    requestDetailExact: true,
    approveTerminal: true,
    partialAdjustment: true,
    approvedTimelineExact: true,
    approvedPrivateCollapsed: true,
    htmlEscapeSafe: true,
    rejectTerminal: true,
    rejectedTimelineExact: true,
    rejectedPrivateCollapsed: true,
    nativeDialog: true,
    initialFocus: true,
    tabForwardWrap: true,
    tabReverseWrap: true,
    idleEscapeClosed: true,
    focusRestored: true,
    busyControlsDisabled: true,
    busyEscapeBlocked: true,
    reasonCleared: true,
    decisionCleared: true,
  }, {
    backendRbacDenied: true,
    logoutRootCleared: true,
    logoutStorageCleared: true,
    logoutLateResponseDiscarded: true,
    revocationRootCleared: true,
    revocationNavigationCleared: true,
    revocationLateResponseDiscarded: true,
    registeredPrivateValues: true,
  }, {
    externalRequests: 0,
    forbiddenQueries: 0,
    consoleErrors: 0,
    pageErrors: 0,
    loginStatuses: [200, 200],
    logoutStatuses: [200],
    billingCreateStatuses: [201, 201],
    billingTransitionStatuses: [200, 200, 200, 200],
    billingListSuccesses: 2,
    billingDetailSuccesses: 2,
  });
}

test("PRD-080 paths are repository-owned and frozen to dedicated roots", () => {
  assert.equal(DRIVER_ROOT, path.join(ROOT, ".tmp", "prd080-browser-driver"));
  assert.equal(ARTIFACT_ROOT, path.join(ROOT, ".artifacts", "prd080-browser"));
  assert.equal(HARNESS_ORIGIN, "http://127.0.0.1:18883");
  assert.equal(HARNESS_CONSOLE_URL, "http://127.0.0.1:5173/#billing-disputes");
  assert.equal(DEBUG_PORT, 19083);
  assert.equal(
    OPERABLE_DIALOG_CONTROL_SELECTOR,
    'button,input:not([type="hidden"]),select,textarea',
  );
  assert.deepEqual(VIEWPORTS, {
    desktop: { name: "desktop-1280x720", width: 1280, height: 720 },
    mobile: { name: "mobile-390x844", width: 390, height: 844 },
  });
  assert.deepEqual(SCREENSHOTS, [
    "page-112-list-desktop-1280x720.png",
    "page-112-list-mobile-390x844.png",
    "page-112-approved-desktop-1280x720.png",
    "page-112-rejected-mobile-390x844.png",
  ]);
});

test("run id and direct child reject traversal, nesting and overwrite aliases", () => {
  assert.equal(safeRunId("prd080-contract-001"), "prd080-contract-001");
  for (const value of ["..", "../escape", "a/b", "a\\b", "x", "含中文"]) {
    assert.throws(() => safeRunId(value), BrowserAcceptanceFailure);
  }
  assert.equal(
    directChild(DRIVER_ROOT, "prd080-contract-001", "config.driver"),
    path.join(DRIVER_ROOT, "prd080-contract-001"),
  );
  assert.throws(
    () => directChild(DRIVER_ROOT, "..", "config.driver"),
    BrowserAcceptanceFailure,
  );
});

test("CLI accepts one run id and rejects all ambient path overrides", () => {
  assert.deepEqual(parseArguments(["--run-id", "prd080-contract-002"]), {
    runId: "prd080-contract-002",
  });
  for (const args of [
    [],
    ["--run-id", "prd080-a", "--run-id", "prd080-b"],
    ["--artifact-dir", "C:\\outside"],
    ["--run-id", "prd080-a", "unexpected"],
  ]) assert.throws(() => parseArguments(args), BrowserAcceptanceFailure);
});

test("request classifier covers auth, list/detail and create/transition traffic", () => {
  const dispute = "bdsp_" + "1".repeat(32);
  assert.equal(classifyRequest("http://localhost:8001/auth/login", "POST"), "auth_login");
  assert.equal(classifyRequest("http://localhost:8001/auth/logout", "POST"), "auth_logout");
  assert.equal(classifyRequest("http://localhost:8001/billing-disputes", "GET"), "billing_list");
  assert.equal(classifyRequest("http://localhost:8001/billing-disputes", "POST"), "billing_create");
  assert.equal(classifyRequest(`http://localhost:8001/billing-disputes/${dispute}`, "GET"), "billing_detail");
  assert.equal(classifyRequest(`http://localhost:8001/billing-disputes/${dispute}`, "PATCH"), "billing_transition");
  assert.equal(classifyRequest("https://example.test/collect", "POST"), "external");
});

test("browser cleanup selects only the exact CDP browser process owner", () => {
  assert.equal(browserProcessIdFromProcessInfo({
    processInfo: [
      { type: "renderer", id: 41 },
      { type: "browser", id: 42 },
      { type: "utility", id: 43 },
    ],
  }), 42);
  for (const value of [
    null,
    {},
    { processInfo: [] },
    { processInfo: [{ type: "browser", id: 0 }] },
    { processInfo: [{ type: "renderer", id: 42 }] },
  ]) assert.equal(browserProcessIdFromProcessInfo(value), null);
});

test("browser cleanup observes inherited stream drain after launcher close", async () => {
  const actions = [];
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  const child = {
    pid: 17,
    exitCode: 0,
    stdout,
    stderr,
    once() { throw new Error("launcher close must not be observed again"); },
    kill() {},
    unref() {},
  };
  const browserClient = {
    async send(method) {
      actions.push(`send:${method}`);
      return { processInfo: [{ type: "browser", id: 42 }] };
    },
    close() { actions.push("browser-close"); },
  };
  const pageSession = { client: { close() { actions.push("page-close"); } } };
  const result = await closePrd080Browser(browserClient, pageSession, child, {
    platform: "win32",
    async terminateOperation(owner) {
      actions.push(`terminate:${owner.pid}`);
      assert.equal(owner.pid, 42);
      assert.equal(owner.exitCode, 0);
      const drained = new Promise((resolve) => owner.once("close", resolve));
      stdout.destroy();
      stderr.destroy();
      await drained;
    },
  });
  assert.deepEqual(result, { treeDrained: true, stdioEof: true, forced: false });
  assert.deepEqual(actions, [
    "send:SystemInfo.getProcessInfo",
    "page-close",
    "send:Browser.close",
    "browser-close",
    "terminate:42",
  ]);
});

test("browser cleanup retries normal drain before force fallback", async () => {
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  const child = {
    pid: 17,
    exitCode: 0,
    stdout,
    stderr,
    once() { throw new Error("launcher close must not be observed again"); },
    kill() {},
    unref() {},
  };
  const browserClient = {
    async send(method) {
      return method === "SystemInfo.getProcessInfo"
        ? { processInfo: [{ type: "browser", id: 42 }] }
        : undefined;
    },
    close() {},
  };
  const owners = [];
  const result = await closePrd080Browser(browserClient, null, child, {
    platform: "win32",
    async terminateOperation(owner) {
      owners.push({ pid: owner.pid, exitCode: owner.exitCode });
      if (owners.length === 1) throw new BrowserAcceptanceFailure("cleanup.browser", "graceful");
      const drained = new Promise((resolve) => owner.once("close", resolve));
      stdout.destroy();
      stderr.destroy();
      await drained;
    },
  });
  assert.deepEqual(owners, [
    { pid: 42, exitCode: 0 },
    { pid: 42, exitCode: 0 },
  ]);
  assert.deepEqual(result, { treeDrained: true, stdioEof: true, forced: false });
});

test("browser cleanup force fallback keeps the exact CDP browser pid", async () => {
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  const child = {
    pid: 17,
    exitCode: 0,
    stdout,
    stderr,
    once() { throw new Error("launcher close must not be observed again"); },
    kill() {},
    unref() {},
  };
  const browserClient = {
    async send(method) {
      return method === "SystemInfo.getProcessInfo"
        ? { processInfo: [{ type: "browser", id: 42 }] }
        : undefined;
    },
    close() {},
  };
  const owners = [];
  const result = await closePrd080Browser(browserClient, null, child, {
    platform: "win32",
    async terminateOperation(owner) {
      owners.push({ pid: owner.pid, exitCode: owner.exitCode });
      if (owners.length < 3) throw new BrowserAcceptanceFailure("cleanup.browser", "graceful");
      const drained = new Promise((resolve) => owner.once("close", resolve));
      stdout.destroy();
      stderr.destroy();
      await drained;
    },
  });
  assert.deepEqual(owners, [
    { pid: 42, exitCode: 0 },
    { pid: 42, exitCode: 0 },
    { pid: 42, exitCode: null },
  ]);
  assert.deepEqual(result, { treeDrained: true, stdioEof: true, forced: true });
});

test("browser cleanup does not complete when only one inherited stream drains", async () => {
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  const child = {
    pid: 17,
    exitCode: 0,
    stdout,
    stderr,
    once() { throw new Error("launcher close must not be observed again"); },
    kill() {},
    unref() {},
  };
  const browserClient = {
    async send() { return { processInfo: [{ type: "browser", id: 42 }] }; },
    close() {},
  };
  const pageSession = { client: { close() {} } };
  await assert.rejects(
    closePrd080Browser(browserClient, pageSession, child, {
      platform: "win32",
      async terminateOperation(owner) {
        let closeObserved = false;
        owner.once("close", () => { closeObserved = true; });
        stdout.destroy();
        await delay(25);
        assert.equal(closeObserved, false);
        throw new BrowserAcceptanceFailure("cleanup.process_tree", "close_timeout");
      },
    }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "cleanup.process_tree"
      && error.code === "close_timeout",
  );
  stderr.destroy();
});

test("browser request guard permits only frozen loopback origins and safe filters", () => {
  for (const origin of ALLOWED_ORIGINS) {
    assert.equal(browserRequestAllowed(`${origin}/health`).allowed, true);
  }
  assert.deepEqual(browserRequestAllowed("data:text/plain,ok"), {
    allowed: true,
    forbiddenQuery: false,
    external: false,
  });
  assert.equal(browserRequestAllowed("https://example.test/").external, true);
  assert.equal(
    browserRequestAllowed("http://localhost:8001/billing-disputes?status=approved").allowed,
    true,
  );
  assert.equal(
    browserRequestAllowed("http://localhost:8001/billing-disputes?tenant_id=x").forbiddenQuery,
    true,
  );
  assert.equal(
    browserRequestAllowed("http://localhost:8001/billing-disputes?reason=x").forbiddenQuery,
    true,
  );
  assert.equal(browserRequestAllowed("http://token@localhost:8001/billing-disputes").allowed, false);
});

test("completion facts are exact, boolean-only and keep all PRDs incomplete", () => {
  const facts = completeFacts();
  assert.equal(validateCompletionFacts(facts), true);
  assert.equal(facts.status, "passed");
  assert.equal(facts.all_108_prds_complete, false);
  assert.deepEqual(facts.screenshots, SCREENSHOTS);
  assert.deepEqual(facts.network, {
    external_requests: 0,
    forbidden_queries: 0,
    console_errors: 0,
    page_errors: 0,
    login_statuses: [200, 200],
    logout_statuses: [200],
    create_statuses: [201, 201],
    transition_statuses: [200, 200, 200, 200],
    list_successes: 2,
    detail_successes: 2,
  });
});

test("completion validation fails closed on every critical gate", () => {
  const mutations = [
    ["desktop", "horizontal_overflow_free", false],
    ["desktop", "partial_adjustment", false],
    ["desktop", "html_escape_safe", false],
    ["mobile", "busy_escape_blocked", false],
    ["mobile", "private_text_collapsed", false],
    ["lifecycle", "backend_rbac_denied", false],
    ["lifecycle", "logout_late_response_discarded", false],
    ["lifecycle", "revocation_late_response_discarded", false],
    ["network", "external_requests", 1],
    ["network", "console_errors", 1],
    ["network", "list_successes", 1],
    ["network", "detail_successes", 1],
  ];
  for (const [section, name, value] of mutations) {
    const facts = structuredClone(completeFacts());
    facts[section][name] = value;
    assert.throws(() => validateCompletionFacts(facts), BrowserAcceptanceFailure);
  }
  const extra = structuredClone(completeFacts());
  extra.reason = "must never be accepted";
  assert.throws(() => validateCompletionFacts(extra), BrowserAcceptanceFailure);
});

test("source contracts forbid mocks, host temp and sensitive evidence bodies", async () => {
  const runner = await readFile(
    path.join(ROOT, "scripts", "acceptance", "prd080-browser-acceptance.mjs"),
    "utf8",
  );
  const harness = await readFile(
    path.join(ROOT, "scripts", "acceptance", "prd080-browser-harness.py"),
    "utf8",
  );
  for (const source of [runner, harness]) {
    assert.doesNotMatch(source, /os\.environ\[(?:["']TMP["']|["']TEMP["'])\]\s*$/m);
    assert.doesNotMatch(source, /\.artifacts[\\/]prd081|PRD-081 browser/i);
    assert.match(source, /all_108_prds_complete[^\n]{0,80}(?:false|False)/);
  }
  assert.doesNotMatch(runner, /Fetch\.fulfillRequest|route\.fulfill|mockBilling/i);
  assert.match(runner, /billing_create/);
  assert.match(runner, /billing_transition/);
  assert.match(runner, /register-private/);
  assert.match(runner, /capture_layer_viewport_intersection/);
  assert.match(runner, /http:\/\/localhost:8001\/billing-disputes\*/);
  assert.match(runner, /http:\/\/127\.0\.0\.1:8001\/billing-disputes\*/);
  assert.match(runner, /request_missing/);
  assert.match(runner, /status_\$\{status\}/);
  const loginRole = runner.slice(
    runner.indexOf("async function loginRole("),
    runner.indexOf("async function navigateTo("),
  );
  assert.match(loginRole, /history\.replaceState[^\n]+#billing-disputes/);
  const loginNetworkGate = loginRole.indexOf("waitForTrackerCount");
  const loginStatusGate = loginRole.indexOf("tracker.loginStatuses.at(-1) === 200");
  const loginShellGate = loginRole.indexOf("document.querySelector('#logout-button')");
  const loginPageGate = loginRole.indexOf("document.querySelector('[data-billing-disputes-root]')");
  assert.ok(loginNetworkGate > 0);
  assert.ok(loginNetworkGate < loginStatusGate);
  assert.ok(loginStatusGate < loginShellGate);
  assert.ok(loginShellGate < loginPageGate);
  assert.match(harness, /EXPECTED_MIGRATION_COUNT = 73/);
  assert.match(harness, /EXPECTED_MIGRATION_VERSION = "0073"/);
  assert.match(harness, /ALLOWED_ARTIFACTS/);
  assert.match(harness, /browser-acceptance\.json/);
  assert.match(harness, /sha256-manifest\.json/);
  assert.match(harness, /_verify_private_outside_database/);
  assert.doesNotMatch(
    harness,
    /shutil\.(?:copy|copy2|copyfile|copytree)\s*\(/,
  );
  assert.doesNotMatch(harness, /permission\.code LIKE ['"]billing\.%/);
  for (const permission of [
    "billing.dispute.read",
    "billing.dispute.create",
    "billing.dispute.review",
    "billing.finops.write",
  ]) assert.match(harness, new RegExp(permission.replaceAll(".", "\\.")));
});
