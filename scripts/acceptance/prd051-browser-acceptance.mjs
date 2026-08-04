/**
 * PRD-051 PAGE-074/PAGE-075 的聚焦真实浏览器验收。
 *
 * 脚本只连接调用方已经启动的本地 Console、Control API 与 Gateway。它要求同批次
 * Runtime 证据已经通过，使用真实登录表单进入正式账本，检查详情和调整对话框，
 * 并在 1280x720、390x844 两个视口生成截图及布局证据。管理员身份只从环境变量读取；
 * Session 值、调整草稿和受保护详情不会写入 JSON，也不会出现在未遮罩截图中。
 */

import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";


// Playwright 由工作区运行环境提供；createRequire 保持 ESM 验收脚本与现有 runner 兼容。
const require = createRequire(import.meta.url);
// 两个明确验收视口与用户给定的桌面、移动端尺寸完全一致。
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
// 仅允许当前机器上的服务 origin，认证请求不得跟随到外部主机。
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
// Runtime 提供的资源 ID 在进入 selector 前必须满足稳定安全字符合同。
const SAFE_IDENTIFIER_PATTERN = /^[A-Za-z0-9._:-]{1,160}$/;
// URL 查询禁止出现凭据、正文和调整草稿相关字段；request_id 等公共追踪字段仍可使用。
const SENSITIVE_URL_KEY_PATTERN = /password|token|secret|authorization|prompt|response|reason/i;
// 最终 JSON 字段名不能给后续维护者留下写入敏感值的槽位；公共 response 计数单独放行。
const FORBIDDEN_EVIDENCE_KEY_PATTERN = /password|token|secret|authorization|reason|idempotency(?:_key)?/i;
const FORBIDDEN_EVIDENCE_EXACT_KEYS = new Set([
  "body",
  "endpoint",
  "endpoint_uri",
  "endpoint_url",
  "prompt",
  "response",
]);
// Console 登录态存储键只在页面上下文用于注销和布尔核验，值永远不返回 Node。
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);


/** 只暴露固定 stage/code，避免 Playwright 原始 URL、DOM 或调用栈进入终端。 */
class BrowserAcceptanceFailure extends Error {
  /** 使用脚本内部常量构造可公开失败。 */
  constructor(stage, code) {
    super(`prd051_browser_acceptance_failed stage=${stage} code=${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 固定失败入口，不接受底层异常正文。 */
function fail(stage, code) {
  throw new BrowserAcceptanceFailure(stage, code);
}


/** 执行布尔门禁，失败时只传播固定阶段和错误码。 */
function requireCondition(condition, stage, code) {
  if (!condition) fail(stage, code);
}


/** 把未知异常压缩成安全浏览器错误；已脱敏错误保持原阶段。 */
function toSafeFailure(error, stage, code = "stage_failed") {
  if (error instanceof BrowserAcceptanceFailure) return error;
  return new BrowserAcceptanceFailure(stage, code);
}


/** 规范化 HTTP 回环 origin，并拒绝凭据、路径、查询、fragment 与缺失端口。 */
function normalizeLoopbackOrigin(value, label) {
  let target;
  try {
    target = new URL(String(value));
  } catch {
    fail(`config.${label}`, "invalid_url");
  }
  const hostname = target.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  requireCondition(target.protocol === "http:", `config.${label}`, "scheme_not_http");
  requireCondition(LOOPBACK_HOSTS.has(hostname), `config.${label}`, "host_not_loopback");
  requireCondition(!target.username && !target.password, `config.${label}`, "url_credentials");
  requireCondition(target.pathname === "/", `config.${label}`, "url_path");
  requireCondition(!target.search && !target.hash, `config.${label}`, "url_suffix");
  requireCondition(Boolean(target.port), `config.${label}`, "port_missing");
  return target.origin;
}


/** 从脚本位置解析固定工作区根，不信任环境变量覆盖信任边界。 */
function workspaceRoot() {
  // fileURLToPath 统一处理 Windows 盘符和 URL 转义，避免手写 pathname 规范化规则。
  const resolved = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  requireCondition(path.basename(resolved).toLowerCase() === "metahub", "config.workspace", "unexpected_root");
  return resolved;
}


/** 把批次目录限制为 ``artifacts/prd051/<batch>`` 的单层安全路径。 */
function resolveArtifactDirectory(value, root = workspaceRoot()) {
  const artifactRoot = path.resolve(root, "artifacts", "prd051");
  const candidate = path.resolve(root, String(value));
  const relative = path.relative(artifactRoot, candidate);
  requireCondition(relative && !relative.startsWith("..") && !path.isAbsolute(relative), "config.artifact", "outside_workspace");
  requireCondition(!relative.includes(path.sep), "config.artifact", "batch_depth");
  requireCondition(/^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$/.test(relative), "config.artifact", "batch_name");
  return candidate;
}


/**
 * 只从环境变量取得登录身份，并在 Chrome 启动前删除口令变量，防止子进程继承。
 * 邮箱和租户也不提供命令行参数，使所有身份信息保持同一来源合同。
 */
function readEnvironmentIdentity(env = process.env) {
  const adminPassword = env.METAHUB_PRD051_ADMIN_PASSWORD || env.METAHUB_ADMIN_PASSWORD || "";
  const tenantSlug = String(env.METAHUB_PRD051_TENANT_SLUG || env.METAHUB_TENANT_SLUG || "default").trim();
  const adminEmail = String(env.METAHUB_PRD051_ADMIN_EMAIL || env.METAHUB_ADMIN_EMAIL || "admin@metahub.local").trim().toLowerCase();
  for (const name of [
    "METAHUB_PRD051_ADMIN_PASSWORD",
    "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD051_ADMIN_EMAIL",
    "METAHUB_ADMIN_EMAIL",
    "METAHUB_PRD051_TENANT_SLUG",
    "METAHUB_TENANT_SLUG",
  ]) delete env[name];
  requireCondition(adminPassword.length >= 8, "config.identity", "admin_password_missing");
  requireCondition(Boolean(tenantSlug), "config.identity", "tenant_slug_missing");
  requireCondition(adminEmail.includes("@"), "config.identity", "admin_email_invalid");
  // 身份只在当前进程内传递，返回对象随后会纳入证据敏感值扫描而不会被序列化。
  return { tenantSlug, adminEmail, adminPassword };
}


/** 解析非敏感命令行参数；任何未知参数都失败关闭。 */
function parseArguments(argv = process.argv.slice(2), env = process.env) {
  const identity = readEnvironmentIdentity(env);
  const values = new Map();
  const flags = new Set();
  for (let index = 0; index < argv.length; index += 1) {
    const name = String(argv[index]);
    if (name === "--headed") {
      flags.add(name);
      continue;
    }
    requireCondition(
      ["--artifact-dir", "--console-url", "--control-url", "--gateway-url"].includes(name),
      "config.arguments",
      "unknown_argument",
    );
    requireCondition(index + 1 < argv.length && !values.has(name), "config.arguments", "argument_invalid");
    values.set(name, String(argv[++index]));
  }
  const artifactValue = values.get("--artifact-dir") || env.METAHUB_PRD051_ARTIFACT_DIR || "";
  requireCondition(Boolean(artifactValue), "config.artifact", "missing");
  const root = workspaceRoot();
  return {
    root,
    artifactDirectory: resolveArtifactDirectory(artifactValue, root),
    consoleOrigin: normalizeLoopbackOrigin(
      values.get("--console-url") || env.METAHUB_PRD051_CONSOLE_URL || "http://localhost:5173",
      "console_origin",
    ),
    controlOrigin: normalizeLoopbackOrigin(
      values.get("--control-url") || env.METAHUB_PRD051_CONTROL_URL || "http://localhost:8001",
      "control_origin",
    ),
    gatewayOrigin: normalizeLoopbackOrigin(
      values.get("--gateway-url") || env.METAHUB_PRD051_GATEWAY_URL || "http://localhost:8000",
      "gateway_origin",
    ),
    chromePath: String(env.METAHUB_CHROME_PATH || "").trim(),
    headed: flags.has("--headed") || env.METAHUB_BROWSER_HEADED === "1",
    ...identity,
  };
}


/** 把 camelCase、连字符和其他分隔符统一为小写下划线，避免敏感键借命名风格绕过。 */
function normalizeEvidenceKey(key) {
  return String(key)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}


/** 判断证据字段名是否可能承载凭据、正文、上游端点或其他明确禁止的数据。 */
function isForbiddenEvidenceKey(key) {
  const normalized = normalizeEvidenceKey(key);
  if (FORBIDDEN_EVIDENCE_KEY_PATTERN.test(normalized)) return true;
  if (FORBIDDEN_EVIDENCE_EXACT_KEYS.has(normalized)) return true;
  // Provider/Virtual Key、Prompt、响应正文和 Endpoint 属于明确禁止进入验收制品的数据类别。
  if (/(?:^|_)provider_(?:api_)?key(?:_|$)/.test(normalized)) return true;
  if (/(?:^|_)virtual_key(?:_|$)/.test(normalized)) return true;
  if (/(?:^|_)prompt(?:_|$)/.test(normalized)) return true;
  // 只允许固定的聚合计数，不允许 response 原文、正文、payload 或 content 槽位。
  if (/(?:^|_)response_count$/.test(normalized)) return false;
  if (/(?:^|_)(?:raw_|provider_)?response(?:_(?:body|content|payload))?(?:_|$)/.test(normalized)) {
    return true;
  }
  if (/^(?:request_|response_|raw_)?body$/.test(normalized)) return true;
  if (/(?:^|_)endpoint(?:_(?:url|uri|credential|config))?(?:_|$)/.test(normalized)) return true;
  return false;
}


/** 递归拒绝 Runtime 或 Browser 证据中的敏感字段槽位。 */
function assertSafeKeys(value, stage = "evidence.privacy") {
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value)) {
    for (const child of value) assertSafeKeys(child, stage);
    return;
  }
  for (const [key, child] of Object.entries(value)) {
    requireCondition(!isForbiddenEvidenceKey(key), stage, "forbidden_key");
    assertSafeKeys(child, stage);
  }
}


/** 读取并验证同批次 Runtime 证据，只投影浏览器需要的安全资源 ID。 */
async function loadRuntimeEvidence(artifactDirectory) {
  const target = path.join(artifactDirectory, "runtime-evidence.json");
  requireCondition(existsSync(target), "runtime_evidence.load", "missing");
  let payload;
  try {
    payload = JSON.parse((await readFile(target, "utf8")).replace(/^\uFEFF/, ""));
  } catch {
    fail("runtime_evidence.load", "invalid_json");
  }
  requireCondition(payload && typeof payload === "object" && !Array.isArray(payload), "runtime_evidence.load", "not_object");
  assertSafeKeys(payload, "runtime_evidence.privacy");
  requireCondition(payload.status === "passed" && payload.prd_id === "PRD-051", "runtime_evidence.load", "not_passed");
  requireCondition(payload.cleanup?.session_revoked === true, "runtime_evidence.load", "cleanup_missing");
  const manualEntryId = String(payload.generated_resources?.manual_entry_id || "");
  requireCondition(SAFE_IDENTIFIER_PATTERN.test(manualEntryId), "runtime_evidence.load", "entry_id_invalid");
  return { manualEntryId };
}


/** 判断一个请求 URL 是否属于允许的本地服务，且没有敏感查询字段。 */
function browserRequestAllowed(rawUrl, allowedOrigins) {
  let target;
  try {
    target = new URL(String(rawUrl));
  } catch {
    return false;
  }
  if (["data:", "blob:", "about:"].includes(target.protocol)) return true;
  if (!allowedOrigins.has(target.origin)) return false;
  if (target.username || target.password) return false;
  for (const key of target.searchParams.keys()) {
    if (SENSITIVE_URL_KEY_PATTERN.test(key)) return false;
  }
  return true;
}


/**
 * 安装最小网络与浏览器错误门禁。
 * 只记录计数，不保留 URL、Console 文本、请求头或响应正文。
 */
async function installBrowserGuards(context, page, config) {
  const allowedOrigins = new Set([config.consoleOrigin, config.controlOrigin, config.gatewayOrigin]);
  const summary = { allowed_request_count: 0, blocked_request_count: 0, browser_error_count: 0 };
  await context.route("**/*", async (route) => {
    // 只放行冻结的三个回环 origin，任何外部或带凭据 URL 都在发出前终止。
    if (browserRequestAllowed(route.request().url(), allowedOrigins)) {
      summary.allowed_request_count += 1;
      await route.continue();
      return;
    }
    summary.blocked_request_count += 1;
    await route.abort("blockedbyclient");
  });
  page.on("pageerror", () => {
    summary.browser_error_count += 1;
  });
  page.on("console", (message) => {
    // 只累计错误数量，不保存 Console 文本，避免服务端正文或标识进入验收制品。
    if (message.type() === "error") summary.browser_error_count += 1;
  });
  return summary;
}


/** 使用真实 Console 登录表单，不读取登录响应中的 Session 值。 */
async function loginThroughConsole(page, config, onAuthenticated) {
  await page.goto(`${config.consoleOrigin}/`, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const form = page.locator("#login-form");
  await form.waitFor({ state: "visible", timeout: 30_000 });
  await form.locator('[name="tenantSlug"]').fill(config.tenantSlug);
  await form.locator('[name="email"]').fill(config.adminEmail);
  await form.locator('[name="password"]').fill(config.adminPassword);
  const loginResponse = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === config.controlOrigin
      && target.pathname === "/auth/login"
      && response.request().method() === "POST";
  }, { timeout: 30_000 });
  await form.locator('button[type="submit"]').click();
  const response = await loginResponse;
  requireCondition(response.status() === 200, "browser.login", "status_mismatch");
  onAuthenticated?.();
  requireCondition(Boolean(response.headers()["x-request-id"]), "browser.login", "request_id_missing");
  await page.locator('[data-console-nav="finops-ledger"]').waitFor({ state: "visible", timeout: 45_000 });
  return { http_status: 200, request_id_present: true, admin_navigation_visible: true };
}


/** 导航到 PAGE-074，并等待真实 FinOps GET 完成和至少一条 Runtime 分录出现。 */
async function openLedgerPage(page, config, runtime) {
  const ledgerResponse = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === config.controlOrigin
      && target.pathname === "/finops/ledger"
      && response.request().method() === "GET";
  }, { timeout: 30_000 });
  await page.locator('[data-console-nav="finops-ledger"]').click();
  const response = await ledgerResponse;
  requireCondition(response.status() === 200, "browser.page074", "ledger_status");
  requireCondition(
    String(response.headers()["cache-control"] || "").toLowerCase().includes("no-store"),
    "browser.page074",
    "cache_control_missing",
  );
  await page.locator("#finops-ledger-title").waitFor({ state: "visible", timeout: 30_000 });
  await page.locator(".finops-loading").waitFor({ state: "detached", timeout: 30_000 }).catch(() => {});
  const rows = page.locator("[data-finops-ledger-row]");
  requireCondition(await rows.count() > 0, "browser.page074", "ledger_empty");
  const runtimeButton = page.locator(`[data-finops-entry-id="${runtime.manualEntryId}"]`);
  requireCondition(await runtimeButton.count() === 1, "browser.page074", "runtime_entry_missing");
  requireCondition(
    await page.locator('.console-shell[data-active-navigation="finops-ledger"]').count() === 1,
    "browser.page074",
    "navigation_not_active",
  );
  return {
    page_id: "PAGE-074",
    title_visible: true,
    runtime_entry_visible: true,
    row_count: await rows.count(),
    cache_control_no_store: true,
  };
}


/** 等待两个动画帧，让响应式布局和 dialog 定位在测量前稳定。 */
async function settleLayout(page) {
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}


/**
 * 测量根节点、页面横向滚动和可交互控件重叠。
 * 只返回整数与布尔值，不返回文本、属性、DOM HTML 或业务字段。
 */
async function readLayoutMetrics(page, selector) {
  return page.evaluate((rootSelector) => {
    const root = document.querySelector(rootSelector);
    if (!root) return null;
    const rootRectangle = root.getBoundingClientRect();
    const controls = [...root.querySelectorAll("button,input,select,textarea")]
      .filter((element) => {
        const style = getComputedStyle(element);
        const rectangle = element.getBoundingClientRect();
        return style.display !== "none"
          && style.visibility !== "hidden"
          && rectangle.width > 1
          && rectangle.height > 1;
      })
      .map((element) => ({ element, rectangle: element.getBoundingClientRect() }));
    let overlapCount = 0;
    for (let left = 0; left < controls.length; left += 1) {
      for (let right = left + 1; right < controls.length; right += 1) {
        if (controls[left].element.contains(controls[right].element)
          || controls[right].element.contains(controls[left].element)) continue;
        const horizontal = Math.min(controls[left].rectangle.right, controls[right].rectangle.right)
          - Math.max(controls[left].rectangle.left, controls[right].rectangle.left);
        const vertical = Math.min(controls[left].rectangle.bottom, controls[right].rectangle.bottom)
          - Math.max(controls[left].rectangle.top, controls[right].rectangle.top);
        if (horizontal > 2 && vertical > 2) overlapCount += 1;
      }
    }
    const clippedControlCount = controls.filter(({ rectangle }) => (
      rectangle.left < -1 || rectangle.right > innerWidth + 1
    )).length;
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      root_left: Math.round(rootRectangle.left),
      root_right: Math.round(rootRectangle.right),
      root_client_width: root.clientWidth,
      root_scroll_width: root.scrollWidth,
      clipped_control_count: clippedControlCount,
      overlap_count: overlapCount,
    };
  }, selector);
}


/** 将原始布局数值压成稳定通过证据；任一横向溢出或控件重叠都阻止截图通过。 */
function assertLayoutMetrics(metrics, viewport, stage) {
  requireCondition(metrics && typeof metrics === "object", stage, "root_missing");
  requireCondition(metrics.viewport_width === viewport.width, stage, "viewport_width");
  requireCondition(metrics.viewport_height === viewport.height, stage, "viewport_height");
  requireCondition(metrics.document_scroll_width <= viewport.width + 1, stage, "document_overflow");
  requireCondition(metrics.body_scroll_width <= viewport.width + 1, stage, "body_overflow");
  requireCondition(metrics.root_left >= -1 && metrics.root_right <= viewport.width + 1, stage, "root_overflow");
  requireCondition(metrics.root_scroll_width <= metrics.root_client_width + 1, stage, "root_scroll_overflow");
  requireCondition(metrics.clipped_control_count === 0, stage, "control_clipped");
  requireCondition(metrics.overlap_count === 0, stage, "control_overlap");
  return { horizontal_overflow: false, clipped_control_count: 0, overlap_count: 0 };
}


/** 证明截图锚点至少有一个像素位于当前视口，DOM visible 不能替代真实可见范围。 */
function assertViewportIntersection(box, viewport, stage) {
  requireCondition(box && typeof box === "object", stage, "anchor_box_missing");
  const values = [box.x, box.y, box.width, box.height, viewport.width, viewport.height];
  requireCondition(values.every((value) => Number.isFinite(value)), stage, "anchor_box_invalid");
  requireCondition(box.width > 0 && box.height > 0, stage, "anchor_box_empty");
  requireCondition(
    box.x < viewport.width
      && box.x + box.width > 0
      && box.y < viewport.height
      && box.y + box.height > 0,
    stage,
    "anchor_outside_viewport",
  );
  return { anchor_intersects_viewport: true };
}


/** 冻结 PAGE-075 原因和操作者的遮罩数量与可见性，缺失或重复都失败关闭。 */
function assertProtectedDetailMaskCoverage(state, stage) {
  requireCondition(state?.reasonCount === 1, stage, "reason_mask_count");
  requireCondition(state?.reasonVisibleCount === 1, stage, "reason_mask_not_visible");
  requireCondition(state?.actorCount === 1, stage, "actor_mask_count");
  requireCondition(state?.actorVisibleCount === 1, stage, "actor_mask_not_visible");
  return { protected_detail_masked: true };
}


/** 统计 locator 中按 CSS/可访问性语义可见的节点，不把离屏但可渲染节点误作不存在。 */
async function visibleLocatorCount(locator) {
  const count = await locator.count();
  let visibleCount = 0;
  for (let index = 0; index < count; index += 1) {
    if (await locator.nth(index).isVisible()) visibleCount += 1;
  }
  return { count, visibleCount };
}


/** 构造截图遮罩；PAGE-075 必须实测原因和操作者各有一个可见遮罩。 */
async function screenshotMasks(page, { requireProtectedDetailMasks = false, stage } = {}) {
  const userMenu = page.locator(".user-menu");
  const reason = page.locator(".finops-detail-list > div").filter({ hasText: "调整原因" });
  const actor = page.locator(".finops-detail-list > div").filter({ hasText: "操作者" });
  const masks = [];
  if (await userMenu.count() > 0) masks.push(userMenu);
  const reasonState = await visibleLocatorCount(reason);
  const actorState = await visibleLocatorCount(actor);
  if (reasonState.count > 0) masks.push(reason);
  if (actorState.count > 0) masks.push(actor);
  if (!requireProtectedDetailMasks) {
    return { locators: masks, protectedDetailMasked: false };
  }
  const evidence = assertProtectedDetailMaskCoverage({
    reasonCount: reasonState.count,
    reasonVisibleCount: reasonState.visibleCount,
    actorCount: actorState.count,
    actorVisibleCount: actorState.visibleCount,
  }, stage);
  return { locators: masks, protectedDetailMasked: evidence.protected_detail_masked };
}


/** 校验 Playwright PNG 的签名、视口尺寸和最小字节量，避免空白或错误格式伪装截图。 */
function inspectScreenshot(buffer, viewport, stage) {
  requireCondition(Buffer.isBuffer(buffer) && buffer.length > 5_000, stage, "screenshot_too_small");
  requireCondition(buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])), stage, "png_signature");
  requireCondition(buffer.readUInt32BE(16) === viewport.width, stage, "png_width");
  requireCondition(buffer.readUInt32BE(20) === viewport.height, stage, "png_height");
  return { bytes: buffer.length, sha256: createHash("sha256").update(buffer).digest("hex") };
}


/** 在两个固定视口测量布局并以独占文件语义写截图。 */
async function captureState(
  page,
  artifactDirectory,
  stateName,
  rootSelector,
  { anchorSelector = rootSelector, requireProtectedDetailMasks = false } = {},
) {
  const screenshots = [];
  const layouts = {};
  let protectedDetailMasked = requireProtectedDetailMasks;
  for (const viewport of VIEWPORTS) {
    const stage = `browser.capture.${stateName}.${viewport.name}`;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const anchor = page.locator(anchorSelector).first();
    await anchor.waitFor({ state: "visible", timeout: 20_000 });
    await anchor.scrollIntoViewIfNeeded();
    await settleLayout(page);
    layouts[viewport.name] = {
      ...assertLayoutMetrics(await readLayoutMetrics(page, rootSelector), viewport, stage),
      ...assertViewportIntersection(await anchor.boundingBox(), viewport, stage),
    };
    const maskState = await screenshotMasks(page, {
      requireProtectedDetailMasks,
      stage,
    });
    protectedDetailMasked = protectedDetailMasked && maskState.protectedDetailMasked;
    const buffer = await page.screenshot({
      fullPage: false,
      animations: "disabled",
      caret: "hide",
      mask: maskState.locators,
      maskColor: "#64748b",
    });
    const inspected = inspectScreenshot(buffer, viewport, stage);
    const filename = `${stateName}-${viewport.name}.png`;
    try {
      await writeFile(path.join(artifactDirectory, "screenshots", filename), buffer, { flag: "wx" });
    } catch {
      fail(stage, "screenshot_write_failed");
    }
    screenshots.push({
      state: stateName,
      viewport: viewport.name,
      file: `screenshots/${filename}`,
      ...inspected,
    });
  }
  return { screenshots, layouts, protectedDetailMasked };
}


/** 打开 Runtime 创建的分录详情并确认 PAGE-075 标题与目标选择状态。 */
async function openEntryDetail(page, runtime) {
  const button = page.locator(`[data-finops-entry-id="${runtime.manualEntryId}"]`);
  await button.click();
  await page.locator("#finops-detail-title").waitFor({ state: "visible", timeout: 20_000 });
  requireCondition(
    (await page.locator(".finops-detail-heading").textContent()).includes("PAGE-075"),
    "browser.page075",
    "page_marker_missing",
  );
  requireCondition(await button.getAttribute("aria-pressed") === "true", "browser.page075", "row_not_selected");
  return { page_id: "PAGE-075", title_visible: true, runtime_entry_selected: true };
}


/** 扫描 DOM 表单值与两类 Storage，只返回一次性草稿命中数量。 */
async function countDraftHits(page, marker) {
  return page.evaluate(({ value, storageKeys }) => {
    const candidates = [document.documentElement.innerHTML];
    for (const control of document.querySelectorAll("input,textarea,select")) {
      candidates.push(String(control.value || ""));
    }
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key) candidates.push(String(storage.getItem(key) || ""));
      }
    }
    // storageKeys 只用于固定读取访问，防止未来键名改动时扫描被树摇优化掉。
    for (const key of storageKeys) candidates.push(String(localStorage.getItem(key) || ""));
    return candidates.filter((candidate) => candidate.includes(value)).length;
  }, { value: marker, storageKeys: AUTH_STORAGE_KEYS });
}


/**
 * 验证普通调整对话框的字段白名单与取消销毁语义。
 * 脚本从不提交该表单，实际财务写入由 Runtime API 验收单独负责。
 */
async function verifyAdjustmentDialog(page, draftMarker) {
  await page.locator("#finops-close-detail").click();
  await page.locator("#finops-detail-title").waitFor({ state: "detached", timeout: 20_000 });
  const openButton = page.locator("#finops-open-adjustment");
  await openButton.click();
  const dialog = page.locator("#finops-adjustment-dialog");
  await dialog.waitFor({ state: "visible", timeout: 20_000 });
  const form = dialog.locator("#finops-adjustment-form");
  requireCondition(await form.locator('[name="tenant_id"]').count() === 0, "browser.dialog", "tenant_field_present");
  requireCondition(await form.locator('[name="currency"]').count() === 0, "browser.dialog", "currency_field_present");
  requireCondition(await form.locator('[name="direction"]').count() === 0, "browser.dialog", "direction_field_present");
  await page.waitForFunction(() => document.activeElement?.getAttribute("name") === "adjustmentType");
  requireCondition(
    await form.locator('[name="adjustmentType"]').evaluate((element) => document.activeElement === element),
    "browser.dialog",
    "initial_focus_missing",
  );
  await form.locator('[name="amount"]').fill("0.000001");
  await form.locator('[name="reason"]').fill(draftMarker);
  // Escape 必须走页面状态机关闭，而不是仅隐藏原生 dialog；随后应销毁草稿并恢复触发器焦点。
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached", timeout: 20_000 });
  requireCondition(await countDraftHits(page, draftMarker) === 0, "browser.dialog", "draft_not_destroyed");
  await page.waitForFunction(() => document.activeElement?.id === "finops-open-adjustment");
  requireCondition(
    await page.locator("#finops-open-adjustment").evaluate((element) => document.activeElement === element),
    "browser.dialog",
    "trigger_focus_not_restored",
  );

  // 第二次打开必须是全新状态；截图只捕获空表单，避免业务原因进入 PNG。
  await page.locator("#finops-open-adjustment").click();
  const reopened = page.locator("#finops-adjustment-dialog");
  await reopened.waitFor({ state: "visible", timeout: 20_000 });
  await page.waitForFunction(() => document.activeElement?.getAttribute("name") === "adjustmentType");
  requireCondition(
    await reopened.locator('[name="adjustmentType"]').evaluate((element) => document.activeElement === element),
    "browser.dialog",
    "reopened_focus_missing",
  );
  requireCondition(await reopened.locator('[name="amount"]').inputValue() === "", "browser.dialog", "amount_restored");
  requireCondition(await reopened.locator('[name="reason"]').inputValue() === "", "browser.dialog", "draft_restored");
  return {
    visible: true,
    service_owned_fields_absent: true,
    cancel_destroyed_draft: true,
    escape_closed_dialog: true,
    initial_focus_verified: true,
    trigger_focus_restored: true,
    reopened_empty: true,
  };
}


/** 验证登出 HTTP、统一链路字段和两类浏览器存储的布尔清理快照。 */
function verifyLogoutEvidence({ status, headers, payload, storageState }) {
  const requestId = String(headers?.["x-request-id"] || "");
  const traceId = String(headers?.["x-trace-id"] || "");
  requireCondition(status === 200, "browser.logout", "status_mismatch");
  requireCondition(SAFE_IDENTIFIER_PATTERN.test(requestId), "browser.logout", "request_id_missing");
  requireCondition(SAFE_IDENTIFIER_PATTERN.test(traceId), "browser.logout", "trace_id_missing");
  requireCondition(payload && typeof payload === "object" && !Array.isArray(payload), "browser.logout", "response_not_object");
  requireCondition(payload.logged_out === true, "browser.logout", "not_revoked");
  requireCondition(payload.request_id === requestId, "browser.logout", "request_id_mismatch");
  requireCondition(payload.trace_id === traceId, "browser.logout", "trace_id_mismatch");
  for (const storageName of ["localStorage", "sessionStorage"]) {
    const cleared = storageState?.[storageName];
    requireCondition(
      Array.isArray(cleared)
        && cleared.length === AUTH_STORAGE_KEYS.length
        && cleared.every((value) => value === true),
      "browser.logout",
      "auth_storage_not_cleared",
    );
  }
  return {
    http_status: 200,
    request_id: requestId,
    trace_id: traceId,
    logged_out: true,
    browser_auth_cleared: true,
  };
}


/** 通过真实登出按钮撤销 Session，并确认浏览器认证存储已清除。 */
async function logoutThroughConsole(page, config, onRevoked) {
  await page.locator("#finops-cancel-adjustment").click();
  await page.locator("#finops-adjustment-dialog").waitFor({ state: "detached", timeout: 20_000 });
  await page.locator("#logout-button").waitFor({ state: "visible", timeout: 20_000 });
  const logoutResponse = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === config.controlOrigin
      && target.pathname === "/auth/logout"
      && response.request().method() === "POST";
  }, { timeout: 30_000 });
  await page.locator("#logout-button").click();
  const response = await logoutResponse;
  let payload;
  try {
    payload = await response.json();
  } catch {
    fail("browser.logout", "response_not_json");
  }
  if (response.status() === 200 && payload?.logged_out === true) {
    onRevoked?.();
  }
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30_000 });
  const storageState = await page.evaluate((keys) => ({
    localStorage: keys.map((key) => localStorage.getItem(key) === null),
    sessionStorage: keys.map((key) => sessionStorage.getItem(key) === null),
  }), AUTH_STORAGE_KEYS);
  // 只把布尔清理快照交给验证器，不把任一 Storage 值返回 Node 或写入证据。
  return verifyLogoutEvidence({
    status: response.status(),
    headers: response.headers(),
    payload,
    storageState,
  });
}



/**
 * 异常路径在页面上下文内撤销当前 Session。
 *
 * Token 只在浏览器 Storage 内读取并直接发送给回环 Control API；函数只返回布尔结果，
 * 防止认证材料穿过 Playwright 边界进入 Node 日志或验收证据。
 */
async function revokeSessionAfterFailure(page, config) {
  try {
    return await page.evaluate(async ({ controlOrigin, storageKeys }) => {
      const storages = [sessionStorage, localStorage];
      const token = storages
        .map((storage) => storage.getItem(storageKeys[0]))
        .find((value) => typeof value === "string" && value.length > 0);
      // 缺少页面内 Token 时无法证明服务端已撤销，必须失败关闭并由调用方记录固定清理码。
      if (!token) return false;

      let revoked = false;
      try {
        const response = await fetch(`${controlOrigin}/auth/logout`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        });
        const payload = response.status === 200 ? await response.json() : null;
        revoked = response.status === 200 && payload?.logged_out === true;
      } catch {
        revoked = false;
      }
      if (revoked) {
        for (const storage of storages) {
          for (const key of storageKeys) storage.removeItem(key);
        }
      }
      return revoked && storages.every(
        (storage) => storageKeys.every((key) => storage.getItem(key) === null),
      );
    }, { controlOrigin: config.controlOrigin, storageKeys: AUTH_STORAGE_KEYS });
  } catch {
    return false;
  }
}

/** 递归扫描最终 JSON，确保环境身份和一次性草稿不会出现在安全摘要。 */
function assertSafeEvidence(value, sensitiveValues) {
  assertSafeKeys(value);
  const encoded = JSON.stringify(value);
  for (const sensitive of sensitiveValues) {
    if (sensitive) requireCondition(!encoded.includes(sensitive), "evidence.privacy", "sensitive_value");
  }
  requireCondition(!encoded.includes("Bearer "), "evidence.privacy", "bearer_value");
}


/** 独占写入最终 Browser JSON；只有完整流程通过且 Chrome 已关闭后才调用。 */
async function publishBrowserEvidence(artifactDirectory, evidence) {
  const target = path.join(artifactDirectory, "browser-evidence.json");
  try {
    await writeFile(target, `${JSON.stringify(evidence, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
  } catch {
    fail("evidence.publish", "output_exists_or_unwritable");
  }
  return target;
}


/** 执行真实浏览器主流程，并在任一路径关闭 Context 与 Chrome。 */
async function executeBrowserAcceptance(config) {
  const runtime = await loadRuntimeEvidence(config.artifactDirectory);
  const screenshotsDirectory = path.join(config.artifactDirectory, "screenshots");
  try {
    await mkdir(screenshotsDirectory, { recursive: false });
  } catch {
    fail("evidence.screenshots", "directory_exists_or_unwritable");
  }

  let chromium;
  try {
    ({ chromium } = require("playwright"));
  } catch {
    fail("browser.launch", "playwright_unavailable");
  }
  let browser;
  let context;
  let page;
  let result;
  let failure = null;
  let sessionNeedsRevocation = false;
  const draftMarker = `PRD051浏览器草稿-${randomBytes(12).toString("hex")}`;
  try {
    browser = await chromium.launch({
      ...(config.chromePath ? { executablePath: config.chromePath } : {}),
      headless: !config.headed,
    });
    context = await browser.newContext({
      viewport: { width: VIEWPORTS[0].width, height: VIEWPORTS[0].height },
    });
    page = await context.newPage();
    const network = await installBrowserGuards(context, page, config);
    const login = await loginThroughConsole(page, config, () => {
      // 登录响应一旦成功即由 finally 接管清理，后续导航或截图失败也不能遗留有效 Session。
      sessionNeedsRevocation = true;
    });
    const page074 = await openLedgerPage(page, config, runtime);
    const page074Capture = await captureState(
      page,
      config.artifactDirectory,
      "page074",
      ".console-main",
      { anchorSelector: "#finops-ledger-title" },
    );
    const page075 = await openEntryDetail(page, runtime);
    const page075Capture = await captureState(
      page,
      config.artifactDirectory,
      "page075",
      ".console-main",
      {
        anchorSelector: "#finops-detail-title",
        requireProtectedDetailMasks: true,
      },
    );
    const dialog = await verifyAdjustmentDialog(page, draftMarker);
    const dialogCapture = await captureState(
      page,
      config.artifactDirectory,
      "adjustment-dialog",
      "#finops-adjustment-dialog",
      { anchorSelector: "#finops-adjustment-title" },
    );
    const logout = await logoutThroughConsole(page, config, () => {
      // 服务端确认撤销后立即清除兜底标记，避免后续证据校验失败时重复使用已注销 Token。
      sessionNeedsRevocation = false;
    });
    requireCondition(network.blocked_request_count === 0, "browser.network", "blocked_request");
    requireCondition(network.browser_error_count === 0, "browser.runtime", "browser_error");
    requireCondition(page075Capture.protectedDetailMasked, "browser.privacy", "detail_masks_unverified");
    result = {
      schema_version: 1,
      status: "passed",
      prd_id: "PRD-051",
      checks: { login, page074, page075, adjustment_dialog: dialog, logout },
      viewports: VIEWPORTS.map(({ name, width, height }) => ({ name, width, height })),
      layouts: {
        page074: page074Capture.layouts,
        page075: page075Capture.layouts,
        adjustment_dialog: dialogCapture.layouts,
      },
      screenshots: [
        ...page074Capture.screenshots,
        ...page075Capture.screenshots,
        ...dialogCapture.screenshots,
      ],
      network,
      privacy: {
        environment_only_credentials: true,
        draft_destroyed_before_capture: true,
        protected_detail_masked: page075Capture.protectedDetailMasked,
        safe_projection_verified: true,
      },
      cleanup: { session_revoked: logout.logged_out, browser_closed: true },
    };
    assertSafeEvidence(result, [config.adminPassword, config.adminEmail, config.tenantSlug, draftMarker]);
  } catch (error) {
    failure = toSafeFailure(error, "browser.execute", "unexpected_failure");
  } finally {
    try {
      // 仅在成功登录且尚未收到服务端撤销确认时兜底，避免普通启动失败误发登出请求。
      if (sessionNeedsRevocation && page) {
        const sessionRevoked = await revokeSessionAfterFailure(page, config);
        sessionNeedsRevocation = !sessionRevoked;
        // 撤销失败不能覆盖主业务失败；后续只传播固定清理码，不暴露底层异常或认证材料。
        if (!sessionRevoked) {
          if (failure) {
            // 主失败阶段保持不变；只附加固定清理码，避免底层网络或 Token 信息泄漏。
            failure.cleanupCode = "session_revoke_failed";
          } else {
            failure = new BrowserAcceptanceFailure("browser.cleanup", "session_revoke_failed");
          }
        }
      }
      if (context) await context.close();
      if (browser) await browser.close();
    } catch {
      if (!failure) failure = new BrowserAcceptanceFailure("browser.cleanup", "close_failed");
    }
  }
  if (failure) throw failure;
  requireCondition(result && result.status === "passed", "browser.execute", "result_missing");
  await publishBrowserEvidence(config.artifactDirectory, result);
  return result;
}


/** 命令行入口：成功输出安全 JSON，失败只输出固定 stage/code。 */
async function main() {
  try {
    const result = await executeBrowserAcceptance(parseArguments());
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } catch (error) {
    const safe = toSafeFailure(error, "browser.main", "unexpected_failure");
    const cleanupSuffix = safe.cleanupCode ? ` cleanup_code=${safe.cleanupCode}` : "";
    process.stderr.write(`${safe.message}${cleanupSuffix}\n`);
    process.exitCode = 1;
  }
}


// 单元或语法检查 import 时不触发网络和 Chrome；直接执行才进入真实验收。
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}


export {
  BrowserAcceptanceFailure,
  VIEWPORTS,
  assertLayoutMetrics,
  assertProtectedDetailMaskCoverage,
  assertSafeEvidence,
  assertViewportIntersection,
  browserRequestAllowed,
  inspectScreenshot,
  normalizeLoopbackOrigin,
  readEnvironmentIdentity,
  resolveArtifactDirectory,
  toSafeFailure,
  revokeSessionAfterFailure,
  verifyLogoutEvidence,
};
