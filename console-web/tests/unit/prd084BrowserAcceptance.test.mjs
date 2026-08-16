import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BrowserAcceptanceFailure,
  CREATE_BEARER_KEYS,
  CREATE_KEYS,
  HARNESS_ORIGIN,
  MODEL_KEYS,
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
} from "../../../scripts/acceptance/prd084-browser-acceptance.mjs";

const DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const DRIVER = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd084-browser-acceptance.mjs");
const HARNESS = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd084-browser-harness.py");


function layout(width, height) {
  return {
    width,
    height,
    horizontalOverflow: false,
    textOverflow: false,
    overlapCount: 0,
    tableScrollBounded: true,
  };
}


function completeInputs() {
  return {
    admin: {
      dialogDesktop: { layout: layout(1280, 720), nonblank: true, noSensitiveDom: true },
      listDesktop: { layout: layout(1280, 720), nonblank: true, noSensitiveDom: true },
      listMobile: { layout: layout(390, 844), nonblank: true, noSensitiveDom: true },
      nativeDialog: true,
      noneSuccess: true,
      bearerFailure: true,
      tokenCleared: true,
      urlCleared: true,
      endpointRows: 2,
    },
    developer: {
      navigationVisible: true,
      readTestOnly: true,
      root: true,
      navigation: true,
      endpoints: true,
      lateDiscarded: true,
    },
    secondary: {
      navigationVisible: true,
      empty: true,
      previous: true,
      storageCleared: true,
      screenshot: { layout: layout(390, 844), nonblank: true, noSensitiveDom: true },
    },
    registration: { status: 200 },
    tracker: {
      externalRequests: 0,
      forbiddenQueries: 0,
      consoleErrors: 0,
      pageErrors: 0,
      loginStatuses: [200, 200, 200],
      logoutStatuses: [200, 200, 200],
      createStatuses: [201, 201],
      testStatuses: [200, 200, 200],
      listSuccesses: 3,
      noStoreResponses: true,
      noStoreObserved: 8,
      requestContractsExact: true,
      createContracts: ["none", "bearer"],
      testContractCount: 3,
    },
  };
}


test("PAGE-116 freezes run IDs, dual viewports, screenshots, and closed create keys", () => {
  assert.deepEqual(parseArguments(["--run-id", "browser-contract"]), { runId: "browser-contract" });
  assert.equal(safeRunId("final-20260817"), "final-20260817");
  assert.throws(() => safeRunId("../escape"));
  assert.deepEqual(VIEWPORTS.desktop, { name: "desktop-1280x720", width: 1280, height: 720 });
  assert.deepEqual(VIEWPORTS.mobile, { name: "mobile-390x844", width: 390, height: 844 });
  assert.equal(SCREENSHOTS.length, 4);
  assert.deepEqual(CREATE_KEYS, [
    "provider_code", "name", "engine_kind", "base_url", "auth_type", "initial_model",
  ]);
  assert.deepEqual(CREATE_BEARER_KEYS, [...CREATE_KEYS, "bearer_token"]);
  assert.equal(MODEL_KEYS.length, 8);
});


test("network guard admits only loopback and classifies PAGE-116 routes", () => {
  assert.equal(browserRequestAllowed("http://localhost:5173/#local-models").allowed, true);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/local-endpoints").allowed, true);
  assert.equal(browserRequestAllowed("http://localhost:8000/health").allowed, true);
  assert.equal(browserRequestAllowed(`${HARNESS_ORIGIN}/health`).allowed, true);
  assert.equal(browserRequestAllowed("https://example.com/").external, true);
  assert.equal(browserRequestAllowed("http://localhost:8001/local-endpoints?tenant_id=x").forbiddenQuery, true);
  assert.equal(classifyRequest("http://localhost:8001/local-endpoints", "GET"), "endpoint_list");
  assert.equal(classifyRequest("http://localhost:8001/local-endpoints", "POST"), "endpoint_create");
  assert.equal(
    classifyRequest(`http://localhost:8001/local-endpoints/lme_${"a".repeat(32)}/test`, "POST"),
    "endpoint_test",
  );
});


test("completion requires auth modes, test outcomes, drift, isolation, layout, and privacy", () => {
  const inputs = completeInputs();
  assert.equal(requestContractsExact(inputs.tracker), true);
  const facts = buildCompletionFacts(inputs);
  assert.equal(validateCompletionFacts(facts), facts);
  assert.equal(facts.all_108_prds_complete, false);
  assert.equal(facts.desktop.none_success, true);
  assert.equal(facts.desktop.bearer_failure, true);
  assert.equal(facts.desktop.token_cleared, true);
  assert.equal(facts.mobile.tenant_isolation_empty, true);
  assert.equal(facts.lifecycle.developer_read_test_only, true);
  assert.equal(facts.lifecycle.permission_drift_endpoint_dom_cleared, true);
  assert.equal(facts.lifecycle.previous_tenant_dom_cleared, true);
  assert.deepEqual(facts.screenshots, SCREENSHOTS);

  inputs.tracker.createContracts = ["bearer", "none"];
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


test("sources prohibit escalation, response-body capture, sensitive evidence, and stale 0076", async () => {
  const [driver, harness] = await Promise.all([readFile(DRIVER, "utf8"), readFile(HARNESS, "utf8")]);
  assert.match(driver, /all_108_prds_complete: false/);
  assert.match(harness, /EXPECTED_MIGRATION_COUNT = 77/);
  assert.match(harness, /EXPECTED_MIGRATION_VERSION = "0077"/);
  assert.match(driver, /developer_read_test_only/);
  assert.match(driver, /permission_drift_endpoint_dom_cleared/);
  assert.match(driver, /previous_tenant_dom_cleared/);
  assert.doesNotMatch(`${driver}\n${harness}`, /sandbox_permissions/);
  assert.doesNotMatch(driver, /responseBody|Network\.getResponseBody/);
  assert.doesNotMatch(driver, /https:\/\/[^"']+\.invalid\/private|Bearer-[A-Za-z0-9_-]{16,}/);
  assert.doesNotMatch(driver, /page-115|prd083-browser-harness\.py/);
});


test("failure coordinates remain fixed and do not include response content", () => {
  const failure = new BrowserAcceptanceFailure("browser.contract", "invalid");
  assert.equal(failure.stage, "browser.contract");
  assert.equal(failure.code, "invalid");
  assert.equal(failure.message, "browser.contract:invalid");
});
