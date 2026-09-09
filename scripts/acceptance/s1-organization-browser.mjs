/** Real browser submissions against isolated Control and Gateway servers. */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const { chromium, request } = createRequire(import.meta.url)("playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const origin = process.env.S1_ORIGIN;
const artifacts = process.env.S1_ARTIFACTS;
assert.equal(new URL(origin).hostname, "s1.localhost");
assert.equal(path.dirname(path.resolve(artifacts)), path.join(root, ".artifacts"));
const report = { status: "failed", steps: [], blockedExternalRequests: 0, pageErrors: 0, viewports: [], apiResponses: [] };
let stage = "launch";
let browser;
let currentPage;

async function step(name, operation) {
  stage = name;
  await operation();
  report.steps.push(name);
}

async function layout(page, name) {
  const facts = await page.evaluate(() => ({
    width: innerWidth,
    overflow: document.documentElement.scrollWidth > innerWidth + 1,
    minInputPx: Math.min(...[...document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select")]
      .filter((element) => element.getBoundingClientRect().width)
      .map((element) => parseFloat(getComputedStyle(element).fontSize))),
  }));
  report.viewports.push({ name, ...facts });
  assert.equal(facts.overflow, false);
  assert.ok(facts.minInputPx >= 14);
  await page.screenshot({ path: path.join(artifacts, name + ".png") });
}

try {
  browser = await chromium.launch({
    executablePath: path.join(root, ".tools/chrome-for-testing/chrome-win64/chrome.exe"),
    headless: true,
    args: ["--host-resolver-rules=MAP s1.localhost 127.0.0.1", "--no-proxy-server",
      "--disk-cache-size=1048576", "--disable-application-cache"],
  });
  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    const tag = String(viewport.width);
    const context = await browser.newContext({ viewport, serviceWorkers: "block" });
    await context.route("**/*", (route) => {
      if (new URL(route.request().url()).origin === origin) return route.continue();
      report.blockedExternalRequests += 1;
      return route.abort();
    });
    const page = await context.newPage();
    currentPage = page;
    page.setDefaultTimeout(12_000);
    page.on("pageerror", () => { report.pageErrors += 1; });
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (url.origin === origin && !url.pathname.startsWith("/src/") && !url.pathname.endsWith(".html")) {
        report.apiResponses.push({ path: url.pathname.replace(/[0-9a-f]{32,}/gu, "id"), status: response.status() });
      }
    });
    page.on("dialog", (dialog) => dialog.accept());
    const slug = "s1-browser-" + tag + "-" + randomUUID().slice(0, 8);
    const password = randomUUID() + "!Aa9";
    let firstKey;
    let rotatedKey;
    await step(tag + ".registration", async () => {
      await page.goto(origin + "/identity.html");
      await page.locator('[data-identity-action="show-register"]').click();
      const form = page.locator("[data-identity-registration-form]");
      await form.locator('button[type="submit"]:enabled').waitFor();
      await page.locator('img[alt="五位数字验证码"]').waitFor();
      await page.waitForFunction(() => document.querySelector('img[alt="五位数字验证码"]')?.naturalWidth === 180);
      assert.ok(await page.locator('img[alt="五位数字验证码"]').evaluate((image) => image.naturalWidth === 180));
      await layout(page, tag + "-registration");
      const popupReady = context.waitForEvent("page");
      await form.getByRole("link", { name: "隐私条款" }).click();
      const privacy = await popupReady;
      await privacy.waitForLoadState();
      assert.ok((await privacy.locator("body").textContent()).includes("隐私"));
      await privacy.close();
      await form.locator('[name="email"]').fill(slug + "@example.test");
      await form.locator('[name="displayName"]').fill("Browser Owner " + tag);
      await form.locator('[name="password"]').fill(password);
      await form.locator('[name="confirmPassword"]').fill(password);
      await form.getByRole("button", { name: "显示密码", exact: true }).click();
      assert.equal(await form.locator('[name="password"]').getAttribute("type"), "text");
      await form.getByRole("button", { name: "隐藏密码", exact: true }).click();
      await form.locator('[name="consent"]').check();
      await form.locator('[name="captchaAnswer"]').fill("22222");
      await form.locator('button[type="submit"]').click();
      await page.locator("#organization-start-title").waitFor();
      assert.equal(await page.locator('[data-identity-action="show-workspace-create"]').count(), 1);
      assert.equal(await page.locator('[data-identity-action="set-workspace-kind"]').count(), 0);
    });
    await step(tag + ".organization", async () => {
      await page.locator('[data-identity-action="show-workspace-create"]').click();
      const form = page.locator("[data-workspace-create-form]");
      await form.locator('[name="name"]').fill("Browser Organization " + tag);
      await form.locator('[name="slug"]').fill(slug);
      await form.locator('button[type="submit"]').click();
      await page.waitForURL("**/console.html*");
      await page.goto(origin + "/console.html#virtual-keys");
      await page.locator("#create-consumer-form").waitFor();
      const session = await context.request.get(origin + "/workspace/session");
      assert.equal(session.status(), 200);
      assert.equal((await session.json()).context.role, "owner");
      assert.ok([400, 401, 403].includes((await context.request.get(origin + "/platform/registration-policy")).status()));
      assert.equal(await page.locator("[data-platform-registration-policy-form]").count(), 0);
    });
    await step(tag + ".consumers", async () => {
      for (let index = 0; index < 2; index += 1) {
        const form = page.locator("#create-consumer-form");
        await form.locator('[name="displayName"]').fill("Employee " + (index + 1));
        await form.locator('[name="department"]').fill("QA");
        await form.locator('button[type="submit"]').click();
        await page.waitForFunction((count) => document.querySelectorAll("[data-consumer-form]").length === count, index + 1);
      }
      await layout(page, tag + "-consumers");
    });
    await step(tag + ".keys", async () => {
      for (let index = 0; index < 2; index += 1) {
        const form = page.locator("#create-virtual-key-form");
        await form.locator('[name="name"]').fill("Employee tool " + (index + 1));
        await form.locator('[name="consumerId"]').selectOption({ label: (await form.locator('[name="consumerId"] option').allTextContents())[index + 1] });
        await form.locator('button[type="submit"]').click();
        const secret = page.locator(".one-time-secret code");
        await secret.waitFor();
        if (!index) firstKey = await secret.textContent();
        else assert.notEqual(await secret.textContent(), firstKey);
        await page.locator("[data-dismiss-virtual-key-secret]").click();
      }
      assert.ok(firstKey);
      const anonymous = await request.newContext();
      try {
        const denied = await anonymous.get(origin + "/consumers", { headers: { Authorization: "Bearer " + firstKey } });
        assert.equal(denied.status(), 401);
        const valid = await anonymous.post(process.env.S1_GATEWAY_ORIGIN + "/feedback", { headers: { Authorization: "Bearer " + firstKey }, data: {} });
        assert.equal(valid.status(), 422);
      } finally { await anonymous.dispose(); }
      assert.equal(await page.evaluate((secret) => [...Object.values(localStorage), ...Object.values(sessionStorage)].some((value) => value.includes(secret)), firstKey), false);
    });
    await step(tag + ".rotation-disable", async () => {
      await page.getByRole("button", { name: "轮换 Employee tool 1", exact: true }).click();
      await page.locator(".one-time-secret code").waitFor();
      rotatedKey = await page.locator(".one-time-secret code").textContent();
      assert.notEqual(rotatedKey, firstKey);
      await page.locator("[data-dismiss-virtual-key-secret]").click();
      const anonymous = await request.newContext();
      const feedback = (key) => anonymous.post(process.env.S1_GATEWAY_ORIGIN + "/feedback", { headers: { Authorization: "Bearer " + key }, data: {} });
      try {
        assert.equal((await feedback(firstKey)).status(), 401);
        assert.equal((await feedback(rotatedKey)).status(), 422);
        const employee = page.locator("[data-consumer-form]").filter({ has: page.locator('input[value="Employee 1"]') });
        await employee.locator('[name="active"]').uncheck();
        await employee.locator('button[type="submit"]').click();
        await page.waitForFunction(() => [...document.querySelectorAll("[data-consumer-form]")].some((form) => form.elements.displayName.value === "Employee 1" && form.dataset.revision === "2"));
        assert.equal((await feedback(rotatedKey)).status(), 401);
      } finally { await anonymous.dispose(); }
      await layout(page, tag + "-completed");
    });
    await context.close();
  }
  assert.equal(report.blockedExternalRequests, 0);
  assert.equal(report.pageErrors, 0);
  report.status = "passed";
} catch (error) {
  report.failedStage = stage;
  report.failureType = error.name;
  if (currentPage && !currentPage.isClosed()) {
    report.failurePage = new URL(currentPage.url()).pathname;
    await currentPage.screenshot({ path: path.join(artifacts, "failure-redacted.png"),
      mask: [currentPage.locator("input"), currentPage.locator(".one-time-secret")] }).catch(() => {});
  }
} finally {
  await browser?.close();
  await writeFile(path.join(artifacts, "browser.json"), JSON.stringify(report, null, 2));
}
process.exitCode = report.status === "passed" ? 0 : 1;
