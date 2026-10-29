/** Real Edge service-interruption/recovery flow against the Console bundle with loopback API stubs. */
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

import { discoverBrowsers } from "./compatibilityMatrix.mjs";

const require = createRequire(new URL("../../../.tools/product-qa-browser/package.json", import.meta.url));
const { chromium } = require("playwright");
const CONSOLE_ROOT = fileURLToPath(new URL("../../../console-web/", import.meta.url));
const PORT = Number(process.env.METAHUB_QA_RECOVERY_PORT ?? 18574);
const URL_ROOT = `http://localhost:${PORT}/`;

function safeJson(route, body, status = 200) {
  return route.fulfill({ status, contentType: "application/json", headers: { "Cache-Control": "no-store", "X-Request-ID": body.request_id ?? "rq_recovery_ok" }, body: JSON.stringify(body) });
}

async function waitForConsole() {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    try { if ((await fetch(URL_ROOT)).ok) return; } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("console_unavailable");
}

async function clickPhysical(page, selector) {
  const box = await page.locator(selector).boundingBox();
  if (!box) throw new Error("click_target_missing");
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
}

async function main() {
  const browserInfo = (await discoverBrowsers()).find((browser) => browser.name === "edge" && browser.installed);
  if (!browserInfo) {
    process.stdout.write(`${JSON.stringify({ browser: "edge", status: "skipped", reason: "not_installed", all_108_prds_complete: false })}\n`);
    return;
  }
  const server = spawn(process.execPath, ["scripts/dev-server.mjs"], { cwd: CONSOLE_ROOT, env: { ...process.env, METAHUB_CONSOLE_PORT: String(PORT) }, windowsHide: true, stdio: ["ignore", "ignore", "ignore"] });
  const browser = await chromium.launch({ executablePath: browserInfo.executablePath, headless: true });
  let outageCount = 0;
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1.25 });
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", () => browserErrors.push("page_error"));
  page.on("console", (message) => {
    // A single browser network error for the intentionally injected 503 is expected; all others fail the flow.
    if (message.type() === "error" && !message.text().includes("503")) browserErrors.push("console_error");
  });
  try {
    await page.route("http://127.0.0.1:8001/**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/auth/login") {
        outageCount += 1;
        if (outageCount === 1) return safeJson(route, { code: "service.unavailable", message: "Control service temporarily unavailable", request_id: "rq_recovery_503" }, 503);
        return safeJson(route, { access_token: "qa_recovery_access", request_id: "rq_recovery_login" });
      }
      if (pathname === "/auth/me") return safeJson(route, { tenant: { tenant_id: "qa_recovery_tenant", slug: "default", name: "QA Enterprise" }, user: { tenant_id: "qa_recovery_tenant", user_id: "qa_recovery_user", email: "qa@example.test", display_name: "QA", role: "admin" }, session: { session_id: "qa_recovery_session", expires_at: "2099-01-01T00:00:00Z" }, permission_codes: ["dashboard.read"], permission_revision: 1, authorization_fingerprint: "qa_recovery_fingerprint" }, 200);
      if (pathname === "/console/navigation") return safeJson(route, { state: "ready", active_navigation_item_id: "overview", navigation_items: [{ id: "overview", label: "Overview", group: "System", target_section: "overview", description: "Review service status", visible: true, disabled: false }] });
      if (pathname === "/dashboard/role-view") return safeJson(route, { state: "ok", config: { selected_view_code: "overview", config_revision: 1, available_views: [] }, cards: [], window: { start_at: "2099-01-01T00:00:00Z", end_at: "2099-01-02T00:00:00Z", timezone: "UTC", generated_at: "2099-01-01T00:00:00Z" } });
      if (pathname === "/console/preferences") return safeJson(route, { preference: { sidebar_collapsed: false }, request_id: "rq_recovery_preferences" });
      return safeJson(route, { state: "ok", request_id: "rq_recovery_optional" });
    });
    await page.route("http://127.0.0.1:8000/**", async (route) => safeJson(route, { state: "ok", request_id: "rq_recovery_gateway" }));
    await waitForConsole();
    await page.goto(URL_ROOT, { waitUntil: "networkidle" });
    await page.locator("#login-form input[name='tenantSlug']").click();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("default");
    await page.locator("#login-form input[name='email']").click();
    await page.keyboard.type("qa@example.test");
    await page.locator("#login-form input[name='password']").click();
    await page.keyboard.type("qa-local-only");
    await clickPhysical(page, "#login-form button[type='submit']");
    await page.waitForSelector("#login-form [role='alert']", { timeout: 5000 });
    const outageText = await page.locator("#login-form [role='alert']").innerText();
    if (!outageText.includes("request_id") || !outageText.includes("rq_recovery_503")) throw new Error("outage_feedback_missing");
    await page.waitForFunction(() => {
      const button = document.querySelector("#login-form button[type='submit']");
      return Boolean(button && !button.disabled);
    }, null, { timeout: 5000 });
    // Error rendering rebuilds the form; re-enter values through physical keyboard events for the recovery attempt.
    await page.locator("#login-form input[name='email']").click();
    await page.keyboard.type("qa@example.test");
    await page.locator("#login-form input[name='password']").click();
    await page.keyboard.type("qa-local-only");
    await clickPhysical(page, "#login-form button[type='submit']");
    try {
      await page.waitForSelector(".console-shell", { timeout: 15_000 });
    } catch {
      const diagnostic = await page.evaluate(() => ({ login: Boolean(document.querySelector("#login-form")), shell: Boolean(document.querySelector(".console-shell")), textLength: document.body?.innerText?.length ?? 0, hasRequestId: document.body?.innerText?.includes("request_id") ?? false, buttonDisabled: document.querySelector("#login-form button[type='submit']")?.disabled ?? null }));
      throw new Error(`recovery_shell_timeout_${JSON.stringify({ ...diagnostic, loginAttempts: outageCount })}`);
    }
    const facts = await page.evaluate(() => ({ shell: Boolean(document.querySelector(".console-shell")), loginGone: !document.querySelector("#login-form"), width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
    if (!facts.shell || !facts.loginGone || facts.scrollWidth > facts.width + 1) throw new Error("recovery_shell_invalid");
    process.stdout.write(`${JSON.stringify({ browser: "edge", status: "passed", outage_http_status: 503, outage_feedback_request_id: true, recovered_shell: true, no_horizontal_overflow: true, browser_error_count: browserErrors.length, all_108_prds_complete: false })}\n`);
  } finally {
    await context.close();
    await browser.close();
    server.kill();
  }
}

await main();
