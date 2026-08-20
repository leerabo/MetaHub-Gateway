import assert from "node:assert/strict";
import test from "node:test";

import { buildCompatibilityCases, COMPATIBILITY_VIEWPORTS, DEVICE_SCALE_FACTORS, discoverBrowsers, summarizeCompatibility } from "./compatibilityMatrix.mjs";

test("COMP matrix freezes viewport and device-scale coverage", () => {
  assert.deepEqual(DEVICE_SCALE_FACTORS, [1, 1.25, 1.5]);
  assert.deepEqual(COMPATIBILITY_VIEWPORTS.map(({ name }) => name), ["desktop-1280x720", "desktop-1440x900", "mobile-390x844"]);
  const browsers = [{ name: "edge", status: "available" }, { name: "firefox", status: "skipped" }];
  const cases = buildCompatibilityCases(browsers);
  assert.equal(cases.length, 18);
  assert.equal(cases.filter((item) => item.browser === "edge").length, 9);
  assert.ok(cases.every((item) => DEVICE_SCALE_FACTORS.includes(item.deviceScaleFactor)));
});

test("COMP optional browser discovery reports missing Firefox explicitly", async () => {
  const browsers = await discoverBrowsers({
    METAHUB_QA_CHROMIUM_PATH: "C:\\does-not-exist\\chrome.exe",
    METAHUB_QA_EDGE_PATH: "C:\\does-not-exist\\edge.exe",
    METAHUB_QA_FIREFOX_PATH: "C:\\does-not-exist\\firefox.exe",
  });
  assert.deepEqual(browsers.map(({ name, status }) => ({ name, status })), [
    { name: "chromium", status: "skipped" },
    { name: "edge", status: "skipped" },
    { name: "firefox", status: "skipped" },
  ]);
});

test("COMP summary distinguishes passed and skipped real browsers", () => {
  const browsers = [{ name: "edge", status: "available" }, { name: "firefox", status: "skipped" }];
  const cases = buildCompatibilityCases(browsers, [1], [COMPATIBILITY_VIEWPORTS[0]]);
  const summary = summarizeCompatibility(cases, [{ browser: "edge", deviceScaleFactor: 1, viewport: "desktop-1280x720", status: "passed", noHorizontalOverflow: true, loginFieldsVisible: true }]);
  assert.deepEqual(summary, { total: 2, passed: 1, skipped: 1, failed: 0, all_108_prds_complete: false });
});
