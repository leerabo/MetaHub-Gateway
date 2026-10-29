/**
 * PRD-060 PAGE-087 与公开邀请页真实浏览器验收。
 *
 * 只访问三个本机 loopback origin。浏览器 profile、截图和脱敏 JSON 证据全部写入调用方指定的
 * D:/MetaHub/.tmp 新目录；密码、Session、邀请 Token、完整链接和邮件文案只在进程内存中短暂存在。
 */

import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { lstat, mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";

// Playwright 只由 Codex 桌面运行库通过 NODE_PATH 提供，不增加 Console 生产依赖。
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

// 物理仓库根和证据根必须由脚本自身定位，不能信任调用方提供的可逃逸工作目录。
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const TEMP_ROOT = path.join(REPOSITORY_ROOT, ".tmp");

// 视口来自 PRD 冻结验收口径；页面字体不能随 viewport 动态缩放。
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);

// 只允许既有 Session 槽位保存认证数据；邀请 Token 不得进入任一 Storage key/value。
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);


/** 用固定 stage/code 表示失败，禁止底层浏览器异常携带 Token URL 或页面正文进入终端。 */
class BrowserAcceptanceFailure extends Error {
  /** 保存脱敏失败分类。 */
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 失败关闭断言；stage/code 只能使用代码内固定值。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** 包装 Playwright 调用并移除原始异常文本。 */
async function runBrowserOperation(stage, operation) {
  try {
    return await operation();
  } catch (error) {
    // 已脱敏失败保持原 stage/code；其他异常全部折叠，禁止传播 Token URL 或 DOM 正文。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(stage, "operation_failed");
  }
}


/** 把配置地址收敛为指定本机端口，防止验收凭据被发送到外部网络。 */
function normalizeLoopbackOrigin(value, expectedPort, stage) {
  let parsed;
  try {
    parsed = new URL(String(value));
  } catch {
    throw new BrowserAcceptanceFailure(stage, "url_invalid");
  }
  requireCondition(parsed.protocol === "http:", stage, "protocol");
  requireCondition(["localhost", "127.0.0.1"].includes(parsed.hostname), stage, "host");
  requireCondition(parsed.port === String(expectedPort), stage, "port");
  requireCondition(parsed.pathname === "/" && !parsed.search && !parsed.hash, stage, "shape");
  return parsed.origin;
}


/** 创建 D:/MetaHub/.tmp 下的新普通证据目录，拒绝覆盖、父级逃逸或链接节点。 */
async function createArtifactDirectory(value) {
  const target = path.resolve(String(value || ""));
  const relative = path.relative(TEMP_ROOT, target);
  requireCondition(
    Boolean(relative) && !relative.startsWith("..") && !path.isAbsolute(relative),
    "config.artifact_directory",
    "outside_temp",
  );
  try {
    await mkdir(target, { recursive: false });
    const information = await lstat(target);
    requireCondition(
      information.isDirectory() && !information.isSymbolicLink(),
      "config.artifact_directory",
      "not_plain_directory",
    );
    await mkdir(path.join(target, "screenshots"));
    await mkdir(path.join(target, "browser-home"));
    await mkdir(path.join(target, "browser-home", "local"));
    await mkdir(path.join(target, "browser-home", "roaming"));
    await mkdir(path.join(target, "downloads"));
  } catch (error) {
    // 目录合同失败可以原样保留固定分类，文件系统异常只转换为稳定失败码。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure("config.artifact_directory", "exists_or_unwritable");
  }
  return target;
}


/** 读取本地地址、浏览器和身份；敏感字段不会复制进最终 evidence。 */
async function readConfiguration(environment = process.env) {
  const consoleOrigin = normalizeLoopbackOrigin(
    environment.METAHUB_CONSOLE_URL || "http://localhost:5173",
    5173,
    "config.console",
  );
  const controlOrigin = normalizeLoopbackOrigin(
    environment.METAHUB_CONTROL_URL || "http://localhost:8001",
    8001,
    "config.control",
  );
  const gatewayOrigin = normalizeLoopbackOrigin(
    environment.METAHUB_GATEWAY_URL || "http://localhost:8000",
    8000,
    "config.gateway",
  );
  const chromePath = String(environment.METAHUB_PLAYWRIGHT_CHROME_PATH || "");
  const adminPassword = String(environment.METAHUB_ACCEPTANCE_ADMIN_PASSWORD || "");
  const invitedPassword = String(environment.METAHUB_ACCEPTANCE_INVITED_PASSWORD || "");
  requireCondition(new Set([consoleOrigin, controlOrigin, gatewayOrigin]).size === 3, "config.origins", "duplicate");
  requireCondition(path.isAbsolute(chromePath) && existsSync(chromePath), "config.chrome", "missing");
  requireCondition(adminPassword.length >= 12, "config.identity", "admin_password_missing");
  requireCondition(invitedPassword.length >= 8, "config.identity", "invited_password_missing");
  const artifactDirectory = await createArtifactDirectory(environment.METAHUB_ACCEPTANCE_ARTIFACT_DIR);
  return Object.freeze({
    artifactDirectory,
    consoleOrigin,
    controlOrigin,
    gatewayOrigin,
    chromePath,
    tenantSlug: String(environment.METAHUB_ACCEPTANCE_TENANT || "default"),
    adminEmail: String(environment.METAHUB_ACCEPTANCE_EMAIL || "admin@metahub.local"),
    adminPassword,
    invitedPassword,
  });
}


/** 安装外部网络门禁并累计脱敏的请求、错误和关键状态码。 */
async function installBrowserGuards(context, page, config, tracker) {
  const allowedOrigins = new Set([
    config.consoleOrigin,
    config.controlOrigin,
    config.gatewayOrigin,
  ]);
  page.on("pageerror", () => { tracker.pageErrorCount += 1; });
  page.on("console", (message) => {
    // 只累计错误数量，不保存可能含页面数据的 Console 文本或参数。
    if (message.type() === "error") tracker.consoleErrorCount += 1;
  });
  page.on("request", (request) => {
    try {
      const target = new URL(request.url());
      if (
        target.origin === config.controlOrigin
        && /^\/invitations\/[^/]+\/accept$/.test(target.pathname)
        && request.method() === "POST"
      ) {
        const headers = request.headers();
        tracker.acceptRequestCount += 1;
        tracker.acceptAuthorizationSent ||= Object.hasOwn(headers, "authorization");
        tracker.acceptCookieSent ||= Object.hasOwn(headers, "cookie");
      }
    } catch {
      // 非法 URL 会在 route 门禁中阻断；事件监听器不保留原字符串。
    }
  });
  page.on("response", (response) => {
    try {
      const target = new URL(response.url());
      const method = response.request().method();
      if (target.origin !== config.controlOrigin) return;
      // 只记录固定认证端点状态码，URL、Header 与响应正文一律不进入 evidence。
      if (target.pathname === "/auth/login" && method === "POST") tracker.loginStatuses.push(response.status());
      if (target.pathname === "/invitations" && method === "POST") tracker.createStatuses.push(response.status());
      if (/^\/invitations\/[^/]+\/accept$/.test(target.pathname) && method === "POST") {
        tracker.acceptStatuses.push(response.status());
      }
      // 注销状态同样只保留整数，便于证明 Session 已执行后端销毁。
      if (target.pathname === "/auth/logout" && method === "POST") tracker.logoutStatuses.push(response.status());
    } catch {
      // 只累计已规范化的本机合同，不记录原始 URL。
    }
  });
  await context.route("**/*", async (route) => {
    try {
      const target = new URL(route.request().url());
      if (["data:", "blob:", "about:"].includes(target.protocol) || allowedOrigins.has(target.origin)) {
        await route.continue();
        return;
      }
    } catch {
      // 无法规范化的请求与第三方请求同样失败关闭。
    }
    tracker.blockedRequestCount += 1;
    await route.abort("blockedbyclient");
  });
}


/** 通过真实登录表单建立管理员会话，只记录状态，不读取或输出 Session 值。 */
async function loginAdministrator(page, config) {
  await page.goto(`${config.consoleOrigin}/`, { waitUntil: "domcontentloaded" });
  const form = page.locator("#login-form");
  await form.waitFor({ state: "visible", timeout: 30000 });
  await form.locator('[name="tenantSlug"]').fill(config.tenantSlug);
  await form.locator('[name="email"]').fill(config.adminEmail);
  await form.locator('[name="password"]').fill(config.adminPassword);
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/auth/login"
        && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await form.locator('button[type="submit"]').click();
  const response = await responsePromise;
  requireCondition(response.status() === 200, "browser.login", "status");
  await page.locator('[data-console-nav="users"]').waitFor({ state: "visible", timeout: 30000 });
}


/** 打开 PAGE-086/087、真实创建邀请并通过 Clipboard 获取一次性同源链接。 */
async function createInvitationThroughDialog(page, config, screenshots) {
  await page.locator('[data-console-nav="users"]').click();
  await page.locator("#users-title").waitFor({ state: "visible", timeout: 30000 });
  await page.locator("#invite-user-button").click();
  const dialog = page.locator("#invitation-dialog");
  await dialog.waitFor({ state: "visible", timeout: 10000 });
  const expiryOptions = await dialog.locator('[name="expiresInHours"] option').evaluateAll(
    (options) => options.map((option) => ({ value: option.value, selected: option.selected })),
  );
  requireCondition(
    JSON.stringify(expiryOptions) === JSON.stringify([
      { value: "24", selected: false },
      { value: "72", selected: true },
      { value: "168", selected: false },
    ]),
    "browser.dialog",
    "expiry_options",
  );
  const invitedEmail = `prd060-browser-${Date.now().toString(36)}@example.invalid`;
  await dialog.locator('[name="email"]').fill(invitedEmail);
  await dialog.locator('[name="role"]').selectOption("developer");
  await dialog.locator('[name="expiresInHours"]').selectOption("24");
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/invitations"
        && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await dialog.locator('.invitation-create-button').click();
  const response = await responsePromise;
  requireCondition(response.status() === 201, "browser.dialog", "create_status");
  const responsePayload = await response.json();
  const acceptToken = String(responsePayload?.accept_token || "");
  requireCondition(/^[A-Za-z0-9_-]{43}$/.test(acceptToken), "browser.dialog", "token_shape");
  await dialog.locator(".invitation-created-result").waitFor({ state: "visible", timeout: 10000 });
  requireCondition(await dialog.locator("[data-invitation-close]").isEnabled(), "browser.dialog", "close_disabled");
  requireCondition(
    !(await page.locator("body").textContent()).includes(acceptToken),
    "browser.dialog",
    "token_in_dom",
  );
  for (const viewport of VIEWPORTS) {
    screenshots.push(await captureState(page, config, "invitation-dialog", viewport, "#invitation-dialog"));
  }
  await dialog.locator("[data-copy-invitation-link]").click();
  const acceptLink = await page.evaluate(() => navigator.clipboard.readText());
  requireCondition(
    acceptLink === `${config.consoleOrigin}/#accept-invitation/${acceptToken}`,
    "browser.dialog",
    "clipboard_link",
  );
  await page.evaluate(() => navigator.clipboard.writeText(""));
  const privacy = await page.evaluate(({ token, authKeys }) => ({
    url_hit: location.href.includes(token),
    dom_hit: document.documentElement.innerHTML.includes(token),
    storage_hit: [...Object.entries(localStorage), ...Object.entries(sessionStorage)]
      .some(([key, value]) => !authKeys.includes(key) && `${key}${value}`.includes(token)),
  }), { token: acceptToken, authKeys: AUTH_STORAGE_KEYS });
  requireCondition(!privacy.url_hit && !privacy.dom_hit && !privacy.storage_hit, "browser.dialog", "privacy");
  await dialog.locator("[data-invitation-close]").click();
  await dialog.waitFor({ state: "hidden", timeout: 10000 });
  // 原始 Token 与完整链接只在私有内存对象中交给匿名上下文，绝不进入 DOM 或 evidence。
  return { acceptLink, acceptToken };
}


/** 在全新匿名上下文打开链接，验证地址 scrub、本地确认密码和无 Session 接受。 */
async function acceptInvitationPublicly(page, config, tracker, secrets, screenshots) {
  await page.goto(secrets.acceptLink, { waitUntil: "domcontentloaded" });
  const form = page.locator("#invitation-accept-form");
  await form.waitFor({ state: "visible", timeout: 30000 });
  requireCondition(page.url() === `${config.consoleOrigin}/#accept-invitation`, "browser.public", "url_not_scrubbed");
  const initialPrivacy = await page.evaluate((token) => ({
    dom_hit: document.documentElement.innerHTML.includes(token),
    local_hit: JSON.stringify(Object.entries(localStorage)).includes(token),
    session_hit: JSON.stringify(Object.entries(sessionStorage)).includes(token),
  }), secrets.acceptToken);
  requireCondition(
    !initialPrivacy.dom_hit && !initialPrivacy.local_hit && !initialPrivacy.session_hit,
    "browser.public",
    "token_leak",
  );
  for (const viewport of VIEWPORTS) {
    screenshots.push(await captureState(page, config, "invitation-public-form", viewport, "#invitation-accept-form"));
  }

  await form.locator('[name="displayName"]').fill("PRD-060 Browser User");
  await form.locator('[name="password"]').fill(config.invitedPassword);
  await form.locator('[name="confirmPassword"]').fill(`${config.invitedPassword}x`);
  await form.locator('button[type="submit"]').click();
  await page.getByText("两次输入的密码不一致", { exact: true }).waitFor({ state: "visible", timeout: 10000 });
  requireCondition(tracker.acceptRequestCount === 0, "browser.public", "mismatch_sent_request");

  const retryForm = page.locator("#invitation-accept-form");
  await retryForm.locator('[name="displayName"]').fill("PRD-060 Browser User");
  await retryForm.locator('[name="password"]').fill(config.invitedPassword);
  await retryForm.locator('[name="confirmPassword"]').fill(config.invitedPassword);
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && /^\/invitations\/[^/]+\/accept$/.test(target.pathname)
        && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await retryForm.locator('button[type="submit"]').click();
  const response = await responsePromise;
  requireCondition(response.status() === 201, "browser.public", "accept_status");
  await page.getByText("已加入团队", { exact: true }).waitFor({ state: "visible", timeout: 10000 });
  requireCondition(tracker.acceptRequestCount === 1, "browser.public", "accept_request_count");
  requireCondition(!tracker.acceptAuthorizationSent, "browser.public", "authorization_sent");
  requireCondition(!tracker.acceptCookieSent, "browser.public", "cookie_sent");
  requireCondition(page.url() === `${config.consoleOrigin}/#accept-invitation`, "browser.public", "success_url");
  requireCondition(
    !(await page.locator("html").textContent()).includes(secrets.acceptToken),
    "browser.public",
    "success_token_dom",
  );
  await page.setViewportSize({ width: VIEWPORTS[0].width, height: VIEWPORTS[0].height });
  screenshots.push(await captureState(
    page,
    config,
    "invitation-public-success",
    VIEWPORTS[0],
    ".invitation-public-result",
  ));
  await page.locator("[data-invitation-login]").click();
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30000 });
  requireCondition(!page.url().includes("accept-invitation"), "browser.public", "login_sentinel_retained");
}


/** 读取当前布局的固定指标，避免截图看似成功但控件溢出或移出视口。 */
async function readLayoutMetrics(page, selector) {
  return page.evaluate((targetSelector) => {
    const target = document.querySelector(targetSelector);
    const rect = target?.getBoundingClientRect();
    const visibleControls = [...(target?.querySelectorAll("button,input,select,textarea") ?? [])]
      .filter((element) => {
        const style = getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
      });
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      target_visible: Boolean(rect && rect.width > 0 && rect.height > 0),
      target_left: rect?.left ?? -1,
      target_right: rect?.right ?? -1,
      target_top: rect?.top ?? -1,
      target_bottom: rect?.bottom ?? -1,
      control_overflow_count: visibleControls.filter(
        (element) => element.scrollWidth > element.clientWidth + 1,
      ).length,
    };
  }, selector);
}


/** 验证布局在指定视口无横向溢出、控件文本溢出或目标区域越界。 */
function assertLayoutMetrics(metrics, viewport, stage) {
  requireCondition(
    metrics.viewport_width === viewport.width && metrics.viewport_height === viewport.height,
    stage,
    "viewport",
  );
  requireCondition(
    metrics.document_scroll_width <= viewport.width + 1 && metrics.body_scroll_width <= viewport.width + 1,
    stage,
    "horizontal_overflow",
  );
  requireCondition(metrics.target_visible, stage, "target_hidden");
  requireCondition(metrics.target_left >= -1 && metrics.target_right <= viewport.width + 1, stage, "target_width");
  requireCondition(metrics.target_top >= -1 && metrics.target_bottom <= viewport.height + 1, stage, "target_height");
  requireCondition(metrics.control_overflow_count === 0, stage, "control_text_overflow");
  return metrics;
}


/** 等待两帧使 CSS、dialog 和焦点变化稳定，避免截图捕获过渡中间态。 */
async function settleLayout(page) {
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}


/** 捕获固定视口 PNG，执行像素非空和布局双重检查，并只返回脱敏摘要。 */
async function captureState(page, config, stateName, viewport, selector) {
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  await settleLayout(page);
  const layout = assertLayoutMetrics(
    await readLayoutMetrics(page, selector),
    viewport,
    `browser.layout.${stateName}.${viewport.name}`,
  );
  const filename = `${stateName}-${viewport.name}.png`;
  const screenshotPath = path.join(config.artifactDirectory, "screenshots", filename);
  const buffer = await page.screenshot({ path: screenshotPath, type: "png", fullPage: false });
  const pixels = inspectPngPixels(buffer, viewport, `browser.screenshot.${stateName}.${viewport.name}`);
  return {
    state: stateName,
    viewport: viewport.name,
    filename,
    sha256: createHash("sha256").update(buffer).digest("hex"),
    byte_length: buffer.length,
    layout: {
      horizontal_overflow: false,
      target_in_viewport: true,
      control_text_overflow_count: layout.control_overflow_count,
    },
    ...pixels,
  };
}


/** 注销管理员并验证认证 Storage 和受保护 DOM 已销毁。 */
async function logoutAdministrator(page, config) {
  await page.setViewportSize({ width: VIEWPORTS[0].width, height: VIEWPORTS[0].height });
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/auth/logout"
        && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await page.locator("#logout-button").click();
  const response = await responsePromise;
  requireCondition(response.status() === 200, "browser.logout", "status");
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30000 });
  const cleared = await page.evaluate((authKeys) => ({
    local_count: localStorage.length,
    auth_slot_count: authKeys.filter((key) => sessionStorage.getItem(key) !== null).length,
    protected_count: document.querySelectorAll(".console-shell,#invitation-dialog").length,
  }), AUTH_STORAGE_KEYS);
  requireCondition(cleared.local_count === 0, "browser.logout", "local_storage");
  requireCondition(cleared.auth_slot_count === 0, "browser.logout", "auth_storage");
  requireCondition(cleared.protected_count === 0, "browser.logout", "protected_dom");
}


/** 启动离线 Edge，完成管理弹窗、公开接受、双视口、隐私、网络和注销验收。 */
async function executeAcceptance(config) {
  const tracker = {
    blockedRequestCount: 0,
    pageErrorCount: 0,
    consoleErrorCount: 0,
    loginStatuses: [],
    createStatuses: [],
    acceptStatuses: [],
    logoutStatuses: [],
    acceptRequestCount: 0,
    acceptAuthorizationSent: false,
    acceptCookieSent: false,
  };
  const screenshots = [];
  const browserHome = path.join(config.artifactDirectory, "browser-home");
  let browser = null;
  let adminContext = null;
  let publicContext = null;
  let secrets = null;
  try {
    browser = await runBrowserOperation("browser.launch", () => chromium.launch({
      executablePath: config.chromePath,
      headless: true,
      downloadsPath: path.join(config.artifactDirectory, "downloads"),
      env: {
        ...process.env,
        TEMP: config.artifactDirectory,
        TMP: config.artifactDirectory,
        HOME: browserHome,
        USERPROFILE: browserHome,
        LOCALAPPDATA: path.join(browserHome, "local"),
        APPDATA: path.join(browserHome, "roaming"),
      },
      args: [
        "--disable-background-networking",
        "--disable-breakpad",
        "--disable-component-update",
        "--disable-crash-reporter",
        "--disable-default-apps",
        "--disable-domain-reliability",
        "--disable-features=AutofillServerCommunication,OptimizationHints,MediaRouter",
        "--disable-gpu",
        "--no-default-browser-check",
        "--no-first-run",
      ],
    }));
    adminContext = await browser.newContext({
      viewport: { width: VIEWPORTS[0].width, height: VIEWPORTS[0].height },
      locale: "zh-CN",
      serviceWorkers: "block",
      acceptDownloads: false,
      permissions: ["clipboard-read", "clipboard-write"],
    });
    const adminPage = await adminContext.newPage();
    await installBrowserGuards(adminContext, adminPage, config, tracker);
    await runBrowserOperation("browser.login", () => loginAdministrator(adminPage, config));
    secrets = await runBrowserOperation(
      "browser.dialog",
      () => createInvitationThroughDialog(adminPage, config, screenshots),
    );

    publicContext = await browser.newContext({
      viewport: { width: VIEWPORTS[0].width, height: VIEWPORTS[0].height },
      locale: "zh-CN",
      serviceWorkers: "block",
      acceptDownloads: false,
    });
    const publicPage = await publicContext.newPage();
    await installBrowserGuards(publicContext, publicPage, config, tracker);
    await runBrowserOperation(
      "browser.public",
      () => acceptInvitationPublicly(publicPage, config, tracker, secrets, screenshots),
    );
    secrets.acceptToken = "";
    secrets.acceptLink = "";
    await runBrowserOperation("browser.logout", () => logoutAdministrator(adminPage, config));

    requireCondition(tracker.blockedRequestCount === 0, "browser.network", "blocked_request");
    requireCondition(tracker.pageErrorCount === 0, "browser.errors", "page_error");
    requireCondition(tracker.consoleErrorCount === 0, "browser.errors", "console_error");
    requireCondition(tracker.createStatuses.includes(201), "browser.http", "create_status");
    requireCondition(tracker.acceptStatuses.length === 1 && tracker.acceptStatuses[0] === 201, "browser.http", "accept_status");
    requireCondition(tracker.loginStatuses.includes(200), "browser.http", "login_status");
    requireCondition(tracker.logoutStatuses.includes(200), "browser.http", "logout_status");
    return {
      status: "passed",
      prd_id: "PRD-060",
      page_ids: ["PAGE-086", "PAGE-087", "PUBLIC-INVITATION"],
      checks: {
        http_statuses: { login: 200, create_invitation: 201, accept_invitation: 201, logout: 200 },
        dialog: {
          expiry_options: [24, 72, 168],
          default_expiry_hours: 72,
          raw_token_in_template_dom: false,
          clipboard_link_verified: true,
        },
        public_accept: {
          address_scrubbed: true,
          password_confirmation_local: true,
          authorization_sent: false,
          cookie_sent: false,
          login_sentinel_cleared: true,
        },
        privacy: {
          raw_token_url_dom_storage_hits: 0,
          clipboard_cleared: true,
          auth_storage_cleared_after_logout: true,
        },
        network: { blocked_request_count: 0, external_request_count: 0 },
        browser_errors: { page: 0, console: 0 },
      },
      screenshots,
    };
  } finally {
    // 无论 Browser 哪一步失败都先覆写内存 Secret，再关闭上下文和进程。
    if (secrets) {
      secrets.acceptToken = "";
      secrets.acceptLink = "";
    }
    await publicContext?.close().catch(() => undefined);
    await adminContext?.close().catch(() => undefined);
    await browser?.close().catch(() => undefined);
  }
}


/** CLI 入口只输出固定成功摘要或脱敏失败分类。 */
async function main() {
  try {
    const config = await readConfiguration();
    const evidence = await executeAcceptance(config);
    await writeFile(
      path.join(config.artifactDirectory, "browser-acceptance.json"),
      `${JSON.stringify(evidence, null, 2)}\n`,
      { encoding: "utf8", flag: "wx" },
    );
    process.stdout.write(`${JSON.stringify({
      status: evidence.status,
      prd_id: evidence.prd_id,
      screenshot_count: evidence.screenshots.length,
    })}\n`);
    return 0;
  } catch (error) {
    const failure = error instanceof BrowserAcceptanceFailure
      ? { stage: error.stage, code: error.code }
      : { stage: "browser.acceptance", code: "unexpected_failure" };
    process.stderr.write(`${JSON.stringify({ status: "failed", ...failure })}\n`);
    return 1;
  }
}


export {
  BrowserAcceptanceFailure,
  VIEWPORTS,
  assertLayoutMetrics,
  normalizeLoopbackOrigin,
  readLayoutMetrics,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
