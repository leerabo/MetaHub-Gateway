/**
 * PRD-056 PAGE-081/PAGE-082 真实浏览器验收器。
 *
 * 脚本只访问调用方启动的三个回环服务，并消费同批次 Runtime manifest。密码、Session
 * Token、规则 pattern 和原始 Provider 正文只驻留内存；截图与 Browser JSON 只写入
 * .tmp/prd056/<batch> 的普通目录。
 */

import { createHash, randomBytes } from "node:crypto";
import { existsSync, lstatSync, realpathSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { inflateSync } from "node:zlib";


const require = createRequire(import.meta.url);
// 这些常量冻结视口、回环边界、公开 ID 和敏感值规则，避免运行参数扩大网络或证据范围。
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const BATCH_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$/;
const SAFE_IDENTIFIER_PATTERN = /^[A-Za-z0-9_.:-]{1,160}$/;
const ERROR_MAP_ID_PATTERN = /^pem_[0-9a-f]{32}$/;
const AUTH_STORAGE_KEYS = new Set(["metahub.auth_token", "metahub.auth_context"]);
const SENSITIVE_URL_KEY_PATTERN = /password|token|secret|authorization|prompt|response|provider[_-]?key|api[_-]?key|payload|tenant[_-]?id/i;
const FORBIDDEN_EVIDENCE_KEYS = new Set([
  "authorization", "credential_id", "encrypted_key", "endpoint", "endpoint_url",
  "field_pattern", "message_pattern", "messages", "password", "payload", "prompt",
  "provider_key", "raw_body", "response", "response_body", "secret", "token",
]);
const FORBIDDEN_VALUE_PATTERN = /Bearer\s+[^\s"']{8,}|\bmhv_[A-Za-z0-9_-]{12,}\b|\bsk[-_][A-Za-z0-9_-]{12,}\b/i;
const PRIVATE_PROVIDER_SENTINEL = "prd056-private-upstream-body-sentinel";
const EXPECTED_HTTP_CONSOLE_MESSAGES = new Set([
  "Failed to load resource: the server responded with a status of 403",
  "Failed to load resource: the server responded with a status of 403 (Forbidden)",
  "Failed to load resource: the server responded with a status of 409",
  "Failed to load resource: the server responded with a status of 409 (Conflict)",
]);


/** 仅承载代码内固定分类的安全异常，禁止把 URL、DOM 或凭据拼进错误消息。 */
class BrowserAcceptanceFailure extends Error {
  /** 保存固定失败分类；调用点不得传入 URL、DOM、Token 或响应正文。 */
  constructor(stage, code) {
    super("prd056_browser_acceptance_failed stage=" + stage + " code=" + code);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 以固定 stage/code 终止验收，避免 Playwright 异常泄露页面内容。 */
function fail(stage, code) {
  throw new BrowserAcceptanceFailure(stage, code);
}


/** 执行失败关闭断言，所有失败文本都来自代码内稳定分类。 */
function requireCondition(condition, stage, code) {
  if (!condition) fail(stage, code);
}


/** 把未知浏览器异常折叠为固定分类，不传播底层 URL 或 DOM。 */
function toSafeFailure(error, stage, code = "stage_failed") {
  if (error instanceof BrowserAcceptanceFailure) return error;
  return new BrowserAcceptanceFailure(stage, code);
}


/** 包装一次浏览器操作，禁止底层异常正文进入终端。 */
async function runSafeBrowserOperation(stage, code, operation) {
  try {
    return await operation();
  } catch (error) {
    // Playwright 异常可能携带页面正文或地址，因此统一折叠后再向上传播。
    throw toSafeFailure(error, stage, code);
  }
}


/** 规范化带显式端口的 HTTP 回环 origin，拒绝凭据、路径和 URL 后缀。 */
function normalizeLoopbackOrigin(value, label = "origin") {
  let target;
  try {
    target = new URL(String(value));
  } catch {
    fail("config." + label, "invalid_url");
  }
  const hostname = target.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  requireCondition(target.protocol === "http:", "config." + label, "scheme_not_http");
  requireCondition(LOOPBACK_HOSTS.has(hostname), "config." + label, "host_not_loopback");
  requireCondition(!target.username && !target.password, "config." + label, "credentials");
  requireCondition(target.pathname === "/" && !target.search && !target.hash, "config." + label, "suffix");
  requireCondition(Boolean(target.port), "config." + label, "port_missing");
  return target.origin;
}


/** 从脚本物理位置恢复唯一 MetaHub 根，不允许环境变量扩大信任边界。 */
function workspaceRoot() {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  requireCondition(path.basename(root).toLowerCase() === "metahub", "config.workspace", "unexpected_root");
  return root;
}


/** 将证据路径限制为工作区 .tmp/prd056 下一个普通批次目录。 */
function resolveArtifactDirectory(value, root = workspaceRoot()) {
  const artifactRoot = path.resolve(root, ".tmp", "prd056");
  const candidate = path.resolve(root, String(value));
  const relative = path.relative(artifactRoot, candidate);
  requireCondition(relative && !relative.startsWith("..") && !path.isAbsolute(relative), "config.artifact", "outside_root");
  requireCondition(!relative.includes(path.sep), "config.artifact", "batch_depth");
  requireCondition(BATCH_NAME_PATTERN.test(relative), "config.artifact", "batch_name");
  return candidate;
}


/** 检查批次、Runtime manifest 和 runtime.db 都是批次内普通实体。 */
function assertArtifactDirectorySafe(candidate) {
  const directory = path.resolve(candidate);
  try {
    const directoryStat = lstatSync(directory);
    requireCondition(directoryStat.isDirectory() && !directoryStat.isSymbolicLink(), "config.artifact", "directory_type");
    requireCondition(path.resolve(realpathSync(directory)) === directory, "config.artifact", "directory_realpath");
    for (const filename of ["runtime-acceptance.json", "runtime.db"]) {
      const target = path.join(directory, filename);
      const stat = lstatSync(target);
      requireCondition(stat.isFile() && !stat.isSymbolicLink(), "config.artifact", "file_type");
      requireCondition(path.relative(directory, path.resolve(realpathSync(target))) === filename, "config.artifact", "file_realpath");
    }
  } catch (error) {
    // 已分类失败必须原样保留，未知文件系统细节则转换为固定错误码。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    fail("config.artifact", "missing_or_unreadable");
  }
  return directory;
}


/** 读取管理员身份并立即删除所有环境变量别名，避免浏览器子进程继承。 */
function readEnvironmentIdentity(env = process.env) {
  const password = String(env.METAHUB_PRD056_ADMIN_PASSWORD || env.METAHUB_ADMIN_PASSWORD || "");
  const email = String(env.METAHUB_PRD056_ADMIN_EMAIL || "admin@metahub.local").trim().toLowerCase();
  const tenantSlug = String(env.METAHUB_PRD056_TENANT_SLUG || "default").trim();
  for (const name of [
    "METAHUB_PRD056_ADMIN_PASSWORD", "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD056_ADMIN_EMAIL", "METAHUB_PRD056_TENANT_SLUG",
  ]) delete env[name];
  requireCondition(password.length >= 8, "config.identity", "password_missing");
  requireCondition(email.includes("@") && Boolean(tenantSlug), "config.identity", "identity_invalid");
  // 身份只保留在当前进程内，返回值不允许写入 Browser 证据。
  return { role: "admin", email, password, tenantSlug };
}


/** 解析非敏感 CLI 参数；未知、重复或缺值参数全部失败关闭。 */
function parseArguments(argv = process.argv.slice(2), env = process.env) {
  const identity = readEnvironmentIdentity(env);
  const values = new Map();
  let headed = false;
  for (let index = 0; index < argv.length; index += 1) {
    const name = String(argv[index]);
    if (name === "--headed") {
      headed = true;
      continue;
    }
    requireCondition(
      ["--artifact-dir", "--console-url", "--control-url", "--gateway-url", "--chrome-path"].includes(name),
      "config.arguments",
      "unknown_argument",
    );
    requireCondition(index + 1 < argv.length && !values.has(name), "config.arguments", "argument_invalid");
    values.set(name, String(argv[++index]));
  }
  const artifactValue = values.get("--artifact-dir") || env.METAHUB_PRD056_ARTIFACT_DIR || "";
  requireCondition(Boolean(artifactValue), "config.artifact", "missing");
  const artifactDirectory = assertArtifactDirectorySafe(resolveArtifactDirectory(artifactValue));
  const origins = {
    consoleOrigin: normalizeLoopbackOrigin(values.get("--console-url") || "http://localhost:5173", "console"),
    controlOrigin: normalizeLoopbackOrigin(values.get("--control-url") || "http://localhost:8001", "control"),
    gatewayOrigin: normalizeLoopbackOrigin(values.get("--gateway-url") || "http://localhost:8000", "gateway"),
  };
  requireCondition(new Set(Object.values(origins)).size === 3, "config.origins", "not_distinct");
  const chromePath = values.get("--chrome-path") || env.METAHUB_PLAYWRIGHT_CHROME_PATH || "";
  if (chromePath) requireCondition(path.isAbsolute(chromePath) && existsSync(chromePath), "config.chrome", "path_invalid");
  return {
    ...identity,
    ...origins,
    artifactDirectory,
    databasePath: path.join(artifactDirectory, "runtime.db"),
    chromePath,
    headed,
  };
}


/** 规范化字段名，防止 camelCase 和标点绕过隐私门禁。 */
function normalizeEvidenceKey(key) {
  return String(key)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}


/** 递归拒绝敏感字段、凭据形态、Provider 正文和本批次已知明文。 */
function assertSafeEvidence(value, knownSensitive = [], stage = "evidence.privacy") {
  const walk = (node) => {
    if (Array.isArray(node)) {
      for (const item of node) walk(item);
      return;
    }
    if (node && typeof node === "object") {
      for (const [key, child] of Object.entries(node)) {
        requireCondition(!FORBIDDEN_EVIDENCE_KEYS.has(normalizeEvidenceKey(key)), stage, "forbidden_key");
        walk(child);
      }
      return;
    }
    requireCondition(node === null || ["string", "number", "boolean"].includes(typeof node), stage, "non_json_value");
  };
  walk(value);
  const encoded = JSON.stringify(value);
  requireCondition(!FORBIDDEN_VALUE_PATTERN.test(encoded), stage, "credential_pattern");
  requireCondition(!encoded.includes(PRIVATE_PROVIDER_SENTINEL), stage, "provider_body");
  for (const sensitive of knownSensitive) {
    if (sensitive) requireCondition(!encoded.includes(sensitive), stage, "known_sensitive");
  }
  return { evidence_privacy_verified: true };
}


/** 验证 Runtime manifest 的同批次握手字段，禁止浏览器自行猜测历史 ID。 */
function validateRuntimeEvidence(payload) {
  requireCondition(payload && typeof payload === "object" && !Array.isArray(payload), "runtime_evidence", "not_object");
  requireCondition(payload.prd_id === "PRD-056" && payload.status === "passed", "runtime_evidence", "status");
  const resources = payload.generated_resources;
  requireCondition(resources && typeof resources === "object", "runtime_evidence", "resources");
  requireCondition(ERROR_MAP_ID_PATTERN.test(resources.error_map_id), "runtime_evidence", "error_map_id");
  requireCondition(Number.isInteger(resources.error_map_version) && resources.error_map_version >= 2, "runtime_evidence", "version");
  requireCondition(Number.isInteger(resources.head_revision) && resources.head_revision >= 1, "runtime_evidence", "revision");
  for (const name of ["mapped_request_id", "nonstream_request_id", "stream_request_id"]) {
    requireCondition(
      typeof resources[name] === "string" && SAFE_IDENTIFIER_PATTERN.test(resources[name]),
      "runtime_evidence",
      name,
    );
  }
  requireCondition(payload.checks?.gateway?.hot_update_without_gateway_restart === true, "runtime_evidence", "hot_update");
  requireCondition(
    payload.checks?.cleanup?.all_sessions_revoked === true
      && payload.checks?.cleanup?.temporary_users_disabled === true,
    "runtime_evidence",
    "cleanup",
  );
  assertSafeEvidence(payload);
  return {
    errorMapId: resources.error_map_id,
    errorMapVersion: resources.error_map_version,
    headRevision: resources.head_revision,
    mappedRequestId: resources.mapped_request_id,
    nonstreamRequestId: resources.nonstream_request_id,
    streamRequestId: resources.stream_request_id,
  };
}


/**
 * 解析编辑对话框冻结的当前规则版本，并确认它没有回退到 Runtime manifest 基线之前。
 * Browser 失败重跑时数据库版本允许单调前进，但 DOM 缺失、非规范整数或版本倒退必须失败关闭。
 */
function parseCurrentErrorMapVersion(value, baselineVersion) {
  const version = Number(value);
  requireCondition(
    typeof value === "string"
      && /^[1-9]\d*$/.test(value)
      && Number.isSafeInteger(version)
      && Number.isSafeInteger(baselineVersion)
      && baselineVersion >= 1
      && version >= baselineVersion,
    "browser.conflict",
    "current_version_invalid",
  );
  return version;
}


/**
 * 为并行更新、stale 表单和最终保存规划三个互不相同且区别于当前值的合法优先级。
 * 固定候选区间便于验收取证；排除当前值可保证重复运行时并行 PATCH 一定不是 no-op。
 */
function planConflictPriorities(value) {
  const currentPriority = Number(value);
  requireCondition(
    typeof value === "string"
      && /^[1-9]\d*$/.test(value)
      && Number.isSafeInteger(currentPriority)
      && currentPriority >= 1
      && currentPriority <= 1000,
    "browser.conflict",
    "current_priority_invalid",
  );
  const [directPriority, stalePriority, finalPriority] = [911, 912, 913, 914]
    .filter((candidate) => candidate !== currentPriority)
    .slice(0, 3);
  return Object.freeze({ directPriority, stalePriority, finalPriority });
}


/** 冻结 Browser 必须实际打开的 attempt 与 stream 两个历史 Fact owner。 */
function requestLogTargets(runtime) {
  requireCondition(runtime && typeof runtime === "object", "runtime_evidence", "request_targets");
  return Object.freeze([
    Object.freeze({ key: "mapped", requestId: runtime.mappedRequestId, owner: "attempt", tenantRuleJump: true }),
    Object.freeze({ key: "stream", requestId: runtime.streamRequestId, owner: "stream_session", tenantRuleJump: false }),
  ]);
}


/** 仅提取已校验 Runtime 资源中的字符串 ID，避免数字 revision 进入 String.includes。 */
function runtimePublicIdentifiers(runtime) {
  requireCondition(runtime && typeof runtime === "object" && !Array.isArray(runtime), "runtime_evidence.ids", "not_object");
  return Object.values(runtime).filter((value) => typeof value === "string" && value.length > 0);
}


/** 读取 Runtime manifest 并冻结原始字节 SHA-256，供 Browser 证据关联。 */
async function loadRuntimeEvidence(artifactDirectory) {
  const target = path.join(artifactDirectory, "runtime-acceptance.json");
  let raw;
  let payload;
  try {
    raw = await readFile(target);
    payload = JSON.parse(raw.toString("utf8"));
  } catch {
    fail("runtime_evidence.load", "invalid_json");
  }
  return {
    runtime: validateRuntimeEvidence(payload),
    sha256: createHash("sha256").update(raw).digest("hex"),
  };
}


/** 判断浏览器请求是否只访问三个允许的本机 origin，URL 参数不得携带敏感键。 */
function browserRequestAllowed(rawUrl, allowedOrigins) {
  let target;
  try {
    target = new URL(rawUrl);
  } catch {
    return false;
  }
  if (["data:", "blob:", "about:"].includes(target.protocol)) return true;
  if (!allowedOrigins.has(target.origin) || target.username || target.password) return false;
  for (const key of target.searchParams.keys()) {
    if (SENSITIVE_URL_KEY_PATTERN.test(key)) return false;
  }
  return true;
}


/**
 * 把 Chromium 为预期 403/409 生成的无参数网络错误与应用 console.error 分开。
 * 只接受固定英文浏览器文案且不读取参数值；404/500、带参数日志或未知格式都按异常计数。
 */
function classifyBrowserConsoleMessage({ type = "", text = "", argumentCount = -1 } = {}) {
  if (type !== "error") return "ignored";
  if (argumentCount === 0 && EXPECTED_HTTP_CONSOLE_MESSAGES.has(text)) {
    return "expected_http_error";
  }
  return "unexpected_error";
}


/** 安装网络、页面和 console 门禁，并按角色统计 error-map 请求。 */
async function installBrowserGuards(context, page, config) {
  const allowedOrigins = new Set([config.consoleOrigin, config.controlOrigin, config.gatewayOrigin]);
  const tracker = {
    blocked_request_count: 0,
    browser_error_count: 0,
    console_error_count: 0,
    expected_http_console_error_count: 0,
    error_map_get_count: 0,
    error_map_mutation_count: 0,
    request_log_get_count: 0,
  };
  await context.route("**/*", async (route) => {
    // 只放行三个已冻结回环 origin，其他请求在浏览器发出前即失败关闭。
    if (browserRequestAllowed(route.request().url(), allowedOrigins)) {
      await route.continue();
    } else {
      tracker.blocked_request_count += 1;
      await route.abort("blockedbyclient");
    }
  });
  page.on("pageerror", () => { tracker.browser_error_count += 1; });
  page.on("console", (message) => {
    // 只在内存中匹配固定浏览器文案；message 正文和参数绝不写入终端、JSON 或日志。
    const classification = classifyBrowserConsoleMessage({
      type: message.type(),
      text: message.text(),
      argumentCount: message.args().length,
    });
    // 预期 HTTP 错误只计入独立验收指标，用于证明异常流程确实触发，不应混入真实浏览器错误总数。
    if (classification === "expected_http_error") {
      tracker.expected_http_console_error_count += 1;
    }
    // 意外错误必须独立判断并累计，不能挂在白名单分支的 else 上，避免白名单命中掩盖真实控制台错误。
    if (classification === "unexpected_error") {
      tracker.console_error_count += 1;
    }
  });
  page.on("request", (request) => {
    try {
      const target = new URL(request.url());
      if (target.origin !== config.controlOrigin) return;
      // Error Map 统计同时覆盖列表、详情和写请求，用于证明 viewer 没有预取或越权写入。
      if (target.pathname === "/error-maps" || target.pathname.startsWith("/error-maps/")) {
        // GET 与 mutation 分开计数，避免前端隐藏按钮掩盖真实后端访问。
        if (request.method() === "GET") tracker.error_map_get_count += 1;
        // 非 GET 均视为 mutation，新增方法也不会绕过验收统计。
        else tracker.error_map_mutation_count += 1;
      }
      if (target.pathname === "/request-logs" || target.pathname.startsWith("/request-logs/")) {
        if (request.method() === "GET") tracker.request_log_get_count += 1;
      }
    } catch {
      tracker.blocked_request_count += 1;
    }
  });
  return tracker;
}


/** 通过真实登录页建立 Session，并核验后端角色与 shell 身份。 */
async function loginThroughConsole(page, config, identity) {
  await runSafeBrowserOperation("browser.login.navigate", "failed", () => page.goto(config.consoleOrigin + "/", { waitUntil: "domcontentloaded" }));
  const form = page.locator("#login-form");
  await form.waitFor({ state: "visible", timeout: 30000 });
  await form.locator('[name="tenantSlug"]').fill(identity.tenantSlug || config.tenantSlug);
  await form.locator('[name="email"]').fill(identity.email);
  await form.locator('[name="password"]').fill(identity.password);
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      // 登录只接受冻结 Control origin 的 POST，避免同路径第三方响应造成假阳性。
      return target.origin === config.controlOrigin && target.pathname === "/auth/login" && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await form.locator('button[type="submit"]').click();
  const response = await responsePromise;
  requireCondition(response.status() === 200, "browser.login", "status");
  const shell = page.locator(".console-shell");
  await shell.waitFor({ state: "visible", timeout: 30000 });
  // 角色必须来自登录后真实侧栏文本；生产 Shell 没有测试专用 data 属性，不能等待虚构 DOM。
  const renderedRole = await shell.locator(".sidebar-title span").textContent();
  requireCondition(
    String(renderedRole ?? "").trim() === identity.role, "browser.login", "role_mismatch",
  );
}


/**
 * 校验注销后的脱敏计数，只要求认证槽位、敏感键和受保护 DOM 清零。
 * 开发服务器可保留不含敏感信息的会话键，不能把“认证已销毁”错误扩大为整个 sessionStorage 为空。
 */
function assertLoggedOutState(metrics) {
  const names = [
    "local_storage_count",
    "session_auth_slot_count",
    "session_non_auth_slot_count",
    "forbidden_storage_key_count",
    "protected_dom_count",
  ];
  requireCondition(
    metrics && typeof metrics === "object" && !Array.isArray(metrics)
      && names.every((name) => Number.isSafeInteger(metrics[name]) && metrics[name] >= 0),
    "browser.logout",
    "metrics_invalid",
  );
  // 各残留面使用固定分类，失败日志不包含键名、DOM 或敏感值，同时能直接定位竞态发生在哪一层。
  requireCondition(metrics.local_storage_count === 0, "browser.logout", "local_storage_not_cleared");
  requireCondition(metrics.session_auth_slot_count === 0, "browser.logout", "auth_slots_not_cleared");
  requireCondition(metrics.forbidden_storage_key_count === 0, "browser.logout", "sensitive_storage_not_cleared");
  requireCondition(metrics.protected_dom_count === 0, "browser.logout", "protected_dom_not_cleared");
  return { storage_cleared: true, protected_dom_cleared: true };
}


/** 点击真实注销按钮，并验证 DOM 与两个认证槽位均已销毁。 */
async function logoutAndVerify(page) {
  // 等待器与点击属于同一 Promise 组；点击失败时也会消费后续超时拒绝，避免 finally 遗留未处理异常。
  const [response] = await Promise.all([
    page.waitForResponse((candidate) => {
      try {
        // 注销必须观察真实 POST 响应，单纯跳回登录页不能视为 Session 已撤销。
        return new URL(candidate.url()).pathname === "/auth/logout" && candidate.request().method() === "POST";
      } catch {
        return false;
      }
    }, { timeout: 30000 }),
    page.locator("#logout-button").click(),
  ]);
  requireCondition(response.status() === 200, "browser.logout", "status");
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30000 });
  const cleared = await page.evaluate((authKeys) => {
    const local = Object.entries(localStorage);
    const session = Object.entries(sessionStorage);
    const forbiddenKey = /password|token|secret|prompt|response|payload|request[_-]?id|tenant[_-]?id|provider[_-]?key/i;
    return {
      local_storage_count: local.length,
      session_auth_slot_count: session.filter(([key]) => authKeys.includes(key)).length,
      session_non_auth_slot_count: session.filter(([key]) => !authKeys.includes(key)).length,
      forbidden_storage_key_count: [...local, ...session]
        .filter(([key]) => !authKeys.includes(key) && forbiddenKey.test(key)).length,
      protected_dom_count: document.querySelectorAll(".error-maps-panel,.provider-error-fact").length,
    };
  }, [...AUTH_STORAGE_KEYS]);
  return assertLoggedOutState(cleared);
}


/** 点击后端允许的导航并等待 shell 与目标面板同时稳定。 */
async function openNavigation(page, itemId, panelSelector) {
  const button = page.locator(`[data-console-nav="${itemId}"]`);
  await button.waitFor({ state: "visible", timeout: 30000 });
  await button.click();
  await page.locator(`.console-shell[data-active-navigation="${itemId}"]`).waitFor({ state: "visible", timeout: 30000 });
  await page.locator(panelSelector).waitFor({ state: "visible", timeout: 30000 });
}


/** 建立一次性 developer/viewer 账号账本，意图先于 HTTP 写入登记。 */
function createTemporaryAccountLedger(config, nowMillis = Date.now()) {
  requireCondition(Number.isFinite(nowMillis), "browser.users.ledger", "clock");
  return { tenantSlug: config.tenantSlug, createdAfter: new Date(nowMillis - 2000).toISOString(), intents: [] };
}


/** 在管理员真实 Session 内创建 developer/viewer，密码只驻留当前进程。 */
async function createRestrictedRoleUsers(page, config, ledger) {
  const credentials = [];
  for (const role of ["developer", "viewer"]) {
    const suffix = randomBytes(6).toString("hex");
    const email = `prd056-browser-${role}-${suffix}@metahub.local`;
    const displayName = `PRD056 Browser ${role} ${suffix}`;
    const password = `Prd056Browser!${randomBytes(18).toString("hex")}`;
    const requestId = `req_p56_browser_create_${role}_${suffix}`;
    const traceId = `trc_p56_browser_create_${role}_${suffix}`;
    const intent = { role, email, displayName, userId: "", tenantId: "" };
    ledger.intents.push(intent);
    const result = await page.evaluate(async (input) => {
      const access = sessionStorage.getItem("metahub.auth_token");
      if (!access) return { status: 0 };
      const response = await fetch(input.controlOrigin + "/users", {
        method: "POST",
        headers: {
          Authorization: "Bearer " + access,
          "Content-Type": "application/json",
          "X-Request-ID": input.requestId,
          "X-Trace-ID": input.traceId,
        },
        body: JSON.stringify({
          email: input.email,
          display_name: input.displayName,
          password: input.password,
          role: input.role,
          status: "active",
        }),
      });
      const body = await response.json().catch(() => ({}));
      return {
        status: response.status,
        role: body.user?.role ?? "",
        userId: body.user?.user_id ?? "",
        tenantId: body.user?.tenant_id ?? "",
        requestId: body.request_id ?? "",
      };
    }, { controlOrigin: config.controlOrigin, role, email, displayName, password, requestId, traceId });
    requireCondition(result.status === 201 && result.role === role, "browser.users." + role, "create");
    requireCondition(SAFE_IDENTIFIER_PATTERN.test(result.userId) && SAFE_IDENTIFIER_PATTERN.test(result.tenantId), "browser.users." + role, "owner");
    requireCondition(result.requestId === requestId, "browser.users." + role, "request_id");
    intent.userId = result.userId;
    intent.tenantId = result.tenantId;
    credentials.push({ role, email, password, tenantSlug: config.tenantSlug });
  }
  return credentials;
}


/** 按随机邮箱、创建时间、角色和 tenant owner 停用临时账号并撤销全部 Session。 */
function disableTemporaryUsersAndSessions(config, ledger, { requireAll = false } = {}) {
  requireCondition(ledger?.tenantSlug === config.tenantSlug && Array.isArray(ledger.intents), "browser.users.cleanup", "ledger");
  let database;
  let transactionOpen = false;
  try {
    const { DatabaseSync } = require("node:sqlite");
    database = new DatabaseSync(config.databasePath);
    database.exec("PRAGMA foreign_keys=ON; PRAGMA busy_timeout=10000; BEGIN IMMEDIATE");
    transactionOpen = true;
    const selectUser = database.prepare(`
      SELECT u.user_id,u.tenant_id,u.email,u.display_name,u.role,u.status,u.created_at
      FROM users u JOIN tenants t ON t.tenant_id=u.tenant_id
      WHERE t.slug=? AND u.email=?
    `);
    const revoke = database.prepare(`
      UPDATE sessions SET status='revoked',revoked_at=COALESCE(revoked_at,?),updated_at=?
      WHERE tenant_id=? AND user_id=? AND status<>'revoked'
    `);
    const disable = database.prepare(`
      UPDATE users SET status='disabled',updated_at=?
      WHERE tenant_id=? AND user_id=? AND status<>'disabled'
    `);
    const remaining = database.prepare(`
      SELECT COUNT(*) AS count FROM sessions
      WHERE tenant_id=? AND user_id=? AND status<>'revoked'
    `);
    const now = new Date().toISOString();
    let matched = 0;
    let disabled = 0;
    let revoked = 0;
    for (const intent of ledger.intents) {
      requireCondition(
        ["developer", "viewer"].includes(intent.role)
          && intent.email.startsWith(`prd056-browser-${intent.role}-`)
          && intent.email.endsWith("@metahub.local"),
        "browser.users.cleanup",
        "intent",
      );
      const row = selectUser.get(ledger.tenantSlug, intent.email);
      if (!row) continue;
      requireCondition(
        row.user_id === intent.userId
          && row.tenant_id === intent.tenantId
          && row.role === intent.role
          && row.display_name === intent.displayName
          && Date.parse(row.created_at) >= Date.parse(ledger.createdAfter),
        "browser.users.cleanup",
        "owner_mismatch",
      );
      matched += 1;
      revoked += Number(revoke.run(now, now, row.tenant_id, row.user_id).changes);
      disabled += Number(disable.run(now, row.tenant_id, row.user_id).changes);
      requireCondition(Number(remaining.get(row.tenant_id, row.user_id).count) === 0, "browser.users.cleanup", "session_active");
    }
    if (requireAll) requireCondition(ledger.intents.length === 2 && matched === 2, "browser.users.cleanup", "user_missing");
    database.exec("COMMIT");
    transactionOpen = false;
    return {
      intended_user_count: ledger.intents.length,
      matched_user_count: matched,
      newly_disabled_user_count: disabled,
      newly_revoked_session_count: revoked,
      all_matched_users_disabled: matched === ledger.intents.length,
      all_matched_sessions_revoked: true,
    };
  } catch (error) {
    if (transactionOpen) {
      try { database.exec("ROLLBACK"); } catch { /* 保留更具体的清理失败。 */ }
    }
    // 已分类的 owner/事务失败直接保留，未知 SQLite 细节统一脱敏。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    fail("browser.users.cleanup", "database_failed");
  } finally {
    try { database?.close(); } catch { /* 关闭失败不覆盖主结论。 */ }
  }
}


/** 在页面 Session 内直调 Control，用于证明隐藏按钮之外的后端 RBAC。 */
async function directControlCall(page, config, { method, pathname, body, requestId, traceId }) {
  return page.evaluate(async (input) => {
    const access = sessionStorage.getItem("metahub.auth_token");
    if (!access) return { status: 0, code: "", requestId: "" };
    const response = await fetch(input.controlOrigin + input.pathname, {
      method: input.method,
      headers: {
        Authorization: "Bearer " + access,
        "Content-Type": "application/json",
        "X-Request-ID": input.requestId,
        "X-Trace-ID": input.traceId,
      },
      body: input.body === undefined ? undefined : JSON.stringify(input.body),
    });
    const payload = await response.json().catch(() => ({}));
    return { status: response.status, code: payload.code ?? "", requestId: payload.request_id ?? "", body: payload };
  }, { controlOrigin: config.controlOrigin, method, pathname, body, requestId, traceId });
}


/** 等待页面布局、字体与两帧渲染稳定，避免截图捕获过渡态。 */
async function settleLayout(page) {
  await page.evaluate(async () => {
    await document.fonts?.ready;
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}


/** 读取水平溢出、遮挡和目标可见性指标；不返回 DOM 正文。 */
async function readLayoutMetrics(page, targetSelector) {
  return page.evaluate((selector) => {
    const root = document.documentElement;
    const target = document.querySelector(selector);
    const navigation = document.querySelector(".console-sidebar");
    const main = document.querySelector(".console-main");
    const targetBox = target?.getBoundingClientRect() ?? null;
    const navigationBox = navigation?.getBoundingClientRect() ?? null;
    const mainBox = main?.getBoundingClientRect() ?? null;
    const overlaps = navigationBox && mainBox
      ? Math.max(0, Math.min(navigationBox.right, mainBox.right) - Math.max(navigationBox.left, mainBox.left))
        * Math.max(0, Math.min(navigationBox.bottom, mainBox.bottom) - Math.max(navigationBox.top, mainBox.top))
      : 0;
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: root.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      target_visible: Boolean(targetBox && targetBox.width > 0 && targetBox.height > 0),
      target_inside_viewport: Boolean(targetBox && targetBox.left >= -1 && targetBox.right <= innerWidth + 1),
      navigation_main_overlap_area: overlaps,
      dialog_count: document.querySelectorAll('[role="dialog"]').length,
    };
  }, targetSelector);
}


/** 验证目标不产生水平溢出或侧栏遮挡；移动端允许侧栏折叠但不允许覆盖主内容。 */
function assertLayoutMetrics(metrics, viewport, stage = "browser.layout") {
  requireCondition(metrics.viewport_width === viewport.width && metrics.viewport_height === viewport.height, stage, "viewport");
  requireCondition(metrics.document_scroll_width <= viewport.width + 1 && metrics.body_scroll_width <= viewport.width + 1, stage, "horizontal_overflow");
  requireCondition(metrics.target_visible && metrics.target_inside_viewport, stage, "target_bounds");
  requireCondition(metrics.navigation_main_overlap_area === 0, stage, "navigation_overlap");
  return metrics;
}


/** PNG Paeth 预测器，用于无依赖还原扫描线像素。 */
function paethPredictor(left, up, upperLeft) {
  const estimate = left + up - upperLeft;
  const leftDistance = Math.abs(estimate - left);
  const upDistance = Math.abs(estimate - up);
  const cornerDistance = Math.abs(estimate - upperLeft);
  if (leftDistance <= upDistance && leftDistance <= cornerDistance) return left;
  return upDistance <= cornerDistance ? up : upperLeft;
}


/** 解码 Playwright PNG 的 RGB/RGBA 扫描线并确认截图不是单色空白。 */
function inspectPngPixels(buffer, viewport, stage = "browser.screenshot") {
  requireCondition(Buffer.isBuffer(buffer) && buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])), stage, "signature");
  let offset = 8;
  let width = 0;
  let height = 0;
  let colorType = -1;
  const chunks = [];
  while (offset + 12 <= buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.subarray(offset + 4, offset + 8).toString("ascii");
    const data = buffer.subarray(offset + 8, offset + 8 + length);
    requireCondition(offset + 12 + length <= buffer.length, stage, "chunk_bounds");
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      colorType = data[9];
      requireCondition(data[8] === 8 && data[10] === 0 && data[11] === 0 && data[12] === 0, stage, "png_format");
    } else if (type === "IDAT") chunks.push(data);
    offset += 12 + length;
    if (type === "IEND") break;
  }
  requireCondition(width === viewport.width && height === viewport.height, stage, "dimensions");
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : 0;
  requireCondition(channels > 0 && chunks.length > 0, stage, "color_type");
  const raw = inflateSync(Buffer.concat(chunks));
  const stride = width * channels;
  requireCondition(raw.length === (stride + 1) * height, stage, "scanline_length");
  let previous = Buffer.alloc(stride);
  let cursor = 0;
  let firstPixel = null;
  let changedPixels = 0;
  const colors = new Set();
  for (let row = 0; row < height; row += 1) {
    const filter = raw[cursor++];
    const reconstructed = Buffer.allocUnsafe(stride);
    for (let index = 0; index < stride; index += 1) {
      const value = raw[cursor++];
      const left = index >= channels ? reconstructed[index - channels] : 0;
      const up = previous[index] ?? 0;
      const corner = index >= channels ? previous[index - channels] : 0;
      const predictor = filter === 0 ? 0
        : filter === 1 ? left
          : filter === 2 ? up
            : filter === 3 ? Math.floor((left + up) / 2)
              : filter === 4 ? paethPredictor(left, up, corner)
                : -1;
      requireCondition(predictor >= 0, stage, "filter");
      reconstructed[index] = (value + predictor) & 255;
    }
    for (let column = 0; column < width; column += Math.max(1, Math.floor(width / 80))) {
      const pixel = reconstructed.subarray(column * channels, column * channels + 3).toString("hex");
      colors.add(pixel);
      if (firstPixel === null) firstPixel = pixel;
      else if (pixel !== firstPixel) changedPixels += 1;
    }
    previous = reconstructed;
  }
  requireCondition(colors.size >= 4 && changedPixels >= 20, stage, "blank_pixels");
  return { width, height, sampled_color_count: colors.size, non_blank_pixels: true };
}


/** 固定截图文件名，状态和 viewport 只能来自代码内枚举。 */
function screenshotFilename(stateName, viewportName) {
  requireCondition(/^[a-z0-9-]{3,48}$/.test(stateName), "evidence.screenshot", "state_name");
  requireCondition(/^(desktop-1280x720|mobile-390x844)$/.test(viewportName), "evidence.screenshot", "viewport_name");
  return `${stateName}-${viewportName}.png`;
}


/** 捕获当前全页视口并返回脱敏的文件摘要和布局指标。 */
async function captureState(page, screenshotDirectory, stateName, viewport, selector) {
  await settleLayout(page);
  const layout = assertLayoutMetrics(await readLayoutMetrics(page, selector), viewport, `browser.layout.${stateName}`);
  const filename = screenshotFilename(stateName, viewport.name);
  const target = path.join(screenshotDirectory, filename);
  const buffer = await runSafeBrowserOperation(`browser.screenshot.${stateName}`, "capture_failed", () => page.screenshot({ path: target, type: "png", fullPage: false }));
  const pixels = inspectPngPixels(buffer, viewport, `browser.screenshot.${stateName}`);
  return {
    screenshot: {
      state: stateName,
      viewport: viewport.name,
      filename,
      sha256: createHash("sha256").update(buffer).digest("hex"),
      byte_length: buffer.length,
      ...pixels,
    },
    layout,
  };
}


/** 独占创建 screenshots 目录，避免新旧截图混用。 */
async function createScreenshotDirectory(artifactDirectory) {
  const target = path.join(artifactDirectory, "screenshots");
  try {
    await mkdir(target);
    const stat = lstatSync(target);
    requireCondition(stat.isDirectory() && !stat.isSymbolicLink(), "evidence.screenshots", "not_plain_directory");
  } catch (error) {
    // 已分类路径失败保留原码，其他文件系统异常不得暴露物理路径。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    fail("evidence.screenshots", "exists_or_unwritable");
  }
  return target;
}


/** 扫描 URL、DOM 和 Storage，确认密码、Token、request_id 与原始正文没有被持久化。 */
async function readPrivacyMetrics(page, publicIds = [], secretValues = []) {
  return page.evaluate(({ ids, secrets, authKeys, sentinel }) => {
    const html = document.documentElement.outerHTML;
    const url = location.href;
    const local = Object.entries(localStorage);
    const session = Object.entries(sessionStorage);
    const storageText = JSON.stringify({ local, session });
    const authToken = sessionStorage.getItem("metahub.auth_token") || "";
    return {
      local_storage_count: local.length,
      session_auth_slot_count: session.filter(([key]) => authKeys.includes(key)).length,
      forbidden_storage_key_count: [...local, ...session].filter(([key]) => !authKeys.includes(key) && /password|token|secret|prompt|response|payload|request[_-]?id/i.test(key)).length,
      public_id_url_or_storage_hits: ids.filter((value) => value && (url.includes(value) || storageText.includes(value))).length,
      secret_url_dom_storage_hits: secrets.filter((value) => value && (url.includes(value) || html.includes(value) || storageText.includes(value))).length,
      provider_body_hits: [url, html, storageText].filter((value) => value.includes(sentinel)).length,
      bearer_dom_hits: (html.match(/Bearer\s+[^"\s]{8,}/gi) ?? []).length,
      auth_token_exposed: Boolean(authToken) && (url.includes(authToken) || html.includes(authToken)),
    };
  }, { ids: publicIds, secrets: secretValues, authKeys: [...AUTH_STORAGE_KEYS], sentinel: PRIVATE_PROVIDER_SENTINEL });
}


/** 把隐私指标压缩为稳定零命中证据；正常 Session 只允许两个认证槽位。 */
function assertBrowserPrivacy(metrics, stage = "browser.privacy") {
  requireCondition(metrics.local_storage_count === 0, stage, "local_storage");
  requireCondition(metrics.session_auth_slot_count === AUTH_STORAGE_KEYS.size, stage, "auth_slots");
  requireCondition(metrics.forbidden_storage_key_count === 0, stage, "storage_key");
  requireCondition(metrics.public_id_url_or_storage_hits === 0, stage, "public_id_persisted");
  requireCondition(metrics.secret_url_dom_storage_hits === 0, stage, "secret_exposed");
  requireCondition(metrics.provider_body_hits === 0, stage, "provider_body");
  requireCondition(metrics.bearer_dom_hits === 0 && metrics.auth_token_exposed === false, stage, "token_exposed");
  // 证据只返回零命中结论，不返回任何被扫描的 URL、DOM 或 Storage 原文。
  return { url_dom_storage_sensitive_hits: 0, auth_storage_contract_valid: true };
}


/** 打开 PAGE-081 并按公开 error_map_id 等待本租户目标规则，避免用文本猜测页面成功。 */
async function openErrorMaps(page, runtime) {
  await openNavigation(page, "error-maps", ".error-maps-panel");
  await page.locator(`[data-error-map-row="${runtime.errorMapId}"]`).waitFor({ state: "visible", timeout: 30000 });
}


/** 验证移动端 dialog 的焦点陷阱、Escape 关闭和焦点恢复。 */
async function verifyMobileDialog(page, runtime) {
  const editButton = page.locator(`[data-error-map-edit="${runtime.errorMapId}"]`);
  await editButton.click();
  const dialog = page.locator(".error-map-dialog");
  await dialog.waitFor({ state: "visible", timeout: 30000 });
  const autofocusInside = await page.evaluate(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement) === true);
  requireCondition(autofocusInside, "browser.dialog", "initial_focus");
  for (let index = 0; index < 10; index += 1) await page.keyboard.press("Tab");
  const trapped = await page.evaluate(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement) === true);
  requireCondition(trapped, "browser.dialog", "focus_escape");
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached", timeout: 30000 });
  const restored = await page.evaluate((id) => document.activeElement?.getAttribute("data-error-map-edit") === id, runtime.errorMapId);
  requireCondition(restored, "browser.dialog", "focus_restore");
  return { initial_focus: true, focus_trapped: true, escape_closed: true, focus_restored: true };
}


/** 核验浏览器真实响应的 HTTP 状态、请求头 request_id 与统一 JSON 回传完全一致。 */
async function verifyBrowserResponseIdentity(response, { stage, status, code = null }) {
  let headers;
  let payload;
  try {
    headers = response.request().headers();
    payload = await response.json();
  } catch {
    fail(stage, "response_unreadable");
  }
  const requestId = headers?.["x-request-id"];
  requireCondition(response.status() === status, stage, "status");
  requireCondition(typeof requestId === "string" && SAFE_IDENTIFIER_PATTERN.test(requestId), stage, "request_id_header");
  requireCondition(payload && typeof payload === "object" && !Array.isArray(payload), stage, "payload");
  requireCondition(payload.request_id === requestId, stage, "request_id_mismatch");
  if (code !== null) requireCondition(payload.code === code, stage, "code");
  return requestId;
}

/** 制造确定性的 stale 409，并实证两个 PATCH、冲突 GET 和最终 PATCH 的 ID 两两不同。 */
async function exerciseStaleConflict(page, config, runtime) {
  const resourcePath = "/error-maps/" + runtime.errorMapId;
  await page.locator('[data-error-map-edit="' + runtime.errorMapId + '"]').click();
  const dialog = page.locator(".error-map-dialog");
  await dialog.waitFor({ state: "visible", timeout: 30000 });
  const currentVersion = parseCurrentErrorMapVersion(
    await dialog.getAttribute("data-error-map-version"),
    runtime.errorMapVersion,
  );
  const priority = dialog.locator('[name="priority"]');
  const conflictPriorities = planConflictPriorities(await priority.inputValue());
  await priority.fill(String(conflictPriorities.stalePriority));
  const directRequestId = "req_p56_browser_patch_b_" + randomBytes(6).toString("hex");
  const direct = await directControlCall(page, config, {
    method: "PATCH",
    pathname: resourcePath,
    body: { expected_version: currentVersion, priority: conflictPriorities.directPriority },
    requestId: directRequestId,
    traceId: directRequestId.replace("req_", "trc_"),
  });
  requireCondition(
    direct.status === 200
      && direct.requestId === directRequestId
      && direct.body?.changed === true
      && direct.body?.error_map?.error_map_id === runtime.errorMapId
      && direct.body?.error_map?.version === currentVersion + 1
      && direct.body?.error_map?.priority === conflictPriorities.directPriority,
    "browser.conflict",
    "parallel_patch",
  );

  const staleResponsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === resourcePath
        && response.request().method() === "PATCH"
        && response.status() === 409;
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  const conflictGetPromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/error-maps"
        && target.searchParams.get("error_map_id") === runtime.errorMapId
        && response.request().method() === "GET"
        && response.status() === 200;
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await dialog.locator('button[type="submit"]').click();
  const staleRequestId = await verifyBrowserResponseIdentity(
    await staleResponsePromise,
    { stage: "browser.conflict.stale", status: 409, code: "error_map.version_conflict" },
  );
  const conflictGetRequestId = await verifyBrowserResponseIdentity(
    await conflictGetPromise,
    { stage: "browser.conflict.refresh", status: 200 },
  );
  requireCondition(
    new Set([directRequestId, staleRequestId, conflictGetRequestId]).size === 3,
    "browser.conflict",
    "request_id_reused",
  );
  await page.locator("[data-error-map-adopt]").waitFor({ state: "visible", timeout: 30000 });
  const feedback = page.locator('.error-map-dialog-feedback[role="alert"]');
  const feedbackText = await feedback.textContent();
  requireCondition(
    typeof feedbackText === "string" && feedbackText.includes("request_id"),
    "browser.conflict",
    "request_id_missing",
  );
  return {
    parallel_patch_request_distinct: true,
    conflict_get_request_distinct: true,
    stale_409_rendered: true,
    adoptAndSave: async () => {
      await page.locator("[data-error-map-adopt]").click();
      const adoptedVersion = parseCurrentErrorMapVersion(
        await dialog.getAttribute("data-error-map-version"),
        currentVersion + 1,
      );
      requireCondition(
        adoptedVersion === currentVersion + 1
          && await priority.inputValue() === String(conflictPriorities.directPriority),
        "browser.conflict",
        "latest_not_adopted",
      );
      await priority.fill(String(conflictPriorities.finalPriority));
      const successPromise = page.waitForResponse((response) => {
        try {
          const target = new URL(response.url());
          return target.origin === config.controlOrigin
            && target.pathname === resourcePath
            && response.request().method() === "PATCH"
            && response.status() === 200;
        } catch {
          return false;
        }
      }, { timeout: 30000 });
      await dialog.locator('button[type="submit"]').click();
      const finalResponse = await successPromise;
      const finalRequestId = await verifyBrowserResponseIdentity(
        finalResponse,
        { stage: "browser.conflict.final", status: 200 },
      );
      let finalBody;
      try {
        finalBody = await finalResponse.json();
      } catch {
        fail("browser.conflict.final", "payload_unreadable");
      }
      requireCondition(
        finalBody?.changed === true
          && finalBody?.error_map?.error_map_id === runtime.errorMapId
          && finalBody?.error_map?.version === currentVersion + 2
          && finalBody?.error_map?.priority === conflictPriorities.finalPriority,
        "browser.conflict.final",
        "mutation_result_invalid",
      );
      requireCondition(
        new Set([directRequestId, staleRequestId, conflictGetRequestId, finalRequestId]).size === 4,
        "browser.conflict",
        "final_request_id_reused",
      );
      await dialog.waitFor({ state: "detached", timeout: 30000 });
      return {
        latest_adopted: true,
        new_request_succeeded: true,
        mutation_request_ids_distinct: true,
      };
    },
  };
}

/** 验证 developer 页面只读且后端直接 POST 仍返回 403。 */
async function verifyDeveloperReadOnly(page, config, runtime) {
  await openErrorMaps(page, runtime);
  requireCondition(await page.locator("#error-map-create,[data-error-map-edit],[data-error-map-status]").count() === 0, "browser.developer", "write_control_visible");
  const requestId = `req_p56_browser_developer_denied_${randomBytes(5).toString("hex")}`;
  const denied = await directControlCall(page, config, {
    method: "POST",
    pathname: "/error-maps",
    body: { tenant_id: "must-not-be-trusted" },
    requestId,
    traceId: requestId.replace("req_", "trc_"),
  });
  requireCondition(denied.status === 403 && denied.code === "auth.permission_denied" && denied.requestId === requestId, "browser.developer", "backend_rbac");
  return { write_controls_hidden: true, backend_post_denied: true };
}


/** 在 PAGE-082 打开一个 Runtime 指定请求，并验证其 frozen Fact owner。 */
async function openRequestLogFact(page, config, runtime, target) {
  await openNavigation(page, "request-logs", ".request-logs-panel");
  const form = page.locator("#request-log-filter-form");
  await form.locator('[name="requestId"]').fill(target.requestId);
  const listResponse = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin && target.pathname === "/request-logs" && response.request().method() === "GET";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await form.locator('button[type="submit"]').click();
  await verifyBrowserResponseIdentity(
    await listResponse,
    { stage: "browser.request_log.list." + target.key, status: 200 },
  );
  const detailButton = page.locator(`[data-request-log-detail="${target.requestId}"]`);
  await detailButton.waitFor({ state: "visible", timeout: 30000 });
  const detailResponse = page.waitForResponse((response) => {
    try {
      const destination = new URL(response.url());
      return destination.origin === config.controlOrigin
        && destination.pathname === `/request-logs/${target.requestId}`
        && response.request().method() === "GET";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await detailButton.click();
  await verifyBrowserResponseIdentity(
    await detailResponse,
    { stage: "browser.request_log.detail." + target.key, status: 200 },
  );
  const fact = page.locator(`.provider-error-fact[data-provider-error-fact-owner="${target.owner}"]`);
  await fact.waitFor({ state: "visible", timeout: 30000 });
  // 只有 tenant rule Fact 应展示可追溯跳转，builtin/guard Fact 不得伪造规则链接。
  if (target.tenantRuleJump) {
    const jump = fact.locator(`[data-provider-error-map-jump="${runtime.errorMapId}"]`);
    requireCondition(await jump.count() === 1, "browser.request_log", "tenant_rule_jump");
  }
  requireCondition(!String(await fact.textContent()).includes(PRIVATE_PROVIDER_SENTINEL), "browser.request_log", "provider_body");
  return {
    owner: target.owner,
    fact_visible: true,
    tenant_rule_jump_visible: target.tenantRuleJump,
    raw_provider_body_absent: true,
  };
}


/** 验证 viewer 没有导航、DOM 或预取，同时后端直接 GET 仍返回 403。 */
async function verifyViewerNoAccess(page, config, tracker) {
  const before = tracker.error_map_get_count + tracker.error_map_mutation_count;
  requireCondition(await page.locator('[data-console-nav="error-maps"]').count() === 0, "browser.viewer", "navigation_visible");
  await page.evaluate(() => { location.hash = "error-maps"; });
  await page.waitForTimeout(800);
  requireCondition(await page.locator(".error-maps-panel,[data-error-map-row]").count() === 0, "browser.viewer", "dom_visible");
  const after = tracker.error_map_get_count + tracker.error_map_mutation_count;
  requireCondition(after === before, "browser.viewer", "prefetch");
  const requestId = `req_p56_browser_viewer_denied_${randomBytes(5).toString("hex")}`;
  const denied = await directControlCall(page, config, {
    method: "GET",
    pathname: "/error-maps",
    requestId,
    traceId: requestId.replace("req_", "trc_"),
  });
  requireCondition(
    denied.status === 403 && denied.code === "auth.permission_denied" && denied.requestId === requestId,
    "browser.viewer",
    "backend_rbac",
  );
  return { navigation_hidden: true, dom_absent: true, prefetch_count: 0, backend_get_denied: true };
}


/** 独占发布 Browser JSON，同一批次重跑不得覆盖既有事实。 */
async function publishEvidence(directory, evidence) {
  const target = path.join(directory, "browser-acceptance.json");
  try {
    await writeFile(target, JSON.stringify(evidence, null, 2) + "\n", { encoding: "utf8", flag: "wx" });
  } catch {
    fail("evidence.publish", "write_failed");
  }
  return target;
}


/** 执行六状态截图、三角色 RBAC、stale CAS、日志 Fact、隐私和 Session 回收。 */
async function executeBrowserAcceptance(config) {
  const runtimeEvidence = await loadRuntimeEvidence(config.artifactDirectory);
  const screenshotDirectory = await createScreenshotDirectory(config.artifactDirectory);
  let chromium;
  try {
    ({ chromium } = require("playwright"));
  } catch {
    fail("browser.playwright", "module_missing");
  }
  const launchOptions = { headless: !config.headed };
  if (config.chromePath) launchOptions.executablePath = config.chromePath;
  const browser = await runSafeBrowserOperation("browser.launch", "failed", () => chromium.launch(launchOptions));
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, locale: "zh-CN" });
  const page = await context.newPage();
  const tracker = await installBrowserGuards(context, page, config);
  const ledger = createTemporaryAccountLedger(config);
  const screenshots = [];
  const layouts = {};
  const states = {};
  let credentials = [];
  let cleanup = null;
  let authenticated = false;
  try {
    await loginThroughConsole(page, config, config);
    authenticated = true;
    credentials = await createRestrictedRoleUsers(page, config, ledger);
    await openErrorMaps(page, runtimeEvidence.runtime);
    let capture = await captureState(page, screenshotDirectory, "admin-list", VIEWPORTS.desktop, ".error-maps-panel");
    screenshots.push(capture.screenshot);
    layouts.admin_list = capture.layout;

    await page.setViewportSize({ width: VIEWPORTS.mobile.width, height: VIEWPORTS.mobile.height });
    await page.locator(`[data-error-map-edit="${runtimeEvidence.runtime.errorMapId}"]`).click();
    await page.locator(".error-map-dialog").waitFor({ state: "visible", timeout: 30000 });
    capture = await captureState(page, screenshotDirectory, "admin-mobile-edit", VIEWPORTS.mobile, ".error-map-dialog");
    screenshots.push(capture.screenshot);
    layouts.admin_mobile_edit = capture.layout;
    await page.keyboard.press("Escape");
    await page.locator(".error-map-dialog").waitFor({ state: "detached", timeout: 30000 });
    states.mobile_dialog = await verifyMobileDialog(page, runtimeEvidence.runtime);

    await page.setViewportSize({ width: VIEWPORTS.desktop.width, height: VIEWPORTS.desktop.height });
    const conflict = await exerciseStaleConflict(page, config, runtimeEvidence.runtime);
    capture = await captureState(page, screenshotDirectory, "stale-conflict", VIEWPORTS.desktop, ".error-map-dialog");
    screenshots.push(capture.screenshot);
    layouts.stale_conflict = capture.layout;
    states.stale_conflict = {
      parallel_patch_request_distinct: conflict.parallel_patch_request_distinct,
      conflict_get_request_distinct: conflict.conflict_get_request_distinct,
      stale_409_rendered: conflict.stale_409_rendered,
      ...(await conflict.adoptAndSave()),
    };
    states.admin_privacy = assertBrowserPrivacy(
      await readPrivacyMetrics(page, runtimePublicIdentifiers(runtimeEvidence.runtime), [config.password, ...credentials.map((item) => item.password)]),
      "browser.privacy.admin",
    );
    states.admin_logout = await logoutAndVerify(page);
    authenticated = false;

    const developer = credentials.find((item) => item.role === "developer");
    requireCondition(Boolean(developer), "browser.developer", "credential_missing");
    await loginThroughConsole(page, config, developer);
    authenticated = true;
    states.developer = await verifyDeveloperReadOnly(page, config, runtimeEvidence.runtime);
    capture = await captureState(page, screenshotDirectory, "developer-readonly", VIEWPORTS.desktop, ".error-maps-panel");
    screenshots.push(capture.screenshot);
    layouts.developer_readonly = capture.layout;
    const logTargets = requestLogTargets(runtimeEvidence.runtime);
    states.request_log = {
      mapped: await openRequestLogFact(page, config, runtimeEvidence.runtime, logTargets[0]),
    };
    capture = await captureState(page, screenshotDirectory, "mapped-request-log", VIEWPORTS.desktop, ".provider-error-fact");
    screenshots.push(capture.screenshot);
    layouts.mapped_request_log = capture.layout;
    // 流式握手失败必须从同批 manifest 打开真实 stream_session Fact，不能只依赖 Runtime API 断言。
    states.request_log.stream = await openRequestLogFact(
      page,
      config,
      runtimeEvidence.runtime,
      logTargets[1],
    );
    states.developer_privacy = assertBrowserPrivacy(
      await readPrivacyMetrics(page, runtimePublicIdentifiers(runtimeEvidence.runtime), credentials.map((item) => item.password)),
      "browser.privacy.developer",
    );
    states.developer_logout = await logoutAndVerify(page);
    authenticated = false;

    const viewer = credentials.find((item) => item.role === "viewer");
    requireCondition(Boolean(viewer), "browser.viewer", "credential_missing");
    await loginThroughConsole(page, config, viewer);
    authenticated = true;
    states.viewer = await verifyViewerNoAccess(page, config, tracker);
    capture = await captureState(page, screenshotDirectory, "viewer-no-navigation", VIEWPORTS.desktop, ".console-main");
    screenshots.push(capture.screenshot);
    layouts.viewer_no_navigation = capture.layout;
    states.viewer_privacy = assertBrowserPrivacy(
      await readPrivacyMetrics(page, runtimePublicIdentifiers(runtimeEvidence.runtime), credentials.map((item) => item.password)),
      "browser.privacy.viewer",
    );
    states.viewer_logout = await logoutAndVerify(page);
    authenticated = false;

    cleanup = disableTemporaryUsersAndSessions(config, ledger, { requireAll: true });
    requireCondition(screenshots.length === 6 && screenshots.every((item) => item.non_blank_pixels === true), "browser.screenshots", "count_or_blank");
    requireCondition(tracker.blocked_request_count === 0, "browser.network", "blocked");
    requireCondition(tracker.browser_error_count === 0, "browser.errors", "page_error");
    requireCondition(tracker.console_error_count === 0, "browser.errors", "console_error");
    // stale 409、developer 403 和 viewer 403 各产生一条 Chromium 网络 console error，数量漂移同样失败。
    requireCondition(
      tracker.expected_http_console_error_count === 3,
      "browser.errors",
      "expected_http_error_count",
    );
    requireCondition(states.viewer.prefetch_count === 0, "browser.viewer", "prefetch_count");

    const evidence = {
      schema_version: 1,
      prd_id: "PRD-056",
      status: "passed",
      runtime_evidence_sha256: runtimeEvidence.sha256,
      generated_resources: {
        error_map_id: runtimeEvidence.runtime.errorMapId,
        error_map_version: runtimeEvidence.runtime.errorMapVersion,
        head_revision: runtimeEvidence.runtime.headRevision,
        mapped_request_id: runtimeEvidence.runtime.mappedRequestId,
        nonstream_request_id: runtimeEvidence.runtime.nonstreamRequestId,
        stream_request_id: runtimeEvidence.runtime.streamRequestId,
      },
      checks: {
        states,
        layouts,
        screenshots: { count: screenshots.length, all_non_blank: true },
        network: {
          blocked_request_count: tracker.blocked_request_count,
          error_map_get_count: tracker.error_map_get_count,
          error_map_mutation_count: tracker.error_map_mutation_count,
          request_log_get_count: tracker.request_log_get_count,
          viewer_prefetch_count: states.viewer.prefetch_count,
        },
        errors: {
          browser_error_count: tracker.browser_error_count,
          console_error_count: tracker.console_error_count,
          expected_http_console_error_count: tracker.expected_http_console_error_count,
        },
        cleanup,
      },
      screenshots,
    };
    assertSafeEvidence(evidence, [config.password, ...credentials.flatMap((item) => [item.email, item.password])]);
    await publishEvidence(config.artifactDirectory, evidence);
    return evidence;
  } finally {
    // 页面流程中途失败也必须先尝试注销，数据库账本再承担最终失败关闭回收。
    if (authenticated) {
      try { await logoutAndVerify(page); } catch { /* 最终仍由数据库账本撤销。 */ }
    }
    await context.close().catch(() => undefined);
    await browser.close().catch(() => undefined);
    if (!cleanup && ledger.intents.length > 0) {
      try { cleanup = disableTemporaryUsersAndSessions(config, ledger); } catch { cleanup = null; }
    }
    credentials = credentials.map((item) => ({ ...item, email: "", password: "" }));
    ledger.intents = ledger.intents.map((item) => ({ ...item, email: "", displayName: "", userId: "", tenantId: "" }));
  }
}


/** CLI 入口只输出固定成功摘要或脱敏失败分类。 */
async function main() {
  try {
    const config = parseArguments();
    await executeBrowserAcceptance(config);
    process.stdout.write('{"prd_id":"PRD-056","status":"passed","evidence":"browser-acceptance.json"}\n');
    return 0;
  } catch (error) {
    const safe = toSafeFailure(error, "browser.unhandled", "unexpected");
    process.stderr.write(safe.message + "\n");
    return 1;
  }
}


export {
  BrowserAcceptanceFailure,
  VIEWPORTS,
  assertArtifactDirectorySafe,
  assertBrowserPrivacy,
  assertLoggedOutState,
  assertLayoutMetrics,
  assertSafeEvidence,
  browserRequestAllowed,
  classifyBrowserConsoleMessage,
  createScreenshotDirectory,
  createTemporaryAccountLedger,
  disableTemporaryUsersAndSessions,
  inspectPngPixels,
  normalizeLoopbackOrigin,
  parseCurrentErrorMapVersion,
  planConflictPriorities,
  publishEvidence,
  readEnvironmentIdentity,
  requestLogTargets,
  resolveArtifactDirectory,
  runtimePublicIdentifiers,
  screenshotFilename,
  validateRuntimeEvidence,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
