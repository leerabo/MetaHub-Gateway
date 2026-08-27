/** PRD-120..122 Workspace Edge identity, desired state, telemetry, and Fleet clients. */

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
const TELEMETRY_POLICY_ID = /^etp_[0-9a-f]{32}$/u;
const TELEMETRY_CURSOR_ID = /^euc_[0-9a-f]{32}$/u;
const TELEMETRY_EPOCH_ID = /^ete_[0-9a-f]{32}$/u;
const SECRET_REFERENCE_ID = /^lsr_[0-9a-f]{32}$/u;
const INSTALLER_RELEASE_ID = /^irl_[0-9a-f]{32}$/u;
const INSTALLER_ARTIFACT_ID = /^ira_[0-9a-f]{32}$/u;
const INSTALL_PROFILE_ID = /^ipr_[0-9a-f]{32}$/u;
const PREFLIGHT_REPORT_ID = /^pfr_[0-9a-f]{32}$/u;
const INSTALLATION_ID = /^ins_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const OWNER_ID = /^[a-z0-9][a-z0-9_:-]{2,79}$/u;
const SAFE_LABEL = /^[A-Za-z0-9][A-Za-z0-9 ._:/()-]{0,79}$/u;
const UTC_TIME = /^20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{3})?Z$/u;
const CHAIN_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const SAFE_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u;
const REGION_CODE = /^[a-z0-9][a-z0-9-]{1,39}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const ONE_USE_VALUE = /^e(?:rg|ch)_[A-Za-z0-9_-]{43}$/u;
const SEMVER = /^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$/u;
const INSTALLER_CODE = /^[a-z][a-z0-9_.-]{1,79}$/u;
const INSTALLER_KEY_ID = /^[a-z0-9][a-z0-9._:-]{2,79}$/u;
const INSTALLER_FILENAME = /^[^\u0000-\u0020\u007f\\/]{1,120}$/u;
const INSTALLER_SIGNATURE = /^[A-Za-z0-9_-]+$/u;
const DNS_TARGET = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/u;
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

const PREFLIGHT_CHECK_CODES = Object.freeze([
  "artifact.integrity", "host.os", "host.architecture", "host.root", "host.cpu",
  "host.memory", "host.disk", "network.ports", "network.dns", "host.clock",
  "network.proxy", "network.ca", "host.filesystem", "runtime.container",
]);
const INSTALLER_ARTIFACT_KINDS = Object.freeze([
  "cli", "compose", "container_bundle", "profile", "sbom",
]);
const INSTALLER_MEDIA_TYPES = Object.freeze([
  "application/json", "application/spdx+json",
  "application/vnd.docker.distribution.manifest.v2+json", "application/x-tar",
  "application/yaml", "text/x-python",
]);

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
  "telemetry_last_receipt_at", "telemetry_lag_seconds", "telemetry_backlog_seconds",
  "telemetry_cpu_bucket", "telemetry_memory_bucket",
  "telemetry_error_rate_basis_points", "telemetry_failure_code",
]);
const DESIRED_STATE_KEYS = Object.freeze([
  "desired_state_id", "deployment_id", "generation", "base_generation",
  "rollback_of_generation", "source_config_snapshot_id", "source_generation",
  "source_revocation_watermark", "change_class", "status", "revision", "head_revision",
  "requested_by_account_id", "approved_by_account_id", "desired_generation",
  "applied_generation", "lkg_generation", "last_ack_status", "last_reason_code", "drifted",
  "requested_at", "decided_at", "published_at", "last_ack_at", "updated_at",
]);
const SECRET_REFERENCE_KEYS = Object.freeze([
  "active_version", "capabilities", "deployment_id", "kind", "label",
  "last_test_outcome", "provider_connection_id", "reference_id", "revision",
  "rotated_at", "schema_version", "status", "tenant_id", "updated_at", "workspace_id",
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

export async function listEdgeInstallers({ controlApiUrl, signal } = {}) {
  const result = await request(controlApiUrl, "/edge/installers", { signal }, "workspace");
  exact(result.payload, [
    "downloads", "installations", "preflights", "releases", "request_id", "trace_id",
  ], "installer_catalog");
  boundedArray(result.payload.releases, 100, "installer_releases");
  boundedArray(result.payload.downloads, 3_200, "installer_downloads");
  boundedArray(result.payload.preflights, 500, "installer_preflights");
  boundedArray(result.payload.installations, 500, "installer_installations");

  const releases = result.payload.releases.map(validateInstallerRelease);
  unique(releases.map((item) => item.manifest.release_id), "installer_releases");
  const releasesById = new Map(releases.map((item) => [item.manifest.release_id, item]));
  const downloads = result.payload.downloads.map((item) => (
    validateInstallerDownload(item, releasesById)
  ));
  unique(downloads.map((item) => item.download_path), "installer_downloads");
  unique(downloads.map((item) => `${item.release_id}\0${item.artifact_id}`), "installer_downloads");
  const expectedArtifacts = releases.flatMap((release) => release.manifest.artifacts.map(
    (artifact) => `${release.manifest.release_id}\0${artifact.artifact_id}`,
  ));
  if (downloads.length !== expectedArtifacts.length
    || expectedArtifacts.some((key) => !downloads.some(
      (item) => `${item.release_id}\0${item.artifact_id}` === key,
    ))) invalidWorkspaceResponse("installer_downloads");

  const preflights = result.payload.preflights.map(validateEdgePreflightSummary);
  unique(preflights.map((item) => item.preflight_report_id), "installer_preflights");
  const installations = result.payload.installations.map(validateEdgeInstallationSummary);
  unique(installations.map((item) => item.installation_id), "installer_installations");
  return Object.freeze({
    releases: Object.freeze(releases),
    downloads: Object.freeze(downloads),
    preflights: Object.freeze(preflights),
    installations: Object.freeze(installations),
    ...trace(result.payload),
  });
}

export function edgeInstallerArtifactUrl({ controlApiUrl, downloadPath } = {}) {
  const path = String(downloadPath ?? "");
  if (!/^\/edge\/installers\/irl_[0-9a-f]{32}\/artifacts\/ira_[0-9a-f]{32}$/u.test(path)) {
    invalidWorkspaceRequest("download_path");
  }
  return apiUrl(controlApiUrl, path, "workspace");
}

export async function listEdgeSecretReferences({ controlApiUrl, signal } = {}) {
  const result = await request(
    controlApiUrl,
    "/edge/secret-references",
    { signal },
    "workspace",
  );
  exact(result.payload, ["references", "request_id", "trace_id"], "secret_references");
  boundedArray(result.payload.references, 500, "secret_references");
  const references = result.payload.references.map(validateSecretReference);
  unique(references.map((item) => item.reference_id), "secret_references");
  return Object.freeze({ references: Object.freeze(references), ...trace(result.payload) });
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

export async function getEdgeTelemetryWatermark({
  controlApiUrl,
  deploymentId,
  signal,
} = {}) {
  const id = clientId(deploymentId, DEPLOYMENT_ID, "deployment_id");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/telemetry-watermark`,
    { signal },
    "workspace",
  );
  const watermark = validateTelemetryWatermark(result.payload);
  if (watermark.deployment_id !== id) invalidWorkspaceResponse("telemetry_owner");
  const revision = responseRevision(result.response);
  if (revision !== (watermark.policy?.revision ?? 0)) {
    invalidWorkspaceResponse("telemetry_etag");
  }
  return Object.freeze({ ...watermark, revision });
}

export async function patchEdgeTelemetryPolicy({
  controlApiUrl,
  deploymentId,
  telemetryLevel,
  expectedRevision,
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(deploymentId, DEPLOYMENT_ID, "deployment_id");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/telemetry-policy`,
    {
      method: "PATCH",
      body: {
        telemetry_level: clientChoice(
          telemetryLevel,
          ["minimal", "operational", "billing"],
          "telemetry_level",
        ),
      },
      csrf: true,
      headers: {
        "Idempotency-Key": clientIdempotencyKey(idempotencyKey),
        "If-Match": `"${clientInteger(expectedRevision, 1, Number.MAX_SAFE_INTEGER, "revision")}"`,
      },
      signal,
    },
    "workspace",
  );
  exact(result.payload, [
    "policy", "idempotent_replay", "request_id", "trace_id",
  ], "telemetry_policy_response");
  const policy = validateTelemetryPolicy(result.payload.policy);
  if (policy.deployment_id !== id) invalidWorkspaceResponse("telemetry_policy_owner");
  const revision = responseRevision(result.response);
  if (revision !== policy.revision) invalidWorkspaceResponse("telemetry_policy_etag");
  return Object.freeze({
    policy,
    revision,
    idempotent_replay: boolean(result.payload.idempotent_replay, "idempotent_replay"),
    ...trace(result.payload),
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

function validateInstallerRelease(value) {
  plain(value, "installer_release");
  exact(value, ["manifest", "manifest_sha256", "signature"], "installer_release");
  const manifest = validateInstallerManifest(value.manifest);
  const signature = validateInstallerSignature(value.signature);
  return Object.freeze({
    manifest,
    manifest_sha256: digest(value.manifest_sha256, "installer_manifest_sha256"),
    signature,
  });
}

function validateInstallerManifest(value) {
  plain(value, "installer_manifest");
  exact(value, [
    "agent_max_version", "agent_min_version", "artifacts", "channel",
    "control_max_version", "control_min_version", "n_minus_one", "profiles",
    "release_id", "schema_max", "schema_min", "schema_version", "signature_purpose",
    "supported_arch", "supported_os", "version",
  ], "installer_manifest");
  boundedArray(value.supported_os, 1, "installer_supported_os");
  boundedArray(value.supported_arch, 2, "installer_supported_arch");
  if (JSON.stringify(value.supported_os) !== JSON.stringify(["linux"])
    || JSON.stringify(value.supported_arch) !== JSON.stringify(["aarch64", "x86_64"])) {
    invalidWorkspaceResponse("installer_platforms");
  }
  boundedArray(value.artifacts, 32, "installer_artifacts");
  boundedArray(value.profiles, 8, "installer_profiles");
  if (value.artifacts.length < 5 || value.profiles.length < 1) {
    invalidWorkspaceResponse("installer_manifest");
  }
  const artifacts = value.artifacts.map(validateInstallerArtifact);
  const profiles = value.profiles.map(validateInstallProfile);
  unique(artifacts.map((item) => item.artifact_id), "installer_artifacts");
  unique(artifacts.map((item) => item.filename), "installer_artifacts");
  unique(profiles.map((item) => item.install_profile_id), "installer_profiles");
  if (INSTALLER_ARTIFACT_KINDS.some((kind) => !artifacts.some((item) => item.kind === kind))
    || artifacts.filter((item) => item.kind === "sbom").length !== 1
    || artifacts.filter((item) => item.kind === "profile").length !== 1) {
    invalidWorkspaceResponse("installer_artifacts");
  }
  const sbomDigest = artifacts.find((item) => item.kind === "sbom").sha256;
  if (artifacts.some((item) => item.sbom_sha256 !== sbomDigest)) {
    invalidWorkspaceResponse("installer_sbom");
  }
  const schemaMin = integer(value.schema_min, 1, 9_999, "installer_schema_min");
  const schemaMax = integer(value.schema_max, 1, 9_999, "installer_schema_max");
  const agentMin = installerSemver(value.agent_min_version, "installer_agent_min");
  const agentMax = installerSemver(value.agent_max_version, "installer_agent_max");
  const controlMin = installerSemver(value.control_min_version, "installer_control_min");
  const controlMax = installerSemver(value.control_max_version, "installer_control_max");
  if (schemaMin > schemaMax || compareSemver(agentMin, agentMax) > 0
    || compareSemver(controlMin, controlMax) > 0) {
    invalidWorkspaceResponse("installer_compatibility");
  }
  return Object.freeze({
    schema_version: responseChoice(
      value.schema_version, ["metahub.installer/v1"], "installer_schema_version",
    ),
    release_id: responseId(value.release_id, INSTALLER_RELEASE_ID, "installer_release_id"),
    version: installerSemver(value.version, "installer_version"),
    channel: responseChoice(value.channel, ["stable", "extended"], "installer_channel"),
    signature_purpose: responseChoice(
      value.signature_purpose,
      ["metahub.edge-installer.release/v1"],
      "installer_signature_purpose",
    ),
    supported_os: Object.freeze(["linux"]),
    supported_arch: Object.freeze(["aarch64", "x86_64"]),
    schema_min: schemaMin,
    schema_max: schemaMax,
    agent_min_version: agentMin,
    agent_max_version: agentMax,
    control_min_version: controlMin,
    control_max_version: controlMax,
    n_minus_one: boolean(value.n_minus_one, "installer_n_minus_one"),
    artifacts: Object.freeze(artifacts),
    profiles: Object.freeze(profiles),
  });
}

function validateInstallerArtifact(value) {
  plain(value, "installer_artifact");
  exact(value, [
    "artifact_id", "executable", "filename", "kind", "media_type", "sbom_sha256",
    "sha256", "size_bytes",
  ], "installer_artifact");
  const filename = responseText(value.filename, 120, "installer_filename");
  if (!INSTALLER_FILENAME.test(filename) || filename === "." || filename === "..") {
    invalidWorkspaceResponse("installer_filename");
  }
  return Object.freeze({
    artifact_id: responseId(value.artifact_id, INSTALLER_ARTIFACT_ID, "installer_artifact_id"),
    kind: responseChoice(value.kind, INSTALLER_ARTIFACT_KINDS, "installer_artifact_kind"),
    filename,
    media_type: responseChoice(value.media_type, INSTALLER_MEDIA_TYPES, "installer_media_type"),
    sha256: digest(value.sha256, "installer_artifact_sha256"),
    sbom_sha256: digest(value.sbom_sha256, "installer_artifact_sbom_sha256"),
    size_bytes: integer(value.size_bytes, 1, 8 * 1024 * 1024 * 1024, "installer_artifact_size"),
    executable: boolean(value.executable, "installer_artifact_executable"),
  });
}

function validateInstallProfile(value) {
  plain(value, "install_profile");
  exact(value, [
    "ca_mode", "cpu_cores", "disk_mib", "display_name", "dns_targets", "filesystem",
    "install_profile_id", "memory_mib", "profile_code", "proxy_modes", "required_ports",
    "runtime", "runtime_min_version",
  ], "install_profile");
  boundedArray(value.required_ports, 16, "install_profile_ports");
  boundedArray(value.dns_targets, 8, "install_profile_dns");
  boundedArray(value.proxy_modes, 2, "install_profile_proxy");
  if (!value.required_ports.length || !value.dns_targets.length || !value.proxy_modes.length) {
    invalidWorkspaceResponse("install_profile");
  }
  const ports = value.required_ports.map((item) => integer(
    item, 1, 65_535, "install_profile_port",
  ));
  if (JSON.stringify(ports) !== JSON.stringify([...new Set(ports)].sort((a, b) => a - b))) {
    invalidWorkspaceResponse("install_profile_ports");
  }
  const dnsTargets = value.dns_targets.map((item) => {
    const target = responseText(item, 253, "install_profile_dns");
    if (!DNS_TARGET.test(target)) invalidWorkspaceResponse("install_profile_dns");
    return target;
  });
  if (JSON.stringify(dnsTargets) !== JSON.stringify([...new Set(dnsTargets)].sort())) {
    invalidWorkspaceResponse("install_profile_dns");
  }
  const proxyModes = value.proxy_modes.map((item) => responseChoice(
    item, ["direct", "http_connect"], "install_profile_proxy",
  ));
  if (JSON.stringify(proxyModes) !== JSON.stringify([...new Set(proxyModes)].sort())) {
    invalidWorkspaceResponse("install_profile_proxy");
  }
  return Object.freeze({
    install_profile_id: responseId(
      value.install_profile_id, INSTALL_PROFILE_ID, "install_profile_id",
    ),
    profile_code: responseChoice(value.profile_code, ["standard", "compact"], "install_profile_code"),
    display_name: installerText(value.display_name, 2, 80, "install_profile_name"),
    cpu_cores: integer(value.cpu_cores, 2, 256, "install_profile_cpu"),
    memory_mib: integer(value.memory_mib, 2_048, 1_048_576, "install_profile_memory"),
    disk_mib: integer(value.disk_mib, 10_240, 16_777_216, "install_profile_disk"),
    required_ports: Object.freeze(ports),
    dns_targets: Object.freeze(dnsTargets),
    runtime: responseChoice(value.runtime, ["docker"], "install_profile_runtime"),
    runtime_min_version: installerSemver(value.runtime_min_version, "install_profile_runtime_version"),
    filesystem: responseChoice(value.filesystem, ["local"], "install_profile_filesystem"),
    proxy_modes: Object.freeze(proxyModes),
    ca_mode: responseChoice(value.ca_mode, ["system", "system_or_custom"], "install_profile_ca"),
  });
}

function validateInstallerSignature(value) {
  plain(value, "installer_signature");
  exact(value, ["algorithm", "key_id", "public_key", "value"], "installer_signature");
  const keyId = responseText(value.key_id, 80, "installer_key_id");
  const publicKey = responseText(value.public_key, 43, "installer_public_key");
  const signature = responseText(value.value, 86, "installer_signature_value");
  if (!INSTALLER_KEY_ID.test(keyId) || publicKey.length !== 43 || signature.length !== 86
    || !INSTALLER_SIGNATURE.test(publicKey) || !INSTALLER_SIGNATURE.test(signature)) {
    invalidWorkspaceResponse("installer_signature");
  }
  return Object.freeze({
    algorithm: responseChoice(value.algorithm, ["Ed25519"], "installer_signature_algorithm"),
    key_id: keyId,
    public_key: publicKey,
    value: signature,
  });
}

function validateInstallerDownload(value, releasesById) {
  plain(value, "installer_download");
  exact(value, [
    "artifact_id", "download_path", "filename", "release_id", "sha256", "size_bytes",
  ], "installer_download");
  const releaseId = responseId(value.release_id, INSTALLER_RELEASE_ID, "installer_release_id");
  const artifactId = responseId(value.artifact_id, INSTALLER_ARTIFACT_ID, "installer_artifact_id");
  const artifact = releasesById.get(releaseId)?.manifest.artifacts.find(
    (item) => item.artifact_id === artifactId,
  );
  const path = responseText(value.download_path, 240, "installer_download_path");
  if (!artifact || path !== `/edge/installers/${releaseId}/artifacts/${artifactId}`
    || value.filename !== artifact.filename || value.sha256 !== artifact.sha256
    || value.size_bytes !== artifact.size_bytes) {
    invalidWorkspaceResponse("installer_download");
  }
  return Object.freeze({
    artifact_id: artifactId,
    release_id: releaseId,
    filename: artifact.filename,
    download_path: path,
    sha256: artifact.sha256,
    size_bytes: artifact.size_bytes,
  });
}

function validateEdgePreflightSummary(value) {
  plain(value, "installer_preflight");
  exact(value, [
    "checks", "completed_at", "deployment_id", "duration_ms", "generation",
    "install_profile_id", "outcome", "preflight_report_id", "received_at", "release_id",
    "started_at",
  ], "installer_preflight");
  boundedArray(value.checks, 32, "installer_preflight_checks");
  if (value.checks.length !== PREFLIGHT_CHECK_CODES.length) {
    invalidWorkspaceResponse("installer_preflight_checks");
  }
  const checks = value.checks.map((item, index) => validatePreflightCheck(
    item, PREFLIGHT_CHECK_CODES[index],
  ));
  const outcome = responseChoice(value.outcome, ["passed", "failed"], "installer_preflight_outcome");
  const expectedOutcome = checks.some((item) => item.required && item.outcome === "failed")
    ? "failed" : "passed";
  const startedAt = timestamp(value.started_at, "installer_preflight_started_at");
  const completedAt = timestamp(value.completed_at, "installer_preflight_completed_at");
  const durationMs = integer(value.duration_ms, 0, 120_000, "installer_preflight_duration");
  if (outcome !== expectedOutcome || Date.parse(completedAt) < Date.parse(startedAt)
    || Math.abs(Date.parse(completedAt) - Date.parse(startedAt) - durationMs) > 2_000) {
    invalidWorkspaceResponse("installer_preflight");
  }
  return Object.freeze({
    preflight_report_id: responseId(
      value.preflight_report_id, PREFLIGHT_REPORT_ID, "preflight_report_id",
    ),
    deployment_id: responseId(value.deployment_id, DEPLOYMENT_ID, "deployment_id"),
    release_id: responseId(value.release_id, INSTALLER_RELEASE_ID, "installer_release_id"),
    install_profile_id: responseId(
      value.install_profile_id, INSTALL_PROFILE_ID, "install_profile_id",
    ),
    generation: integer(value.generation, 1, 2_147_483_647, "installer_generation"),
    outcome,
    checks: Object.freeze(checks),
    duration_ms: durationMs,
    started_at: startedAt,
    completed_at: completedAt,
    received_at: timestamp(value.received_at, "installer_preflight_received_at"),
  });
}

function validatePreflightCheck(value, expectedCode) {
  plain(value, "installer_preflight_check");
  exact(value, ["code", "duration_ms", "outcome", "reason_code", "required"], "installer_preflight_check");
  const code = installerCode(value.code, "installer_preflight_code");
  if (code !== expectedCode) invalidWorkspaceResponse("installer_preflight_checks");
  return Object.freeze({
    code,
    outcome: responseChoice(
      value.outcome, ["passed", "warning", "failed"], "installer_preflight_check_outcome",
    ),
    required: boolean(value.required, "installer_preflight_required"),
    reason_code: installerCode(value.reason_code, "installer_preflight_reason"),
    duration_ms: integer(value.duration_ms, 0, 120_000, "installer_preflight_check_duration"),
  });
}

function validateEdgeInstallationSummary(value) {
  plain(value, "installation");
  exact(value, [
    "compensation_status", "completed_at", "current_step", "deployment_id", "failed_at",
    "failure_reason_code", "generation", "install_profile_id", "installation_id",
    "journal_sha256", "last_receipt_sha256", "preflight_report_id", "release_id",
    "retain_audit", "retain_config", "retain_secrets", "revision", "started_at", "status",
    "updated_at",
  ], "installation");
  const startedAt = timestamp(value.started_at, "installation_started_at");
  const updatedAt = timestamp(value.updated_at, "installation_updated_at");
  const completedAt = nullableTimestamp(value.completed_at, "installation_completed_at");
  const failedAt = nullableTimestamp(value.failed_at, "installation_failed_at");
  if (Date.parse(updatedAt) < Date.parse(startedAt)
    || (completedAt && Date.parse(completedAt) < Date.parse(startedAt))
    || (failedAt && Date.parse(failedAt) < Date.parse(startedAt))) {
    invalidWorkspaceResponse("installation_times");
  }
  return Object.freeze({
    installation_id: responseId(value.installation_id, INSTALLATION_ID, "installation_id"),
    deployment_id: responseId(value.deployment_id, DEPLOYMENT_ID, "deployment_id"),
    release_id: responseId(value.release_id, INSTALLER_RELEASE_ID, "installer_release_id"),
    install_profile_id: responseId(
      value.install_profile_id, INSTALL_PROFILE_ID, "install_profile_id",
    ),
    preflight_report_id: responseId(
      value.preflight_report_id, PREFLIGHT_REPORT_ID, "preflight_report_id",
    ),
    generation: integer(value.generation, 1, 2_147_483_647, "installation_generation"),
    status: responseChoice(value.status, [
      "preflight_passed", "installing", "installed", "connecting", "active", "failed",
      "uninstalling", "uninstalled",
    ], "installation_status"),
    current_step: value.current_step === null ? null : responseChoice(value.current_step, [
      "staging_prepared", "artifacts_copied", "runtime_validated", "runtime_started",
      "health_verified", "release_committed", "device_registered", "initial_snapshot",
    ], "installation_step"),
    revision: integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "installation_revision"),
    journal_sha256: digest(value.journal_sha256, "installation_journal_sha256"),
    last_receipt_sha256: value.last_receipt_sha256 === null
      ? null : digest(value.last_receipt_sha256, "installation_receipt_sha256"),
    compensation_status: responseChoice(value.compensation_status, [
      "not_required", "pending", "completed", "failed",
    ], "installation_compensation_status"),
    retain_config: boolean(value.retain_config, "installation_retain_config"),
    retain_audit: boolean(value.retain_audit, "installation_retain_audit"),
    retain_secrets: boolean(value.retain_secrets, "installation_retain_secrets"),
    failure_reason_code: value.failure_reason_code === null
      ? null : installerCode(value.failure_reason_code, "installation_failure_reason"),
    started_at: startedAt,
    updated_at: updatedAt,
    completed_at: completedAt,
    failed_at: failedAt,
  });
}

function installerText(value, minimum, maximum, label) {
  const text = responseText(value, maximum, label);
  if (text.length < minimum) invalidWorkspaceResponse(label);
  return text;
}

function installerSemver(value, label) {
  const text = responseText(value, 64, label);
  if (!SEMVER.test(text)) invalidWorkspaceResponse(label);
  return text;
}

function compareSemver(left, right) {
  const first = left.split(".").map(Number);
  const second = right.split(".").map(Number);
  for (let index = 0; index < 3; index += 1) {
    if (first[index] !== second[index]) return first[index] - second[index];
  }
  return 0;
}

function installerCode(value, label) {
  const text = responseText(value, 80, label);
  if (!INSTALLER_CODE.test(text)) invalidWorkspaceResponse(label);
  return text;
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

function validateSecretReference(value) {
  plain(value, "secret_reference");
  exact(value, SECRET_REFERENCE_KEYS, "secret_reference");
  const reference = Object.freeze({
    schema_version: responseChoice(value.schema_version, ["1"], "secret_schema_version"),
    reference_id: responseId(value.reference_id, SECRET_REFERENCE_ID, "reference_id"),
    deployment_id: responseId(value.deployment_id, DEPLOYMENT_ID, "deployment_id"),
    tenant_id: responseId(value.tenant_id, OWNER_ID, "tenant_id"),
    workspace_id: responseId(value.workspace_id, OWNER_ID, "workspace_id"),
    provider_connection_id: responseId(
      value.provider_connection_id,
      OWNER_ID,
      "provider_connection_id",
    ),
    label: responseSafeLabel(value.label),
    kind: responseChoice(value.kind, [
      "api_key", "bearer_token", "basic_password", "oauth_client_secret",
    ], "secret_kind"),
    status: responseChoice(value.status, [
      "active", "rotating", "degraded", "disabled",
    ], "secret_status"),
    revision: integer(value.revision, 1, 2_147_483_647, "secret_revision"),
    active_version: nullableInteger(
      value.active_version,
      1,
      2_147_483_647,
      "active_version",
    ),
    last_test_outcome: value.last_test_outcome === null ? null : responseChoice(
      value.last_test_outcome,
      [
        "succeeded", "authentication_failed", "authorization_failed", "rate_limited",
        "timeout", "tls_failed", "network_unreachable", "provider_unavailable", "unsupported",
      ],
      "last_test_outcome",
    ),
    capabilities: responseCapabilities(value.capabilities),
    rotated_at: value.rotated_at === null ? null : responseUtcTime(value.rotated_at, "rotated_at"),
    updated_at: responseUtcTime(value.updated_at, "updated_at"),
  });
  if ((reference.status === "active" && reference.active_version === null)
    || (reference.status === "disabled" && reference.active_version !== null)
    || (reference.last_test_outcome === null && reference.capabilities.length)) {
    invalidWorkspaceResponse("secret_reference_state");
  }
  return reference;
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
  const lagSeconds = value.telemetry_lag_seconds === null
    ? null
    : integer(value.telemetry_lag_seconds, 0, Number.MAX_SAFE_INTEGER, "telemetry_lag_seconds", true);
  const backlogSeconds = value.telemetry_backlog_seconds === null
    ? null
    : integer(value.telemetry_backlog_seconds, 0, Number.MAX_SAFE_INTEGER, "telemetry_backlog_seconds", true);
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
    telemetry_last_receipt_at: nullableTimestamp(
      value.telemetry_last_receipt_at,
      "telemetry_last_receipt_at",
      true,
    ),
    telemetry_lag_seconds: lagSeconds,
    telemetry_backlog_seconds: backlogSeconds,
    telemetry_cpu_bucket: responseChoice(
      value.telemetry_cpu_bucket,
      ["unknown", "low", "medium", "high", "critical"],
      "telemetry_cpu_bucket",
      true,
    ),
    telemetry_memory_bucket: responseChoice(
      value.telemetry_memory_bucket,
      ["unknown", "low", "medium", "high", "critical"],
      "telemetry_memory_bucket",
      true,
    ),
    telemetry_error_rate_basis_points: integer(
      value.telemetry_error_rate_basis_points,
      0,
      10_000,
      "telemetry_error_rate_basis_points",
      true,
    ),
    telemetry_failure_code: responseChoice(value.telemetry_failure_code, [
      "none", "capacity_exhausted", "config_unavailable", "internal_error",
      "network_unreachable", "policy_denied", "provider_5xx",
      "provider_rate_limited", "provider_timeout", "request_cancelled",
    ], "telemetry_failure_code", true),
    updated_at: timestamp(value.updated_at, "updated_at", true),
  });
}

function validateTelemetryWatermark(value) {
  plain(value, "telemetry_watermark");
  exact(value, [
    "deployment_id", "policy", "cursors", "usage", "failures", "last_receipt_at",
    "lag_seconds", "backlog_seconds", "request_id", "trace_id",
  ], "telemetry_watermark");
  boundedArray(value.cursors, 4, "telemetry_cursors");
  boundedArray(value.failures, 32, "telemetry_failures");
  const cursors = value.cursors.map(validateTelemetryCursor);
  unique(cursors.map((item) => item.stream), "telemetry_streams");
  return Object.freeze({
    deployment_id: responseId(value.deployment_id, DEPLOYMENT_ID, "deployment_id"),
    policy: value.policy === null ? null : validateTelemetryPolicy(value.policy),
    cursors: Object.freeze(cursors),
    usage: value.usage === null ? null : validateTelemetryUsage(value.usage),
    failures: Object.freeze(value.failures.map(validateTelemetryFailure)),
    last_receipt_at: nullableTimestamp(value.last_receipt_at, "last_receipt_at"),
    lag_seconds: nullableInteger(
      value.lag_seconds,
      0,
      Number.MAX_SAFE_INTEGER,
      "lag_seconds",
    ),
    backlog_seconds: nullableInteger(
      value.backlog_seconds,
      0,
      Number.MAX_SAFE_INTEGER,
      "backlog_seconds",
    ),
    ...trace(value),
  });
}

function validateTelemetryPolicy(value) {
  plain(value, "telemetry_policy");
  exact(value, [
    "policy_id", "deployment_id", "telemetry_level", "revision", "updated_at",
  ], "telemetry_policy");
  return Object.freeze({
    policy_id: responseId(value.policy_id, TELEMETRY_POLICY_ID, "policy_id"),
    deployment_id: responseId(value.deployment_id, DEPLOYMENT_ID, "deployment_id"),
    telemetry_level: responseChoice(
      value.telemetry_level,
      ["minimal", "operational", "billing"],
      "telemetry_level",
    ),
    revision: integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "policy_revision"),
    updated_at: timestamp(value.updated_at, "policy_updated_at"),
  });
}

function validateTelemetryCursor(value) {
  plain(value, "telemetry_cursor");
  exact(value, [
    "cursor_id", "stream", "epoch_id", "contiguous_sequence",
    "highest_seen_sequence", "expected_sequence", "updated_at",
  ], "telemetry_cursor");
  const contiguous = integer(
    value.contiguous_sequence,
    0,
    Number.MAX_SAFE_INTEGER,
    "contiguous_sequence",
  );
  const highest = integer(
    value.highest_seen_sequence,
    contiguous,
    Number.MAX_SAFE_INTEGER,
    "highest_seen_sequence",
  );
  const expected = integer(
    value.expected_sequence,
    1,
    Number.MAX_SAFE_INTEGER,
    "expected_sequence",
  );
  if (expected !== contiguous + 1) invalidWorkspaceResponse("expected_sequence");
  return Object.freeze({
    cursor_id: responseId(value.cursor_id, TELEMETRY_CURSOR_ID, "cursor_id"),
    stream: responseChoice(
      value.stream,
      ["health", "operational", "usage", "failure"],
      "telemetry_stream",
    ),
    epoch_id: responseId(value.epoch_id, TELEMETRY_EPOCH_ID, "epoch_id"),
    contiguous_sequence: contiguous,
    highest_seen_sequence: highest,
    expected_sequence: expected,
    updated_at: timestamp(value.updated_at, "cursor_updated_at"),
  });
}

function validateTelemetryUsage(value) {
  plain(value, "telemetry_usage");
  exact(value, [
    "request_count", "input_tokens", "output_tokens", "cache_tokens",
    "cost_cny_micros", "cost_usd_micros",
  ], "telemetry_usage");
  return Object.freeze(Object.fromEntries(Object.keys(value).map((key) => [
    key,
    integer(value[key], 0, Number.MAX_SAFE_INTEGER, key),
  ])));
}

function validateTelemetryFailure(value) {
  plain(value, "telemetry_failure");
  exact(value, ["component_code", "failure_code", "count"], "telemetry_failure");
  return Object.freeze({
    component_code: responseChoice(value.component_code, [
      "gateway", "config_sync", "provider_pool", "usage_spool", "telemetry_sync",
    ], "component_code"),
    failure_code: responseChoice(value.failure_code, [
      "capacity_exhausted", "config_unavailable", "internal_error",
      "network_unreachable", "policy_denied", "provider_5xx",
      "provider_rate_limited", "provider_timeout", "request_cancelled",
    ], "failure_code"),
    count: integer(value.count, 1, Number.MAX_SAFE_INTEGER, "failure_count"),
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

function responseSafeLabel(value) {
  const text = responseText(value, 80, "secret_label");
  if (!SAFE_LABEL.test(text)) invalidWorkspaceResponse("secret_label");
  return text;
}

function responseCapabilities(value) {
  boundedArray(value, 3, "secret_capabilities");
  const capabilities = value.map((item) => responseChoice(
    item,
    ["chat_completions", "embeddings", "models"],
    "secret_capability",
  ));
  if (capabilities.join("\0") !== [...new Set(capabilities)].sort().join("\0")) {
    invalidWorkspaceResponse("secret_capabilities");
  }
  return Object.freeze(capabilities);
}

function responseUtcTime(value, label) {
  const text = responseText(value, 24, label);
  if (!UTC_TIME.test(text)) invalidWorkspaceResponse(label);
  return text;
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
