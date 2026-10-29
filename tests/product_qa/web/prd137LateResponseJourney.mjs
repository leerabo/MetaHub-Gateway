/** Real identity-page races; API responses are fetched, never synthesized. */
import { createRequire } from "node:module";
import { randomUUID, createHash } from "node:crypto";

const require = createRequire(new URL("../../../.tools/product-qa-browser/package.json", import.meta.url));
const { chromium, request: playwrightRequest } = require("playwright");
const chunks = [];
for await (const chunk of process.stdin) {
  chunks.push(chunk);
  if (chunks.reduce((total, value) => total + value.length, 0) > 8192) process.exit(1);
}
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
const origin = new URL(input.origin);
if (origin.protocol !== "https:" || origin.hostname !== "127.0.0.1"
  || !origin.port || origin.pathname !== "/" || !/^[A-Za-z0-9+/]{43}=$/u.test(input.spki)) {
  throw new Error("private_origin_required");
}
let stage = "launch";
const checks = [];
function check(code, condition) {
  checks.push({ code, passed: Boolean(condition) });
  if (!condition) throw new Error(code);
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
async function bounded(promise) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("operation_timeout")), 45_000);
    })]);
  } finally { clearTimeout(timer); }
}
let browser;
let context;
let held;
let release = deferred();
const responses = [];
try {
  browser = await chromium.launch({ executablePath: input.browser, headless: true, timeout: 30_000,
    args: ["--ignore-certificate-errors-spki-list=" + input.spki] });
  context = await browser.newContext({ viewport: input.viewport, serviceWorkers: "block" });
  context.setDefaultTimeout(30_000);
  context.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (new Set(["/identity/workspace-switches", "/workspace/session", "/memberships",
      "/workspace/navigation", "/identity/workspaces", "/identity/me", "/identity/logout"]).has(path)) {
      responses.push({ path, status: response.status() });
      if (responses.length > 80) responses.shift();
    }
  });
  const primary = await context.newPage();
  let armed = false;
  let ready = deferred();
  let released = deferred();
  let aborted = 0;
  primary.on("requestfailed", (request) => {
    if (new URL(request.url()).pathname === "/memberships"
      && /aborted/iu.test(request.failure()?.errorText ?? "")) aborted += 1;
  });
  await context.route("**/*", async (route) => {
    const requested = new URL(route.request().url());
    if (requested.origin === origin.origin) return route.continue();
    if (requested.origin !== "https://127.0.0.1:8001") return route.abort("blockedbyclient");
    const url = new URL(requested.pathname + requested.search, origin).href;
    if (!armed || requested.pathname !== "/memberships"
      || route.request().frame().page() !== primary) return route.continue({ url });
    armed = false;
    try {
      const response = await route.fetch({ url, maxRedirects: 0, timeout: 30_000 });
      const body = await response.body();
      const payload = JSON.parse(body.toString("utf8"));
      held = {
        cookie: (await route.request().allHeaders()).cookie,
        status: response.status(),
        private: /no-store/iu.test(response.headers()["cache-control"] ?? ""),
        members: payload.memberships,
        sha256: createHash("sha256").update(body).digest("hex"),
      };
      ready.resolve();
      await release.promise;
      try {
        await route.fulfill({ response, body });
        released.resolve("delivered");
      } catch {
        released.resolve("aborted");
      } finally { await response.dispose(); }
    } catch {
      ready.resolve();
      released.resolve("fetch_failed");
      await route.abort().catch(() => {});
    }
  });
  async function session() {
    const response = await context.request.get(new URL("/workspace/session", origin).href);
    check("current_session_authenticated", response.status() === 200);
    const payload = await response.json();
    await response.dispose();
    return payload.context;
  }
  async function rejectedCookie(cookie) {
    const replayContext = await playwrightRequest.newContext();
    try {
      const response = await replayContext.get(new URL("/workspace/session", origin).href, {
        headers: { Cookie: cookie },
      });
      return response.status() === 401;
    } finally { await replayContext.dispose(); }
  }
  async function visibleOwner(page, name) {
    await page.waitForFunction((expected) => (
      document.querySelector(".identity-workspace-selector strong")?.textContent === expected
      && document.querySelectorAll(".identity-membership-row").length > 0
    ), name);
  }
  async function switchTo(page, name) {
    stage = "open_workspace_selector";
    await page.locator("details.identity-workspace-selector summary").click();
    const choice = page.locator("[data-workspace-option]").filter({ hasText: name });
    stage = "click_workspace_choice";
    await choice.click();
    stage = "wait_peer_new_owner";
    await visibleOwner(page, name);
  }
  async function arm() {
    held = null;
    ready = deferred();
    release = deferred();
    released = deferred();
    armed = true;
    await primary.locator("[data-identity-action='refresh-workspace-context']").click();
    await bounded(ready.promise);
    check("delayed_real_private_response", held?.status === 200 && held.private
      && Array.isArray(held.members) && held.members.length > 0);
  }
  async function storageClean(page, forbidden) {
    const clean = await page.evaluate(async (values) => {
      const serialized = JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]);
      return values.every((value) => !serialized.includes(value))
        && (await caches.keys()).length === 0
        && (await indexedDB.databases()).length === 0;
    }, forbidden);
    check("browser_stores_exclude_private_state", clean);
  }
  stage = "login";
  await primary.goto(new URL("/identity.html", origin).href);
  const form = primary.locator("[data-identity-login-form]");
  await form.locator("input[name='email']").fill(input.email);
  await form.locator("input[name='password']").fill(input.password);
  await form.locator("button[type='submit']").click();
  await visibleOwner(primary, "Assurance source");
  const source = await session();
  check("registered_source_owner", source.workspace_id === input.workspace_id && source.role === "owner");
  check("source_contains_real_member", await primary.locator(".identity-membership-list")
    .innerText().then((text) => text.includes("Assurance member")));

  stage = "create_second_workspace";
  await primary.locator("[data-identity-action='show-workspace-create']").click();
  const create = primary.locator("[data-workspace-create-form]");
  await create.locator("input[name='name']").fill("Assurance browser target");
  await create.locator("input[name='slug']").fill("asr-browser-" + randomUUID().replaceAll("-", ""));
  await create.locator("select[name='region']").selectOption("cn-north-1");
  await create.locator("select[name='serviceMode']").selectOption("internal_only");
  const created = primary.waitForResponse((response) => (
    new URL(response.url()).pathname === "/workspaces" && response.request().method() === "POST"
  ));
  await create.locator("button[type='submit']").click();
  const creation = await created;
  check("real_second_workspace_created", creation.status() === 201);
  const target = (await creation.json()).workspace;
  await primary.locator("[data-workspace-create-form]").waitFor({ state: "detached" });
  await visibleOwner(primary, "Assurance source");
  const peer = await context.newPage();
  await peer.goto(new URL("/identity.html", origin).href);
  await visibleOwner(peer, "Assurance source");

  stage = "switch_with_delayed_memberships";
  await arm();
  check("held_source_has_distinct_member", held.members.some((member) => (
    member.display_name === "Assurance member"
  )));
  const sourceCookie = held.cookie;
  await switchTo(peer, "Assurance browser target");
  stage = "wait_primary_new_owner";
  await visibleOwner(primary, "Assurance browser target");
  check("new_session_owner_before_release", (await session()).workspace_id === target.workspace_id);
  const beforeRelease = await primary.locator(".identity-membership-list").innerText();
  check("new_members_before_release", !beforeRelease.includes("Assurance member"));
  release.resolve();
  const switchRelease = await bounded(released.promise);
  check("old_response_released_or_aborted", ["delivered", "aborted"].includes(switchRelease));
  await primary.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
  await visibleOwner(primary, "Assurance browser target");
  check("old_dom_not_restored", (await primary.locator(".identity-membership-list").innerText()) === beforeRelease);
  const freshMembers = await context.request.get(new URL("/memberships", origin).href);
  check("subsequent_members_match_new_owner", freshMembers.status() === 200
    && (await freshMembers.json()).memberships.every((member) => member.display_name !== "Assurance member"));
  await freshMembers.dispose();
  check("rotated_workspace_cookie_rejected", await rejectedCookie(sourceCookie));
  await storageClean(primary, [input.email, input.password, input.workspace_id, target.workspace_id, "Assurance member"]);

  stage = "logout_with_delayed_memberships";
  await arm();
  const revokedCookie = held.cookie;
  const logout = peer.waitForResponse((response) => (
    new URL(response.url()).pathname === "/identity/logout" && response.request().method() === "POST"
  ));
  await peer.locator("[data-identity-action='logout']").click();
  check("logout_confirmed_by_server", (await logout).ok());
  await primary.locator("[data-identity-login-form]").waitFor({ state: "visible" });
  release.resolve();
  const logoutRelease = await bounded(released.promise);
  check("revoked_response_released_or_aborted", ["delivered", "aborted"].includes(logoutRelease));
  await primary.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
  check("logout_dom_not_restored", await primary.locator("[data-identity-login-form]").isVisible()
    && await primary.locator(".identity-membership-row").count() === 0
    && await primary.locator(".identity-workspace-selector").count() === 0);
  check("revoked_cookie_rejected", await rejectedCookie(revokedCookie));
  await storageClean(primary, [input.email, input.password, input.workspace_id, target.workspace_id]);
  check("native_abort_observed", aborted >= 2);
  check("viewport_no_horizontal_overflow", await primary.evaluate(() => (
    document.documentElement.scrollWidth <= innerWidth + 1
  )));
  await primary.screenshot({ path: input.screenshot, fullPage: true, mask: [primary.locator("input")] });
  process.stdout.write(JSON.stringify({ schema: "metahub.browser-late-response.v1", status: "passed",
    viewport: input.viewport, checks, native_aborts: aborted,
    switch_release: switchRelease, logout_release: logoutRelease,
    real_authentication: true, synthetic_api_responses: false, guest_integrated: false }) + "\n");
} catch (error) {
  const pages = [];
  for (const page of context?.pages() ?? []) {
    pages.push(await page.evaluate(() => ({
      login: Boolean(document.querySelector("[data-identity-login-form]")),
      selectorOpen: Boolean(document.querySelector("details.identity-workspace-selector[open]")),
      choices: document.querySelectorAll("[data-workspace-option]").length,
      memberRows: document.querySelectorAll(".identity-membership-row").length,
      sourceOwner: document.querySelector(".identity-workspace-selector strong")?.textContent === "Assurance source",
      targetOwner: document.querySelector(".identity-workspace-selector strong")?.textContent === "Assurance browser target",
      alerts: document.querySelectorAll("[role='alert']").length,
    })).catch(() => ({ unavailable: true })));
  }
  process.stdout.write(JSON.stringify({ schema: "metahub.browser-late-response.v1", status: "failed",
    stage, error_type: error?.constructor?.name ?? "Error", checks, responses, pages }) + "\n");
  process.exitCode = 1;
} finally {
  release.resolve();
  await context?.close();
  await browser?.close();
}
