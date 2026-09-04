import assert from "node:assert/strict";
import test from "node:test";

import {
  createWorkspaceLegalHold,
  getWorkspaceDataGovernance,
  listPlatformResidencyEvidence,
  releaseWorkspaceLegalHold,
  updateWorkspaceDataGovernance,
} from "../../src/lib/dataGovernance.js";
import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { renderIdentitySecurityPage } from "../../src/features/identity/renderIdentityPage.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";

const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const HOLD_ID = `lgh_${"2".repeat(32)}`;
const HASH = "a".repeat(64);
const CLASSIFICATIONS = ["audit", "billing", "file", "identity", "prompt", "response"];
const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_DOCUMENT = globalThis.document;

test.afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  if (ORIGINAL_DOCUMENT === undefined) delete globalThis.document;
  else globalThis.document = ORIGINAL_DOCUMENT;
});

test("PRD-117 strict clients isolate credentials and send CAS and idempotency headers", async () => {
  globalThis.document = {
    cookie: "__Host-metahub-workspace-csrf=workspace-csrf; __Host-metahub-platform-csrf=platform-csrf",
  };
  const calls = installFetch((url, options) => {
    if (String(url).includes("/platform/data-residency/evidence")) {
      return jsonResponse(evidenceResponse());
    }
    if (options.method === "POST" || options.method === "DELETE") {
      return jsonResponse(legalHoldResponse());
    }
    return jsonResponse(governanceResponse({
      simulation: options.method === "PATCH" ? simulation() : null,
    }));
  });

  await getWorkspaceDataGovernance({ controlApiUrl: "http://localhost:8001", workspaceId: WORKSPACE_ID });
  await updateWorkspaceDataGovernance({
    controlApiUrl: "http://localhost:8001",
    workspaceId: WORKSPACE_ID,
    expectedRevision: 1,
    idempotencyKey: "governance-operation-0001",
    ...governanceDraft(),
    confirmed: false,
  });
  await createWorkspaceLegalHold({
    controlApiUrl: "http://localhost:8001",
    workspaceId: WORKSPACE_ID,
    idempotencyKey: "legal-hold-create-0001",
    classificationCode: "prompt",
    scopeKind: "classification",
    selectorSha256: null,
    reasonCode: "litigation",
    expiresAt: null,
    confirmed: true,
  });
  await releaseWorkspaceLegalHold({
    controlApiUrl: "http://localhost:8001",
    workspaceId: WORKSPACE_ID,
    holdId: HOLD_ID,
    expectedRevision: 1,
    idempotencyKey: "legal-hold-release-0001",
    reasonCode: "matter_closed",
    confirmed: true,
  });
  await listPlatformResidencyEvidence({
    controlApiUrl: "http://localhost:8001",
    workspaceId: WORKSPACE_ID,
    component: "gateway",
    outcome: "blocked",
    limit: 50,
  });

  assert.equal(calls.length, 5);
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[1].options.headers["If-Match"], '"1"');
  assert.equal(calls[1].options.headers["Idempotency-Key"], "governance-operation-0001");
  assert.deepEqual(Object.keys(JSON.parse(calls[1].options.body)).sort(), [
    "allowed_residency_groups", "confirmed", "cross_region_mode",
    "primary_residency_group", "reason_code", "retention_policies",
  ]);
  assert.equal(calls[2].options.headers["If-Match"], undefined);
  assert.equal(
    calls[2].url,
    `http://localhost:8001/workspaces/${WORKSPACE_ID}/legal-holds`,
  );
  assert.equal(JSON.parse(calls[2].options.body).selector_sha256, null);
  assert.equal(calls[3].options.method, "DELETE");
  assert.equal(
    calls[3].url,
    `http://localhost:8001/workspaces/${WORKSPACE_ID}/legal-holds/${HOLD_ID}`,
  );
  assert.equal(calls[3].options.headers["If-Match"], '"1"');
  assert.match(calls[4].url, /workspace_id=wsp_/u);
  assert.match(calls[4].url, /component=gateway/u);
  assert.match(calls[4].url, /outcome=blocked/u);
  assert.equal(calls[4].options.headers["X-CSRF-Token"], undefined);
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.headers.Authorization, undefined);
  }
});

test("PRD-117 clients reject response extension and invalid platform filters", async () => {
  globalThis.fetch = async () => jsonResponse({
    ...governanceResponse(),
    object_key: "must-not-cross-boundary",
  });
  await assert.rejects(
    getWorkspaceDataGovernance({ controlApiUrl: "http://localhost:8001", workspaceId: WORKSPACE_ID }),
    (error) => error.code === "data_governance.response_invalid" && error.status === 502,
  );
  await assert.rejects(
    async () => listPlatformResidencyEvidence({
      controlApiUrl: "http://localhost:8001",
      component: "provider_secret",
    }),
    (error) => error.code === "data_governance.request_invalid",
  );
});

test("PRD-117 Identity renderer exposes governance controls by exact permissions only", () => {
  const root = createRoot();
  const state = identityState({
    dataGovernanceCanManage: true,
    legalHoldVisible: true,
    legalHoldCanManage: true,
  });
  renderIdentitySecurityPage(root, state);

  assert.match(root.innerHTML, /驻留与保留/u);
  assert.match(root.innerHTML, /data-data-governance-policy-form/u);
  assert.match(root.innerHTML, /模拟策略/u);
  assert.match(root.innerHTML, /应用并发布/u);
  assert.match(root.innerHTML, /data-label="保留天数"/u);
  assert.match(root.innerHTML, /data-label="到期动作"/u);
  assert.match(root.innerHTML, /data-label="备份宽限"/u);
  assert.match(root.innerHTML, /name="confirmed" type="checkbox" required/u);
  assert.match(root.innerHTML, /data-legal-hold-create-form/u);
  assert.match(root.innerHTML, /最近删除终态/u);
  assert.doesNotMatch(root.innerHTML, new RegExp(`rgp_${"3".repeat(32)}`, "u"));
  assert.doesNotMatch(root.innerHTML, new RegExp(HOLD_ID, "u"));
  assert.doesNotMatch(root.innerHTML, /selector_sha256|object_key|prompt_body|response_body/u);
  assert.doesNotMatch(root.innerHTML, new RegExp(HASH, "u"));

  renderIdentitySecurityPage(root, identityState({
    dataGovernanceCanManage: false,
    legalHoldVisible: false,
    legalHoldCanManage: false,
  }));
  assert.match(root.innerHTML, /当前职责仅可查看驻留与保留策略/u);
  assert.doesNotMatch(root.innerHTML, /data-legal-hold-create-form|应用并发布/u);
});

test("PRD-117 Identity controller requires matching simulation before confirmed apply", async () => {
  const root = createRoot();
  const writes = [];
  let sequence = 0;
  const controller = createIdentityPageController({
    root,
    newIdempotencyKey: () => `governance-controller-${String(++sequence).padStart(4, "0")}`,
    api: identityApi({
      dataGovernance: async () => governanceResponse(),
      updateDataGovernance: async (input) => {
        writes.push({ ...input, signal: undefined });
        return governanceResponse({
          simulation: simulation(),
          applied: input.confirmed,
          revision: input.confirmed ? 2 : 1,
          generation: input.confirmed ? 2 : 1,
        });
      },
    }),
  });
  await controller.mount();
  const draft = governanceDraft();

  assert.equal(await controller.applyDataGovernance({ ...draft, confirmed: true }), false);
  assert.match(root.innerHTML, /请先重新模拟/u);
  assert.equal(await controller.simulateDataGovernance(draft), true);
  assert.equal(writes[0].confirmed, false);
  assert.match(root.innerHTML, /候选 generation/u);
  assert.equal(await controller.applyDataGovernance({ ...draft, confirmed: true }), true);
  assert.equal(writes[1].confirmed, true);
  assert.notEqual(writes[0].idempotencyKey, writes[1].idempotencyKey);
  assert.match(root.innerHTML, /Revision 2/u);
  controller.destroy();
});

test("PRD-117 Platform controller loads, filters, and paginates only evidence DTOs", async () => {
  const root = createRoot();
  const reads = [];
  const controller = createPlatformPageController({
    root,
    api: {
      session: async () => platformSession(),
      navigation: async () => ({
        items: [{ id: "data-residency", label: "数据驻留", href: "/platform.html#data-residency" }],
      }),
      residencyEvidence: async (input) => {
        reads.push({ ...input, signal: undefined });
        return evidenceResponse({ nextCursor: reads.length === 1 ? "cursor-next" : null });
      },
    },
  });
  assert.equal(await controller.mount(), true);
  assert.equal(controller.selectView("data-residency"), undefined);
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(root.innerHTML, /数据驻留/u);
  assert.match(root.innerHTML, /Policy-bound observations/u);
  assert.match(root.innerHTML, /Generation 1/u);
  assert.doesNotMatch(root.innerHTML, /object_key|prompt_body|response_body|provider_key/u);
  assert.doesNotMatch(root.innerHTML, new RegExp(HASH, "u"));
  assert.equal(await controller.filterResidencyEvidence({
    workspaceId: WORKSPACE_ID,
    component: "gateway",
    outcome: "blocked",
  }), true);
  assert.equal(reads[1].workspaceId, WORKSPACE_ID);
  assert.equal(reads[1].component, "gateway");
  assert.equal(reads[1].outcome, "blocked");
  controller.destroy();
});

function governanceDraft() {
  return {
    allowedResidencyGroups: ["cn-main"],
    primaryResidencyGroup: "cn-main",
    crossRegionMode: "same_group",
    reasonCode: "compliance_update",
    retentionPolicies: CLASSIFICATIONS.map((classificationCode) => ({
      classificationCode,
      retentionDays: classificationCode === "audit" ? 365 : 30,
      deletionMode: classificationCode === "identity" ? "anonymize" : "hard_delete",
      backupGraceDays: 7,
    })),
  };
}

function governanceResponse({ simulation: candidate = null, applied = false, revision = 1, generation = 1 } = {}) {
  return {
    workspace_id: WORKSPACE_ID,
    policy: {
      residency_policy_id: `rgp_${"3".repeat(32)}`,
      allowed_residency_groups: ["cn-main"],
      primary_residency_group: "cn-main",
      cross_region_mode: "same_group",
      revision,
      generation,
      policy_sha256: HASH,
      valid_until: "2026-09-26T00:00:00Z",
      updated_at: "2026-08-26T00:00:00Z",
    },
    classifications: CLASSIFICATIONS.map((code, index) => ({
      data_classification_id: `dcl_${String(index + 1).repeat(32)}`,
      code,
      sensitivity_level: new Set(["prompt", "response", "identity"]).has(code)
        ? "restricted" : "confidential",
      allowed_purposes: ["service_operation"],
      default_retention_days: code === "audit" ? 365 : 30,
    })),
    retention_policies: CLASSIFICATIONS.map((classificationCode, index) => ({
      retention_policy_id: `rtp_${String(index + 1).repeat(32)}`,
      classification_code: classificationCode,
      retention_days: classificationCode === "audit" ? 365 : 30,
      deletion_mode: classificationCode === "identity" ? "anonymize" : "hard_delete",
      backup_grace_days: 7,
      revision,
      generation,
    })),
    legal_holds: [legalHold()],
    deletion_schedules: [{
      deletion_schedule_id: `dgs_${"4".repeat(32)}`,
      classification_code: "response",
      due_at: "2026-08-25T00:00:00Z",
      status: "blocked",
      policy_generation: generation,
      hold_generation: 1,
      checkpoint_code: "hold-check",
      deleted_count: 0,
      retained_count: 8,
      safe_outcome_code: "data_governance.hold_active",
      revision: 2,
      completed_at: "2026-08-26T00:00:00Z",
      updated_at: "2026-08-26T00:00:00Z",
    }],
    fence: {
      policy_generation: generation,
      hold_generation: 1,
      deletion_generation: 2,
      revision: 4,
    },
    simulation: candidate,
    applied,
    idempotent_replay: false,
    request_id: "req_governance",
    trace_id: "trc_governance",
  };
}

function simulation() {
  return {
    candidate_sha256: "b".repeat(64),
    next_generation: 2,
    placement_compliant: true,
    migration_required_components: [],
  };
}

function legalHold() {
  return {
    legal_hold_id: HOLD_ID,
    classification_code: "prompt",
    scope_kind: "classification",
    selector_configured: false,
    reason_code: "litigation",
    status: "active",
    revision: 1,
    created_hold_generation: 1,
    expires_at: null,
    released_at: null,
    created_at: "2026-08-26T00:00:00Z",
    updated_at: "2026-08-26T00:00:00Z",
  };
}

function legalHoldResponse() {
  return {
    hold: legalHold(),
    fence: {
      policy_generation: 1,
      hold_generation: 1,
      deletion_generation: 2,
      revision: 4,
    },
    idempotent_replay: false,
    request_id: "req_hold",
    trace_id: "trc_hold",
  };
}

function evidenceResponse({ nextCursor = null } = {}) {
  return {
    items: [{
      residency_evidence_id: `rse_${"5".repeat(32)}`,
      workspace_id: WORKSPACE_ID,
      policy_generation: 1,
      component_code: "gateway",
      region_code: "cn-north-1",
      storage_class: "routing",
      outcome: "blocked",
      violation_code: "generation_stale",
      observed_watermark: 42,
      manifest_sha256: HASH,
      observed_at: "2026-08-26T00:00:00Z",
    }],
    next_cursor: nextCursor,
    request_id: "req_evidence",
    trace_id: "trc_evidence",
  };
}

function identityState(overrides = {}) {
  return {
    account: {
      email: "owner@example.com",
      display_name: "Owner",
      status: "active",
      mfa_configured: true,
    },
    currentSession: identitySession(),
    sessions: [identitySession()],
    workspaces: [],
    workspaceChoices: [],
    workspaceContext: workspaceContext(),
    memberships: [],
    navigationItems: [],
    platformAccess: false,
    dataGovernance: governanceResponse(),
    dataGovernanceVisible: true,
    dataGovernanceBusy: false,
    dataGovernanceError: null,
    dataGovernanceDraft: null,
    ...overrides,
  };
}

function identityApi(overrides = {}) {
  const permissionCodes = [
    "data_governance.read", "data_governance.manage", "legal_hold.read", "legal_hold.manage",
  ];
  return {
    me: async () => ({
      account: identityState().account,
      current_session: identitySession(),
    }),
    sessions: async () => ({ sessions: [identitySession()] }),
    identityWorkspaces: async () => ({ workspaces: [workspaceChoice()] }),
    workspaceSession: async () => ({
      context: workspaceSessionContext(),
      authorization: workspaceAuthorization(permissionCodes),
    }),
    workspaceNavigation: async () => ({
      groups: [],
      membership_revision: 1,
      permission_revision: 1,
      permission_fingerprint: HASH,
    }),
    logout: async () => ({ logged_out: true }),
    ...overrides,
  };
}

function identitySession() {
  return {
    session_id: `gis_${"6".repeat(32)}`,
    device_family: "chrome",
    created_at: "2026-08-25T00:00:00Z",
    last_seen_at: "2026-08-26T00:00:00Z",
    expires_at: "2026-08-27T00:00:00Z",
    is_current: true,
  };
}

function workspaceContext() {
  return {
    workspace_id: WORKSPACE_ID,
    kind: "organization",
    name: "Acme",
    slug: "acme",
    role: "owner",
    permission_codes: [
      "data_governance.read", "data_governance.manage", "legal_hold.read", "legal_hold.manage",
    ],
  };
}

function workspaceChoice() {
  return {
    workspace_id: WORKSPACE_ID,
    kind: "organization",
    name: "Acme",
    slug: "acme",
    role: "owner",
  };
}

function workspaceSessionContext() {
  return {
    workspace_id: WORKSPACE_ID,
    kind: "organization",
    name: "Acme",
    slug: "acme",
    role: "owner",
    session_status: "active",
    expires_at: "2026-08-27T00:00:00Z",
  };
}

function workspaceAuthorization(permissionCodes) {
  return {
    permission_codes: [...permissionCodes],
    permission_revision: 1,
    permission_fingerprint: HASH,
    membership_revision: 1,
  };
}

function platformSession() {
  return {
    context: {
      operator: { display_name: "Compliance", roles: ["platform_compliance"] },
      permissions: ["platform.data_residency.read"],
    },
  };
}

function createRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    contains() { return true; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}

function installFetch(responder) {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return responder(url, options);
  };
  return calls;
}

function jsonResponse(payload, { status = 200 } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    async json() { return structuredClone(payload); },
  };
}
