/**
 * Read-only navigation and control audit for every Workspace source route.
 * It verifies target contracts and records UX heuristics without submitting
 * forms or invoking destructive commands.
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import { writeFile } from "node:fs/promises";
import { WORKSPACE_ROUTE_REGISTRY, WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../../console-web/src/lib/workspaceNavigation.js";
import { allowedAuditRoutes } from "./roleAuditExpectations.mjs";

const { chromium } = createRequire(import.meta.url)(path.resolve(".tools/product-qa-browser/node_modules/playwright"));
const controlPort = Number(process.env.METAHUB_QA_CONTROL_PORT);
const gatewayPort = Number(process.env.METAHUB_QA_GATEWAY_PORT);
const consolePort = Number(process.env.METAHUB_QA_CONSOLE_PORT);
assert.ok(controlPort > 0 && gatewayPort > 0 && consolePort > 0);
const origin = `http://127.0.0.1:${consolePort}`;
const roleDefinitions = [
  { name: "admin", email: process.env.METAHUB_QA_ADMIN_EMAIL, password: process.env.METAHUB_QA_ADMIN_PASSWORD, viewport: { width: 1440, height: 900 } },
  { name: "developer", email: process.env.METAHUB_QA_DEVELOPER_EMAIL, password: process.env.METAHUB_QA_DEVELOPER_PASSWORD, viewport: { width: 1440, height: 900 } },
  { name: "viewer", email: process.env.METAHUB_QA_VIEWER_EMAIL, password: process.env.METAHUB_QA_VIEWER_PASSWORD, viewport: { width: 390, height: 844 } },
];
const requestedRole = process.env.METAHUB_QA_ROLE?.trim();
const roles = roleDefinitions.filter((role) => !requestedRole || role.name === requestedRole);
assert.ok(roles.length > 0, `unknown navigation audit role: ${requestedRole ?? ""}`);
const targets = process.env.METAHUB_QA_TARGETS
  ? process.env.METAHUB_QA_TARGETS.split(",").map((value) => value.trim()).filter(Boolean)
  : Object.keys(WORKSPACE_SOURCE_ROUTE_REGISTRY);
for (const target of targets) assert.ok(WORKSPACE_SOURCE_ROUTE_REGISTRY[target], `unknown navigation audit route: ${target}`);

function routeLocal(page) {
  for (const [from, to] of [[8001, controlPort], [8000, gatewayPort]]) {
    for (const host of ["127.0.0.1", "localhost"]) {
      page.route(`http://${host}:${from}/**`, (route) => route.continue({
        url: route.request().url().replace(`${host}:${from}`, `127.0.0.1:${to}`),
      }));
    }
  }
}

async function login(page, role) {
  await page.goto(`${origin}/identity.html`, { waitUntil: "domcontentloaded" });
  const form = page.locator("[data-identity-login-form]");
  await form.waitFor({ state: "visible", timeout: 20_000 });
  await form.locator("[name=email]").fill(role.email);
  await form.locator("[name=password]").fill(role.password);
  await form.locator("button[type=submit]").click();
  await page.waitForURL(/console\.html(?:[?#]|$)/u, { timeout: 25_000 });
  await page.locator(".console-shell").waitFor({ state: "visible", timeout: 20_000 });
}

function internalRouteFromHref(href) {
  try {
    const url = new URL(href, `${origin}/console.html`);
    if (url.origin !== origin) return null;
    if (url.pathname !== "/console.html") {
      if (["/identity.html", "/status.html", "/partner.html", "/platform.html", "/index.html"].includes(url.pathname)) {
        return { kind: "known-page", path: url.pathname };
      }
      return { kind: "internal-path", path: url.pathname };
    }
    if (!url.hash) return { kind: "console-root", path: url.pathname };
    const routeCode = decodeURIComponent(url.hash.slice(1));
    return { kind: "console-route", routeCode };
  } catch {
    return { kind: "invalid", href };
  }
}

async function collectPage(page, target, allowed) {
  const startedAt = performance.now();
  const responseErrors = [];
  const requestFailures = [];
  const consoleErrors = [];
  page.on("response", (response) => {
    if (response.status() >= 400) responseErrors.push({ path: new URL(response.url()).pathname, status: response.status(), method: response.request().method() });
  });
  page.on("requestfailed", (request) => {
    const pathname = new URL(request.url()).pathname;
    const failure = request.failure()?.errorText;
    if (!(request.method() === "POST" && pathname === "/workspace/navigation/page-views" && failure === "net::ERR_ABORTED")) {
      requestFailures.push(`${request.method()} ${pathname}${failure ? ` [${failure}]` : ""}`);
    }
  });
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });

  let result;
  try {
    await page.goto(`${origin}/console.html#${WORKSPACE_SOURCE_ROUTE_REGISTRY[target]}`, { waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.locator(".console-shell").waitFor({ state: "visible", timeout: 15_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {});
    const dom = await page.evaluate(({ target, registry, sourceRegistry }) => {
      const visible = (element) => element.checkVisibility?.({ visibilityProperty: true, opacityProperty: true }) ?? false;
      const textOf = (element) => (element.textContent ?? "").replace(/\s+/gu, " ").trim();
      const hasAccessibleName = (element) => Boolean(
        element.getAttribute("aria-label")?.trim()
        || element.getAttribute("title")?.trim()
        || textOf(element)
        || [...(element.labels ?? [])].some((label) => textOf(label)),
      );
      const elements = [...document.querySelectorAll("a[href], button, [role='button']")].filter(visible);
      const links = elements.filter((element) => element.matches("a[href]"));
      const buttons = elements.filter((element) => !element.matches("a[href]"));
      const linkRows = links.map((element) => ({
        text: textOf(element).slice(0, 120),
        href: element.getAttribute("href"),
        target: element.getAttribute("target"),
        navTarget: element.getAttribute("data-console-nav"),
      }));
      const buttonRows = buttons.map((element) => ({
        text: textOf(element).slice(0, 120),
        type: element.getAttribute("type"),
        navTarget: element.getAttribute("data-console-nav"),
        action: [...element.attributes].filter((attribute) => attribute.name.startsWith("data-")).map((attribute) => [attribute.name, attribute.value]),
        named: hasAccessibleName(element),
      }));
      const main = document.querySelector("main");
      const mainText = textOf(main ?? document.body);
      const headingCount = main?.querySelectorAll("h1,h2").length ?? 0;
      const actionText = /下一步|开始|配置|添加|邀请|创建|连接|查看|试用|start|configure|add|invite|create|connect|view/iu.test(mainText);
      const emptyState = /暂无|没有|尚未|未配置|无记录|no (?:data|records|configured)|not configured|empty/iu.test(mainText);
      const routeCode = document.querySelector(".console-shell")?.dataset.activeNavigation ?? null;
      const unknownNavTargets = [...new Set(elements.map((element) => element.getAttribute("data-console-nav")).filter(Boolean))]
        .filter((value) => !Object.hasOwn(sourceRegistry, value));
      return {
        active: routeCode,
        expectedActive: target,
        headingCount,
        mainTextSample: mainText.slice(0, 220),
        mainSignature: mainText.replace(/\s+/gu, " ").trim().slice(0, 2_000),
        emptyState,
        actionText,
        links: linkRows,
        buttons: buttonRows,
        unnamedButtons: buttonRows.filter((row) => !row.named),
        unknownNavTargets,
        allowedRouteCodes: [...registry],
      };
    }, { target, registry: Object.keys(WORKSPACE_ROUTE_REGISTRY), sourceRegistry: WORKSPACE_SOURCE_ROUTE_REGISTRY });
    const routeIssues = [];
    for (const link of dom.links) {
      const parsed = internalRouteFromHref(link.href);
      if (!parsed) continue;
      if (parsed.kind === "invalid" || parsed.kind === "console-root" || parsed.kind === "internal-path") routeIssues.push({ code: "invalid_internal_href", link, parsed });
      if (parsed.kind === "console-route"
        && !Object.hasOwn(WORKSPACE_ROUTE_REGISTRY, parsed.routeCode)
        && !Object.hasOwn(WORKSPACE_SOURCE_ROUTE_REGISTRY, parsed.routeCode)) routeIssues.push({ code: "unknown_console_route", link, parsed });
      if (link.href.endsWith("#") || /^javascript:/iu.test(link.href)) routeIssues.push({ code: "empty_href", link });
    }
    for (const button of dom.buttons) if (button.navTarget && !Object.hasOwn(WORKSPACE_SOURCE_ROUTE_REGISTRY, button.navTarget)) routeIssues.push({ code: "unknown_nav_target", button });
    const heuristicIssues = [];
    if (dom.headingCount === 0) heuristicIssues.push({ code: "missing_main_heading" });
    if (dom.emptyState && !dom.actionText) heuristicIssues.push({ code: "empty_state_without_next_step" });
    if (dom.active !== target && allowed) routeIssues.push({ code: "allowed_route_fallback", active: dom.active, target });
    if (dom.active === target && !allowed) routeIssues.push({ code: "forbidden_route_visible", active: dom.active, target });
    result = { target, allowed, ...dom, routeIssues, heuristicIssues, responseErrors, requestFailures, consoleErrors, navigationMs: Math.round(performance.now() - startedAt) };
  } catch (error) {
    result = { target, allowed, failure: String(error.message), responseErrors, requestFailures, consoleErrors };
  }
  return result;
}

const browser = await chromium.launch({ headless: true });
const report = { schema: "metahub.prd148.navigation-link-audit.v1", route_count: targets.length, roles: [], passed: false };
try {
  for (const role of roles) {
    const seed = await browser.newContext({ viewport: role.viewport, serviceWorkers: "block" });
    const loginPage = await seed.newPage(); routeLocal(loginPage);
    const navigationResponsePromise = loginPage.waitForResponse(
      (response) => new URL(response.url()).pathname === "/workspace/navigation",
      { timeout: 30_000 },
    );
    await login(loginPage, role);
    const navigationResponse = await navigationResponsePromise;
    assert.equal(navigationResponse.status(), 200, "workspace navigation lookup failed");
    const navigation = await navigationResponse.json();
    let advancedTools = null;
    if (navigation.advanced_tools_available) {
      const advancedResponsePromise = loginPage.waitForResponse(
        (response) => new URL(response.url()).pathname === "/workspace/advanced-tools",
        { timeout: 20_000 },
      );
      await loginPage.goto(`${origin}/console.html?audit=entitlements#workspace.advanced.provider-errors`, { waitUntil: "domcontentloaded" });
      const advancedResponse = await advancedResponsePromise;
      assert.equal(advancedResponse.status(), 200, "advanced entitlement lookup failed");
      advancedTools = await advancedResponse.json();
    }
    const allowedRoutes = navigation ? allowedAuditRoutes(navigation, advancedTools) : new Set();
    const storageState = await seed.storageState(); await seed.close();
    const pages = [];
    for (const target of targets) {
      const context = await browser.newContext({ viewport: role.viewport, storageState, serviceWorkers: "block" });
      const page = await context.newPage(); routeLocal(page);
      pages.push(await collectPage(page, target, allowedRoutes.has(WORKSPACE_SOURCE_ROUTE_REGISTRY[target])));
      await context.close();
    }
    const duplicateSignatures = Object.values(pages.reduce((groups, page) => {
      const signature = (page.mainSignature ?? "").replace(/\d+/gu, "#").replace(/\s+/gu, " ").trim();
      if (!signature) return groups;
      (groups[signature] ??= []).push(page.target);
      return groups;
    }, {})).filter((items) => items.length > 1);
    const duplicateAnalysis = duplicateSignatures.map((targetsInGroup) => {
      const groupPages = targetsInGroup.map((target) => pages.find((page) => page.target === target));
      const active = [...new Set(groupPages.map((page) => page?.active).filter(Boolean))];
      const fallback = active.length === 1
        && groupPages.filter((page) => page?.allowed && page.active === page.target).length <= 1
        && groupPages.filter((page) => !(page && !page.allowed && page.active !== page.target)).length <= 1;
      return {
        targets: targetsInGroup,
        active,
        classification: fallback ? "authorization_fallback" : "same_accessible_page_signature",
      };
    });
    report.roles.push({ role: role.name, viewport: `${role.viewport.width}x${role.viewport.height}`, pages, duplicateSignatures, duplicateAnalysis });
  }
  report.summary = {
    pages: report.roles.reduce((sum, role) => sum + role.pages.length, 0),
    route_issues: report.roles.flatMap((role) => role.pages.flatMap((page) => page.routeIssues ?? [])),
    heuristic_issues: report.roles.flatMap((role) => role.pages.flatMap((page) => page.heuristicIssues ?? [])),
    failures: report.roles.flatMap((role) => role.pages.filter((page) => page.failure)),
    response_errors: report.roles.flatMap((role) => role.pages.flatMap((page) => page.responseErrors ?? [])),
    request_failures: report.roles.flatMap((role) => role.pages.flatMap((page) => page.requestFailures ?? [])),
    console_errors: report.roles.flatMap((role) => role.pages.flatMap((page) => page.consoleErrors ?? [])),
  };
  report.passed = report.summary.route_issues.length === 0
    && report.summary.heuristic_issues.length === 0
    && report.summary.failures.length === 0
    && report.summary.response_errors.length === 0
    && report.summary.request_failures.length === 0
    && report.summary.console_errors.length === 0
    && report.roles.every((role) => role.pages.length === targets.length);
  const output = process.env.METAHUB_QA_REPORT;
  if (output) await writeFile(path.resolve(output), `${JSON.stringify(report, null, 2)}\n`, "utf8");
  process.stdout.write(JSON.stringify({ pages: report.summary.pages, passed: report.passed, routeIssues: report.summary.route_issues.length, heuristicIssues: report.summary.heuristic_issues.length, duplicates: report.roles.reduce((sum, role) => sum + role.duplicateSignatures.length, 0) }));
} finally { await browser.close(); }
