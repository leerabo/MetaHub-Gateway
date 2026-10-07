import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformProviderEngineeringController } from "../../src/features/platform-provider-engineering/platformProviderEngineeringController.js";
import { createPlatformProviderEngineeringStore } from "../../src/features/platform-provider-engineering/platformProviderEngineeringStore.js";
import { renderPlatformProviderEngineering } from "../../src/features/platform-provider-engineering/renderPlatformProviderEngineering.js";
import { createWorkspaceProviderEngineeringController } from "../../src/features/workspace-provider-engineering/workspaceProviderEngineeringController.js";
import { createWorkspaceProviderEngineeringStore } from "../../src/features/workspace-provider-engineering/workspaceProviderEngineeringStore.js";
import {
  renderWorkspaceProviderEngineering,
  renderWorkspaceProviderErrorMaps,
} from "../../src/features/workspace-provider-engineering/renderWorkspaceProviderEngineering.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import {
  applyPlatformErrorCatalogCommand,
  applyPlatformProviderAdapterCommand,
  applyWorkspaceProviderErrorOverrideCommand,
  listPlatformAdapterContracts,
  listPlatformProviderAdapters,
  listWorkspaceProviderErrorOverrides,
} from "../../src/lib/providerEngineering.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const NOW = "2026-09-01T08:00:00.000Z";
const PLATFORM_CSRF = "P".repeat(43);
const WORKSPACE_CSRF = "W".repeat(43);
const STEP_UP = "G".repeat(43);
const PLATFORM_KEY = "provider-command-143-0001";
const WORKSPACE_KEY = "override-command-143-0001";
const ADAPTER_ID = `parl_${"a".repeat(32)}`;
const ADAPTER_ID_B = `parl_${"b".repeat(32)}`;
const CATALOG_ID = `pecr_${"c".repeat(32)}`;
const RUN_ID = `pacr_${"d".repeat(32)}`;
const CASE_ID = `pacc_${"e".repeat(32)}`;
const OVERRIDE_ID = `pem_${"f".repeat(32)}`;
const SDK_ID = `sdr_${"1".repeat(32)}`;
const OPERATOR_ID = `pto_${"2".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"3".repeat(32)}`;
const PROVIDER_ID = "provider_143";
const PROVIDER_ID_B = "provider_143_b";

test("PRD-143 clients use credential-isolated Cookie routes and exact write headers", async (t) => {
  installDocument(t);
  const calls = installFetch(t, (url, options) => {
    const parsed = new URL(url);
    const method = options.method ?? "GET";
    if (parsed.pathname === "/platform/provider-adapters" && method === "GET") {
      return jsonResponse(platformAdapterList());
    }
    if (parsed.pathname === `/platform/provider-adapters/${ADAPTER_ID}/contract-runs`) {
      return jsonResponse(parsed.searchParams.get("view") === "cases"
        ? platformContractCases() : platformContractRuns());
    }
    if (parsed.pathname === `/platform/provider-adapters/${ADAPTER_ID}/commands`) {
      return jsonResponse(platformAdapterCommand(), 202);
    }
    if (parsed.pathname === "/platform/provider-error-catalogs/releases") {
      return jsonResponse(platformCatalogCommand());
    }
    if (parsed.pathname === "/workspace/provider-error-overrides" && method === "GET") {
      return jsonResponse(workspaceList(PROVIDER_ID));
    }
    if (parsed.pathname === "/workspace/provider-error-overrides" && method === "PATCH") {
      return jsonResponse(workspaceCommand(PROVIDER_ID));
    }
    throw new Error(`unexpected Provider Engineering route ${method} ${parsed.pathname}`);
  });

  await listPlatformProviderAdapters({
    controlApiUrl: CONTROL_URL,
    filters: { adapter: "openai", status: "released" },
    cursor: "page.cursor",
    limit: 25,
  });
  await listPlatformAdapterContracts({
    controlApiUrl: CONTROL_URL,
    providerAdapterReleaseId: ADAPTER_ID,
    view: "runs",
    filters: { scope: "platform_release_ci", status: "passed" },
    limit: 50,
  });
  await listPlatformAdapterContracts({
    controlApiUrl: CONTROL_URL,
    providerAdapterReleaseId: ADAPTER_ID,
    view: "cases",
    runId: RUN_ID,
    cursor: "case.cursor",
    limit: 100,
  });
  await applyPlatformProviderAdapterCommand({
    controlApiUrl: CONTROL_URL,
    adapterIdentifier: ADAPTER_ID,
    command: { action: "run_contract" },
    expectedRevision: 2,
    idempotencyKey: PLATFORM_KEY,
    stepUpGrant: STEP_UP,
  });
  await applyPlatformErrorCatalogCommand({
    controlApiUrl: CONTROL_URL,
    command: {
      action: "submit_review",
      error_catalog_release_id: CATALOG_ID,
      expected_revision: 2,
    },
    expectedRevision: 2,
    idempotencyKey: PLATFORM_KEY,
    stepUpGrant: STEP_UP,
  });
  await listWorkspaceProviderErrorOverrides({
    controlApiUrl: CONTROL_URL,
    filters: { provider_id: PROVIDER_ID, status: "active" },
    limit: 50,
  });
  await applyWorkspaceProviderErrorOverrideCommand({
    controlApiUrl: CONTROL_URL,
    command: {
      action: "disable",
      provider_error_override_id: OVERRIDE_ID,
      expected_version: 3,
    },
    expectedHeadRevision: 4,
    idempotencyKey: WORKSPACE_KEY,
    stepUpGrant: STEP_UP,
  });

  assert.equal(calls.length, 7);
  for (const { url, options } of calls) {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, undefined);
    assert.doesNotMatch(url, /tenant_id|workspace_id|provider_key|secret|token/iu);
  }
  assert.deepEqual(calls.slice(0, 3).map(({ options }) => options.method ?? "GET"), [
    "GET", "GET", "GET",
  ]);
  assert.equal(new URL(calls[0].url).searchParams.get("cursor"), "page.cursor");
  assert.equal(new URL(calls[2].url).searchParams.get("run_id"), RUN_ID);

  const [adapterWrite, catalogWrite, workspaceWrite] = [calls[3], calls[4], calls[6]];
  assert.equal(adapterWrite.options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(catalogWrite.options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(workspaceWrite.options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  for (const { options } of [adapterWrite, catalogWrite]) {
    assert.equal(options.method, "POST");
    assert.equal(options.headers["If-Match"], '"2"');
    assert.equal(options.headers["Idempotency-Key"], PLATFORM_KEY);
    assert.equal(options.headers["X-Step-Up-Grant"], STEP_UP);
  }
  assert.equal(workspaceWrite.options.method, "PATCH");
  assert.equal(workspaceWrite.options.headers["If-Match"], '"4"');
  assert.equal(workspaceWrite.options.headers["Idempotency-Key"], WORKSPACE_KEY);
  assert.equal(workspaceWrite.options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.deepEqual(JSON.parse(adapterWrite.options.body), { action: "run_contract" });
  assert.deepEqual(JSON.parse(workspaceWrite.options.body), {
    action: "disable",
    provider_error_override_id: OVERRIDE_ID,
    expected_version: 3,
  });
});

test("strict DTOs reject extensions, owner drift, and invalid requests before fetch", async (t) => {
  let calls = 0;
  installFetch(t, () => {
    calls += 1;
    return jsonResponse({
      ...platformAdapterList(),
      adapters: [{ ...adapter(), source_path: "private/source/tree" }],
    });
  });
  await assert.rejects(
    listPlatformProviderAdapters({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError
      && error.status === 502
      && error.code === "platform.provider_engineering_response_invalid",
  );

  globalThis.fetch = async () => {
    calls += 1;
    return jsonResponse({ ...workspaceList(PROVIDER_ID), tenant_id: "forbidden" });
  };
  await assert.rejects(
    listWorkspaceProviderErrorOverrides({
      controlApiUrl: CONTROL_URL,
      filters: { provider_id: PROVIDER_ID },
    }),
    (error) => error instanceof AuthApiError
      && error.status === 502
      && error.code === "workspace.provider_engineering_response_invalid",
  );

  globalThis.fetch = async () => {
    calls += 1;
    return jsonResponse(workspaceList(PROVIDER_ID_B));
  };
  await assert.rejects(
    listWorkspaceProviderErrorOverrides({
      controlApiUrl: CONTROL_URL,
      filters: { provider_id: PROVIDER_ID },
    }),
    (error) => error instanceof AuthApiError && error.status === 502,
  );

  const beforeInvalid = calls;
  await assert.rejects(
    listPlatformProviderAdapters({
      controlApiUrl: CONTROL_URL,
      filters: { tenant_id: "tenant-forbidden" },
    }),
    (error) => error instanceof PlatformApiError && error.code === "platform.request_invalid",
  );
  await assert.rejects(
    applyWorkspaceProviderErrorOverrideCommand({
      controlApiUrl: CONTROL_URL,
      command: {
        action: "disable",
        provider_error_override_id: OVERRIDE_ID,
        expected_version: 3,
        tenant_id: "tenant-forbidden",
      },
      expectedHeadRevision: 4,
      idempotencyKey: WORKSPACE_KEY,
      stepUpGrant: STEP_UP,
    }),
    (error) => error instanceof AuthApiError && error.code === "error_map.input_invalid",
  );
  assert.equal(calls, beforeInvalid);
});

test("legacy adapter baselines preserve catalog versions without weakening release SemVer", async (t) => {
  let responseIndex = 0;
  installFetch(t, () => {
    responseIndex += 1;
    const release = responseIndex === 1
      ? adapter({
        release_version: "adapter-spec-v2",
        protocol_family: "legacy_catalog",
        sdk_release_id: null,
        status: "legacy_baseline",
        release_generation: 0,
        readiness: "legacy",
      })
      : adapter({ release_version: "adapter-spec-v2" });
    return jsonResponse(platformAdapterList({ adapters: [release] }));
  });

  const legacy = await listPlatformProviderAdapters({ controlApiUrl: CONTROL_URL });
  assert.equal(legacy.adapters[0].release_version, "adapter-spec-v2");
  await assert.rejects(
    listPlatformProviderAdapters({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError
      && error.code === "platform.provider_engineering_response_invalid",
  );
});

test("Platform and Workspace stores keep independent owners and reject late tickets", () => {
  const platform = createPlatformProviderEngineeringStore();
  const workspace = createWorkspaceProviderEngineeringStore();
  const platformContextValue = platformContext();
  const authContext = workspaceAuthContext();
  const session = workspaceSession();
  assert.equal(platform.enter(platformContextValue, [{ id: "provider-engineering" }]), true);
  assert.equal(workspace.enter({
    authContext,
    workspaceSession: session,
    activeNavigationItemId: "providers",
    providerId: PROVIDER_ID,
  }), true);

  const platformTicket = platform.begin("adapters");
  const workspaceTicket = workspace.begin("read");
  assert.equal(platform.accept(platformTicket, { adapters: platformAdapterList() }), true);
  assert.equal(workspace.accept(workspaceTicket, { response: workspaceList(PROVIDER_ID) }), true);
  assert.equal(Object.hasOwn(platform.snapshot(), "response"), false);
  assert.equal(Object.hasOwn(workspace.snapshot(), "adapters"), false);

  const stale = platform.begin("runs");
  platform.leave();
  assert.equal(stale.controller.signal.aborted, true);
  assert.equal(platform.accept(stale, { runs: platformContractRuns() }), false);
  assert.equal(platform.snapshot().adapters, null);
  assert.equal(workspace.snapshot().response.compatibility.provider_id, PROVIDER_ID);
});

test("controllers discard late owner responses across Platform leave and Workspace Provider switch", async () => {
  const platformLate = deferred();
  const platform = createPlatformProviderEngineeringController({
    api: {
      providerAdapters: () => platformLate.promise,
      adapterContracts: async () => platformContractRuns(),
    },
  });
  platform.enter(platformContext(), [{ id: "provider-engineering" }]);
  const platformPending = platform.loadAdapters();
  platform.leave();
  platformLate.resolve(platformAdapterList());
  assert.equal(await platformPending, false);
  assert.equal(platform.snapshot().adapters, null);

  const workspaceLate = deferred();
  const firstReadStarted = deferred();
  const workspace = createWorkspaceProviderEngineeringController({
    api: {
      workspaceSession: async () => workspaceSession(),
      providerErrorOverrides: async ({ filters }) => {
        if (filters.provider_id === PROVIDER_ID) {
          firstReadStarted.resolve();
          return workspaceLate.promise;
        }
        return workspaceList(PROVIDER_ID_B);
      },
    },
  });
  const first = workspace.open({
    authContext: workspaceAuthContext(),
    activeNavigationItemId: "providers",
    providerId: PROVIDER_ID,
  });
  await firstReadStarted.promise;
  const second = workspace.open({
    authContext: workspaceAuthContext(),
    activeNavigationItemId: "providers",
    providerId: PROVIDER_ID_B,
  });
  assert.equal(await second, true);
  workspaceLate.resolve(workspaceList(PROVIDER_ID));
  assert.equal(await first, false);
  assert.equal(workspace.snapshot().providerId, PROVIDER_ID_B);
  assert.equal(workspace.snapshot().response.compatibility.provider_id, PROVIDER_ID_B);
});

test("restored error-maps navigation uses the Workspace override contract without provider.read", async () => {
  const filters = [];
  const controller = createWorkspaceProviderEngineeringController({
    api: {
      workspaceSession: async () => workspaceSession(),
      providerErrorOverrides: async (input) => {
        filters.push(input.filters);
        return workspaceList(PROVIDER_ID, { compatibility: null });
      },
    },
  });
  assert.equal(await controller.open({
    authContext: workspaceAuthContext([
      PERMISSIONS.WORKSPACE_PROVIDER_ERROR_OVERRIDE_READ,
      PERMISSIONS.WORKSPACE_PROVIDER_ERROR_OVERRIDE_MANAGE,
    ]),
    activeNavigationItemId: "error-maps",
    providerId: "",
  }), true);
  assert.deepEqual(filters, [{ status: "", output_error_code: "" }]);
  const snapshot = controller.snapshot();
  assert.equal(snapshot.activeNavigationItemId, "error-maps");
  assert.equal(snapshot.canManage, true);
  const html = renderWorkspaceProviderErrorMaps(snapshot);
  assert.match(html, /data-workspace-provider-error-maps-root/u);
  assert.match(html, /高级异常处理/u);
  assert.match(html, /data-workspace-provider-override-create-form/u);
  assert.doesNotMatch(html, /data-error-map|\/error-maps/u);
});

test("failed intents reuse idempotency keys while step-up grants remain one-shot", async () => {
  const platformKeys = [];
  const platformGrants = [];
  let platformCommands = 0;
  let platformKeyCounter = 0;
  const platform = createPlatformProviderEngineeringController({
    newIdempotencyKey: () => `platform-intent-143-${++platformKeyCounter}-0000`,
    api: {
      providerAdapters: async () => platformAdapterList(),
      adapterContracts: async () => platformContractRuns(),
      createIdentityStepUpGrant: async () => ({ grant: `${++platformCommands}`.repeat(43) }),
      commandProviderAdapter: async ({ idempotencyKey, stepUpGrant }) => {
        platformKeys.push(idempotencyKey);
        platformGrants.push(stepUpGrant);
        if (platformKeys.length === 1) {
          throw new PlatformApiError({ status: 503, code: "platform.dependency_unavailable" });
        }
        return platformAdapterCommand();
      },
    },
  });
  platform.enter(platformContext([
    "platform.provider_adapter.read",
    "platform.error_catalog.read",
    "platform.provider_adapter.release",
  ]), [{ id: "provider-engineering" }]);
  await platform.loadAdapters();
  const adapterInput = {
    adapterIdentifier: ADAPTER_ID,
    command: { action: "release" },
    expectedRevision: 2,
  };
  assert.equal(await platform.applyAdapter(adapterInput, {
    method: "totp", verificationCode: "123456",
  }), false);
  assert.equal(await platform.applyAdapter(adapterInput, {
    method: "totp", verificationCode: "123456",
  }), true);
  assert.deepEqual(platformKeys, [platformKeys[0], platformKeys[0]]);
  assert.notEqual(platformGrants[0], platformGrants[1]);
  assert.equal(platformKeyCounter, 1);

  const workspaceKeys = [];
  const workspaceGrants = [];
  let workspaceCommands = 0;
  let workspaceKeyCounter = 0;
  const workspace = createWorkspaceProviderEngineeringController({
    newIdempotencyKey: () => `workspace-intent-143-${++workspaceKeyCounter}-0000`,
    api: {
      workspaceSession: async () => workspaceSession(),
      providerErrorOverrides: async () => workspaceList(PROVIDER_ID),
      createIdentityStepUpGrant: async () => ({ grant: `${++workspaceCommands + 4}`.repeat(43) }),
      commandProviderErrorOverride: async ({ idempotencyKey, stepUpGrant }) => {
        workspaceKeys.push(idempotencyKey);
        workspaceGrants.push(stepUpGrant);
        if (workspaceKeys.length === 1) {
          throw new AuthApiError({ status: 503, code: "workspace.dependency_unavailable" });
        }
        return workspaceCommand(PROVIDER_ID);
      },
    },
  });
  await workspace.open({
    authContext: workspaceAuthContext(),
    activeNavigationItemId: "providers",
    providerId: PROVIDER_ID,
  });
  const overrideCommand = {
    action: "disable",
    provider_error_override_id: OVERRIDE_ID,
    expected_version: 3,
  };
  assert.equal(await workspace.apply(overrideCommand, {
    method: "totp", verificationCode: "654321",
  }), false);
  assert.equal(await workspace.apply(overrideCommand, {
    method: "totp", verificationCode: "654321",
  }), true);
  assert.deepEqual(workspaceKeys, [workspaceKeys[0], workspaceKeys[0]]);
  assert.notEqual(workspaceGrants[0], workspaceGrants[1]);
  assert.equal(workspaceKeyCounter, 1);
  assert.doesNotMatch(
    `${JSON.stringify(platform.snapshot())}${JSON.stringify(workspace.snapshot())}`,
    /123456|654321|GGGGGG|platform-intent|workspace-intent/u,
  );
});

test("renderers escape dynamic data and preserve Platform versus Workspace disclosure boundaries", () => {
  const unsafeAdapter = adapter({ adapter_code: "bad<script>alert(1)</script>" });
  const platformHtml = renderPlatformProviderEngineering({
    phase: "ready",
    loading: [],
    filters: {},
    runFilters: {},
    adapters: platformAdapterList({ adapters: [unsafeAdapter] }),
    selectedAdapterId: ADAPTER_ID,
    runs: platformContractRuns(),
    selectedRunId: RUN_ID,
    cases: platformContractCases(),
    catalog: catalog(),
    adapterHistory: [],
    runHistory: [],
    caseHistory: [],
  }, new Set([
    "platform.provider_adapter.read",
    "platform.error_catalog.read",
  ]));
  assert.match(platformHtml, /bad&lt;script&gt;alert\(1\)&lt;\/script&gt;/u);
  assert.doesNotMatch(platformHtml, /<script>|source_path|build_path|runner_command|tenant_id|workspace_id|provider_key/iu);

  const workspaceHtml = renderWorkspaceProviderEngineering({
    ...createWorkspaceProviderEngineeringStore().snapshot(),
    providerId: PROVIDER_ID,
    canManage: true,
    phase: "ready",
    response: workspaceList(PROVIDER_ID),
  }, { provider_id: PROVIDER_ID, name: "Provider <safe>" });
  assert.match(workspaceHtml, /Provider &lt;safe&gt;/u);
  assert.doesNotMatch(
    workspaceHtml,
    /contract_case_id|attestation|source_revision|sbom|provenance|runner_version|platform_release_ci/iu,
  );
});

test("legacy enterprise routes, DOM, imports, and persistence hooks are absent from active runtime", async () => {
  const [entry, developerRenderer, requestLogRenderer, consoleService, client, ...providerFiles] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/request-logs/renderRequestLogPanel.js", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/console/service.py", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/providerEngineering.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform-provider-engineering/platformProviderEngineeringStore.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform-provider-engineering/platformProviderEngineeringController.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/workspace-provider-engineering/workspaceProviderEngineeringStore.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/workspace-provider-engineering/workspaceProviderEngineeringController.js", import.meta.url), "utf8"),
  ]);
  const activeRuntime = `${entry}\n${developerRenderer}\n${requestLogRenderer}`;
  assert.doesNotMatch(activeRuntime, /adapter-specs|adapter-contract-runs/u);
  assert.match(activeRuntime, /case "error-maps":[\s\S]*renderWorkspaceProviderErrorMaps/u);
  assert.match(consoleService, /WORKSPACE_NAVIGATION_EXCLUDED_IDS[\s\S]*adapter-specs[\s\S]*adapter-contract-runs/u);
  assert.doesNotMatch(entry, /lib\/(?:adapterSpecs|adapterContractRuns|errorMaps)|features\/(?:adapter-specs|adapter-contract-runs|error-maps)/u);
  assert.match(client, /\/platform\/provider-adapters/u);
  assert.match(client, /\/workspace\/provider-error-overrides/u);
  assert.doesNotMatch(`${client}\n${providerFiles.join("\n")}`, /localStorage|sessionStorage|console\.log/u);
});

function adapter(overrides = {}) {
  return {
    provider_adapter_release_id: ADAPTER_ID,
    adapter_code: "openai",
    release_version: "1.2.3",
    protocol_family: "openai_compatible",
    sdk_release_id: SDK_ID,
    artifact_sha256: "1".repeat(64),
    sbom_sha256: "2".repeat(64),
    provenance_sha256: "3".repeat(64),
    signature_sha256: "4".repeat(64),
    spec_manifest_version: "adapter-spec-v2",
    spec_manifest_sha256: "5".repeat(64),
    compatibility_sha256: "6".repeat(64),
    required_runtime_version: "python-3.12",
    required_api_version: "14.3.0",
    status: "released",
    revision: 2,
    release_generation: 1,
    is_head: true,
    readiness: "released",
    contract: { queued: 0, running: 0, passed: 1, failed: 0, qualifying_run_id: RUN_ID },
    actions: ["run_contract", "deprecate", "revoke"],
    created_at: NOW,
    updated_at: NOW,
    released_at: NOW,
    ...overrides,
  };
}

function platformAdapterList(overrides = {}) {
  return {
    adapters: [adapter()],
    next_cursor: "next.cursor",
    limit: 50,
    permission_fingerprint: "7".repeat(64),
    request_id: "req_provider_adapters",
    trace_id: "trc_provider_adapters",
    ...overrides,
  };
}

function contractRun(overrides = {}) {
  return {
    contract_run_id: RUN_ID,
    scope: "platform_release_ci",
    provider_adapter_release_id: ADAPTER_ID,
    adapter_code: "openai",
    schema_version: "contract-v2",
    runner_version: "runner-2.0.0",
    suite_kind: "release_ci",
    mode: "isolated_runner",
    source_revision_sha256: "8".repeat(64),
    artifact_sha256: "1".repeat(64),
    catalog_sha256: "9".repeat(64),
    status: "passed",
    coverage_complete: true,
    total_cases: 1,
    passed_cases: 1,
    failed_cases: 0,
    failure_codes: [],
    report_sha256: "a".repeat(64),
    attestation_sha256: "b".repeat(64),
    qualifies_release: true,
    legacy: false,
    generation: 1,
    started_at: NOW,
    completed_at: NOW,
    created_at: NOW,
    ...overrides,
  };
}

function contractCase(overrides = {}) {
  return {
    contract_case_id: CASE_ID,
    contract_run_id: RUN_ID,
    ordinal: 1,
    case_key_sha256: "c".repeat(64),
    case_kind: "request",
    variant: "canonical",
    provider_code: "openai",
    adapter_code: "openai",
    status: "passed",
    failure_code: null,
    duration_bucket: "lt_100ms",
    evidence_sha256: "d".repeat(64),
    created_at: NOW,
    ...overrides,
  };
}

function platformContractRuns(overrides = {}) {
  return {
    provider_adapter_release_id: ADAPTER_ID,
    view: "runs",
    runs: [contractRun()],
    cases: [],
    next_cursor: null,
    limit: 50,
    permission_fingerprint: "7".repeat(64),
    request_id: "req_contract_runs",
    trace_id: "trc_contract_runs",
    ...overrides,
  };
}

function platformContractCases(overrides = {}) {
  return {
    provider_adapter_release_id: ADAPTER_ID,
    view: "cases",
    runs: [],
    cases: [contractCase()],
    next_cursor: null,
    limit: 100,
    permission_fingerprint: "7".repeat(64),
    request_id: "req_contract_cases",
    trace_id: "trc_contract_cases",
    ...overrides,
  };
}

function platformAdapterCommand(overrides = {}) {
  return {
    adapter: adapter(),
    command_id: "provider-adapter-command-143",
    contract_run_id: RUN_ID,
    idempotent_replay: false,
    permission_fingerprint: "7".repeat(64),
    request_id: "req_adapter_command",
    trace_id: "trc_adapter_command",
    ...overrides,
  };
}

function catalog(overrides = {}) {
  return {
    error_catalog_release_id: CATALOG_ID,
    builtin_catalog_version: "builtin-v2",
    catalog_sha256: "e".repeat(64),
    signed_candidate_id: "signed-candidate-143",
    sdk_release_id: SDK_ID,
    artifact_sha256: "1".repeat(64),
    rule_count: 12,
    compatibility_sha256: "6".repeat(64),
    status: "candidate",
    revision: 2,
    release_generation: 0,
    is_head: false,
    actions: ["submit_review"],
    created_at: NOW,
    updated_at: NOW,
    released_at: null,
    ...overrides,
  };
}

function platformCatalogCommand(overrides = {}) {
  return {
    catalog: catalog(),
    command_id: "provider-catalog-command-143",
    idempotent_replay: false,
    permission_fingerprint: "7".repeat(64),
    request_id: "req_catalog_command",
    trace_id: "trc_catalog_command",
    ...overrides,
  };
}

function overrideInfo(providerId = PROVIDER_ID, overrides = {}) {
  return {
    provider_error_override_id: OVERRIDE_ID,
    provider_id: providerId,
    provider_code: "openai",
    provider_name: "OpenAI",
    priority: 500,
    http_statuses: [404],
    http_status_class: null,
    error_field: "error.code",
    field_operator: "equals",
    field_pattern: "model_not_found",
    output_error_code: "provider.model_not_found",
    status: "active",
    version: 3,
    revision: 3,
    pattern_sha256: "f".repeat(64),
    content_sha256: "0".repeat(64),
    updated_at: NOW,
    actions: ["update", "disable", "archive"],
    ...overrides,
  };
}

function compatibility(providerId = PROVIDER_ID) {
  return {
    provider_id: providerId,
    provider_code: "openai",
    provider_catalog_version: "catalog-v2",
    provider_adapter_release_id: ADAPTER_ID,
    builtin_error_catalog_release_id: CATALOG_ID,
    override_head_revision: 4,
    compatibility_status: "compatible",
    reason_codes: [],
    health_status: "healthy",
    projected_at: NOW,
  };
}

function workspaceHead() {
  return {
    revision: 4,
    content_sha256: "0".repeat(64),
    builtin_catalog_version: "builtin-v2",
    updated_at: NOW,
  };
}

function workspaceList(providerId = PROVIDER_ID, overrides = {}) {
  return {
    overrides: [overrideInfo(providerId)],
    head: workspaceHead(),
    compatibility: compatibility(providerId),
    next_cursor: null,
    limit: 50,
    permission_revision: 9,
    permission_fingerprint: "7".repeat(64),
    request_id: `req_workspace_${providerId}`,
    trace_id: `trc_workspace_${providerId}`,
    ...overrides,
  };
}

function workspaceCommand(providerId = PROVIDER_ID, overrides = {}) {
  return {
    override: overrideInfo(providerId, { status: "disabled", version: 4, revision: 4 }),
    head: { ...workspaceHead(), revision: 5 },
    compatibility: { ...compatibility(providerId), override_head_revision: 5 },
    command_id: "workspace-override-command-143",
    changed: true,
    idempotent_replay: false,
    permission_revision: 9,
    permission_fingerprint: "7".repeat(64),
    request_id: "req_workspace_command",
    trace_id: "trc_workspace_command",
    ...overrides,
  };
}

function platformContext(permissions = [
  "platform.provider_adapter.read",
  "platform.error_catalog.read",
]) {
  return {
    operator: {
      operator_id: OPERATOR_ID,
      revision: 7,
      roles: ["platform_provider_engineer"],
    },
    permissions,
    expires_at: "2026-09-01T12:00:00.000Z",
  };
}

function workspaceAuthContext(permissions = [
  PERMISSIONS.PROVIDER_READ,
  PERMISSIONS.WORKSPACE_PROVIDER_ERROR_OVERRIDE_READ,
  PERMISSIONS.WORKSPACE_PROVIDER_ERROR_OVERRIDE_MANAGE,
]) {
  return withPermissionSnapshot({
    user: {
      tenant_id: "tenant_prd143",
      user_id: "usr_prd143",
      email: "developer@example.invalid",
      role: "developer",
    },
    session: { session_id: "ses_prd143", session_revision: 3 },
  }, permissions, {
    permissionRevision: 8,
    authorizationFingerprint: "workspace-provider-engineering-fingerprint",
  });
}

function workspaceSession() {
  return {
    context: {
      workspace_id: WORKSPACE_ID,
      role: "developer",
      session_status: "active",
      expires_at: "2026-09-01T12:00:00.000Z",
    },
  };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "private, no-store",
      "X-Request-ID": payload.request_id ?? "req_prd143",
      "X-Trace-ID": payload.trace_id ?? "trc_prd143",
    },
  });
}

function installDocument(t) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      cookie: `__Host-metahub-platform-csrf=${PLATFORM_CSRF}; __Host-metahub-workspace-csrf=${WORKSPACE_CSRF}`,
    },
  });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, "document", descriptor);
    else delete globalThis.document;
  });
}

function installFetch(t, responder) {
  const previous = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return responder(String(url), options);
  };
  t.after(() => { globalThis.fetch = previous; });
  return calls;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}
