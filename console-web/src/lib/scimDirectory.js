import { IdentityApiError } from "./identity.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const RESOURCE_ID = /^(?:sct|sgl|srm|ssr|scf)_[0-9a-f]{32}$/u;
const TOKEN = /^mhscim_[A-Za-z0-9_-]{32,220}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const ROLE_CODES = new Set([
  "admin",
  "developer",
  "security_admin",
  "finance_admin",
  "legal_admin",
  "auditor",
  "viewer",
]);

export function getScimDirectorySummary({ controlApiUrl, workspaceId, signal } = {}) {
  return scimRequest(controlApiUrl, workspaceId, "/scim-directory", {
    responseType: "summary",
    signal,
  });
}

export function createScimToken({
  controlApiUrl,
  workspaceId,
  expiresInDays,
  idempotencyKey,
  signal,
} = {}) {
  return scimRequest(controlApiUrl, workspaceId, "/scim-tokens", {
    method: "POST",
    body: { expires_in_days: integer(expiresInDays, 1, 365, "expires_in_days") },
    idempotencyKey: commandKey(idempotencyKey),
    responseType: "token",
    signal,
  });
}

export function rotateScimToken({
  controlApiUrl,
  workspaceId,
  tokenId,
  revision,
  expiresInDays,
  idempotencyKey,
  signal,
} = {}) {
  return scimRequest(
    controlApiUrl,
    workspaceId,
    `/scim-tokens/${encodeURIComponent(resourceId(tokenId, "sct_"))}/rotate`,
    {
      method: "POST",
      body: { expires_in_days: integer(expiresInDays, 1, 365, "expires_in_days") },
      revision: integer(revision, 1, 2_147_483_647, "revision"),
      idempotencyKey: commandKey(idempotencyKey),
      responseType: "token",
      signal,
    },
  );
}

export function upsertScimRoleMapping({
  controlApiUrl,
  workspaceId,
  groupId,
  roleCode,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  if (!ROLE_CODES.has(roleCode)) throw requestError("role_code");
  return scimRequest(controlApiUrl, workspaceId, "/scim-role-mappings", {
    method: "PUT",
    body: {
      scim_group_link_id: resourceId(groupId, "sgl_"),
      role_code: roleCode,
    },
    revision: integer(revision, 0, 2_147_483_647, "revision"),
    idempotencyKey: commandKey(idempotencyKey),
    responseType: "mapping",
    signal,
  });
}

export function resolveScimConflict({
  controlApiUrl,
  workspaceId,
  conflictId,
  resolutionCode,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  if (!new Set(["keep_local", "accept_directory", "ignore_directory"]).has(resolutionCode)) {
    throw requestError("resolution_code");
  }
  return scimRequest(
    controlApiUrl,
    workspaceId,
    `/scim-conflicts/${encodeURIComponent(resourceId(conflictId, "scf_"))}/resolve`,
    {
      method: "POST",
      body: { resolution_code: resolutionCode },
      revision: integer(revision, 1, 2_147_483_647, "revision"),
      idempotencyKey: commandKey(idempotencyKey),
      responseType: "conflict",
      signal,
    },
  );
}

async function scimRequest(controlApiUrl, workspaceId, path, options) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if ((options.method ?? "GET") !== "GET") {
    headers["X-CSRF-Token"] = readWorkspaceCsrfCookie();
  }
  if (options.revision !== undefined) headers["If-Match"] = `"${options.revision}"`;
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  let response;
  try {
    response = await fetch(scimUrl(controlApiUrl, workspaceId, path), {
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
      code: "scim.unavailable",
      message: "SCIM 目录服务暂时不可用，请稍后重试。",
    });
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw new IdentityApiError({
      status: Number(response.status) || 0,
      code: safeCode(payload.code) || "scim.unavailable",
      message: typeof payload.message === "string"
        ? payload.message
        : "SCIM 目录请求未完成，请稍后重试。",
      requestId: payload.request_id || response.headers?.get?.("X-Request-ID"),
      traceId: payload.trace_id || response.headers?.get?.("X-Trace-ID"),
    });
  }
  return validateResponse(payload, options.responseType);
}

function validateResponse(payload, responseType) {
  object(payload, "response");
  if (responseType === "summary") return validateSummary(payload);
  if (responseType === "token") {
    exact(payload, ["bearer_token", "idempotent_replay", "request_id", "token", "trace_id"]);
    const bearer = payload.bearer_token;
    if (bearer !== null && (typeof bearer !== "string" || !TOKEN.test(bearer))) invalid();
    return {
      token: validateToken(payload.token),
      bearer_token: bearer,
      idempotent_replay: boolean(payload.idempotent_replay),
      ...trace(payload),
    };
  }
  if (responseType === "mapping") {
    exact(payload, ["idempotent_replay", "mapping", "request_id", "trace_id"]);
    return {
      mapping: validateMapping(payload.mapping),
      idempotent_replay: boolean(payload.idempotent_replay),
      ...trace(payload),
    };
  }
  if (responseType === "conflict") {
    exact(payload, ["conflict", "idempotent_replay", "request_id", "trace_id"]);
    return {
      conflict: validateConflict(payload.conflict),
      idempotent_replay: boolean(payload.idempotent_replay),
      ...trace(payload),
    };
  }
  invalid();
}

function validateSummary(payload) {
  exact(payload, [
    "conflicts",
    "groups",
    "linked_group_count",
    "linked_user_count",
    "pending_invitation_count",
    "request_id",
    "role_mappings",
    "sync_runs",
    "tokens",
    "trace_id",
    "workspace_id",
  ]);
  array(payload.tokens, 10);
  array(payload.groups, 100);
  array(payload.role_mappings, 100);
  array(payload.sync_runs, 100);
  array(payload.conflicts, 100);
  return {
    workspace_id: workspaceId(payload.workspace_id),
    tokens: payload.tokens.map(validateToken),
    groups: payload.groups.map(validateGroup),
    role_mappings: payload.role_mappings.map(validateMapping),
    sync_runs: payload.sync_runs.map(validateSync),
    conflicts: payload.conflicts.map(validateConflict),
    pending_invitation_count: integer(payload.pending_invitation_count, 0, Number.MAX_SAFE_INTEGER),
    linked_user_count: integer(payload.linked_user_count, 0, Number.MAX_SAFE_INTEGER),
    linked_group_count: integer(payload.linked_group_count, 0, Number.MAX_SAFE_INTEGER),
    ...trace(payload),
  };
}

function validateToken(value) {
  object(value);
  exact(value, [
    "expires_at",
    "grace_expires_at",
    "last_used_at",
    "revision",
    "scim_token_id",
    "status",
    "token_prefix",
  ]);
  if (!new Set(["active", "rotating", "revoked"]).has(value.status)) invalid();
  return {
    scim_token_id: resourceId(value.scim_token_id, "sct_"),
    token_prefix: text(value.token_prefix, 8, 20),
    status: value.status,
    revision: integer(value.revision, 1, 2_147_483_647),
    expires_at: date(value.expires_at),
    grace_expires_at: nullableDate(value.grace_expires_at),
    last_used_at: nullableDate(value.last_used_at),
  };
}

function validateGroup(value) {
  object(value);
  exact(value, ["display_name", "revision", "scim_group_link_id", "state", "updated_at"]);
  if (!new Set(["active", "disabled"]).has(value.state)) invalid();
  return {
    scim_group_link_id: resourceId(value.scim_group_link_id, "sgl_"),
    display_name: text(value.display_name, 1, 120),
    state: value.state,
    revision: integer(value.revision, 1, 2_147_483_647),
    updated_at: date(value.updated_at),
  };
}

function validateMapping(value) {
  object(value);
  exact(value, [
    "group_display_name",
    "revision",
    "role_code",
    "scim_group_link_id",
    "scim_role_mapping_id",
    "status",
    "updated_at",
  ]);
  if (!ROLE_CODES.has(value.role_code) || !new Set(["active", "disabled"]).has(value.status)) {
    invalid();
  }
  return {
    scim_role_mapping_id: resourceId(value.scim_role_mapping_id, "srm_"),
    scim_group_link_id: resourceId(value.scim_group_link_id, "sgl_"),
    group_display_name: text(value.group_display_name, 1, 120),
    role_code: value.role_code,
    status: value.status,
    revision: integer(value.revision, 1, 2_147_483_647),
    updated_at: date(value.updated_at),
  };
}

function validateSync(value) {
  object(value);
  exact(value, [
    "completed_at",
    "conflict_count",
    "created_count",
    "disabled_count",
    "safe_outcome_code",
    "scim_sync_run_id",
    "started_at",
    "status",
    "updated_count",
  ]);
  if (!new Set(["running", "completed", "failed", "interrupted"]).has(value.status)) invalid();
  return {
    scim_sync_run_id: resourceId(value.scim_sync_run_id, "ssr_"),
    status: value.status,
    created_count: integer(value.created_count, 0, Number.MAX_SAFE_INTEGER),
    updated_count: integer(value.updated_count, 0, Number.MAX_SAFE_INTEGER),
    disabled_count: integer(value.disabled_count, 0, Number.MAX_SAFE_INTEGER),
    conflict_count: integer(value.conflict_count, 0, Number.MAX_SAFE_INTEGER),
    safe_outcome_code: safeCode(value.safe_outcome_code) || invalid(),
    started_at: date(value.started_at),
    completed_at: nullableDate(value.completed_at),
  };
}

function validateConflict(value) {
  object(value);
  exact(value, [
    "created_at",
    "reason_code",
    "resolved_at",
    "revision",
    "scim_conflict_id",
    "status",
    "subject_type",
  ]);
  if (!new Set(["user", "group", "membership"]).has(value.subject_type)
    || !new Set([
      "owner_protected",
      "human_membership",
      "revision_stale",
      "duplicate_external_id",
      "account_unverified",
      "role_conflict",
    ]).has(value.reason_code)
    || !new Set(["open", "resolved", "ignored"]).has(value.status)) invalid();
  return {
    scim_conflict_id: resourceId(value.scim_conflict_id, "scf_"),
    subject_type: value.subject_type,
    reason_code: value.reason_code,
    status: value.status,
    revision: integer(value.revision, 1, 2_147_483_647),
    created_at: date(value.created_at),
    resolved_at: nullableDate(value.resolved_at),
  };
}

function scimUrl(controlApiUrl, value, path) {
  const base = new URL(String(controlApiUrl ?? ""));
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    throw requestError("control_api_url");
  }
  return `${base.toString().replace(/\/$/u, "")}/workspaces/${workspaceId(value)}${path}`;
}

async function readJson(response) {
  try {
    const value = await response.json();
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
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

function workspaceId(value) {
  if (typeof value !== "string" || !WORKSPACE_ID.test(value)) throw requestError("workspace_id");
  return value;
}

function resourceId(value, prefix) {
  if (typeof value !== "string" || !value.startsWith(prefix) || !RESOURCE_ID.test(value)) {
    throw requestError("resource_id");
  }
  return value;
}

function commandKey(value) {
  if (typeof value !== "string" || !IDEMPOTENCY_KEY.test(value)) {
    throw requestError("idempotency_key");
  }
  return value;
}

function integer(value, minimum, maximum, field = "integer") {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) {
    throw requestError(field);
  }
  return number;
}

function boolean(value) {
  if (typeof value !== "boolean") invalid();
  return value;
}

function text(value, minimum, maximum) {
  if (typeof value !== "string" || value !== value.trim()
    || value.length < minimum || value.length > maximum) invalid();
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
    request_id: text(payload.request_id, 8, 160),
    trace_id: text(payload.trace_id, 8, 160),
  };
}

function safeCode(value) {
  return typeof value === "string" && /^[a-z0-9][a-z0-9._]{1,79}$/u.test(value)
    ? value
    : "";
}

function invalid() {
  throw new IdentityApiError({
    status: 502,
    code: "scim.response_invalid",
    message: "SCIM 目录响应合同不完整。",
  });
}

function requestError(field) {
  return new IdentityApiError({
    status: 422,
    code: "scim.request_invalid",
    message: `SCIM 目录请求字段无效：${field}。`,
  });
}
