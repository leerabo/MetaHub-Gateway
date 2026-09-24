/** Positive seeded Partner and Device journeys on the current migration head. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import { mkdir } from "node:fs/promises";
import { measureInteractiveLayout } from "./layoutMetrics.mjs";

const { chromium } = createRequire(import.meta.url)(path.resolve(".tools/product-qa-browser/node_modules/playwright"));
const controlPort = Number(process.env.METAHUB_QA_CONTROL_PORT);
const consolePort = Number(process.env.METAHUB_QA_CONSOLE_PORT);
assert.ok(controlPort > 0 && consolePort > 0);
const origin = `http://127.0.0.1:${consolePort}`;
const artifactRoot = path.resolve(process.env.METAHUB_QA_ARTIFACT_ROOT, "positive-domain-screenshots");
await mkdir(artifactRoot, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
const page = await context.newPage();
const responseErrors = [];
const requestFailures = [];
const consoleErrors = [];
page.on("response", (response) => {
  if (response.status() >= 400) responseErrors.push({
    status: response.status(),
    method: response.request().method(),
    path: new URL(response.url()).pathname,
  });
});
page.on("requestfailed", (request) => requestFailures.push({
  method: request.method(),
  path: new URL(request.url()).pathname,
  error: request.failure()?.errorText ?? null,
}));
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push({ text: message.text(), location: message.location().url || null });
});
for (const host of ["127.0.0.1", "localhost"]) {
  await page.route(`http://${host}:8001/**`, (route) => route.continue({
    url: route.request().url().replace(`${host}:8001`, `127.0.0.1:${controlPort}`),
  }));
}

const result = {
  schema: "metahub.prd148.positive-domains.v1",
  scope: "Disposable seeded current-migration-head data; no live commercial or GA acceptance",
  device: {},
  partner: {},
  expected_response_errors: [],
  expected_request_cancellations: [],
  unexpected_response_errors: [],
  unexpected_request_failures: [],
  unexpected_console_errors: [],
  passed: false,
};

try {
  await page.goto(`${origin}/identity.html`, { waitUntil: "domcontentloaded" });
  const login = page.locator("[data-identity-login-form]");
  await login.waitFor({ state: "visible", timeout: 20000 });
  await login.locator("[name=email]").fill(process.env.METAHUB_QA_ADMIN_EMAIL);
  await login.locator("[name=password]").fill(process.env.METAHUB_QA_ADMIN_PASSWORD);
  await login.locator("button[type=submit]").click();
  await page.waitForURL(/console\.html(?:[?#]|$)/u, { timeout: 25000 });

  await page.goto(`${origin}/console.html#workspace.gateway.private-deployments`, { waitUntil: "domcontentloaded" });
  const deviceRoot = page.locator("[data-private-gateway-root]");
  await deviceRoot.waitFor({ state: "visible", timeout: 20000 });
  await page.getByRole("button", { name: /^(Create deployment|创建部署)$/u }).click();
  const create = page.locator("[data-private-gateway-create]");
  await create.waitFor({ state: "visible", timeout: 10000 });
  const registrationResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === "/edge/registrations" && response.request().method() === "POST"
  ), { timeout: 15000 });
  await create.locator("[name=display_name]").fill("QA Browser Device");
  await create.locator("[name=region_code]").fill("cn-east-1");
  await create.locator("button[type=submit]").click();
  const registration = await registrationResponse;
  assert.equal(registration.status(), 201);
  await page.locator("[data-private-gateway-one-use]").waitFor({ state: "visible", timeout: 10000 });
  result.device.registration_status = registration.status();
  result.device.one_time_material_visible = true;
  await page.locator("[data-private-gateway-close]").first().click();
  await page.reload({ waitUntil: "domcontentloaded" });
  await deviceRoot.waitFor({ state: "visible", timeout: 15000 });
  const deviceText = await page.locator("main.console-main").innerText();
  assert.match(deviceText, /QA Browser Device/u);
  result.device.deployment_visible_after_reload = true;
  const installerRequirements = deviceRoot.locator("[data-private-gateway-installer-requirements]");
  assert.equal(await installerRequirements.count(), 1);
  assert.equal(await installerRequirements.getAttribute("open"), null, "environment requirements start collapsed");
  await installerRequirements.locator("summary").click();
  assert.equal(await installerRequirements.getAttribute("open"), "", "operator can inspect environment requirements");
  await installerRequirements.locator("summary").click();
  assert.equal(await installerRequirements.getAttribute("open"), null, "operator can return to the compact installer");
  result.device.installer_requirements_toggle = true;
  const operationGroups = deviceRoot.locator("[data-private-gateway-operation-group]");
  assert.equal(await operationGroups.count(), 2);
  for (const group of await operationGroups.all()) {
    assert.equal(await group.getAttribute("open"), null, "low-frequency operations start collapsed");
    await group.locator("summary").click();
    assert.equal(await group.getAttribute("open"), "", "operator can inspect an operation group");
    await group.locator("summary").click();
    assert.equal(await group.getAttribute("open"), null, "operator can return to the compact first-use view");
  }
  result.device.operation_groups_toggle = true;
  result.device.layout = {
    ...(await page.evaluate(measureInteractiveLayout)),
    overflowPx: await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)),
  };
  assert.equal(result.device.layout.overflowPx, 0);
  assert.equal(result.device.layout.clipped_control_count, 0);
  assert.equal(result.device.layout.interactive_overlap_count, 0);
  await page.screenshot({ path: path.join(artifactRoot, "admin-device-positive.png"), fullPage: true });

  await context.clearCookies({ name: "__Host-metahub-workspace-session" });
  await context.clearCookies({ name: "__Host-metahub-workspace-csrf" });
  const partnerResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === "/partner/workspaces"
  ), { timeout: 15000 });
  await page.goto(`${origin}/partner.html`, { waitUntil: "domcontentloaded" });
  const partner = await partnerResponse;
  assert.equal(partner.status(), 200);
  await page.getByRole("heading", { name: "Attributed workspaces", exact: true }).waitFor({ state: "visible", timeout: 15000 });
  const partnerText = await page.locator("#partner-app").innerText();
  assert.match(partnerText, /QA Browser Partner/u);
  assert.match(partnerText, /Workspace/u);
  result.partner.response_status = partner.status();
  result.partner.membership_projection_visible = true;
  result.partner.layout = {
    ...(await page.evaluate(measureInteractiveLayout)),
    overflowPx: await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)),
  };
  assert.equal(result.partner.layout.overflowPx, 0);
  assert.equal(result.partner.layout.clipped_control_count, 0);
  assert.equal(result.partner.layout.interactive_overlap_count, 0);
  await page.screenshot({ path: path.join(artifactRoot, "admin-partner-positive.png"), fullPage: true });
  const expectedResponse = responseErrors.filter((item) => (
    (item.status === 404 && item.method === "GET" && /^\/edge\/licenses\/edp_[0-9a-f]{32}$/u.test(item.path))
    || (item.status === 401 && item.method === "GET" && ["/identity/me", "/workspace/session"].includes(item.path))
  ));
  const expectedRequests = requestFailures.filter((item) => (
    item.error === "net::ERR_ABORTED"
    && ((item.method === "GET" && item.path === "/identity/me")
      || (item.method === "POST" && item.path === "/workspace/navigation/page-views"))
  ));
  const expectedConsole = consoleErrors.filter((item) => (
    item.location?.endsWith("/identity/me")
    || item.location?.endsWith("/workspace/session")
    || /\/edge\/licenses\/edp_[0-9a-f]{32}$/u.test(item.location ?? "")
  ));
  result.expected_response_errors = expectedResponse;
  result.expected_request_cancellations = expectedRequests;
  result.unexpected_response_errors = responseErrors.filter((item) => !expectedResponse.includes(item));
  result.unexpected_request_failures = requestFailures.filter((item) => !expectedRequests.includes(item));
  result.unexpected_console_errors = consoleErrors.filter((item) => !expectedConsole.includes(item));
  result.passed = result.unexpected_response_errors.length === 0
    && result.unexpected_request_failures.length === 0
    && result.unexpected_console_errors.length === 0;
} finally {
  result.response_errors = responseErrors;
  result.request_failures = requestFailures;
  result.console_errors = consoleErrors;
  await context.close();
  await browser.close();
}

process.stdout.write(JSON.stringify(result));
