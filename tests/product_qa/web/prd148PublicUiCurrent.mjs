/** Current public UI smoke audit: desktop/mobile interaction and reverse-proxy subpath loading. */
import assert from "node:assert/strict";
import { createReadStream } from "node:fs";
import { mkdir, readFile, stat } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)(
  path.resolve(".tools/product-qa-browser/node_modules/playwright"),
);
const consoleRoot = path.resolve("console-web");
const distRoot = path.resolve("console-web/dist");
const currentOrigin = process.env.METAHUB_PUBLIC_QA_URL ?? "http://127.0.0.1:5173";
const artifactRoot = path.resolve(".artifacts/product-qa-current/20260924-public-ui");

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

function staticMount(root, mount) {
  const server = http.createServer(async (request, response) => {
    const requestPath = new URL(request.url ?? "/", "http://localhost").pathname;
    if (!requestPath.startsWith(`${mount}/`) && requestPath !== mount) {
      response.writeHead(404).end();
      return;
    }
    const relative = requestPath.slice(mount.length) || "/index.html";
    const candidate = path.resolve(root, `.${relative}`);
    if (!candidate.startsWith(`${root}${path.sep}`)) {
      response.writeHead(404).end();
      return;
    }
    try {
      const info = await stat(candidate);
      const file = info.isDirectory() ? path.join(candidate, "index.html") : candidate;
      response.writeHead(200, {
        "content-type": contentTypes[path.extname(file)] ?? "application/octet-stream",
        "cache-control": "no-store",
      });
      createReadStream(file).pipe(response);
    } catch {
      response.writeHead(404).end();
    }
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => {
    const address = server.address();
    resolve({ server, origin: `http://127.0.0.1:${address.port}${mount}` });
  }));
}

async function layout(page) {
  return page.evaluate(() => {
    const visible = (selector) => {
      const element = document.querySelector(selector);
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      return { top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height };
    };
    return {
      viewport: { width: innerWidth, height: innerHeight },
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: document.documentElement.clientWidth,
      hero: visible(".home-hero"),
      actions: visible(".home-hero-actions"),
      bottom: visible(".home-hero-bottom"),
      overflow: getComputedStyle(document.querySelector(".home-hero")).overflow,
    };
  });
}

async function auditHome(browser, url, label, viewport) {
  const context = await browser.newContext({ viewport, serviceWorkers: "block" });
  const page = await context.newPage();
  const staticFailures = [];
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !/status of 401/iu.test(message.text())) consoleErrors.push(message.text());
  });
  page.on("response", (response) => {
    const pathname = new URL(response.url()).pathname;
    if ((pathname.includes("/src/") || /\/(?:index|identity|status)\.html$/u.test(pathname))
      && response.status() >= 400) staticFailures.push(`${pathname}:${response.status()}`);
  });
  await page.goto(`${url}/index.html`, { waitUntil: "networkidle" });
  await page.locator("#home-title").waitFor({ state: "visible" });
  assert.equal(await page.locator(".home-header nav").count(), 1);
  assert.equal(await page.locator(".home-hero-actions a").count(), 2);
  const before = await layout(page);
  assert.equal(before.documentWidth, before.viewportWidth, `${label}: horizontal overflow`);
  assert.equal(before.overflow, "visible", `${label}: hero must not clip content`);
  assert.ok(before.hero.bottom >= before.actions.bottom, `${label}: hero action escaped hero`);
  assert.ok(before.hero.bottom >= before.bottom.bottom, `${label}: hero footer escaped hero`);
  await page.screenshot({ path: path.join(artifactRoot, `${label}-${viewport.width}x${viewport.height}.png`), fullPage: true });

  if (viewport.width <= 600) {
    const toggle = page.locator("[data-home-nav-toggle]");
    await toggle.click();
    await expectAttribute(toggle, "aria-expanded", "true");
    assert.equal(await page.locator("[data-home-mobile-nav]").isVisible(), true);
    assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("href")), "#capabilities");
    await page.keyboard.press("Escape");
    await expectAttribute(toggle, "aria-expanded", "false");
    assert.equal(await page.locator("[data-home-mobile-nav]").isVisible(), false);
    assert.equal(await page.evaluate(() => document.activeElement?.matches("[data-home-nav-toggle]")), true);
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expectAttribute(toggle, "aria-expanded", "true");
    await page.mouse.click(12, 300);
    await expectAttribute(toggle, "aria-expanded", "false");
    assert.equal(await page.evaluate(() => document.activeElement?.matches("[data-home-nav-toggle]")), true);
  }

  assert.deepEqual(staticFailures, [], `${label}: static resource failures`);
  assert.deepEqual(consoleErrors, [], `${label}: browser console errors`);
  const result = { label, viewport, layout: before, staticFailures, consoleErrors };
  await context.close();
  return result;
}

async function auditIdentity(browser, url, label, viewport) {
  const context = await browser.newContext({ viewport, serviceWorkers: "block" });
  const page = await context.newPage();
  const staticFailures = [];
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !/status of 401/iu.test(message.text())) consoleErrors.push(message.text());
  });
  page.on("response", (response) => {
    const pathname = new URL(response.url()).pathname;
    if ((pathname.includes("/src/") || /\/(?:index|identity|status)\.html$/u.test(pathname))
      && response.status() >= 400) staticFailures.push(`${pathname}:${response.status()}`);
  });
  const startedAt = Date.now();
  await page.goto(`${url}/identity.html`, { waitUntil: "domcontentloaded" });
  await page.locator("[data-identity-login-form]").waitFor({ state: "visible", timeout: 15_000 });
  const loginReadyMs = Date.now() - startedAt;
  const initialLayout = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
    form: (() => {
      const rect = document.querySelector("[data-identity-login-form]")?.getBoundingClientRect();
      return rect ? { top: rect.top, bottom: rect.bottom, width: rect.width } : null;
    })(),
  }));
  assert.equal(initialLayout.documentWidth, initialLayout.viewportWidth, `${label}: identity horizontal overflow`);
  await page.screenshot({ path: path.join(artifactRoot, `${label}-identity-${viewport.width}x${viewport.height}.png`), fullPage: true });
  const password = page.locator("[data-identity-login-form] [name=password]");
  const passwordToggle = page.locator("[data-password-toggle]");
  await passwordToggle.click();
  assert.equal(await password.getAttribute("type"), "text", `${label}: password reveal`);
  await passwordToggle.click();
  assert.equal(await password.getAttribute("type"), "password", `${label}: password conceal`);
  await page.locator("[name=email]").fill("invalid@example.test");
  await password.fill("wrong-password");
  const loginResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === "/identity/login" && response.request().method() === "POST"
  ));
  await page.locator("[data-identity-login-form] button[type=submit]").click();
  assert.equal((await loginResponse).status(), 401, `${label}: invalid login must be rejected`);
  await page.locator(".identity-error[role=alert]").waitFor({ state: "visible", timeout: 5_000 });
  const policyResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === "/identity/registration-policy"
  ));
  await page.locator("[data-identity-action='show-register']").click();
  assert.equal((await policyResponse).status(), 200, `${label}: registration policy`);
  await page.locator("[data-identity-registration-form]").waitFor({ state: "visible", timeout: 5_000 });
  assert.deepEqual(staticFailures, [], `${label}: identity static resource failures`);
  assert.deepEqual(consoleErrors, [], `${label}: identity browser console errors`);
  const result = { label, viewport, loginReadyMs, initialLayout, staticFailures, consoleErrors };
  await context.close();
  return result;
}

async function expectAttribute(locator, name, value) {
  await locator.waitFor({ state: "visible" });
  assert.equal(await locator.getAttribute(name), value, `${name} should be ${value}`);
}

await mkdir(artifactRoot, { recursive: true });
const browser = await chromium.launch({ headless: true });
let mounted;
const report = { schema: "metahub.prd148.public-ui-current.v1", passed: false, checks: [] };
try {
  report.checks.push(await auditHome(browser, currentOrigin, "local", { width: 1440, height: 900 }));
  report.checks.push(await auditHome(browser, currentOrigin, "mobile", { width: 390, height: 844 }));
  report.checks.push(await auditHome(browser, currentOrigin, "low-height", { width: 390, height: 480 }));
  report.checks.push(await auditIdentity(browser, currentOrigin, "local", { width: 1440, height: 900 }));
  report.checks.push(await auditIdentity(browser, currentOrigin, "mobile", { width: 390, height: 844 }));
  mounted = await staticMount(distRoot, "/metahub");
  report.checks.push(await auditHome(browser, mounted.origin, "subpath", { width: 390, height: 844 }));
  report.passed = true;
} finally {
  await mounted?.server.close();
  await browser.close();
}
await writeReport(report);
process.stdout.write(JSON.stringify(report));

async function writeReport(value) {
  await readFile(path.join(consoleRoot, "index.html"), "utf8");
  await import("node:fs/promises").then(({ writeFile }) => writeFile(
    path.join(artifactRoot, "report.json"),
    `${JSON.stringify(value, null, 2)}\n`,
    "utf8",
  ));
}
