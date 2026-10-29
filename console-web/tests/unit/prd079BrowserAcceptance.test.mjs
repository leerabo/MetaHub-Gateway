/** PRD-079 Browser 验收器的路径、完成信号、网络与隐私合同。 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  ALLOWED_ORIGINS,
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
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
  directChild,
  parseArguments,
  safeRunId,
  validateCompletionFacts,
} from "../../../scripts/acceptance/prd079-browser-acceptance.mjs";

const TEST_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(TEST_DIRECTORY, "..", "..", "..");

function layout(width, height) {
  return Object.freeze({
    width,
    height,
    root_visible: true,
    root_horizontal_bounds: true,
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
    planDesktop: { layout: layout(1280, 720), pixels: pixels() },
    planMobile: { layout: layout(390, 844), pixels: pixels() },
    quotaDesktop: { layout: layout(1280, 720), pixels: pixels() },
    quotaMobile: { layout: layout(390, 844), pixels: pixels() },
    planAuthenticated: true,
    planCasExact: true,
    planSaved: true,
    planNativeModal: true,
    planInitialFocus: true,
    planTabForwardWrap: true,
    planTabReverseWrap: true,
    planIdleEscapeClosed: true,
    planFocusRestored: true,
    planBusyEscapeBlocked: true,
    quotaTypesExact: true,
    quotaAdjustmentsExact: true,
    quotaReportSplit: true,
    quotaNativeModal: true,
    quotaInitialFocus: true,
    quotaIdleEscapeClosed: true,
    quotaReasonCleared: true,
    quotaFocusRestored: true,
    quotaBusyControlsDisabled: true,
    quotaBusyEscapeBlocked: true,
  }, {
    backendRbacDenied: true,
    logoutRootCleared: true,
    logoutStorageCleared: true,
    logoutLateResponseDiscarded: true,
    revocationRootCleared: true,
    revocationNavigationCleared: true,
    revocationLateResponseDiscarded: true,
  }, {
    externalRequests: 0,
    forbiddenQueries: 0,
    consoleErrors: 0,
    pageErrors: 0,
    loginStatuses: [200, 200],
    logoutStatuses: [200],
    planPatchStatuses: [200],
    quotaAdjustmentStatuses: [201, 201, 201, 201],
  });
}

test("PRD-079 paths are repository-owned and frozen to dedicated roots", () => {
  assert.equal(DRIVER_ROOT, path.join(ROOT, ".tmp", "prd079-browser-driver"));
  assert.equal(ARTIFACT_ROOT, path.join(ROOT, ".artifacts", "prd079-browser"));
  assert.equal(HARNESS_ORIGIN, "http://127.0.0.1:18882");
  assert.equal(HARNESS_CONSOLE_URL, "http://127.0.0.1:5173/#plans");
  assert.equal(
    OPERABLE_DIALOG_CONTROL_SELECTOR,
    'button,input:not([type="hidden"]),select,textarea',
  );
  assert.deepEqual(VIEWPORTS, {
    desktop: { name: "desktop-1280x720", width: 1280, height: 720 },
    mobile: { name: "mobile-390x844", width: 390, height: 844 },
  });
  assert.deepEqual(SCREENSHOTS, [
    "page-110-plan-desktop-1280x720.png",
    "page-110-plan-mobile-390x844.png",
    "page-111-quota-desktop-1280x720.png",
    "page-111-quota-mobile-390x844.png",
  ]);
});

test("run id and direct child reject traversal, nesting and overwrite aliases", () => {
  assert.equal(safeRunId("prd079-contract-001"), "prd079-contract-001");
  for (const value of ["..", "../escape", "a/b", "a\\b", "x", "含中文"] ) {
    assert.throws(() => safeRunId(value), BrowserAcceptanceFailure);
  }
  assert.equal(
    directChild(DRIVER_ROOT, "prd079-contract-001", "config.driver"),
    path.join(DRIVER_ROOT, "prd079-contract-001"),
  );
  assert.throws(
    () => directChild(DRIVER_ROOT, "..", "config.driver"),
    BrowserAcceptanceFailure,
  );
});

test("CLI accepts one run id and rejects all ambient path overrides", () => {
  assert.deepEqual(parseArguments(["--run-id", "prd079-contract-002"]), {
    runId: "prd079-contract-002",
  });
  for (const args of [
    [],
    ["--run-id", "prd079-a", "--run-id", "prd079-b"],
    ["--artifact-dir", "C:\\outside"],
    ["--run-id", "prd079-a", "unexpected"],
  ]) assert.throws(() => parseArguments(args), BrowserAcceptanceFailure);
});

test("request classifier covers auth, Plan, Quota and local service traffic", () => {
  assert.equal(classifyRequest("http://localhost:8001/auth/login", "POST"), "auth_login");
  assert.equal(classifyRequest("http://localhost:8001/auth/logout", "POST"), "auth_logout");
  assert.equal(classifyRequest("http://localhost:8001/plans", "GET"), "plan_read");
  assert.equal(classifyRequest("http://localhost:8001/plans", "PATCH"), "plan_patch");
  assert.equal(classifyRequest("http://localhost:8001/quota-buckets", "GET"), "quota_read");
  assert.equal(classifyRequest("http://localhost:8001/quota-adjustments", "POST"), "quota_adjustment");
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

test("browser request guard permits only frozen loopback origins and no sensitive query", () => {
  for (const origin of ALLOWED_ORIGINS) {
    assert.equal(browserRequestAllowed(`${origin}/health`).allowed, true);
  }
  assert.deepEqual(browserRequestAllowed("data:text/plain,ok"), {
    allowed: true,
    forbiddenQuery: false,
    external: false,
  });
  assert.equal(browserRequestAllowed("https://example.test/").external, true);
  assert.equal(browserRequestAllowed("http://localhost:8001/plans?tenant_id=x").forbiddenQuery, true);
  assert.equal(browserRequestAllowed("http://token@localhost:8001/plans").allowed, false);
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
    plan_patch_statuses: [200],
    quota_adjustment_statuses: [201, 201, 201, 201],
  });
});

test("completion validation fails closed on every critical gate", () => {
  const mutations = [
    ["desktop", "horizontal_overflow", true],
    ["desktop", "overlap_count", 1],
    ["desktop", "plan_cas_exact", false],
    ["mobile", "text_overflow", true],
    ["mobile", "quota_adjustments_exact", false],
    ["lifecycle", "backend_rbac_denied", false],
    ["lifecycle", "logout_late_response_discarded", false],
    ["lifecycle", "revocation_late_response_discarded", false],
    ["network", "external_requests", 1],
    ["network", "console_errors", 1],
    ["network", "page_errors", 1],
  ];
  for (const [section, name, value] of mutations) {
    const facts = structuredClone(completeFacts());
    facts[section][name] = value;
    assert.throws(() => validateCompletionFacts(facts), BrowserAcceptanceFailure);
  }
  const extra = structuredClone(completeFacts());
  extra.page_text = "must never be accepted";
  assert.throws(() => validateCompletionFacts(extra), BrowserAcceptanceFailure);
});

test("source contracts forbid host temp, evidence bodies and PRD-080 scope", async () => {
  const runner = await readFile(
    path.join(ROOT, "scripts", "acceptance", "prd079-browser-acceptance.mjs"),
    "utf8",
  );
  const harness = await readFile(
    path.join(ROOT, "scripts", "acceptance", "prd079-browser-harness.py"),
    "utf8",
  );
  for (const source of [runner, harness]) {
    assert.doesNotMatch(source, /os\.environ\[(?:["']TMP["']|["']TEMP["'])\]\s*$/m);
    assert.doesNotMatch(source, /\.artifacts[\\/]prd080|PRD-080 browser/i);
    assert.match(source, /all_108_prds_complete[^\n]{0,80}(?:false|False)/);
  }
  assert.match(runner, /capture_layer_viewport_intersection/);
  assert.match(runner, /browser\.plan\.mobile[\s\S]{0,160}revealRoot: true/);
  assert.match(harness, /ALLOWED_ARTIFACTS/);
  assert.match(harness, /browser-acceptance\.json/);
  assert.match(harness, /sha256-manifest\.json/);
  assert.doesNotMatch(harness, /copy2\([^\n]*database|artifact[^\n]*sqlite/i);
  assert.doesNotMatch(harness, /permission\.code LIKE ['"]billing\.%/);
  for (const permission of [
    "billing.plan.read",
    "billing.plan.write",
    "billing.quota.read",
    "billing.quota.write",
  ]) assert.match(harness, new RegExp(permission.replaceAll(".", "\\.")));
});
