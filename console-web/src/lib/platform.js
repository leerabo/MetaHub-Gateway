const GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf";
const PLATFORM_CSRF_COOKIE = "__Host-metahub-platform-csrf";
const WORKSPACE_CSRF_COOKIE = "__Host-metahub-workspace-csrf";
const OPAQUE_ID = /^(?:pto|wsp|sgr|bgs|pau)_[0-9a-f]{32}$/u;

export class PlatformApiError extends Error {
  constructor({ status = 0, code = "platform.request_failed", message, requestId = "-" } = {}) {
    super(safeText(message) || "平台请求未完成，请稍后重试。");
    this.name = "PlatformApiError";
    this.status = Number(status) || 0;
    this.code = safeText(code) || "platform.request_failed";
    this.requestId = safeText(requestId) || "-";
  }
}

export function openPlatformSession({ controlApiUrl, totpCode, signal } = {}) {
  return request(controlApiUrl, "/platform/sessions", {
    method: "POST",
    body: { totp_code: totp(totpCode) },
    csrf: GLOBAL_CSRF_COOKIE,
    signal,
  });
}

export function getPlatformSession({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/session", { signal });
}

export function getPlatformNavigation({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/navigation", { signal });
}

export function listPlatformOperators({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/operators?limit=100&offset=0", { signal });
}

export function createPlatformOperator({ controlApiUrl, accountId, roles, signal } = {}) {
  return request(controlApiUrl, "/platform/operators", {
    method: "POST",
    body: { account_id: opaque(accountId, "gac"), roles: roleList(roles) },
    csrf: PLATFORM_CSRF_COOKIE,
    signal,
  });
}

export function updatePlatformOperator({
  controlApiUrl,
  operatorId,
  revision,
  status,
  roles,
  signal,
} = {}) {
  return request(controlApiUrl, `/platform/operators/${encodeURIComponent(opaque(operatorId, "pto"))}`, {
    method: "PATCH",
    body: { status, roles: roleList(roles) },
    csrf: PLATFORM_CSRF_COOKIE,
    headers: { "If-Match": `"${positiveInteger(revision)}"` },
    signal,
  });
}

export function listSupportGrants({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/support-grants?limit=100&offset=0", { signal });
}

export function createSupportGrant({
  controlApiUrl,
  workspaceId,
  reasonCode,
  durationMinutes,
  signal,
} = {}) {
  return request(controlApiUrl, "/platform/support-grants", {
    method: "POST",
    body: {
      workspace_id: opaque(workspaceId, "wsp"),
      reason_code: reason(reasonCode),
      duration_minutes: boundedInteger(durationMinutes, 1, 30),
    },
    csrf: PLATFORM_CSRF_COOKIE,
    signal,
  });
}

export function approveSupportGrant({ controlApiUrl, grantId, signal } = {}) {
  return request(
    controlApiUrl,
    `/platform/support-grants/${encodeURIComponent(opaque(grantId, "sgr"))}/approve`,
    { method: "POST", body: {}, csrf: PLATFORM_CSRF_COOKIE, signal },
  );
}

export function revokeSupportGrant({ controlApiUrl, grantId, reasonCode, signal } = {}) {
  return request(
    controlApiUrl,
    `/platform/support-grants/${encodeURIComponent(opaque(grantId, "sgr"))}/revoke`,
    {
      method: "POST",
      body: { reason_code: reason(reasonCode) },
      csrf: PLATFORM_CSRF_COOKIE,
      signal,
    },
  );
}

export function createSupportSession({ controlApiUrl, grantId, signal } = {}) {
  return request(
    controlApiUrl,
    `/platform/support-grants/${encodeURIComponent(opaque(grantId, "sgr"))}/sessions`,
    { method: "POST", body: {}, csrf: PLATFORM_CSRF_COOKIE, signal },
  );
}

export function getSupportDiagnostics({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/support/workspace/diagnostics", { signal });
}

export function createBreakGlassSession({
  controlApiUrl,
  workspaceId,
  reasonCode,
  totpCode,
  signal,
} = {}) {
  return request(controlApiUrl, "/platform/break-glass-sessions", {
    method: "POST",
    body: {
      workspace_id: opaque(workspaceId, "wsp"),
      reason_code: reason(reasonCode),
      totp_code: totp(totpCode),
      confirmed: true,
    },
    csrf: PLATFORM_CSRF_COOKIE,
    signal,
  });
}

export function listPlatformAudit({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/audit-logs?limit=100", { signal });
}

export function readPlatformCsrfCookie(cookieText = globalThis.document?.cookie ?? "") {
  return readCookie(PLATFORM_CSRF_COOKIE, cookieText);
}

async function request(controlApiUrl, path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers ?? {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = readCookie(options.csrf);
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new PlatformApiError({ code: "platform.network_unavailable", message: "暂时无法连接平台服务。" });
  }
  const payload = await json(response);
  if (!response.ok) {
    throw new PlatformApiError({
      status: response.status,
      code: payload?.code,
      message: payload?.message,
      requestId: payload?.request_id,
    });
  }
  validatePayload(payload);
  return payload;
}

function apiUrl(controlApiUrl, path) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    throw new PlatformApiError({ code: "platform.configuration_invalid", message: "平台服务地址无效。" });
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    throw new PlatformApiError({ code: "platform.configuration_invalid", message: "平台服务地址无效。" });
  }
  return new URL(path, base).toString();
}

function readCookie(name, cookieText = globalThis.document?.cookie ?? "") {
  const values = String(cookieText)
    .split(";")
    .map((value) => value.trim())
    .filter((value) => value.startsWith(`${name}=`))
    .map((value) => value.slice(name.length + 1));
  if (values.length !== 1 || !values[0] || values[0].includes(",")) {
    throw new PlatformApiError({ code: "platform.csrf_invalid", message: "安全校验已失效，请重新进入平台。" });
  }
  const decoded = decodeURIComponent(values[0]);
  if (/[^A-Za-z0-9_-]/u.test(decoded) || decoded.length < 43 || decoded.length > 128) {
    throw new PlatformApiError({ code: "platform.csrf_invalid", message: "安全校验已失效，请重新进入平台。" });
  }
  return decoded;
}

function validatePayload(value, depth = 0) {
  if (depth > 8) invalidResponse();
  if (Array.isArray(value)) {
    if (value.length > 200) invalidResponse();
    for (const item of value) validatePayload(item, depth + 1);
    return;
  }
  if (value === null || ["string", "number", "boolean"].includes(typeof value)) return;
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse();
  for (const [key, item] of Object.entries(value)) {
    if (/^(?:tenant_id|account_id|token|csrf|secret|ciphertext|prompt|response)$/iu.test(key)) {
      invalidResponse();
    }
    validatePayload(item, depth + 1);
  }
}

function opaque(value, prefix) {
  const text = String(value ?? "").trim();
  if (!OPAQUE_ID.test(text.replace(/^gac_/u, "pto_")) || !text.startsWith(`${prefix}_`)) {
    throw new PlatformApiError({ code: "platform.request_invalid", message: "对象标识无效。" });
  }
  return text;
}

function roleList(value) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 13) invalidRequest();
  const roles = value.map((role) => String(role));
  if (new Set(roles).size !== roles.length || roles.some((role) => !/^[a-z_]{3,40}$/u.test(role))) {
    invalidRequest();
  }
  return roles;
}

function reason(value) {
  const text = String(value ?? "").trim();
  if (!/^[a-z0-9][a-z0-9._-]{1,79}$/u.test(text)) invalidRequest();
  return text;
}

function totp(value) {
  const text = String(value ?? "");
  if (!/^[0-9]{6}$/u.test(text)) invalidRequest();
  return text;
}

function positiveInteger(value) {
  return boundedInteger(value, 1, Number.MAX_SAFE_INTEGER);
}

function boundedInteger(value, minimum, maximum) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) invalidRequest();
  return number;
}

function invalidRequest() {
  throw new PlatformApiError({ code: "platform.request_invalid", message: "平台请求字段无效。" });
}

function invalidResponse() {
  throw new PlatformApiError({ code: "platform.response_invalid", message: "平台响应格式无效。" });
}

async function json(response) {
  try {
    const value = await response.json();
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function safeText(value) {
  return typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240) : "";
}
