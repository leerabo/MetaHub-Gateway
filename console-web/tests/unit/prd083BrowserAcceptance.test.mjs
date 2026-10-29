import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BrowserAcceptanceFailure,
  HARNESS_ORIGIN,
  POLICY_KEYS,
  SCREENSHOTS,
  VIEWPORTS,
  browserRequestAllowed,
  buildCompletionFacts,
  classifyRequest,
  createDriverDirectory,
  parseArguments,
  requestContractsExact,
  safeRunId,
  validateCompletionFacts,
} from "../../../scripts/acceptance/prd083-browser-acceptance.mjs";

const DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const DRIVER = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd083-browser-acceptance.mjs");
const HARNESS = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd083-browser-harness.py");


function completeInputs() {
  const desktopLayout = {
    width: 1280,
    height: 720,
    horizontalOverflow: false,
    textOverflow: false,
    overlapCount: 0,
  };
  return {
    page: {
      desktop: { layout: desktopLayout, nonblank: true, noSensitiveDom: true, nextContentHint: true },
      mobile: {
        layout: { ...desktopLayout, width: 390, height: 844 },
        nonblank: true,
        noSensitiveDom: true,
        nextContentHint: true,
      },
      boundedFields: true,
      adminSummary: true,
      conflict: true,
    },
    developer: { navigationVisible: true, readOnly: true },
    viewer: { navigationHidden: true, pageHidden: true },
    drift: { rootCleared: true, navigationCleared: true, lateDiscarded: true, storageCleared: true },
    registration: { status: 200 },
    tracker: {
      externalRequests: 0,
      forbiddenQueries: 0,
      consoleErrors: 0,
      pageErrors: 0,
      loginStatuses: [200, 200, 200, 200, 200, 200],
      logoutStatuses: [200, 200, 200, 200, 200, 200],
      policyStatuses: [200, 200, 409, 200],
      listSuccesses: 8,
      noStoreResponses: true,
      noStoreObserved: 13,
      requestContractsExact: true,
      requestContractCount: 4,
      policyRevisions: [1, 2, 3, 4],
    },
  };
}


test("PAGE-115 freezes run IDs, viewports, four PNG names, and exact policy keys", () => {
  assert.deepEqual(parseArguments(["--run-id", "browser-contract"]), { runId: "browser-contract" });
  assert.equal(safeRunId("final-20260816"), "final-20260816");
  assert.throws(() => safeRunId("../escape"));
  assert.deepEqual(VIEWPORTS.desktop, { name: "desktop-1280x720", width: 1280, height: 720 });
  assert.deepEqual(VIEWPORTS.mobile, { name: "mobile-390x844", width: 390, height: 844 });
  assert.equal(SCREENSHOTS.length, 4);
  assert.deepEqual(POLICY_KEYS, [
    "expected_revision", "status", "egress_mode", "connect_timeout_seconds",
    "probe_timeout_seconds", "call_timeout_seconds", "failure_threshold", "open_seconds",
  ]);
});


test("network guard admits only loopback and classifies the real network policy route", () => {
  assert.equal(browserRequestAllowed("http://localhost:5173/#mcp-tools").allowed, true);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/mcp/servers?limit=50").allowed, true);
  assert.equal(browserRequestAllowed(`${HARNESS_ORIGIN}/health`).allowed, true);
  assert.equal(browserRequestAllowed("https://example.com/").external, true);
  assert.equal(browserRequestAllowed("http://localhost:8001/mcp/servers?tenant_id=x").forbiddenQuery, true);
  assert.equal(
    classifyRequest(
      `http://localhost:8001/mcp/servers/mcps_${"a".repeat(32)}/network-policy`,
      "PATCH",
    ),
    "network_policy",
  );
});


test("completion requires all edit roles, read-only/no-DOM roles, conflict rebase, drift, and privacy", () => {
  const inputs = completeInputs();
  assert.equal(requestContractsExact(inputs.tracker), true);
  const facts = buildCompletionFacts(inputs);
  assert.equal(validateCompletionFacts(facts), facts);
  assert.equal(facts.all_108_prds_complete, false);
  assert.equal(facts.desktop.admin_updated, true);
  assert.equal(facts.desktop.security_updated, true);
  assert.equal(facts.desktop.ops_updated, true);
  assert.equal(facts.desktop.draft_preserved, true);
  assert.equal(facts.lifecycle.developer_read_only, true);
  assert.equal(facts.lifecycle.viewer_page_hidden, true);
  assert.equal(facts.lifecycle.late_response_discarded, true);
  assert.deepEqual(facts.screenshots, SCREENSHOTS);

  inputs.tracker.policyRevisions = [1, 2, 4, 4];
  assert.equal(requestContractsExact(inputs.tracker), false);
  assert.throws(() => validateCompletionFacts(buildCompletionFacts(inputs)));
});


test("driver owner marker failure rolls back only the exact new directory", async () => {
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
});


test("sources prohibit escalation, response-body capture, sensitive evidence, and stale 0075", async () => {
  const [driver, harness] = await Promise.all([readFile(DRIVER, "utf8"), readFile(HARNESS, "utf8")]);
  assert.match(driver, /all_108_prds_complete: false/);
  assert.match(harness, /EXPECTED_MIGRATION_COUNT = 76/);
  assert.match(harness, /EXPECTED_MIGRATION_VERSION = "0076"/);
  assert.match(driver, /admin_updated/);
  assert.match(driver, /security_updated/);
  assert.match(driver, /developer_read_only/);
  assert.match(driver, /late_response_discarded/);
  assert.doesNotMatch(`${driver}\n${harness}`, /sandbox_permissions/);
  assert.doesNotMatch(driver, /responseBody|Network\.getResponseBody/);
  assert.doesNotMatch(driver, /https:\/\/[^"']+\.invalid\/private|Bearer-[A-Za-z0-9_-]{16,}/);
  assert.doesNotMatch(driver, /page-114|prd082-browser-harness\.py/);
});


test("failure coordinates remain fixed and do not include response content", () => {
  const failure = new BrowserAcceptanceFailure("browser.contract", "invalid");
  assert.equal(failure.stage, "browser.contract");
  assert.equal(failure.code, "invalid");
  assert.equal(failure.message, "browser.contract:invalid");
});
