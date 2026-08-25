const CSRF_COOKIE_NAME = "__Host-metahub-global-csrf";
const ACCOUNT_KEYS = Object.freeze(["display_name", "email", "mfa_configured", "status"]);
const SESSION_KEYS = Object.freeze([
  "assurance_level",
  "auth_method",
  "created_at",
  "device_family",
  "expires_at",
  "is_current",
  "last_seen_at",
  "revoke_generation",
  "session_id",
  "trusted_device_id",
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

export async function beginIdentityTotpEnrollment({
  controlApiUrl,
  currentPassword,
  signal,
} = {}) {
  return identityRequest(controlApiUrl, "/identity/mfa/totp/enrollments", {
    method: "POST",
    body: { current_password: currentPassword },
    responseType: "mfaEnrollment",
    csrf: true,
    signal,
  });
}

export async function verifyIdentityTotpEnrollment({
  controlApiUrl,
  factorId,
  code,
  signal,
} = {}) {
  return identityRequest(controlApiUrl, "/identity/mfa/totp/verifications", {
    method: "POST",
    body: { factor_id: factorId, code },
    responseType: "mfaVerification",
    csrf: true,
    signal,
  });
}

export async function createIdentityMfaEnrollment({
  controlApiUrl,
  method,
  currentPassword,
  signal,
} = {}) {
  const normalizedMethod = requireRequestChoice(
    method,
    ["totp", "webauthn", "recovery_codes"],
    "method",
  );
  return identityRequest(controlApiUrl, "/identity/mfa/enrollments", {
    method: "POST",
    body: {
      method: normalizedMethod,
      current_password: requireBoundedString(
        currentPassword,
        "identity.request_invalid",
        1024,
      ),
    },
    responseType: "unifiedMfaEnrollment",
    csrf: true,
    signal,
  });
}

export async function completeIdentityWebAuthnRegistration({
  controlApiUrl,
  challengeId,
  credential,
  signal,
} = {}) {
  return identityRequest(controlApiUrl, "/identity/mfa/challenges", {
    method: "POST",
    body: {
      operation: "complete_registration",
      challenge_id: requireOpaqueId(challengeId, "mfc_", "identity.request_invalid"),
      credential: requireCredentialObject(credential),
    },
    responseType: "webauthnCompletion",
    csrf: true,
    signal,
  });
}

export async function beginIdentityWebAuthnStepUp({ controlApiUrl, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/mfa/challenges", {
    method: "POST",
    body: { operation: "begin_step_up" },
    responseType: "webauthnChallenge",
    csrf: true,
    signal,
  });
}

export async function beginIdentityAccountRecovery({
  controlApiUrl,
  currentPassword,
  signal,
} = {}) {
  return identityRequest(controlApiUrl, "/identity/mfa/challenges", {
    method: "POST",
    body: {
      operation: "begin_recovery",
      current_password: requireBoundedString(
        currentPassword,
        "identity.request_invalid",
        1024,
      ),
    },
    responseType: "recoveryChallenge",
    csrf: true,
    signal,
  });
}

export async function completeIdentityAccountRecovery({
  controlApiUrl,
  challengeId,
  notificationCode,
  recoveryCode,
  signal,
} = {}) {
  const normalizedNotificationCode = requireBoundedString(
    notificationCode,
    "identity.request_invalid",
    6,
  );
  if (!/^[0-9]{6}$/u.test(normalizedNotificationCode)) {
    throw new IdentityApiError({ code: "identity.request_invalid" });
  }
  return identityRequest(controlApiUrl, "/identity/mfa/challenges", {
    method: "POST",
    body: {
      operation: "complete_recovery",
      challenge_id: requireOpaqueId(challengeId, "mfc_", "identity.request_invalid"),
      notification_code: normalizedNotificationCode,
      recovery_code: requireRecoveryCode(recoveryCode, "identity.request_invalid"),
    },
    responseType: "recoveryCompletion",
    csrf: true,
    signal,
  });
}

export async function disableIdentityMfaEnrollment({
  controlApiUrl,
  factorId,
  currentPassword,
  signal,
} = {}) {
  return identityRequest(
    controlApiUrl,
    `/identity/mfa/enrollments/${encodeURIComponent(
      requireOpaqueId(factorId, "mfa_", "identity.request_invalid"),
    )}`,
    {
      method: "DELETE",
      body: {
        current_password: requireBoundedString(
          currentPassword,
          "identity.request_invalid",
          1024,
        ),
      },
      responseType: "revoke",
      csrf: true,
      signal,
    },
  );
}

export async function getIdentitySecurity({ controlApiUrl, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/security", {
    responseType: "identitySecurity",
    signal,
  });
}

export async function trustIdentityDevice({
  controlApiUrl,
  label,
  totpCode,
  signal,
} = {}) {
  return identityRequest(controlApiUrl, "/identity/trusted-devices", {
    method: "POST",
    body: {
      label: requireBoundedString(label, "identity.request_invalid", 80),
      totp_code: requireTotpCode(totpCode),
    },
    responseType: "trustedDevice",
    csrf: true,
    signal,
  });
}

export async function revokeIdentityTrustedDevice({
  controlApiUrl,
  trustedDeviceId,
  signal,
} = {}) {
  return identityRequest(
    controlApiUrl,
    `/identity/trusted-devices/${encodeURIComponent(
      requireOpaqueId(trustedDeviceId, "trd_", "identity.request_invalid"),
    )}`,
    {
      method: "DELETE",
      responseType: "revoke",
      csrf: true,
      signal,
    },
  );
}

export async function createIdentityStepUpGrant({
  controlApiUrl,
  method,
  workspaceId,
  actionCode,
  resourceType,
  resourceId,
  code,
  challengeId,
  credential,
  signal,
} = {}) {
  const normalizedMethod = requireRequestChoice(
    method,
    ["totp", "webauthn", "recovery_code"],
    "method",
  );
  const body = {
    method: normalizedMethod,
    action_code: requireBoundedString(actionCode, "identity.request_invalid", 80),
    resource_type: requireBoundedString(resourceType, "identity.request_invalid", 60),
    resource_id: requireBoundedString(resourceId, "identity.request_invalid", 120),
  };
  if (workspaceId !== undefined && workspaceId !== null) {
    body.workspace_id = requireOpaqueId(workspaceId, "wsp_", "identity.request_invalid");
  }
  if (normalizedMethod === "webauthn") {
    body.challenge_id = requireOpaqueId(challengeId, "mfc_", "identity.request_invalid");
    body.credential = requireCredentialObject(credential);
  } else {
    body.code = normalizedMethod === "totp"
      ? requireTotpCode(code)
      : requireRecoveryCode(code);
  }
  return identityRequest(controlApiUrl, "/identity/step-up", {
    method: "POST",
    body,
    responseType: "stepUpGrant",
    csrf: true,
    signal,
  });
}

export async function createWebAuthnCredential(
  publicKeyOptions,
  credentialsApi = globalThis.navigator?.credentials,
) {
  if (!credentialsApi?.create) {
    throw new IdentityApiError({
      code: "identity.webauthn_unavailable",
      message: "当前浏览器无法使用安全密钥。",
    });
  }
  const publicKey = decodeCreationOptions(publicKeyOptions);
  const credential = await credentialsApi.create({ publicKey });
  return serializeWebAuthnCredential(credential, "registration");
}

export async function getWebAuthnAssertion(
  publicKeyOptions,
  credentialsApi = globalThis.navigator?.credentials,
) {
  if (!credentialsApi?.get) {
    throw new IdentityApiError({
      code: "identity.webauthn_unavailable",
      message: "当前浏览器无法使用安全密钥。",
    });
  }
  const publicKey = decodeRequestOptions(publicKeyOptions);
  const credential = await credentialsApi.get({ publicKey });
  return serializeWebAuthnCredential(credential, "authentication");
}

export async function getIdentityMe({ controlApiUrl, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/me", {
    responseType: "accountSession",
    signal,
  });
}

export async function getIdentityPlatformAccess({ controlApiUrl, signal } = {}) {
  return identityRequest(controlApiUrl, "/identity/platform-access", {
    responseType: "platformAccess",
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
    headers["X-CSRF-Token"] = readIdentityCsrfCookie();
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

export function readIdentityCsrfCookie(cookieText = globalThis.document?.cookie ?? "") {
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
  if (responseType === "mfaEnrollment") {
    requireExactKeys(
      payload,
      ["expires_at", "factor_id", "provisioning_uri", "request_id", "secret", "trace_id"],
      "response",
    );
    const factorId = requireBoundedString(payload.factor_id, "identity.response_invalid", 36);
    const secret = requireBoundedString(payload.secret, "identity.response_invalid", 128);
    const provisioningUri = requireBoundedString(
      payload.provisioning_uri,
      "identity.response_invalid",
      1024,
    );
    if (!/^mfa_[0-9a-f]{32}$/u.test(factorId) || !/^[A-Z2-7]{16,128}$/u.test(secret)) {
      invalidResponse("mfa_enrollment");
    }
    let uri;
    try {
      uri = new URL(provisioningUri);
    } catch {
      invalidResponse("mfa_enrollment.provisioning_uri");
    }
    if (uri.protocol !== "otpauth:" || uri.hostname !== "totp") {
      invalidResponse("mfa_enrollment.provisioning_uri");
    }
    return {
      factor_id: factorId,
      secret,
      provisioning_uri: provisioningUri,
      expires_at: requireTimestamp(payload.expires_at, "expires_at"),
      request_id: requireBoundedString(payload.request_id, "identity.response_invalid", 160),
      trace_id: requireBoundedString(payload.trace_id, "identity.response_invalid", 160),
    };
  }
  if (responseType === "mfaVerification") {
    requireExactKeys(
      payload,
      ["factor_id", "request_id", "revision", "status", "trace_id", "verified_at"],
      "response",
    );
    const factorId = requireBoundedString(payload.factor_id, "identity.response_invalid", 36);
    if (
      !/^mfa_[0-9a-f]{32}$/u.test(factorId)
      || payload.status !== "active"
      || !Number.isSafeInteger(payload.revision)
      || payload.revision < 2
    ) {
      invalidResponse("mfa_verification");
    }
    return {
      factor_id: factorId,
      status: "active",
      revision: payload.revision,
      verified_at: requireTimestamp(payload.verified_at, "verified_at"),
      request_id: requireBoundedString(payload.request_id, "identity.response_invalid", 160),
      trace_id: requireBoundedString(payload.trace_id, "identity.response_invalid", 160),
    };
  }
  if (responseType === "unifiedMfaEnrollment") {
    requireExactKeys(
      payload,
      [
        "challenge_id",
        "expires_at",
        "factor_id",
        "method",
        "provisioning_uri",
        "public_key_options",
        "recovery_codes",
        "request_id",
        "secret",
        "trace_id",
      ],
      "response",
    );
    const method = requireChoice(
      payload.method,
      ["totp", "webauthn", "recovery_codes"],
      "method",
    );
    const base = {
      method,
      factor_id: null,
      challenge_id: null,
      public_key_options: null,
      secret: null,
      provisioning_uri: null,
      recovery_codes: null,
      expires_at: null,
      ...validateRequestTrace(payload),
    };
    if (method === "totp") {
      const factorId = requireOpaqueId(payload.factor_id, "mfa_");
      const secret = requireBoundedString(payload.secret, "identity.response_invalid", 128);
      const provisioningUri = requireBoundedString(
        payload.provisioning_uri,
        "identity.response_invalid",
        1024,
      );
      if (!/^[A-Z2-7]{16,128}$/u.test(secret)) invalidResponse("mfa_enrollment.secret");
      let uri;
      try {
        uri = new URL(provisioningUri);
      } catch {
        invalidResponse("mfa_enrollment.provisioning_uri");
      }
      if (uri.protocol !== "otpauth:" || uri.hostname !== "totp") {
        invalidResponse("mfa_enrollment.provisioning_uri");
      }
      return {
        ...base,
        factor_id: factorId,
        secret,
        provisioning_uri: provisioningUri,
        expires_at: requireTimestamp(payload.expires_at, "expires_at"),
      };
    }
    if (method === "webauthn") {
      return {
        ...base,
        factor_id: requireOpaqueId(payload.factor_id, "mfa_"),
        challenge_id: requireOpaqueId(payload.challenge_id, "mfc_"),
        public_key_options: requirePublicKeyOptions(payload.public_key_options),
        expires_at: requireTimestamp(payload.expires_at, "expires_at"),
      };
    }
    if (
      payload.factor_id !== null
      || payload.challenge_id !== null
      || payload.public_key_options !== null
      || payload.secret !== null
      || payload.provisioning_uri !== null
      || payload.expires_at !== null
      || !Array.isArray(payload.recovery_codes)
      || payload.recovery_codes.length !== 10
    ) {
      invalidResponse("recovery_codes");
    }
    return {
      ...base,
      recovery_codes: payload.recovery_codes.map(
        (code) => requireRecoveryCode(code, "identity.response_invalid"),
      ),
    };
  }
  if (responseType === "webauthnChallenge") {
    requireExactKeys(
      payload,
      [
        "challenge_id",
        "expires_at",
        "method",
        "public_key_options",
        "purpose",
        "request_id",
        "trace_id",
      ],
      "response",
    );
    if (payload.method !== "webauthn" || payload.purpose !== "step_up") {
      invalidResponse("webauthn_challenge");
    }
    return {
      challenge_id: requireOpaqueId(payload.challenge_id, "mfc_"),
      purpose: "step_up",
      method: "webauthn",
      public_key_options: requirePublicKeyOptions(payload.public_key_options),
      expires_at: requireTimestamp(payload.expires_at, "expires_at"),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "recoveryChallenge") {
    requireExactKeys(
      payload,
      [
        "challenge_id",
        "expires_at",
        "purpose",
        "ready_at",
        "request_id",
        "status",
        "trace_id",
      ],
      "response",
    );
    if (payload.purpose !== "recovery" || payload.status !== "pending") {
      invalidResponse("recovery_challenge");
    }
    return {
      challenge_id: requireOpaqueId(payload.challenge_id, "mfc_"),
      status: "pending",
      ready_at: requireTimestamp(payload.ready_at, "ready_at"),
      expires_at: requireTimestamp(payload.expires_at, "expires_at"),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "recoveryCompletion") {
    requireExactKeys(
      payload,
      [
        "assurance_level",
        "completed_at",
        "recovered",
        "request_id",
        "trace_id",
      ],
      "response",
    );
    if (payload.recovered !== true || payload.assurance_level !== "recovery") {
      invalidResponse("recovery_completion");
    }
    return {
      recovered: true,
      assurance_level: "recovery",
      completed_at: requireTimestamp(payload.completed_at, "completed_at"),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "webauthnCompletion") {
    requireExactKeys(
      payload,
      [
        "factor_id",
        "factor_type",
        "request_id",
        "revision",
        "status",
        "trace_id",
        "verified_at",
      ],
      "response",
    );
    if (
      payload.factor_type !== "webauthn"
      || payload.status !== "active"
      || !Number.isSafeInteger(payload.revision)
      || payload.revision < 2
    ) {
      invalidResponse("webauthn_completion");
    }
    return {
      factor_id: requireOpaqueId(payload.factor_id, "mfa_"),
      factor_type: "webauthn",
      status: "active",
      revision: payload.revision,
      verified_at: requireTimestamp(payload.verified_at, "verified_at"),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "identitySecurity") {
    requireExactKeys(
      payload,
      [
        "factors",
        "recovery_challenge",
        "recovery_code_set",
        "request_id",
        "sessions",
        "trace_id",
        "trusted_devices",
      ],
      "response",
    );
    if (
      !Array.isArray(payload.factors)
      || payload.factors.length > 20
      || !Array.isArray(payload.trusted_devices)
      || payload.trusted_devices.length > 50
      || !Array.isArray(payload.sessions)
      || payload.sessions.length > 100
    ) {
      invalidResponse("identity_security");
    }
    return {
      factors: payload.factors.map(validateMfaFactor),
      recovery_challenge: payload.recovery_challenge === null
        ? null
        : validateRecoveryChallenge(payload.recovery_challenge),
      recovery_code_set: payload.recovery_code_set === null
        ? null
        : validateRecoveryCodeSet(payload.recovery_code_set),
      trusted_devices: payload.trusted_devices.map(validateTrustedDevice),
      sessions: payload.sessions.map(validateSession),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "trustedDevice") {
    requireExactKeys(
      payload,
      ["request_id", "trace_id", "trusted_device"],
      "response",
    );
    return {
      trusted_device: validateTrustedDevice(payload.trusted_device),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "stepUpGrant") {
    requireExactKeys(
      payload,
      [
        "action_code",
        "assurance_level",
        "auth_method",
        "expires_at",
        "grant",
        "grant_id",
        "request_id",
        "resource_id",
        "resource_type",
        "trace_id",
      ],
      "response",
    );
    const grant = requireBoundedString(payload.grant, "identity.response_invalid", 128);
    if (!/^[A-Za-z0-9_-]{32,128}$/u.test(grant)) invalidResponse("step_up.grant");
    return {
      grant,
      grant_id: requireOpaqueId(payload.grant_id, "sug_"),
      action_code: requireBoundedString(
        payload.action_code,
        "identity.response_invalid",
        80,
      ),
      resource_type: requireBoundedString(
        payload.resource_type,
        "identity.response_invalid",
        60,
      ),
      resource_id: requireBoundedString(
        payload.resource_id,
        "identity.response_invalid",
        120,
      ),
      auth_method: requireChoice(
        payload.auth_method,
        ["totp", "webauthn", "recovery_code"],
        "auth_method",
      ),
      assurance_level: requireChoice(
        payload.assurance_level,
        ["mfa", "recovery"],
        "assurance_level",
      ),
      expires_at: requireTimestamp(payload.expires_at, "expires_at"),
      ...validateRequestTrace(payload),
    };
  }
  if (responseType === "platformAccess") {
    requireExactKeys(payload, ["available", "request_id", "trace_id"], "response");
    if (typeof payload.available !== "boolean") invalidResponse("platform_access.available");
    return {
      available: payload.available,
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
    assurance_level: requireChoice(
      session.assurance_level,
      ["password", "mfa", "recovery"],
      "session.assurance_level",
    ),
    auth_method: requireChoice(
      session.auth_method,
      ["password", "totp", "webauthn", "recovery_code"],
      "session.auth_method",
    ),
    trusted_device_id: session.trusted_device_id === null
      ? null
      : requireOpaqueId(session.trusted_device_id, "trd_"),
    revoke_generation: requirePositiveInteger(
      session.revoke_generation,
      "session.revoke_generation",
    ),
  };
}

function validateMfaFactor(value) {
  requirePlainObject(value, "factor");
  requireExactKeys(
    value,
    ["created_at", "factor_id", "factor_type", "revision", "status", "verified_at"],
    "factor",
  );
  return {
    factor_id: requireOpaqueId(value.factor_id, "mfa_"),
    factor_type: requireChoice(value.factor_type, ["totp", "webauthn"], "factor_type"),
    status: requireChoice(value.status, ["pending", "active", "disabled"], "factor.status"),
    revision: requirePositiveInteger(value.revision, "factor.revision"),
    verified_at: value.verified_at === null
      ? null
      : requireTimestamp(value.verified_at, "factor.verified_at"),
    created_at: requireTimestamp(value.created_at, "factor.created_at"),
  };
}

function validateRecoveryCodeSet(value) {
  requirePlainObject(value, "recovery_code_set");
  requireExactKeys(
    value,
    ["generated_at", "recovery_code_set_id", "remaining_count", "revision", "status"],
    "recovery_code_set",
  );
  return {
    recovery_code_set_id: requireOpaqueId(value.recovery_code_set_id, "rcs_"),
    status: requireChoice(
      value.status,
      ["active", "superseded", "exhausted", "revoked"],
      "recovery_code_set.status",
    ),
    revision: requirePositiveInteger(value.revision, "recovery_code_set.revision"),
    remaining_count: requireNonnegativeInteger(
      value.remaining_count,
      "recovery_code_set.remaining_count",
    ),
    generated_at: requireTimestamp(value.generated_at, "recovery_code_set.generated_at"),
  };
}

function validateRecoveryChallenge(value) {
  requirePlainObject(value, "recovery_challenge");
  requireExactKeys(
    value,
    ["challenge_id", "expires_at", "ready_at", "status"],
    "recovery_challenge",
  );
  if (value.status !== "pending") invalidResponse("recovery_challenge.status");
  return {
    challenge_id: requireOpaqueId(value.challenge_id, "mfc_"),
    status: "pending",
    ready_at: requireTimestamp(value.ready_at, "recovery_challenge.ready_at"),
    expires_at: requireTimestamp(value.expires_at, "recovery_challenge.expires_at"),
  };
}

function validateTrustedDevice(value) {
  requirePlainObject(value, "trusted_device");
  requireExactKeys(
    value,
    [
      "created_at",
      "device_family",
      "expires_at",
      "label",
      "last_used_at",
      "revision",
      "status",
      "trust_level",
      "trusted_device_id",
    ],
    "trusted_device",
  );
  return {
    trusted_device_id: requireOpaqueId(value.trusted_device_id, "trd_"),
    label: requireBoundedString(value.label, "identity.response_invalid", 80),
    device_family: requireChoice(
      value.device_family,
      ["chrome", "edge", "firefox", "safari", "mobile", "unknown"],
      "trusted_device.device_family",
    ),
    trust_level: requireChoice(
      value.trust_level,
      ["mfa_verified"],
      "trusted_device.trust_level",
    ),
    status: requireChoice(
      value.status,
      ["active", "revoked", "expired"],
      "trusted_device.status",
    ),
    revision: requirePositiveInteger(value.revision, "trusted_device.revision"),
    expires_at: requireTimestamp(value.expires_at, "trusted_device.expires_at"),
    last_used_at: value.last_used_at === null
      ? null
      : requireTimestamp(value.last_used_at, "trusted_device.last_used_at"),
    created_at: requireTimestamp(value.created_at, "trusted_device.created_at"),
  };
}

function validateRequestTrace(payload) {
  return {
    request_id: requireBoundedString(payload.request_id, "identity.response_invalid", 160),
    trace_id: requireBoundedString(payload.trace_id, "identity.response_invalid", 160),
  };
}

function requirePublicKeyOptions(value) {
  requirePlainObject(value, "public_key_options");
  let serialized;
  try {
    serialized = JSON.stringify(value);
  } catch {
    invalidResponse("public_key_options");
  }
  if (!serialized || serialized.length > 32_768 || !boundedJsonValue(value, 0)) {
    invalidResponse("public_key_options");
  }
  return structuredCloneJson(value);
}

function boundedJsonValue(value, depth) {
  if (depth > 8) return false;
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value === "string") return value.length <= 4096 && !/[\u0000-\u001f\u007f]/u.test(value);
  if (Array.isArray(value)) {
    return value.length <= 64 && value.every((item) => boundedJsonValue(item, depth + 1));
  }
  if (!value || typeof value !== "object") return false;
  const entries = Object.entries(value);
  return entries.length <= 64
    && entries.every(([key, item]) => /^[A-Za-z][A-Za-z0-9_-]{0,63}$/u.test(key)
      && boundedJsonValue(item, depth + 1));
}

function structuredCloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function decodeCreationOptions(value) {
  const options = requirePublicKeyOptions(value);
  if (!options.user || typeof options.user !== "object") invalidResponse("webauthn.user");
  return {
    ...options,
    challenge: decodeBase64Url(options.challenge, "webauthn.challenge"),
    user: {
      ...options.user,
      id: decodeBase64Url(options.user.id, "webauthn.user.id"),
    },
    excludeCredentials: decodeCredentialDescriptors(options.excludeCredentials),
  };
}

function decodeRequestOptions(value) {
  const options = requirePublicKeyOptions(value);
  return {
    ...options,
    challenge: decodeBase64Url(options.challenge, "webauthn.challenge"),
    allowCredentials: decodeCredentialDescriptors(options.allowCredentials),
  };
}

function decodeCredentialDescriptors(value) {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.length > 64) invalidResponse("webauthn.credentials");
  return value.map((descriptor) => {
    if (!descriptor || typeof descriptor !== "object" || Array.isArray(descriptor)) {
      invalidResponse("webauthn.credential");
    }
    return {
      ...descriptor,
      id: decodeBase64Url(descriptor.id, "webauthn.credential.id"),
    };
  });
}

function decodeBase64Url(value, label) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{1,4096}$/u.test(value)) {
    invalidResponse(label);
  }
  try {
    const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
    const binary = globalThis.atob(padded);
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    invalidResponse(label);
  }
}

function serializeWebAuthnCredential(credential, purpose) {
  if (!credential || typeof credential !== "object" || !credential.response) {
    throw new IdentityApiError({
      code: "identity.webauthn_cancelled",
      message: "安全密钥验证未完成。",
    });
  }
  const response = purpose === "registration"
    ? {
        clientDataJSON: encodeBuffer(credential.response.clientDataJSON),
        attestationObject: encodeBuffer(credential.response.attestationObject),
        transports: typeof credential.response.getTransports === "function"
          ? credential.response.getTransports()
          : [],
      }
    : {
        authenticatorData: encodeBuffer(credential.response.authenticatorData),
        clientDataJSON: encodeBuffer(credential.response.clientDataJSON),
        signature: encodeBuffer(credential.response.signature),
        userHandle: credential.response.userHandle === null
          ? null
          : encodeBuffer(credential.response.userHandle),
      };
  return requireCredentialObject({
    id: requireBoundedString(credential.id, "identity.request_invalid", 4096),
    rawId: encodeBuffer(credential.rawId),
    response,
    type: "public-key",
    authenticatorAttachment: credential.authenticatorAttachment ?? null,
    clientExtensionResults: typeof credential.getClientExtensionResults === "function"
      ? credential.getClientExtensionResults()
      : {},
  });
}

function encodeBuffer(value) {
  if (!(value instanceof ArrayBuffer) && !ArrayBuffer.isView(value)) {
    throw new IdentityApiError({ code: "identity.webauthn_invalid" });
  }
  const bytes = value instanceof ArrayBuffer
    ? new Uint8Array(value)
    : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return globalThis.btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function requireCredentialObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || !boundedJsonValue(value, 0)) {
    throw new IdentityApiError({ code: "identity.request_invalid" });
  }
  const serialized = JSON.stringify(value);
  if (serialized.length > 65_536) {
    throw new IdentityApiError({ code: "identity.request_invalid" });
  }
  return structuredCloneJson(value);
}

function requireOpaqueId(value, prefix, code = "identity.response_invalid") {
  const text = requireBoundedString(value, code, 36);
  if (text.length !== 36 || !text.startsWith(prefix) || !/^[a-z]{3}_[0-9a-f]{32}$/u.test(text)) {
    throw new IdentityApiError({ code });
  }
  return text;
}

function requireRequestChoice(value, choices) {
  if (!choices.includes(value)) {
    throw new IdentityApiError({ code: "identity.request_invalid" });
  }
  return value;
}

function requireChoice(value, choices, label) {
  if (!choices.includes(value)) invalidResponse(label);
  return value;
}

function requirePositiveInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 1) invalidResponse(label);
  return value;
}

function requireTotpCode(value) {
  const code = requireBoundedString(value, "identity.request_invalid", 6);
  if (!/^[0-9]{6}$/u.test(code)) throw new IdentityApiError({ code: "identity.request_invalid" });
  return code;
}

function requireRecoveryCode(value, errorCode = "identity.request_invalid") {
  const code = requireBoundedString(value, errorCode, 20);
  if (!/^MHRC-[23456789A-HJ-NP-Z]{5}-[23456789A-HJ-NP-Z]{5}$/u.test(code)) {
    throw new IdentityApiError({ code: errorCode });
  }
  return code;
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
