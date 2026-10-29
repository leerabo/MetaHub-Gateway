/**
 * PRD-053 PAGE-077/PAGE-078 成本异常检测真实浏览器验收。
 *
 * 脚本只连接调用方已启动的三个 HTTP 回环服务，并依赖同批次已通过的 Runtime 证据。
 * admin、developer、viewer 身份均经真实 Console 登录；密码只驻留进程内存，Token 只驻留
 * 浏览器认证槽位。最终 JSON、URL、Storage、终端输出和未遮罩截图不得包含密码、Token、
 * cursor、治理草稿、完整 Virtual Key、Prompt、Response 或 Provider Key。
 */

import { createHash, randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { inflateSync } from "node:zlib";


// Playwright 只从调用方提供的 NODE_PATH 解析，验收器不会安装、下载或改写任何依赖。
const require = createRequire(import.meta.url);
// 两档视口与 PRD-053 冻结验收口径一致，截图必须精确等于这里的宽高。
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
// 网络只允许本机回环地址；即使 DNS 指向本机，非显式回环主机也不能进入验收白名单。
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
// Runtime 资源只允许稳定不透明 ID，禁止把展示名或凭据伪装成 selector 输入。
const SAFE_IDENTIFIER_PATTERN = /^[A-Za-z0-9._:-]{1,160}$/;
// canonical micros 与倍率都以无前导零十进制字符串传输，浏览器不得借 Number 重新判定异常。
const CANONICAL_INTEGER_PATTERN = /^(?:0|[1-9][0-9]*)$/;
// URL 不能承载凭据、正文、cursor、治理草稿或幂等键；这些值只允许停留在页面闭包或请求头。
const SENSITIVE_URL_KEY_PATTERN = /password|token|secret|authorization|prompt|response|provider[_-]?key|api[_-]?key|cursor|idempotency|dismiss|duration|rpm|tpm|concurrency/i;
// 浏览器认证态只允许出现在这两个 sessionStorage 槽位，登出后必须同时清除。
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);
// 证据字段禁止为敏感正文预留槽位；virtual_key_id 是不透明资源 ID，属于明确允许项。
const FORBIDDEN_EVIDENCE_EXACT_KEYS = new Set([
  "body",
  "content",
  "endpoint",
  "endpoint_uri",
  "endpoint_url",
  "password",
  "prompt",
  "response",
  "secret",
  "token",
]);
// 正则补充覆盖复合字段名，防止凭据正文通过前后缀包装后进入 Runtime 或 Browser 证据。
const FORBIDDEN_EVIDENCE_KEY_PATTERN = /(?:^|_)(?:authorization|api_key|key_plaintext|key_prefix|key_secret|key_value|plaintext_key|provider_key|raw_key|response_body|response_content|response_payload)(?:_|$)/;
// 错误投影使用固定公开链路值，既能精确断言页面，又不会把真实服务端异常写入截图或证据。
const ERROR_REQUEST_ID = "req_prd053_browser_injected_unavailable";
const ERROR_TRACE_ID = "trc_prd053_browser_injected_unavailable";
// 真实列表响应只接受后端冻结的三类规则、四种状态和两个严重级别。
const RULE_TYPES = new Set(["tenant_spend_spike", "virtual_key_spend_spike", "virtual_key_off_hours"]);
const ANOMALY_STATUSES = new Set(["open", "acknowledged", "dismissed", "mitigated"]);
const SEVERITIES = new Set(["warning", "critical"]);


/** 只暴露固定 stage/code 的验收异常，避免 Playwright URL、DOM 或底层响应正文进入终端。 */
class BrowserAcceptanceFailure extends Error {
  /** 使用内部固定枚举构造公开错误；调用方不得把外部异常正文传入。 */
  constructor(stage, code) {
    super(`prd053_browser_acceptance_failed stage=${stage} code=${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 以固定阶段和错误码终止验收，不拼接任何不可信值。 */
function fail(stage, code) {
  throw new BrowserAcceptanceFailure(stage, code);
}


/** 执行失败关闭断言；condition 之外的信息不会进入异常。 */
function requireCondition(condition, stage, code) {
  if (!condition) fail(stage, code);
}


/** 把未知异常压缩为固定错误，已经脱敏的验收异常则保持原阶段。 */
function toSafeFailure(error, stage, code = "stage_failed") {
  // 已脱敏异常可安全保留首个失败阶段，不能用后继包装掩盖根因。
  if (error instanceof BrowserAcceptanceFailure) return error;
  return new BrowserAcceptanceFailure(stage, code);
}


/** 执行单个浏览器操作并把 Playwright 原始异常折叠为固定阶段码，避免泄露 URL 或 DOM。 */
async function runSafeBrowserOperation(stage, code, operation) {
  try {
    return await operation();
  } catch (error) {
    // 浏览器原始异常可能含 URL 或 DOM 内容，统一转换为固定阶段码后再向上抛出。
    throw toSafeFailure(error, stage, code);
  }
}


/** 规范化 HTTP 回环 origin，并拒绝凭据、路径、查询、fragment 和缺失端口。 */
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


/** 从当前脚本位置解析唯一工作区根，不接受环境变量改写信任边界。 */
function workspaceRoot() {
  const resolved = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  requireCondition(path.basename(resolved).toLowerCase() === "metahub", "config.workspace", "unexpected_root");
  return resolved;
}


/** 把证据目录硬限制为 artifacts/prd053 下单层、安全命名的批次目录。 */
function resolveArtifactDirectory(value, root = workspaceRoot()) {
  const artifactRoot = path.resolve(root, "artifacts", "prd053");
  const candidate = path.resolve(root, String(value));
  const relative = path.relative(artifactRoot, candidate);
  requireCondition(relative && !relative.startsWith("..") && !path.isAbsolute(relative), "config.artifact", "outside_root");
  requireCondition(!relative.includes(path.sep), "config.artifact", "batch_depth");
  requireCondition(/^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$/.test(relative), "config.artifact", "batch_name");
  return candidate;
}


/**
 * 从环境变量读取管理员身份，并在 Chrome 启动前删除相关变量。
 * 返回值只在当前 Node 进程内使用，不会进入最终证据。
 */
function readEnvironmentIdentity(env = process.env) {
  const adminPassword = String(env.METAHUB_PRD053_ADMIN_PASSWORD || env.METAHUB_ADMIN_PASSWORD || "");
  const tenantSlug = String(env.METAHUB_PRD053_TENANT_SLUG || env.METAHUB_TENANT_SLUG || "default").trim();
  const adminEmail = String(env.METAHUB_PRD053_ADMIN_EMAIL || env.METAHUB_ADMIN_EMAIL || "admin@metahub.local").trim().toLowerCase();
  for (const name of [
    "METAHUB_PRD053_ADMIN_PASSWORD",
    "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD053_ADMIN_EMAIL",
    "METAHUB_ADMIN_EMAIL",
    "METAHUB_PRD053_TENANT_SLUG",
    "METAHUB_TENANT_SLUG",
  ]) delete env[name];
  requireCondition(adminPassword.length >= 8, "config.identity", "admin_password_missing");
  requireCondition(Boolean(tenantSlug), "config.identity", "tenant_slug_missing");
  requireCondition(adminEmail.includes("@"), "config.identity", "admin_email_invalid");
  // 身份只返回当前调用栈并由最终敏感值扫描兜底，绝不能复制到浏览器验收证据。
  return { tenantSlug, adminEmail, adminPassword };
}


/** 解析非敏感命令行参数；任何未知、重复或缺值参数都失败关闭。 */
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
  const artifactValue = values.get("--artifact-dir") || env.METAHUB_PRD053_ARTIFACT_DIR || "";
  requireCondition(Boolean(artifactValue), "config.artifact", "missing");
  const root = workspaceRoot();
  return {
    root,
    artifactDirectory: resolveArtifactDirectory(artifactValue, root),
    consoleOrigin: normalizeLoopbackOrigin(
      values.get("--console-url") || env.METAHUB_PRD053_CONSOLE_URL || "http://localhost:5173",
      "console_origin",
    ),
    controlOrigin: normalizeLoopbackOrigin(
      values.get("--control-url") || env.METAHUB_PRD053_CONTROL_URL || "http://localhost:8001",
      "control_origin",
    ),
    gatewayOrigin: normalizeLoopbackOrigin(
      values.get("--gateway-url") || env.METAHUB_PRD053_GATEWAY_URL || "http://localhost:8000",
      "gateway_origin",
    ),
    chromePath: String(env.METAHUB_CHROME_PATH || "").trim(),
    headed: flags.has("--headed") || env.METAHUB_BROWSER_HEADED === "1",
    ...identity,
  };
}


/** 统一 camelCase、连字符和空白为小写下划线，防止敏感槽位借命名风格绕过。 */
function normalizeEvidenceKey(key) {
  return String(key)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}


/** 判断证据字段是否可能承载密码、密钥、正文、上游响应或 Endpoint 配置。 */
function isForbiddenEvidenceKey(key) {
  const normalized = normalizeEvidenceKey(key);
  // 不透明资源 ID 可用于验收关联，但任何明文值、前缀、配置或 secret 字段仍严格拒绝。
  if (["provider_key_id", "provider_key_create", "virtual_key_id", "top_virtual_key_id"].includes(normalized)) return false;
  if (/(?:^|_)(?:cursor|idempotency_key|dismiss_reason|duration_minutes|rpm_limit|tpm_limit|concurrency_limit)(?:_|$)/.test(normalized)) return true;
  if (FORBIDDEN_EVIDENCE_EXACT_KEYS.has(normalized)) return true;
  // 分词匹配阻断密码、Token 和正文类槽位，同时明确保留 request_id 等公共追踪字段。
  if (/(?:^|_)(?:password|token|secret|authorization|prompt)(?:_|$)/.test(normalized)) return true;
  return FORBIDDEN_EVIDENCE_KEY_PATTERN.test(normalized);
}


/** 递归拒绝 Runtime 与 Browser 证据中的敏感字段槽位。 */
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


/** 扫描序列化值中的常见凭据形态，补足仅检查字段名无法覆盖的伪装场景。 */
function assertSafeSerializedValues(value, stage = "evidence.privacy") {
  let encoded;
  try {
    encoded = JSON.stringify(value);
  } catch {
    fail(stage, "not_serializable");
  }
  requireCondition(!/Bearer\s+[^"\s]{8,}/i.test(encoded), stage, "bearer_value");
  requireCondition(!/\bmhv_[A-Za-z0-9_-]{16,}\b/.test(encoded), stage, "virtual_key_value");
  requireCondition(!/\bsk[-_][A-Za-z0-9_-]{12,}\b/i.test(encoded), stage, "provider_key_value");
}


/** 读取必需对象；错误只暴露固定字段类别。 */
function requiredObject(value, stage, code) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), stage, code);
  return value;
}


/** 读取并校验一个不透明资源 ID，绝不把原值拼入错误。 */
function requiredIdentifier(value, stage, code) {
  const normalized = String(value ?? "");
  requireCondition(SAFE_IDENTIFIER_PATTERN.test(normalized), stage, code);
  return normalized;
}


/** 校验 canonical 非负整数字符串并返回原文，避免大金额或倍率经 Number 丢失精度。 */
function canonicalInteger(value, stage = "contract.integer", code = "integer_invalid") {
  const normalized = String(value ?? "");
  requireCondition(CANONICAL_INTEGER_PATTERN.test(normalized), stage, code);
  try {
    BigInt(normalized);
  } catch {
    fail(stage, code);
  }
  return normalized;
}


/** 校验 Runtime 公开 HTTP 摘要，链路 ID、状态和可选错误码必须互相一致。 */
function validateRuntimeHttpEvidence(value, expectedStatus, stage, expectedCode = null) {
  const evidence = requiredObject(value, stage, "missing");
  requireCondition(evidence.status === expectedStatus, stage, "status_mismatch");
  requiredIdentifier(evidence.request_id, stage, "request_id_missing");
  requiredIdentifier(evidence.trace_id, stage, "trace_id_missing");
  if (expectedCode !== null) requireCondition(evidence.code === expectedCode, stage, "code_mismatch");
  return evidence;
}


/**
 * 校验同批次 Runtime 证据并只投影 Browser 所需的不透明资源 ID。
 * Runtime 必须已经证明 detector、Webhook、后端 RBAC、临时限流 429 与 release 恢复，Browser
 * 不能用仅有页面节点的结果替代这些后端事实。
 */
function validateRuntimeEvidence(payload) {
  const root = requiredObject(payload, "runtime_evidence.load", "not_object");
  assertSafeKeys(root, "runtime_evidence.privacy");
  assertSafeSerializedValues(root, "runtime_evidence.privacy");
  requireCondition(root.schema_version === 1, "runtime_evidence.load", "schema_version");
  requireCondition(root.status === "passed" && root.prd_id === "PRD-053", "runtime_evidence.load", "not_passed");

  const checks = requiredObject(root.checks, "runtime_evidence.checks", "missing");
  const cleanup = requiredObject(checks.cleanup, "runtime_evidence.cleanup", "missing");
  requireCondition(
    checks.all_sessions_revoked === true
      && cleanup.session_revoked === true
      && cleanup.admin_session_revoked === true
      && cleanup.developer_session_revoked === true
      && cleanup.viewer_session_revoked === true,
    "runtime_evidence.cleanup",
    "sessions_not_revoked",
  );
  requireCondition(checks.detection?.detector_generated_anomaly === true, "runtime_evidence.detection", "not_generated");
  requireCondition(checks.event?.webhook_hmac_valid === true, "runtime_evidence.event", "hmac_unverified");
  requireCondition(checks.event?.webhook_payload_field_count === 20, "runtime_evidence.event", "payload_contract");
  requireCondition(
    checks.permissions?.admin_read_allowed === true
      && checks.permissions?.anonymous_denied === true
      && checks.permissions?.developer_denied === true
      && checks.permissions?.viewer_denied === true,
    "runtime_evidence.permissions",
    "rbac_unverified",
  );
  requireCondition(
    checks.apply?.temporary_limit_active === true
      && checks.apply?.idempotent_replay_verified === true,
    "runtime_evidence.apply",
    "apply_unverified",
  );
  requireCondition(
    checks.release?.temporary_limit_released === true
      && checks.release?.mitigated_history_preserved === true,
    "runtime_evidence.release",
    "release_unverified",
  );
  requireCondition(
    checks.gateway_blocked?.status === 429
      && checks.gateway_blocked?.code === "gateway.rate_limit_exceeded"
      && checks.gateway_blocked?.provider_attempt_count_zero === true,
    "runtime_evidence.gateway_blocked",
    "rate_limit_unverified",
  );
  requireCondition(
    checks.gateway_restored?.status === 200
      && checks.gateway_restored?.release_restored_gateway === true
      && checks.gateway_restored?.provider_attempt_recorded === true,
    "runtime_evidence.gateway_restored",
    "restore_unverified",
  );

  const http = requiredObject(root.http, "runtime_evidence.http", "missing");
  validateRuntimeHttpEvidence(http.anomalies_admin_list, 200, "runtime_evidence.http.admin");
  validateRuntimeHttpEvidence(http.anomalies_developer_list, 403, "runtime_evidence.http.developer", "auth.permission_denied");
  validateRuntimeHttpEvidence(http.anomalies_viewer_list, 403, "runtime_evidence.http.viewer", "auth.permission_denied");
  validateRuntimeHttpEvidence(http.anomaly_developer_patch, 403, "runtime_evidence.http.developer_patch", "auth.permission_denied");
  validateRuntimeHttpEvidence(http.anomaly_viewer_patch, 403, "runtime_evidence.http.viewer_patch", "auth.permission_denied");
  const gatewayBlockedHttp = validateRuntimeHttpEvidence(
    http.gateway_rate_limited,
    429,
    "runtime_evidence.http.gateway_blocked",
    "gateway.rate_limit_exceeded",
  );
  const gatewayRestoredHttp = validateRuntimeHttpEvidence(
    http.gateway_after_release,
    200,
    "runtime_evidence.http.gateway_restored",
  );
  requireCondition(
    gatewayBlockedHttp.request_id === checks.gateway_blocked.request_id
      && gatewayBlockedHttp.trace_id === checks.gateway_blocked.trace_id
      && gatewayRestoredHttp.request_id === checks.gateway_restored.request_id
      && gatewayRestoredHttp.trace_id === checks.gateway_restored.trace_id,
    "runtime_evidence.http.gateway_chain",
    "summary_mismatch",
  );

  const resources = requiredObject(root.generated_resources, "runtime_evidence.resources", "missing");
  const runtime = {
    tenantId: requiredIdentifier(resources.tenant_id, "runtime_evidence.resources", "tenant_id"),
    environmentId: requiredIdentifier(resources.environment_id, "runtime_evidence.resources", "environment_id"),
    projectId: requiredIdentifier(resources.project_id, "runtime_evidence.resources", "project_id"),
    applicationId: requiredIdentifier(resources.application_id, "runtime_evidence.resources", "application_id"),
    virtualKeyId: requiredIdentifier(resources.virtual_key_id, "runtime_evidence.resources", "virtual_key_id"),
    anomalyId: requiredIdentifier(resources.cost_anomaly_id, "runtime_evidence.resources", "anomaly_id"),
    overrideId: requiredIdentifier(
      resources.temporary_rate_limit_override_id,
      "runtime_evidence.resources",
      "override_id",
    ),
  };
  requireCondition(/^can_[0-9a-f]{32}$/.test(runtime.anomalyId), "runtime_evidence.resources", "anomaly_id_shape");
  return runtime;
}

/** 从同批次目录读取 runtime-acceptance.json，并执行完整冻结合同校验。 */
async function loadRuntimeEvidence(artifactDirectory) {
  const target = path.join(artifactDirectory, "runtime-acceptance.json");
  requireCondition(existsSync(target), "runtime_evidence.load", "missing");
  let payload;
  try {
    payload = JSON.parse((await readFile(target, "utf8")).replace(/^\uFEFF/, ""));
  } catch {
    fail("runtime_evidence.load", "invalid_json");
  }
  return validateRuntimeEvidence(payload);
}


/** 判断浏览器请求是否只访问冻结的三个回环 origin，且 URL 不携带敏感查询键。 */
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
 * 安装浏览器网络和运行时错误门禁。
 * 只累计数量，不保存 URL、Console 文本、请求头或响应正文。
 */
async function installBrowserGuards(context, page, config) {
  const allowedOrigins = new Set([config.consoleOrigin, config.controlOrigin, config.gatewayOrigin]);
  const summary = {
    allowed_request_count: 0,
    blocked_request_count: 0,
    browser_error_count: 0,
    page_error_count: 0,
    console_error_count: 0,
    expected_http_console_error_count: 0,
  };
  const expectedHttpStatuses = [];
  Object.defineProperty(summary, "expectHttpConsoleError", {
    enumerable: false,
    value(status) {
      requireCondition(status === 503, "browser.runtime", "expected_status_invalid");
      expectedHttpStatuses.push(status);
    },
  });
  await context.route("**/*", async (route) => {
    // 放行结论只依赖冻结 origin 和无敏感查询键，不能根据页面来源或请求头临时扩大边界。
    if (browserRequestAllowed(route.request().url(), allowedOrigins)) {
      summary.allowed_request_count += 1;
      await route.continue();
      return;
    }
    summary.blocked_request_count += 1;
    await route.abort("blockedbyclient");
  });
  page.on("pageerror", () => {
    summary.page_error_count += 1;
    summary.browser_error_count += 1;
  });
  page.on("console", (message) => {
    // 非错误级别消息与验收失败无关，提前返回也避免读取可能含业务内容的正文。
    if (message.type() !== "error") return;
    // 只把验收器主动登记的固定 503 资源模板视为预期；文本仅瞬时比较，绝不进入证据或日志。
    const messageText = String(message.text());
    const expectedIndex = expectedHttpStatuses.findIndex(
      (status) => messageText.startsWith(
        `Failed to load resource: the server responded with a status of ${status}`,
      ),
    );
    if (expectedIndex >= 0) {
      expectedHttpStatuses.splice(expectedIndex, 1);
      summary.expected_http_console_error_count += 1;
      return;
    }
    summary.console_error_count += 1;
    summary.browser_error_count += 1;
  });
  return summary;
}


/**
 * 使用真实 Console 表单建立指定角色 Session。
 * 登录响应中的 Token 始终由页面认证模块写入 sessionStorage，验收器只读取状态、公开 request_id
 * 和渲染后的角色标签，避免认证材料穿过浏览器边界进入 Node。
 */
async function loginThroughConsole(page, config, identity, onAuthenticated) {
  await page.goto(`${config.consoleOrigin}/`, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const form = page.locator("#login-form");
  await form.waitFor({ state: "visible", timeout: 30_000 });
  await form.locator('[name="tenantSlug"]').fill(config.tenantSlug);
  await form.locator('[name="email"]').fill(identity.email);
  await form.locator('[name="password"]').fill(identity.password);
  const loginResponse = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === config.controlOrigin
      && target.pathname === "/auth/login"
      && response.request().method() === "POST";
  }, { timeout: 30_000 });
  await form.locator('button[type="submit"]').click();
  const response = await loginResponse;
  requireCondition(response.status() === 200, `browser.login.${identity.role}`, "status_mismatch");
  onAuthenticated?.();
  const requestId = requiredIdentifier(
    response.headers()["x-request-id"],
    `browser.login.${identity.role}`,
    "request_id_missing",
  );
  await page.locator("#logout-button").waitFor({ state: "visible", timeout: 45_000 });
  const userMenuText = await page.locator(".user-menu").innerText();
  requireCondition(userMenuText.includes(identity.role), `browser.login.${identity.role}`, "role_not_rendered");
  const anomalyNavigationCount = await page.locator('[data-console-nav="cost-anomalies"]').count();
  requireCondition(
    identity.role === "admin" ? anomalyNavigationCount === 1 : anomalyNavigationCount === 0,
    `browser.login.${identity.role}`,
    "navigation_rbac",
  );
  return {
    http_status: 200,
    request_id: requestId,
    role: identity.role,
    cost_anomaly_navigation_visible: identity.role === "admin",
  };
}

/** 安全读取 JSON 响应；解析失败不传播正文或 URL。 */
async function readJsonResponse(response, stage) {
  try {
    const payload = await response.json();
    return requiredObject(payload, stage, "response_not_object");
  } catch (error) {
    // 已知验收错误已安全脱敏；JSON 解析错误则统一压缩，不能泄露响应正文。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    fail(stage, "response_not_json");
  }
}


/** 判断响应是否是指定 Control API 的 GET，查询值只在内存中比较。 */
function isControlGet(response, controlOrigin, pathname, virtualKeyId = null) {
  let target;
  try {
    target = new URL(response.url());
  } catch {
    return false;
  }
  if (target.origin !== controlOrigin || target.pathname !== pathname || response.request().method() !== "GET") {
    return false;
  }
  return virtualKeyId === null || target.searchParams.get("virtual_key_id") === virtualKeyId;
}


/**
 * 校验真实 GET /cost-anomalies 的安全 DTO，并锁定 Runtime 生成且已 release 的目标事实。
 * 所有 tenant_id 必须等于当前同批次租户，金额和倍率保持 canonical 字符串，前端不得重算检测结论。
 */
function validateAnomalyApiProjection(payload, runtime) {
  const document = requiredObject(payload, "anomaly_api.document", "not_object");
  const anomalies = Array.isArray(document.cost_anomalies) ? document.cost_anomalies : [];
  requireCondition(anomalies.length > 0, "anomaly_api.document", "items_missing");
  requireCondition(Number.isSafeInteger(document.total) && document.total >= anomalies.length, "anomaly_api.document", "total_invalid");
  requiredIdentifier(document.request_id, "anomaly_api.document", "request_id_missing");
  requiredIdentifier(document.trace_id, "anomaly_api.document", "trace_id_missing");

  for (const anomaly of anomalies) {
    requireCondition(anomaly && typeof anomaly === "object" && !Array.isArray(anomaly), "anomaly_api.item", "not_object");
    requireCondition(anomaly.tenant_id === runtime.tenantId, "anomaly_api.item", "tenant_mismatch");
    requireCondition(/^can_[0-9a-f]{32}$/.test(String(anomaly.cost_anomaly_id ?? "")), "anomaly_api.item", "id_shape");
    requireCondition(RULE_TYPES.has(anomaly.rule_type), "anomaly_api.item", "rule_type");
    requireCondition(ANOMALY_STATUSES.has(anomaly.status), "anomaly_api.item", "status");
    requireCondition(SEVERITIES.has(anomaly.severity), "anomaly_api.item", "severity");
    for (const field of [
      "current_cost_micros",
      "baseline_total_micros",
      "baseline_average_micros",
      "threshold_multiplier_millis",
      "minimum_cost_micros",
    ]) canonicalInteger(anomaly[field], "anomaly_api.item", `${field}_invalid`);
    // 非工作时段规则没有“当前值/基线值”的倍率语义，因此后端会返回 null；
    // 其他规则必须保留 canonical 整数字符串，避免把缺失倍率误判为合法异常。
    if (anomaly.observed_ratio_millis === null) {
      requireCondition(
        anomaly.rule_type === "virtual_key_off_hours",
        "anomaly_api.item",
        "observed_ratio_millis_invalid",
      );
    } else {
      canonicalInteger(anomaly.observed_ratio_millis, "anomaly_api.item", "observed_ratio_millis_invalid");
    }
  }

  const matches = anomalies.filter((item) => item.cost_anomaly_id === runtime.anomalyId);
  requireCondition(matches.length === 1, "anomaly_api.target", "count");
  const target = matches[0];
  requireCondition(target.environment_id === runtime.environmentId, "anomaly_api.target", "environment");
  requireCondition(target.scope_type === "virtual_key", "anomaly_api.target", "scope_type");
  requireCondition(target.scope_id === runtime.virtualKeyId, "anomaly_api.target", "scope_id");
  requireCondition(target.virtual_key_id === runtime.virtualKeyId, "anomaly_api.target", "virtual_key");
  requireCondition(target.status === "mitigated", "anomaly_api.target", "status");
  const override = requiredObject(target.temporary_rate_limit_override, "anomaly_api.target", "override_missing");
  requireCondition(
    override.temporary_rate_limit_override_id === runtime.overrideId
      && override.effective_status === "released",
    "anomaly_api.target",
    "release_not_visible",
  );
  const actions = Array.isArray(target.actions) ? target.actions : [];
  requireCondition(
    actions.some((item) => item?.action_type === "apply_temporary_rate_limit")
      && actions.some((item) => item?.action_type === "release_temporary_rate_limit"),
    "anomaly_api.target",
    "history_incomplete",
  );
  const actionable = anomalies.find((item) => (
    (item.status === "open" || item.status === "acknowledged")
    && item.scope_type === "virtual_key"
    && item.mitigation_eligible === true
    && item.temporary_rate_limit_override?.effective_status !== "active"
  ));
  requireCondition(Boolean(actionable), "anomaly_api.target", "actionable_missing");
  const summary = requiredObject(document.summary, "anomaly_api.summary", "missing");
  requireCondition(["healthy", "degraded", "idle"].includes(summary.detector_status), "anomaly_api.summary", "detector_status");
  for (const field of ["open_count", "critical_count", "mitigated_count", "active_override_count"]) {
    requireCondition(Number.isSafeInteger(summary[field]) && summary[field] >= 0, "anomaly_api.summary", `${field}_invalid`);
  }
  return {
    target,
    actionableId: actionable.cost_anomaly_id,
    evidence: {
      tenant_qualified_items: true,
      canonical_amounts_verified: true,
      detector_summary_verified: true,
      released_mitigation_visible: true,
      immutable_action_history_visible: true,
      visible_anomaly_count: anomalies.length,
      request_id: document.request_id,
      trace_id: document.trace_id,
    },
  };
}


/** 把 GET 响应投影为不含正文的公开 HTTP 证据，并核对 Header 与正文链路 ID。 */
function responseEvidence(response, payload, stage) {
  const requestId = requiredIdentifier(response.headers()["x-request-id"], stage, "request_id_missing");
  const traceId = requiredIdentifier(response.headers()["x-trace-id"], stage, "trace_id_missing");
  requireCondition(payload.request_id === requestId && payload.trace_id === traceId, stage, "chain_mismatch");
  requireCondition(String(response.headers()["cache-control"] ?? "").includes("no-store"), stage, "no_store_missing");
  return { http_status: response.status(), request_id: requestId, trace_id: traceId, no_store: true };
}


/** 统计页面发出的成本异常 GET；计数器不保存 URL、Header、响应或 Session。 */
function installCostAnomalyRequestCounter(page, config) {
  const tracker = { get_count: 0 };
  page.on("request", (request) => {
    try {
      const target = new URL(request.url());
      if (
        target.origin === config.controlOrigin
        && target.pathname === "/cost-anomalies"
        && request.method() === "GET"
      ) tracker.get_count += 1;
    } catch {
      // 非法 URL 已由总网络门禁阻断；计数器保持纯数字，不能传播解析异常。
    }
  });
  return tracker;
}


/** 点击后端授权的成本异常导航，校验真实 URL、响应和 PAGE-077/078 首屏 DOM。 */
async function openCostAnomalyPage(page, config, runtime) {
  const responsePromise = page.waitForResponse(
    (response) => isControlGet(response, config.controlOrigin, "/cost-anomalies"),
    { timeout: 30_000 },
  );
  await page.locator('[data-console-nav="cost-anomalies"]').click();
  const response = await responsePromise;
  requireCondition(response.status() === 200, "browser.anomaly_api", "status_mismatch");
  const payload = await readJsonResponse(response, "browser.anomaly_api");
  const projection = validateAnomalyApiProjection(payload, runtime);
  const http = responseEvidence(response, payload, "browser.anomaly_api");
  await page.locator("#cost-anomaly-title").waitFor({ state: "visible", timeout: 30_000 });
  await page.locator("#cost-anomaly-list-title").waitFor({ state: "visible", timeout: 20_000 });
  await page.locator("#cost-anomaly-alerts-title").waitFor({ state: "visible", timeout: 20_000 });
  const location = new URL(page.url());
  requireCondition(location.origin === config.consoleOrigin, "browser.navigation", "origin");
  requireCondition(location.pathname === "/" && location.search === "", "browser.navigation", "url_shape");
  requireCondition(location.hash === "#cost-anomalies", "browser.navigation", "hash");
  return { projection, apiEvidence: { ...http, ...projection.evidence } };
}


/** 选择 Runtime 目标异常并核对列表、详情、输入质量、release 状态和动作历史。 */
async function verifyCostAnomalyDom(page, runtime) {
  const listButton = page.locator(
    `.cost-anomaly-list [data-cost-anomaly-select="${runtime.anomalyId}"]`,
  );
  requireCondition(await listButton.count() === 1, "browser.page077", "target_row_count");
  await listButton.click();
  await page.locator("#cost-anomaly-detail-title").waitFor({ state: "visible", timeout: 20_000 });
  requireCondition(await listButton.getAttribute("aria-pressed") === "true", "browser.page077", "target_not_selected");
  const detailText = await page.locator(".cost-anomaly-detail").innerText();
  requireCondition(detailText.includes(runtime.anomalyId), "browser.page077", "anomaly_id_missing");
  requireCondition(detailText.includes(runtime.environmentId), "browser.page077", "environment_missing");
  requireCondition(detailText.includes(runtime.virtualKeyId), "browser.page077", "virtual_key_missing");
  requireCondition(detailText.includes("输入质量"), "browser.page077", "input_quality_missing");
  requireCondition(detailText.includes("已解除"), "browser.page077", "release_status_missing");
  requireCondition(detailText.includes("应用临时限流") && detailText.includes("解除临时限流"), "browser.page077", "history_missing");
  requireCondition(await page.locator(".cost-anomaly-alert-card").count() >= 1, "browser.page078", "alert_card_missing");
  return {
    page077_list_visible: true,
    page077_detail_visible: true,
    page078_alert_card_visible: true,
    runtime_target_selected: true,
    input_quality_visible: true,
    released_mitigation_visible: true,
    action_history_visible: true,
  };
}


/**
 * 打开真实可处置 Key 告警的临时限流表单，验证完整字段、双视口可见性与草稿销毁。
 * 表单不提交，后端动作主流程已由同批次 Runtime 验收完成；截图会遮罩输入值且证据不保存草稿。
 */
async function verifyAdminActionDialog(page, actionableId, config, sensitiveValues) {
  const selectButton = page.locator(`.cost-anomaly-list [data-cost-anomaly-select="${actionableId}"]`);
  requireCondition(await selectButton.count() === 1, "browser.action_dialog", "actionable_row_missing");
  await runSafeBrowserOperation("browser.action_dialog", "selection_click_failed", () => selectButton.click());
  // 选择详情会触发一次异步 GET 和整页重绘；旧详情标题本来就可见，不能拿它当完成信号。
  // 必须等待目标行的选中状态与目标异常专属动作同时出现，避免在旧 DOM 上产生竞态误报。
  const button = page.locator(
    `[data-cost-anomaly-action="apply_temporary_rate_limit"][data-cost-anomaly-id="${actionableId}"]`,
  );
  await runSafeBrowserOperation(
    "browser.action_dialog",
    "selection_refresh_timeout",
    () => page.waitForFunction(
      (anomalyId) => (
        document.querySelector(`.cost-anomaly-list [data-cost-anomaly-select="${anomalyId}"]`)?.getAttribute("aria-pressed") === "true"
        && document.querySelector(
          `[data-cost-anomaly-action="apply_temporary_rate_limit"][data-cost-anomaly-id="${anomalyId}"]`,
        ) !== null
      ),
      actionableId,
      { timeout: 20_000 },
    ),
  );
  await runSafeBrowserOperation("browser.action_dialog", "action_visibility_timeout", () => button.waitFor({ state: "visible", timeout: 20_000 }));
  await runSafeBrowserOperation("browser.action_dialog", "action_click_failed", () => button.click());
  const dialog = page.locator("#cost-anomaly-action-dialog");
  await runSafeBrowserOperation("browser.action_dialog", "dialog_visibility_timeout", () => dialog.waitFor({ state: "visible", timeout: 20_000 }));
  const form = dialog.locator("#cost-anomaly-action-form");
  for (const fieldName of ["durationMinutes", "rpmLimit", "tpmLimit", "concurrencyLimit"]) {
    requireCondition(
      await form.locator(`[name="${fieldName}"]`).count() === 1,
      "browser.action_dialog",
      "temporary_limit_field_missing",
    );
  }
  for (const forbiddenName of [
    "tenant_id",
    "actor_user_id",
    "status",
    "expires_at",
    "idempotency_key",
    "operation_request_id",
    "operation_trace_id",
  ]) requireCondition(await form.locator(`[name="${forbiddenName}"]`).count() === 0, "browser.action_dialog", "service_field_present");

  const capture = await runSafeBrowserOperation(
    "browser.action_dialog",
    "capture_failed",
    () => captureState(
      page,
      config,
      "action-dialog",
      "#cost-anomaly-action-dialog",
      sensitiveValues,
      true,
    ),
  );
  const draftValue = "98765431";
  await form.locator('[name="rpmLimit"]').fill(draftValue);
  await page.locator("#cost-anomaly-action-cancel").click();
  await dialog.waitFor({ state: "detached", timeout: 20_000 });
  const residual = await page.evaluate((value) => {
    const values = [document.documentElement.innerHTML];
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key) values.push(`${key}=${storage.getItem(key) ?? ""}`);
      }
    }
    return values.filter((item) => item.includes(value)).length;
  }, draftValue);
  requireCondition(residual === 0, "browser.action_dialog", "draft_not_destroyed");
  return {
    evidence: {
      admin_actions_visible: true,
      temporary_limit_form_visible: true,
      service_owned_fields_absent: true,
      cancel_destroyed_draft: true,
      viewport_contained: true,
    },
    capture,
  };
}


/** 在管理员 Session 内经公开 /users API 创建本轮 developer/viewer 登录身份。 */
async function createRestrictedRoleUsers(page, config) {
  const suffix = randomBytes(8).toString("hex");
  const credentials = [];
  const creationEvidence = {};
  for (const role of ["developer", "viewer"]) {
    const email = `prd053-browser-${role}-${suffix}@metahub.local`;
    const password = `Prd053Browser!${randomBytes(18).toString("hex")}`;
    const requestId = `req_prd053_browser_create_${role}_${suffix}`;
    const traceId = `trc_prd053_browser_create_${role}_${suffix}`;
    const result = await page.evaluate(async (input) => {
      const access = sessionStorage.getItem("metahub.auth_token");
      if (!access) return { status: 0 };
      const response = await fetch(`${input.controlOrigin}/users`, {
        method: "POST",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${access}`,
          "Content-Type": "application/json",
          "X-Request-ID": input.requestId,
          "X-Trace-ID": input.traceId,
        },
        body: JSON.stringify({
          email: input.email,
          display_name: `PRD053 browser ${input.role}`,
          password: input.password,
          role: input.role,
          status: "active",
        }),
      });
      const payload = await response.json().catch(() => ({}));
      return {
        status: response.status,
        requestId: response.headers.get("X-Request-ID"),
        traceId: response.headers.get("X-Trace-ID"),
        userId: payload?.user?.user_id ?? "",
        role: payload?.user?.role ?? "",
      };
    }, { controlOrigin: config.controlOrigin, role, email, password, requestId, traceId });
    requireCondition(result.status === 201, `browser.rbac.${role}`, "create_status");
    requireCondition(result.requestId === requestId && result.traceId === traceId, `browser.rbac.${role}`, "create_chain");
    requireCondition(result.role === role, `browser.rbac.${role}`, "create_role");
    requiredIdentifier(result.userId, `browser.rbac.${role}`, "user_id_missing");
    credentials.push({ role, email, password });
    creationEvidence[`${role}_created`] = true;
  }
  return { credentials, evidence: creationEvidence };
}


/**
 * 使用真实低权限 Session 手写 #cost-anomalies 并刷新，验证导航、页面 DOM 与 API 预取同时缺失。
 * 后端 403 由 Runtime 证据负责；这里锁定前端不得把隐藏按钮误当成唯一边界，也不得泄露页面结构。
 */
async function verifyRestrictedRoleUi(page, config, identity, tracker, lifecycle) {
  const login = await loginThroughConsole(page, config, identity, lifecycle.onAuthenticated);
  const before = tracker.get_count;
  await page.goto(`${config.consoleOrigin}/#cost-anomalies`, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await page.locator("#logout-button").waitFor({ state: "visible", timeout: 45_000 });
  requireCondition(await page.locator('[data-console-nav="cost-anomalies"]').count() === 0, `browser.rbac.${identity.role}`, "navigation_visible");
  requireCondition(await page.locator("#cost-anomaly-title").count() === 0, `browser.rbac.${identity.role}`, "page_visible");
  requireCondition(await page.locator(".cost-anomaly-alert-card").count() === 0, `browser.rbac.${identity.role}`, "card_visible");
  const active = await page.locator(".console-shell").getAttribute("data-active-navigation");
  requireCondition(active !== "cost-anomalies", `browser.rbac.${identity.role}`, "active_navigation");
  // 外壳会先恢复 Session，再异步读取后端裁剪导航；必须等待清理完成，不能把短暂旧 hash 误判为越权。
  try {
    await page.waitForFunction(
      () => window.location.hash !== "#cost-anomalies",
      null,
      { timeout: 20_000 },
    );
  } catch { fail(`browser.rbac.${identity.role}`, "hash_not_sanitized"); }
  requireCondition(tracker.get_count === before, `browser.rbac.${identity.role}`, "api_prefetched");
  // 每个低权限 Session 都单独扫描 Storage，证明角色口令和治理状态没有因手写 hash 落入持久槽位。
  assertBrowserPrivacy(
    await readPrivacyMetrics(page, [identity.password]),
    `browser.rbac.${identity.role}.privacy`,
  );
  const logout = await logoutThroughConsole(page, config, lifecycle.onRevoked);
  return {
    role: identity.role,
    login,
    navigation_hidden: true,
    manual_hash_sanitized: true,
    protected_dom_absent: true,
    api_prefetch_count: 0,
    storage_sensitive_fields_absent: true,
    logout,
  };
}


/** 注入一次固定 503 响应，证明 PAGE-077 会同时展示统一错误码和公开 request_id。 */
async function verifyUnifiedErrorDisplay(page, config, network) {
  const overview = page.locator('[data-console-nav="overview"]');
  await overview.click();
  await page.locator('.console-shell[data-active-navigation="overview"]').waitFor({ state: "visible", timeout: 20_000 });
  let intercepted = false;
  const handler = async (route) => {
    const request = route.request();
    let target;
    try {
      target = new URL(request.url());
    } catch {
      await route.fallback();
      return;
    }
    if (!intercepted && target.origin === config.controlOrigin && target.pathname === "/cost-anomalies" && request.method() === "GET") {
      intercepted = true;
      network.expectHttpConsoleError(503);
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        headers: {
          "Access-Control-Allow-Origin": config.consoleOrigin,
          "Cache-Control": "no-store",
          "X-Request-ID": ERROR_REQUEST_ID,
          "X-Trace-ID": ERROR_TRACE_ID,
        },
        body: JSON.stringify({
          code: "cost_anomaly.unavailable",
          message: "成本异常服务暂不可用",
          request_id: ERROR_REQUEST_ID,
          trace_id: ERROR_TRACE_ID,
        }),
      });
      return;
    }
    await route.fallback();
  };
  await page.route("**/*", handler);
  try {
    await page.locator('[data-console-nav="cost-anomalies"]').click();
    const error = page.locator(".cost-anomaly-error");
    await error.waitFor({ state: "visible", timeout: 30_000 });
    const text = await error.innerText();
    requireCondition(intercepted, "browser.error_projection", "request_not_intercepted");
    requireCondition(text.includes("cost_anomaly.unavailable"), "browser.error_projection", "code_missing");
    requireCondition(text.includes(ERROR_REQUEST_ID), "browser.error_projection", "request_id_missing");
    requireCondition(!text.includes(ERROR_TRACE_ID), "browser.error_projection", "trace_id_exposed");
  } finally {
    await page.unroute("**/*", handler);
  }
  return {
    http_status: 503,
    code: "cost_anomaly.unavailable",
    request_id: ERROR_REQUEST_ID,
    unified_error_visible: true,
    raw_response_hidden: true,
  };
}

/** 等待两个动画帧，使响应式样式和滚动定位在测量前稳定。 */
async function settleLayout(page) {
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}


/**
 * 测量 PAGE-077/078 的 document、主框架、控件和关键证据区域。
 * 仅返回数值计数；DOM 文本、异常 ID、request_id 和表单值不会离开页面上下文。
 */
async function readLayoutMetrics(page, selector) {
  return page.evaluate((rootSelector) => {
    const root = document.querySelector(rootSelector);
    if (!root) return null;
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rectangle = element.getBoundingClientRect();
      return style.display !== "none"
        && style.visibility !== "hidden"
        && Number(style.opacity || "1") > 0
        && rectangle.width > 1
        && rectangle.height > 1;
    };
    /** 检查任一横向裁切祖先，防止移动端看似无 document overflow 但内容被 hidden 吞掉。 */
    const horizontallyClippedByAncestor = (element) => {
      const rectangle = element.getBoundingClientRect();
      for (let ancestor = element.parentElement; ancestor && ancestor !== document.body; ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        if (["hidden", "clip"].includes(style.overflowX)) {
          const parent = ancestor.getBoundingClientRect();
          if (rectangle.left < parent.left - 1 || rectangle.right > parent.right + 1) return true;
        }
        if (ancestor === root) break;
      }
      return false;
    };
    const rootRectangle = root.getBoundingClientRect();
    const controls = [...root.querySelectorAll("button,input,select,textarea")].filter(visible);
    const viewportControls = controls.filter((element) => {
      const rectangle = element.getBoundingClientRect();
      return rectangle.bottom > 0 && rectangle.top < innerHeight;
    });
    let controlOverlapCount = 0;
    for (let left = 0; left < viewportControls.length; left += 1) {
      for (let right = left + 1; right < viewportControls.length; right += 1) {
        const aElement = viewportControls[left];
        const bElement = viewportControls[right];
        if (aElement.contains(bElement) || bElement.contains(aElement)) continue;
        const a = aElement.getBoundingClientRect();
        const b = bElement.getBoundingClientRect();
        if (
          Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2
          && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2
        ) controlOverlapCount += 1;
      }
    }
    const clippedControlCount = controls.filter((element) => {
      const rectangle = element.getBoundingClientRect();
      return rectangle.left < -1 || rectangle.right > innerWidth + 1 || horizontallyClippedByAncestor(element);
    }).length;
    const content = [...root.querySelectorAll([
      ".cost-anomaly-summary > *",
      ".cost-anomaly-alert-card",
      ".cost-anomaly-filter-form > label",
      ".cost-anomaly-list-item",
      ".cost-anomaly-detail-list > div",
      ".cost-anomaly-mitigation dl > div",
      ".cost-anomaly-actions",
    ].join(","))].filter(visible);
    const clippedContentCount = content.filter(horizontallyClippedByAncestor).length;
    const horizontalContainers = [...root.querySelectorAll([
      ".cost-anomaly-summary",
      ".cost-anomaly-alert-grid",
      ".cost-anomaly-filter-form",
      ".cost-anomaly-workspace",
      ".cost-anomaly-detail-list",
      ".cost-anomaly-limit-grid",
    ].join(","))].filter(visible);
    const containerOverflowCount = horizontalContainers.filter(
      (element) => element.scrollWidth > element.clientWidth + 1,
    ).length;
    const regions = [...root.querySelectorAll([
      ".cost-anomaly-heading",
      ".cost-anomaly-summary",
      ".cost-anomaly-alerts",
      ".cost-anomaly-page",
    ].join(","))].filter(visible);
    let regionOverlapCount = 0;
    for (let left = 0; left < regions.length; left += 1) {
      for (let right = left + 1; right < regions.length; right += 1) {
        const a = regions[left].getBoundingClientRect();
        const b = regions[right].getBoundingClientRect();
        if (
          Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2
          && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2
        ) regionOverlapCount += 1;
      }
    }
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
      clipped_content_count: clippedContentCount,
      horizontal_container_overflow_count: containerOverflowCount,
      control_overlap_count: controlOverlapCount,
      region_overlap_count: regionOverlapCount,
    };
  }, selector);
}

/** 把原始布局指标冻结为全零问题证据；任何横向溢出、裁切或重叠都阻止通过。 */
function assertLayoutMetrics(metrics, viewport, stage = "browser.layout") {
  requireCondition(metrics && typeof metrics === "object", stage, "root_missing");
  requireCondition(metrics.viewport_width === viewport.width, stage, "viewport_width");
  requireCondition(metrics.viewport_height === viewport.height, stage, "viewport_height");
  requireCondition(metrics.document_scroll_width <= viewport.width + 1, stage, "document_overflow");
  requireCondition(metrics.body_scroll_width <= viewport.width + 1, stage, "body_overflow");
  requireCondition(metrics.root_left >= -1 && metrics.root_right <= viewport.width + 1, stage, "root_overflow");
  requireCondition(metrics.root_scroll_width <= metrics.root_client_width + 1, stage, "root_scroll_overflow");
  requireCondition(metrics.clipped_control_count === 0, stage, "control_clipped");
  requireCondition(metrics.clipped_content_count === 0, stage, "content_clipped");
  requireCondition(metrics.horizontal_container_overflow_count === 0, stage, "container_overflow");
  requireCondition(metrics.control_overlap_count === 0, stage, "control_overlap");
  requireCondition(metrics.region_overlap_count === 0, stage, "region_overlap");
  return {
    horizontal_overflow: false,
    clipped_control_count: 0,
    clipped_content_count: 0,
    overlap_count: 0,
  };
}


/** 证明截图锚点与当前视口相交，避免把离屏空白误作目标状态截图。 */
function assertViewportIntersection(box, viewport, stage = "browser.layout") {
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

/** 证明目标框完整落在当前视口内；仅相交不足以证明底部确认按钮可操作。 */
function assertViewportContainment(box, viewport, stage = "browser.layout") {
  assertViewportIntersection(box, viewport, stage);
  requireCondition(
    box.x >= -1
      && box.y >= -1
      && box.x + box.width <= viewport.width + 1
      && box.y + box.height <= viewport.height + 1,
    stage,
    "anchor_not_contained",
  );
  return { anchor_fully_contained: true };
}


/**
 * 在页面上下文扫描 DOM、URL 和两类 Storage。
 * 认证 Token 只做页面内布尔比较；返回值仅含计数，任何 Storage 原文都不能穿过浏览器边界。
 */
async function readPrivacyMetrics(page, sensitiveValues = []) {
  return page.evaluate(({ knownSensitive, storageKeys }) => {
    const html = document.documentElement?.outerHTML ?? "";
    const text = document.documentElement?.innerText ?? "";
    const url = new URL(window.location.href);
    const sessionToken = sessionStorage.getItem(storageKeys[0]);
    const entries = [];
    for (const [storageName, storage] of [["local", localStorage], ["session", sessionStorage]]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key) entries.push({ storageName, key, value: String(storage.getItem(key) ?? "") });
      }
    }
    const forbiddenPattern = /password|secret|authorization|prompt|response|provider[_-]?key|api[_-]?key|cursor|idempotency|dismiss|duration|rpm|tpm|concurrency/i;
    // DOM 可展示“Provider Key”等安全标签；属性门禁只拒绝本 PRD 明确要求驻留内存的治理状态。
    const ephemeralDomPattern = /cursor|idempotency|dismiss_reason|duration_minutes|rpm_limit|tpm_limit|concurrency_limit/i;
    const sensitiveQueryCount = [...url.searchParams.keys()].filter((key) => forbiddenPattern.test(key)).length;
    const forbiddenStorageKeyCount = entries.filter(
      ({ key }) => !storageKeys.includes(key) && forbiddenPattern.test(key),
    ).length;
    let forbiddenAuthContextFieldCount = 0;
    const contextEntry = entries.find(({ key }) => key === storageKeys[1]);
    if (contextEntry) {
      try {
        // 递归只检查字段名，不把 auth context 的用户、租户或 Session 值复制到返回结果。
        const walk = (value) => {
          if (Array.isArray(value)) {
            for (const child of value) walk(child);
            return;
          }
          if (!value || typeof value !== "object") return;
          for (const [key, child] of Object.entries(value)) {
            // auth context 不应包含凭据、游标或治理草稿；命中时只累加计数，原字段和值都留在页面内。
            if (forbiddenPattern.test(key)) forbiddenAuthContextFieldCount += 1;
            walk(child);
          }
        };
        walk(JSON.parse(contextEntry.value));
      } catch {
        forbiddenAuthContextFieldCount += 1;
      }
    }
    const storageText = entries.map(({ key, value }) => `${key}=${value}`).join("\n");
    const combinedVisible = `${html}\n${text}\n${url.href}`;
    const knownSensitiveHitCount = knownSensitive.filter(
      (value) => value && (combinedVisible.includes(value) || storageText.includes(value)),
    ).length;
    const attributes = [...document.querySelectorAll("*")].flatMap((element) => (
      [...element.attributes].map((attribute) => `${attribute.name}=${attribute.value}`)
    ));
    return {
      password_input_count: document.querySelectorAll('input[type="password"]').length,
      full_virtual_key_pattern_count: ((`${combinedVisible}\n${storageText}`).match(/\bmhv_[A-Za-z0-9_-]{16,}\b/g) ?? []).length,
      provider_key_pattern_count: ((`${combinedVisible}\n${storageText}`).match(/\bsk[-_][A-Za-z0-9_-]{12,}\b/gi) ?? []).length,
      bearer_pattern_count: (combinedVisible.match(/Bearer\s+[^"\s]{8,}/gi) ?? []).length,
      sensitive_query_count: sensitiveQueryCount,
      forbidden_dom_attribute_count: attributes.filter((value) => ephemeralDomPattern.test(value)).length,
      forbidden_storage_key_count: forbiddenStorageKeyCount,
      forbidden_auth_context_field_count: forbiddenAuthContextFieldCount,
      known_sensitive_hit_count: knownSensitiveHitCount,
      local_storage_entry_count: entries.filter(({ storageName }) => storageName === "local").length,
      local_auth_slot_count: storageKeys.filter((key) => localStorage.getItem(key) !== null).length,
      session_auth_slot_count: storageKeys.filter((key) => sessionStorage.getItem(key) !== null).length,
      session_token_present: typeof sessionToken === "string" && sessionToken.length > 0,
      session_token_exposed: Boolean(sessionToken) && combinedVisible.includes(sessionToken),
    };
  }, { knownSensitive: sensitiveValues.filter(Boolean), storageKeys: AUTH_STORAGE_KEYS });
}


/** 把隐私扫描压缩为稳定通过证据，任一额外 Storage 或敏感命中都失败关闭。 */
function assertBrowserPrivacy(metrics, stage = "browser.privacy") {
  requireCondition(metrics && typeof metrics === "object", stage, "metrics_missing");
  requireCondition(metrics.password_input_count === 0, stage, "password_input_present");
  requireCondition(metrics.full_virtual_key_pattern_count === 0, stage, "virtual_key_exposed");
  requireCondition(metrics.provider_key_pattern_count === 0, stage, "provider_key_exposed");
  requireCondition(metrics.bearer_pattern_count === 0, stage, "bearer_exposed");
  requireCondition(metrics.sensitive_query_count === 0, stage, "sensitive_query");
  requireCondition(metrics.forbidden_dom_attribute_count === 0, stage, "forbidden_dom_attribute");
  requireCondition(metrics.forbidden_storage_key_count === 0, stage, "forbidden_storage_key");
  requireCondition(metrics.forbidden_auth_context_field_count === 0, stage, "forbidden_auth_context_field");
  requireCondition(metrics.known_sensitive_hit_count === 0, stage, "known_sensitive_value");
  requireCondition(metrics.local_storage_entry_count === 0 && metrics.local_auth_slot_count === 0, stage, "local_storage_not_empty");
  requireCondition(metrics.session_auth_slot_count === AUTH_STORAGE_KEYS.length, stage, "session_auth_incomplete");
  requireCondition(metrics.session_token_present === true && metrics.session_token_exposed === false, stage, "session_token_invalid");
  return {
    sensitive_dom_hits: 0,
    sensitive_url_hits: 0,
    forbidden_storage_hits: 0,
    auth_storage_contract_valid: true,
  };
}

/** PNG Paeth 预测器只用于复原截图像素，不输出图像内容。 */
function paethPredictor(left, up, upperLeft) {
  const estimate = left + up - upperLeft;
  const leftDistance = Math.abs(estimate - left);
  const upDistance = Math.abs(estimate - up);
  const upperLeftDistance = Math.abs(estimate - upperLeft);
  if (leftDistance <= upDistance && leftDistance <= upperLeftDistance) return left;
  if (upDistance <= upperLeftDistance) return up;
  return upperLeft;
}


/**
 * 解析 Playwright 的 8-bit RGB/RGBA PNG，精确核对视口尺寸并证明像素非透明、非单色。
 * 返回值只含尺寸、字节数、哈希和布尔结论，不保存像素样本或 OCR 文本。
 */
function inspectPngPixels(buffer, viewport, stage = "browser.screenshot") {
  requireCondition(Buffer.isBuffer(buffer), stage, "not_buffer");
  requireCondition(buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])), stage, "png_signature");
  let offset = 8;
  let header = null;
  const compressed = [];
  while (offset + 12 <= buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.subarray(offset + 4, offset + 8).toString("ascii");
    const dataStart = offset + 8;
    const dataEnd = dataStart + length;
    requireCondition(dataEnd + 4 <= buffer.length, stage, "png_chunk_bounds");
    const data = buffer.subarray(dataStart, dataEnd);
    if (type === "IHDR") {
      requireCondition(length === 13, stage, "png_header_length");
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
    offset = dataEnd + 4;
    if (type === "IEND") break;
  }
  requireCondition(header && compressed.length > 0, stage, "png_chunks_missing");
  requireCondition(
    header.bitDepth === 8
      && [2, 6].includes(header.colorType)
      && header.compression === 0
      && header.filter === 0
      && header.interlace === 0,
    stage,
    "png_encoding",
  );
  requireCondition(header.width === viewport.width && header.height === viewport.height, stage, "png_dimensions");
  requireCondition(buffer.length <= 50 * 1024 * 1024, stage, "png_size_limit");
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
  let visiblePixelFound = header.colorType === 2;
  let visualVariance = false;
  const firstColor = pixels.subarray(0, 3);
  for (let pixelOffset = 0; pixelOffset < pixels.length; pixelOffset += channels) {
    if (channels === 4 && pixels[pixelOffset + 3] > 0) visiblePixelFound = true;
    if (
      pixels[pixelOffset] !== firstColor[0]
      || pixels[pixelOffset + 1] !== firstColor[1]
      || pixels[pixelOffset + 2] !== firstColor[2]
    ) {
      visualVariance = true;
      break;
    }
  }
  requireCondition(visiblePixelFound && visualVariance, stage, "png_blank");
  return {
    width: header.width,
    height: header.height,
    bytes: buffer.length,
    sha256: createHash("sha256").update(buffer).digest("hex"),
    non_blank_pixels: true,
  };
}


/** 收集截图遮罩；顶部身份与动作输入草稿都不属于可持久化成本异常证据。 */
async function screenshotMasks(page) {
  const locators = [];
  for (const selector of [
    ".user-menu",
    ".cost-anomaly-action-dialog input",
    ".cost-anomaly-action-dialog select",
  ]) {
    const matches = page.locator(selector);
    const count = await matches.count();
    for (let index = 0; index < count; index += 1) {
      if (await matches.nth(index).isVisible()) locators.push(matches.nth(index));
    }
  }
  return locators;
}


/** 生成冻结截图相对路径；状态名和视口名都必须来自安全字符集合。 */
function screenshotFilename(stateName, viewportName) {
  requireCondition(/^[a-z0-9][a-z0-9-]{2,48}$/.test(String(stateName)), "browser.screenshot", "state_name");
  requireCondition(VIEWPORTS.some((item) => item.name === viewportName), "browser.screenshot", "viewport_name");
  return `screenshots/${stateName}-${viewportName}.png`;
}


/**
 * 在两档视口对真实页面状态执行布局、Storage 隐私、像素和独占截图验收。
 * requireFullContainment 只用于必须完整可操作的弹窗，返回每档布局指标与脱敏 PNG 摘要。
 */
async function captureState(
  page,
  config,
  stateName,
  anchorSelector,
  sensitiveValues = [],
  requireFullContainment = false,
) {
  const screenshots = [];
  const layouts = {};
  for (const viewport of VIEWPORTS) {
    const stage = `browser.capture.${stateName}.${viewport.name}`;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const anchor = page.locator(anchorSelector).first();
    await anchor.waitFor({ state: "visible", timeout: 20_000 });
    await anchor.scrollIntoViewIfNeeded();
    await settleLayout(page);
    const anchorBox = await anchor.boundingBox();
    layouts[viewport.name] = {
      ...assertLayoutMetrics(
        await readLayoutMetrics(page, requireFullContainment ? anchorSelector : "body"),
        viewport,
        stage,
      ),
      ...assertViewportIntersection(anchorBox, viewport, stage),
      ...(requireFullContainment ? assertViewportContainment(anchorBox, viewport, stage) : {}),
    };
    assertBrowserPrivacy(await readPrivacyMetrics(page, sensitiveValues), stage);
    const buffer = await page.screenshot({
      fullPage: false,
      animations: "disabled",
      caret: "hide",
      mask: await screenshotMasks(page),
      maskColor: "#64748b",
    });
    const inspected = inspectPngPixels(buffer, viewport, stage);
    assertBrowserPrivacy(await readPrivacyMetrics(page, sensitiveValues), stage);
    const relative = screenshotFilename(stateName, viewport.name);
    try {
      await writeFile(path.join(config.artifactDirectory, relative), buffer, { flag: "wx" });
    } catch {
      fail(stage, "screenshot_write_failed");
    }
    screenshots.push({ state: stateName, viewport: viewport.name, file: relative, ...inspected });
  }
  return { screenshots, layouts };
}

/** 验证登出响应的状态、统一链路字段和两种浏览器 Storage 清理结果。 */
function verifyLogoutEvidence({ status, headers, payload, storageState }) {
  const requestId = requiredIdentifier(headers?.["x-request-id"], "browser.logout", "request_id_missing");
  const traceId = requiredIdentifier(headers?.["x-trace-id"], "browser.logout", "trace_id_missing");
  requireCondition(status === 200, "browser.logout", "status_mismatch");
  requireCondition(payload && typeof payload === "object" && !Array.isArray(payload), "browser.logout", "response_not_object");
  requireCondition(payload.logged_out === true, "browser.logout", "not_revoked");
  requireCondition(payload.request_id === requestId && payload.trace_id === traceId, "browser.logout", "chain_mismatch");
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


/** 通过真实登出按钮撤销管理员 Session，并证明认证槽位已经清空。 */
async function logoutThroughConsole(page, config, onRevoked) {
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
  if (response.status() === 200 && payload?.logged_out === true) onRevoked?.();
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30_000 });
  const storageState = await page.evaluate((keys) => ({
    localStorage: keys.map((key) => localStorage.getItem(key) === null),
    sessionStorage: keys.map((key) => sessionStorage.getItem(key) === null),
  }), AUTH_STORAGE_KEYS);
  return verifyLogoutEvidence({
    status: response.status(),
    headers: response.headers(),
    payload,
    storageState,
  });
}


/**
 * 异常路径在页面内部撤销 Session。
 * Token 只从 sessionStorage 读出并直接发往回环 Control API，返回 Node 的只有清理布尔值。
 */
async function revokeSessionAfterFailure(page, config) {
  try {
    return await page.evaluate(async ({ controlOrigin, storageKeys }) => {
      const storages = [sessionStorage, localStorage];
      const token = sessionStorage.getItem(storageKeys[0]);
      // 缺少 Token 时无法证明服务端撤销，异常路径必须失败关闭。
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
      // 只有服务端明确撤销后才清理本地槽位，避免用“本地消失”伪装 Session 已失效。
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


/** 递归扫描最终 Browser JSON，并拒绝环境身份、Bearer 和常见完整 Key 形态。 */
function assertSafeEvidence(value, sensitiveValues = []) {
  assertSafeKeys(value, "browser_evidence.privacy");
  assertSafeSerializedValues(value, "browser_evidence.privacy");
  const encoded = JSON.stringify(value);
  for (const sensitive of sensitiveValues) {
    requireCondition(!sensitive || !encoded.includes(String(sensitive)), "browser_evidence.privacy", "sensitive_value");
  }
}


/** 独占写入 browser-evidence.json；旧证据存在时绝不覆盖。 */
async function publishBrowserEvidence(artifactDirectory, evidence) {
  const target = path.join(artifactDirectory, "browser-evidence.json");
  try {
    await writeFile(target, `${JSON.stringify(evidence, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
  } catch {
    fail("evidence.publish", "output_exists_or_unwritable");
  }
  return target;
}


/**
 * 执行 PRD-053 真实浏览器主流程。
 * 同一 Context 依次使用 admin/developer/viewer/admin Session，每次切换都先真实登出；任一阶段失败
 * 都由 finally 撤销当前活动 Session，并在关闭浏览器后才允许独占发布证据。
 */
async function executeBrowserAcceptance(config) {
  const runtime = await loadRuntimeEvidence(config.artifactDirectory);
  requireCondition(!existsSync(path.join(config.artifactDirectory, "browser-evidence.json")), "evidence.publish", "output_exists");
  const screenshotsDirectory = path.join(config.artifactDirectory, "screenshots");
  try {
    await mkdir(screenshotsDirectory, { recursive: false });
  } catch {
    let entries;
    try {
      entries = await readdir(screenshotsDirectory);
    } catch {
      fail("evidence.screenshots", "directory_unreadable");
    }
    // Runtime/编排器可以预建同批次空目录；任何既有文件都可能属于旧验收，必须拒绝覆盖或混批。
    requireCondition(entries.length === 0, "evidence.screenshots", "directory_not_empty");
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
  let restrictedCredentials = [];
  const lifecycle = {
    onAuthenticated() {
      sessionNeedsRevocation = true;
    },
    onRevoked() {
      sessionNeedsRevocation = false;
    },
  };
  const adminIdentity = {
    role: "admin",
    email: config.adminEmail,
    password: config.adminPassword,
  };
  try {
    browser = await chromium.launch({
      ...(config.chromePath ? { executablePath: config.chromePath } : {}),
      headless: !config.headed,
    });
    context = await browser.newContext({ viewport: VIEWPORTS[0] });
    page = await context.newPage();
    const network = await installBrowserGuards(context, page, config);
    const tracker = installCostAnomalyRequestCounter(page, config);

    await loginThroughConsole(page, config, adminIdentity, lifecycle.onAuthenticated);
    const restricted = await createRestrictedRoleUsers(page, config);
    restrictedCredentials = restricted.credentials;
    await logoutThroughConsole(page, config, lifecycle.onRevoked);
    const rbac = {};
    for (const identity of restrictedCredentials) {
      rbac[identity.role] = await verifyRestrictedRoleUi(page, config, identity, tracker, lifecycle);
    }

    const login = await loginThroughConsole(page, config, adminIdentity, lifecycle.onAuthenticated);
    const opened = await openCostAnomalyPage(page, config, runtime);
    const sensitiveValues = [config.adminPassword, ...restrictedCredentials.map((item) => item.password)];
    const listCapture = await captureState(
      page,
      config,
      "page077-list",
      "#cost-anomaly-list-title",
      sensitiveValues,
    );
    const alertCapture = await captureState(
      page,
      config,
      "page078-alerts",
      "#cost-anomaly-alerts-title",
      sensitiveValues,
    );
    const dom = await verifyCostAnomalyDom(page, runtime);
    const detailCapture = await captureState(
      page,
      config,
      "page077-detail",
      "#cost-anomaly-detail-title",
      sensitiveValues,
    );
    const adminAction = await verifyAdminActionDialog(
      page,
      opened.projection.actionableId,
      config,
      sensitiveValues,
    );
    const errorProjection = await verifyUnifiedErrorDisplay(page, config, network);
    const errorCapture = await captureState(
      page,
      config,
      "request-id-error",
      ".cost-anomaly-error",
      sensitiveValues,
    );
    await page.locator('[data-console-nav="overview"]').click();
    await page.locator('.console-shell[data-active-navigation="overview"]').waitFor({ state: "visible", timeout: 20_000 });
    await openCostAnomalyPage(page, config, runtime);
    const logout = await logoutThroughConsole(page, config, lifecycle.onRevoked);

    requireCondition(network.blocked_request_count === 0, "browser.network", "blocked_request");
    requireCondition(network.browser_error_count === 0, "browser.runtime", "browser_error");
    result = {
      schema_version: 1,
      status: "passed",
      prd_id: "PRD-053",
      checks: {
        login,
        role_users: restricted.evidence,
        rbac,
        anomaly_api: opened.apiEvidence,
        anomaly_dom: dom,
        admin_action_dialog: adminAction.evidence,
        unified_error: errorProjection,
        logout,
      },
      viewports: VIEWPORTS.map(({ name, width, height }) => ({ name, width, height })),
      layouts: {
        page077_list: listCapture.layouts,
        page078_alerts: alertCapture.layouts,
        page077_detail: detailCapture.layouts,
        action_dialog: adminAction.capture.layouts,
        request_id_error: errorCapture.layouts,
      },
      screenshots: [
        ...listCapture.screenshots,
        ...alertCapture.screenshots,
        ...detailCapture.screenshots,
        ...adminAction.capture.screenshots,
        ...errorCapture.screenshots,
      ],
      network,
      privacy: {
        environment_only_admin_credentials: true,
        generated_role_credentials_memory_only: true,
        runtime_projection_safe: true,
        screenshot_identity_masked: true,
        dom_url_storage_scanned: true,
        ephemeral_governance_state_verified: true,
        safe_projection_verified: true,
      },
      cleanup: { session_revoked: logout.logged_out, browser_closed: true },
    };
    assertSafeEvidence(result, [
      config.adminPassword,
      config.adminEmail,
      ...restrictedCredentials.flatMap((item) => [item.email, item.password]),
    ]);
  } catch (error) {
    failure = toSafeFailure(error, "browser.execute", "unexpected_failure");
  } finally {
    try {
      // 成功登录后立刻转移清理责任；截图、RBAC 或证据失败都不能遗留当前角色 Session。
      if (sessionNeedsRevocation && page) {
        const sessionRevoked = await revokeSessionAfterFailure(page, config);
        sessionNeedsRevocation = !sessionRevoked;
        // 服务端未明确撤销时必须阻止 passed；已有主失败仅追加固定清理码，避免覆盖首个失败阶段。
        if (!sessionRevoked) {
          // 主失败已存在时只附加固定枚举，不能拼入注销响应、URL 或底层异常正文。
          if (failure) failure.cleanupCode = "session_revoke_failed";
          // 主流程原本无错误时，Session 残留本身就是必须失败关闭的验收结果。
          else failure = new BrowserAcceptanceFailure("browser.cleanup", "session_revoke_failed");
        }
      }
      if (context) await context.close();
      if (browser) await browser.close();
    } catch {
      if (!failure) failure = new BrowserAcceptanceFailure("browser.cleanup", "close_failed");
    }
    // 明文凭据仅驻留当前进程；清理引用不能替代服务端注销，但可缩短失败后的内存生命周期。
    restrictedCredentials = [];
  }
  if (failure) throw failure;
  requireCondition(result?.status === "passed", "browser.execute", "result_missing");
  await publishBrowserEvidence(config.artifactDirectory, result);
  return result;
}

/** 命令行入口成功时输出安全 JSON，失败时只输出固定 stage/code 和可选清理码。 */
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


// 单元测试 import 时只加载纯合同函数；仅直接执行脚本才连接回环服务并启动 Chrome。
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}


export {
  AUTH_STORAGE_KEYS,
  BrowserAcceptanceFailure,
  VIEWPORTS,
  assertBrowserPrivacy,
  assertLayoutMetrics,
  assertSafeEvidence,
  assertViewportContainment,
  assertViewportIntersection,
  browserRequestAllowed,
  canonicalInteger,
  inspectPngPixels,
  isForbiddenEvidenceKey,
  loadRuntimeEvidence,
  normalizeLoopbackOrigin,
  publishBrowserEvidence,
  readEnvironmentIdentity,
  resolveArtifactDirectory,
  revokeSessionAfterFailure,
  screenshotFilename,
  toSafeFailure,
  validateAnomalyApiProjection,
  validateRuntimeEvidence,
  verifyLogoutEvidence,
};