import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { renderPrivacyCenter } from "../../src/features/identity/renderPrivacyCenter.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  PrivacyApiError,
  cancelDataSubjectRequest,
  createDataSubjectRequest,
  downloadPrivacyExport,
  getPrivacyPreferences,
  issuePrivacyExportReceipt,
  listDataSubjectRequests,
  listPlatformPrivacyQueue,
  savePrivacyExport,
  updatePrivacyPreferences,
} from "../../src/lib/privacyCenter.js";

const CONTROL_URL = "http://control.local";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const REQUEST_ID = `dsr_${"2".repeat(32)}`;
const EXPORT_ID = `pex_${"3".repeat(32)}`;
const PREFERENCE_ID = `prf_${"4".repeat(32)}`;
const AUTHORIZATION_FINGERPRINT = "f".repeat(64);
const CONSENT_ID = `con_${"5".repeat(32)}`;
const GLOBAL_CSRF = "G".repeat(43);
const STEP_UP = "S".repeat(43);
const RECEIPT = "R".repeat(43);
const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_DOCUMENT = globalThis.document;

test.afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  if (ORIGINAL_DOCUMENT === undefined) delete globalThis.document;
  else globalThis.document = ORIGINAL_DOCUMENT;
});

test("PRD-134 strict clients bind Global CSRF, CAS, idempotency, and one-shot grants", async () => {
  globalThis.document = { cookie: `__Host-metahub-global-csrf=${GLOBAL_CSRF}` };
  const calls = installFetch((url, options) => {
    const path = new URL(String(url)).pathname;
    if (path === "/identity/privacy-preferences") return jsonResponse(preferenceResponse());
    if (path.endsWith("/export-receipts")) return jsonResponse(receiptResponse());
    if (path === "/platform/privacy/queue") return jsonResponse(platformQueue());
    if (path === "/identity/data-subject-requests" && options.method === "GET") {
      return jsonResponse(requestList());
    }
    return jsonResponse(requestResponse());
  });

  await getPrivacyPreferences({ controlApiUrl: CONTROL_URL });
  await updatePrivacyPreferences({
    controlApiUrl: CONTROL_URL,
    expectedRevision: 1,
    idempotencyKey: "privacy-preference-0001",
    analyticsEnabled: true,
    productUpdatesEnabled: false,
    modelImprovementEnabled: false,
    termsVersion: "privacy-2026-08",
    locale: "zh-cn",
  });
  await listDataSubjectRequests({ controlApiUrl: CONTROL_URL });
  await createDataSubjectRequest({
    controlApiUrl: CONTROL_URL,
    requestKind: "export",
    stepUpGrant: STEP_UP,
    idempotencyKey: "privacy-request-create-0001",
  });
  await cancelDataSubjectRequest({
    controlApiUrl: CONTROL_URL,
    dataSubjectRequestId: REQUEST_ID,
    expectedRevision: 1,
    stepUpGrant: STEP_UP,
    idempotencyKey: "privacy-request-cancel-0001",
  });
  await issuePrivacyExportReceipt({
    controlApiUrl: CONTROL_URL,
    dataSubjectRequestId: REQUEST_ID,
    expectedRevision: 2,
    idempotencyKey: "privacy-export-receipt-0001",
  });
  await listPlatformPrivacyQueue({ controlApiUrl: CONTROL_URL });

  assert.deepEqual(calls.map(({ url, options }) => [new URL(url).pathname, options.method]), [
    ["/identity/privacy-preferences", "GET"],
    ["/identity/privacy-preferences", "PATCH"],
    ["/identity/data-subject-requests", "GET"],
    ["/identity/data-subject-requests", "POST"],
    [`/identity/data-subject-requests/${REQUEST_ID}`, "DELETE"],
    [`/identity/data-subject-requests/${REQUEST_ID}/export-receipts`, "POST"],
    ["/platform/privacy/queue", "GET"],
  ]);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  for (const index of [1, 3, 4, 5]) {
    assert.equal(calls[index].options.headers["X-CSRF-Token"], GLOBAL_CSRF);
  }
  assert.equal(calls[1].options.headers["If-Match"], '"1"');
  assert.equal(calls[4].options.headers["If-Match"], '"1"');
  assert.equal(calls[5].options.headers["If-Match"], '"2"');
  assert.equal(calls[1].options.headers["Idempotency-Key"], "privacy-preference-0001");
  assert.equal(calls[3].options.headers["Idempotency-Key"], "privacy-request-create-0001");
  assert.equal(calls[4].options.headers["Idempotency-Key"], "privacy-request-cancel-0001");
  assert.equal(calls[5].options.headers["Idempotency-Key"], "privacy-export-receipt-0001");
  assert.deepEqual(JSON.parse(calls[3].options.body), {
    request_kind: "export", step_up_grant: STEP_UP, confirmed: true,
  });
  assert.deepEqual(JSON.parse(calls[4].options.body), {
    step_up_grant: STEP_UP, confirmed: true,
  });
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.headers.Authorization, undefined);
  }
});

test("PRD-134 clients reject response extensions and non-minimal platform projections", async () => {
  globalThis.fetch = async () => jsonResponse({
    ...preferenceResponse(),
    account_id: `gac_${"9".repeat(32)}`,
  });
  await assert.rejects(
    getPrivacyPreferences({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PrivacyApiError
      && error.code === "privacy.response_invalid" && error.status === 502,
  );

  globalThis.fetch = async () => jsonResponse({
    ...platformQueue(),
    items: [{ ...platformQueue().items[0], workspace_id: WORKSPACE_ID }],
  });
  await assert.rejects(
    listPlatformPrivacyQueue({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "privacy.response_invalid" && error.status === 502,
  );
});

test("PRD-134 export sends receipt only in a header, verifies digests, and revokes blob URL", async () => {
  const content = new TextEncoder().encode(JSON.stringify({ schema_version: 1, owners: {} }));
  const contentDigest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", content));
  const receiptDigest = new Uint8Array(await globalThis.crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(RECEIPT),
  ));
  const calls = installFetch((_url, _options) => new Response(content, {
    status: 200,
    headers: {
      "Cache-Control": "no-store, private",
      "Content-Type": "application/json",
      "Content-Disposition": 'attachment; filename="metahub-identity-export-prq_aaaaaaaaaaaaaaaa.json"',
      Digest: `sha-256=${Buffer.from(contentDigest).toString("base64")}`,
      "X-Artifact-Receipt-SHA256": Buffer.from(receiptDigest).toString("hex"),
      "X-Content-Type-Options": "nosniff",
    },
  }));
  const result = await downloadPrivacyExport({
    controlApiUrl: CONTROL_URL,
    identityExport: identityExport({ sizeBytes: content.byteLength }),
    receipt: RECEIPT,
  });
  assert.equal(calls.length, 1);
  assert.equal(new URL(calls[0].url).search, "");
  assert.equal(calls[0].options.headers["X-Privacy-Export-Receipt"], RECEIPT);
  assert.equal(calls[0].url.includes(RECEIPT), false);

  const events = [];
  savePrivacyExport(result, {
    URL: {
      createObjectURL() { events.push("create"); return "blob:privacy"; },
      revokeObjectURL(value) { events.push(`revoke:${value}`); },
    },
    document: {
      body: { append() { events.push("append"); } },
      createElement() {
        return { click() { events.push("click"); }, remove() { events.push("remove"); } };
      },
    },
  });
  assert.deepEqual(events, ["create", "append", "click", "remove", "revoke:blob:privacy"]);
});

test("PRD-134 Identity controller binds step-up to the current Workspace and clears input secrets", async () => {
  const root = createRoot();
  const seen = [];
  const controller = createIdentityPageController({
    root,
    channelFactory: () => null,
    newIdempotencyKey: () => "privacy-controller-command-0001",
    api: identityApi({
      stepUp: async (input) => {
        seen.push(["step-up", input.actionCode, input.resourceType, input.resourceId, input.code]);
        return { grant: STEP_UP };
      },
      createPrivacyRequest: async (input) => {
        seen.push(["create", input.requestKind, input.stepUpGrant, input.idempotencyKey]);
        return requestResponse();
      },
    }),
  });
  assert.equal(await controller.mount(), true);
  const input = {
    requestKind: "export", stepUpMethod: "totp", stepUpCode: "123456", confirmed: true,
  };
  assert.equal(await controller.createPrivacyRequest(input), true);
  assert.equal(input.stepUpCode, "");
  assert.deepEqual(seen, [
    ["step-up", "privacy.dsr.create", "privacy_request", WORKSPACE_ID, "123456"],
    ["create", "export", STEP_UP, "privacy-controller-command-0001"],
  ]);
  assert.doesNotMatch(root.innerHTML, /123456|SSSSSSSSSSSSSSSS/u);
  assert.match(root.innerHTML, /prq_aaaaaaaaaaaaaaaa/u);
  controller.destroy();
});

test("PRD-134 Identity download reuses the receipt command after a transient failure", async () => {
  const issued = [];
  let keyGeneration = 0;
  let attempts = 0;
  const controller = createIdentityPageController({
    root: createRoot(),
    channelFactory: () => null,
    newIdempotencyKey: () => `privacy-receipt-command-${String(++keyGeneration).padStart(4, "0")}`,
    api: identityApi({
      issuePrivacyExportReceipt: async (input) => {
        issued.push([input.expectedRevision, input.idempotencyKey]);
        attempts += 1;
        if (attempts === 1) {
          throw new PrivacyApiError({
            status: 0, code: "privacy.unavailable", requestId: "-",
          });
        }
        return receiptResponse();
      },
      downloadPrivacyExport: async () => ({ downloaded: true }),
    }),
  });
  assert.equal(await controller.mount(), true);
  assert.equal(await controller.downloadPrivacyExport(REQUEST_ID), false);
  assert.equal(await controller.downloadPrivacyExport(REQUEST_ID), true);
  assert.deepEqual(issued, [
    [2, "privacy-receipt-command-0001"],
    [2, "privacy-receipt-command-0001"],
  ]);
  controller.destroy();
});

test("PRD-134 renderers expose Identity controls and only minimal Platform queue facts", () => {
  const identityHtml = renderPrivacyCenter({
    workspaceContext: { workspace_id: WORKSPACE_ID },
    privacyVisible: true,
    privacyCanManage: true,
    privacyCanCreate: true,
    privacyCanCancel: true,
    privacyBusy: false,
    privacyPreferences: preferenceResponse(),
    privacyRequests: requestList(),
  });
  assert.match(identityHtml, /data-privacy-preference-form/u);
  assert.match(identityHtml, /data-privacy-request-form/u);
  assert.match(identityHtml, /data-identity-action="download-privacy-export"/u);
  assert.match(identityHtml, /分域进度/u);
  assert.doesNotMatch(identityHtml, /step_up_grant|X-Privacy-Export-Receipt/u);

  const root = createRoot();
  renderPlatformWorkspace(root, {
    loading: false,
    session: platformSession(),
    navigation: [{ id: "privacy", label: "Privacy", href: "/platform.html#privacy" }],
    view: "privacy",
    privacyQueue: platformQueue(),
    error: null,
    feedback: null,
    pendingAction: null,
  });
  assert.match(root.innerHTML, /prq_aaaaaaaaaaaaaaaa/u);
  assert.match(root.innerHTML, /分域处理进度/u);
  assert.doesNotMatch(root.innerHTML, /gac_|wsp_|owner@example|account_id|workspace_id/u);
});

test("PRD-134 source audit forbids browser storage and URL-carried receipts", async () => {
  const source = await readFile(new URL("../../src/lib/privacyCenter.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /localStorage|sessionStorage/u);
  assert.doesNotMatch(source, /searchParams.*receipt|receipt.*searchParams/u);
  assert.match(source, /"X-Privacy-Export-Receipt": secret/u);
  assert.match(source, /finally\s*\{[\s\S]*secret = ""/u);
});

function preferenceResponse() {
  return {
    preference: {
      privacy_preference_id: PREFERENCE_ID,
      workspace_id: WORKSPACE_ID,
      analytics_enabled: false,
      product_updates_enabled: false,
      model_improvement_enabled: false,
      terms_version: "privacy-2026-08",
      locale: "zh-cn",
      revision: 1,
      created_at: "2026-08-30T10:00:00Z",
      updated_at: "2026-08-30T10:00:00Z",
    },
    consent_records: [{
      consent_record_id: CONSENT_ID,
      purpose: "privacy_terms",
      terms_version: "privacy-2026-08",
      locale: "zh-cn",
      source_kind: "registration",
      decision: "granted",
      evidence_sha256: "a".repeat(64),
      decided_at: "2026-08-30T10:00:00Z",
    }],
    idempotent_replay: false,
    request_id: "req_privacy_preference",
    trace_id: "trc_privacy_preference",
  };
}

function requestResponse() {
  return {
    request: privacyRequest(),
    idempotent_replay: false,
    request_id: "req_privacy_request",
    trace_id: "trc_privacy_request",
  };
}

function requestList() {
  return {
    items: [privacyRequest()],
    request_id: "req_privacy_list",
    trace_id: "trc_privacy_list",
  };
}

function privacyRequest() {
  return {
    data_subject_request_id: REQUEST_ID,
    request_code: "prq_aaaaaaaaaaaaaaaa",
    workspace_id: WORKSPACE_ID,
    request_kind: "export",
    status: "ready",
    revision: 4,
    deadline_at: "2026-09-29T10:00:00Z",
    deadline_warning_at: "2026-09-26T10:00:00Z",
    warning_sent: false,
    received_at: "2026-08-30T10:00:00Z",
    updated_at: "2026-08-30T10:05:00Z",
    terminal_reason_code: null,
    terminal_at: null,
    owner_progress: ownerProgress(),
    identity_export: identityExport(),
    erasure_decisions: [],
    retention_exceptions: [],
  };
}

function identityExport({ sizeBytes = 128 } = {}) {
  return {
    identity_export_id: EXPORT_ID,
    schema_version: 1,
    size_bytes: sizeBytes,
    revision: 2,
    status: "ready",
    receipt_status: "none",
    ready_at: "2026-08-30T10:05:00Z",
    downloaded_at: null,
    expires_at: "2026-08-30T10:35:00Z",
  };
}

function ownerProgress() {
  return ["identity", "membership", "audit", "billing", "support", "edge", "data_governance"]
    .map((owner_domain) => ({
      owner_domain,
      status: "completed",
      outcome_code: "exported",
      checkpoint_code: "completed",
      attempt_count: 1,
      completed_at: "2026-08-30T10:05:00Z",
    }));
}

function receiptResponse() {
  return {
    identity_export_id: EXPORT_ID,
    receipt: RECEIPT,
    expires_at: "2026-08-30T10:10:00Z",
    request_id: "req_privacy_receipt",
    trace_id: "trc_privacy_receipt",
  };
}

function platformQueue() {
  return {
    items: [{
      request_code: "prq_aaaaaaaaaaaaaaaa",
      request_kind: "export",
      status: "ready",
      deadline_at: "2026-09-29T10:00:00Z",
      deadline_warning_at: "2026-09-26T10:00:00Z",
      warning_sent: false,
      domain_progress: ownerProgress(),
      legal_outcomes: [],
      received_at: "2026-08-30T10:00:00Z",
      updated_at: "2026-08-30T10:05:00Z",
    }],
    request_id: "req_platform_privacy",
    trace_id: "trc_platform_privacy",
  };
}

function identityApi(overrides = {}) {
  const permissionCodes = [
    "privacy.preference.read", "privacy.preference.manage", "privacy.dsr.read",
    "privacy.dsr.create", "privacy.dsr.cancel",
  ];
  return {
    me: async () => ({ account: identityAccount(), current_session: identitySession() }),
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
      permission_fingerprint: AUTHORIZATION_FINGERPRINT,
    }),
    privacyPreferences: async () => preferenceResponse(),
    privacyRequests: async () => requestList(),
    logout: async () => ({ logged_out: true }),
    ...overrides,
  };
}

function identityAccount() {
  return { email: "owner@example.test", display_name: "Owner", status: "active", mfa_configured: true };
}

function identitySession() {
  return {
    session_id: `gis_${"6".repeat(32)}`,
    device_family: "chrome",
    created_at: "2026-08-30T09:00:00Z",
    last_seen_at: "2026-08-30T10:00:00Z",
    expires_at: "2026-08-31T10:00:00Z",
    is_current: true,
    assurance_level: "mfa",
    auth_method: "totp",
    trusted_device_id: null,
    revoke_generation: 1,
  };
}

function workspaceChoice() {
  return { workspace_id: WORKSPACE_ID, kind: "organization", name: "Acme", slug: "acme", role: "owner" };
}

function workspaceSessionContext() {
  return {
    ...workspaceChoice(),
    session_status: "active",
    expires_at: "2026-08-31T10:00:00Z",
  };
}

function workspaceAuthorization(permissionCodes) {
  return {
    permission_codes: [...permissionCodes],
    permission_revision: 1,
    permission_fingerprint: AUTHORIZATION_FINGERPRINT,
    membership_revision: 1,
  };
}

function platformSession() {
  return {
    context: {
      operator_id: `pto_${"7".repeat(32)}`,
      display_name: "Privacy operator",
      roles: ["platform_privacy"],
      permissions: ["platform.privacy.read"],
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
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return responder(url, options);
  };
  return calls;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
