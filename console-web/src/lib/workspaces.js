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
const WORKSPACE_CSRF_COOKIE_NAME = "__Host-metahub-workspace-csrf";
const WORKSPACE_ROLES = Object.freeze([
  "owner",
  "admin",
  "developer",
  "security_admin",
  "finance_admin",
  "legal_admin",
  "auditor",
  "viewer",
  "service_account",
]);
const DELEGABLE_WORKSPACE_ROLES = Object.freeze(
  WORKSPACE_ROLES.filter((role) => !new Set(["owner", "service_account"]).has(role)),
);

export async function listIdentityWorkspaces({ controlApiUrl, signal } = {}) {
  return workspaceAccessRequest(controlApiUrl, "/identity/workspaces", {
    responseType: "identityWorkspaceList",
    signal,
  });
}

export async function switchIdentityWorkspace({ controlApiUrl, workspaceId, signal } = {}) {
  return workspaceAccessRequest(controlApiUrl, "/identity/workspace-switches", {
    method: "POST",
    body: { workspace_id: requireClientId(workspaceId, "workspace_id") },
    csrf: "global",
    responseType: "workspaceContext",
    signal,
  });
}

export async function getCurrentWorkspaceSession({ controlApiUrl, signal } = {}) {
  return workspaceAccessRequest(controlApiUrl, "/workspace/session", {
    responseType: "workspaceSession",
    signal,
  });
}

export async function listWorkspaceMemberships({ controlApiUrl, signal } = {}) {
  return workspaceAccessRequest(controlApiUrl, "/memberships", {
    responseType: "membershipList",
    signal,
  });
}

export async function updateWorkspaceMembership({
  controlApiUrl,
  membershipId,
  revision,
  role,
  status,
  signal,
} = {}) {
  const body = {};
  if (role !== undefined) {
    body.role = requireClientChoice(role, DELEGABLE_WORKSPACE_ROLES, "role");
  }
  if (status !== undefined) {
    body.status = requireClientChoice(status, ["active", "disabled"], "status");
  }
  if (Object.keys(body).length === 0) invalidClient("changes");
  if (!Number.isSafeInteger(revision) || revision < 1) invalidClient("revision");
  return workspaceAccessRequest(
    controlApiUrl,
    `/memberships/${encodeURIComponent(requireClientId(membershipId, "membership_id"))}`,
    {
      method: "PATCH",
      body,
      csrf: "workspace",
      revision,
      responseType: "membership",
      signal,
    },
  );
}

export async function createWorkspaceMembershipInvitation({
  controlApiUrl,
  email,
  role,
  expiresInHours = 72,
  signal,
} = {}) {
  const normalizedEmail = requireClientText(email, 320, "email").trim().toLowerCase();
  if (!normalizedEmail.includes("@")) invalidClient("email");
  if (!Number.isSafeInteger(expiresInHours) || expiresInHours < 1 || expiresInHours > 168) {
    invalidClient("expires_in_hours");
  }
  return workspaceAccessRequest(controlApiUrl, "/membership-invitations", {
    method: "POST",
    body: {
      email: normalizedEmail,
      role: requireClientChoice(role, DELEGABLE_WORKSPACE_ROLES, "role"),
      expires_in_hours: expiresInHours,
    },
    csrf: "workspace",
    responseType: "membershipInvitation",
    signal,
  });
}

export async function transferWorkspaceOwner({
  controlApiUrl,
  targetMembershipId,
  currentPassword,
  signal,
} = {}) {
  return workspaceAccessRequest(controlApiUrl, "/workspace-owner-transfers", {
    method: "POST",
    body: {
      target_membership_id: requireClientId(targetMembershipId, "target_membership_id"),
      current_password: requireClientText(currentPassword, 1024, "current_password"),
    },
    csrf: "workspace",
    responseType: "ownerTransfer",
    signal,
  });
}

export async function getWorkspaceConsoleNavigation({ controlApiUrl, signal } = {}) {
  return workspaceAccessRequest(controlApiUrl, "/console/navigation", {
    responseType: "navigation",
    signal,
  });
}

export function readWorkspaceCsrfCookie(cookieText = globalThis.document?.cookie ?? "") {
  return readExactCookie(cookieText, WORKSPACE_CSRF_COOKIE_NAME, "workspace_session.csrf_invalid");
}

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

async function workspaceAccessRequest(controlApiUrl, path, options) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf === "global") headers["X-CSRF-Token"] = readIdentityCsrfCookie();
  if (options.csrf === "workspace") headers["X-CSRF-Token"] = readWorkspaceCsrfCookie();
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
      code: "workspace_session.unavailable",
      message: "暂时无法连接空间授权服务，请稍后重试。",
    });
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw new IdentityApiError({
      status: Number(response.status) || 0,
      code: payload.code,
      message: payload.message || "空间授权请求未完成，请稍后重试。",
      requestId: payload.request_id || response.headers?.get?.("X-Request-ID"),
      traceId: payload.trace_id || response.headers?.get?.("X-Trace-ID"),
    });
  }
  try {
    return validateWorkspaceAccessResponse(payload, options.responseType);
  } catch (error) {
    if (error instanceof IdentityApiError && error.code === "workspace.response_invalid") {
      invalidAccessResponse("response");
    }
    throw error;
  }
}

function validateWorkspaceAccessResponse(payload, responseType) {
  requirePlainObject(payload, "response");
  if (responseType === "identityWorkspaceList") {
    requireExactKeys(
      payload,
      ["limit", "offset", "request_id", "total", "trace_id", "workspaces"],
      "response",
    );
    if (!Array.isArray(payload.workspaces) || payload.workspaces.length > 100) {
      invalidAccessResponse("workspaces");
    }
    return {
      workspaces: payload.workspaces.map(validateIdentityWorkspace),
      limit: requireCount(payload.limit, "limit"),
      offset: requireCount(payload.offset, "offset"),
      total: requireCount(payload.total, "total"),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "workspaceContext" || responseType === "workspaceSession") {
    const responseKeys = responseType === "workspaceContext"
      ? ["context", "request_id", "trace_id"]
      : ["context", "request_id", "trace_id"];
    requireExactKeys(payload, responseKeys, "response");
    return { context: validateWorkspaceContext(payload.context), ...validateRequestTrace(payload) };
  }
  if (responseType === "membershipList") {
    requireExactKeys(
      payload,
      ["limit", "memberships", "offset", "request_id", "total", "trace_id"],
      "response",
    );
    if (!Array.isArray(payload.memberships) || payload.memberships.length > 100) {
      invalidAccessResponse("memberships");
    }
    return {
      memberships: payload.memberships.map(validateMembership),
      limit: requireCount(payload.limit, "limit"),
      offset: requireCount(payload.offset, "offset"),
      total: requireCount(payload.total, "total"),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "membership") {
    requireExactKeys(payload, ["membership", "request_id", "trace_id"], "response");
    return { membership: validateMembership(payload.membership), ...validateRequestTrace(payload) };
  }
  if (responseType === "membershipInvitation") {
    requireExactKeys(
      payload,
      ["accept_token", "invitation", "request_id", "trace_id"],
      "response",
    );
    requirePlainObject(payload.invitation, "invitation");
    requireExactKeys(
      payload.invitation,
      ["expires_at", "invitation_id", "role", "status"],
      "invitation",
    );
    const token = requireText(payload.accept_token, 64, "accept_token");
    if (!/^[A-Za-z0-9_-]{43}$/u.test(token)) invalidAccessResponse("accept_token");
    return {
      invitation: {
        invitation_id: requireText(payload.invitation.invitation_id, 80, "invitation_id"),
        role: requireChoice(payload.invitation.role, WORKSPACE_ROLES, "invitation.role"),
        status: requireChoice(payload.invitation.status, ["pending"], "invitation.status"),
        expires_at: requireTimestamp(payload.invitation.expires_at, "invitation.expires_at"),
      },
      accept_token: token,
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "ownerTransfer") {
    requireExactKeys(
      payload,
      ["request_id", "source", "target", "trace_id", "transferred"],
      "response",
    );
    if (payload.transferred !== true) invalidAccessResponse("transferred");
    return {
      transferred: true,
      source: validateMembership(payload.source),
      target: validateMembership(payload.target),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "navigation") return validateWorkspaceNavigation(payload);
  invalidAccessResponse("type");
}

function validateIdentityWorkspace(workspace) {
  requirePlainObject(workspace, "workspace");
  requireExactKeys(workspace, ["kind", "name", "role", "slug", "workspace_id"], "workspace");
  return {
    workspace_id: requireText(workspace.workspace_id, 80, "workspace_id"),
    kind: requireChoice(workspace.kind, ["personal", "organization"], "kind"),
    name: requireText(workspace.name, 160, "name"),
    slug: workspace.slug === null ? null : requireText(workspace.slug, 48, "slug"),
    role: requireChoice(workspace.role, WORKSPACE_ROLES, "role"),
  };
}

function validateWorkspaceContext(context) {
  requirePlainObject(context, "context");
  requireExactKeys(
    context,
    ["expires_at", "kind", "name", "role", "session_status", "slug", "workspace_id"],
    "context",
  );
  return {
    ...validateIdentityWorkspace({
      workspace_id: context.workspace_id,
      kind: context.kind,
      name: context.name,
      slug: context.slug,
      role: context.role,
    }),
    session_status: requireChoice(context.session_status, ["active"], "session_status"),
    expires_at: requireTimestamp(context.expires_at, "expires_at"),
  };
}

function validateMembership(membership) {
  requirePlainObject(membership, "membership");
  requireExactKeys(
    membership,
    [
      "can_manage",
      "display_name",
      "is_current",
      "membership_id",
      "revision",
      "role",
      "status",
    ],
    "membership",
  );
  if (typeof membership.can_manage !== "boolean" || typeof membership.is_current !== "boolean") {
    invalidAccessResponse("membership.flags");
  }
  return {
    membership_id: requireText(membership.membership_id, 80, "membership_id"),
    display_name: requireText(membership.display_name, 160, "display_name"),
    role: requireChoice(membership.role, WORKSPACE_ROLES, "membership.role"),
    status: requireChoice(membership.status, ["pending", "active", "disabled"], "membership.status"),
    revision: requirePositiveInteger(membership.revision, "membership.revision"),
    is_current: membership.is_current,
    can_manage: membership.can_manage,
  };
}

function validateWorkspaceNavigation(payload) {
  requireExactKeys(
    payload,
    [
      "active_navigation_item_id",
      "authorization_fingerprint",
      "gateway_base_url",
      "gateway_base_url_status",
      "navigation_items",
      "permission_codes",
      "permission_revision",
      "request_id",
      "trace_id",
    ],
    "response",
  );
  if (!Array.isArray(payload.navigation_items) || payload.navigation_items.length > 200) {
    invalidAccessResponse("navigation_items");
  }
  validateTextArray(payload.permission_codes, 300, "permission_codes");
  requirePositiveInteger(payload.permission_revision, "permission_revision");
  requireText(payload.authorization_fingerprint, 256, "authorization_fingerprint");
  if (payload.gateway_base_url !== null) requireText(payload.gateway_base_url, 2048, "gateway_base_url");
  requireChoice(
    payload.gateway_base_url_status,
    ["ready", "configuration_required"],
    "gateway_base_url_status",
  );
  return {
    navigation_items: payload.navigation_items.map(validateNavigationItem),
    active_navigation_item_id: requireText(
      payload.active_navigation_item_id,
      80,
      "active_navigation_item_id",
    ),
    ...validateRequestTrace(payload),
  };
}

function validateNavigationItem(item) {
  requirePlainObject(item, "navigation_item");
  requireExactKeys(
    item,
    [
      "description",
      "disabled",
      "empty_state_hint",
      "group",
      "id",
      "label",
      "order",
      "required_permissions",
      "required_roles",
      "target_section",
      "visible",
    ],
    "navigation_item",
  );
  if (typeof item.disabled !== "boolean" || typeof item.visible !== "boolean") {
    invalidAccessResponse("navigation_item.flags");
  }
  validateTextArray(item.required_permissions, 300, "required_permissions");
  validateTextArray(item.required_roles, 20, "required_roles");
  if (!Number.isSafeInteger(item.order) || item.order < 0) invalidAccessResponse("order");
  return {
    id: requireText(item.id, 80, "navigation_item.id"),
    label: requireText(item.label, 160, "navigation_item.label"),
    group: requireText(item.group, 160, "navigation_item.group"),
    target_section: requireText(item.target_section, 80, "navigation_item.target_section"),
    visible: item.visible,
    disabled: item.disabled,
    order: item.order,
  };
}

function validateTextArray(values, maximum, label) {
  if (!Array.isArray(values) || values.length > maximum) invalidAccessResponse(label);
  const result = values.map((value) => requireText(value, 160, label));
  if (new Set(result).size !== result.length) invalidAccessResponse(label);
  return result;
}

function validateRequestTrace(payload) {
  return {
    request_id: requireText(payload.request_id, 160, "request_id"),
    trace_id: requireText(payload.trace_id, 160, "trace_id"),
  };
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

function requireClientId(value, label) {
  const text = requireClientText(value, 80, label);
  const pattern = label.includes("membership")
    ? /^wmb_[0-9a-f]{32}$/u
    : /^wsp_[0-9a-f]{32}$/u;
  if (!pattern.test(text)) invalidClient(label);
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

function requireTimestamp(value, label) {
  const text = requireText(value, 80, label);
  if (!Number.isFinite(Date.parse(text))) invalidAccessResponse(label);
  return text;
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

function invalidAccessResponse(label) {
  throw new IdentityApiError({
    code: "workspace_session.response_invalid",
    message: `空间授权服务返回了无效的${label}数据。`,
  });
}

function readExactCookie(cookieText, cookieName, errorCode) {
  const matches = String(cookieText)
    .split(";")
    .map((part) => part.trim())
    .filter((part) => part.startsWith(`${cookieName}=`))
    .map((part) => part.slice(cookieName.length + 1));
  if (matches.length !== 1 || !matches[0] || matches[0].includes(",")) {
    throw new IdentityApiError({
      code: errorCode,
      message: "空间安全校验已失效，请重新进入空间。",
    });
  }
  try {
    const value = decodeURIComponent(matches[0]);
    if (!value || /[\u0000-\u0020\u007f,]/u.test(value)) throw new Error("invalid");
    return value;
  } catch {
    throw new IdentityApiError({
      code: errorCode,
      message: "空间安全校验已失效，请重新进入空间。",
    });
  }
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
