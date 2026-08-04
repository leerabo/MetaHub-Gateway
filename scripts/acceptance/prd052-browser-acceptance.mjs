/**
 * PRD-052 PAGE-076 预算中心真实浏览器验收。
 *
 * 脚本只连接调用方已启动的三个 HTTP 回环服务，并严格依赖同批次、已通过的 Runtime 证据。
 * 管理员密码只驻留进程内存，Token 只驻留浏览器认证槽位；最终 JSON、URL、终端输出和未遮罩
 * 截图都不得包含密码、Token、完整 Virtual Key、Prompt、Response 或 Provider Key。
 */

import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { inflateSync } from "node:zlib";


// Playwright 只从调用方提供的 NODE_PATH 解析，验收器不会安装、下载或改写任何依赖。
const require = createRequire(import.meta.url);
// 两档视口与 PRD-052 冻结验收口径一致，截图必须精确等于这里的宽高。
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
// 网络只允许本机回环地址；即使 DNS 指向本机，非显式回环主机也不能进入验收白名单。
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
// Runtime 资源只允许稳定不透明 ID，禁止把展示名或凭据伪装成 selector 输入。
const SAFE_IDENTIFIER_PATTERN = /^[A-Za-z0-9._:-]{1,160}$/;
// 金额沿用后端最多六位小数的 CNY 字符串合同，所有求和都转换为整数微元。
const SAFE_CNY_PATTERN = /^(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,6})?$/;
// URL 查询参数不能承载凭据、正文或上游响应；公共 request_id 不在禁止列表中。
const SENSITIVE_URL_KEY_PATTERN = /password|token|secret|authorization|prompt|response|provider[_-]?key|api[_-]?key/i;
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
// 作用链保留 Application 兼容层，但本 PRD 的预算规则仍只冻结 tenant/project/virtual_key 三层。
const EXPECTED_SCOPE_CHAIN = Object.freeze(["tenant", "project", "application", "virtual_key"]);


/** 只暴露固定 stage/code 的验收异常，避免 Playwright URL、DOM 或底层响应正文进入终端。 */
class BrowserAcceptanceFailure extends Error {
  /** 使用内部固定枚举构造公开错误；调用方不得把外部异常正文传入。 */
  constructor(stage, code) {
    super(`prd052_browser_acceptance_failed stage=${stage} code=${code}`);
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


/** 把证据目录硬限制为 artifacts/prd052 下单层、安全命名的批次目录。 */
function resolveArtifactDirectory(value, root = workspaceRoot()) {
  const artifactRoot = path.resolve(root, "artifacts", "prd052");
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
  const adminPassword = String(env.METAHUB_PRD052_ADMIN_PASSWORD || env.METAHUB_ADMIN_PASSWORD || "");
  const tenantSlug = String(env.METAHUB_PRD052_TENANT_SLUG || env.METAHUB_TENANT_SLUG || "default").trim();
  const adminEmail = String(env.METAHUB_PRD052_ADMIN_EMAIL || env.METAHUB_ADMIN_EMAIL || "admin@metahub.local").trim().toLowerCase();
  for (const name of [
    "METAHUB_PRD052_ADMIN_PASSWORD",
    "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD052_ADMIN_EMAIL",
    "METAHUB_ADMIN_EMAIL",
    "METAHUB_PRD052_TENANT_SLUG",
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
  const artifactValue = values.get("--artifact-dir") || env.METAHUB_PRD052_ARTIFACT_DIR || "";
  requireCondition(Boolean(artifactValue), "config.artifact", "missing");
  const root = workspaceRoot();
  return {
    root,
    artifactDirectory: resolveArtifactDirectory(artifactValue, root),
    consoleOrigin: normalizeLoopbackOrigin(
      values.get("--console-url") || env.METAHUB_PRD052_CONSOLE_URL || "http://localhost:5173",
      "console_origin",
    ),
    controlOrigin: normalizeLoopbackOrigin(
      values.get("--control-url") || env.METAHUB_PRD052_CONTROL_URL || "http://localhost:8001",
      "control_origin",
    ),
    gatewayOrigin: normalizeLoopbackOrigin(
      values.get("--gateway-url") || env.METAHUB_PRD052_GATEWAY_URL || "http://localhost:8000",
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


/** 把 CNY 十进制字符串精确转换为整数微元，用于无浮点误差的合同比较。 */
function cnyToMicros(value, stage = "contract.amount") {
  const normalized = String(value ?? "");
  requireCondition(SAFE_CNY_PATTERN.test(normalized), stage, "amount_invalid");
  const [whole, fraction = ""] = normalized.split(".");
  const micros = (BigInt(whole) * 1_000_000n) + BigInt(fraction.padEnd(6, "0"));
  requireCondition(micros <= 1_000_000_000_000_000n, stage, "amount_out_of_range");
  return micros;
}


/**
 * 校验 Runtime 证据冻结合同并投影 Browser 所需最小安全字段。
 * 证据必须已通过、已注销，且 Runtime 已证明三级摘要、可信作用链和 Project 阻断。
 */
function validateRuntimeEvidence(payload) {
  const root = requiredObject(payload, "runtime_evidence.load", "not_object");
  assertSafeKeys(root, "runtime_evidence.privacy");
  assertSafeSerializedValues(root, "runtime_evidence.privacy");
  requireCondition(root.status === "passed" && root.prd_id === "PRD-052", "runtime_evidence.load", "not_passed");
  requireCondition(root.cleanup?.session_revoked === true, "runtime_evidence.load", "cleanup_missing");
  requireCondition(root.privacy?.safe_projection_verified === true, "runtime_evidence.load", "privacy_missing");

  const resources = requiredObject(root.generated_resources, "runtime_evidence.resources", "missing");
  const ruleIds = requiredObject(resources.budget_rule_ids, "runtime_evidence.resources", "rule_ids_missing");
  const budgetStatus = requiredObject(root.checks?.budget_status, "runtime_evidence.checks", "budget_status_missing");
  const expectedSplit = requiredObject(budgetStatus.expected_split, "runtime_evidence.checks", "expected_split_missing");
  requireCondition(
    Array.isArray(budgetStatus.expected_split_scope_types)
      && budgetStatus.expected_split_scope_types.join(",") === "tenant,project,virtual_key",
    "runtime_evidence.checks",
    "expected_split_scope_types",
  );
  requireCondition(budgetStatus.three_level_summary_verified === true, "runtime_evidence.checks", "summary_unverified");
  requireCondition(budgetStatus.trusted_scope_chain_verified === true, "runtime_evidence.checks", "scope_chain_unverified");
  requireCondition(budgetStatus.project_exhausted_verified === true, "runtime_evidence.checks", "project_unverified");

  const gateway = requiredObject(root.checks?.gateway, "runtime_evidence.checks", "gateway_missing");
  requireCondition(gateway.http_status === 402, "runtime_evidence.checks", "gateway_status");
  requireCondition(gateway.code === "gateway.budget_exhausted", "runtime_evidence.checks", "gateway_code");
  requireCondition(gateway.project_rule_governed === true, "runtime_evidence.checks", "gateway_rule_unverified");
  requireCondition(gateway.provider_attempt_count_zero === true, "runtime_evidence.checks", "provider_attempted");
  requiredIdentifier(gateway.request_id, "runtime_evidence.checks", "gateway_request_id");
  requiredIdentifier(gateway.trace_id, "runtime_evidence.checks", "gateway_trace_id");

  const runtime = {
    tenantId: requiredIdentifier(resources.tenant_id, "runtime_evidence.resources", "tenant_id"),
    environmentId: requiredIdentifier(resources.environment_id, "runtime_evidence.resources", "environment_id"),
    projectId: requiredIdentifier(resources.project_id, "runtime_evidence.resources", "project_id"),
    applicationId: requiredIdentifier(resources.application_id, "runtime_evidence.resources", "application_id"),
    virtualKeyId: requiredIdentifier(resources.virtual_key_id, "runtime_evidence.resources", "virtual_key_id"),
    usageLedgerId: requiredIdentifier(resources.usage_ledger_id, "runtime_evidence.resources", "usage_ledger_id"),
    finopsLedgerEntryId: requiredIdentifier(resources.finops_ledger_entry_id, "runtime_evidence.resources", "finops_entry_id"),
    ruleIds: {
      tenant: requiredIdentifier(ruleIds.tenant, "runtime_evidence.resources", "tenant_rule_id"),
      project: requiredIdentifier(ruleIds.project, "runtime_evidence.resources", "project_rule_id"),
      virtual_key: requiredIdentifier(ruleIds.virtual_key, "runtime_evidence.resources", "virtual_key_rule_id"),
    },
    expectedSplit: {
      ledger_spent_cny: String(expectedSplit.ledger_spent_cny ?? ""),
      reserved_cny: String(expectedSplit.reserved_cny ?? ""),
      effective_spend_cny: String(expectedSplit.effective_spend_cny ?? ""),
    },
    expectedSplitScopeTypes: [...budgetStatus.expected_split_scope_types],
  };
  const uniqueIds = new Set(Object.values(runtime.ruleIds));
  requireCondition(uniqueIds.size === 3, "runtime_evidence.resources", "rule_ids_not_unique");
  const ledgerMicros = cnyToMicros(runtime.expectedSplit.ledger_spent_cny, "runtime_evidence.amount");
  const reservedMicros = cnyToMicros(runtime.expectedSplit.reserved_cny, "runtime_evidence.amount");
  const effectiveMicros = cnyToMicros(runtime.expectedSplit.effective_spend_cny, "runtime_evidence.amount");
  requireCondition(ledgerMicros + reservedMicros === effectiveMicros, "runtime_evidence.amount", "split_mismatch");
  return runtime;
}


/** 从同批次目录读取 runtime-evidence.json，并执行完整冻结合同校验。 */
async function loadRuntimeEvidence(artifactDirectory) {
  const target = path.join(artifactDirectory, "runtime-evidence.json");
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
  const summary = { allowed_request_count: 0, blocked_request_count: 0, browser_error_count: 0 };
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
    summary.browser_error_count += 1;
  });
  page.on("console", (message) => {
    // 只累计浏览器错误数量，不读取或保存可能包含业务响应和凭据的 Console 文本。
    if (message.type() === "error") summary.browser_error_count += 1;
  });
  return summary;
}


/** 使用真实 Console 登录表单建立管理员 Session，不读取登录响应中的 Token。 */
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
  const requestId = requiredIdentifier(response.headers()["x-request-id"], "browser.login", "request_id_missing");
  await page.locator('[data-console-nav="budgets"]').waitFor({ state: "visible", timeout: 45_000 });
  return { http_status: 200, request_id: requestId, admin_navigation_visible: true };
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


/** 校验 BudgetScope 的类型、ID、Environment 和稳定 scope ID。 */
/** 同时校验租户归属与可信父链，防止页面把不相关资源拼成虚假预算作用链。 */
function validateScope(scope, expected, runtime, stage) {
  const value = requiredObject(scope, stage, "scope_missing");
  requireCondition(value.scope_type === expected.type, stage, "scope_type");
  requireCondition(value.scope_id === expected.id, stage, "scope_id");
  requireCondition(value.environment_id === runtime.environmentId, stage, "scope_environment");
  requiredIdentifier(value.budget_scope_id, stage, "budget_scope_id");
  requireCondition(value.parent_scope_type === expected.parentType, stage, "parent_type");
  requireCondition(value.parent_scope_id === expected.parentId, stage, "parent_id");
  return value;
}


/**
 * 交叉校验 /budgets 与 Key 限定 /budget-status 的真实响应。
 * 三条 Runtime 规则必须逐一命中，可信链必须包含 Application 兼容层，Project 行金额必须与
 * Runtime 冻结拆分完全一致，且 active Project block 决定聚合阻断。
 */
function validateBudgetApiProjection(rulesPayload, statusPayload, runtime) {
  const rulesDocument = requiredObject(rulesPayload, "budget_api.rules", "not_object");
  const statusDocument = requiredObject(statusPayload, "budget_api.status", "not_object");
  const rules = Array.isArray(rulesDocument.budget_rules) ? rulesDocument.budget_rules : [];
  const statuses = Array.isArray(statusDocument.statuses) ? statusDocument.statuses : [];
  const specifications = [
    { type: "tenant", id: runtime.tenantId, ruleId: runtime.ruleIds.tenant },
    { type: "project", id: runtime.projectId, ruleId: runtime.ruleIds.project },
    { type: "virtual_key", id: runtime.virtualKeyId, ruleId: runtime.ruleIds.virtual_key },
  ];
  const matchedStatuses = new Map();
  for (const specification of specifications) {
    const matchingRules = rules.filter((item) => item?.budget_rule_id === specification.ruleId);
    requireCondition(matchingRules.length === 1, "budget_api.rules", "runtime_rule_count");
    const rule = matchingRules[0];
    requireCondition(rule.scope_type === specification.type && rule.scope_id === specification.id, "budget_api.rules", "runtime_rule_scope");
    requireCondition(rule.environment_id === runtime.environmentId, "budget_api.rules", "runtime_rule_environment");
    const matchingStatuses = statuses.filter((item) => item?.budget_rule_id === specification.ruleId);
    requireCondition(matchingStatuses.length === 1, "budget_api.status", "runtime_status_count");
    const status = matchingStatuses[0];
    requireCondition(status.scope_type === specification.type && status.scope_id === specification.id, "budget_api.status", "runtime_status_scope");
    requireCondition(status.environment_id === runtime.environmentId, "budget_api.status", "runtime_status_environment");
    matchedStatuses.set(specification.type, status);
  }

  const aggregate = requiredObject(statusDocument.aggregate, "budget_api.aggregate", "missing");
  const chain = Array.isArray(aggregate.scope_chain) ? aggregate.scope_chain : [];
  requireCondition(chain.length === EXPECTED_SCOPE_CHAIN.length, "budget_api.scope_chain", "length");
  requireCondition(chain.map((item) => item?.scope_type).join(",") === EXPECTED_SCOPE_CHAIN.join(","), "budget_api.scope_chain", "order");
  const expectedScopes = [
    { type: "tenant", id: runtime.tenantId, parentType: null, parentId: null },
    { type: "project", id: runtime.projectId, parentType: "tenant", parentId: runtime.tenantId },
    { type: "application", id: runtime.applicationId, parentType: "project", parentId: runtime.projectId },
    { type: "virtual_key", id: runtime.virtualKeyId, parentType: "application", parentId: runtime.applicationId },
  ];
  const validatedChain = chain.map((scope, index) => (
    validateScope(scope, expectedScopes[index], runtime, "budget_api.scope_chain")
  ));

  const projectStatus = matchedStatuses.get("project");
  requireCondition(projectStatus.status === "exhausted", "budget_api.project", "not_exhausted");
  requireCondition(projectStatus.next_action === "block", "budget_api.project", "not_blocking");
  requireCondition(projectStatus.block_status === "active", "budget_api.project", "block_not_active");
  requireCondition(aggregate.aggregate_status === "exhausted", "budget_api.aggregate", "not_exhausted");
  requireCondition(aggregate.effective_action === "block", "budget_api.aggregate", "not_blocking");
  for (const scopeType of runtime.expectedSplitScopeTypes) {
    const scopedStatus = matchedStatuses.get(scopeType);
    requireCondition(Boolean(scopedStatus), "budget_api.amounts", "scope_status_missing");
    for (const field of ["ledger_spent_cny", "reserved_cny", "effective_spend_cny"]) {
      requireCondition(scopedStatus[field] === runtime.expectedSplit[field], "budget_api.amounts", "split_value");
      cnyToMicros(scopedStatus[field], "budget_api.amounts");
    }
    requireCondition(
      cnyToMicros(scopedStatus.ledger_spent_cny, "budget_api.amounts")
        + cnyToMicros(scopedStatus.reserved_cny, "budget_api.amounts")
        === cnyToMicros(scopedStatus.effective_spend_cny, "budget_api.amounts"),
      "budget_api.amounts",
      "split_sum",
    );
  }
  return {
    evidence: {
      three_level_rules_verified: true,
      trusted_scope_chain_verified: true,
      application_compatibility_verified: true,
      formal_ledger_reservation_split_verified: true,
      project_exhausted_verified: true,
      aggregate_block_verified: true,
      scope_chain_length: validatedChain.length,
      visible_rule_count: rules.length,
    },
    domTargets: {
      ruleIds: { ...runtime.ruleIds },
      scopeIds: Object.fromEntries(validatedChain.map((item) => [item.scope_type, item.budget_scope_id])),
      expectedSplit: { ...runtime.expectedSplit },
    },
  };
}


/** 从响应头提取并校验统一 request_id，同时冻结 no-store 缓存边界。 */
function responseEvidence(response, stage) {
  const headers = response.headers();
  const requestId = requiredIdentifier(headers["x-request-id"], stage, "request_id_missing");
  requireCondition(String(headers["cache-control"] || "").toLowerCase().includes("no-store"), stage, "cache_control_missing");
  return { http_status: response.status(), request_id: requestId, cache_control_no_store: true };
}


/**
 * 进入预算中心并强制选择本批次 Runtime Key。
 * 即使 Runtime Key 恰好是默认项，也主动派发 change，确保所校验响应属于当前动作而非旧刷新。
 */
async function openBudgetCenter(page, config, runtime) {
  const initialRulesResponse = page.waitForResponse(
    (response) => isControlGet(response, config.controlOrigin, "/budgets"),
    { timeout: 30_000 },
  );
  const initialStatusResponse = page.waitForResponse(
    (response) => isControlGet(response, config.controlOrigin, "/budget-status"),
    { timeout: 30_000 },
  );
  await page.locator('[data-console-nav="budgets"]').click();
  const [initialRules, initialStatus] = await Promise.all([initialRulesResponse, initialStatusResponse]);
  requireCondition(initialRules.status() === 200 && initialStatus.status() === 200, "browser.budgets", "initial_status");
  await page.locator("#budgets-title").waitFor({ state: "visible", timeout: 30_000 });
  const keySelector = page.locator("#budget-scope-chain-key");
  await keySelector.waitFor({ state: "visible", timeout: 30_000 });
  const runtimeOptionCount = await keySelector.locator("option").evaluateAll(
    (options, value) => options.filter((option) => option.value === value).length,
    runtime.virtualKeyId,
  );
  requireCondition(runtimeOptionCount === 1, "browser.budgets", "runtime_key_option");

  const rulesResponsePromise = page.waitForResponse(
    (response) => isControlGet(response, config.controlOrigin, "/budgets"),
    { timeout: 30_000 },
  );
  const statusResponsePromise = page.waitForResponse(
    (response) => isControlGet(response, config.controlOrigin, "/budget-status", runtime.virtualKeyId),
    { timeout: 30_000 },
  );
  await keySelector.evaluate((element, value) => {
    element.value = value;
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }, runtime.virtualKeyId);
  const [rulesResponse, statusResponse] = await Promise.all([rulesResponsePromise, statusResponsePromise]);
  requireCondition(rulesResponse.status() === 200, "browser.budgets", "rules_status");
  requireCondition(statusResponse.status() === 200, "browser.budgets", "status_status");
  const [rulesPayload, statusPayload] = await Promise.all([
    readJsonResponse(rulesResponse, "browser.budgets.rules_json"),
    readJsonResponse(statusResponse, "browser.budgets.status_json"),
  ]);
  const projection = validateBudgetApiProjection(rulesPayload, statusPayload, runtime);
  await page.locator("#budget-rule-form").waitFor({ state: "visible", timeout: 30_000 });
  return {
    projection,
    apiEvidence: {
      rules: responseEvidence(rulesResponse, "browser.budgets.rules"),
      status: responseEvidence(statusResponse, "browser.budgets.status"),
    },
  };
}


/** 核对三条规则、四段可信作用链、Project 金额拆分和聚合阻断均真实呈现在 DOM 中。 */
async function verifyBudgetDom(page, projection) {
  requireCondition(await page.locator('.console-shell[data-active-navigation="budgets"]').count() === 1, "browser.dom", "navigation_not_active");
  requireCondition(await page.locator(".budget-aggregate").count() === 1, "browser.dom", "aggregate_missing");
  requireCondition((await page.locator(".budget-aggregate").innerText()).includes("阻断"), "browser.dom", "aggregate_block_missing");
  requireCondition(await page.locator('.budget-summary-section[aria-label="Environment 预算摘要"]').count() === 1, "browser.dom", "summary_missing");
  const ruleRows = page.locator(".budget-rule-row:not(.budget-rule-head):not(.budget-rule-empty)");
  for (const ruleId of Object.values(projection.domTargets.ruleIds)) {
    requireCondition(await ruleRows.filter({ hasText: ruleId }).count() === 1, "browser.dom", "runtime_rule_missing");
  }
  const chainRows = page.locator(".budget-chain-row:not(.budget-chain-head)");
  requireCondition(await chainRows.count() === EXPECTED_SCOPE_CHAIN.length, "browser.dom", "scope_chain_count");
  for (const scopeId of Object.values(projection.domTargets.scopeIds)) {
    requireCondition(await chainRows.filter({ hasText: scopeId }).count() === 1, "browser.dom", "scope_chain_row_missing");
  }
  const splitLabels = [
    ["正式入账", projection.domTargets.expectedSplit.ledger_spent_cny],
    ["预留", projection.domTargets.expectedSplit.reserved_cny],
    ["有效消耗", projection.domTargets.expectedSplit.effective_spend_cny],
  ];
  for (const scopeType of ["tenant", "project", "virtual_key"]) {
    const scopedRow = chainRows.filter({ hasText: projection.domTargets.scopeIds[scopeType] });
    for (const [label, value] of splitLabels) {
      const cell = scopedRow.locator(`[data-label="${label}"]`);
      requireCondition(await cell.count() === 1 && (await cell.innerText()).includes(`${value} CNY`), "browser.dom", "split_cell_missing");
    }
  }
  const projectRow = chainRows.filter({ hasText: projection.domTargets.scopeIds.project });
  requireCondition((await projectRow.innerText()).includes("已耗尽"), "browser.dom", "project_status_missing");
  return {
    runtime_rules_visible: true,
    scope_chain_visible: true,
    split_columns_visible: true,
    project_exhausted_visible: true,
    rendered_rule_count: await ruleRows.count(),
  };
}


/**
 * 验证管理员写表单覆盖三层 scope，并确认 Runtime Key 只通过不透明 ID 选择且自动对齐 Environment。
 * 函数不提交表单，真实 POST/PATCH 已由同批次 Runtime 验收负责。
 */
async function verifyAdminBudgetForm(page, runtime) {
  const form = page.locator("#budget-rule-form");
  await form.waitFor({ state: "visible", timeout: 20_000 });
  requireCondition(await form.locator('[name="tenant_id"], [name="actor"], [name="token"]').count() === 0, "browser.form", "service_field_present");
  const requiredControls = [
    "environmentId",
    "scopeType",
    "scopeId",
    "name",
    "dailyBudgetCny",
    "warningThresholdPercent",
    "exhaustedAction",
    "status",
  ];
  for (const name of requiredControls) {
    requireCondition(await form.locator(`[name="${name}"]`).count() === 1, "browser.form", "control_missing");
  }
  const scopeType = form.locator('[name="scopeType"]');
  const scopeId = form.locator('[name="scopeId"]');
  const environment = form.locator('[name="environmentId"]');
  const scopeTypes = await scopeType.locator("option").evaluateAll((options) => options.map((option) => option.value));
  requireCondition(["tenant", "project", "virtual_key"].every((value) => scopeTypes.includes(value)), "browser.form", "scope_type_missing");
  await scopeType.selectOption("virtual_key");
  const runtimeKeyOptionCount = await scopeId.locator("option").evaluateAll(
    (options, value) => options.filter((option) => option.value === value).length,
    runtime.virtualKeyId,
  );
  requireCondition(runtimeKeyOptionCount === 1, "browser.form", "runtime_key_missing");
  await scopeId.selectOption(runtime.virtualKeyId);
  requireCondition(await environment.inputValue() === runtime.environmentId, "browser.form", "environment_not_aligned");
  requireCondition(await form.locator('button[type="submit"]:not([disabled])').count() === 1, "browser.form", "submit_unavailable");
  requireCondition(await page.locator("[data-budget-mode-rule-id]").count() >= 3, "browser.form", "mode_controls_missing");
  return {
    visible: true,
    three_level_scope_options: true,
    service_owned_fields_absent: true,
    runtime_key_environment_aligned: true,
    submit_available: true,
    mode_controls_visible: true,
  };
}


/** 等待两个动画帧，使响应式样式和滚动定位在测量前稳定。 */
async function settleLayout(page) {
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}


/**
 * 测量 document/body/root 横向溢出、可见控件裁切、关键容器裁切及主要区域重叠。
 * 只返回数字和布尔量，不把 DOM 文本、属性值或业务 ID 带回 Node。
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
    /** 检测元素是否被横向 hidden/clip 祖先裁切，覆盖父容器吞掉表格右列的回归。 */
    const horizontallyClippedByAncestor = (element) => {
      const rectangle = element.getBoundingClientRect();
      for (let ancestor = element.parentElement; ancestor && ancestor !== document.body; ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        if (["hidden", "clip"].includes(style.overflowX)) {
          const ancestorRectangle = ancestor.getBoundingClientRect();
          if (rectangle.left < ancestorRectangle.left - 1 || rectangle.right > ancestorRectangle.right + 1) return true;
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
        const leftElement = viewportControls[left];
        const rightElement = viewportControls[right];
        if (leftElement.contains(rightElement) || rightElement.contains(leftElement)) continue;
        const a = leftElement.getBoundingClientRect();
        const b = rightElement.getBoundingClientRect();
        const horizontal = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const vertical = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (horizontal > 2 && vertical > 2) controlOverlapCount += 1;
      }
    }
    const clippedControlCount = controls.filter((element) => {
      const rectangle = element.getBoundingClientRect();
      return rectangle.left < -1
        || rectangle.right > innerWidth + 1
        || horizontallyClippedByAncestor(element);
    }).length;
    const contentSelectors = [
      ".budget-aggregate > div",
      ".budget-scope-summary",
      ".budget-chain-row > *",
      ".budget-rule-row > *",
      ".budget-rule-form > label",
      ".budget-actions",
    ];
    const content = [...root.querySelectorAll(contentSelectors.join(","))].filter(visible);
    const clippedContentCount = content.filter(horizontallyClippedByAncestor).length;
    const horizontalContainers = [...root.querySelectorAll([
      ".budget-aggregate",
      ".budget-scope-summary-grid",
      ".budget-chain-table",
      ".budget-rule-form",
      ".budget-rule-table",
    ].join(","))].filter(visible);
    const containerOverflowCount = horizontalContainers.filter(
      (element) => element.scrollWidth > element.clientWidth + 1,
    ).length;
    const regions = [...root.querySelectorAll([
      ".budget-heading",
      ".budget-aggregate",
      ".budget-summary-section",
      ".budget-chain-selector",
      ".budget-chain-section",
      ".budget-rule-form",
      ".budget-rule-table",
    ].join(","))].filter(visible);
    let regionOverlapCount = 0;
    for (let left = 0; left < regions.length; left += 1) {
      for (let right = left + 1; right < regions.length; right += 1) {
        const a = regions[left].getBoundingClientRect();
        const b = regions[right].getBoundingClientRect();
        const horizontal = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const vertical = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (horizontal > 2 && vertical > 2) regionOverlapCount += 1;
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


/**
 * 在页面上下文内验证认证槽位、URL、DOM 和完整 Key 形态。
 * Token 只参与页面内布尔比较，绝不穿过 Playwright 边界返回 Node。
 */
async function readPrivacyMetrics(page, config) {
  return page.evaluate(({ password, storageKeys }) => {
    const html = document.documentElement?.outerHTML ?? "";
    const text = document.documentElement?.innerText ?? "";
    const url = new URL(window.location.href);
    const sessionToken = sessionStorage.getItem(storageKeys[0]);
    const localAuthCount = storageKeys.filter((key) => localStorage.getItem(key) !== null).length;
    const sessionAuthCount = storageKeys.filter((key) => sessionStorage.getItem(key) !== null).length;
    const sensitiveQueryCount = [...url.searchParams.keys()].filter(
      (key) => /password|token|secret|authorization|prompt|response|provider[_-]?key|api[_-]?key/i.test(key),
    ).length;
    return {
      password_input_count: document.querySelectorAll('input[type="password"]').length,
      password_value_exposed: Boolean(password) && (html.includes(password) || text.includes(password) || url.href.includes(password)),
      full_virtual_key_pattern_count: (html.match(/\bmhv_[A-Za-z0-9_-]{16,}\b/g) ?? []).length,
      bearer_pattern_count: (html.match(/Bearer\s+[^"\s]{8,}/gi) ?? []).length,
      sensitive_query_count: sensitiveQueryCount,
      local_auth_slot_count: localAuthCount,
      session_auth_slot_count: sessionAuthCount,
      session_token_present: typeof sessionToken === "string" && sessionToken.length > 0,
      session_token_exposed: Boolean(sessionToken) && (html.includes(sessionToken) || text.includes(sessionToken) || url.href.includes(sessionToken)),
    };
  }, { password: config.adminPassword, storageKeys: AUTH_STORAGE_KEYS });
}


/** 把页面隐私指标冻结为通过证据，禁止返回任何原始 DOM 或认证值。 */
function assertBrowserPrivacy(metrics, stage = "browser.privacy") {
  requireCondition(metrics && typeof metrics === "object", stage, "metrics_missing");
  requireCondition(metrics.password_input_count === 0 && metrics.password_value_exposed === false, stage, "password_exposed");
  requireCondition(metrics.full_virtual_key_pattern_count === 0, stage, "virtual_key_exposed");
  requireCondition(metrics.bearer_pattern_count === 0, stage, "bearer_exposed");
  requireCondition(metrics.sensitive_query_count === 0, stage, "sensitive_query");
  requireCondition(metrics.local_auth_slot_count === 0, stage, "local_auth_present");
  requireCondition(metrics.session_auth_slot_count === AUTH_STORAGE_KEYS.length, stage, "session_auth_incomplete");
  requireCondition(metrics.session_token_present === true && metrics.session_token_exposed === false, stage, "session_token_invalid");
  // 对外只返回零命中和认证槽位布尔结论，原始 DOM、URL 与 Session 值不得离开页面上下文。
  return { sensitive_dom_hits: 0, sensitive_url_hits: 0, auth_storage_contract_valid: true };
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


/** 收集截图遮罩，确保用户身份和可能展示 Key 前缀的选择器不进入未遮罩 PNG。 */
async function screenshotMasks(page) {
  const selectors = [".user-menu", "#budget-scope-chain-key", '#budget-rule-form [name="scopeId"]'];
  const locators = [];
  for (const selector of selectors) {
    const locator = page.locator(selector);
    const count = await locator.count();
    for (let index = 0; index < count; index += 1) {
      if (await locator.nth(index).isVisible()) locators.push(locator.nth(index));
    }
  }
  return locators;
}


/** 在两档视口对一个真实页面状态执行布局、隐私、像素和独占截图写入验收。 */
async function captureState(page, config, stateName, anchorSelector) {
  const screenshots = [];
  const layouts = {};
  for (const viewport of VIEWPORTS) {
    const stage = `browser.capture.${stateName}.${viewport.name}`;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const anchor = page.locator(anchorSelector).first();
    await anchor.waitFor({ state: "visible", timeout: 20_000 });
    await anchor.scrollIntoViewIfNeeded();
    await settleLayout(page);
    layouts[viewport.name] = {
      ...assertLayoutMetrics(await readLayoutMetrics(page, ".console-shell"), viewport, stage),
      ...assertViewportIntersection(await anchor.boundingBox(), viewport, stage),
    };
    assertBrowserPrivacy(await readPrivacyMetrics(page, config), stage);
    const masks = await screenshotMasks(page);
    const buffer = await page.screenshot({
      fullPage: false,
      animations: "disabled",
      caret: "hide",
      mask: masks,
      maskColor: "#64748b",
    });
    const inspected = inspectPngPixels(buffer, viewport, stage);
    assertBrowserPrivacy(await readPrivacyMetrics(page, config), stage);
    const filename = `${stateName}-${viewport.name}.png`;
    try {
      await writeFile(path.join(config.artifactDirectory, "screenshots", filename), buffer, { flag: "wx" });
    } catch {
      fail(stage, "screenshot_write_failed");
    }
    screenshots.push({ state: stateName, viewport: viewport.name, file: `screenshots/${filename}`, ...inspected });
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


/** 执行真实浏览器主流程，并在成功、失败两条路径都关闭 Context、Chrome 和管理员 Session。 */
async function executeBrowserAcceptance(config) {
  const runtime = await loadRuntimeEvidence(config.artifactDirectory);
  requireCondition(!existsSync(path.join(config.artifactDirectory, "browser-evidence.json")), "evidence.publish", "output_exists");
  try {
    await mkdir(path.join(config.artifactDirectory, "screenshots"), { recursive: false });
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
  try {
    browser = await chromium.launch({
      ...(config.chromePath ? { executablePath: config.chromePath } : {}),
      headless: !config.headed,
    });
    context = await browser.newContext({ viewport: VIEWPORTS[0] });
    page = await context.newPage();
    const network = await installBrowserGuards(context, page, config);
    const login = await loginThroughConsole(page, config, () => {
      sessionNeedsRevocation = true;
    });
    const opened = await openBudgetCenter(page, config, runtime);
    const dom = await verifyBudgetDom(page, opened.projection);
    const adminForm = await verifyAdminBudgetForm(page, runtime);
    const overviewCapture = await captureState(page, config, "budget-overview", "#budgets-title");
    const chainCapture = await captureState(page, config, "budget-scope-chain", ".budget-chain-section");
    const formCapture = await captureState(page, config, "budget-admin-form", "#budget-rule-form");
    const logout = await logoutThroughConsole(page, config, () => {
      sessionNeedsRevocation = false;
    });
    requireCondition(network.blocked_request_count === 0, "browser.network", "blocked_request");
    requireCondition(network.browser_error_count === 0, "browser.runtime", "browser_error");
    result = {
      schema_version: 1,
      status: "passed",
      prd_id: "PRD-052",
      checks: {
        login,
        budget_api: { ...opened.apiEvidence, ...opened.projection.evidence },
        budget_dom: dom,
        admin_form: adminForm,
        logout,
      },
      viewports: VIEWPORTS.map(({ name, width, height }) => ({ name, width, height })),
      layouts: {
        budget_overview: overviewCapture.layouts,
        budget_scope_chain: chainCapture.layouts,
        budget_admin_form: formCapture.layouts,
      },
      screenshots: [
        ...overviewCapture.screenshots,
        ...chainCapture.screenshots,
        ...formCapture.screenshots,
      ],
      network,
      privacy: {
        environment_only_credentials: true,
        runtime_projection_safe: true,
        screenshot_identity_and_key_selectors_masked: true,
        dom_url_storage_scanned: true,
        safe_projection_verified: true,
      },
      cleanup: { session_revoked: logout.logged_out, browser_closed: true },
    };
    assertSafeEvidence(result, [config.adminPassword, config.adminEmail, config.tenantSlug]);
  } catch (error) {
    failure = toSafeFailure(error, "browser.execute", "unexpected_failure");
  } finally {
    try {
      // 成功登录后由 finally 持有清理责任；后续任何截图或证据失败都不能遗留 Session。
      if (sessionNeedsRevocation && page) {
        const sessionRevoked = await revokeSessionAfterFailure(page, config);
        sessionNeedsRevocation = !sessionRevoked;
        // 清理失败必须阻止 passed；已有主失败时只附固定码，不能覆盖首个业务失败阶段。
        if (!sessionRevoked) {
          // 固定 cleanup_code 不携带 Token、URL 或底层异常，适合与主失败一起输出。
          if (failure) failure.cleanupCode = "session_revoke_failed";
          // 主流程原本成功但撤销失败时，清理本身成为唯一失败，不能继续发布证据。
          else failure = new BrowserAcceptanceFailure("browser.cleanup", "session_revoke_failed");
        }
      }
      if (context) await context.close();
      if (browser) await browser.close();
    } catch {
      if (!failure) failure = new BrowserAcceptanceFailure("browser.cleanup", "close_failed");
    }
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
  assertViewportIntersection,
  browserRequestAllowed,
  cnyToMicros,
  inspectPngPixels,
  isForbiddenEvidenceKey,
  normalizeLoopbackOrigin,
  publishBrowserEvidence,
  readEnvironmentIdentity,
  resolveArtifactDirectory,
  revokeSessionAfterFailure,
  toSafeFailure,
  validateBudgetApiProjection,
  validateRuntimeEvidence,
  verifyLogoutEvidence,
};
