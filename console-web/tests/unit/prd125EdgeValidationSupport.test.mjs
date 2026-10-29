import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import {
  acceptEdgeDelivery,
  getEdgeSupportBundle,
  getEdgeValidation,
  startEdgeValidation,
} from "../../src/lib/edgeManagement.js";
import {
  approveLocalSupportBundle,
  cancelLocalSupportBundle,
  clearLocalAdminSession,
  createLocalAdminSession,
  previewLocalSupportBundle,
} from "../../src/lib/localSecretManagement.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const LOCAL_URL = "http://localhost:8000";
const TENANT_ID = "123e4567-e89b-12d3-a456-426614174000";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const DEPLOYMENT_ID = `edp_${"2".repeat(32)}`;
const FIRST_RUN_ID = `dvr_${"3".repeat(32)}`;
const RETRY_RUN_ID = `dvr_${"4".repeat(32)}`;
const RELEASE_ID = `irl_${"5".repeat(32)}`;
const ACCEPTANCE_ID = `eda_${"6".repeat(32)}`;
const BUNDLE_ID = `esb_${"7".repeat(32)}`;
const ACCOUNT_ID = "usr_edge_prd125";
const CHECK_CODES = [
  "device_certificate", "signed_config_lkg", "provider_connectivity",
  "minimal_gateway_request", "usage_cost_failure_sync", "backup_readiness",
  "capacity_readiness", "performance_baseline",
];
const COLLECTORS = [
  "runtime.version", "runtime.watermarks", "runtime.health", "runtime.errors",
  "runtime.logs",
];
const COLLECTOR_META = {
  "runtime.version": ["meta/version.json", "version", "application/json"],
  "runtime.watermarks": ["state/watermarks.json", "watermark", "application/json"],
  "runtime.health": ["health/summary.json", "health", "application/json"],
  "runtime.errors": ["errors/codes.json", "error_code", "application/json"],
  "runtime.logs": ["logs/events.ndjson", "log_summary", "application/x-ndjson"],
};

function context(role, permissionCodes) {
  return withPermissionSnapshot({
    tenant: { tenant_id: TENANT_ID, name: "PRD-125 tenant" },
    user: {
      tenant_id: TENANT_ID,
      user_id: ACCOUNT_ID,
      role,
      email: "edge-prd125@example.test",
    },
    session: { session_id: "ses_edge_prd125", session_revision: 1 },
  }, permissionCodes, {
    permissionRevision: 35,
    authorizationFingerprint: `edge_prd125_${role}`,
  });
}

function shell(auth) {
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

function deployment() {
  return {
    deployment_id: DEPLOYMENT_ID,
    workspace_id: WORKSPACE_ID,
    display_name: "PRD-125 private gateway",
    region_code: "cn-east-1",
    mode: "connected",
    status: "active",
    revision: 3,
    revocation_generation: 1,
    agent_version: "1.2.0",
    health_code: "healthy",
    last_connected_at: "2026-08-27T04:10:00Z",
    certificate_expires_at: "2026-09-27T04:00:00Z",
    identity_generation: 1,
    current_certificate: null,
    recent_events: [],
    created_at: "2026-08-27T04:00:00Z",
    updated_at: "2026-08-27T04:10:00Z",
    revoked_at: null,
  };
}

function check(code, runId, failed = false, index = CHECK_CODES.indexOf(code)) {
  return {
    validation_check_id: `dvc_${(index + 1).toString(16).repeat(32)}`,
    code,
    outcome: failed ? "failed" : "passed",
    reason_code: failed ? `${code}.failed` : "check.passed",
    recovery_action_code: failed ? `${code}.recover` : null,
    evidence_sha256: (index + 1).toString(16).repeat(64),
    duration_ms: 100 + index,
    source_validation_run_id: runId,
    created_at: "2026-08-27T04:12:00Z",
  };
}

function validationResponse({
  runId = FIRST_RUN_ID,
  sourceRunId = null,
  attempt = 1,
  status = "requested",
  failedCode = null,
  acceptance = null,
  requestId = "req_prd125_validation",
} = {}) {
  const requested = sourceRunId ? ["provider_connectivity"] : CHECK_CODES;
  const completed = status === "completed";
  const checks = completed
    ? requested.map((code) => check(code, runId, code === failedCode))
    : [];
  let effectiveChecks = [];
  if (completed) {
    effectiveChecks = CHECK_CODES.map((code) => (
      sourceRunId && code !== "provider_connectivity"
        ? check(code, sourceRunId)
        : check(code, runId, code === failedCode)
    ));
  }
  return {
    validation: {
      validation_run_id: runId,
      deployment_id: DEPLOYMENT_ID,
      source_validation_run_id: sourceRunId,
      attempt,
      plan_version: "edge-delivery-v1",
      deployment_revision: 3,
      installer_release_id: RELEASE_ID,
      desired_generation: 4,
      applied_generation: 4,
      lkg_generation: 4,
      input_contract_sha256: "a".repeat(64),
      requested_check_codes: requested,
      status,
      outcome: completed ? (failedCode ? "failed" : "passed") : null,
      duration_ms: completed ? 1_000 : null,
      checks,
      effective_checks: effectiveChecks,
      started_at: completed ? "2026-08-27T04:12:00Z" : null,
      completed_at: completed ? "2026-08-27T04:12:01Z" : null,
      created_at: "2026-08-27T04:11:59Z",
      updated_at: completed ? "2026-08-27T04:12:01Z" : "2026-08-27T04:11:59Z",
    },
    delivery_acceptance: acceptance,
    idempotent_replay: false,
    request_id: requestId,
    trace_id: "trc_prd125_validation",
  };
}

function acceptance() {
  return {
    delivery_acceptance_id: ACCEPTANCE_ID,
    validation_run_id: RETRY_RUN_ID,
    validation_set_sha256: "b".repeat(64),
    deployment_revision: 3,
    installer_release_id: RELEASE_ID,
    desired_generation: 4,
    applied_generation: 4,
    lkg_generation: 4,
    accepted_by_account_id: ACCOUNT_ID,
    accepted_at: "2026-08-27T04:13:00Z",
  };
}

function manifest() {
  const entries = COLLECTORS.map((collectorCode, index) => {
    const [path, classification, mediaType] = COLLECTOR_META[collectorCode];
    return {
      collector_code: collectorCode,
      path,
      classification,
      media_type: mediaType,
      size_bytes: 100 + index,
      sha256: (index + 8).toString(16).repeat(64),
    };
  });
  return {
    schema_version: "metahub.support-bundle/v1",
    bundle_id: BUNDLE_ID,
    deployment_id: DEPLOYMENT_ID,
    scan_version: "support-bundle-scan-v1",
    scan_outcome: "clean",
    entries,
    total_uncompressed_bytes: entries.reduce((total, item) => total + item.size_bytes, 0),
    archive_size_bytes: 400,
    archive_sha256: "c".repeat(64),
    created_at: "2026-08-27T04:14:00Z",
    expires_at: "2026-08-28T04:14:00Z",
  };
}

function localBundle(status = "preview_ready") {
  const uploaded = status === "uploaded";
  return {
    bundle_id: BUNDLE_ID,
    deployment_id: DEPLOYMENT_ID,
    status,
    revision: uploaded ? 3 : 1,
    manifest: manifest(),
    cloud_object_reference: uploaded ? BUNDLE_ID : null,
    failure_code: null,
    created_at: "2026-08-27T04:14:00Z",
    expires_at: "2026-08-28T04:14:00Z",
    approved_at: uploaded ? "2026-08-27T04:15:00Z" : null,
    uploaded_at: uploaded ? "2026-08-27T04:15:01Z" : null,
  };
}

function cloudBundle() {
  return {
    bundle: {
      support_bundle_id: BUNDLE_ID,
      deployment_id: DEPLOYMENT_ID,
      status: "ready",
      revision: 1,
      manifest_sha256: "d".repeat(64),
      entries: manifest().entries.map((item) => ({
        collector_code: item.collector_code,
        classification: item.classification,
        media_type: item.media_type,
        size_bytes: item.size_bytes,
      })),
      scan_version: "support-bundle-scan-v1",
      scan_outcome: "clean",
      archive_size_bytes: 400,
      object_size_bytes: 450,
      approved_by_account_id: ACCOUNT_ID,
      approved_at: "2026-08-27T04:15:00Z",
      expires_at: "2026-08-28T04:14:00Z",
      created_at: "2026-08-27T04:14:00Z",
    },
    request_id: "req_prd125_bundle",
    trace_id: "trc_prd125_bundle",
  };
}

function workspaceResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
    },
  });
}

function localResponse(payload, status = 200, csrf = "") {
  const headers = { "Content-Type": "application/json", "Cache-Control": "no-store, private" };
  if (csrf) headers["X-CSRF-Token"] = csrf;
  return new Response(JSON.stringify(payload), { status, headers });
}

test("validation and support clients enforce Cookie, CSRF, CAS, step-up, and trimmed DTOs", async (t) => {
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  t.after(() => {
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  });
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${"e".repeat(43)}` };
  const started = validationResponse();
  const completed = validationResponse({ status: "completed", failedCode: "provider_connectivity" });
  const accepted = {
    acceptance: acceptance(),
    idempotent_replay: false,
    request_id: "req_prd125_acceptance",
    trace_id: "trc_prd125_acceptance",
  };
  const replies = [
    workspaceResponse(started, 201),
    workspaceResponse(completed),
    workspaceResponse(accepted, 201),
    workspaceResponse(cloudBundle()),
  ];
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return replies.shift();
  };

  assert.equal((await startEdgeValidation({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    idempotencyKey: "prd125-validation-start-0001",
  })).validation.status, "requested");
  assert.equal((await getEdgeValidation({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    validationRunId: FIRST_RUN_ID,
  })).validation.outcome, "failed");
  assert.equal((await acceptEdgeDelivery({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    validationRunId: RETRY_RUN_ID,
    expectedRevision: 3,
    idempotencyKey: "prd125-delivery-accept-0001",
    stepUpGrant: "s".repeat(43),
  })).acceptance.delivery_acceptance_id, ACCEPTANCE_ID);
  assert.equal((await getEdgeSupportBundle({
    controlApiUrl: CONTROL_URL,
    bundleId: BUNDLE_ID,
  })).bundle.entries[4].classification, "log_summary");

  assert.equal(calls.every((call) => call.options.credentials === "include"), true);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "e".repeat(43));
  assert.equal(calls[0].options.headers["Idempotency-Key"], "prd125-validation-start-0001");
  assert.equal("X-CSRF-Token" in calls[1].options.headers, false);
  assert.equal(calls[2].options.headers["If-Match"], '"3"');
  assert.equal(calls[2].options.headers["X-Step-Up-Grant"], "s".repeat(43));
  assert.equal(calls[3].url, `${CONTROL_URL}/edge/support-bundles/${BUNDLE_ID}`);

  const leaked = cloudBundle();
  leaked.bundle.object_reference = "forbidden/object";
  globalThis.fetch = async () => workspaceResponse(leaked);
  await assert.rejects(
    getEdgeSupportBundle({ controlApiUrl: CONTROL_URL, bundleId: BUNDLE_ID }),
    (error) => error.code === "edge.response_invalid",
  );
});

test("local support client previews fixed collectors and requires customer CAS approval", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
    clearLocalAdminSession();
  });
  const csrf = "f".repeat(43);
  const cancelled = { ...localBundle(), status: "cancelled" };
  const replies = [
    localResponse({
      deployment_id: DEPLOYMENT_ID,
      expires_at: Math.floor(Date.now() / 1000) + 600,
    }, 201, csrf),
    localResponse(localBundle(), 201),
    localResponse(localBundle("uploaded")),
    localResponse(cancelled),
  ];
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return replies.shift();
  };
  const handoff = new TextEncoder().encode("local-prd125-handoff-credential-123456789");
  await createLocalAdminSession({
    localApiOrigin: LOCAL_URL,
    deploymentId: DEPLOYMENT_ID,
    accountId: ACCOUNT_ID,
    tenantId: TENANT_ID,
    workspaceId: WORKSPACE_ID,
    handoffCredential: handoff,
  });
  assert.equal(handoff.every((value) => value === 0), true);
  const preview = await previewLocalSupportBundle({ deploymentId: DEPLOYMENT_ID });
  assert.deepEqual(preview.manifest.entries.map((item) => item.collector_code), COLLECTORS);
  assert.equal((await approveLocalSupportBundle({
    deploymentId: DEPLOYMENT_ID,
    bundleId: BUNDLE_ID,
    revision: 1,
  })).status, "uploaded");
  assert.equal((await cancelLocalSupportBundle({
    deploymentId: DEPLOYMENT_ID,
    bundleId: BUNDLE_ID,
    revision: 1,
  })).status, "cancelled");
  assert.deepEqual(JSON.parse(calls[1].options.body), { collector_codes: COLLECTORS });
  assert.equal(calls[1].options.headers["X-CSRF-Token"], csrf);
  assert.equal(calls[2].options.headers["If-Match"], "1");
  assert.equal(calls[3].options.method, "DELETE");
});

test("admin validation state retries only failed checks and signs one immutable generation", () => {
  const auth = context("admin", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_VALIDATION_READ,
    PERMISSIONS.EDGE_VALIDATION_MANAGE,
    PERMISSIONS.EDGE_DELIVERY_ACCEPT,
  ]);
  const owner = privateGatewayPageOwner(auth, shell(auth));
  assert.equal(owner.canManageValidations, true);
  assert.equal(owner.canAcceptDelivery, true);
  assert.equal(owner.canManageSupportBundles, false);
  const state = createPrivateGatewayPageState();
  state.enter(owner);
  const load = state.beginLoad(owner, 1);
  assert.equal(state.acceptLoad(
    load,
    { deployments: [deployment()] },
    owner,
    1,
    [],
    [],
    null,
    null,
  ), true);
  state.finishLoad(load);

  const start = state.beginValidation(owner, DEPLOYMENT_ID, "start");
  assert.equal(state.acceptValidation(start, validationResponse(), owner), true);
  state.finishValidation(start);
  const refresh = state.beginValidation(owner, DEPLOYMENT_ID, "refresh");
  assert.equal(state.acceptValidation(
    refresh,
    validationResponse({ status: "completed", failedCode: "provider_connectivity" }),
    owner,
  ), true);
  state.finishValidation(refresh);
  let html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /模型服务连通性[\s\S]*失败[\s\S]*检查本地模型服务连接/u);
  assert.match(html, /仅重试失败项/u);

  const retry = state.beginValidation(owner, DEPLOYMENT_ID, "retry_failed");
  const passed = validationResponse({
    runId: RETRY_RUN_ID,
    sourceRunId: FIRST_RUN_ID,
    attempt: 2,
    status: "completed",
  });
  assert.equal(state.acceptValidation(retry, passed, owner), true);
  state.finishValidation(retry);
  assert.equal(state.openDeliveryAcceptance(owner, DEPLOYMENT_ID), true);
  const acceptanceOperation = state.beginDeliveryAcceptance(owner);
  assert.equal(Object.hasOwn(acceptanceOperation, "verificationCode"), false);
  assert.equal(JSON.stringify(state.snapshot()).includes("123456"), false);
  assert.equal(state.acceptDeliveryAcceptance(acceptanceOperation, {
    acceptance: acceptance(),
    idempotent_replay: false,
    request_id: "req_prd125_acceptance",
    trace_id: "trc_prd125_acceptance",
  }, owner), true);
  state.finishDeliveryAcceptance(acceptanceOperation);
  html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /全部通过[\s\S]*交付已签署/u);
  assert.doesNotMatch(html, /evidence_sha256|input_contract_sha256/u);
});

test("security admin previews local files but Workspace projection removes paths and object metadata", () => {
  const auth = context("security_admin", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_SECRET_REFERENCE_READ,
    PERMISSIONS.EDGE_VALIDATION_READ,
    PERMISSIONS.EDGE_SUPPORT_BUNDLE_READ,
  ]);
  const owner = privateGatewayPageOwner(auth, shell(auth));
  assert.equal(owner.canManageValidations, false);
  assert.equal(owner.canAcceptDelivery, false);
  assert.equal(owner.canManageSupportBundles, true);
  const state = createPrivateGatewayPageState();
  state.enter(owner);
  const load = state.beginLoad(owner, 1);
  assert.equal(state.acceptLoad(
    load,
    { deployments: [deployment()] },
    owner,
    1,
    [],
    [],
    { references: [] },
    null,
  ), true);
  state.finishLoad(load);
  assert.equal(state.openSecretDialog(owner, DEPLOYMENT_ID, "handoff"), true);
  const localSession = state.beginLocalSession(owner, LOCAL_URL);
  assert.equal(state.acceptLocalSession(localSession, {
    local_api_origin: LOCAL_URL,
    deployment_id: DEPLOYMENT_ID,
    expires_at: Math.floor(Date.now() / 1000) + 600,
  }, owner), true);
  state.finishLocalSession(localSession);

  const preview = state.beginSupportBundle(owner, DEPLOYMENT_ID, "preview");
  assert.equal(state.acceptSupportBundle(preview, localBundle(), owner), true);
  state.finishSupportBundle(preview);
  let html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /脱敏支持包预览已生成/u);
  assert.match(html, /meta\/version\.json/u);
  assert.match(html, /批准加密上传/u);
  assert.doesNotMatch(html, /authorization|cookie|secret_value/iu);

  const approve = state.beginSupportBundle(owner, DEPLOYMENT_ID, "approve");
  assert.equal(state.acceptSupportBundle(approve, {
    local: localBundle("uploaded"),
    cloud: cloudBundle(),
  }, owner), true);
  state.finishSupportBundle(approve);
  html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /可供授权支持读取[\s\S]*密文大小[\s\S]*日志事件摘要/u);
  assert.doesNotMatch(html, /meta\/version\.json|object_reference|object_sha256|approval_proof/iu);
});

test("PRD-125 wiring keeps verification material out of state and browser Storage", async () => {
  const [clientSource, localSource, stateSource, renderSource, bindingSource, mainSource] = await Promise.all([
    readFile(new URL("../../src/lib/edgeManagement.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/localSecretManagement.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/private-gateways/privateGatewayPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/private-gateways/renderPrivateGateways.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/private-gateways/bindPrivateGateways.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
  ]);
  assert.match(clientSource, /X-Step-Up-Grant/u);
  assert.match(localSource, /\/local\/support-bundles/u);
  assert.match(bindingSource, /data-private-gateway-acceptance-dialog[\s\S]*input\.value = ""/u);
  assert.match(mainSource, /createIdentityStepUpGrant[\s\S]*acceptEdgeDelivery/u);
  assert.match(mainSource, /approveLocalSupportBundle[\s\S]*getEdgeSupportBundle/u);
  assert.doesNotMatch(stateSource, /verificationCode|stepUpGrant|approval_proof/iu);
  assert.doesNotMatch(renderSource, /object_sha256|approval_proof|raw[_ -]?log/iu);
  assert.doesNotMatch(`${localSource}\n${bindingSource}`, /localStorage|sessionStorage|indexedDB/iu);
});
