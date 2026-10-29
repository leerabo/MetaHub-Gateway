/** Real-browser PRD-147 Platform migration journey against an isolated runtime. */
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium, firefox } = require(path.resolve(".tools/product-qa-browser/node_modules/playwright"));

const controlPort = Number(process.env.METAHUB_QA_CONTROL_PORT);
const consolePort = Number(process.env.METAHUB_QA_CONSOLE_PORT);
const browserName = String(process.env.METAHUB_QA_BROWSER ?? "chromium").toLowerCase();
const scale = Number(process.env.METAHUB_QA_SCALE ?? "1");
const adminEmail = String(process.env.METAHUB_QA_ADMIN_EMAIL ?? "");
const adminPassword = String(process.env.METAHUB_QA_ADMIN_PASSWORD ?? "");
const bootstrapRecoverySecret = String(
  process.env.METAHUB_QA_PLATFORM_BOOTSTRAP_RECOVERY_SECRET ?? "",
);
for (const name of [
  "METAHUB_QA_ADMIN_EMAIL",
  "METAHUB_QA_ADMIN_PASSWORD",
  "METAHUB_QA_PLATFORM_BOOTSTRAP_RECOVERY_SECRET",
]) delete process.env[name];

assert.ok(Number.isInteger(controlPort) && controlPort > 0, "control_port_invalid");
assert.ok(Number.isInteger(consolePort) && consolePort > 0, "console_port_invalid");
assert.ok(["chromium", "edge", "firefox"].includes(browserName), "browser_invalid");
assert.ok([1, 1.25, 1.5].includes(scale), "scale_invalid");
assert.ok(adminEmail.includes("@") && adminPassword.length >= 16, "admin_identity_invalid");
assert.ok(bootstrapRecoverySecret.length >= 32, "bootstrap_recovery_secret_invalid");

const consoleUrl = `http://127.0.0.1:${consolePort}`;
const controlPatterns = ["http://127.0.0.1:8001/**", "http://localhost:8001/**"];
const base32Alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

function decodeBase32(value) {
  let bits = "";
  for (const character of String(value).replace(/=+$/u, "").toUpperCase()) {
    const index = base32Alphabet.indexOf(character);
    assert.ok(index >= 0, "totp_secret_invalid");
    bits += index.toString(2).padStart(5, "0");
  }
  const bytes = [];
  for (let offset = 0; offset + 8 <= bits.length; offset += 8) {
    bytes.push(Number.parseInt(bits.slice(offset, offset + 8), 2));
  }
  return Buffer.from(bytes);
}

function totp(secret, timestamp = Date.now()) {
  const counter = BigInt(Math.floor(timestamp / 30_000));
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(counter);
  const digest = createHmac("sha1", decodeBase32(secret)).update(message).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const binary = digest.readUInt32BE(offset) & 0x7fffffff;
  return String(binary % 1_000_000).padStart(6, "0");
}

async function stableTotpAnchor(page) {
  const remaining = 30_000 - (Date.now() % 30_000);
  if (remaining < 12_000) await page.waitForTimeout(remaining + 1_000);
  return Date.now();
}

async function postWithGlobalCsrf(page, pathName, body) {
  const result = await page.evaluate(async ({ pathName, body }) => {
    const csrf = document.cookie.split("; ").find(
      (entry) => entry.startsWith("__Host-metahub-global-csrf="),
    )?.split("=").slice(1).join("=") ?? "";
    const response = await fetch(`http://127.0.0.1:8001${pathName}`, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body),
    });
    return {
      status: response.status,
      cacheControl: response.headers.get("cache-control"),
      body: await response.json(),
    };
  }, { pathName, body });
  assert.equal(result.status, 200, `${pathName}:${result.status}:${result.body?.code ?? "unknown"}`);
  assert.match(result.cacheControl ?? "", /no-store/u, `${pathName}:cache_control_missing`);
  return result.body;
}

async function layoutFacts(page, viewport) {
  await page.setViewportSize(viewport);
  await page.waitForTimeout(150);
  return page.evaluate(() => {
    const visible = [...document.querySelectorAll("button, input, select, textarea, a[href]")]
      .filter((element) => {
        const rectangle = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rectangle.width > 0 && rectangle.height > 0
          && style.visibility !== "hidden" && style.display !== "none";
      });
    const clipped = visible.filter((element) => (
      element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1
    ));
    return {
      viewport: `${innerWidth}x${innerHeight}`,
      horizontal_overflow_px: Math.max(
        0, document.documentElement.scrollWidth - document.documentElement.clientWidth,
      ),
      clipped_control_count: clipped.length,
      migration_root_count: document.querySelectorAll("[data-console-migration-root]").length,
      retired_runtime_count: document.querySelectorAll(
        "[data-competitor-comparison-root], .customer-health-panel, .partner-portal-panel, .product-analytics-steps, .identity-payment-commerce",
      ).length,
    };
  });
}

const edgeExecutable = process.env.METAHUB_QA_EDGE_PATH
  ?? "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const browser = browserName === "firefox"
  ? await firefox.launch({ headless: true })
  : await chromium.launch({
    headless: true,
    ...(browserName === "edge" ? { executablePath: edgeExecutable } : {}),
  });
const context = await browser.newContext({
  viewport: { width: 1280, height: 720 },
  deviceScaleFactor: scale,
});
const page = await context.newPage();
const consoleErrors = [];
const requestFailures = [];
page.on("console", (message) => {
  if (message.type() !== "error") return;
  const text = message.text();
  const status = text.match(/status of (\d{3})/iu)?.[1] ?? "none";
  const source = (() => {
    try { return new URL(message.location().url).pathname || "/"; } catch { return "unknown"; }
  })();
  if (status === "401" && new Set(["/identity/me", "/workspace/session", "/platform/session"]).has(source)) return;
  const detail = text.replace(/\s+/gu, " ").trim().slice(0, 240);
  consoleErrors.push(`${status}:${source}:${detail || "empty"}`);
});
page.on("requestfailed", (request) => {
  const reason = request.failure()?.errorText ?? "";
  if (/aborted/iu.test(reason)) return;
  try { requestFailures.push(`${request.method()} ${new URL(request.url()).pathname}`); }
  catch { requestFailures.push("invalid_url"); }
});
for (const pattern of controlPatterns) {
  await page.route(pattern, async (route) => {
    const routed = route.request().url()
      .replace("127.0.0.1:8001", `127.0.0.1:${controlPort}`)
      .replace("localhost:8001", `127.0.0.1:${controlPort}`);
    await route.continue({ url: routed });
  });
}

try {
  await page.goto(`${consoleUrl}/identity.html`, { waitUntil: "domcontentloaded" });
  const login = page.locator("[data-identity-login-form]");
  await login.waitFor({ state: "visible", timeout: 20_000 });
  await login.locator('[name="email"]').fill(adminEmail);
  await login.locator('[name="password"]').fill(adminPassword);
  await login.getByRole("button", { name: "登录", exact: true }).click();
  try {
    await page.locator('[data-identity-action="logout"], #logout-button').first().waitFor({ state: "visible", timeout: 25_000 });
  } catch (error) {
    const diagnostic = {
      url: page.url(),
      logout_count: await page.locator('[data-identity-action="logout"], #logout-button').count(),
      body_text: (await page.locator("body").innerText()).slice(0, 4000),
      html_head: (await page.locator("body").innerHTML()).slice(0, 12000),
    };
    console.error(JSON.stringify(diagnostic));
    throw error;
  }

  const enrollment = await postWithGlobalCsrf(page, "/identity/mfa/totp/enrollments", {
    current_password: adminPassword,
  });
  const totpAnchor = await stableTotpAnchor(page);
  const firstCode = totp(enrollment.secret, totpAnchor - 30_000);
  await postWithGlobalCsrf(page, "/identity/mfa/totp/verifications", {
    factor_id: enrollment.factor_id,
    code: firstCode,
  });
  await postWithGlobalCsrf(page, "/platform/bootstrap", {
    current_password: adminPassword,
    totp_code: totp(enrollment.secret, totpAnchor),
    recovery_secret: bootstrapRecoverySecret,
    reason_code: "initial_platform_owner",
    confirmed: true,
  });
  // Platform and Workspace are separate credential domains. The identity
  // login above may have created a Workspace session for the landing page;
  // remove it before opening the platform portal so a delayed Workspace
  // page-view beacon cannot race the Platform session boundary.
  await context.clearCookies({ name: "__Host-metahub-workspace-session" });
  await context.clearCookies({ name: "__Host-metahub-workspace-csrf" });
  // Establish the platform session from the standalone platform portal. Doing
  // this with no Workspace cookies keeps the two operator domains isolated.
  await page.goto(`${consoleUrl}/platform.html`, { waitUntil: "domcontentloaded" });
  await postWithGlobalCsrf(page, "/platform/sessions", {
    totp_code: totp(enrollment.secret, totpAnchor + 30_000),
  });
  await page.waitForFunction(
    () => document.cookie.includes("__Host-metahub-platform-csrf="),
    { timeout: 5_000 },
  );

  const statusResponses = [];
  page.on("response", (response) => {
    try {
      if (new URL(response.url()).pathname === "/platform/console-migration/status") {
        statusResponses.push(response.status());
      }
    } catch { /* Every production request is expected to be an absolute URL. */ }
  });
  // The first platform.html load was intentionally unauthenticated and
  // rendered the gate. Put the requested Platform view in the location before
  // the reload, then let the controller read both the newly issued Platform
  // cookie and the initial hash in one mount.
  await page.evaluate(() => { globalThis.location.hash = "console-migration"; });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator("[data-console-migration-root]").waitFor({ state: "visible", timeout: 25_000 });
  await page.locator(".console-migration-summary").waitFor({ state: "visible", timeout: 25_000 });
  assert.match(await page.locator("[data-console-migration-root]").innerText(), /调度合同一致/u);
  assert.equal(await page.locator("[data-platform-view].is-active").count(), 1);
  assert.equal(await page.locator('[data-platform-view="console-migration"].is-active').count(), 1);

  const responseCountBeforeRefresh = statusResponses.length;
  const refreshButton = page.locator("[data-console-migration-refresh]");
  assert.equal(await refreshButton.getAttribute("aria-label"), "刷新");
  await refreshButton.focus();
  assert.equal(
    await refreshButton.evaluate((element) => element === document.activeElement),
    true,
    "migration_refresh_focus_missing",
  );
  const refreshResponse = page.waitForResponse((response) => {
    try {
      return new URL(response.url()).pathname === "/platform/console-migration/status";
    } catch {
      return false;
    }
  }, { timeout: 10_000 });
  await refreshButton.press("Enter");
  assert.equal((await refreshResponse).status(), 200, "migration_refresh_failed");
  assert.ok(statusResponses.length > responseCountBeforeRefresh, "migration_refresh_missing");
  assert.ok(statusResponses.every((status) => status === 200), "migration_status_failed");

  const viewports = [];
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 1280, height: 720 },
    { width: 390, height: 844 },
  ]) {
    const facts = await layoutFacts(page, viewport);
    assert.equal(facts.horizontal_overflow_px, 0, `horizontal_overflow:${facts.viewport}`);
    assert.equal(facts.clipped_control_count, 0, `clipped_controls:${facts.viewport}`);
    assert.equal(facts.migration_root_count, 1, `migration_root:${facts.viewport}`);
    assert.equal(facts.retired_runtime_count, 0, `retired_runtime:${facts.viewport}`);
    viewports.push(facts);
  }
  assert.deepEqual(consoleErrors, [], `console_errors:${consoleErrors.join(",")}`);
  assert.deepEqual(requestFailures, [], `request_failures:${requestFailures.join(",")}`);
  process.stdout.write(`${JSON.stringify({
    schema: "metahub.prd147.browser.v1",
    browser: browserName,
    scale,
    status_read_count: statusResponses.length,
    status_success_count: statusResponses.filter((status) => status === 200).length,
    console_error_count: consoleErrors.length,
    request_failure_count: requestFailures.length,
    viewports,
    passed: true,
  })}\n`);
} finally {
  await context.close();
  await browser.close();
}
