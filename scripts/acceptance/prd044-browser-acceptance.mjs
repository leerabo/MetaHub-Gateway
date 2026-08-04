/**
 * PRD-044 策略引擎 DSL 最小版真实浏览器验收。
 *
 * 脚本通过公开 API 准备临时角色账号，再用真实 Console 完成管理员策略创建、校验、
 * 更新和三档响应式检查。密码与 Token 只驻留当前进程内存，不进入源码、截图或输出。
 */

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);
// Playwright 由工作区运行时提供，避免把浏览器依赖写入 Console 生产包。
const { chromium } = require("playwright");

// 服务地址、Chrome 和产物目录允许 CI 覆盖，本地默认值与开发环境保持一致。
const CONSOLE_URL = process.env.METAHUB_CONSOLE_URL ?? "http://localhost:5173/#policies";
const CONTROL_API_URL = process.env.METAHUB_CONTROL_API_URL ?? "http://localhost:8001";
const CHROME_PATH = process.env.METAHUB_CHROME_PATH
  ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const TENANT_SLUG = process.env.METAHUB_TENANT_SLUG ?? "default";
const ARTIFACT_DIR = path.resolve(process.env.METAHUB_PRD044_ARTIFACT_DIR ?? "artifacts/prd044");

/**
 * 读取必需环境变量。
 *
 * 参数：name 为环境变量名。
 * 返回：去除首尾空白后的值。
 * 安全：缺失时立即失败，不能在验收脚本中保存管理员弱口令。
 */
function requiredEnvironment(name) {
  const value = String(process.env[name] ?? "").trim();
  assert.ok(value, `缺少必需环境变量 ${name}`);
  return value;
}

/**
 * 调用控制面 JSON API 并校验预期状态。
 *
 * 参数：pathname、请求选项和 expectedStatus 为公开 API 合同。
 * 返回：解析后的 JSON；不会输出 Authorization、密码或响应正文。
 */
async function controlJson(pathname, options, expectedStatus) {
  const response = await fetch(`${CONTROL_API_URL}${pathname}`, options);
  const body = await response.json();
  assert.equal(response.status, expectedStatus, `${pathname} 返回 HTTP ${response.status}`);
  return { body, headers: response.headers };
}

/**
 * 为浏览器权限验收准备 admin/developer/viewer 账号。
 *
 * 返回：三个只在当前 Node 进程使用的凭据对象和安全后缀。
 * 权限：developer/viewer 由真实 admin `/users` 创建，不能直接写库或伪造前端角色。
 */
async function prepareAccounts() {
  const admin = {
    email: process.env.METAHUB_ADMIN_EMAIL ?? "admin@metahub.local",
    password: requiredEnvironment("METAHUB_ADMIN_PASSWORD"),
  };
  const login = await controlJson(
    "/auth/login",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_slug: TENANT_SLUG,
        email: admin.email,
        password: admin.password,
      }),
    },
    200,
  );
  const token = String(login.body.access_token ?? "");
  assert.ok(token, "admin 登录响应缺少 access_token");
  const suffix = randomBytes(5).toString("hex");
  const accounts = { admin };
  for (const role of ["developer", "viewer"]) {
    const account = {
      email: `prd044-browser-${role}-${suffix}@metahub.local`,
      password: `Prd044!${randomBytes(18).toString("base64url")}`,
    };
    await controlJson(
      "/users",
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json",
          "X-Request-ID": `req_prd044_browser_user_${role}_${suffix}`,
        },
        body: JSON.stringify({
          email: account.email,
          display_name: `PRD044 ${role}`,
          password: account.password,
          role,
        }),
      },
      201,
    );
    accounts[role] = account;
  }
  return { accounts, suffix };
}

/**
 * 收集页面脚本、控制台和非成功 HTTP 响应。
 *
 * 参数：page 为当前 Playwright 页面。
 * 返回：仅含公开路径和固定错误文本的数组，不读取请求头、Token 或正文。
 */
function captureBrowserErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    // 只把 error 级浏览器消息视为验收失败，普通调试输出不能造成页面假失败。
    if (message.type() === "error") {
      errors.push(`console: ${message.text()}`);
    }
  });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      const url = new URL(response.url());
      errors.push(`http ${response.status()}: ${url.pathname}${url.search}`);
    }
  });
  return errors;
}

/**
 * 使用真实登录表单进入控制台。
 *
 * 参数：page、内存账号、预期角色和目标 URL。
 * 返回：登录提交前的 hash，用于证明非 admin 的越权直达确实被尝试。
 */
async function login(page, account, expectedRole, targetUrl = CONSOLE_URL) {
  await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
  const requestedHash = await page.evaluate(() => window.location.hash);
  await page.locator("#login-form").waitFor({ state: "visible" });
  await page.locator('[name="tenantSlug"]').fill(TENANT_SLUG);
  await page.locator('[name="email"]').fill(account.email);
  await page.locator('[name="password"]').fill(account.password);
  await page.locator('#login-form button[type="submit"]').click();
  await page.waitForFunction(
    () => document.querySelector(".console-shell") || document.querySelector(".login-error"),
    undefined,
    { timeout: 30_000 },
  );
  // 登录失败只读取页面已经脱敏的统一错误，严禁把提交的账号或密码拼入异常。
  if (await page.locator(".login-error").count()) {
    // 抛出的内容来自安全展示层，便于定位 request_id 且不泄露认证请求体。
    throw new Error(`角色登录失败：${await page.locator(".login-error").innerText()}`);
  }
  const userMenu = page.locator(".user-menu");
  await userMenu.waitFor({ state: "visible", timeout: 30_000 });
  assert.match(await userMenu.innerText(), new RegExp(`\\b${expectedRole}\\b`));
  return { requestedHash };
}

/**
 * 等待控制台异步刷新替换 DOM 后稳定。
 *
 * 参数：page 为当前页面。
 * 返回：无；超时的 networkidle 会降级为短暂稳定等待，避免周期刷新造成假失败。
 */
async function waitForConsoleStability(page) {
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined);
  await page.waitForTimeout(600);
}

/**
 * 打开 admin-only Policy 页面。
 *
 * 参数：page 为已登录管理员页面。
 * 返回：无；完成时 `.policies-panel` 和编辑表单属于当前最新 DOM。
 */
async function openPolicies(page) {
  const shell = page.locator(".console-shell");
  await shell.waitFor({ state: "visible" });
  if (await shell.getAttribute("data-active-navigation") !== "policies") {
    const button = page.locator('.sidebar-link[data-console-nav="policies"]');
    await button.waitFor({ state: "visible" });
    await button.click();
    await page.waitForFunction(
      () => document.querySelector(".console-shell")?.dataset.activeNavigation === "policies",
      undefined,
      { timeout: 30_000 },
    );
  }
  await page.locator(".policies-panel #policy-editor-form").waitFor({ state: "visible" });
  await waitForConsoleStability(page);
}

/**
 * 填写当前 Policy 编辑器。
 *
 * 参数：page 与草稿字段；route_to 时自动选择首个后端授权的 active Model。
 * 返回：所选 target_model_id，非 route_to 返回 null。
 */
async function fillPolicyDraft(page, draft) {
  await page.locator('#policy-editor-form [name="name"]').fill(draft.name);
  await page.locator('#policy-editor-form [name="status"]').selectOption(draft.status ?? "active");
  await page.locator('#policy-editor-form [name="priority"]').fill(String(draft.priority));
  await page.locator('#policy-editor-form [name="description"]').fill(draft.description ?? "");
  await page.locator('#policy-editor-form [name="conditionExpr"]').fill(draft.conditionExpr);
  await page.locator('#policy-editor-form [name="actionType"]').selectOption(draft.actionType);
  // 非 route_to 动作不得触碰隐藏模型字段，避免浏览器误提交陈旧 target_model_id。
  if (draft.actionType !== "route_to") {
    return null;
  }
  const target = page.locator('#policy-editor-form [name="targetModelId"]');
  await target.waitFor({ state: "visible" });
  const value = await target.locator('option:not([value=""])').first().getAttribute("value");
  assert.ok(value, "route_to 编辑器没有当前租户 active Model");
  await target.selectOption(value);
  return value;
}

/**
 * 点击校验并等待真实 `/policies/validate` 响应。
 *
 * 参数：page 与期望 valid 状态。
 * 返回：HTTP 状态；领域错误也必须使用 200 + valid=false。
 */
async function validateDraft(page, expectedValid) {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/policies/validate")
      && response.request().method() === "POST",
    { timeout: 30_000 },
  );
  await page.locator("[data-policy-validate]").click();
  const response = await responsePromise;
  assert.equal(response.status(), 200, "浏览器校验没有返回 HTTP 200");
  const selector = expectedValid ? ".policy-validation-passed" : ".policy-validation-failed";
  await page.locator(selector).waitFor({ state: "visible", timeout: 30_000 });
  assert.equal(await page.locator("[data-policy-save]").isEnabled(), expectedValid);
  return response.status();
}

/**
 * 保存当前草稿并校验方法、状态和 ETag。
 *
 * 参数：page、POST/PATCH、期望 HTTP 和 ETag。
 * 返回：响应状态；保存按钮必须在后端校验成功后才可点击。
 */
async function saveDraft(page, method, expectedStatus, expectedEtag) {
  const responsePromise = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return url.pathname.startsWith("/policies") && response.request().method() === method;
    },
    { timeout: 30_000 },
  );
  await page.locator("[data-policy-save]").click();
  const response = await responsePromise;
  assert.equal(response.status(), expectedStatus);
  assert.equal(await response.headerValue("etag"), expectedEtag);
  return response.status();
}

/**
 * 校验 Policy 页面根节点、关键区块和按钮文字没有越界。
 *
 * 参数：page 与期望视口宽度。
 * 返回：可写入验收记录的布局摘要。
 */
async function assertPolicyLayout(page, viewportWidth) {
  const layout = await page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    const selectors = [
      ".policies-panel",
      ".policy-heading",
      ".policy-editor",
      "#policy-editor-form",
      ".policy-editor-actions",
      ".policy-validation",
      ".policy-filters",
      ".policy-table",
      ".policy-row",
    ];
    const offenders = [...document.querySelectorAll(selectors.join(","))]
      .filter((element) => {
        const rectangle = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return style.display !== "none"
          && rectangle.width > 0
          && (rectangle.left < -1 || rectangle.right > root.clientWidth + 1);
      })
      .map((element) => ({
        selector: element.id ? `#${element.id}` : `.${element.classList[0]}`,
        left: Math.round(element.getBoundingClientRect().left),
        right: Math.round(element.getBoundingClientRect().right),
      }));
    const clippedButtons = [...document.querySelectorAll(".policies-panel button")]
      .filter((button) => button.offsetParent && button.scrollWidth > button.clientWidth + 1)
      .map((button) => button.textContent?.trim() ?? "");
    return {
      innerWidth: window.innerWidth,
      clientWidth: root.clientWidth,
      scrollWidth: root.scrollWidth,
      bodyScrollWidth: body.scrollWidth,
      offenders,
      clippedButtons,
    };
  });
  assert.equal(layout.innerWidth, viewportWidth);
  assert.equal(layout.clientWidth, viewportWidth);
  assert.ok(layout.scrollWidth <= viewportWidth, `根节点横向溢出：${JSON.stringify(layout)}`);
  assert.ok(layout.bodyScrollWidth <= viewportWidth, `body 横向溢出：${JSON.stringify(layout)}`);
  assert.deepEqual(layout.offenders, [], `Policy 区块越界：${JSON.stringify(layout.offenders)}`);
  assert.deepEqual(layout.clippedButtons, [], `Policy 按钮文字截断：${JSON.stringify(layout.clippedButtons)}`);
  return layout;
}

/**
 * 保存遮罩身份区域的页面截图。
 *
 * 参数：page 和文件名。
 * 返回：无；邮箱、租户和 Session 信息不能进入共享 PNG。
 */
async function takePrivacySafeScreenshot(page, fileName) {
  const userMenu = page.locator(".user-menu");
  assert.equal(await userMenu.count(), 1);
  await page.screenshot({
    path: path.join(ARTIFACT_DIR, fileName),
    fullPage: false,
    mask: [userMenu],
    maskColor: "#e5e7eb",
  });
}

/**
 * 执行管理员策略编辑器与三档布局验收。
 *
 * 返回：不含账号、Token、DSL 正文和模型 ID 的公开结果摘要。
 */
async function runAdminAcceptance(browser, account, suffix) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();
  const browserErrors = captureBrowserErrors(page);
  const policyRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === CONTROL_API_URL && url.pathname.startsWith("/policies")) {
      policyRequests.push(`${request.method()} ${url.pathname}`);
    }
  });
  try {
    await login(page, account, "admin");
    await openPolicies(page);
    const policyName = `PRD044 browser route ${suffix}`;
    // 127 code points 紧贴 DSL v1 的 128 上限，既保持合法又能真实压测无空格文本换行。
    const longValue = `browser${"x".repeat(120)}`;
    const targetModelSelected = await fillPolicyDraft(page, {
      name: policyName,
      status: "active",
      priority: 71,
      description: "PRD044 browser acceptance",
      conditionExpr: `env=="${longValue}"`,
      actionType: "route_to",
    });
    assert.ok(targetModelSelected);
    await validateDraft(page, true);
    await saveDraft(page, "POST", 201, '"1"');
    await page.waitForFunction(
      () => document.querySelector(".policy-feedback")?.textContent?.includes("策略已创建"),
      undefined,
      { timeout: 30_000 },
    );

    let row = page.locator(".policy-row", { hasText: policyName }).first();
    await row.waitFor({ state: "visible" });
    await row.locator("[data-policy-edit]").click();
    await page.locator("#policy-editor-title").filter({ hasText: "编辑策略" }).waitFor();
    await page.locator('#policy-editor-form [name="priority"]').fill("69");
    await page.locator('#policy-editor-form [name="description"]').fill("PRD044 browser updated");
    assert.equal(await page.locator("[data-policy-save]").isEnabled(), false);
    await validateDraft(page, true);
    await saveDraft(page, "PATCH", 200, '"2"');
    await page.waitForFunction(
      () => document.querySelector(".policy-feedback")?.textContent?.includes("策略已更新"),
      undefined,
      { timeout: 30_000 },
    );
    row = page.locator(".policy-row", { hasText: policyName }).first();
    assert.match(await row.innerText(), /v2/);

    await page.locator("#policy-new").click();
    const invalidSentinel = `PRD044_BROWSER_INVALID_${suffix}`;
    await fillPolicyDraft(page, {
      name: `PRD044 browser invalid ${suffix}`,
      status: "active",
      priority: 72,
      description: "invalid draft is never saved",
      conditionExpr: `env.${invalidSentinel}=="x"`,
      actionType: "allow",
    });
    await validateDraft(page, false);
    const validationText = await page.locator(".policy-validation-failed").innerText();
    assert.doesNotMatch(validationText, new RegExp(invalidSentinel));

    await row.locator("[data-policy-edit]").click();
    await validateDraft(page, true);
    await page.locator('#policy-filter-form [name="policyAction"]').selectOption("route_to");
    assert.ok(await page.locator(".policy-row", { hasText: policyName }).count() >= 1);
    await page.locator("#policy-filter-reset").click();

    await page.locator(".policy-heading").scrollIntoViewIfNeeded();
    const desktopLayout = await assertPolicyLayout(page, 1280);
    await takePrivacySafeScreenshot(page, "browser-desktop-1280x720.png");
    await page.setViewportSize({ width: 900, height: 800 });
    await page.locator(".policy-heading").scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    const intermediateLayout = await assertPolicyLayout(page, 900);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator(".policy-heading").scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    const mobileLayout = await assertPolicyLayout(page, 390);
    await takePrivacySafeScreenshot(page, "browser-mobile-390x844.png");

    assert.ok(policyRequests.some((item) => item === "POST /policies/validate"));
    assert.ok(policyRequests.some((item) => item === "POST /policies"));
    assert.ok(policyRequests.some((item) => item.startsWith("PATCH /policies/")));
    assert.deepEqual(browserErrors, [], `admin 浏览器错误：${JSON.stringify(browserErrors)}`);
    return {
      status: "passed",
      created: true,
      updatedVersion: 2,
      invalidDraftRejected: true,
      routeTargetSelected: true,
      policyRequestCount: policyRequests.length,
      desktopLayout,
      intermediateLayout,
      mobileLayout,
      browserErrorCount: 0,
      privacyMaskApplied: true,
    };
  } finally {
    await context.close();
  }
}

/**
 * 验证非 admin 直达策略 hash 时前端强制回退且不预取 Policy 正文。
 *
 * 参数：browser、角色账号和角色名。
 * 返回：hash、导航、DOM 与网络副作用摘要。
 */
async function runNonAdminAcceptance(browser, account, role) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();
  const browserErrors = captureBrowserErrors(page);
  const policyRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === CONTROL_API_URL && url.pathname.startsWith("/policies")) {
      policyRequests.push(`${request.method()} ${url.pathname}`);
    }
  });
  try {
    const directUrl = new URL(CONSOLE_URL);
    directUrl.hash = "policies";
    const evidence = await login(page, account, role, directUrl.toString());
    await waitForConsoleStability(page);
    const currentHash = await page.evaluate(() => window.location.hash);
    const activeNavigation = await page.locator(".console-shell").getAttribute("data-active-navigation");
    assert.equal(evidence.requestedHash, "#policies");
    assert.equal(currentHash, "");
    assert.equal(activeNavigation, "overview");
    assert.equal(await page.locator('.sidebar-link[data-console-nav="policies"]').count(), 0);
    assert.equal(await page.locator(".policies-panel").count(), 0);
    assert.deepEqual(policyRequests, [], `${role} 越权直达仍请求了 Policy API`);
    assert.deepEqual(browserErrors, [], `${role} 浏览器错误：${JSON.stringify(browserErrors)}`);
    return {
      status: "passed",
      requestedHash: evidence.requestedHash,
      rejectedHashCleared: currentHash === "",
      fallbackNavigation: activeNavigation,
      policyNavigation: false,
      policyDom: false,
      policyRequestCount: 0,
      browserErrorCount: 0,
    };
  } finally {
    await context.close();
  }
}

/**
 * 启动系统 Chrome 并顺序执行三个角色验收。
 *
 * 返回：无；只输出可公开的布尔值、计数与布局摘要，任一断言失败均返回非零退出码。
 */
async function main() {
  await mkdir(ARTIFACT_DIR, { recursive: true });
  const { accounts, suffix } = await prepareAccounts();
  const browser = await chromium.launch({
    executablePath: CHROME_PATH,
    headless: process.env.METAHUB_BROWSER_HEADED !== "1",
  });
  try {
    const admin = await runAdminAcceptance(browser, accounts.admin, suffix);
    const developer = await runNonAdminAcceptance(browser, accounts.developer, "developer");
    const viewer = await runNonAdminAcceptance(browser, accounts.viewer, "viewer");
    console.log(JSON.stringify({ admin, developer, viewer }, null, 2));
  } finally {
    await browser.close();
  }
}

await main();
