const GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf";
const PLATFORM_CSRF_COOKIE = "__Host-metahub-platform-csrf";
const WORKSPACE_CSRF_COOKIE = "__Host-metahub-workspace-csrf";
const OPAQUE_ID = /^(?:pto|wsp|sgr|bgs|pau)_[0-9a-f]{32}$/u;
const WORKSPACE_SLUG = /^[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])$/u;
const WORKSPACE_REGION = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const WORKSPACE_FILTER_VALUES = Object.freeze({
  kind: new Set(["organization"]),
  operational_status: new Set([
    "active",
    "suspended",
    "deletion_pending",
    "deleted",
    "migration_conflict",
  ]),
  risk_tag: new Set([
    "security_review",
    "payment_attention",
    "adoption_attention",
    "compliance_hold",
  ]),
  health_status: new Set(["fresh", "stale", "not_ready", "unavailable"]),
  health_risk: new Set(["healthy", "watch", "at_risk", "critical"]),
  plan_status: new Set(["not_available"]),
  deployment_status: new Set(["not_configured"]),
});
const WORKSPACE_RISK_TAGS = new Set([
  "security_review",
  "payment_attention",
  "adoption_attention",
  "compliance_hold",
]);
const OWNER_NOTIFICATION_TEMPLATES = new Set([
  "workspace_operational_notice",
  "workspace_risk_follow_up",
  "workspace_service_update",
]);

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
    body: totpCode ? { totp_code: totp(totpCode) } : {},
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

export function getPlatformRegistrationPolicy({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/registration-policy", { signal });
}

export function updatePlatformRegistrationPolicy({ controlApiUrl, policy, signal } = {}) {
  return request(controlApiUrl, "/platform/registration-policy", {
    method: "PUT", body: policy, csrf: PLATFORM_CSRF_COOKIE, signal,
  });
}

export function listPlatformWorkspaces({
  controlApiUrl,
  filters = {},
  cursor,
  limit = 50,
  signal,
} = {}) {
  const parameters = new URLSearchParams({ limit: String(boundedInteger(limit, 1, 100)) });
  if (cursor !== undefined && cursor !== null) parameters.set("cursor", workspaceCursor(cursor));
  for (const [key, value] of Object.entries(workspaceFilters(filters))) {
    parameters.set(key, value);
  }
  return request(controlApiUrl, `/platform/workspaces?${parameters.toString()}`, { signal });
}

export function getPlatformWorkspace({ controlApiUrl, workspaceId, signal } = {}) {
  return request(
    controlApiUrl,
    `/platform/workspaces/${encodeURIComponent(opaque(workspaceId, "wsp"))}`,
    { signal },
  );
}

export function getPlatformWorkspaceHealth({ controlApiUrl, workspaceId, signal } = {}) {
  return request(
    controlApiUrl,
    `/platform/workspaces/${encodeURIComponent(opaque(workspaceId, "wsp"))}/health`,
    { signal },
  );
}

export function setPlatformWorkspacePrivateGateway({
  controlApiUrl,
  workspaceId,
  revision,
  enabled,
  signal,
} = {}) {
  return request(
    controlApiUrl,
    `/platform/workspaces/${encodeURIComponent(opaque(workspaceId, "wsp"))}/private-gateway`,
    {
      method: "POST",
      body: { enabled: Boolean(enabled), confirmed: true },
      csrf: PLATFORM_CSRF_COOKIE,
      headers: { "If-Match": `"${positiveInteger(revision)}"` },
      signal,
    },
  );
}

export function listPlatformWorkspaceEvents({
  controlApiUrl,
  workspaceId,
  limit = 50,
  signal,
} = {}) {
  const path = `/platform/workspaces/${encodeURIComponent(opaque(workspaceId, "wsp"))}/events`;
  return request(controlApiUrl, `${path}?limit=${boundedInteger(limit, 1, 100)}`, { signal });
}

export function suspendPlatformWorkspace({
  controlApiUrl,
  workspaceId,
  revision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  return workspaceMutation(controlApiUrl, workspaceId, "suspend", {
    method: "POST",
    revision,
    idempotencyKey,
    body: { reason_code: reason(reasonCode), confirmed: true },
    signal,
  });
}

export function restorePlatformWorkspace({
  controlApiUrl,
  workspaceId,
  revision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  return workspaceMutation(controlApiUrl, workspaceId, "restore", {
    method: "POST",
    revision,
    idempotencyKey,
    body: { reason_code: reason(reasonCode), confirmed: true },
    signal,
  });
}

export function addPlatformWorkspaceRiskTag({
  controlApiUrl,
  workspaceId,
  revision,
  tagCode,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  return workspaceMutation(controlApiUrl, workspaceId, "risk-tags", {
    method: "POST",
    revision,
    idempotencyKey,
    body: { tag_code: riskTag(tagCode), reason_code: reason(reasonCode) },
    signal,
  });
}

export function removePlatformWorkspaceRiskTag({
  controlApiUrl,
  workspaceId,
  revision,
  tagCode,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  return workspaceMutation(
    controlApiUrl,
    workspaceId,
    `risk-tags/${encodeURIComponent(riskTag(tagCode))}`,
    {
      method: "DELETE",
      revision,
      idempotencyKey,
      body: { reason_code: reason(reasonCode) },
      signal,
    },
  );
}

export function createPlatformWorkspaceOwnerNotification({
  controlApiUrl,
  workspaceId,
  revision,
  templateCode,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const template = String(templateCode ?? "");
  if (!OWNER_NOTIFICATION_TEMPLATES.has(template)) invalidRequest();
  return workspaceMutation(controlApiUrl, workspaceId, "owner-notifications", {
    method: "POST",
    revision,
    idempotencyKey,
    body: { template_code: template, reason_code: reason(reasonCode) },
    signal,
  });
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

export function listPlatformSaasPlans({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/plans", { signal });
}

export function createPlatformSaasPlan({ controlApiUrl, plan, signal } = {}) {
  return request(controlApiUrl, "/platform/plans", {
    method: "POST",
    body: platformPlanPayload(plan),
    csrf: PLATFORM_CSRF_COOKIE,
    signal,
  });
}

export function getPlatformProviderCatalog({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/provider-catalog", { signal });
}

export function publishPlatformProviderCatalog({
  controlApiUrl,
  manifest,
  keyId,
  signature,
  reasonCode,
  confirmed,
  expectedRevision,
  idempotencyKey,
  signal,
} = {}) {
  if (!manifest || Object.getPrototypeOf(manifest) !== Object.prototype
    || manifest.schema_version !== 1
    || !/^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]{0,3}$/u.test(String(manifest.version ?? ""))
    || !Array.isArray(manifest.entries) || manifest.entries.length < 10 || manifest.entries.length > 100
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$/u.test(String(keyId ?? ""))
    || !/^[A-Za-z0-9_-]{86}$/u.test(String(signature ?? ""))
    || !new Set(["catalog_release", "compliance_update", "endpoint_update"]).has(reasonCode)
    || confirmed !== true) invalidRequest();
  return request(controlApiUrl, "/platform/provider-catalog/versions", {
    method: "POST",
    body: {
      manifest,
      key_id: String(keyId),
      signature: String(signature),
      reason_code: reasonCode,
      confirmed: true,
    },
    csrf: PLATFORM_CSRF_COOKIE,
    headers: {
      "If-Match": `"${positiveInteger(expectedRevision)}"`,
      "Idempotency-Key": idempotency(idempotencyKey),
    },
    signal,
  });
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

function workspaceMutation(controlApiUrl, workspaceId, suffix, options) {
  return request(
    controlApiUrl,
    `/platform/workspaces/${encodeURIComponent(opaque(workspaceId, "wsp"))}/${suffix}`,
    {
      method: options.method,
      body: options.body,
      csrf: PLATFORM_CSRF_COOKIE,
      headers: {
        "If-Match": `"${positiveInteger(options.revision)}"`,
        "Idempotency-Key": idempotency(options.idempotencyKey),
      },
      signal: options.signal,
    },
  );
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

function workspaceFilters(value) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) invalidRequest();
  const normalized = {};
  for (const key of Object.keys(value).sort()) {
    const text = String(value[key] ?? "").trim();
    if (!text) continue;
    if (key === "search") {
      if (!text.startsWith("wsp_") && !WORKSPACE_SLUG.test(text)) invalidRequest();
      if (text.startsWith("wsp_") && (!OPAQUE_ID.test(text) || !text.startsWith("wsp_"))) {
        invalidRequest();
      }
    } else if (key === "region") {
      if (!WORKSPACE_REGION.test(text)) invalidRequest();
    } else if (!WORKSPACE_FILTER_VALUES[key]?.has(text)) {
      invalidRequest();
    }
    normalized[key] = text;
  }
  return normalized;
}

function workspaceCursor(value) {
  const text = String(value ?? "");
  if (text.length > 2048 || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/u.test(text)) invalidRequest();
  return text;
}

function riskTag(value) {
  const text = String(value ?? "");
  if (!WORKSPACE_RISK_TAGS.has(text)) invalidRequest();
  return text;
}

function idempotency(value) {
  const text = value === undefined || value === null ? generatedIdempotencyKey() : String(value);
  if (!IDEMPOTENCY_KEY.test(text)) invalidRequest();
  return text;
}

function generatedIdempotencyKey() {
  const crypto = globalThis.crypto;
  if (!crypto || typeof crypto.getRandomValues !== "function") {
    throw new PlatformApiError({
      code: "platform.configuration_invalid",
      message: "当前环境无法生成安全请求标识。",
    });
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `platform:${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function reason(value) {
  const text = String(value ?? "").trim();
  if (!/^[a-z0-9][a-z0-9._-]{1,79}$/u.test(text)) invalidRequest();
  return text;
}

function platformPlanPayload(value) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) invalidRequest();
  const planCode = String(value.plan_code ?? "").trim();
  const displayName = String(value.display_name ?? "").trim();
  const billingPeriod = String(value.billing_period ?? "");
  const currency = String(value.currency ?? "");
  const unitPrice = String(value.unit_price ?? "");
  const entitlements = value.entitlements;
  if (!/^[a-z0-9][a-z0-9_-]{1,39}$/u.test(planCode)
    || displayName.length < 1 || displayName.length > 80
    || !new Set(["none", "monthly", "yearly"]).has(billingPeriod)
    || !new Set(["CNY", "USD"]).has(currency)
    || !/^(?:0|[1-9][0-9]{0,10})\.[0-9]{2}$/u.test(unitPrice)
    || !entitlements || Object.getPrototypeOf(entitlements) !== Object.prototype) {
    invalidRequest();
  }
  const displayOrder = boundedInteger(value.display_order, 0, 10000);
  const trialDays = boundedInteger(value.trial_days, 0, 90);
  const normalizedEntitlements = {
    gateway_requests_per_month: boundedInteger(
      entitlements.gateway_requests_per_month, 0, 1_000_000_000,
    ),
    workspace_seats: boundedInteger(entitlements.workspace_seats, 1, 100_000),
    model_aliases: boundedInteger(entitlements.model_aliases, 1, 100_000),
    retention_days: boundedInteger(entitlements.retention_days, 1, 3650),
    priority_support: entitlements.priority_support === true,
  };
  return {
    plan_code: planCode,
    display_name: displayName,
    display_order: displayOrder,
    billing_period: billingPeriod,
    currency,
    unit_price: unitPrice,
    trial_days: trialDays,
    entitlements: normalizedEntitlements,
    publish: value.publish === true,
  };
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
