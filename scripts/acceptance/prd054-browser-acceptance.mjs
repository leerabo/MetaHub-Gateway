/**
 * PRD-054 PAGE-079 请求解释中心真实浏览器验收。
 *
 * 脚本只访问调用方已启动的三个 HTTP 回环服务，依赖同批次 Runtime 证据，并把截图和
 * Browser JSON 独占写入 artifacts/prd054/<batch>。密码、Token、Prompt、Response、
 * Webhook URL/payload 和 source request_id 均不得进入 URL 或 Storage。
 */

import { createHash, randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { inflateSync } from "node:zlib";


// Playwright 只从调用方已准备的 NODE_PATH 解析；验收器绝不下载浏览器或安装依赖。
const require = createRequire(import.meta.url);
// 两档视口与 PRD 冻结口径一致，每档都覆盖 complete、partial 和 error 三种真实状态。
const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720, mode: "columns" }),
  Object.freeze({ name: "mobile-390x844", width: 390, height: 844, mode: "stack" }),
]);
// 页面与 API 网络仅允许显式本机回环 origin，禁止 DNS 名称、外网和 URL 凭据绕过。
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const SAFE_IDENTIFIER_PATTERN = /^[A-Za-z0-9._:-]{1,160}$/;
const BATCH_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$/;
const EXPLANATION_ID_PATTERN = /^rxe_[0-9a-f]{32}$/;
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);
const STAGE_ORDER = Object.freeze([
  "authentication", "rate_limit", "budget", "dlp", "policy", "routing",
  "keypool", "provider", "usage_and_finops", "logs_and_webhooks",
]);
const SECTION_ORDER = Object.freeze([
  "authentication", "rate_limit", "budget", "dlp", "policy", "routing",
  "keypool", "provider_response", "usage_and_finops", "logs", "webhooks",
]);
const EVIDENCE_STATES = new Set(["complete", "partial", "not_recorded", "not_applicable", "pending"]);
const STAGE_STATUSES = new Set(["passed", "blocked", "failed", "pending", "not_evaluated", "not_recorded"]);

// 运行 fixture 中的四个明文陷阱只用于页面内布尔扫描，绝不允许写入 Browser 证据。
const PRIVATE_WEBHOOK_URL = "http://127.0.0.1:9/prd054-private-hook";
const PRIVATE_WEBHOOK_TOKEN = "Bearer prd054-private-token-value";
const PRIVATE_PROVIDER_RESPONSE = "prd054 private provider response body";
const PRIVATE_ERROR_SUMMARY = "prd054 private provider error summary";
const PRIVATE_FIXTURE_VALUES = Object.freeze([
  PRIVATE_WEBHOOK_URL,
  PRIVATE_WEBHOOK_TOKEN,
  PRIVATE_PROVIDER_RESPONSE,
  PRIVATE_ERROR_SUMMARY,
]);

// URL 与最终证据都不得承载认证材料、业务正文或扩大 Explain 披露面的查询键。
const SENSITIVE_URL_KEY_PATTERN = /password|token|secret|authorization|prompt|response|provider[_-]?key|api[_-]?key|payload|include[_-]?raw|tenant[_-]?id/i;
const FORBIDDEN_EVIDENCE_KEYS = new Set([
  "authorization", "credential_id", "encrypted_secret", "endpoint_url", "error_summary",
  "key_hash", "key_plaintext", "key_secret", "key_value", "match_text", "messages",
  "payload", "payload_json", "password", "prompt", "provider_key", "raw_usage",
  "response", "response_body", "secret", "token",
]);
// 凭据形态正则与事实字段白名单共同阻止敏感正文进入最终验收证据。
const FORBIDDEN_VALUE_PATTERN = /Bearer\s+[^\s"']{8,}|\bmhv_[A-Za-z0-9_-]{16,}\b|\bsk[-_][A-Za-z0-9_-]{12,}\b/i;

// 每个事实 kind 的 exact-key 集合独立于页面实现，Browser 可发现 DTO 与 renderer 同时漂移。
const FACT_VALUE_KEYS = Object.freeze({
  authentication: [
    "virtual_key_id", "virtual_key_prefix", "environment_id", "environment_name",
    "project_id", "project_name", "application_id", "application_name",
    "snapshot_version", "policy_snapshot_status",
  ],
  rate_limit: ["decision", "error_code"],
  budget_evaluation: [
    "budget_rule_id", "scope_type", "scope_id", "budget_date", "spent_before_cny",
    "estimated_request_cost_cny", "projected_cost_cny", "budget_cny",
    "threshold_percent", "budget_status", "action_taken", "reason_code",
  ],
  budget_scope_event: [
    "event_id", "budget_rule_id", "scope_type", "scope_id", "action_taken",
    "posted_cost_micros", "reserved_cost_micros", "estimated_request_cost_micros",
    "projected_cost_micros", "budget_micros",
  ],
  dlp_match: [
    "dlp_rule_id", "rule_name", "rule_type", "action", "message_index",
    "match_text_hash", "match_length",
  ],
  policy_snapshot: [
    "snapshot_version", "snapshot_status", "policy_id", "policy_action_type",
    "policy_target_model_id", "policy_decision", "policy_reason_code", "routing_policy_id",
    "routing_policy_version", "routing_mode",
  ],
  routing_decision: [
    "decision_status", "decision_reason", "requested_model", "selected_model_id",
    "selected_internal_model_id", "provider_id", "provider_code", "error_code",
  ],
  routing_candidate: [
    "model_id", "provider_id", "candidate_role", "configured_priority", "rank", "status",
    "reason_code", "health_state", "health_fresh", "keypool_state", "price_available",
    "price_source", "price_schema_version", "currency", "estimated_cost_amount",
    "quality_component_bps", "cost_component_bps", "latency_component_bps",
    "weighted_score_bps",
  ],
  cache_affinity: [
    "attempt_sequence", "routing_attempt_id", "target_kind", "target_generation",
    "provider_id", "provider_code", "endpoint_preference_applied",
    "endpoint_preference_state", "binding_state_before", "selection_reason",
    "estimated_hit", "outcome_status",
  ],
  routing_attempt: [
    "attempt_sequence", "attempt_role", "trigger_type", "trigger_error_code", "model_id",
    "internal_model_id", "provider_id", "provider_code", "status", "error_code",
    "error_category", "upstream_http_status", "retryable", "latency_ms", "usage_status",
    "estimated_cost_cny",
  ],
  usage: [
    "input_tokens", "output_tokens", "total_tokens", "cache_tokens", "cost_amount",
    "cost_cny", "cost_usd", "currency", "price_schema_version", "price_snapshot_id",
    "estimated", "usage_source", "routing_attempt_id",
  ],
  finops_entry: [
    "ledger_sequence", "event_id", "entry_type", "direction", "amount_micros", "currency",
    "normalized_cny_micros", "cny_normalization_status", "price_evidence_status",
    "price_snapshot_source", "posting_source", "quota_type", "price_snapshot_id",
    "price_schema_version", "input_tokens", "output_tokens", "total_tokens", "cache_tokens",
    "estimated", "status",
  ],
  request_log: [
    "source_status", "error_code", "stream_status", "latency_ms", "fallback_triggered",
    "routing_attempt_count", "final_routing_attempt_id",
  ],
  stream_session: [
    "status", "inter_token_timeout_ms", "chunk_count", "bytes_sent", "first_chunk_at",
    "last_chunk_at", "error_code", "completed_at",
  ],
  webhook_delivery: [
    "event_id", "event_type", "webhook_endpoint_id", "endpoint_name", "status",
    "attempt_count", "max_attempts", "http_status", "duration_ms", "error_code",
    "next_retry_at", "delivery_kind", "replayed_from_delivery_id",
  ],
  webhook_attempt: [
    "webhook_delivery_id", "webhook_endpoint_id", "attempt_no", "status", "http_status",
    "duration_ms", "error_code",
  ],
});
// 区块与事实类型的固定映射用于阻止服务端把未知事实注入错误页面区域。
const SECTION_FACT_KINDS = Object.freeze({
  authentication: new Set(["authentication"]),
  rate_limit: new Set(["rate_limit"]),
  budget: new Set(["budget_evaluation", "budget_scope_event"]),
  dlp: new Set(["dlp_match"]),
  policy: new Set(["policy_snapshot"]),
  routing: new Set(["routing_decision", "routing_candidate"]),
  keypool: new Set(["routing_candidate", "cache_affinity", "routing_attempt"]),
  provider_response: new Set(["routing_attempt"]),
  usage_and_finops: new Set(["usage", "finops_entry"]),
  logs: new Set(["request_log", "stream_session"]),
  webhooks: new Set(["webhook_delivery", "webhook_attempt"]),
});


/** 只暴露固定 stage/code 的验收异常，避免 Playwright URL、DOM 或响应正文进入终端。 */
class BrowserAcceptanceFailure extends Error {
  /** 构造公开失败分类；调用方不得传入不可信文本。 */
  constructor(stage, code) {
    super(`prd054_browser_acceptance_failed stage=${stage} code=${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 以固定分类终止验收，不拼接任何不可信值。 */
function fail(stage, code) {
  throw new BrowserAcceptanceFailure(stage, code);
}


/** 执行失败关闭断言；condition 之外的信息不会进入异常。 */
function requireCondition(condition, stage, code) {
  if (!condition) fail(stage, code);
}


/** 把未知异常压缩成固定错误，已脱敏异常则保留首个失败阶段。 */
function toSafeFailure(error, stage, code = "stage_failed") {
  if (error instanceof BrowserAcceptanceFailure) return error;
  return new BrowserAcceptanceFailure(stage, code);
}


/** 运行一个浏览器操作，并折叠 Playwright 可能携带 URL/DOM 的原始异常。 */
async function runSafeBrowserOperation(stage, code, operation) {
  try {
    return await operation();
  } catch (error) {
    // Playwright 异常可能携带 URL 或 DOM，必须折叠为固定公开分类。
    throw toSafeFailure(error, stage, code);
  }
}


/** 规范化 HTTP 回环 origin，拒绝凭据、路径、查询、fragment 和缺失端口。 */
function normalizeLoopbackOrigin(value, label = "origin") {
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


/** 从脚本位置恢复唯一 MetaHub 根，不接受环境变量重定义信任边界。 */
function workspaceRoot() {
  const resolved = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
  requireCondition(path.basename(resolved).toLowerCase() === "metahub", "config.workspace", "unexpected_root");
  return resolved;
}


/** 把证据目录硬限制为 artifacts/prd054 下恰好一个安全批次目录。 */
function resolveArtifactDirectory(value, root = workspaceRoot()) {
  const artifactRoot = path.resolve(root, "artifacts", "prd054");
  const candidate = path.resolve(root, String(value));
  const relative = path.relative(artifactRoot, candidate);
  requireCondition(relative && !relative.startsWith("..") && !path.isAbsolute(relative), "config.artifact", "outside_root");
  requireCondition(!relative.includes(path.sep), "config.artifact", "batch_depth");
  requireCondition(BATCH_NAME_PATTERN.test(relative), "config.artifact", "batch_name");
  return candidate;
}


/** 从环境读取管理员身份，并在 Chrome 启动前删除所有口令别名。 */
function readEnvironmentIdentity(env = process.env) {
  const adminPassword = String(env.METAHUB_PRD054_ADMIN_PASSWORD || env.METAHUB_ADMIN_PASSWORD || "");
  const tenantSlug = String(env.METAHUB_PRD054_TENANT_SLUG || env.METAHUB_TENANT_SLUG || "default").trim();
  const adminEmail = String(env.METAHUB_PRD054_ADMIN_EMAIL || env.METAHUB_ADMIN_EMAIL || "admin@metahub.local").trim().toLowerCase();
  for (const name of [
    "METAHUB_PRD054_ADMIN_PASSWORD", "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD054_ADMIN_EMAIL", "METAHUB_ADMIN_EMAIL",
    "METAHUB_PRD054_TENANT_SLUG", "METAHUB_TENANT_SLUG",
  ]) delete env[name];
  requireCondition(adminPassword.length >= 8, "config.identity", "admin_password_missing");
  requireCondition(Boolean(tenantSlug), "config.identity", "tenant_slug_missing");
  requireCondition(adminEmail.includes("@"), "config.identity", "admin_email_invalid");
  // 身份只在当前浏览器进程内使用，禁止进入最终验收证据或日志。
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
      ["--artifact-dir", "--console-url", "--control-url", "--gateway-url"].includes(name),
      "config.arguments",
      "unknown_argument",
    );
    requireCondition(index + 1 < argv.length && !values.has(name), "config.arguments", "argument_invalid");
    values.set(name, String(argv[++index]));
  }
  const artifactValue = values.get("--artifact-dir") || env.METAHUB_PRD054_ARTIFACT_DIR || "";
  requireCondition(Boolean(artifactValue), "config.artifact", "missing");
  const root = workspaceRoot();
  const config = {
    root,
    artifactDirectory: resolveArtifactDirectory(artifactValue, root),
    consoleOrigin: normalizeLoopbackOrigin(values.get("--console-url") || env.METAHUB_PRD054_CONSOLE_URL || "http://localhost:5173", "console_origin"),
    controlOrigin: normalizeLoopbackOrigin(values.get("--control-url") || env.METAHUB_PRD054_CONTROL_URL || "http://localhost:8001", "control_origin"),
    gatewayOrigin: normalizeLoopbackOrigin(values.get("--gateway-url") || env.METAHUB_PRD054_GATEWAY_URL || "http://localhost:8000", "gateway_origin"),
    chromePath: String(env.METAHUB_CHROME_PATH || "").trim(),
    headed: headed || env.METAHUB_BROWSER_HEADED === "1",
    ...identity,
  };
  requireCondition(new Set([config.consoleOrigin, config.controlOrigin, config.gatewayOrigin]).size === 3, "config.origins", "must_be_distinct");
  return config;
}


/** 统一字段名后再做隐私判断，避免 camelCase 或连字符绕过。 */
function normalizeEvidenceKey(key) {
  return String(key)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}


/** 递归拒绝最终证据中的凭据、正文槽位和非 JSON 值。 */
function assertSafeEvidence(value, knownSensitive = [], stage = "evidence.privacy") {
  const walk = (node) => {
    if (Array.isArray(node)) {
      for (const child of node) walk(child);
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
  let encoded;
  try {
    encoded = JSON.stringify(value);
  } catch {
    fail(stage, "not_serializable");
  }
  requireCondition(!FORBIDDEN_VALUE_PATTERN.test(encoded), stage, "credential_pattern");
  for (const sensitive of knownSensitive.filter(Boolean)) {
    requireCondition(!encoded.includes(sensitive), stage, "known_sensitive_value");
  }
  return true;
}


/** 读取一个必需普通对象；失败只暴露固定字段类别。 */
function requiredObject(value, stage, code = "not_object") {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), stage, code);
  return value;
}


/** 校验并返回一个公开不透明 ID，不把原值拼入错误。 */
function requiredIdentifier(value, stage, code = "identifier_invalid") {
  const normalized = String(value ?? "");
  requireCondition(SAFE_IDENTIFIER_PATTERN.test(normalized), stage, code);
  return normalized;
}


/** 校验 Runtime 公开 HTTP 摘要的状态、链路和可选错误码。 */
function validateRuntimeHttpEvidence(value, expectedStatus, stage, expectedCode = null) {
  const evidence = requiredObject(value, stage, "missing");
  requireCondition(evidence.status === expectedStatus, stage, "status_mismatch");
  requiredIdentifier(evidence.request_id, stage, "request_id_missing");
  requiredIdentifier(evidence.trace_id, stage, "trace_id_missing");
  if (expectedCode !== null) requireCondition(evidence.code === expectedCode, stage, "code_mismatch");
  return evidence;
}


/** 验证 Browser 的前置 Runtime 证据已经完整覆盖 0044、RBAC、DTO、outbox 和清理。 */
function validateRuntimeEvidence(payload) {
  const root = requiredObject(payload, "runtime_evidence", "not_object");
  assertSafeEvidence(root, [], "runtime_evidence.privacy");
  requireCondition(root.schema_version === 1 && root.prd_id === "PRD-054" && root.status === "passed", "runtime_evidence", "not_passed");
  const checks = requiredObject(root.checks, "runtime_evidence.checks", "missing");
  requireCondition(checks.database?.migration_0044_applied === true, "runtime_evidence.database", "migration");
  requireCondition(checks.database?.integrity_verified === true, "runtime_evidence.database", "integrity");
  requireCondition(
    checks.services?.gateway_http_200 === true
      && checks.services?.control_http_200 === true
      && checks.services?.console_http_200 === true,
    "runtime_evidence.services",
    "not_ready",
  );
  requireCondition(
    checks.roles?.developer_session_bound === true && checks.roles?.viewer_session_bound === true,
    "runtime_evidence.roles",
    "missing",
  );
  requireCondition(
    checks.api?.admin_allowed === true
      && checks.api?.developer_allowed === true
      && checks.api?.viewer_denied_before_input === true
      && checks.api?.cross_tenant_same_shape_404 === true,
    "runtime_evidence.api",
    "rbac_or_tenant",
  );
  requireCondition(
    checks.api?.complete_contract?.status === "complete"
      && checks.api?.partial_contract?.status === "partial"
      && checks.api?.complete_contract?.section_count === 11
      && checks.api?.partial_contract?.decision_stage_count === 10,
    "runtime_evidence.api",
    "dto_contract",
  );
  requireCondition(
    checks.outbox?.source_outbox_persisted === true
      && checks.outbox?.stable_event_ids_unique === true
      && checks.outbox?.failed_request_fact_count === 0,
    "runtime_evidence.outbox",
    "not_verified",
  );
  requireCondition(
    checks.cleanup?.admin_session_revoked === true
      && checks.cleanup?.developer_session_revoked === true
      && checks.cleanup?.viewer_session_revoked === true
      && checks.cleanup?.session_revoked === true,
    "runtime_evidence.cleanup",
    "not_revoked",
  );
  const resources = requiredObject(root.generated_resources, "runtime_evidence.resources", "missing");
  const completeRequestId = requiredIdentifier(resources.complete_request_id, "runtime_evidence.resources", "complete_id");
  const partialRequestId = requiredIdentifier(resources.partial_request_id, "runtime_evidence.resources", "partial_id");
  requireCondition(completeRequestId !== partialRequestId, "runtime_evidence.resources", "request_ids_equal");
  const http = requiredObject(root.http, "runtime_evidence.http", "missing");
  validateRuntimeHttpEvidence(http.admin_complete, 200, "runtime_evidence.http.admin_complete");
  validateRuntimeHttpEvidence(http.developer_complete, 200, "runtime_evidence.http.developer_complete");
  validateRuntimeHttpEvidence(http.admin_partial, 200, "runtime_evidence.http.admin_partial");
  validateRuntimeHttpEvidence(http.viewer_malformed, 403, "runtime_evidence.http.viewer", "auth.permission_denied");
  validateRuntimeHttpEvidence(http.cross_tenant, 404, "runtime_evidence.http.cross", "request_explanation.not_found");
  validateRuntimeHttpEvidence(http.invalid_query, 422, "runtime_evidence.http.query", "request_explanation.request_invalid");
  // Browser 仅继承已验证安全标识，原始 Runtime JSON 不直接进入页面证据。
  return { completeRequestId, partialRequestId, tenantId: requiredIdentifier(resources.tenant_id, "runtime_evidence.resources", "tenant_id") };
}


/** 读取并校验同批次 Runtime JSON，同时返回安全摘要和文件哈希。 */
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


/** 判断浏览器请求是否严格落在三服务白名单，且 URL 不携带敏感查询键。 */
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


/** 安装浏览器网络、页面异常与控制台错误门禁，并独立计数 Explain GET。 */
async function installBrowserGuards(context, page, config) {
  const allowedOrigins = new Set([config.consoleOrigin, config.controlOrigin, config.gatewayOrigin]);
  const expectedHttpStatuses = [];
  const summary = {
    blocked_request_count: 0,
    browser_error_count: 0,
    console_error_count: 0,
    expected_http_console_error_count: 0,
    request_explain_get_count: 0,
    expectHttpStatus(status) {
      expectedHttpStatuses.push(status);
    },
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
  page.on("pageerror", () => {
    summary.browser_error_count += 1;
  });
  page.on("console", (message) => {
    // 只有错误级控制台消息影响验收，普通业务日志不应被误判为页面故障。
    if (message.type() !== "error") return;
    const text = String(message.text());
    const index = expectedHttpStatuses.findIndex((status) => text.startsWith(`Failed to load resource: the server responded with a status of ${status}`));
    if (index >= 0) {
      expectedHttpStatuses.splice(index, 1);
      summary.expected_http_console_error_count += 1;
      return;
    }
    summary.console_error_count += 1;
    summary.browser_error_count += 1;
  });
  return summary;
}


/** 安全读取 Playwright 响应 JSON；解析异常不能把正文带入错误。 */
async function readJsonResponse(response, stage) {
  try {
    const payload = await response.json();
    return requiredObject(payload, stage, "not_object");
  } catch (error) {
    // 响应正文可能包含内部信息，解析失败只能转成固定安全分类。
    throw toSafeFailure(error, stage, "invalid_json");
  }
}


/** 使用真实 Console 表单登录指定角色，Token 只由页面认证模块写入 sessionStorage。 */
async function loginThroughConsole(page, config, identity) {
  await runSafeBrowserOperation(`browser.login.${identity.role}`, "navigation", () => page.goto(`${config.consoleOrigin}/`, { waitUntil: "domcontentloaded", timeout: 30_000 }));
  const form = page.locator("#login-form");
  await form.waitFor({ state: "visible", timeout: 30_000 });
  await form.locator('[name="tenantSlug"]').fill(config.tenantSlug);
  await form.locator('[name="email"]').fill(identity.email);
  await form.locator('[name="password"]').fill(identity.password);
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      // 登录响应必须同时匹配 Control origin、固定路径和 POST 方法，避免旁路响应抢占。
      return target.origin === config.controlOrigin && target.pathname === "/auth/login" && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30_000 });
  await form.locator('button[type="submit"]').click();
  const response = await responsePromise;
  requireCondition(response.status() === 200, `browser.login.${identity.role}`, "status_mismatch");
  await page.locator("#logout-button").waitFor({ state: "visible", timeout: 45_000 });
  const navCount = await page.locator('[data-console-nav="request-explanations"]').count();
  requireCondition(navCount === (identity.role === "viewer" ? 0 : 1), `browser.login.${identity.role}`, "navigation_visibility");
  return { role: identity.role, request_explanation_navigation_visible: navCount === 1 };
}


/** 在管理员页面内经公开 /users API 创建一次性 developer/viewer 登录身份。 */
async function createRestrictedRoleUsers(page, config) {
  const credentials = [];
  for (const role of ["developer", "viewer"]) {
    const email = `prd054-browser-${role}-${randomBytes(6).toString("hex")}@metahub.local`;
    const password = `Prd054Browser!${randomBytes(18).toString("hex")}`;
    const requestId = `req_prd054_browser_create_${role}_${randomBytes(5).toString("hex")}`;
    const traceId = `trc_prd054_browser_create_${role}_${randomBytes(5).toString("hex")}`;
    const result = await runSafeBrowserOperation(`browser.users.${role}`, "request_failed", () => page.evaluate(async (input) => {
      const access = sessionStorage.getItem("metahub.auth_token");
      // 缺少页面登录令牌时立即失败关闭，不能匿名创建低权限验收用户。
      if (!access) return { status: 0, role: "", requestId: "" };
      const response = await fetch(`${input.controlOrigin}/users`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${access}`,
          "Content-Type": "application/json",
          "X-Request-ID": input.requestId,
          "X-Trace-ID": input.traceId,
        },
        body: JSON.stringify({
          email: input.email,
          display_name: `PRD054 Browser ${input.role}`,
          password: input.password,
          role: input.role,
          status: "active",
        }),
      });
      let body = {};
      try { body = await response.json(); } catch { body = {}; }
      return { status: response.status, role: body.user?.role ?? "", requestId: body.request_id ?? "" };
    }, { controlOrigin: config.controlOrigin, role, email, password, requestId, traceId }));
    requireCondition(result.status === 201 && result.role === role, `browser.users.${role}`, "create_status");
    requiredIdentifier(result.requestId, `browser.users.${role}`, "request_id");
    credentials.push({ role, email, password });
  }
  return credentials;
}


/** 校验浏览器收到的 RequestExplanation exact DTO、阶段顺序、事实白名单与精度类型。 */
function validateExplanationProjection(payload, expectedSourceRequestId, expectedStatus) {
  const document = requiredObject(payload, "browser.dto.response", "not_object");
  assertSafeEvidence(document, PRIVATE_FIXTURE_VALUES, "browser.dto.privacy");
  requireCondition(new Set(Object.keys(document)).size === 3 && ["request_explanation", "request_id", "trace_id"].every((key) => key in document), "browser.dto.response", "keys");
  requiredIdentifier(document.request_id, "browser.dto.response", "request_id");
  requiredIdentifier(document.trace_id, "browser.dto.response", "trace_id");
  const explanation = requiredObject(document.request_explanation, "browser.dto.explanation", "missing");
  const expectedKeys = new Set([
    "request_explanation_id", "source_request_id", "source_trace_id", "status",
    "evidence_version", "captured_at", "summary", "decision_chain", ...SECTION_ORDER,
    "missing_sections", "truncated_sections", "view_event_status",
  ]);
  requireCondition(Object.keys(explanation).length === expectedKeys.size && Object.keys(explanation).every((key) => expectedKeys.has(key)), "browser.dto.explanation", "keys");
  requireCondition(EXPLANATION_ID_PATTERN.test(String(explanation.request_explanation_id)), "browser.dto.explanation", "id");
  requireCondition(explanation.source_request_id === expectedSourceRequestId, "browser.dto.explanation", "source_request_id");
  requireCondition(explanation.status === expectedStatus, "browser.dto.explanation", "status");
  requireCondition(explanation.evidence_version === "request_explanation_v1" && explanation.view_event_status === "pending", "browser.dto.explanation", "event_contract");
  requireCondition(Array.isArray(explanation.decision_chain) && explanation.decision_chain.length === 10, "browser.dto.stages", "count");
  requireCondition(explanation.decision_chain.every((entry, index) => {
    const keys = Object.keys(requiredObject(entry, "browser.dto.stage", "not_object"));
    return entry.stage === STAGE_ORDER[index]
      && STAGE_STATUSES.has(entry.status)
      && EVIDENCE_STATES.has(entry.evidence_state)
      && keys.length === 6
      && ["stage", "status", "evidence_state", "reason_code", "occurred_at", "evidence_refs"].every((key) => keys.includes(key));
  }), "browser.dto.stages", "contract");
  const missing = Array.isArray(explanation.missing_sections) ? explanation.missing_sections : null;
  const truncated = Array.isArray(explanation.truncated_sections) ? explanation.truncated_sections : null;
  requireCondition(missing && truncated, "browser.dto.completeness", "arrays");
  if (expectedStatus === "complete") requireCondition(missing.length === 0 && truncated.length === 0, "browser.dto.completeness", "complete_gap");
  else requireCondition(missing.length + truncated.length > 0, "browser.dto.completeness", "partial_without_gap");
  for (const sectionName of SECTION_ORDER) {
    const section = requiredObject(explanation[sectionName], `browser.dto.section.${sectionName}`, "missing");
    requireCondition(Object.keys(section).length === 4 && ["evidence_state", "reason_code", "facts", "truncated"].every((key) => key in section), `browser.dto.section.${sectionName}`, "keys");
    requireCondition(EVIDENCE_STATES.has(section.evidence_state) && Array.isArray(section.facts), `browser.dto.section.${sectionName}`, "state_or_facts");
    for (const factValue of section.facts) {
      const fact = requiredObject(factValue, `browser.dto.fact.${sectionName}`, "not_object");
      requireCondition(Object.keys(fact).length === 4 && ["kind", "fact_id", "occurred_at", "values"].every((key) => key in fact), `browser.dto.fact.${sectionName}`, "keys");
      requireCondition(SECTION_FACT_KINDS[sectionName].has(fact.kind), `browser.dto.fact.${sectionName}`, "kind");
      const values = requiredObject(fact.values, `browser.dto.fact.${sectionName}`, "values");
      const expected = FACT_VALUE_KEYS[fact.kind];
      requireCondition(Object.keys(values).length === expected.length && expected.every((key) => key in values), `browser.dto.fact.${sectionName}`, "value_keys");
      requireCondition(Object.values(values).every((value) => value === null || ["string", "number", "boolean"].includes(typeof value)), `browser.dto.fact.${sectionName}`, "nested_value");
    }
  }
  requireCondition(explanation.summary?.estimated_cost_amount === null || typeof explanation.summary?.estimated_cost_amount === "string", "browser.dto.summary", "decimal_precision");
  return { status: expectedStatus, decision_stage_count: 10, section_count: 11, privacy_verified: true };
}


/** 校验成功响应 Header/正文链路与 Explain 防缓存头，并返回脱敏 HTTP 摘要。 */
function responseEvidence(response, payload, stage) {
  const requestId = requiredIdentifier(response.headers()["x-request-id"], stage, "request_header");
  const traceId = requiredIdentifier(response.headers()["x-trace-id"], stage, "trace_header");
  requireCondition(payload.request_id === requestId && payload.trace_id === traceId, stage, "chain_mismatch");
  requireCondition(response.headers()["cache-control"] === "no-store, private", stage, "cache_control");
  requireCondition(response.headers().pragma === "no-cache", stage, "pragma");
  requireCondition(String(response.headers().vary || "").toLowerCase().split(",").map((item) => item.trim()).includes("authorization"), stage, "vary");
  return { status: response.status(), request_id: requestId, trace_id: traceId, no_store: true };
}


/** 打开 PAGE-079；页面 URL 只能使用固定 hash，不能携带 source request_id。 */
async function openExplanationPage(page, config) {
  await page.locator('[data-console-nav="request-explanations"]').click();
  await page.locator("#request-explanations-title").waitFor({ state: "visible", timeout: 30_000 });
  const location = new URL(page.url());
  requireCondition(location.origin === config.consoleOrigin && location.pathname === "/" && location.search === "", "browser.navigation", "url_shape");
  requireCondition(location.hash === "#request-explanations", "browser.navigation", "hash");
}


/** 从 PAGE-079 表单发起一次真实查询，验证响应与目标 UI 状态。 */
async function queryExplanation(page, config, requestId, expectedStatus, tracker) {
  const input = page.locator('#request-explanation-form input[name="requestId"]');
  await input.fill(requestId);
  // 仅错误状态预登记预期 404，其他浏览器控制台错误仍应计为失败。
  if (expectedStatus === "error") tracker.expectHttpStatus(404);
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      return target.origin === config.controlOrigin
        && target.pathname === `/request-explain/${encodeURIComponent(requestId)}`
        && response.request().method() === "GET";
    } catch {
      return false;
    }
  }, { timeout: 30_000 });
  await page.locator('#request-explanation-form button[type="submit"]').click();
  const response = await responsePromise;
  const payload = await readJsonResponse(response, `browser.query.${expectedStatus}`);
  // 不存在请求必须清除旧正文并展示统一链路号，禁止残留上一次解释内容。
  if (expectedStatus === "error") {
    requireCondition(response.status() === 404 && payload.code === "request_explanation.not_found", "browser.query.error", "contract");
    requireCondition(payload.request_id === response.headers()["x-request-id"], "browser.query.error", "request_id_chain");
    const alert = page.locator('.request-explanation-empty[role="alert"]');
    await alert.waitFor({ state: "visible", timeout: 30_000 });
    const alertText = await alert.innerText();
    requireCondition(alertText.includes(payload.request_id), "browser.query.error", "request_id_hidden");
    // 错误态保留响应式双列骨架用于稳定布局，但不得残留任何成功态阶段、区块或历史解释正文。
    requireCondition(
      await page.locator('.request-explanation-body[data-explanation-status="error"]').count() === 1,
      "browser.query.error",
      "error_body_missing",
    );
    requireCondition(
      await page.locator('.request-explanation-body:not([data-explanation-status="error"]), .request-explanation-stage, [data-request-explanation-section]').count() === 0,
      "browser.query.error",
      "stale_body",
    );
    return {
      http: { status: 404, code: "request_explanation.not_found", request_id: requiredIdentifier(payload.request_id, "browser.query.error", "request_id") },
      contract: { status: "error", unified_error_visible: true, stale_explanation_hidden: true },
    };
  }
  requireCondition(response.status() === 200, `browser.query.${expectedStatus}`, "status_mismatch");
  const contract = validateExplanationProjection(payload, requestId, expectedStatus);
  const http = responseEvidence(response, payload, `browser.query.${expectedStatus}`);
  const body = page.locator(`.request-explanation-body[data-explanation-status="${expectedStatus}"]`);
  await body.waitFor({ state: "visible", timeout: 30_000 });
  requireCondition(await page.locator(".request-explanation-stage").count() === 10, `browser.dom.${expectedStatus}`, "stage_count");
  requireCondition(await page.locator("[data-request-explanation-section]").count() === 11, `browser.dom.${expectedStatus}`, "section_count");
  const domPrivacy = await page.evaluate(({ sourceRequestId, traps }) => {
    const html = document.documentElement?.outerHTML ?? "";
    const storage = [localStorage, sessionStorage].flatMap((area) => {
      const values = [];
      for (let index = 0; index < area.length; index += 1) {
        const key = area.key(index);
        if (key) values.push(`${key}=${area.getItem(key) ?? ""}`);
      }
      return values;
    }).join("\n");
    return {
      trap_hits: traps.filter((value) => html.includes(value) || storage.includes(value)).length,
      source_id_url_hits: location.href.includes(sourceRequestId) ? 1 : 0,
      source_id_storage_hits: storage.includes(sourceRequestId) ? 1 : 0,
    };
  }, { sourceRequestId: requestId, traps: PRIVATE_FIXTURE_VALUES });
  requireCondition(domPrivacy.trap_hits === 0 && domPrivacy.source_id_url_hits === 0 && domPrivacy.source_id_storage_hits === 0, `browser.dom.${expectedStatus}`, "privacy");
  if (expectedStatus === "partial") {
    requireCondition(await page.locator("[data-request-explanation-section][open]").count() > 0, "browser.dom.partial", "abnormal_not_expanded");
  }
  return { http, contract };
}


/** 等待两个动画帧，使响应式样式和滚动位置在测量前稳定。 */
async function settleLayout(page) {
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}


/** 测量 PAGE-079 横向溢出、裁切、控件重叠及桌面/移动布局关系。 */
async function readLayoutMetrics(page) {
  return page.evaluate(() => {
    const root = document.querySelector(".request-explanations-panel");
    const body = root?.querySelector(".request-explanation-body");
    const summary = body?.querySelector(".request-explanation-summary");
    const evidence = body?.querySelector(".request-explanation-evidence");
    if (!root) return null;
    const visible = (element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 1 && box.height > 1;
    };
    /** 检查元素是否被祖先容器横向裁剪，避免截图掩盖不可见内容。 */
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
    const rootBox = root.getBoundingClientRect();
    const controls = [...root.querySelectorAll("button,input,select,textarea")].filter(visible);
    let controlOverlapCount = 0;
    for (let left = 0; left < controls.length; left += 1) {
      for (let right = left + 1; right < controls.length; right += 1) {
        const a = controls[left].getBoundingClientRect();
        const b = controls[right].getBoundingClientRect();
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2) controlOverlapCount += 1;
      }
    }
    const content = [...root.querySelectorAll([
      ".request-explanation-heading", ".request-explanation-form", ".request-explanation-summary",
      ".request-explanation-stage", ".request-explanation-section", ".request-explanation-record",
    ].join(","))].filter(visible);
    const horizontalContainers = [...root.querySelectorAll([
      ".request-explanation-form", ".request-explanation-body", ".request-explanation-summary-grid",
      ".request-explanation-evidence", ".request-explanation-sections",
    ].join(","))].filter(visible);
    const summaryBox = summary?.getBoundingClientRect() ?? null;
    const evidenceBox = evidence?.getBoundingClientRect() ?? null;
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
      root_left: rootBox.left,
      root_right: rootBox.right,
      root_client_width: root.clientWidth,
      root_scroll_width: root.scrollWidth,
      clipped_control_count: controls.filter((element) => {
        const box = element.getBoundingClientRect();
        return box.left < -1 || box.right > innerWidth + 1 || clippedByAncestor(element);
      }).length,
      clipped_content_count: content.filter(clippedByAncestor).length,
      horizontal_container_overflow_count: horizontalContainers.filter((element) => element.scrollWidth > element.clientWidth + 1).length,
      control_overlap_count: controlOverlapCount,
      summary_box: summaryBox ? { left: summaryBox.left, top: summaryBox.top, right: summaryBox.right, bottom: summaryBox.bottom } : null,
      evidence_box: evidenceBox ? { left: evidenceBox.left, top: evidenceBox.top, right: evidenceBox.right, bottom: evidenceBox.bottom } : null,
    };
  });
}


/** 将布局指标冻结为全零问题证据，并核对桌面双列与移动单列关系。 */
function assertLayoutMetrics(metrics, viewport, stage = "browser.layout") {
  requireCondition(metrics && typeof metrics === "object", stage, "root_missing");
  requireCondition(metrics.viewport_width === viewport.width && metrics.viewport_height === viewport.height, stage, "viewport");
  requireCondition(metrics.document_scroll_width <= viewport.width + 1 && metrics.body_scroll_width <= viewport.width + 1, stage, "document_overflow");
  requireCondition(metrics.root_left >= -1 && metrics.root_right <= viewport.width + 1, stage, "root_overflow");
  requireCondition(metrics.root_scroll_width <= metrics.root_client_width + 1, stage, "root_scroll_overflow");
  requireCondition(metrics.clipped_control_count === 0 && metrics.clipped_content_count === 0, stage, "clipping");
  requireCondition(metrics.horizontal_container_overflow_count === 0 && metrics.control_overlap_count === 0, stage, "overlap_or_container");
  requireCondition(metrics.summary_box && metrics.evidence_box, stage, "columns_missing");
  if (viewport.mode === "columns") {
    requireCondition(metrics.evidence_box.left >= metrics.summary_box.right - 2, stage, "desktop_not_columns");
  } else {
    requireCondition(metrics.evidence_box.top >= metrics.summary_box.bottom - 2, stage, "mobile_not_stacked");
  }
  return {
    horizontal_overflow: false,
    clipped_control_count: 0,
    clipped_content_count: 0,
    overlap_count: 0,
    responsive_mode: viewport.mode,
  };
}


/** 证明截图锚点与当前视口相交，避免把离屏空白误作页面证据。 */
function assertViewportIntersection(box, viewport, stage = "browser.layout") {
  requireCondition(box && [box.x, box.y, box.width, box.height].every(Number.isFinite), stage, "anchor_box");
  requireCondition(box.width > 0 && box.height > 0, stage, "anchor_empty");
  requireCondition(box.x < viewport.width && box.x + box.width > 0 && box.y < viewport.height && box.y + box.height > 0, stage, "anchor_outside");
  return { anchor_intersects_viewport: true };
}


/** PNG Paeth 预测器只用于复原截图像素，不输出任何图像内容。 */
function paethPredictor(left, up, upperLeft) {
  const estimate = left + up - upperLeft;
  const distances = [Math.abs(estimate - left), Math.abs(estimate - up), Math.abs(estimate - upperLeft)];
  if (distances[0] <= distances[1] && distances[0] <= distances[2]) return left;
  if (distances[1] <= distances[2]) return up;
  return upperLeft;
}


/** 解析 8-bit RGB/RGBA PNG，核对尺寸并证明截图像素非透明、非单色。 */
function inspectPngPixels(buffer, viewport, stage = "browser.screenshot") {
  requireCondition(Buffer.isBuffer(buffer), stage, "not_buffer");
  requireCondition(buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])), stage, "png_signature");
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
        width: data.readUInt32BE(0), height: data.readUInt32BE(4), bitDepth: data[8],
        colorType: data[9], compression: data[10], filter: data[11], interlace: data[12],
      };
    } else if (type === "IDAT") compressed.push(data);
    offset = end + 4;
    if (type === "IEND") break;
  }
  requireCondition(header && compressed.length > 0, stage, "png_chunks");
  requireCondition(header.bitDepth === 8 && [2, 6].includes(header.colorType) && header.compression === 0 && header.filter === 0 && header.interlace === 0, stage, "png_encoding");
  requireCondition(header.width === viewport.width && header.height === viewport.height, stage, "png_dimensions");
  const channels = header.colorType === 6 ? 4 : 3;
  const stride = header.width * channels;
  let raw;
  try { raw = inflateSync(Buffer.concat(compressed)); } catch { fail(stage, "png_inflate"); }
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
    if (pixels[index] !== first[0] || pixels[index + 1] !== first[1] || pixels[index + 2] !== first[2]) variance = true;
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


/** 生成固定截图相对路径；状态和视口只能来自冻结枚举。 */
function screenshotFilename(stateName, viewportName) {
  requireCondition(["complete", "partial", "error"].includes(stateName), "browser.screenshot", "state_name");
  requireCondition(VIEWPORTS.some((item) => item.name === viewportName), "browser.screenshot", "viewport_name");
  return `screenshots/${stateName}-${viewportName}.png`;
}


/** 捕获当前状态的布局与像素证据；用户菜单被遮罩，截图文件禁止覆盖。 */
async function captureCurrentState(page, config, stateName, viewport) {
  const stage = `browser.capture.${stateName}.${viewport.name}`;
  const anchor = page.locator("#request-explanations-title");
  await anchor.scrollIntoViewIfNeeded();
  await settleLayout(page);
  const layout = assertLayoutMetrics(await readLayoutMetrics(page), viewport, stage);
  const intersection = assertViewportIntersection(await anchor.boundingBox(), viewport, stage);
  const relative = screenshotFilename(stateName, viewport.name);
  const target = path.join(config.artifactDirectory, relative);
  const masks = [];
  const userMenu = page.locator(".user-menu");
  if (await userMenu.count() > 0 && await userMenu.first().isVisible()) masks.push(userMenu.first());
  const buffer = await page.screenshot({ type: "png", fullPage: false, mask: masks, animations: "disabled" });
  try {
    await writeFile(target, buffer, { flag: "wx" });
  } catch {
    fail(stage, "screenshot_write_failed");
  }
  return {
    layout: { ...layout, ...intersection },
    screenshot: { file: relative.replaceAll("\\", "/"), state: stateName, viewport: viewport.name, ...inspectPngPixels(buffer, viewport, stage) },
  };
}


/** 在页面上下文扫描 URL、DOM 和 Storage；认证 Token 只做布尔判断，不离开页面。 */
async function readPrivacyMetrics(page, sourceRequestIds = []) {
  return page.evaluate(({ authKeys, requestIds, traps }) => {
    const html = document.documentElement?.outerHTML ?? "";
    const url = location.href;
    const entries = [];
    for (const [storageName, storage] of [["local", localStorage], ["session", sessionStorage]]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key) entries.push({ storageName, key, value: String(storage.getItem(key) ?? "") });
      }
    }
    const storageText = entries.map(({ key, value }) => `${key}=${value}`).join("\n");
    const authToken = sessionStorage.getItem(authKeys[0]);
    return {
      local_storage_count: entries.filter((item) => item.storageName === "local").length,
      session_auth_slot_count: authKeys.filter((key) => sessionStorage.getItem(key) !== null).length,
      forbidden_storage_key_count: entries.filter(({ key }) => !authKeys.includes(key) && /password|token|secret|prompt|response|payload|request[_-]?id/i.test(key)).length,
      request_id_url_or_storage_hits: requestIds.filter((value) => url.includes(value) || storageText.includes(value)).length,
      private_fixture_hits: traps.filter((value) => html.includes(value) || storageText.includes(value) || url.includes(value)).length,
      bearer_dom_hits: (html.match(/Bearer\s+[^"\s]{8,}/gi) ?? []).length,
      auth_token_exposed: Boolean(authToken) && (html.includes(authToken) || url.includes(authToken)),
    };
  }, { authKeys: AUTH_STORAGE_KEYS, requestIds: sourceRequestIds, traps: PRIVATE_FIXTURE_VALUES });
}


/** 把登录态隐私扫描压缩为稳定证据，任一 source ID/陷阱/认证材料命中都失败。 */
function assertBrowserPrivacy(metrics, stage = "browser.privacy") {
  requireCondition(metrics.local_storage_count === 0, stage, "local_storage");
  requireCondition(metrics.session_auth_slot_count === AUTH_STORAGE_KEYS.length, stage, "auth_slots");
  requireCondition(metrics.forbidden_storage_key_count === 0, stage, "forbidden_storage_key");
  requireCondition(metrics.request_id_url_or_storage_hits === 0, stage, "source_id_persisted");
  requireCondition(metrics.private_fixture_hits === 0 && metrics.bearer_dom_hits === 0, stage, "private_value");
  requireCondition(metrics.auth_token_exposed === false, stage, "auth_token_exposed");
  // 最终只返回零命中布尔摘要，扫描到的 DOM 或 Storage 正文绝不进入证据。
  return { source_id_persistence_hits: 0, private_value_hits: 0, auth_storage_contract_valid: true };
}


/** 校验登出后的 Storage、Explain DOM 和旧 source ID 均已同步清空。 */
function verifySessionCleanup(metrics, stage = "browser.session_cleanup") {
  requireCondition(metrics && typeof metrics === "object", stage, "metrics_missing");
  requireCondition(metrics.local_storage_count === 0 && metrics.session_storage_count === 0, stage, "storage_not_empty");
  requireCondition(metrics.explanation_dom_count === 0, stage, "explanation_dom_present");
  requireCondition(metrics.source_id_dom_hits === 0, stage, "source_id_dom_present");
  return { storage_cleared: true, explanation_dom_cleared: true, source_id_cleared: true };
}


/** 真实点击注销，并在登录页核对 PAGE-079 状态和认证 Storage 已完全销毁。 */
async function logoutAndVerify(page, config, sourceRequestIds = []) {
  const responsePromise = page.waitForResponse((response) => {
    try {
      const target = new URL(response.url());
      // 注销响应必须绑定 Control origin、固定路径和 POST 方法，避免采用无关响应。
      return target.origin === config.controlOrigin && target.pathname === "/auth/logout" && response.request().method() === "POST";
    } catch {
      return false;
    }
  }, { timeout: 30_000 });
  await page.locator("#logout-button").click();
  const response = await responsePromise;
  requireCondition(response.status() === 200, "browser.logout", "status_mismatch");
  await page.locator("#login-form").waitFor({ state: "visible", timeout: 30_000 });
  const metrics = await page.evaluate(({ requestIds }) => {
    const html = document.documentElement?.outerHTML ?? "";
    return {
      local_storage_count: localStorage.length,
      session_storage_count: sessionStorage.length,
      explanation_dom_count: document.querySelectorAll(".request-explanations-panel, #request-explanations-title, .request-explanation-body").length,
      source_id_dom_hits: requestIds.filter((value) => html.includes(value)).length,
    };
  }, { requestIds: sourceRequestIds });
  // 清理结果只保留稳定布尔字段，不返回 Storage 或页面原始文本。
  return verifySessionCleanup(metrics);
}


/** 异常路径优先使用页面内 Token 调用真实 logout，再清空 Storage；只返回撤销布尔值。 */
async function revokeSessionAfterFailure(page, controlOrigin) {
  try {
    return await page.evaluate(async (origin) => {
      const access = sessionStorage.getItem("metahub.auth_token");
      let revoked = false;
      if (access) {
        try {
          const response = await fetch(`${origin}/auth/logout`, {
            method: "POST",
            headers: { Authorization: `Bearer ${access}`, "X-Request-ID": "req_prd054_browser_failure_cleanup" },
          });
          revoked = response.status === 200;
        } catch { revoked = false; }
      }
      localStorage.clear();
      sessionStorage.clear();
      return revoked;
    }, controlOrigin);
  } catch {
    return false;
  }
}


/** 验证 developer 真实可查询，viewer 手写 hash 仍保持零 DOM、零 Explain GET。 */
async function verifyRestrictedRoles(page, config, credentials, runtime, tracker) {
  const evidence = {};
  const developer = credentials.find((item) => item.role === "developer");
  const viewer = credentials.find((item) => item.role === "viewer");
  requireCondition(developer && viewer, "browser.roles", "credentials_missing");
  await loginThroughConsole(page, config, developer);
  await openExplanationPage(page, config);
  const developerQuery = await queryExplanation(page, config, runtime.completeRequestId, "complete", tracker);
  evidence.developer = {
    allowed: true,
    dto_status: developerQuery.contract.status,
    cleanup: await logoutAndVerify(page, config, [runtime.completeRequestId]),
  };
  await loginThroughConsole(page, config, viewer);
  const before = tracker.request_explain_get_count;
  await page.evaluate(() => { location.hash = "#request-explanations"; });
  await page.waitForTimeout(250);
  requireCondition(await page.locator('[data-console-nav="request-explanations"]').count() === 0, "browser.viewer", "navigation_visible");
  requireCondition(await page.locator("#request-explanations-title, .request-explanations-panel").count() === 0, "browser.viewer", "dom_visible");
  requireCondition(tracker.request_explain_get_count === before, "browser.viewer", "api_prefetched");
  evidence.viewer = {
    zero_navigation: true,
    zero_dom: true,
    zero_request_explain_get: true,
    cleanup: await logoutAndVerify(page, config, [runtime.completeRequestId, runtime.partialRequestId]),
  };
  return evidence;
}


/** 以独占创建方式发布 Browser JSON；已存在证据不得被重跑覆盖。 */
async function publishEvidence(directory, evidence) {
  const target = path.join(directory, "browser-acceptance.json");
  const encoded = `${JSON.stringify(evidence, null, 2)}\n`;
  try {
    await writeFile(target, encoded, { encoding: "utf8", flag: "wx" });
  } catch {
    fail("evidence.publish", "write_failed");
  }
  return target;
}


/** 执行六状态截图、三角色真实页面、网络/隐私和 Session 清理验收。 */
async function executeBrowserAcceptance(config) {
  const runtimeEvidence = await loadRuntimeEvidence(config.artifactDirectory);
  await mkdir(path.join(config.artifactDirectory, "screenshots"), { recursive: true });
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
  const adminIdentity = { role: "admin", email: config.adminEmail, password: config.adminPassword };
  const screenshots = [];
  const layouts = {};
  const states = {};
  let restrictedCredentials = [];
  let authenticated = false;
  let cleanupCount = 0;
  try {
    await loginThroughConsole(page, config, adminIdentity);
    authenticated = true;
    restrictedCredentials = await createRestrictedRoleUsers(page, config);
    await openExplanationPage(page, config);
    const missingRequestId = `req_prd054_browser_missing_${randomBytes(6).toString("hex")}`;
    for (const viewport of VIEWPORTS) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const targets = [
        ["complete", runtimeEvidence.runtime.completeRequestId],
        ["partial", runtimeEvidence.runtime.partialRequestId],
        ["error", missingRequestId],
      ];
      for (const [stateName, requestId] of targets) {
        const result = await queryExplanation(page, config, requestId, stateName, tracker);
        const captured = await captureCurrentState(page, config, stateName, viewport);
        states[`${viewport.name}:${stateName}`] = result.contract;
        layouts[`${viewport.name}:${stateName}`] = captured.layout;
        screenshots.push(captured.screenshot);
      }
    }
    const privacy = assertBrowserPrivacy(
      await readPrivacyMetrics(page, [runtimeEvidence.runtime.completeRequestId, runtimeEvidence.runtime.partialRequestId]),
    );
    const adminCleanup = await logoutAndVerify(page, config, [runtimeEvidence.runtime.completeRequestId, runtimeEvidence.runtime.partialRequestId, missingRequestId]);
    authenticated = false;
    cleanupCount += 1;
    const roles = await verifyRestrictedRoles(page, config, restrictedCredentials, runtimeEvidence.runtime, tracker);
    cleanupCount += 2;
    requireCondition(tracker.blocked_request_count === 0, "browser.network", "request_blocked");
    requireCondition(tracker.browser_error_count === 0 && tracker.console_error_count === 0, "browser.errors", "unexpected_error");
    requireCondition(tracker.request_explain_get_count === 7, "browser.network", "explain_get_count");
    const evidence = {
      schema_version: 1,
      prd_id: "PRD-054",
      status: "passed",
      runtime_evidence_sha256: runtimeEvidence.sha256,
      generated_resources: {
        complete_request_id: runtimeEvidence.runtime.completeRequestId,
        partial_request_id: runtimeEvidence.runtime.partialRequestId,
      },
      checks: {
        admin: { allowed: true, navigation_visible: true, cleanup: adminCleanup },
        roles,
        states,
        layouts,
        privacy,
        screenshots: { count: screenshots.length, all_non_blank: screenshots.every((item) => item.non_blank_pixels) },
        network: {
          blocked_request_count: tracker.blocked_request_count,
          request_explain_get_count: tracker.request_explain_get_count,
          viewer_prefetch_count: 0,
        },
        errors: {
          browser_error_count: tracker.browser_error_count,
          console_error_count: tracker.console_error_count,
          expected_http_console_error_count: tracker.expected_http_console_error_count,
        },
        session_cleanup: { role_count: cleanupCount, all_roles_cleared: cleanupCount === 3 },
      },
      screenshots,
    };
    const sensitive = [
      config.adminPassword,
      ...restrictedCredentials.flatMap((item) => [item.email, item.password]),
      ...PRIVATE_FIXTURE_VALUES,
    ];
    assertSafeEvidence(evidence, sensitive);
    await publishEvidence(config.artifactDirectory, evidence);
    return evidence;
  } finally {
    // 中途失败仍必须撤销已登录 Session，防止验收凭据留在运行库中。
    if (authenticated) await revokeSessionAfterFailure(page, config.controlOrigin);
    restrictedCredentials = restrictedCredentials.map((item) => ({ ...item, email: "", password: "" }));
    await context.close().catch(() => undefined);
    await browser.close().catch(() => undefined);
  }
}


/** CLI 入口只输出固定成功摘要或脱敏失败分类。 */
async function main() {
  try {
    const config = parseArguments();
    await executeBrowserAcceptance(config);
    process.stdout.write('{"prd_id":"PRD-054","status":"passed","evidence":"browser-acceptance.json"}\n');
    return 0;
  } catch (error) {
    const safe = toSafeFailure(error, "browser.unhandled", "unexpected");
    process.stderr.write(`${safe.message}\n`);
    return 1;
  }
}


export {
  BrowserAcceptanceFailure,
  VIEWPORTS,
  assertBrowserPrivacy,
  assertLayoutMetrics,
  assertSafeEvidence,
  assertViewportIntersection,
  browserRequestAllowed,
  inspectPngPixels,
  loadRuntimeEvidence,
  normalizeLoopbackOrigin,
  readEnvironmentIdentity,
  resolveArtifactDirectory,
  responseEvidence,
  screenshotFilename,
  validateExplanationProjection,
  validateRuntimeEvidence,
  verifySessionCleanup,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
