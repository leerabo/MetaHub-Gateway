import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  CONSOLE_MIGRATION_DOMAINS,
  getConsoleMigrationStatus,
  normalizeConsoleMigrationStatus,
  submitConsoleMigrationBackfill,
  submitConsoleMigrationCutover,
  submitConsoleMigrationRollback,
} from "../../src/lib/consoleMigration.js";
import { createConsoleMigrationController } from "../../src/features/console-migration/consoleMigrationController.js";
import { createConsoleMigrationStore } from "../../src/features/console-migration/consoleMigrationStore.js";
import { renderConsoleMigration } from "../../src/features/console-migration/renderConsoleMigration.js";

const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_DOCUMENT = globalThis.document;
const BASE_URL = "https://console.invalid";
const FINGERPRINT = "a".repeat(64);
const CSRF = "c".repeat(43);
const STEP_UP = "s".repeat(43);
const NOW = "2026-09-04T00:00:00Z";

test.afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  if (ORIGINAL_DOCUMENT === undefined) delete globalThis.document;
  else globalThis.document = ORIGINAL_DOCUMENT;
});

function statusResponse() {
  return {
    domains: CONSOLE_MIGRATION_DOMAINS.map((domain_code, index) => ({
      domain_code,
      source_migration: `013${index + 1}`,
      migration_count: 1,
      blocked_count: 0,
      active_count: 0,
    })),
    items: [{
      migration_id: `cdm_${"1".repeat(32)}`,
      workspace_ref: "2".repeat(64),
      domain_code: "channel_attribution",
      state: "inventoried",
      progress_basis_points: 0,
      operation_generation: 1,
      target_generation: 0,
      revision: 1,
      reconciliation_status: "missing",
      reason_code: "reconciliation_missing",
      approval_ready: false,
      security_review_ready: false,
      route_ready: false,
      scheduler_ready: true,
      last_safe_at: NOW,
      allowed_actions: ["backfill.start", "backfill.cancel"],
    }],
    next_cursor: null,
    scheduler: {
      baseline_slot_count: 41,
      final_installer_count: 36,
      cluster_context_count: 37,
      queue_contract_count: 64,
      registry_generation: 1,
      enabled_context_count: 37,
      active_lease_count: 1,
      manifest_matches: true,
    },
    runtime_surfaces: [
      "scheduler-lease-coordinator", "risk-fact-sink", "control-telemetry",
    ].map((runtime_surface_code) => ({
      runtime_surface_code,
      execution_class: runtime_surface_code === "scheduler-lease-coordinator"
        ? "per_replica_control_bounded"
        : "per_replica_bounded",
      queue_capacity: 100,
      batch_limit: 10,
      healthy_replica_count: 1,
      degraded_replica_count: 0,
    })),
    contract: {
      contract_generation: 0,
      route_deny_epoch: 1,
      legacy_write_fenced: false,
      status: "expanded",
      blocking_check_count: 0,
    },
    permission_fingerprint: FINGERPRINT,
    request_id: "req_prd147",
    trace_id: "trc_prd147",
  };
}

function commandResponse(operationId, revision = 1) {
  return {
    operation_id: operationId,
    migration_id: operationId.startsWith("cdm_") ? operationId : null,
    state: operationId.startsWith("cmb_") ? "prepared" : "inventoried",
    revision,
    operation_generation: 1,
    accepted: true,
    idempotent_replay: false,
    polling_location_code: "console-migration-status",
    contract_generation: 0,
    registry_generation: 1,
    permission_fingerprint: FINGERPRINT,
    request_id: "req_command",
    trace_id: "trc_command",
  };
}

function privateResponse(payload, revision = null) {
  const headers = {
    "cache-control": "private, no-store",
    "content-type": "application/json",
    "x-permission-fingerprint": FINGERPRINT,
  };
  if (revision !== null) {
    headers.etag = `"${revision}"`;
    headers["x-polling-location-code"] = "console-migration-status";
  }
  return new Response(JSON.stringify(payload), { status: 200, headers });
}

test("status client validates frozen manifests and private response headers", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return privateResponse(statusResponse());
  };
  const result = await getConsoleMigrationStatus({
    controlApiUrl: BASE_URL,
    domainCode: "channel_attribution",
    state: "inventoried",
    workspaceRef: "f".repeat(64),
    limit: 25,
  });
  assert.equal(result.domains.length, 8);
  assert.equal(Object.isFrozen(result.items[0]), true);
  const request = calls[0];
  assert.match(request.url, /domain_code=channel_attribution/);
  assert.match(request.url, /workspace_ref=f{64}/);
  assert.equal(request.options.credentials, "include");
  assert.equal(request.options.cache, "no-store");
  assert.equal(new Headers(request.options.headers).has("authorization"), false);

  assert.throws(
    () => normalizeConsoleMigrationStatus(
      { ...statusResponse(), unexpected_private_field: "blocked" },
      privateResponse(statusResponse()).headers,
    ),
    (error) => error?.status === 502,
  );
  const duplicate = statusResponse();
  duplicate.domains[7] = { ...duplicate.domains[0] };
  assert.throws(
    () => normalizeConsoleMigrationStatus(duplicate, privateResponse(duplicate).headers),
    (error) => error?.status === 502,
  );
});

test("command clients send exact CSRF, CAS, step-up and idempotency contracts", async () => {
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${CSRF}` };
  const migrationId = `cdm_${"3".repeat(32)}`;
  const queue = [
    privateResponse(commandResponse(`cmb_${"4".repeat(32)}`), 1),
    privateResponse({ ...commandResponse(migrationId, 2), state: "quiescing" }, 2),
    privateResponse({ ...commandResponse(migrationId, 3), state: "rollback_queued" }, 3),
  ];
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return queue.shift();
  };

  await submitConsoleMigrationBackfill({
    controlApiUrl: BASE_URL,
    command: {
      action: "prepare",
      domain_code: "workspace_navigation",
      scope: "all_active",
      workspace_ref: null,
      expected_revision: 0,
      resource_profile: "bounded-v1",
      confirmed: true,
    },
    expectedRevision: 0,
    stepUpGrant: STEP_UP,
    idempotencyKey: "prd147-backfill-prepare-0001",
  });
  await submitConsoleMigrationCutover({
    controlApiUrl: BASE_URL,
    command: {
      action: "prepare",
      migration_id: migrationId,
      expected_revision: 1,
      confirmed: true,
    },
    expectedRevision: 1,
    stepUpGrant: STEP_UP,
    idempotencyKey: "prd147-cutover-prepare-0001",
  });
  await submitConsoleMigrationRollback({
    controlApiUrl: BASE_URL,
    command: {
      action: "prepare",
      migration_id: migrationId,
      expected_revision: 2,
      reason_code: "operator_recovery",
      target_generation: 1,
      confirmed: true,
    },
    expectedRevision: 2,
    stepUpGrant: STEP_UP,
    idempotencyKey: "prd147-rollback-prepare-0001",
  });

  assert.equal(calls.length, 3);
  for (const { options } of calls) {
    const headers = new Headers(options.headers);
    assert.equal(headers.get("x-csrf-token"), CSRF);
    assert.equal(headers.get("x-step-up-grant"), STEP_UP);
    assert.match(headers.get("idempotency-key"), /^prd147-/u);
    assert.match(headers.get("if-match"), /^"[0-2]"$/u);
    assert.equal(headers.has("authorization"), false);
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
  }
  assert.equal(JSON.parse(calls[0].options.body).workspace_ref, null);
});

test("commands reject unknown fields, mismatched CAS and unsafe rollback targets", async () => {
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${CSRF}` };
  const base = {
    action: "start",
    migration_id: `cdm_${"5".repeat(32)}`,
    expected_revision: 2,
    reconciliation_generation: 0,
    resource_profile: "bounded-v1",
    confirmed: true,
  };
  assert.throws(
    () => submitConsoleMigrationBackfill({
      controlApiUrl: BASE_URL,
      command: { ...base, tenant_id: "forbidden" },
      expectedRevision: 2,
      stepUpGrant: STEP_UP,
      idempotencyKey: "prd147-backfill-start-0001",
    }),
    (error) => error?.code === "console_migration.invalid",
  );
  await assert.rejects(
    submitConsoleMigrationBackfill({
      controlApiUrl: BASE_URL,
      command: base,
      expectedRevision: 3,
      stepUpGrant: STEP_UP,
      idempotencyKey: "prd147-backfill-start-0002",
    }),
    (error) => error?.code === "console_migration.invalid",
  );
  assert.throws(
    () => submitConsoleMigrationRollback({
      controlApiUrl: BASE_URL,
      command: {
        action: "prepare",
        migration_id: base.migration_id,
        expected_revision: 2,
        reason_code: "restore_legacy_routes",
        target_generation: 0,
        confirmed: true,
      },
      expectedRevision: 2,
      stepUpGrant: STEP_UP,
      idempotencyKey: "prd147-rollback-prepare-0002",
    }),
    (error) => error?.code === "console_migration.invalid",
  );
});

function owner(marker = "6") {
  return {
    operator: {
      operator_id: `pto_${marker.repeat(32)}`,
      revision: 1,
      status: "active",
      roles: ["platform_ops"],
    },
    permissions: [
      "platform.console_migration.read",
      "platform.console_migration.backfill",
    ],
    expires_at: "2026-09-04T01:00:00Z",
  };
}

test("migration store isolates owner, late response, cursor and retry intent", () => {
  const store = createConsoleMigrationStore();
  const navigation = [{ id: "console-migration" }];
  const firstOwner = owner("6");
  assert.equal(store.enter(firstOwner, navigation), true);
  const first = store.begin("load");
  assert.ok(first);
  store.leave();
  assert.equal(store.enter(owner("7"), navigation), true);
  assert.equal(store.acceptLoad(first, statusResponse()), false);

  const second = store.begin("load");
  const response = { ...statusResponse(), next_cursor: "x".repeat(43) };
  assert.equal(store.acceptLoad(second, response), true);
  assert.equal(store.snapshot().canNext, true);
  assert.doesNotMatch(JSON.stringify(store.snapshot()), /x{43}/u);
  assert.equal(store.move("next"), true);
  const paged = store.begin("load");
  assert.equal(paged.cursor, "x".repeat(43));
  assert.equal(store.acceptLoad(paged, statusResponse()), true);

  const intent = store.commandIntent("same-command", () => "prd147:retry:fixed-command");
  assert.equal(intent.idempotencyKey, "prd147:retry:fixed-command");
  assert.doesNotMatch(JSON.stringify(store.snapshot()), /fixed-command/u);
});

test("controller scrubs step-up material and reloads after an accepted command", async () => {
  const secretCode = "123456";
  const secretGrant = "g".repeat(43);
  const migrationId = `cdm_${"8".repeat(32)}`;
  const observed = [];
  const api = {
    consoleMigrationStatus: async () => statusResponse(),
    createIdentityStepUpGrant: async (input) => {
      observed.push({ stage: "step-up", code: input.code });
      return { grant: secretGrant };
    },
    consoleMigrationBackfill: async (input) => {
      observed.push({ stage: "command", grant: input.stepUpGrant });
      return commandResponse(migrationId, 2);
    },
  };
  const controller = createConsoleMigrationController({
    api,
    newIdempotencyKey: () => "prd147:controller:command-0001",
  });
  assert.equal(controller.enter(owner("9"), [{ id: "console-migration" }]), true);
  assert.equal(await controller.load(), true);
  assert.equal(await controller.command("backfill", {
    action: "start",
    migration_id: migrationId,
    expected_revision: 1,
    reconciliation_generation: 0,
    resource_profile: "bounded-v1",
    confirmed: true,
  }, { method: "totp", verificationCode: secretCode }), true);
  assert.deepEqual(observed, [
    { stage: "step-up", code: secretCode },
    { stage: "command", grant: secretGrant },
  ]);
  const serialized = JSON.stringify(controller.snapshot());
  assert.doesNotMatch(serialized, new RegExp(secretCode, "u"));
  assert.doesNotMatch(serialized, new RegExp(secretGrant, "u"));
  assert.equal(controller.snapshot().busy, false);
  assert.equal(controller.snapshot().response.domains.length, 8);
});

test("workbench renders compact status and only backend-authorized actions", () => {
  const response = statusResponse();
  const html = renderConsoleMigration({
    phase: "ready",
    busy: false,
    response,
    filters: { domainCode: "", state: "", workspaceRef: "" },
    canPrevious: false,
    canNext: false,
    feedback: null,
    error: null,
  }, new Set(["platform.console_migration.backfill"]));
  assert.match(html, /控制台迁移治理/u);
  assert.match(html, /开始回填/u);
  assert.match(html, /准备回填/u);
  assert.doesNotMatch(html, /workspace_id|tenant_id|authorization|secret/iu);
});

test("platform shell mounts the dedicated migration workbench and client", async () => {
  const [html, main, state, renderer] = await Promise.all([
    readFile(new URL("../../platform.html", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/platformPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/renderPlatformPage.js", import.meta.url), "utf8"),
  ]);
  assert.match(html, /console-migration\.css/u);
  assert.match(main, /getConsoleMigrationStatus/u);
  assert.match(main, /submitConsoleMigrationBackfill/u);
  assert.match(main, /submitConsoleMigrationCutover/u);
  assert.match(main, /submitConsoleMigrationRollback/u);
  assert.match(state, /createConsoleMigrationController/u);
  assert.match(state, /bindConsoleMigrationInteractions/u);
  assert.match(renderer, /renderConsoleMigration/u);
  assert.doesNotMatch(main, /consoleMigration[^\n]*Authorization/u);
});

test("production surface retires Identity subscription and tenant Plan runtime artifacts", async () => {
  const [
    identityMain,
    identityState,
    identityRenderer,
    identityStyles,
    workspaceMain,
    developerStart,
    quotaClient,
    quotaRenderer,
    manifestSource,
    buildScript,
    dockerfile,
  ] = await Promise.all([
    readFile(new URL("../../src/features/identity/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/identity/identityPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/identity/renderIdentityPage.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/identity.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/workspaceQuota.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/workspace-quota/renderWorkspaceQuota.js", import.meta.url), "utf8"),
    readFile(new URL("../../production-surface-manifest.json", import.meta.url), "utf8"),
    readFile(new URL("../../scripts/build-production.mjs", import.meta.url), "utf8"),
    readFile(new URL("../../../infra/docker/console.Dockerfile", import.meta.url), "utf8"),
  ]);
  const identitySurface = [identityMain, identityState, identityRenderer, identityStyles].join("\n");
  assert.doesNotMatch(
    identitySurface,
    /saasBilling|identity-subscription|data-saas-subscription|catalog\/plans|subscriptions\/current/u,
  );
  assert.match(workspaceMain, /features\/workspace-quota/u);
  assert.match(developerStart, /workspace-quota\/renderWorkspaceQuota/u);
  assert.doesNotMatch(
    workspaceMain,
    /features\/plan-billing|lib\/planBilling|planManagementPageState|loadPlanManagementState/u,
  );
  assert.doesNotMatch(quotaClient, /`\$\{controlApiUrl\}\/plans|normalizePlanPatchRequest|patchCurrentPlan/u);
  assert.doesNotMatch(quotaRenderer, /data-plan-management|plan-billing-|plans-panel/u);

  const manifest = JSON.parse(manifestSource);
  assert.ok(manifest.retired_runtime_modules.includes("src/features/plan-billing/"));
  assert.ok(manifest.retired_runtime_modules.includes("src/lib/saasBilling.js"));
  assert.ok(manifest.retired_external_routes.includes("/subscriptions/current"));
  assert.ok(manifest.retired_runtime_literals.includes("identity-subscription"));
  assert.match(buildScript, /reachableJavascript/u);
  assert.match(buildScript, /production-assets\.json/u);
  const runtimeStage = dockerfile.split(/^FROM\s+/gmu).at(-1);
  assert.match(dockerfile, /RUN npm run build/u);
  assert.match(runtimeStage, /console-web\/dist/u);
  assert.doesNotMatch(runtimeStage, /COPY console-web\/src/u);
});
