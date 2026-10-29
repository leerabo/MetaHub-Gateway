/** PAGE-116 Local Model Endpoint control client with closed request and response contracts. */

import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

const LOCAL_ENDPOINT_ID = /^lme_[0-9a-f]{32}$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const MACHINE_CODE = /^[a-z0-9][a-z0-9._-]{0,159}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const ENDPOINT_HINT = /^https?:\/\/\[(?:private|public|loopback)\]\/v1$/;
const ENGINE_KINDS = new Set(["vllm", "ollama", "openai_compatible"]);
const AUTH_TYPES = new Set(["none", "bearer"]);
const ENDPOINT_STATUSES = new Set(["active", "disabled"]);
const HEALTH_STATUSES = new Set(["healthy", "unhealthy", "observing", "unknown", "disabled"]);
const TEST_STATUSES = new Set(["success", "failed"]);
const CAPABILITY_KEYS = Object.freeze(["chat", "embeddings", "stream", "tools", "json_mode", "vision"]);

const CREATE_KEYS = Object.freeze([
  "provider_code", "name", "engine_kind", "base_url", "auth_type", "initial_model",
]);
const CREATE_BEARER_KEYS = Object.freeze([...CREATE_KEYS, "bearer_token"]);
const INITIAL_MODEL_REQUEST_KEYS = Object.freeze([
  "internal_model_id", "provider_model_id", "display_name", "capabilities", "context_window",
  "input_price_per_1k", "output_price_per_1k", "cache_price_per_1k", "currency",
]);
const INITIAL_MODEL_KEYS = Object.freeze([
  "model_id", "internal_model_id", "provider_model_id", "display_name", "capabilities",
  "context_window", "input_price_per_1k", "output_price_per_1k", "cache_price_per_1k", "currency", "status",
]);
const ENDPOINT_KEYS = Object.freeze([
  "local_endpoint_id", "tenant_id", "provider_id", "provider_code", "name", "engine_kind",
  "endpoint_hint", "auth_type", "status", "health_status", "last_tested_at", "revision",
  "catalog_revision", "catalog_status", "last_discovered_at", "discovered_model_count",
  "credential_configured", "initial_model",
]);
const LIST_KEYS = Object.freeze(["local_endpoints", "request_id", "trace_id"]);
const CREATE_RESPONSE_KEYS = Object.freeze(["local_endpoint", "created", "request_id", "trace_id"]);
const TEST_RESPONSE_KEYS = Object.freeze(["local_endpoint", "test_result", "request_id", "trace_id"]);
const TEST_RESULT_KEYS = Object.freeze([
  "status", "http_status", "latency_ms", "error_code", "discovered_model_count",
  "catalog_truncated", "checked_at",
]);
const ERROR_KEYS = Object.freeze([
  "code", "message", "request_id", "trace_id", "suggestion", "diagnosis",
]);
const DIAGNOSIS_KEYS = Object.freeze([
  "rule_id", "category", "reason", "suggestion", "action", "severity", "retryable",
  "matched_by", "safe_summary",
]);

const ERROR_STATUS = Object.freeze({
  "auth.token_missing": 401,
  "auth.token_invalid": 401,
  "auth.token_expired": 401,
  "auth.session_revoked": 401,
  "auth.permission_denied": 403,
  "local_endpoint.not_found": 404,
  "local_endpoint.conflict": 409,
  "local_endpoint.idempotency_conflict": 409,
  "provider_health.probe_in_progress": 409,
  "provider_health.probe_superseded": 409,
  "local_endpoint.request_invalid": 422,
  "local_endpoint.engine_invalid": 422,
  "local_endpoint.url_invalid": 422,
  "local_endpoint.auth_invalid": 422,
  "local_endpoint.catalog_invalid": 422,
  "local_endpoint.unavailable": 503,
});

const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.token_expired": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有所需的本地模型权限",
  "local_endpoint.not_found": "当前租户中找不到该本地 Endpoint",
  "local_endpoint.conflict": "本地 Endpoint 状态已变化",
  "local_endpoint.idempotency_conflict": "该 request_id 已用于其他创建意图",
  "provider_health.probe_in_progress": "该 Endpoint 正在测试",
  "provider_health.probe_superseded": "Endpoint 配置已变化，请重新测试",
  "local_endpoint.request_invalid": "本地 Endpoint 请求不合法",
  "local_endpoint.engine_invalid": "本地推理引擎不受支持",
  "local_endpoint.url_invalid": "Endpoint URL 不符合安全要求",
  "local_endpoint.auth_invalid": "Endpoint 认证配置不合法",
  "local_endpoint.catalog_invalid": "Endpoint 模型目录响应不合法",
  "local_endpoint.unavailable": "本地 Endpoint 服务暂不可用",
  "local_endpoint.response_invalid": "本地 Endpoint 响应合同不完整",
});

/** Read the current tenant's safe Local Endpoint aggregates. */
export async function listLocalEndpoints({
  controlApiUrl, token, tenantId, authentication = "legacy", signal,
}) {
  requireTenantForAuthentication(tenantId, authentication);
  const payload = await requestJson(`${controlApiUrl}/local-endpoints`, {
    authentication,
    token,
    headers: { Accept: "application/json" },
    signal,
  }, new Set([200]));
  const normalized = normalizeLocalEndpointList(payload, tenantId);
  if (!normalized) throw clientError("local_endpoint.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** Atomically register one endpoint and its initial model. URL and token are consumed only here. */
export async function createLocalEndpoint({
  controlApiUrl,
  token,
  tenantId,
  request,
  requestId,
  authentication = "legacy",
  signal,
}) {
  requireTenantForAuthentication(tenantId, authentication);
  const body = normalizeLocalEndpointCreateRequest(request);
  const normalizedRequestId = requireChain(requestId, "local_endpoint.request_invalid", 422);
  const payload = await requestJson(`${controlApiUrl}/local-endpoints`, {
    authentication,
    token,
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Request-ID": normalizedRequestId,
    },
    body: JSON.stringify(body),
    signal,
  }, new Set([200, 201]));
  const normalized = normalizeLocalEndpointCreateResponse(payload, tenantId);
  if (!normalized) throw clientError("local_endpoint.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** Run the bounded backend test. The request body is exactly an empty object. */
export async function testLocalEndpoint({
  controlApiUrl, token, tenantId, localEndpointId, authentication = "legacy", signal,
}) {
  requireTenantForAuthentication(tenantId, authentication);
  const id = requireLocalEndpointId(localEndpointId);
  const payload = await requestJson(
    `${controlApiUrl}/local-endpoints/${encodeURIComponent(id)}/test`,
    {
      authentication,
      token,
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: "{}",
      signal,
    },
    new Set([200]),
  );
  const normalized = normalizeLocalEndpointTestResponse(payload, tenantId, id);
  if (!normalized) throw clientError("local_endpoint.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** Validate and freeze the only accepted create body. Unknown fields fail closed. */
export function normalizeLocalEndpointCreateRequest(value) {
  const authType = value?.auth_type;
  const expectedKeys = authType === "bearer" ? CREATE_BEARER_KEYS : CREATE_KEYS;
  if (
    !exact(value, expectedKeys)
    || !machine(value.provider_code, 64)
    || !display(value.name, 80)
    || !ENGINE_KINDS.has(value.engine_kind)
    || !validBaseUrl(value.base_url)
    || !AUTH_TYPES.has(authType)
    || (authType === "bearer" && !secret(value.bearer_token))
  ) throw clientError("local_endpoint.request_invalid", 422);
  const initialModel = normalizeInitialModelRequest(value.initial_model);
  if (!initialModel) throw clientError("local_endpoint.request_invalid", 422);
  return Object.freeze({
    provider_code: value.provider_code,
    name: value.name,
    engine_kind: value.engine_kind,
    base_url: value.base_url,
    auth_type: authType,
    ...(authType === "bearer" ? { bearer_token: value.bearer_token } : {}),
    initial_model: initialModel,
  });
}

/** Validate the exact GET envelope and every tenant-qualified safe aggregate. */
export function normalizeLocalEndpointList(payload, tenantId) {
  if (
    !exact(payload, LIST_KEYS)
    || !Array.isArray(payload.local_endpoints)
    || payload.local_endpoints.length > 200
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;
  const items = payload.local_endpoints.map((item) => normalizeLocalEndpointInfo(item, tenantId));
  if (items.some((item) => !item)) return null;
  if (tenantId === null && items.length > 1
    && items.some((item) => item.tenant_id !== items[0].tenant_id)) return null;
  const ids = items.map((item) => item.local_endpoint_id);
  if (new Set(ids).size !== ids.length) return null;
  return Object.freeze({ ...payload, local_endpoints: Object.freeze(items) });
}

/** Validate a create response without retaining the submitted URL or token. */
export function normalizeLocalEndpointCreateResponse(payload, tenantId) {
  if (
    !exact(payload, CREATE_RESPONSE_KEYS)
    || typeof payload.created !== "boolean"
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;
  const endpoint = normalizeLocalEndpointInfo(payload.local_endpoint, tenantId);
  return endpoint ? Object.freeze({ ...payload, local_endpoint: endpoint }) : null;
}

/** Validate the test result and require its aggregate owner to match the requested ID. */
export function normalizeLocalEndpointTestResponse(payload, tenantId, localEndpointId) {
  if (!exact(payload, TEST_RESPONSE_KEYS) || !chain(payload.request_id) || !chain(payload.trace_id)) return null;
  const endpoint = normalizeLocalEndpointInfo(payload.local_endpoint, tenantId);
  const result = normalizeTestResult(payload.test_result);
  if (!endpoint || endpoint.local_endpoint_id !== localEndpointId || !result) return null;
  return Object.freeze({ ...payload, local_endpoint: endpoint, test_result: result });
}

/** Create a non-sensitive request ID; no tenant, URL, token, or model value is encoded. */
export function createLocalEndpointRequestId() {
  const value = globalThis.crypto?.randomUUID?.()?.replaceAll("-", "").toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(String(value ?? ""))) {
    throw clientError("local_endpoint.unavailable", 503);
  }
  return `req_${value}`;
}

function normalizeInitialModelRequest(value) {
  if (
    !exact(value, INITIAL_MODEL_REQUEST_KEYS)
    || !machine(value.internal_model_id, 160)
    || !machine(value.provider_model_id, 160)
    || !display(value.display_name, 160)
    || !validCapabilities(value.capabilities)
    || !integer(value.context_window, 1, 10_000_000)
    || !price(value.input_price_per_1k)
    || !price(value.output_price_per_1k)
    || !price(value.cache_price_per_1k)
    || value.currency !== "CNY"
  ) return null;
  return Object.freeze({ ...value, capabilities: Object.freeze({ ...value.capabilities }) });
}

function normalizeLocalEndpointInfo(value, tenantId) {
  if (
    !exact(value, ENDPOINT_KEYS)
    || !LOCAL_ENDPOINT_ID.test(String(value.local_endpoint_id ?? ""))
    || (tenantId === null ? !safeId(value.tenant_id) : value.tenant_id !== tenantId)
    || !safeId(value.provider_id)
    || !machine(value.provider_code, 64)
    || !display(value.name, 80)
    || !ENGINE_KINDS.has(value.engine_kind)
    || !ENDPOINT_HINT.test(String(value.endpoint_hint ?? ""))
    || !AUTH_TYPES.has(value.auth_type)
    || !ENDPOINT_STATUSES.has(value.status)
    || !HEALTH_STATUSES.has(value.health_status)
    || !nullableUtc(value.last_tested_at)
    || !integer(value.revision, 1, Number.MAX_SAFE_INTEGER)
    || !integer(value.catalog_revision, 0, Number.MAX_SAFE_INTEGER)
    || !machine(value.catalog_status, 64)
    || !nullableUtc(value.last_discovered_at)
    || !integer(value.discovered_model_count, 0, 200)
    || typeof value.credential_configured !== "boolean"
    || value.credential_configured !== (value.auth_type === "bearer")
  ) return null;
  const initialModel = normalizeInitialModel(value.initial_model);
  if (!initialModel) return null;
  return Object.freeze({ ...value, initial_model: initialModel });
}

function normalizeInitialModel(value) {
  if (
    !exact(value, INITIAL_MODEL_KEYS)
    || !safeId(value.model_id)
    || !machine(value.internal_model_id, 160)
    || !machine(value.provider_model_id, 160)
    || !display(value.display_name, 160)
    || !validCapabilities(value.capabilities)
    || !integer(value.context_window, 1, 10_000_000)
    || !price(value.input_price_per_1k)
    || !price(value.output_price_per_1k)
    || !price(value.cache_price_per_1k)
    || value.currency !== "CNY"
    || !ENDPOINT_STATUSES.has(value.status)
  ) return null;
  return Object.freeze({ ...value, capabilities: Object.freeze({ ...value.capabilities }) });
}

function normalizeTestResult(value) {
  if (
    !exact(value, TEST_RESULT_KEYS)
    || !TEST_STATUSES.has(value.status)
    || !(value.http_status === null || integer(value.http_status, 100, 599))
    || !(value.latency_ms === null || integer(value.latency_ms, 0, 3_600_000))
    || !(value.error_code === null || ERROR_CODE.test(String(value.error_code)))
    || !integer(value.discovered_model_count, 0, 200)
    || typeof value.catalog_truncated !== "boolean"
    || !utc(value.checked_at)
    || (value.status === "success" && value.error_code !== null)
  ) return null;
  return Object.freeze({ ...value });
}

async function requestJson(url, init, successStatuses) {
  let response;
  try {
    response = await authenticatedResourceFetch(url, init);
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError("local_endpoint.unavailable", 503);
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw clientError("local_endpoint.response_invalid", 502);
  }
  if (!validBoundary(response, payload)) throw clientError("local_endpoint.response_invalid", 502);
  if (response.ok) {
    if (!successStatuses.has(response.status)) {
      throw clientError("local_endpoint.response_invalid", 502, payload.request_id, payload.trace_id);
    }
    return payload;
  }
  if (!validError(payload) || ERROR_STATUS[payload.code] !== response.status) {
    throw clientError("local_endpoint.response_invalid", 502);
  }
  throw clientError(payload.code, response.status, payload.request_id, payload.trace_id);
}

function validBoundary(response, payload) {
  const cacheControl = header(response?.headers, "Cache-Control");
  const requestId = header(response?.headers, "X-Request-ID");
  const traceId = header(response?.headers, "X-Trace-ID");
  return cacheControl === "no-store, private"
    && chain(requestId)
    && chain(traceId)
    && payload?.request_id === requestId
    && payload?.trace_id === traceId;
}

function validError(value) {
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
    )),
  );
}

function authorizationHeaders(token) {
  if (typeof token !== "string" || !token || /\s/u.test(token)) {
    throw clientError("auth.token_missing", 401);
  }
  return { Authorization: `Bearer ${token}`, Accept: "application/json" };
}

function clientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = ERROR_CODE.test(String(code ?? "")) ? code : "local_endpoint.unavailable";
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "本地 Endpoint 请求失败",
    requestId: chain(requestId) ? requestId : "-",
    traceId: chain(traceId) ? traceId : "-",
  });
}

function validBaseUrl(value) {
  if (typeof value !== "string" || value.length < 8 || value.length > 2048 || /[\u0000-\u0020\u007f]/u.test(value)) return false;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol)
      && !url.username
      && !url.password
      && !url.search
      && !url.hash
      && Boolean(url.hostname)
      && (url.pathname === "/" || url.pathname === "" || url.pathname === "/v1" || url.pathname.endsWith("/v1"));
  } catch {
    return false;
  }
}

function validCapabilities(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  return keys.length > 0
    && keys.every((key) => CAPABILITY_KEYS.includes(key) && value[key] === true)
    && new Set(keys).size === keys.length;
}

function exact(value, keys) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()));
}

function requireTenantForAuthentication(value, authentication) {
  if (authentication === "workspace" && value === null) return;
  if (!safeId(value)) throw clientError("local_endpoint.request_invalid", 422);
  return value;
}

function requireLocalEndpointId(value) {
  if (!LOCAL_ENDPOINT_ID.test(String(value ?? ""))) throw clientError("local_endpoint.not_found", 404);
  return value;
}

function requireChain(value, code, status) {
  if (!chain(value)) throw clientError(code, status);
  return value;
}

function header(headers, name) {
  const value = headers?.get?.(name) ?? headers?.get?.(name.toLowerCase()) ?? "";
  return typeof value === "string" ? value.trim() : "";
}

function safeId(value) {
  return typeof value === "string" && SAFE_ID.test(value);
}

function machine(value, maximum) {
  return typeof value === "string" && value.length <= maximum && MACHINE_CODE.test(value);
}

function display(value, maximum) {
  return typeof value === "string" && value.length > 0 && [...value].length <= maximum
    && !/[\u0000-\u001f\u007f-\u009f]/u.test(value);
}

function secret(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 8192
    && !/[\u0000\r\n]/u.test(value);
}

function integer(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function price(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1_000_000;
}

function chain(value) {
  return typeof value === "string" && CHAIN_ID.test(value);
}

function nullableUtc(value) {
  return value === null || utc(value);
}

function utc(value) {
  return typeof value === "string" && UTC_TIME.test(value) && Number.isFinite(Date.parse(value));
}
