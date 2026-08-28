/** PRD-130 strict Workspace and Platform offline-media exchange clients. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const IDS = Object.freeze({
  workspace: /^wsp_[0-9a-f]{32}$/u,
  deployment: /^edp_[0-9a-f]{32}$/u,
  bundle: /^obb_[0-9a-f]{32}$/u,
  request: /^obr_[0-9a-f]{32}$/u,
  conflict: /^ofc_[0-9a-f]{32}$/u,
  entry: /^obe_[0-9a-f]{32}$/u,
});
const TENANT = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const SAFE_CODE = /^[a-z][a-z0-9_.-]{1,79}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const GRANT = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,4095}$/u;
const CHAIN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const MEDIA_TYPE = /^[a-z0-9][a-z0-9.+-]{0,31}\/[a-z0-9][a-z0-9.+-]{0,63}$/u;
const NONCE = /^[A-Za-z0-9_-]{16}$/u;
const KEY_ID = /^[a-z0-9][a-z0-9._:-]{2,79}$/u;
const DOMAINS = Object.freeze([
  "desired_state", "license", "release", "status", "usage", "audit", "validation",
]);
const DIRECTIONS = Object.freeze(["cloud_to_edge", "edge_to_cloud"]);

export async function getWorkspaceOfflineBundleState({
  controlApiUrl,
  deploymentId,
  signal,
} = {}) {
  const deployment = clientPattern(deploymentId, IDS.deployment, "deployment_id", "workspace");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(deployment)}/offline-bundles`,
    { signal },
    "workspace",
  );
  const state = offlineState(result.payload, "workspace");
  if (state.deployment_id !== deployment) invalidResponse("workspace", "owner");
  return state;
}

export async function requestWorkspaceOfflineBundle({
  controlApiUrl,
  operation,
  manifest,
  reasonCode,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  const action = clientChoice(operation, ["export", "import"], "operation", "workspace");
  const checkedManifest = clientManifest(manifest, "workspace");
  const expectedDirection = action === "export" ? "cloud_to_edge" : "edge_to_cloud";
  if (checkedManifest.direction !== expectedDirection) invalidRequest("workspace", "direction");
  const result = await request(
    controlApiUrl,
    `/edge/offline-bundles/${action}`,
    workspaceWrite({
      stepUpGrant,
      idempotencyKey,
      body: {
        manifest,
        reason_code: clientPattern(reasonCode, SAFE_CODE, "reason_code", "workspace"),
      },
      signal,
    }),
    "workspace",
  );
  return offlineState(result.payload, "workspace");
}

export async function resolveWorkspaceOfflineConflict({
  controlApiUrl,
  conflictId,
  resolution,
  manifest,
  expectedCurrentGeneration,
  reasonCode,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  const conflict = clientPattern(conflictId, IDS.conflict, "conflict_id", "workspace");
  clientManifest(manifest, "workspace");
  const result = await request(
    controlApiUrl,
    `/edge/offline-bundles/conflicts/${encodeURIComponent(conflict)}/resolutions`,
    workspaceWrite({
      stepUpGrant,
      idempotencyKey,
      body: {
        resolution: clientChoice(
          resolution,
          ["keep_cloud", "keep_local", "supersede_with_new"],
          "resolution",
          "workspace",
        ),
        manifest,
        expected_current_generation: clientInteger(
          expectedCurrentGeneration, 1, 2_147_483_647,
          "expected_current_generation", "workspace",
        ),
        reason_code: clientPattern(reasonCode, SAFE_CODE, "reason_code", "workspace"),
      },
      signal,
    }),
    "workspace",
  );
  return offlineState(result.payload, "workspace");
}

export async function listPlatformOfflineBundles({ controlApiUrl, signal } = {}) {
  const result = await request(
    controlApiUrl, "/platform/edge/offline-bundles", { signal }, "platform",
  );
  exact(result.payload, ["items", "request_id", "trace_id"], "platform_list", "platform");
  responseArray(result.payload.items, 500, "items", "platform");
  const items = result.payload.items.map((item) => platformItem(item));
  unique(items.map((item) => `${item.workspace_id}:${item.state.deployment_id}`), "items", "platform");
  return Object.freeze({ items: Object.freeze(items), ...trace(result.payload, "platform") });
}

export async function decidePlatformOfflineBundle({
  controlApiUrl,
  requestId,
  action,
  expectedRevision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const offlineRequest = clientPattern(requestId, IDS.request, "request_id", "platform");
  const result = await request(
    controlApiUrl,
    `/platform/edge/offline-bundles/${encodeURIComponent(offlineRequest)}/decisions`,
    {
      method: "POST",
      csrf: true,
      idempotencyKey: clientPattern(
        idempotencyKey, IDEMPOTENCY, "idempotency_key", "platform",
      ),
      body: {
        action: clientChoice(action, ["approve", "reject"], "action", "platform"),
        expected_revision: clientInteger(
          expectedRevision, 1, 2_147_483_647, "expected_revision", "platform",
        ),
        reason_code: clientPattern(reasonCode, SAFE_CODE, "reason_code", "platform"),
      },
      signal,
    },
    "platform",
  );
  exact(result.payload, [
    "request", "signed_bundle", "request_id", "trace_id", "idempotent_replay",
  ], "decision", "platform");
  const requestSummary = offlineRequestSummary(result.payload.request, "platform");
  if (requestSummary.request_id !== offlineRequest) invalidResponse("platform", "owner");
  const bundle = result.payload.signed_bundle === null
    ? null : signedBundleSummary(result.payload.signed_bundle);
  return Object.freeze({
    request: requestSummary,
    bundle,
    idempotent_replay: responseBoolean(
      result.payload.idempotent_replay, "idempotent_replay", "platform",
    ),
    ...trace(result.payload, "platform"),
  });
}

function workspaceWrite({ stepUpGrant, idempotencyKey, body, signal }) {
  return {
    method: "POST",
    csrf: true,
    stepUpGrant: clientPattern(stepUpGrant, GRANT, "step_up_grant", "workspace"),
    idempotencyKey: clientPattern(
      idempotencyKey, IDEMPOTENCY, "idempotency_key", "workspace",
    ),
    body,
    signal,
  };
}

function platformItem(value) {
  exact(value, ["workspace_id", "tenant_id", "state"], "platform_item", "platform");
  const workspace = responsePattern(value.workspace_id, IDS.workspace, "workspace_id", "platform");
  const tenant = responsePattern(value.tenant_id, TENANT, "tenant_id", "platform");
  return Object.freeze({
    workspace_id: workspace,
    tenant_id: tenant,
    state: offlineState(value.state, "platform"),
  });
}

function offlineState(value, domain) {
  exact(value, [
    "deployment_id", "asynchronous_management", "requests", "bundles", "conflicts",
    "request_id", "trace_id", "idempotent_replay",
  ], "state", domain);
  if (value.asynchronous_management !== true) invalidResponse(domain, "management_mode");
  responseArray(value.requests, 500, "requests", domain);
  responseArray(value.bundles, 500, "bundles", domain);
  responseArray(value.conflicts, 500, "conflicts", domain);
  const requests = value.requests.map((item) => offlineRequestSummary(item, domain));
  const bundles = value.bundles.map((item) => bundleSummary(item, domain));
  const conflicts = value.conflicts.map((item) => conflictSummary(item, domain));
  unique(requests.map((item) => item.request_id), "requests", domain);
  unique(bundles.map((item) => item.bundle_id), "bundles", domain);
  unique(conflicts.map((item) => item.conflict_id), "conflicts", domain);
  const deployment = responsePattern(
    value.deployment_id, IDS.deployment, "deployment_id", domain,
  );
  if ([...requests, ...bundles, ...conflicts].some(
    (item) => item.deployment_id !== deployment,
  )) invalidResponse(domain, "owner");
  return Object.freeze({
    deployment_id: deployment,
    asynchronous_management: true,
    requests: Object.freeze(requests),
    bundles: Object.freeze(bundles),
    conflicts: Object.freeze(conflicts),
    idempotent_replay: responseBoolean(
      value.idempotent_replay, "idempotent_replay", domain,
    ),
    ...trace(value, domain),
  });
}

function offlineRequestSummary(value, domain) {
  exact(value, [
    "request_id", "deployment_id", "operation", "status", "reason_code",
    "target_bundle_id", "target_conflict_id", "revision", "requested_at", "decided_at",
  ], "request", domain);
  return Object.freeze({
    request_id: responsePattern(value.request_id, IDS.request, "request_id", domain),
    deployment_id: responsePattern(
      value.deployment_id, IDS.deployment, "deployment_id", domain,
    ),
    operation: responseChoice(value.operation, ["export", "import", "resolve"],
      "operation", domain),
    status: responseChoice(value.status, ["pending_platform", "approved", "rejected", "expired"],
      "status", domain),
    reason_code: responsePattern(value.reason_code, SAFE_CODE, "reason_code", domain),
    target_bundle_id: nullablePattern(
      value.target_bundle_id, IDS.bundle, "target_bundle_id", domain,
    ),
    target_conflict_id: nullablePattern(
      value.target_conflict_id, IDS.conflict, "target_conflict_id", domain,
    ),
    revision: responseInteger(value.revision, 1, 2_147_483_647, "revision", domain),
    requested_at: timestamp(value.requested_at, "requested_at", domain),
    decided_at: nullableTimestamp(value.decided_at, "decided_at", domain),
  });
}

function bundleSummary(value, domain) {
  exact(value, [
    "bundle_id", "deployment_id", "direction", "producer_generation", "domains",
    "entry_count", "encrypted_size_bytes", "transfer_status", "next_sequence", "chunk_count",
    "manifest_sha256", "created_at", "expires_at",
  ], "bundle", domain);
  responseArray(value.domains, DOMAINS.length, "domains", domain);
  const domains = value.domains.map((item) => responseChoice(item, DOMAINS, "domain", domain));
  unique(domains, "domains", domain);
  const chunkCount = responseInteger(value.chunk_count, 1, 1_000_000, "chunk_count", domain);
  const nextSequence = responseInteger(value.next_sequence, 0, chunkCount, "next_sequence", domain);
  return Object.freeze({
    bundle_id: responsePattern(value.bundle_id, IDS.bundle, "bundle_id", domain),
    deployment_id: responsePattern(
      value.deployment_id, IDS.deployment, "deployment_id", domain,
    ),
    direction: responseChoice(value.direction, DIRECTIONS, "direction", domain),
    producer_generation: responseInteger(
      value.producer_generation, 1, 2_147_483_647, "producer_generation", domain,
    ),
    domains: Object.freeze(domains),
    entry_count: responseInteger(value.entry_count, 1, 1024, "entry_count", domain),
    encrypted_size_bytes: responseInteger(
      value.encrypted_size_bytes, 17, 10_737_418_240, "encrypted_size_bytes", domain,
    ),
    transfer_status: responseChoice(value.transfer_status,
      ["prepared", "transferring", "verified", "applied", "conflict", "failed"],
      "transfer_status", domain),
    next_sequence: nextSequence,
    chunk_count: chunkCount,
    manifest_sha256: responsePattern(
      value.manifest_sha256, SHA256, "manifest_sha256", domain,
    ),
    created_at: timestamp(value.created_at, "created_at", domain),
    expires_at: timestamp(value.expires_at, "expires_at", domain),
  });
}

function conflictSummary(value, domain) {
  exact(value, [
    "conflict_id", "bundle_id", "deployment_id", "domain", "base_generation",
    "base_sha256", "cloud_generation", "cloud_sha256", "local_generation", "local_sha256",
    "resolution", "new_generation", "detected_at", "resolved_at",
  ], "conflict", domain);
  const resolution = value.resolution === null ? null : responseChoice(
    value.resolution, ["keep_cloud", "keep_local", "supersede_with_new"],
    "resolution", domain,
  );
  return Object.freeze({
    conflict_id: responsePattern(value.conflict_id, IDS.conflict, "conflict_id", domain),
    bundle_id: responsePattern(value.bundle_id, IDS.bundle, "bundle_id", domain),
    deployment_id: responsePattern(
      value.deployment_id, IDS.deployment, "deployment_id", domain,
    ),
    domain: responseChoice(value.domain, DOMAINS, "domain", domain),
    base_generation: responseInteger(
      value.base_generation, 0, 2_147_483_647, "base_generation", domain,
    ),
    base_sha256: nullablePattern(value.base_sha256, SHA256, "base_sha256", domain),
    cloud_generation: responseInteger(
      value.cloud_generation, 1, 2_147_483_647, "cloud_generation", domain,
    ),
    cloud_sha256: responsePattern(value.cloud_sha256, SHA256, "cloud_sha256", domain),
    local_generation: responseInteger(
      value.local_generation, 1, 2_147_483_647, "local_generation", domain,
    ),
    local_sha256: responsePattern(value.local_sha256, SHA256, "local_sha256", domain),
    resolution,
    new_generation: nullableInteger(
      value.new_generation, 1, 2_147_483_647, "new_generation", domain,
    ),
    detected_at: timestamp(value.detected_at, "detected_at", domain),
    resolved_at: nullableTimestamp(value.resolved_at, "resolved_at", domain),
  });
}

function signedBundleSummary(value) {
  exact(value, ["manifest", "manifest_sha256", "signature"], "signed_bundle", "platform");
  const summary = manifestDocument(value.manifest, "platform");
  exact(value.signature, ["algorithm", "key_id", "public_key", "value"],
    "signature", "platform");
  if (value.signature.algorithm !== "Ed25519") invalidResponse("platform", "signature");
  responsePattern(value.signature.key_id, KEY_ID, "key_id", "platform");
  responsePattern(value.signature.public_key, /^[A-Za-z0-9_-]{43}$/u,
    "public_key", "platform");
  responsePattern(value.signature.value, /^[A-Za-z0-9_-]{86}$/u,
    "signature_value", "platform");
  return Object.freeze({
    ...summary,
    manifest_sha256: responsePattern(
      value.manifest_sha256, SHA256, "manifest_sha256", "platform",
    ),
  });
}

function manifestDocument(value, domain) {
  exact(value, [
    "schema_version", "signature_purpose", "bundle_id", "direction", "workspace_id",
    "tenant_id", "deployment_id", "producer_generation", "version_vector", "entries",
    "entry_merkle_root", "key_envelope_sha256", "kms_key_id_sha256",
    "encryption_key_version", "created_at", "expires_at",
  ], "manifest", domain);
  if (value.schema_version !== "metahub.edge-offline-bundle/v1"
    || value.signature_purpose !== "metahub.edge-offline-bundle.manifest/v1") {
    invalidResponse(domain, "manifest_version");
  }
  responseArray(value.version_vector, DOMAINS.length, "version_vector", domain);
  responseArray(value.entries, 1024, "entries", domain);
  if (!value.version_vector.length || !value.entries.length) invalidResponse(domain, "manifest");
  const versions = value.version_vector.map((item) => domainVersion(item, domain));
  const entries = value.entries.map((item) => bundleEntry(item, domain));
  unique(versions.map((item) => item.domain), "version_vector", domain);
  unique(entries.map((item) => item.entry_id), "entries", domain);
  if (versions.some((item, index) => index > 0 && versions[index - 1].domain >= item.domain)
    || entries.some((item, index) => index > 0
      && `${entries[index - 1].domain}:${String(entries[index - 1].sequence).padStart(4, "0")}:${entries[index - 1].entry_id}`
        >= `${item.domain}:${String(item.sequence).padStart(4, "0")}:${item.entry_id}`)) {
    invalidResponse(domain, "manifest_order");
  }
  if (entries.some((item) => !versions.some((version) => version.domain === item.domain))) {
    invalidResponse(domain, "entry_domain");
  }
  const producerGeneration = responseInteger(
    value.producer_generation, 1, 2_147_483_647, "producer_generation", domain,
  );
  if (producerGeneration !== Math.max(...versions.map((item) => item.generation))) {
    invalidResponse(domain, "producer_generation");
  }
  responsePattern(value.entry_merkle_root, SHA256, "entry_merkle_root", domain);
  responsePattern(value.key_envelope_sha256, SHA256, "key_envelope_sha256", domain);
  responsePattern(value.kms_key_id_sha256, SHA256, "kms_key_id_sha256", domain);
  responseInteger(
    value.encryption_key_version, 1, 2_147_483_647, "encryption_key_version", domain,
  );
  const createdAt = timestamp(value.created_at, "created_at", domain);
  const expiresAt = timestamp(value.expires_at, "expires_at", domain);
  if (Date.parse(expiresAt) <= Date.parse(createdAt)) invalidResponse(domain, "expiry");
  return Object.freeze({
    bundle_id: responsePattern(value.bundle_id, IDS.bundle, "bundle_id", domain),
    direction: responseChoice(value.direction, DIRECTIONS, "direction", domain),
    workspace_id: responsePattern(value.workspace_id, IDS.workspace, "workspace_id", domain),
    tenant_id: responsePattern(value.tenant_id, TENANT, "tenant_id", domain),
    deployment_id: responsePattern(
      value.deployment_id, IDS.deployment, "deployment_id", domain,
    ),
    producer_generation: producerGeneration,
    domains: Object.freeze(versions.map((item) => item.domain)),
    entry_count: entries.length,
    created_at: createdAt,
    expires_at: expiresAt,
  });
}

function clientManifest(value, domain) {
  try {
    return manifestDocument(value, domain);
  } catch (error) {
    if (error?.code !== "edge_offline_bundle.response_invalid") throw error;
    invalidRequest(domain, "manifest");
  }
}

function domainVersion(value, domain) {
  exact(value, [
    "domain", "base_generation", "base_sha256", "generation", "value_sha256",
  ], "version", domain);
  const baseGeneration = responseInteger(
    value.base_generation, 0, 2_147_483_647, "base_generation", domain,
  );
  const baseSha256 = nullablePattern(value.base_sha256, SHA256, "base_sha256", domain);
  const generation = responseInteger(
    value.generation, 1, 2_147_483_647, "generation", domain,
  );
  const valueSha256 = responsePattern(value.value_sha256, SHA256, "value_sha256", domain);
  if ((baseGeneration === 0) !== (baseSha256 === null) || generation < baseGeneration
    || (generation === baseGeneration && valueSha256 !== baseSha256)) {
    invalidResponse(domain, "version");
  }
  return Object.freeze({
    domain: responseChoice(value.domain, DOMAINS, "domain", domain),
    base_generation: baseGeneration,
    base_sha256: baseSha256,
    generation,
    value_sha256: valueSha256,
  });
}

function bundleEntry(value, domain) {
  exact(value, [
    "entry_id", "domain", "sequence", "media_type", "plaintext_sha256",
    "ciphertext_sha256", "plaintext_size", "ciphertext_size", "nonce",
  ], "entry", domain);
  const plaintextSize = responseInteger(
    value.plaintext_size, 1, 67_108_864, "plaintext_size", domain,
  );
  const ciphertextSize = responseInteger(
    value.ciphertext_size, 17, 67_108_880, "ciphertext_size", domain,
  );
  responsePattern(value.nonce, NONCE, "nonce", domain);
  if (ciphertextSize !== plaintextSize + 16) invalidResponse(domain, "entry_size");
  return Object.freeze({
    entry_id: responsePattern(value.entry_id, IDS.entry, "entry_id", domain),
    domain: responseChoice(value.domain, DOMAINS, "domain", domain),
    sequence: responseInteger(value.sequence, 0, 1023, "sequence", domain),
    media_type: responsePattern(value.media_type, MEDIA_TYPE, "media_type", domain),
    plaintext_sha256: responsePattern(
      value.plaintext_sha256, SHA256, "plaintext_sha256", domain,
    ),
    ciphertext_sha256: responsePattern(
      value.ciphertext_sha256, SHA256, "ciphertext_sha256", domain,
    ),
    plaintext_size: plaintextSize,
    ciphertext_size: ciphertextSize,
  });
}

async function request(controlApiUrl, path, options, domain) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = domain === "workspace"
    ? readWorkspaceCsrfCookie() : readPlatformCsrfCookie();
  if (options.stepUpGrant) headers["X-Step-Up-Grant"] = options.stepUpGrant;
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
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
    throw apiError(domain, 0, "edge_offline_bundle.network_unavailable", "Service unavailable");
  }
  const payload = await readJson(response, domain);
  if (!response.ok) {
    const code = SAFE_CODE.test(String(payload?.code ?? ""))
      ? payload.code : "edge_offline_bundle.request_failed";
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
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(domain, "url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest(domain, "url");
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

function exact(value, keys, label, domain) {
  plain(value, label, domain);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) invalidResponse(domain, label);
}

function plain(value, label, domain) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse(domain, label);
}

function trace(value, domain) {
  return Object.freeze({
    request_id: responsePattern(value.request_id, CHAIN, "request_id", domain),
    trace_id: responsePattern(value.trace_id, CHAIN, "trace_id", domain),
  });
}

function responseArray(value, maximum, label, domain) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(domain, label);
}

function unique(values, label, domain) {
  if (values.length !== new Set(values).size) invalidResponse(domain, label);
}

function clientPattern(value, pattern, label, domain) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalidRequest(domain, label);
  return text;
}

function clientChoice(value, choices, label, domain) {
  if (!choices.includes(value)) invalidRequest(domain, label);
  return value;
}

function clientInteger(value, minimum, maximum, label, domain) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidRequest(domain, label);
  }
  return value;
}

function responsePattern(value, pattern, label, domain) {
  const text = typeof value === "string" ? value : "";
  if (!pattern.test(text)) invalidResponse(domain, label);
  return text;
}

function nullablePattern(value, pattern, label, domain) {
  return value === null ? null : responsePattern(value, pattern, label, domain);
}

function responseChoice(value, choices, label, domain) {
  if (!choices.includes(value)) invalidResponse(domain, label);
  return value;
}

function responseInteger(value, minimum, maximum, label, domain) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function nullableInteger(value, minimum, maximum, label, domain) {
  return value === null ? null : responseInteger(value, minimum, maximum, label, domain);
}

function responseBoolean(value, label, domain) {
  if (typeof value !== "boolean") invalidResponse(domain, label);
  return value;
}

function timestamp(value, label, domain) {
  const text = typeof value === "string" ? value : "";
  if (!text || text.length > 40 || !Number.isFinite(Date.parse(text))) {
    invalidResponse(domain, label);
  }
  return text;
}

function nullableTimestamp(value, label, domain) {
  return value === null ? null : timestamp(value, label, domain);
}

function safeText(value) {
  return typeof value === "string" && value.length <= 240 ? value : "Request failed";
}

function apiError(domain, status, code, message, requestId = "-") {
  if (domain === "platform") return new PlatformApiError({ status, code, message, requestId });
  return new AuthApiError({ status, code, message, requestId });
}

function invalidRequest(domain, label) {
  throw apiError(domain, 422, "edge_offline_bundle.request_invalid", `Invalid ${label}`);
}

function invalidResponse(domain, label) {
  throw apiError(domain, 502, "edge_offline_bundle.response_invalid", `Invalid ${label}`);
}
