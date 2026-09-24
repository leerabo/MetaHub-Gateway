/** Current-source Partner page audit for an account without a ChannelMembership. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import { measureInteractiveLayout } from "./layoutMetrics.mjs";

const { chromium } = createRequire(import.meta.url)(path.resolve(".tools/product-qa-browser/node_modules/playwright"));
const controlPort = Number(process.env.METAHUB_QA_CONTROL_PORT);
const consolePort = Number(process.env.METAHUB_QA_CONSOLE_PORT);
assert.ok(controlPort > 0 && consolePort > 0);
const origin = `http://127.0.0.1:${consolePort}`;
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
const page = await context.newPage();
const responseStatuses = [];
const requestFailures = [];
const consoleErrors = [];
page.on("response", (response) => {
  try {
    if (new URL(response.url()).pathname === "/partner/workspaces") responseStatuses.push(response.status());
  } catch { /* ignore non-http responses */ }
});
page.on("requestfailed", (request) => requestFailures.push(`${request.method()} ${new URL(request.url()).pathname}`));
page.on("console", (message) => {
  if (message.type() !== "error") return;
  // Chromium reports the expected 403 membership boundary as a generic
  // resource error; retain application-level console errors only.
  if (/Failed to load resource: the server responded with a status of 403 \(Forbidden\)/u.test(message.text())) return;
  consoleErrors.push(message.text());
});
for (const host of ["127.0.0.1", "localhost"]) {
  await page.route(`http://${host}:8001/**`, (route) => route.continue({
    url: route.request().url().replace(`${host}:8001`, `127.0.0.1:${controlPort}`),
  }));
}
try {
  await page.goto(`${origin}/identity.html`, { waitUntil: "domcontentloaded" });
  const form = page.locator("[data-identity-login-form]");
  await form.waitFor({ state: "visible", timeout: 20_000 });
  await form.locator("[name=email]").fill(process.env.METAHUB_QA_ADMIN_EMAIL);
  await form.locator("[name=password]").fill(process.env.METAHUB_QA_ADMIN_PASSWORD);
  await form.locator("button[type=submit]").click();
  await page.waitForURL(/console\.html(?:[?#]|$)/u, { timeout: 25_000 });
  // Partner APIs accept the global identity session and reject mixed workspace
  // credential domains. The identity flow auto-enters a workspace, so leave
  // that domain before exercising the Partner membership boundary.
  await context.clearCookies({ name: "__Host-metahub-workspace-session" });
  await context.clearCookies({ name: "__Host-metahub-workspace-csrf" });
  responseStatuses.length = 0;
  requestFailures.length = 0;
  consoleErrors.length = 0;
  await page.goto(`${origin}/partner.html`, { waitUntil: "domcontentloaded" });
  // Requests from the previous console document can finish after navigation;
  // start the page-level failure window once Partner has loaded.
  requestFailures.length = 0;
  consoleErrors.length = 0;
  await page.locator("#partner-app").waitFor({ state: "visible", timeout: 20_000 });
  await page.getByRole("heading", { name: "Partner access revoked", exact: true }).waitFor({ state: "visible", timeout: 20_000 });
  assert.deepEqual(responseStatuses, [403]);
  const layout = {
    ...(await page.evaluate(measureInteractiveLayout)),
    overflowPx: await page.evaluate(() => Math.max(
      0,
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )),
  };
  assert.equal(layout.overflowPx, 0);
  assert.equal(layout.clipped_control_count, 0);
  assert.equal(layout.interactive_overlap_count, 0);
  const text = await page.locator("body").innerText();
  assert.doesNotMatch(text, /access token|provider key|response body|sqlite|tenant_id/iu);
  const refresh = page.getByRole("button", { name: "Refresh", exact: true });
  await refresh.click();
  await page.waitForTimeout(250);
  assert.deepEqual(responseStatuses, [403, 403]);
  assert.deepEqual(requestFailures, []);
  assert.deepEqual(consoleErrors, []);
  process.stdout.write(JSON.stringify({
    schema: "metahub.prd148.partner-audit.v1",
    role: "admin_without_channel_membership",
    viewport: "390x844",
    membership_boundary: "partner.membership_required",
    response_statuses: responseStatuses,
    layout,
    passed: true,
  }));
} finally {
  await context.close();
  await browser.close();
}
