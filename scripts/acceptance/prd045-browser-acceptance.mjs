/**
 * PRD-045 策略快照发布、回滚、调用日志和 RBAC 的真实浏览器验收。
 *
 * 脚本通过公开 Control API 准备一次性候选和角色账号，再使用真实 Console 完成
 * 发布与回滚。密码和 Token 只驻留当前 Node 进程内存，截图会遮罩用户身份区域。
 */

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const require = createRequire(import.meta.url);
// Playwright 由工作区运行时提供，不写入 Console 生产依赖。
const { chromium } = require("playwright");

// 服务地址、租户、管理员标识和证据路径只允许由本地验收环境覆盖，并由 assertLocalScope 再次限制。
const CONTROL_API_URL = process.env.METAHUB_CONTROL_API_URL ?? "http://localhost:8001";
const CONSOLE_URL = process.env.METAHUB_CONSOLE_URL ?? "http://localhost:5173/#policy-snapshots";
const TENANT_SLUG = (process.env.METAHUB_TENANT_SLUG ?? "default").trim();
const ADMIN_EMAIL = (process.env.METAHUB_ADMIN_EMAIL ?? "admin@metahub.local").trim().toLowerCase();
const DATABASE_PATH = path.resolve(
  process.env.METAHUB_DATABASE_PATH ?? ".data/metahub-dev-prd031.db",
);
const ARTIFACT_DIR = path.resolve(
  process.env.METAHUB_PRD045_ARTIFACT_DIR ?? "artifacts/prd045",
);
const CHROME_PATH = process.env.METAHUB_CHROME_PATH
  ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

/**
 * 读取必需环境变量。
 *
 * 参数：name 为变量名。
 * 返回：去除首尾空白后的值。
 * 安全：失败只报告变量名，绝不打印其他环境内容。
 */
function requiredEnvironment(name) {
  const value = String(process.env[name] ?? "").trim();
  assert.ok(value, `缺少必需环境变量 ${name}`);
  return value;
}

/**
 * 确认服务和数据库都属于本地验收范围。
 *
 * 约束：非回环 URL、工作区外数据库或缺少显式确认时立即失败，防止误改共享环境。
 */
function assertLocalScope() {
  assert.equal(process.env.METAHUB_CONFIRM_BROWSER_STATE_MUTATION, "1");
  const allowedHosts = new Set(["127.0.0.1", "::1", "localhost"]);
  for (const value of [CONTROL_API_URL, CONSOLE_URL]) {
    assert.ok(allowedHosts.has(new URL(value).hostname), "浏览器验收只允许回环地址");
  }
  assert.ok(TENANT_SLUG && ADMIN_EMAIL, "租户和管理员标识不能为空");
  const workspace = path.resolve(".");
  for (const [label, localPath] of [
    ["数据库", DATABASE_PATH],
    ["验收证据目录", ARTIFACT_DIR],
  ]) {
    assert.ok(localPath.startsWith(`${workspace}${path.sep}`), `${label}必须位于工作区内`);
  }
}

/**
 * 调用 Control JSON API 并校验状态和可选统一错误码。
 *
 * 参数：pathname、fetch 选项、期望 HTTP 和错误码。
 * 返回：解析后的 JSON 与响应对象。
 * 安全：失败消息只包含路径、HTTP 和安全错误码，不拼接响应正文。
 */
async function controlJson(pathname, options, expectedStatus, expectedCode = null) {
  const response = await fetch(`${CONTROL_API_URL}${pathname}`, options);
  let body = {};
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  assert.equal(
    response.status,
    expectedStatus,
    `${pathname} HTTP ${response.status}, code=${body.code ?? "-"}`,
  );
  if (expectedCode !== null) {
    assert.equal(body.code, expectedCode, `${pathname} 返回了错误的统一错误码`);
  }
  if (response.status !== 304) {
    assert.ok(body.request_id, `${pathname} 缺少 request_id`);
    assert.ok(body.trace_id, `${pathname} 缺少 trace_id`);
  }
  return { body, response };
}

/**
 * 使用公开认证接口登录。
 *
 * 参数：邮箱、只驻留内存的密码和本轮唯一 Session User-Agent 标记。
 * 返回：短期 Token、用户与会话摘要；调用方不得写入日志或截图。
 */
async function apiLogin(email, password, sessionMarker) {
  const { body } = await controlJson(
    "/auth/login",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "User-Agent": sessionMarker,
      },
      body: JSON.stringify({ tenant_slug: TENANT_SLUG, email, password }),
    },
    200,
  );
  assert.ok(body.access_token);
  return body;
}

/**
 * 生成 Bearer Header，并按需附带稳定 request_id。
 *
 * 参数：Token 和可选 request_id。
 * 返回：只供当前 fetch 使用的 Header 对象。
 */
function authHeaders(token, requestId = null) {
  const headers = { Authorization: `Bearer ${token}` };
  if (requestId) {
    headers["X-Request-ID"] = requestId;
  }
  return headers;
}

/**
 * 准备管理员、一次性角色账号、可发布候选和恢复账本。
 *
 * 返回：浏览器账号、Token、临时资源 ID、mask 恢复版本和发布前 current。
 * 权限：所有业务资源均通过真实 admin API 创建或修改。
 */
function createStateLedger() {
  const suffix = randomBytes(5).toString("hex");
  return {
    suffix,
    tenantId: null,
    accounts: {},
    bootstrapAdminToken: null,
    bootstrapAdminUserId: null,
    bootstrapSessionId: null,
    bootstrapSessionMarker: `prd045-browser-bootstrap-${suffix}`,
    roleTokens: {},
    userIds: [],
    userIdsByRole: {},
    userIntents: [],
    masksToRestore: [],
    tempPolicy: null,
    tempPolicyIntent: {
      name: `PRD045 browser acceptance ${suffix}`,
      description: "PRD-045 browser acceptance only",
    },
    baseVersion: null,
    baseOperation: null,
    baselineHeadEtag: null,
    baselineCandidateEtag: null,
    publishReason: `PRD045 browser publish ${suffix}`,
    rollbackReason: `PRD045 browser rollback ${suffix}`,
    recoveryReason: `PRD045 browser cleanup ${suffix}`,
    publishedSnapshot: null,
    candidateMutated: false,
  };
}

/**
 * 逐步填充浏览器验收清理账本。
 *
 * 参数：main 在任何网络或浏览器动作前创建的可变账本。
 * 返回：同一账本；每个有副作用的 API 成功后立即登记，确保后续步骤失败仍可定向回收。
 */
async function prepareState(state) {
  const adminPassword = requiredEnvironment("METAHUB_ADMIN_PASSWORD");
  const adminLogin = await apiLogin(
    ADMIN_EMAIL,
    adminPassword,
    state.bootstrapSessionMarker,
  );
  state.bootstrapAdminToken = adminLogin.access_token;
  state.bootstrapAdminUserId = adminLogin.user.user_id;
  state.bootstrapSessionId = String(adminLogin.session_id);
  state.tenantId = adminLogin.user.tenant_id;
  for (const role of ["admin", "developer", "viewer"]) {
    const account = {
      email: `prd045-browser-${role}-${state.suffix}@metahub.local`,
      password: `Prd045!${randomBytes(20).toString("base64url")}`,
    };
    const displayName = `PRD045 browser ${role}`;
    // 创建调用前先登记唯一身份；即使响应丢失，清理仍可按 tenant + email 精确定位并核对角色。
    state.accounts[role] = account;
    state.userIntents.push({ email: account.email, displayName, role });
    const { body: created } = await controlJson(
      "/users",
      {
        method: "POST",
        headers: {
          ...authHeaders(state.bootstrapAdminToken, `req_prd045_browser_user_${role}_${state.suffix}`),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          email: account.email,
          display_name: displayName,
          password: account.password,
          role,
        }),
      },
      201,
    );
    state.userIds.push(created.user.user_id);
    state.userIdsByRole[role] = created.user.user_id;
    // 临时 admin 只通过真实 Console 表单登录；developer/viewer 另建 API Session 供后端 RBAC 验收。
    if (role !== "admin") {
      state.roleTokens[role] = (await apiLogin(
        account.email,
        account.password,
        `prd045-browser-api-${role}-${state.suffix}`,
      )).access_token;
    }
  }

  const { body: listed } = await controlJson(
    "/policies",
    { headers: authHeaders(state.bootstrapAdminToken) },
    200,
  );
  for (const policy of listed.policies) {
    if (policy.status !== "active" || policy.action.type !== "mask") {
      continue;
    }
    const restoreIntent = {
      policyId: policy.policy_id,
      originalVersion: policy.version,
    };
    // PATCH 前登记精确对象和旧 CAS；响应未知时 cleanup 会从数据库重新读取当前版本。
    state.masksToRestore.push(restoreIntent);
    state.candidateMutated = true;
    const { body: disabled } = await controlJson(
      `/policies/${policy.policy_id}`,
      {
        method: "PATCH",
        headers: {
          ...authHeaders(state.bootstrapAdminToken, `req_prd045_browser_mask_${policy.policy_id}`),
          "Content-Type": "application/json",
          "If-Match": `"${policy.version}"`,
        },
        body: JSON.stringify({ status: "disabled" }),
      },
      200,
    );
    assert.equal(disabled.policy.version, restoreIntent.originalVersion + 1);
  }

  const { body: beforeCandidate, response: beforeResponse } = await controlJson(
    "/policy-snapshots/current",
    { headers: authHeaders(state.bootstrapAdminToken) },
    200,
  );
  assert.equal(beforeCandidate.candidate_status, "valid");
  assert.equal(beforeCandidate.candidate_matches_current, true);
  state.baseVersion = beforeCandidate.current_snapshot.snapshot_version;
  state.baseOperation = beforeCandidate.current_snapshot.operation;
  state.baselineHeadEtag = beforeResponse.headers.get("ETag");
  state.baselineCandidateEtag = beforeResponse.headers.get("X-Policy-Candidate-ETag");
  assert.ok(state.baselineHeadEtag && state.baselineCandidateEtag);
  assert.equal(state.baselineHeadEtag, `"${state.baseVersion}"`);

  // POST 前登记候选变更意图；提交成功但响应中断时 cleanup 将按唯一名称查回 Policy。
  state.candidateMutated = true;
  const { body: createdPolicy } = await controlJson(
    "/policies",
    {
      method: "POST",
      headers: {
          ...authHeaders(state.bootstrapAdminToken, `req_prd045_browser_policy_${state.suffix}`),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        name: state.tempPolicyIntent.name,
        description: state.tempPolicyIntent.description,
        status: "active",
        priority: 2,
        rule: { condition_expr: `env == "prd045-browser-${state.suffix}"` },
        action: { type: "allow" },
      }),
    },
    201,
  );
  state.tempPolicy = createdPolicy.policy;
  const { body: ready } = await controlJson(
    "/policy-snapshots/current",
    { headers: authHeaders(state.bootstrapAdminToken) },
    200,
  );
  assert.equal(ready.candidate_status, "valid");
  assert.equal(ready.candidate_matches_current, false);
  assert.ok(beforeResponse.headers.get("ETag"));
  return state;
}

/**
 * 收集浏览器脚本、console error 和非成功 HTTP。
 *
 * 参数：Playwright Page。
 * 返回：只含公开路径和固定错误文本的数组，不读取请求 Header 或正文。
 */
function captureBrowserErrors(page) {
  const errors = [];
  // 浏览器原始异常可能拼入响应正文，验收输出只登记固定类别，不复制 message。
  page.on("pageerror", () => errors.push("pageerror"));
  page.on("console", (message) => {
    // Console 原文不可信且可能含身份或业务内容，只保留是否出现 error 的事实。
    if (message.type() === "error") {
      errors.push("console_error");
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
 * 使用真实 Console 登录表单进入目标 hash。
 *
 * 参数：Page、内存账号、期望角色和目标 URL。
 * 返回：提交登录前的原始 hash，供越权直达断言。
 */
async function loginPage(page, account, expectedRole, targetUrl = CONSOLE_URL) {
  await page.goto(targetUrl, { waitUntil: "domcontentloaded" });
  const requestedHash = await page.evaluate(() => window.location.hash);
  await page.locator("#login-form").waitFor({ state: "visible" });
  await page.locator('[name="tenantSlug"]').fill(TENANT_SLUG);
  await page.locator('[name="email"]').fill(account.email);
  await page.locator('[name="password"]').fill(account.password);
  await page.locator('#login-form button[type="submit"]').click();
  await page.locator(".console-shell").waitFor({ state: "visible", timeout: 30_000 });
  assert.match(await page.locator(".user-menu").innerText(), new RegExp(`\\b${expectedRole}\\b`));
  return { requestedHash };
}

/**
 * 等待 Console 异步请求和 DOM 替换稳定。
 *
 * 参数：Playwright Page。
 * 返回：无；周期刷新导致 networkidle 超时时会使用短稳定等待。
 */
async function waitForConsoleStability(page) {
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined);
  await page.waitForTimeout(500);
}

/**
 * 注销当前浏览器会话，避免验收结束后留下可用 Session。
 *
 * 参数：已登录 Page。
 * 返回：无；完成时登录表单重新可见。
 */
async function logoutPage(page) {
  const button = page.locator("#logout-button");
  if (await button.count()) {
    await button.click();
    await page.locator("#login-form").waitFor({ state: "visible", timeout: 30_000 });
  }
}

/**
 * 在异常路径尽力注销浏览器会话，且不覆盖最先发生的验收断言。
 *
 * 参数：可能已经关闭或已经回到登录页的 Page。
 * 返回：无；真正的清理失败仍由 API Token 注销和临时用户禁用兜底。
 */
async function logoutPageBestEffort(page) {
  try {
    if (!page.isClosed()) {
      await logoutPage(page);
    }
  } catch {
    // 页面崩溃或服务已停止时无法通过 UI 注销；finally 仍必须继续关闭 Context 和执行服务端清理。
  }
}

/**
 * 打开 admin-only 策略发布页面。
 *
 * 参数：已登录 admin Page。
 * 返回：无；完成时 current、candidate、publish 和 history 均已渲染。
 */
async function openPolicySnapshots(page) {
  const shell = page.locator(".console-shell");
  if (await shell.getAttribute("data-active-navigation") !== "policy-snapshots") {
    await page.locator('.sidebar-link[data-console-nav="policy-snapshots"]').click();
  }
  await page.locator(".policy-snapshots-panel #policy-snapshot-publish-form").waitFor({
    state: "visible",
    timeout: 30_000,
  });
  await waitForConsoleStability(page);
}

/**
 * 展开后端声明的唯一“上一激活版本”回滚表单。
 *
 * 参数：Page 和发布前版本号。
 * 返回：已显示的回滚表单 Locator。
 * 约束：不能用整行 hasText 匹配版本；当前版本的“接续 vN”来源文案也会包含旧版本号。
 */
async function openPreviousRollbackForm(page, expectedVersion) {
  const buttons = page.locator("[data-policy-snapshot-rollback-open]");
  await buttons.first().waitFor({ state: "visible", timeout: 30_000 });
  assert.equal(await buttons.count(), 1, "页面必须只暴露上一激活版本的一个回滚入口");
  const button = buttons.first();
  const renderedVersion = await button.evaluate((element) => (
    element.closest(".policy-snapshot-row")
      ?.querySelector(".policy-snapshot-version strong")
      ?.textContent
      ?.trim() ?? ""
  ));
  assert.equal(renderedVersion, `v${expectedVersion}`);
  await button.click();
  const form = page.locator("[data-policy-snapshot-rollback-form]");
  await form.waitFor({ state: "visible", timeout: 30_000 });
  return form;
}

/**
 * 校验策略快照页面没有根节点横向溢出或按钮文字裁切。
 *
 * 参数：Page 和期望视口宽度。
 * 返回：可写入安全验收摘要的布局计数。
 */
async function assertSnapshotLayout(page, viewportWidth) {
  const layout = await page.evaluate(() => {
    const root = document.documentElement;
    const selectors = [
      ".policy-snapshots-panel",
      ".policy-snapshot-heading",
      ".policy-snapshot-current",
      ".policy-snapshot-candidate",
      ".policy-snapshot-publish",
      ".policy-snapshot-history",
      ".policy-snapshot-row",
    ];
    const offenders = [...document.querySelectorAll(selectors.join(","))]
      .filter((element) => {
        const rectangle = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return style.display !== "none"
          && rectangle.width > 0
          && (rectangle.left < -1 || rectangle.right > root.clientWidth + 1);
      })
      .map((element) => element.className);
    const clippedButtons = [...document.querySelectorAll(".policy-snapshots-panel button")]
      .filter((button) => button.offsetParent && button.scrollWidth > button.clientWidth + 1)
      .map((button) => button.textContent?.trim() ?? "");
    const panelRectangle = document.querySelector(".policy-snapshots-panel")?.getBoundingClientRect();
    return {
      innerWidth: window.innerWidth,
      clientWidth: root.clientWidth,
      scrollWidth: root.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      offenders,
      clippedButtons,
      panelVisibleInViewport: Boolean(
        panelRectangle
          && panelRectangle.width > 0
          && panelRectangle.height > 0
          && panelRectangle.top < window.innerHeight
          && panelRectangle.bottom > 0
          && panelRectangle.left < window.innerWidth
          && panelRectangle.right > 0
      ),
    };
  });
  assert.equal(layout.innerWidth, viewportWidth);
  assert.equal(layout.clientWidth, viewportWidth);
  assert.ok(layout.scrollWidth <= viewportWidth, JSON.stringify(layout));
  assert.ok(layout.bodyScrollWidth <= viewportWidth, JSON.stringify(layout));
  assert.deepEqual(layout.offenders, []);
  assert.deepEqual(layout.clippedButtons, []);
  assert.equal(layout.panelVisibleInViewport, true, JSON.stringify(layout));
  return layout;
}

/**
 * 把策略快照主面板定位到当前视口。
 *
 * 参数：Page。
 * 返回：无；响应式重排后必须让功能主面板进入截图，不能只留下移动端侧栏作为验收证据。
 */
async function focusSnapshotEvidence(page) {
  const panel = page.locator(".policy-snapshots-panel");
  await panel.waitFor({ state: "visible" });
  await panel.evaluate((element) => element.scrollIntoView({ block: "start", inline: "nearest" }));
  await page.waitForFunction(() => {
    const rectangle = document.querySelector(".policy-snapshots-panel")?.getBoundingClientRect();
    return Boolean(
      rectangle
        && rectangle.width > 0
        && rectangle.height > 0
        && rectangle.top < window.innerHeight
        && rectangle.bottom > 0
    );
  });
}

/**
 * 保存遮罩身份区域的非空页面截图。
 *
 * 参数：Page 和目标文件名。
 * 返回：无；邮箱、租户和 Session 信息由 mask 覆盖。
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
 * 使用真实页面发布候选、回滚上一版、检查三档布局和调用日志详情。
 *
 * 返回：版本、请求数量、布局、日志筛选和浏览器错误摘要。
 */
async function runAdminAcceptance(browser, state) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();
  const browserErrors = captureBrowserErrors(page);
  const snapshotRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === CONTROL_API_URL && url.pathname.startsWith("/policy-snapshots")) {
      snapshotRequests.push(`${request.method()} ${url.pathname}`);
    }
  });
  page.on("dialog", async (dialog) => {
    assert.equal(dialog.type(), "confirm");
    await dialog.accept();
  });
  try {
    await loginPage(page, state.accounts.admin, "admin");
    await openPolicySnapshots(page);
    assert.match(await page.locator(".policy-snapshot-current").innerText(), new RegExp(`v${state.baseVersion}`));
    // 回滚 head 与 active 候选不同是合法状态；文案必须与准备阶段读取到的线上操作类型一致。
    const expectedCandidateTitle = state.baseOperation === "rollback"
      ? /回滚内容与候选不同/
      : /候选可发布/;
    assert.match(await page.locator(".policy-snapshot-candidate").innerText(), expectedCandidateTitle);

    const publishResponse = page.waitForResponse(
      (response) => response.url().endsWith("/policy-snapshots/publish")
        && response.request().method() === "POST",
      { timeout: 30_000 },
    );
    await page.locator('#policy-snapshot-publish-form [name="reason"]').fill(state.publishReason);
    const publishButton = page.locator("[data-policy-snapshot-publish]");
    assert.equal(await publishButton.isEnabled(), true);
    await publishButton.click();
    const published = await publishResponse;
    assert.equal(published.status(), 201);
    const publishedBody = await published.json();
    const publishedVersion = publishedBody.policy_snapshot.snapshot_version;
    state.publishedSnapshot = publishedBody.policy_snapshot;
    assert.equal(publishedVersion, state.baseVersion + 1);
    await page.waitForFunction(
      (version) => document.querySelector(".policy-snapshot-current")?.textContent?.includes(`v${version}`),
      publishedVersion,
      { timeout: 30_000 },
    );

    const rollbackForm = await openPreviousRollbackForm(page, state.baseVersion);
    await rollbackForm.locator('[name="reason"]').fill(state.rollbackReason);
    const rollbackResponse = page.waitForResponse(
      (response) => response.url().includes("/rollback")
        && response.request().method() === "POST",
      { timeout: 30_000 },
    );
    await rollbackForm.locator('button[type="submit"]').click();
    const rolledBack = await rollbackResponse;
    assert.equal(rolledBack.status(), 201);
    const rollbackBody = await rolledBack.json();
    const rollbackVersion = rollbackBody.policy_snapshot.snapshot_version;
    assert.equal(rollbackVersion, publishedVersion + 1);
    assert.equal(
      rollbackBody.policy_snapshot.rollback_source_snapshot_version,
      state.baseVersion,
    );
    await page.waitForFunction(
      (version) => document.querySelector(".policy-snapshot-current")?.textContent?.includes(`v${version}`),
      rollbackVersion,
      { timeout: 30_000 },
    );
    assert.match(await page.locator(".policy-snapshot-candidate").innerText(), /回滚内容与候选不同/);

    await focusSnapshotEvidence(page);
    const desktopLayout = await assertSnapshotLayout(page, 1280);
    await takePrivacySafeScreenshot(page, "browser-desktop-1280x720.png");
    await page.setViewportSize({ width: 900, height: 800 });
    await page.waitForTimeout(300);
    await focusSnapshotEvidence(page);
    const intermediateLayout = await assertSnapshotLayout(page, 900);
    await takePrivacySafeScreenshot(page, "browser-intermediate-900x800.png");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(300);
    await focusSnapshotEvidence(page);
    const mobileLayout = await assertSnapshotLayout(page, 390);
    await takePrivacySafeScreenshot(page, "browser-mobile-390x844.png");

    await page.setViewportSize({ width: 1280, height: 720 });
    await page.locator('.sidebar-link[data-console-nav="request-logs"]').click();
    await page.locator("#request-log-filter-form").waitFor({ state: "visible" });
    const logResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/request-logs"
        && new URL(response.url()).searchParams.get("snapshot_version") === "2",
      { timeout: 30_000 },
    );
    await page.locator('#request-log-filter-form [name="snapshotVersion"]').fill("2");
    await page.locator('#request-log-filter-form button[type="submit"]').click();
    const filteredLogResponse = await logResponse;
    assert.equal(filteredLogResponse.status(), 200);
    const filteredLogPayload = await filteredLogResponse.json();
    assert.ok(Array.isArray(filteredLogPayload.request_logs));
    assert.ok(filteredLogPayload.request_logs.length > 0);
    assert.ok(filteredLogPayload.request_logs.every((item) => item.snapshot_version === 2));
    const expectedLogIds = filteredLogPayload.request_logs.map((item) => item.request_id);
    // HTTP 200 只表示过滤数据已返回；这里绑定响应行数、顺序和版本等待异步重绘，避免把旧 v3 行误判为 v2。
    await page.waitForFunction(
      ({ requestIds, snapshotVersion }) => {
        const rows = [...document.querySelectorAll(
          ".request-log-row:not(.request-log-head):not(.request-log-empty)",
        )];
        return rows.length === requestIds.length && rows.every((row, index) => (
          row.querySelector(".request-log-id code")?.textContent?.trim() === requestIds[index]
          && row.querySelector(".request-log-snapshot strong")?.textContent?.trim()
            === "v" + snapshotVersion
        ));
      },
      { requestIds: expectedLogIds, snapshotVersion: 2 },
      { timeout: 30_000 },
    );
    const expectedLogId = expectedLogIds[0];
    const logRow = page.locator(
      ".request-log-row:not(.request-log-head):not(.request-log-empty)",
    ).first();
    assert.equal(await logRow.locator(".request-log-id code").innerText(), expectedLogId);
    assert.equal(await logRow.locator(".request-log-snapshot strong").innerText(), "v2");
    const expectedDetailPath = "/request-logs/" + encodeURIComponent(expectedLogId);
    const detailResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === expectedDetailPath
        && response.request().method() === "GET",
      { timeout: 30_000 },
    );
    await logRow.locator("[data-request-log-detail]").click();
    const loadedDetailResponse = await detailResponse;
    assert.equal(loadedDetailResponse.status(), 200);
    const loadedDetailPayload = await loadedDetailResponse.json();
    assert.equal(loadedDetailPayload.request_log?.log?.request_id, expectedLogId);
    assert.equal(loadedDetailPayload.request_log?.log?.snapshot_version, 2);
    // 空详情占位在点击前已经可见，必须等待目标 request_id 与 v2 真正渲染后再做脱敏断言。
    await page.waitForFunction(
      ({ requestId, snapshotVersion }) => {
        const detailElement = document.querySelector(
          ".request-log-detail:not(.empty-state):not(.error-state)",
        );
        const text = detailElement?.textContent ?? "";
        return text.includes(requestId)
          && text.includes("Policy 快照")
          && text.includes("v" + snapshotVersion);
      },
      { requestId: expectedLogId, snapshotVersion: 2 },
      { timeout: 30_000 },
    );
    const detail = page.locator(".request-log-detail:not(.empty-state):not(.error-state)");
    const detailText = await detail.innerText();
    assert.match(detailText, /Policy 快照/);
    assert.match(detailText, /v2/);
    assert.doesNotMatch(
      detailText,
      /canonical_expression|condition_expr|messages|Prompt|Response|Authorization/i,
    );
    assert.deepEqual(browserErrors, []);
    await logoutPage(page);
    return {
      publishedVersion,
      rollbackVersion,
      rollbackSourceVersion: state.baseVersion,
      snapshotRequestCount: snapshotRequests.length,
      requestLogVersionFilter: 2,
      requestLogDetailSafe: true,
      desktopLayout,
      intermediateLayout,
      mobileLayout,
      browserErrorCount: 0,
      privacyMaskApplied: true,
    };
  } finally {
    await logoutPageBestEffort(page);
    await context.close();
  }
}

/**
 * 验证非 admin 直达快照 hash 时回退 overview，且不预取任何快照 API。
 *
 * 返回：hash、导航、DOM、网络和浏览器错误摘要。
 */
async function runNonAdminAcceptance(browser, account, role) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();
  const errors = captureBrowserErrors(page);
  const requests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === CONTROL_API_URL && url.pathname.startsWith("/policy-snapshots")) {
      requests.push(`${request.method()} ${url.pathname}`);
    }
  });
  try {
    const direct = new URL(CONSOLE_URL);
    direct.hash = "policy-snapshots";
    const evidence = await loginPage(page, account, role, direct.toString());
    await waitForConsoleStability(page);
    const currentHash = await page.evaluate(() => window.location.hash);
    assert.equal(evidence.requestedHash, "#policy-snapshots");
    assert.equal(currentHash, "");
    assert.equal(
      await page.locator(".console-shell").getAttribute("data-active-navigation"),
      "overview",
    );
    assert.equal(await page.locator('.sidebar-link[data-console-nav="policy-snapshots"]').count(), 0);
    assert.equal(await page.locator(".policy-snapshots-panel").count(), 0);
    assert.deepEqual(requests, []);
    assert.deepEqual(errors, []);
    await logoutPage(page);
    return {
      requestedHash: evidence.requestedHash,
      fallbackNavigation: "overview",
      policyNavigation: false,
      policyDom: false,
      snapshotRequestCount: 0,
      browserErrorCount: 0,
    };
  } finally {
    await logoutPageBestEffort(page);
    await context.close();
  }
}

/**
 * 逐角色验证后端 current/history/publish/rollback 权限，不依赖前端隐藏。
 *
 * 返回：九个 403 状态和统一错误码计数。
 */
async function verifyBackendRbac(state) {
  const statuses = {};
  for (const [role, token] of Object.entries(state.roleTokens)) {
    const calls = [
      ["current", "GET", "/policy-snapshots/current", null],
      ["history", "GET", "/policy-snapshots", null],
      ["publish", "POST", "/policy-snapshots/publish", { reason: "forbidden" }],
      ["rollback", "POST", "/policy-snapshots/psnap_not_owned/rollback", { reason: "forbidden" }],
    ];
    for (const [label, method, pathname, payload] of calls) {
      const { body, response } = await controlJson(
        pathname,
        {
          method,
          headers: {
            ...authHeaders(token),
            "Content-Type": "application/json",
          },
          body: payload ? JSON.stringify(payload) : undefined,
        },
        403,
        "auth.permission_denied",
      );
      statuses[`${role}_${label}`] = response.status;
      assert.ok(body.request_id && body.trace_id);
    }
  }
  // 汇总只包含角色、动作标签和 HTTP 状态，不携带 Token 或错误响应正文。
  return { statuses, permissionDeniedCount: Object.keys(statuses).length };
}

/**
 * 执行单个清理阶段并只登记固定阶段名。
 *
 * 参数：失败数组、安全阶段名和异步清理动作。
 * 返回：动作是否成功；失败原文不会进入输出，后续独立清理仍会继续。
 */
async function attemptCleanup(failures, label, action) {
  try {
    await action();
    return true;
  } catch {
    // 只记录固定阶段名，避免原始异常意外携带 Token、密码、DSL 或对象正文。
    failures.push(label);
    return false;
  }
}

/**
 * 按当前 tenant 与精确 ID 或名称读取一个 Policy 清理记录。
 *
 * 参数：清理账本和二选一的 policyId/name；返回：0 或 1 条安全元数据。
 * 约束：查询结果超过一条立即失败，不能通过前缀或模糊名称批量修改业务 Policy。
 */
function readPolicyCleanupRecord(state, { policyId = null, name = null }) {
  assert.ok(state.tenantId);
  assert.notEqual(Boolean(policyId), Boolean(name));
  const database = new DatabaseSync(DATABASE_PATH, { readOnly: true });
  try {
    const sql = policyId
      ? "SELECT policy_id, name, description, status, version, created_by, updated_by FROM policies WHERE tenant_id=? AND policy_id=?"
      : "SELECT policy_id, name, description, status, version, created_by, updated_by FROM policies WHERE tenant_id=? AND name=?";
    const rows = database.prepare(sql).all(state.tenantId, policyId ?? name);
    assert.ok(rows.length <= 1);
    if (!rows.length) {
      return null;
    }
    const row = rows[0];
    return {
      policyId: String(row.policy_id),
      name: String(row.name),
      description: String(row.description ?? ""),
      status: String(row.status),
      version: Number(row.version),
      createdBy: String(row.created_by),
      updatedBy: String(row.updated_by),
    };
  } finally {
    database.close();
  }
}

/**
 * 精确撤销本轮 bootstrap 管理员登录创建的 Session。
 *
 * 参数：含 tenant slug、管理员邮箱和唯一 User-Agent 标记的账本。
 * 返回：本次直接撤销的活动 Session 数；API logout 已成功时可为 0。
 */
function revokeBootstrapSessions(state) {
  const database = new DatabaseSync(DATABASE_PATH);
  const now = new Date().toISOString();
  try {
    const rows = state.bootstrapSessionId
      ? database.prepare(`
          SELECT s.session_id, s.status, s.user_agent, u.user_id
          FROM sessions s
          JOIN users u ON u.tenant_id=s.tenant_id AND u.user_id=s.user_id
          WHERE s.tenant_id=? AND s.session_id=?
        `).all(state.tenantId, state.bootstrapSessionId)
      : database.prepare(`
          SELECT s.session_id, s.status, s.user_agent, u.user_id
          FROM sessions s
          JOIN users u ON u.tenant_id=s.tenant_id AND u.user_id=s.user_id
          JOIN tenants t ON t.tenant_id=u.tenant_id
          WHERE t.slug=? AND u.email=? AND s.user_agent=?
        `).all(TENANT_SLUG, ADMIN_EMAIL, state.bootstrapSessionMarker);
    assert.ok(state.bootstrapSessionId ? rows.length === 1 : rows.length <= 1);
    if (state.bootstrapAdminUserId && rows.length) {
      assert.equal(String(rows[0].user_id), state.bootstrapAdminUserId);
    }
    if (!rows.length) {
      return 0;
    }
    assert.equal(String(rows[0].user_agent), state.bootstrapSessionMarker);
    const updated = database.prepare(`
      UPDATE sessions
      SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
      WHERE session_id=? AND status='active'
    `).run(now, now, String(rows[0].session_id));
    const active = database.prepare(
      "SELECT COUNT(*) AS total FROM sessions WHERE session_id=? AND status='active'",
    ).get(String(rows[0].session_id));
    assert.equal(Number(active.total), 0);
    return Number(updated.changes);
  } finally {
    database.close();
  }
}

/**
 * 在同一数据库事务内撤销临时三角色全部 Session 并禁用用户。
 *
 * 参数：含精确 email/display_name/role intent 的账本。
 * 返回：实际找到并禁用的用户数、直接撤销的活动 Session 数。
 * 约束：必须保留至少一个非临时 active admin，且所有查询都追加 tenant_id。
 */
function disableTemporaryUsersAndSessions(state) {
  if (!state.tenantId || !state.userIntents.length) {
    // 尚未开始创建临时用户时没有可回收对象，不能扩展为模糊租户扫描。
    return { usersDisabled: 0, sessionsRevoked: 0 };
  }
  const database = new DatabaseSync(DATABASE_PATH);
  try {
    database.exec("BEGIN IMMEDIATE");
    try {
      const records = [];
      for (const intent of state.userIntents) {
        const rows = database.prepare(`
          SELECT user_id, email, display_name, role
          FROM users
          WHERE tenant_id=? AND email=?
        `).all(state.tenantId, intent.email);
        assert.ok(rows.length <= 1);
        if (!rows.length) {
          continue;
        }
        const row = rows[0];
        assert.equal(String(row.email), intent.email);
        assert.equal(String(row.display_name), intent.displayName);
        assert.equal(String(row.role), intent.role);
        records.push({ userId: String(row.user_id), role: String(row.role) });
      }
      const userIds = records.map((record) => record.userId);
      assert.ok(state.userIds.every((userId) => userIds.includes(userId)));
      if (!userIds.length) {
        // 创建请求均未提交时按空结果收口，禁止为了“清理”而修改其他现有用户。
        database.exec("COMMIT");
        return { usersDisabled: 0, sessionsRevoked: 0 };
      }
      const placeholders = userIds.map(() => "?").join(",");
      const remainingAdmins = database.prepare(`
        SELECT COUNT(*) AS total
        FROM users
        WHERE tenant_id=? AND role='admin' AND status='active'
          AND user_id NOT IN (${placeholders})
      `).get(state.tenantId, ...userIds);
      assert.ok(Number(remainingAdmins.total) >= 1);

      const now = new Date().toISOString();
      const sessions = database.prepare(`
        UPDATE sessions
        SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
        WHERE tenant_id=? AND user_id IN (${placeholders}) AND status='active'
      `).run(now, now, state.tenantId, ...userIds);
      const users = database.prepare(`
        UPDATE users SET status='disabled', updated_at=?
        WHERE tenant_id=? AND user_id IN (${placeholders})
      `).run(now, state.tenantId, ...userIds);
      assert.equal(Number(users.changes), userIds.length);
      const activeSessions = database.prepare(`
        SELECT COUNT(*) AS total FROM sessions
        WHERE tenant_id=? AND user_id IN (${placeholders}) AND status='active'
      `).get(state.tenantId, ...userIds);
      assert.equal(Number(activeSessions.total), 0);
      const activeUsers = database.prepare(`
        SELECT COUNT(*) AS total FROM users
        WHERE tenant_id=? AND user_id IN (${placeholders}) AND status='active'
      `).get(state.tenantId, ...userIds);
      assert.equal(Number(activeUsers.total), 0);
      database.exec("COMMIT");
      return {
        usersDisabled: userIds.length,
        sessionsRevoked: Number(sessions.changes),
      };
    } catch (error) {
      database.exec("ROLLBACK");
      // 事务失败必须在回滚后继续抛出，让外层登记固定清理阶段并阻止验收通过。
      throw error;
    }
  } finally {
    database.close();
  }
}

/**
 * 清理临时 Policy、恢复 mask、发布必要的安全恢复版本并禁用临时用户。
 *
 * 参数：main 预先创建并由 prepareState 逐步填充的资源账本。
 * 返回：清理计数和可选恢复版本；所有可执行阶段尝试完毕后再汇总失败。
 */
async function cleanupState(state) {
  const failures = [];
  let bootstrapSessionsRevoked = 0;
  // bootstrap 登录未返回 Token 时不会继续业务写入，但仍要按唯一 User-Agent 对账可能已提交的 Session。
  if (!state?.bootstrapAdminToken) {
    await attemptCleanup(failures, "bootstrap_session", async () => {
      bootstrapSessionsRevoked = revokeBootstrapSessions(state);
    });
    assert.deepEqual(failures, [], `清理阶段失败：${failures.join(",")}`);
    // 此分支只表示业务资源准备被跳过，bootstrap Session 对账仍已执行并形成计数。
    return { skipped: true, bootstrapSessionsRevoked };
  }

  let tempPolicyDisabled = false;
  await attemptCleanup(failures, "temp_policy", async () => {
    let record = readPolicyCleanupRecord(state, { name: state.tempPolicyIntent.name });
    if (!record) {
      assert.equal(state.tempPolicy, null);
      tempPolicyDisabled = true;
      return;
    }
    assert.equal(record.name, state.tempPolicyIntent.name);
    assert.equal(record.description, state.tempPolicyIntent.description);
    assert.equal(record.createdBy, state.bootstrapAdminUserId);
    if (state.tempPolicy) {
      assert.equal(record.policyId, state.tempPolicy.policy_id);
    }
    if (record.status === "active") {
      const headers = {
        ...authHeaders(
          state.bootstrapAdminToken,
          `req_prd045_browser_policy_cleanup_${state.suffix}`,
        ),
        "Content-Type": "application/json",
        "If-Match": `"${record.version}"`,
      };
      try {
        await controlJson(
          `/policies/${record.policyId}`,
          { method: "PATCH", headers, body: JSON.stringify({ status: "disabled" }) },
          200,
        );
      } catch {
        // 响应中断后以数据库最终状态对账；若写入未提交，下面的 disabled 断言仍会失败。
      }
    }
    record = readPolicyCleanupRecord(state, { name: state.tempPolicyIntent.name });
    assert.ok(!record || record.status === "disabled");
    tempPolicyDisabled = true;
  });

  let recoveryVersion = null;
  if (state.candidateMutated && tempPolicyDisabled && state.baselineCandidateEtag) {
    await attemptCleanup(failures, "snapshot_recovery", async () => {
      const { body: current, response } = await controlJson(
        "/policy-snapshots/current",
        { headers: authHeaders(state.bootstrapAdminToken) },
        200,
      );
      const candidateEtag = response.headers.get("X-Policy-Candidate-ETag");
      assert.equal(current.candidate_status, "valid");
      assert.equal(candidateEtag, state.baselineCandidateEtag);
      if (current.current_snapshot && current.candidate_matches_current === false) {
        const head = current.current_snapshot;
        assert.equal(head.operation, "publish");
        assert.equal(head.snapshot_version, state.baseVersion + 1);
        assert.equal(head.previous_snapshot_version, state.baseVersion);
        assert.equal(head.change_reason, state.publishReason);
        assert.equal(head.created_by?.user_id, state.userIdsByRole.admin);
        assert.equal(response.headers.get("ETag"), `"${head.snapshot_version}"`);
        if (state.publishedSnapshot) {
          assert.equal(head.policy_snapshot_id, state.publishedSnapshot.policy_snapshot_id);
        }
        try {
          const { body: recovered } = await controlJson(
            "/policy-snapshots/publish",
            {
              method: "POST",
              headers: {
                ...authHeaders(
                  state.bootstrapAdminToken,
                  `req_prd045_browser_recovery_${state.suffix}`,
                ),
                "Content-Type": "application/json",
                "If-Match": response.headers.get("ETag"),
                "If-Policy-Candidate-Match": candidateEtag,
              },
              body: JSON.stringify({ reason: state.recoveryReason }),
            },
            201,
          );
          const recovery = recovered.policy_snapshot;
          recoveryVersion = recovery.snapshot_version;
          assert.equal(recoveryVersion, state.baseVersion + 2);
          assert.equal(recovery.previous_snapshot_version, state.baseVersion + 1);
          assert.equal(recovery.change_reason, state.recoveryReason);
          assert.equal(recovery.created_by?.user_id, state.bootstrapAdminUserId);
        } catch {
          const { body: reconciled } = await controlJson(
            "/policy-snapshots/current",
            { headers: authHeaders(state.bootstrapAdminToken) },
            200,
          );
          assert.equal(reconciled.candidate_matches_current, true);
          assert.equal(reconciled.current_snapshot.operation, "publish");
          assert.equal(reconciled.current_snapshot.change_reason, state.recoveryReason);
          assert.equal(reconciled.current_snapshot.created_by?.user_id, state.bootstrapAdminUserId);
          assert.equal(reconciled.current_snapshot.previous_snapshot_version, state.baseVersion + 1);
          recoveryVersion = reconciled.current_snapshot.snapshot_version;
          assert.equal(recoveryVersion, state.baseVersion + 2);
        }
      }
    });
  } else if (state.candidateMutated) {
    // 临时 Policy 未确认禁用或缺少候选基线时禁止恢复发布，避免固化验收规则或他人候选。
    failures.push("snapshot_recovery_precondition");
  }

  let masksRestored = 0;
  for (const [index, item] of state.masksToRestore.entries()) {
    const restored = await attemptCleanup(failures, `mask_restore_${index}`, async () => {
      let record = readPolicyCleanupRecord(state, { policyId: item.policyId });
      assert.ok(record);
      if (record.status === "active" && record.version === item.originalVersion) {
        return;
      }
      assert.equal(record.status, "disabled");
      assert.equal(record.version, item.originalVersion + 1);
      assert.equal(record.updatedBy, state.bootstrapAdminUserId);
      try {
        await controlJson(
          `/policies/${item.policyId}`,
          {
            method: "PATCH",
            headers: {
              ...authHeaders(
                state.bootstrapAdminToken,
                `req_prd045_browser_mask_restore_${item.policyId}`,
              ),
              "Content-Type": "application/json",
              "If-Match": `"${record.version}"`,
            },
            body: JSON.stringify({ status: "active" }),
          },
          200,
        );
      } catch {
        // 恢复响应未知时继续读取精确 Policy；只有状态与预期版本都正确才视为已收口。
      }
      record = readPolicyCleanupRecord(state, { policyId: item.policyId });
      assert.equal(record?.status, "active");
      assert.equal(record?.version, item.originalVersion + 2);
      assert.equal(record?.updatedBy, state.bootstrapAdminUserId);
    });
    masksRestored += restored ? 1 : 0;
  }

  let apiSessionsLoggedOut = 0;
  for (const [role, token] of Object.entries(state.roleTokens)) {
    try {
      await controlJson(
        "/auth/logout",
        { method: "POST", headers: authHeaders(token) },
        200,
      );
      apiSessionsLoggedOut += 1;
    } catch {
      // API logout 响应未知时由 tenant + 精确临时用户事务撤销全部活动 Session。
    }
  }

  let temporaryUsersDisabled = 0;
  let temporarySessionsRevoked = 0;
  await attemptCleanup(failures, "temporary_users", async () => {
    const cleaned = disableTemporaryUsersAndSessions(state);
    temporaryUsersDisabled = cleaned.usersDisabled;
    temporarySessionsRevoked = cleaned.sessionsRevoked;
  });

  try {
    await controlJson(
      "/auth/logout",
      { method: "POST", headers: authHeaders(state.bootstrapAdminToken) },
      200,
    );
    apiSessionsLoggedOut += 1;
  } catch {
    // bootstrap logout 也以唯一 User-Agent 的数据库最终状态为准，避免响应丢失造成误报或遗留。
  }
  await attemptCleanup(failures, "bootstrap_session", async () => {
    bootstrapSessionsRevoked = revokeBootstrapSessions(state);
  });

  assert.deepEqual(failures, [], `清理阶段失败：${failures.join(",")}`);
  return {
    skipped: false,
    tempPolicyDisabled,
    masksRestored,
    temporaryUsersDisabled,
    temporarySessionsRevoked,
    apiSessionsLoggedOut,
    bootstrapSessionsRevoked,
    recoveryVersion,
  };
}

/**
 * 启动 Chrome，顺序执行 admin、developer、viewer 和后端权限验收。
 *
 * 输出：只包含版本、布局、计数和布尔值，不包含账号、Token、原因或 Policy ID。
 */
async function main() {
  assertLocalScope();
  await mkdir(ARTIFACT_DIR, { recursive: true });
  const state = createStateLedger();
  let result = null;
  let cleanup = null;
  let browser = null;
  try {
    browser = await chromium.launch({
      executablePath: CHROME_PATH,
      headless: process.env.METAHUB_BROWSER_HEADED !== "1",
    });
    await prepareState(state);
    const backendRbac = await verifyBackendRbac(state);
    const admin = await runAdminAcceptance(browser, state);
    const developer = await runNonAdminAcceptance(browser, state.accounts.developer, "developer");
    const viewer = await runNonAdminAcceptance(browser, state.accounts.viewer, "viewer");
    result = { status: "passed", admin, developer, viewer, backendRbac };
  } finally {
    try {
      await browser?.close();
    } finally {
      // 浏览器关闭失败也不能阻止服务端资源回收；账本从第一项副作用起持续可用。
      cleanup = await cleanupState(state);
    }
  }
  assert.ok(result);
  result.cleanup = cleanup;
  console.log(JSON.stringify(result, null, 2));
}

await main();
