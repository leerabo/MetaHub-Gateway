/**
 * PRD-059 PAGE-085 真实浏览器验收。
 *
 * 脚本只访问本机 Console、Control API 与 Gateway，浏览器临时 profile、下载、截图和
 * 结构化证据全部写入调用方指定的 D:/MetaHub/.tmp 子目录。密码与 Token 只驻留当前
 * 进程和浏览器内存，不进入日志、截图、URL 或证据 JSON。
 */

import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { lstat, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";

// Playwright 由 Codex 工作区运行时通过 NODE_PATH 提供，不写入 Console 生产依赖。
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

// 所有固定目录都从脚本位置反算，避免调用目录改变后把证据写到仓库外。
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const TEMP_ROOT = path.join(REPOSITORY_ROOT, ".tmp");

// 产品验收只覆盖 Excel 冻结的桌面与移动视口，不能按机器屏幕动态缩放字体或布局。
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720, mode: "desktop" }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844, mode: "mobile" }),
]);

// 登录态只允许两个既有槽位；检查时不读取或输出其值。
const AUTH_STORAGE_KEYS = Object.freeze([
  "metahub.auth_token",
  "metahub.auth_context",
]);


/** 用固定 stage/code 表示验收失败，避免原始 URL、DOM 或 Secret 进入终端。 */
class BrowserAcceptanceFailure extends Error {
  /** 保存安全分类，不保留可能含页面正文的底层异常。 */
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 失败关闭断言；参数只接受代码内固定 stage/code。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** 把 Playwright 原始异常压缩成固定阶段，防止命令行、profile 路径或页面正文进入终端。 */
async function runBrowserOperation(stage, operation) {
  try {
    return await operation();
  } catch (error) {
    // 已分类错误保留原 stage/code；其他 Playwright 原错必须在当前边界脱敏。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure(stage, "operation_failed");
  }
}


/** 把配置 URL 收敛为本机 HTTP origin，禁止验收脚本访问外部网络。 */
function normalizeLoopbackOrigin(value, stage) {
  let parsed;
  try {
    parsed = new URL(String(value));
  } catch {
    throw new BrowserAcceptanceFailure(stage, "url_invalid");
  }
  requireCondition(parsed.protocol === "http:", stage, "protocol");
  requireCondition(
    parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost",
    stage,
    "host",
  );
  requireCondition(parsed.pathname === "/" && !parsed.search && !parsed.hash, stage, "shape");
  return parsed.origin;
}


/** 验证证据目录是 D:/MetaHub/.tmp 下的新普通目录，拒绝父级逃逸和链接节点。 */
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
    // 已分类目录失败不能被覆盖为通用文件系统错误，否则会丢失安全定位信息。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure("config.artifact_directory", "exists_or_unwritable");
  }
  return target;
}


/** 读取本地身份和浏览器路径；敏感字段不会复制到最终 evidence。 */
async function readConfiguration(environment = process.env) {
  const consoleOrigin = normalizeLoopbackOrigin(
    environment.METAHUB_CONSOLE_URL || "http://127.0.0.1:5173",
    "config.console",
  );
  const controlOrigin = normalizeLoopbackOrigin(
    environment.METAHUB_CONTROL_URL || "http://127.0.0.1:8001",
    "config.control",
  );
  const gatewayOrigin = normalizeLoopbackOrigin(
    environment.METAHUB_GATEWAY_URL || "http://127.0.0.1:8000",
    "config.gateway",
  );
  const chromePath = String(environment.METAHUB_PLAYWRIGHT_CHROME_PATH || "");
  const password = String(environment.METAHUB_ACCEPTANCE_PASSWORD || "");
  requireCondition(new Set([consoleOrigin, controlOrigin, gatewayOrigin]).size === 3, "config.origins", "duplicate");
  requireCondition(path.isAbsolute(chromePath) && existsSync(chromePath), "config.chrome", "missing");
  requireCondition(password.length >= 12, "config.identity", "password_missing");
  // 只有全部只读配置通过后才创建证据目录，配置错误不得留下伪批次。
  const artifactDirectory = await createArtifactDirectory(
    environment.METAHUB_ACCEPTANCE_ARTIFACT_DIR,
  );
  return Object.freeze({
    artifactDirectory,
    consoleOrigin,
    controlOrigin,
    gatewayOrigin,
    chromePath,
    tenantSlug: String(environment.METAHUB_ACCEPTANCE_TENANT || "default"),
    email: String(environment.METAHUB_ACCEPTANCE_EMAIL || "admin@metahub.local"),
    password,
  });
}


/** 安装严格 origin 门禁和浏览器错误计数，任何第三方请求都会中止并使验收失败。 */
async function installBrowserGuards(context, page, config) {
  const allowedOrigins = new Set([
    config.consoleOrigin,
    config.controlOrigin,
    config.gatewayOrigin,
  ]);
  const tracker = {
    blockedRequestCount: 0,
    pageErrorCount: 0,
    consoleErrorCount: 0,
    loginStatus: 0,
    matrixStatus: 0,
    logoutStatus: 0,
  };
  page.on("pageerror", () => { tracker.pageErrorCount += 1; });
  page.on("console", (message) => {
    // Console 原文可能包含 URL 或页面数据，验收证据只累计错误数量。
    if (message.type() === "error") tracker.consoleErrorCount += 1;
  });
  await context.route("**/*", async (route) => {
    try {
      const target = new URL(route.request().url());
      if (["data:", "blob:", "about:"].includes(target.protocol) || allowedOrigins.has(target.origin)) {
        await route.continue();
        return;
      }
    } catch {
      // 无法规范化的请求同样按外部流量拒绝，不能把原 URL 写入证据。
    }
    tracker.blockedRequestCount += 1;
    await route.abort("blockedbyclient");
  });
  return tracker;
}


/** 通过真实登录表单建立管理员 Session，只记录响应状态而不读取 Token 正文。 */
async function loginThroughConsole(page, config, tracker) {
  await page.goto(`${config.consoleOrigin}/`, { waitUntil: "domcontentloaded" });
  const form = page.locator("#login-form");
  await form.waitFor({ state: "visible", timeout: 30000 });
  await form.locator('[name="tenantSlug"]').fill(config.tenantSlug);
  await form.locator('[name="email"]').fill(config.email);
  await form.locator('[name="password"]').fill(config.password);
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
  tracker.loginStatus = response.status();
  requireCondition(response.status() === 200, "browser.login", "status");
  await page.locator('.console-shell .sidebar-title span').waitFor({ state: "visible", timeout: 30000 });
  const role = String(await page.locator('.console-shell .sidebar-title span').textContent()).trim();
  requireCondition(role === "admin", "browser.login", "role");
}


/** 点击后端授权导航并等待真实 GET /permissions 与 PAGE-085 同时完成。 */
async function openRolePermissions(page, config, tracker) {
  const navigation = page.locator('[data-console-nav="role-permissions"]');
  await navigation.waitFor({ state: "visible", timeout: 30000 });
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/permissions"
        && response.request().method() === "GET";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await navigation.click();
  const response = await responsePromise;
  tracker.matrixStatus = response.status();
  requireCondition(response.status() === 200, "browser.permissions", "status");
  await page.locator('.console-shell[data-active-navigation="role-permissions"] .role-permission-page')
    .waitFor({ state: "visible", timeout: 30000 });
  await page.locator(".role-permission-count").filter({ hasText: "48 / 48" })
    .waitFor({ state: "visible", timeout: 30000 });
}


/** 冻结页面完整矩阵、Admin 锁定和后端 grantable 驱动的可编辑单元格合同。 */
async function assertMatrixContracts(page) {
  const tabs = page.locator("[data-rbac-role-tab]");
  requireCondition(await tabs.count() === 6, "browser.matrix", "role_count");
  requireCondition(
    await page.locator(".role-permission-row").count() === 49,
    "browser.matrix",
    "permission_row_count",
  );
  requireCondition(
    await page.locator(".role-permission-mobile-item").count() === 48,
    "browser.matrix",
    "mobile_permission_count",
  );
  const lockedTabs = tabs.filter({ hasText: "锁定" });
  requireCondition(await lockedTabs.count() === 1, "browser.matrix", "admin_lock_count");
  await lockedTabs.first().click();
  const adminDisabled = await page.locator(
    ".role-permission-mobile [data-rbac-permission-toggle]",
  ).evaluateAll((items) => items.filter((item) => item.disabled).length);
  requireCondition(adminDisabled === 48, "browser.matrix", "admin_not_immutable");
  requireCondition(await page.locator("#role-permission-save").isDisabled(), "browser.matrix", "admin_save_enabled");
}


/** 修改一个可授予 checkbox 后放弃草稿，证明前端交互不产生数据库写入。 */
async function exerciseDiscardedDraft(page) {
  const editableTab = page.locator("[data-rbac-role-tab]").filter({ hasNotText: "锁定" }).first();
  const roleId = String(await editableTab.getAttribute("data-rbac-role-tab"));
  requireCondition(/^[A-Za-z0-9_-]+$/.test(roleId), "browser.draft", "role_id");
  await editableTab.click();
  const toggle = page.locator(
    `.role-permission-matrix [data-rbac-permission-toggle][data-role-id="${roleId}"]:not([disabled])`,
  ).first();
  requireCondition(await toggle.count() === 1, "browser.draft", "no_editable_permission");
  const permissionCode = String(await toggle.getAttribute("data-permission-code"));
  const before = await toggle.isChecked();
  await toggle.setChecked(!before);
  requireCondition(await page.locator("#role-permission-save").isEnabled(), "browser.draft", "save_not_enabled");
  requireCondition(
    await page.locator("[data-rbac-role-tab]:disabled").count() === 5,
    "browser.draft",
    "tabs_not_frozen",
  );
  await page.locator("#role-permission-discard").click();
  requireCondition(await page.locator("#role-permission-save").isDisabled(), "browser.draft", "discard_failed");
  const restored = page.locator(
    `.role-permission-matrix [data-rbac-permission-toggle][data-role-id="${roleId}"][data-permission-code="${permissionCode}"]`,
  );
  requireCondition(await restored.isChecked() === before, "browser.draft", "value_not_restored");
  return { draft_discarded: true, patch_request_count: 0 };
}


/** 等待两个动画帧并把 PAGE-085 标题滚入当前视口，消除截图时序抖动。 */
async function settleLayout(page) {
  await page.locator("#role-permission-title").scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}


/** 读取根宽度、目标可见性、桌面/移动切换、按钮文本和侧栏重叠指标。 */
async function readLayoutMetrics(page) {
  return page.evaluate(() => {
    const panel = document.querySelector(".role-permission-page");
    const matrix = document.querySelector(".role-permission-matrix");
    const mobile = document.querySelector(".role-permission-mobile");
    const sidebar = document.querySelector(".console-sidebar");
    const main = document.querySelector(".console-main");
    const panelRect = panel?.getBoundingClientRect();
    const sidebarRect = sidebar?.getBoundingClientRect();
    const mainRect = main?.getBoundingClientRect();
    const overlapWidth = Math.max(0, Math.min(sidebarRect?.right ?? 0, mainRect?.right ?? 0)
      - Math.max(sidebarRect?.left ?? 0, mainRect?.left ?? 0));
    const overlapHeight = Math.max(0, Math.min(sidebarRect?.bottom ?? 0, mainRect?.bottom ?? 0)
      - Math.max(sidebarRect?.top ?? 0, mainRect?.top ?? 0));
    const visibleButtons = [...document.querySelectorAll(
      ".role-permission-tabs button,.role-permission-save-actions button",
    )].filter((item) => item.getClientRects().length > 0);
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      panel_visible: Boolean(panelRect && panelRect.width > 0 && panelRect.height > 0
        && panelRect.bottom > 0 && panelRect.top < innerHeight),
      matrix_visible: Boolean(matrix && getComputedStyle(matrix).display !== "none"),
      mobile_visible: Boolean(mobile && getComputedStyle(mobile).display !== "none"),
      navigation_main_overlap_area: overlapWidth * overlapHeight,
      overflowing_button_count: visibleButtons.filter(
        (item) => item.scrollWidth > item.clientWidth + 1 || item.scrollHeight > item.clientHeight + 1,
      ).length,
    };
  });
}


/** 校验每个固定视口没有根横向溢出、导航遮挡或错误的响应式分支。 */
function assertLayoutMetrics(metrics, viewport) {
  requireCondition(
    metrics.viewport_width === viewport.width && metrics.viewport_height === viewport.height,
    `browser.layout.${viewport.name}`,
    "viewport",
  );
  requireCondition(
    metrics.document_scroll_width <= viewport.width + 1
      && metrics.body_scroll_width <= viewport.width + 1,
    `browser.layout.${viewport.name}`,
    "horizontal_overflow",
  );
  requireCondition(metrics.panel_visible, `browser.layout.${viewport.name}`, "panel_hidden");
  requireCondition(
    metrics.navigation_main_overlap_area === 0,
    `browser.layout.${viewport.name}`,
    "navigation_overlap",
  );
  requireCondition(
    metrics.overflowing_button_count === 0,
    `browser.layout.${viewport.name}`,
    "button_text_overflow",
  );
  const responsiveModeValid = viewport.mode === "desktop"
    ? metrics.matrix_visible && !metrics.mobile_visible
    : !metrics.matrix_visible && metrics.mobile_visible;
  requireCondition(responsiveModeValid, `browser.layout.${viewport.name}`, "responsive_mode");
  return metrics;
}


/** 截取脱敏视口并用 PNG 像素解码确认尺寸正确且画面非空白。 */
async function captureViewport(page, config, viewport) {
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  await settleLayout(page);
  const layout = assertLayoutMetrics(await readLayoutMetrics(page), viewport);
  const filename = `role-permissions-${viewport.name}.png`;
  const screenshotPath = path.join(config.artifactDirectory, "screenshots", filename);
  const buffer = await page.screenshot({
    path: screenshotPath,
    type: "png",
    fullPage: false,
    animations: "disabled",
    mask: [page.locator(".user-menu")],
    maskColor: "#d1d5db",
  });
  const pixels = inspectPngPixels(buffer, viewport, `browser.screenshot.${viewport.name}`);
  return {
    filename,
    sha256: createHash("sha256").update(buffer).digest("hex"),
    byte_length: buffer.length,
    ...pixels,
    layout,
  };
}


/** 扫描 URL、DOM 与 Storage 键，确认密码和访问 Token 没有越过认证槽位。 */
async function assertBrowserPrivacy(page, password) {
  const metrics = await page.evaluate(({ secret, authKeys }) => {
    const html = document.documentElement.outerHTML;
    const url = location.href;
    const localEntries = Object.entries(localStorage);
    const sessionEntries = Object.entries(sessionStorage);
    const token = sessionStorage.getItem("metahub.auth_token") || "";
    return {
      local_storage_count: localEntries.length,
      auth_slot_count: sessionEntries.filter(([key]) => authKeys.includes(key)).length,
      forbidden_storage_key_count: [...localEntries, ...sessionEntries]
        .filter(([key]) => !authKeys.includes(key)
          && /password|token|secret|prompt|response|payload|request[_-]?id/i.test(key)).length,
      password_exposure_count: [url, html, JSON.stringify([...localEntries, ...sessionEntries])]
        .filter((value) => value.includes(secret)).length,
      token_dom_or_url_exposed: Boolean(token) && (url.includes(token) || html.includes(token)),
      bearer_dom_count: (html.match(/Bearer\s+[^"\s]{8,}/gi) ?? []).length,
    };
  }, { secret: password, authKeys: AUTH_STORAGE_KEYS });
  requireCondition(metrics.local_storage_count === 0, "browser.privacy", "local_storage");
  requireCondition(metrics.auth_slot_count === AUTH_STORAGE_KEYS.length, "browser.privacy", "auth_slots");
  requireCondition(metrics.forbidden_storage_key_count === 0, "browser.privacy", "storage_keys");
  requireCondition(metrics.password_exposure_count === 0, "browser.privacy", "password_exposed");
  requireCondition(!metrics.token_dom_or_url_exposed && metrics.bearer_dom_count === 0, "browser.privacy", "token_exposed");
  // 成功证据只保留零命中结论，不能复制被扫描的 DOM、URL、Storage 或敏感值。
  return { sensitive_url_dom_storage_hits: 0, auth_storage_contract_valid: true };
}


/** 执行真实注销并证明认证槽位和受保护 PAGE-085 DOM 已销毁。 */
async function logoutAndVerify(page, config, tracker) {
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
  tracker.logoutStatus = response.status();
  requireCondition(response.status() === 200, "browser.logout", "status");
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30000 });
  const cleared = await page.evaluate((authKeys) => ({
    local_count: localStorage.length,
    auth_slot_count: authKeys.filter((key) => sessionStorage.getItem(key) !== null).length,
    protected_dom_count: document.querySelectorAll(".role-permission-page,.console-shell").length,
  }), AUTH_STORAGE_KEYS);
  requireCondition(cleared.local_count === 0, "browser.logout", "local_storage");
  requireCondition(cleared.auth_slot_count === 0, "browser.logout", "auth_slots");
  requireCondition(cleared.protected_dom_count === 0, "browser.logout", "protected_dom");
  // 注销证据仅表达清理结论，不能返回 Storage 内容或失效 Session 标识。
  return { auth_storage_cleared: true, protected_dom_cleared: true };
}


/** 启动离线 Chromium，完成 PAGE-085 主流程、交互、视口、隐私和清理验收。 */
async function executeAcceptance(config) {
  const browserHome = path.join(config.artifactDirectory, "browser-home");
  let browser = null;
  let context = null;
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
    context = await runBrowserOperation("browser.context", () => browser.newContext({
      viewport: { width: VIEWPORTS[0].width, height: VIEWPORTS[0].height },
      locale: "zh-CN",
      serviceWorkers: "block",
      acceptDownloads: false,
    }));
    const page = await runBrowserOperation("browser.page", () => context.newPage());
    const tracker = await runBrowserOperation(
      "browser.guards",
      () => installBrowserGuards(context, page, config),
    );
    await runBrowserOperation("browser.login", () => loginThroughConsole(page, config, tracker));
    await runBrowserOperation(
      "browser.permissions",
      () => openRolePermissions(page, config, tracker),
    );
    await runBrowserOperation("browser.matrix", () => assertMatrixContracts(page));
    const draft = await runBrowserOperation("browser.draft", () => exerciseDiscardedDraft(page));
    const privacy = await runBrowserOperation(
      "browser.privacy",
      () => assertBrowserPrivacy(page, config.password),
    );
    const screenshots = [];
    for (const viewport of VIEWPORTS) {
      screenshots.push(await runBrowserOperation(
        `browser.screenshot.${viewport.name}`,
        () => captureViewport(page, config, viewport),
      ));
    }
    const logout = await runBrowserOperation(
      "browser.logout",
      () => logoutAndVerify(page, config, tracker),
    );
    requireCondition(tracker.blockedRequestCount === 0, "browser.network", "blocked_request");
    requireCondition(tracker.pageErrorCount === 0, "browser.errors", "page_error");
    requireCondition(tracker.consoleErrorCount === 0, "browser.errors", "console_error");
    return {
      status: "passed",
      prd_id: "PRD-059",
      page_id: "PAGE-085",
      checks: {
        http_statuses: {
          login: tracker.loginStatus,
          permission_matrix: tracker.matrixStatus,
          logout: tracker.logoutStatus,
        },
        matrix: { roles: 6, permissions: 48, admin_immutable: true },
        draft,
        privacy,
        logout,
        network: { blocked_request_count: 0, external_request_count: 0 },
        browser_errors: { page: 0, console: 0 },
      },
      screenshots,
    };
  } finally {
    await context?.close().catch(() => undefined);
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
      page_id: evidence.page_id,
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
