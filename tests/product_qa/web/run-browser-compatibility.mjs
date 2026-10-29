/**
 * Real-browser compatibility runner. It starts only the static Console server and uses
 * loopback API stubs; no credentials or business response bodies are captured.
 * Edge/Chrome run when installed; Firefox is reported as skipped when absent.
 */
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { discoverBrowsers, buildCompatibilityCases, summarizeCompatibility, DEVICE_SCALE_FACTORS, COMPATIBILITY_VIEWPORTS } from "./compatibilityMatrix.mjs";

const require = createRequire(new URL("../../../.tools/product-qa-browser/package.json", import.meta.url));
const { chromium, firefox } = require("playwright");
const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const CONSOLE_ROOT = fileURLToPath(new URL("../../../console-web/", import.meta.url));
const PORT = Number(process.env.METAHUB_QA_CONSOLE_PORT ?? 18573);
const CONSOLE_URL = `http://localhost:${PORT}/`;

async function waitForConsole() {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    try { if ((await fetch(CONSOLE_URL)).ok) return; } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("console_unavailable");
}

function json(route, body, status = 200) {
  return route.fulfill({ status, contentType: "application/json", headers: { "Cache-Control": "no-store", "X-Request-ID": "rq_qa_browser" }, body: JSON.stringify(body) });
}

async function runCase(browserInfo, browserType, testCase) {
  const browser = await browserType.launch({ executablePath: browserInfo.executablePath, headless: true });
  const context = await browser.newContext({ viewport: { width: testCase.width, height: testCase.height }, deviceScaleFactor: testCase.deviceScaleFactor });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push("console_error"); });
  await page.route("http://127.0.0.1:8001/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/auth/me") return json(route, { tenant: { tenant_id: "qa_tenant", slug: "default", name: "QA Enterprise" }, user: { tenant_id: "qa_tenant", user_id: "qa_user", email: "qa@example.test", display_name: "QA", role: "admin" }, session: { session_id: "qa_session", expires_at: "2099-01-01T00:00:00Z" }, permission_codes: ["dashboard.read"], permission_revision: 1, authorization_fingerprint: "qa_fingerprint" });
    if (pathname === "/console/navigation") return json(route, { state: "ready", active_navigation_item_id: "overview", navigation_items: [{ id: "overview", label: "Overview", group: "System", target_section: "overview", visible: true, disabled: false }] });
    return json(route, { state: "ok", request_id: "rq_qa_browser" });
  });
  await page.goto(CONSOLE_URL, { waitUntil: "networkidle" });
  const facts = await page.evaluate(() => ({
    width: innerWidth,
    height: innerHeight,
    scrollWidth: document.documentElement.scrollWidth,
    loginFieldsVisible: Boolean(document.querySelector("#login-form input[name='email']") && document.querySelector("#login-form input[name='password']")),
  }));
  await context.close();
  await browser.close();
  return { ...testCase, status: consoleErrors.length === 0 && facts.scrollWidth <= facts.width + 1 && facts.loginFieldsVisible ? "passed" : "failed", noHorizontalOverflow: facts.scrollWidth <= facts.width + 1, loginFieldsVisible: facts.loginFieldsVisible };
}

async function main() {
  const server = spawn(process.execPath, ["scripts/dev-server.mjs"], { cwd: CONSOLE_ROOT, env: { ...process.env, METAHUB_CONSOLE_PORT: String(PORT) }, windowsHide: true, stdio: ["ignore", "ignore", "ignore"] });
  try {
    await waitForConsole();
    const browsers = await discoverBrowsers();
    const cases = buildCompatibilityCases(browsers, DEVICE_SCALE_FACTORS, COMPATIBILITY_VIEWPORTS);
    const observations = [];
    for (const browserInfo of browsers) {
      if (!browserInfo.installed) continue;
      const browserType = browserInfo.name === "firefox" ? firefox : chromium;
      for (const testCase of cases.filter((item) => item.browser === browserInfo.name)) observations.push(await runCase(browserInfo, browserType, testCase));
    }
    process.stdout.write(`${JSON.stringify({ browsers: browsers.map(({ name, status }) => ({ name, status })), summary: summarizeCompatibility(cases, observations), observations: observations.map(({ browser, deviceScaleFactor, viewport, status, noHorizontalOverflow, loginFieldsVisible }) => ({ browser, deviceScaleFactor, viewport, status, noHorizontalOverflow, loginFieldsVisible })), all_108_prds_complete: false })}\n`);
  } finally {
    server.kill();
  }
}

await main();
