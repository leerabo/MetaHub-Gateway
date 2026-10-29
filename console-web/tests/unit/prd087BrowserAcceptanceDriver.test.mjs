/** Contract tests for the real PRD-087 PAGE-119 browser driver. */

// PAGE-119 驱动测试：验证浏览器证据目录、视口尺寸和敏感信息不落盘。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BrowserAcceptanceFailure,
  HARNESS_ORIGIN,
  SCREENSHOTS,
  VIEWPORTS,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyRequest,
  createTracker,
  inspectBusinessRequest,
  parseArguments,
  recordResponseStatus,
  responseRequestId,
  requestContractsExact,
  safeRunId,
  validateCompletionFacts,
} from "../../../scripts/acceptance/prd087-browser-acceptance.mjs";

const DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const DRIVER = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd087-browser-acceptance.mjs");
const HARNESS = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd087-browser-harness.py");


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


function screenshot(width, height) {
  return { layout: layout(width, height), nonblank: true, noSensitiveDom: true };
}


function completeInputs() {
  const tracker = {
    externalRequests: 0,
    forbiddenQueries: 0,
    consoleErrors: 0,
    pageErrors: 0,
    loginStatuses: [200, 200, 200, 200],
    logoutStatuses: [200, 200, 200, 200],
    partnerRequestCount: 16,
    partnerStatuses: [404, 201, ...Array(14).fill(200)],
    noStoreResponses: true,
    noStoreObserved: 16,
    requestContractExact: true,
    partnerRoutes: new Set([
      "GET /partners/profile",
      "POST /partners",
      "PATCH /partners/branding",
      "GET /partners/projects",
      "GET /partners/customers",
      "POST /partners/customers",
      "GET /partners/commission",
    ]),
  };
  return {
    admin: {
      setupVisible: true,
      configured: true,
      operationsVisible: true,
      desktop: screenshot(1280, 720),
      mobile: screenshot(390, 844),
    },
    viewer: {
      navigationVisible: true,
      operationEntry: true,
      operationCompleted: true,
      root: true,
      navigation: true,
      dom: true,
      branding: true,
      lateDiscarded: true,
      wide: screenshot(1440, 900),
    },
    unauthorized: { navigation: true, page: true, apiPrefetchAbsent: true },
    secondary: {
      navigationVisible: true,
      independent: true,
      previousCleared: true,
      mobile: screenshot(390, 844),
      storageCleared: true,
    },
    registration: { status: 200 },
    tracker,
  };
}


test("PAGE-119 freezes run IDs, real viewports, and screenshot contract", () => {
  assert.deepEqual(parseArguments(["--run-id", "browser-contract"]), { runId: "browser-contract" });
  assert.equal(safeRunId("final-20260817"), "final-20260817");
  assert.throws(() => safeRunId("../escape"));
  assert.deepEqual(VIEWPORTS.desktop, { name: "desktop-1280x720", width: 1280, height: 720 });
  assert.deepEqual(VIEWPORTS.mobile, { name: "mobile-390x844", width: 390, height: 844 });
  assert.deepEqual(VIEWPORTS.wide, { name: "wide-1440x900", width: 1440, height: 900 });
  assert.deepEqual(SCREENSHOTS, [
    "page-119-partner-setup-mobile-390x844.png",
    "page-119-partner-ready-desktop-1280x720.png",
    "page-119-partner-commission-wide-1440x900.png",
    "page-119-partner-tenant-isolation-mobile-390x844.png",
  ]);
});


test("network guard admits only loopback and exact partner query names", () => {
  assert.equal(browserRequestAllowed("http://localhost:5173/#partner-portal").allowed, true);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/partners/profile").allowed, true);
  assert.equal(browserRequestAllowed("http://localhost:8001/partners/customers?limit=50&offset=0").allowed, true);
  assert.equal(browserRequestAllowed("http://localhost:8001/partners/commission?format=csv&limit=50").forbiddenQuery, true);
  assert.equal(browserRequestAllowed("http://localhost:8001/partners/profile?tenant_id=private").forbiddenQuery, true);
  assert.equal(browserRequestAllowed("https://example.com/").external, true);
  assert.equal(classifyRequest("http://localhost:8001/partners/profile", "GET"), "partner:GET:/partners/profile");
  assert.equal(classifyRequest("http://localhost:8001/partners/profile", "OPTIONS"), "control_preflight");
  assert.equal(
    classifyRequest("http://localhost:8001/partners/customers/pcu_11111111111111111111111111111111", "PATCH"),
    "partner:PATCH:/partners/customers/{partner_customer_id}",
  );
  assert.equal(browserRequestAllowed(`${HARNESS_ORIGIN}/health`).allowed, true);
});


test("partner response statuses deduplicate across Fetch and Network without bodies", () => {
  const tracker = createTracker({
    adminPassword: "private-admin",
    rolePassword: "private-role",
    stopToken: "private-stop",
    driverOwner: "private-owner",
  });
  tracker.requestMethods.set("network-1", "GET");
  tracker.requestCategories.set("network-1", "partner:GET:/partners/profile");
  tracker.fetchNetworkIds.set("fetch-1", "network-1");
  recordResponseStatus(tracker, {
    requestId: responseRequestId(tracker, { requestId: "fetch-1" }),
    method: "GET",
    url: "http://localhost:8001/partners/profile",
    status: 200,
    headers: [{ name: "Cache-Control", value: "no-store, private" }],
  });
  recordResponseStatus(tracker, {
    requestId: "network-1",
    url: "http://localhost:8001/partners/profile",
    status: 200,
    headers: { "Cache-Control": "no-store, private" },
  });
  assert.deepEqual(tracker.partnerStatuses, [200]);
  assert.equal(tracker.noStoreObserved, 1);
  assert.equal(tracker.noStoreResponses, true);

  tracker.requestMethods.set("preflight-1", "OPTIONS");
  tracker.requestCategories.set("preflight-1", "control_preflight");
  recordResponseStatus(tracker, {
    requestId: "preflight-1",
    url: "http://localhost:8001/partners/profile",
    status: 204,
    headers: {},
  });
  assert.deepEqual(tracker.partnerStatuses, [200]);
  assert.equal(tracker.noStoreObserved, 1);
});


test("mutation contracts accept real plain revisions and bounded idempotency headers", () => {
  const tracker = createTracker({
    adminPassword: "private-admin",
    rolePassword: "private-role",
    stopToken: "private-stop",
    driverOwner: "private-owner",
  });
  inspectBusinessRequest(tracker, {
    method: "POST",
    url: "http://localhost:8001/partners",
    headers: {
      Authorization: "Bearer private-session-one",
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": "p" + "k".repeat(159),
    },
  });
  inspectBusinessRequest(tracker, {
    method: "PATCH",
    url: "http://localhost:8001/partners/branding",
    headers: {
      Authorization: "Bearer private-session-one",
      Accept: "application/json",
      "Content-Type": "application/json",
      "If-Match": "1",
    },
  });
  assert.equal(tracker.requestContractExact, true);
  assert.deepEqual([...tracker.partnerRoutes], ["POST /partners", "PATCH /partners/branding"]);
  assert.equal(tracker.partnerRequestCount, 2);

  inspectBusinessRequest(tracker, {
    method: "OPTIONS",
    url: "http://localhost:8001/partners/profile",
    hasPostData: false,
    headers: {
      Origin: "http://localhost:5173",
      "Access-Control-Request-Method": "GET",
      "Access-Control-Request-Headers": "authorization",
    },
  });
  assert.equal(tracker.requestContractExact, true);
  assert.equal(tracker.partnerRequestCount, 2);

  const tooLong = createTracker({
    adminPassword: "private-admin",
    rolePassword: "private-role",
    stopToken: "private-stop",
    driverOwner: "private-owner",
  });
  inspectBusinessRequest(tooLong, {
    method: "POST",
    url: "http://localhost:8001/partners",
    hasPostData: true,
    headers: {
      Authorization: "Bearer private-session-two",
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": "p" + "k".repeat(160),
    },
  });
  assert.equal(tooLong.requestContractExact, false);
});


test("completion requires initialization, viewer operation, revocation, isolation, and exact routes", () => {
  const inputs = completeInputs();
  assert.equal(requestContractsExact(inputs.tracker), true);
  const facts = buildCompletionFacts(inputs);
  assert.equal(validateCompletionFacts(facts), facts);
  assert.equal(facts.all_108_prds_complete, false);
  assert.equal(facts.desktop.page_119, true);
  assert.equal(facts.mobile.setup_visible, true);
  assert.equal(facts.lifecycle.viewer_operation_entry_visible, true);
  assert.equal(facts.lifecycle.unauthorized_api_prefetch_absent, true);
  assert.equal(facts.lifecycle.dynamic_revocation_branding_cleared, true);
  assert.equal(facts.lifecycle.late_response_discarded, true);
  assert.equal(facts.lifecycle.secondary_tenant_independent, true);
  assert.equal(facts.network.no_response_body_capture, true);
  assert.deepEqual(facts.screenshots, SCREENSHOTS);

  inputs.tracker.partnerRoutes.delete("POST /partners/customers");
  assert.equal(requestContractsExact(inputs.tracker), false);
  assert.throws(() => validateCompletionFacts(buildCompletionFacts(inputs)));
});


test("sources prohibit escalation, response capture, sensitive URLs, and PRD-088 drift", async () => {
  const [driver, harness] = await Promise.all([readFile(DRIVER, "utf8"), readFile(HARNESS, "utf8")]);
  assert.match(driver, /all_108_prds_complete: false/);
  assert.match(harness, /EXPECTED_MIGRATION_COUNT = 80/);
  assert.match(harness, /EXPECTED_MIGRATION_VERSION = "0080"/);
  assert.match(driver, /unauthorized_api_prefetch_absent/);
  assert.match(driver, /dynamic_revocation_branding_cleared/);
  assert.match(driver, /late_response_discarded/);
  assert.match(driver, /browser.viewer.login_settle/);
  assert.match(driver, /previous_tenant_dom_cleared/);
  assert.match(driver, /forceNavigation: true/);
  assert.match(driver, /secrets: privateValues/);
  assert.doesNotMatch(`${driver}\n${harness}`, /sandbox_permissions/);
  assert.doesNotMatch(driver, /responseBody|Network\.getResponseBody/);
  assert.doesNotMatch(driver, /https:\/\/[^"']+\.invalid\/private|Bearer-[A-Za-z0-9_-]{16,}/);
  assert.doesNotMatch(`${driver}\n${harness}`, /PRD-088|prd088|page-120/);
});


test("failure coordinates are stable and exclude content", () => {
  const failure = new BrowserAcceptanceFailure("browser.contract", "invalid");
  assert.equal(failure.stage, "browser.contract");
  assert.equal(failure.code, "invalid");
  assert.equal(failure.message, "browser.contract:invalid");
});
