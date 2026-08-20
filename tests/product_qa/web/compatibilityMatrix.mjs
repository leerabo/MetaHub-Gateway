/** Browser/device matrix helpers. Browser launchers report only safe capability facts. */
import { access } from "node:fs/promises";

export const DEVICE_SCALE_FACTORS = Object.freeze([1, 1.25, 1.5]);
export const COMPATIBILITY_VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "desktop-1440x900", width: 1440, height: 900 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);

const BROWSER_ENVIRONMENT_KEYS = Object.freeze({
  chromium: "METAHUB_QA_CHROMIUM_PATH",
  edge: "METAHUB_QA_EDGE_PATH",
  firefox: "METAHUB_QA_FIREFOX_PATH",
});

export async function discoverBrowsers(environment = process.env) {
  const candidates = {
    chromium: environment[BROWSER_ENVIRONMENT_KEYS.chromium] || "C:\\MetaHub\\.tools\\chrome-portable\\chrome.exe",
    edge: environment[BROWSER_ENVIRONMENT_KEYS.edge] || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    firefox: environment[BROWSER_ENVIRONMENT_KEYS.firefox] || "C:\\Program Files\\Mozilla Firefox\\firefox.exe",
  };
  const result = [];
  for (const [name, executablePath] of Object.entries(candidates)) {
    let installed = false;
    try {
      await access(executablePath);
      installed = true;
    } catch {
      // Missing optional browsers are reported as skipped, never silently treated as passed.
    }
    result.push(Object.freeze({ name, executablePath, installed, status: installed ? "available" : "skipped" }));
  }
  return Object.freeze(result);
}

export function buildCompatibilityCases(browsers, factors = DEVICE_SCALE_FACTORS, viewports = COMPATIBILITY_VIEWPORTS) {
  if (!Array.isArray(browsers)) throw new TypeError("browsers_required");
  if (!factors.every((factor) => [1, 1.25, 1.5].includes(Number(factor)))) throw new Error("scale_factor_invalid");
  return Object.freeze(browsers.flatMap((browser) => (
    factors.flatMap((deviceScaleFactor) => viewports.map((viewport) => Object.freeze({
      browser: browser.name,
      browser_status: browser.status,
      deviceScaleFactor,
      viewport: viewport.name,
      width: viewport.width,
      height: viewport.height,
    })))
  )));
}

export function summarizeCompatibility(cases, observations) {
  const byKey = new Map((observations ?? []).map((observation) => [`${observation.browser}/${observation.deviceScaleFactor}/${observation.viewport}`, observation]));
  const results = cases.map((testCase) => {
    const key = `${testCase.browser}/${testCase.deviceScaleFactor}/${testCase.viewport}`;
    const observation = byKey.get(key);
    if (testCase.browser_status === "skipped") return { ...testCase, status: "skipped" };
    return {
      ...testCase,
      status: observation?.status === "passed" && observation?.noHorizontalOverflow && observation?.loginFieldsVisible ? "passed" : "failed",
    };
  });
  return Object.freeze({
    total: results.length,
    passed: results.filter((result) => result.status === "passed").length,
    skipped: results.filter((result) => result.status === "skipped").length,
    failed: results.filter((result) => result.status === "failed").length,
    all_108_prds_complete: false,
  });
}
