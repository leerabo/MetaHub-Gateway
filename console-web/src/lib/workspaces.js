import { IdentityApiError, readIdentityCsrfCookie } from "./identity.js";

const WORKSPACE_KEYS = Object.freeze([
  "environment",
  "kind",
  "name",
  "profile_state",
  "provisioning",
  "region",
  "revision",
  "service_mode",
  "slug",
  "status",
  "workspace_id",
]);

export async function listAccountWorkspaces({ controlApiUrl, signal } = {}) {
  return workspaceRequest(controlApiUrl, "/workspaces", { responseType: "list", signal });
}

export async function createAccountWorkspace({
  controlApiUrl,
  kind,
  name,
  slug,
  region,
  serviceMode,
  idempotencyKey,
  signal,
} = {}) {
  const normalizedKind = requireClientChoice(kind, ["personal", "organization"], "kind");
  const body = {
    kind: normalizedKind,
    region: requireClientText(region, 40, "region"),
    service_mode: requireClientChoice(
      serviceMode,
      ["internal_only", "public_service"],
      "service_mode",
    ),
  };
  if (normalizedKind === "organization") {
    body.name = requireClientText(name, 160, "name");
    body.slug = requireClientText(slug, 48, "slug");
  }
  return workspaceRequest(controlApiUrl, "/workspaces", {
    method: "POST",
    body,
    idempotencyKey: requireIdempotencyKey(idempotencyKey),
    csrf: true,
    responseType: "detail",
    signal,
  });
}

export async function getAccountWorkspace({ controlApiUrl, workspaceId, signal } = {}) {
  const id = requireClientText(workspaceId, 80, "workspace_id");
  return workspaceRequest(controlApiUrl, `/workspaces/${encodeURIComponent(id)}`, {
    responseType: "detail",
    signal,
  });
}

export async function updateAccountWorkspace({
  controlApiUrl,
  workspaceId,
  revision,
  changes,
  signal,
} = {}) {
  const id = requireClientText(workspaceId, 80, "workspace_id");
  if (!Number.isSafeInteger(revision) || revision < 1) invalidClient("revision");
  if (!changes || typeof changes !== "object" || Array.isArray(changes)) {
    invalidClient("changes");
  }
  const allowed = new Set(["display_label", "name", "profile_state"]);
  const body = {};
  for (const [key, value] of Object.entries(changes)) {
    if (!allowed.has(key)) invalidClient("changes");
    body[key] = requireClientText(value, key === "name" ? 160 : 120, key);
  }
  if (Object.keys(body).length === 0) invalidClient("changes");
  return workspaceRequest(controlApiUrl, `/workspaces/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
    csrf: true,
    revision,
    responseType: "detail",
    signal,
  });
}

async function workspaceRequest(controlApiUrl, path, options) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = readIdentityCsrfCookie();
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  if (options.revision) headers["If-Match"] = `"${options.revision}"`;
  let response;
  try {
    response = await fetch(workspaceUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new IdentityApiError({
      code: "workspace.network_unavailable",
      message: "暂时无法连接空间服务，请稍后重试。",
    });
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw new IdentityApiError({
      status: Number(response.status) || 0,
      code: payload.code,
      message: payload.message || "空间请求未完成，请稍后重试。",
      requestId: payload.request_id || response.headers?.get?.("X-Request-ID"),
      traceId: payload.trace_id || response.headers?.get?.("X-Trace-ID"),
    });
  }
  return options.responseType === "list" ? validateList(payload) : validateDetail(payload);
}

function validateDetail(payload) {
  requirePlainObject(payload, "response");
  requireExactKeys(payload, ["request_id", "trace_id", "workspace"], "response");
  return {
    workspace: validateWorkspace(payload.workspace),
    request_id: requireText(payload.request_id, 160, "request_id"),
    trace_id: requireText(payload.trace_id, 160, "trace_id"),
  };
}

function validateList(payload) {
  requirePlainObject(payload, "response");
  requireExactKeys(
    payload,
    ["limit", "offset", "request_id", "total", "trace_id", "workspaces"],
    "response",
  );
  if (!Array.isArray(payload.workspaces) || payload.workspaces.length > 100) {
    invalidResponse("workspaces");
  }
  return {
    workspaces: payload.workspaces.map(validateWorkspace),
    limit: requireCount(payload.limit, "limit"),
    offset: requireCount(payload.offset, "offset"),
    total: requireCount(payload.total, "total"),
    request_id: requireText(payload.request_id, 160, "request_id"),
    trace_id: requireText(payload.trace_id, 160, "trace_id"),
  };
}

function validateWorkspace(workspace) {
  requirePlainObject(workspace, "workspace");
  requireExactKeys(workspace, WORKSPACE_KEYS, "workspace");
  requirePlainObject(workspace.environment, "environment");
  requireExactKeys(workspace.environment, ["code", "origin", "status"], "environment");
  requirePlainObject(workspace.provisioning, "provisioning");
  requireExactKeys(workspace.provisioning, ["status"], "provisioning");
  return {
    workspace_id: requireText(workspace.workspace_id, 80, "workspace_id"),
    kind: requireChoice(workspace.kind, ["personal", "organization"], "kind"),
    status: requireChoice(workspace.status, ["active", "migration_conflict"], "status"),
    profile_state: requireChoice(
      workspace.profile_state,
      ["draft", "confirmed", "migration_conflict"],
      "profile_state",
    ),
    name: requireText(workspace.name, 160, "name"),
    slug: workspace.slug === null ? null : requireText(workspace.slug, 48, "slug"),
    region: requireText(workspace.region, 40, "region"),
    service_mode: requireChoice(
      workspace.service_mode,
      ["internal_only", "public_service"],
      "service_mode",
    ),
    environment: {
      code: requireChoice(workspace.environment.code, ["dev"], "environment.code"),
      status: requireChoice(
        workspace.environment.status,
        ["active", "disabled"],
        "environment.status",
      ),
      origin: requireChoice(
        workspace.environment.origin,
        ["system_created", "legacy_existing", "user_created"],
        "environment.origin",
      ),
    },
    provisioning: {
      status: requireChoice(
        workspace.provisioning.status,
        ["core_committed", "ready", "failed"],
        "provisioning.status",
      ),
    },
    revision: requirePositiveInteger(workspace.revision, "revision"),
  };
}

function workspaceUrl(controlApiUrl, path) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidClient("configuration");
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidClient("configuration");
  }
  return new URL(path, base).toString();
}

function requireIdempotencyKey(value) {
  const text = requireClientText(value, 128, "idempotency_key");
  if (text.length < 16 || !/^[A-Za-z0-9._~-]+$/u.test(text)) invalidClient("idempotency_key");
  return text;
}

function requireClientChoice(value, choices, label) {
  const text = requireClientText(value, 80, label);
  if (!new Set(choices).has(text)) invalidClient(label);
  return text;
}

function requireClientText(value, maximum, label) {
  if (!validText(value, maximum)) invalidClient(label);
  return value;
}

function requireChoice(value, choices, label) {
  const text = requireText(value, 80, label);
  if (!new Set(choices).has(text)) invalidResponse(label);
  return text;
}

function requireText(value, maximum, label) {
  if (!validText(value, maximum)) invalidResponse(label);
  return value;
}

function validText(value, maximum) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= maximum &&
    !/[\u0000-\u001f\u007f]/u.test(value)
  );
}

function requireCount(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) invalidResponse(label);
  return value;
}

function requirePositiveInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 1) invalidResponse(label);
  return value;
}

function requirePlainObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidResponse(label);
}

function requireExactKeys(value, expected, label) {
  const actual = Object.keys(value).sort();
  const allowed = [...expected].sort();
  if (actual.length !== allowed.length || actual.some((key, index) => key !== allowed[index])) {
    invalidResponse(label);
  }
}

function invalidClient(label) {
  throw new IdentityApiError({
    code: "workspace.request_invalid",
    message: `空间${label}配置无效。`,
  });
}

function invalidResponse(label) {
  throw new IdentityApiError({
    code: "workspace.response_invalid",
    message: `空间服务返回了无效的${label}数据。`,
  });
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
