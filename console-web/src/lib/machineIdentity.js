// Strict PRD-118 client: Workspace Cookie credentials only and one-time secrets in memory.
import { IdentityApiError } from "./identity.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const SERVICE_ACCOUNT_ID = /^svc_[0-9a-f]{32}$/u;
const OAUTH_CLIENT_ID = /^ocl_[0-9a-f]{32}$/u;
const PUBLIC_CLIENT_ID = /^moc_[0-9a-f]{32}$/u;
const CLIENT_SECRET = /^mcs_[A-Za-z0-9_-]{40,80}$/u;
const CREDENTIAL_PREFIX = /^mcs_[A-Za-z0-9_-]{8}$/u;
const NAME = /^[a-z0-9][a-z0-9_-]{1,79}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const SAFE_TEXT = /^[^\u0000-\u001f\u007f]+$/u;
const SCOPE_TEMPLATES = Object.freeze({
  provider_reader: Object.freeze(["provider.read"]),
  observability_reader: Object.freeze(["request_log.read"]),
  compliance_collector: Object.freeze(["compliance.collect"]),
});
const ERROR_MESSAGES = Object.freeze({
  "workspace_session.required": "需要有效的空间会话。",
  "workspace_session.invalid": "空间会话无效，请重新进入空间。",
  "workspace_session.expired": "空间会话已过期，请重新进入空间。",
  "workspace_session.revoked": "空间会话已撤销。",
  "workspace_session.permission_denied": "当前空间职责无权执行此操作。",
  "workspace_session.credential_conflict": "检测到冲突的凭据域。",
  "workspace_session.csrf_invalid": "空间安全校验已失效，请刷新后重试。",
  "machine_identity.request_invalid": "机器身份请求字段无效。",
  "machine_identity.not_found": "Service Account 不存在或不可见。",
  "machine_identity.client_not_found": "OAuth client 不存在。",
  "machine_identity.name_conflict": "当前空间已存在同名 Service Account。",
  "machine_identity.client_conflict": "此 Service Account 已有 OAuth client。",
  "machine_identity.conflict": "机器身份状态已变化，请刷新后重试。",
  "machine_identity.revision_conflict": "机器身份版本已变化，请刷新后重试。",
  "machine_identity.idempotency_conflict": "请求标识已用于其他操作，请重新发起这次操作。",
  "machine_identity.scope_denied": "所选 scope 超出当前操作者授权。",
  "machine_identity.unavailable": "机器身份服务暂时不可用，请稍后重试。",
});

export function listWorkspaceServiceAccounts({ controlApiUrl, workspaceId, signal } = {}) {
  return machineRequest(controlApiUrl, workspaceId, "/service-accounts", {
    responseType: "list",
    signal,
  });
}

export function createWorkspaceServiceAccount({
  controlApiUrl,
  workspaceId,
  name,
  displayName,
  scopeTemplate,
  scopes,
  tokenTtlSeconds,
  ipAllowlist = [],
  mtlsFingerprintSha256 = null,
  idempotencyKey,
  signal,
} = {}) {
  const template = choice(scopeTemplate, Object.keys(SCOPE_TEMPLATES), "scope_template");
  const normalizedScopes = scopeList(scopes, template);
  const networks = stringList(ipAllowlist, 16, 64, "ip_allowlist");
  const fingerprint = optionalHash(mtlsFingerprintSha256);
  return machineRequest(controlApiUrl, workspaceId, "/service-accounts", {
    method: "POST",
    body: {
      name: machineName(name),
      display_name: text(displayName, 2, 120, "display_name"),
      scope_template: template,
      scopes: normalizedScopes,
      token_ttl_seconds: integer(tokenTtlSeconds, 60, 3600, "token_ttl_seconds"),
      ip_allowlist: networks,
      mtls_fingerprint_sha256: fingerprint,
    },
    idempotencyKey: commandKey(idempotencyKey),
    responseType: "serviceAccount",
    signal,
  });
}

export function createWorkspaceOAuthClient({
  controlApiUrl,
  workspaceId,
  serviceAccountId,
  idempotencyKey,
  signal,
} = {}) {
  return machineRequest(controlApiUrl, workspaceId, "/oauth/clients", {
    method: "POST",
    body: {
      service_account_id: serviceAccountIdValue(serviceAccountId),
      audience: "metahub-control",
    },
    idempotencyKey: commandKey(idempotencyKey),
    responseType: "credential",
    signal,
  });
}

export function rotateWorkspaceServiceAccountCredential({
  controlApiUrl,
  workspaceId,
  serviceAccountId,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  const id = serviceAccountIdValue(serviceAccountId);
  return machineRequest(
    controlApiUrl,
    workspaceId,
    `/service-accounts/${encodeURIComponent(id)}/rotate`,
    {
      method: "POST",
      revision: integer(revision, 1, 2_147_483_647, "revision"),
      idempotencyKey: commandKey(idempotencyKey),
      responseType: "credential",
      signal,
    },
  );
}

export function revokeWorkspaceServiceAccount({
  controlApiUrl,
  workspaceId,
  serviceAccountId,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  const id = serviceAccountIdValue(serviceAccountId);
  return machineRequest(
    controlApiUrl,
    workspaceId,
    `/service-accounts/${encodeURIComponent(id)}`,
    {
      method: "DELETE",
      revision: integer(revision, 1, 2_147_483_647, "revision"),
      idempotencyKey: commandKey(idempotencyKey),
      responseType: "serviceAccount",
      signal,
    },
  );
}

async function machineRequest(controlApiUrl, workspaceId, path, options) {
  const expectedWorkspaceId = workspaceIdValue(workspaceId);
  const method = options.method ?? "GET";
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = readWorkspaceCsrfCookie();
  if (options.idempotencyKey !== undefined) {
    headers["Idempotency-Key"] = options.idempotencyKey;
  }
  if (options.revision !== undefined) headers["If-Match"] = `"${options.revision}"`;
  let response;
  try {
    response = await fetch(machineUrl(controlApiUrl, path), {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError("machine_identity.unavailable");
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = safeCode(payload.code) || "machine_identity.unavailable";
    throw clientError(
      code,
      Number(response.status) || 0,
      safeLinkId(payload.request_id) || response.headers?.get?.("X-Request-ID") || "-",
      safeLinkId(payload.trace_id) || response.headers?.get?.("X-Trace-ID") || "-",
    );
  }
  return validateResponse(payload, options.responseType, expectedWorkspaceId);
}

function validateResponse(payload, responseType, expectedWorkspaceId) {
  object(payload);
  if (responseType === "list") {
    exact(payload, ["request_id", "service_accounts", "trace_id"]);
    array(payload.service_accounts, 100);
    return {
      service_accounts: payload.service_accounts.map((item) =>
        validateServiceAccount(item, expectedWorkspaceId)),
      ...trace(payload),
    };
  }
  if (responseType === "serviceAccount") {
    exact(payload, ["request_id", "service_account", "trace_id"]);
    return {
      service_account: validateServiceAccount(payload.service_account, expectedWorkspaceId),
      ...trace(payload),
    };
  }
  if (responseType === "credential") {
    exact(payload, ["client_secret", "oauth_client", "request_id", "service_account", "trace_id"]);
    const serviceAccount = validateServiceAccount(payload.service_account, expectedWorkspaceId);
    const oauthClient = validateOAuthClient(payload.oauth_client);
    if (JSON.stringify(serviceAccount.oauth_client) !== JSON.stringify(oauthClient)) invalid();
    if (typeof payload.client_secret !== "string" || !CLIENT_SECRET.test(payload.client_secret)) {
      invalid();
    }
    return {
      service_account: serviceAccount,
      oauth_client: oauthClient,
      client_secret: payload.client_secret,
      ...trace(payload),
    };
  }
  invalid();
}

function validateServiceAccount(value, expectedWorkspaceId) {
  object(value);
  exact(value, [
    "constraint_generation",
    "created_at",
    "display_name",
    "ip_allowlist",
    "last_used_at",
    "mtls_required",
    "name",
    "oauth_client",
    "revision",
    "revoked_at",
    "scope_template",
    "scopes",
    "service_account_id",
    "status",
    "token_ttl_seconds",
    "updated_at",
    "workspace_id",
  ]);
  const workspaceId = workspaceIdValue(value.workspace_id);
  if (workspaceId !== expectedWorkspaceId) invalid();
  const template = choiceResponse(value.scope_template, Object.keys(SCOPE_TEMPLATES));
  const scopes = scopeListResponse(value.scopes, template);
  if (!new Set(["active", "disabled", "revoked"]).has(value.status)) invalid();
  if (typeof value.mtls_required !== "boolean") invalid();
  return {
    service_account_id: resourceId(value.service_account_id, SERVICE_ACCOUNT_ID),
    workspace_id: workspaceId,
    name: responseText(value.name, 2, 80),
    display_name: responseText(value.display_name, 2, 120),
    status: value.status,
    scope_template: template,
    scopes,
    token_ttl_seconds: responseInteger(value.token_ttl_seconds, 60, 3600),
    ip_allowlist: stringListResponse(value.ip_allowlist, 16, 64),
    mtls_required: value.mtls_required,
    constraint_generation: responseInteger(value.constraint_generation, 1, 2_147_483_647),
    revision: responseInteger(value.revision, 1, 2_147_483_647),
    last_used_at: nullableDate(value.last_used_at),
    revoked_at: nullableDate(value.revoked_at),
    created_at: date(value.created_at),
    updated_at: date(value.updated_at),
    oauth_client: value.oauth_client === null ? null : validateOAuthClient(value.oauth_client),
  };
}

function validateOAuthClient(value) {
  object(value);
  exact(value, [
    "audience",
    "client_id",
    "created_at",
    "credential_expires_at",
    "credential_generation",
    "credential_prefix",
    "last_used_at",
    "oauth_client_id",
    "revision",
    "status",
    "updated_at",
  ]);
  if (value.audience !== "metahub-control"
    || !new Set(["active", "rotating", "revoked"]).has(value.status)) invalid();
  const prefix = value.credential_prefix;
  if (prefix !== null && (typeof prefix !== "string" || !CREDENTIAL_PREFIX.test(prefix))) {
    invalid();
  }
  return {
    oauth_client_id: resourceId(value.oauth_client_id, OAUTH_CLIENT_ID),
    client_id: resourceId(value.client_id, PUBLIC_CLIENT_ID),
    audience: "metahub-control",
    status: value.status,
    credential_generation: responseInteger(value.credential_generation, 1, 2_147_483_647),
    revision: responseInteger(value.revision, 1, 2_147_483_647),
    credential_prefix: prefix,
    credential_expires_at: nullableDate(value.credential_expires_at),
    last_used_at: nullableDate(value.last_used_at),
    created_at: date(value.created_at),
    updated_at: date(value.updated_at),
  };
}

function machineUrl(controlApiUrl, path) {
  let base;
  try {
    base = new URL(String(controlApiUrl ?? ""));
  } catch {
    throw requestError("control_api_url");
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    throw requestError("control_api_url");
  }
  return `${base.toString().replace(/\/$/u, "")}${path}`;
}

async function readJson(response) {
  try {
    const value = await response.json();
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

function scopeList(values, template) {
  if (!Array.isArray(values)) throw requestError("scopes");
  const normalized = values.map((value) => String(value));
  const allowed = SCOPE_TEMPLATES[template];
  if (normalized.length < 1 || normalized.length > allowed.length
    || normalized.some((value, index) => !allowed.includes(value)
      || (index > 0 && normalized[index - 1] >= value))) {
    throw requestError("scopes");
  }
  return normalized;
}

function scopeListResponse(values, template) {
  try {
    return scopeList(values, template);
  } catch {
    invalid();
  }
}

function stringList(values, maximum, itemMaximum, field) {
  if (!Array.isArray(values) || values.length > maximum) throw requestError(field);
  const normalized = values.map((value) => String(value).trim());
  if (normalized.some((value, index) => !value || value.length > itemMaximum
    || !SAFE_TEXT.test(value) || (index > 0 && normalized[index - 1] >= value))) {
    throw requestError(field);
  }
  return normalized;
}

function stringListResponse(values, maximum, itemMaximum) {
  try {
    return stringList(values, maximum, itemMaximum, "response");
  } catch {
    invalid();
  }
}

function machineName(value) {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (!NAME.test(normalized)) throw requestError("name");
  return normalized;
}

function optionalHash(value) {
  if (value === null || value === undefined || value === "") return null;
  const normalized = String(value).trim().toLowerCase();
  if (!HASH.test(normalized)) throw requestError("mtls_fingerprint_sha256");
  return normalized;
}

function serviceAccountIdValue(value) {
  return resourceId(value, SERVICE_ACCOUNT_ID, true);
}

function workspaceIdValue(value) {
  return resourceId(value, WORKSPACE_ID, true, "workspace_id");
}

function commandKey(value) {
  if (typeof value !== "string" || !IDEMPOTENCY_KEY.test(value)) {
    throw requestError("idempotency_key");
  }
  return value;
}

function resourceId(value, pattern, request = false, field = "resource_id") {
  if (typeof value !== "string" || !pattern.test(value)) {
    if (request) throw requestError(field);
    invalid();
  }
  return value;
}

function choice(value, choices, field) {
  if (!choices.includes(value)) throw requestError(field);
  return value;
}

function choiceResponse(value, choices) {
  if (!choices.includes(value)) invalid();
  return value;
}

function integer(value, minimum, maximum, field) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) {
    throw requestError(field);
  }
  return number;
}

function responseInteger(value, minimum, maximum) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalid();
  return value;
}

function text(value, minimum, maximum, field) {
  const normalized = String(value ?? "").trim();
  if (normalized.length < minimum || normalized.length > maximum || !SAFE_TEXT.test(normalized)) {
    throw requestError(field);
  }
  return normalized;
}

function responseText(value, minimum, maximum) {
  if (typeof value !== "string" || value !== value.trim()
    || value.length < minimum || value.length > maximum || !SAFE_TEXT.test(value)) invalid();
  return value;
}

function date(value) {
  if (typeof value !== "string" || !value || Number.isNaN(Date.parse(value))) invalid();
  return value;
}

function nullableDate(value) {
  return value === null ? null : date(value);
}

function trace(payload) {
  return {
    request_id: responseText(payload.request_id, 8, 160),
    trace_id: responseText(payload.trace_id, 8, 160),
  };
}

function exact(value, keys) {
  if (JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) invalid();
}

function object(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  return value;
}

function array(value, maximum) {
  if (!Array.isArray(value) || value.length > maximum) invalid();
  return value;
}

function safeCode(value) {
  return typeof value === "string" && /^[a-z0-9][a-z0-9._]{1,95}$/u.test(value)
    ? value
    : "";
}

function safeLinkId(value) {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/u.test(value)
    ? value
    : "";
}

function clientError(code, status = 0, requestId = "-", traceId = "-") {
  return new IdentityApiError({
    status,
    code,
    message: ERROR_MESSAGES[code] ?? ERROR_MESSAGES["machine_identity.unavailable"],
    requestId,
    traceId,
  });
}

function requestError(field) {
  return new IdentityApiError({
    status: 422,
    code: "machine_identity.request_invalid",
    message: `机器身份请求字段无效：${field}。`,
  });
}

function invalid() {
  throw new IdentityApiError({
    status: 502,
    code: "machine_identity.response_invalid",
    message: "机器身份响应合同不完整。",
  });
}
