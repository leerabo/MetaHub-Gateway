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
  getWorkspaceBackupStatus,
  listPlatformBackupStatuses,
  mutateWorkspaceRestoreDrill,
} from "../../src/lib/edgeBackups.js";
import {
  clearLocalAdminSession,
  createLocalAdminSession,
  createLocalBackup,
  restoreLocalBackup,
} from "../../src/lib/localSecretManagement.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const LOCAL_URL = "https://127.0.0.1:9443";
const DEPLOYMENT_ID = `edp_${"1".repeat(32)}`;
const BACKUP_ID = `ebk_${"2".repeat(32)}`;
const DRILL_ID = `pdd_${"3".repeat(32)}`;
const RESTORE_JOB_ID = `rst_${"4".repeat(32)}`;
const LOCAL_RESTORE_ID = `lrj_${"5".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"6".repeat(32)}`;
const TENANT_ID = "12345678-1234-4123-8123-123456789abc";
const WORKSPACE_CSRF = "W".repeat(43);
const PLATFORM_CSRF = "P".repeat(43);
const LOCAL_CSRF = "L".repeat(43);
const IDEMPOTENCY = "edge-backup-test-0000001";
const STEP_UP = "step-up-grant-000000000001";
const NOW = Date.now();

test("strict backup clients keep Workspace and Platform credentials isolated", async (t) => {
  installCookies(t);
  const calls = installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/backup-status")) return jsonResponse(statusResponse(), { etag: '"2"' });
    if (path.endsWith("/restore-drills")) {
      const action = JSON.parse(options.body).action;
      return jsonResponse(drillResponse({
        drill: action === "approve" ? drill({ status: "approved", revision: 2,
          approved_by_account_id: "gac_approver" }) : drill(),
      }), { etag: action === "approve" ? '"2"' : '"1"', status: action === "approve" ? 200 : 201 });
    }
    if (path === "/platform/edge/backups") return jsonResponse(statusList());
    throw new Error(`unexpected route ${path}`);
  });

  await getWorkspaceBackupStatus({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID });
  await mutateWorkspaceRestoreDrill({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    action: "request",
    sourceBackupId: BACKUP_ID,
    quarterCode: "2026-Q3",
    rpoObjectiveSeconds: 86_400,
    rtoObjectiveSeconds: 14_400,
    isolatedTargetSha256: "a".repeat(64),
    reasonCode: "quarterly_dr_drill",
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await mutateWorkspaceRestoreDrill({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    action: "approve",
    drillId: DRILL_ID,
    expectedRevision: 1,
    reasonCode: "isolated_restore_approved",
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await listPlatformBackupStatuses({ controlApiUrl: CONTROL_URL });

  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[1].options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[3].options.headers["X-CSRF-Token"], undefined);
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.redirect, "error");
    assert.equal("Authorization" in call.options.headers, false);
  }
  const wire = JSON.stringify(calls);
  assert.doesNotMatch(wire, /object_url|storage_path|envelope|chunk_body|secret_value/iu);
});

test("backup clients fail closed on unknown fields, duplicate owners, and ETag drift", async (t) => {
  installCookies(t);
  installFetch(t, () => jsonResponse(statusResponse({ object_url: "forbidden" }), { etag: '"2"' }));
  await assert.rejects(
    () => getWorkspaceBackupStatus({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error instanceof AuthApiError && error.code === "edge_backup.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(statusList({ statuses: [backupStatus(), backupStatus()] }));
  await assert.rejects(
    () => listPlatformBackupStatuses({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError && error.code === "edge_backup.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(statusResponse(), { etag: '"99"' });
  await assert.rejects(
    () => getWorkspaceBackupStatus({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error.code === "edge_backup.response_invalid",
  );
});

test("local backup and restore clients require the in-memory local admin session", async (t) => {
  clearLocalAdminSession();
  t.after(clearLocalAdminSession);
  const calls = installFetch(t, (url) => {
    const path = new URL(url).pathname;
    if (path === "/local/admin/session-handoffs") {
      return localJsonResponse({ deployment_id: DEPLOYMENT_ID, expires_at: nowSeconds() + 300 },
        { status: 201, csrf: LOCAL_CSRF });
    }
    if (path === "/local/backups") return localJsonResponse(localBackup(), { status: 201 });
    if (path === "/local/restores") return localJsonResponse(localRestore());
    throw new Error(`unexpected local route ${path}`);
  });
  const credential = new TextEncoder().encode("local-handoff-credential-0000000000001");
  await createLocalAdminSession({
    localApiOrigin: LOCAL_URL,
    deploymentId: DEPLOYMENT_ID,
    accountId: "gac_security",
    tenantId: TENANT_ID,
    workspaceId: WORKSPACE_ID,
    handoffCredential: credential,
  });
  assert.ok(credential.every((byte) => byte === 0));
  await createLocalBackup({
    deploymentId: DEPLOYMENT_ID,
    sourceGeneration: 12,
    retentionSeconds: 2_592_000,
    storageKind: "customer_object_store",
  });
  await restoreLocalBackup({
    deploymentId: DEPLOYMENT_ID,
    backupId: BACKUP_ID,
    targetSchemaHead: "0121",
  });
  await assert.rejects(
    () => restoreLocalBackup({
      deploymentId: DEPLOYMENT_ID,
      backupId: BACKUP_ID,
      targetSchemaHead: "0121",
      drillContext: {
        drill_id: DRILL_ID,
        restore_job_id: RESTORE_JOB_ID,
        expected_revision: 2,
        source_backup_completed_at: iso(-0.1),
        device_reenrollment_evidence_sha256: "a".repeat(64),
        minimal_request_evidence_sha256: "b".repeat(64),
        unexpected_field: "blocked",
      },
    }),
    (error) => error.status === 422 && error.code === "local_restore.request_invalid",
  );
  assert.equal(calls[1].options.headers["X-CSRF-Token"], LOCAL_CSRF);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], LOCAL_CSRF);
  assert.equal(calls.length, 3);
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    source_generation: 12,
    retention_seconds: 2_592_000,
    storage_kind: "customer_object_store",
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    backup_id: BACKUP_ID,
    target_schema_head: "0121",
  });
  assert.doesNotMatch(JSON.stringify(calls), /manifest_sha256|merkle_root|kms_key_id/iu);
});

test("Workspace and Platform backup views prune facts on permission drift", async (t) => {
  const auth = workspaceContext("backup-owner-a", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_BACKUP_READ,
    PERMISSIONS.EDGE_BACKUP_CREATE,
    PERMISSIONS.EDGE_DRILL_MANAGE,
    PERMISSIONS.EDGE_RESTORE_APPROVE,
  ]);
  const shell = workspaceShell(auth);
  const owner = privateGatewayPageOwner(auth, shell);
  assert.equal(owner.canReadBackups, true);
  assert.equal(owner.canManageDrills, true);

  const state = createPrivateGatewayPageState();
  state.enter(owner);
  const load = state.beginLoad(owner, 1);
  assert.ok(load);
  assert.equal(state.acceptLoad(
    load, { deployments: [deployment()] }, owner, 1,
    [], [], null, null, [], [], [],
    [{ deploymentId: DEPLOYMENT_ID, response: statusResponse() }],
  ), true);
  state.finishLoad(load);
  const html = renderPrivateGateways(state.snapshot(), auth, shell);
  assert.match(html, /备份与恢复/u);
  assert.match(html, /RPO 正常/u);
  assert.match(html, /隔离恢复演练/u);
  assert.doesNotMatch(html, /object_url|storage_path|envelope|kms_key_id_sha256/iu);

  const operation = state.beginDrillMutation(owner, {
    action: "request",
    deploymentId: DEPLOYMENT_ID,
    sourceBackupId: BACKUP_ID,
    quarterCode: "2026-Q3",
    rpoObjectiveSeconds: 86_400,
    rtoObjectiveSeconds: 14_400,
    isolatedTargetSha256: "a".repeat(64),
    reasonCode: "quarterly_dr_drill",
  }, IDEMPOTENCY);
  assert.ok(operation);
  assert.equal(state.acceptDrillMutation(operation, drillResponse(), owner), true);
  state.finishDrillMutation(operation);

  const changed = workspaceContext("backup-owner-b", [PERMISSIONS.EDGE_DEPLOYMENT_READ]);
  state.enter(privateGatewayPageOwner(changed, workspaceShell(changed)));
  assert.deepEqual(state.snapshot().backupStates, {});

  installWindow(t, "#edge-backups");
  const root = fakeRoot();
  const controller = createPlatformPageController({ root, api: platformApi() });
  assert.equal(await controller.mount(), true);
  assert.match(root.innerHTML, /网关备份/u);
  assert.match(root.innerHTML, /RPO 正常/u);
  assert.match(root.innerHTML, /生产未触碰/u);
  assert.doesNotMatch(root.innerHTML, /manifest_sha256|kms_key_id_sha256|isolated_target_sha256/iu);
  controller.destroy();

  const [platformCss, edgeCss, clientSource, stateSource] = await Promise.all([
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/edge-operations.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/edgeBackups.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/private-gateways/privateGatewayPageState.js", import.meta.url), "utf8"),
  ]);
  assert.match(platformCss, /@media \(max-width: 760px\)[\s\S]*\.platform-edge-backup-table/u);
  assert.match(edgeCss, /@media \(max-width: 760px\)[\s\S]*\.private-gateway-backup-facts/u);
  assert.doesNotMatch(`${clientSource}\n${stateSource}`, /localStorage|sessionStorage|indexedDB/iu);
});

function backupStatus(overrides = {}) {
  return {
    deployment_id: DEPLOYMENT_ID,
    status: "current",
    rpo_objective_seconds: 86_400,
    latest_source_generation: 12,
    last_success_at: iso(-0.1),
    last_check_outcome: "verified",
    revision: 2,
    updated_at: iso(0),
    latest_backup: backupReport(),
    recent_backups: [backupReport()],
    recent_drills: [drill({ status: "succeeded", revision: 3,
      approved_by_account_id: "gac_approver", started_at: iso(-0.05),
      completed_at: iso(0), actual_rpo_seconds: 120, actual_rto_seconds: 240,
      objective_outcome: "met" })],
    ...overrides,
  };
}

function backupReport(overrides = {}) {
  return {
    backup_id: BACKUP_ID,
    source_generation: 12,
    schema_head: "0121",
    storage_kind: "customer_object_store",
    status: "completed",
    size_bucket: "100mb_1gb",
    chunk_count: 8,
    manifest_sha256: "7".repeat(64),
    kms_key_id_sha256: "8".repeat(64),
    check_outcome: "verified",
    failure_code: null,
    started_at: iso(-0.2),
    completed_at: iso(-0.1),
    retention_until: iso(30),
    ...overrides,
  };
}

function drill(overrides = {}) {
  return {
    drill_id: DRILL_ID,
    restore_job_id: RESTORE_JOB_ID,
    source_backup_id: BACKUP_ID,
    status: "requested",
    quarter_code: "2026-Q3",
    rpo_objective_seconds: 86_400,
    rto_objective_seconds: 14_400,
    actual_rpo_seconds: null,
    actual_rto_seconds: null,
    isolated_target_sha256: "a".repeat(64),
    production_untouched: true,
    objective_outcome: null,
    revision: 1,
    requested_by_account_id: "gac_requester",
    approved_by_account_id: null,
    created_at: iso(-0.1),
    started_at: null,
    completed_at: null,
    updated_at: iso(0),
    ...overrides,
  };
}

function statusResponse(overrides = {}) {
  return { status: backupStatus(), request_id: "request-prd129", trace_id: "trace-prd129", ...overrides };
}

function statusList(overrides = {}) {
  return { statuses: [backupStatus()], request_id: "request-prd129", trace_id: "trace-prd129", ...overrides };
}

function drillResponse(overrides = {}) {
  return { deployment_id: DEPLOYMENT_ID, drill: drill(), idempotent_replay: false,
    request_id: "request-prd129", trace_id: "trace-prd129", ...overrides };
}

function localBackup() {
  return {
    backup_id: BACKUP_ID,
    deployment_id: DEPLOYMENT_ID,
    source_generation: 12,
    schema_head: "0121",
    storage_kind: "customer_object_store",
    status: "ready",
    manifest_sha256: "7".repeat(64),
    merkle_root: "9".repeat(64),
    kms_key_id_sha256: "8".repeat(64),
    chunk_count: 8,
    encrypted_size_bytes: 1024,
    created_at: iso(-0.1),
    expires_at: iso(30),
    revision: 1,
  };
}

function localRestore() {
  return {
    restore_id: LOCAL_RESTORE_ID,
    backup_id: BACKUP_ID,
    deployment_id: DEPLOYMENT_ID,
    target_schema_head: "0121",
    status: "succeeded",
    failure_code: null,
    revision: 4,
    created_at: iso(-0.05),
    completed_at: iso(0),
    updated_at: iso(0),
  };
}

function deployment() {
  return {
    agent_version: "1.0.0", certificate_expires_at: iso(30), created_at: iso(-30),
    current_certificate: null, deployment_id: DEPLOYMENT_ID, display_name: "Backup Gateway",
    health_code: "healthy", identity_generation: 1, last_connected_at: iso(0),
    mode: "connected", recent_events: [], region_code: "cn-test", revision: 1,
    revocation_generation: 0, revoked_at: null, status: "active", updated_at: iso(0),
    workspace_id: WORKSPACE_ID,
  };
}

function workspaceContext(fingerprint, permissionCodes) {
  return withPermissionSnapshot({
    user: { tenant_id: TENANT_ID, account_id: "gac_current", user_id: "usr_prd129",
      email: "owner@example.invalid", role: "security_admin" },
    tenant: { tenant_id: TENANT_ID },
    session: { session_id: "ses_prd129", session_revision: 1 },
  }, permissionCodes, { authorizationFingerprint: fingerprint });
}

function workspaceShell(auth) {
  return {
    state: "ok", activeNavigationItemId: "private-gateways",
    navigation: {
      active_navigation_item_id: "private-gateways",
      permission_codes: auth.permission_codes,
      permission_revision: auth.permission_revision,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: [{ id: "private-gateways", target_section: "private-gateways",
        required_permissions: [PERMISSIONS.EDGE_DEPLOYMENT_READ], visible: true, disabled: false }],
    },
  };
}

function platformApi() {
  return {
    session: async () => ({ context: { operator: { display_name: "Ops", roles: ["platform_ops"] },
      permissions: ["platform.edge_backup.read"] } }),
    navigation: async () => ({ items: [{ id: "edge-backups", label: "Gateway backups",
      href: "/platform.html#edge-backups" }] }),
    edgeBackups: async () => statusList(),
  };
}

function installCookies(t) {
  const original = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}; __Host-metahub-platform-csrf=${PLATFORM_CSRF}` };
  t.after(() => { globalThis.document = original; });
}

function installFetch(t, responder) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return responder(url, options);
  };
  t.after(() => { globalThis.fetch = original; });
  return calls;
}

function jsonResponse(payload, { status = 200, etag = null } = {}) {
  const headers = { "Content-Type": "application/json", "Cache-Control": "private, no-store",
    "X-Request-ID": payload.request_id, "X-Trace-ID": payload.trace_id };
  if (etag) headers.ETag = etag;
  return new Response(JSON.stringify(payload), { status, headers });
}

function localJsonResponse(payload, { status = 200, csrf = null } = {}) {
  const headers = { "Content-Type": "application/json", "Cache-Control": "private, no-store" };
  if (csrf) headers["X-CSRF-Token"] = csrf;
  return new Response(JSON.stringify(payload), { status, headers });
}

function fakeRoot() {
  return { innerHTML: "", addEventListener() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, replaceChildren() { this.innerHTML = ""; } };
}

function installWindow(t, hash) {
  const original = globalThis.window;
  globalThis.window = { location: { hash }, history: { replaceState(_a, _b, value) {
    globalThis.window.location.hash = value;
  } } };
  t.after(() => { globalThis.window = original; });
}

function iso(days) { return new Date(NOW + days * 86_400_000).toISOString(); }
function nowSeconds() { return Math.floor(Date.now() / 1000); }
