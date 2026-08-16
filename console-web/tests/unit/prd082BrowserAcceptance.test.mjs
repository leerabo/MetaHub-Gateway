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
  createDriverDirectory,
  parseArguments,
  requestContractsExact,
  safeRunId,
  terminateIdempotentBrowserOwner,
  validateCompletionFacts,
} from "../../../scripts/acceptance/prd082-browser-acceptance.mjs";

const DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const DRIVER = path.resolve(
  DIRECTORY,
  "../../../scripts/acceptance/prd082-browser-acceptance.mjs",
);


function completeInputs() {
  const layout = {
    width: 1280,
    height: 720,
    horizontalOverflow: false,
    textOverflow: false,
    overlapCount: 0,
  };
  return {
    page: {
      desktop: { layout, nonblank: true, noSensitiveDom: true, nextContentHint: true },
      mobile: {
        layout: { ...layout, width: 390, height: 844 },
        nonblank: true,
        noSensitiveDom: true,
        nextContentHint: true,
      },
      interaction: {
        serversVisible: true,
        toolsVisible: true,
        registrationSubmitted: true,
        refreshSubmitted: true,
        serverDisabled: true,
        serverEnabled: true,
        schemaDetailVerified: true,
        policyHighApproval: true,
        invocationSummaryVisible: true,
        approvalSummaryVisible: true,
        registrationDialogNative: true,
      },
    },
    logoutState: { cleared: true, abortObserved: true, lateDiscarded: true },
    viewer: { navigationHidden: true, pageHidden: true },
    revocation: { rootCleared: true, navigationCleared: true, lateDiscarded: true },
    registration: { status: 200 },
    tracker: {
      externalRequests: 0,
      forbiddenQueries: 0,
      consoleErrors: 0,
      pageErrors: 0,
      loginStatuses: [200, 200, 200, 200, 200],
      logoutStatuses: [200, 200, 200, 200, 200],
      registrationStatuses: [202],
      refreshStatuses: [202],
      serverStatuses: [202, 202],
      policyStatuses: [200],
      listSuccesses: 6,
      detailSuccesses: 2,
      noStoreResponses: true,
      noStoreObserved: 12,
      requestContractsExact: true,
      requestContractCounts: {
        registration: 1,
        refresh: 1,
        serverStatus: 2,
        policy: 1,
      },
      serverStatusValues: ["disabled", "active"],
      idempotencyKeys: new Set(["one-key-1", "two-key-2", "three-key-3", "four-key-4"]),
    },
  };
}


test("PAGE-114 driver freezes run IDs, viewports, and screenshot names", () => {
  assert.deepEqual(parseArguments(["--run-id", "browser-contract"]), {
    runId: "browser-contract",
  });
  assert.equal(safeRunId("final-20260816"), "final-20260816");
  assert.throws(() => safeRunId("../escape"));
  assert.deepEqual(VIEWPORTS.desktop, {
    name: "desktop-1280x720",
    width: 1280,
    height: 720,
  });
  assert.deepEqual(VIEWPORTS.mobile, {
    name: "mobile-390x844",
    width: 390,
    height: 844,
  });
  assert.equal(SCREENSHOTS.length, 4);
});


test("network guard admits only frozen loopback origins and no sensitive query keys", () => {
  assert.equal(browserRequestAllowed("http://localhost:5173/#mcp-tools").allowed, true);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/mcp/servers?limit=50").allowed, true);
  assert.equal(browserRequestAllowed(`${HARNESS_ORIGIN}/health`).allowed, true);
  assert.equal(browserRequestAllowed("https://example.com/").external, true);
  assert.equal(
    browserRequestAllowed("http://localhost:8001/mcp/tools?tenant_id=x").forbiddenQuery,
    true,
  );
  assert.equal(classifyRequest("http://localhost:8001/mcp/servers", "POST"), "server_registration");
  assert.equal(
    classifyRequest(
      "http://localhost:8001/mcp/servers/mcps_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/refresh-tools",
      "POST",
    ),
    "server_refresh",
  );
  assert.equal(
    classifyRequest(
      "http://localhost:8001/mcp/tools/mcpt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "PATCH",
    ),
    "tool_policy",
  );
});


test("completion facts require all tabs, mutations, RBAC lifecycle, and privacy gates", () => {
  const inputs = completeInputs();
  assert.equal(requestContractsExact(inputs.tracker), true);
  const facts = buildCompletionFacts(inputs);
  assert.equal(validateCompletionFacts(facts), facts);
  assert.equal(facts.all_108_prds_complete, false);
  assert.equal(facts.desktop.server_disabled, true);
  assert.equal(facts.desktop.server_enabled, true);
  assert.equal(facts.mobile.approval_summary_visible, true);
  assert.equal(facts.lifecycle.revocation_late_discarded, true);
  assert.deepEqual(facts.screenshots, SCREENSHOTS);

  inputs.tracker.serverStatusValues = ["active", "disabled"];
  assert.equal(requestContractsExact(inputs.tracker), false);
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


test("driver owner marker failure rolls back the exact new directory", async () => {
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
  assert.equal(created.length, 2);
  assert.deepEqual(removed, [{
    target: created[1],
    options: { recursive: true, force: false },
  }]);
});


test("driver source requests no escalation and never reads response bodies through CDP", async () => {
  const source = await readFile(DRIVER, "utf8");
  assert.match(source, /all_108_prds_complete: false/);
  assert.doesNotMatch(source, /sandbox_permissions/);
  assert.doesNotMatch(source, /responseBody|Network\.getResponseBody/);
  assert.doesNotMatch(source, /\.artifacts[\\/]prd083|PRD-083 browser/i);
});
