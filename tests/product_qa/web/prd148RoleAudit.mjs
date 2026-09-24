/** Steady, per-page role audit against an isolated current-source runtime. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import { mkdir } from "node:fs/promises";
import { WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../../console-web/src/lib/workspaceNavigation.js";
import { measureInteractiveLayout } from "./layoutMetrics.mjs";
import { allowedAuditRoutes, forbiddenPagePatterns } from "./roleAuditExpectations.mjs";

const { chromium } = createRequire(import.meta.url)(path.resolve(".tools/product-qa-browser/node_modules/playwright"));
const controlPort = Number(process.env.METAHUB_QA_CONTROL_PORT);
const gatewayPort = Number(process.env.METAHUB_QA_GATEWAY_PORT);
const consolePort = Number(process.env.METAHUB_QA_CONSOLE_PORT);
assert.ok(controlPort > 0 && gatewayPort > 0 && consolePort > 0);
const origin = `http://127.0.0.1:${consolePort}`;
const configuredRoles = [
  { name: "admin", email: process.env.METAHUB_QA_ADMIN_EMAIL, password: process.env.METAHUB_QA_ADMIN_PASSWORD, viewport: { width: 1440, height: 900 } },
  { name: "developer", email: process.env.METAHUB_QA_DEVELOPER_EMAIL, password: process.env.METAHUB_QA_DEVELOPER_PASSWORD, viewport: { width: 1440, height: 900 } },
  { name: "viewer", email: process.env.METAHUB_QA_VIEWER_EMAIL, password: process.env.METAHUB_QA_VIEWER_PASSWORD, viewport: { width: 390, height: 844 } },
];
const requestedRole = process.env.METAHUB_QA_ROLE?.trim();
const roles = configuredRoles.filter((role) => !requestedRole || role.name === requestedRole);
assert.ok(roles.length > 0, `unknown audit role: ${requestedRole ?? ""}`);
// Walk every source route so authorization fallbacks and deep links are checked
// against the same registry that renders the production navigation.
const requestedTargets = process.env.METAHUB_QA_TARGETS
  ?.split(",")
  .map((target) => target.trim())
  .filter(Boolean);
const targets = requestedTargets ?? Object.keys(WORKSPACE_SOURCE_ROUTE_REGISTRY);
for (const target of targets) assert.ok(WORKSPACE_SOURCE_ROUTE_REGISTRY[target], `unknown audit route: ${target}`);
const screenshotRoot = path.resolve(process.env.METAHUB_QA_ARTIFACT_ROOT ?? ".artifacts/product-qa", "role-screenshots");
await mkdir(screenshotRoot, { recursive: true });

function routeLocal(page) {
  for (const [from, to] of [[8001, controlPort], [8000, gatewayPort]]) for (const host of ["127.0.0.1", "localhost"]) {
    page.route(`http://${host}:${from}/**`, (route) => route.continue({ url: route.request().url().replace(`${host}:${from}`, `127.0.0.1:${to}`) }));
  }
}

async function facts(page) {
  const geometry = await page.evaluate(measureInteractiveLayout);
  const result = await page.evaluate((patterns) => {
    const text = document.body.innerText;
    return { active: document.querySelector(".console-shell")?.dataset.activeNavigation ?? null,
      overflowPx: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
      pendingResponseMessages: text.match(/等待\s+\/[^\n]*响应[。.]?/gu) ?? [],
      forbidden: patterns.filter((pattern) => new RegExp(pattern, "i").test(text)), textSample: text.replace(/\s+/gu, " ").trim().slice(0, 180) };
  }, forbiddenPagePatterns.map((pattern) => pattern.source));
  return { ...result, ...geometry };
}

async function login(page, role) {
  await page.goto(`${origin}/identity.html`, { waitUntil: "domcontentloaded" });
  const form = page.locator("[data-identity-login-form]"); await form.waitFor({ state: "visible", timeout: 20_000 });
  await form.locator("[name=email]").fill(role.email); await form.locator("[name=password]").fill(role.password);
  await form.locator("button[type=submit]").click(); await page.waitForURL(/console\.html(?:[?#]|$)/u, { timeout: 25_000 });
  await page.locator("#console-sidebar").waitFor({ state: "visible", timeout: 20_000 });
}

async function auditTarget(browser, role, storageState, target, allowed) {
  const context = await browser.newContext({ viewport: role.viewport, storageState, serviceWorkers: "block" });
  const page = await context.newPage(); routeLocal(page); const responseErrors = []; const requestFailures = [];
  const consoleErrors = []; const pendingResponses = [];
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (response.status() < 400) return;
    const detail = { path: url.pathname, status: response.status(), method: response.request().method() };
    responseErrors.push(detail);
    pendingResponses.push(response.json().then((body) => {
      detail.code = typeof body.code === "string" ? body.code : null;
      detail.requestId = typeof body.request_id === "string" ? body.request_id : null;
    }).catch(() => {}));
  });
  page.on("requestfailed", (request) => requestFailures.push(`${request.method()} ${new URL(request.url()).pathname}`));
  let result;
  try {
    const routeCode = WORKSPACE_SOURCE_ROUTE_REGISTRY[target];
    assert.ok(routeCode, `unknown audit route: ${target}`);
    await page.goto(`${origin}/console.html#${routeCode}`, { waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.locator(".console-shell").waitFor({ state: "visible", timeout: 15_000 });
    await page.waitForLoadState("networkidle", { timeout: 20_000 });
    await Promise.all(pendingResponses);
    const state = await facts(page);
    if (state.interactive_overlap_count || ["webhooks", "budgets", "feedback-summary", "request-explanations"].includes(target)) {
      const screenshot = path.join(screenshotRoot, `${role.name}-${target}.png`);
      await page.screenshot({ path: screenshot, fullPage: true });
      state.screenshot = path.relative(process.cwd(), screenshot);
    }
    assert.ok(allowed ? state.active === target : state.active !== target, `authorization navigation mismatch: ${target}, active=${state.active}, allowed=${allowed}`);
    result = { target, routeCode, allowed, ...state, responseErrors, requestFailures, consoleErrors };
  }
  catch (error) { result = { target, failure: String(error.message), ...(await facts(page).catch(() => ({}))), responseErrors, requestFailures }; }
  finally { await context.close(); }
  return result;
}

const browser = await chromium.launch({ headless: true }); const report = { schema: "metahub.prd148.role-audit.v2", roles: [], passed: false };
try {
  for (const role of roles) {
    const seed = await browser.newContext({ viewport: role.viewport, serviceWorkers: "block" }); const loginPage = await seed.newPage(); routeLocal(loginPage);
    const consoleErrors = []; const expectedBootstrap401 = [];
    loginPage.on("console", (message) => {
      if (message.type() !== "error") return;
      let pathname = "";
      try { pathname = new URL(message.location().url).pathname; } catch {}
      if (message.text().includes("401") && ["/identity/me", "/workspace/session"].includes(pathname)) expectedBootstrap401.push(pathname);
      else consoleErrors.push(message.text());
    });
    const navigationResponsePromise = loginPage.waitForResponse((response) => new URL(response.url()).pathname === "/workspace/navigation", { timeout: 30_000 });
    await login(loginPage, role);
    await loginPage.waitForLoadState("networkidle", { timeout: 20_000 });
    const navigationResponse = await navigationResponsePromise;
    assert.equal(navigationResponse.status(), 200);
    const navigation = await navigationResponse.json();
    let advancedTools = null;
    if (navigation.advanced_tools_available) {
      const responsePromise = loginPage.waitForResponse(response => new URL(response.url()).pathname === "/workspace/advanced-tools", { timeout: 20_000 });
      await loginPage.goto(`${origin}/console.html?audit=entitlements#workspace.advanced.provider-errors`, { waitUntil: "domcontentloaded" });
      const response = await responsePromise;
      assert.equal(response.status(), 200, "advanced entitlement lookup failed");
      advancedTools = await response.json();
      await loginPage.waitForLoadState("networkidle", { timeout: 20_000 });
    }
    const allowedRoutes = allowedAuditRoutes(navigation, advancedTools);
    const storageState = await seed.storageState();
    const navigationMetrics = await loginPage.locator("[data-console-nav]").evaluateAll((items) => {
      const all = [...new Set(items.map((item) => item.getAttribute("data-console-nav")).filter(Boolean))];
      const deferred = [...new Set(items.filter((item) => item.dataset.sidebarVisible === "false")
        .map((item) => item.getAttribute("data-console-nav")).filter(Boolean))];
      return {
        authorizedNavigationCount: all.length,
        defaultSidebarNavigationCount: [...new Set(items.filter((item) => item.dataset.sidebarDefault === "true")
          .map((item) => item.getAttribute("data-console-nav")).filter(Boolean))].length,
        deferredNavigationCount: deferred.length,
      };
    });
    await seed.close();
    const pages = []; for (const target of targets) pages.push(await auditTarget(browser, role, storageState, target, allowedRoutes.has(WORKSPACE_SOURCE_ROUTE_REGISTRY[target])));
    report.roles.push({ role: role.name, viewport: `${role.viewport.width}x${role.viewport.height}`,
      ...navigationMetrics, pages, consoleErrorCount: consoleErrors.length, expectedBootstrap401 });
  }
  report.scope = "Seeded roles and local browser regression; no external identity, commercial acceptance or GA signoff.";
  report.passed = report.roles.every((role) => role.consoleErrorCount === 0 && role.pages.every((page) => !page.failure && page.overflowPx === 0 && page.clipped_control_count === 0 && page.interactive_overlap_count === 0 && page.pendingResponseMessages.length === 0 && page.forbidden.length === 0 && page.responseErrors.length === 0 && page.requestFailures.length === 0 && page.consoleErrors.length === 0));
  process.stdout.write(JSON.stringify(report));
} finally { await browser.close(); }
