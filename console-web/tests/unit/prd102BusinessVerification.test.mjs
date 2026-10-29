// PRD-102 Console 合同覆盖凭据隔离、严格 DTO、权限裁剪与敏感表单清理。
import assert from "node:assert/strict";
import test from "node:test";

import { createIdentityPageController as createAccountController } from "../../src/features/identity/identityPageState.js";
const createIdentityPageController = (options) => createAccountController({ initialSection: "business", ...options });
import { renderIdentitySecurityPage as renderAccountPage } from "../../src/features/identity/renderIdentityPage.js";
const renderIdentitySecurityPage = (root, state) => renderAccountPage(root, { accountSection: "business", ...state });
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  getWorkspaceBusinessVerification,
  listPlatformBusinessVerifications,
  reviewPlatformBusinessVerification,
  submitWorkspaceBusinessVerification,
  updateWorkspaceBusinessBillingProfile,
} from "../../src/lib/businessVerification.js";

// 固定标识与 CSRF 值只用于验证请求合同，测试不得把真实凭据或对象引用写入快照。
const CONTROL_URL = "http://control.test";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const INTENT_ID = `bvi_${"2".repeat(32)}`;
const EVIDENCE_ID = `bve_${"3".repeat(32)}`;
const HASH = "a".repeat(64);
const AUTHORIZATION_FINGERPRINT = "f".repeat(64);
const NOW = "2026-08-23T00:00:00Z";
const WORKSPACE_CSRF = "w".repeat(43);
const PLATFORM_CSRF = "p".repeat(43);

test("Workspace verification writes bind Cookie, CSRF, CAS, and idempotency headers", async () => {
  const originalDocument = globalThis.document;
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.document = {
    cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}; __Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
  };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(String(url).endsWith("/billing-profile")
      ? billingResponse()
      : workspaceResponse());
  };
  try {
    await submitWorkspaceBusinessVerification({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      submission: submission(),
      idempotencyKey: "workspace-verification-0001",
    });
    await updateWorkspaceBusinessBillingProfile({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      billingProfile: billingInput(),
      revision: 0,
      idempotencyKey: "workspace-billing-profile-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }

  assert.deepEqual(calls.map((call) => [new URL(call.url).pathname, call.options.method]), [
    [`/workspaces/${WORKSPACE_ID}/business-verifications`, "POST"],
    [`/workspaces/${WORKSPACE_ID}/billing-profile`, "PATCH"],
  ]);
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
    assert.equal("Authorization" in call.options.headers, false);
  }
  assert.equal(calls[0].options.headers["Idempotency-Key"], "workspace-verification-0001");
  assert.equal(calls[0].options.headers["If-Match"], undefined);
  assert.equal(calls[1].options.headers["Idempotency-Key"], "workspace-billing-profile-0001");
  assert.equal(calls[1].options.headers["If-Match"], '"0"');
});

test("Platform verification uses only the Platform credential domain", async () => {
  const originalDocument = globalThis.document;
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.document = {
    cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}; __Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
  };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(String(url).endsWith("/review") ? reviewResponse() : platformResponse());
  };
  try {
    await listPlatformBusinessVerifications({ controlApiUrl: CONTROL_URL, status: "submitted" });
    await reviewPlatformBusinessVerification({
      controlApiUrl: CONTROL_URL,
      intentId: INTENT_ID,
      revision: 1,
      decision: "approve",
      reasonCode: "verified",
      idempotencyKey: "platform-verification-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }

  assert.equal(calls[0].url, `${CONTROL_URL}/platform/business-verifications?limit=20&offset=0&status=submitted`);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.notEqual(calls[1].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[1].options.headers["If-Match"], '"1"');
  assert.equal(calls[1].options.headers["Idempotency-Key"], "platform-verification-0001");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    decision: "approve",
    reason_code: "verified",
    confirmed: true,
  });
  assert.equal(calls.every((call) => call.options.credentials === "include"), true);
  assert.equal(calls.some((call) => "Authorization" in call.options.headers), false);
});

test("verification clients reject unexpected or owner-domain response fields", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => jsonResponse({ ...workspaceResponse(), ciphertext: "private" });
    await assert.rejects(
      getWorkspaceBusinessVerification({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID }),
      (error) => error.code === "business_verification.response_invalid",
    );

    const unsafe = platformResponse();
    unsafe.cases[0].workspace_id = WORKSPACE_ID;
    globalThis.fetch = async () => jsonResponse(unsafe);
    await assert.rejects(
      listPlatformBusinessVerifications({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "business_verification.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Workspace and Platform renderers prune controls by live role and permission", () => {
  const owner = { innerHTML: "" };
  renderIdentitySecurityPage(owner, identityState("organization", "owner"));
  assert.match(owner.innerHTML, /data-business-verification-form/u);
  assert.match(owner.innerHTML, /data-business-billing-form/u);

  for (const [kind, role] of [["organization", "developer"], ["personal", "owner"]]) {
    const restricted = { innerHTML: "" };
    renderIdentitySecurityPage(restricted, identityState(kind, role));
    assert.doesNotMatch(restricted.innerHTML, /data-business-(?:verification|billing)-form/u);
  }

  const reviewer = { innerHTML: "" };
  renderPlatformWorkspace(reviewer, platformState([
    "platform.verification.read",
    "platform.verification.review",
  ]));
  assert.match(reviewer.innerHTML, new RegExp(EVIDENCE_ID, "u"));
  assert.match(reviewer.innerHTML, /verification-approve|verification-evidence-token/u);
  assert.doesNotMatch(reviewer.innerHTML, /private-object-reference|workspace_id|legal_name/iu);

  const readonly = { innerHTML: "" };
  renderPlatformWorkspace(readonly, platformState(["platform.verification.read"]));
  assert.match(readonly.innerHTML, /营业执照 · application\/pdf/u);
  assert.doesNotMatch(readonly.innerHTML, new RegExp(EVIDENCE_ID, "u"));
  assert.doesNotMatch(readonly.innerHTML, /verification-approve|verification-evidence-token/u);
});

test("business forms clear DOM and in-memory secrets without Storage access", async () => {
  const originalFormData = globalThis.FormData;
  const storageDescriptors = new Map(
    ["localStorage", "sessionStorage"].map((name) => [
      name,
      Object.getOwnPropertyDescriptor(globalThis, name),
    ]),
  );
  for (const name of storageDescriptors.keys()) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      get() { throw new Error(`${name} must not be accessed`); },
    });
  }

  const inputs = [
    { type: "text", value: "Sensitive legal name" },
    { type: "text", value: "Sensitive registration number" },
    { type: "checkbox", checked: true },
    { type: "hidden", value: "1" },
  ];
  const form = {
    matches: (selector) => selector === "[data-business-verification-form]",
    querySelectorAll: () => inputs,
  };
  const values = new Map([
    ["countryCode", "CN"],
    ["entityType", "company"],
    ["legalName", "Sensitive legal name"],
    ["registrationNumber", "Sensitive registration number"],
    ["evidenceType", "business_license"],
    ["objectReference", "obj_controlled-reference"],
    ["contentSha256", HASH],
    ["mimeType", "application/pdf"],
    ["sizeBytes", "1024"],
    ["kmsKeyVersion", "1"],
    ["scanAttestation", HASH],
    ["confirmed", "on"],
  ]);
  /** 模拟浏览器 FormData 读取并确认控制器只消费当前提交表单。 */
  globalThis.FormData = class {
    constructor(received) { assert.equal(received, form); }
    get(name) { return values.get(name) ?? null; }
  };

  let submitted = null;
  const root = createRoot();
  const controller = createIdentityPageController({
    root,
    newIdempotencyKey: () => "workspace-verification-form-0001",
    api: workingIdentityApi({
      submitBusinessVerification: async ({ submission: value }) => {
        submitted = value;
        return workspaceResponse();
      },
    }),
  });
  try {
    assert.equal(await controller.mount(), true);
    await root.listeners.submit({ target: form, preventDefault() {} });
    assert.deepEqual(inputs.map((input) => input.type === "checkbox" ? input.checked : input.value), [
      "", "", false, "1",
    ]);
    assert.equal(submitted.legal_name, "");
    assert.equal(submitted.registration_number, "");
    assert.equal(submitted.evidence[0].object_reference, "");
    assert.equal(submitted.evidence[0].scan_attestation, "");
  } finally {
    controller.destroy();
    globalThis.FormData = originalFormData;
    for (const [name, descriptor] of storageDescriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
});

/** 构造包含短生命周期主体和证据明文的合法 Workspace 提交输入。 */
function submission() {
  return {
    country_code: "CN",
    entity_type: "company",
    legal_name: "Example Legal Entity",
    registration_number: "REGISTRATION-001",
    evidence: [{
      evidence_type: "business_license",
      object_reference: "obj_controlled-reference",
      content_sha256: HASH,
      mime_type: "application/pdf",
      size_bytes: 1024,
      kms_key_version: 1,
      malware_status: "passed",
      dlp_status: "passed",
      scan_attestation: HASH,
    }],
    reason_code: "initial_submission",
    confirmed: true,
  };
}

/** 构造完整开票输入，用于验证提交后内存和表单值会被清空。 */
function billingInput() {
  return {
    invoice_title: "Example Legal Entity",
    tax_number: "TAX-001",
    address: "Example address",
    phone: "010-12345678",
    bank_name: "Example Bank",
    bank_account: "ACCOUNT-001",
    confirmed: true,
  };
}

/** 构造只含掩码和配置状态的 Workspace 企业认证响应。 */
function workspaceResponse() {
  return {
    workspace_id: WORKSPACE_ID,
    legal_entity: {
      country_code: "CN",
      entity_type: "company",
      legal_name_configured: true,
      registration_number_masked: "****N001",
      revision: 1,
      updated_at: NOW,
    },
    verification: null,
    contract: null,
    billing_profile: null,
    idempotent_replay: false,
    request_id: "req-verification",
    trace_id: "trc-verification",
  };
}

/** 构造带 CAS revision 的最小开票资料安全响应。 */
function billingResponse() {
  return {
    workspace_id: WORKSPACE_ID,
    billing_profile: billingProjection(),
    idempotent_replay: false,
    request_id: "req-billing-profile",
    trace_id: "trc-billing-profile",
  };
}

/** 构造 Platform 审核队列响应，案件不得包含 Workspace owner 字段。 */
function platformResponse() {
  return {
    cases: [platformCase()],
    limit: 20,
    offset: 0,
    total: 1,
    request_id: "req-platform-verification",
    trace_id: "trc-platform-verification",
  };
}

/** 构造审批完成响应，合同只保留不可变版本引用和有效期。 */
function reviewResponse() {
  return {
    case: { ...platformCase(), status: "approved", reason_code: "verified", revision: 2 },
    contract: {
      contract_profile_id: `ctp_${"4".repeat(32)}`,
      signing_status: "verified",
      immutable_version_reference: "contract-version-0001",
      effective_at: NOW,
      expires_at: null,
    },
    idempotent_replay: false,
    request_id: "req-platform-review",
    trace_id: "trc-platform-review",
  };
}

/** 构造单个审核案件，并显式移除测试夹具中的私有对象引用。 */
function platformCase() {
  return {
    business_verification_intent_id: INTENT_ID,
    country_code: "CN",
    entity_type: "company",
    status: "submitted",
    reason_code: "initial_submission",
    profile_revision: 1,
    profile_sha256: HASH,
    evidence_manifest_sha256: HASH,
    evidence: { evidence_count: 1, malware_status: "passed", dlp_status: "passed" },
    evidence_items: [{
      verification_evidence_id: EVIDENCE_ID,
      evidence_type: "business_license",
      mime_type: "application/pdf",
      size_bytes: 1024,
      malware_status: "passed",
      dlp_status: "passed",
      object_reference: "private-object-reference",
    }].map(({ object_reference: _private, ...safe }) => safe),
    revision: 1,
    submitted_at: NOW,
    expires_at: "2026-09-22T00:00:00Z",
  };
}

/** 构造开票字段的配置状态与末四位掩码投影。 */
function billingProjection() {
  return {
    invoice_title_configured: true,
    tax_number_masked: "****X001",
    address_configured: true,
    phone_configured: true,
    bank_name_configured: true,
    bank_account_masked: "****T001",
    verification_status: "unverified",
    revision: 1,
    updated_at: NOW,
  };
}

/** 组合不同 Workspace 类型和角色，验证前端权限裁剪而非后端授权替代。 */
function identityState(kind, role) {
  const state = identitySecurityState();
  state.workspaceContext = { workspace_id: WORKSPACE_ID, name: "Workspace", kind, role };
  state.businessVerification = { ...workspaceResponse(), billing_profile: billingProjection() };
  return state;
}

/** 构造账号安全页面所需的最小稳定状态，避免测试依赖真实会话。 */
function identitySecurityState() {
  const currentSession = {
    session_id: "private-session",
    is_current: true,
    device_family: "browser",
    created_at: NOW,
    last_seen_at: NOW,
    expires_at: "2026-08-24T00:00:00Z",
  };
  return {
    account: { display_name: "Finance", email: "finance@example.test", status: "active", mfa_configured: true },
    currentSession,
    sessions: [currentSession],
    workspaces: [],
    workspaceChoices: [],
    memberships: [],
    navigationItems: [],
    onboarding: null,
    saasCatalog: null,
    subscription: null,
    billingSummary: null,
    businessVerification: workspaceResponse(),
    businessVerificationBusy: false,
    platformAccess: false,
  };
}

/** 构造 Platform verification 视图状态，用权限集合驱动只读或审核控件。 */
function platformState(permissions) {
  return {
    session: { context: { permissions, operator: { display_name: "Reviewer", roles: ["platform_verification"] } } },
    navigation: [{ id: "verification", label: "Verification", href: "/platform.html#verification" }],
    view: "verification",
    verification: platformResponse(),
    verificationStatus: "",
    verificationOffset: 0,
    loading: false,
  };
}

/** 提供成功的 Identity API 桩，允许单项覆盖敏感提交行为。 */
function workingIdentityApi(overrides = {}) {
  const state = identityState("organization", "owner");
  return {
    me: async () => ({
      account: state.account,
      current_session: state.currentSession,
      request_id: "req-me",
      trace_id: "trc-me",
    }),
    sessions: async () => ({ sessions: state.sessions }),
    workspaces: async () => ({ workspaces: [] }),
    identityWorkspaces: async () => ({ workspaces: [state.workspaceContext] }),
    workspaceSession: async () => ({
      context: state.workspaceContext,
      authorization: workspaceAuthorization(),
    }),
    memberships: async () => ({ memberships: [] }),
    workspaceNavigation: async () => workspaceNavigationProjection(),
    businessVerification: async () => workspaceResponse(),
    ...overrides,
  };
}

function workspaceAuthorization() {
  return {
    permission_codes: [],
    permission_revision: 1,
    permission_fingerprint: AUTHORIZATION_FINGERPRINT,
    membership_revision: 1,
  };
}

function workspaceNavigationProjection() {
  return {
    groups: [],
    permission_revision: 1,
    permission_fingerprint: AUTHORIZATION_FINGERPRINT,
    membership_revision: 1,
  };
}

/** 构造最小 DOM 根节点并记录事件监听器，供状态机交互测试调用。 */
function createRoot() {
  const listeners = {};
  return {
    innerHTML: "",
    listeners,
    addEventListener(type, listener) { listeners[type] = listener; },
    contains() { return true; },
    querySelector() { return { focus() {} }; },
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}

/** 构造可解析的 Fetch JSON 响应，保持状态码与响应体显式可控。 */
function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    async json() { return structuredClone(payload); },
  };
}
