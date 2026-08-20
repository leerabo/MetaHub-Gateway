const CSRF_COOKIE_NAME = "__Host-metahub-global-csrf";
const ACCOUNT_KEYS = Object.freeze(["display_name", "email", "mfa_configured", "status"]);
const SESSION_KEYS = Object.freeze([
  "created_at",
  "device_family",
  "expires_at",
  "is_current",
  "last_seen_at",
  "session_id",
]);

export class IdentityApiError extends Error {
  constructor({ status = 0, code, message, requestId = "-", traceId = "-" } = {}) {
    super(safeErrorText(message));
    this.name = "IdentityApiError";
    this.status = status;
    this.code = boundedString(code, 120) || "identity.request_failed";
    this.requestId = boundedString(requestId, 160) || "-";
    this.traceId = boundedString(traceId, 160) || "-";
  }
}

export async function loginIdentity({ controlApiUrl, email, password, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/login", {
    method: "POST",
    body: { email, password },
    responseType: "accountSession",
    signal,
  });
}

export async function registerIdentity({
  controlApiUrl,
  email,
  displayName,
  password,
  consentVersion,
  invitationToken,
  signal,
} = {}) {
  const body = {
    email,
    display_name: displayName,
    password,
    consent_version: consentVersion,
  };
  if (invitationToken !== undefined && invitationToken !== null && invitationToken !== "") {
    body.invitation_token = invitationToken;
  }
  return identityRequest(controlApiUrl, "/identity/registrations", {
    method: "POST",
    body,
    responseType: "pendingChallenge",
    signal,
  });
}

export async function verifyIdentityRegistration({ controlApiUrl, intentId, code, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/verifications", {
    method: "POST",
    body: { intent_id: intentId, code },
    responseType: "accountSession",
    signal,
  });
}

export async function resendIdentityVerification({ controlApiUrl, intentId, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/verifications/resend", {
    method: "POST",
    body: { intent_id: intentId },
    responseType: "pendingChallenge",
    signal,
  });
}

export async function requestIdentityPasswordReset({ controlApiUrl, email, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/password-resets", {
    method: "POST",
    body: { email },
    responseType: "pendingChallenge",
    signal,
  });
}

export async function confirmIdentityPasswordReset({
  controlApiUrl,
  intentId,
  code,
  newPassword,
  signal,
} = {}) {
  return identityRequest(controlApiUrl, "/identity/password-resets/confirm", {
    method: "POST",
    body: { intent_id: intentId, code, new_password: newPassword },
    responseType: "accountSession",
    signal,
  });
}

export async function getIdentityMe({ controlApiUrl, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/me", {
    responseType: "accountSession",
    signal,
  });
}

export async function listIdentitySessions({ controlApiUrl, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/sessions", {
    responseType: "sessionList",
    signal,
  });
}

export async function revokeIdentitySession({ controlApiUrl, sessionId, signal } = {}) {
  const id = requireBoundedString(sessionId, "identity.session_invalid", 160);
  return identityRequest(controlApiUrl, `/identity/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
    responseType: "revoke",
    csrf: true,
    signal,
  });
}

export async function logoutIdentity({ controlApiUrl, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/logout", {
    method: "POST",
    responseType: "logout",
    csrf: true,
    signal,
  });
}

export async function rotateIdentityPassword({
  controlApiUrl,
  currentPassword,
  newPassword,
  signal,
} = {}) {
  return identityRequest(controlApiUrl, "/identity/credentials/password/rotate", {
    method: "POST",
    body: { current_password: currentPassword, new_password: newPassword },
    responseType: "rotation",
    csrf: true,
    signal,
  });
}

async function identityRequest(controlApiUrl, path, options) {
  const url = identityUrl(controlApiUrl, path);
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (options.csrf) {
    headers["X-CSRF-Token"] = readUniqueCsrfCookie();
  }

  let response;
  try {
    response = await fetch(url, {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw error;
    }
    throw new IdentityApiError({
      code: "identity.network_unavailable",
      message: "暂时无法连接账号服务，请稍后重试。",
    });
  }

  const payload = await readJson(response);
  if (!response.ok) {
    throw new IdentityApiError({
      status: Number(response.status) || 0,
      code: payload.code,
      message: payload.message || "账号请求未完成，请稍后重试。",
      requestId: payload.request_id || response.headers?.get?.("X-Request-ID"),
      traceId: payload.trace_id || response.headers?.get?.("X-Trace-ID"),
    });
  }

  return validateResponse(payload, options.responseType);
}

function identityUrl(controlApiUrl, path) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    throw new IdentityApiError({ code: "identity.configuration_invalid", message: "账号服务地址无效。" });
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    throw new IdentityApiError({ code: "identity.configuration_invalid", message: "账号服务地址无效。" });
  }
  return new URL(path, base).toString();
}

function readUniqueCsrfCookie(cookieText = globalThis.document?.cookie ?? "") {
  const matches = String(cookieText)
    .split(";")
    .map((part) => part.trim())
    .filter((part) => part.startsWith(`${CSRF_COOKIE_NAME}=`))
    .map((part) => part.slice(CSRF_COOKIE_NAME.length + 1));
  if (matches.length !== 1 || !matches[0] || matches[0].includes(",")) {
    throw new IdentityApiError({
      code: "identity.csrf_unavailable",
      message: "安全校验已失效，请重新登录。",
    });
  }
  try {
    const value = decodeURIComponent(matches[0]);
    if (!value || /[\u0000-\u0020\u007f,]/u.test(value)) {
      throw new Error("invalid");
    }
    return value;
  } catch {
    throw new IdentityApiError({
      code: "identity.csrf_unavailable",
      message: "安全校验已失效，请重新登录。",
    });
  }
}

function validateResponse(payload, responseType) {
  requirePlainObject(payload, "response");
  if (responseType === "accountSession") {
    requireExactKeys(payload, ["account", "current_session", "request_id", "trace_id"], "response");
    return {
      account: validateAccount(payload.account),
      current_session: validateSession(payload.current_session),
      request_id: requireBoundedString(payload.request_id, "identity.response_invalid", 160),
      trace_id: requireBoundedString(payload.trace_id, "identity.response_invalid", 160),
    };
  }
  if (responseType === "sessionList") {
    requireExactKeys(payload, ["limit", "offset", "request_id", "sessions", "total", "trace_id"], "response");
    if (!Array.isArray(payload.sessions) || payload.sessions.length > 100) {
      invalidResponse("sessions");
    }
    return {
      sessions: payload.sessions.map(validateSession),
      limit: requireNonnegativeInteger(payload.limit, "limit"),
      offset: requireNonnegativeInteger(payload.offset, "offset"),
      total: requireNonnegativeInteger(payload.total, "total"),
      request_id: requireBoundedString(payload.request_id, "identity.response_invalid", 160),
      trace_id: requireBoundedString(payload.trace_id, "identity.response_invalid", 160),
    };
  }
  if (responseType === "pendingChallenge") {
    requireExactKeys(
      payload,
      [
        "expires_at",
        "intent_id",
        "purpose",
        "request_id",
        "resend_available_at",
        "status",
        "trace_id",
      ],
      "response",
    );
    const purpose = requireBoundedString(payload.purpose, "identity.response_invalid", 40);
    if (!new Set(["registration", "password_reset"]).has(purpose) || payload.status !== "pending") {
      invalidResponse("challenge");
    }
    return {
      intent_id: requireBoundedString(payload.intent_id, "identity.response_invalid", 80),
      purpose,
      status: "pending",
      expires_at: requireTimestamp(payload.expires_at, "expires_at"),
      resend_available_at: requireTimestamp(
        payload.resend_available_at,
        "resend_available_at",
      ),
      request_id: requireBoundedString(payload.request_id, "identity.response_invalid", 160),
      trace_id: requireBoundedString(payload.trace_id, "identity.response_invalid", 160),
    };
  }
  if (responseType === "logout") {
    requireExactKeys(payload, ["logged_out", "request_id", "trace_id"], "response");
    requireTrue(payload.logged_out, "logged_out");
  } else if (responseType === "revoke") {
    requireExactKeys(payload, ["request_id", "revoked", "trace_id"], "response");
    requireTrue(payload.revoked, "revoked");
  } else if (responseType === "rotation") {
    requireExactKeys(payload, ["account", "current_session", "request_id", "rotated", "trace_id"], "response");
    requireTrue(payload.rotated, "rotated");
    validateAccount(payload.account);
    validateSession(payload.current_session);
  } else {
    invalidResponse("type");
  }
  return payload;
}

function validateAccount(account) {
  requirePlainObject(account, "account");
  requireExactKeys(account, ACCOUNT_KEYS, "account");
  const status = requireBoundedString(account.status, "identity.response_invalid", 40);
  if (!new Set(["active", "disabled", "locked", "migration_conflict"]).has(status)) {
    invalidResponse("account.status");
  }
  if (typeof account.mfa_configured !== "boolean") {
    invalidResponse("account.mfa_configured");
  }
  return {
    email: requireBoundedString(account.email, "identity.response_invalid", 320),
    display_name: optionalBoundedString(account.display_name, 160),
    status,
    mfa_configured: account.mfa_configured,
  };
}

function validateSession(session) {
  requirePlainObject(session, "session");
  requireExactKeys(session, SESSION_KEYS, "session");
  if (typeof session.is_current !== "boolean") {
    invalidResponse("session.is_current");
  }
  return {
    session_id: requireBoundedString(session.session_id, "identity.response_invalid", 160),
    device_family: requireBoundedString(session.device_family, "identity.response_invalid", 80),
    created_at: requireTimestamp(session.created_at, "created_at"),
    last_seen_at: requireTimestamp(session.last_seen_at, "last_seen_at"),
    expires_at: requireTimestamp(session.expires_at, "expires_at"),
    is_current: session.is_current,
  };
}

function requireExactKeys(value, allowed, label) {
  const actual = Object.keys(value).sort();
  const expected = [...allowed].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    invalidResponse(label);
  }
}

function requirePlainObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    invalidResponse(label);
  }
}

function requireTimestamp(value, label) {
  const text = requireBoundedString(value, "identity.response_invalid", 64);
  if (!Number.isFinite(Date.parse(text))) {
    invalidResponse(label);
  }
  return text;
}

function requireNonnegativeInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) {
    invalidResponse(label);
  }
  return value;
}

function requireTrue(value, label) {
  if (value !== true) {
    invalidResponse(label);
  }
}

function requireBoundedString(value, code, max) {
  const text = boundedString(value, max);
  if (!text) {
    throw new IdentityApiError({ code, message: "账号服务返回了无效数据。" });
  }
  return text;
}

function optionalBoundedString(value, max) {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  return requireBoundedString(value, "identity.response_invalid", max);
}

function boundedString(value, max) {
  if (typeof value !== "string" || value.length === 0 || value.length > max) {
    return "";
  }
  return /[\u0000-\u001f\u007f]/u.test(value) ? "" : value;
}

function invalidResponse(label) {
  throw new IdentityApiError({
    code: "identity.response_invalid",
    message: `账号服务返回了无效的${label}数据。`,
  });
}

function safeErrorText(value) {
  return boundedString(value, 240) || "账号请求未完成，请稍后重试。";
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
