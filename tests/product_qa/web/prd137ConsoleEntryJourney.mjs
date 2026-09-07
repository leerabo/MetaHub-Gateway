/** Real Global/Workspace login must reach Console without a second Legacy credential. */
import { createRequire } from "node:module";

const require = createRequire(new URL("../../../.tools/product-qa-browser/package.json", import.meta.url));
const { chromium, request: playwrightRequest } = require("playwright");
const chunks = [];
let bytes = 0;
for await (const chunk of process.stdin) {
  chunks.push(chunk);
  bytes += chunk.length;
  if (bytes > 8192) process.exit(1);
}
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
const origin = new URL(input.origin);
if (origin.protocol !== "https:" || origin.hostname !== "127.0.0.1"
  || !origin.port || origin.pathname !== "/" || !/^[A-Za-z0-9+/]{43}=$/u.test(input.spki)) {
  throw new Error("private_origin_required");
}
const checks = [];
const responses = [];
let stage = "launch";
let context;
let consolePage;
let authorizationRequests = 0;
const report = {
  schema: "metahub.browser-late-response.v1",
  journey: "console-entry",
  viewport: input.viewport,
  real_authentication: true,
  synthetic_api_responses: false,
  guest_integrated: false,
  checks,
};
function check(code, condition) {
  checks.push({ code, passed: Boolean(condition) });
  if (!condition) throw new Error(code);
}
async function legacyStorageAbsent(page) {
  return page.evaluate(() => [localStorage, sessionStorage].every((storage) => (
    storage.getItem("metahub.auth_token") === null
    && storage.getItem("metahub.auth_context") === null
  )));
}
let browser;
try {
  browser = await chromium.launch({ executablePath: input.browser, headless: true, timeout: 30_000,
    args: ["--ignore-certificate-errors-spki-list=" + input.spki] });
  context = await browser.newContext({ viewport: input.viewport, serviceWorkers: "block" });
  context.setDefaultTimeout(30_000);
  const apiOrigins = new Set([
    "https://127.0.0.1:8001", "http://127.0.0.1:8001", "http://localhost:8001",
  ]);
  await context.route("**/*", async (route) => {
    try {
      const requested = new URL(route.request().url());
      if (requested.origin === origin.origin) return await route.continue();
      if (!apiOrigins.has(requested.origin)) return await route.abort("blockedbyclient");
      const url = new URL(requested.pathname + requested.search, origin).href;
      if (requested.protocol === origin.protocol) return await route.continue({ url });
      const response = await route.fetch({ url, maxRedirects: 0, timeout: 30_000 });
      try { return await route.fulfill({ response }); }
      finally { await response.dispose(); }
    } catch {
      // A cancelled page request cannot be continued during context teardown.
      await route.abort("failed").catch(() => {});
    }
  });
  stage = "identity_login";
  const identityPage = await context.newPage();
  await identityPage.goto(new URL("/identity.html", origin).href);
  const login = identityPage.locator("[data-identity-login-form]");
  await login.locator("input[name='email']").fill(input.email);
  await login.locator("input[name='password']").fill(input.password);
  await login.locator("button[type='submit']").click();
  await identityPage.waitForFunction(() => (
    document.querySelector(".identity-workspace-selector strong")?.textContent === "Assurance source"
    && document.querySelectorAll(".identity-membership-row").length > 0
  ));
  const session = await context.request.get(new URL("/workspace/session", origin).href);
  check("workspace_cookie_authenticated", session.status() === 200
    && (await session.json()).context.workspace_id === input.workspace_id);
  await session.dispose();
  check("identity_did_not_create_legacy_storage", await legacyStorageAbsent(identityPage));

  stage = "console_entry";
  consolePage = await context.newPage();
  consolePage.on("request", (request) => {
    if (Object.keys(request.headers()).some((name) => name.toLowerCase() === "authorization")) {
      authorizationRequests += 1;
    }
  });
  const knownPaths = new Set(["/auth/login", "/auth/me", "/identity/me", "/workspace/session",
    "/workspace/navigation", "/workspace/overview"]);
  consolePage.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (knownPaths.has(path) && responses.length < 40) responses.push({ path, status: response.status() });
  });
  await consolePage.goto(new URL("/index.html#workspace.home.overview", origin).href);
  const entered = await consolePage.locator(".console-shell[data-active-route='workspace.home.overview']")
    .waitFor({ state: "visible" }).then(() => true, () => false);
  check("console_authenticated_entry", entered);
  check("console_has_real_navigation", await consolePage.locator("[data-console-nav]").count() > 0
    && responses.some((response) => response.path === "/workspace/navigation" && response.status === 200));
  await consolePage.locator(".console-experience-section-list").waitFor({ state: "visible" });
  check("console_has_real_overview", responses.some((response) => response.path === "/workspace/overview"
    && response.status === 200) && await consolePage.locator(".console-experience-section").count() > 0);
  const refreshedOverview = consolePage.waitForResponse((response) => (
    new URL(response.url()).pathname === "/workspace/overview" && response.status() === 200
  ));
  await consolePage.locator("[data-console-experience-action='refresh']").click();
  await refreshedOverview;
  await consolePage.locator(".console-experience-section-list").waitFor({ state: "visible" });
  check("console_refresh_keeps_workspace_entry", await consolePage.locator("#logout-button").isVisible());
  check("console_did_not_create_legacy_storage", await legacyStorageAbsent(consolePage));
  check("console_did_not_send_bearer", authorizationRequests === 0);
  check("console_not_second_login", await consolePage.locator("#login-form").count() === 0);
  check("viewport_no_horizontal_overflow", await consolePage.evaluate(() => (
    document.documentElement.scrollWidth <= innerWidth + 1
  )));
  report.overview_screenshot_saved = await consolePage.screenshot({
    path: input.screenshot.replace(/\.png$/u, "-overview.png"), fullPage: true,
    mask: [consolePage.locator("input")],
  }).then(() => true);

  stage = "console_logout";
  const oldCookie = (await context.cookies(origin.origin)).map((cookie) => `${cookie.name}=${cookie.value}`).join("; ");
  const logoutResponse = consolePage.waitForResponse((response) => (
    new URL(response.url()).pathname === "/identity/logout" && response.request().method() === "POST"
  ));
  await consolePage.locator("#logout-button").click();
  check("console_logout_confirmed_by_server", (await logoutResponse).ok());
  await consolePage.locator("[data-console-session-retry]").waitFor({ state: "visible" });
  await identityPage.locator("[data-identity-login-form]").waitFor({ state: "visible" });
  check("console_logout_clears_shell", await consolePage.locator(".console-shell").count() === 0
    && await consolePage.locator(".console-experience-section-list").count() === 0);
  check("console_logout_invalidates_identity_peer", await identityPage.locator(".identity-membership-row").count() === 0);
  const replay = await playwrightRequest.newContext();
  try {
    const response = await replay.get(new URL("/workspace/session", origin).href, { headers: { Cookie: oldCookie } });
    check("console_logout_rejects_old_cookie", response.status() === 401);
    await response.dispose();
  } finally { await replay.dispose(); }
  check("console_logout_no_legacy_storage", await legacyStorageAbsent(consolePage));
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  report.stage = stage;
  report.error_type = error?.constructor?.name ?? "Error";
  report.responses = responses;
  report.authorization_requests = authorizationRequests;
  report.legacy_login_visible = consolePage
    ? await consolePage.locator("#login-form").isVisible().catch(() => false)
    : false;
  process.exitCode = 1;
} finally {
  if (consolePage) {
    report.screenshot_saved = await consolePage.screenshot({
      path: input.screenshot, fullPage: true, mask: [consolePage.locator("input")], timeout: 10_000,
    }).then(() => true, () => false);
  }
  if (report.screenshot_saved !== true) {
    report.status = "failed";
    process.exitCode = 1;
  }
  process.stdout.write(JSON.stringify(report) + "\n");
  try {
    await context?.close();
    await browser?.close();
  } catch {
    process.exitCode = 1;
  }
}
