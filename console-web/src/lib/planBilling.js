/**
 * PAGE-110/111 套餐与额度控制面客户端。
 *
 * tenant 与 actor 只来自 Bearer Session。本模块严格校验 no-store、链路镜像和 exact DTO，
 * 未知字段、跨租户 owner、金额守恒或状态组合异常都折叠为固定客户端错误。
 */

import { AuthApiError } from "./auth.js";

// 标识、金额、时间与 exact 字段常量共同限定客户端接受的后端合同边界。
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const PLAN_ID = /^pln_[0-9a-f]{32}$/;
const BUCKET_ID = /^qbk_[0-9a-f]{32}$/;
const ADJUSTMENT_ID = /^qadj_[0-9a-f]{32}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const CNY_AMOUNT = /^(?:0|[1-9][0-9]*)\.[0-9]{6}$/;
const POSITIVE_CNY_AMOUNT = /^(?:0\.(?!000000)[0-9]{6}|[1-9][0-9]*\.[0-9]{6})$/;
const VISIBLE_IDEMPOTENCY_KEY = /^[\x21-\x7e]{8,128}$/;
const MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807n;

const QUOTA_ORDER = Object.freeze(["trial", "free", "gift", "paid"]);
const PLAN_KEYS = Object.freeze([
  "plan_id", "tenant_id", "display_name", "base_tier", "effective_tier", "status",
  "trial_status", "trial_started_at", "trial_ends_at", "trial_cycle", "state_revision",
  "quota_generation", "created_at", "updated_at",
]);
const BUCKET_KEYS = Object.freeze([
  "quota_bucket_id", "tenant_id", "plan_id", "quota_type", "granted", "consumed",
  "reserved", "available", "status", "expires_at", "state_revision",
  "allocation_consumed", "allocation_count", "created_at", "updated_at",
]);
const PLAN_RESPONSE_KEYS = Object.freeze([
  "plan", "quota_buckets", "trial_cycle_changed", "request_id", "trace_id",
]);
const OVERAGE_KEYS = Object.freeze(["amount", "settlement_count"]);
const ADJUSTMENT_KEYS = Object.freeze([
  "quota_adjustment_id", "tenant_id", "plan_id", "quota_type", "direction", "amount",
  "actor_user_id", "operation_request_id", "resulting_bucket_revision", "created_at",
]);
const QUOTA_RESPONSE_KEYS = Object.freeze([
  "plan", "quota_buckets", "overage", "recent_adjustments", "request_id", "trace_id",
]);
const ADJUSTMENT_RESPONSE_KEYS = Object.freeze([
  "adjustment", "quota_bucket", "plan", "idempotent_replay", "request_id", "trace_id",
]);
const PLAN_BODY_KEYS = Object.freeze([
  "expected_revision", "display_name", "base_tier", "status", "trial_ends_at",
]);
const ADJUSTMENT_BODY_KEYS = Object.freeze([
  "quota_type", "direction", "amount", "reason", "expected_bucket_revision",
]);
const ERROR_KEYS = Object.freeze([
  "code", "message", "request_id", "trace_id", "suggestion", "diagnosis",
]);
const DIAGNOSIS_KEYS = Object.freeze([
  "rule_id", "category", "reason", "suggestion", "action", "severity", "retryable",
  "matched_by", "safe_summary",
]);

const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.token_expired": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有所需的套餐或额度权限",
  "plan.request_invalid": "套餐请求不合法",
  "plan.not_found": "当前租户没有套餐",
  "plan.revision_conflict": "套餐版本已变化，请重新读取后再保存",
  "plan.suspended": "当前套餐已暂停",
  "plan.unavailable": "套餐服务暂不可用",
  "quota.request_invalid": "额度请求不合法",
  "quota.adjustment_conflict": "额度已变化或当前余额不足",
  "quota.not_found": "当前租户没有对应额度桶",
  "quota.unavailable": "额度服务暂不可用",
  "plan.response_invalid": "套餐响应合同不完整",
  "quota.response_invalid": "额度响应合同不完整",
});

// 错误状态表只允许已知业务错误与 HTTP 状态精确配对，其他响应视为合同损坏。
const ERROR_STATUS = Object.freeze({
  "auth.token_missing": 401,
  "auth.token_invalid": 401,
  "auth.token_expired": 401,
  "auth.session_revoked": 401,
  "auth.permission_denied": 403,
  "plan.request_invalid": 422,
  "plan.not_found": 404,
  "plan.revision_conflict": 409,
  "plan.suspended": 409,
  "plan.unavailable": 503,
  "quota.request_invalid": 422,
  "quota.adjustment_conflict": 409,
  "quota.not_found": 404,
  "quota.unavailable": 503,
});

let requestSequence = 0;

/** 读取当前认证租户的唯一套餐和四类额度摘要。 */
export async function getCurrentPlan({
  controlApiUrl,
  token,
  tenantId,
  signal,
  requestId = createPlanBillingRequestId("plan_get"),
}) {
  requireTenant(tenantId, "plan.request_invalid");
  const payload = await requestJson(`${controlApiUrl}/plans`, {
    headers: authorizationHeaders(token, requestId),
    signal,
  }, new Set([200]), "plan.response_invalid");
  const normalized = normalizePlanResponse(payload, tenantId);
  // 套餐响应无法通过 exact 合同或租户校验时，必须收敛为固定客户端错误。
  if (!normalized) throw clientError("plan.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** 使用完整 revision CAS 创建或修改当前认证租户套餐。 */
export async function patchCurrentPlan({
  controlApiUrl,
  token,
  tenantId,
  request,
  signal,
  requestId = createPlanBillingRequestId("plan_patch"),
}) {
  requireTenant(tenantId, "plan.request_invalid");
  const body = normalizePlanPatchRequest(request);
  const payload = await requestJson(`${controlApiUrl}/plans`, {
    method: "PATCH",
    headers: {
      ...authorizationHeaders(token, requestId),
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal,
  }, new Set([200]), "plan.response_invalid");
  const normalized = normalizePlanResponse(payload, tenantId, body);
  // 套餐写响应无法通过 exact 合同或租户校验时，不得接纳服务端正文。
  if (!normalized) throw clientError("plan.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** 读取当前认证租户的四类额度、独立 overage 和最近调整摘要。 */
export async function getQuotaBuckets({
  controlApiUrl,
  token,
  tenantId,
  signal,
  requestId = createPlanBillingRequestId("quota_get"),
}) {
  requireTenant(tenantId, "quota.request_invalid");
  const payload = await requestJson(`${controlApiUrl}/quota-buckets`, {
    headers: authorizationHeaders(token, requestId),
    signal,
  }, new Set([200]), "quota.response_invalid");
  const normalized = normalizeQuotaBucketsResponse(payload, tenantId);
  // 额度响应无法通过 exact 合同或租户校验时，必须收敛为固定客户端错误。
  if (!normalized) throw clientError("quota.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** 创建额度调整，或以同一内存幂等键恢复完全相同的调整事实。 */
export async function createQuotaAdjustment({
  controlApiUrl,
  token,
  tenantId,
  request,
  idempotencyKey,
  signal,
  requestId = createPlanBillingRequestId("quota_post"),
}) {
  requireTenant(tenantId, "quota.request_invalid");
  const body = normalizeQuotaAdjustmentRequest(request);
  const key = requireIdempotencyKey(idempotencyKey);
  const payload = await requestJson(`${controlApiUrl}/quota-adjustments`, {
    method: "POST",
    headers: {
      ...authorizationHeaders(token, requestId),
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
    body: JSON.stringify(body),
    signal,
  }, new Set([200, 201]), "quota.response_invalid");
  const normalized = normalizeQuotaAdjustmentResponse(payload, tenantId, body);
  // 调整响应无法通过 exact 合同或租户校验时，不得更新任何本地额度事实。
  if (!normalized) throw clientError("quota.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** 将套餐表单收紧为后端唯一接受的五字段 JSON。 */
export function normalizePlanPatchRequest(value) {
  if (!exact(value, PLAN_BODY_KEYS)) throw clientError("plan.request_invalid", 422);
  const displayName = normalizeDisplayText(value.display_name, 80);
  if (
    !integer(value.expected_revision, 0, Number.MAX_SAFE_INTEGER)
    || !displayName
    || !["free", "paid"].includes(value.base_tier)
    || !["active", "suspended"].includes(value.status)
    || !(value.trial_ends_at === null || utc(value.trial_ends_at))
  ) throw clientError("plan.request_invalid", 422);
  return Object.freeze({
    expected_revision: value.expected_revision,
    display_name: displayName,
    base_tier: value.base_tier,
    status: value.status,
    trial_ends_at: value.trial_ends_at,
  });
}

/** 将额度 Dialog 收紧为后端唯一接受的五字段 JSON。 */
export function normalizeQuotaAdjustmentRequest(value) {
  if (!exact(value, ADJUSTMENT_BODY_KEYS)) throw clientError("quota.request_invalid", 422);
  const reason = normalizeDisplayText(value.reason, 500);
  if (
    !QUOTA_ORDER.includes(value.quota_type)
    || !["credit", "debit"].includes(value.direction)
    || !positiveMoney(value.amount)
    || !reason
    || !integer(value.expected_bucket_revision, 1, Number.MAX_SAFE_INTEGER)
  ) throw clientError("quota.request_invalid", 422);
  return Object.freeze({
    quota_type: value.quota_type,
    direction: value.direction,
    amount: value.amount,
    reason,
    expected_bucket_revision: value.expected_bucket_revision,
  });
}

/** 校验 PAGE-110 成功响应以及 PATCH 意图镜像。 */
export function normalizePlanResponse(payload, tenantId, request = null) {
  if (
    !exact(payload, PLAN_RESPONSE_KEYS)
    || typeof payload.trial_cycle_changed !== "boolean"
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
    || !Array.isArray(payload.quota_buckets)
  ) return null;
  const plan = payload.plan === null ? null : normalizePlan(payload.plan, tenantId);
  if (payload.plan !== null && !plan) return null;
  const buckets = normalizeBuckets(payload.quota_buckets, tenantId, plan?.plan_id ?? null);
  if (!buckets) return null;
  if ((plan === null) !== (buckets.length === 0)) return null;
  if (request) {
    const trialEndMatches = request.trial_ends_at === null
      ? (
        (plan?.trial_status === "not_started" && plan.trial_ends_at === null)
        || (plan?.trial_status === "expired" && plan.trial_ends_at !== null)
      )
      : plan?.trial_ends_at === request.trial_ends_at;
    if (
      !plan
      || plan.display_name !== request.display_name
      || plan.base_tier !== request.base_tier
      || plan.status !== request.status
      || !trialEndMatches
      || plan.state_revision !== request.expected_revision + 1
    ) return null;
  }
  return Object.freeze({ ...payload, plan, quota_buckets: buckets });
}

/** 校验 PAGE-111 成功响应，overage 始终与四类 Bucket 分离。 */
export function normalizeQuotaBucketsResponse(payload, tenantId) {
  if (
    !exact(payload, QUOTA_RESPONSE_KEYS)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
    || !Array.isArray(payload.quota_buckets)
    || !Array.isArray(payload.recent_adjustments)
    || payload.recent_adjustments.length > 50
    || !exact(payload.overage, OVERAGE_KEYS)
    || !money(payload.overage.amount)
    || !nonnegative(payload.overage.settlement_count)
  ) return null;
  const plan = payload.plan === null ? null : normalizePlan(payload.plan, tenantId);
  if (payload.plan !== null && !plan) return null;
  const buckets = normalizeBuckets(payload.quota_buckets, tenantId, plan?.plan_id ?? null);
  if (!buckets || ((plan === null) !== (buckets.length === 0))) return null;
  const adjustments = payload.recent_adjustments.map((item) => (
    normalizeAdjustment(item, tenantId, plan?.plan_id ?? null)
  ));
  if (adjustments.some((item) => !item)) return null;
  const adjustmentIds = adjustments.map((item) => item.quota_adjustment_id);
  if (new Set(adjustmentIds).size !== adjustmentIds.length) return null;
  return Object.freeze({
    ...payload,
    plan,
    quota_buckets: buckets,
    overage: Object.freeze({ ...payload.overage }),
    recent_adjustments: Object.freeze(adjustments),
  });
}

/** 校验调整成功响应仍属于本次 tenant、Bucket revision 和完整公开意图。 */
export function normalizeQuotaAdjustmentResponse(payload, tenantId, request) {
  if (
    !exact(payload, ADJUSTMENT_RESPONSE_KEYS)
    || typeof payload.idempotent_replay !== "boolean"
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;
  const plan = normalizePlan(payload.plan, tenantId);
  const bucket = normalizeBucket(payload.quota_bucket, tenantId, plan?.plan_id ?? null);
  const adjustment = normalizeAdjustment(payload.adjustment, tenantId, plan?.plan_id ?? null);
  const expectedResultingRevision = request?.expected_bucket_revision + 1;
  if (
    !plan
    || !bucket
    || !adjustment
    || adjustment.quota_type !== request?.quota_type
    || adjustment.direction !== request?.direction
    || adjustment.amount !== request?.amount
    || adjustment.resulting_bucket_revision !== expectedResultingRevision
    || bucket.quota_type !== request?.quota_type
    || bucket.state_revision < adjustment.resulting_bucket_revision
    || (!payload.idempotent_replay
      && bucket.state_revision !== adjustment.resulting_bucket_revision)
  ) return null;
  return Object.freeze({ ...payload, plan, quota_bucket: bucket, adjustment });
}

/** 为每次 HTTP attempt 生成不含 tenant、金额或业务正文的 request_id。 */
export function createPlanBillingRequestId(lane = "request") {
  requestSequence += 1;
  const kind = /^(?:plan_get|plan_patch|quota_get|quota_post|request)$/.test(lane)
    ? lane
    : "request";
  return `console_plan_billing_${kind}_${requestSequence}_${secureUuid().replaceAll("-", "")}`;
}

/** 为一个额度调整意图生成仅驻留 Dialog operation 闭包的幂等键。 */
export function createQuotaAdjustmentIdempotencyKey() {
  return `quota-adjustment-${secureUuid()}`;
}

function normalizePlan(value, tenantId) {
  if (
    !exact(value, PLAN_KEYS)
    || !PLAN_ID.test(String(value.plan_id ?? ""))
    || value.tenant_id !== tenantId
    || normalizeDisplayText(value.display_name, 80) !== value.display_name
    || !["free", "paid"].includes(value.base_tier)
    || !["free", "paid", "trial"].includes(value.effective_tier)
    || !["active", "suspended"].includes(value.status)
    || !["not_started", "active", "expired"].includes(value.trial_status)
    || !nullableUtc(value.trial_started_at)
    || !nullableUtc(value.trial_ends_at)
    || !integer(value.trial_cycle, 0, 1)
    || !integer(value.state_revision, 1, Number.MAX_SAFE_INTEGER)
    || !integer(value.quota_generation, 1, Number.MAX_SAFE_INTEGER)
    || !utc(value.created_at)
    || !utc(value.updated_at)
    || value.effective_tier !== (value.trial_status === "active" ? "trial" : value.base_tier)
    || (value.trial_status === "not_started" && (
      value.trial_started_at !== null || value.trial_ends_at !== null || value.trial_cycle !== 0
    ))
    || (value.trial_status === "active" && (
      value.trial_started_at === null || value.trial_ends_at === null || value.trial_cycle !== 1
    ))
    || (value.trial_status === "expired" && (
      value.trial_started_at === null || value.trial_ends_at === null || value.trial_cycle !== 1
    ))
  ) return null;
  return Object.freeze({ ...value });
}

/** 规范化四类额度桶并校验固定顺序、唯一标识与共同租户归属。 */
function normalizeBuckets(values, tenantId, planId) {
  if (!Array.isArray(values)) return null;
  if (planId === null) return values.length === 0 ? Object.freeze([]) : null;
  if (values.length !== QUOTA_ORDER.length) return null;
  const buckets = values.map((value) => normalizeBucket(value, tenantId, planId));
  if (buckets.some((item) => !item)) return null;
  if (JSON.stringify(buckets.map((item) => item.quota_type)) !== JSON.stringify(QUOTA_ORDER)) return null;
  if (new Set(buckets.map((item) => item.quota_bucket_id)).size !== buckets.length) return null;
  return Object.freeze(buckets);
}

/** 校验单个额度桶的 exact 字段、金额守恒、状态和套餐归属。 */
function normalizeBucket(value, tenantId, planId) {
  if (
    !exact(value, BUCKET_KEYS)
    || !BUCKET_ID.test(String(value.quota_bucket_id ?? ""))
    || value.tenant_id !== tenantId
    || value.plan_id !== planId
    || !QUOTA_ORDER.includes(value.quota_type)
    || ![value.granted, value.consumed, value.reserved, value.available, value.allocation_consumed].every(money)
    || !["active", "exhausted", "expired", "suspended"].includes(value.status)
    || !nullableUtc(value.expires_at)
    || !integer(value.state_revision, 1, Number.MAX_SAFE_INTEGER)
    || !nonnegative(value.allocation_count)
    || !utc(value.created_at)
    || !utc(value.updated_at)
  ) return null;
  const granted = moneyMicros(value.granted);
  const consumed = moneyMicros(value.consumed);
  const reserved = moneyMicros(value.reserved);
  const available = moneyMicros(value.available);
  if (consumed + reserved > granted || granted - consumed - reserved !== available) return null;
  if (value.quota_type !== "trial" && value.expires_at !== null) return null;
  return Object.freeze({ ...value });
}

/** 校验额度调整公开事实的 exact 字段、金额和租户套餐归属。 */
function normalizeAdjustment(value, tenantId, planId) {
  if (
    !exact(value, ADJUSTMENT_KEYS)
    || !ADJUSTMENT_ID.test(String(value.quota_adjustment_id ?? ""))
    || value.tenant_id !== tenantId
    || value.plan_id !== planId
    || !QUOTA_ORDER.includes(value.quota_type)
    || !["credit", "debit"].includes(value.direction)
    || !positiveMoney(value.amount)
    || !safeId(value.actor_user_id)
    || !chain(value.operation_request_id)
    || !integer(value.resulting_bucket_revision, 2, Number.MAX_SAFE_INTEGER)
    || !utc(value.created_at)
  ) return null;
  return Object.freeze({ ...value });
}

/** 执行无凭据缓存请求并严格校验状态、响应信封和固定错误合同。 */
async function requestJson(url, init, successStatuses, invalidResponseCode) {
  let response;
  try {
    response = await fetch(url, { ...init, cache: "no-store", credentials: "omit" });
  } catch (error) {
    // 主动中止保持原始类型供代次逻辑识别，其余网络错误统一折叠为服务不可用。
    if (error?.name === "AbortError") throw error;
    throw clientError(invalidResponseCode.startsWith("plan.") ? "plan.unavailable" : "quota.unavailable", 503);
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    // 非 JSON 正文不能进入业务状态，必须按响应合同损坏处理。
    throw clientError(invalidResponseCode, 502);
  }
  // 缓存策略或链路标识不匹配时拒绝整个响应，避免接纳错配事实。
  if (!validResponseBoundary(response, payload)) throw clientError(invalidResponseCode, 502);
  if (!response.ok) {
    // 错误码必须来自白名单且与 HTTP 状态精确对应，否则视为不可信响应。
    if (!validErrorPayload(payload) || ERROR_STATUS[payload.code] !== response.status) {
      throw clientError(invalidResponseCode, 502);
    }
    // 已验证错误只转换为固定客户端错误，不透传服务端原始文案。
    throw clientError(payload.code, response.status, payload.request_id, payload.trace_id);
  }
  if (!successStatuses.has(response.status)) {
    // 非声明成功状态即使携带 JSON 也不能进入调用方业务状态。
    throw clientError(invalidResponseCode, 502, payload.request_id, payload.trace_id);
  }
  return payload;
}

/** 校验私有缓存策略以及响应头与正文链路标识的精确镜像。 */
function validResponseBoundary(response, payload) {
  const cacheControl = header(response?.headers, "Cache-Control");
  const requestId = header(response?.headers, "X-Request-ID");
  const traceId = header(response?.headers, "X-Trace-ID");
  return cacheControl === "no-store, private"
    && chain(requestId)
    && chain(traceId)
    && payload?.request_id === requestId
    && payload?.trace_id === traceId;
}

/** 校验后端错误信封的 exact 字段、链路标识和诊断值类型。 */
function validErrorPayload(value) {
  return Boolean(
    exact(value, ERROR_KEYS)
    && ERROR_CODE.test(String(value.code ?? ""))
    && typeof value.message === "string"
    && typeof value.suggestion === "string"
    && chain(value.request_id)
    && chain(value.trace_id)
    && exact(value.diagnosis, DIAGNOSIS_KEYS)
    && DIAGNOSIS_KEYS.every((key) => (
      key === "retryable"
        ? typeof value.diagnosis[key] === "boolean"
        : typeof value.diagnosis[key] === "string"
    ))
  );
}

/** 构造 Bearer 与请求链路头，令牌或标识非法时失败关闭。 */
function authorizationHeaders(token, requestId) {
  // 认证令牌禁止为空或包含空白，请求标识也必须满足链路格式约束。
  if (typeof token !== "string" || !token || /\s/u.test(token) || !chain(requestId)) {
    throw clientError("auth.token_missing", 401);
  }
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/json",
    "X-Request-ID": requestId,
  };
}

/** 将受控错误码转换为固定 AuthApiError，不保留不可信服务正文。 */
function clientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = ERROR_CODE.test(String(code ?? "")) ? code : "quota.unavailable";
  // 认证类客户端错误只携带白名单文案和经过格式校验的链路标识。
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "套餐与额度请求失败",
    requestId: chain(requestId) ? requestId : "-",
    traceId: chain(traceId) ? traceId : "-",
  });
}

/** 要求调用方租户标识满足安全格式，租户只能来自认证上下文。 */
function requireTenant(value, code) {
  // 非法租户标识不得进入 URL 或请求正文，直接转换为固定请求错误。
  if (!safeId(value)) throw clientError(code, 422);
}

/** 校验可见幂等键格式并拒绝会破坏单值请求头语义的逗号。 */
function requireIdempotencyKey(value) {
  if (!VISIBLE_IDEMPOTENCY_KEY.test(String(value ?? "")) || String(value).includes(",")) {
    // 幂等键不满足单值可见字符边界时不得发起额度写请求。
    throw clientError("quota.request_invalid", 422);
  }
  return value;
}

/** 通过平台加密随机源生成规范 UUID，缺失安全能力时拒绝降级。 */
function secureUuid() {
  const value = globalThis.crypto?.randomUUID?.()?.toLowerCase();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(String(value ?? ""))) {
    // 随机源缺失或 UUID 形态异常时不能生成可预测的请求标识。
    throw clientError("quota.unavailable", 503);
  }
  return value;
}

/** 比较对象字段与允许集合是否完全一致，拒绝缺失和未知字段。 */
function exact(value, keys) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()),
  );
}

/** 读取并修剪响应头单值，缺失或非字符串值统一视为空。 */
function header(headers, name) {
  const value = headers?.get?.(name) ?? headers?.get?.(name.toLowerCase()) ?? "";
  return typeof value === "string" ? value.trim() : "";
}

/** 校验租户及业务标识是否处于允许的可见字符边界内。 */
function safeId(value) {
  return typeof value === "string" && SAFE_ID.test(value);
}

/** 校验请求与追踪标识格式，防止非法链路值进入日志和错误。 */
function chain(value) {
  return typeof value === "string" && CHAIN_ID.test(value);
}

/** 校验非负六位人民币文本及数据库整数上限，不使用浮点数。 */
function money(value) {
  return typeof value === "string"
    && CNY_AMOUNT.test(value)
    && moneyMicros(value) <= MAX_SQLITE_INTEGER;
}

/** 校验正数六位人民币文本及数据库整数上限，不使用浮点数。 */
function positiveMoney(value) {
  return typeof value === "string"
    && POSITIVE_CNY_AMOUNT.test(value)
    && moneyMicros(value) <= MAX_SQLITE_INTEGER;
}

/** 将已验证六位人民币文本精确转换为微元整数，避免舍入误差。 */
function moneyMicros(value) {
  const [whole, fraction] = String(value).split(".");
  return BigInt(whole) * 1_000_000n + BigInt(fraction);
}

/** 校验安全整数处于调用方声明的闭区间，拒绝隐式数值转换。 */
function integer(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

/** 校验值为非负安全整数，供计数和修订字段共同使用。 */
function nonnegative(value) {
  return integer(value, 0, Number.MAX_SAFE_INTEGER);
}

/** 接受空值或严格 UTC 时间文本，不允许其他可选时间形态。 */
function nullableUtc(value) {
  return value === null || utc(value);
}

/** 严格校验 UTC 时间文本及真实日历字段，拒绝日期自动归一。 */
function utc(value) {
  if (typeof value !== "string" || !UTC_TIME.test(value)) return false;
  const [datePart, timePart] = value.split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour, minute, rawSecond] = timePart.replace(/Z$/, "").split(":");
  const second = Number(rawSecond.split(".")[0]);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(Number(hour), Number(minute), second, 0);
  return date.getUTCFullYear() === year
    && date.getUTCMonth() === month - 1
    && date.getUTCDate() === day
    && date.getUTCHours() === Number(hour)
    && date.getUTCMinutes() === Number(minute)
    && date.getUTCSeconds() === second;
}

/** 规范化用户可见短文本，并限制长度和控制字符边界。 */
function normalizeDisplayText(value, maximum) {
  if (typeof value !== "string") return "";
  const normalized = value.normalize("NFKC").trim();
  return normalized.length >= 1
    && [...normalized].length <= maximum
    && !/[\u0000-\u001f\u007f-\u009f]/u.test(normalized)
    ? normalized
    : "";
}
