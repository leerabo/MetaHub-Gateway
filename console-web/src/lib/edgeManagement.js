/** PRD-120/121 Workspace Edge identity, desired state, and Platform Fleet clients. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const DEPLOYMENT_ID = /^edp_[0-9a-f]{32}$/u;
const INTENT_ID = /^eri_[0-9a-f]{32}$/u;
const CERTIFICATE_ID = /^edc_[0-9a-f]{32}$/u;
const IDENTITY_ID = /^edi_[0-9a-f]{32}$/u;
const EVENT_ID = /^ege_[0-9a-f]{32}$/u;
const DESIRED_STATE_ID = /^edst_[0-9a-f]{32}$/u;
const CONFIG_SNAPSHOT_ID = /^csn_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const CHAIN_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const SAFE_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u;
const REGION_CODE = /^[a-z0-9][a-z0-9-]{1,39}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const ONE_USE_VALUE = /^e(?:rg|ch)_[A-Za-z0-9_-]{43}$/u;
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

const DEPLOYMENT_KEYS = Object.freeze([
  "agent_version", "certificate_expires_at", "created_at", "current_certificate",
  "deployment_id", "display_name", "health_code", "identity_generation",
  "last_connected_at", "mode", "recent_events", "region_code", "revision",
  "revocation_generation", "revoked_at", "status", "updated_at", "workspace_id",
]);
const CERTIFICATE_KEYS = Object.freeze([
  "certificate_id", "certificate_sha256", "drain_until", "generation", "identity_id",
  "issuer_key_id", "not_after", "not_before", "public_key_sha256", "status",
]);
const EVENT_KEYS = Object.freeze([
  "event_id", "event_type", "generation", "occurred_at", "outcome_code", "reason_code",
]);
const FLEET_KEYS = Object.freeze([
  "agent_version", "certificate_expires_at", "deployment_id", "health_code",
  "last_connected_at", "mode", "region_code", "revision", "revocation_generation",
  "status", "updated_at", "desired_generation", "applied_generation", "lkg_generation",
  "desired_state_status", "desired_state_reason_code", "desired_state_drifted",
]);
const DESIRED_STATE_KEYS = Object.freeze([
  "desired_state_id", "deployment_id", "generation", "base_generation",
  "rollback_of_generation", "source_config_snapshot_id", "source_generation",
  "source_revocation_watermark", "change_class", "status", "revision", "head_revision",
  "requested_by_account_id", "approved_by_account_id", "desired_generation",
  "applied_generation", "lkg_generation", "last_ack_status", "last_reason_code", "drifted",
  "requested_at", "decided_at", "published_at", "last_ack_at", "updated_at",
]);

export async function listEdgeDeployments({ controlApiUrl, signal } = {}) {
  const result = await request(controlApiUrl, "/edge/deployments", { signal }, "workspace");
  exact(result.payload, ["deployments", "request_id", "trace_id"], "list");
  boundedArray(result.payload.deployments, 200, "deployments");
  const deployments = result.payload.deployments.map(validateDeployment);
  unique(deployments.map((item) => item.deployment_id), "deployments");
  return Object.freeze({
    deployments: Object.freeze(deployments),
    ...trace(result.payload),
  });
}

export async function createEdgeRegistration({
  controlApiUrl,
  displayName,
  regionCode,
  mode = "connected",
  tokenTtlSeconds = 600,
  idempotencyKey,
  signal,
} = {}) {
  const body = Object.freeze({
    display_name: clientText(displayName, 120, "display_name", 2),
    region_code: clientRegion(regionCode),
    mode: clientChoice(mode, ["connected", "isolated"], "mode"),
    token_ttl_seconds: clientInteger(tokenTtlSeconds, 60, 600, "token_ttl_seconds"),
  });
  const result = await request(controlApiUrl, "/edge/registrations", {
    method: "POST",
    body,
    csrf: true,
    headers: { "Idempotency-Key": clientIdempotencyKey(idempotencyKey) },
    signal,
  }, "workspace");
  exact(result.payload, [
    "challenge", "deployment", "expires_at", "idempotent_replay", "registration_intent_id",
    "registration_token", "request_id", "trace_id",
  ], "registration");
  const replay = boolean(result.payload.idempotent_replay, "idempotent_replay");
  const registrationToken = nullableOneUse(result.payload.registration_token, "erg_", "registration_token");
  const challenge = nullableOneUse(result.payload.challenge, "ech_", "challenge");
  if ((!replay && (!registrationToken || !challenge)) || (replay && Boolean(registrationToken) !== Boolean(challenge))) {
    invalidWorkspaceResponse("registration_secret");
  }
  return Object.freeze({
    deployment: validateDeployment(result.payload.deployment),
    registration_intent_id: responseId(result.payload.registration_intent_id, INTENT_ID, "intent_id"),
    registration_token: registrationToken,
    challenge,
    expires_at: timestamp(result.payload.expires_at, "expires_at"),
    idempotent_replay: replay,
    ...trace(result.payload),
  });
}

export async function revokeEdgeDeployment({
  controlApiUrl,
  deploymentId,
  revision,
  reasonCode = "owner_requested",
  idempotencyKey,
  signal,
} = {}) {
  const id = responseId(deploymentId, DEPLOYMENT_ID, "deployment_id", true);
  const result = await request(controlApiUrl, `/edge/deployments/${encodeURIComponent(id)}/revoke`, {
    method: "POST",
    body: { reason_code: clientChoice(reasonCode, [
      "owner_requested", "security_incident", "device_lost", "deployment_retired",
    ], "reason_code") },
    csrf: true,
    headers: {
      "Idempotency-Key": clientIdempotencyKey(idempotencyKey),
      "If-Match": `"${clientInteger(revision, 1, Number.MAX_SAFE_INTEGER, "revision")}"`,
    },
    signal,
  }, "workspace");
  exact(result.payload, [
    "deployment", "idempotent_replay", "request_id", "revoked_session_count", "trace_id",
  ], "revoke");
  const deployment = validateDeployment(result.payload.deployment);
  if (deployment.deployment_id !== id || deployment.status !== "revoked") invalidWorkspaceResponse("deployment");
  return Object.freeze({
    deployment,
    revoked_session_count: integer(result.payload.revoked_session_count, 0, Number.MAX_SAFE_INTEGER, "revoked_session_count"),
    idempotent_replay: boolean(result.payload.idempotent_replay, "idempotent_replay"),
    ...trace(result.payload),
  });
}

export async function getEdgeDesiredState({ controlApiUrl, deploymentId, signal } = {}) {
  const id = clientId(deploymentId, DEPLOYMENT_ID, "deployment_id");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/desired-state`,
    { signal },
    "workspace",
  );
  exact(result.payload, ["state", "idempotent_replay", "request_id", "trace_id"], "desired_state");
  const state = result.payload.state === null ? null : validateDesiredState(result.payload.state);
  if (state && state.deployment_id !== id) invalidWorkspaceResponse("desired_state_owner");
  const revision = responseRevision(result.response);
  if (revision !== desiredResponseRevision(state)) invalidWorkspaceResponse("desired_state_etag");
  return Object.freeze({
    state,
    revision,
    idempotent_replay: boolean(result.payload.idempotent_replay, "idempotent_replay"),
    ...trace(result.payload),
  });
}

export async function publishEdgeDesiredState({
  controlApiUrl,
  deploymentId,
  sourceConfigSnapshotId,
  expectedRevision,
  idempotencyKey,
  signal,
} = {}) {
  return writeDesiredState({
    controlApiUrl,
    deploymentId,
    path: "desired-state",
    body: {
      action: "publish",
      source_config_snapshot_id: clientId(
        sourceConfigSnapshotId,
        CONFIG_SNAPSHOT_ID,
        "source_config_snapshot_id",
      ),
    },
    expectedRevision,
    idempotencyKey,
    signal,
  });
}

export async function decideEdgeDesiredState({
  controlApiUrl,
  deploymentId,
  action,
  desiredStateId,
  reasonCode,
  expectedRevision,
  idempotencyKey,
  signal,
} = {}) {
  const decision = clientChoice(action, ["approve", "reject"], "action");
  return writeDesiredState({
    controlApiUrl,
    deploymentId,
    path: "desired-state",
    body: {
      action: decision,
      desired_state_id: clientId(desiredStateId, DESIRED_STATE_ID, "desired_state_id"),
      reason_code: clientChoice(
        reasonCode,
        decision === "approve"
          ? ["approved_change"]
          : ["risk_unacceptable", "change_not_required"],
        "reason_code",
      ),
    },
    expectedRevision,
    idempotencyKey,
    signal,
  });
}

export async function rollbackEdgeDesiredState({
  controlApiUrl,
  deploymentId,
  lkgGeneration,
  reasonCode,
  expectedRevision,
  idempotencyKey,
  signal,
} = {}) {
  return writeDesiredState({
    controlApiUrl,
    deploymentId,
    path: "rollbacks",
    body: {
      lkg_generation: clientInteger(
        lkgGeneration,
        1,
        Number.MAX_SAFE_INTEGER,
        "lkg_generation",
      ),
      reason_code: clientChoice(
        reasonCode,
        ["operator_requested", "apply_failed_recovery"],
        "reason_code",
      ),
    },
    expectedRevision,
    idempotencyKey,
    signal,
  });
}

export async function listPlatformEdgeFleet({ controlApiUrl, signal } = {}) {
  const result = await request(
    controlApiUrl,
    "/platform/fleet/edge-deployments",
    { signal },
    "platform",
  );
  exact(result.payload, ["deployments", "request_id", "trace_id"], "fleet");
  boundedArray(result.payload.deployments, 500, "deployments", true);
  const deployments = result.payload.deployments.map(validateFleetDeployment);
  unique(deployments.map((item) => item.deployment_id), "fleet", true);
  return Object.freeze({ deployments: Object.freeze(deployments), ...trace(result.payload, true) });
}

export function createEdgeIdempotencyKey(lane = "command") {
  const normalizedLane = String(lane).replace(/[^a-z0-9_-]/gu, "").slice(0, 24) || "command";
  const uuid = globalThis.crypto?.randomUUID?.()?.replaceAll("-", "").toLowerCase();
  if (!/^[0-9a-f]{32}$/u.test(String(uuid ?? ""))) invalidWorkspaceRequest("idempotency_key");
  return `edge-${normalizedLane}-${uuid}`;
}

export function workspaceCookieApiOrigin(controlApiUrl, pageLocation = globalThis.window?.location) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { throw domainError("workspace", "workspace.configuration_invalid", 0, "服务地址无效"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    throw domainError("workspace", "workspace.configuration_invalid", 0, "服务地址无效");
  }
  const pageHostname = String(pageLocation?.hostname ?? "").toLowerCase();
  if (LOOPBACK_HOSTS.has(base.hostname.toLowerCase()) && LOOPBACK_HOSTS.has(pageHostname)) {
    base.hostname = pageHostname;
  }
  return base.origin;
}

function validateDeployment(value) {
  plain(value, "deployment");
  exact(value, DEPLOYMENT_KEYS, "deployment");
  const certificate = value.current_certificate === null ? null : validateCertificate(value.current_certificate);
  boundedArray(value.recent_events, 12, "recent_events");
  const events = value.recent_events.map(validateEvent);
  const deploymentId = responseId(value.deployment_id, DEPLOYMENT_ID, "deployment_id");
  if (certificate && certificate.generation !== value.identity_generation) invalidWorkspaceResponse("certificate_generation");
  return Object.freeze({
    deployment_id: deploymentId,
    workspace_id: responseId(value.workspace_id, WORKSPACE_ID, "workspace_id"),
    display_name: responseText(value.display_name, 120, "display_name"),
    region_code: responseRegion(value.region_code),
    mode: responseChoice(value.mode, ["connected", "isolated"], "mode"),
    status: responseChoice(value.status, ["pending", "active", "revoked", "retired"], "status"),
    revision: integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision"),
    revocation_generation: integer(value.revocation_generation, 1, Number.MAX_SAFE_INTEGER, "revocation_generation"),
    agent_version: nullableText(value.agent_version, 64, "agent_version"),
    health_code: responseChoice(value.health_code, [
      "not_registered", "healthy", "certificate_expiring", "revoked", "offline", "unknown",
    ], "health_code"),
    last_connected_at: nullableTimestamp(value.last_connected_at, "last_connected_at"),
    certificate_expires_at: nullableTimestamp(value.certificate_expires_at, "certificate_expires_at"),
    identity_generation: nullableInteger(value.identity_generation, 1, Number.MAX_SAFE_INTEGER, "identity_generation"),
    current_certificate: certificate,
    recent_events: Object.freeze(events),
    created_at: timestamp(value.created_at, "created_at"),
    updated_at: timestamp(value.updated_at, "updated_at"),
    revoked_at: nullableTimestamp(value.revoked_at, "revoked_at"),
  });
}

function validateCertificate(value) {
  plain(value, "certificate");
  exact(value, CERTIFICATE_KEYS, "certificate");
  return Object.freeze({
    certificate_id: responseId(value.certificate_id, CERTIFICATE_ID, "certificate_id"),
    identity_id: responseId(value.identity_id, IDENTITY_ID, "identity_id"),
    generation: integer(value.generation, 1, Number.MAX_SAFE_INTEGER, "generation"),
    status: responseChoice(value.status, ["active", "draining", "superseded", "revoked", "expired"], "certificate_status"),
    certificate_sha256: digest(value.certificate_sha256, "certificate_sha256"),
    public_key_sha256: digest(value.public_key_sha256, "public_key_sha256"),
    issuer_key_id: responseChoice(value.issuer_key_id, ["edge-device-ca-v1"], "issuer_key_id"),
    not_before: timestamp(value.not_before, "not_before"),
    not_after: timestamp(value.not_after, "not_after"),
    drain_until: nullableTimestamp(value.drain_until, "drain_until"),
  });
}

function validateEvent(value) {
  plain(value, "event");
  exact(value, EVENT_KEYS, "event");
  return Object.freeze({
    event_id: responseId(value.event_id, EVENT_ID, "event_id"),
    event_type: responseChoice(value.event_type, [
      "edge.registered", "edge.certificate_rotated", "edge.revoked", "edge.authentication_failed",
    ], "event_type"),
    generation: integer(value.generation, 1, Number.MAX_SAFE_INTEGER, "generation"),
    outcome_code: responseChoice(value.outcome_code, ["succeeded", "rejected"], "outcome_code"),
    reason_code: responseText(value.reason_code, 80, "reason_code"),
    occurred_at: timestamp(value.occurred_at, "occurred_at"),
  });
}

function validateFleetDeployment(value) {
  plain(value, "fleet_deployment", true);
  exact(value, FLEET_KEYS, "fleet_deployment", true);
  const desiredGeneration = integer(
    value.desired_generation,
    0,
    Number.MAX_SAFE_INTEGER,
    "desired_generation",
    true,
  );
  const appliedGeneration = integer(
    value.applied_generation,
    0,
    desiredGeneration,
    "applied_generation",
    true,
  );
  const lkgGeneration = integer(
    value.lkg_generation,
    0,
    appliedGeneration,
    "lkg_generation",
    true,
  );
  const drifted = platformBoolean(value.desired_state_drifted, "desired_state_drifted");
  if (drifted !== (desiredGeneration !== appliedGeneration)) {
    invalidPlatformResponse("desired_state_drifted");
  }
  return Object.freeze({
    deployment_id: responseId(value.deployment_id, DEPLOYMENT_ID, "deployment_id", false, true),
    region_code: responseRegion(value.region_code, true),
    mode: responseChoice(value.mode, ["connected", "isolated"], "mode", true),
    status: responseChoice(value.status, ["pending", "active", "revoked", "retired"], "status", true),
    revision: integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision", true),
    revocation_generation: integer(value.revocation_generation, 1, Number.MAX_SAFE_INTEGER, "revocation_generation", true),
    agent_version: nullableText(value.agent_version, 64, "agent_version", true),
    health_code: responseText(value.health_code, 80, "health_code", true),
    certificate_expires_at: nullableTimestamp(value.certificate_expires_at, "certificate_expires_at", true),
    last_connected_at: nullableTimestamp(value.last_connected_at, "last_connected_at", true),
    desired_generation: desiredGeneration,
    applied_generation: appliedGeneration,
    lkg_generation: lkgGeneration,
    desired_state_status: responseChoice(value.desired_state_status, [
      "not_published", "not_observed", "applied", "failed",
    ], "desired_state_status", true),
    desired_state_reason_code: responseChoice(value.desired_state_reason_code, [
      "not_published", "not_observed", "applied", "preflight_failed", "apply_failed",
      "storage_failed",
    ], "desired_state_reason_code", true),
    desired_state_drifted: drifted,
    updated_at: timestamp(value.updated_at, "updated_at", true),
  });
}

function validateDesiredState(value) {
  plain(value, "desired_state");
  exact(value, DESIRED_STATE_KEYS, "desired_state");
  const desiredGeneration = integer(
    value.desired_generation,
    0,
    Number.MAX_SAFE_INTEGER,
    "desired_generation",
  );
  const appliedGeneration = integer(
    value.applied_generation,
    0,
    desiredGeneration,
    "applied_generation",
  );
  const lkgGeneration = integer(value.lkg_generation, 0, appliedGeneration, "lkg_generation");
  const generation = integer(value.generation, 1, Number.MAX_SAFE_INTEGER, "generation");
  const baseGeneration = integer(
    value.base_generation,
    0,
    generation - 1,
    "base_generation",
  );
  const drifted = boolean(value.drifted, "drifted");
  if (drifted !== (desiredGeneration !== appliedGeneration)) {
    invalidWorkspaceResponse("drifted");
  }
  return Object.freeze({
    desired_state_id: responseId(value.desired_state_id, DESIRED_STATE_ID, "desired_state_id"),
    deployment_id: responseId(value.deployment_id, DEPLOYMENT_ID, "deployment_id"),
    generation,
    base_generation: baseGeneration,
    rollback_of_generation: nullableInteger(
      value.rollback_of_generation,
      1,
      Number.MAX_SAFE_INTEGER,
      "rollback_of_generation",
    ),
    source_config_snapshot_id: responseId(
      value.source_config_snapshot_id,
      CONFIG_SNAPSHOT_ID,
      "source_config_snapshot_id",
    ),
    source_generation: integer(value.source_generation, 1, Number.MAX_SAFE_INTEGER, "source_generation"),
    source_revocation_watermark: integer(
      value.source_revocation_watermark,
      0,
      Number.MAX_SAFE_INTEGER,
      "source_revocation_watermark",
    ),
    change_class: responseChoice(value.change_class, ["standard", "high_risk"], "change_class"),
    status: responseChoice(
      value.status,
      ["approval_pending", "published", "superseded", "rejected"],
      "desired_status",
    ),
    revision: integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "desired_revision"),
    head_revision: integer(value.head_revision, 0, Number.MAX_SAFE_INTEGER, "head_revision"),
    requested_by_account_id: responseText(value.requested_by_account_id, 160, "requested_by"),
    approved_by_account_id: nullableText(value.approved_by_account_id, 160, "approved_by"),
    desired_generation: desiredGeneration,
    applied_generation: appliedGeneration,
    lkg_generation: lkgGeneration,
    last_ack_status: responseChoice(
      value.last_ack_status,
      ["not_observed", "applied", "failed"],
      "last_ack_status",
    ),
    last_reason_code: responseChoice(value.last_reason_code, [
      "not_observed", "applied", "preflight_failed", "apply_failed", "storage_failed",
    ], "last_reason_code"),
    drifted,
    requested_at: timestamp(value.requested_at, "requested_at"),
    decided_at: nullableTimestamp(value.decided_at, "decided_at"),
    published_at: nullableTimestamp(value.published_at, "published_at"),
    last_ack_at: nullableTimestamp(value.last_ack_at, "last_ack_at"),
    updated_at: timestamp(value.updated_at, "desired_updated_at"),
  });
}

async function writeDesiredState({
  controlApiUrl,
  deploymentId,
  path,
  body,
  expectedRevision,
  idempotencyKey,
  signal,
}) {
  const id = clientId(deploymentId, DEPLOYMENT_ID, "deployment_id");
  const result = await request(controlApiUrl, `/edge/deployments/${encodeURIComponent(id)}/${path}`, {
    method: "POST",
    body,
    csrf: true,
    headers: {
      "Idempotency-Key": clientIdempotencyKey(idempotencyKey),
      "If-Match": `"${clientInteger(
        expectedRevision,
        0,
        Number.MAX_SAFE_INTEGER,
        "expected_revision",
      )}"`,
    },
    signal,
  }, "workspace");
  exact(result.payload, ["state", "idempotent_replay", "request_id", "trace_id"], "desired_state");
  const state = validateDesiredState(result.payload.state);
  if (state.deployment_id !== id) invalidWorkspaceResponse("desired_state_owner");
  const revision = responseRevision(result.response);
  if (revision !== desiredResponseRevision(state)) invalidWorkspaceResponse("desired_state_etag");
  return Object.freeze({
    state,
    revision,
    idempotent_replay: boolean(result.payload.idempotent_replay, "idempotent_replay"),
    ...trace(result.payload),
  });
}

async function request(controlApiUrl, path, options, domain) {
  const headers = { Accept: "application/json", ...(options.headers ?? {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = readWorkspaceCsrfCookie();
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, domain), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw domainError(domain, `${domain}.network_unavailable`, 0, "服务暂时不可用");
  }
  const payload = await readJson(response, domain);
  if (!response.ok) {
    const code = SAFE_CODE.test(String(payload?.code ?? "")) ? payload.code : `${domain}.request_failed`;
    throw domainError(domain, code, Number(response.status) || 0, safeMessage(payload?.message), payload?.request_id, payload?.trace_id);
  }
  validateBoundary(response, payload, domain);
  return { payload, response };
}

function validateBoundary(response, payload, domain) {
  const directives = String(response.headers?.get?.("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim()).filter(Boolean);
  const requestId = response.headers?.get?.("X-Request-ID") ?? "";
  const traceId = response.headers?.get?.("X-Trace-ID") ?? "";
  if (!directives.includes("no-store") || !directives.includes("private")
    || !CHAIN_ID.test(requestId) || !CHAIN_ID.test(traceId)
    || payload?.request_id !== requestId || payload?.trace_id !== traceId) {
    if (domain === "platform") invalidPlatformResponse("boundary");
    invalidWorkspaceResponse("boundary");
  }
}

function apiUrl(controlApiUrl, path, domain) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { throw domainError(domain, `${domain}.configuration_invalid`, 0, "服务地址无效"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    throw domainError(domain, `${domain}.configuration_invalid`, 0, "服务地址无效");
  }
  return new URL(path, base).toString();
}

async function readJson(response, domain) {
  try {
    const payload = await response.json();
    plain(payload, "response", domain === "platform");
    return payload;
  } catch (error) {
    if (error instanceof AuthApiError || error instanceof PlatformApiError) throw error;
    if (domain === "platform") invalidPlatformResponse("json");
    invalidWorkspaceResponse("json");
  }
}

function trace(payload, platform = false) {
  return {
    request_id: responseText(payload.request_id, 160, "request_id", platform),
    trace_id: responseText(payload.trace_id, 160, "trace_id", platform),
  };
}

function exact(value, keys, label, platform = false) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    if (platform) invalidPlatformResponse(label);
    invalidWorkspaceResponse(label);
  }
}

function plain(value, label, platform = false) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) {
    if (platform) invalidPlatformResponse(label);
    invalidWorkspaceResponse(label);
  }
}

function boundedArray(value, maximum, label, platform = false) {
  if (!Array.isArray(value) || value.length > maximum) {
    if (platform) invalidPlatformResponse(label);
    invalidWorkspaceResponse(label);
  }
}

function unique(values, label, platform = false) {
  if (values.length !== new Set(values).size) {
    if (platform) invalidPlatformResponse(label);
    invalidWorkspaceResponse(label);
  }
}

function responseId(value, pattern, label, client = false, platform = false) {
  const text = String(value ?? "");
  if (!pattern.test(text)) {
    if (client) invalidWorkspaceRequest(label);
    if (platform) invalidPlatformResponse(label);
    invalidWorkspaceResponse(label);
  }
  return text;
}

function responseText(value, maximum, label, platform = false) {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    if (platform) invalidPlatformResponse(label);
    invalidWorkspaceResponse(label);
  }
  return value;
}

function nullableText(value, maximum, label, platform = false) {
  return value === null ? null : responseText(value, maximum, label, platform);
}

function responseChoice(value, values, label, platform = false) {
  if (!values.includes(value)) {
    if (platform) invalidPlatformResponse(label);
    invalidWorkspaceResponse(label);
  }
  return value;
}

function responseRegion(value, platform = false) {
  const text = responseText(value, 40, "region_code", platform);
  if (!REGION_CODE.test(text)) {
    if (platform) invalidPlatformResponse("region_code");
    invalidWorkspaceResponse("region_code");
  }
  return text;
}

function integer(value, minimum, maximum, label, platform = false) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    if (platform) invalidPlatformResponse(label);
    invalidWorkspaceResponse(label);
  }
  return value;
}

function nullableInteger(value, minimum, maximum, label) {
  return value === null ? null : integer(value, minimum, maximum, label);
}

function boolean(value, label) {
  if (typeof value !== "boolean") invalidWorkspaceResponse(label);
  return value;
}

function platformBoolean(value, label) {
  if (typeof value !== "boolean") invalidPlatformResponse(label);
  return value;
}

function timestamp(value, label, platform = false) {
  const text = responseText(value, 40, label, platform);
  if (!Number.isFinite(Date.parse(text))) {
    if (platform) invalidPlatformResponse(label);
    invalidWorkspaceResponse(label);
  }
  return text;
}

function nullableTimestamp(value, label, platform = false) {
  return value === null ? null : timestamp(value, label, platform);
}

function digest(value, label) {
  const text = responseText(value, 64, label);
  if (!SHA256.test(text)) invalidWorkspaceResponse(label);
  return text;
}

function nullableOneUse(value, prefix, label) {
  if (value === null) return null;
  const text = responseText(value, 47, label);
  if (!ONE_USE_VALUE.test(text) || !text.startsWith(prefix)) invalidWorkspaceResponse(label);
  return text;
}

function clientText(value, maximum, label, minimum = 1) {
  if (typeof value !== "string" || value !== value.trim() || value.length < minimum || value.length > maximum) {
    invalidWorkspaceRequest(label);
  }
  return value;
}

function clientId(value, pattern, label) {
  const text = clientText(value, 160, label);
  if (!pattern.test(text)) invalidWorkspaceRequest(label);
  return text;
}

function responseRevision(response) {
  const match = /^"(0|[1-9][0-9]*)"$/u.exec(String(response.headers?.get?.("ETag") ?? ""));
  const revision = Number(match?.[1] ?? Number.NaN);
  if (!Number.isSafeInteger(revision)) invalidWorkspaceResponse("etag");
  return revision;
}

function desiredResponseRevision(state) {
  if (!state) return 0;
  return state.status === "approval_pending" ? state.revision : state.head_revision;
}

function clientRegion(value) {
  const text = clientText(value, 40, "region_code", 2).toLowerCase();
  if (!REGION_CODE.test(text)) invalidWorkspaceRequest("region_code");
  return text;
}

function clientChoice(value, values, label) {
  if (!values.includes(value)) invalidWorkspaceRequest(label);
  return value;
}

function clientInteger(value, minimum, maximum, label) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalidWorkspaceRequest(label);
  return value;
}

function clientIdempotencyKey(value) {
  if (typeof value !== "string" || !IDEMPOTENCY_KEY.test(value)) invalidWorkspaceRequest("idempotency_key");
  return value;
}

function safeMessage(value) {
  return typeof value === "string" && value.length <= 240 ? value : "请求未完成";
}

function domainError(domain, code, status, message, requestId = "-", traceId = "-") {
  if (domain === "platform") return new PlatformApiError({ status, code, message, requestId });
  return new AuthApiError({ status, code, message, requestId, traceId });
}

function invalidWorkspaceRequest(label) {
  throw new AuthApiError({ status: 422, code: "edge.request_invalid", message: `私有 Gateway 字段无效：${label}` });
}

function invalidWorkspaceResponse(label) {
  throw new AuthApiError({ status: 502, code: "edge.response_invalid", message: `私有 Gateway 响应无效：${label}` });
}

function invalidPlatformResponse(label) {
  throw new PlatformApiError({ status: 502, code: "platform.edge_fleet_response_invalid", message: `Fleet 响应无效：${label}` });
}
