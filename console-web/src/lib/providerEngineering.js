/** Strict, credential-isolated clients for PRD-143 Provider Engineering. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const PATTERN = Object.freeze({
  adapterCode: /^[a-z0-9][a-z0-9._-]{1,63}$/u,
  adapterRelease: /^parl_[0-9a-f]{32}$/u,
  catalogRelease: /^pecr_[0-9a-f]{32}$/u,
  contractRun: /^(?:pacr_|acr_)[0-9a-f]{32}$/u,
  contractCase: /^(?:pacc_|acc_)[0-9a-f]{32}$/u,
  override: /^pem_[0-9a-f]{32}$/u,
  sdkRelease: /^sdr_[0-9a-f]{32}$/u,
  sha256: /^[0-9a-f]{64}$/u,
  safeReference: /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u,
  cursor: /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/u,
  opaque: /^[A-Za-z0-9_-]{43,128}$/u,
  idempotency: /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u,
  semver: /^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$/u,
  legacyRelease: /^[A-Za-z0-9._+-]{1,80}$/u,
});

const ADAPTER_STATUSES = Object.freeze([
  "legacy_baseline", "candidate", "testing", "review_required", "approved",
  "released", "deprecated", "revoked",
]);
const ADAPTER_ACTIONS = Object.freeze([
  "create_candidate", "run_contract", "review_failure", "release", "deprecate", "revoke",
]);
const CATALOG_ACTIONS = Object.freeze([
  "create_candidate", "submit_review", "security_approve", "release", "deprecate",
]);
const OVERRIDE_ACTIONS = Object.freeze(["create", "update", "enable", "disable", "archive"]);

export async function listPlatformProviderAdapters({
  controlApiUrl,
  filters = {},
  cursor = null,
  limit = 50,
  signal,
} = {}) {
  const query = platformAdapterQuery(filters, cursor, limit);
  const payload = await request(controlApiUrl, `/platform/provider-adapters?${query}`, {
    domain: "platform",
    signal,
  });
  exact(payload, ["adapters", "next_cursor", "limit", "permission_fingerprint", "request_id", "trace_id"]);
  const adapters = frozenArray(payload.adapters, 0, 100, "adapters", adapterInfo);
  integer(payload.limit, 1, 100, "limit");
  nullableCursor(payload.next_cursor);
  if (adapters.length > payload.limit) invalidPlatformResponse("pagination");
  return Object.freeze({
    adapters,
    next_cursor: payload.next_cursor,
    limit: payload.limit,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function listPlatformAdapterContracts({
  controlApiUrl,
  providerAdapterReleaseId,
  view = "runs",
  filters = {},
  runId = null,
  cursor = null,
  limit,
  signal,
} = {}) {
  const releaseId = requestPattern(providerAdapterReleaseId, PATTERN.adapterRelease, "adapter_release_id");
  const query = contractQuery(view, filters, runId, cursor, limit);
  const payload = await request(
    controlApiUrl,
    `/platform/provider-adapters/${encodeURIComponent(releaseId)}/contract-runs?${query}`,
    { domain: "platform", signal },
  );
  exact(payload, [
    "provider_adapter_release_id", "view", "runs", "cases", "next_cursor", "limit",
    "permission_fingerprint", "request_id", "trace_id",
  ]);
  if (payload.provider_adapter_release_id !== releaseId || payload.view !== view) {
    invalidPlatformResponse("contract_owner");
  }
  const runs = frozenArray(payload.runs, 0, 200, "runs", contractRunInfo);
  const cases = frozenArray(payload.cases, 0, 500, "cases", contractCaseInfo);
  if ((view === "runs" && cases.length) || (view === "cases" && runs.length)) {
    invalidPlatformResponse("contract_view");
  }
  integer(payload.limit, 1, 500, "limit");
  nullableCursor(payload.next_cursor);
  return Object.freeze({
    provider_adapter_release_id: releaseId,
    view,
    runs,
    cases,
    next_cursor: payload.next_cursor,
    limit: payload.limit,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function applyPlatformProviderAdapterCommand({
  controlApiUrl,
  adapterIdentifier,
  command,
  expectedRevision,
  idempotencyKey,
  stepUpGrant,
  signal,
} = {}) {
  const body = adapterCommand(command);
  const identifier = requestPattern(
    adapterIdentifier,
    body.action === "create_candidate" ? PATTERN.adapterCode : PATTERN.adapterRelease,
    "adapter_identifier",
  );
  const payload = await request(
    controlApiUrl,
    `/platform/provider-adapters/${encodeURIComponent(identifier)}/commands`,
    writeOptions("platform", body, expectedRevision, idempotencyKey, stepUpGrant, signal),
  );
  exact(payload, [
    "adapter", "command_id", "contract_run_id", "idempotent_replay",
    "permission_fingerprint", "request_id", "trace_id",
  ]);
  const adapter = adapterInfo(payload.adapter);
  if (body.action !== "create_candidate" && adapter.provider_adapter_release_id !== identifier) {
    invalidPlatformResponse("adapter_owner");
  }
  nullablePattern(payload.contract_run_id, PATTERN.contractRun, "contract_run_id");
  boolean(payload.idempotent_replay, "idempotent_replay");
  return Object.freeze({
    adapter,
    command_id: safeString(payload.command_id, 1, 128, "command_id"),
    contract_run_id: payload.contract_run_id,
    idempotent_replay: payload.idempotent_replay,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function applyPlatformErrorCatalogCommand({
  controlApiUrl,
  command,
  expectedRevision,
  idempotencyKey,
  stepUpGrant,
  signal,
} = {}) {
  const body = catalogCommand(command);
  const payload = await request(
    controlApiUrl,
    "/platform/provider-error-catalogs/releases",
    writeOptions("platform", body, expectedRevision, idempotencyKey, stepUpGrant, signal),
  );
  exact(payload, [
    "catalog", "command_id", "idempotent_replay", "permission_fingerprint",
    "request_id", "trace_id",
  ]);
  const catalog = catalogInfo(payload.catalog);
  if (body.error_catalog_release_id
    && catalog.error_catalog_release_id !== body.error_catalog_release_id) {
    invalidPlatformResponse("catalog_owner");
  }
  boolean(payload.idempotent_replay, "idempotent_replay");
  return Object.freeze({
    catalog,
    command_id: safeString(payload.command_id, 1, 128, "command_id"),
    idempotent_replay: payload.idempotent_replay,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function listWorkspaceProviderErrorOverrides({
  controlApiUrl,
  filters = {},
  cursor = null,
  limit = 50,
  signal,
} = {}) {
  const query = workspaceOverrideQuery(filters, cursor, limit);
  const payload = await request(
    controlApiUrl,
    `/workspace/provider-error-overrides?${query}`,
    { domain: "workspace", signal },
  );
  exact(payload, [
    "overrides", "head", "compatibility", "next_cursor", "limit", "permission_revision",
    "permission_fingerprint", "request_id", "trace_id",
  ], invalidWorkspaceResponse);
  const overrides = frozenArray(
    payload.overrides, 0, 100, "overrides", overrideInfo, invalidWorkspaceResponse,
  );
  const head = overrideHead(payload.head);
  const compatibility = payload.compatibility === null ? null : compatibilityInfo(payload.compatibility);
  integer(payload.limit, 1, 100, "limit", invalidWorkspaceResponse);
  integer(payload.permission_revision, 1, Number.MAX_SAFE_INTEGER, "permission_revision", invalidWorkspaceResponse);
  nullableCursor(payload.next_cursor, invalidWorkspaceResponse);
  if (filters.provider_id && compatibility?.provider_id !== filters.provider_id) {
    invalidWorkspaceResponse("compatibility_owner");
  }
  return Object.freeze({
    overrides,
    head,
    compatibility,
    next_cursor: payload.next_cursor,
    limit: payload.limit,
    permission_revision: payload.permission_revision,
    permission_fingerprint: workspaceHash(payload.permission_fingerprint, "permission_fingerprint"),
    ...workspaceTrace(payload),
  });
}

export async function applyWorkspaceProviderErrorOverrideCommand({
  controlApiUrl,
  command,
  expectedHeadRevision,
  idempotencyKey,
  stepUpGrant,
  signal,
} = {}) {
  const body = workspaceCommand(command);
  const payload = await request(
    controlApiUrl,
    "/workspace/provider-error-overrides",
    writeOptions(
      "workspace", body, expectedHeadRevision, idempotencyKey, stepUpGrant, signal,
    ),
  );
  exact(payload, [
    "override", "head", "compatibility", "command_id", "changed", "idempotent_replay",
    "permission_revision", "permission_fingerprint", "request_id", "trace_id",
  ], invalidWorkspaceResponse);
  const override = overrideInfo(payload.override);
  const compatibility = compatibilityInfo(payload.compatibility);
  if (override.provider_id !== compatibility.provider_id) invalidWorkspaceResponse("override_owner");
  boolean(payload.changed, "changed", invalidWorkspaceResponse);
  boolean(payload.idempotent_replay, "idempotent_replay", invalidWorkspaceResponse);
  integer(payload.permission_revision, 1, Number.MAX_SAFE_INTEGER, "permission_revision", invalidWorkspaceResponse);
  return Object.freeze({
    override,
    head: overrideHead(payload.head),
    compatibility,
    command_id: workspaceString(payload.command_id, 1, 128, "command_id"),
    changed: payload.changed,
    idempotent_replay: payload.idempotent_replay,
    permission_revision: payload.permission_revision,
    permission_fingerprint: workspaceHash(payload.permission_fingerprint, "permission_fingerprint"),
    ...workspaceTrace(payload),
  });
}

function platformAdapterQuery(filters, cursor, limit) {
  requestPlain(filters, "filters");
  const allowed = new Set(["adapter", "status", "protocol", "release_generation", "compatibility"]);
  const query = new URLSearchParams();
  for (const [key, raw] of Object.entries(filters)) {
    if (!allowed.has(key) || raw === "" || raw === null || raw === undefined) {
      if (!allowed.has(key)) invalidPlatformRequest(`filter.${key}`);
      continue;
    }
    if (key === "adapter") requestPattern(raw, /^(?:[a-z0-9][a-z0-9._-]{1,63}|parl_[0-9a-f]{32})$/u, key);
    if (key === "status") requestChoice(raw, ADAPTER_STATUSES, key);
    if (key === "protocol") requestChoice(raw, ["openai_compatible", "anthropic_compatible", "generic_http", "legacy_catalog"], key);
    if (key === "release_generation") requestInteger(raw, 0, 2_147_483_647, key);
    if (key === "compatibility") requestPattern(raw, PATTERN.sha256, key);
    query.set(key, String(raw));
  }
  addCursorAndLimit(query, cursor, limit, 100, "platform");
  return query.toString();
}

function contractQuery(view, filters, runId, cursor, limit) {
  requestChoice(view, ["runs", "cases"], "view");
  requestPlain(filters, "filters");
  const query = new URLSearchParams({ view });
  if (view === "cases") {
    if (Object.keys(filters).length) invalidPlatformRequest("case_filters");
    query.set("run_id", requestPattern(runId, PATTERN.contractRun, "run_id"));
    addCursorAndLimit(query, cursor, limit ?? 100, 500, "platform");
    return query.toString();
  }
  if (runId !== null && runId !== undefined && runId !== "") invalidPlatformRequest("run_id");
  const choices = Object.freeze({
    scope: ["platform_release_ci", "legacy_tenant_merge_gate", "legacy_tenant_probe"],
    status: ["queued", "running", "passed", "failed", "cancelled"],
    suite: ["merge_gate", "probe", "release_ci"],
    mode: ["mock", "gateway_probe", "isolated_runner"],
  });
  for (const [key, raw] of Object.entries(filters)) {
    if (raw === "" || raw === null || raw === undefined) continue;
    if (Object.hasOwn(choices, key)) requestChoice(raw, choices[key], key);
    else if (["from", "to"].includes(key)) requestTimestamp(raw, key);
    else invalidPlatformRequest(`filter.${key}`);
    query.set(key, String(raw));
  }
  addCursorAndLimit(query, cursor, limit ?? 50, 200, "platform");
  return query.toString();
}

function workspaceOverrideQuery(filters, cursor, limit) {
  requestPlain(filters, "filters", invalidWorkspaceRequest);
  const allowed = new Set(["provider_id", "override_id", "source", "status", "output_error_code"]);
  const query = new URLSearchParams();
  for (const [key, raw] of Object.entries(filters)) {
    if (!allowed.has(key)) invalidWorkspaceRequest(`filter.${key}`);
    if (raw === "" || raw === null || raw === undefined) continue;
    if (key === "provider_id") workspaceRequestPattern(raw, PATTERN.safeReference, key);
    if (key === "override_id") workspaceRequestPattern(raw, PATTERN.override, key);
    if (key === "source") workspaceRequestChoice(raw, ["workspace"], key);
    if (key === "status") workspaceRequestChoice(raw, ["active", "disabled", "archived"], key);
    if (key === "output_error_code") workspaceRequestPattern(raw, PATTERN.safeReference, key);
    query.set(key, String(raw));
  }
  if (filters.override_id && Object.keys(filters).some((key) => key !== "override_id")) {
    invalidWorkspaceRequest("override_filter");
  }
  addCursorAndLimit(query, cursor, limit, 100, "workspace");
  return query.toString();
}

function adapterCommand(value) {
  requestPlain(value, "command");
  const action = requestChoice(value.action, ADAPTER_ACTIONS, "action");
  const keys = action === "create_candidate"
    ? ["action", "sdk_release_id", "release_version", "protocol_family", "spec_manifest_version", "spec_manifest_sha256", "compatibility_sha256", "required_runtime_version", "required_api_version"]
    : action === "review_failure"
      ? ["action", "contract_run_id", "failure_disposition"]
      : ["action"];
  requestExact(value, keys);
  if (action === "create_candidate") {
    requestPattern(value.sdk_release_id, PATTERN.sdkRelease, "sdk_release_id");
    requestPattern(value.release_version, PATTERN.semver, "release_version");
    requestChoice(value.protocol_family, ["openai_compatible", "anthropic_compatible", "generic_http"], "protocol_family");
    requestString(value.spec_manifest_version, 1, 80, "spec_manifest_version");
    requestPattern(value.spec_manifest_sha256, PATTERN.sha256, "spec_manifest_sha256");
    requestPattern(value.compatibility_sha256, PATTERN.sha256, "compatibility_sha256");
    requestString(value.required_runtime_version, 1, 80, "required_runtime_version");
    requestPattern(value.required_api_version, PATTERN.semver, "required_api_version");
  }
  if (action === "review_failure") {
    requestPattern(value.contract_run_id, PATTERN.contractRun, "contract_run_id");
    requestChoice(value.failure_disposition, ["approve_remediation", "reject"], "failure_disposition");
  }
  return Object.freeze({ ...value });
}

function catalogCommand(value) {
  requestPlain(value, "command");
  const action = requestChoice(value.action, CATALOG_ACTIONS, "action");
  const keys = action === "create_candidate"
    ? ["action", "builtin_catalog_version", "sdk_release_id", "signed_candidate_id"]
    : ["action", "error_catalog_release_id", "expected_revision"];
  requestExact(value, keys);
  if (action === "create_candidate") {
    requestString(value.builtin_catalog_version, 1, 80, "builtin_catalog_version");
    requestPattern(value.sdk_release_id, PATTERN.sdkRelease, "sdk_release_id");
    requestPattern(value.signed_candidate_id, PATTERN.safeReference, "signed_candidate_id");
  } else {
    requestPattern(value.error_catalog_release_id, PATTERN.catalogRelease, "error_catalog_release_id");
    requestInteger(value.expected_revision, 1, Number.MAX_SAFE_INTEGER, "expected_revision");
  }
  return Object.freeze({ ...value });
}

function workspaceCommand(value) {
  requestPlain(value, "command", invalidWorkspaceRequest);
  const action = workspaceRequestChoice(value.action, OVERRIDE_ACTIONS, "action");
  const editable = ["provider_id", "priority", "http_statuses", "http_status_class", "error_field", "field_operator", "field_pattern", "output_error_code"];
  const keys = action === "create"
    ? ["action", ...editable]
    : action === "update"
      ? Object.keys(value)
      : ["action", "provider_error_override_id", "expected_version"];
  requestExact(value, keys, invalidWorkspaceRequest);
  if (action === "update") {
    if (!Object.hasOwn(value, "provider_error_override_id") || !Object.hasOwn(value, "expected_version")
      || !editable.some((key) => Object.hasOwn(value, key))) invalidWorkspaceRequest("update_shape");
    if (Object.keys(value).some((key) => !new Set(["action", "provider_error_override_id", "expected_version", ...editable]).has(key))) {
      invalidWorkspaceRequest("update_shape");
    }
  }
  if (action !== "create") {
    workspaceRequestPattern(value.provider_error_override_id, PATTERN.override, "override_id");
    workspaceRequestInteger(value.expected_version, 1, Number.MAX_SAFE_INTEGER, "expected_version");
  }
  for (const key of editable) {
    if (!Object.hasOwn(value, key)) continue;
    if (key === "provider_id") workspaceRequestPattern(value[key], PATTERN.safeReference, key);
    if (key === "priority") workspaceRequestInteger(value[key], 1, 1000, key);
    if (key === "http_statuses") {
      if (!Array.isArray(value[key]) || value[key].length > 32
        || value[key].some((item) => !Number.isSafeInteger(item) || item < 100 || item > 599)
        || new Set(value[key]).size !== value[key].length) invalidWorkspaceRequest(key);
    }
    if (key === "http_status_class" && value[key] !== null) workspaceRequestChoice(value[key], ["4xx"], key);
    if (key === "error_field" && value[key] !== null) workspaceRequestChoice(value[key], ["error.code", "error.type", "code", "type"], key);
    if (key === "field_operator" && value[key] !== null) workspaceRequestChoice(value[key], ["equals", "contains"], key);
    if (key === "field_pattern" && value[key] !== null) workspaceRequestString(value[key], 1, 240, key);
    if (key === "output_error_code") workspaceRequestPattern(value[key], PATTERN.safeReference, key);
  }
  return Object.freeze({ ...value, ...(value.http_statuses ? { http_statuses: [...value.http_statuses] } : {}) });
}

function writeOptions(domain, body, revision, idempotencyKey, stepUpGrant, signal) {
  const fail = domain === "platform" ? invalidPlatformRequest : invalidWorkspaceRequest;
  const checkedRevision = requestInteger(revision, 0, Number.MAX_SAFE_INTEGER, "revision", fail);
  return {
    domain,
    method: domain === "platform" ? "POST" : "PATCH",
    body,
    headers: {
      "X-CSRF-Token": domain === "platform" ? readPlatformCsrfCookie() : readWorkspaceCsrfCookie(),
      "If-Match": `"${checkedRevision}"`,
      "Idempotency-Key": requestPattern(idempotencyKey, PATTERN.idempotency, "idempotency_key", fail),
      "X-Step-Up-Grant": requestPattern(stepUpGrant, PATTERN.opaque, "step_up_grant", fail),
    },
    signal,
  };
}

async function request(controlApiUrl, path, options) {
  const domain = options.domain;
  const fail = domain === "platform" ? invalidPlatformRequest : invalidWorkspaceRequest;
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { fail("control_api_url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    fail("control_api_url");
  }
  let response;
  try {
    response = await fetch(new URL(path, base).toString(), {
      method: options.method ?? "GET",
      headers: {
        Accept: "application/json",
        ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(options.headers ?? {}),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    if (domain === "platform") {
      throw new PlatformApiError({ code: "platform.network_unavailable", message: "暂时无法连接 Provider Engineering 服务。" });
    }
    throw new AuthApiError({ code: "workspace_session.unavailable", message: "暂时无法连接 Provider 兼容服务。" });
  }
  const payload = await responseJson(response, domain);
  if (!response.ok) {
    const input = {
      status: Number(response.status) || 0,
      code: safeErrorText(payload.code, domain === "platform" ? "platform.request_failed" : "error_map.unavailable"),
      message: safeErrorText(payload.message, "Provider Engineering 请求未完成。"),
      requestId: safeErrorText(payload.request_id, response.headers?.get?.("X-Request-ID") ?? "-"),
      traceId: safeErrorText(payload.trace_id, response.headers?.get?.("X-Trace-ID") ?? "-"),
    };
    throw domain === "platform" ? new PlatformApiError(input) : new AuthApiError(input);
  }
  return payload;
}

async function responseJson(response, domain) {
  try {
    const payload = await response.json();
    if (!payload || Object.getPrototypeOf(payload) !== Object.prototype) throw new Error("shape");
    return payload;
  } catch {
    if (domain === "platform") invalidPlatformResponse("json");
    invalidWorkspaceResponse("json");
  }
}

function adapterInfo(value) {
  exact(value, [
    "provider_adapter_release_id", "adapter_code", "release_version", "protocol_family",
    "sdk_release_id", "artifact_sha256", "sbom_sha256", "provenance_sha256",
    "signature_sha256", "spec_manifest_version", "spec_manifest_sha256",
    "compatibility_sha256", "required_runtime_version", "required_api_version", "status",
    "revision", "release_generation", "is_head", "readiness", "contract", "actions",
    "created_at", "updated_at", "released_at",
  ]);
  pattern(value.provider_adapter_release_id, PATTERN.adapterRelease, "adapter_release_id");
  pattern(value.adapter_code, PATTERN.adapterCode, "adapter_code");
  const protocolFamily = choice(value.protocol_family, ["openai_compatible", "anthropic_compatible", "generic_http", "legacy_catalog"], "protocol_family");
  const adapterStatus = choice(value.status, ADAPTER_STATUSES, "status");
  pattern(
    value.release_version,
    protocolFamily === "legacy_catalog" && adapterStatus === "legacy_baseline"
      ? PATTERN.legacyRelease
      : PATTERN.semver,
    "release_version",
  );
  nullablePattern(value.sdk_release_id, PATTERN.sdkRelease, "sdk_release_id");
  for (const key of ["artifact_sha256", "sbom_sha256", "provenance_sha256", "signature_sha256", "spec_manifest_sha256", "compatibility_sha256"]) hash(value[key], key);
  safeString(value.spec_manifest_version, 1, 80, "spec_manifest_version");
  safeString(value.required_runtime_version, 1, 80, "required_runtime_version");
  safeString(value.required_api_version, 1, 80, "required_api_version");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  integer(value.release_generation, 0, Number.MAX_SAFE_INTEGER, "release_generation");
  boolean(value.is_head, "is_head");
  choice(value.readiness, ["legacy", "candidate", "testing", "review", "ready", "released", "deprecated", "revoked"], "readiness");
  contractAggregate(value.contract);
  actionList(value.actions, ADAPTER_ACTIONS.filter((item) => item !== "create_candidate"), "actions");
  timestamp(value.created_at, "created_at");
  timestamp(value.updated_at, "updated_at");
  nullableTimestamp(value.released_at, "released_at");
  return Object.freeze({ ...value, contract: contractAggregate(value.contract), actions: Object.freeze([...value.actions]) });
}

function contractAggregate(value) {
  exact(value, ["queued", "running", "passed", "failed", "qualifying_run_id"]);
  for (const key of ["queued", "running", "passed", "failed"]) integer(value[key], 0, Number.MAX_SAFE_INTEGER, key);
  nullablePattern(value.qualifying_run_id, PATTERN.contractRun, "qualifying_run_id");
  return Object.freeze({ ...value });
}

function contractRunInfo(value) {
  exact(value, [
    "contract_run_id", "scope", "provider_adapter_release_id", "adapter_code",
    "schema_version", "runner_version", "suite_kind", "mode", "source_revision_sha256",
    "artifact_sha256", "catalog_sha256", "status", "coverage_complete", "total_cases",
    "passed_cases", "failed_cases", "failure_codes", "report_sha256", "attestation_sha256",
    "qualifies_release", "legacy", "generation", "started_at", "completed_at", "created_at",
  ]);
  pattern(value.contract_run_id, PATTERN.contractRun, "contract_run_id");
  choice(value.scope, ["platform_release_ci", "legacy_tenant_merge_gate", "legacy_tenant_probe"], "scope");
  nullablePattern(value.provider_adapter_release_id, PATTERN.adapterRelease, "adapter_release_id");
  pattern(value.adapter_code, PATTERN.adapterCode, "adapter_code");
  safeString(value.schema_version, 1, 80, "schema_version");
  safeString(value.runner_version, 1, 80, "runner_version");
  choice(value.suite_kind, ["merge_gate", "probe", "release_ci"], "suite_kind");
  choice(value.mode, ["mock", "gateway_probe", "isolated_runner"], "mode");
  for (const key of ["source_revision_sha256", "artifact_sha256", "catalog_sha256", "report_sha256", "attestation_sha256"]) hash(value[key], key);
  choice(value.status, ["queued", "running", "passed", "failed", "cancelled"], "status");
  boolean(value.coverage_complete, "coverage_complete");
  for (const key of ["total_cases", "passed_cases", "failed_cases"]) integer(value[key], 0, 100000, key);
  actionList(value.failure_codes, null, "failure_codes");
  boolean(value.qualifies_release, "qualifies_release");
  boolean(value.legacy, "legacy");
  integer(value.generation, 1, Number.MAX_SAFE_INTEGER, "generation");
  nullableTimestamp(value.started_at, "started_at");
  nullableTimestamp(value.completed_at, "completed_at");
  timestamp(value.created_at, "created_at");
  return Object.freeze({ ...value, failure_codes: Object.freeze([...value.failure_codes]) });
}

function contractCaseInfo(value) {
  exact(value, [
    "contract_case_id", "contract_run_id", "ordinal", "case_key_sha256", "case_kind",
    "variant", "provider_code", "adapter_code", "status", "failure_code",
    "duration_bucket", "evidence_sha256", "created_at",
  ]);
  pattern(value.contract_case_id, PATTERN.contractCase, "contract_case_id");
  pattern(value.contract_run_id, PATTERN.contractRun, "contract_run_id");
  integer(value.ordinal, 1, 100000, "ordinal");
  hash(value.case_key_sha256, "case_key_sha256");
  choice(value.case_kind, ["request", "non_stream", "stream", "error", "usage"], "case_kind");
  safeString(value.variant, 1, 120, "variant");
  safeString(value.provider_code, 1, 80, "provider_code");
  pattern(value.adapter_code, PATTERN.adapterCode, "adapter_code");
  choice(value.status, ["passed", "failed", "skipped"], "status");
  nullableString(value.failure_code, 1, 80, "failure_code");
  choice(value.duration_bucket, ["lt_10ms", "lt_100ms", "lt_1s", "lt_10s", "gte_10s"], "duration_bucket");
  hash(value.evidence_sha256, "evidence_sha256");
  timestamp(value.created_at, "created_at");
  return Object.freeze({ ...value });
}

function catalogInfo(value) {
  exact(value, [
    "error_catalog_release_id", "builtin_catalog_version", "catalog_sha256",
    "signed_candidate_id", "sdk_release_id", "artifact_sha256", "rule_count",
    "compatibility_sha256", "status", "revision", "release_generation", "is_head",
    "actions", "created_at", "updated_at", "released_at",
  ]);
  pattern(value.error_catalog_release_id, PATTERN.catalogRelease, "catalog_release_id");
  safeString(value.builtin_catalog_version, 1, 80, "builtin_catalog_version");
  hash(value.catalog_sha256, "catalog_sha256");
  nullablePattern(value.signed_candidate_id, PATTERN.safeReference, "signed_candidate_id");
  nullablePattern(value.sdk_release_id, PATTERN.sdkRelease, "sdk_release_id");
  hash(value.artifact_sha256, "artifact_sha256");
  integer(value.rule_count, 1, 100000, "rule_count");
  hash(value.compatibility_sha256, "compatibility_sha256");
  choice(value.status, ["legacy_reconciled", "candidate", "security_review", "approved", "released", "deprecated"], "status");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  integer(value.release_generation, 0, Number.MAX_SAFE_INTEGER, "release_generation");
  boolean(value.is_head, "is_head");
  actionList(value.actions, CATALOG_ACTIONS.filter((item) => item !== "create_candidate"), "actions");
  timestamp(value.created_at, "created_at");
  timestamp(value.updated_at, "updated_at");
  nullableTimestamp(value.released_at, "released_at");
  return Object.freeze({ ...value, actions: Object.freeze([...value.actions]) });
}

function overrideInfo(value) {
  exact(value, [
    "provider_error_override_id", "provider_id", "provider_code", "provider_name", "priority",
    "http_statuses", "http_status_class", "error_field", "field_operator", "field_pattern",
    "output_error_code", "status", "version", "revision", "pattern_sha256", "content_sha256",
    "updated_at", "actions",
  ], invalidWorkspaceResponse);
  workspacePattern(value.provider_error_override_id, PATTERN.override, "override_id");
  workspaceNullablePattern(value.provider_id, PATTERN.safeReference, "provider_id");
  workspaceNullableString(value.provider_code, 1, 80, "provider_code");
  workspaceNullableString(value.provider_name, 1, 160, "provider_name");
  integer(value.priority, 1, 1000, "priority", invalidWorkspaceResponse);
  if (!Array.isArray(value.http_statuses) || value.http_statuses.length > 32
    || value.http_statuses.some((item) => !Number.isSafeInteger(item) || item < 100 || item > 599)) invalidWorkspaceResponse("http_statuses");
  if (value.http_status_class !== null) workspaceChoice(value.http_status_class, ["4xx"], "http_status_class");
  if (value.error_field !== null) workspaceChoice(value.error_field, ["error.code", "error.type", "code", "type"], "error_field");
  if (value.field_operator !== null) workspaceChoice(value.field_operator, ["equals", "contains"], "field_operator");
  workspaceNullableString(value.field_pattern, 1, 240, "field_pattern");
  workspacePattern(value.output_error_code, PATTERN.safeReference, "output_error_code");
  workspaceChoice(value.status, ["active", "disabled", "archived"], "status");
  integer(value.version, 1, Number.MAX_SAFE_INTEGER, "version", invalidWorkspaceResponse);
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision", invalidWorkspaceResponse);
  workspaceHash(value.pattern_sha256, "pattern_sha256");
  workspaceHash(value.content_sha256, "content_sha256");
  workspaceTimestamp(value.updated_at, "updated_at");
  workspaceActionList(value.actions, OVERRIDE_ACTIONS.filter((item) => item !== "create"), "actions");
  return Object.freeze({ ...value, http_statuses: Object.freeze([...value.http_statuses]), actions: Object.freeze([...value.actions]) });
}

function overrideHead(value) {
  exact(value, ["revision", "content_sha256", "builtin_catalog_version", "updated_at"], invalidWorkspaceResponse);
  integer(value.revision, 0, Number.MAX_SAFE_INTEGER, "revision", invalidWorkspaceResponse);
  workspaceHash(value.content_sha256, "content_sha256");
  workspaceString(value.builtin_catalog_version, 1, 80, "builtin_catalog_version");
  if (value.updated_at !== null) workspaceTimestamp(value.updated_at, "updated_at");
  return Object.freeze({ ...value });
}

function compatibilityInfo(value) {
  exact(value, [
    "provider_id", "provider_code", "provider_catalog_version", "provider_adapter_release_id",
    "builtin_error_catalog_release_id", "override_head_revision", "compatibility_status",
    "reason_codes", "health_status", "projected_at",
  ], invalidWorkspaceResponse);
  workspacePattern(value.provider_id, PATTERN.safeReference, "provider_id");
  workspaceString(value.provider_code, 1, 80, "provider_code");
  workspaceNullableString(value.provider_catalog_version, 1, 80, "provider_catalog_version");
  workspaceNullablePattern(value.provider_adapter_release_id, PATTERN.adapterRelease, "adapter_release_id");
  workspaceNullablePattern(value.builtin_error_catalog_release_id, PATTERN.catalogRelease, "catalog_release_id");
  integer(value.override_head_revision, 0, Number.MAX_SAFE_INTEGER, "override_head_revision", invalidWorkspaceResponse);
  workspaceChoice(value.compatibility_status, ["compatible", "warning", "blocked", "unknown"], "compatibility_status");
  workspaceActionList(value.reason_codes, null, "reason_codes");
  workspaceChoice(value.health_status, ["healthy", "degraded", "unavailable", "unknown"], "health_status");
  workspaceTimestamp(value.projected_at, "projected_at");
  return Object.freeze({ ...value, reason_codes: Object.freeze([...value.reason_codes]) });
}

function addCursorAndLimit(query, cursor, limit, maximum, domain) {
  const fail = domain === "platform" ? invalidPlatformRequest : invalidWorkspaceRequest;
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    query.set("cursor", requestPattern(cursor, PATTERN.cursor, "cursor", fail));
  }
  query.set("limit", String(requestInteger(limit, 1, maximum, "limit", fail)));
}

function actionList(value, choices, label) {
  if (!Array.isArray(value) || value.length > 32) invalidPlatformResponse(label);
  value.forEach((item) => choices ? choice(item, choices, label) : safeString(item, 1, 80, label));
  if (new Set(value).size !== value.length) invalidPlatformResponse(label);
  return value;
}

function workspaceActionList(value, choices, label) {
  if (!Array.isArray(value) || value.length > 32) invalidWorkspaceResponse(label);
  value.forEach((item) => choices ? workspaceChoice(item, choices, label) : workspaceString(item, 1, 80, label));
  if (new Set(value).size !== value.length) invalidWorkspaceResponse(label);
  return value;
}

function exact(value, keys, fail = invalidPlatformResponse) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) fail("shape");
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) fail("shape");
  return value;
}

function frozenArray(value, minimum, maximum, label, mapper, fail = invalidPlatformResponse) {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) fail(label);
  return Object.freeze(value.map(mapper));
}

function safeString(value, minimum, maximum, label, fail = invalidPlatformResponse) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum || /[\u0000-\u001f\u007f]/u.test(value)) fail(label);
  return value;
}
function nullableString(value, minimum, maximum, label) { return value === null ? null : safeString(value, minimum, maximum, label); }
function workspaceString(value, minimum, maximum, label) { return safeString(value, minimum, maximum, label, invalidWorkspaceResponse); }
function workspaceNullableString(value, minimum, maximum, label) { return value === null ? null : workspaceString(value, minimum, maximum, label); }
function pattern(value, expression, label) { if (typeof value !== "string" || !expression.test(value)) invalidPlatformResponse(label); return value; }
function nullablePattern(value, expression, label) { return value === null ? null : pattern(value, expression, label); }
function workspacePattern(value, expression, label) { if (typeof value !== "string" || !expression.test(value)) invalidWorkspaceResponse(label); return value; }
function workspaceNullablePattern(value, expression, label) { return value === null ? null : workspacePattern(value, expression, label); }
function hash(value, label) { return pattern(value, PATTERN.sha256, label); }
function workspaceHash(value, label) { return workspacePattern(value, PATTERN.sha256, label); }
function integer(value, minimum, maximum, label, fail = invalidPlatformResponse) { if (!Number.isSafeInteger(value) || value < minimum || value > maximum) fail(label); return value; }
function boolean(value, label, fail = invalidPlatformResponse) { if (typeof value !== "boolean") fail(label); return value; }
function choice(value, choices, label) { if (!choices.includes(value)) invalidPlatformResponse(label); return value; }
function workspaceChoice(value, choices, label) { if (!choices.includes(value)) invalidWorkspaceResponse(label); return value; }
function timestamp(value, label) { if (typeof value !== "string" || value.length > 80 || !Number.isFinite(Date.parse(value))) invalidPlatformResponse(label); return value; }
function nullableTimestamp(value, label) { return value === null ? null : timestamp(value, label); }
function workspaceTimestamp(value, label) { if (typeof value !== "string" || value.length > 80 || !Number.isFinite(Date.parse(value))) invalidWorkspaceResponse(label); return value; }
function nullableCursor(value, fail = invalidPlatformResponse) { if (value !== null && (typeof value !== "string" || value.length > 2048 || !PATTERN.cursor.test(value))) fail("next_cursor"); }
function trace(value) { return { request_id: safeString(value.request_id, 1, 160, "request_id"), trace_id: safeString(value.trace_id, 1, 160, "trace_id") }; }
function workspaceTrace(value) { return { request_id: workspaceString(value.request_id, 1, 160, "request_id"), trace_id: workspaceString(value.trace_id, 1, 160, "trace_id") }; }

function requestPlain(value, label, fail = invalidPlatformRequest) { if (!value || Object.getPrototypeOf(value) !== Object.prototype) fail(label); return value; }
function requestExact(value, keys, fail = invalidPlatformRequest) { const actual = Object.keys(value).sort(); const expected = [...keys].sort(); if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) fail("shape"); }
function requestString(value, minimum, maximum, label, fail = invalidPlatformRequest) { if (typeof value !== "string" || value.length < minimum || value.length > maximum || /[\u0000-\u001f\u007f]/u.test(value)) fail(label); return value; }
function requestPattern(value, expression, label, fail = invalidPlatformRequest) { if (typeof value !== "string" || !expression.test(value)) fail(label); return value; }
function requestChoice(value, choices, label, fail = invalidPlatformRequest) { if (!choices.includes(value)) fail(label); return value; }
function requestInteger(value, minimum, maximum, label, fail = invalidPlatformRequest) { if (!Number.isSafeInteger(value) || value < minimum || value > maximum) fail(label); return value; }
function requestTimestamp(value, label) { if (typeof value !== "string" || value.length > 80 || !Number.isFinite(Date.parse(value))) invalidPlatformRequest(label); return value; }
function workspaceRequestString(value, minimum, maximum, label) { return requestString(value, minimum, maximum, label, invalidWorkspaceRequest); }
function workspaceRequestPattern(value, expression, label) { return requestPattern(value, expression, label, invalidWorkspaceRequest); }
function workspaceRequestChoice(value, choices, label) { return requestChoice(value, choices, label, invalidWorkspaceRequest); }
function workspaceRequestInteger(value, minimum, maximum, label) { return requestInteger(value, minimum, maximum, label, invalidWorkspaceRequest); }

function safeErrorText(value, fallback) {
  return typeof value === "string" && value
    ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240)
    : fallback;
}

function invalidPlatformRequest(label) {
  throw new PlatformApiError({ code: "platform.request_invalid", message: `Provider Engineering 请求字段无效：${String(label).slice(0, 80)}` });
}
function invalidPlatformResponse(label) {
  throw new PlatformApiError({ status: 502, code: "platform.provider_engineering_response_invalid", message: `Provider Engineering 响应无效：${String(label).slice(0, 80)}` });
}
function invalidWorkspaceRequest(label) {
  throw new AuthApiError({ code: "error_map.input_invalid", message: `Provider override 请求字段无效：${String(label).slice(0, 80)}` });
}
function invalidWorkspaceResponse(label) {
  throw new AuthApiError({ status: 502, code: "workspace.provider_engineering_response_invalid", message: `Provider override 响应无效：${String(label).slice(0, 80)}` });
}
