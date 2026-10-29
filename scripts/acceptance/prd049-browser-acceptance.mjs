/**
 * PRD-049 PAGE-070/PAGE-071 真实 Chrome 验收。
 *
 * 脚本只消费 ``artifacts/prd049/<batch>`` 的脱敏 Runtime 状态和同批次 SQLite，固定连接
 * localhost:8000/8001/5173。管理员密码、临时账号密码和所有 Session Token 只驻留进程
 * 内存；截图与结果写入前都会扫描 DOM、URL、Storage、网络元数据和结果对象。
 */

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { statSync } from "node:fs";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath, pathToFileURL } from "node:url";

import { inspectPngPixels } from "./prd047-browser-acceptance.mjs";

const require = createRequire(import.meta.url);
// 工作区、固定端口、角色页面、认证槽位与白名单共同限定浏览器验收的本地安全边界。
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = path.resolve(SCRIPT_DIR, "..", "..");

// 三个服务地址故意不可配置，避免把带凭据的浏览器流量误发给其他 localhost 端口。
const CONTROL_API_URL = "http://localhost:8001";
const GATEWAY_API_URL = "http://localhost:8000";
const CONSOLE_URL = "http://localhost:5173";
const TENANT_SLUG = "default";
const CHROME_PATH = process.env.METAHUB_CHROME_PATH
  ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

// 三档视口同时覆盖桌面、900px 折叠边界和常见窄屏手机。
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  Object.freeze({ name: "intermediate-900x800", width: 900, height: 800 }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
]);
const RESTRICTED_NAVIGATIONS = Object.freeze(["request-logs", "feedback-summary"]);
const AUTH_TOKEN_KEY = "metahub.auth_token";
const AUTH_CONTEXT_KEY = "metahub.auth_context";
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const GATEWAY_HEALTH_PATHS = new Set(["/health", "/health/live", "/health/ready", "/version"]);
// Browser 在消费 Runtime 结果前再次核对 0039 六项保护，防止验收期间数据库被替换或降级。
const PRD049_HARDENING_TRIGGERS = new Set([
  "trg_request_feedback_conflict_insert_prd049",
  "trg_request_feedback_event_conflict_insert_prd049",
  "trg_request_log_feedback_parent_conflict_insert_prd049",
  "trg_request_log_feedback_parent_conflict_update_prd049",
  "trg_routing_attempt_feedback_parent_conflict_insert_prd049",
  "trg_routing_attempt_feedback_parent_conflict_update_prd049",
]);
// 内部协议、批次/资源 ID 格式和标签字典只允许固定安全枚举，避免验收元数据携带敏感正文。
const INTERNAL_PROTOCOLS = new Set(["about:", "blob:", "data:"]);
const BATCH_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const RUNTIME_SUFFIX_PATTERN = /^[a-z0-9]{8,32}$/;
const RESOURCE_ID_PATTERN = /^[A-Za-z0-9._:-]{1,160}$/;
const REQUEST_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;
const SAFE_STAGE_PATTERN = /^[a-z0-9_.-]{1,120}$/;
const SAFE_CODE_PATTERN = /^[a-z0-9_]{1,80}$/;
const TAG_ORDER = Object.freeze([
  "accurate", "relevant", "complete", "helpful", "clear",
  "inaccurate", "irrelevant", "incomplete", "unsafe", "slow",
]);
const TAG_LABELS = Object.freeze({
  accurate: "准确",
  relevant: "相关",
  complete: "完整",
  helpful: "有帮助",
  clear: "清晰",
  inaccurate: "不准确",
  irrelevant: "不相关",
  incomplete: "不完整",
  unsafe: "不安全",
  slow: "响应慢",
});

// 固定形态哨兵用于发现未能从 Runtime 状态恢复原值的 Key、Prompt 和 Response 泄露。
const GENERIC_SECRET_PATTERNS = Object.freeze([
  /\bBearer\s+[A-Za-z0-9._~+/-]{8,}/i,
  /\bmhv_[A-Za-z0-9_-]{9,}/,
  /\bsk[-_][A-Za-z0-9_-]{10,}/i,
  /\bwhsec_[A-Za-z0-9_-]{7,}/i,
  /prd048-prompt-[A-Za-z0-9_-]+/i,
  /\bselected-(?:a|b)\b/i,
  /Prd049(?:Developer|Viewer|Browser)!/,
]);
// 敏感环境变量只按名称进入内存扫描集合，变量原值绝不会被复制进诊断或验收结果。
const SENSITIVE_ENV_NAME = /(?:PASSWORD|TOKEN|SECRET|API_KEY|PROVIDER_KEY|VIRTUAL_KEY|PROMPT|RESPONSE)/i;

/** 只携带固定阶段和错误码的验收异常，禁止复制底层 URL、正文、Token 或调用栈。 */
class BrowserAcceptanceStageError extends Error {
  /** 使用白名单 stage/code 构造可审计且不含凭据的错误。 */
  constructor(stage, code) {
    const safeStage = SAFE_STAGE_PATTERN.test(String(stage)) ? String(stage) : "browser.unknown";
    const safeCode = SAFE_CODE_PATTERN.test(String(code)) ? String(code) : "stage_failed";
    super(`browser_acceptance_failed stage=${safeStage} code=${safeCode}`);
    this.name = "BrowserAcceptanceStageError";
    this.stage = safeStage;
    this.code = safeCode;
  }
}

/** 把任意异常收敛为固定诊断；底层 message、stack、cause 均不会跨阶段传播。 */
function toSafeStageError(error, stage, fallbackCode = "stage_failed") {
  if (error instanceof BrowserAcceptanceStageError) return error;
  // 角色与全局 finally 会用 AggregateError 保证主流程、注销和清理全部执行。只有聚合中
  // 恰好一个错误时才递归恢复已经脱敏的精确阶段；多个失败仍收敛为外层固定 code，避免
  // 拼接底层 message、stack 或可能含凭据的 cause。
  if (error instanceof AggregateError && error.errors.length === 1) {
    return toSafeStageError(error.errors[0], stage, fallbackCode);
  }
  const errorName = String(error?.name ?? "Error");
  let code = fallbackCode;
  // 只按标准异常类别选择固定错误码，避免读取可能携带 URL、Token 或正文的 message。
  if (errorName === "AssertionError") code = "assertion_failed";
  else if (errorName === "TimeoutError") code = "timeout";
  return new BrowserAcceptanceStageError(stage, code);
}

/** 执行一个命名阶段，并在离开阶段前完成异常脱敏。 */
async function runAcceptanceStage(stage, action, fallbackCode = "stage_failed") {
  try {
    return await action();
  } catch (error) {
    // 原始异常必须在当前阶段终止前脱敏，禁止把底层 cause 或调用栈带入外层结果。
    throw toSafeStageError(error, stage, fallbackCode);
  }
}

/** 判断 candidate 是否严格位于 root 内部；相等路径只在显式允许时通过。 */
function pathIsInside(root, candidate, allowEqual = false) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return (allowEqual && relative === "")
    || (relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative));
}

/**
 * 根据安全批次名生成唯一的 Runtime、数据库目录和浏览器制品路径。
 * 参数只接受一个目录段，防止 ``..``、盘符或分隔符把写入扩大到 PRD-049 之外。
 */
function resolveBatchPaths(workspaceRoot, batch) {
  const normalizedBatch = String(batch ?? "");
  assert.ok(BATCH_PATTERN.test(normalizedBatch) && ![".", ".."].includes(normalizedBatch));
  const acceptanceRoot = path.resolve(workspaceRoot, "artifacts", "prd049");
  const batchDirectory = path.resolve(acceptanceRoot, normalizedBatch);
  const browserDirectory = path.join(batchDirectory, "browser");
  assert.ok(pathIsInside(acceptanceRoot, batchDirectory));
  assert.ok(pathIsInside(batchDirectory, browserDirectory));
  return {
    acceptanceRoot,
    batchDirectory,
    browserDirectory,
    runtimeStatePath: path.join(batchDirectory, "runtime-state.json"),
    resultPath: path.join(browserDirectory, "browser-result.json"),
  };
}

/** 根据 Runtime 白名单文件名解析同批次数据库，禁止绝对路径和嵌套目录。 */
function resolveRuntimeDatabasePath(batchDirectory, databaseFile) {
  const filename = String(databaseFile ?? "");
  assert.ok(/^[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.db$/.test(filename));
  assert.equal(path.basename(filename), filename);
  const databasePath = path.resolve(batchDirectory, filename);
  assert.ok(pathIsInside(batchDirectory, databasePath));
  return databasePath;
}

/** 校验一个稳定 ID；失败信息只包含字段标签，不回显不可信原值。 */
function requiredResourceId(value, label) {
  assert.ok(RESOURCE_ID_PATTERN.test(String(value ?? "")), `${label} 标识符非法`);
  return String(value);
}

/** 递归拒绝 Runtime 状态中不应出现的凭据或正文键名。 */
function assertRuntimeStateKeyPrivacy(value, pathParts = []) {
  if (!value || typeof value !== "object") return true;
  for (const [key, child] of Object.entries(value)) {
    const normalized = key.toLowerCase();
    assert.ok(
      !/(?:password|access_token|plain(?:text)?_key|provider_key|prompt_text|response_text|webhook_secret)/.test(normalized),
      `runtime-state 出现敏感字段 ${[...pathParts, key].join(".")}`,
    );
    assertRuntimeStateKeyPrivacy(child, [...pathParts, key]);
  }
  return true;
}

/**
 * 校验 PRD-049 Runtime 状态并投影浏览器所需的最小稳定事实。
 * Runtime 已撤销其临时凭据；浏览器会另建账号，绝不从状态文件恢复密码或 Token。
 */
function validateRuntimeStateDocument(body) {
  assert.ok(body && typeof body === "object" && !Array.isArray(body));
  assertRuntimeStateKeyPrivacy(body);
  assert.equal(body.prd_id, "PRD-049");
  assert.equal(body.status, "passed");
  assert.equal(body.lifecycle?.credentials_cleanup_completed, true);
  assert.equal(body.lifecycle?.credentials_cleanup_verified, true);
  const suffix = String(body.suffix ?? "");
  assert.ok(RUNTIME_SUFFIX_PATTERN.test(suffix));
  const ids = body.resource_ids ?? {};
  const sourceRequestIds = Array.isArray(body.source_request_ids)
    ? body.source_request_ids.map(String)
    : [];
  assert.equal(sourceRequestIds.length, 2);
  assert.equal(new Set(sourceRequestIds).size, 2);
  assert.ok(sourceRequestIds.every((item) => REQUEST_ID_PATTERN.test(item)));
  assert.deepEqual(
    new Set(sourceRequestIds),
    new Set([
      `req_prd049_runtime_session_source_${suffix}`,
      `req_prd049_runtime_key_source_${suffix}`,
    ]),
  );
  for (const check of [
    "admin_summary_read",
    "developer_summary_read",
    "viewer_feedback_denied_before_body",
    "viewer_summary_denied",
    "migration_0039_applied",
    "migration_0039_trigger_count",
    "control_feedback_method_not_allowed",
    "gateway_feedback_method_not_allowed",
    "feedback_method_not_allowed_parity",
    "request_log_feedback_join",
    "database_aggregate_exact",
    "database_sensitive_hits",
    "log_sensitive_hits",
  ]) {
    assert.ok(Object.hasOwn(body.checks ?? {}, check), `Runtime 缺少 ${check}`);
  }
  assert.equal(body.checks.admin_summary_read, true);
  assert.equal(body.checks.developer_summary_read, true);
  assert.equal(body.checks.viewer_feedback_denied_before_body, true);
  assert.equal(body.checks.viewer_summary_denied, true);
  assert.equal(body.checks.migration_0039_applied, true);
  assert.equal(body.checks.migration_0039_trigger_count, 6);
  assert.equal(body.checks.control_feedback_method_not_allowed, true);
  assert.equal(body.checks.gateway_feedback_method_not_allowed, true);
  assert.equal(body.checks.feedback_method_not_allowed_parity, true);
  assert.equal(body.checks.request_log_feedback_join, true);
  assert.equal(body.checks.database_aggregate_exact, true);
  assert.equal(body.checks.database_sensitive_hits, 0);
  assert.equal(body.checks.log_sensitive_hits, 0);
  return {
    suffix,
    databaseFile: String(body.database_file ?? ""),
    tenantId: requiredResourceId(ids.main_tenant_id, "mainTenantId"),
    adminUserId: requiredResourceId(ids.admin_user_id, "adminUserId"),
    modelAId: requiredResourceId(ids.model_a_id, "modelAId"),
    sourceRequestIds,
  };
}

/** 读取 UTF-8/BOM Runtime JSON，并执行完整生命周期与隐私校验。 */
async function loadRuntimeState(filePath) {
  const source = (await readFile(filePath, "utf8")).replace(/^\uFEFF/, "");
  return validateRuntimeStateDocument(JSON.parse(source));
}

/** 把秒精度 UTC 时间向前或向后移动，用于构造严格闭开 Summary 窗口。 */
function shiftCanonicalUtc(value, seconds) {
  const timestamp = Date.parse(String(value));
  assert.ok(Number.isFinite(timestamp));
  return new Date(timestamp + seconds * 1000).toISOString().replace(".000Z", "Z");
}

/**
 * 从两条同模型 Feedback 事实推导页面权威期望。
 * 输入来自只读 SQLite；函数只返回 ID、整数统计和白名单标签，不返回 actor ID 或 Key 信息。
 */
function deriveFeedbackExpectations(rows) {
  assert.ok(Array.isArray(rows) && rows.length === 2);
  const normalized = rows.map((row) => {
    const tags = JSON.parse(String(row.tags_json));
    assert.ok(Array.isArray(tags) && tags.length <= 5);
    assert.ok(tags.every((tag) => TAG_ORDER.includes(tag)));
    return {
      sourceRequestId: requiredResourceId(row.source_request_id, "sourceRequestId"),
      feedbackId: requiredResourceId(row.request_feedback_id, "feedbackId"),
      modelId: requiredResourceId(row.model_id, "feedbackModelId"),
      rating: Number(row.rating),
      accepted: Number(row.accepted) === 1,
      tags,
      actorType: String(row.actor_type),
      createdAt: String(row.created_at),
    };
  });
  assert.equal(new Set(normalized.map((item) => item.modelId)).size, 1);
  assert.deepEqual(new Set(normalized.map((item) => item.actorType)), new Set(["session_user", "virtual_key"]));
  assert.ok(normalized.every((item) => Number.isInteger(item.rating) && item.rating >= 1 && item.rating <= 5));
  const feedbackCount = normalized.length;
  const ratingSum = normalized.reduce((total, item) => total + item.rating, 0);
  const acceptedCount = normalized.filter((item) => item.accepted).length;
  const ratingDistribution = Object.fromEntries([1, 2, 3, 4, 5].map((rating) => [
    String(rating),
    normalized.filter((item) => item.rating === rating).length,
  ]));
  const tagCounts = TAG_ORDER.map((tag) => ({
    tag,
    count: normalized.filter((item) => item.tags.includes(tag)).length,
  })).filter((item) => item.count > 0);
  const createdTimes = normalized.map((item) => item.createdAt).sort();
  return {
    feedbacks: normalized,
    detailFeedback: normalized.find((item) => item.actorType === "session_user"),
    modelId: normalized[0].modelId,
    fromTime: shiftCanonicalUtc(createdTimes[0], -1),
    toTime: shiftCanonicalUtc(createdTimes.at(-1), 1),
    feedbackCount,
    ratingSum,
    averageRatingMilli: Math.round((ratingSum * 1000) / feedbackCount),
    acceptedCount,
    adoptionRateBps: Math.round((acceptedCount * 10000) / feedbackCount),
    ratingDistribution,
    tagCounts,
  };
}

/**
 * 核对数据库至少为 0039 且六项冲突守卫完整。
 *
 * 独立 helper 让轻量测试可以证明版本降级或任一 UPDATE 守卫缺失都会阻断浏览器验收，
 * 避免只依赖 Runtime 状态文件中的布尔结论。
 */
function assertPrd049DatabaseHardening(database) {
  const migration = database.prepare("SELECT MAX(version) AS version FROM schema_migrations").get();
  const migrationVersion = Number(migration?.version ?? 0);
  assert.ok(migrationVersion >= 39);
  const hardeningTriggers = new Set(database.prepare(
    "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN ("
      + "'trg_request_feedback_conflict_insert_prd049',"
      + "'trg_request_feedback_event_conflict_insert_prd049',"
      + "'trg_request_log_feedback_parent_conflict_insert_prd049',"
      + "'trg_request_log_feedback_parent_conflict_update_prd049',"
      + "'trg_routing_attempt_feedback_parent_conflict_insert_prd049',"
      + "'trg_routing_attempt_feedback_parent_conflict_update_prd049')",
  ).all().map((row) => String(row.name)));
  assert.equal(hardeningTriggers.size, 6);
  assert.deepEqual(hardeningTriggers, PRD049_HARDENING_TRIGGERS);
  return { migrationVersion, triggerCount: hardeningTriggers.size };
}

/** 以只读连接核对 Runtime stable ID、租户归属和两条不可变 Feedback 事实。 */
function loadStableDatabaseFacts(databasePath, runtime, { adminEmail = "admin@metahub.local" } = {}) {
  const database = new DatabaseSync(databasePath, { readOnly: true });
  try {
    database.exec("PRAGMA query_only=ON");
    assertPrd049DatabaseHardening(database);
    const tenant = database.prepare(
      "SELECT tenant_id, slug, status FROM tenants WHERE tenant_id=? AND slug=?",
    ).get(runtime.tenantId, TENANT_SLUG);
    assert.ok(tenant && tenant.status === "active");
    const admin = database.prepare(
      "SELECT user_id, email, role, status FROM users WHERE tenant_id=? AND user_id=?",
    ).get(runtime.tenantId, runtime.adminUserId);
    assert.ok(
      admin
        && admin.email === adminEmail
        && admin.role === "admin"
        && admin.status === "active",
    );
    const rows = database.prepare(`
      SELECT source_request_id, request_feedback_id, model_id, rating, accepted,
             tags_json, actor_type, created_at
      FROM request_feedbacks
      WHERE tenant_id=? AND source_request_id IN (?, ?)
      ORDER BY source_request_id
    `).all(runtime.tenantId, ...runtime.sourceRequestIds);
    assert.equal(rows.length, 2);
    assert.deepEqual(
      new Set(rows.map((row) => String(row.source_request_id))),
      new Set(runtime.sourceRequestIds),
    );
    const logs = database.prepare(`
      SELECT request_id, model_id, status, stream_status, final_routing_attempt_id
      FROM request_logs
      WHERE tenant_id=? AND request_id IN (?, ?)
    `).all(runtime.tenantId, ...runtime.sourceRequestIds);
    assert.equal(logs.length, 2);
    assert.ok(logs.every((row) => row.status === "success" && row.model_id && row.final_routing_attempt_id));
    const expectations = deriveFeedbackExpectations(rows);
    assert.equal(expectations.modelId, runtime.modelAId);
    const model = database.prepare(
      "SELECT model_id, model_alias, display_name FROM models WHERE tenant_id=? AND model_id=?",
    ).get(runtime.tenantId, expectations.modelId);
    assert.ok(model);
    const tenantFeedbackCount = Number(database.prepare(
      "SELECT COUNT(*) AS total FROM request_feedbacks WHERE tenant_id=?",
    ).get(runtime.tenantId).total);
    return {
      ...expectations,
      modelAlias: String(model.model_alias),
      modelDisplayName: String(model.display_name ?? ""),
      tenantFeedbackCount,
      immutableFingerprint: JSON.stringify(rows),
    };
  } finally {
    database.close();
  }
}

/** 将 URL.hostname 的 IPv6 方括号去掉，统一回环判断。 */
function normalizedHostname(target) {
  return String(target.hostname).replace(/^\[|\]$/g, "").toLowerCase();
}

/** 识别浏览器验收关心的三个 API 表面，只返回固定枚举而不保留 URL 或 query。 */
function classifyTargetApi(rawUrl, controlOrigin = CONTROL_API_URL) {
  let target;
  try {
    target = new URL(String(rawUrl));
  } catch {
    return null;
  }
  if (target.origin !== new URL(controlOrigin).origin) return null;
  if (target.pathname === "/feedback/summary") return "feedback_summary";
  if (target.pathname === "/feedback") return "feedback_post";
  if (target.pathname === "/request-logs") return "request_log_list";
  if (/^\/request-logs\/[^/]+$/.test(target.pathname)) return "request_log_detail";
  return null;
}

/** 把请求头键转为小写；值只用于网络门禁，不进入验收制品。 */
function normalizedHeaders(headers = {}) {
  return Object.fromEntries(
    Object.entries(headers).map(([key, value]) => [String(key).toLowerCase(), String(value)]),
  );
}

/**
 * 纯函数判定浏览器请求是否可以离开 Context。
 * Console/Control 必须精确 origin；Gateway 只允许无凭据健康 GET 或其最小 CORS 预检。
 */
function decideBrowserNetworkRequest(
  { url, method = "GET", headers = {} },
  {
    consoleOrigin = CONSOLE_URL,
    controlOrigin = CONTROL_API_URL,
    gatewayOrigin = GATEWAY_API_URL,
  } = {},
) {
  let target;
  try {
    target = new URL(String(url));
  } catch {
    return { allowed: false, destination: "blocked", violation: "invalid_url" };
  }
  if (INTERNAL_PROTOCOLS.has(target.protocol)) {
    return { allowed: true, destination: "browser_internal", violation: null };
  }
  const loopback = target.protocol === "http:"
    && LOOPBACK_HOSTS.has(normalizedHostname(target))
    && target.username === ""
    && target.password === "";
  if (loopback && target.origin === new URL(consoleOrigin).origin) {
    return { allowed: true, destination: "console", violation: null };
  }
  if (loopback && target.origin === new URL(controlOrigin).origin) {
    return { allowed: true, destination: "control", violation: null };
  }
  const requestHeaders = normalizedHeaders(headers);
  const requestMethod = String(method).toUpperCase();
  const pathAllowed = GATEWAY_HEALTH_PATHS.has(target.pathname)
    && target.search === ""
    && target.hash === "";
  const credentialHeaderPresent = ["authorization", "cookie", "x-internal-token"]
    .some((name) => Object.hasOwn(requestHeaders, name));
  const getAllowed = requestMethod === "GET"
    && requestHeaders.origin === new URL(consoleOrigin).origin
    && !credentialHeaderPresent;
  const requestedHeaders = String(requestHeaders["access-control-request-headers"] ?? "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
    .sort();
  const preflightAllowed = requestMethod === "OPTIONS"
    && requestHeaders.origin === new URL(consoleOrigin).origin
    && String(requestHeaders["access-control-request-method"] ?? "").toUpperCase() === "GET"
    && requestedHeaders.length === 1
    && requestedHeaders[0] === "x-request-id"
    && !credentialHeaderPresent;
  if (
    loopback
      && target.origin === new URL(gatewayOrigin).origin
      && pathAllowed
      && (getAllowed || preflightAllowed)
  ) {
    return { allowed: true, destination: "gateway_health", violation: null };
  }
  let violation = "origin";
  if (target.origin === new URL(gatewayOrigin).origin) {
    if (!GATEWAY_HEALTH_PATHS.has(target.pathname)) violation = "gateway_path";
    else if (target.search || target.hash) violation = "gateway_query";
    else violation = "gateway_method_or_headers";
  } else if (target.protocol !== "http:") {
    violation = "protocol";
  }
  return { allowed: false, destination: "blocked", violation };
}

/** 为 Context 安装精确网络门禁，并只采集固定类别、状态和安全响应头证据。 */
async function installBrowserGuards(context, page) {
  const evidence = {
    requestCount: 0,
    blockedRequestCount: 0,
    violations: [],
    targetRequests: [],
    targetResponses: [],
    browserErrors: [],
    checkpointCount: 0,
  };
  let sequence = 0;
  await context.route("**/*", async (route) => {
    const request = route.request();
    const decision = decideBrowserNetworkRequest({
      url: request.url(),
      method: request.method(),
      headers: await request.allHeaders(),
    });
    evidence.requestCount += 1;
    if (!decision.allowed) {
      evidence.blockedRequestCount += 1;
      evidence.violations.push(decision.violation);
      await route.abort("blockedbyclient");
      return;
    }
    const surface = classifyTargetApi(request.url());
    if (surface) {
      sequence += 1;
      evidence.targetRequests.push({ sequence, method: request.method(), surface });
    }
    await route.continue();
  });
  page.on("response", (response) => {
    const surface = classifyTargetApi(response.url());
    if (surface) {
      const headers = response.headers();
      evidence.targetResponses.push({
        method: response.request().method(),
        surface,
        status: response.status(),
        requestIdPresent: Boolean(headers["x-request-id"]),
        traceIdPresent: Boolean(headers["x-trace-id"]),
        noStore: String(headers["cache-control"] ?? "").startsWith("no-store"),
      });
    }
    // 任意目标或非目标 5xx 都说明页面运行不健康，但证据只能保存固定错误类别。
    if (response.status() >= 500) evidence.browserErrors.push("http_5xx");
  });
  page.on("pageerror", () => evidence.browserErrors.push("pageerror"));
  page.on("console", (message) => {
    // Console error 可能包含页面数据，因此仅登记类别，不能复制浏览器原始消息文本。
    if (message.type() === "error") evidence.browserErrors.push("console_error");
  });
  return evidence;
}

/** 收集当前已知密码、Token 和调用方提供的其他敏感环境值，仅驻留内存。 */
function collectConfiguredSecrets(values = [], env = process.env) {
  const secrets = new Set(values.map(String).filter((value) => value.length >= 4));
  for (const [name, value] of Object.entries(env)) {
    if (SENSITIVE_ENV_NAME.test(name) && String(value ?? "").length >= 4) {
      secrets.add(String(value));
    }
  }
  // 返回集合只在当前进程参与布尔扫描，调用方不得将其序列化为任何验收制品。
  return secrets;
}

/** 判断文本是否包含任一精确凭据或通用 Key/Prompt/Response 哨兵，只返回布尔值。 */
function containsSecret(value, exactSecrets = []) {
  const text = String(value ?? "");
  return [...exactSecrets]
    .map(String)
    .filter((item) => item.length >= 4)
    .some((secret) => text.includes(secret))
    || GENERIC_SECRET_PATTERNS.some((pattern) => pattern.test(text));
}

/**
 * 纯函数核对 DOM、URL 与 Storage 隐私。
 * 登录态只允许当前 Token 出现在唯一 sessionStorage 槽位，注销态两个认证槽必须同时消失。
 */
function assertSensitiveSurfaceSnapshot(
  snapshot,
  { exactSecrets = [], activeToken = null, loggedOut = false, label = "browser" } = {},
) {
  const localEntries = new Map(snapshot.localStorageEntries ?? []);
  const sessionEntries = new Map(snapshot.sessionStorageEntries ?? []);
  const publicValues = [
    snapshot.domText,
    snapshot.domHtml,
    snapshot.url,
    ...localEntries.keys(),
    ...localEntries.values(),
  ];
  assert.ok(publicValues.every((value) => !containsSecret(value, exactSecrets)), `${label} 公开表面泄露敏感值`);
  assert.ok(!localEntries.has(AUTH_TOKEN_KEY) && !localEntries.has(AUTH_CONTEXT_KEY));
  if (loggedOut) {
    assert.ok(!sessionEntries.has(AUTH_TOKEN_KEY) && !sessionEntries.has(AUTH_CONTEXT_KEY));
  } else {
    assert.ok(typeof activeToken === "string" && activeToken.length >= 16);
    assert.equal(sessionEntries.get(AUTH_TOKEN_KEY), activeToken);
    assert.ok(sessionEntries.has(AUTH_CONTEXT_KEY));
  }
  for (const [key, value] of sessionEntries) {
    // 活跃 Token 仅可豁免唯一认证槽位，其他键值仍必须执行完整敏感扫描。
    if (!loggedOut && key === AUTH_TOKEN_KEY) continue;
    assert.ok(!containsSecret(key, exactSecrets) && !containsSecret(value, exactSecrets));
  }
  const authSlotCount = [...sessionEntries.keys()]
    .filter((key) => key === AUTH_TOKEN_KEY || key === AUTH_CONTEXT_KEY).length;
  assert.equal(authSlotCount, loggedOut ? 0 : 2);
  // 脱敏结果只保留零命中和槽位合同布尔值，不返回 DOM、Storage 或 Token 原文。
  return { domSensitiveHitCount: 0, storageSensitiveHitCount: 0, authStorageContractValid: true };
}

/** 从真实页面读取只驻留内存的 DOM、URL 和 Web Storage 快照。 */
async function readSensitiveSurfaceSnapshot(page) {
  return page.evaluate(() => ({
    domText: document.documentElement?.innerText ?? "",
    domHtml: document.documentElement?.outerHTML ?? "",
    url: window.location.href,
    localStorageEntries: Object.entries(window.localStorage),
    sessionStorageEntries: Object.entries(window.sessionStorage),
  }));
}

/** 核对 Feedback 业务 DOM 没有出现后端禁止暴露的正文或凭据字段名。 */
async function assertFeedbackDomProjection(page) {
  const fragments = await page.locator(
    ".request-log-detail, .request-feedback-section, .feedback-summary-panel",
  ).evaluateAll((elements) => elements.map((element) => element.outerHTML));
  const text = fragments.join("\n");
  assert.doesNotMatch(
    text,
    /(?:provider_key|key_hash|token_hash|password_hash|prompt_text|response_text|request_body|response_body)/i,
  );
  return true;
}

/** 在页面生命周期检查点联合执行网络、浏览器错误、DOM/URL/Storage 和字段投影门禁。 */
async function scanBrowserCheckpoint(
  page,
  evidence,
  exactSecrets,
  { activeToken = null, loggedOut = false, label = "browser" } = {},
) {
  assert.equal(evidence.blockedRequestCount, 0);
  assert.deepEqual(evidence.violations, []);
  assert.deepEqual(evidence.browserErrors, []);
  const privacy = assertSensitiveSurfaceSnapshot(await readSensitiveSurfaceSnapshot(page), {
    exactSecrets,
    activeToken,
    loggedOut,
    label,
  });
  await assertFeedbackDomProjection(page);
  evidence.checkpointCount += 1;
  return privacy;
}

/** 根据固定动作和随机批次生成可追踪 request_id，禁止把密码、Token 或正文用作标识符。 */
function operationRequestId(kind, suffix) {
  return `req_prd049_browser_${kind}_${suffix}`.slice(0, 128);
}

/** 安全读取 JSON 响应；解析失败只返回空对象，不在错误中复制正文。 */
async function readJson(response) {
  try {
    const body = await response.json();
    return body && typeof body === "object" && !Array.isArray(body) ? body : {};
  } catch {
    return {};
  }
}

/**
 * 调用真实 Control API 并核对统一 request_id/trace_id。
 * 返回体只在当前调用栈使用，调用方写结果时必须投影为状态或计数。
 */
async function controlJson(pathname, options, expectedStatus, expectedCode = null) {
  const response = await fetch(`${CONTROL_API_URL}${pathname}`, options);
  const body = await readJson(response);
  assert.equal(response.status, expectedStatus, `${pathname} HTTP 状态异常`);
  assert.ok(body.request_id && body.trace_id);
  assert.equal(body.request_id, response.headers.get("X-Request-ID"));
  assert.equal(body.trace_id, response.headers.get("X-Trace-ID"));
  if (expectedCode !== null) assert.equal(body.code, expectedCode);
  return { body, response };
}

/** 构造带 Bearer 与操作 request_id 的 Control 请求头，Token 不会进入结果。 */
function authHeaders(token, requestId) {
  return { Authorization: `Bearer ${token}`, "X-Request-ID": requestId };
}

/** 登记一个已提交 Session 的 tenant/user owner，供最终精确撤销。 */
function registerSession(state, body, expectedOwner) {
  const sessionId = requiredResourceId(body.session_id, "sessionId");
  assert.ok(!state.sessionOwners.has(sessionId));
  assert.ok(
    body.user?.tenant_id === expectedOwner.tenantId
      && body.user?.user_id === expectedOwner.userId
      && body.user?.email === expectedOwner.email
      && body.user?.role === expectedOwner.role,
  );
  state.sessionOwners.set(sessionId, { ...expectedOwner });
  const token = String(body.access_token ?? "");
  assert.ok(token.length >= 16);
  state.secretRegistry.add(token);
  // Token 与 Session ID 只返回给当前内存调用链，外层结果只能保存对应会话数量。
  return { token, sessionId };
}

/** 使用真实登录 API 创建只驻内存的 Session，并核对 Runtime/临时用户 owner。 */
async function apiLogin(state, account, role, userId, marker) {
  const requestId = operationRequestId(`api_login_${role}`, state.browserSuffix);
  const expectedOwner = {
    tenantId: state.runtime.tenantId,
    userId,
    email: account.email,
    role,
  };
  // 请求发出前登记唯一 User-Agent intent，响应丢失时仍能从同批次库精确找到已提交 Session。
  state.sessionIntents.set(marker, expectedOwner);
  const { body } = await controlJson("/auth/login", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Request-ID": requestId,
      "User-Agent": marker,
    },
    body: JSON.stringify({
      tenant_slug: TENANT_SLUG,
      email: account.email,
    password: account.password,
    }),
  }, 200);
  // 登录成功必须先登记 Session owner，再把内存 Token 交给后续真实权限探针。
  return registerSession(state, body, expectedOwner);
}

/**
 * 管理员通过真实 `/users` 创建 developer/viewer。
 * intent 在 POST 前登记，以便响应丢失时仍能按随机 email/displayName/role 有界对账。
 */
async function prepareAccounts(state) {
  const adminSession = await apiLogin(
    state,
    state.accounts.admin,
    "admin",
    state.runtime.adminUserId,
    `prd049-browser-bootstrap-${state.browserSuffix}`,
  );
  state.apiTokens.set("admin", adminSession.token);
  for (const role of ["developer", "viewer"]) {
    const account = {
      email: `prd049-browser-${role}-${state.browserSuffix}@metahub.local`,
      displayName: `PRD049 browser ${role} ${state.browserSuffix}`,
      password: `Prd049Browser!${randomBytes(18).toString("base64url")}`,
      role,
    };
    state.secretRegistry.add(account.password);
    state.userIntents.set(role, account);
    const requestId = operationRequestId(`create_${role}`, state.browserSuffix);
    const { body } = await controlJson("/users", {
      method: "POST",
      headers: {
        ...authHeaders(adminSession.token, requestId),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        email: account.email,
        display_name: account.displayName,
        password: account.password,
        role,
      }),
    }, 201);
    const user = body.user ?? {};
    const userId = requiredResourceId(user.user_id, `${role}UserId`);
    assert.ok(
      user.tenant_id === state.runtime.tenantId
        && user.email === account.email
        && user.display_name === account.displayName
        && user.role === role
        && user.status === "active",
    );
    state.temporaryUserOwners.set(userId, {
      tenantId: state.runtime.tenantId,
      email: account.email,
      displayName: account.displayName,
      role,
    });
    state.accounts[role] = { ...account, userId };
    const roleSession = await apiLogin(
      state,
      account,
      role,
      userId,
      `prd049-browser-api-${role}-${state.browserSuffix}`,
    );
    state.apiTokens.set(role, roleSession.token);
  }
  // 账号准备证据只公开数量，两个随机密码、用户 ID 和三枚 API Token 均不得进入结果。
  return { temporaryAccountCount: 2, apiSessionCount: 3 };
}

/** 核对 Summary DTO 与只读 SQLite 推导值一致，不把完整响应写入制品。 */
function assertSummaryPayload(body, stableFacts) {
  assert.equal(body.scope_type, "tenant");
  assert.equal(body.from_time, stableFacts.fromTime);
  assert.equal(body.to_time, stableFacts.toTime);
  assert.equal(body.summary?.feedback_count, stableFacts.feedbackCount);
  assert.equal(body.summary?.rating_sum, stableFacts.ratingSum);
  assert.equal(body.summary?.average_rating_milli, stableFacts.averageRatingMilli);
  assert.equal(body.summary?.accepted_count, stableFacts.acceptedCount);
  assert.equal(body.summary?.adoption_rate_bps, stableFacts.adoptionRateBps);
  assert.deepEqual(body.summary?.rating_distribution, stableFacts.ratingDistribution);
  assert.deepEqual(body.summary?.tag_counts, stableFacts.tagCounts);
  assert.equal(body.models?.length, 1);
  assert.equal(body.models[0].model_id, stableFacts.modelId);
  assert.equal(body.models[0].feedback_count, stableFacts.feedbackCount);
  return true;
}

/**
 * 纯判定 PAGE-071 DOM 是否已经收敛到筛选后的权威事实。
 *
 * snapshot 只包含页面展示文本，不包含 Token、URL 或响应正文。四个指标按渲染
 * 顺序精确比较；模型样本数取独立单元格，避免别名中的数字造成误命中；标签按固定字典
 * 和精确计数逐项覆盖，允许未来页面增加不影响既有事实的其他标签。
 */
function summaryDomMatchesStableFacts(snapshot, stableFacts) {
  if (!snapshot || !stableFacts) return false;
  const numericFacts = [
    stableFacts.feedbackCount,
    stableFacts.averageRatingMilli,
    stableFacts.adoptionRateBps,
    stableFacts.acceptedCount,
  ];
  if (!numericFacts.every((value) => Number.isSafeInteger(value) && value >= 0)) return false;
  const expectedMetrics = [
    String(stableFacts.feedbackCount),
    String(stableFacts.averageRatingMilli / 1000) + " / 5",
    String(stableFacts.adoptionRateBps / 100) + "%",
    String(stableFacts.acceptedCount),
  ];
  const metricStrongValues = snapshot.metricStrongValues;
  if (
    !Array.isArray(metricStrongValues)
      || metricStrongValues.length !== expectedMetrics.length
      || metricStrongValues.some((value, index) => value !== expectedMetrics[index])
  ) {
    return false;
  }

  const modelRows = snapshot.modelRows;
  if (!Array.isArray(modelRows) || modelRows.length !== 1) return false;
  const modelAlias = String(stableFacts.modelAlias ?? "");
  const modelDisplayName = String(stableFacts.modelDisplayName ?? "");
  const modelRow = modelRows[0] ?? {};
  const modelText = String(modelRow.text ?? "");
  if (
    !modelAlias
      || !modelText.includes(modelAlias)
      || (modelDisplayName && !modelText.includes(modelDisplayName))
      || String(modelRow.feedbackCount ?? "") !== String(stableFacts.feedbackCount)
  ) {
    return false;
  }

  const tagRows = Array.isArray(snapshot.tagRows) ? snapshot.tagRows : [];
  const expectedTags = Array.isArray(stableFacts.tagCounts) ? stableFacts.tagCounts : [];
  return expectedTags.every((expected) => {
    const label = TAG_LABELS[expected.tag];
    return Boolean(label) && tagRows.some(
      (row) => row?.label === label && row?.count === String(expected.count),
    );
  });
}

/** 一次读取 PAGE-071 的指标、模型行和标签，避免跨 DOM 替换拼接出不存在的混合快照。 */
async function readFeedbackSummaryDomSnapshot(page) {
  return page.locator(".feedback-summary-panel").evaluate((panel) => ({
    metricStrongValues: [...panel.querySelectorAll(".feedback-summary-metrics strong")]
      .map((element) => String(element.textContent ?? "").trim()),
    modelRows: [...panel.querySelectorAll(
      ".feedback-model-row:not(.feedback-model-head)",
    )].map((element) => {
      const cells = element.querySelectorAll(":scope > span");
      return {
        text: String(element.innerText ?? "").trim(),
        feedbackCount: String(cells[1]?.textContent ?? "").trim(),
      };
    }),
    tagRows: [...panel.querySelectorAll(".feedback-summary-tags div > span")]
      .map((element) => ({
        label: String(element.querySelector("em")?.textContent ?? "").trim(),
        count: String(element.querySelector("strong")?.textContent ?? "").trim(),
      })),
  }));
}

/**
 * 等待 Console 把已返回的筛选响应真正提交到 DOM。
 *
 * waitForResponse 在响应体可读时即可结束，而页面的异步 refresh 仍可能尚未替换旧报表。
 * 因此这里用无敏感数据的原子快照短轮询；超时只抛固定断言，不回显租户或模型信息。
 */
async function waitForFilteredSummaryDom(page, stableFacts, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  do {
    const snapshot = await readFeedbackSummaryDomSnapshot(page);
    if (summaryDomMatchesStableFacts(snapshot, stableFacts)) return snapshot;
    await page.waitForTimeout(50);
  } while (Date.now() < deadline);
  assert.fail("filtered feedback summary DOM did not converge");
}

/** 对三角色直接攻击后端，证明 RBAC 发生在正文/query 解析之前且合法读取可追踪。 */
async function verifyBackendPermissions(state) {
  const results = {};
  for (const role of ["admin", "developer", "viewer"]) {
    const token = state.apiTokens.get(role);
    assert.ok(token);
    const summaryRequestId = operationRequestId(`rbac_summary_${role}`, state.browserSuffix);
    // viewer 故意携带畸形 query，必须在解析租户参数和分页前由后端 RBAC 返回 403。
    if (role === "viewer") {
      const { response } = await controlJson(
        "/feedback/summary?tenant_id=forbidden&limit=not-a-number",
        { headers: authHeaders(token, summaryRequestId) },
        403,
        "auth.permission_denied",
      );
      assert.ok(String(response.headers.get("Cache-Control") ?? "").startsWith("no-store"));
    } else {
      const query = new URLSearchParams({
        model_id: state.stableFacts.modelId,
        from_time: state.stableFacts.fromTime,
        to_time: state.stableFacts.toTime,
        limit: "20",
        offset: "0",
      });
      const { body, response } = await controlJson(
        `/feedback/summary?${query}`,
        { headers: authHeaders(token, summaryRequestId) },
        200,
      );
      // 原始成功响应也必须通过凭据/正文哨兵扫描，不能只依赖 Console 投影后的 DOM。
      assert.equal(containsSecret(JSON.stringify(body), state.secretRegistry), false);
      assertSummaryPayload(body, state.stableFacts);
      assert.equal(response.headers.get("Cache-Control"), "no-store, private");
    }
    const postRequestId = operationRequestId(`rbac_post_${role}`, state.browserSuffix);
    const postExpectedStatus = role === "viewer" ? 403 : 422;
    const postExpectedCode = role === "viewer" ? "auth.permission_denied" : "feedback.input_invalid";
    await controlJson("/feedback", {
      method: "POST",
      headers: {
        ...authHeaders(token, postRequestId),
        "Content-Type": "application/json",
      },
      body: role === "viewer" ? "not-json-and-must-not-be-read" : "{}",
    }, postExpectedStatus, postExpectedCode);
    const logRequestId = operationRequestId(`rbac_log_${role}`, state.browserSuffix);
    const logProbe = await controlJson(
      `/request-logs/${encodeURIComponent(state.stableFacts.detailFeedback.sourceRequestId)}`,
      { headers: authHeaders(token, logRequestId) },
      role === "viewer" ? 403 : 200,
      role === "viewer" ? "auth.permission_denied" : null,
    );
    // RequestLog 原始 DTO 不得携带 Provider Key、Prompt 或 Response；错误体也不能反射攻击正文。
    assert.equal(containsSecret(JSON.stringify(logProbe.body), state.secretRegistry), false);
    results[role] = {
      summaryStatus: role === "viewer" ? 403 : 200,
      feedbackPostStatus: postExpectedStatus,
      requestLogStatus: role === "viewer" ? 403 : 200,
    };
  }
  // 固定矩阵复核能防止单个角色状态被循环赋值或未来改动静默放宽。
  return assertBackendPermissionEvidence(results);
}

/** 纯函数锁定三角色后端状态矩阵，避免调用方只检查“请求已发出”。 */
function assertBackendPermissionEvidence(results) {
  assert.deepEqual(results.admin, { summaryStatus: 200, feedbackPostStatus: 422, requestLogStatus: 200 });
  assert.deepEqual(results.developer, { summaryStatus: 200, feedbackPostStatus: 422, requestLogStatus: 200 });
  assert.deepEqual(results.viewer, { summaryStatus: 403, feedbackPostStatus: 403, requestLogStatus: 403 });
  return { ...results, backendRbacVerified: true };
}

/** 核对固定三服务健康，以及 Gateway 确实没有注册控制面 Summary。 */
async function verifyFixedServices() {
  for (const url of [`${CONTROL_API_URL}/health`, `${GATEWAY_API_URL}/health`, `${CONSOLE_URL}/`]) {
    const response = await fetch(url);
    assert.equal(response.status, 200);
  }
  const absent = await fetch(`${GATEWAY_API_URL}/feedback/summary`, {
    headers: { "X-Request-ID": "req_prd049_browser_gateway_summary_absent" },
  });
  assert.equal(absent.status, 404);
  return { controlStatus: 200, gatewayStatus: 200, consoleStatus: 200, gatewaySummaryStatus: 404 };
}

/** 使用真实 Console 登录表单，登记 UI Session owner 并确认 `/auth/me` 归属。 */
async function loginPage(page, state, role, requestedNavigation) {
  const account = state.accounts[role];
  await page.goto(`${CONSOLE_URL}/#${requestedNavigation}`, { waitUntil: "domcontentloaded" });
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30_000 });
  await page.locator('[name="tenantSlug"]').fill(TENANT_SLUG);
  await page.locator('[name="email"]').fill(account.email);
  await page.locator('[name="password"]').fill(account.password);
  const loginResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === "/auth/login"
      && response.request().method() === "POST";
  }, { timeout: 30_000 });
  const meResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === "/auth/me"
      && response.request().method() === "GET";
  }, { timeout: 30_000 });
  await page.locator('#login-form button[type="submit"]').click();
  const loginResponse = await loginResponsePromise;
  assert.equal(loginResponse.status(), 200);
  const loginBody = await loginResponse.json();
  const session = registerSession(state, loginBody, {
    tenantId: state.runtime.tenantId,
    userId: role === "admin" ? state.runtime.adminUserId : account.userId,
    email: account.email,
    role,
  });
  state.uiTokens.set(role, session.token);
  const meResponse = await meResponsePromise;
  assert.equal(meResponse.status(), 200);
  const meBody = await meResponse.json();
  assert.ok(
    meBody.user?.tenant_id === state.runtime.tenantId
      && meBody.user?.user_id === (role === "admin" ? state.runtime.adminUserId : account.userId)
      && meBody.user?.role === role
      && meBody.session?.session_id === session.sessionId,
  );
  await page.locator(".console-shell").waitFor({ state: "visible", timeout: 30_000 });
  assert.match(await page.locator(".user-menu").innerText(), new RegExp(`\\b${role}\\b`));
  // UI Session 只在当前角色生命周期驻留，finally 注销与数据库兜底都依赖该 owner。
  return session;
}

/** 点击后端授权的导航项并等待新页面标题；hash 不能替代后端导航许可。 */
async function navigate(page, navigation, titleSelector) {
  const button = page.locator(`[data-console-nav="${navigation}"]`);
  await button.waitFor({ state: "visible", timeout: 30_000 });
  await button.click();
  await page.locator(`.console-shell[data-active-navigation="${navigation}"]`).waitFor({
    state: "visible",
    timeout: 30_000,
  });
  await page.locator(titleSelector).waitFor({ state: "visible", timeout: 30_000 });
  await page.waitForTimeout(250);
}

/**
 * 检查根页面、目标面板、控件文字和可交互元素不横向越界或彼此覆盖。
 * 重叠计算排除祖先/后代，避免把 button 内 span 等合法嵌套误判为冲突。
 */
async function assertResponsiveLayout(page, viewport, panelSelector) {
  const metrics = await page.evaluate(({ selector, expectedWidth }) => {
    const root = document.documentElement;
    const body = document.body;
    const panel = document.querySelector(selector);
    if (!panel) return { missing: true };
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rectangle = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rectangle.width > 0 && rectangle.height > 0;
    };
    const panelRectangle = panel.getBoundingClientRect();
    const nodes = [...panel.querySelectorAll("button,input,select,textarea,[role='row'],fieldset,section")]
      .filter(visible);
    const horizontalOffenderCount = nodes.filter((element) => {
      const rectangle = element.getBoundingClientRect();
      return rectangle.left < -1 || rectangle.right > root.clientWidth + 1;
    }).length;
    const clippedControlCount = [...panel.querySelectorAll("button")]
      .filter(visible)
      .filter((element) => element.scrollWidth > element.clientWidth + 1).length;
    const controls = [...panel.querySelectorAll("button,input,select,textarea")]
      .filter(visible)
      .map((element) => ({ element, rectangle: element.getBoundingClientRect() }));
    let overlapCount = 0;
    for (let left = 0; left < controls.length; left += 1) {
      for (let right = left + 1; right < controls.length; right += 1) {
        const first = controls[left];
        const second = controls[right];
        if (first.element.contains(second.element) || second.element.contains(first.element)) continue;
        const overlapWidth = Math.min(first.rectangle.right, second.rectangle.right)
          - Math.max(first.rectangle.left, second.rectangle.left);
        const overlapHeight = Math.min(first.rectangle.bottom, second.rectangle.bottom)
          - Math.max(first.rectangle.top, second.rectangle.top);
        if (overlapWidth > 2 && overlapHeight > 2) overlapCount += 1;
      }
    }
    return {
      missing: false,
      innerWidth: window.innerWidth,
      expectedWidth,
      rootScrollWidth: root.scrollWidth,
      bodyScrollWidth: body.scrollWidth,
      panelScrollWidth: panel.scrollWidth,
      panelClientWidth: panel.clientWidth,
      panelLeft: Math.round(panelRectangle.left),
      panelRight: Math.round(panelRectangle.right),
      horizontalOffenderCount,
      clippedControlCount,
      overlapCount,
    };
  }, { selector: panelSelector, expectedWidth: viewport.width });
  assert.equal(metrics.missing, false);
  assert.equal(metrics.innerWidth, viewport.width);
  assert.ok(metrics.rootScrollWidth <= viewport.width);
  assert.ok(metrics.bodyScrollWidth <= viewport.width);
  assert.ok(metrics.panelScrollWidth <= metrics.panelClientWidth + 1);
  assert.ok(metrics.panelLeft >= -1 && metrics.panelRight <= viewport.width + 1);
  assert.equal(metrics.horizontalOffenderCount, 0);
  assert.equal(metrics.clippedControlCount, 0);
  assert.equal(metrics.overlapCount, 0);
  return { horizontalOverflow: false, clippedControlCount: 0, overlapCount: 0 };
}

/** 截图前后复扫隐私并用共享 PNG 解析器证明尺寸和像素非空。 */
async function capturePage(page, state, role, pageName, viewport, scan) {
  await scan(`${pageName}:${viewport.name}:before`);
  const masks = [page.locator(".user-menu")];
  const virtualKeyFacts = page.locator(".request-log-detail-grid > div", { hasText: "Virtual Key" });
  if (await virtualKeyFacts.count()) masks.push(virtualKeyFacts);
  const buffer = await page.screenshot({ fullPage: true, mask: masks, maskColor: "#e5e7eb" });
  const pixels = inspectPngPixels(buffer, { expectedWidth: viewport.width, minHeight: viewport.height });
  await scan(`${pageName}:${viewport.name}:after`);
  const filename = `${role}-${pageName}-${viewport.name}.png`;
  await writeFile(path.join(state.paths.browserDirectory, filename), buffer);
  return { filename, ...pixels, privacyMaskApplied: true };
}

/** PAGE-070 按 Runtime source request_id 定位列表、详情和已有不可变 Feedback。 */
async function verifyFeedbackDetailPage(page, state, scan) {
  await navigate(page, "request-logs", "#request-logs-title");
  await scan("page070:before_filter");
  const expected = state.stableFacts.detailFeedback;
  const form = page.locator("#request-log-filter-form");
  await form.locator('[name="requestId"]').fill(expected.sourceRequestId);
  const listResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === "/request-logs"
      && target.searchParams.get("request_id") === expected.sourceRequestId;
  }, { timeout: 30_000 });
  await form.locator('button[type="submit"]').click();
  const listResponse = await listResponsePromise;
  assert.equal(listResponse.status(), 200);
  // 列表原始 DTO 与详情一致执行内存级敏感扫描，防止安全字段虽未渲染却仍经网络泄露。
  assert.equal(containsSecret(await listResponse.text(), state.secretRegistry), false);
  const detailButton = page.locator(`[data-request-log-detail="${expected.sourceRequestId}"]`);
  await detailButton.waitFor({ state: "visible", timeout: 30_000 });
  const row = detailButton.locator("xpath=ancestor::*[@role='row'][1]");
  assert.match(await row.innerText(), /已反馈/);
  const detailResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === `/request-logs/${expected.sourceRequestId}`
      && response.request().method() === "GET";
  }, { timeout: 30_000 });
  await detailButton.click();
  const detailResponse = await detailResponsePromise;
  assert.equal(detailResponse.status(), 200);
  const detailBody = await detailResponse.json();
  assert.equal(containsSecret(JSON.stringify(detailBody), state.secretRegistry), false);
  const feedback = detailBody.request_log?.feedback;
  assert.ok(
    feedback
      && feedback.request_feedback_id === expected.feedbackId
      && feedback.request_id === expected.sourceRequestId
      && feedback.model_id === expected.modelId
      && feedback.rating === expected.rating
      && feedback.accepted === expected.accepted
      && JSON.stringify(feedback.tags) === JSON.stringify(expected.tags)
      && feedback.actor_type === expected.actorType
      && detailBody.request_log.feedback_eligible === false
      && detailBody.request_log.feedback_ineligible_reason === "already_recorded",
  );
  const section = page.locator(".request-feedback-recorded");
  await section.waitFor({ state: "visible", timeout: 30_000 });
  const text = await section.innerText();
  assert.match(text, /已记录反馈/);
  assert.ok(text.includes(`${expected.rating} / 5`));
  assert.match(text, expected.accepted ? /已采纳/ : /未采纳/);
  assert.ok(text.includes(expected.modelId));
  for (const tag of expected.tags) assert.ok(text.includes(TAG_LABELS[tag]));
  assert.equal(await section.locator("[data-request-feedback-form]").count(), 0);
  assert.equal(await section.locator("button").count(), 0);
  await scan("page070:after_detail");
  return {
    requestLogMarked: true,
    immutableFeedbackVisible: true,
    feedbackFormAbsent: true,
    feedbackRating: expected.rating,
    feedbackAccepted: expected.accepted,
    feedbackTagCount: expected.tags.length,
  };
}

/** PAGE-071 使用 stable model/time 筛选，核对服务端总计、模型行、分布和标签。 */
async function verifyFeedbackSummaryPage(page, state, scan) {
  const initialResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === "/feedback/summary"
      && response.request().method() === "GET";
  }, { timeout: 30_000 });
  await navigate(page, "feedback-summary", "#feedback-summary-title");
  assert.equal((await initialResponsePromise).status(), 200);
  const form = page.locator("#feedback-summary-filter-form");
  // PAGE-071 只能暴露 model/time 筛选，tenant、actor 与 Virtual Key 范围必须来自后端身份。
  assert.equal(await form.locator('[name="tenant_id"], [name="actor"], [name="virtual_key_id"]').count(), 0);
  await form.locator('[name="modelId"]').fill(state.stableFacts.modelId);
  await form.locator('[name="fromTime"]').fill(state.stableFacts.fromTime);
  await form.locator('[name="toTime"]').fill(state.stableFacts.toTime);
  const filteredResponsePromise = page.waitForResponse((response) => {
    const target = new URL(response.url());
    return target.origin === CONTROL_API_URL
      && target.pathname === "/feedback/summary"
      && target.searchParams.get("model_id") === state.stableFacts.modelId
      && target.searchParams.get("from_time") === state.stableFacts.fromTime
      && target.searchParams.get("to_time") === state.stableFacts.toTime;
  }, { timeout: 30_000 });
  await form.locator('button[type="submit"]').click();
  const filteredResponse = await filteredResponsePromise;
  assert.equal(filteredResponse.status(), 200);
  assertSummaryPayload(await filteredResponse.json(), state.stableFacts);
  // 后端响应正确不代表 Console 已完成 refresh；必须等待筛选后的完整 DOM 原子快照收敛。
  const filteredDomSnapshot = await waitForFilteredSummaryDom(page, state.stableFacts);
  assert.equal(summaryDomMatchesStableFacts(filteredDomSnapshot, state.stableFacts), true);
  await page.locator(".feedback-summary-metrics").waitFor({ state: "visible", timeout: 30_000 });
  const metricText = await page.locator(".feedback-summary-metrics").innerText();
  assert.ok(metricText.includes(String(state.stableFacts.feedbackCount)));
  assert.ok(metricText.includes(`${state.stableFacts.averageRatingMilli / 1000} / 5`));
  assert.ok(metricText.includes(`${state.stableFacts.adoptionRateBps / 100}%`));
  const modelRows = page.locator(".feedback-model-row:not(.feedback-model-head)");
  assert.equal(await modelRows.count(), 1);
  const modelText = await modelRows.first().innerText();
  assert.ok(modelText.includes(state.stableFacts.modelAlias));
  if (state.stableFacts.modelDisplayName) assert.ok(modelText.includes(state.stableFacts.modelDisplayName));
  assert.ok(modelText.includes(String(state.stableFacts.feedbackCount)));
  const tagsText = await page.locator(".feedback-summary-tags").innerText();
  for (const tag of state.stableFacts.tagCounts) {
    assert.ok(tagsText.includes(TAG_LABELS[tag.tag]) && tagsText.includes(String(tag.count)));
  }
  await scan("page071:after_filter");
  return {
    filteredModelCount: 1,
    feedbackCount: state.stableFacts.feedbackCount,
    averageRatingMilli: state.stableFacts.averageRatingMilli,
    adoptionRateBps: state.stableFacts.adoptionRateBps,
    tagCount: state.stableFacts.tagCounts.length,
  };
}

/** 纯函数核对 reader 必须真实读取两页，viewer 则必须对目标 API 零请求、零响应。 */
function assertRoleTargetEvidence(role, evidence) {
  const requests = evidence.targetRequests ?? [];
  const responses = evidence.targetResponses ?? [];
  // viewer 的权限裁剪必须同时做到零导航 DOM 和零目标 API 预取，不能只隐藏按钮。
  if (role === "viewer") {
    assert.equal(requests.length, 0);
    assert.equal(responses.length, 0);
    return { targetRequestCount: 0, targetResponseCount: 0, targetPrefetchCount: 0 };
  }
  const getSurfaces = new Set(requests.filter((item) => item.method === "GET").map((item) => item.surface));
  for (const surface of ["request_log_list", "request_log_detail", "feedback_summary"]) {
    assert.ok(getSurfaces.has(surface));
  }
  assert.equal(requests.filter((item) => item.surface === "feedback_post").length, 0);
  // 已有反馈和汇总页都是只读验收，任何目标非 GET 请求都意味着页面制造了业务副作用。
  assert.ok(requests.every((item) => item.method === "GET"));
  assert.ok(responses.every((item) => item.method === "GET"));
  const successful = responses.filter((item) => item.method === "GET" && item.status === 200);
  for (const surface of ["request_log_list", "request_log_detail", "feedback_summary"]) {
    assert.ok(successful.some((item) => item.surface === surface));
  }
  assert.ok(successful.every((item) => item.requestIdPresent && item.traceIdPresent && item.noStore));
  return {
    targetRequestCount: requests.length,
    targetResponseCount: responses.length,
    targetPrefetchCount: 0,
    feedbackPostCount: 0,
  };
}

/** 最多两次执行真实注销；只有 HTTP 200 且登录页/Storage 终态同时成立才成功。 */
async function executeLogoutWithRetry({ attemptAction, verifyAction, maxAttempts = 2 }) {
  assert.ok(Number.isInteger(maxAttempts) && maxAttempts >= 1 && maxAttempts <= 3);
  const audit = { completed: false, attemptCount: 0, responseStatuses: [], authStorageCleared: false };
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    audit.attemptCount = attempt;
    let outcome;
    try {
      outcome = await attemptAction(attempt);
    } catch {
      audit.responseStatuses.push("attempt_error");
      continue;
    }
    const status = Number.isInteger(outcome?.status) ? outcome.status : null;
    audit.responseStatuses.push(status ?? "missing_response");
    if (status !== 200) continue;
    await verifyAction();
    audit.completed = true;
    audit.authStorageCleared = true;
    return audit;
  }
  const error = new Error("注销在有限重试后仍未成功");
  error.audit = { ...audit };
  // 有限次数耗尽必须阻止 passed，且抛出的固定文案不会携带底层认证异常。
  throw error;
}

/** 点击 UI 注销并复扫登录页、Storage 和网络证据。 */
async function logoutStrict(page, evidence, exactSecrets) {
  return executeLogoutWithRetry({
    async attemptAction() {
      const button = page.locator("#logout-button");
      if ((await button.count()) !== 1) return { status: null };
      const responsePromise = page.waitForResponse((response) => {
        const target = new URL(response.url());
        return target.origin === CONTROL_API_URL
          && target.pathname === "/auth/logout"
          && response.request().method() === "POST";
      }, { timeout: 10_000 }).catch(() => null);
      await button.click({ timeout: 10_000 });
      const response = await responsePromise;
      return { status: response?.status() ?? null };
    },
    async verifyAction() {
      await page.locator("#login-form").waitFor({ state: "visible", timeout: 10_000 });
      await scanBrowserCheckpoint(page, evidence, exactSecrets, {
        activeToken: null,
        loggedOut: true,
        label: "logout",
      });
    },
  });
}

/** 统一执行角色页面、finally 注销和 Context 关闭，任一失败都不能返回部分通过。 */
async function runRoleLifecycle({ stagePrefix, executeAction, logoutAction, closeAction }) {
  let result;
  let logout;
  const failures = [];
  try {
    result = await executeAction();
  } catch (error) {
    failures.push(toSafeStageError(error, `${stagePrefix}.page`, "page_failed"));
  }
  try {
    logout = await logoutAction();
  } catch (error) {
    failures.push(toSafeStageError(error, `${stagePrefix}.logout`, "logout_failed"));
  }
  try {
    await closeAction();
  } catch (error) {
    failures.push(toSafeStageError(error, `${stagePrefix}.context_close`, "context_close_failed"));
  }
  // 页面、注销或 Context 任一失败都要聚合上报，禁止遗漏权限或 Session 清理失败。
  if (failures.length) throw new AggregateError(failures, "浏览器角色生命周期失败");
  return { ...result, logout };
}

/** admin/developer 完成 PAGE-070、PAGE-071 和各自六张响应式截图。 */
async function runReaderRole(browser, state, role) {
  const userAgent = `prd049-browser-ui-${role}-${state.browserSuffix}`;
  const expectedOwner = {
    tenantId: state.runtime.tenantId,
    userId: role === "admin" ? state.runtime.adminUserId : state.accounts[role].userId,
    email: state.accounts[role].email,
    role,
  };
  // UI 登录同样在请求前登记随机 marker，避免浏览器收到 200 但 Playwright 未读取正文时泄漏 Session。
  state.sessionIntents.set(userAgent, expectedOwner);
  const context = await browser.newContext({ viewport: VIEWPORTS[0], userAgent });
  const page = await context.newPage();
  const evidence = await installBrowserGuards(context, page);
  let session = null;
  return runRoleLifecycle({
    stagePrefix: role,
    async executeAction() {
      session = await runAcceptanceStage(`${role}.login`, () => loginPage(page, state, role, "request-logs"));
      const scan = (label) => scanBrowserCheckpoint(page, evidence, state.secretRegistry, {
        activeToken: session.token,
        label: `${role}:${label}`,
      });
      const page070 = await runAcceptanceStage(
        `${role}.page070.verify`,
        () => verifyFeedbackDetailPage(page, state, scan),
      );
      const screenshots = [];
      const layouts = { page070: {}, page071: {} };
      for (const viewport of VIEWPORTS) {
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        layouts.page070[viewport.name] = await runAcceptanceStage(
          `${role}.page070.layout.${viewport.name}`,
          () => assertResponsiveLayout(page, viewport, ".request-logs-panel"),
        );
        screenshots.push(await capturePage(page, state, role, "page-070", viewport, scan));
      }
      const page071 = await runAcceptanceStage(
        `${role}.page071.verify`,
        () => verifyFeedbackSummaryPage(page, state, scan),
      );
      for (const viewport of VIEWPORTS) {
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        layouts.page071[viewport.name] = await runAcceptanceStage(
          `${role}.page071.layout.${viewport.name}`,
          () => assertResponsiveLayout(page, viewport, ".feedback-summary-panel"),
        );
        screenshots.push(await capturePage(page, state, role, "page-071", viewport, scan));
      }
      const target = assertRoleTargetEvidence(role, evidence);
      const privacy = await scan("final");
      return {
        role,
        page070,
        page071,
        layouts,
        screenshots,
        screenshotCount: screenshots.length,
        privacy,
        privacyCheckpointCount: evidence.checkpointCount,
        externalRequestCount: evidence.blockedRequestCount,
        browserErrorCount: evidence.browserErrors.length,
        ...target,
      };
    },
    async logoutAction() {
      // 登录尚未创建 Session 时无需伪造后端注销；一旦登记则必须走真实 UI 注销。
      if (!session) return { completed: true, required: false, attemptCount: 0, responseStatuses: [], authStorageCleared: true };
      // 注销证据只保留是否必需和脱敏审计，Token 继续留在内存扫描集合直到最终写盘。
      return { required: true, ...await logoutStrict(page, evidence, state.secretRegistry) };
    },
    async closeAction() {
      await context.close();
    },
  });
}

/** viewer 直达两条受限 hash 均回退 overview，并保持导航、业务 DOM 和目标 API 全为零。 */
async function runViewerRole(browser, state) {
  const role = "viewer";
  const userAgent = `prd049-browser-ui-${role}-${state.browserSuffix}`;
  const expectedOwner = {
    tenantId: state.runtime.tenantId,
    userId: state.accounts.viewer.userId,
    email: state.accounts.viewer.email,
    role,
  };
  // viewer 的 Session 也需要未知结果对账，零业务预取并不等于没有认证会话。
  state.sessionIntents.set(userAgent, expectedOwner);
  const context = await browser.newContext({ viewport: VIEWPORTS[0], userAgent });
  const page = await context.newPage();
  const evidence = await installBrowserGuards(context, page);
  let session = null;
  return runRoleLifecycle({
    stagePrefix: role,
    async executeAction() {
      session = await runAcceptanceStage(`${role}.login`, () => loginPage(page, state, role, "feedback-summary"));
      const scan = (label) => scanBrowserCheckpoint(page, evidence, state.secretRegistry, {
        activeToken: session.token,
        label: `${role}:${label}`,
      });
      const fallbacks = [];
      for (const navigation of RESTRICTED_NAVIGATIONS) {
        await page.evaluate((hash) => { window.location.hash = hash; }, navigation);
        await page.locator('.console-shell[data-active-navigation="overview"]').waitFor({
          state: "visible",
          timeout: 30_000,
        });
        await page.waitForTimeout(300);
        assert.equal(await page.locator(`[data-console-nav="${navigation}"]`).count(), 0);
        assert.equal(await page.locator(`.${navigation}-panel`).count(), 0);
        assert.equal(await page.locator(".request-feedback-section").count(), 0);
        await scan(`fallback:${navigation}`);
        fallbacks.push({ requested: navigation, active: "overview", restrictedDom: false });
      }
      const screenshots = [];
      const layouts = {};
      for (const viewport of VIEWPORTS) {
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        layouts[viewport.name] = await assertResponsiveLayout(page, viewport, "main");
        screenshots.push(await capturePage(page, state, role, "overview-fallback", viewport, scan));
      }
      const target = assertRoleTargetEvidence(role, evidence);
      const privacy = await scan("final");
      return {
        role,
        fallbacks,
        layouts,
        screenshots,
        screenshotCount: screenshots.length,
        privacy,
        privacyCheckpointCount: evidence.checkpointCount,
        externalRequestCount: evidence.blockedRequestCount,
        browserErrorCount: evidence.browserErrors.length,
        ...target,
      };
    },
    async logoutAction() {
      // viewer 虽然业务 API 零预取，仍持有认证 Session，因此成功登录后同样必须真实注销。
      if (!session) return { completed: true, required: false, attemptCount: 0, responseStatuses: [], authStorageCleared: true };
      // 真实注销和 Storage 清空共同证明受限角色没有把认证态遗留给下一 Context。
      return { required: true, ...await logoutStrict(page, evidence, state.secretRegistry) };
    },
    async closeAction() {
      await context.close();
    },
  });
}

/** 通过 API 尝试注销内存中的 bootstrap/角色 Session；失败仍继续数据库精确兜底。 */
async function logoutApiTokens(state) {
  let logoutCount = 0;
  const failures = [];
  for (const [role, token] of state.apiTokens) {
    try {
      const requestId = operationRequestId(`cleanup_logout_${role}`, state.browserSuffix);
      const { body } = await controlJson("/auth/logout", {
        method: "POST",
        headers: authHeaders(token, requestId),
      }, 200);
      assert.equal(body.logged_out, true);
      logoutCount += 1;
    } catch {
      failures.push(role);
    }
  }
  return { logoutCount, failures };
}

/**
 * 在同批次 SQLite 内按 tenant + 精确 ID/email 撤销本轮 Session、禁用临时账号。
 * 反馈、调用日志、模型统计等业务事实绝不更新；提交前后必须逐行指纹和租户总数一致。
 */
function cleanupTemporaryAccounts(state) {
  assert.ok(pathIsInside(state.paths.batchDirectory, state.databasePath));
  const database = new DatabaseSync(state.databasePath);
  database.exec("PRAGMA foreign_keys=ON");
  database.exec("PRAGMA busy_timeout=10000");
  database.exec("BEGIN IMMEDIATE");
  let revokedSessionCount = 0;
  let disabledTemporaryUserCount = 0;
  try {
    const processedSessionIds = new Set();
    const ownedUserIds = [];
    for (const [role, intent] of state.userIntents) {
      const rows = database.prepare(`
        SELECT user_id, tenant_id, email, display_name, role, status, created_at
        FROM users WHERE tenant_id=? AND email=?
      `).all(state.runtime.tenantId, intent.email);
      assert.ok(rows.length <= 1);
      if (!rows.length) continue;
      const row = rows[0];
      assert.ok(
        row.tenant_id === state.runtime.tenantId
          && row.email === intent.email
          && row.display_name === intent.displayName
          && row.role === role,
      );
      const registered = state.temporaryUserOwners.get(String(row.user_id));
      if (!registered) {
        // API 响应未知时只接纳本轮开始后创建的精确随机 intent，绝不碰历史同名账号。
        assert.ok(Date.parse(String(row.created_at)) >= state.startedAtMillis - 2000);
      } else {
        assert.ok(registered.email === intent.email && registered.role === role);
      }
      ownedUserIds.push(String(row.user_id));
    }
    assert.ok([...state.temporaryUserOwners.keys()].every((userId) => ownedUserIds.includes(userId)));
    for (const userId of ownedUserIds) {
      const sessionUpdate = database.prepare(`
        UPDATE sessions
        SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
        WHERE tenant_id=? AND user_id=? AND status='active'
      `).run(state.cleanupTime, state.cleanupTime, state.runtime.tenantId, userId);
      revokedSessionCount += Number(sessionUpdate.changes);
      const userUpdate = database.prepare(`
        UPDATE users SET status='disabled', updated_at=?
        WHERE tenant_id=? AND user_id=? AND status='active'
      `).run(state.cleanupTime, state.runtime.tenantId, userId);
      disabledTemporaryUserCount += Number(userUpdate.changes);
    }
    // 管理员只撤销本脚本登记的精确 Session ID，绝不扩大为 tenant/user 全量注销。
    for (const [sessionId, owner] of state.sessionOwners) {
      const row = database.prepare(`
        SELECT session_id, tenant_id, user_id, status FROM sessions
        WHERE tenant_id=? AND session_id=?
      `).get(owner.tenantId, sessionId);
      assert.ok(row && row.user_id === owner.userId);
      if (row.status === "active") {
        const update = database.prepare(`
          UPDATE sessions
          SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
          WHERE tenant_id=? AND session_id=? AND status='active'
        `).run(state.cleanupTime, state.cleanupTime, owner.tenantId, sessionId);
        assert.equal(Number(update.changes), 1);
        revokedSessionCount += 1;
      }
      processedSessionIds.add(sessionId);
    }
    // 对账登录“已提交但响应未知”的窗口；随机 marker + owner + 创建时间三重门禁避免碰到历史会话。
    for (const [marker, owner] of state.sessionIntents) {
      const rows = database.prepare(`
        SELECT session_id, tenant_id, user_id, status, user_agent, created_at
        FROM sessions
        WHERE tenant_id=? AND user_id=? AND user_agent=?
      `).all(owner.tenantId, owner.userId, marker);
      for (const row of rows) {
        const sessionId = String(row.session_id);
        assert.ok(
          row.tenant_id === owner.tenantId
            && row.user_id === owner.userId
            && row.user_agent === marker
            && Date.parse(String(row.created_at)) >= state.startedAtMillis - 2000,
        );
        // 已按精确 ID 处理或已由 API/UI 注销的 Session 不重复计数，只处理仍活动的未知结果。
        if (processedSessionIds.has(sessionId) || row.status !== "active") continue;
        const update = database.prepare(`
          UPDATE sessions
          SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
          WHERE tenant_id=? AND session_id=? AND user_id=? AND user_agent=? AND status='active'
        `).run(
          state.cleanupTime,
          state.cleanupTime,
          owner.tenantId,
          sessionId,
          owner.userId,
          marker,
        );
        assert.equal(Number(update.changes), 1);
        revokedSessionCount += 1;
        processedSessionIds.add(sessionId);
      }
      const remaining = database.prepare(`
        SELECT COUNT(*) AS total FROM sessions
        WHERE tenant_id=? AND user_id=? AND user_agent=? AND status='active'
      `).get(owner.tenantId, owner.userId, marker);
      assert.equal(Number(remaining.total), 0);
    }
    if (ownedUserIds.length) {
      const placeholders = ownedUserIds.map(() => "?").join(",");
      const activeUsers = database.prepare(`
        SELECT COUNT(*) AS total FROM users
        WHERE tenant_id=? AND user_id IN (${placeholders}) AND status='active'
      `).get(state.runtime.tenantId, ...ownedUserIds);
      const activeSessions = database.prepare(`
        SELECT COUNT(*) AS total FROM sessions
        WHERE tenant_id=? AND user_id IN (${placeholders}) AND status='active'
      `).get(state.runtime.tenantId, ...ownedUserIds);
      assert.equal(Number(activeUsers.total), 0);
      assert.equal(Number(activeSessions.total), 0);
    }
    const facts = database.prepare(`
      SELECT source_request_id, request_feedback_id, model_id, rating, accepted,
             tags_json, actor_type, created_at
      FROM request_feedbacks
      WHERE tenant_id=? AND source_request_id IN (?, ?)
      ORDER BY source_request_id
    `).all(state.runtime.tenantId, ...state.runtime.sourceRequestIds);
    const tenantFeedbackCount = Number(database.prepare(
      "SELECT COUNT(*) AS total FROM request_feedbacks WHERE tenant_id=?",
    ).get(state.runtime.tenantId).total);
    assert.equal(JSON.stringify(facts), state.stableFacts.immutableFingerprint);
    assert.equal(tenantFeedbackCount, state.stableFacts.tenantFeedbackCount);
    const foreignKeys = database.prepare("PRAGMA foreign_key_check").all();
    const integrity = database.prepare("PRAGMA integrity_check").get();
    assert.deepEqual(foreignKeys, []);
    assert.equal(Object.values(integrity)[0], "ok");
    database.exec("COMMIT");
    return {
      verified: true,
      disabledTemporaryUserCount,
      revokedSessionCount,
      feedbackFactsUnchanged: true,
      foreignKeyCheckClean: true,
      integrityCheckOk: true,
    };
  } catch (error) {
    database.exec("ROLLBACK");
    // 清理事务失败必须在回滚后继续抛出，不能用部分撤销结果掩盖活动 Session 或账号。
    throw error;
  } finally {
    database.close();
  }
}

/** 无论主流程成功或失败都执行清理；多个失败以固定阶段异常聚合。 */
async function runOwnedStateLifecycle({ executeAction, cleanupAction }) {
  let result;
  let cleanup;
  const failures = [];
  try {
    result = await executeAction();
  } catch (error) {
    failures.push(toSafeStageError(error, "browser.execute", "execution_failed"));
  }
  try {
    cleanup = await cleanupAction();
  } catch (error) {
    failures.push(toSafeStageError(error, "browser.cleanup", "cleanup_failed"));
  }
  // 主流程或清理任一失败都阻止结果写盘，聚合项只包含固定阶段和错误码。
  if (failures.length) throw new AggregateError(failures, "PRD-049 浏览器验收生命周期失败");
  return { result, cleanup };
}

/** 最终结果写盘前执行精确 Secret 和通用哨兵扫描。 */
function assertResultRedacted(result, exactSecrets = []) {
  assert.ok(!containsSecret(JSON.stringify(result), exactSecrets));
  return true;
}

/** 构造只驻内存的运行账本；稳定 ID 与凭据分别存放，结果不会序列化账本本身。 */
function createAcceptanceState({ paths, runtime, databasePath, stableFacts, adminEmail, adminPassword }) {
  const browserSuffix = randomBytes(6).toString("hex");
  const secretRegistry = collectConfiguredSecrets([adminPassword]);
  return {
    paths,
    runtime,
    databasePath,
    stableFacts,
    browserSuffix,
    startedAtMillis: Date.now(),
    cleanupTime: "",
    secretRegistry,
    accounts: { admin: { email: adminEmail, password: adminPassword } },
    apiTokens: new Map(),
    uiTokens: new Map(),
    sessionOwners: new Map(),
    sessionIntents: new Map(),
    userIntents: new Map(),
    temporaryUserOwners: new Map(),
  };
}

/** 启动真实 Chrome 并顺序执行三角色；Context 仍相互隔离，避免 Storage 跨角色复用。 */
async function runBrowserAcceptance(state) {
  const { chromium } = require("playwright");
  const browser = await chromium.launch({
    executablePath: CHROME_PATH,
    headless: process.env.METAHUB_BROWSER_HEADED !== "1",
  });
  try {
    const admin = await runReaderRole(browser, state, "admin");
    const developer = await runReaderRole(browser, state, "developer");
    const viewer = await runViewerRole(browser, state);
    return { admin, developer, viewer };
  } finally {
    await browser.close();
  }
}

/** 执行 API 注销和 SQLite 精确兜底；API 单点失败仍不会跳过本地终态核对。 */
async function cleanupAcceptance(state) {
  const apiLogout = await logoutApiTokens(state);
  state.cleanupTime = new Date().toISOString();
  const database = cleanupTemporaryAccounts(state);
  assert.deepEqual(apiLogout.failures, []);
  return { apiLogoutCount: apiLogout.logoutCount, ...database };
}

/** 验证本地命令行范围、Chrome、Runtime 和数据库都属于同一 PRD-049 批次。 */
function assertLocalScope(paths, databasePath, adminPassword) {
  assert.equal(process.env.METAHUB_CONFIRM_BROWSER_STATE_MUTATION, "1");
  assert.ok(adminPassword.length >= 8);
  assert.ok(statSync(CHROME_PATH).isFile());
  assert.ok(statSync(paths.runtimeStatePath).isFile());
  assert.ok(statSync(databasePath).isFile());
  assert.ok(pathIsInside(paths.batchDirectory, databasePath));
  return true;
}

/** 主入口：只有页面、权限、隐私、注销和数据库清理全部成功才写 passed 结果。 */
async function main() {
  const batch = String(process.env.METAHUB_PRD049_BATCH ?? "");
  const paths = resolveBatchPaths(WORKSPACE_ROOT, batch);
  const runtime = await loadRuntimeState(paths.runtimeStatePath);
  const databasePath = resolveRuntimeDatabasePath(paths.batchDirectory, runtime.databaseFile);
  const adminEmail = String(process.env.METAHUB_ADMIN_EMAIL ?? "admin@metahub.local").trim().toLowerCase();
  const adminPassword = String(
    process.env.METAHUB_PRD049_ADMIN_PASSWORD ?? process.env.METAHUB_ADMIN_PASSWORD ?? "",
  );
  assertLocalScope(paths, databasePath, adminPassword);
  const stableFacts = loadStableDatabaseFacts(databasePath, runtime, { adminEmail });
  const state = createAcceptanceState({ paths, runtime, databasePath, stableFacts, adminEmail, adminPassword });
  await mkdir(paths.browserDirectory, { recursive: true });
  await rm(paths.resultPath, { force: true });
  const lifecycle = await runOwnedStateLifecycle({
    async executeAction() {
      const services = await runAcceptanceStage("services.verify", verifyFixedServices);
      const accounts = await runAcceptanceStage("accounts.prepare", () => prepareAccounts(state));
      const backendRbac = await runAcceptanceStage("backend_rbac.verify", () => verifyBackendPermissions(state));
      const roles = await runAcceptanceStage("browser.roles", () => runBrowserAcceptance(state));
      return { services, accounts, backendRbac, roles };
    },
    cleanupAction: () => cleanupAcceptance(state),
  });
  const { admin, developer, viewer } = lifecycle.result.roles;
  const result = {
    status: "passed",
    prdId: "PRD-049",
    runtimeSuffix: runtime.suffix,
    services: lifecycle.result.services,
    accounts: lifecycle.result.accounts,
    backendRbac: lifecycle.result.backendRbac,
    admin,
    developer,
    viewer,
    viewportCount: VIEWPORTS.length,
    screenshotCount: admin.screenshotCount + developer.screenshotCount + viewer.screenshotCount,
    cleanup: lifecycle.cleanup,
  };
  assertResultRedacted(result, state.secretRegistry);
  const encoded = `${JSON.stringify(result, null, 2)}\n`;
  await writeFile(paths.resultPath, encoded, "utf8");
  process.stdout.write(encoded);
  state.secretRegistry.clear();
  state.apiTokens.clear();
  state.uiTokens.clear();
  state.accounts = {};
}

// 单元测试 import 时只暴露纯/helper 合同；命令行执行才连接服务、数据库和 Chrome。
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    await main();
  } catch (error) {
    const safe = toSafeStageError(error, "browser.main", "acceptance_failed");
    process.stderr.write(`${safe.message}\n`);
    process.exitCode = 1;
  }
}

export {
  BrowserAcceptanceStageError,
  assertBackendPermissionEvidence,
  assertPrd049DatabaseHardening,
  assertResultRedacted,
  assertRoleTargetEvidence,
  assertRuntimeStateKeyPrivacy,
  assertSensitiveSurfaceSnapshot,
  classifyTargetApi,
  collectConfiguredSecrets,
  containsSecret,
  decideBrowserNetworkRequest,
  deriveFeedbackExpectations,
  executeLogoutWithRetry,
  pathIsInside,
  resolveBatchPaths,
  resolveRuntimeDatabasePath,
  runAcceptanceStage,
  runOwnedStateLifecycle,
  runRoleLifecycle,
  summaryDomMatchesStableFacts,
  toSafeStageError,
  validateRuntimeStateDocument,
};
