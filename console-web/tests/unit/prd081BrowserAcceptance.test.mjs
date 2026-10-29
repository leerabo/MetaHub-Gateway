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
  parseArguments,
  safeRunId,
  terminateIdempotentBrowserOwner,
  validateCompletionFacts,
} from "../../../scripts/acceptance/prd081-browser-acceptance.mjs";

const DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const DRIVER = path.resolve(DIRECTORY, "../../../scripts/acceptance/prd081-browser-acceptance.mjs");


function completeInputs() {
  const layout = {
    width: 1280,
    height: 720,
    horizontalOverflow: false,
    textOverflow: false,
    overlapCount: 0,
  };
  return {
    desktop: { layout, nonblank: true, noSensitiveDom: true },
    mobile: {
      layout: { ...layout, width: 390, height: 844 },
      nonblank: true,
      noSensitiveDom: true,
    },
    interaction: {
      cefNarrowed: true,
      jsonNoAutoExpand: true,
      limitBounds: true,
      jsonCreated: true,
      cefCreated: true,
      jsonDelivered: true,
      cefDelivered: true,
      secretsHidden: true,
      jsonMetadata: { endpoint: true, delivery: true, noSensitive: true },
      cefMetadata: { endpoint: true, delivery: true, noSensitive: true },
    },
    permissions: {
      backendViewerDenied: true,
      viewerNavigationHidden: true,
      viewerPageHidden: true,
      logoutCleared: true,
    },
    tracker: {
      externalRequests: 0,
      forbiddenQueries: 0,
      consoleErrors: 0,
      pageErrors: 0,
      loginStatuses: [200, 200],
      logoutStatuses: [200, 200],
      createStatuses: [200, 200],
      testStatuses: [200, 200],
      listSuccesses: 3,
      deliveryListSuccesses: 2,
      createContractsExact: true,
      createFormats: new Set(["json", "cef"]),
    },
  };
}


test("PAGE-113 driver freezes run IDs, viewports, and screenshot names", () => {
  assert.deepEqual(parseArguments(["--run-id", "browser-contract"]), { runId: "browser-contract" });
  assert.equal(safeRunId("final-20260815"), "final-20260815");
  assert.throws(() => safeRunId("../escape"));
  assert.deepEqual(VIEWPORTS.desktop, { name: "desktop-1280x720", width: 1280, height: 720 });
  assert.deepEqual(VIEWPORTS.mobile, { name: "mobile-390x844", width: 390, height: 844 });
  assert.equal(SCREENSHOTS.length, 4);
});


test("network guard admits only frozen loopback origins and no sensitive query keys", () => {
  assert.equal(browserRequestAllowed("http://localhost:5173/#webhooks").allowed, true);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/webhooks?include_deliveries=true").allowed, true);
  assert.equal(browserRequestAllowed("http://localhost:8000/health").allowed, true);
  assert.equal(browserRequestAllowed(`${HARNESS_ORIGIN}/health`).allowed, true);
  assert.equal(browserRequestAllowed("https://example.com/").external, true);
  assert.equal(browserRequestAllowed("http://localhost:8001/webhooks?tenant_id=x").forbiddenQuery, true);
  assert.equal(classifyRequest("http://localhost:8001/webhooks", "POST"), "webhook_create");
  assert.equal(
    classifyRequest("http://localhost:8001/webhooks/wh_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/test", "POST"),
    "webhook_test",
  );
});


test("completion facts require both formats, both viewports, RBAC, and no browser leakage", () => {
  const inputs = completeInputs();
  const facts = buildCompletionFacts(inputs);
  assert.equal(validateCompletionFacts(facts), facts);
  assert.equal(facts.all_108_prds_complete, false);
  assert.equal(facts.interaction.cef_immediate_narrowing, true);
  assert.equal(facts.interaction.json_no_auto_expand, true);
  assert.equal(facts.permissions.backend_viewer_denied, true);
  assert.deepEqual(facts.screenshots, SCREENSHOTS);
  inputs.tracker.createFormats = new Set(["json"]);
  assert.throws(() => validateCompletionFacts(buildCompletionFacts(inputs)));
});


test("Windows browser cleanup accepts taskkill exit only after exact PID and streams are gone", async () => {
  let unrefCount = 0;
  let waitCount = 0;
  let exitWaitCount = 0;
  const owner = { pid: 42, stdout: {}, stderr: {}, unref() { unrefCount += 1; } };
  await terminateIdempotentBrowserOwner(owner, {
    async terminateOperation() {
      throw new BrowserAcceptanceFailure("cleanup.process_tree", "exit");
    },
    isAlive: () => false,
    async waitForExitOperation(pid, isAlive) {
      assert.equal(pid, 42);
      assert.equal(isAlive(), false);
      exitWaitCount += 1;
    },
    async waitOperation() { waitCount += 1; },
  });
  assert.equal(exitWaitCount, 1);
  assert.equal(waitCount, 1);
  assert.equal(unrefCount, 1);

  await assert.rejects(
    terminateIdempotentBrowserOwner(owner, {
      async terminateOperation() {
        throw new BrowserAcceptanceFailure("cleanup.process_tree", "exit");
      },
      isAlive: () => true,
      async waitForExitOperation() {
        throw new BrowserAcceptanceFailure("cleanup.process_tree", "still_alive");
      },
      async waitOperation() {},
    }),
  );
});


test("driver source does not request escalation or publish webhook bytes", async () => {
  const source = await readFile(DRIVER, "utf8");
  assert.match(source, /all_108_prds_complete: false/);
  assert.doesNotMatch(source, /sandbox_permissions/);
  assert.doesNotMatch(source, /responseBody|Network\.getResponseBody/);
  assert.doesNotMatch(source, /\.artifacts[\\/]prd082|PRD-082 browser/i);
});
