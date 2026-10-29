/**
 * PAGE-106 Response Cache 控制面客户端。
 *
 * 安全边界：tenant 与 actor 只来自 Bearer Session；本模块严格校验 no-store、链路镜像与
 * exact DTO，任何未知字段、owner 漂移或金额组合异常都折叠为固定客户端错误。
 */

import { AuthApiError } from "./auth.js";

// 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const CACHE_POLICY_ID = /^rcp_[0-9a-f]{32}$/;
const CACHE_INVALIDATION_ID = /^rci_[0-9a-f]{32}$/;
const PRIVATE_CURSOR = /^[A-Za-z0-9_-]{1,4096}$/;
const MACHINE_CODE = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const SELECTOR = /^(?:policy|model_alias|model|virtual_key|data_class|tag):[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const CNY_AMOUNT = /^(?:0|[1-9][0-9]*)\.[0-9]{6}$/;
const VISIBLE_IDEMPOTENCY_KEY = /^[\x21-\x7e]{16,128}$/;

const POLICY_KEYS = Object.freeze([
  "cache_policy_id", "tenant_id", "model_alias_id", "status", "data_class",
  "single_data_class_confirmed", "ttl_seconds", "charge_cache_hits", "tags", "version",
  "ready_entry_count", "updated_at",
]);
const POLICY_ITEM_KEYS = Object.freeze([
  "tenant_id", "model_alias_id", "alias", "environment_id", "environment_code", "cache_policy",
]);
const LIST_KEYS = Object.freeze([
  "tenant_id", "items", "limit", "next_cursor", "has_more", "global_enabled", "request_id", "trace_id",
]);
const PATCH_KEYS = Object.freeze([
  "tenant_id", "cache_policy", "changed", "idempotent_replay", "invalidated_entry_count",
  "fenced_fill_count", "request_id", "trace_id",
]);
const INVALIDATION_KEYS = Object.freeze([
  "cache_invalidation_id", "tenant_id", "scope", "match_mode", "tag_count",
  "matched_entry_count", "deleted_entry_count", "fenced_fill_count", "reason_code", "created_at",
]);
const INVALIDATE_RESPONSE_KEYS = Object.freeze([
  "tenant_id", "invalidation", "idempotent_replay", "request_id", "trace_id",
]);
const POLICY_BODY_KEYS = Object.freeze([
  "model_alias_id", "status", "data_class", "single_data_class_confirmed", "ttl_seconds",
  "charge_cache_hits", "tags", "expected_version",
]);
const INVALIDATE_BODY_KEYS = Object.freeze(["scope", "match_mode", "tags", "reason_code"]);
const REQUEST_LOG_CACHE_KEYS = Object.freeze([
  "tenant_id", "status", "hit", "charge_cache_hits", "cache_policy_id", "cache_policy_version",
  "source_generation_cost_status", "source_generation_cost_cny",
  "current_reference_cost_status", "current_reference_cost_cny",
  "saved_cost_status", "saved_cost_cny", "billed_cost_status", "billed_cost_cny",
]);
// 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
const ERROR_KEYS = Object.freeze([
  "code", "message", "request_id", "trace_id", "suggestion", "diagnosis",
]);
const DIAGNOSIS_KEYS = Object.freeze([
  "rule_id", "category", "reason", "suggestion", "action", "severity", "retryable",
  "matched_by", "safe_summary",
]);

const REQUEST_STATUSES = new Set([
  "disabled", "ineligible", "bypassed_experiment", "unavailable_bypass", "corrupt_miss",
  "miss_owner", "miss_non_owner", "fill_stored", "fill_skipped", "hit", "hit_blocked",
  "settlement_failed",
]);
const COST_STATUSES = new Set(["known", "unavailable", "not_applicable"]);
const REASONS = new Set([
  "manual_finops", "manual_security", "manual_configuration", "manual_other",
]);
const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.token_expired": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有所需的响应缓存权限",
  "response_cache.policy_not_found": "当前租户中找不到该缓存策略",
  "response_cache.version_conflict": "缓存策略版本已变化，请显式读取并重放草稿",
  "response_cache.request_conflict": "该幂等键已用于其他操作意图",
  "response_cache.input_invalid": "响应缓存请求不合法",
  "response_cache.query_invalid": "响应缓存查询不合法",
  "response_cache.configuration_unavailable": "响应缓存尚未完成安全配置",
  "response_cache.unavailable": "响应缓存管理服务暂不可用",
  "response_cache.response_invalid": "响应缓存响应合同不完整",
});
// Control API 公开错误码与 HTTP 状态是一一冻结的；未知 code 或错配状态都视为响应合同损坏。
const ERROR_STATUS = Object.freeze({
  "auth.token_missing": 401,
  "auth.token_invalid": 401,
  "auth.token_expired": 401,
  "auth.session_revoked": 401,
  "auth.permission_denied": 403,
  "response_cache.policy_not_found": 404,
  "response_cache.version_conflict": 409,
  "response_cache.request_conflict": 409,
  "response_cache.input_invalid": 422,
  "response_cache.query_invalid": 422,
  "response_cache.configuration_unavailable": 503,
  "response_cache.unavailable": 503,
});

let requestSequence = 0;

/** 查询当前 Session tenant 的 Alias/CachePolicy 安全摘要。 */
/** 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。 */
export async function listResponseCachePolicies({
  controlApiUrl,
  token,
  tenantId,
  limit = 50,
  cursor = null,
  signal,
  requestId = createResponseCacheRequestId("list"),
}) {
  // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
  if (!safeId(tenantId) || !integer(limit, 1, 100) || !nullableCursor(cursor)) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    throw clientError("response_cache.query_invalid", 422);
  }
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor !== null) query.set("cursor", cursor);
  const payload = await requestJson(`${controlApiUrl}/cache-policies?${query}`, {
    headers: authorizationHeaders(token, requestId),
    signal,
  });
  const normalized = normalizeResponseCachePolicyList(payload, tenantId, limit);
  if (!normalized) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    throw clientError("response_cache.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return normalized;
}

/** 使用完整 CAS 快照创建或更新一个 Alias 的缓存策略。 */
export async function updateResponseCachePolicy({
  controlApiUrl,
  token,
  tenantId,
  request,
  idempotencyKey,
  signal,
  requestId = createResponseCacheRequestId("save"),
}) {
  const body = normalizeResponseCachePolicyRequest(request);
  const key = requireIdempotencyKey(idempotencyKey);
  // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
  if (!safeId(tenantId)) throw clientError("response_cache.input_invalid", 422);
  const payload = await requestJson(`${controlApiUrl}/cache-policies`, {
    method: "PATCH",
    headers: {
      ...authorizationHeaders(token, requestId),
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
    body: JSON.stringify(body),
    signal,
  });
  const normalized = normalizeResponseCachePolicyPatch(payload, tenantId, body);
  if (!normalized) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    throw clientError("response_cache.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return normalized;
}

/** 失效当前 tenant 的全部缓存，或同时匹配一组已验证 selector。 */
export async function invalidateResponseCache({
  controlApiUrl,
  token,
  tenantId,
  request,
  idempotencyKey,
  signal,
  requestId = createResponseCacheRequestId("invalidate"),
}) {
  const body = normalizeResponseCacheInvalidationRequest(request);
  const key = requireIdempotencyKey(idempotencyKey);
  // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
  if (!safeId(tenantId)) throw clientError("response_cache.input_invalid", 422);
  const payload = await requestJson(`${controlApiUrl}/cache/invalidate`, {
    method: "POST",
    headers: {
      ...authorizationHeaders(token, requestId),
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
    body: JSON.stringify(body),
    signal,
  });
  const normalized = normalizeResponseCacheInvalidationResponse(payload, tenantId, body);
  if (!normalized) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    throw clientError("response_cache.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return normalized;
}

/** 校验 GET envelope、稳定排序和逐层 tenant/Alias 镜像。 */
/** 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。 */
export function normalizeResponseCachePolicyList(payload, tenantId, expectedLimit = null) {
  if (
    !exact(payload, LIST_KEYS)
    || payload.tenant_id !== tenantId
    || !Array.isArray(payload.items)
    || payload.items.length > 100
    || !integer(payload.limit, 1, 100)
    || (expectedLimit !== null && payload.limit !== expectedLimit)
    || !nullableCursor(payload.next_cursor)
    || typeof payload.has_more !== "boolean"
    || payload.has_more !== (payload.next_cursor !== null)
    || typeof payload.global_enabled !== "boolean"
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;
  const items = payload.items.map((item) => normalizePolicyItem(item, tenantId));
  if (items.some((item) => !item)) return null;
  const owners = items.map((item) => item.model_alias_id);
  const order = items.map((item) => [item.environment_code, item.alias, item.model_alias_id].join("\u0000"));
  if (
    new Set(owners).size !== owners.length
    || order.some((value, index) => index > 0 && canonicalCompare(order[index - 1], value) >= 0)
  ) return null;
  return Object.freeze({ ...payload, items: Object.freeze(items) });
}

/** 校验 PATCH 成功体仍属于本次 tenant、Alias 和完整配置意图。 */
export function normalizeResponseCachePolicyPatch(payload, tenantId, request) {
  if (
    !exact(payload, PATCH_KEYS)
    || payload.tenant_id !== tenantId
    || typeof payload.changed !== "boolean"
    || typeof payload.idempotent_replay !== "boolean"
    || !nonnegative(payload.invalidated_entry_count)
    || !nonnegative(payload.fenced_fill_count)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;
  const policy = normalizePolicy(payload.cache_policy, tenantId, request?.model_alias_id);
  if (!policy || !policyMatchesRequest(policy, request)) return null;
  return Object.freeze({ ...payload, cache_policy: policy });
}

/** 校验手工失效成功体和提交 intent 的 scope/reason/tag count 一致。 */
export function normalizeResponseCacheInvalidationResponse(payload, tenantId, request) {
  if (
    !exact(payload, INVALIDATE_RESPONSE_KEYS)
    || payload.tenant_id !== tenantId
    || typeof payload.idempotent_replay !== "boolean"
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;
  const value = payload.invalidation;
  if (
    !exact(value, INVALIDATION_KEYS)
    || !CACHE_INVALIDATION_ID.test(String(value.cache_invalidation_id ?? ""))
    || value.tenant_id !== tenantId
    || value.scope !== request?.scope
    || value.match_mode !== request?.match_mode
    || value.reason_code !== request?.reason_code
    || value.tag_count !== request?.tags?.length
    || !nonnegative(value.matched_entry_count)
    || !nonnegative(value.deleted_entry_count)
    || value.deleted_entry_count > value.matched_entry_count
    || !nonnegative(value.fenced_fill_count)
    || !utc(value.created_at)
  ) return null;
  return Object.freeze({ ...payload, invalidation: Object.freeze({ ...value }) });
}

/** 把编辑器值收紧为后端 exact PATCH body；不会附加 tenant、actor 或局部更新字段。 */
export function normalizeResponseCachePolicyRequest(value) {
  if (!exact(value, POLICY_BODY_KEYS)) throw clientError("response_cache.input_invalid", 422);
  const tags = value.tags;
  const validDataClass = value.data_class === null || machineCode(value.data_class);
  if (
    !safeId(value.model_alias_id)
    || !["enabled", "disabled"].includes(value.status)
    || !validDataClass
    || typeof value.single_data_class_confirmed !== "boolean"
    || !integer(value.ttl_seconds, 60, 86_400)
    || typeof value.charge_cache_hits !== "boolean"
    || !Array.isArray(tags)
    || tags.length > 16
    || tags.some((tag) => !machineCode(tag))
    || new Set(tags).size !== tags.length
    || tags.some((tag, index) => index > 0 && canonicalCompare(tags[index - 1], tag) >= 0)
    || !integer(value.expected_version, 0, Number.MAX_SAFE_INTEGER)
    || (value.status === "enabled" && (!value.data_class || !value.single_data_class_confirmed))
    || (value.status === "disabled" && value.single_data_class_confirmed)
  ) throw clientError("response_cache.input_invalid", 422);
  return Object.freeze({ ...value, tags: Object.freeze([...tags]) });
}

/** 把失效表单收紧为 all 或 all-match tags 两种互斥请求。 */
export function normalizeResponseCacheInvalidationRequest(value) {
  if (!exact(value, INVALIDATE_BODY_KEYS)) throw clientError("response_cache.input_invalid", 422);
  const tags = value.tags;
  if (
    !["all", "tags"].includes(value.scope)
    || value.match_mode !== "all"
    || !Array.isArray(tags)
    || tags.length > 16
    || tags.some((tag) => !SELECTOR.test(String(tag ?? "")))
    || new Set(tags).size !== tags.length
    || tags.some((tag, index) => index > 0 && canonicalCompare(tags[index - 1], tag) >= 0)
    || !REASONS.has(value.reason_code)
    || (value.scope === "all" ? tags.length !== 0 : tags.length < 1)
  ) throw clientError("response_cache.input_invalid", 422);
  return Object.freeze({ ...value, tags: Object.freeze([...tags]) });
}

/**
 * 校验 RequestLog 详情中的 7.5 exact 投影。
 * 金额使用十进制微元字符串解析，避免 JavaScript float 改变 saved=reference-billed 等式。
 */
export function normalizeResponseCacheRequestLogInfo(value, tenantId) {
  if (value === null) return null;
  if (
    !exact(value, REQUEST_LOG_CACHE_KEYS)
    || value.tenant_id !== tenantId
    || !REQUEST_STATUSES.has(value.status)
    || typeof value.hit !== "boolean"
    || value.hit !== (value.status === "hit")
  ) return undefined;
  const owner = [value.charge_cache_hits, value.cache_policy_id, value.cache_policy_version];
  const ownerEmpty = owner.every((item) => item === null);
  const ownerFull = typeof owner[0] === "boolean"
    && CACHE_POLICY_ID.test(String(owner[1] ?? ""))
    && integer(owner[2], 1, Number.MAX_SAFE_INTEGER);
  if (!ownerEmpty && !ownerFull) return undefined;
  const pairs = [
    [value.source_generation_cost_status, value.source_generation_cost_cny],
    [value.current_reference_cost_status, value.current_reference_cost_cny],
    [value.saved_cost_status, value.saved_cost_cny],
    [value.billed_cost_status, value.billed_cost_cny],
  ];
  if (pairs.some(([status, amount]) => !validCostPair(status, amount))) return undefined;
  if (value.hit) {
    if (!ownerFull || pairs.some(([status]) => !["known", "unavailable"].includes(status))) {
      return undefined;
    }
    const referenceKnown = value.current_reference_cost_status === "known";
    const billedKnown = value.billed_cost_status === "known";
    // 免费命中若账单证据已知必须精确为 0；unknown 仍保持 null，不能伪造零价成功。
    if (!value.charge_cache_hits && billedKnown && value.billed_cost_cny !== "0.000000") {
      return undefined;
    }
    if (referenceKnown && billedKnown) {
      if (
        value.saved_cost_status !== "known"
        || amountMicros(value.current_reference_cost_cny) - amountMicros(value.billed_cost_cny)
          !== amountMicros(value.saved_cost_cny)
      ) return undefined;
    } else if (value.saved_cost_status !== "unavailable" || value.saved_cost_cny !== null) {
      return undefined;
    }
  } else if (pairs.some(([status, amount]) => status !== "not_applicable" || amount !== null)) {
    return undefined;
  }
  return Object.freeze({ ...value });
}

/** 从已验证列表派生 PAGE-106 唯一允许枚举的 selector，不读取额外模型或 Virtual Key。 */
export function responseCacheSelectors(items) {
  const result = new Set();
  for (const item of Array.isArray(items) ? items : []) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    if (!safeId(item?.model_alias_id)) continue;
    result.add(`model_alias:${item.model_alias_id}`);
    if (CACHE_POLICY_ID.test(String(item?.cache_policy?.cache_policy_id ?? ""))) {
      result.add(`policy:${item.cache_policy.cache_policy_id}`);
    }
    for (const tag of Array.isArray(item?.cache_policy?.tags) ? item.cache_policy.tags : []) {
      if (machineCode(tag)) result.add(`tag:${tag}`);
    }
  }
  return Object.freeze([...result].sort());
}

/** 为每次 HTTP attempt 生成新的安全 request_id，不包含 tenant、Alias、tag 或配置值。 */
export function createResponseCacheRequestId(lane = "request") {
  requestSequence += 1;
  const kind = /^(?:list|save|invalidate|request)$/.test(lane) ? lane : "request";
  const random = secureUuid().replaceAll("-", "").slice(0, 20);
  return `console_response_cache_${kind}_${requestSequence}_${random}`;
}

/** 为一个稳定写 intent 生成只驻留 operation closure 的幂等键。 */
export function createResponseCacheIdempotencyKey() {
  return `response-cache-${secureUuid()}`;
}

/** 校验一个 Alias 列表项及其可空 policy owner。 */
function normalizePolicyItem(value, tenantId) {
  if (
    !exact(value, POLICY_ITEM_KEYS)
    || value.tenant_id !== tenantId
    || !safeId(value.model_alias_id)
    || !displayText(value.alias, 128)
    || !safeId(value.environment_id)
    || !machineCode(value.environment_code)
  ) return null;
  const policy = value.cache_policy === null
    ? null
    : normalizePolicy(value.cache_policy, tenantId, value.model_alias_id);
  if (value.cache_policy !== null && !policy) return null;
  return Object.freeze({ ...value, cache_policy: policy });
}

/** 校验 GET/PATCH 共用的 CachePolicySummary exact 字段与 canonical tag。 */
/** 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。 */
function normalizePolicy(value, tenantId, modelAliasId) {
  if (
    !exact(value, POLICY_KEYS)
    || !CACHE_POLICY_ID.test(String(value.cache_policy_id ?? ""))
    || value.tenant_id !== tenantId
    || value.model_alias_id !== modelAliasId
    || !["enabled", "disabled"].includes(value.status)
    || !(value.data_class === null || machineCode(value.data_class))
    || typeof value.single_data_class_confirmed !== "boolean"
    || !integer(value.ttl_seconds, 60, 86_400)
    || typeof value.charge_cache_hits !== "boolean"
    || !Array.isArray(value.tags)
    || value.tags.length > 16
    || value.tags.some((tag) => !machineCode(tag))
    || new Set(value.tags).size !== value.tags.length
    || value.tags.some((tag, index) => index > 0 && canonicalCompare(value.tags[index - 1], tag) >= 0)
    || !integer(value.version, 1, Number.MAX_SAFE_INTEGER)
    || !nonnegative(value.ready_entry_count)
    || !utc(value.updated_at)
    || (value.status === "enabled" && (!value.data_class || !value.single_data_class_confirmed))
    || (value.status === "disabled" && value.single_data_class_confirmed)
  ) return null;
  return Object.freeze({ ...value, tags: Object.freeze([...value.tags]) });
}

/** 比较 PATCH 返回的配置与当前 intent，阻止错误幂等 owner 被前端接纳。 */
function policyMatchesRequest(policy, request) {
  return Boolean(
    request
    && policy.model_alias_id === request.model_alias_id
    && policy.status === request.status
    && policy.data_class === request.data_class
    && policy.single_data_class_confirmed === request.single_data_class_confirmed
    && policy.ttl_seconds === request.ttl_seconds
    && policy.charge_cache_hits === request.charge_cache_hits
    && JSON.stringify(policy.tags) === JSON.stringify(request.tags)
  );
}

/** 执行无 Cookie、无浏览器缓存的请求，并对成功和错误统一校验响应安全边界。 */
async function requestJson(url, init) {
  let response;
  try {
    response = await fetch(url, { ...init, cache: "no-store", credentials: "omit" });
  } catch (error) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    if (error?.name === "AbortError") throw error;
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    throw clientError("response_cache.unavailable", 503);
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    // 非 JSON 的上游状态不能冒充可信业务错误；客户端统一以合同失败收口。
    throw clientError("response_cache.response_invalid", 502);
  }
  if (!validResponseBoundary(response, payload)) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    throw clientError("response_cache.response_invalid", 502);
  }
  if (response.ok && response.status !== 200) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    throw clientError("response_cache.response_invalid", 502, payload.request_id, payload.trace_id);
  }
  if (!response.ok) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    if (!validErrorPayload(payload) || ERROR_STATUS[payload.code] !== response.status) {
      // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
      throw clientError("response_cache.response_invalid", 502);
    }
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    throw clientError(payload.code, response.status, payload.request_id, payload.trace_id);
  }
  return payload;
}

/** 校验 Cache-Control 精确值以及 Header/body request/trace 双向镜像。 */
/** 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。 */
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

/** 统一错误只接受公开固定字段；服务端 message/diagnosis 校验后仍不进入页面状态。 */
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
      key === "retryable" ? typeof value.diagnosis[key] === "boolean" : typeof value.diagnosis[key] === "string"
    ))
  );
}

/** 构造只含内存 Bearer、Accept 和 attempt request_id 的请求头。 */
/** 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。 */
function authorizationHeaders(token, requestId) {
  if (typeof token !== "string" || !token || /\s/u.test(token) || !chain(requestId)) {
    throw clientError("auth.token_missing", 401);
  }
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/json",
    "X-Request-ID": requestId,
  };
}

/** 创建固定本地 AuthApiError，禁止透传服务端 message、diagnosis 或调用方输入。 */
function clientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = ERROR_CODE.test(String(code ?? "")) ? code : "response_cache.unavailable";
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "响应缓存请求失败",
    requestId: chain(requestId) ? requestId : "-",
    traceId: chain(traceId) ? traceId : "-",
  });
}

/** 幂等键只允许单个可见 ASCII 值，逗号也被拒绝以避免代理折叠歧义。 */
function requireIdempotencyKey(value) {
  if (!VISIBLE_IDEMPOTENCY_KEY.test(String(value ?? "")) || String(value).includes(",")) {
    throw clientError("response_cache.input_invalid", 422);
  }
  return value;
}

/** 读取真实 Headers 或单测 Map，名称查找失败时按小写再尝试一次。 */
function header(headers, name) {
  const value = headers?.get?.(name) ?? headers?.get?.(name.toLowerCase()) ?? "";
  return typeof value === "string" ? value.trim() : "";
}

/** 判断对象字段集合与合同完全一致。 */
function exact(value, keys) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()),
  );
}

/** 校验金额状态/nullability 组合，不接受伪造零值。 */
function validCostPair(status, amount) {
  if (!COST_STATUSES.has(status)) return false;
  return status === "known" ? CNY_AMOUNT.test(String(amount ?? "")) : amount === null;
}

/** 将六位小数 CNY 转为整数微元；调用前已经通过正则校验。 */
function amountMicros(value) {
  const [whole, fraction] = String(value).split(".");
  return BigInt(whole) * 1_000_000n + BigInt(fraction);
}

/** 返回安全随机 UUID；缺少浏览器安全随机源时失败关闭。 */
function secureUuid() {
  const value = globalThis.crypto?.randomUUID?.()?.toLowerCase();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(String(value ?? ""))) {
    // 客户端只接受冻结字段和统一错误，租户归属与权限结论仍以后端为准。
    throw clientError("response_cache.unavailable", 503);
  }
  return value;
}

/** 校验短安全资源 ID。 */
function safeId(value) {
  return typeof value === "string" && SAFE_ID.test(value);
}

/** 校验 machine code，不做 trim 或 Unicode 归一化。 */
function machineCode(value) {
  return typeof value === "string" && MACHINE_CODE.test(value);
}

/** Alias 等展示文本必须非空、有界且不含控制字符。 */
function displayText(value, maximum) {
  return typeof value === "string"
    && value.length > 0
    && [...value].length <= maximum
    && !/[\u0000-\u001f\u007f-\u009f]/u.test(value);
}

/** 校验 JavaScript 可精确表达的闭区间整数。 */
function integer(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

/** 校验非负安全整数。 */
function nonnegative(value) {
  return integer(value, 0, Number.MAX_SAFE_INTEGER);
}

/** 校验 request_id/trace_id 的安全展示字符。 */
function chain(value) {
  return typeof value === "string" && CHAIN_ID.test(value);
}

/** 校验 opaque cursor，只验证传输字符，不在浏览器解析 owner。 */
function nullableCursor(value) {
  return value === null || (typeof value === "string" && PRIVATE_CURSOR.test(value));
}

/** 校验 UTC 时间且拒绝 Date 自动归一化的非法日期。 */
function utc(value) {
  if (typeof value !== "string") return false;
  const match = UTC_TIME.exec(value);
  if (!match) return false;
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

/** 使用 UTF-16 code-unit 顺序冻结 canonical ASCII/tag 和服务端 BINARY 排序，不依赖浏览器 locale。 */
function canonicalCompare(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}
