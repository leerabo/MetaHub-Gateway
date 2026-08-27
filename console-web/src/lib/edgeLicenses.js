/** PRD-128 strict Workspace and Platform private-Gateway license clients. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const IDS = Object.freeze({
  deployment: /^edp_[0-9a-f]{32}$/u,
  license: /^lic_[0-9a-f]{32}$/u,
  entitlement: /^let_[0-9a-f]{32}$/u,
  workspace: /^wsp_[0-9a-f]{32}$/u,
  acknowledgement: /^lak_[0-9a-f]{32}$/u,
  order: /^ord_[0-9a-f]{32}$/u,
});
const TENANT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const SAFE_CODE = /^[a-z][a-z0-9_.-]{1,79}$/u;
const SAFE_KEY_ID = /^[a-z0-9][a-z0-9._:-]{2,79}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const CHAIN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const PLATFORM_CSRF_COOKIE = "__Host-metahub-platform-csrf";
const FEATURES = Object.freeze([
  "advanced_routing", "audit_export", "diagnostic_support",
  "policy_enforcement", "telemetry_export",
]);

export async function getWorkspaceLicense({ controlApiUrl, deploymentId, signal } = {}) {
  const id = clientId(deploymentId, IDS.deployment, "deployment_id", "workspace");
  const result = await request(
    controlApiUrl,
    `/edge/licenses/${encodeURIComponent(id)}`,
    { signal },
    "workspace",
  );
  const response = licenseResponse(result.payload, "workspace");
  if (response.license.deployment_id !== id) invalidResponse("workspace", "license_owner");
  validateEtag(result.response, response.license.revision, "workspace");
  return response;
}

export async function refreshWorkspaceLicense({
  controlApiUrl,
  licenseId,
  expectedRevision,
  reasonCode = "online_refresh",
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(licenseId, IDS.license, "license_id", "workspace");
  const result = await request(
    controlApiUrl,
    `/edge/licenses/${encodeURIComponent(id)}/refresh`,
    {
      method: "POST",
      csrf: true,
      idempotencyKey: clientIdempotency(idempotencyKey, "workspace"),
      body: {
        expected_revision: clientInteger(
          expectedRevision, 1, Number.MAX_SAFE_INTEGER, "expected_revision", "workspace",
        ),
        reason_code: clientReason(reasonCode, "workspace"),
      },
      signal,
    },
    "workspace",
  );
  const response = licenseResponse(result.payload, "workspace");
  if (response.license.license_id !== id) invalidResponse("workspace", "license_owner");
  validateEtag(result.response, response.license.revision, "workspace");
  return response;
}

export async function listPlatformLicenses({ controlApiUrl, signal } = {}) {
  const result = await request(controlApiUrl, "/platform/edge/licenses", { signal }, "platform");
  exact(result.payload, ["licenses", "request_id", "trace_id"], "license_list", "platform");
  responseArray(result.payload.licenses, 200, "licenses", "platform");
  const licenses = result.payload.licenses.map((item) => licenseSummary(item, "platform"));
  unique(licenses.map((item) => item.license_id), "licenses", "platform");
  unique(licenses.map((item) => item.deployment_id), "deployments", "platform");
  return Object.freeze({ licenses: Object.freeze(licenses), ...trace(result.payload, "platform") });
}

export async function issuePlatformLicense({
  controlApiUrl,
  deploymentId,
  nodeLimit,
  concurrentRequestLimit,
  features = [],
  expiresAt,
  graceDays = 7,
  sourceOrderId = null,
  expectedRevision = null,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const deployment = clientId(deploymentId, IDS.deployment, "deployment_id", "platform");
  const result = await request(
    controlApiUrl,
    "/edge/licenses",
    {
      method: "POST",
      csrf: true,
      idempotencyKey: clientIdempotency(idempotencyKey, "platform"),
      body: {
        deployment_id: deployment,
        node_limit: clientInteger(nodeLimit, 1, 100_000, "node_limit", "platform"),
        concurrent_request_limit: clientInteger(
          concurrentRequestLimit, 1, 10_000_000, "concurrent_request_limit", "platform",
        ),
        features: clientFeatures(features, "platform"),
        expires_at: clientTimestamp(expiresAt, "expires_at", "platform"),
        grace_days: clientInteger(graceDays, 7, 30, "grace_days", "platform"),
        source_order_id: sourceOrderId === null || sourceOrderId === ""
          ? null : clientId(sourceOrderId, IDS.order, "source_order_id", "platform"),
        expected_revision: expectedRevision === null || expectedRevision === ""
          ? null : clientInteger(
            expectedRevision, 1, Number.MAX_SAFE_INTEGER, "expected_revision", "platform",
          ),
        reason_code: clientReason(reasonCode, "platform"),
      },
      signal,
    },
    "platform",
  );
  const response = licenseResponse(result.payload, "platform");
  if (response.license.deployment_id !== deployment) invalidResponse("platform", "license_owner");
  validateEtag(result.response, response.license.revision, "platform");
  return response;
}

export async function revokePlatformLicense({
  controlApiUrl,
  licenseId,
  expectedRevision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(licenseId, IDS.license, "license_id", "platform");
  const result = await request(
    controlApiUrl,
    `/edge/licenses/${encodeURIComponent(id)}/revoke`,
    {
      method: "POST",
      csrf: true,
      idempotencyKey: clientIdempotency(idempotencyKey, "platform"),
      body: {
        expected_revision: clientInteger(
          expectedRevision, 1, Number.MAX_SAFE_INTEGER, "expected_revision", "platform",
        ),
        reason_code: clientReason(reasonCode, "platform"),
      },
      signal,
    },
    "platform",
  );
  const response = licenseResponse(result.payload, "platform");
  if (response.license.license_id !== id || response.license.status !== "revoked") {
    invalidResponse("platform", "license_revoke");
  }
  validateEtag(result.response, response.license.revision, "platform");
  return response;
}

function licenseResponse(value, domain) {
  exact(value, ["license", "idempotent_replay", "request_id", "trace_id"],
    "license_response", domain);
  responseBoolean(value.idempotent_replay, "idempotent_replay", domain);
  return Object.freeze({
    license: licenseSummary(value.license, domain),
    idempotent_replay: value.idempotent_replay,
    ...trace(value, domain),
  });
}

function licenseSummary(value, domain) {
  exact(value, [
    "license_id", "workspace_id", "tenant_id", "deployment_id", "status",
    "current_entitlement_id", "capacity", "generation", "refresh_counter",
    "revocation_generation", "manifest_sha256", "signing_key_id", "issued_at",
    "not_before", "expires_at", "grace_ends_at", "last_acknowledgement",
    "latest_usage", "revision", "updated_at", "revoked_reason_code", "revoked_at",
  ], "license", domain);
  const licenseId = responseId(value.license_id, IDS.license, "license_id", domain);
  const status = responseChoice(value.status, ["active", "revoked"], "status", domain);
  const revokedReason = nullableReason(value.revoked_reason_code, "revoked_reason_code", domain);
  const revokedAt = nullableTimestamp(value.revoked_at, "revoked_at", domain);
  if ((status === "revoked") !== Boolean(revokedReason && revokedAt)) {
    invalidResponse(domain, "revocation_state");
  }
  return Object.freeze({
    license_id: licenseId,
    workspace_id: responseId(value.workspace_id, IDS.workspace, "workspace_id", domain),
    tenant_id: responsePattern(value.tenant_id, TENANT_ID, "tenant_id", domain),
    deployment_id: responseId(value.deployment_id, IDS.deployment, "deployment_id", domain),
    status,
    current_entitlement_id: responseId(
      value.current_entitlement_id, IDS.entitlement, "entitlement_id", domain,
    ),
    capacity: licenseCapacity(value.capacity, domain),
    generation: responseInteger(value.generation, 1, 1_000_000_000, "generation", domain),
    refresh_counter: responseInteger(
      value.refresh_counter, 1, 1_000_000_000, "refresh_counter", domain,
    ),
    revocation_generation: responseInteger(
      value.revocation_generation, 0, 1_000_000_000, "revocation_generation", domain,
    ),
    manifest_sha256: responsePattern(value.manifest_sha256, SHA256, "manifest_sha256", domain),
    signing_key_id: responsePattern(value.signing_key_id, SAFE_KEY_ID, "signing_key_id", domain),
    issued_at: timestamp(value.issued_at, "issued_at", domain),
    not_before: timestamp(value.not_before, "not_before", domain),
    expires_at: timestamp(value.expires_at, "expires_at", domain),
    grace_ends_at: timestamp(value.grace_ends_at, "grace_ends_at", domain),
    last_acknowledgement: value.last_acknowledgement === null
      ? null : acknowledgement(value.last_acknowledgement, domain),
    latest_usage: value.latest_usage === null ? null : usage(value.latest_usage, domain),
    revision: responseInteger(value.revision, 1, 1_000_000_000, "revision", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
    revoked_reason_code: revokedReason,
    revoked_at: revokedAt,
  });
}

function licenseCapacity(value, domain) {
  exact(value, ["node_limit", "concurrent_request_limit", "features"], "capacity", domain);
  const features = responseFeatures(value.features, domain);
  return Object.freeze({
    node_limit: responseInteger(value.node_limit, 1, 100_000, "node_limit", domain),
    concurrent_request_limit: responseInteger(
      value.concurrent_request_limit, 1, 10_000_000, "concurrent_request_limit", domain,
    ),
    features,
  });
}

function acknowledgement(value, domain) {
  exact(value, [
    "acknowledgement_id", "generation", "disposition", "decision_status",
    "reason_code", "acknowledged_at",
  ], "acknowledgement", domain);
  return Object.freeze({
    acknowledgement_id: responseId(
      value.acknowledgement_id, IDS.acknowledgement, "acknowledgement_id", domain,
    ),
    generation: responseInteger(value.generation, 1, 1_000_000_000, "generation", domain),
    disposition: responseChoice(value.disposition, ["applied", "rejected"], "disposition", domain),
    decision_status: responseChoice(value.decision_status, [
      "current", "grace", "restricted", "revoked", "not_yet_valid", "clock_rollback",
    ], "decision_status", domain),
    reason_code: responsePattern(value.reason_code, SAFE_CODE, "reason_code", domain),
    acknowledged_at: timestamp(value.acknowledged_at, "acknowledged_at", domain),
  });
}

function usage(value, domain) {
  exact(value, [
    "active_node_units", "active_request_units", "denied_count",
    "bucket_started_at", "bucket_ended_at",
  ], "usage", domain);
  const started = timestamp(value.bucket_started_at, "bucket_started_at", domain);
  const ended = timestamp(value.bucket_ended_at, "bucket_ended_at", domain);
  if (Date.parse(ended) <= Date.parse(started)) invalidResponse(domain, "usage_bucket");
  return Object.freeze({
    active_node_units: responseInteger(
      value.active_node_units, 0, 100_000, "active_node_units", domain,
    ),
    active_request_units: responseInteger(
      value.active_request_units, 0, 10_000_000, "active_request_units", domain,
    ),
    denied_count: responseInteger(value.denied_count, 0, 1_000_000_000, "denied_count", domain),
    bucket_started_at: started,
    bucket_ended_at: ended,
  });
}

async function request(controlApiUrl, path, options, domain) {
  const headers = { Accept: "application/json" };
  if (options.csrf) {
    headers["X-CSRF-Token"] = domain === "platform"
      ? readCookie(PLATFORM_CSRF_COOKIE) : readWorkspaceCsrfCookie();
  }
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  if (options.body) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, domain), {
      method: options.method ?? "GET",
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw apiError(domain, 0, "edge_license.network_unavailable", "License service unavailable");
  }
  const payload = await readJson(response, domain);
  if (!response.ok) {
    const code = SAFE_CODE.test(String(payload?.code ?? ""))
      ? payload.code : "edge_license.request_failed";
    throw apiError(domain, response.status, code, safeText(payload?.message), payload?.request_id);
  }
  const directives = String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim());
  const requestId = response.headers.get("X-Request-ID") ?? "";
  const traceId = response.headers.get("X-Trace-ID") ?? "";
  if (!directives.includes("no-store") || !directives.includes("private")
    || !CHAIN.test(requestId) || !CHAIN.test(traceId)
    || payload.request_id !== requestId || payload.trace_id !== traceId) {
    invalidResponse(domain, "boundary");
  }
  return { payload, response };
}

function apiUrl(controlApiUrl, path, domain) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(domain, "control_api_url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest(domain, "control_api_url");
  }
  return new URL(path, base).toString();
}

async function readJson(response, domain) {
  try {
    const payload = await response.json();
    plain(payload, "response", domain);
    return payload;
  } catch (error) {
    if (error instanceof AuthApiError || error instanceof PlatformApiError) throw error;
    invalidResponse(domain, "json");
  }
}

function readCookie(name) {
  const prefix = `${name}=`;
  const matches = String(globalThis.document?.cookie ?? "").split(";")
    .map((item) => item.trim()).filter((item) => item.startsWith(prefix));
  if (matches.length !== 1) invalidRequest("platform", "csrf");
  const value = matches[0].slice(prefix.length);
  if (!/^[A-Za-z0-9_-]{32,128}$/u.test(value)) invalidRequest("platform", "csrf");
  return value;
}

function exact(value, keys, label, domain) {
  plain(value, label, domain);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    invalidResponse(domain, label);
  }
}

function plain(value, label, domain) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse(domain, label);
}

function trace(value, domain) {
  return Object.freeze({
    request_id: responseText(value.request_id, 160, "request_id", domain),
    trace_id: responseText(value.trace_id, 160, "trace_id", domain),
  });
}

function validateEtag(response, revision, domain) {
  if (response.headers.get("ETag") !== `"${revision}"`) invalidResponse(domain, "etag");
}

function responseArray(value, maximum, label, domain) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(domain, label);
}

function unique(values, label, domain) {
  if (values.length !== new Set(values).size) invalidResponse(domain, label);
}

function clientId(value, pattern, label, domain) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalidRequest(domain, label);
  return text;
}

function clientInteger(value, minimum, maximum, label, domain) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidRequest(domain, label);
  }
  return value;
}

function clientReason(value, domain) {
  const text = String(value ?? "");
  if (!SAFE_CODE.test(text)) invalidRequest(domain, "reason_code");
  return text;
}

function clientIdempotency(value, domain) {
  const text = String(value ?? "");
  if (!IDEMPOTENCY.test(text)) invalidRequest(domain, "idempotency_key");
  return text;
}

function clientTimestamp(value, label, domain) {
  const text = String(value ?? "");
  if (text.length > 40 || !Number.isFinite(Date.parse(text))) invalidRequest(domain, label);
  return text;
}

function clientFeatures(value, domain) {
  if (!Array.isArray(value) || value.length > FEATURES.length) invalidRequest(domain, "features");
  const normalized = value.map(String);
  if (normalized.length !== new Set(normalized).size
    || normalized.some((item) => !FEATURES.includes(item))) invalidRequest(domain, "features");
  return [...normalized].sort();
}

function responseId(value, pattern, label, domain) {
  return responsePattern(value, pattern, label, domain);
}

function responsePattern(value, pattern, label, domain) {
  const text = responseText(value, 2048, label, domain);
  if (!pattern.test(text)) invalidResponse(domain, label);
  return text;
}

function responseText(value, maximum, label, domain) {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function responseInteger(value, minimum, maximum, label, domain) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function responseChoice(value, choices, label, domain) {
  if (!choices.includes(value)) invalidResponse(domain, label);
  return value;
}

function responseBoolean(value, label, domain) {
  if (typeof value !== "boolean") invalidResponse(domain, label);
  return value;
}

function responseFeatures(value, domain) {
  responseArray(value, FEATURES.length, "features", domain);
  const normalized = value.map((item) => responseChoice(item, FEATURES, "feature", domain));
  if (normalized.length !== new Set(normalized).size
    || JSON.stringify(normalized) !== JSON.stringify([...normalized].sort())) {
    invalidResponse(domain, "features");
  }
  return Object.freeze(normalized);
}

function timestamp(value, label, domain) {
  const text = responseText(value, 40, label, domain);
  if (!Number.isFinite(Date.parse(text))) invalidResponse(domain, label);
  return text;
}

function nullableTimestamp(value, label, domain) {
  return value === null ? null : timestamp(value, label, domain);
}

function nullableReason(value, label, domain) {
  return value === null ? null : responsePattern(value, SAFE_CODE, label, domain);
}

function safeText(value) {
  return typeof value === "string" && value.length <= 240 ? value : "Request failed";
}

function apiError(domain, status, code, message, requestId = "-") {
  if (domain === "platform") return new PlatformApiError({ status, code, message, requestId });
  return new AuthApiError({ status, code, message, requestId });
}

function invalidRequest(domain, label) {
  throw apiError(domain, 422, "edge_license.request_invalid", `Invalid ${label}`);
}

function invalidResponse(domain, label) {
  throw apiError(domain, 502, "edge_license.response_invalid", `Invalid ${label}`);
}
