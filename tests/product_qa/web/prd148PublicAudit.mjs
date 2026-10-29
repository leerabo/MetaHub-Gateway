/** Anonymous public entry, login, registration and recovery surface audit. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";

const { chromium } = createRequire(import.meta.url)(
  path.resolve(".tools/product-qa-browser/node_modules/playwright"),
);
const controlPort = Number(process.env.METAHUB_QA_CONTROL_PORT);
const gatewayPort = Number(process.env.METAHUB_QA_GATEWAY_PORT);
const consolePort = Number(process.env.METAHUB_QA_CONSOLE_PORT);
const origin = `http://127.0.0.1:${consolePort}`;
assert.ok(controlPort > 0 && gatewayPort > 0 && consolePort > 0);

function routeLocal(page) {
  for (const [from, to] of [[8001, controlPort], [8000, gatewayPort]]) {
    page.route(`http://127.0.0.1:${from}/**`, (route) => route.continue({
      url: route.request().url().replace(`127.0.0.1:${from}`, `127.0.0.1:${to}`),
    }));
    page.route(`http://localhost:${from}/**`, (route) => route.continue({
      url: route.request().url().replace(`localhost:${from}`, `127.0.0.1:${to}`),
    }));
  }
}

async function layout(page) {
  return page.evaluate(() => {
    const controls = [...document.querySelectorAll("button, input, select, textarea, a[href]")]
      .filter((element) => {
        const r = element.getBoundingClientRect();
        const s = getComputedStyle(element);
        return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
      });
    return {
      overflowPx: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
      controls: controls.length,
      clipped: controls.filter((element) => element.scrollWidth > element.clientWidth + 1
        || element.scrollHeight > element.clientHeight + 1).length,
      text: document.body.innerText.replace(/\s+/gu, " ").trim().slice(0, 260),
    };
  });
}

const browser = await chromium.launch({ headless: true });
const report = { schema: "metahub.prd148.public-audit.v1", viewports: [], passed: false };
try {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 720 },
    { width: 390, height: 844 }, { width: 320, height: 568 }]) {
    const context = await browser.newContext({ viewport, serviceWorkers: "block" });
    const page = await context.newPage();
    const errors = [];
    const responseErrors = [];
    page.on("console", (message) => {
      if (message.type() !== "error" || /status of 401/iu.test(message.text())) return;
      errors.push(message.text());
    });
    page.on("response", (response) => {
      if (response.status() >= 500) responseErrors.push(`${response.request().method()} ${new URL(response.url()).pathname}:${response.status()}`);
    });
    routeLocal(page);
    await page.goto(`${origin}/`, { waitUntil: "domcontentloaded" });
    await page.locator("[data-gateway-scene]").waitFor({ timeout: 12_000 });
    await page.waitForTimeout(250);
    const home = await layout(page);
    await page.goto(`${origin}/identity.html`, { waitUntil: "domcontentloaded" });
    const login = page.locator("[data-identity-login-form]");
    await login.waitFor({ state: "visible", timeout: 12_000 });
    const loginLayout = await layout(page);
    await login.locator("[name=email]").fill("invalid@example.test");
    await login.locator("[name=password]").fill("wrong-password");
    const loginResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname === "/identity/login" && response.request().method() === "POST");
    await login.locator("button[type=submit]").click();
    assert.equal((await loginResponse).status(), 401, "invalid credentials must be rejected");
    await page.locator(".identity-error[role=alert]").waitFor({ state: "visible" });
    const loginErrorVisible = await page.locator(".identity-error[role=alert]").isVisible();
    const policyResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname === "/identity/registration-policy");
    await page.locator("[data-identity-action='show-register']").click();
    const policy = await policyResponse;
    assert.equal(policy.status(), 200, "registration policy must load successfully");
    const policyFacts = await policy.json();
    const registration = page.locator("[data-identity-registration-form]");
    await registration.waitFor({ state: "visible", timeout: 12_000 });
    await page.waitForFunction(
      () => !document.querySelector("[data-identity-registration-form] .identity-setup-status"),
      undefined, { timeout: 12_000 },
    );
    const captcha = page.locator("img[alt='五位数字验证码']");
    const captchaVisible = await captcha.count() > 0;
    const captchaWidth = captchaVisible ? await captcha.evaluate((element) => element.naturalWidth) : 0;
    const registrationLayout = await layout(page);
    const registrationText = await registration.innerText();
    // The registration policy must finish loading before this audit decides
    // whether the isolated runtime deliberately disables CAPTCHA. A transient
    // "preparing" state is a fixture failure, not evidence of a valid policy.
    const captchaInputCount = await registration.locator("input[name=captchaAnswer]").count();
    const captchaPolicyDisabled = policyFacts.human_verification === false && !captchaVisible
      && !/本地注册验证|准备中/u.test(registrationText)
      && captchaInputCount === 0;
    if (/本地注册验证|准备中/u.test(registrationText)) {
      throw new Error("registration_policy_not_ready");
    }
    await page.goto(`${origin}/identity.html`, { waitUntil: "domcontentloaded" });
    await login.waitFor({ state: "visible", timeout: 12_000 });
    await page.locator("[data-identity-action='show-reset']").click();
    const recovery = await layout(page);
    report.viewports.push({ viewport: `${viewport.width}x${viewport.height}`, home, login: loginLayout,
      registration: registrationLayout, recovery, captchaVisible, captchaWidth, captchaPolicyDisabled, loginErrorVisible,
      consoleErrorCount: errors.length, responseErrors });
    await context.close();
  }
  report.passed = report.viewports.every((item) => item.home.overflowPx === 0
    && item.login.overflowPx === 0 && item.registration.overflowPx === 0 && item.recovery.overflowPx === 0
    && item.home.clipped === 0 && item.login.clipped === 0 && item.registration.clipped === 0
    && item.recovery.clipped === 0 && ((item.captchaVisible && item.captchaWidth === 180)
      || item.captchaPolicyDisabled)
    && item.loginErrorVisible && item.consoleErrorCount === 0 && item.responseErrors.length === 0);
  process.stdout.write(JSON.stringify(report));
} finally {
  await browser.close();
}
