import { IdentityApiError } from "./identity.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const PLAN_VERSION_ID = /^spv_[0-9a-f]{32}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const MONEY = /^(?:0|[1-9][0-9]{0,10})\.[0-9]{2}$/u;

export function listPublicSaasPlans({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/catalog/plans", { signal, cache: "default" });
}

export function getWorkspaceSubscription({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/subscriptions/current", { signal });
}

export function createWorkspaceSubscription({
  controlApiUrl,
  planVersionId,
  startTrial = false,
  idempotencyKey,
  signal,
} = {}) {
  return request(controlApiUrl, "/subscriptions", {
    method: "POST",
    csrf: true,
    idempotencyKey: checkedIdempotencyKey(idempotencyKey),
    body: {
      plan_version_id: checkedPlanVersionId(planVersionId),
      start_trial: startTrial === true,
    },
    signal,
  });
}

export function changeWorkspaceSubscription({
  controlApiUrl,
  planVersionId,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  if (!Number.isSafeInteger(Number(revision)) || Number(revision) < 1) invalidRequest();
  return request(controlApiUrl, "/subscriptions/current/change-plan", {
    method: "POST",
    csrf: true,
    idempotencyKey: checkedIdempotencyKey(idempotencyKey),
    revision: Number(revision),
    body: { plan_version_id: checkedPlanVersionId(planVersionId) },
    signal,
  });
}

async function request(controlApiUrl, path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = readWorkspaceCsrfCookie();
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  if (options.revision) headers["If-Match"] = `"${options.revision}"`;
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: options.cache ?? "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new IdentityApiError({
      code: "subscription.network_unavailable",
      message: "暂时无法连接套餐服务。",
    });
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw new IdentityApiError({
      status: response.status,
      code: payload?.code,
      message: payload?.message || "套餐请求未完成，请稍后重试。",
      requestId: payload?.request_id,
      traceId: payload?.trace_id,
    });
  }
  validateSafeProjection(payload);
  return payload;
}

function apiUrl(controlApiUrl, path) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidRequest();
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest();
  }
  return new URL(path, base).toString();
}

function checkedPlanVersionId(value) {
  const text = String(value ?? "");
  if (!PLAN_VERSION_ID.test(text)) invalidRequest();
  return text;
}

function checkedIdempotencyKey(value) {
  const text = String(value ?? "");
  if (!IDEMPOTENCY_KEY.test(text)) invalidRequest();
  return text;
}

function validateSafeProjection(value, depth = 0) {
  if (depth > 9) invalidResponse();
  if (Array.isArray(value)) {
    if (value.length > 200) invalidResponse();
    value.forEach((item) => validateSafeProjection(item, depth + 1));
    return;
  }
  if (value === null || ["string", "number", "boolean"].includes(typeof value)) {
    if (typeof value === "string" && value.length > 4096) invalidResponse();
    return;
  }
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse();
  for (const [key, item] of Object.entries(value)) {
    if (/^(?:tenant_id|account_id|token|csrf|secret|ciphertext|payment_method)$/iu.test(key)) {
      invalidResponse();
    }
    if (key === "plan_version_id" && item !== null && !PLAN_VERSION_ID.test(String(item))) {
      invalidResponse();
    }
    if (["unit_price", "amount"].includes(key) && !MONEY.test(String(item))) {
      invalidResponse();
    }
    validateSafeProjection(item, depth + 1);
  }
}

function invalidRequest() {
  throw new IdentityApiError({
    code: "subscription.request_invalid",
    message: "套餐请求字段无效。",
  });
}

function invalidResponse() {
  throw new IdentityApiError({
    code: "subscription.response_invalid",
    message: "套餐响应格式无效。",
  });
}

async function readJson(response) {
  try {
    const value = await response.json();
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}
