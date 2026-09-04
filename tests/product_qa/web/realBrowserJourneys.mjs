/** Real browser mouse journeys against an isolated product-QA runtime. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import { normalizeWorkspaceNavigation } from "../../../console-web/src/lib/workspaceNavigation.js";

const require = createRequire(import.meta.url);
const { chromium, firefox } = require(path.resolve(".tools/product-qa-browser/node_modules/playwright"));

const controlPort = Number(process.env.METAHUB_QA_CONTROL_PORT);
const gatewayPort = Number(process.env.METAHUB_QA_GATEWAY_PORT);
const consolePort = Number(process.env.METAHUB_QA_CONSOLE_PORT);
assert.ok(Number.isInteger(controlPort) && controlPort > 0);
assert.ok(Number.isInteger(gatewayPort) && gatewayPort > 0);
assert.ok(Number.isInteger(consolePort) && consolePort > 0);

const consoleUrl = `http://127.0.0.1:${consolePort}/`;
const browserName = String(process.env.METAHUB_QA_BROWSER ?? "chromium").toLowerCase();
const scale = Number(process.env.METAHUB_QA_SCALE ?? "1");
assert.ok(["chromium", "edge", "firefox"].includes(browserName), "browser_invalid");
assert.ok([1, 1.25, 1.5].includes(scale), "scale_invalid");
const edgeExecutable = process.env.METAHUB_QA_EDGE_PATH
  ?? "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const controlPatterns = ["http://127.0.0.1:8001/**", "http://localhost:8001/**"];
const gatewayPatterns = ["http://127.0.0.1:8000/**", "http://localhost:8000/**"];
const NAVIGATION_DATA_BUDGET_MS = 4_000;
const NAVIGATION_RESPONSE_OBSERVER_TIMEOUT_MS = NAVIGATION_DATA_BUDGET_MS + 500;
const forbiddenProductText = [
  /PRD-\d{3}/i,
  /migration[_ -]?(?:id|version|number)?/i,
  /sqlite_master/i,
  /worker[_ ]state/i,
  /scheduler[_ ]state/i,
  /(?:^|[\\/])(?:src|docs|infra)[\\/]/i,
];

async function humanClick(page, locator) {
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      await locator.waitFor({ state: "visible", timeout: 15_000 });
      await locator.scrollIntoViewIfNeeded();
      const box = await locator.boundingBox();
      if (!box) throw new Error("mouse_target_missing");
      const x = box.x + box.width / 2;
      const y = box.y + box.height / 2;
      await page.mouse.move(x, y, { steps: 6 });
      await page.mouse.down();
      await page.waitForTimeout(45);
      await page.mouse.up();
      return;
    } catch (error) {
      if (attempt === 3) throw error;
      await page.waitForTimeout(150);
    }
  }
}

async function collapseSidebar(page) {
  const selector = "[data-sidebar-toggle]";
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const toggle = page.locator(selector);
    if (await toggle.getAttribute("aria-expanded") === "false") return;
    await humanClick(page, toggle);
    try {
      await page.locator(`${selector}[aria-expanded='false']`).waitFor({ state: "visible", timeout: 3_000 });
      return;
    } catch {
      if (attempt === 2) throw new Error("sidebar_collapse_failed");
    }
  }
}

async function assertAllNavigationGroupsCollapsed(page) {
  assert.equal(
    await page.locator("[data-sidebar-group-toggle][aria-expanded='true']").count(),
    0,
    "sidebar_groups_should_start_collapsed",
  );
}

async function expandNavigationGroup(page, navigationId) {
  const navigation = page.locator(`[data-console-nav='${navigationId}']`);
  try {
    await navigation.first().waitFor({ state: "attached", timeout: 4_000 });
  } catch (error) {
    const diagnostic = await page.evaluate(() => ({
      routes: Array.from(document.querySelectorAll("[data-console-nav]"), (element) => (
        String(element.getAttribute("data-console-nav") ?? "")
      )).filter(Boolean),
      shellState: document.querySelector(".console-shell-message")?.textContent?.trim() ?? "",
    }));
    diagnostic.responseShape = page.__safeWorkspaceNavigationShape ?? null;
    throw new Error(
      `navigation_missing:${navigationId}:${JSON.stringify(diagnostic)}`,
      { cause: error },
    );
  }
  const group = navigation.locator("xpath=ancestor::*[@data-sidebar-group][1]");
  const groupKey = await group.getAttribute("data-sidebar-group");
  assert.ok(groupKey, `sidebar_group_missing:${navigationId}`);
  const toggle = page.locator(`[data-sidebar-group-toggle='${groupKey}']`);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (await toggle.getAttribute("aria-expanded") === "true") break;
    await humanClick(page, toggle);
    try {
      await page.locator(
        `[data-sidebar-group-toggle='${groupKey}'][aria-expanded='true']`,
      ).waitFor({ state: "visible", timeout: 1_000 });
      break;
    } catch (error) {
      if (attempt === 2) throw error;
    }
  }
  assert.equal(
    await page.locator("[data-sidebar-group-toggle][aria-expanded='true']").count(),
    1,
    `sidebar_accordion_violation:${navigationId}`,
  );
  return { groupKey, navigation };
}

async function openNavigation(page, navigationId, responsePaths) {
  const { groupKey, navigation } = await expandNavigationGroup(page, navigationId);
  const responses = Promise.all(responsePaths.map(async (expectedPath) => {
    try {
      await page.waitForResponse((response) => {
        let responsePath = "";
        try { responsePath = new URL(response.url()).pathname; } catch { return false; }
        return response.request().method() === "GET"
          && responsePath === expectedPath
          && response.status() >= 200
          && response.status() < 300;
      }, { timeout: NAVIGATION_RESPONSE_OBSERVER_TIMEOUT_MS });
    } catch (error) {
      throw new Error(`navigation_response_wait_failed:${navigationId}:${expectedPath}`, { cause: error });
    }
  }));
  const started = performance.now();
  await humanClick(page, navigation);
  try {
    await Promise.all([
      responses,
      page.locator(
        `.console-shell[data-active-navigation='${navigationId}']`,
      ).waitFor({ state: "visible", timeout: NAVIGATION_DATA_BUDGET_MS }),
      page.locator(
        `[data-console-nav='${navigationId}'][aria-current='page']`,
      ).waitFor({ state: "attached", timeout: NAVIGATION_DATA_BUDGET_MS }),
    ]);
  } catch (error) {
    throw new Error(
      `navigation_data_wait_failed:${navigationId}:${responsePaths.join(",")}`,
      { cause: error },
    );
  }
  const elapsedMs = Math.round(performance.now() - started);
  assert.ok(
    elapsedMs <= NAVIGATION_DATA_BUDGET_MS,
    `navigation_data_budget_exceeded:${navigationId}:${elapsedMs}`,
  );
  return { groupKey, elapsedMs };
}

async function typeField(page, label, value) {
  const field = page.getByLabel(label, { exact: true });
  await humanClick(page, field);
  await field.fill("");
  await field.pressSequentially(value, { delay: 8 });
}

async function createJourneyPage(browser, viewport) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: scale });
  const page = await context.newPage();
  let consoleErrorCount = 0;
  const consoleErrorKinds = [];
  let authProbeActive = true;
  const expectedAuthProbeConsoleErrorKinds = [];
  let expectedFaultConsoleErrorCount = 0;
  let expectedFaultActive = false;
  let requestFailureCount = 0;
  let requestAbortCount = 0;
  const requestFailureKinds = [];
  const expectedFailureUrls = new Set();
  const expectedFailurePaths = new Set();
  page.on("console", (message) => {
    if (message.type() === "error") {
      const text = message.text();
      const status = text.match(/status of (\d{3})/iu)?.[1] ?? "none";
      let source = "unknown";
      try {
        source = new URL(message.location().url).pathname || "/";
      } catch {
        source = "unknown";
      }
      const kind = `${/failed to load resource/iu.test(text) ? "resource" : "script"}:${status}:${source}`;
      if (authProbeActive
        && status === "401"
        && new Set(["/identity/me", "/workspace/session"]).has(source)) {
        expectedAuthProbeConsoleErrorKinds.push(kind);
      } else if (expectedFaultActive) expectedFaultConsoleErrorCount += 1;
      else {
        consoleErrorCount += 1;
        consoleErrorKinds.push(kind);
      }
    }
  });
  page.on("requestfailed", (request) => {
    const errorText = request.failure()?.errorText ?? "";
    let requestPath = "";
    try { requestPath = new URL(request.url()).pathname; } catch { /* keep invalid URL as a normal failure */ }
    if (/aborted/i.test(errorText) || expectedFailureUrls.has(request.url()) || expectedFailurePaths.has(requestPath)) {
      requestAbortCount += 1;
      expectedFailureUrls.delete(request.url());
      return;
    }
    requestFailureCount += 1;
    try {
      const url = new URL(request.url());
      requestFailureKinds.push(`${request.method()} ${url.pathname}`);
    } catch {
      requestFailureKinds.push("invalid_url");
    }
  });
  for (const controlPattern of controlPatterns) {
    await page.route(controlPattern, async (route) => {
      const routed = route.request().url()
        .replace("127.0.0.1:8001", `127.0.0.1:${controlPort}`)
        .replace("localhost:8001", `127.0.0.1:${controlPort}`);
      await route.continue({ url: routed });
    });
  }
  for (const gatewayPattern of gatewayPatterns) {
    await page.route(gatewayPattern, async (route) => {
      const routed = route.request().url()
        .replace("127.0.0.1:8000", `127.0.0.1:${gatewayPort}`)
        .replace("localhost:8000", `127.0.0.1:${gatewayPort}`);
      await route.continue({ url: routed });
    });
  }
  return {
    context,
    page,
    expectFailure(url) {
      expectedFailureUrls.add(url);
      try { expectedFailurePaths.add(new URL(url).pathname); } catch { /* route URL is always loopback */ }
    },
    facts: () => ({
      console_error_count: consoleErrorCount,
      console_error_kinds: consoleErrorKinds,
      expected_auth_probe_console_error_count: expectedAuthProbeConsoleErrorKinds.length,
      expected_auth_probe_console_error_kinds: expectedAuthProbeConsoleErrorKinds,
      expected_fault_console_error_count: expectedFaultConsoleErrorCount,
      request_failure_count: requestFailureCount,
      request_abort_count: requestAbortCount,
      request_failure_kinds: requestFailureKinds,
    }),
    expectServiceFault() { expectedFaultActive = true; },
    clearServiceFault() { expectedFaultActive = false; },
    finishAuthProbes() {
      authProbeActive = false;
      return [...expectedAuthProbeConsoleErrorKinds].sort();
    },
  };
}

async function establishScopedSessions(page, email, password) {
  await page.goto(`${consoleUrl}identity.html`, { waitUntil: "domcontentloaded" });
  const form = page.locator("[data-identity-login-form]");
  await form.waitFor({ state: "visible", timeout: 20_000 });
  const workspaceSessionResponse = page.waitForResponse((response) => {
    let responsePath = "";
    try { responsePath = new URL(response.url()).pathname; } catch { return false; }
    return response.request().method() === "GET"
      && responsePath === "/workspace/session"
      && response.status() >= 200
      && response.status() < 300;
  }, { timeout: 25_000 });
  await typeField(page, "邮箱", email);
  await typeField(page, "密码", password);
  await humanClick(page, form.getByRole("button", { name: "登录", exact: true }));
  await Promise.all([
    workspaceSessionResponse,
    page.locator(".identity-workspace-selector strong").waitFor({
      state: "visible",
      timeout: 25_000,
    }),
  ]);
}

async function login(runtime, email, password, locale) {
  const { page } = runtime;
  await establishScopedSessions(page, email, password);
  assert.deepEqual(runtime.finishAuthProbes(), [
    "resource:401:/identity/me",
    "resource:401:/workspace/session",
  ]);
  await page.goto(consoleUrl, { waitUntil: "domcontentloaded" });
  const form = page.locator("#login-form");
  assert.equal(await form.evaluate((element) => element.checkValidity()), false);
  assert.equal(await page.locator("input[name='tenantSlug']").getAttribute("required"), null);
  assert.equal(await page.locator("input[name='email']").getAttribute("required"), "");
  assert.equal(await page.locator("input[name='password']").getAttribute("required"), "");

  const tenantField = page.getByLabel("租户标识", { exact: true });
  await humanClick(page, tenantField);
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("name")), "email");
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("name")), "password");
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("type")), "submit");

  await typeField(page, "租户标识", "default");
  await typeField(page, "邮箱", email);
  await typeField(page, "密码", password);
  const navigationResponse = page.waitForResponse((response) => {
    let responsePath = "";
    try { responsePath = new URL(response.url()).pathname; } catch { return false; }
    return response.request().method() === "GET"
      && responsePath === "/workspace/navigation"
      && response.status() >= 200
      && response.status() < 300;
  }, { timeout: 20_000 });
  await humanClick(page, page.getByRole("button", { name: "登录", exact: true }));
  try {
    await page.locator("#console-sidebar").waitFor({ state: "visible", timeout: 20_000 });
  } catch {
    const alert = await page.locator("[role='alert']").allTextContents();
    throw new Error(`login_failed:${alert.join(" ").slice(0, 240)}`);
  }
  const resolvedNavigationResponse = await navigationResponse;
  const navigationPayload = await resolvedNavigationResponse.json();
  let payloadValidation = { passed: true };
  try {
    normalizeWorkspaceNavigation(navigationPayload);
  } catch (error) {
    payloadValidation = {
      passed: false,
      stack: String(error?.stack ?? "").split("\n").slice(0, 8),
    };
  }
  page.__safeWorkspaceNavigationShape = {
    keys: Object.keys(navigationPayload).sort(),
    values: Object.fromEntries([
      "contract_version", "contract_generation", "projection_generation",
      "projection_status", "navigation_epoch", "membership_revision",
      "permission_revision", "advanced_tools_available",
    ].map((key) => [key, navigationPayload[key]])),
    groups: Array.isArray(navigationPayload.groups)
      ? navigationPayload.groups.map((group) => ({
        group_code: group?.group_code,
        item_routes: Array.isArray(group?.items)
          ? group.items.map((item) => ({
            route_code: item?.route_code,
            kind: item?.kind,
            order: item?.order,
          }))
          : null,
      }))
      : null,
    detail_route_codes: navigationPayload.detail_route_codes,
    developer_route_codes: navigationPayload.developer_route_codes,
    account_action_codes: navigationPayload.account_action_codes,
    preference: navigationPayload.preference,
    payloadValidation,
    headers: {
      cache_control: resolvedNavigationResponse.headers()["cache-control"],
      pragma: resolvedNavigationResponse.headers().pragma,
      vary: resolvedNavigationResponse.headers().vary,
      etag: resolvedNavigationResponse.headers().etag,
      permission_fingerprint_matches:
        resolvedNavigationResponse.headers()["x-permission-fingerprint"]
          === navigationPayload.permission_fingerprint,
    },
  };
  const expectedLogoutLabel = locale === "en-US" ? "Log out" : "\u767b\u51fa";
  await page.waitForFunction(
    (expected) => document.querySelector("#logout-button")?.textContent?.trim() === expected,
    expectedLogoutLabel,
    { timeout: 20_000 },
  );
  return { keyboard_navigation_passed: true, native_validation_passed: true };
}

async function assertProductSurface(page) {
  const text = await page.locator("body").innerText();
  for (const pattern of forbiddenProductText) assert.doesNotMatch(text, pattern);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 1, `horizontal_overflow:${overflow}`);
  const layout = await page.evaluate(() => {
    const visible = [...document.querySelectorAll("button, input, select, textarea, a[href]")]
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      });
    const clipped = visible.filter((element) => (
      ["BUTTON", "A"].includes(element.tagName)
      && (element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1)
    ));
    let overlaps = 0;
    for (let leftIndex = 0; leftIndex < visible.length; leftIndex += 1) {
      const left = visible[leftIndex];
      const leftRect = left.getBoundingClientRect();
      for (let rightIndex = leftIndex + 1; rightIndex < visible.length; rightIndex += 1) {
        const right = visible[rightIndex];
        if (left.contains(right) || right.contains(left)) continue;
        const rightRect = right.getBoundingClientRect();
        const overlapWidth = Math.min(leftRect.right, rightRect.right) - Math.max(leftRect.left, rightRect.left);
        const overlapHeight = Math.min(leftRect.bottom, rightRect.bottom) - Math.max(leftRect.top, rightRect.top);
        if (overlapWidth > 2 && overlapHeight > 2) overlaps += 1;
      }
    }
    return { clipped_control_count: clipped.length, interactive_overlap_count: overlaps };
  });
  assert.equal(layout.clipped_control_count, 0, "clipped_interactive_control");
  assert.equal(layout.interactive_overlap_count, 0, "overlapping_interactive_controls");
  return { horizontal_overflow_px: Math.max(0, overflow), ...layout };
}

async function logout(page) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (await page.locator("#login-form").isVisible()) return;
    await humanClick(page, page.locator("#logout-button"));
    try {
      await page.locator("#login-form").waitFor({ state: "visible", timeout: 5_000 });
      return;
    } catch {
      if (attempt === 2) throw new Error("logout_failed");
    }
  }
}

async function verifyServiceRecovery(runtime) {
  const { page } = runtime;
  let interrupted = true;
  const routeHandler = async (route) => {
    if (interrupted && route.request().url().includes("/usage/summary")) {
      interrupted = false;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          code: "service.unavailable",
          message: "service temporarily unavailable",
          request_id: "qa_fault_request",
          trace_id: "qa_fault_trace",
        }),
      });
      return;
    }
    await route.continue();
  };
  runtime.expectServiceFault();
  runtime.expectFailure(`${consoleUrl}usage/summary`);
  await page.route("**/usage/summary", routeHandler);
  const { navigation: usageNavigation } = await expandNavigationGroup(page, "usage");
  await humanClick(page, usageNavigation);
  await page.locator("#usage-title").waitFor({ state: "visible", timeout: 20_000 });
  await page.waitForTimeout(350);
  const errorText = await page.locator("body").innerText();
  assert.match(errorText, /request_id/i, "service_error_request_id_missing");
  await page.unroute("**/usage/summary", routeHandler);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator("#console-sidebar").waitFor({ state: "visible", timeout: 20_000 });
  const { navigation: recoveredUsageNavigation } = await expandNavigationGroup(page, "usage");
  await humanClick(page, recoveredUsageNavigation);
  await page.locator("#usage-title").waitFor({ state: "visible", timeout: 20_000 });
  await page.waitForTimeout(500);
  const recoveredText = await page.locator("body").innerText();
  assert.doesNotMatch(recoveredText, /用量账本暂不可用|usage ledger unavailable/i);
  await page.waitForTimeout(300);
  runtime.clearServiceFault();
}

async function runJourney(browser, definition) {
  const started = performance.now();
  const runtime = await createJourneyPage(browser, definition.viewport);
  try {
    const loginFacts = await login(
      runtime,
      definition.email,
      definition.password,
      definition.locale,
    );
    const logoutLabel = (await runtime.page.locator("#logout-button").innerText()).trim();
    assert.equal(logoutLabel, definition.locale === "en-US" ? "Log out" : "\u767b\u51fa");
    const executionFacts = await definition.execute(runtime.page, runtime) ?? {};
    const layout = await assertProductSurface(runtime.page);
    await logout(runtime.page);
    const facts = runtime.facts();
    assert.equal(
      facts.request_failure_count,
      0,
      `request_failures:${facts.request_failure_kinds.join(",")}`,
    );
    assert.equal(
      facts.console_error_count,
      0,
      `console_errors:${facts.console_error_kinds.join(",")}`,
    );
    return {
      journey_id: definition.id,
      role: definition.role,
      ui_locale: definition.locale,
      viewport: `${definition.viewport.width}x${definition.viewport.height}`,
      duration_ms: Math.round(performance.now() - started),
      ...executionFacts,
      ...loginFacts,
      ...layout,
      ...facts,
      passed: true,
    };
  } finally {
    await runtime.context.close();
  }
}

const browser = browserName === "firefox"
  ? await firefox.launch({ headless: true })
  : await chromium.launch({
    headless: true,
    ...(browserName === "edge" ? { executablePath: edgeExecutable } : {}),
  });
try {
  const journeys = [];
  journeys.push(await runJourney(browser, {
    id: "FLOW-01",
    role: "admin",
    locale: "en-US",
    viewport: { width: 1280, height: 720 },
    email: process.env.METAHUB_QA_ADMIN_EMAIL,
    password: process.env.METAHUB_QA_ADMIN_PASSWORD,
    async execute(page, runtime) {
      await assertAllNavigationGroupsCollapsed(page);
      const taskHome = await openNavigation(
        page,
        "quickstart",
        ["/workspace/getting-started"],
      );
      await page.locator(".workspace-task-home").waitFor({ state: "visible", timeout: 4_000 });
      const advancedTrigger = page.locator("[data-workspace-advanced-open]");
      const advancedResponse = page.waitForResponse((response) => {
        let responsePath = "";
        try { responsePath = new URL(response.url()).pathname; } catch { return false; }
        return response.request().method() === "GET"
          && responsePath === "/workspace/advanced-tools"
          && response.status() >= 200
          && response.status() < 300;
      }, { timeout: 10_000 });
      await humanClick(page, advancedTrigger);
      await advancedResponse;
      const advancedDialog = page.locator("[data-workspace-advanced-dialog]");
      await advancedDialog.waitFor({ state: "visible", timeout: 4_000 });
      assert.equal(await advancedDialog.getAttribute("open"), "");
      assert.equal(await page.evaluate(() => (
        document.querySelector("[data-workspace-advanced-dialog]")
          ?.contains(document.activeElement) === true
      )), true, "advanced_dialog_focus_missing");
      await page.keyboard.press("Escape");
      await advancedDialog.waitFor({ state: "detached", timeout: 4_000 });
      assert.equal(await advancedTrigger.evaluate((element) => document.activeElement === element), true);
      const providers = await openNavigation(page, "providers", ["/providers"]);
      const usage = await openNavigation(page, "usage", ["/usage/summary", "/usage/records"]);
      assert.notEqual(providers.groupKey, usage.groupKey, "qa_navigation_groups_must_be_distinct");
      assert.equal(
        await page.locator(`[data-sidebar-group-toggle='${providers.groupKey}']`).getAttribute("aria-expanded"),
        "false",
        "opening_usage_must_collapse_provider_group",
      );
      await verifyServiceRecovery(runtime);
      await collapseSidebar(page);
      return {
        advanced_dialog_keyboard_passed: true,
        task_home_visible: true,
        navigation_latency_ms: {
          task_home: taskHome.elapsedMs,
          providers: providers.elapsedMs,
          usage: usage.elapsedMs,
        },
      };
    },
  }));
  journeys.push(await runJourney(browser, {
    id: "FLOW-02",
    role: "developer",
    locale: "zh-CN",
    viewport: { width: 1440, height: 900 },
    email: process.env.METAHUB_QA_DEVELOPER_EMAIL,
    password: process.env.METAHUB_QA_DEVELOPER_PASSWORD,
    async execute(page) {
      await assertAllNavigationGroupsCollapsed(page);
      const projects = await openNavigation(page, "projects", ["/projects"]);
      const virtualKeys = await openNavigation(
        page,
        "virtual-keys",
        ["/virtual-keys", "/projects", "/environments"],
      );
      return {
        navigation_latency_ms: {
          projects: projects.elapsedMs,
          virtual_keys: virtualKeys.elapsedMs,
        },
      };
    },
  }));
  journeys.push(await runJourney(browser, {
    id: "FLOW-03",
    role: "viewer",
    locale: "zh-CN",
    viewport: { width: 390, height: 844 },
    email: process.env.METAHUB_QA_VIEWER_EMAIL,
    password: process.env.METAHUB_QA_VIEWER_PASSWORD,
    async execute(page) {
      const toggle = page.locator("[data-sidebar-toggle]");
      assert.equal(await toggle.getAttribute("aria-expanded"), "false");
      assert.equal(
        await page.locator(".console-shell.sidebar-collapsed").count(),
        1,
        "mobile_drawer_should_start_closed",
      );
      await humanClick(page, toggle);
      await page.locator("[data-sidebar-toggle][aria-expanded='true']").waitFor({
        state: "visible",
        timeout: 4_000,
      });
      const touchTargets = page.locator(
        "[data-sidebar-toggle], [data-sidebar-group-toggle], [data-workspace-advanced-open]",
      );
      const targetSizes = await touchTargets.evaluateAll((elements) => elements
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        })
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return { width: rect.width, height: rect.height };
        }));
      assert.ok(targetSizes.length > 1, "mobile_touch_targets_missing");
      for (const target of targetSizes) {
        assert.ok(target.width >= 44 && target.height >= 44, `mobile_touch_target_too_small:${target.width}x${target.height}`);
      }
      await openNavigation(page, "quickstart", ["/workspace/getting-started"]);
      await page.locator(".workspace-task-home").waitFor({ state: "visible", timeout: 4_000 });
      const taskActionSizes = await page.locator(".workspace-task-row button").evaluateAll(
        (elements) => elements.map((element) => {
          const rect = element.getBoundingClientRect();
          return { width: rect.width, height: rect.height };
        }),
      );
      for (const target of taskActionSizes) {
        assert.ok(target.width >= 44 && target.height >= 44, `mobile_task_target_too_small:${target.width}x${target.height}`);
      }
      await collapseSidebar(page);
      const body = (await page.locator("body").innerText()).toLowerCase();
      for (const sensitive of ["provider key", "access token", "response body"]) {
        assert.ok(!body.includes(sensitive));
      }
      return { mobile_drawer_default_closed: true, mobile_touch_targets: targetSizes.length };
    },
  }));
  process.stdout.write(JSON.stringify({ browser: browserName, scale, journeys, all_108_prds_complete: false }));
} finally {
  await browser.close();
}
