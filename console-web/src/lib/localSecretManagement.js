/** PRD-123/125 customer-owned local Secret and support-bundle API client. */

const DEPLOYMENT_ID = /^edp_[0-9a-f]{32}$/u;
const REFERENCE_ID = /^lsr_[0-9a-f]{32}$/u;
const TEST_ID = /^lpt_[0-9a-f]{32}$/u;
const SUPPORT_BUNDLE_ID = /^esb_[0-9a-f]{32}$/u;
const OWNER_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const SAFE_LABEL = /^[A-Za-z0-9][A-Za-z0-9 ._:/()-]{0,79}$/u;
const SAFE_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u;
const CSRF = /^[A-Za-z0-9_-]{43}$/u;
const UTC_TIME = /^20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{3})?Z$/u;
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);
const MAX_RESPONSE_BYTES = 32_768;
const REFERENCE_KEYS = Object.freeze([
  "active_version", "capabilities", "deployment_id", "kind", "label",
  "last_test_outcome", "provider_connection_id", "reference_id", "revision",
  "rotated_at", "schema_version", "status", "tenant_id", "updated_at", "workspace_id",
]);
const SUPPORT_BUNDLE_COLLECTOR_CODES = Object.freeze([
  "runtime.version", "runtime.watermarks", "runtime.health", "runtime.errors",
  "runtime.logs",
]);

let localSession = null;

export class LocalSecretApiError extends Error {
  constructor({ status = 0, code = "local_secret.unavailable" } = {}) {
    super(code);
    this.name = "LocalSecretApiError";
    this.status = Number.isSafeInteger(status) ? status : 0;
    this.code = SAFE_CODE.test(String(code)) ? String(code) : "local_secret.unavailable";
  }
}

export async function createLocalAdminSession({
  localApiOrigin,
  deploymentId,
  accountId,
  tenantId,
  workspaceId,
  handoffCredential,
  signal,
} = {}) {
  const origin = localOrigin(localApiOrigin);
  const owner = Object.freeze({
    deploymentId: identifier(deploymentId, DEPLOYMENT_ID, "local_admin.request_invalid"),
    accountId: identifier(accountId, OWNER_ID, "local_admin.request_invalid"),
    tenantId: identifier(tenantId, OWNER_ID, "local_admin.request_invalid"),
    workspaceId: identifier(workspaceId, OWNER_ID, "local_admin.request_invalid"),
  });
  if (!(handoffCredential instanceof Uint8Array)) {
    throw new LocalSecretApiError({ status: 422, code: "local_admin.request_invalid" });
  }
  try {
    if (!validHandoff(handoffCredential)) {
      throw new LocalSecretApiError({ status: 422, code: "local_admin.request_invalid" });
    }
    const result = await localRequest(origin, "/local/admin/session-handoffs", {
      method: "POST",
      body: handoffCredential,
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Local-Account-ID": owner.accountId,
        "X-Local-Deployment-ID": owner.deploymentId,
        "X-Local-Tenant-ID": owner.tenantId,
        "X-Local-Workspace-ID": owner.workspaceId,
      },
      signal,
      expectedStatus: 201,
    });
    exact(result.payload, ["deployment_id", "expires_at"], "local_admin.response_invalid");
    const expiresAt = integer(
      result.payload.expires_at,
      1,
      Number.MAX_SAFE_INTEGER,
      "local_admin.response_invalid",
    );
    if (result.payload.deployment_id !== owner.deploymentId
      || expiresAt <= Math.floor(Date.now() / 1000)) {
      throw new LocalSecretApiError({ status: 502, code: "local_admin.response_invalid" });
    }
    const csrfToken = String(result.response.headers.get("X-CSRF-Token") ?? "");
    if (!CSRF.test(csrfToken)) {
      throw new LocalSecretApiError({ status: 502, code: "local_admin.response_invalid" });
    }
    localSession = Object.freeze({ origin, owner, expiresAt, csrfToken });
    return Object.freeze({
      local_api_origin: origin,
      deployment_id: owner.deploymentId,
      expires_at: expiresAt,
    });
  } finally {
    zeroBytes(handoffCredential);
  }
}

export function localAdminSessionSnapshot() {
  if (!localSession) return null;
  return Object.freeze({
    local_api_origin: localSession.origin,
    deployment_id: localSession.owner.deploymentId,
    expires_at: localSession.expiresAt,
  });
}

export function clearLocalAdminSession() {
  localSession = null;
}

export async function deleteLocalAdminSession({ localApiOrigin, signal } = {}) {
  const origin = localOrigin(localApiOrigin ?? localSession?.origin);
  const csrfToken = localSession?.origin === origin ? localSession.csrfToken : null;
  localSession = null;
  try {
    await localRequest(origin, "/local/admin/session", {
      method: "DELETE",
      headers: csrfToken ? { "X-CSRF-Token": csrfToken } : {},
      signal,
      expectEmpty: true,
      expectedStatus: 204,
    });
    return true;
  } catch {
    return false;
  }
}

export async function createLocalSecret({
  deploymentId,
  providerConnectionId,
  label,
  kind,
  secretBytes,
  signal,
} = {}) {
  const session = requireSession(deploymentId);
  return withSecretBytes(secretBytes, async (bytes) => {
    const result = await localRequest(session.origin, "/local/secrets", {
      method: "POST",
      body: bytes,
      headers: {
        ...sessionHeaders(session),
        "Content-Type": "application/octet-stream",
        "X-Local-Secret-Kind": choice(kind, [
          "api_key", "bearer_token", "basic_password", "oauth_client_secret",
        ]),
        "X-Local-Secret-Label": safeLabel(label),
        "X-Provider-Connection-ID": identifier(
          providerConnectionId,
          OWNER_ID,
          "local_secret.payload_invalid",
        ),
      },
      signal,
      expectedStatus: 201,
    });
    return validateReference(result.payload, session.owner);
  });
}

export async function rotateLocalSecret({
  deploymentId,
  referenceId,
  revision,
  secretBytes,
  signal,
} = {}) {
  const session = requireSession(deploymentId);
  const id = identifier(referenceId, REFERENCE_ID, "local_secret.payload_invalid");
  return withSecretBytes(secretBytes, async (bytes) => {
    const result = await localRequest(
      session.origin,
      `/local/secrets/${encodeURIComponent(id)}/rotate`,
      {
        method: "POST",
        body: bytes,
        headers: {
          ...sessionHeaders(session),
          "Content-Type": "application/octet-stream",
          "If-Match": String(integer(revision, 1, 2_147_483_647)),
        },
        signal,
        expectedStatus: 200,
      },
    );
    const reference = validateReference(result.payload, session.owner);
    if (reference.reference_id !== id) invalidResponse("local_secret.response_invalid");
    return reference;
  });
}

export async function deleteLocalSecret({
  deploymentId,
  referenceId,
  revision,
  signal,
} = {}) {
  const session = requireSession(deploymentId);
  const id = identifier(referenceId, REFERENCE_ID, "local_secret.payload_invalid");
  await localRequest(session.origin, `/local/secrets/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: {
      ...sessionHeaders(session),
      "If-Match": String(integer(revision, 1, 2_147_483_647)),
    },
    signal,
    expectEmpty: true,
    expectedStatus: 204,
  });
  return true;
}

export async function testLocalProviderConnection({
  deploymentId,
  providerConnectionId,
  signal,
} = {}) {
  const session = requireSession(deploymentId);
  const providerId = identifier(
    providerConnectionId,
    OWNER_ID,
    "local_secret.payload_invalid",
  );
  const result = await localRequest(
    session.origin,
    `/local/provider-connections/${encodeURIComponent(providerId)}/test`,
    {
      method: "POST",
      headers: sessionHeaders(session),
      signal,
      expectedStatus: 200,
    },
  );
  exact(result.payload, [
    "capabilities", "created_at", "latency_bucket", "outcome", "reference_id",
    "test_id", "version",
  ], "local_secret.response_invalid");
  return Object.freeze({
    test_id: identifier(result.payload.test_id, TEST_ID, "local_secret.response_invalid"),
    reference_id: identifier(
      result.payload.reference_id,
      REFERENCE_ID,
      "local_secret.response_invalid",
    ),
    version: integer(
      result.payload.version,
      1,
      2_147_483_647,
      "local_secret.response_invalid",
    ),
    outcome: choice(result.payload.outcome, providerOutcomes(), "local_secret.response_invalid"),
    capabilities: capabilities(result.payload.capabilities),
    latency_bucket: choice(
      result.payload.latency_bucket,
      ["lt_100ms", "100_499ms", "500_1999ms", "gte_2000ms"],
      "local_secret.response_invalid",
    ),
    created_at: utcTime(result.payload.created_at, "local_secret.response_invalid"),
  });
}

export async function previewLocalSupportBundle({
  deploymentId,
  collectorCodes = SUPPORT_BUNDLE_COLLECTOR_CODES,
  signal,
} = {}) {
  const session = requireSession(deploymentId);
  const codes = supportBundleCollectorCodes(collectorCodes, "local_support_bundle.request_invalid");
  const result = await localRequest(session.origin, "/local/support-bundles", {
    method: "POST",
    body: JSON.stringify({ collector_codes: codes }),
    headers: { ...sessionHeaders(session), "Content-Type": "application/json" },
    signal,
    expectedStatus: 201,
  });
  return validateLocalSupportBundle(result.payload, session.owner);
}

export async function approveLocalSupportBundle({
  deploymentId,
  bundleId,
  revision,
  signal,
} = {}) {
  const session = requireSession(deploymentId);
  const id = identifier(bundleId, SUPPORT_BUNDLE_ID, "local_support_bundle.request_invalid");
  const result = await localRequest(
    session.origin,
    `/local/support-bundles/${encodeURIComponent(id)}/approve`,
    {
      method: "POST",
      headers: {
        ...sessionHeaders(session),
        "If-Match": String(integer(
          revision,
          1,
          2_147_483_647,
          "local_support_bundle.request_invalid",
        )),
      },
      signal,
      expectedStatus: 200,
    },
  );
  const bundle = validateLocalSupportBundle(result.payload, session.owner);
  if (bundle.bundle_id !== id) invalidResponse("local_support_bundle.response_invalid");
  return bundle;
}

export async function cancelLocalSupportBundle({
  deploymentId,
  bundleId,
  revision,
  signal,
} = {}) {
  const session = requireSession(deploymentId);
  const id = identifier(bundleId, SUPPORT_BUNDLE_ID, "local_support_bundle.request_invalid");
  const result = await localRequest(
    session.origin,
    `/local/support-bundles/${encodeURIComponent(id)}`,
    {
      method: "DELETE",
      headers: {
        ...sessionHeaders(session),
        "If-Match": String(integer(
          revision,
          1,
          2_147_483_647,
          "local_support_bundle.request_invalid",
        )),
      },
      signal,
      expectedStatus: 200,
    },
  );
  const bundle = validateLocalSupportBundle(result.payload, session.owner);
  if (bundle.bundle_id !== id || bundle.status !== "cancelled") {
    invalidResponse("local_support_bundle.response_invalid");
  }
  return bundle;
}

async function localRequest(origin, path, options = {}) {
  let response;
  try {
    response = await fetch(new URL(path, `${origin}/`).toString(), {
      method: options.method ?? "GET",
      headers: { Accept: "application/json", ...(options.headers ?? {}) },
      body: options.body,
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new LocalSecretApiError({ status: 0, code: "local_secret.network_unavailable" });
  }
  validateCacheBoundary(response);
  const bytes = await readBoundedBody(response);
  if (options.expectEmpty) {
    if (!response.ok || response.status !== options.expectedStatus || bytes.length !== 0) {
      throw await responseError(response, bytes);
    }
    return Object.freeze({ payload: null, response });
  }
  const payload = parseJson(response, bytes);
  if (!response.ok) throw await responseError(response, bytes, payload);
  if (response.status !== options.expectedStatus) invalidResponse("local_secret.response_invalid");
  return Object.freeze({ payload, response });
}

async function readBoundedBody(response) {
  const length = response.headers.get("Content-Length");
  if (length !== null && (!/^(0|[1-9][0-9]{0,5})$/u.test(length)
    || Number(length) > MAX_RESPONSE_BYTES)) invalidResponse("local_secret.response_invalid");
  if (!response.body?.getReader) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.length > MAX_RESPONSE_BYTES) invalidResponse("local_secret.response_invalid");
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_RESPONSE_BYTES) invalidResponse("local_secret.response_invalid");
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function parseJson(response, bytes) {
  if (String(response.headers.get("Content-Type") ?? "").split(";", 1)[0].toLowerCase()
    !== "application/json") invalidResponse("local_secret.response_invalid");
  try {
    const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    if (!plain(value)) invalidResponse("local_secret.response_invalid");
    return value;
  } catch (error) {
    if (error instanceof LocalSecretApiError) throw error;
    invalidResponse("local_secret.response_invalid");
  }
}

async function responseError(response, bytes, parsed = null) {
  let payload = parsed;
  if (!payload && bytes.length) {
    try { payload = parseJson(response, bytes); } catch { payload = null; }
  }
  const code = SAFE_CODE.test(String(payload?.code ?? ""))
    ? String(payload.code)
    : "local_secret.request_failed";
  return new LocalSecretApiError({ status: Number(response.status) || 0, code });
}

function validateReference(value, owner) {
  if (!plain(value)) invalidResponse("local_secret.response_invalid");
  exact(value, REFERENCE_KEYS, "local_secret.response_invalid");
  const reference = Object.freeze({
    schema_version: choice(value.schema_version, ["1"], "local_secret.response_invalid"),
    reference_id: identifier(value.reference_id, REFERENCE_ID, "local_secret.response_invalid"),
    deployment_id: identifier(value.deployment_id, DEPLOYMENT_ID, "local_secret.response_invalid"),
    tenant_id: identifier(value.tenant_id, OWNER_ID, "local_secret.response_invalid"),
    workspace_id: identifier(value.workspace_id, OWNER_ID, "local_secret.response_invalid"),
    provider_connection_id: identifier(
      value.provider_connection_id,
      OWNER_ID,
      "local_secret.response_invalid",
    ),
    label: safeLabel(value.label, "local_secret.response_invalid"),
    kind: choice(value.kind, [
      "api_key", "bearer_token", "basic_password", "oauth_client_secret",
    ], "local_secret.response_invalid"),
    status: choice(value.status, [
      "active", "rotating", "degraded", "disabled",
    ], "local_secret.response_invalid"),
    revision: integer(value.revision, 1, 2_147_483_647, "local_secret.response_invalid"),
    active_version: value.active_version === null
      ? null : integer(
        value.active_version,
        1,
        2_147_483_647,
        "local_secret.response_invalid",
      ),
    last_test_outcome: value.last_test_outcome === null ? null : choice(
      value.last_test_outcome,
      providerOutcomes(),
      "local_secret.response_invalid",
    ),
    capabilities: capabilities(value.capabilities),
    rotated_at: value.rotated_at === null
      ? null : utcTime(value.rotated_at, "local_secret.response_invalid"),
    updated_at: utcTime(value.updated_at, "local_secret.response_invalid"),
  });
  if (reference.deployment_id !== owner.deploymentId
    || reference.tenant_id !== owner.tenantId
    || reference.workspace_id !== owner.workspaceId
    || (reference.status === "active" && reference.active_version === null)
    || (reference.status === "disabled" && reference.active_version !== null)
    || (reference.last_test_outcome === null && reference.capabilities.length)) {
    invalidResponse("local_secret.response_invalid");
  }
  return reference;
}

function validateLocalSupportBundle(value, owner) {
  if (!plain(value)) invalidResponse("local_support_bundle.response_invalid");
  exact(value, [
    "approved_at", "bundle_id", "cloud_object_reference", "created_at", "deployment_id",
    "expires_at", "failure_code", "manifest", "revision", "status", "uploaded_at",
  ], "local_support_bundle.response_invalid");
  const bundleId = identifier(
    value.bundle_id,
    SUPPORT_BUNDLE_ID,
    "local_support_bundle.response_invalid",
  );
  const deploymentId = identifier(
    value.deployment_id,
    DEPLOYMENT_ID,
    "local_support_bundle.response_invalid",
  );
  const manifest = validateSupportBundleManifest(value.manifest, bundleId, deploymentId);
  const status = choice(value.status, [
    "preview_ready", "uploading", "uploaded", "failed", "cancelled", "expired",
  ], "local_support_bundle.response_invalid");
  const cloudReference = value.cloud_object_reference === null
    ? null
    : identifier(
      value.cloud_object_reference,
      SUPPORT_BUNDLE_ID,
      "local_support_bundle.response_invalid",
    );
  const failureCode = value.failure_code === null
    ? null
    : safeCode(value.failure_code, "local_support_bundle.response_invalid");
  const approvedAt = value.approved_at === null
    ? null
    : utcTime(value.approved_at, "local_support_bundle.response_invalid");
  const uploadedAt = value.uploaded_at === null
    ? null
    : utcTime(value.uploaded_at, "local_support_bundle.response_invalid");
  const createdAt = utcTime(value.created_at, "local_support_bundle.response_invalid");
  const expiresAt = utcTime(value.expires_at, "local_support_bundle.response_invalid");
  if (deploymentId !== owner.deploymentId
    || manifest.created_at !== createdAt || manifest.expires_at !== expiresAt
    || (status === "preview_ready" && (
      value.revision !== 1 || cloudReference !== null || failureCode !== null
      || approvedAt !== null || uploadedAt !== null
    ))
    || (status === "uploaded" && (
      cloudReference !== bundleId || failureCode !== null || approvedAt === null
      || uploadedAt === null
    ))
    || (status === "failed") !== (failureCode !== null)
    || (!["uploaded", "failed", "uploading"].includes(status) && approvedAt !== null)
    || (status !== "uploaded" && cloudReference !== null)
    || (status !== "uploaded" && uploadedAt !== null)) {
    invalidResponse("local_support_bundle.response_invalid");
  }
  return Object.freeze({
    bundle_id: bundleId,
    deployment_id: deploymentId,
    status,
    revision: integer(
      value.revision,
      1,
      2_147_483_647,
      "local_support_bundle.response_invalid",
    ),
    manifest,
    cloud_object_reference: cloudReference,
    failure_code: failureCode,
    created_at: createdAt,
    expires_at: expiresAt,
    approved_at: approvedAt,
    uploaded_at: uploadedAt,
  });
}

function validateSupportBundleManifest(value, expectedBundleId, expectedDeploymentId) {
  if (!plain(value)) invalidResponse("local_support_bundle.response_invalid");
  exact(value, [
    "archive_sha256", "archive_size_bytes", "bundle_id", "created_at", "deployment_id",
    "entries", "expires_at", "scan_outcome", "scan_version", "schema_version",
    "total_uncompressed_bytes",
  ], "local_support_bundle.response_invalid");
  const entries = Array.isArray(value.entries)
    ? value.entries.map(validateSupportBundleEntry)
    : invalidResponse("local_support_bundle.response_invalid");
  if (!entries.length || entries.length > SUPPORT_BUNDLE_COLLECTOR_CODES.length
    || entries.reduce((total, entry) => total + entry.size_bytes, 0)
      !== value.total_uncompressed_bytes
    || !supportBundleCollectorCodes(
      entries.map((entry) => entry.collector_code),
      "local_support_bundle.response_invalid",
    )) {
    invalidResponse("local_support_bundle.response_invalid");
  }
  const bundleId = identifier(
    value.bundle_id,
    SUPPORT_BUNDLE_ID,
    "local_support_bundle.response_invalid",
  );
  const deploymentId = identifier(
    value.deployment_id,
    DEPLOYMENT_ID,
    "local_support_bundle.response_invalid",
  );
  if (bundleId !== expectedBundleId || deploymentId !== expectedDeploymentId) {
    invalidResponse("local_support_bundle.response_invalid");
  }
  return Object.freeze({
    schema_version: choice(
      value.schema_version,
      ["metahub.support-bundle/v1"],
      "local_support_bundle.response_invalid",
    ),
    bundle_id: bundleId,
    deployment_id: deploymentId,
    scan_version: choice(
      value.scan_version,
      ["support-bundle-scan-v1"],
      "local_support_bundle.response_invalid",
    ),
    scan_outcome: choice(
      value.scan_outcome,
      ["clean"],
      "local_support_bundle.response_invalid",
    ),
    entries: Object.freeze(entries),
    total_uncompressed_bytes: integer(
      value.total_uncompressed_bytes,
      1,
      100 * 1024 * 1024,
      "local_support_bundle.response_invalid",
    ),
    archive_size_bytes: integer(
      value.archive_size_bytes,
      1,
      100 * 1024 * 1024,
      "local_support_bundle.response_invalid",
    ),
    archive_sha256: sha256(value.archive_sha256, "local_support_bundle.response_invalid"),
    created_at: utcTime(value.created_at, "local_support_bundle.response_invalid"),
    expires_at: utcTime(value.expires_at, "local_support_bundle.response_invalid"),
  });
}

function validateSupportBundleEntry(value) {
  if (!plain(value)) invalidResponse("local_support_bundle.response_invalid");
  exact(value, [
    "classification", "collector_code", "media_type", "path", "sha256", "size_bytes",
  ], "local_support_bundle.response_invalid");
  const collectorCode = choice(
    value.collector_code,
    SUPPORT_BUNDLE_COLLECTOR_CODES,
    "local_support_bundle.response_invalid",
  );
  const expected = {
    "runtime.version": ["meta/version.json", "version", "application/json"],
    "runtime.watermarks": ["state/watermarks.json", "watermark", "application/json"],
    "runtime.health": ["health/summary.json", "health", "application/json"],
    "runtime.errors": ["errors/codes.json", "error_code", "application/json"],
    "runtime.logs": ["logs/events.ndjson", "log_summary", "application/x-ndjson"],
  }[collectorCode];
  if (value.path !== expected[0] || value.classification !== expected[1]
    || value.media_type !== expected[2]) {
    invalidResponse("local_support_bundle.response_invalid");
  }
  return Object.freeze({
    collector_code: collectorCode,
    path: value.path,
    classification: value.classification,
    media_type: value.media_type,
    size_bytes: integer(
      value.size_bytes,
      1,
      8 * 1024 * 1024,
      "local_support_bundle.response_invalid",
    ),
    sha256: sha256(value.sha256, "local_support_bundle.response_invalid"),
  });
}

function requireSession(deploymentId) {
  const id = identifier(deploymentId, DEPLOYMENT_ID, "local_admin.authentication_failed");
  if (!localSession || localSession.owner.deploymentId !== id
    || localSession.expiresAt <= Math.floor(Date.now() / 1000)) {
    localSession = null;
    throw new LocalSecretApiError({ status: 401, code: "local_admin.authentication_failed" });
  }
  return localSession;
}

async function withSecretBytes(value, callback) {
  if (!(value instanceof Uint8Array)) {
    throw new LocalSecretApiError({ status: 422, code: "local_secret.payload_invalid" });
  }
  try {
    if (!value.length || value.length > 65_536) {
      throw new LocalSecretApiError({ status: 422, code: "local_secret.payload_invalid" });
    }
    return await callback(value);
  } finally {
    zeroBytes(value);
  }
}

function sessionHeaders(session) { return { "X-CSRF-Token": session.csrfToken }; }
function validHandoff(value) {
  return value.length >= 32 && value.length <= 512
    && value.every((character) => character >= 33 && character <= 126);
}
function providerOutcomes() {
  return [
    "succeeded", "authentication_failed", "authorization_failed", "rate_limited",
    "timeout", "tls_failed", "network_unreachable", "provider_unavailable", "unsupported",
  ];
}
function capabilities(value) {
  if (!Array.isArray(value) || value.length > 3) invalidResponse("local_secret.response_invalid");
  const result = value.map((item) => choice(
    item,
    ["chat_completions", "embeddings", "models"],
    "local_secret.response_invalid",
  ));
  if (result.join("\0") !== [...new Set(result)].sort().join("\0")) {
    invalidResponse("local_secret.response_invalid");
  }
  return Object.freeze(result);
}
function supportBundleCollectorCodes(value, code) {
  if (!Array.isArray(value) || !value.length || value.length > SUPPORT_BUNDLE_COLLECTOR_CODES.length) {
    invalidByCode(code);
  }
  const result = value.map((item) => choice(item, SUPPORT_BUNDLE_COLLECTOR_CODES, code));
  if (result.length !== new Set(result).size || result.some((item, index) => (
    index > 0
    && SUPPORT_BUNDLE_COLLECTOR_CODES.indexOf(result[index - 1])
      >= SUPPORT_BUNDLE_COLLECTOR_CODES.indexOf(item)
  ))) invalidByCode(code);
  return Object.freeze(result);
}
function localOrigin(value) {
  let url;
  try { url = new URL(String(value ?? "")); } catch { invalidRequest("local_admin.request_invalid"); }
  const loopback = LOOPBACK.has(url.hostname.toLowerCase());
  if (!new Set(["http:", "https:"]).has(url.protocol) || url.username || url.password
    || !url.hostname || (url.pathname !== "/" && url.pathname !== "")
    || url.search || url.hash || (url.protocol === "http:" && !loopback)) {
    invalidRequest("local_admin.request_invalid");
  }
  return url.origin;
}
function identifier(value, pattern, code) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalidByCode(code);
  return text;
}
function safeLabel(value, code = "local_secret.payload_invalid") {
  const text = String(value ?? "");
  if (!SAFE_LABEL.test(text)) invalidByCode(code);
  return text;
}
function choice(value, allowed, code = "local_secret.payload_invalid") {
  if (!allowed.includes(value)) invalidByCode(code);
  return value;
}
function integer(value, minimum, maximum, code = "local_secret.payload_invalid") {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidByCode(code);
  }
  return value;
}
function utcTime(value, code) {
  const text = String(value ?? "");
  if (!UTC_TIME.test(text)) invalidByCode(code);
  return text;
}
function safeCode(value, code) {
  const text = String(value ?? "");
  if (!/^[a-z][a-z0-9_.-]{1,79}$/u.test(text)) invalidByCode(code);
  return text;
}
function sha256(value, code) {
  const text = String(value ?? "");
  if (!/^[0-9a-f]{64}$/u.test(text)) invalidByCode(code);
  return text;
}
function exact(value, keys, code) {
  const actual = plain(value) ? Object.keys(value).sort() : [];
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) invalidResponse(code);
}
function plain(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype);
}
function validateCacheBoundary(response) {
  const directives = String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim());
  if (!directives.includes("no-store") || !directives.includes("private")) {
    invalidResponse("local_secret.response_invalid");
  }
}
function invalidRequest(code) { throw new LocalSecretApiError({ status: 422, code }); }
function invalidResponse(code) { throw new LocalSecretApiError({ status: 502, code }); }
function invalidByCode(code) {
  if (String(code).endsWith(".response_invalid")) invalidResponse(code);
  invalidRequest(code);
}
function zeroBytes(value) { value.fill(0); }
