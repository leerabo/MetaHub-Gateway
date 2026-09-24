import assert from "node:assert/strict";
import test from "node:test";
import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { renderIdentitySecurityPage } from "../../src/features/identity/renderIdentityPage.js";
import { identitySection, workspaceSectionRoute } from "../../src/features/identity/identitySections.js";
import { IdentityApiError, identityLoginHref, identityLoginNotice } from "../../src/lib/identity.js";

const account = { email: "trial@example.test", display_name: "Trial", status: "active", mfa_configured: false };
const session = { session_id: `gis_${"b".repeat(32)}`, is_current: true, assurance_level: "password" };
const choice = { workspace_id: `wsp_${"c".repeat(32)}`, name: "Trial organization", kind: "organization", role: "owner" };
const authorization = { permission_codes: [], permission_revision: 1, permission_fingerprint: "a".repeat(64), membership_revision: 1 };
const preference = { locale: "zh-CN", available_locales: ["zh-CN", "en-US"], revision: 1, confirmation_state: "confirmed" };
const unprotectedSecurity = { factors: [], sessions: [session], trusted_devices: [], recovery_code_set: null };
function fixture(options = {}, overrides = {}) {
  const calls = [];
  const root = { innerHTML: "", addEventListener() {}, querySelectorAll: () => [] };
  const api = {
    login: async () => ({ account, current_session: session }),
    me: async () => ({ account, current_session: session }),
    sessions: async () => ({ sessions: [session] }),
    workspaces: async () => ({ workspaces: [choice] }),
    identityWorkspaces: async () => ({ workspaces: [choice] }),
    workspaceSession: async () => ({ context: choice, authorization }),
    workspaceNavigation: async () => { calls.push("navigation"); return { ...authorization, groups: [] }; },
    memberships: async () => { calls.push("members"); return { memberships: [] }; },
    onboarding: async () => { calls.push("onboarding"); throw new Error("unselected onboarding"); },
    billingSummary: async () => { calls.push("billing"); throw new Error("unselected billing"); },
    preferences: async () => { calls.push("preferences"); return preference; },
    updatePreferences: async () => preference,
    switchWorkspace: async () => calls.push("switch"),
    ...overrides,
  };
  const controller = createIdentityPageController({ root, api, channelFactory: () => null, ...options });
  return { root, calls, controller };
}

test("account protection starts with status and actions, not password forms", async () => {
  const { root, controller } = fixture({ initialSection: "security", autoEnterWorkspace: false }, {
    security: async () => unprotectedSecurity,
  });
  await controller.mount();
  assert.equal((root.innerHTML.match(/class="identity-security-tool"/gu) ?? []).length, 3);
  assert.doesNotMatch(root.innerHTML, /<form\b|type="password"/u);
  assert.doesNotMatch(root.innerHTML, /class="identity-security-summary"/u);
  assert.match(root.innerHTML, /添加验证方式/u);
  assert.equal(controller.toggleSecurityTool("enrollment"), true);
  assert.match(root.innerHTML, /data-identity-mfa-enrollment-form/u);
  controller.setMfaEnrollmentMethod("webauthn");
  assert.match(root.innerHTML, /注册安全密钥/u);
  assert.match(root.innerHTML, /class="identity-security-tool" open/u);
  assert.equal(controller.toggleSecurityTool("enrollment"), true);
  assert.doesNotMatch(root.innerHTML, /type="password"/u);
  controller.destroy();
});

test("unavailable security actions show prerequisites without unusable forms", async () => {
  const { root, controller } = fixture({ initialSection: "security", autoEnterWorkspace: false }, {
    security: async () => unprotectedSecurity,
  });
  await controller.mount();
  controller.toggleSecurityTool("devices");
  assert.match(root.innerHTML, /需先启用认证器/u);
  assert.doesNotMatch(root.innerHTML, /data-identity-trusted-device-form/u);
  controller.toggleSecurityTool("recovery");
  assert.match(root.innerHTML, /需先启用验证方式并生成恢复码/u);
  assert.doesNotMatch(root.innerHTML, /<form\b|需先启用认证器/u);
  assert.equal(controller.toggleSecurityTool("unknown"), false);
  controller.destroy();
});

test("security form switching scrubs inputs and pending enrollment, and section changes reset choices", async () => {
  const input = { value: "private-test-draft" };
  const { root, controller } = fixture({ initialSection: "security", autoEnterWorkspace: false }, {
    security: async () => unprotectedSecurity,
    beginMfa: async () => ({ factor_id: "test", secret: "TRANSIENT_TEST_SECRET", provisioning_uri: "otpauth://totp/test" }),
  });
  root.querySelectorAll = () => [input];
  await controller.mount();
  controller.toggleSecurityTool("enrollment");
  await controller.beginMfaEnrollment({ currentPassword: "local-test-only" });
  assert.match(root.innerHTML, /TRANSIENT_TEST_SECRET/u);
  input.value = "private-test-draft";
  controller.toggleSecurityTool("devices");
  assert.equal(input.value, "");
  assert.doesNotMatch(root.innerHTML, /TRANSIENT_TEST_SECRET|data-identity-mfa-verification-form/u);
  await controller.showAccountSection("preferences");
  assert.equal(controller.toggleSecurityTool("enrollment"), false);
  await controller.showAccountSection("security");
  assert.doesNotMatch(root.innerHTML, /class="identity-security-tool" open/u);
  controller.showLogin();
  assert.equal(controller.toggleSecurityTool("enrollment"), false);
  controller.destroy();
});

test("busy security work keeps its action visible and rejects view switches", async () => {
  let complete;
  const { root, controller } = fixture({ initialSection: "security", autoEnterWorkspace: false }, {
    security: async () => unprotectedSecurity,
    beginMfa: () => new Promise((resolve) => { complete = resolve; }),
  });
  await controller.mount();
  controller.toggleSecurityTool("enrollment");
  const pending = controller.beginMfaEnrollment({ currentPassword: "local-test-only" });
  await new Promise(setImmediate);
  assert.equal(controller.toggleSecurityTool("devices"), false);
  assert.match(root.innerHTML, /data-identity-mfa-enrollment-form/u);
  complete({ factor_id: "test", secret: "TRANSIENT_TEST_SECRET", provisioning_uri: "otpauth://totp/test" });
  await pending;
  controller.destroy();
});

test("existing recovery challenge remains visible without choosing a form", async () => {
  const { root, controller } = fixture({ initialSection: "security", autoEnterWorkspace: false }, {
    security: async () => ({ ...unprotectedSecurity, recovery_challenge: {
      ready_at: "2026-09-12T00:00:00Z", expires_at: "2026-09-13T00:00:00Z",
    } }),
  });
  await controller.mount();
  assert.match(root.innerHTML, /data-identity-recovery-complete-form/u);
  assert.equal(controller.toggleSecurityTool("recovery"), false);
  controller.toggleSecurityTool("devices");
  assert.match(root.innerHTML, /data-identity-recovery-complete-form/u);
  controller.destroy();
});

test("security action errors stay visible while all optional forms are closed", () => {
  const root = { innerHTML: "" };
  renderIdentitySecurityPage(root, { account, currentSession: session, accountSection: "security",
    identitySecurityError: { message: "Security status unavailable", requestId: "req_test" } });
  assert.match(root.innerHTML, /Security status unavailable/u);
  assert.doesNotMatch(root.innerHTML, /<form\b/u);
});

test("interrupted console entry shows a fixed login notice and bounded request reference", async () => {
  const href = identityLoginHref({ interrupted: true, requestId: "req_test_123" });
  const loginNotice = identityLoginNotice(new URL(href, "http://localhost").searchParams);
  const { root, controller } = fixture();
  await controller.mount({ login: true, loginNotice });
  assert.match(root.innerHTML, /当前登录状态不可用，请重新登录。/u);
  assert.match(root.innerHTML, /request_id: req_test_123/u);
  assert.match(root.innerHTML, /data-identity-login-form/u);
  assert.equal(identityLoginHref(), "/identity.html?view=login");
  controller.destroy();
});

test("login handoff rejects arbitrary messages, identifiers and non-login views", () => {
  assert.equal(identityLoginHref({ interrupted: true, requestId: '<script>secret</script>' }),
    "/identity.html?view=login&reason=session-unavailable");
  assert.equal(identityLoginNotice(new URLSearchParams("view=register&reason=session-unavailable")), null);
  assert.equal(identityLoginNotice(new URLSearchParams("view=login&reason=arbitrary-message")), null);
  assert.equal(identityLoginNotice(new URLSearchParams("view=login&reason=session-unavailable&request_id=secret")).requestId, "-");
});

for (const operation of ["mount", "login"]) {
  test(`existing organization enters the console without loading account panels after ${operation}`, async () => {
    let entered = 0;
    const { controller, calls } = fixture({ onWorkspaceReady: () => entered++ });
    await controller[operation]({ email: account.email, password: "test-only" });
    assert.equal(entered, 1);
    assert.deepEqual(calls, []);
    controller.destroy();
  });

  test(`explicit preferences load through ${operation} and do not redirect`, async () => {
    let entered = 0;
    const { controller, calls, root } = fixture({ initialSection: "preferences", autoEnterWorkspace: false, onWorkspaceReady: () => entered++ });
    await controller[operation]({ email: account.email, password: "test-only" });
    assert.equal(entered, 0);
    assert.deepEqual(calls, ["navigation", "preferences"]);
    assert.match(root.innerHTML, /data-account-preference-form/u);
    assert.doesNotMatch(root.innerHTML, /账号偏好尚未加载|data-identity-password-form|data-onboarding-step-panel/u);
    controller.destroy();
  });
}

for (const kind of ["empty", "multiple", "recovery", "invitation", "invalid-owner", "invalid-authorization"]) {
  test(`entry does not redirect without a valid unambiguous workspace: ${kind}`, async () => {
    let entered = 0;
    const missing = () => { throw new IdentityApiError({ status: 401, code: "workspace.session_missing", message: "Missing" }); };
    const overrides = kind === "empty" ? {
      workspaces: async () => ({ workspaces: [] }), identityWorkspaces: async () => ({ workspaces: [] }), workspaceSession: missing,
    } : kind === "multiple" ? {
      identityWorkspaces: async () => ({ workspaces: [choice, { ...choice, workspace_id: `wsp_${"d".repeat(32)}` }] }), workspaceSession: missing,
    } : kind === "recovery" ? {
      me: async () => ({ account, current_session: { ...session, assurance_level: "recovery" } }), workspaceSession: missing,
    } : kind === "invalid-owner" ? {
      workspaceSession: async () => ({ context: { ...choice, workspace_id: `wsp_${"e".repeat(32)}` }, authorization }),
    } : kind === "invalid-authorization" ? {
      workspaceSession: async () => ({ context: choice, authorization: null }),
    } : {};
    const { controller, root, calls } = fixture({ onWorkspaceReady: () => entered++, invitationToken: kind === "invitation" ? "private-test-invitation" : null }, overrides);
    await controller.mount();
    assert.equal(entered, 0);
    assert.ok(!calls.includes("switch"));
    assert.doesNotMatch(root.innerHTML, /data-onboarding-step-panel|data-account-preference-form|data-identity-password-form/u);
    if (kind === "empty") assert.match(root.innerHTML, /创建组织/u);
    controller.destroy();
  });
}

test("single organization establishes and validates a workspace session before entry", async () => {
  let reads = 0;
  let entered = 0;
  const { controller, calls } = fixture({ onWorkspaceReady: () => entered++ }, {
    workspaceSession: async () => {
      if (++reads === 1) throw new IdentityApiError({ status: 401, code: "workspace.session_missing" });
      return { context: choice, authorization };
    },
  });
  await controller.mount();
  assert.equal(entered, 1);
  assert.equal(reads, 2);
  assert.deepEqual(calls, ["switch"]);
  controller.destroy();
});

test("ordinary login restores entry intent after a previous entry or cancelled creation", async () => {
  let entered = 0;
  const { controller } = fixture({ onWorkspaceReady: () => entered++ });
  await controller.mount();
  controller.showWorkspaceCreate();
  controller.cancelWorkspaceCreate();
  controller.showLogin();
  await controller.login({ email: account.email, password: "test-only" });
  assert.equal(entered, 2);
  controller.destroy();
});

test("leaving preferences cancels its request and ignores late data", async () => {
  let resolvePreference;
  let signal;
  const { controller, root } = fixture({ initialSection: "preferences", autoEnterWorkspace: false }, {
    preferences: (input) => { signal = input.signal; return new Promise((resolve) => { resolvePreference = resolve; }); },
  });
  const loading = controller.mount();
  await new Promise(setImmediate);
  assert.ok(signal);
  await controller.showAccountSection("workspaces");
  assert.equal(signal.aborted, true);
  resolvePreference(preference);
  await loading;
  assert.match(root.innerHTML, /data-account-section="workspaces"/u);
  assert.doesNotMatch(root.innerHTML, /data-account-preference-form/u);
  controller.destroy();
});

test("account default renders only organization entry, even with every domain projection supplied", () => {
  const root = { innerHTML: "" };
  renderIdentitySecurityPage(root, { account, currentSession: session, workspaceContext: choice,
    workspaceChoices: [choice], workspaces: [choice], conditionalAccessVisible: true,
    dataGovernanceVisible: true, machineIdentityVisible: true });
  assert.match(root.innerHTML, /进入空间/u);
  assert.equal((root.innerHTML.match(/<form\b/gu) ?? []).length, 0);
  assert.doesNotMatch(root.innerHTML, /identity-onboarding-panel|identity-mfa-start|identity-machine-form|账号偏好尚未加载/u);
});

test("legacy account anchors resolve to fixed sections only", () => {
  assert.equal(identitySection("#account-preferences"), "preferences");
  assert.equal(identitySection("#identity-memberships-title"), "members");
  assert.equal(identitySection("https://example.test"), "workspaces");
  assert.equal(workspaceSectionRoute("#members"), "workspace.settings.members");
  assert.equal(workspaceSectionRoute("#access"), "workspace.governance.policies");
});

test("explicit account entry never paints a login form while verifying the session", async () => {
  let resolveIdentity;
  const { root, controller } = fixture({ autoEnterWorkspace: false }, {
    me: () => new Promise((resolve) => { resolveIdentity = resolve; }),
  });
  const mounted = controller.mount();
  assert.match(root.innerHTML, /identity-account-loading/u);
  assert.doesNotMatch(root.innerHTML, /data-identity-login-form|登录账号/u);
  resolveIdentity({ account, current_session: session });
  await mounted;
  controller.destroy();
});

test("explicit login does not restore a different account's existing session", async () => {
  const { root, controller } = fixture({}, { me: async () => assert.fail("unexpected session restore") });
  assert.equal(await controller.mount({ login: true }), true);
  assert.match(root.innerHTML, /data-identity-login-form/u);
  controller.destroy();
});

test("direct password-reset entry retains the reset view", async () => {
  const publicViews = [];
  const { root, controller } = fixture({
    onPublicViewChange: (view) => publicViews.push(view),
  });
  assert.equal(await controller.mount({ reset: true }), true);
  assert.match(root.innerHTML, /data-identity-reset-request-form/u);
  assert.deepEqual(publicViews, ["reset"]);
  controller.destroy();
});

test("anonymous session check leaves the visible login form and typed password intact", async () => {
  let rejectIdentity;
  const { root, controller } = fixture({}, {
    me: () => new Promise((_resolve, reject) => { rejectIdentity = reject; }),
  });
  const password = { value: "local-test-input" };
  root.querySelector = (selector) => selector === "[data-identity-login-form]"
    && root.innerHTML.includes("data-identity-login-form") ? {} : null;
  root.querySelectorAll = (selector) => selector.includes("password") ? [password] : [];
  const mounted = controller.mount();
  const visibleForm = root.innerHTML;
  rejectIdentity(new IdentityApiError({ status: 401, code: "identity.session_expired" }));
  await mounted;
  assert.equal(root.innerHTML, visibleForm);
  assert.equal(password.value, "local-test-input");
  controller.destroy();
  assert.equal(password.value, "");
});

test("a late anonymous session response cannot replace an explicitly selected registration form", async () => {
  let rejectIdentity;
  const { root, controller } = fixture({}, {
    me: () => new Promise((_resolve, reject) => { rejectIdentity = reject; }),
  });
  const mounted = controller.mount();
  await controller.showRegistration();
  rejectIdentity(new IdentityApiError({ status: 401, code: "identity.session_expired" }));
  await mounted;
  assert.match(root.innerHTML, /data-identity-registration-form/u);
  controller.destroy();
});

test("logout navigates home only after server logout succeeds", async () => {
  let navigated = 0;
  const { controller } = fixture({ onLoggedOut: () => navigated++ }, { logout: async () => {} });
  await controller.mount();
  assert.equal(await controller.logout(), true);
  assert.equal(navigated, 1);
  controller.destroy();
});

test("failed logout does not claim successful navigation", async () => {
  const { controller, root } = fixture({ onLoggedOut: () => assert.fail("logout failed") }, {
    logout: async () => { throw new IdentityApiError({ status: 503, message: "退出未完成" }); },
  });
  await controller.mount();
  assert.equal(await controller.logout(), false);
  assert.match(root.innerHTML, /退出未完成/u);
  controller.destroy();
});
