/** Real browser submissions against isolated Control and Gateway servers. */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { verifyUsageAfterDisable, verifyWorkspaceFinance } from "./s2-workspace-finance-browser.mjs";

const { chromium, request } = createRequire(import.meta.url)("playwright");
const { PNG } = createRequire(import.meta.url)("pngjs");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const origin = process.env.S1_ORIGIN;
const artifacts = process.env.S1_ARTIFACTS;
const financeFlow = process.env.S1_FLOW === "finance";
assert.equal(new URL(origin).hostname, "s1.localhost");
const loopbackOrigin = new URL(origin);
loopbackOrigin.hostname = "127.0.0.1";
assert.equal(path.dirname(path.resolve(artifacts)), path.join(root, ".artifacts"));
const report = { status: "failed", steps: [], blockedExternalRequests: 0, pageErrors: 0, viewports: [], apiResponses: [], assetFailures: [], stepTimings: [], motion: [], onboardingDestinations: [] };
const pendingAssets = new Map();
const pendingRequests = new Map();
let stage = "launch";
let browser;
let currentPage;

async function browserJson(page, pathname) {
  assert.ok(pathname.startsWith("/") && !pathname.startsWith("//"));
  return page.evaluate(async (pathname) => {
    const response = await fetch(pathname, { credentials: "include", cache: "no-store", redirect: "error" });
    return { status: response.status, body: await response.json() };
  }, pathname);
}

async function step(name, operation) {
  stage = name;
  const started = performance.now();
  try {
    await operation();
    report.steps.push(name);
  } finally {
    report.stepTimings.push({ name, elapsedMs: Math.round(performance.now() - started) });
  }
}

async function layout(page, name) {
  const facts = await page.evaluate(() => {
    const minimumFont = (selector) => Math.min(...[...document.querySelectorAll(selector)]
      .filter((element) => element.getBoundingClientRect().width)
      .map((element) => parseFloat(getComputedStyle(element).fontSize)));
    return {
      width: innerWidth,
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      minInputPx: minimumFont("input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select,textarea"),
      smallInputs: [...document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select,textarea")]
        .filter((element) => element.getBoundingClientRect().width && parseFloat(getComputedStyle(element).fontSize) < 16)
        .map((element) => ({ tag: element.tagName, name: element.name, id: element.id,
          fontSize: getComputedStyle(element).fontSize })),
      minOnboardingPrimaryPx: minimumFont(".identity-onboarding-step-copy strong,.identity-onboarding-facts dd"),
      minOnboardingSecondaryPx: minimumFont(".identity-onboarding-step-copy small,.identity-onboarding-facts dt,.identity-onboarding-state"),
    };
  });
  report.viewports.push({ name, ...facts });
  assert.equal(facts.overflow, false);
  assert.ok(facts.minInputPx >= 16);
  assert.ok(facts.minOnboardingPrimaryPx >= 16);
  assert.ok(facts.minOnboardingSecondaryPx >= 14);
  await page.screenshot({ path: path.join(artifacts, name + ".png") });
}

async function verifyAccessMotion(page, width) {
  const canvas = page.locator("[data-access-scene] canvas");
  if (width < 860) {
    assert.equal(await canvas.count(), 0);
    report.motion.push({ width, hiddenCanvasAbsent: true });
    return;
  }
  await page.locator('[data-access-scene][data-rendered="true"]').waitFor();
  const bounds = await canvas.evaluate((element) => {
    const canvasBounds = element.getBoundingClientRect();
    const clip = element.closest(".identity-access-visual").getBoundingClientRect();
    const left = Math.ceil(Math.max(0, canvasBounds.left, clip.left));
    const top = Math.ceil(Math.max(0, canvasBounds.top, clip.top));
    const right = Math.floor(Math.min(innerWidth, canvasBounds.right, clip.right));
    const bottom = Math.floor(Math.min(innerHeight, canvasBounds.bottom, clip.bottom));
    return { left, top, width: right - left, height: bottom - top };
  });
  assert.ok(bounds.width > 100 && bounds.height > 100);
  const visibleCanvas = async () => {
    const screenshot = PNG.sync.read(await page.screenshot());
    const crop = new PNG({ width: bounds.width, height: bounds.height });
    PNG.bitblt(screenshot, crop, bounds.left, bounds.top, bounds.width, bounds.height, 0, 0);
    return crop;
  };
  // Negative CSS canvas coordinates must not become an invalid screenshot clip.
  const first = await visibleCanvas();
  await writeFile(path.join(artifacts, width + "-scene.png"), PNG.sync.write(first));
  await page.waitForTimeout(350);
  const next = await visibleCanvas();
  assert.equal(first.width, next.width);
  assert.equal(first.height, next.height);
  let changed = 0;
  const colors = new Set();
  for (let offset = 0; offset < first.data.length; offset += 4) {
    colors.add((first.data[offset] << 16) | (first.data[offset + 1] << 8) | first.data[offset + 2]);
    if (Math.abs(first.data[offset] - next.data[offset])
      + Math.abs(first.data[offset + 1] - next.data[offset + 1])
      + Math.abs(first.data[offset + 2] - next.data[offset + 2]) > 12) changed += 1;
  }
  const motion = { width, colors: colors.size, changedPixels: changed, bounds, toggle: false };
  report.motion.push(motion);
  assert.ok(colors.size > 100, "3D canvas must contain a rendered scene");
  assert.ok(changed > 100, "3D scene must move");
  const toggle = page.locator("[data-access-motion]");
  await toggle.click();
  assert.equal(await toggle.getAttribute("aria-pressed"), "true");
  await toggle.click();
  assert.equal(await toggle.getAttribute("aria-pressed"), "false");
  motion.toggle = true;
}

try {
  browser = await chromium.launch({
    executablePath: path.join(root, ".tools/chrome-for-testing/chrome-win64/chrome.exe"),
    headless: true,
    args: ["--host-resolver-rules=MAP s1.localhost 127.0.0.1", "--no-proxy-server",
      "--disk-cache-size=1048576", "--disable-application-cache"],
  });
  const viewports = [{ width: 1280, height: 900 }, { width: 390, height: 844 }];
  if (financeFlow) viewports.push({ width: 1440, height: 900 });
  for (const viewport of viewports) {
    const tag = String(viewport.width);
    const context = await browser.newContext({ viewport, serviceWorkers: "block" });
    context.setDefaultTimeout(12_000);
    await context.route("**/*", (route) => {
      if (new URL(route.request().url()).origin === origin) return route.continue();
      report.blockedExternalRequests += 1;
      return route.abort();
    });
    const page = await context.newPage();
    currentPage = page;
    page.setDefaultTimeout(12_000);
    page.on("pageerror", () => { report.pageErrors += 1; });
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.origin === origin) pendingRequests.set(request, {
        path: url.pathname.replace(/[0-9a-f]{32,}/gu, "id"),
        started: performance.now(),
      });
      if (url.origin === origin && url.pathname.startsWith("/src/")) {
        pendingAssets.set(request, { path: url.pathname, started: performance.now() });
      }
    });
    page.on("requestfinished", (request) => {
      pendingAssets.delete(request);
      pendingRequests.delete(request);
    });
    page.on("requestfailed", (request) => {
      const asset = pendingAssets.get(request);
      if (asset) report.assetFailures.push({ path: asset.path, type: "requestfailed" });
      pendingAssets.delete(request);
      pendingRequests.delete(request);
    });
    page.on("response", async (response) => {
      const url = new URL(response.url());
      if (url.origin === origin && !url.pathname.startsWith("/src/") && !url.pathname.endsWith(".html")) {
        const entry = { path: url.pathname.replace(/[0-9a-f]{32,}/gu, "id"), status: response.status() };
        report.apiResponses.push(entry);
        if (response.status() >= 400) {
          const body = await response.json().catch(() => null);
          if (/^[a-z][a-z0-9_.]{0,100}$/u.test(body?.code ?? "")) entry.code = body.code;
        }
      }
      if (url.origin === origin && url.pathname.startsWith("/src/") && !response.ok()) {
        report.assetFailures.push({ path: url.pathname, status: response.status() });
      }
    });
    page.on("dialog", (dialog) => dialog.accept());
    const slug = "s1-browser-" + tag + "-" + randomUUID().slice(0, 8);
    const password = randomUUID() + "!Aa9";
    let firstKey;
    let rotatedKey;
    let usageFixture;
    await step(tag + ".registration", async () => {
      await page.goto(origin + "/identity.html");
      await page.locator('[data-identity-action="show-register"]').click();
      const form = page.locator("[data-identity-registration-form]");
      await form.locator('button[type="submit"]:enabled').waitFor();
      await page.locator('img[alt="五位数字验证码"]').waitFor();
      await page.waitForFunction(() => document.querySelector('img[alt="五位数字验证码"]')?.naturalWidth === 180);
      assert.ok(await page.locator('img[alt="五位数字验证码"]').evaluate((image) => image.naturalWidth === 180));
      if (!financeFlow) await verifyAccessMotion(page, viewport.width);
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
      const session = await browserJson(page, "/workspace/session");
      assert.equal(session.status, 200);
      assert.equal(session.body.context.role, "owner");
      assert.ok([400, 401, 403].includes((await browserJson(page, "/platform/registration-policy")).status));
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
        const denied = await anonymous.get(new URL("/consumers", loopbackOrigin).href,
          { headers: { Authorization: "Bearer " + firstKey } });
        assert.equal(denied.status(), 401);
        const valid = await anonymous.post(process.env.S1_GATEWAY_ORIGIN + "/feedback", { headers: { Authorization: "Bearer " + firstKey }, data: {} });
        assert.equal(valid.status(), 422);
      } finally { await anonymous.dispose(); }
      assert.equal(await page.evaluate((secret) => [...Object.values(localStorage), ...Object.values(sessionStorage)].some((value) => value.includes(secret)), firstKey), false);
    });
    if (financeFlow) {
      usageFixture = await verifyWorkspaceFinance({ page, origin, tag, report, step, layout, browserJson });
    }
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
    if (financeFlow) {
      await step(tag + ".finance-historical-attribution", () => verifyUsageAfterDisable({
        page, origin, fixture: usageFixture, browserJson,
      }));
    }
    if (!financeFlow) await step(tag + ".onboarding-navigation", async () => {
      await page.goto(origin + "/identity.html");
      const panel = page.locator("[data-onboarding-step-panel]");
      await panel.waitFor();
      for (const index of [0, 1]) {
        await page.locator(`[data-onboarding-step-index="${index}"]`).click();
        await panel.locator('[data-identity-action="confirm-onboarding-step"]').click();
        await panel.locator('[data-identity-action="reopen-onboarding-step"]:enabled').waitFor();
      }
      const destinations = [
        ["tenant", "#workspace-profile-form"], ["environments", "#create-environment-form"],
        ["providers", "#providers-title"], ["models", "#create-model-form"],
        ["model-aliases", "#create-model-alias-form"], ["projects", "#create-project-form"],
        ["virtual-keys", "#create-consumer-form"], ["sdk-examples", "#sdk-examples-title"],
      ];
      for (const [index, [sourceId, ready]] of destinations.entries()) {
        await page.locator(`[data-onboarding-step-index="${index}"]`).click();
        const link = panel.locator(".identity-onboarding-link");
        assert.equal(await link.getAttribute("href"), "/console.html#" + sourceId);
        const opened = context.waitForEvent("page");
        await link.click({ modifiers: ["Control"] });
        const destination = await opened;
        currentPage = destination;
        stage = tag + ".onboarding-navigation." + sourceId;
        const check = { width: viewport.width, sourceId, status: "opened", responses: [] };
        report.onboardingDestinations.push(check);
        destination.on("pageerror", () => { report.pageErrors += 1; });
        destination.on("response", (response) => {
          const url = new URL(response.url());
          if (url.origin === origin && !url.pathname.startsWith("/src/") && !url.pathname.endsWith(".html")) {
            check.responses.push({ path: url.pathname.replace(/[0-9a-f]{32,}/gu, "id"), status: response.status() });
          }
        });
        await destination.waitForURL(origin + "/console.html#" + sourceId);
        await destination.locator(ready).waitFor();
        if (sourceId === "tenant") {
          const name = "Profile saved " + tag;
          const form = destination.locator(ready);
          await form.locator('[name="workspaceName"]').fill(name);
          const saved = destination.waitForResponse((response) => response.request().method() === "PATCH"
            && new URL(response.url()).pathname.startsWith("/workspaces/"));
          const reentered = destination.waitForResponse((response) => response.request().method() === "POST"
            && new URL(response.url()).pathname === "/identity/workspace-switches");
          await form.locator('button[type="submit"]').click();
          assert.equal((await saved).status(), 200);
          assert.equal((await reentered).status(), 200);
          await destination.waitForFunction((expected) => {
            const input = document.querySelector('#workspace-profile-form [name="workspaceName"]');
            return input?.value === expected && !input.disabled;
          }, name);
          const session = await browserJson(destination, "/workspace/session");
          assert.equal(session.status, 200);
          assert.equal(session.body.context.name, name);
          await destination.reload();
          await destination.locator(ready).waitFor();
          assert.equal(await form.locator('[name="workspaceName"]').inputValue(), name);
          await layout(destination, tag + "-profile-saved");
          check.profileSaved = true;
          await page.reload();
          await panel.waitFor();
        }
        if (sourceId === "sdk-examples") {
          check.downloads = [];
          for (const language of ["python", "javascript"]) {
            await destination.locator(`[data-sdk-language-tab="${language}"]`).click();
            const received = destination.waitForEvent("download");
            await destination.locator(`[data-download-sdk-language="${language}"]`).click();
            const download = await received;
            assert.equal(await download.failure(), null);
            const filename = download.suggestedFilename();
            assert.match(filename, new RegExp(`^metahub-${language}-examples(?:-[0-9.]+)?\\.zip$`));
            const stream = await download.createReadStream();
            const chunks = [];
            for await (const chunk of stream) chunks.push(chunk);
            const archive = Buffer.concat(chunks);
            assert.ok(archive.length > 100);
            assert.equal(archive.subarray(0, 4).toString("hex"), "504b0304");
            check.downloads.push({ language, filename, bytes: archive.length, zipSignature: true });
          }
        }
        assert.equal(await destination.locator('[role="alert"]').count(), 0);
        check.status = "passed";
        await destination.close();
        currentPage = page;
      }
      await layout(page, tag + "-onboarding");
    });
    await context.close();
  }
  assert.equal(report.blockedExternalRequests, 0);
  assert.equal(report.pageErrors, 0);
  assert.equal(report.assetFailures.length, 0);
  assert.equal(report.apiResponses.filter((response) => response.status >= 500).length, 0);
  assert.equal(report.onboardingDestinations.flatMap((item) => item.responses)
    .filter((response) => response.status >= 500).length, 0);
  report.status = "passed";
} catch (error) {
  report.failedStage = stage;
  report.failureType = error.name;
  report.connectionFailures = ["ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED", "ECONNRESET",
    "ETIMEDOUT", "socket hang up", "Timeout", "Target closed", "strict mode violation"]
    .filter((reason) => String(error.message).includes(reason));
  report.actionability = ["element is not stable", "element is not visible",
    "element is not enabled", "intercepts pointer events", "waiting for locator",
    "waiting for scheduled navigations", "waiting until \"load\""].filter((reason) =>
    String(error.message).includes(reason));
  report.failureLocation = String(error.stack).split("\n")
    .filter((line) => /s[12]-[a-z-]+\.mjs:\d+:\d+/u.test(line))
    .map((line) => line.match(/s[12]-[a-z-]+\.mjs:\d+:\d+/u)[0]);
  report.pendingAssets = [...pendingAssets.values()].map(({ path, started }) => ({
    path, elapsedMs: Math.round(performance.now() - started),
  }));
  report.pendingRequests = [...pendingRequests.values()].map(({ path, started }) => ({
    path, elapsedMs: Math.round(performance.now() - started),
  }));
  if (currentPage && !currentPage.isClosed()) {
    report.failurePage = new URL(currentPage.url()).pathname;
    report.documentState = await currentPage.evaluate(() => ({
      readyState: document.readyState,
      appChildren: document.querySelector("#app")?.childElementCount ?? null,
      scripts: [...document.scripts].map((script) => new URL(script.src).pathname),
    })).catch(() => null);
    await currentPage.screenshot({ path: path.join(artifacts, "failure-redacted.png"),
      mask: [currentPage.locator("input,textarea"), currentPage.locator(".one-time-secret")] }).catch(() => {});
  }
} finally {
  await browser?.close();
  await writeFile(path.join(artifacts, "browser.json"), JSON.stringify(report, null, 2));
}
process.exitCode = report.status === "passed" ? 0 : 1;
