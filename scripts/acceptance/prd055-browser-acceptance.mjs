/**
 * PRD-055 PAGE-080 调用日志高级查询真实浏览器验收。
 *
 * 脚本只访问调用方已启动的三个 HTTP 回环服务，依赖同批次 Runtime 证据，并把截图与
 * Browser JSON 独占写入 artifacts/prd055/<batch>。密码、Token 和筛选值不得进入证据。
 */

import { createHash, randomBytes } from "node:crypto";
import { existsSync, lstatSync, realpathSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { inflateSync } from "node:zlib";


// Playwright 只从调用方准备的 NODE_PATH 解析；验收器绝不下载浏览器或安装依赖。
const require = createRequire(import.meta.url);
// 两档视口都覆盖成功、空结果与错误，避免只验收桌面主流程。
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
// 网络、证据批次和公开 ID 均使用显式白名单，阻止访问外网或越出项目目录。
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const BATCH_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$/;
const SAFE_IDENTIFIER_PATTERN = /^[A-Za-z0-9._:-]{1,160}$/;
const QUERY_EVENT_PATTERN = /^lqe_[0-9a-f]{32}$/;
const SAVED_FILTER_PATTERN = /^sf_[0-9a-f]{32}$/;
const AUTH_STORAGE_KEYS = new Set(["metahub.auth_token", "metahub.auth_context"]);

// URL query 禁止承载身份和正文；filters 是本 PRD 定义的有界 Base64URL 合同。
const SENSITIVE_URL_KEY_PATTERN = /password|token|secret|authorization|prompt|response|provider[_-]?key|api[_-]?key|payload|tenant[_-]?id|owner[_-]?id/i;
// 最终证据按规范化字段名拒绝凭据、正文和远端投递细节。
const FORBIDDEN_EVIDENCE_KEYS = new Set([
  "authorization", "credential_id", "encrypted_secret", "endpoint_url", "error_summary",
  "key_hash", "key_plaintext", "key_secret", "key_value", "messages", "password",
  "payload", "payload_json", "prompt", "provider_key", "raw_usage", "response",
  "response_body", "secret", "token",
]);
// 凭据形态正则补足字段名门禁，防止 Token 明文藏在普通字符串值中。
const FORBIDDEN_VALUE_PATTERN = /Bearer\s+[^\s"']{8,}|\bmhv_[A-Za-z0-9_-]{16,}\b|\bsk[-_][A-Za-z0-9_-]{12,}\b/i;


class BrowserAcceptanceFailure extends Error {
  /** 保存固定失败分类；调用点不得传入 URL、DOM、Token 或响应正文。 */
  constructor(stage, code) {
    super("prd055_browser_acceptance_failed stage=" + stage + " code=" + code);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 以固定 stage/code 终止验收，不拼接任何不可信值。 */
function fail(stage, code) {
  throw new BrowserAcceptanceFailure(stage, code);
}


/** 执行失败关闭断言，失败消息只来自代码内冻结分类。 */
function requireCondition(condition, stage, code) {
  if (!condition) fail(stage, code);
}


/** 把未知异常折叠为固定分类，避免 Playwright 把 URL 或 DOM 带到终端。 */
function toSafeFailure(error, stage, code = "stage_failed") {
  if (error instanceof BrowserAcceptanceFailure) return error;
  return new BrowserAcceptanceFailure(stage, code);
}


/** 包装一次浏览器操作，所有底层异常都收敛为固定安全错误。 */
async function runSafeBrowserOperation(stage, code, operation) {
  try {
    return await operation();
  } catch (error) {
    // Playwright 异常可能携带 URL 或 DOM，必须折叠为固定公开分类。
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
  requireCondition(!target.username && !target.password, "config." + label, "url_credentials");
  requireCondition(target.pathname === "/", "config." + label, "url_path");
  requireCondition(!target.search && !target.hash, "config." + label, "url_suffix");
  requireCondition(Boolean(target.port), "config." + label, "port_missing");
  return target.origin;
}


/** 从脚本物理位置恢复唯一 MetaHub 根，不接受环境变量扩大信任边界。 */
function workspaceRoot() {
  const resolved = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  requireCondition(path.basename(resolved).toLowerCase() === "metahub", "config.workspace", "unexpected_root");
  return resolved;
}


/** 把证据目录硬限制为 artifacts/prd055 下一个非链接安全批次目录。 */
function resolveArtifactDirectory(value, root = workspaceRoot()) {
  const artifactRoot = path.resolve(root, "artifacts", "prd055");
  const candidate = path.resolve(root, String(value));
  const relative = path.relative(artifactRoot, candidate);
  requireCondition(relative && !relative.startsWith("..") && !path.isAbsolute(relative), "config.artifact", "outside_root");
  requireCondition(!relative.includes(path.sep), "config.artifact", "batch_depth");
  requireCondition(BATCH_NAME_PATTERN.test(relative), "config.artifact", "batch_name");
  return candidate;
}


/** 核对批次目录与 Runtime JSON 的真实路径，拒绝目录链接、文件链接和边界跳转。 */
function assertArtifactDirectorySafe(candidate) {
  const normalized = path.resolve(candidate);
  try {
    const directoryStat = lstatSync(normalized);
    requireCondition(directoryStat.isDirectory(), "config.artifact", "not_directory");
    requireCondition(!directoryStat.isSymbolicLink(), "config.artifact", "directory_symlink");
    const realDirectory = path.resolve(realpathSync(normalized));
    const sameDirectory = process.platform === "win32"
      ? realDirectory.toLowerCase() === normalized.toLowerCase()
      : realDirectory === normalized;
    requireCondition(sameDirectory, "config.artifact", "directory_realpath");

    const runtimeTarget = path.join(normalized, "runtime-acceptance.json");
    const runtimeStat = lstatSync(runtimeTarget);
    requireCondition(runtimeStat.isFile(), "config.artifact", "runtime_not_file");
    requireCondition(!runtimeStat.isSymbolicLink(), "config.artifact", "runtime_symlink");
    const runtimeReal = path.resolve(realpathSync(runtimeTarget));
    const runtimeRelative = path.relative(normalized, runtimeReal);
    requireCondition(
      runtimeRelative === "runtime-acceptance.json",
      "config.artifact",
      "runtime_realpath",
    );
  } catch (error) {
    // 文件系统异常可能携带主机路径，除既有安全分类外只能转换为固定缺失错误。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    fail("config.artifact", "missing_or_unreadable");
  }
  // 返回值仍是已经规范化并验证真实路径的批次目录，调用方不得重新解析环境输入。
  return normalized;
}


/** 固定解析同批次 runtime.db，拒绝链接、目录和 realpath 边界跳转。 */
function resolveRuntimeDatabasePath(artifactDirectory) {
  const directory = path.resolve(artifactDirectory);
  const target = path.join(directory, "runtime.db");
  try {
    const stat = lstatSync(target);
    requireCondition(stat.isFile() && !stat.isSymbolicLink(), "config.database", "not_plain_file");
    const realTarget = path.resolve(realpathSync(target));
    const relative = path.relative(directory, realTarget);
    requireCondition(relative === "runtime.db", "config.database", "realpath");
  } catch (error) {
    // 主机文件异常可能携带绝对路径，只保留固定安全分类。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    fail("config.database", "missing_or_unreadable");
  }
  return target;
}


/** 从环境读取管理员身份，并在启动 Chromium 前删除全部密码别名。 */
function readEnvironmentIdentity(env = process.env) {
  const adminPassword = String(env.METAHUB_PRD055_ADMIN_PASSWORD || env.METAHUB_ADMIN_PASSWORD || "");
  const tenantSlug = String(env.METAHUB_PRD055_TENANT_SLUG || env.METAHUB_TENANT_SLUG || "default").trim();
  const adminEmail = String(env.METAHUB_PRD055_ADMIN_EMAIL || env.METAHUB_ADMIN_EMAIL || "admin@metahub.local").trim().toLowerCase();
  for (const name of [
    "METAHUB_PRD055_ADMIN_PASSWORD", "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD055_ADMIN_EMAIL", "METAHUB_ADMIN_EMAIL",
    "METAHUB_PRD055_TENANT_SLUG", "METAHUB_TENANT_SLUG",
  ]) delete env[name];
  requireCondition(adminPassword.length >= 8, "config.identity", "admin_password_missing");
  requireCondition(Boolean(tenantSlug), "config.identity", "tenant_slug_missing");
  requireCondition(adminEmail.includes("@"), "config.identity", "admin_email_invalid");
  // 身份材料只供当前浏览器进程使用，返回后仍禁止写入最终证据。
  return { tenantSlug, adminEmail, adminPassword };
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
  const artifactValue = values.get("--artifact-dir") || env.METAHUB_PRD055_ARTIFACT_DIR || "";
  requireCondition(Boolean(artifactValue), "config.artifact", "missing");
  const artifactDirectory = assertArtifactDirectorySafe(
    resolveArtifactDirectory(artifactValue),
  );
  const consoleOrigin = normalizeLoopbackOrigin(values.get("--console-url") || env.METAHUB_PRD055_CONSOLE_URL || "http://localhost:5173", "console");
  const controlOrigin = normalizeLoopbackOrigin(values.get("--control-url") || env.METAHUB_PRD055_CONTROL_URL || "http://localhost:8001", "control");
  const gatewayOrigin = normalizeLoopbackOrigin(values.get("--gateway-url") || env.METAHUB_PRD055_GATEWAY_URL || "http://localhost:8000", "gateway");
  requireCondition(new Set([consoleOrigin, controlOrigin, gatewayOrigin]).size === 3, "config.origins", "must_be_distinct");
  const chromePath = values.get("--chrome-path") || env.METAHUB_PLAYWRIGHT_CHROME_PATH || "";
  if (chromePath) requireCondition(path.isAbsolute(chromePath) && existsSync(chromePath), "config.chrome", "path_invalid");
  return {
    ...identity,
    artifactDirectory,
    databasePath: resolveRuntimeDatabasePath(artifactDirectory),
    consoleOrigin,
    controlOrigin,
    gatewayOrigin,
    chromePath,
    headed,
  };
}


/** 规范化证据字段名，阻断 camelCase 和连字符绕过敏感字段白名单。 */
function normalizeEvidenceKey(key) {
  return String(key)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}


/** 对最终证据执行字段、凭据形态和本轮明文三重隐私门禁。 */
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
  for (const sensitive of knownSensitive) {
    if (sensitive) requireCondition(!encoded.includes(sensitive), stage, "known_sensitive_value");
  }
  return { evidence_privacy_verified: true };
}


/** 校验并提取 Runtime 证据中 Browser 唯一需要的公开夹具 ID。 */
function validateRuntimeEvidence(payload) {
  requireCondition(payload && typeof payload === "object" && !Array.isArray(payload), "runtime_evidence", "not_object");
  requireCondition(payload.prd_id === "PRD-055" && payload.status === "passed", "runtime_evidence", "status");
  const sharedRequestId = payload.generated_resources?.shared_request_id;
  requireCondition(typeof sharedRequestId === "string" && SAFE_IDENTIFIER_PATTERN.test(sharedRequestId), "runtime_evidence", "shared_request_id");
  requireCondition(payload.generated_resources?.performance_rows >= 100000, "runtime_evidence", "performance_rows");
  requireCondition(payload.checks?.performance?.thresholds_passed === true, "runtime_evidence", "performance");
  requireCondition(payload.checks?.queries?.cross_tenant_same_id_isolated === true, "runtime_evidence", "tenant_isolation");
  requireCondition(payload.checks?.cleanup?.session_revoked === true, "runtime_evidence", "cleanup");
  assertSafeEvidence(payload);
  return { sharedRequestId };
}


/** 读取同批次 Runtime JSON，并冻结其 SHA-256 供 Browser 证据关联。 */
async function loadRuntimeEvidence(artifactDirectory) {
  const target = path.join(artifactDirectory, "runtime-acceptance.json");
  requireCondition(existsSync(target), "runtime_evidence.load", "missing");
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


/** 判断浏览器请求是否位于三服务白名单，且 URL query 不携带敏感键。 */
function browserRequestAllowed(rawUrl, allowedOrigins) {
  let target;
  try {
    target = new URL(String(rawUrl));
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


/** 安装网络、页面异常和控制台错误门禁，并统计 RequestLog/Explain 请求。 */
async function installBrowserGuards(context, page, config) {
  const allowedOrigins = new Set([config.consoleOrigin, config.controlOrigin, config.gatewayOrigin]);
  const expectedHttpStatuses = [];
  const summary = {
    blocked_request_count: 0,
    browser_error_count: 0,
    console_error_count: 0,
    expected_http_console_error_count: 0,
    request_log_get_count: 0,
    saved_filter_request_count: 0,
    request_explain_get_count: 0,
    expectHttpStatus(status) { expectedHttpStatuses.push(status); },
  };
  await context.route("**/*", async (route) => {
    const request = route.request();
    if (!browserRequestAllowed(request.url(), allowedOrigins)) {
      summary.blocked_request_count += 1;
      await route.abort("blockedbyclient");
      return;
    }
    try {
      const target = new URL(request.url());
      if (target.origin === config.controlOrigin && target.pathname === "/request-logs" && request.method() === "GET") {
        summary.request_log_get_count += 1;
      }
      if (
        target.origin === config.controlOrigin
          && target.pathname.startsWith("/request-logs/saved-filters")
      ) {
        summary.saved_filter_request_count += 1;
      }
      if (target.origin === config.controlOrigin && target.pathname.startsWith("/request-explain/") && request.method() === "GET") {
        summary.request_explain_get_count += 1;
      }
    } catch {
      summary.blocked_request_count += 1;
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
  page.on("pageerror", () => { summary.browser_error_count += 1; });
  page.on("console", (message) => {
    // 非错误级别日志不属于验收失败，且不得读取其可能携带的业务文本。
    if (message.type() !== "error") return;
    const text = String(message.text());
    const found = expectedHttpStatuses.findIndex((status) => text.startsWith("Failed to load resource: the server responded with a status of " + status));
    if (found >= 0) {
      expectedHttpStatuses.splice(found, 1);
      summary.expected_http_console_error_count += 1;
      return;
    }
    summary.console_error_count += 1;
    summary.browser_error_count += 1;
  });
  return summary;
}


/** 使用真实 Console 表单登录指定角色，Token 仅由页面认证模块写入 sessionStorage。 */
async function loginThroughConsole(page, config, identity) {
  await runSafeBrowserOperation("browser.login." + identity.role, "navigation", () => page.goto(config.consoleOrigin + "/", { waitUntil: "domcontentloaded", timeout: 30000 }));
  const form = page.locator("#login-form");
  await form.waitFor({ state: "visible", timeout: 30000 });
  await form.locator('[name="tenantSlug"]').fill(config.tenantSlug);
  await form.locator('[name="email"]').fill(identity.email);
  await form.locator('[name="password"]').fill(identity.password);
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      // 登录响应必须同时匹配 Control origin、固定路径和 POST，防止旁路响应抢占。
      return target.origin === config.controlOrigin && target.pathname === "/auth/login" && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await form.locator('button[type="submit"]').click();
  const response = await responsePromise;
  requireCondition(response.status() === 200, "browser.login." + identity.role, "status");
  await page.locator("#logout-button").waitFor({ state: "visible", timeout: 45000 });
  const navCount = await page.locator('[data-console-nav="request-logs"]').count();
  requireCondition(navCount === (identity.role === "viewer" ? 0 : 1), "browser.login." + identity.role, "navigation_visibility");
  return { role: identity.role, navigation_visible: navCount === 1 };
}


/** 读取响应 JSON 并限制为普通对象；解析失败时不得把响应正文带入错误。 */
async function readJsonResponse(response, stage) {
  try {
    const payload = await response.json();
    requireCondition(payload && typeof payload === "object" && !Array.isArray(payload), stage, "not_object");
    return payload;
  } catch (error) {
    // 响应正文可能含内部信息，解析失败只允许固定安全分类离开函数。
    throw toSafeFailure(error, stage, "invalid_json");
  }
}


/** 核对 RequestLog 成功响应的分页、QueryEvent、租户归属和最小披露合同。 */
function validateRequestLogList(payload, { expectedRequestId = null, expectedEmpty = false } = {}) {
  requireCondition(payload && typeof payload === "object" && !Array.isArray(payload), "browser.dto.list", "not_object");
  assertSafeEvidence(payload);
  requireCondition(Array.isArray(payload.request_logs), "browser.dto.list", "logs_not_array");
  for (const key of ["total", "limit", "offset"]) {
    requireCondition(Number.isSafeInteger(payload[key]) && payload[key] >= 0, "browser.dto.list", key + "_invalid");
  }
  requireCondition(typeof payload.has_more === "boolean", "browser.dto.list", "has_more_invalid");
  requireCondition(QUERY_EVENT_PATTERN.test(String(payload.query_event_id || "")), "browser.dto.list", "query_event_id");
  if (expectedEmpty) {
    requireCondition(payload.total === 0 && payload.request_logs.length === 0, "browser.dto.list", "not_empty");
  }
  if (expectedRequestId) {
    requireCondition(
      payload.request_logs.length === 1 && payload.request_logs[0]?.request_id === expectedRequestId,
      "browser.dto.list",
      "request_id_mismatch",
    );
  }
  requireCondition(
    payload.request_logs.every((item) => item && typeof item === "object" && !Array.isArray(item)),
    "browser.dto.list",
    "log_not_object",
  );
  return {
    returned_count: payload.request_logs.length,
    total: payload.total,
    limit: payload.limit,
    offset: payload.offset,
    query_event_id_valid: true,
  };
}


/** 对 Runtime 证据做完整前置门禁，防止 Browser 在迁移、RBAC 或性能未通过时单独宣称成功。 */
function validateRuntimeEvidenceStrict(payload) {
  const runtime = validateRuntimeEvidence(payload);
  requireCondition(payload.schema_version === 1, "runtime_evidence", "schema_version");
  const checks = payload.checks;
  requireCondition(checks?.database?.migration_0045_applied === true, "runtime_evidence.database", "migration");
  requireCondition(checks?.database?.required_indexes_present === true, "runtime_evidence.database", "indexes");
  requireCondition(checks?.database?.required_triggers_present === true, "runtime_evidence.database", "triggers");
  requireCondition(checks?.database?.integrity_verified === true, "runtime_evidence.database", "integrity");
  requireCondition(
    checks?.services?.gateway_http_200 === true
      && checks?.services?.control_http_200 === true
      && checks?.services?.console_http_200 === true,
    "runtime_evidence.services",
    "not_ready",
  );
  requireCondition(
    checks?.roles?.developer_session_bound === true && checks?.roles?.viewer_session_bound === true,
    "runtime_evidence.roles",
    "missing",
  );
  requireCondition(
    checks?.queries?.inline_v1_accurate === true
      && checks?.queries?.mixed_sources_rejected === true
      && checks?.queries?.pagination_bounded === true
      && checks?.queries?.cross_tenant_same_id_isolated === true,
    "runtime_evidence.queries",
    "incomplete",
  );
  requireCondition(
    checks?.saved_filters?.create_list_apply_update_delete === true
      && checks?.saved_filters?.owner_isolation_same_tenant === true
      && checks?.saved_filters?.tenant_isolation === true
      && checks?.saved_filters?.optimistic_lock === true
      && checks?.saved_filters?.soft_delete === true,
    "runtime_evidence.saved_filters",
    "incomplete",
  );
  requireCondition(
    checks?.query_plans?.explain_query_plan_verified === true
      && checks?.query_plans?.full_table_scan_rejected === true,
    "runtime_evidence.query_plans",
    "incomplete",
  );
  requireCondition(
    checks?.events?.filter_values_absent === true
      && checks?.events?.source_outbox_dispatched === true
      && checks?.events?.immutable_facts_verified === true,
    "runtime_evidence.events",
    "incomplete",
  );
  requireCondition(
    checks?.final_database?.integrity_check === "ok"
      && checks?.final_database?.foreign_key_check === "ok",
    "runtime_evidence.final_database",
    "incomplete",
  );
  return runtime;
}


/** 从 Runtime JSON 读取严格投影并冻结 SHA-256，Browser 证据只引用同一批次事实。 */
async function loadStrictRuntimeEvidence(artifactDirectory) {
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
    runtime: validateRuntimeEvidenceStrict(payload),
    sha256: createHash("sha256").update(raw).digest("hex"),
  };
}


/** 建立一次性账号账本；意图先于 HTTP 写入登记，覆盖提交成功但响应丢失的清理场景。 */
function createTemporaryAccountLedger(config, nowMillis = Date.now()) {
  requireCondition(Number.isFinite(nowMillis), "browser.users.ledger", "clock");
  return {
    tenantSlug: config.tenantSlug,
    // SQLite 时间精度和进程调度存在微小偏差，清理门禁只放宽固定两秒。
    createdAfter: new Date(nowMillis - 2000).toISOString(),
    intents: [],
  };
}


/** 在管理员真实页面内创建一次性 developer/viewer，密码只驻留当前进程内存。 */
async function createRestrictedRoleUsers(page, config, ledger) {
  requireCondition(ledger?.tenantSlug === config.tenantSlug, "browser.users.ledger", "tenant");
  const credentials = [];
  for (const role of ["developer", "viewer"]) {
    const suffix = randomBytes(6).toString("hex");
    const email = "prd055-browser-" + role + "-" + suffix + "@metahub.local";
    const displayName = "PRD055 Browser " + role + " " + suffix;
    const password = "Prd055Browser!" + randomBytes(18).toString("hex");
    const requestId = "req_prd055_browser_create_" + role + "_" + randomBytes(5).toString("hex");
    const traceId = "trc_prd055_browser_create_" + role + "_" + randomBytes(5).toString("hex");
    const intent = { role, email, displayName, userId: "", tenantId: "" };
    // 必须在发送 POST 前登记；即使浏览器在提交后断开，finally 仍能按随机邮箱回收。
    ledger.intents.push(intent);
    const result = await runSafeBrowserOperation("browser.users." + role, "request_failed", () => page.evaluate(async (input) => {
      const access = sessionStorage.getItem("metahub.auth_token");
      // 缺少页面登录令牌时立即失败关闭，不能匿名创建验收角色。
      if (!access) return { status: 0, role: "", requestId: "", traceId: "" };
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
        userId: body.user?.user_id ?? "",
        tenantId: body.user?.tenant_id ?? "",
        email: body.user?.email ?? "",
        displayName: body.user?.display_name ?? "",
        role: body.user?.role ?? "",
        userStatus: body.user?.status ?? "",
        requestId: body.request_id ?? response.headers.get("X-Request-ID") ?? "",
        traceId: body.trace_id ?? response.headers.get("X-Trace-ID") ?? "",
      };
    }, { controlOrigin: config.controlOrigin, role, email, displayName, password, requestId, traceId }));
    requireCondition(
      result.status === 201
        && result.role === role
        && result.email === email
        && result.displayName === displayName
        && result.userStatus === "active",
      "browser.users." + role,
      "create_status",
    );
    requireCondition(SAFE_IDENTIFIER_PATTERN.test(result.userId), "browser.users." + role, "user_id");
    requireCondition(SAFE_IDENTIFIER_PATTERN.test(result.tenantId), "browser.users." + role, "tenant_id");
    requireCondition(SAFE_IDENTIFIER_PATTERN.test(result.requestId), "browser.users." + role, "request_id");
    requireCondition(SAFE_IDENTIFIER_PATTERN.test(result.traceId), "browser.users." + role, "trace_id");
    intent.userId = result.userId;
    intent.tenantId = result.tenantId;
    credentials.push({ role, email, password });
  }
  return credentials;
}


/**
 * 撤销本批次临时账号的全部 Session 并停用账号。
 *
 * 业务 API 故意不提供用户删除；测试清理只能在已验证的同批次 runtime.db 内按随机邮箱、
 * 创建时间、角色和展示名四重匹配，保留 QueryEvent/SavedFilter 外键证据。
 */
function disableTemporaryUsersAndSessions(config, ledger, { requireAll = false } = {}) {
  requireCondition(
    ledger?.tenantSlug === config.tenantSlug && Array.isArray(ledger.intents),
    "browser.users.cleanup",
    "ledger",
  );
  let database;
  let transactionOpen = false;
  try {
    const { DatabaseSync } = require("node:sqlite");
    database = new DatabaseSync(config.databasePath);
    database.exec("PRAGMA foreign_keys=ON; PRAGMA busy_timeout=10000; BEGIN IMMEDIATE");
    transactionOpen = true;
    const selectUser = database.prepare(`
      SELECT u.user_id, u.tenant_id, u.email, u.display_name, u.role, u.status, u.created_at
      FROM users AS u
      JOIN tenants AS t ON t.tenant_id=u.tenant_id
      WHERE t.slug=? AND u.email=?
    `);
    const revokeSessions = database.prepare(`
      UPDATE sessions
      SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
      WHERE tenant_id=? AND user_id=? AND status<>'revoked'
    `);
    const disableUser = database.prepare(`
      UPDATE users SET status='disabled', updated_at=?
      WHERE tenant_id=? AND user_id=? AND status<>'disabled'
    `);
    const countSessions = database.prepare(`
      SELECT COUNT(*) AS count FROM sessions
      WHERE tenant_id=? AND user_id=? AND status<>'revoked'
    `);
    let matchedUsers = 0;
    let disabledUsers = 0;
    let revokedSessions = 0;
    const now = new Date().toISOString();
    for (const intent of ledger.intents) {
      requireCondition(
        ["developer", "viewer"].includes(intent.role)
          && intent.email.startsWith("prd055-browser-" + intent.role + "-")
          && intent.email.endsWith("@metahub.local"),
        "browser.users.cleanup",
        "intent",
      );
      const row = selectUser.get(ledger.tenantSlug, intent.email);
      if (!row) continue;
      requireCondition(
        row.role === intent.role
          && row.display_name === intent.displayName
          && row.email === intent.email
          && ["active", "disabled"].includes(row.status)
          && Number.isFinite(Date.parse(row.created_at))
          && Date.parse(row.created_at) >= Date.parse(ledger.createdAfter)
          && (!intent.userId || row.user_id === intent.userId)
          && (!intent.tenantId || row.tenant_id === intent.tenantId),
        "browser.users.cleanup",
        "owner_mismatch",
      );
      matchedUsers += 1;
      revokedSessions += Number(revokeSessions.run(
        now,
        now,
        row.tenant_id,
        row.user_id,
      ).changes);
      disabledUsers += Number(disableUser.run(now, row.tenant_id, row.user_id).changes);
      requireCondition(
        Number(countSessions.get(row.tenant_id, row.user_id).count) === 0,
        "browser.users.cleanup",
        "session_not_revoked",
      );
    }
    if (requireAll) {
      requireCondition(
        ledger.intents.length === 2 && matchedUsers === ledger.intents.length,
        "browser.users.cleanup",
        "user_missing",
      );
    }
    database.exec("COMMIT");
    transactionOpen = false;
    return {
      intended_user_count: ledger.intents.length,
      matched_user_count: matchedUsers,
      newly_disabled_user_count: disabledUsers,
      newly_revoked_session_count: revokedSessions,
      all_matched_users_disabled: matchedUsers === ledger.intents.length,
      all_matched_sessions_revoked: true,
    };
  } catch (error) {
    if (transactionOpen) {
      try { database.exec("ROLLBACK"); } catch { /* 最终仍按固定清理错误失败。 */ }
    }
    // 已分类验收错误必须保留原 stage/code；固定 database_failed 只收敛未知数据库异常。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    fail("browser.users.cleanup", "database_failed");
  } finally {
    try { database?.close(); } catch { /* 关闭失败不覆盖更具体的清理结论。 */ }
  }
}


/** 匹配一个真实 Control API 响应，避免同页并行请求抢占等待器。 */
function waitForControlResponse(page, config, pathname, method, timeout = 30000) {
  return page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === pathname
        && response.request().method() === method;
    } catch {
      return false;
    }
  }, { timeout });
}


/** 从真实侧栏进入 PAGE-080，并等待首个日志 GET 与稳定面板。 */
async function openRequestLogsPage(page, config) {
  const responsePromise = waitForControlResponse(page, config, "/request-logs", "GET", 45000);
  await page.locator('[data-console-nav="request-logs"]').click();
  const response = await responsePromise;
  requireCondition(response.status() === 200, "browser.open_request_logs", "status");
  const payload = await readJsonResponse(response, "browser.open_request_logs");
  validateRequestLogList(payload);
  await page.locator("#request-logs-title").waitFor({ state: "visible", timeout: 45000 });
  await page.locator("#request-log-filter-form").waitFor({ state: "visible", timeout: 45000 });
  return { allowed: true, navigation_visible: true };
}


/** 触发表单查询并验证成功、空结果或统一 422 错误三种真实页面状态。 */
async function queryRequestLogs(page, config, {
  stateName,
  requestId = "",
  expectedStatus,
  expectedCode = null,
  expectedEmpty = false,
  tracker,
}) {
  const form = page.locator("#request-log-filter-form");
  await form.locator('[name="requestId"]').fill(requestId);
  // 错误态使用真实单边时间窗口触发后端 422，不拦截或伪造网络响应。
  if (stateName === "error") {
    // 单边时间窗口由后端统一判为 422，借此验收真实异常投影而不拦截或伪造网络。
    await form.locator('[name="createdTo"]').fill("");
    tracker.expectHttpStatus(422);
  }
  const responsePromise = waitForControlResponse(page, config, "/request-logs", "GET");
  await form.locator('button[type="submit"]').click();
  const response = await responsePromise;
  requireCondition(response.status() === expectedStatus, "browser.query." + stateName, "status");
  const payload = await readJsonResponse(response, "browser.query." + stateName);
  if (expectedCode) {
    requireCondition(payload.code === expectedCode, "browser.query." + stateName, "code");
    requireCondition(SAFE_IDENTIFIER_PATTERN.test(String(payload.request_id || "")), "browser.query." + stateName, "request_id");
    const error = page.locator(".request-logs-panel .error-state");
    await error.waitFor({ state: "visible", timeout: 30000 });
    const text = await error.innerText();
    requireCondition(text.includes(String(payload.request_id)), "browser.query." + stateName, "request_id_not_visible");
    requireCondition(await page.locator(".request-log-table").count() === 0, "browser.query." + stateName, "stale_table_visible");
    return { unified_error_visible: true, stale_results_hidden: true, code: expectedCode };
  }
  const result = validateRequestLogList(payload, {
    expectedRequestId: expectedEmpty ? null : requestId,
    expectedEmpty,
  });
  if (expectedEmpty) {
    await page.locator(".request-log-empty").waitFor({ state: "visible", timeout: 30000 });
    requireCondition(await page.locator("[data-request-log-explain]").count() === 0, "browser.query.empty", "stale_rows_visible");
    return { ...result, empty_state_visible: true };
  }
  const row = page.locator('[data-request-log-explain="' + requestId + '"]');
  await row.waitFor({ state: "visible", timeout: 30000 });
  return { ...result, matching_row_visible: true };
}


/** 离开错误态并重新进入 PAGE-080，确保后续生命周期不继承无效筛选。 */
async function reopenRequestLogsPage(page, config) {
  await page.locator('[data-console-nav="overview"]').click();
  await page.locator('.console-shell[data-active-navigation="overview"]').waitFor({ state: "visible", timeout: 45000 });
  return openRequestLogsPage(page, config);
}


/** 把当前表单配置为有足够分页数据的 Provider 失败查询，所有值仍只驻留页面内存。 */
async function prepareSavedFilterForm(page) {
  const form = page.locator("#request-log-filter-form");
  await form.locator('[name="requestId"]').fill("");
  await form.locator('[name="status"]').selectOption("failed");
  await form.locator('[name="providerCode"]').fill("deepseek");
  const advanced = form.locator(".request-log-advanced-fields");
  if (!(await advanced.getAttribute("open"))) await advanced.locator("summary").click();
  await form.locator('[name="terminalStage"]').selectOption("provider");
  await form.locator('[name="sortBy"]').selectOption("latency_ms");
  await form.locator('[name="sortDirection"][value="asc"]').check();
}


/** 验证 SavedFilter 创建、显式应用、更新、分页、解释跳转与软删除的真实页面闭环。 */
async function exerciseSavedFilterLifecycle(page, config, tracker, viewport, explainRequestId) {
  await prepareSavedFilterForm(page);
  const originalName = "PRD055 Browser Filter " + randomBytes(5).toString("hex");
  await page.locator("#request-log-saved-filter-name").fill(originalName);
  const createResponsePromise = waitForControlResponse(page, config, "/request-logs/saved-filters", "POST");
  await page.locator("#request-log-saved-filter-create").click();
  const createResponse = await createResponsePromise;
  requireCondition(createResponse.status() === 201, "browser.saved_filter.create", "status");
  const createPayload = await readJsonResponse(createResponse, "browser.saved_filter.create");
  assertSafeEvidence(createPayload);
  const created = createPayload.saved_filter;
  const savedFilterId = String(created?.saved_filter_id || "");
  requireCondition(SAVED_FILTER_PATTERN.test(savedFilterId), "browser.saved_filter.create", "id");
  requireCondition(created?.version === 1, "browser.saved_filter.create", "version");
  requireCondition(!("tenant_id" in created) && !("owner_user_id" in created), "browser.saved_filter.create", "owner_exposed");
  const option = page.locator('#request-log-saved-filter-select option[value="' + savedFilterId + '"]');
  await option.waitFor({ state: "attached", timeout: 30000 });

  const beforeSelection = tracker.request_log_get_count;
  await page.locator("#request-log-saved-filter-select").selectOption(savedFilterId);
  await page.waitForTimeout(250);
  requireCondition(tracker.request_log_get_count === beforeSelection, "browser.saved_filter.select", "implicit_query");
  requireCondition(await page.locator("#request-log-saved-filter-apply").isEnabled(), "browser.saved_filter.select", "apply_disabled");

  const applyResponsePromise = waitForControlResponse(page, config, "/request-logs", "GET");
  await page.locator("#request-log-saved-filter-apply").click();
  const applyResponse = await applyResponsePromise;
  requireCondition(applyResponse.status() === 200, "browser.saved_filter.apply", "status");
  const applyUrl = new URL(applyResponse.url());
  requireCondition(applyUrl.searchParams.get("saved_filter_id") === savedFilterId, "browser.saved_filter.apply", "id_missing");
  for (const forbidden of [
    "filters", "status", "request_id", "model", "provider_code", "environment_id",
    "snapshot_version", "project_id", "application_id", "error_code", "sort_by", "sort_direction",
  ]) {
    requireCondition(!applyUrl.searchParams.has(forbidden), "browser.saved_filter.apply", "client_copy");
  }
  const appliedPayload = await readJsonResponse(applyResponse, "browser.saved_filter.apply");
  const applied = validateRequestLogList(appliedPayload);
  requireCondition(
    appliedPayload.request_logs.length === 20
      && appliedPayload.request_logs.every((item) => item.status === "failed" && item.provider_code === "deepseek"),
    "browser.saved_filter.apply",
    "result_accuracy",
  );
  const appliedFirstRequestId = String(appliedPayload.request_logs[0]?.request_id || "");
  requireCondition(SAFE_IDENTIFIER_PATTERN.test(appliedFirstRequestId), "browser.saved_filter.apply", "first_request_id");
  // 响应返回早于主应用 refresh 完成；必须等新首行出现，不能继续操作旧分页控件。
  await page.locator('[data-request-log-explain="' + appliedFirstRequestId + '"]').waitFor({
    state: "visible",
    timeout: 30000,
  });

  const next = page.locator("#request-log-next");
  requireCondition(await next.isEnabled(), "browser.pagination", "next_disabled");
  const pageResponsePromise = waitForControlResponse(page, config, "/request-logs", "GET");
  await next.click();
  const pageResponse = await pageResponsePromise;
  requireCondition(pageResponse.status() === 200, "browser.pagination", "status");
  const pageUrl = new URL(pageResponse.url());
  requireCondition(
    pageUrl.searchParams.get("saved_filter_id") === savedFilterId
      && pageUrl.searchParams.get("offset") === "20",
    "browser.pagination",
    "query_contract",
  );
  const pagePayload = await readJsonResponse(pageResponse, "browser.pagination");
  const secondPage = validateRequestLogList(pagePayload);
  requireCondition(secondPage.offset === 20 && secondPage.returned_count === 20, "browser.pagination", "page_contract");
  await page.locator('.request-log-pager[data-offset="20"]').waitFor({ state: "visible", timeout: 30000 });

  const renamed = "PRD055 Browser Renamed " + randomBytes(5).toString("hex");
  await page.locator("#request-log-saved-filter-name").fill(renamed);
  const updateResponsePromise = waitForControlResponse(
    page,
    config,
    "/request-logs/saved-filters/" + savedFilterId,
    "PATCH",
  );
  await page.locator("#request-log-saved-filter-update").click();
  const updateResponse = await updateResponsePromise;
  requireCondition(updateResponse.status() === 200, "browser.saved_filter.update", "status");
  const updatePayload = await readJsonResponse(updateResponse, "browser.saved_filter.update");
  requireCondition(
    updatePayload.saved_filter?.saved_filter_id === savedFilterId
      && updatePayload.saved_filter?.version === 2
      && updatePayload.saved_filter?.name === renamed,
    "browser.saved_filter.update",
    "contract",
  );
  // PATCH 响应先于后续 list refresh；等 option 文本更新后才允许进入解释跳转。
  await page.waitForFunction(
    ({ id, expectedName }) => {
      const option = document.querySelector(
        '#request-log-saved-filter-select option[value="' + CSS.escape(id) + '"]',
      );
      return option?.textContent === expectedName;
    },
    { id: savedFilterId, expectedName: renamed },
    { timeout: 30000 },
  );
  // 更新后的选中筛选、分页结果和操作控件必须在当前真实视口内单独留证。
  const savedFilterCapture = await captureCurrentState(
    page,
    config,
    "saved-filter",
    viewport,
  );

  // SavedFilter 的合成分页行不保证拥有完整解释事实；回到 Runtime 已验证的请求再验收 Explain 跳转。
  await reopenRequestLogsPage(page, config);
  await queryRequestLogs(page, config, {
    stateName: "explain-source",
    requestId: explainRequestId,
    expectedStatus: 200,
    tracker,
  });
  const explainAction = page.locator('[data-request-log-explain="' + explainRequestId + '"]');
  const sourceRequestId = String(await explainAction.getAttribute("data-request-log-explain") || "");
  requireCondition(
    sourceRequestId === explainRequestId && SAFE_IDENTIFIER_PATTERN.test(sourceRequestId),
    "browser.explain_handoff",
    "request_id",
  );
  const explainResponsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === "/request-explain/" + encodeURIComponent(sourceRequestId)
        && response.request().method() === "GET";
    } catch {
      return false;
    }
  }, { timeout: 30000 });
  await explainAction.click();
  const explainResponse = await explainResponsePromise;
  requireCondition(explainResponse.status() === 200, "browser.explain_handoff", "status");
  await page.locator("#request-explanations-title").waitFor({ state: "visible", timeout: 30000 });
  requireCondition(!(await pageUrlContains(page, sourceRequestId)), "browser.explain_handoff", "source_id_in_url");

  await openRequestLogsPage(page, config);
  await page.locator("#request-log-saved-filter-select").selectOption(savedFilterId);
  page.once("dialog", (dialog) => dialog.accept());
  const deleteResponsePromise = waitForControlResponse(
    page,
    config,
    "/request-logs/saved-filters/" + savedFilterId,
    "DELETE",
  );
  await page.locator("#request-log-saved-filter-delete").click();
  const deleteResponse = await deleteResponsePromise;
  requireCondition(deleteResponse.status() === 204, "browser.saved_filter.delete", "status");
  await page.waitForFunction(
    (id) => !document.querySelector('#request-log-saved-filter-select option[value="' + CSS.escape(id) + '"]'),
    savedFilterId,
    { timeout: 30000 },
  );
  return {
    create_listed: true,
    selection_query_count: 0,
    explicit_apply_only: true,
    server_authoritative_query: true,
    update_version: 2,
    pagination_offset: 20,
    explanation_handoff: true,
    soft_delete_visible: true,
    applied_page: applied,
    capture: savedFilterCapture,
  };
}


/** 页面 URL 的值扫描必须在浏览器上下文完成，原始 URL 不进入 Node 异常或证据。 */
async function pageUrlContains(page, value) {
  return page.evaluate((candidate) => location.href.includes(candidate), value);
}


/** 等待两个动画帧，让响应式布局和滚动位置在测量前稳定。 */
async function settleLayout(page) {
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}


/** 测量 PAGE-080 横向溢出、祖先裁切和可操作控件重叠。 */
async function readLayoutMetrics(page) {
  return page.evaluate(() => {
    const visible = (element) => {
      if (!element) return false;
      if (element.closest("details:not([open])")) return false;
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 1 && box.height > 1;
    };
    const root = [...document.querySelectorAll(".request-logs-panel")].find(visible);
    if (!root) return null;
    const clippedByAncestor = (element) => {
      const box = element.getBoundingClientRect();
      for (let ancestor = element.parentElement; ancestor && ancestor !== document.body; ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        const parent = ancestor.getBoundingClientRect();
        if (["hidden", "clip"].includes(style.overflowX) && (box.left < parent.left - 1 || box.right > parent.right + 1)) return true;
        if (ancestor === root) break;
      }
      return false;
    };
    const controls = [...root.querySelectorAll("button,input,select,textarea")].filter(visible);
    let controlOverlapCount = 0;
    for (let left = 0; left < controls.length; left += 1) {
      for (let right = left + 1; right < controls.length; right += 1) {
        const a = controls[left].getBoundingClientRect();
        const b = controls[right].getBoundingClientRect();
        const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (width > 2 && height > 2) controlOverlapCount += 1;
      }
    }
    const content = [...root.querySelectorAll([
      ".request-logs-heading", ".request-log-saved-filters", ".request-log-filter-form",
      ".request-log-empty", ".error-state", ".request-log-pager",
    ].join(","))].filter(visible);
    const horizontalContainers = [...root.querySelectorAll([
      ".request-log-saved-filters", ".request-log-filter-form", ".request-log-advanced-grid",
      ".request-log-query-controls", ".request-log-table", ".request-log-pager",
    ].join(","))].filter(visible);
    const rootBox = root.getBoundingClientRect();
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      root_left: rootBox.left,
      root_right: rootBox.right,
      root_client_width: root.clientWidth,
      root_scroll_width: root.scrollWidth,
      anchor_box: { x: rootBox.x, y: rootBox.y, width: rootBox.width, height: rootBox.height },
      clipped_control_count: controls.filter((element) => {
        const box = element.getBoundingClientRect();
        return box.left < -1 || box.right > innerWidth + 1 || clippedByAncestor(element);
      }).length,
      clipped_content_count: content.filter(clippedByAncestor).length,
      horizontal_container_overflow_count: horizontalContainers.filter(
        (element) => element.scrollWidth > element.clientWidth + 1,
      ).length,
      control_overlap_count: controlOverlapCount,
    };
  });
}


/** 把布局原始数值冻结为无溢出、无裁切、无重叠的稳定证据。 */
function assertLayoutMetrics(metrics, viewport, stage = "browser.layout") {
  requireCondition(metrics && typeof metrics === "object", stage, "root_missing");
  requireCondition(metrics.viewport_width === viewport.width && metrics.viewport_height === viewport.height, stage, "viewport");
  requireCondition(
    metrics.document_scroll_width <= viewport.width + 1 && metrics.body_scroll_width <= viewport.width + 1,
    stage,
    "document_overflow",
  );
  requireCondition(metrics.root_left >= -1 && metrics.root_right <= viewport.width + 1, stage, "root_overflow");
  requireCondition(metrics.root_scroll_width <= metrics.root_client_width + 1, stage, "root_scroll_overflow");
  requireCondition(metrics.clipped_control_count === 0 && metrics.clipped_content_count === 0, stage, "clipping");
  requireCondition(
    metrics.horizontal_container_overflow_count === 0 && metrics.control_overlap_count === 0,
    stage,
    "overlap_or_container",
  );
  return {
    horizontal_overflow: false,
    clipped_control_count: 0,
    clipped_content_count: 0,
    overlap_count: 0,
    responsive_mode: viewport.width < 720 ? "single-column" : "compact-grid",
  };
}


/** 证明截图锚点和当前视口相交，避免把离屏空白当作页面证据。 */
function assertViewportIntersection(box, viewport, stage = "browser.layout") {
  requireCondition(box && [box.x, box.y, box.width, box.height].every(Number.isFinite), stage, "anchor_box");
  requireCondition(box.width > 0 && box.height > 0, stage, "anchor_empty");
  requireCondition(
    box.x < viewport.width && box.x + box.width > 0
      && box.y < viewport.height && box.y + box.height > 0,
    stage,
    "anchor_outside",
  );
  return { anchor_intersects_viewport: true };
}


/** PNG Paeth 预测器只用于复原本地截图像素，不输出任何图像正文。 */
function paethPredictor(left, up, upperLeft) {
  const estimate = left + up - upperLeft;
  const leftDistance = Math.abs(estimate - left);
  const upDistance = Math.abs(estimate - up);
  const upperLeftDistance = Math.abs(estimate - upperLeft);
  if (leftDistance <= upDistance && leftDistance <= upperLeftDistance) return left;
  if (upDistance <= upperLeftDistance) return up;
  return upperLeft;
}


/** 解析 8-bit RGB/RGBA PNG，核对尺寸并证明截图存在非透明、非单色像素。 */
function inspectPngPixels(buffer, viewport, stage = "browser.screenshot") {
  requireCondition(Buffer.isBuffer(buffer), stage, "not_buffer");
  requireCondition(
    buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])),
    stage,
    "png_signature",
  );
  let offset = 8;
  let header = null;
  const compressed = [];
  while (offset + 12 <= buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.subarray(offset + 4, offset + 8).toString("ascii");
    const start = offset + 8;
    const end = start + length;
    requireCondition(end + 4 <= buffer.length, stage, "png_bounds");
    const data = buffer.subarray(start, end);
    if (type === "IHDR") {
      header = {
        width: data.readUInt32BE(0),
        height: data.readUInt32BE(4),
        bitDepth: data[8],
        colorType: data[9],
        compression: data[10],
        filter: data[11],
        interlace: data[12],
      };
    } else if (type === "IDAT") {
      compressed.push(data);
    }
    offset = end + 4;
    if (type === "IEND") break;
  }
  requireCondition(header && compressed.length > 0, stage, "png_chunks");
  requireCondition(
    header.bitDepth === 8 && [2, 6].includes(header.colorType)
      && header.compression === 0 && header.filter === 0 && header.interlace === 0,
    stage,
    "png_encoding",
  );
  requireCondition(header.width === viewport.width && header.height === viewport.height, stage, "png_dimensions");
  const channels = header.colorType === 6 ? 4 : 3;
  const stride = header.width * channels;
  let raw;
  try {
    raw = inflateSync(Buffer.concat(compressed));
  } catch {
    fail(stage, "png_inflate");
  }
  requireCondition(raw.length === (stride + 1) * header.height, stage, "png_raw_length");
  const pixels = Buffer.alloc(stride * header.height);
  for (let row = 0; row < header.height; row += 1) {
    const rawStart = row * (stride + 1);
    const outputStart = row * stride;
    const filterType = raw[rawStart];
    requireCondition(filterType >= 0 && filterType <= 4, stage, "png_filter");
    for (let column = 0; column < stride; column += 1) {
      const encoded = raw[rawStart + 1 + column];
      const left = column >= channels ? pixels[outputStart + column - channels] : 0;
      const up = row > 0 ? pixels[outputStart - stride + column] : 0;
      const upperLeft = row > 0 && column >= channels ? pixels[outputStart - stride + column - channels] : 0;
      let predictor = 0;
      if (filterType === 1) predictor = left;
      else if (filterType === 2) predictor = up;
      else if (filterType === 3) predictor = Math.floor((left + up) / 2);
      else if (filterType === 4) predictor = paethPredictor(left, up, upperLeft);
      pixels[outputStart + column] = (encoded + predictor) & 0xff;
    }
  }
  const first = pixels.subarray(0, 3);
  let visible = channels === 3;
  let variance = false;
  for (let index = 0; index < pixels.length; index += channels) {
    if (channels === 4 && pixels[index + 3] > 0) visible = true;
    if (pixels[index] !== first[0] || pixels[index + 1] !== first[1] || pixels[index + 2] !== first[2]) {
      variance = true;
    }
    if (visible && variance) break;
  }
  requireCondition(visible && variance, stage, "png_blank");
  return {
    width: header.width,
    height: header.height,
    bytes: buffer.length,
    sha256: createHash("sha256").update(buffer).digest("hex"),
    non_blank_pixels: true,
  };
}


/** 生成固定状态与视口组合的截图相对路径，拒绝调用方注入文件名。 */
function screenshotFilename(stateName, viewportName) {
  requireCondition(
    ["success", "empty", "error", "saved-filter"].includes(stateName),
    "browser.screenshot",
    "state_name",
  );
  requireCondition(VIEWPORTS.some((item) => item.name === viewportName), "browser.screenshot", "viewport_name");
  return "screenshots/" + stateName + "-" + viewportName + ".png";
}


/** 捕获当前 PAGE-080 状态并独占写入截图，同时返回布局和像素摘要。 */
async function captureCurrentState(page, config, stateName, viewport) {
  const stage = "browser.capture." + stateName + "." + viewport.name;
  await page.evaluate(() => {
    const root = [...document.querySelectorAll(".request-logs-panel")].find((element) => {
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 1 && box.height > 1;
    });
    root?.scrollIntoView({ block: "start" });
  });
  await settleLayout(page);
  const rawLayout = await readLayoutMetrics(page);
  const layout = assertLayoutMetrics(rawLayout, viewport, stage);
  const intersection = assertViewportIntersection(rawLayout?.anchor_box, viewport, stage);
  const relative = screenshotFilename(stateName, viewport.name);
  const target = path.join(config.artifactDirectory, relative);
  const masks = [];
  const userMenu = page.locator(".user-menu");
  if (await userMenu.count() > 0 && await userMenu.first().isVisible()) masks.push(userMenu.first());
  const buffer = await page.screenshot({
    type: "png",
    fullPage: false,
    mask: masks,
    animations: "disabled",
  });
  try {
    await writeFile(target, buffer, { flag: "wx" });
  } catch {
    fail(stage, "screenshot_write_failed");
  }
  return {
    layout: { ...layout, ...intersection },
    screenshot: {
      file: relative,
      state: stateName,
      viewport: viewport.name,
      ...inspectPngPixels(buffer, viewport, stage),
    },
  };
}


/** 在页面上下文扫描 URL、DOM 与 Storage；原始内容和 Token 不离开浏览器。 */
async function readPrivacyMetrics(page, requestIds = [], passwords = []) {
  return page.evaluate(({ authKeys, publicIds, secretValues }) => {
    const html = document.documentElement?.outerHTML ?? "";
    const url = location.href;
    const entries = [];
    for (const [storageName, storage] of [["local", localStorage], ["session", sessionStorage]]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key) entries.push({ storageName, key, value: String(storage.getItem(key) ?? "") });
      }
    }
    const storageText = entries.map(({ key, value }) => key + "=" + value).join("\n");
    const authToken = sessionStorage.getItem("metahub.auth_token");
    return {
      local_storage_count: entries.filter((item) => item.storageName === "local").length,
      session_auth_slot_count: authKeys.filter((key) => sessionStorage.getItem(key) !== null).length,
      forbidden_storage_key_count: entries.filter(
        ({ key }) => !authKeys.includes(key)
          && /password|token|secret|prompt|response|payload|request[_-]?id|filter/i.test(key),
      ).length,
      request_id_url_or_storage_hits: publicIds.filter(
        (value) => url.includes(value) || storageText.includes(value),
      ).length,
      password_url_dom_storage_hits: secretValues.filter(
        (value) => value && (url.includes(value) || html.includes(value) || storageText.includes(value)),
      ).length,
      bearer_dom_hits: (html.match(/Bearer\s+[^"\s]{8,}/gi) ?? []).length,
      auth_token_exposed: Boolean(authToken) && (html.includes(authToken) || url.includes(authToken)),
    };
  }, { authKeys: [...AUTH_STORAGE_KEYS], publicIds: requestIds, secretValues: passwords });
}


/** 把登录态隐私扫描压缩为稳定零命中证据。 */
function assertBrowserPrivacy(metrics, stage = "browser.privacy") {
  requireCondition(metrics.local_storage_count === 0, stage, "local_storage");
  requireCondition(metrics.session_auth_slot_count === AUTH_STORAGE_KEYS.size, stage, "auth_slots");
  requireCondition(metrics.forbidden_storage_key_count === 0, stage, "forbidden_storage_key");
  requireCondition(metrics.request_id_url_or_storage_hits === 0, stage, "request_id_persisted");
  requireCondition(metrics.password_url_dom_storage_hits === 0, stage, "password_exposed");
  requireCondition(metrics.bearer_dom_hits === 0 && metrics.auth_token_exposed === false, stage, "token_exposed");
  return {
    request_id_persistence_hits: 0,
    password_exposure_hits: 0,
    auth_storage_contract_valid: true,
  };
}


/** 校验注销后认证槽位、PAGE-080 DOM 和旧 request_id 均已销毁。 */
function verifySessionCleanup(metrics, stage = "browser.session_cleanup") {
  requireCondition(metrics && typeof metrics === "object", stage, "metrics_missing");
  requireCondition(metrics.local_storage_count === 0 && metrics.session_storage_count === 0, stage, "storage_not_empty");
  requireCondition(metrics.request_log_dom_count === 0, stage, "request_log_dom_present");
  requireCondition(metrics.request_id_dom_hits === 0, stage, "request_id_dom_present");
  return {
    storage_cleared: true,
    request_log_dom_cleared: true,
    request_id_cleared: true,
  };
}


/** 真实点击 Console 注销，并验证页面 owner 与浏览器 Storage 同步清空。 */
async function logoutAndVerify(page, config, requestIds = []) {
  const responsePromise = waitForControlResponse(page, config, "/auth/logout", "POST");
  await page.locator("#logout-button").click();
  const response = await responsePromise;
  requireCondition(response.status() === 200, "browser.logout", "status");
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30000 });
  const metrics = await page.evaluate((publicIds) => {
    const html = document.documentElement?.outerHTML ?? "";
    return {
      local_storage_count: localStorage.length,
      session_storage_count: sessionStorage.length,
      request_log_dom_count: document.querySelectorAll(
        ".request-logs-panel, #request-logs-title, #request-log-filter-form",
      ).length,
      request_id_dom_hits: publicIds.filter((value) => html.includes(value)).length,
    };
  }, requestIds);
  // 注销结果只返回稳定布尔摘要，Storage 和 DOM 正文不得进入证据。
  return verifySessionCleanup(metrics);
}


/** 异常退出时优先调用真实 logout，再清空 Storage，防止一次性验收 Session 残留。 */
async function revokeSessionAfterFailure(page, controlOrigin) {
  try {
    return await page.evaluate(async (origin) => {
      const access = sessionStorage.getItem("metahub.auth_token");
      let revoked = false;
      if (access) {
        try {
          const response = await fetch(origin + "/auth/logout", {
            method: "POST",
            headers: {
              Authorization: "Bearer " + access,
              "X-Request-ID": "req_prd055_browser_failure_cleanup",
            },
          });
          revoked = response.status === 200;
        } catch {
          revoked = false;
        }
      }
      localStorage.clear();
      sessionStorage.clear();
      return revoked;
    }, controlOrigin);
  } catch {
    return false;
  }
}


/**
 * 执行一个必须可补偿撤销的浏览器 Session。
 *
 * 登录成功后任何查询、隐私检查或正常 logout 失败都会进入 revoke；登录本身失败时没有可撤销
 * Session，避免补偿请求掩盖真实认证错误。
 */
async function runRevocableSession({ login, operation, logout, revoke }) {
  let sessionNeedsRevocation = false;
  try {
    await login();
    sessionNeedsRevocation = true;
    const value = await operation();
    const cleanup = await logout();
    sessionNeedsRevocation = false;
    return { value, cleanup };
  } finally {
    // logout 未确认成功时强制补偿撤销，防止查询或隐私断言失败后遗留可用 Session。
    if (sessionNeedsRevocation) await revoke();
  }
}


/** 比较 viewer 前后的三类受保护请求计数，任一增长都证明发生了越权预取。 */
function assertNoViewerPrefetch(before, after) {
  const keys = ["requestLogs", "savedFilters", "explanations"];
  let count = 0;
  for (const key of keys) {
    requireCondition(
      Number.isInteger(before?.[key])
        && Number.isInteger(after?.[key])
        && after[key] >= before[key],
      "browser.roles.viewer",
      "counter_invalid",
    );
    count += after[key] - before[key];
  }
  requireCondition(count === 0, "browser.roles.viewer", "api_prefetched");
  return count;
}


/** 验证 developer 可真实查询，而 viewer 手写 hash 后仍无入口、无 DOM、无 API 预取。 */
async function verifyRestrictedRoles(page, config, credentials, runtime, tracker) {
  const developer = credentials.find((item) => item.role === "developer");
  const viewer = credentials.find((item) => item.role === "viewer");
  requireCondition(developer && viewer, "browser.roles", "credentials_missing");

  const developerRun = await runRevocableSession({
    login: () => loginThroughConsole(page, config, developer),
    operation: async () => {
      await openRequestLogsPage(page, config);
      const query = await queryRequestLogs(page, config, {
        stateName: "success",
        requestId: runtime.sharedRequestId,
        expectedStatus: 200,
        tracker,
      });
      const privacy = assertBrowserPrivacy(
        await readPrivacyMetrics(page, [runtime.sharedRequestId], [developer.password]),
        "browser.roles.developer.privacy",
      );
      return { query, privacy };
    },
    logout: () => logoutAndVerify(page, config, [runtime.sharedRequestId]),
    revoke: () => revokeSessionAfterFailure(page, config.controlOrigin),
  });

  const viewerRun = await runRevocableSession({
    login: () => loginThroughConsole(page, config, viewer),
    operation: async () => {
      const beforeViewer = {
        requestLogs: tracker.request_log_get_count,
        savedFilters: tracker.saved_filter_request_count,
        explanations: tracker.request_explain_get_count,
      };
      await page.goto(config.consoleOrigin + "/#request-logs", {
        waitUntil: "domcontentloaded",
        timeout: 30000,
      });
      await page.locator("#logout-button").waitFor({ state: "visible", timeout: 45000 });
      await runSafeBrowserOperation(
        "browser.roles.viewer",
        "hash_not_sanitized",
        () => page.waitForFunction(() => location.hash !== "#request-logs", null, { timeout: 20000 }),
      );
      requireCondition(
        await page.locator('[data-console-nav="request-logs"]').count() === 0,
        "browser.roles.viewer",
        "navigation_visible",
      );
      requireCondition(
        await page.locator(".request-logs-panel, #request-logs-title").count() === 0,
        "browser.roles.viewer",
        "dom_visible",
      );
      const afterViewer = {
        requestLogs: tracker.request_log_get_count,
        savedFilters: tracker.saved_filter_request_count,
        explanations: tracker.request_explain_get_count,
      };
      const prefetchCount = assertNoViewerPrefetch(beforeViewer, afterViewer);
      const activeNavigation = await page.locator(".console-shell").getAttribute("data-active-navigation");
      requireCondition(activeNavigation !== "request-logs", "browser.roles.viewer", "active_navigation");
      const privacy = assertBrowserPrivacy(
        await readPrivacyMetrics(page, [runtime.sharedRequestId], [viewer.password]),
        "browser.roles.viewer.privacy",
      );
      return { privacy, prefetchCount };
    },
    logout: () => logoutAndVerify(page, config, [runtime.sharedRequestId]),
    revoke: () => revokeSessionAfterFailure(page, config.controlOrigin),
  });

  return {
    developer: {
      allowed: true,
      query: developerRun.value.query,
      privacy: developerRun.value.privacy,
      cleanup: developerRun.cleanup,
    },
    viewer: {
      navigation_hidden: true,
      manual_hash_sanitized: true,
      protected_dom_absent: true,
      request_log_prefetch_count: viewerRun.value.prefetchCount,
      privacy: viewerRun.value.privacy,
      cleanup: viewerRun.cleanup,
    },
  };
}


/** 独占创建截图目录；既有目录可能包含旧批次文件或链接，必须失败关闭。 */
async function createScreenshotDirectory(artifactDirectory) {
  const target = path.join(artifactDirectory, "screenshots");
  try {
    await mkdir(target);
    const stat = lstatSync(target);
    requireCondition(stat.isDirectory() && !stat.isSymbolicLink(), "evidence.screenshots", "not_plain_directory");
  } catch (error) {
    // 已存在、链接或不可写均禁止复用，避免新截图与旧证据混合。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    fail("evidence.screenshots", "exists_or_unwritable");
  }
  // 截图写入只消费本次新建的普通目录，禁止调用方覆盖目标路径。
  return target;
}


/** 以独占创建方式发布 Browser JSON；同批次重跑不得覆盖已有验收事实。 */
async function publishEvidence(directory, evidence) {
  const target = path.join(directory, "browser-acceptance.json");
  const encoded = JSON.stringify(evidence, null, 2) + "\n";
  try {
    await writeFile(target, encoded, { encoding: "utf8", flag: "wx" });
  } catch {
    fail("evidence.publish", "write_failed");
  }
  return target;
}


/** 执行八状态截图、双视口 SavedFilter 闭环、三角色 RBAC、隐私与 Session 清理验收。 */
async function executeBrowserAcceptance(config) {
  const runtimeEvidence = await loadStrictRuntimeEvidence(config.artifactDirectory);
  await createScreenshotDirectory(config.artifactDirectory);
  let chromium;
  try {
    ({ chromium } = require("playwright"));
  } catch {
    fail("browser.playwright", "module_missing");
  }
  const launchOptions = { headless: !config.headed };
  if (config.chromePath) launchOptions.executablePath = config.chromePath;
  const browser = await runSafeBrowserOperation("browser.launch", "failed", () => chromium.launch(launchOptions));
  const context = await browser.newContext({
    viewport: { width: VIEWPORTS[0].width, height: VIEWPORTS[0].height },
    locale: "zh-CN",
  });
  const page = await context.newPage();
  const tracker = await installBrowserGuards(context, page, config);
  const adminIdentity = { role: "admin", email: config.adminEmail, password: config.adminPassword };
  const missingRequestId = "req_prd055_browser_missing_" + randomBytes(6).toString("hex");
  const screenshots = [];
  const layouts = {};
  const states = {};
  const savedFilters = {};
  const temporaryAccounts = createTemporaryAccountLedger(config);
  let credentials = [];
  let fixtureCleanup = null;
  let authenticated = false;
  try {
    await loginThroughConsole(page, config, adminIdentity);
    authenticated = true;
    credentials = await createRestrictedRoleUsers(page, config, temporaryAccounts);
    await openRequestLogsPage(page, config);

    for (const viewport of VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      states[viewport.name + ":success"] = await queryRequestLogs(page, config, {
        stateName: "success",
        requestId: runtimeEvidence.runtime.sharedRequestId,
        expectedStatus: 200,
        tracker,
      });
      let captured = await captureCurrentState(page, config, "success", viewport);
      layouts[viewport.name + ":success"] = captured.layout;
      screenshots.push(captured.screenshot);

      states[viewport.name + ":empty"] = await queryRequestLogs(page, config, {
        stateName: "empty",
        requestId: missingRequestId,
        expectedStatus: 200,
        expectedEmpty: true,
        tracker,
      });
      captured = await captureCurrentState(page, config, "empty", viewport);
      layouts[viewport.name + ":empty"] = captured.layout;
      screenshots.push(captured.screenshot);

      states[viewport.name + ":error"] = await queryRequestLogs(page, config, {
        stateName: "error",
        expectedStatus: 422,
        expectedCode: "request_log.query_invalid",
        tracker,
      });
      captured = await captureCurrentState(page, config, "error", viewport);
      layouts[viewport.name + ":error"] = captured.layout;
      screenshots.push(captured.screenshot);
      await reopenRequestLogsPage(page, config);
      const lifecycle = await exerciseSavedFilterLifecycle(
        page,
        config,
        tracker,
        viewport,
        runtimeEvidence.runtime.sharedRequestId,
      );
      layouts[viewport.name + ":saved-filter"] = lifecycle.capture.layout;
      screenshots.push(lifecycle.capture.screenshot);
      const lifecycleEvidence = { ...lifecycle };
      delete lifecycleEvidence.capture;
      savedFilters[viewport.name] = lifecycleEvidence;
    }

    await page.setViewportSize({ width: VIEWPORTS[0].width, height: VIEWPORTS[0].height });
    const privacy = assertBrowserPrivacy(
      await readPrivacyMetrics(
        page,
        [runtimeEvidence.runtime.sharedRequestId, missingRequestId],
        [config.adminPassword, ...credentials.map((item) => item.password)],
      ),
    );
    const adminCleanup = await logoutAndVerify(
      page,
      config,
      [runtimeEvidence.runtime.sharedRequestId, missingRequestId],
    );
    authenticated = false;
    const roles = await verifyRestrictedRoles(page, config, credentials, runtimeEvidence.runtime, tracker);
    // 只有三角色都注销后才停用临时账号；此摘要写入证据，邮箱和用户 ID 留在内存账本。
    fixtureCleanup = disableTemporaryUsersAndSessions(
      config,
      temporaryAccounts,
      { requireAll: true },
    );

    requireCondition(tracker.blocked_request_count === 0, "browser.network", "request_blocked");
    requireCondition(
      tracker.browser_error_count === 0 && tracker.console_error_count === 0,
      "browser.errors",
      "unexpected_error",
    );
    requireCondition(
      tracker.expected_http_console_error_count === VIEWPORTS.length,
      "browser.errors",
      "expected_422_count",
    );
    requireCondition(tracker.request_log_get_count >= 12, "browser.network", "request_log_get_count");
    requireCondition(
      tracker.request_explain_get_count === VIEWPORTS.length,
      "browser.network",
      "explain_get_count",
    );

    const evidence = {
      schema_version: 1,
      prd_id: "PRD-055",
      status: "passed",
      runtime_evidence_sha256: runtimeEvidence.sha256,
      generated_resources: {
        shared_request_id: runtimeEvidence.runtime.sharedRequestId,
        performance_rows: 100000,
      },
      checks: {
        admin: {
          allowed: true,
          navigation_visible: true,
          cleanup: adminCleanup,
        },
        roles,
        states,
        saved_filters: savedFilters,
        layouts,
        privacy,
        screenshots: {
          count: screenshots.length,
          all_non_blank: screenshots.length === VIEWPORTS.length * 4
            && screenshots.every((item) => item.non_blank_pixels === true),
        },
        network: {
          blocked_request_count: tracker.blocked_request_count,
          request_log_get_count: tracker.request_log_get_count,
          saved_filter_request_count: tracker.saved_filter_request_count,
          request_explain_get_count: tracker.request_explain_get_count,
          viewer_prefetch_count: roles.viewer.request_log_prefetch_count,
        },
        errors: {
          browser_error_count: tracker.browser_error_count,
          console_error_count: tracker.console_error_count,
          expected_http_console_error_count: tracker.expected_http_console_error_count,
        },
        session_cleanup: {
          role_count: 3,
          all_roles_cleared: true,
          temporary_accounts: fixtureCleanup,
        },
      },
      screenshots,
    };
    assertSafeEvidence(evidence, [
      config.adminPassword,
      ...credentials.flatMap((item) => [item.email, item.password]),
    ]);
    await publishEvidence(config.artifactDirectory, evidence);
    return evidence;
  } finally {
    // 中途失败仍优先撤销当前 Session，避免一次性验收身份残留在运行库。
    if (authenticated) await revokeSessionAfterFailure(page, config.controlOrigin);
    await context.close().catch(() => undefined);
    await browser.close().catch(() => undefined);
    if (!fixtureCleanup && temporaryAccounts.intents.length > 0) {
      // 失败路径也按账本回收已提交或响应丢失的部分账号；原始验收错误保持主结论。
      try {
        fixtureCleanup = disableTemporaryUsersAndSessions(config, temporaryAccounts);
      } catch {
        fixtureCleanup = null;
      }
    }
    credentials = credentials.map((item) => ({ ...item, email: "", password: "" }));
    temporaryAccounts.intents = temporaryAccounts.intents.map((item) => ({
      ...item,
      email: "",
      displayName: "",
      userId: "",
      tenantId: "",
    }));
  }
}


/** CLI 入口只输出固定成功摘要或脱敏失败分类。 */
async function main() {
  try {
    const config = parseArguments();
    await executeBrowserAcceptance(config);
    process.stdout.write('{"prd_id":"PRD-055","status":"passed","evidence":"browser-acceptance.json"}\n');
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
  assertLayoutMetrics,
  assertNoViewerPrefetch,
  assertSafeEvidence,
  assertViewportIntersection,
  browserRequestAllowed,
  createScreenshotDirectory,
  createTemporaryAccountLedger,
  disableTemporaryUsersAndSessions,
  inspectPngPixels,
  normalizeLoopbackOrigin,
  publishEvidence,
  readEnvironmentIdentity,
  resolveArtifactDirectory,
  resolveRuntimeDatabasePath,
  runRevocableSession,
  screenshotFilename,
  validateRequestLogList,
  validateRuntimeEvidence,
  validateRuntimeEvidenceStrict,
  verifySessionCleanup,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}

