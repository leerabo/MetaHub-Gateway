import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createMigrationOperationPageState,
  migrationOperationPageOwner,
} from "../../src/features/migration-operations/migrationOperationPageState.js";
import { renderMigrationOperations } from "../../src/features/migration-operations/renderMigrationOperations.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createMigrationAssessment,
  createMigrationRun,
  cutoverMigrationRun,
  getMigrationDifferences,
  rollbackMigrationRun,
} from "../../src/lib/migrationOperations.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const ASSESSMENT_ID = `mas_${"2".repeat(32)}`;
const PLAN_ID = `mpl_${"3".repeat(32)}`;
const RUN_ID = `mrn_${"4".repeat(32)}`;
const ROLLBACK_PLAN_ID = `mrp_${"5".repeat(32)}`;
const WORKSPACE_CSRF = "W".repeat(43);
const STEP_UP = "migration-step-up-grant-00000001";
const IDEMPOTENCY = "migration-command-test-000001";
const REQUEST_ID = "req_prd131_test";
const TRACE_ID = "trc_prd131_test";
const NOW = "2026-08-29T08:00:00.000Z";

test("migration clients use only Workspace cookies and enforce CSRF, CAS, step-up, and idempotency", async (t) => {
  installCookies(t);
  const calls = installFetch(t, (url) => {
    const path = new URL(url).pathname;
    if (path === "/migrations/assessments") {
      return jsonResponse(assessmentResponse(), { status: 201, etag: 1 });
    }
    if (path === "/migrations/runs") return jsonResponse(runResponse(), { status: 201, etag: 1 });
    if (path === `/migrations/${RUN_ID}/differences`) return jsonResponse(differenceResponse(RUN_ID));
    if (path === `/migrations/${RUN_ID}/cutover`) {
      return jsonResponse(runResponse(completedRun()), { etag: 2 });
    }
    if (path === `/migrations/${RUN_ID}/rollback`) {
      return jsonResponse(runResponse(rolledBackRun()), { etag: 4 });
    }
    throw new Error(`unexpected route ${path}`);
  });

  const assessment = await createMigrationAssessment({
    controlApiUrl: CONTROL_URL,
    sourceKind: "legacy_tenant",
    manifestVersion: "legacy-v1",
    sourceManifestSha256: "a".repeat(64),
    domains: ["audit"],
    reasonCode: "initial_assessment",
    confirmed: true,
    idempotencyKey: IDEMPOTENCY,
  });
  const run = await createMigrationRun({
    controlApiUrl: CONTROL_URL,
    assessmentId: ASSESSMENT_ID,
    expectedAssessmentRevision: 1,
    reasonCode: "customer_approved",
    confirmed: true,
    idempotencyKey: IDEMPOTENCY,
  });
  const differences = await getMigrationDifferences({
    controlApiUrl: CONTROL_URL,
    migrationId: RUN_ID,
  });
  const cutover = await cutoverMigrationRun({
    controlApiUrl: CONTROL_URL,
    runId: RUN_ID,
    expectedRevision: 1,
    reasonCode: "customer_approved",
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  const rollback = await rollbackMigrationRun({
    controlApiUrl: CONTROL_URL,
    runId: RUN_ID,
    expectedRevision: 2,
    reasonCode: "customer_requested_rollback",
    confirmed: true,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });

  assert.equal(assessment.assessment.workspace_id, WORKSPACE_ID);
  assert.equal(run.run.status, "observing");
  assert.deepEqual(differences.items, []);
  assert.equal(cutover.run.status, "completed");
  assert.equal(rollback.run.status, "rolled_back");
  assert.equal(calls.length, 5);
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.redirect, "error");
    assert.equal("Authorization" in call.options.headers, false);
  }
  assert.equal(calls[0].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[0].options.headers["Idempotency-Key"], IDEMPOTENCY);
  assert.equal(calls[0].options.headers["If-Match"], undefined);
  assert.equal(calls[1].options.headers["If-Match"], '"1"');
  assert.equal(calls[2].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[3].options.headers["If-Match"], '"1"');
  assert.equal(calls[3].options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.equal(calls[4].options.headers["If-Match"], '"2"');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    source_kind: "legacy_tenant",
    manifest_version: "legacy-v1",
    source_manifest_sha256: "a".repeat(64),
    domains: ["audit"],
    reason_code: "initial_assessment",
    confirmed: true,
  });
});

test("migration client rejects unknown response fields, malformed domains, and owner drift", async (t) => {
  installCookies(t);
  installFetch(t, () => jsonResponse(assessmentResponse({ source_url: "forbidden" }), {
    status: 201,
    etag: 1,
  }));
  await assert.rejects(
    () => createMigrationAssessment({
      controlApiUrl: CONTROL_URL,
      sourceKind: "legacy_tenant",
      manifestVersion: "legacy-v1",
      sourceManifestSha256: "a".repeat(64),
      domains: ["audit"],
      reasonCode: "initial_assessment",
      confirmed: true,
      idempotencyKey: IDEMPOTENCY,
    }),
    (error) => error instanceof AuthApiError && error.code === "migration.response_invalid",
  );
  await assert.rejects(
    () => createMigrationAssessment({
      controlApiUrl: CONTROL_URL,
      sourceKind: "legacy_tenant",
      manifestVersion: "legacy-v1",
      sourceManifestSha256: "a".repeat(64),
      domains: ["workspace", "audit"],
      reasonCode: "initial_assessment",
      confirmed: true,
      idempotencyKey: IDEMPOTENCY,
    }),
    (error) => error instanceof AuthApiError && error.code === "migration.request_invalid",
  );

  globalThis.fetch = async () => jsonResponse({
    ...differenceResponse(RUN_ID),
    run_id: `mrn_${"9".repeat(32)}`,
  });
  await assert.rejects(
    () => getMigrationDifferences({ controlApiUrl: CONTROL_URL, migrationId: RUN_ID }),
    (error) => error instanceof AuthApiError && error.code === "migration.response_invalid",
  );
});

test("Migration Center state rejects late responses and clears all facts on permission drift", () => {
  const auth = workspaceContext("migration-owner-a", [
    PERMISSIONS.MIGRATION_READ, PERMISSIONS.MIGRATION_MANAGE,
  ]);
  const shell = workspaceShell(auth);
  const owner = migrationOperationPageOwner(auth, shell);
  assert.equal(owner.canManage, true);

  const state = createMigrationOperationPageState();
  assert.equal(state.enter(owner), true);
  let keySequence = 0;
  const firstKey = state.idempotencyKey(
    owner, "assessment", { manifest: "a" }, () => `migration-retry-${++keySequence}-00000000`,
  );
  const replayKey = state.idempotencyKey(
    owner, "assessment", { manifest: "a" }, () => `migration-retry-${++keySequence}-00000000`,
  );
  assert.equal(replayKey, firstKey);
  assert.equal(keySequence, 1);
  const first = state.beginAssessment(owner);
  const second = state.beginAssessment(owner);
  assert.equal(first.signal.aborted, true);
  assert.equal(state.acceptAssessment(first, assessmentResponse(), owner), false);
  assert.equal(state.acceptAssessment(second, assessmentResponse(), owner), true);
  state.finish(second);
  const differences = state.beginDifferences(owner, ASSESSMENT_ID);
  assert.equal(state.acceptDifferences(
    differences, differenceResponse(null), owner,
  ), true);
  state.finish(differences);
  const run = state.beginRun(owner);
  assert.equal(state.acceptRun(run, runResponse(), owner), true);
  state.finish(run);
  assert.equal(state.openTransition(owner, "cutover"), true);
  const transition = state.beginTransition(owner, {
    action: "cutover",
    reasonCode: "customer_approved",
    idempotencyKey: IDEMPOTENCY,
  });
  assert.ok(transition);
  assert.doesNotMatch(
    JSON.stringify(state.snapshot()),
    /verification_code|step_up_grant|credential_value|access_token|bearer/iu,
  );

  const readOnlyAuth = workspaceContext("migration-owner-b", [PERMISSIONS.MIGRATION_READ]);
  const readOnlyOwner = migrationOperationPageOwner(readOnlyAuth, workspaceShell(readOnlyAuth));
  assert.equal(state.enter(readOnlyOwner), true);
  assert.equal(transition.signal.aborted, true);
  assert.equal(state.snapshot().assessment, null);
  assert.equal(state.snapshot().run, null);
  assert.equal(state.acceptTransition(transition, runResponse(completedRun()), readOnlyOwner), false);
});

test("Migration Center renders a compact fixed-schema workflow without source selectors", () => {
  const auth = workspaceContext("migration-render", [
    PERMISSIONS.MIGRATION_READ, PERMISSIONS.MIGRATION_MANAGE,
  ]);
  const shell = workspaceShell(auth);
  const owner = migrationOperationPageOwner(auth, shell);
  const state = createMigrationOperationPageState();
  state.enter(owner);
  const assessment = state.beginAssessment(owner);
  state.acceptAssessment(assessment, assessmentResponse(), owner);
  state.finish(assessment);
  const run = state.beginRun(owner);
  state.acceptRun(run, runResponse(), owner);
  state.finish(run);

  const html = renderMigrationOperations(state.snapshot(), auth, shell);
  assert.match(html, /迁移中心/u);
  assert.match(html, /来源评估/u);
  assert.match(html, /计划停机/u);
  assert.match(html, /data-migration-open-transition="cutover"/u);
  assert.match(html, /data-migration-open-transition="rollback"/u);
  assert.doesNotMatch(
    html,
    /source_url|endpoint_url|storage_path|object_url|table_name|column_name|SQL|Bearer|Authorization/iu,
  );

  state.openTransition(owner, "cutover");
  const dialogHtml = renderMigrationOperations(state.snapshot(), auth, shell);
  assert.match(dialogHtml, /data-migration-transition-form/u);
  assert.match(dialogHtml, /autocomplete="one-time-code"/u);
  assert.doesNotMatch(dialogHtml, /name="(?:token|secret|password|sql|url|path)"/iu);

  const readOnlyAuth = workspaceContext("migration-readonly", [PERMISSIONS.MIGRATION_READ]);
  const readOnlyHtml = renderMigrationOperations(null, readOnlyAuth, workspaceShell(readOnlyAuth));
  assert.match(readOnlyHtml, /只读/u);
  assert.doesNotMatch(readOnlyHtml, /data-migration-assessment-form|data-migration-run-form/u);
});

test("Migration Center sources have no persistence or bearer path and main owns every cleanup boundary", async () => {
  const [client, pageState, binding, main, css] = await Promise.all([
    readFile(new URL("../../src/lib/migrationOperations.js", import.meta.url), "utf8"),
    readFile(new URL(
      "../../src/features/migration-operations/migrationOperationPageState.js", import.meta.url,
    ), "utf8"),
    readFile(new URL(
      "../../src/features/migration-operations/bindMigrationOperations.js", import.meta.url,
    ), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(`${client}\n${pageState}\n${binding}`, /localStorage|sessionStorage|indexedDB/iu);
  assert.doesNotMatch(client, /Authorization|Bearer/iu);
  assert.match(client, /credentials:\s*"include"/u);
  assert.match(client, /cache:\s*"no-store"/u);
  assert.match(binding, /input\.value = ""/u);
  assert.match(main, /case "migration-operations":[\s\S]*loadMigrationOperationsState/u);
  assert.match(main, /nextItemId !== "migration-operations"[\s\S]*migrationOperationPageState\.clear/u);
  assert.match(main, /function renderLogin[\s\S]*migrationOperationPageState\.clear/u);
  assert.match(main, /clearMigrationOperationStateOnIdentityChange/u);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*\.migration-table/u);
  assert.match(css, /@media \(max-width: 420px\)[\s\S]*\.migration-stages/u);
});

function assessmentResponse(extraAssessment = {}) {
  return {
    assessment: {
      assessment_id: ASSESSMENT_ID,
      workspace_id: WORKSPACE_ID,
      source_kind: "legacy_tenant",
      manifest_version: "legacy-v1",
      source_manifest_sha256: "a".repeat(64),
      status: "ready",
      object_count: 10,
      blocking_difference_count: 0,
      write_watermark_before: 7,
      write_watermark_after: 7,
      credential_reentry_required_count: 1,
      permission_expansion_count: 0,
      revision: 1,
      created_at: NOW,
      completed_at: NOW,
      snapshots: [{
        domain_code: "audit",
        adapter_version: "migration-adapter-v1",
        source_count: 10,
        target_count: 10,
        source_sha256: "b".repeat(64),
        target_sha256: "b".repeat(64),
        source_sequence: 7,
        target_sequence: 7,
        source_watermark: 7,
        target_watermark: 7,
        matches: true,
      }],
      plan: {
        plan_id: PLAN_ID,
        assessment_id: ASSESSMENT_ID,
        generation: 1,
        status: "ready",
        phases: ["assessment", "backfill", "dual_read", "freeze", "cutover", "observe"],
        plan_sha256: "c".repeat(64),
        planned_downtime_seconds: 300,
        rollback_deadline_seconds: 7200,
        revision: 1,
        created_at: NOW,
      },
      ...extraAssessment,
    },
    idempotent_replay: false,
    request_id: REQUEST_ID,
    trace_id: TRACE_ID,
  };
}

function runResponse(run = observingRun()) {
  return {
    run,
    idempotent_replay: false,
    request_id: REQUEST_ID,
    trace_id: TRACE_ID,
  };
}

function observingRun() {
  return baseRun({
    status: "observing",
    revision: 1,
    checkpoints: ["assessment", "backfill", "dual_read", "freeze"].map(checkpoint),
  });
}

function completedRun() {
  return baseRun({
    status: "completed",
    revision: 2,
    completed_at: NOW,
    checkpoints: ["assessment", "backfill", "dual_read", "freeze", "cutover"].map(checkpoint),
  });
}

function rolledBackRun() {
  return baseRun({
    status: "rolled_back",
    revision: 4,
    completed_at: NOW,
    checkpoints: [
      "assessment", "backfill", "dual_read", "freeze", "cutover", "rollback",
    ].map(checkpoint),
    rollback_plan: rollbackPlan("completed"),
  });
}

function baseRun(overrides = {}) {
  return {
    run_id: RUN_ID,
    assessment_id: ASSESSMENT_ID,
    plan_id: PLAN_ID,
    status: "observing",
    generation: 1,
    freeze_generation: 1,
    blocking_difference_count: 0,
    revision: 1,
    started_at: NOW,
    updated_at: NOW,
    completed_at: null,
    checkpoints: [],
    rollback_plan: rollbackPlan("ready"),
    ...overrides,
  };
}

function checkpoint(phase, index) {
  return {
    checkpoint_id: `mcp_${String(index + 1).repeat(32)}`,
    run_id: RUN_ID,
    phase_code: phase,
    phase_sequence: index + 1,
    input_sha256: "d".repeat(64),
    output_sha256: "e".repeat(64),
    source_watermark: 7,
    target_watermark: 7,
    freeze_generation: 1,
    status: "verified",
    created_at: NOW,
  };
}

function rollbackPlan(status) {
  return {
    rollback_plan_id: ROLLBACK_PLAN_ID,
    run_id: RUN_ID,
    previous_run_id: null,
    last_safe_checkpoint_id: `mcp_${"4".repeat(32)}`,
    compensation_order: ["freeze_writes", "restore_head", "replay_receipts", "verify_owner"],
    rollback_plan_sha256: "f".repeat(64),
    status,
    revision: status === "ready" ? 1 : 2,
    completed_at: status === "completed" ? NOW : null,
  };
}

function differenceResponse(runId) {
  return {
    assessment_id: ASSESSMENT_ID,
    run_id: runId,
    items: [],
    support_scoped: false,
    request_id: REQUEST_ID,
    trace_id: TRACE_ID,
  };
}

function workspaceContext(fingerprint, permissionCodes) {
  return withPermissionSnapshot({
    user: {
      tenant_id: "11111111-1111-4111-8111-111111111111",
      account_id: "gac_prd131",
      user_id: "usr_prd131",
      email: "owner@example.invalid",
      role: "admin",
    },
    tenant: { tenant_id: "11111111-1111-4111-8111-111111111111" },
    session: { session_id: "ses_prd131", session_revision: 1 },
  }, permissionCodes, { authorizationFingerprint: fingerprint });
}

function workspaceShell(auth) {
  return {
    state: "ok",
    activeNavigationItemId: "migration-operations",
    navigation: {
      active_navigation_item_id: "migration-operations",
      permission_codes: auth.permission_codes,
      permission_revision: auth.permission_revision,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: [{
        id: "migration-operations",
        target_section: "migration-operations",
        required_permissions: [PERMISSIONS.MIGRATION_READ],
        visible: true,
        disabled: false,
      }],
    },
  };
}

function installCookies(t) {
  const original = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}` };
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

function jsonResponse(payload, { status = 200, etag } = {}) {
  const headers = {
    "Content-Type": "application/json",
    "Cache-Control": "private, no-store",
    "X-Request-ID": payload.request_id,
    "X-Trace-ID": payload.trace_id,
  };
  if (etag !== undefined) headers.ETag = `"${etag}"`;
  return new Response(JSON.stringify(payload), { status, headers });
}
