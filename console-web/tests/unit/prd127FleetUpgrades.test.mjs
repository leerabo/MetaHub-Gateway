import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import {
  createPlatformRollout,
  getFleetCompatibility,
  getWorkspaceUpgrade,
  listPlatformGatewayReleases,
  listPlatformRollouts,
  mutatePlatformGatewayRelease,
  mutatePlatformRollout,
  requestWorkspaceRollback,
  updateWorkspaceMaintenance,
} from "../../src/lib/edgeUpgrades.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const DEPLOYMENT_ID = `edp_${"1".repeat(32)}`;
const OTHER_DEPLOYMENT_ID = `edp_${"2".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"3".repeat(32)}`;
const CURRENT_RELEASE_ID = `ger_${"4".repeat(32)}`;
const LKG_RELEASE_ID = `ger_${"5".repeat(32)}`;
const INSTALLER_RELEASE_ID = `irl_${"6".repeat(32)}`;
const RELEASE_ARTIFACT_ID = `gar_${"7".repeat(32)}`;
const INSTALLER_ARTIFACT_ID = `ira_${"8".repeat(32)}`;
const ROLLOUT_ID = `rol_${"9".repeat(32)}`;
const BATCH_ID = `rbt_${"a".repeat(32)}`;
const TARGET_ID = `rtt_${"b".repeat(32)}`;
const MAINTENANCE_ID = `mwn_${"c".repeat(32)}`;
const ROLLBACK_ID = `rba_${"d".repeat(32)}`;
const WORKSPACE_CSRF = "W".repeat(43);
const PLATFORM_CSRF = "P".repeat(43);
const STEP_UP = "S".repeat(43);
const IDEMPOTENCY = "edge-upgrade-test-00000001";
const NOW = "2026-08-28T02:00:00.000Z";

test("upgrade clients isolate Workspace and Platform credentials and send exact CAS writes", async (t) => {
  const calls = installFetch(t, (url, options) => routeResponse(new URL(url).pathname, options));
  installCookies(t);

  await getWorkspaceUpgrade({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID });
  await updateWorkspaceMaintenance({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    releaseChannel: "stable",
    timezoneName: "Asia/Shanghai",
    weekdays: [1, 2, 3, 4, 5],
    startMinute: 120,
    durationMinutes: 120,
    expectedRevision: 1,
    reasonCode: "maintenance_policy_updated",
    idempotencyKey: IDEMPOTENCY,
  });
  await requestWorkspaceRollback({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    targetReleaseId: LKG_RELEASE_ID,
    expectedHeadRevision: 2,
    reasonCode: "operator_requested",
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await listPlatformGatewayReleases({ controlApiUrl: CONTROL_URL });
  await mutatePlatformGatewayRelease({
    controlApiUrl: CONTROL_URL,
    action: "approve",
    releaseId: CURRENT_RELEASE_ID,
    expectedRevision: 1,
    reasonCode: "release_approved",
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await listPlatformRollouts({ controlApiUrl: CONTROL_URL });
  await createPlatformRollout({
    controlApiUrl: CONTROL_URL,
    releaseId: CURRENT_RELEASE_ID,
    deploymentIds: [DEPLOYMENT_ID],
    canaryBasisPoints: 100,
    observationSeconds: 1800,
    batchSize: 50,
    failureThresholdBasisPoints: 1000,
    reasonCode: "scheduled_upgrade",
    idempotencyKey: IDEMPOTENCY,
  });
  await mutatePlatformRollout({
    controlApiUrl: CONTROL_URL,
    rolloutId: ROLLOUT_ID,
    action: "pause",
    expectedRevision: 1,
    reasonCode: "operator_pause",
    idempotencyKey: IDEMPOTENCY,
  });
  await getFleetCompatibility({
    controlApiUrl: CONTROL_URL,
    releaseId: CURRENT_RELEASE_ID,
    deploymentId: DEPLOYMENT_ID,
  });

  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[2].options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.equal(calls[3].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[4].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[4].options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.equal(calls[6].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[7].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.redirect, "error");
    assert.equal("Authorization" in call.options.headers, false);
  }
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    release_channel: "stable",
    timezone_name: "Asia/Shanghai",
    weekdays: [1, 2, 3, 4, 5],
    start_minute: 120,
    duration_minutes: 120,
    freeze_starts_at: null,
    freeze_ends_at: null,
    status: "active",
    expected_revision: 1,
    reason_code: "maintenance_policy_updated",
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    target_release_id: LKG_RELEASE_ID,
    expected_head_revision: 2,
    reason_code: "operator_requested",
  });
  assert.equal(new URL(calls[8].url).search,
    `?release_id=${CURRENT_RELEASE_ID}&deployment_id=${DEPLOYMENT_ID}`);
});

test("strict clients reject cross-owner DTOs, forged manifests, duplicate targets, and ETag drift", async (t) => {
  installCookies(t);
  installFetch(t, () => jsonResponse(upgradePayload({
    maintenance: maintenance({ deployment_id: OTHER_DEPLOYMENT_ID }),
  }), { etag: '"2"' }));
  await assert.rejects(
    () => getWorkspaceUpgrade({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error instanceof AuthApiError && error.code === "edge_upgrade.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(releaseList({
    releases: [releaseSummary({
      release: releaseEnvelope({
        manifest: releaseManifest({ artifact_set_sha256: "not-a-hash" }),
      }),
    })],
  }));
  await assert.rejects(
    () => listPlatformGatewayReleases({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError && error.code === "edge_upgrade.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(rolloutList({
    rollouts: [rollout({ targets: [rolloutTarget(), rolloutTarget()] })],
  }));
  await assert.rejects(
    () => listPlatformRollouts({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "edge_upgrade.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(upgradePayload(), { etag: '"999"' });
  await assert.rejects(
    () => getWorkspaceUpgrade({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error.code === "edge_upgrade.response_invalid",
  );
});

test("Workspace upgrade state prunes permissions and aborts rollback when the owner drifts", () => {
  const auth = workspaceContext("upgrade-owner-a", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_UPGRADE_READ,
    PERMISSIONS.EDGE_UPGRADE_MANAGE,
    PERMISSIONS.EDGE_ROLLBACK_MANAGE,
  ]);
  const owner = privateGatewayPageOwner(auth, workspaceShell(auth));
  assert.equal(owner.canReadUpgrades, true);
  assert.equal(owner.canManageUpgrades, true);
  assert.equal(owner.canRollbackUpgrades, true);
  const state = createPrivateGatewayPageState();
  state.enter(owner);
  acceptUpgradeLoad(state, owner, upgradePayload());

  const maintenanceOperation = state.beginMaintenance(owner, DEPLOYMENT_ID, {
    releaseChannel: "stable",
    timezoneName: "Asia/Shanghai",
    weekdays: [1, 2, 3, 4, 5],
    startMinute: 120,
    durationMinutes: 120,
    status: "active",
    reasonCode: "maintenance_policy_updated",
  }, IDEMPOTENCY);
  assert.ok(maintenanceOperation);
  assert.equal(state.rejectMaintenance(maintenanceOperation, { status: 0 }, owner), true);
  state.finishMaintenance(maintenanceOperation);
  const maintenanceRetry = state.beginMaintenance(owner, DEPLOYMENT_ID, {
    releaseChannel: "stable",
    timezoneName: "Asia/Shanghai",
    weekdays: [1, 2, 3, 4, 5],
    startMinute: 120,
    durationMinutes: 120,
    status: "active",
    reasonCode: "maintenance_policy_updated",
  }, "edge-upgrade-test-00000002");
  assert.equal(maintenanceRetry.idempotencyKey, IDEMPOTENCY);
  assert.equal(state.acceptMaintenance(
    maintenanceRetry,
    upgradePayload({ maintenance: maintenance({ revision: 2, policy_generation: 2 }) }),
    owner,
  ), true);
  state.finishMaintenance(maintenanceRetry);

  const rollbackOperation = state.beginUpgradeRollback(
    owner, DEPLOYMENT_ID, LKG_RELEASE_ID, "operator_requested", IDEMPOTENCY,
  );
  assert.ok(rollbackOperation);
  assert.equal(rollbackOperation.idempotencyKey, IDEMPOTENCY);
  assert.equal(Object.hasOwn(rollbackOperation, "verificationCode"), false);
  const changed = workspaceContext("upgrade-owner-b", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_UPGRADE_READ,
    PERMISSIONS.EDGE_UPGRADE_MANAGE,
    PERMISSIONS.EDGE_ROLLBACK_MANAGE,
  ]);
  const changedOwner = privateGatewayPageOwner(changed, workspaceShell(changed));
  state.enter(changedOwner);
  assert.equal(rollbackOperation.controller.signal.aborted, true);
  assert.equal(state.isUpgradeRollbackCurrent(rollbackOperation, changedOwner), false);
  assert.equal(state.snapshot().upgradeStates[DEPLOYMENT_ID], undefined);

  const reader = workspaceContext("upgrade-reader", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_UPGRADE_READ,
  ]);
  const readerOwner = privateGatewayPageOwner(reader, workspaceShell(reader));
  assert.equal(readerOwner.canReadUpgrades, true);
  assert.equal(readerOwner.canManageUpgrades, false);
  assert.equal(readerOwner.canRollbackUpgrades, false);
});

test("Workspace and Platform render permission-pruned responsive controls without step-up leakage", async (t) => {
  const auth = workspaceContext("upgrade-render", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_UPGRADE_READ,
    PERMISSIONS.EDGE_UPGRADE_MANAGE,
    PERMISSIONS.EDGE_ROLLBACK_MANAGE,
  ]);
  const owner = privateGatewayPageOwner(auth, workspaceShell(auth));
  const state = createPrivateGatewayPageState();
  state.enter(owner);
  acceptUpgradeLoad(state, owner, upgradePayload());
  const workspaceHtml = renderPrivateGateways(state.snapshot(), auth, workspaceShell(auth));
  assert.match(workspaceHtml, /data-private-gateway-maintenance/u);
  assert.match(workspaceHtml, /data-private-gateway-upgrade-rollback/u);
  assert.match(workspaceHtml, /当前[\s\S]*可回退版本[\s\S]*回滚历史/u);
  assert.doesNotMatch(workspaceHtml, new RegExp(STEP_UP, "u"));

  installWindow(t, "#edge-upgrades");
  const calls = [];
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api: platformApi({
      createIdentityStepUpGrant: async (input) => {
        calls.push({ lane: "step-up", ...input });
        return { grant: STEP_UP };
      },
      mutateGatewayRelease: async (input) => {
        calls.push({ lane: "release", ...input });
        return releaseResponse({ release: releaseSummary({ status: "released", revision: 2 }) });
      },
    }),
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  assert.equal(await controller.mount(), true);
  assert.match(root.innerHTML, /网关升级/u);
  assert.match(root.innerHTML, /data-platform-edge-release-decision-form/u);
  assert.match(root.innerHTML, /data-platform-edge-rollout-create-form/u);
  const secretCode = "482913";
  assert.equal(await controller.decideGatewayRelease({
    action: "approve",
    releaseId: CURRENT_RELEASE_ID,
    expectedRevision: 1,
    reasonCode: "release_approved",
    method: "totp",
    verificationCode: secretCode,
    confirmed: true,
  }), true);
  assert.equal(calls[0].workspaceId, undefined);
  assert.equal(calls[0].actionCode, "fleet.release.approve");
  assert.equal(calls[0].resourceType, "edge_gateway_release");
  assert.equal(calls[1].stepUpGrant, STEP_UP);
  assert.doesNotMatch(root.innerHTML, new RegExp(`${secretCode}|${STEP_UP}`, "u"));
  controller.destroy();

  const [platformCss, edgeCss, clientSource, stateSource] = await Promise.all([
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/edge-operations.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/edgeUpgrades.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/private-gateways/privateGatewayPageState.js", import.meta.url), "utf8"),
  ]);
  assert.match(platformCss, /@media \(max-width: 760px\)[\s\S]*\.platform-edge-release-table/u);
  assert.match(edgeCss, /@media \(max-width: 760px\)[\s\S]*\.private-gateway-maintenance/u);
  assert.doesNotMatch(`${clientSource}\n${stateSource}`, /localStorage|sessionStorage|indexedDB/iu);
  assert.doesNotMatch(stateSource, /verificationCode|stepUpGrant/iu);
});

function routeResponse(path, options) {
  const method = options.method ?? "GET";
  if (path === `/edge/deployments/${DEPLOYMENT_ID}/upgrade`) {
    return jsonResponse(upgradePayload(), { etag: '"2"' });
  }
  if (path.endsWith("/maintenance-window")) {
    return jsonResponse(upgradePayload({
      maintenance: maintenance({ revision: 2, policy_generation: 2 }),
    }), { etag: '"2"' });
  }
  if (path.endsWith("/rollback")) return jsonResponse(rollbackResponse(), { status: 201, etag: '"1"' });
  if (path === "/platform/releases" && method === "GET") return jsonResponse(releaseList());
  if (path === "/platform/releases" && method === "POST") {
    return jsonResponse(releaseResponse({
      release: releaseSummary({ status: "released", revision: 2 }),
    }), { etag: '"2"' });
  }
  if (path === "/platform/rollouts") return jsonResponse(rolloutList());
  if (path.includes("/rollouts") || path.includes("/fleet/rollouts/")) {
    return jsonResponse(rolloutResponse(), { status: path.includes("/platform/") ? 201 : 200, etag: '"1"' });
  }
  if (path === "/fleet/compatibility") return jsonResponse(compatibilityResponse());
  throw new Error(`unexpected route ${method} ${path}`);
}

function upgradePayload(overrides = {}) {
  return {
    deployment_id: DEPLOYMENT_ID,
    maintenance: maintenance(),
    head: releaseHead(),
    rollouts: [rolloutTarget()],
    rollback_attempts: [rollbackAttempt()],
    request_id: "req_upgrade",
    trace_id: "trc_upgrade",
    ...overrides,
  };
}

function maintenance(overrides = {}) {
  return {
    maintenance_window_id: MAINTENANCE_ID,
    deployment_id: DEPLOYMENT_ID,
    release_channel: "stable",
    timezone_name: "Asia/Shanghai",
    weekdays: [1, 2, 3, 4, 5],
    start_minute: 120,
    duration_minutes: 120,
    freeze_starts_at: null,
    freeze_ends_at: null,
    status: "active",
    policy_generation: 1,
    revision: 1,
    reason_code: "maintenance_policy_updated",
    updated_at: NOW,
    ...overrides,
  };
}

function releaseHead(overrides = {}) {
  return {
    deployment_id: DEPLOYMENT_ID,
    current_release_id: CURRENT_RELEASE_ID,
    lkg_release_id: LKG_RELEASE_ID,
    current_version: "1.2.0",
    lkg_version: "1.1.0",
    desired_generation: 4,
    applied_generation: 4,
    lkg_generation: 3,
    revision: 2,
    updated_at: NOW,
    ...overrides,
  };
}

function rolloutTarget(overrides = {}) {
  return {
    rollout_target_id: TARGET_ID,
    deployment_id: DEPLOYMENT_ID,
    batch_sequence: 0,
    stable_bucket: 12,
    current_version: "1.2.0",
    target_version: "1.3.0",
    status: "selected",
    failure_reason_code: null,
    updated_at: NOW,
    ...overrides,
  };
}

function rollbackAttempt(overrides = {}) {
  return {
    rollback_attempt_id: ROLLBACK_ID,
    deployment_id: DEPLOYMENT_ID,
    source_release_id: CURRENT_RELEASE_ID,
    target_release_id: LKG_RELEASE_ID,
    desired_generation: 5,
    status: "requested",
    reason_code: "operator_requested",
    revision: 1,
    requested_at: NOW,
    completed_at: null,
    updated_at: NOW,
    ...overrides,
  };
}

function releaseManifest(overrides = {}) {
  return {
    schema_version: "metahub.edge-upgrade-release/v1",
    release_id: CURRENT_RELEASE_ID,
    installer_release_id: INSTALLER_RELEASE_ID,
    version: "1.3.0",
    release_channel: "stable",
    signature_purpose: "metahub.edge-upgrade.release/v1",
    artifact_set_sha256: "a".repeat(64),
    sbom_sha256: "b".repeat(64),
    schema_from: 1,
    schema_to: 2,
    down_migration_supported: true,
    backup_required: true,
    compatible_from_versions: ["1.1.0", "1.2.0"],
    agent_min_version: "1.0.0",
    agent_max_version: "2.0.0",
    control_min_version: "1.0.0",
    control_max_version: "2.0.0",
    artifacts: [{
      artifact_id: RELEASE_ARTIFACT_ID,
      installer_artifact_id: INSTALLER_ARTIFACT_ID,
      kind: "container_bundle",
      filename: "gateway-bundle.tar",
      media_type: "application/octet-stream",
      sha256: "c".repeat(64),
      sbom_sha256: "d".repeat(64),
      size_bytes: 4096,
    }],
    ...overrides,
  };
}

function releaseEnvelope(overrides = {}) {
  return {
    manifest: releaseManifest(),
    manifest_sha256: "e".repeat(64),
    signature: {
      algorithm: "Ed25519",
      key_id: "edge-release-test-v1",
      public_key: "A".repeat(43),
      value: "B".repeat(86),
    },
    ...overrides,
  };
}

function releaseSummary(overrides = {}) {
  return {
    release: releaseEnvelope(),
    status: "approval_pending",
    revision: 1,
    submitted_by_operator_id: "operator-submit",
    approved_by_operator_id: null,
    submitted_at: NOW,
    approved_at: null,
    withdrawn_at: null,
    ...overrides,
  };
}

function releaseList(overrides = {}) {
  return { releases: [releaseSummary()], request_id: "req_releases", trace_id: "trc_releases", ...overrides };
}

function releaseResponse(overrides = {}) {
  return { release: releaseSummary(), idempotent_replay: false, request_id: "req_release", trace_id: "trc_release", ...overrides };
}

function rolloutBatch() {
  return {
    rollout_batch_id: BATCH_ID,
    sequence: 0,
    batch_kind: "canary",
    status: "running",
    target_count: 1,
    succeeded_count: 0,
    failed_count: 0,
    observe_until: null,
    updated_at: NOW,
  };
}

function rollout(overrides = {}) {
  return {
    rollout_id: ROLLOUT_ID,
    release_id: CURRENT_RELEASE_ID,
    release_version: "1.3.0",
    release_channel: "stable",
    status: "running",
    generation: 1,
    cancel_watermark: 0,
    canary_basis_points: 100,
    observation_seconds: 1800,
    target_count: 1,
    succeeded_count: 0,
    failed_count: 0,
    current_batch_sequence: 0,
    pause_reason_code: null,
    revision: 1,
    batches: [rolloutBatch()],
    targets: [rolloutTarget()],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function rolloutList(overrides = {}) {
  return { rollouts: [rollout()], request_id: "req_rollouts", trace_id: "trc_rollouts", ...overrides };
}

function rolloutResponse(overrides = {}) {
  return { rollout: rollout(), idempotent_replay: false, request_id: "req_rollout", trace_id: "trc_rollout", ...overrides };
}

function rollbackResponse() {
  return { rollback: rollbackAttempt(), idempotent_replay: false, request_id: "req_rollback", trace_id: "trc_rollback" };
}

function compatibilityResponse() {
  return {
    items: [{
      deployment_id: DEPLOYMENT_ID,
      current_version: "1.2.0",
      lkg_version: "1.1.0",
      release_id: CURRENT_RELEASE_ID,
      release_version: "1.3.0",
      compatible: true,
      reason_codes: ["compatible"],
      maintenance: maintenance(),
    }],
    request_id: "req_compatibility",
    trace_id: "trc_compatibility",
  };
}

function deployment() {
  return {
    deployment_id: DEPLOYMENT_ID,
    workspace_id: WORKSPACE_ID,
    display_name: "Shanghai private gateway",
    region_code: "cn-east-1",
    mode: "connected",
    status: "active",
    revision: 2,
    revocation_generation: 0,
    agent_version: "1.2.0",
    health_code: "healthy",
    last_connected_at: NOW,
    certificate_expires_at: null,
    identity_generation: 1,
    current_certificate: null,
    recent_events: [],
    created_at: NOW,
    updated_at: NOW,
    revoked_at: null,
  };
}

function acceptUpgradeLoad(state, owner, response) {
  const operation = state.beginLoad(owner, 1);
  assert.equal(state.acceptLoad(
    operation,
    { deployments: [deployment()] },
    owner,
    1,
    [],
    [],
    null,
    null,
    [],
    [{ deploymentId: DEPLOYMENT_ID, response }],
  ), true);
  state.finishLoad(operation);
}

function workspaceContext(fingerprint, permissionCodes) {
  return withPermissionSnapshot({
    tenant: { tenant_id: "tenant-upgrade", name: "Upgrade tenant" },
    user: {
      tenant_id: "tenant-upgrade",
      user_id: "usr-upgrade",
      account_id: "acc-upgrade",
      role: "admin",
      email: "upgrade@example.test",
    },
    session: { session_id: "ses-upgrade", session_revision: 1 },
  }, permissionCodes, { permissionRevision: 127, authorizationFingerprint: fingerprint });
}

function workspaceShell(auth) {
  return {
    state: "ok",
    activeNavigationItemId: "private-gateways",
    navigation: {
      active_navigation_item_id: "private-gateways",
      permission_codes: auth.permission_codes,
      permission_revision: auth.permission_revision,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: [{
        id: "private-gateways",
        target_section: "private-gateways",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.EDGE_DEPLOYMENT_READ],
      }],
    },
  };
}

function platformSession() {
  return {
    context: {
      operator: { operator_id: "operator-approve", display_name: "Release approver", roles: ["release_manager"] },
      permissions: [
        "platform.edge_release.read",
        "platform.edge_release.manage",
        "platform.edge_release.approve",
        "platform.edge_rollout.read",
        "platform.edge_rollout.manage",
      ],
    },
  };
}

function platformApi(overrides = {}) {
  return {
    session: async () => platformSession(),
    navigation: async () => ({ items: [{ id: "edge-upgrades", label: "Fleet upgrades", href: "/platform.html#edge-upgrades" }] }),
    gatewayReleases: async () => releaseList(),
    fleetRollouts: async () => rolloutList(),
    ...overrides,
  };
}

function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}

function installWindow(t, hash) {
  const originalWindow = globalThis.window;
  globalThis.window = {
    location: { hash, href: `http://console.local/platform.html${hash}` },
    history: { replaceState(_state, _title, nextHash) { globalThis.window.location.hash = nextHash; } },
  };
  t.after(() => { globalThis.window = originalWindow; });
}

function installCookies(t) {
  const originalDocument = globalThis.document;
  globalThis.document = {
    cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}; __Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
  };
  t.after(() => { globalThis.document = originalDocument; });
}

function installFetch(t, handler) {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return handler(url, options);
  };
  t.after(() => { globalThis.fetch = originalFetch; });
  return calls;
}

function jsonResponse(payload, { status = 200, etag = null } = {}) {
  const headers = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store, private",
    "X-Request-ID": payload.request_id,
    "X-Trace-ID": payload.trace_id,
  };
  if (etag) headers.ETag = etag;
  return new Response(JSON.stringify(payload), { status, headers });
}
