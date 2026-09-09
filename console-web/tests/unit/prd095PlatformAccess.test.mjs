import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  beginIdentityTotpEnrollment,
  getIdentityPlatformAccess,
  verifyIdentityTotpEnrollment,
} from "../../src/lib/identity.js";
import {
  PlatformApiError,
  approveSupportGrant,
  createBreakGlassSession,
  createPlatformOperator,
  createSupportGrant,
  createSupportSession,
  getPlatformNavigation,
  getPlatformSession,
  getPlatformRegistrationPolicy,
  updatePlatformRegistrationPolicy,
  getSupportDiagnostics,
  listPlatformAudit,
  listPlatformOperators,
  listSupportGrants,
  openPlatformSession,
  revokeSupportGrant,
  updatePlatformOperator,
} from "../../src/lib/platform.js";

const CONTROL_URL = "http://control.local";
const ACCOUNT_ID = `gac_${"1".repeat(32)}`;
const OPERATOR_ID = `pto_${"2".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"3".repeat(32)}`;
const GRANT_ID = `sgr_${"4".repeat(32)}`;
const GLOBAL_CSRF = "G".repeat(43);
const PLATFORM_CSRF = "P".repeat(43);
const MFA_FACTOR_ID = `mfa_${"5".repeat(32)}`;
const MFA_SECRET = "A".repeat(32);

test("registration switches belong to platform operator management and use platform CSRF", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${PLATFORM_CSRF}` };
  const policy = { email_verification: false, human_verification: true, revision: 1,
    environment: "dev", email_delivery_available: false,
    request_id: "req_registration_policy", trace_id: "trc_registration_policy" };
  const calls = installFetch(() => jsonResponse(policy));
  try {
    await getPlatformRegistrationPolicy({ controlApiUrl: CONTROL_URL });
    await updatePlatformRegistrationPolicy({ controlApiUrl: CONTROL_URL,
      policy: { email_verification: false, human_verification: false, revision: 1 } });
    assert.equal(new URL(calls[1].url).pathname, "/platform/registration-policy");
    assert.equal(calls[1].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
    assert.equal(calls[1].options.method, "PUT");
    const root = createRoot();
    const state = { ...pageState(), view: "operators", registrationPolicy: policy };
    state.session.context.permissions = ["platform.operator.manage"];
    renderPlatformWorkspace(root, state);
    assert.match(root.innerHTML, /SaaS 平台/u);
    assert.match(root.innerHTML, /data-platform-registration-policy-form/u);
    state.session.context.permissions = [];
    renderPlatformWorkspace(root, state);
    assert.doesNotMatch(root.innerHTML, /data-platform-registration-policy-form/u);
  } finally { globalThis.document = originalDocument; }
});

test("PRD-095 identity clients validate TOTP enrollment and platform entry projection", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-global-csrf=${GLOBAL_CSRF}` };
  const calls = installFetch((url) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/enrollments")) return jsonResponse(mfaEnrollmentPayload());
    if (path.endsWith("/verifications")) return jsonResponse(mfaVerificationPayload());
    return jsonResponse({ available: true, request_id: "req_access", trace_id: "trc_access" });
  });
  try {
    const enrollment = await beginIdentityTotpEnrollment({
      controlApiUrl: CONTROL_URL,
      currentPassword: "CurrentPassword",
    });
    assert.equal(enrollment.secret, MFA_SECRET);
    await verifyIdentityTotpEnrollment({
      controlApiUrl: CONTROL_URL,
      factorId: MFA_FACTOR_ID,
      code: "123456",
    });
    assert.equal(
      (await getIdentityPlatformAccess({ controlApiUrl: CONTROL_URL })).available,
      true,
    );
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(calls.map(({ url }) => new URL(url).pathname), [
    "/identity/mfa/totp/enrollments",
    "/identity/mfa/totp/verifications",
    "/identity/platform-access",
  ]);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], GLOBAL_CSRF);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], GLOBAL_CSRF);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], undefined);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    current_password: "CurrentPassword",
  });
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    factor_id: MFA_FACTOR_ID,
    code: "123456",
  });

  globalThis.document = { cookie: `__Host-metahub-global-csrf=${GLOBAL_CSRF}` };
  installFetch(() => jsonResponse({
    ...mfaEnrollmentPayload(),
    operator_role: "must-not-reach-ui",
  }));
  try {
    await assert.rejects(
      () => beginIdentityTotpEnrollment({
        controlApiUrl: CONTROL_URL,
        currentPassword: "CurrentPassword",
      }),
      (error) => error.code === "identity.response_invalid",
    );
  } finally {
    globalThis.document = originalDocument;
  }
});

test("PRD-095 identity controller removes MFA secrets and shows only projected platform entry", async () => {
  const sensitiveInputs = [{ value: "CurrentPassword" }, { value: "123456" }];
  const root = createRoot(sensitiveInputs);
  let mfaActive = false;
  const seen = [];
  const controller = createIdentityPageController({
    root,
    channelFactory: () => null,
    api: {
      me: async () => identitySessionPayload(mfaActive),
      sessions: async () => identitySessionListPayload(),
      platformAccess: async () => ({
        available: mfaActive,
        request_id: "req_access",
        trace_id: "trc_access",
      }),
      beginMfa: async (input) => {
        seen.push(["begin", input.currentPassword]);
        return mfaEnrollmentPayload();
      },
      verifyMfa: async (input) => {
        seen.push(["verify", input.factorId, input.code]);
        mfaActive = true;
        return mfaVerificationPayload();
      },
    },
  });

  assert.equal(await controller.mount(), true);
  assert.doesNotMatch(root.innerHTML, /\/platform\.html/u);
  assert.equal(await controller.beginMfaEnrollment({ currentPassword: "CurrentPassword" }), true);
  assert.deepEqual(sensitiveInputs.map((input) => input.value), ["", ""]);
  assert.match(root.innerHTML, new RegExp(MFA_SECRET, "u"));
  sensitiveInputs[1].value = "123456";
  assert.equal(await controller.verifyMfaEnrollment({ code: "123456" }), true);
  assert.equal(sensitiveInputs[1].value, "");
  assert.doesNotMatch(root.innerHTML, new RegExp(`${MFA_SECRET}|123456|CurrentPassword`, "u"));
  assert.match(root.innerHTML, /href="\/platform\.html"/u);
  assert.deepEqual(seen, [
    ["begin", "CurrentPassword"],
    ["verify", MFA_FACTOR_ID, "123456"],
  ]);
  controller.destroy();
});

test("PRD-095 clients keep Global, Platform, and Support credential domains exact", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = {
    cookie: [
      `__Host-metahub-global-csrf=${GLOBAL_CSRF}`,
      `__Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
      `__Host-metahub-workspace-csrf=${"W".repeat(43)}`,
      "__Host-unrelated-cookie=inert",
    ].join("; "),
  };
  const calls = installFetch(() => jsonResponse({ request_id: "req_ok", trace_id: "trc_ok" }));
  try {
    await openPlatformSession({ controlApiUrl: CONTROL_URL, totpCode: "123456" });
    await getPlatformSession({ controlApiUrl: CONTROL_URL });
    await getPlatformNavigation({ controlApiUrl: CONTROL_URL });
    await listPlatformOperators({ controlApiUrl: CONTROL_URL });
    await createPlatformOperator({
      controlApiUrl: CONTROL_URL,
      accountId: ACCOUNT_ID,
      roles: ["platform_support"],
    });
    await updatePlatformOperator({
      controlApiUrl: CONTROL_URL,
      operatorId: OPERATOR_ID,
      revision: 3,
      status: "disabled",
      roles: ["platform_support"],
    });
    await listSupportGrants({ controlApiUrl: CONTROL_URL });
    await createSupportGrant({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      reasonCode: "workspace_diagnostics",
      durationMinutes: 15,
    });
    await approveSupportGrant({ controlApiUrl: CONTROL_URL, grantId: GRANT_ID });
    await revokeSupportGrant({
      controlApiUrl: CONTROL_URL,
      grantId: GRANT_ID,
      reasonCode: "operator_revoked",
    });
    await createSupportSession({ controlApiUrl: CONTROL_URL, grantId: GRANT_ID });
    await getSupportDiagnostics({ controlApiUrl: CONTROL_URL });
    await createBreakGlassSession({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      reasonCode: "incident_containment",
      totpCode: "654321",
    });
    await listPlatformAudit({ controlApiUrl: CONTROL_URL });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(
    calls.map(({ url, options }) => [new URL(url).pathname, options.method]),
    [
      ["/platform/sessions", "POST"],
      ["/platform/session", "GET"],
      ["/platform/navigation", "GET"],
      ["/platform/operators", "GET"],
      ["/platform/operators", "POST"],
      [`/platform/operators/${OPERATOR_ID}`, "PATCH"],
      ["/platform/support-grants", "GET"],
      ["/platform/support-grants", "POST"],
      [`/platform/support-grants/${GRANT_ID}/approve`, "POST"],
      [`/platform/support-grants/${GRANT_ID}/revoke`, "POST"],
      [`/platform/support-grants/${GRANT_ID}/sessions`, "POST"],
      ["/support/workspace/diagnostics", "GET"],
      ["/platform/break-glass-sessions", "POST"],
      ["/platform/audit-logs", "GET"],
    ],
  );
  assert.equal(calls[0].options.headers["X-CSRF-Token"], GLOBAL_CSRF);
  for (const index of [4, 5, 7, 8, 9, 10, 12]) {
    assert.equal(calls[index].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  }
  for (const index of [1, 2, 3, 6, 11, 13]) {
    assert.equal(calls[index].options.headers["X-CSRF-Token"], undefined);
  }
  assert.equal(calls[5].options.headers["If-Match"], '"3"');
  assert.deepEqual(JSON.parse(calls[0].options.body), { totp_code: "123456" });
  assert.deepEqual(JSON.parse(calls[12].options.body), {
    workspace_id: WORKSPACE_ID,
    reason_code: "incident_containment",
    totp_code: "654321",
    confirmed: true,
  });
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal("Authorization" in call.options.headers, false);
  }
});

test("PRD-095 clients reject ambiguous credentials and sensitive response fields", async () => {
  const originalDocument = globalThis.document;
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse({ request_id: "req_unused", trace_id: "trc_unused" });
  };
  try {
    globalThis.document = {
      cookie: `__Host-metahub-platform-csrf=${PLATFORM_CSRF}; __Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
    };
    await assert.rejects(
      () => createSupportGrant({
        controlApiUrl: CONTROL_URL,
        workspaceId: WORKSPACE_ID,
        reasonCode: "workspace_diagnostics",
        durationMinutes: 15,
      }),
      (error) => error.code === "platform.csrf_invalid",
    );
    assert.equal(fetchCount, 0);

    globalThis.document = { cookie: `__Host-metahub-platform-csrf=${PLATFORM_CSRF}` };
    installFetch(() => jsonResponse({
      entries: [],
      token: "must-not-reach-ui",
      request_id: "req_invalid",
      trace_id: "trc_invalid",
    }));
    await assert.rejects(
      () => listPlatformAudit({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "platform.response_invalid",
    );
  } finally {
    globalThis.document = originalDocument;
  }
});

test("PRD-095 controller uses backend permissions and scrubs sensitive inputs", async () => {
  const sensitiveInputs = [{ value: "123456" }, { value: "654321" }];
  const root = createRoot(sensitiveInputs);
  const seen = [];
  const controller = createPlatformPageController({
    root,
    api: workingApi({
      openSession: async (input) => {
        seen.push(["open", input.totpCode]);
        return sessionPayload();
      },
      breakGlass: async (input) => {
        seen.push(["break-glass", input.totpCode]);
        return { request_id: "req_break_glass", trace_id: "trc_break_glass" };
      },
    }),
  });

  assert.equal(await controller.mount(), true);
  assert.match(root.innerHTML, /class="platform-shell"/u);
  assert.match(root.innerHTML, /操作员/u);
  assert.match(root.innerHTML, /支持授权/u);
  assert.match(root.innerHTML, /Billing/u);
  assert.match(root.innerHTML, /Customer success/u);
  controller.selectView("workspaces");
  assert.match(root.innerHTML, /<h1>Workspace 目录<\/h1>/u);
  assert.match(root.innerHTML, /没有符合条件的 Workspace/u);
  controller.selectView("not-authorized");
  assert.match(root.innerHTML, /class="platform-shell"/u);
  assert.equal(await controller.openSession("123456"), true);
  assert.deepEqual(sensitiveInputs.map((input) => input.value), ["", ""]);
  sensitiveInputs[0].value = "654321";
  assert.equal(await controller.breakGlass({
    workspaceId: WORKSPACE_ID,
    reasonCode: "incident_containment",
    totpCode: "654321",
  }), true);
  assert.equal(sensitiveInputs[0].value, "");
  assert.deepEqual(seen, [["open", "123456"], ["break-glass", "654321"]]);
  assert.doesNotMatch(root.innerHTML, /123456|654321/u);
  controller.destroy();
  assert.equal(root.innerHTML, "");
});

test("PRD-095 controller commits only the newest refresh snapshot", async () => {
  let operatorsCall = 0;
  let releaseStale;
  const root = createRoot();
  const controller = createPlatformPageController({
    root,
    api: workingApi({
      operators: async () => {
        operatorsCall += 1;
        if (operatorsCall === 1) return operatorListPayload({ display_name: "Initial operator" });
        if (operatorsCall === 2) {
          return new Promise((resolve) => {
            releaseStale = () => resolve(operatorListPayload({ display_name: "Stale operator" }));
          });
        }
        return operatorListPayload({ display_name: "Current operator" });
      },
    }),
  });

  assert.equal(await controller.mount(), true);
  controller.selectView("operators");
  const staleRefresh = controller.refresh();
  for (let attempt = 0; attempt < 10 && !releaseStale; attempt += 1) {
    await Promise.resolve();
  }
  assert.equal(typeof releaseStale, "function");
  assert.equal(await controller.refresh(), true);
  releaseStale();
  assert.equal(await staleRefresh, false);
  assert.match(root.innerHTML, /Current operator/u);
  assert.doesNotMatch(root.innerHTML, /Stale operator/u);
  controller.destroy();
});

test("PRD-095 controller clears only an expired PlatformSession", async () => {
  const platformRoot = createRoot();
  let expirePlatform = false;
  const platformController = createPlatformPageController({
    root: platformRoot,
    api: workingApi({
      navigation: async () => {
        if (expirePlatform) {
          throw new PlatformApiError({
            status: 401,
            code: "platform.session_expired",
            message: "平台会话已过期。",
          });
        }
        return navigationPayload();
      },
    }),
  });
  assert.equal(await platformController.mount(), true);
  expirePlatform = true;
  assert.equal(await platformController.refresh(), false);
  assert.match(platformRoot.innerHTML, /进入工作台/u);
  assert.doesNotMatch(platformRoot.innerHTML, /class="platform-shell"/u);

  const supportRoot = createRoot();
  const supportController = createPlatformPageController({
    root: supportRoot,
    api: workingApi({
      diagnostics: async () => {
        throw new PlatformApiError({
          status: 401,
          code: "support.session_expired",
          message: "支持会话已过期。",
        });
      },
    }),
  });
  assert.equal(await supportController.mount(), true);
  assert.equal(await supportController.openSupport(GRANT_ID), false);
  assert.match(supportRoot.innerHTML, /class="platform-shell"/u);
  assert.match(supportRoot.innerHTML, /支持会话已过期/u);
  platformController.destroy();
  supportController.destroy();
});

test("PRD-095 platform workflow never accesses browser Storage APIs", async () => {
  const descriptors = new Map(
    ["localStorage", "sessionStorage"].map((name) => [
      name,
      Object.getOwnPropertyDescriptor(globalThis, name),
    ]),
  );
  for (const name of descriptors.keys()) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      get() {
        throw new Error(`${name} must not be accessed`);
      },
    });
  }
  const controller = createPlatformPageController({ root: createRoot(), api: workingApi() });
  try {
    assert.equal(await controller.mount(), true);
    assert.equal(await controller.refresh(), true);
  } finally {
    controller.destroy();
    for (const [name, descriptor] of descriptors) restoreProperty(name, descriptor);
  }
});

test("PRD-095 renderer escapes server text and defines stable responsive layouts", async () => {
  const root = createRoot();
  const state = {
    ...pageState(),
    session: sessionPayload({ display_name: '<img src=x onerror="unsafe()">' }),
    navigation: [{ id: "operators", label: "<script>unsafe()</script>" }],
  };
  renderPlatformWorkspace(root, state);
  assert.doesNotMatch(root.innerHTML, /<script>|<img/u);
  assert.match(root.innerHTML, /&lt;script&gt;unsafe\(\)&lt;\/script&gt;/u);
  assert.doesNotMatch(root.innerHTML, /value="(?:123456|654321)"/u);

  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  assert.match(css, /grid-template-columns: 240px minmax\(0, 1fr\)/u);
  assert.match(css, /@media \(max-width: 900px\)/u);
  assert.match(css, /@media \(max-width: 600px\)/u);
  assert.match(css, /min-width: 320px/u);
  assert.match(css, /overflow-x: auto/u);
});

function workingApi(overrides = {}) {
  return {
    session: async () => sessionPayload(),
    openSession: async () => sessionPayload(),
    navigation: async () => navigationPayload(),
    operators: async () => operatorListPayload(),
    createOperator: async () => ({ operator: operatorSummary(), request_id: "req", trace_id: "trc" }),
    updateOperator: async () => ({ operator: operatorSummary(), request_id: "req", trace_id: "trc" }),
    grants: async () => grantListPayload(),
    createGrant: async () => ({ grant: grantSummary(), request_id: "req", trace_id: "trc" }),
    approveGrant: async () => ({ grant: grantSummary(), request_id: "req", trace_id: "trc" }),
    revokeGrant: async () => ({ grant: grantSummary(), request_id: "req", trace_id: "trc" }),
    supportSession: async () => ({ request_id: "req", trace_id: "trc" }),
    diagnostics: async () => diagnosticsPayload(),
    breakGlass: async () => ({ request_id: "req", trace_id: "trc" }),
    audit: async () => auditPayload(),
    ...overrides,
  };
}

function sessionPayload(operatorOverrides = {}) {
  return {
    context: {
      operator: operatorSummary(operatorOverrides),
      permissions: [
        "platform.operator.manage",
        "platform.support.request",
        "platform.support.approve",
        "platform.break_glass.use",
        "platform.audit.read",
      ],
      expires_at: "2026-08-21T12:00:00Z",
    },
    request_id: "req_session",
    trace_id: "trc_session",
  };
}

function operatorSummary(overrides = {}) {
  return {
    operator_id: OPERATOR_ID,
    display_name: "Platform Owner",
    status: "active",
    roles: ["platform_owner"],
    revision: 1,
    ...overrides,
  };
}

function navigationPayload() {
  return {
    items: [
      { id: "operators", label: "操作员", href: "/platform.html#operators" },
      { id: "workspaces", label: "Workspaces", href: "/platform.html#workspaces" },
      { id: "support", label: "支持授权", href: "/platform.html#support" },
      { id: "audit", label: "平台审计", href: "/platform.html#audit" },
      { id: "billing", label: "Billing", href: "/platform.html#billing" },
      { id: "verification", label: "Verification", href: "/platform.html#verification" },
      { id: "compliance", label: "Compliance", href: "/platform.html#compliance" },
      { id: "privacy", label: "Privacy", href: "/platform.html#privacy" },
      { id: "releases", label: "Releases", href: "/platform.html#releases" },
      { id: "providers", label: "Provider engineering", href: "/platform.html#providers" },
      { id: "channels", label: "Channels", href: "/platform.html#channels" },
      { id: "customer-success", label: "Customer success", href: "/platform.html#customer-success" },
      { id: "growth", label: "Growth", href: "/platform.html#growth" },
    ],
    request_id: "req_navigation",
    trace_id: "trc_navigation",
  };
}

function operatorListPayload(operatorOverrides = {}) {
  return {
    operators: [operatorSummary(operatorOverrides)],
    total: 1,
    limit: 100,
    offset: 0,
    request_id: "req_operators",
    trace_id: "trc_operators",
  };
}

function grantSummary() {
  return {
    grant_id: GRANT_ID,
    workspace_id: WORKSPACE_ID,
    status: "active",
    missing_approvals: [],
    expires_at: "2026-08-21T12:00:00Z",
  };
}

function grantListPayload() {
  return {
    grants: [grantSummary()],
    total: 1,
    limit: 100,
    offset: 0,
    request_id: "req_grants",
    trace_id: "trc_grants",
  };
}

function diagnosticsPayload() {
  return {
    workspace_id: WORKSPACE_ID,
    operational_status: "healthy",
    region_code: "cn-north-1",
    provisioning_status: "ready",
    health_band: "healthy",
    active_memberships: 3,
    request_id: "req_diagnostics",
    trace_id: "trc_diagnostics",
  };
}

function auditPayload() {
  return {
    entries: [],
    total: 0,
    limit: 100,
    next_cursor: null,
    request_id: "req_audit",
    trace_id: "trc_audit",
  };
}

function mfaEnrollmentPayload() {
  return {
    factor_id: MFA_FACTOR_ID,
    secret: MFA_SECRET,
    provisioning_uri: `otpauth://totp/MetaHub%3Aowner?secret=${MFA_SECRET}&issuer=MetaHub`,
    expires_at: "2026-08-21T12:00:00Z",
    request_id: "req_mfa_enrollment",
    trace_id: "trc_mfa_enrollment",
  };
}

function mfaVerificationPayload() {
  return {
    factor_id: MFA_FACTOR_ID,
    status: "active",
    revision: 2,
    verified_at: "2026-08-21T11:00:00Z",
    request_id: "req_mfa_verification",
    trace_id: "trc_mfa_verification",
  };
}

function identitySessionPayload(mfaConfigured) {
  return {
    account: {
      email: "owner@example.test",
      display_name: "Owner",
      status: "active",
      mfa_configured: mfaConfigured,
    },
    current_session: identitySessionSummary(),
    request_id: "req_identity",
    trace_id: "trc_identity",
  };
}

function identitySessionListPayload() {
  return {
    sessions: [identitySessionSummary()],
    total: 1,
    limit: 20,
    offset: 0,
    request_id: "req_identity_sessions",
    trace_id: "trc_identity_sessions",
  };
}

function identitySessionSummary() {
  return {
    session_id: `gss_${"6".repeat(32)}`,
    device_family: "chrome",
    created_at: "2026-08-21T10:00:00Z",
    last_seen_at: "2026-08-21T11:00:00Z",
    expires_at: "2026-08-22T10:00:00Z",
    is_current: true,
  };
}

function pageState() {
  return {
    loading: false,
    session: sessionPayload(),
    navigation: navigationPayload().items,
    operators: operatorListPayload(),
    grants: grantListPayload(),
    audit: auditPayload(),
    diagnostics: null,
    view: "overview",
    error: null,
    feedback: null,
    pendingAction: null,
  };
}

function createRoot(sensitiveInputs = []) {
  let html = "";
  return {
    get innerHTML() {
      return html;
    },
    set innerHTML(value) {
      html = String(value);
    },
    addEventListener() {},
    querySelectorAll(selector) {
      return selector.includes('input[type="password"]') || selector.includes('input[name="totpCode"]')
        ? sensitiveInputs
        : [];
    },
    replaceChildren() {
      html = "";
    },
  };
}

function installFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return handler(String(url), options);
  };
  return calls;
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return structuredClone(payload);
    },
  };
}

function restoreProperty(name, descriptor) {
  if (descriptor) Object.defineProperty(globalThis, name, descriptor);
  else delete globalThis[name];
}
