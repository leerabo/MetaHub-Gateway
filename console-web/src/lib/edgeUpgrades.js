/** PRD-127 strict Workspace and Platform fleet upgrade clients. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const IDS = Object.freeze({
  deployment: /^edp_[0-9a-f]{32}$/u,
  release: /^ger_[0-9a-f]{32}$/u,
  installerRelease: /^irl_[0-9a-f]{32}$/u,
  rollout: /^rol_[0-9a-f]{32}$/u,
  batch: /^rbt_[0-9a-f]{32}$/u,
  target: /^rtt_[0-9a-f]{32}$/u,
  rollback: /^rba_[0-9a-f]{32}$/u,
  maintenance: /^mwn_[0-9a-f]{32}$/u,
});
const SHA256 = /^[0-9a-f]{64}$/u;
const SEMVER = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/u;
const PUBLIC_KEY = /^[A-Za-z0-9_-]{43}$/u;
const SIGNATURE = /^[A-Za-z0-9_-]{86}$/u;
const REASON = /^[a-z][a-z0-9_.-]{1,79}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const STEP_UP = /^[A-Za-z0-9_-]{43,128}$/u;
const CHAIN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const CHANNELS = Object.freeze(["preview", "stable", "extended"]);
const PLATFORM_CSRF_COOKIE = "__Host-metahub-platform-csrf";

export async function getWorkspaceUpgrade({ controlApiUrl, deploymentId, signal } = {}) {
  const id = clientId(deploymentId, IDS.deployment, "deployment_id", "workspace");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/upgrade`,
    { signal },
    "workspace",
  );
  const response = workspaceUpgrade(result.payload);
  if (response.deployment_id !== id || response.head.deployment_id !== id
    || (response.maintenance !== null && response.maintenance.deployment_id !== id)
    || response.rollouts.some((item) => item.deployment_id !== id)
    || response.rollback_attempts.some((item) => item.deployment_id !== id)) {
    invalidResponse("workspace", "upgrade_owner");
  }
  validateEtag(result.response, response.head.revision, "workspace");
  return response;
}

export async function updateWorkspaceMaintenance({
  controlApiUrl,
  deploymentId,
  releaseChannel,
  timezoneName,
  weekdays,
  startMinute,
  durationMinutes,
  freezeStartsAt = null,
  freezeEndsAt = null,
  status = "active",
  expectedRevision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(deploymentId, IDS.deployment, "deployment_id", "workspace");
  const days = clientWeekdays(weekdays, "workspace");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/maintenance-window`,
    {
      method: "PATCH",
      csrf: true,
      idempotencyKey: idempotency(idempotencyKey, "workspace"),
      body: {
        release_channel: choice(releaseChannel, CHANNELS, "release_channel", "workspace"),
        timezone_name: timezone(timezoneName),
        weekdays: days,
        start_minute: integer(startMinute, 0, 1439, "start_minute", "workspace"),
        duration_minutes: integer(
          durationMinutes, 30, 720, "duration_minutes", "workspace",
        ),
        freeze_starts_at: optionalClientTimestamp(freezeStartsAt, "freeze_starts_at", "workspace"),
        freeze_ends_at: optionalClientTimestamp(freezeEndsAt, "freeze_ends_at", "workspace"),
        status: choice(status, ["active", "disabled"], "status", "workspace"),
        expected_revision: integer(
          expectedRevision, 0, Number.MAX_SAFE_INTEGER, "expected_revision", "workspace",
        ),
        reason_code: reason(reasonCode, "workspace"),
      },
      signal,
    },
    "workspace",
  );
  const response = workspaceUpgrade(result.payload);
  if (response.deployment_id !== id || response.maintenance === null) {
    invalidResponse("workspace", "maintenance_owner");
  }
  validateEtag(result.response, response.maintenance.revision, "workspace");
  return response;
}

export async function requestWorkspaceRollback({
  controlApiUrl,
  deploymentId,
  targetReleaseId = null,
  expectedHeadRevision,
  reasonCode,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(deploymentId, IDS.deployment, "deployment_id", "workspace");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/rollback`,
    {
      method: "POST",
      csrf: true,
      stepUpGrant: stepUp(stepUpGrant, "workspace"),
      idempotencyKey: idempotency(idempotencyKey, "workspace"),
      body: {
        target_release_id: targetReleaseId === null || targetReleaseId === ""
          ? null : clientId(targetReleaseId, IDS.release, "target_release_id", "workspace"),
        expected_head_revision: integer(
          expectedHeadRevision, 1, Number.MAX_SAFE_INTEGER, "expected_head_revision", "workspace",
        ),
        reason_code: reason(reasonCode, "workspace"),
      },
      signal,
    },
    "workspace",
  );
  exact(result.payload, ["rollback", "idempotent_replay", "request_id", "trace_id"],
    "rollback_response", "workspace");
  const rollback = rollbackAttempt(result.payload.rollback, "workspace");
  if (rollback.deployment_id !== id) invalidResponse("workspace", "rollback_owner");
  responseBoolean(result.payload.idempotent_replay, "idempotent_replay", "workspace");
  trace(result.payload, "workspace");
  validateEtag(result.response, rollback.revision, "workspace");
  return Object.freeze({
    rollback,
    idempotent_replay: result.payload.idempotent_replay,
    ...trace(result.payload, "workspace"),
  });
}

export async function listPlatformGatewayReleases({ controlApiUrl, signal } = {}) {
  const result = await request(controlApiUrl, "/platform/releases", { signal }, "platform");
  exact(result.payload, ["releases", "request_id", "trace_id"],
    "release_list", "platform");
  array(result.payload.releases, 100, "releases", "platform");
  const releases = result.payload.releases.map(releaseSummary);
  unique(releases.map((item) => item.release_id), "platform", "releases");
  return Object.freeze({ releases: Object.freeze(releases), ...trace(result.payload, "platform") });
}

export async function mutatePlatformGatewayRelease({
  controlApiUrl,
  action,
  releaseId = null,
  installerReleaseId = null,
  releaseChannel = null,
  schemaFrom = null,
  schemaTo = null,
  downMigrationSupported = null,
  compatibleFromVersions = null,
  expectedRevision = null,
  reasonCode,
  stepUpGrant = null,
  idempotencyKey,
  signal,
} = {}) {
  const operation = choice(action, ["submit", "approve", "withdraw"], "action", "platform");
  const submitting = operation === "submit";
  const body = {
    action: operation,
    release_id: submitting ? null : clientId(releaseId, IDS.release, "release_id", "platform"),
    installer_release_id: submitting
      ? clientId(installerReleaseId, IDS.installerRelease, "installer_release_id", "platform")
      : null,
    release_channel: submitting
      ? choice(releaseChannel, CHANNELS, "release_channel", "platform") : null,
    schema_from: submitting ? integer(schemaFrom, 1, 9999, "schema_from", "platform") : null,
    schema_to: submitting ? integer(schemaTo, 1, 9999, "schema_to", "platform") : null,
    down_migration_supported: submitting
      ? clientBoolean(downMigrationSupported, "down_migration_supported", "platform") : null,
    compatible_from_versions: submitting
      ? clientVersions(compatibleFromVersions, "platform") : null,
    expected_revision: submitting ? null : integer(
      expectedRevision, 1, Number.MAX_SAFE_INTEGER, "expected_revision", "platform",
    ),
    reason_code: reason(reasonCode, "platform"),
  };
  const result = await request(
    controlApiUrl,
    "/platform/releases",
    {
      method: "POST",
      csrf: true,
      stepUpGrant: submitting ? null : stepUp(stepUpGrant, "platform"),
      idempotencyKey: idempotency(idempotencyKey, "platform"),
      body,
      signal,
    },
    "platform",
  );
  exact(result.payload, ["release", "idempotent_replay", "request_id", "trace_id"],
    "release_response", "platform");
  const release = releaseSummary(result.payload.release);
  responseBoolean(result.payload.idempotent_replay, "idempotent_replay", "platform");
  validateEtag(result.response, release.revision, "platform");
  return Object.freeze({
    release,
    idempotent_replay: result.payload.idempotent_replay,
    ...trace(result.payload, "platform"),
  });
}

export async function listPlatformRollouts({ controlApiUrl, signal } = {}) {
  const result = await request(controlApiUrl, "/platform/rollouts", { signal }, "platform");
  exact(result.payload, ["rollouts", "request_id", "trace_id"],
    "rollout_list", "platform");
  array(result.payload.rollouts, 100, "rollouts", "platform");
  const rollouts = result.payload.rollouts.map((item) => rolloutSummary(item, "platform"));
  unique(rollouts.map((item) => item.rollout_id), "platform", "rollouts");
  return Object.freeze({ rollouts: Object.freeze(rollouts), ...trace(result.payload, "platform") });
}

export async function createPlatformRollout({
  controlApiUrl,
  releaseId,
  deploymentIds,
  canaryBasisPoints = 100,
  observationSeconds = 1800,
  batchSize = 50,
  failureThresholdBasisPoints = 1000,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const release = clientId(releaseId, IDS.release, "release_id", "platform");
  const targets = clientIds(deploymentIds, IDS.deployment, 1000, "deployment_ids", "platform");
  const result = await request(
    controlApiUrl,
    `/platform/releases/${encodeURIComponent(release)}/rollouts`,
    {
      method: "POST",
      csrf: true,
      idempotencyKey: idempotency(idempotencyKey, "platform"),
      body: {
        deployment_ids: targets,
        canary_basis_points: integer(
          canaryBasisPoints, 100, 10000, "canary_basis_points", "platform",
        ),
        observation_seconds: integer(
          observationSeconds, 1800, 86400, "observation_seconds", "platform",
        ),
        batch_size: integer(batchSize, 1, 1000, "batch_size", "platform"),
        failure_threshold_basis_points: integer(
          failureThresholdBasisPoints, 1, 10000,
          "failure_threshold_basis_points", "platform",
        ),
        reason_code: reason(reasonCode, "platform"),
      },
      signal,
    },
    "platform",
  );
  return rolloutResponse(result);
}

export async function mutatePlatformRollout({
  controlApiUrl,
  rolloutId,
  action,
  expectedRevision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(rolloutId, IDS.rollout, "rollout_id", "platform");
  const operation = choice(action, ["pause", "resume"], "action", "platform");
  const result = await request(
    controlApiUrl,
    `/fleet/rollouts/${encodeURIComponent(id)}/pause`,
    {
      method: "POST",
      csrf: true,
      idempotencyKey: idempotency(idempotencyKey, "platform"),
      body: {
        action: operation,
        expected_revision: integer(
          expectedRevision, 1, Number.MAX_SAFE_INTEGER, "expected_revision", "platform",
        ),
        reason_code: reason(reasonCode, "platform"),
      },
      signal,
    },
    "platform",
  );
  const response = rolloutResponse(result);
  if (response.rollout.rollout_id !== id) invalidResponse("platform", "rollout_owner");
  return response;
}

export async function getFleetCompatibility({
  controlApiUrl, releaseId, deploymentId, signal,
} = {}) {
  const release = clientId(releaseId, IDS.release, "release_id", "platform");
  const deployment = clientId(deploymentId, IDS.deployment, "deployment_id", "platform");
  const query = new URLSearchParams({ release_id: release, deployment_id: deployment });
  const result = await request(
    controlApiUrl, `/fleet/compatibility?${query}`, { signal }, "platform",
  );
  exact(result.payload, ["items", "request_id", "trace_id"],
    "compatibility_response", "platform");
  array(result.payload.items, 1, "items", "platform");
  if (result.payload.items.length !== 1) invalidResponse("platform", "compatibility_count");
  const item = compatibility(result.payload.items[0]);
  if (item.release_id !== release || item.deployment_id !== deployment) {
    invalidResponse("platform", "compatibility_owner");
  }
  return Object.freeze({ item, ...trace(result.payload, "platform") });
}

function workspaceUpgrade(value) {
  exact(value, [
    "deployment_id", "maintenance", "head", "rollouts", "rollback_attempts",
    "request_id", "trace_id",
  ], "workspace_upgrade", "workspace");
  array(value.rollouts, 100, "rollouts", "workspace");
  array(value.rollback_attempts, 50, "rollback_attempts", "workspace");
  const rollouts = value.rollouts.map((item) => rolloutTarget(item, "workspace"));
  const rollbacks = value.rollback_attempts.map((item) => rollbackAttempt(item, "workspace"));
  unique(rollouts.map((item) => item.rollout_target_id), "workspace", "rollouts");
  unique(rollbacks.map((item) => item.rollback_attempt_id), "workspace", "rollbacks");
  return Object.freeze({
    deployment_id: responseId(value.deployment_id, IDS.deployment, "deployment_id", "workspace"),
    maintenance: value.maintenance === null ? null : maintenance(value.maintenance, "workspace"),
    head: releaseHead(value.head),
    rollouts: Object.freeze(rollouts),
    rollback_attempts: Object.freeze(rollbacks),
    ...trace(value, "workspace"),
  });
}

function releaseSummary(value) {
  exact(value, [
    "release", "status", "revision", "submitted_by_operator_id", "approved_by_operator_id",
    "submitted_at", "approved_at", "withdrawn_at",
  ], "release_summary", "platform");
  exact(value.release, ["manifest", "manifest_sha256", "signature"],
    "release_envelope", "platform");
  const manifest = releaseManifest(value.release.manifest);
  responsePattern(value.release.manifest_sha256, SHA256, "manifest_sha256", "platform");
  signature(value.release.signature);
  return Object.freeze({
    release_id: manifest.release_id,
    installer_release_id: manifest.installer_release_id,
    version: manifest.version,
    release_channel: manifest.release_channel,
    schema_from: manifest.schema_from,
    schema_to: manifest.schema_to,
    down_migration_supported: manifest.down_migration_supported,
    compatible_from_versions: manifest.compatible_from_versions,
    artifact_count: manifest.artifact_count,
    manifest_sha256: value.release.manifest_sha256,
    status: responseChoice(
      value.status, ["approval_pending", "released", "withdrawn"], "status", "platform",
    ),
    revision: responseInteger(value.revision, 1, "revision", "platform"),
    submitted_by_operator_id: responseText(
      value.submitted_by_operator_id, 160, "submitted_by_operator_id", "platform",
    ),
    approved_by_operator_id: nullableText(
      value.approved_by_operator_id, 160, "approved_by_operator_id", "platform",
    ),
    submitted_at: timestamp(value.submitted_at, "submitted_at", "platform"),
    approved_at: nullableTimestamp(value.approved_at, "approved_at", "platform"),
    withdrawn_at: nullableTimestamp(value.withdrawn_at, "withdrawn_at", "platform"),
  });
}

function releaseManifest(value) {
  exact(value, [
    "schema_version", "release_id", "installer_release_id", "version", "release_channel",
    "signature_purpose", "artifact_set_sha256", "sbom_sha256", "schema_from", "schema_to",
    "down_migration_supported", "backup_required", "compatible_from_versions",
    "agent_min_version", "agent_max_version", "control_min_version", "control_max_version",
    "artifacts",
  ], "release_manifest", "platform");
  responseChoice(value.schema_version, ["metahub.edge-upgrade-release/v1"],
    "schema_version", "platform");
  responseChoice(value.signature_purpose, ["metahub.edge-upgrade.release/v1"],
    "signature_purpose", "platform");
  array(value.artifacts, 32, "artifacts", "platform");
  if (value.artifacts.length < 1) invalidResponse("platform", "artifacts");
  value.artifacts.forEach(releaseArtifact);
  unique(value.artifacts.map((item) => item.artifact_id), "platform", "artifacts");
  responsePattern(value.artifact_set_sha256, SHA256, "artifact_set_sha256", "platform");
  responsePattern(value.sbom_sha256, SHA256, "sbom_sha256", "platform");
  responseBoolean(value.backup_required, "backup_required", "platform");
  const agentMin = responsePattern(value.agent_min_version, SEMVER, "agent_min_version", "platform");
  const agentMax = responsePattern(value.agent_max_version, SEMVER, "agent_max_version", "platform");
  const controlMin = responsePattern(
    value.control_min_version, SEMVER, "control_min_version", "platform",
  );
  const controlMax = responsePattern(
    value.control_max_version, SEMVER, "control_max_version", "platform",
  );
  if (compareSemver(agentMin, agentMax) > 0 || compareSemver(controlMin, controlMax) > 0) {
    invalidResponse("platform", "runtime_version_range");
  }
  const versions = responseVersions(value.compatible_from_versions, "platform");
  return Object.freeze({
    release_id: responseId(value.release_id, IDS.release, "release_id", "platform"),
    installer_release_id: responseId(
      value.installer_release_id, IDS.installerRelease, "installer_release_id", "platform",
    ),
    version: responsePattern(value.version, SEMVER, "version", "platform"),
    release_channel: responseChoice(value.release_channel, CHANNELS, "release_channel", "platform"),
    schema_from: responseInteger(value.schema_from, 1, "schema_from", "platform", 9999),
    schema_to: responseInteger(value.schema_to, 1, "schema_to", "platform", 9999),
    down_migration_supported: responseBoolean(
      value.down_migration_supported, "down_migration_supported", "platform",
    ),
    compatible_from_versions: versions,
    backup_required: value.backup_required,
    agent_min_version: agentMin,
    agent_max_version: agentMax,
    control_min_version: controlMin,
    control_max_version: controlMax,
    artifact_count: value.artifacts.length,
  });
}

function releaseArtifact(value) {
  exact(value, [
    "artifact_id", "installer_artifact_id", "kind", "filename", "media_type", "sha256",
    "sbom_sha256", "size_bytes",
  ], "release_artifact", "platform");
  responseId(value.artifact_id, /^gar_[0-9a-f]{32}$/u, "artifact_id", "platform");
  responseId(value.installer_artifact_id, /^ira_[0-9a-f]{32}$/u,
    "installer_artifact_id", "platform");
  responseChoice(value.kind, ["cli", "compose", "container_bundle", "profile", "sbom"],
    "kind", "platform");
  responsePattern(value.filename, /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/u,
    "filename", "platform");
  responsePattern(value.media_type, /^[a-z0-9][a-z0-9.+-]{0,47}\/[A-Za-z0-9][A-Za-z0-9.+-]{0,46}$/u,
    "media_type", "platform");
  responsePattern(value.sha256, SHA256, "sha256", "platform");
  responsePattern(value.sbom_sha256, SHA256, "sbom_sha256", "platform");
  responseInteger(value.size_bytes, 1, "size_bytes", "platform", 8_589_934_592);
}

function signature(value) {
  exact(value, ["algorithm", "key_id", "public_key", "value"], "signature", "platform");
  responseChoice(value.algorithm, ["Ed25519"], "algorithm", "platform");
  responseText(value.key_id, 80, "key_id", "platform");
  responsePattern(value.public_key, PUBLIC_KEY, "public_key", "platform");
  responsePattern(value.value, SIGNATURE, "signature_value", "platform");
}

function rolloutResponse(result) {
  exact(result.payload, ["rollout", "idempotent_replay", "request_id", "trace_id"],
    "rollout_response", "platform");
  const rollout = rolloutSummary(result.payload.rollout, "platform");
  responseBoolean(result.payload.idempotent_replay, "idempotent_replay", "platform");
  validateEtag(result.response, rollout.revision, "platform");
  return Object.freeze({
    rollout,
    idempotent_replay: result.payload.idempotent_replay,
    ...trace(result.payload, "platform"),
  });
}

function rolloutSummary(value, domain) {
  exact(value, [
    "rollout_id", "release_id", "release_version", "release_channel", "status",
    "generation", "cancel_watermark", "canary_basis_points", "observation_seconds",
    "target_count", "succeeded_count", "failed_count", "current_batch_sequence",
    "pause_reason_code", "revision", "batches", "targets", "created_at", "updated_at",
  ], "rollout", domain);
  array(value.batches, 1001, "batches", domain);
  array(value.targets, 1000, "targets", domain);
  const targets = value.targets.map((item) => rolloutTarget(item, domain));
  const batches = value.batches.map((item) => rolloutBatch(item, domain));
  const targetCount = responseInteger(value.target_count, 1, "target_count", domain, 100000);
  const succeeded = responseInteger(value.succeeded_count, 0, "succeeded_count", domain);
  const failed = responseInteger(value.failed_count, 0, "failed_count", domain);
  if (targets.length !== targetCount || succeeded + failed > targetCount) {
    invalidResponse(domain, "rollout_counts");
  }
  unique(targets.map((item) => item.rollout_target_id), domain, "targets");
  unique(batches.map((item) => item.rollout_batch_id), domain, "batches");
  return Object.freeze({
    rollout_id: responseId(value.rollout_id, IDS.rollout, "rollout_id", domain),
    release_id: responseId(value.release_id, IDS.release, "release_id", domain),
    release_version: responsePattern(value.release_version, SEMVER, "release_version", domain),
    release_channel: responseChoice(value.release_channel, CHANNELS, "release_channel", domain),
    status: responseChoice(value.status,
      ["preparing", "running", "paused", "completed", "failed", "cancelled"],
      "status", domain),
    generation: responseInteger(value.generation, 1, "generation", domain),
    cancel_watermark: responseInteger(value.cancel_watermark, 0, "cancel_watermark", domain),
    canary_basis_points: responseInteger(
      value.canary_basis_points, 100, "canary_basis_points", domain, 10000,
    ),
    observation_seconds: responseInteger(
      value.observation_seconds, 1800, "observation_seconds", domain, 86400,
    ),
    target_count: targetCount,
    succeeded_count: succeeded,
    failed_count: failed,
    current_batch_sequence: responseInteger(
      value.current_batch_sequence, 0, "current_batch_sequence", domain,
    ),
    pause_reason_code: nullableReason(value.pause_reason_code, "pause_reason_code", domain),
    revision: responseInteger(value.revision, 1, "revision", domain),
    batches: Object.freeze(batches),
    targets: Object.freeze(targets),
    created_at: timestamp(value.created_at, "created_at", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  });
}

function rolloutBatch(value, domain) {
  exact(value, [
    "rollout_batch_id", "sequence", "batch_kind", "status", "target_count",
    "succeeded_count", "failed_count", "observe_until", "updated_at",
  ], "rollout_batch", domain);
  const targetCount = responseInteger(value.target_count, 1, "target_count", domain, 1000);
  const succeeded = responseInteger(value.succeeded_count, 0, "succeeded_count", domain);
  const failed = responseInteger(value.failed_count, 0, "failed_count", domain);
  if (succeeded + failed > targetCount) invalidResponse(domain, "batch_counts");
  return Object.freeze({
    rollout_batch_id: responseId(value.rollout_batch_id, IDS.batch, "batch_id", domain),
    sequence: responseInteger(value.sequence, 0, "sequence", domain),
    batch_kind: responseChoice(value.batch_kind, ["canary", "progressive"], "batch_kind", domain),
    status: responseChoice(value.status,
      ["pending", "running", "observing", "completed", "failed", "cancelled"],
      "status", domain),
    target_count: targetCount,
    succeeded_count: succeeded,
    failed_count: failed,
    observe_until: nullableTimestamp(value.observe_until, "observe_until", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  });
}

function rolloutTarget(value, domain) {
  exact(value, [
    "rollout_target_id", "deployment_id", "batch_sequence", "stable_bucket",
    "current_version", "target_version", "status", "failure_reason_code", "updated_at",
  ], "rollout_target", domain);
  return Object.freeze({
    rollout_target_id: responseId(value.rollout_target_id, IDS.target, "target_id", domain),
    deployment_id: responseId(value.deployment_id, IDS.deployment, "deployment_id", domain),
    batch_sequence: responseInteger(value.batch_sequence, 0, "batch_sequence", domain),
    stable_bucket: responseInteger(value.stable_bucket, 0, "stable_bucket", domain, 9999),
    current_version: responseText(value.current_version, 64, "current_version", domain),
    target_version: responseText(value.target_version, 64, "target_version", domain),
    status: responseChoice(value.status, [
      "selected", "leased", "draining", "staging", "migrating", "health_check",
      "committing", "succeeded", "failed", "cancelled", "rolled_back",
    ], "status", domain),
    failure_reason_code: nullableReason(value.failure_reason_code, "failure_reason_code", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  });
}

function releaseHead(value) {
  exact(value, [
    "deployment_id", "current_release_id", "lkg_release_id", "current_version",
    "lkg_version", "desired_generation", "applied_generation", "lkg_generation",
    "revision", "updated_at",
  ], "release_head", "workspace");
  const desired = responseInteger(value.desired_generation, 0, "desired_generation", "workspace");
  const applied = responseInteger(value.applied_generation, 0, "applied_generation", "workspace");
  const lkg = responseInteger(value.lkg_generation, 0, "lkg_generation", "workspace");
  if (applied > desired || lkg > applied) invalidResponse("workspace", "generation_order");
  return Object.freeze({
    deployment_id: responseId(value.deployment_id, IDS.deployment, "deployment_id", "workspace"),
    current_release_id: nullableId(value.current_release_id, IDS.release, "current_release_id", "workspace"),
    lkg_release_id: nullableId(value.lkg_release_id, IDS.release, "lkg_release_id", "workspace"),
    current_version: responseText(value.current_version, 64, "current_version", "workspace"),
    lkg_version: responseText(value.lkg_version, 64, "lkg_version", "workspace"),
    desired_generation: desired,
    applied_generation: applied,
    lkg_generation: lkg,
    revision: responseInteger(value.revision, 1, "revision", "workspace"),
    updated_at: timestamp(value.updated_at, "updated_at", "workspace"),
  });
}

function maintenance(value, domain) {
  exact(value, [
    "maintenance_window_id", "deployment_id", "release_channel", "timezone_name", "weekdays",
    "start_minute", "duration_minutes", "freeze_starts_at", "freeze_ends_at", "status",
    "policy_generation", "revision", "reason_code", "updated_at",
  ], "maintenance", domain);
  const weekdays = responseWeekdays(value.weekdays, domain);
  const freezeStart = nullableTimestamp(value.freeze_starts_at, "freeze_starts_at", domain);
  const freezeEnd = nullableTimestamp(value.freeze_ends_at, "freeze_ends_at", domain);
  if ((freezeStart === null) !== (freezeEnd === null)
    || (freezeStart !== null && Date.parse(freezeEnd) <= Date.parse(freezeStart))) {
    invalidResponse(domain, "freeze_window");
  }
  return Object.freeze({
    maintenance_window_id: responseId(
      value.maintenance_window_id, IDS.maintenance, "maintenance_window_id", domain,
    ),
    deployment_id: responseId(value.deployment_id, IDS.deployment, "deployment_id", domain),
    release_channel: responseChoice(value.release_channel, CHANNELS, "release_channel", domain),
    timezone_name: responseText(value.timezone_name, 64, "timezone_name", domain),
    weekdays,
    start_minute: responseInteger(value.start_minute, 0, "start_minute", domain, 1439),
    duration_minutes: responseInteger(
      value.duration_minutes, 30, "duration_minutes", domain, 720,
    ),
    freeze_starts_at: freezeStart,
    freeze_ends_at: freezeEnd,
    status: responseChoice(value.status, ["active", "disabled"], "status", domain),
    policy_generation: responseInteger(value.policy_generation, 1, "policy_generation", domain),
    revision: responseInteger(value.revision, 1, "revision", domain),
    reason_code: responsePattern(value.reason_code, REASON, "reason_code", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  });
}

function rollbackAttempt(value, domain) {
  exact(value, [
    "rollback_attempt_id", "deployment_id", "source_release_id", "target_release_id",
    "desired_generation", "status", "reason_code", "revision", "requested_at",
    "completed_at", "updated_at",
  ], "rollback", domain);
  return Object.freeze({
    rollback_attempt_id: responseId(
      value.rollback_attempt_id, IDS.rollback, "rollback_attempt_id", domain,
    ),
    deployment_id: responseId(value.deployment_id, IDS.deployment, "deployment_id", domain),
    source_release_id: responseId(
      value.source_release_id, IDS.release, "source_release_id", domain,
    ),
    target_release_id: responseId(
      value.target_release_id, IDS.release, "target_release_id", domain,
    ),
    desired_generation: responseInteger(value.desired_generation, 1, "desired_generation", domain),
    status: responseChoice(value.status, ["requested", "issued", "completed", "failed"],
      "status", domain),
    reason_code: responsePattern(value.reason_code, REASON, "reason_code", domain),
    revision: responseInteger(value.revision, 1, "revision", domain),
    requested_at: timestamp(value.requested_at, "requested_at", domain),
    completed_at: nullableTimestamp(value.completed_at, "completed_at", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  });
}

function compatibility(value) {
  exact(value, [
    "deployment_id", "current_version", "lkg_version", "release_id", "release_version",
    "compatible", "reason_codes", "maintenance",
  ], "compatibility", "platform");
  array(value.reason_codes, 16, "reason_codes", "platform");
  return Object.freeze({
    deployment_id: responseId(value.deployment_id, IDS.deployment, "deployment_id", "platform"),
    current_version: responseText(value.current_version, 64, "current_version", "platform"),
    lkg_version: responseText(value.lkg_version, 64, "lkg_version", "platform"),
    release_id: nullableId(value.release_id, IDS.release, "release_id", "platform"),
    release_version: nullablePattern(value.release_version, SEMVER, "release_version", "platform"),
    compatible: responseBoolean(value.compatible, "compatible", "platform"),
    reason_codes: Object.freeze(value.reason_codes.map((item) => (
      responsePattern(item, REASON, "reason_code", "platform")
    ))),
    maintenance: value.maintenance === null ? null : maintenance(value.maintenance, "platform"),
  });
}

async function request(controlApiUrl, path, options, domain) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = domain === "workspace"
    ? readWorkspaceCsrfCookie() : readCookie(PLATFORM_CSRF_COOKIE);
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
    throw apiError(domain, 0, "edge_upgrade.network_unavailable", "Service unavailable");
  }
  const payload = await readJson(response, domain);
  if (!response.ok) {
    const code = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(payload?.code ?? ""))
      ? payload.code : "edge_upgrade.request_failed";
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

function trace(value, domain) {
  return {
    request_id: responseText(value.request_id, 160, "request_id", domain),
    trace_id: responseText(value.trace_id, 160, "trace_id", domain),
  };
}

function validateEtag(response, revision, domain) {
  if (response.headers.get("ETag") !== `"${revision}"`) invalidResponse(domain, "etag");
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

function array(value, maximum, label, domain) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(domain, label);
}

function unique(values, domain, label) {
  if (values.length !== new Set(values).size) invalidResponse(domain, label);
}

function clientId(value, pattern, label, domain) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalidRequest(domain, label);
  return text;
}

function clientIds(values, pattern, maximum, label, domain) {
  if (!Array.isArray(values) || values.length < 1 || values.length > maximum) {
    invalidRequest(domain, label);
  }
  const result = values.map((item) => clientId(item, pattern, label, domain));
  if (result.length !== new Set(result).size) invalidRequest(domain, label);
  return result;
}

function reason(value, domain) {
  const text = String(value ?? "");
  if (!REASON.test(text)) invalidRequest(domain, "reason_code");
  return text;
}

function idempotency(value, domain) {
  const text = String(value ?? "");
  if (!IDEMPOTENCY.test(text)) invalidRequest(domain, "idempotency_key");
  return text;
}

function stepUp(value, domain) {
  const text = String(value ?? "");
  if (!STEP_UP.test(text)) invalidRequest(domain, "step_up_grant");
  return text;
}

function choice(value, values, label, domain) {
  if (!values.includes(value)) invalidRequest(domain, label);
  return value;
}

function integer(value, minimum, maximum, label, domain) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidRequest(domain, label);
  }
  return value;
}

function clientBoolean(value, label, domain) {
  if (typeof value !== "boolean") invalidRequest(domain, label);
  return value;
}

function clientVersions(values, domain) {
  if (!Array.isArray(values) || values.length < 1 || values.length > 8) {
    invalidRequest(domain, "compatible_from_versions");
  }
  const result = values.map((item) => {
    const text = String(item ?? "").trim();
    if (!SEMVER.test(text)) invalidRequest(domain, "compatible_from_versions");
    return text;
  });
  if (result.length !== new Set(result).size) invalidRequest(domain, "compatible_from_versions");
  return result;
}

function compareSemver(left, right) {
  const a = left.split(".").map(Number);
  const b = right.split(".").map(Number);
  for (let index = 0; index < 3; index += 1) {
    if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1;
  }
  return 0;
}

function clientWeekdays(values, domain) {
  if (!Array.isArray(values) || values.length < 1 || values.length > 7
    || values.some((item) => !Number.isInteger(item) || item < 0 || item > 6)
    || values.length !== new Set(values).size) invalidRequest(domain, "weekdays");
  return [...values].sort((left, right) => left - right);
}

function timezone(value) {
  const text = String(value ?? "");
  if (!/^[A-Za-z0-9_+./-]{3,64}$/u.test(text)) invalidRequest("workspace", "timezone_name");
  return text;
}

function optionalClientTimestamp(value, label, domain) {
  if (value === null || value === "") return null;
  const text = String(value);
  if (text.length > 40 || !Number.isFinite(Date.parse(text))) invalidRequest(domain, label);
  return text;
}

function responseId(value, pattern, label, domain) {
  return responsePattern(value, pattern, label, domain);
}

function nullableId(value, pattern, label, domain) {
  return value === null ? null : responseId(value, pattern, label, domain);
}

function responsePattern(value, pattern, label, domain) {
  const text = responseText(value, 2048, label, domain);
  if (!pattern.test(text)) invalidResponse(domain, label);
  return text;
}

function nullablePattern(value, pattern, label, domain) {
  return value === null ? null : responsePattern(value, pattern, label, domain);
}

function responseChoice(value, values, label, domain) {
  if (!values.includes(value)) invalidResponse(domain, label);
  return value;
}

function responseInteger(value, minimum, label, domain, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function responseBoolean(value, label, domain) {
  if (typeof value !== "boolean") invalidResponse(domain, label);
  return value;
}

function responseText(value, maximum, label, domain) {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function nullableText(value, maximum, label, domain) {
  return value === null ? null : responseText(value, maximum, label, domain);
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
  return value === null ? null : responsePattern(value, REASON, label, domain);
}

function responseVersions(values, domain) {
  array(values, 8, "compatible_from_versions", domain);
  if (values.length < 1) invalidResponse(domain, "compatible_from_versions");
  const result = values.map((item) => (
    responsePattern(item, SEMVER, "compatible_from_version", domain)
  ));
  unique(result, domain, "compatible_from_versions");
  return Object.freeze(result);
}

function responseWeekdays(values, domain) {
  array(values, 7, "weekdays", domain);
  if (values.length < 1 || values.some((item) => !Number.isInteger(item) || item < 0 || item > 6)) {
    invalidResponse(domain, "weekdays");
  }
  unique(values, domain, "weekdays");
  return Object.freeze([...values]);
}

function safeText(value) {
  return typeof value === "string" && value.length <= 240 ? value : "Request failed";
}

function apiError(domain, status, code, message, requestId = "-") {
  if (domain === "platform") return new PlatformApiError({ status, code, message, requestId });
  return new AuthApiError({ status, code, message, requestId });
}

function invalidRequest(domain, label) {
  throw apiError(domain, 422, "edge_upgrade.request_invalid", `Invalid ${label}`);
}

function invalidResponse(domain, label) {
  throw apiError(domain, 502, "edge_upgrade.response_invalid", `Invalid ${label}`);
}
