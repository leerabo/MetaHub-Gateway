/**
 * Human-shaped, non-destructive interaction census for every Workspace route.
 * It exercises reversible controls only; create/delete/approve/revoke/submit
 * actions are recorded and deliberately left for dedicated workflow tests.
 */
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
const roles = [
  { name: "admin", email: process.env.METAHUB_QA_ADMIN_EMAIL, password: process.env.METAHUB_QA_ADMIN_PASSWORD, viewport: { width: 1440, height: 900 } },
  { name: "developer", email: process.env.METAHUB_QA_DEVELOPER_EMAIL, password: process.env.METAHUB_QA_DEVELOPER_PASSWORD, viewport: { width: 1280, height: 720 } },
  { name: "viewer", email: process.env.METAHUB_QA_VIEWER_EMAIL, password: process.env.METAHUB_QA_VIEWER_PASSWORD, viewport: { width: 390, height: 844 } },
].filter((role) => !process.env.METAHUB_QA_ROLE || process.env.METAHUB_QA_ROLE === role.name);
assert.ok(roles.length > 0, "interaction_audit_role_invalid");
const targets = (process.env.METAHUB_QA_TARGETS
  ? process.env.METAHUB_QA_TARGETS.split(",").map((item) => item.trim()).filter(Boolean)
  : Object.keys(WORKSPACE_SOURCE_ROUTE_REGISTRY));
assert.ok(targets.every((target) => Object.hasOwn(WORKSPACE_SOURCE_ROUTE_REGISTRY, target)), "interaction_audit_target_invalid");
const screenshotRoot = path.resolve(process.env.METAHUB_QA_ARTIFACT_ROOT ?? ".artifacts/product-qa", "interaction-screenshots");
await mkdir(screenshotRoot, { recursive: true });

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
  await page.locator("#console-sidebar").waitFor({ state: "visible", timeout: 20_000 });
}

function isDestructive(button) {
  const text = `${button.innerText ?? ""} ${button.ariaLabel ?? ""} ${button.title ?? ""} ${(button.attributes ?? []).map((item) => item.value).join(" ")}`.toLowerCase();
  return /(create|submit|save|delete|remove|revoke|approve|reject|cancel|confirm|publish|rotate|disable|enable|创建|提交|保存|删除|移除|撤权|批准|拒绝|取消|确认|发布|轮换|停用|启用)/u.test(text);
}

async function clickReversibleControls(page) {
  const actions = [];
  const clickOne = async (locator, action) => {
    const count = await locator.count();
    for (let index = 0; index < Math.min(count, 2); index += 1) {
      const button = locator.nth(index);
      if (!(await button.isVisible().catch(() => false)) || await button.isDisabled().catch(() => false)) continue;
      try {
        await button.scrollIntoViewIfNeeded();
        // A sidebar toggle starts a debounced preference PATCH. Register the
        // response waiter before clicking so a later route opener cannot tear
        // down that request and manufacture ERR_ABORTED in the audit.
        const preferenceRequest = action === "sidebar-group-toggle"
          ? page.waitForRequest((request) => request.method() === "PATCH"
            && new URL(request.url()).pathname === "/identity/preferences/navigation",
          { timeout: 750 }).catch(() => null)
          : null;
        await button.click({ timeout: 4_000 });
        // Human interaction settles an async preference write before moving on;
        // this avoids manufacturing route-transition aborts by double-clicking.
        await page.waitForTimeout(action === "sidebar-group-toggle" ? 450 : 50);
        if (preferenceRequest) {
          const request = await preferenceRequest;
          if (request) {
            await page.waitForResponse((response) => response.request() === request, { timeout: 7_000 }).catch(() => null);
          }
        }
        actions.push(action);
      } catch (error) {
        actions.push(`${action}:failed:${String(error.message).slice(0, 120)}`);
      }
    }
  };

  // Accordion/group state, tabs, filters and refresh are reversible.
  await clickOne(page.locator("[data-sidebar-group-toggle]"), "sidebar-group-toggle");
  await clickOne(page.locator("button[aria-pressed]"), "pressed-toggle");
  await clickOne(page.locator("[data-adapter-contract-refresh], [data-adapter-spec-refresh], [data-batch-refresh], [data-approval-refresh], [data-audit-log-filter-reset], [data-billing-dispute-filter-reset], [data-provider-health-refresh], [data-provider-status-refresh], [data-webhook-refresh]"), "refresh-or-reset");
  await clickOne(page.locator("button").filter({ hasText: /查看|详情|detail|view/iu }).first(), "detail-select");
  await clickOne(page.locator("button").filter({ hasText: /关闭|close|收起|collapse/iu }).first(), "close-detail-or-dialog");

  // Generic reversible openers, excluding any command that could write data.
  const openers = page.locator("button").filter({ hasNot: page.locator("[disabled]") });
  const openerCount = await openers.count();
  for (let index = 0; index < Math.min(openerCount, 12); index += 1) {
    const button = openers.nth(index);
    const info = await button.evaluate((element) => ({
      innerText: element.innerText,
      ariaLabel: element.getAttribute("aria-label"),
      title: element.getAttribute("title"),
      attributes: [...element.attributes].map((item) => ({ name: item.name, value: item.value })),
    }));
    if (!(await button.isVisible().catch(() => false)) || isDestructive(info)) continue;
    const marker = await button.evaluate((element) => `${element.getAttribute("data-console-nav") ?? ""} ${element.getAttribute("data-workspace-advanced-open") ?? ""} ${element.getAttribute("data-*-open") ?? ""} ${element.getAttribute("aria-haspopup") ?? ""}`);
    if (!/(open|dialog|menu|advanced|detail|view|expand|展开|查看|打开|更多)/iu.test(marker + " " + await button.innerText())) continue;
    try {
      await button.click({ timeout: 3_000 });
      await page.waitForTimeout(50);
      actions.push("generic-open");
      await page.keyboard.press("Escape").catch(() => {});
    } catch (error) {
      actions.push(`generic-open:failed:${String(error.message).slice(0, 120)}`);
    }
  }
  return actions;
}

async function auditRoute(browser, role, storageState, target, allowed) {
  const context = await browser.newContext({ viewport: role.viewport, storageState, serviceWorkers: "block" });
  const page = await context.newPage();
  routeLocal(page);
  const responseErrors = [];
  const requestFailures = [];
  const navigationRequests = [];
  const pendingNavigationRequests = new Set();
  let lastNavigationRequestAt = 0;
  const expectedRequestCancellations = [];
  const consoleErrors = [];
  page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("response", (response) => {
    if (response.status() < 400) return;
    try {
      const request = response.request();
      const entry = { path: new URL(response.url()).pathname, status: response.status(), method: request.method() };
      if (entry.path === "/identity/preferences/navigation" && entry.method === "PATCH") {
        entry.postData = request.postDataJSON?.() ?? request.postData() ?? null;
        entry.ifMatch = request.headerValue?.("if-match") ?? null;
        entry.idempotencyKey = request.headerValue?.("idempotency-key") ?? null;
      }
      responseErrors.push(entry);
    } catch {}
  });
  page.on("requestfailed", (request) => {
    const failure = request.failure()?.errorText;
    const pathname = new URL(request.url()).pathname;
    const description = `${request.method()} ${pathname}${failure ? ` [${failure}]` : ""}`;
    // Route transitions deliberately abort stale best-effort page visits. Keep
    // these cancellations in the report without treating them as transport
    // failures; every other failed request remains a hard audit failure.
    if (request.method() === "POST" && pathname === "/workspace/navigation/page-views"
      && failure === "net::ERR_ABORTED") {
      expectedRequestCancellations.push(description);
      return;
    }
    requestFailures.push(description);
  });
  page.on("request", (request) => {
    try {
      const url = new URL(request.url());
      if (url.pathname === "/identity/preferences/navigation" && request.method() === "PATCH") {
        pendingNavigationRequests.add(request);
        lastNavigationRequestAt = Date.now();
        navigationRequests.push({
          postData: request.postDataJSON?.() ?? request.postData() ?? null,
          ifMatch: request.headerValue?.("if-match") ?? null,
          idempotencyKey: request.headerValue?.("idempotency-key") ?? null,
        });
      }
    } catch {}
  });
  page.on("requestfinished", (request) => pendingNavigationRequests.delete(request));
  page.on("requestfailed", (request) => pendingNavigationRequests.delete(request));
  let result;
  try {
    await page.goto(`${origin}/console.html#${WORKSPACE_SOURCE_ROUTE_REGISTRY[target]}`, { waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.locator(".console-shell").waitFor({ state: "visible", timeout: 15_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {});
    const before = await page.evaluate(() => {
      const hasName = (element) => {
        const labelledBy = (element.getAttribute("aria-labelledby") ?? "").split(/\s+/u)
          .some((id) => document.getElementById(id)?.textContent?.trim());
        return Boolean(element.getAttribute("aria-label")?.trim() || labelledBy
          || element.getAttribute("title")?.trim()
          || [...(element.labels ?? [])].some((label) => label.textContent?.trim()));
      };
      const controls = [...document.querySelectorAll("button, input, select, textarea, a[href]")].filter((element) => element.checkVisibility?.({ visibilityProperty: true, opacityProperty: true }));
      // Form fields are assessed separately through their associated <label>;
      // here only interactive commands need an accessible name.
      const unlabeled = controls.filter((element) => {
        if (!(element.tagName === "A" || element.tagName === "BUTTON")) return false;
        return !element.textContent?.trim() && !hasName(element);
      });
      const inputsWithoutLabel = [...document.querySelectorAll("input:not([type=hidden]), select, textarea")].filter((element) => element.checkVisibility?.({ visibilityProperty: true, opacityProperty: true }) && !hasName(element));
      return {
        initialActive: document.querySelector(".console-shell")?.dataset.activeNavigation ?? null,
        controls: controls.length,
        buttons: controls.filter((element) => element.tagName === "BUTTON").length,
        links: controls.filter((element) => element.tagName === "A").length,
        forms: document.querySelectorAll("form").length,
        dialogs: document.querySelectorAll("dialog").length,
        unlabeled: unlabeled.length,
        inputsWithoutLabel: inputsWithoutLabel.length,
        unlabeledDetails: unlabeled.slice(0, 40).map((element) => ({
          tag: element.tagName,
          type: element.getAttribute("type"),
          id: element.id,
          name: element.getAttribute("name"),
          text: (element.textContent ?? "").trim().replace(/\s+/gu, " ").slice(0, 100),
          aria: element.getAttribute("aria-label"),
          title: element.getAttribute("title"),
          data: [...element.attributes].filter((attribute) => attribute.name.startsWith("data-")).map((attribute) => [attribute.name, attribute.value]),
        })),
      };
    });
    const actions = await clickReversibleControls(page);
    // Page-visit telemetry is intentionally delayed and serialized to stay out of
    // the active page's render/write critical path. Let an already-authorized
    // fallback visit settle before closing the context; otherwise Chromium marks
    // the in-flight request as ERR_ABORTED even when the API completed with 200.
    // The console debounces preference persistence. Give the debounce timer a
    // chance to enqueue its PATCH before evaluating the pending set.
    await page.waitForTimeout(2_500);
    const settleDeadline = Date.now() + 10_000;
    // Preference writes are debounced by the console. A zero-sized pending set
    // is not enough to close the context safely: a delayed PATCH can be
    // scheduled after the previous request has finished. Require a quiet
    // window after the last observed request and still wait for any in-flight
    // request to finish. Real request failures remain recorded above.
    const quietWindowMs = 1_000;
    const minimumSettleMs = 2_500;
    const settleStartedAt = Date.now();
    while (Date.now() < settleDeadline) {
      const quiet = Date.now() - settleStartedAt >= minimumSettleMs
        && (lastNavigationRequestAt === 0 || Date.now() - lastNavigationRequestAt >= quietWindowMs);
      if (quiet && pendingNavigationRequests.size === 0) break;
      await page.waitForTimeout(100);
    }
    const after = await page.evaluate((patterns) => ({
      active: document.querySelector(".console-shell")?.dataset.activeNavigation ?? null,
      overflowPx: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
      forbidden: patterns.filter((pattern) => new RegExp(pattern, "i").test(document.body.innerText)),
      textSample: document.body.innerText.replace(/\s+/gu, " ").trim().slice(0, 180),
    }), forbiddenPagePatterns.map((pattern) => pattern.source));
    const geometry = await page.evaluate(measureInteractiveLayout);
    // Generic view/task controls may intentionally navigate. Verify the requested
    // route before clicking, and preserve the final route separately.
    const expected = allowed ? before.initialActive === target : before.initialActive !== target;
    result = { target, allowed, expected, ...before, ...after, ...geometry, actions,
      responseErrors, requestFailures, expectedRequestCancellations, consoleErrors, navigationRequests };
    if (!expected || after.overflowPx || geometry.clipped_control_count || geometry.interactive_overlap_count || responseErrors.length || requestFailures.length || consoleErrors.length || before.unlabeled || before.inputsWithoutLabel) {
      const safe = target.replace(/[^a-z0-9_-]/giu, "_");
      result.screenshot = path.relative(process.cwd(), path.join(screenshotRoot, `${role.name}-${safe}.png`));
      await page.screenshot({ path: path.join(screenshotRoot, `${role.name}-${safe}.png`), fullPage: true });
    }
  } catch (error) {
    result = { target, allowed, failure: String(error.message), responseErrors, requestFailures,
      expectedRequestCancellations, consoleErrors, navigationRequests };
  } finally {
    await context.close();
  }
  return result;
}

const browser = await chromium.launch({ headless: true });
const report = { schema: "metahub.prd148.human-interaction-audit.v1", roles: [], route_count: targets.length, passed: false };
try {
  for (const role of roles) {
    const seed = await browser.newContext({ viewport: role.viewport, serviceWorkers: "block" });
    const loginPage = await seed.newPage();
    routeLocal(loginPage);
    await login(loginPage, role);
    await loginPage.waitForLoadState("networkidle", { timeout: 20_000 }).catch(() => {});
    const navigation = await loginPage.evaluate(async () => {
      const response = await fetch("http://127.0.0.1:8001/workspace/navigation", { credentials: "include" });
      return response.ok ? response.json() : { items: [] };
    });
    const advancedTools = await loginPage.evaluate(async () => {
      const response = await fetch("http://127.0.0.1:8001/workspace/advanced-tools", { credentials: "include" });
      return response.ok ? response.json() : null;
    });
    const allowedRoutes = allowedAuditRoutes(navigation, advancedTools);
    const storageState = await seed.storageState();
    await seed.close();
    const pages = [];
    for (const target of targets) pages.push(await auditRoute(browser, role, storageState, target, allowedRoutes.has(WORKSPACE_SOURCE_ROUTE_REGISTRY[target])));
    report.roles.push({ role: role.name, viewport: `${role.viewport.width}x${role.viewport.height}`, pages });
  }
  report.summary = {
    pages: report.roles.reduce((sum, role) => sum + role.pages.length, 0),
    failures: report.roles.flatMap((role) => role.pages.filter((page) => page.failure)),
    response_errors: report.roles.flatMap((role) => role.pages.flatMap((page) => page.responseErrors ?? [])),
    request_failures: report.roles.flatMap((role) => role.pages.flatMap((page) => page.requestFailures ?? [])),
    expected_request_cancellations: report.roles.flatMap((role) => role.pages.flatMap((page) => page.expectedRequestCancellations ?? [])),
    console_errors: report.roles.flatMap((role) => role.pages.flatMap((page) => page.consoleErrors ?? [])),
    layout_issues: report.roles.flatMap((role) => role.pages.filter((page) => page.overflowPx || page.clipped_control_count || page.interactive_overlap_count)),
    labeling_issues: report.roles.flatMap((role) => role.pages.filter((page) => page.unlabeled || page.inputsWithoutLabel)),
    authorization_issues: report.roles.flatMap((role) => role.pages.filter((page) => page.expected !== true)),
    action_issues: report.roles.flatMap((role) => role.pages.filter((page) => page.actions?.some((action) => action.includes(":failed:")))),
    forbidden_content: report.roles.flatMap((role) => role.pages.filter((page) => page.forbidden?.length)),
  };
  report.passed = Object.entries(report.summary).every(([key, value]) =>
    key === "pages" || key === "expected_request_cancellations" || value.length === 0);
  process.stdout.write(JSON.stringify(report));
} finally {
  await browser.close();
}
