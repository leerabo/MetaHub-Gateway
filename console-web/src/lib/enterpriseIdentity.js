import { IdentityApiError } from "./identity.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const RESOURCE_ID = /^(?:vdm|ssc|gac)_[0-9a-f]{32}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;

export async function getEnterpriseIdentitySummary({ controlApiUrl, workspaceId, signal } = {}) {
  return enterpriseRequest(controlApiUrl, workspaceId, "/enterprise-identity", {
    responseType: "summary",
    signal,
  });
}

export async function createEnterpriseDomain({
  controlApiUrl,
  workspaceId,
  domain,
  idempotencyKey,
  signal,
} = {}) {
  return enterpriseRequest(controlApiUrl, workspaceId, "/domains", {
    method: "POST",
    body: { domain: clientText(domain, 253, "domain") },
    idempotencyKey: clientIdempotencyKey(idempotencyKey),
    responseType: "domainCreate",
    signal,
  });
}

export async function verifyEnterpriseDomain({
  controlApiUrl,
  workspaceId,
  domainId,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  return enterpriseRequest(
    controlApiUrl,
    workspaceId,
    `/domains/${encodeURIComponent(clientResourceId(domainId, "domain_id", "vdm_"))}/verify`,
    {
      method: "POST",
      revision: clientRevision(revision),
      idempotencyKey: clientIdempotencyKey(idempotencyKey),
      responseType: "domain",
      signal,
    },
  );
}

export async function createEnterpriseSsoConnection({
  controlApiUrl,
  workspaceId,
  input,
  idempotencyKey,
  signal,
} = {}) {
  const protocol = clientChoice(input?.protocol, ["oidc", "saml"], "protocol");
  const body = {
    verified_domain_id: clientResourceId(
      input?.verifiedDomainId,
      "verified_domain_id",
      "vdm_",
    ),
    connection_code: clientText(input?.connectionCode, 64, "connection_code"),
    display_name: clientText(input?.displayName, 120, "display_name"),
    protocol,
    issuer: clientText(input?.issuer, 512, "issuer"),
  };
  if (protocol === "oidc") {
    Object.assign(body, {
      authorization_endpoint: clientText(
        input?.authorizationEndpoint,
        1024,
        "authorization_endpoint",
      ),
      token_endpoint: clientText(input?.tokenEndpoint, 1024, "token_endpoint"),
      client_id: clientText(input?.clientId, 256, "client_id"),
      client_secret: clientText(input?.clientSecret, 2048, "client_secret", 16),
      jwks: clientJwks(input?.jwks),
    });
  } else {
    Object.assign(body, {
      sso_url: clientText(input?.ssoUrl, 1024, "sso_url"),
      certificate_pem: clientText(input?.certificatePem, 32_768, "certificate_pem", 64),
    });
  }
  return enterpriseRequest(controlApiUrl, workspaceId, "/sso-connections", {
    method: "POST",
    body,
    idempotencyKey: clientIdempotencyKey(idempotencyKey),
    responseType: "connection",
    signal,
  });
}

export async function testEnterpriseSsoConnection({
  controlApiUrl,
  workspaceId,
  connectionId,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  return enterpriseRequest(controlApiUrl, workspaceId, "/sso/test", {
    method: "POST",
    body: {
      sso_connection_id: clientResourceId(connectionId, "sso_connection_id", "ssc_"),
    },
    revision: clientRevision(revision),
    idempotencyKey: clientIdempotencyKey(idempotencyKey),
    responseType: "test",
    signal,
  });
}

export async function updateEnterpriseSsoPolicy({
  controlApiUrl,
  workspaceId,
  mode,
  connectionId,
  breakGlassAccountId,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  const normalizedMode = clientChoice(mode, ["optional", "enforced"], "mode");
  const body = {
    mode: normalizedMode,
    active_connection_id: connectionId === null
      ? null
      : clientResourceId(connectionId, "active_connection_id", "ssc_"),
    break_glass_account_id: breakGlassAccountId === null
      ? null
      : clientResourceId(breakGlassAccountId, "break_glass_account_id", "gac_"),
  };
  return enterpriseRequest(controlApiUrl, workspaceId, "/sso-policy", {
    method: "PATCH",
    body,
    revision: clientRevision(revision, true),
    idempotencyKey: clientIdempotencyKey(idempotencyKey),
    responseType: "policy",
    signal,
  });
}

async function enterpriseRequest(controlApiUrl, workspaceId, path, options) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if ((options.method ?? "GET") !== "GET") {
    headers["X-CSRF-Token"] = readWorkspaceCsrfCookie();
  }
  if (options.revision !== undefined) headers["If-Match"] = `"${options.revision}"`;
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  let response;
  try {
    response = await fetch(enterpriseUrl(controlApiUrl, workspaceId, path), {
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
      code: "sso.unavailable",
      message: "企业身份服务暂时不可用，请稍后重试。",
    });
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw new IdentityApiError({
      status: Number(response.status) || 0,
      code: payload.code,
      message: payload.message || "企业身份请求未完成，请稍后重试。",
      requestId: payload.request_id || response.headers?.get?.("X-Request-ID"),
      traceId: payload.trace_id || response.headers?.get?.("X-Trace-ID"),
    });
  }
  return validateResponse(payload, options.responseType);
}

function validateResponse(payload, responseType) {
  plainObject(payload, "response");
  if (responseType === "summary") {
    exactKeys(
      payload,
      [
        "break_glass_candidates",
        "connections",
        "domains",
        "policy",
        "request_id",
        "trace_id",
        "workspace_id",
      ],
      "summary",
    );
    boundedArray(payload.domains, 20, "domains");
    boundedArray(payload.connections, 20, "connections");
    boundedArray(payload.break_glass_candidates, 20, "break_glass_candidates");
    return {
      workspace_id: responseResourceId(payload.workspace_id, "workspace_id", "wsp_"),
      domains: payload.domains.map(validateDomain),
      connections: payload.connections.map(validateConnection),
      policy: payload.policy === null ? null : validatePolicy(payload.policy),
      break_glass_candidates: payload.break_glass_candidates.map(validateCandidate),
      ...requestTrace(payload),
    };
  }
  if (responseType === "domainCreate") {
    exactKeys(
      payload,
      ["domain", "idempotent_replay", "request_id", "trace_id", "verification_record"],
      "domain_create",
    );
    return {
      domain: validateDomain(payload.domain),
      verification_record: text(payload.verification_record, 1024, "verification_record"),
      idempotent_replay: boolean(payload.idempotent_replay, "idempotent_replay"),
      ...requestTrace(payload),
    };
  }
  if (responseType === "domain") return resourceResponse(payload, "domain", validateDomain);
  if (responseType === "connection") {
    return resourceResponse(payload, "connection", validateConnection);
  }
  if (responseType === "test") {
    exactKeys(
      payload,
      ["connection", "idempotent_replay", "outcome", "request_id", "trace_id"],
      "test",
    );
    if (payload.outcome !== "passed") invalidResponse("outcome");
    return {
      connection: validateConnection(payload.connection),
      outcome: "passed",
      idempotent_replay: boolean(payload.idempotent_replay, "idempotent_replay"),
      ...requestTrace(payload),
    };
  }
  if (responseType === "policy") return resourceResponse(payload, "policy", validatePolicy);
  invalidResponse("response_type");
}

function resourceResponse(payload, name, validator) {
  exactKeys(
    payload,
    [name, "idempotent_replay", "request_id", "trace_id"],
    `${name}_response`,
  );
  return {
    [name]: validator(payload[name]),
    idempotent_replay: boolean(payload.idempotent_replay, "idempotent_replay"),
    ...requestTrace(payload),
  };
}

function validateDomain(value) {
  plainObject(value, "domain");
  exactKeys(
    value,
    [
      "challenge_expires_at",
      "domain",
      "revision",
      "status",
      "updated_at",
      "verified_at",
      "verified_domain_id",
    ],
    "domain",
  );
  return {
    verified_domain_id: responseResourceId(value.verified_domain_id, "domain_id", "vdm_"),
    domain: text(value.domain, 253, "domain"),
    status: choice(value.status, ["pending", "verified", "suspended"], "domain_status"),
    revision: positiveInteger(value.revision, "domain_revision"),
    challenge_expires_at: timestamp(value.challenge_expires_at, "challenge_expires_at"),
    verified_at: value.verified_at === null ? null : timestamp(value.verified_at, "verified_at"),
    updated_at: timestamp(value.updated_at, "updated_at"),
  };
}

function validateConnection(value) {
  plainObject(value, "connection");
  exactKeys(
    value,
    [
      "certificates",
      "connection_code",
      "display_name",
      "issuer",
      "protocol",
      "revision",
      "sso_connection_id",
      "status",
      "tested_at",
      "updated_at",
      "verified_domain_id",
    ],
    "connection",
  );
  boundedArray(value.certificates, 10, "certificates");
  return {
    sso_connection_id: responseResourceId(value.sso_connection_id, "connection_id", "ssc_"),
    verified_domain_id: responseResourceId(value.verified_domain_id, "domain_id", "vdm_"),
    connection_code: text(value.connection_code, 64, "connection_code"),
    display_name: text(value.display_name, 120, "display_name"),
    protocol: choice(value.protocol, ["oidc", "saml"], "protocol"),
    issuer: text(value.issuer, 512, "issuer"),
    status: choice(value.status, ["draft", "testing", "active", "disabled"], "status"),
    revision: positiveInteger(value.revision, "connection_revision"),
    tested_at: value.tested_at === null ? null : timestamp(value.tested_at, "tested_at"),
    updated_at: timestamp(value.updated_at, "updated_at"),
    certificates: value.certificates.map(validateCertificate),
  };
}

function validateCertificate(value) {
  plainObject(value, "certificate");
  exactKeys(
    value,
    ["fingerprint_sha256", "not_after", "not_before", "purpose", "sso_certificate_id", "status"],
    "certificate",
  );
  return {
    sso_certificate_id: text(value.sso_certificate_id, 80, "certificate_id"),
    purpose: choice(value.purpose, ["saml_signing", "oidc_jwk"], "certificate_purpose"),
    fingerprint_sha256: hexDigest(value.fingerprint_sha256, "fingerprint_sha256"),
    status: choice(value.status, ["staged", "active", "retired", "expired"], "cert_status"),
    not_before: timestamp(value.not_before, "not_before"),
    not_after: timestamp(value.not_after, "not_after"),
  };
}

function validatePolicy(value) {
  plainObject(value, "policy");
  exactKeys(
    value,
    ["active_connection_id", "break_glass_configured", "mode", "revision", "updated_at"],
    "policy",
  );
  return {
    mode: choice(value.mode, ["optional", "enforced"], "policy_mode"),
    active_connection_id: value.active_connection_id === null
      ? null
      : responseResourceId(value.active_connection_id, "active_connection_id", "ssc_"),
    break_glass_configured: boolean(value.break_glass_configured, "break_glass_configured"),
    revision: positiveInteger(value.revision, "policy_revision"),
    updated_at: timestamp(value.updated_at, "updated_at"),
  };
}

function validateCandidate(value) {
  plainObject(value, "break_glass_candidate");
  exactKeys(
    value,
    ["account_id", "display_name", "mfa_configured", "password_configured"],
    "break_glass_candidate",
  );
  return {
    account_id: responseResourceId(value.account_id, "account_id", "gac_"),
    display_name: text(value.display_name, 160, "display_name"),
    mfa_configured: boolean(value.mfa_configured, "mfa_configured"),
    password_configured: boolean(value.password_configured, "password_configured"),
  };
}

function enterpriseUrl(controlApiUrl, workspaceId, path) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidClient("control_api_url");
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidClient("control_api_url");
  }
  const id = responseResourceId(workspaceId, "workspace_id", "wsp_");
  return new URL(`/workspaces/${encodeURIComponent(id)}${path}`, base).toString();
}

async function readJson(response) {
  try {
    const payload = await response.json();
    plainObject(payload, "response");
    return payload;
  } catch (error) {
    if (error instanceof IdentityApiError) throw error;
    invalidResponse("json");
  }
}

function clientJwks(value) {
  plainObject(value, "jwks", true);
  if (Object.keys(value).length !== 1 || !Array.isArray(value.keys) || value.keys.length < 1) {
    invalidClient("jwks");
  }
  return value;
}

function clientResourceId(value, label, prefix) {
  const result = clientText(value, 80, label);
  if (!RESOURCE_ID.test(result) || !result.startsWith(prefix)) invalidClient(label);
  return result;
}

function responseResourceId(value, label, prefix) {
  const result = text(value, 80, label);
  if (!/^(?:wsp|vdm|ssc|gac)_[0-9a-f]{32}$/u.test(result) || !result.startsWith(prefix)) {
    invalidResponse(label);
  }
  return result;
}

function clientText(value, maximum, label, minimum = 1) {
  if (typeof value !== "string" || value !== value.trim()
    || value.length < minimum || value.length > maximum) invalidClient(label);
  return value;
}

function clientChoice(value, allowed, label) {
  if (!allowed.includes(value)) invalidClient(label);
  return value;
}

function clientRevision(value, allowZero = false) {
  if (!Number.isSafeInteger(value) || value < (allowZero ? 0 : 1)) invalidClient("revision");
  return value;
}

function clientIdempotencyKey(value) {
  if (typeof value !== "string" || !IDEMPOTENCY_KEY.test(value)) {
    invalidClient("idempotency_key");
  }
  return value;
}

function plainObject(value, label, client = false) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) {
    if (client) invalidClient(label);
    invalidResponse(label);
  }
}

function exactKeys(value, keys, label) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    invalidResponse(label);
  }
}

function boundedArray(value, maximum, label) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(label);
}

function text(value, maximum, label) {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    invalidResponse(label);
  }
  return value;
}

function choice(value, allowed, label) {
  if (!allowed.includes(value)) invalidResponse(label);
  return value;
}

function boolean(value, label) {
  if (typeof value !== "boolean") invalidResponse(label);
  return value;
}

function positiveInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 1) invalidResponse(label);
  return value;
}

function timestamp(value, label) {
  const result = text(value, 40, label);
  if (!Number.isFinite(Date.parse(result))) invalidResponse(label);
  return result;
}

function hexDigest(value, label) {
  const result = text(value, 64, label);
  if (!/^[0-9a-f]{64}$/u.test(result)) invalidResponse(label);
  return result;
}

function requestTrace(payload) {
  return {
    request_id: text(payload.request_id, 160, "request_id"),
    trace_id: text(payload.trace_id, 160, "trace_id"),
  };
}

function invalidClient(label) {
  throw new IdentityApiError({
    code: "sso.request_invalid",
    message: `企业身份字段无效：${label}`,
  });
}

function invalidResponse(label) {
  throw new IdentityApiError({
    code: "sso.response_invalid",
    message: `企业身份响应无效：${label}`,
  });
}
