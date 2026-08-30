/** PRD-136 strict release security clients with separated credential domains. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const IDS = Object.freeze({
  release: /^sdr_[0-9a-f]{32}$/u,
  sbom: /^sbr_[0-9a-f]{32}$/u,
  signature: /^asi_[0-9a-f]{32}$/u,
  provenance: /^bpr_[0-9a-f]{32}$/u,
  finding: /^vfn_[0-9a-f]{32}$/u,
  exception: /^sex_[0-9a-f]{32}$/u,
  patch: /^ptr_[0-9a-f]{32}$/u,
  workspace: /^wsp_[0-9a-f]{32}$/u,
});
const SHA256 = /^[0-9a-f]{64}$/u;
const PUBLIC_KEY = /^[A-Za-z0-9_-]{43}$/u;
const SIGNATURE = /^[A-Za-z0-9_-]{86}$/u;
const SAFE_TRACE = /^[A-Za-z0-9_.:-]{1,160}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const REASONS = ["vendor_fix_pending", "mitigated_exposure", "emergency_continuity"];
const CONTROLS = ["feature_disablement", "network_isolation", "runtime_detection"];

export async function getDeveloperSupplyChainSecurity({
  controlApiUrl, token, releaseIds, signal,
} = {}) {
  return loadReleaseSecurity({
    controlApiUrl,
    token: developerToken(token),
    releaseIds,
    signal,
    domain: "developer",
  });
}

export async function getPlatformSupplyChainSecurity({
  controlApiUrl, releaseIds, signal,
} = {}) {
  return loadReleaseSecurity({ controlApiUrl, releaseIds, signal, domain: "platform" });
}

export async function applyPlatformSecurityException({
  controlApiUrl,
  findingId,
  operation,
  workspaceId,
  reasonCode,
  compensatingControls,
  startsAt,
  expiresAt,
  securityExceptionId,
  expectedRevision,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("confirmed", "platform");
  const finding = requestPattern(findingId, IDS.finding, "finding_id", "platform");
  const checkedOperation = requestChoice(operation, ["request", "approve"], "operation", "platform");
  let body;
  if (checkedOperation === "request") {
    const controls = requestControls(compensatingControls);
    body = {
      operation: "request",
      workspace_id: requestPattern(workspaceId, IDS.workspace, "workspace_id", "platform"),
      reason_code: requestChoice(reasonCode, REASONS, "reason_code", "platform"),
      compensating_controls: controls,
      starts_at: requestTimestamp(startsAt, "starts_at", "platform"),
      expires_at: requestTimestamp(expiresAt, "expires_at", "platform"),
      confirmed: true,
    };
    if (Date.parse(body.expires_at) <= Date.parse(body.starts_at)) {
      invalidRequest("exception_interval", "platform");
    }
  } else {
    body = {
      operation: "approve",
      security_exception_id: requestPattern(
        securityExceptionId, IDS.exception, "security_exception_id", "platform",
      ),
      expected_revision: requestInteger(expectedRevision, "expected_revision", "platform"),
      confirmed: true,
    };
  }
  const value = await platformJson(
    controlApiUrl,
    `/platform/vulnerabilities/${encodeURIComponent(finding)}/exceptions`,
    {
      method: "POST",
      body,
      idempotencyKey: requestPattern(
        idempotencyKey, IDEMPOTENCY, "idempotency_key", "platform",
      ),
      signal,
    },
  );
  exact(value, ["exception", "idempotent_replay", "request_id", "trace_id"],
    "exception_response", "platform");
  const result = exception(value.exception, "platform", true);
  if (result.vulnerability_finding_id !== finding || typeof value.idempotent_replay !== "boolean") {
    invalidResponse("exception_owner", "platform");
  }
  return Object.freeze({
    exception: result,
    idempotent_replay: value.idempotent_replay,
    ...trace(value, "platform"),
  });
}

async function loadReleaseSecurity({ controlApiUrl, token, releaseIds, signal, domain }) {
  const ids = releaseIdList(releaseIds, domain);
  const entries = await Promise.all(ids.map(async (releaseId) => {
    try {
      const [reportValue, provenanceValue] = await Promise.all([
        releaseJson(controlApiUrl, `/releases/${encodeURIComponent(releaseId)}/security-report`, {
          domain, token, signal,
        }),
        releaseJson(controlApiUrl, `/releases/${encodeURIComponent(releaseId)}/provenance`, {
          domain, token, signal,
        }),
      ]);
      const report = securityReportResponse(reportValue, domain, releaseId);
      const provenance = provenanceResponse(provenanceValue, domain, releaseId);
      crossCheck(report.report, provenance.provenance, domain);
      const clientOutcome = await verifyAttestation(report.report.signature, releaseId);
      if (report.report.signature.verification_outcome === "passed" && clientOutcome !== "passed") {
        invalidResponse("attestation_verification", domain);
      }
      return [releaseId, Object.freeze({
        status: "ready",
        report: Object.freeze({
          ...report.report,
          signature: Object.freeze({
            ...report.report.signature,
            client_verification_outcome: clientOutcome,
          }),
        }),
        provenance: provenance.provenance,
        request_ids: Object.freeze([report.request_id, provenance.request_id]),
      })];
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      if (error?.code === "supply_chain.release_not_found") {
        return [releaseId, Object.freeze({ status: "missing", report: null, provenance: null })];
      }
      throw error;
    }
  }));
  return Object.freeze(Object.fromEntries(entries));
}

function securityReportResponse(value, domain, releaseId) {
  exact(value, ["report", "request_id", "trace_id"], "security_report_response", domain);
  const report = securityReport(value.report, domain);
  if (report.sdk_release_id !== releaseId) invalidResponse("release_owner", domain);
  return Object.freeze({ report, ...trace(value, domain) });
}

function securityReport(value, domain) {
  exact(value, [
    "sdk_release_id", "language", "sdk_version", "artifact_sha256", "gate_outcome",
    "blocking_finding_count", "sbom", "signature", "vulnerabilities", "generated_at",
  ], "security_report", domain);
  array(value.vulnerabilities, 500, "vulnerabilities", domain);
  const artifact = responsePattern(value.artifact_sha256, SHA256, "artifact_sha256", domain);
  const result = {
    sdk_release_id: responsePattern(value.sdk_release_id, IDS.release, "sdk_release_id", domain),
    language: responseChoice(value.language, ["python", "javascript"], "language", domain),
    sdk_version: responseText(value.sdk_version, 80, "sdk_version", domain),
    artifact_sha256: artifact,
    gate_outcome: responseChoice(value.gate_outcome, [
      "passed", "blocked", "withdrawn", "attestation_invalid",
    ], "gate_outcome", domain),
    blocking_finding_count: responseInteger(
      value.blocking_finding_count, 0, "blocking_finding_count", domain,
    ),
    sbom: sbom(value.sbom, domain),
    signature: artifactSignature(value.signature, domain),
    vulnerabilities: Object.freeze(value.vulnerabilities.map((item) => (
      vulnerability(item, domain, artifact)
    ))),
    generated_at: timestamp(value.generated_at, "generated_at", domain),
  };
  if (result.signature.artifact_sha256 !== artifact
    || result.signature.sbom_sha256 !== result.sbom.sbom_sha256
    || result.blocking_finding_count !== result.vulnerabilities.filter((item) => (
      new Set(["critical", "high"]).has(item.severity)
    )).length) {
    invalidResponse("security_digest_chain", domain);
  }
  return Object.freeze(result);
}

function sbom(value, domain) {
  exact(value, [
    "sbom_report_id", "format", "format_version", "component_count", "sbom_sha256",
    "immutable_object_receipt_sha256", "license_summary", "vulnerability_summary", "generated_at",
  ], "sbom", domain);
  plain(value.license_summary, "license_summary", domain);
  plain(value.vulnerability_summary, "vulnerability_summary", domain);
  numericRecord(value.license_summary, "license_summary", domain);
  numericRecord(value.vulnerability_summary, "vulnerability_summary", domain);
  return Object.freeze({
    sbom_report_id: responsePattern(value.sbom_report_id, IDS.sbom, "sbom_report_id", domain),
    format: responseChoice(value.format, ["cyclonedx", "spdx"], "format", domain),
    format_version: responseChoice(value.format_version, ["1.5", "2.3"], "format_version", domain),
    component_count: responseInteger(value.component_count, 1, "component_count", domain),
    sbom_sha256: responsePattern(value.sbom_sha256, SHA256, "sbom_sha256", domain),
    immutable_object_receipt_sha256: responsePattern(
      value.immutable_object_receipt_sha256, SHA256, "immutable_receipt", domain,
    ),
    license_summary: Object.freeze({ ...value.license_summary }),
    vulnerability_summary: Object.freeze({ ...value.vulnerability_summary }),
    generated_at: timestamp(value.generated_at, "generated_at", domain),
  });
}

function artifactSignature(value, domain) {
  exact(value, [
    "artifact_signature_id", "purpose", "algorithm", "key_id", "key_version",
    "public_key_b64", "artifact_sha256", "sbom_sha256", "provenance_sha256",
    "attestation_sha256", "signature_b64", "signature_sha256", "verification_outcome",
    "signed_at",
  ], "artifact_signature", domain);
  return Object.freeze({
    artifact_signature_id: responsePattern(
      value.artifact_signature_id, IDS.signature, "artifact_signature_id", domain,
    ),
    purpose: responseChoice(
      value.purpose, ["metahub.artifact.attestation/v1"], "purpose", domain,
    ),
    algorithm: responseChoice(value.algorithm, ["ed25519"], "algorithm", domain),
    key_id: responseText(value.key_id, 80, "key_id", domain),
    key_version: responseInteger(value.key_version, 1, "key_version", domain),
    public_key_b64: responsePattern(value.public_key_b64, PUBLIC_KEY, "public_key_b64", domain),
    artifact_sha256: responsePattern(value.artifact_sha256, SHA256, "artifact_sha256", domain),
    sbom_sha256: responsePattern(value.sbom_sha256, SHA256, "sbom_sha256", domain),
    provenance_sha256: responsePattern(
      value.provenance_sha256, SHA256, "provenance_sha256", domain,
    ),
    attestation_sha256: responsePattern(
      value.attestation_sha256, SHA256, "attestation_sha256", domain,
    ),
    signature_b64: responsePattern(value.signature_b64, SIGNATURE, "signature_b64", domain),
    signature_sha256: responsePattern(
      value.signature_sha256, SHA256, "signature_sha256", domain,
    ),
    verification_outcome: responseChoice(
      value.verification_outcome, ["passed", "failed", "revoked"],
      "verification_outcome", domain,
    ),
    signed_at: timestamp(value.signed_at, "signed_at", domain),
  });
}

function vulnerability(value, domain, artifact) {
  exact(value, [
    "vulnerability_finding_id", "advisory_code", "severity", "evaluation_generation",
    "remediation_state", "fixed_version_code", "first_seen_at", "last_seen_at",
    "sla_deadline_at", "exceptions", "patch_releases",
  ], "vulnerability", domain);
  array(value.exceptions, 100, "exceptions", domain);
  array(value.patch_releases, 100, "patch_releases", domain);
  const findingId = responsePattern(
    value.vulnerability_finding_id, IDS.finding, "vulnerability_finding_id", domain,
  );
  return Object.freeze({
    vulnerability_finding_id: findingId,
    advisory_code: responseText(value.advisory_code, 80, "advisory_code", domain),
    severity: responseChoice(value.severity, ["low", "medium", "high", "critical"],
      "severity", domain),
    evaluation_generation: responseInteger(
      value.evaluation_generation, 1, "evaluation_generation", domain,
    ),
    remediation_state: responseChoice(value.remediation_state, [
      "open", "fixed_available", "patched", "accepted", "closed",
    ], "remediation_state", domain),
    fixed_version_code: value.fixed_version_code === null ? null
      : responseText(value.fixed_version_code, 80, "fixed_version_code", domain),
    first_seen_at: timestamp(value.first_seen_at, "first_seen_at", domain),
    last_seen_at: timestamp(value.last_seen_at, "last_seen_at", domain),
    sla_deadline_at: timestamp(value.sla_deadline_at, "sla_deadline_at", domain),
    exceptions: Object.freeze(value.exceptions.map((item) => exception(item, domain, false))),
    patch_releases: Object.freeze(value.patch_releases.map((item) => (
      patchRelease(item, domain, findingId, artifact)
    ))),
  });
}

function exception(value, domain, full) {
  const keys = full
    ? [
      "security_exception_id", "workspace_id", "vulnerability_finding_id",
      "affected_artifact_sha256", "reason_code", "compensating_controls", "status",
      "review_generation", "revision", "starts_at", "expires_at", "approved_at", "updated_at",
    ]
    : [
      "security_exception_id", "workspace_id", "reason_code", "status", "revision",
      "starts_at", "expires_at",
    ];
  exact(value, keys, "security_exception", domain);
  const base = {
    security_exception_id: responsePattern(
      value.security_exception_id, IDS.exception, "security_exception_id", domain,
    ),
    workspace_id: responsePattern(value.workspace_id, IDS.workspace, "workspace_id", domain),
    reason_code: responseChoice(value.reason_code, REASONS, "reason_code", domain),
    status: responseChoice(value.status, ["pending", "active", "expired", "revoked"],
      "status", domain),
    revision: responseInteger(value.revision, 1, "revision", domain),
    starts_at: timestamp(value.starts_at, "starts_at", domain),
    expires_at: timestamp(value.expires_at, "expires_at", domain),
  };
  if (!full) return Object.freeze(base);
  array(value.compensating_controls, 3, "compensating_controls", domain);
  if (value.compensating_controls.length < 1
    || value.compensating_controls.some((item) => !CONTROLS.includes(item))) {
    invalidResponse("compensating_controls", domain);
  }
  return Object.freeze({
    ...base,
    vulnerability_finding_id: responsePattern(
      value.vulnerability_finding_id, IDS.finding, "vulnerability_finding_id", domain,
    ),
    affected_artifact_sha256: responsePattern(
      value.affected_artifact_sha256, SHA256, "affected_artifact_sha256", domain,
    ),
    compensating_controls: Object.freeze([...value.compensating_controls]),
    review_generation: responseInteger(value.review_generation, 1, "review_generation", domain),
    approved_at: nullableTimestamp(value.approved_at, "approved_at", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  });
}

function patchRelease(value, domain, findingId, artifact) {
  exact(value, [
    "patch_release_id", "workspace_id", "vulnerability_finding_id",
    "affected_artifact_sha256", "fixed_sdk_release_id", "fixed_artifact_sha256",
    "compatibility_matrix", "compatibility_sha256", "rollback_artifact_sha256", "status",
    "revision", "released_at", "updated_at",
  ], "patch_release", domain);
  const matrix = compatibilityMatrix(value.compatibility_matrix, domain);
  const result = {
    patch_release_id: responsePattern(value.patch_release_id, IDS.patch, "patch_release_id", domain),
    workspace_id: responsePattern(value.workspace_id, IDS.workspace, "workspace_id", domain),
    vulnerability_finding_id: responsePattern(
      value.vulnerability_finding_id, IDS.finding, "vulnerability_finding_id", domain,
    ),
    affected_artifact_sha256: responsePattern(
      value.affected_artifact_sha256, SHA256, "affected_artifact_sha256", domain,
    ),
    fixed_sdk_release_id: responsePattern(
      value.fixed_sdk_release_id, IDS.release, "fixed_sdk_release_id", domain,
    ),
    fixed_artifact_sha256: responsePattern(
      value.fixed_artifact_sha256, SHA256, "fixed_artifact_sha256", domain,
    ),
    compatibility_matrix: matrix,
    compatibility_sha256: responsePattern(
      value.compatibility_sha256, SHA256, "compatibility_sha256", domain,
    ),
    rollback_artifact_sha256: responsePattern(
      value.rollback_artifact_sha256, SHA256, "rollback_artifact_sha256", domain,
    ),
    status: responseChoice(value.status, ["proposed", "approved", "released", "rolled_back"],
      "status", domain),
    revision: responseInteger(value.revision, 1, "revision", domain),
    released_at: nullableTimestamp(value.released_at, "released_at", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  };
  if (result.vulnerability_finding_id !== findingId
    || result.affected_artifact_sha256 !== artifact
    || result.rollback_artifact_sha256 !== artifact
    || result.fixed_artifact_sha256 === artifact) invalidResponse("patch_owner", domain);
  return Object.freeze(result);
}

function compatibilityMatrix(value, domain) {
  exact(value, ["cloud", "edge"], "compatibility_matrix", domain);
  for (const target of [value.cloud, value.edge]) {
    exact(target, ["n", "n_minus_1"], "compatibility_target", domain);
    if (target.n !== "passed" || target.n_minus_1 !== "passed") {
      invalidResponse("compatibility_target", domain);
    }
  }
  return Object.freeze({
    cloud: Object.freeze({ n: "passed", n_minus_1: "passed" }),
    edge: Object.freeze({ n: "passed", n_minus_1: "passed" }),
  });
}

function provenanceResponse(value, domain, releaseId) {
  exact(value, ["provenance", "request_id", "trace_id"], "provenance_response", domain);
  const provenance = buildProvenance(value.provenance, domain);
  if (provenance.sdk_release_id !== releaseId) invalidResponse("provenance_owner", domain);
  return Object.freeze({ provenance, ...trace(value, domain) });
}

function buildProvenance(value, domain) {
  exact(value, [
    "build_provenance_id", "sdk_release_id", "artifact_sha256", "source_commit_sha256",
    "source_tree_sha256", "dependency_lock_sha256", "builder_verified",
    "builder_image_sha256", "build_invocation_sha256", "artifact_set_sha256",
    "environment_allowlist_sha256", "provenance_sha256",
    "reproducibility_receipt_sha256", "reproducible", "generated_at",
  ], "build_provenance", domain);
  if (typeof value.builder_verified !== "boolean" || typeof value.reproducible !== "boolean") {
    invalidResponse("provenance_outcome", domain);
  }
  const result = { builder_verified: value.builder_verified, reproducible: value.reproducible };
  for (const key of [
    "artifact_sha256", "source_commit_sha256", "source_tree_sha256", "dependency_lock_sha256",
    "builder_image_sha256", "build_invocation_sha256", "artifact_set_sha256",
    "environment_allowlist_sha256", "provenance_sha256", "reproducibility_receipt_sha256",
  ]) result[key] = responsePattern(value[key], SHA256, key, domain);
  return Object.freeze({
    build_provenance_id: responsePattern(
      value.build_provenance_id, IDS.provenance, "build_provenance_id", domain,
    ),
    sdk_release_id: responsePattern(value.sdk_release_id, IDS.release, "sdk_release_id", domain),
    ...result,
    generated_at: timestamp(value.generated_at, "generated_at", domain),
  });
}

function crossCheck(report, provenance, domain) {
  if (provenance.sdk_release_id !== report.sdk_release_id
    || provenance.artifact_sha256 !== report.artifact_sha256
    || provenance.provenance_sha256 !== report.signature.provenance_sha256
    || provenance.builder_verified !== (report.signature.verification_outcome === "passed")) {
    invalidResponse("provenance_digest_chain", domain);
  }
}

async function verifyAttestation(signature, releaseId) {
  if (!globalThis.crypto?.subtle) return "unavailable";
  try {
    const publicKeyBytes = base64UrlBytes(signature.public_key_b64);
    const signatureBytes = base64UrlBytes(signature.signature_b64);
    const payload = new TextEncoder().encode(JSON.stringify({
      artifact_sha256: signature.artifact_sha256,
      provenance_sha256: signature.provenance_sha256,
      purpose: "metahub.artifact.attestation/v1",
      sbom_sha256: signature.sbom_sha256,
      sdk_release_id: releaseId,
    }));
    if (await bytesSha256(signatureBytes) !== signature.signature_sha256
      || await bytesSha256(payload) !== signature.attestation_sha256) return "failed";
    const key = await globalThis.crypto.subtle.importKey(
      "raw", publicKeyBytes, { name: "Ed25519" }, false, ["verify"],
    );
    return await globalThis.crypto.subtle.verify(
      { name: "Ed25519" }, key, signatureBytes, payload,
    ) ? "passed" : "failed";
  } catch {
    return "failed";
  }
}

async function releaseJson(controlApiUrl, path, { domain, token, signal }) {
  const headers = { Accept: "application/json" };
  if (domain === "developer") headers.Authorization = `Bearer ${token}`;
  return requestJson(controlApiUrl, path, {
    domain, headers, credentials: domain === "developer" ? "omit" : "include", signal,
  });
}

async function platformJson(controlApiUrl, path, options) {
  const headers = {
    Accept: "application/json",
    "Content-Type": "application/json",
    "Idempotency-Key": options.idempotencyKey,
    "X-CSRF-Token": readPlatformCsrfCookie(),
  };
  return requestJson(controlApiUrl, path, {
    domain: "platform",
    method: options.method,
    headers,
    body: JSON.stringify(options.body),
    credentials: "include",
    signal: options.signal,
  });
}

async function requestJson(controlApiUrl, path, options) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, options.domain), {
      method: options.method ?? "GET",
      headers: options.headers,
      body: options.body,
      credentials: options.credentials,
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw apiError(options.domain, 0, "supply_chain.unavailable", "Service unavailable");
  }
  const payload = await readJson(response, options.domain);
  if (!response.ok) throw serverError(response, payload, options.domain);
  requirePrivateNoStore(response, options.domain);
  return payload;
}

async function readJson(response, domain) {
  if (!/^application\/json(?:\s*;|$)/iu.test(response.headers.get("Content-Type") ?? "")) {
    invalidResponse("content_type", domain);
  }
  try {
    const value = await response.json();
    plain(value, "payload", domain);
    return value;
  } catch (error) {
    if (error instanceof AuthApiError || error instanceof PlatformApiError) throw error;
    invalidResponse("json", domain);
  }
}

function serverError(response, payload, domain) {
  const code = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(payload?.code ?? ""))
    ? payload.code : "supply_chain.request_invalid";
  return apiError(
    domain, response.status, code, safeText(payload?.message) || "Request failed",
    safeText(payload?.request_id) || "-", safeText(payload?.trace_id) || "-",
  );
}

function requirePrivateNoStore(response, domain) {
  const directives = String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim());
  if (!directives.includes("private") || !directives.includes("no-store")) {
    invalidResponse("cache_control", domain);
  }
}

function releaseIdList(value, domain) {
  if (!Array.isArray(value) || value.length > 200) invalidRequest("release_ids", domain);
  const ids = value.map((item) => requestPattern(item, IDS.release, "release_id", domain));
  if (new Set(ids).size !== ids.length) invalidRequest("release_ids", domain);
  return Object.freeze(ids);
}

function requestControls(value) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 3) {
    invalidRequest("compensating_controls", "platform");
  }
  const controls = [...new Set(value)].sort();
  if (controls.length !== value.length || controls.some((item) => !CONTROLS.includes(item))) {
    invalidRequest("compensating_controls", "platform");
  }
  return controls;
}

function developerToken(value) {
  if (typeof value !== "string" || value.length < 16 || value.length > 4096
    || /[\u0000-\u0020\u007f]/u.test(value)) invalidRequest("token", "developer");
  return value;
}

function apiUrl(baseUrl, path, domain) {
  let base;
  try { base = new URL(String(baseUrl ?? "")); } catch { invalidRequest("control_api_url", domain); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password
    || base.search || base.hash || (base.pathname !== "/" && base.pathname !== "")) {
    invalidRequest("control_api_url", domain);
  }
  return new URL(path, base).toString();
}

function exact(value, keys, label, domain) {
  plain(value, label, domain);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) invalidResponse(label, domain);
}

function plain(value, label, domain) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse(label, domain);
}

function array(value, maximum, label, domain) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(label, domain);
}

function numericRecord(value, label, domain) {
  if (Object.keys(value).length > 64 || Object.entries(value).some(([key, item]) => (
    !/^[a-z][a-z0-9_]{0,63}$/u.test(key) || !Number.isSafeInteger(item) || item < 0
  ))) invalidResponse(label, domain);
}

function requestPattern(value, pattern, label, domain) {
  if (typeof value !== "string" || !pattern.test(value)) invalidRequest(label, domain);
  return value;
}

function responsePattern(value, pattern, label, domain) {
  if (typeof value !== "string" || !pattern.test(value)) invalidResponse(label, domain);
  return value;
}

function requestChoice(value, choices, label, domain) {
  if (!choices.includes(value)) invalidRequest(label, domain);
  return value;
}

function responseChoice(value, choices, label, domain) {
  if (!choices.includes(value)) invalidResponse(label, domain);
  return value;
}

function requestInteger(value, label, domain) {
  if (!Number.isSafeInteger(Number(value)) || Number(value) < 1) invalidRequest(label, domain);
  return Number(value);
}

function responseInteger(value, minimum, label, domain) {
  if (!Number.isSafeInteger(value) || value < minimum) invalidResponse(label, domain);
  return value;
}

function responseText(value, maximum, label, domain) {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum
    || /[\u0000-\u001f\u007f]/u.test(value)) invalidResponse(label, domain);
  return value;
}

function requestTimestamp(value, label, domain) {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))
    || !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value)) invalidRequest(label, domain);
  return value;
}

function timestamp(value, label, domain) {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))
    || !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value)) invalidResponse(label, domain);
  return value;
}

function nullableTimestamp(value, label, domain) {
  return value === null ? null : timestamp(value, label, domain);
}

function trace(value, domain) {
  return {
    request_id: responsePattern(value.request_id, SAFE_TRACE, "request_id", domain),
    trace_id: responsePattern(value.trace_id, SAFE_TRACE, "trace_id", domain),
  };
}

async function bytesSha256(bytes) {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function base64UrlBytes(value) {
  const padded = value.replace(/-/gu, "+").replace(/_/gu, "/")
    + "=".repeat((4 - value.length % 4) % 4);
  const binary = globalThis.atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function apiError(domain, status, code, message, requestId = "-", traceId = "-") {
  return domain === "platform"
    ? new PlatformApiError({ status, code, message, requestId })
    : new AuthApiError({ status, code, message, requestId, traceId });
}

function invalidRequest(label, domain) {
  throw apiError(domain, 422, "supply_chain.request_invalid", `Invalid ${label}`);
}

function invalidResponse(label, domain) {
  throw apiError(domain, 502, "supply_chain.response_invalid", `Invalid ${label}`);
}

function safeText(value) {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240) : "";
}
