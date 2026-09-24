import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import {
  renderIdentityRegistrationPage,
  renderIdentityResetConfirmPage,
  renderIdentityLoginPage,
  renderIdentitySecurityPage,
  renderIdentityVerificationPage,
} from "../../src/features/identity/renderIdentityPage.js";
import {
  confirmIdentityPasswordReset,
  IdentityApiError,
  getIdentityMe,
  listIdentitySessions,
  loginIdentity,
  logoutIdentity,
  registerIdentity,
  requestIdentityPasswordReset,
  resendIdentityVerification,
  revokeIdentitySession,
  rotateIdentityPassword,
  verifyIdentityRegistration,
} from "../../src/lib/identity.js";
import {
  createAccountWorkspace,
  getAccountWorkspace,
  listAccountWorkspaces,
} from "../../src/lib/workspaces.js";

const CONTROL_URL = "http://control.local";
const SESSION_ONE = "gis_11111111111111111111111111111111";
const SESSION_TWO = "gis_22222222222222222222222222222222";
const WORKSPACE_ONE = "wsp_11111111111111111111111111111111";

test("identity login sends only email and password with credential cookies", async () => {
  const calls = installFetch(() => accountSessionResponse());

  await loginIdentity({
    controlApiUrl: CONTROL_URL,
    email: "owner@example.com",
    password: "PasswordMustRemainPrivate",
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://control.local/identity/login");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.cache, "no-store");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    email: "owner@example.com",
    password: "PasswordMustRemainPrivate",
  });
  assert.equal("Authorization" in calls[0].options.headers, false);
  assert.equal("X-Request-ID" in calls[0].options.headers, false);
  assert.equal("X-CSRF-Token" in calls[0].options.headers, false);
  assert.doesNotMatch(calls[0].options.body, /tenant|workspace|role|permission/u);
});

test("identity reads include credentials and never send CSRF or Authorization", async () => {
  const calls = installFetch((url) =>
    String(url).endsWith("/sessions") ? sessionListResponse() : accountSessionResponse(),
  );

  await getIdentityMe({ controlApiUrl: CONTROL_URL });
  await listIdentitySessions({ controlApiUrl: CONTROL_URL });

  assert.deepEqual(
    calls.map((call) => [call.url, call.options.method, call.options.credentials]),
    [
      ["http://control.local/identity/me", "GET", "include"],
      ["http://control.local/identity/sessions", "GET", "include"],
    ],
  );
  for (const call of calls) {
    assert.equal("Authorization" in call.options.headers, false);
    assert.equal("X-CSRF-Token" in call.options.headers, false);
  }
});

test("workspace client sends exact cookie, CSRF, and idempotency contracts", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-global-csrf=workspace%2Dcsrf" };
  const calls = installFetch((_url, options) =>
    options.method === "POST"
      ? jsonResponse(workspaceDetailPayload(), { status: 201 })
      : jsonResponse(workspaceListPayload()),
  );
  try {
    await listAccountWorkspaces({ controlApiUrl: CONTROL_URL });
    await createAccountWorkspace({
      controlApiUrl: CONTROL_URL,
      kind: "organization",
      name: "Acme AI",
      slug: "acme-ai",
      region: "cn-north-1",
      serviceMode: "internal_only",
      idempotencyKey: "workspace-request-0001",
    });
    await createAccountWorkspace({
      controlApiUrl: CONTROL_URL,
      kind: "personal",
      region: "cn-east-1",
      serviceMode: "public_service",
      idempotencyKey: "workspace-request-0002",
    });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(
    calls.map(({ url, options }) => [url, options.method, options.credentials, options.cache]),
    [
      ["http://control.local/workspaces", "GET", "include", "no-store"],
      ["http://control.local/workspaces", "POST", "include", "no-store"],
      ["http://control.local/workspaces", "POST", "include", "no-store"],
    ],
  );
  assert.equal("X-CSRF-Token" in calls[0].options.headers, false);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[1].options.headers["Idempotency-Key"], "workspace-request-0001");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    kind: "organization",
    region: "cn-north-1",
    service_mode: "internal_only",
    name: "Acme AI",
    slug: "acme-ai",
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    kind: "personal",
    region: "cn-east-1",
    service_mode: "public_service",
  });
  for (const call of calls) {
    assert.equal("Authorization" in call.options.headers, false);
  }
});

test("self-service identity clients send exact in-memory challenge contracts", async () => {
  const calls = installFetch((url) =>
    String(url).endsWith("/verifications") || String(url).endsWith("/confirm")
      ? accountSessionResponse()
      : jsonResponse(pendingChallengePayload(String(url).includes("password-resets") ? "password_reset" : "registration")),
  );

  await registerIdentity({
    controlApiUrl: CONTROL_URL,
    email: "new@example.com",
    displayName: "New Owner",
    password: "PrivateRegistrationPassword",
    consentVersion: "privacy-2026-08",
    invitationToken: "invitation-token-private",
  });
  await verifyIdentityRegistration({
    controlApiUrl: CONTROL_URL,
    intentId: "rgi_11111111111111111111111111111111",
    code: "123456",
  });
  await resendIdentityVerification({
    controlApiUrl: CONTROL_URL,
    intentId: "rgi_11111111111111111111111111111111",
  });
  await requestIdentityPasswordReset({
    controlApiUrl: CONTROL_URL,
    email: "new@example.com",
  });
  await confirmIdentityPasswordReset({
    controlApiUrl: CONTROL_URL,
    intentId: "pri_11111111111111111111111111111111",
    code: "654321",
    newPassword: "PrivateReplacementPassword",
  });

  assert.deepEqual(
    calls.map(({ url, options }) => [url, options.method, JSON.parse(options.body)]),
    [
      [
        "http://control.local/identity/registrations",
        "POST",
        {
          email: "new@example.com",
          display_name: "New Owner",
          password: "PrivateRegistrationPassword",
          consent_version: "privacy-2026-08",
          invitation_token: "invitation-token-private",
        },
      ],
      [
        "http://control.local/identity/verifications",
        "POST",
        { intent_id: "rgi_11111111111111111111111111111111", code: "123456" },
      ],
      [
        "http://control.local/identity/verifications/resend",
        "POST",
        { intent_id: "rgi_11111111111111111111111111111111" },
      ],
      [
        "http://control.local/identity/password-resets",
        "POST",
        { email: "new@example.com" },
      ],
      [
        "http://control.local/identity/password-resets/confirm",
        "POST",
        {
          intent_id: "pri_11111111111111111111111111111111",
          code: "654321",
          new_password: "PrivateReplacementPassword",
        },
      ],
    ],
  );
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal("Authorization" in call.options.headers, false);
    assert.equal("X-CSRF-Token" in call.options.headers, false);
  }
});

test("registration and reset renderers keep intent and secrets out of DOM", () => {
  const root = createRoot();
  renderIdentityRegistrationPage(root, { email: '<unsafe>@example.com', displayName: "<Owner>" });
  assert.match(root.innerHTML, /data-identity-registration-form/u);
  assert.match(root.innerHTML, /autocomplete="new-password"/u);
  assert.match(root.innerHTML, /name="consent" type="checkbox"/u);
  assert.doesNotMatch(root.innerHTML, /<unsafe>|<Owner>|invitation_token|intent_id/u);

  renderIdentityVerificationPage(root, { feedback: "验证码已发送。" });
  assert.match(root.innerHTML, /autocomplete="one-time-code"/u);
  assert.match(root.innerHTML, /pattern="\[0-9\]\{6\}"/u);
  assert.doesNotMatch(root.innerHTML, /rgi_|intent_id|invitation/u);

  renderIdentityResetConfirmPage(root);
  assert.match(root.innerHTML, /data-identity-reset-confirm-form/u);
  assert.doesNotMatch(root.innerHTML, /pri_|intent_id/u);
});

test("self-service controller keeps challenge in memory and scrubs secret inputs", async () => {
  const passwordInputs = [{ value: "RegistrationSecret" }, { value: "RegistrationSecret" }];
  const codeInputs = [{ value: "123456" }];
  const root = createRoot(passwordInputs, codeInputs);
  const seen = [];
  const controller = createIdentityPageController({
    root,
    invitationToken: "private-invitation-token",
    api: workingApi({
      register: async (input) => {
        seen.push(["register", input]);
        return pendingChallengePayload("registration");
      },
      verify: async (input) => {
        seen.push(["verify", input]);
        return accountSessionPayload();
      },
    }),
  });

  controller.showRegistration();
  assert.equal(
    await controller.registerAccount({
      email: "new@example.com",
      displayName: "New Owner",
      password: "RegistrationSecret",
      confirmPassword: "RegistrationSecret",
      consent: true,
    }),
    true,
  );
  assert.match(root.innerHTML, /data-identity-verification-form/u);
  assert.doesNotMatch(root.innerHTML, /private-invitation-token|rgi_/u);
  assert.deepEqual(passwordInputs.map((input) => input.value), ["", ""]);

  assert.equal(await controller.verifyRegistration({ code: "123456" }), true);
  assert.deepEqual(codeInputs.map((input) => input.value), [""]);
  assert.match(root.innerHTML, /data-account-section="workspaces"/u);
  assert.equal(seen[0][0], "register");
  assert.equal(seen[0][1].invitationToken, "private-invitation-token");
  assert.equal(seen[1][0], "verify");
  assert.equal(seen[1][1].intentId, "rgi_11111111111111111111111111111111");
});

test("protected writes use the unique CSRF cookie and encode session paths", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "theme=light; __Host-metahub-global-csrf=csrf%2Dvalue" };
  const calls = installFetch((url) => {
    if (String(url).endsWith("/logout")) return jsonResponse(logoutPayload());
    if (String(url).endsWith("/rotate")) return jsonResponse(rotationPayload());
    return jsonResponse(revokePayload());
  });
  try {
    await logoutIdentity({ controlApiUrl: CONTROL_URL });
    await revokeIdentitySession({ controlApiUrl: CONTROL_URL, sessionId: "session/with space" });
    await rotateIdentityPassword({
      controlApiUrl: CONTROL_URL,
      currentPassword: "CurrentPassword",
      newPassword: "ReplacementPassword",
    });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.equal(calls[0].options.headers["X-CSRF-Token"], "csrf-value");
  assert.equal(calls[1].url, "http://control.local/identity/sessions/session%2Fwith%20space");
  assert.equal(calls[1].options.method, "DELETE");
  assert.equal(calls[2].url, "http://control.local/identity/credentials/password/rotate");
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    current_password: "CurrentPassword",
    new_password: "ReplacementPassword",
  });
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.headers["X-CSRF-Token"], "csrf-value");
    assert.equal("Authorization" in call.options.headers, false);
  }
});

test("missing, duplicate, and malformed CSRF cookies fail before fetch", async () => {
  const originalDocument = globalThis.document;
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse(logoutPayload());
  };
  try {
    for (const cookie of [
      "theme=light",
      "__Host-metahub-global-csrf=one; __Host-metahub-global-csrf=two",
      "__Host-metahub-global-csrf=",
      "__Host-metahub-global-csrf=one,two",
      "__Host-metahub-global-csrf=%E0%A4%A",
    ]) {
      globalThis.document = { cookie };
      await assert.rejects(
        () => logoutIdentity({ controlApiUrl: CONTROL_URL }),
        (error) => error instanceof IdentityApiError && error.code === "identity.csrf_unavailable",
      );
    }
  } finally {
    globalThis.document = originalDocument;
  }
  assert.equal(fetchCount, 0);
});

test("strict DTO validation rejects authorization and internal fields", async () => {
  const forbiddenMutations = [
    (payload) => Object.assign(payload, { tenant_id: "tenant_private" }),
    (payload) => Object.assign(payload.account, { role: "platform_admin" }),
    (payload) => Object.assign(payload.account, { account_id: "gac_private" }),
    (payload) => Object.assign(payload.current_session, { token: "token_private" }),
    (payload) => Object.assign(payload.current_session, { csrf: "csrf_private" }),
    (payload) => Object.assign(payload.current_session, { issuer: "issuer_private" }),
    (payload) => Object.assign(payload.current_session, { credential_hash: "hash_private" }),
  ];

  for (const mutate of forbiddenMutations) {
    const payload = accountSessionPayload();
    mutate(payload);
    installFetch(() => jsonResponse(payload));
    await assert.rejects(
      () => getIdentityMe({ controlApiUrl: CONTROL_URL }),
      (error) => error instanceof IdentityApiError && error.code === "identity.response_invalid",
    );
  }
});

test("workspace request validation fails locally and response DTOs reject internal fields", async () => {
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse(workspaceDetailPayload());
  };
  await assert.rejects(
    () =>
      createAccountWorkspace({
        controlApiUrl: CONTROL_URL,
        kind: "tenant_admin",
        region: "cn-north-1",
        serviceMode: "internal_only",
        idempotencyKey: "workspace-request-0003",
      }),
    (error) => error instanceof IdentityApiError && error.code === "workspace.request_invalid",
  );
  assert.equal(fetchCount, 0);

  for (const mutate of [
    (payload) => Object.assign(payload, { tenant_id: "tenant_private" }),
    (payload) => Object.assign(payload.workspace, { account_id: "gac_private" }),
    (payload) => Object.assign(payload.workspace.environment, { environment_id: "env_private" }),
    (payload) => Object.assign(payload.workspace.provisioning, { generation: 7 }),
  ]) {
    const payload = workspaceDetailPayload();
    mutate(payload);
    installFetch(() => jsonResponse(payload));
    await assert.rejects(
      () =>
        getAccountWorkspace({
          controlApiUrl: CONTROL_URL,
          workspaceId: WORKSPACE_ONE,
        }),
      (error) => error instanceof IdentityApiError && error.code === "workspace.response_invalid",
    );
  }
});

test("identity and workspace clients never read browser storage", async () => {
  const localDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const sessionDescriptor = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      throw new Error("localStorage must not be accessed");
    },
  });
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    get() {
      throw new Error("sessionStorage must not be accessed");
    },
  });
  installFetch((url) =>
    String(url).endsWith("/workspaces") ? jsonResponse(workspaceListPayload()) : accountSessionResponse(),
  );
  try {
    await getIdentityMe({ controlApiUrl: CONTROL_URL });
    await listAccountWorkspaces({ controlApiUrl: CONTROL_URL });
  } finally {
    restoreProperty("localStorage", localDescriptor);
    restoreProperty("sessionStorage", sessionDescriptor);
  }
});

test("login renderer exposes only email and password fields", () => {
  const root = createRoot();
  renderIdentityLoginPage(root, {
    email: 'owner"><script>unsafe()</script>@example.com',
    error: { message: "<script>unsafe()</script>", requestId: "req_<unsafe>" },
  });

  assert.equal((root.innerHTML.match(/<input /gu) ?? []).length, 2);
  assert.match(root.innerHTML, /name="email"/u);
  assert.match(root.innerHTML, /name="password"/u);
  assert.doesNotMatch(root.innerHTML, /name="(?:tenant|workspace|role|permission)"/u);
  assert.doesNotMatch(root.innerHTML, /<script>/u);
  assert.match(root.innerHTML, /request_id: req_&lt;unsafe&gt;/u);
  assert.match(root.innerHTML, /autocomplete="username"/u);
  assert.match(root.innerHTML, /autocomplete="current-password"/u);
});

test("security renderer escapes workspace fields and omits all internal identifiers", () => {
  const root = createRoot();
  renderIdentitySecurityPage(root, {
    account: account({ email: "owner@example.com", display_name: "<Owner>" }),
    currentSession: session({ session_id: SESSION_ONE, is_current: true }),
    sessions: [
      session({ session_id: SESSION_ONE, is_current: true }),
      session({ session_id: SESSION_TWO, is_current: false, device_family: "firefox" }),
    ],
    workspaces: [
      workspace({
        workspace_id: WORKSPACE_ONE,
        name: "<Unsafe Workspace>",
        slug: "safe-slug",
      }),
    ],
  });

  assert.doesNotMatch(
    root.innerHTML,
    new RegExp(`${SESSION_ONE}|${SESSION_TWO}|${WORKSPACE_ONE}`, "u"),
  );
  assert.doesNotMatch(
    root.innerHTML,
    /account_id|tenant_id|tenant_slug|workspace_id|permission|issuer|audience|credential_hash|csrf|token/iu,
  );
  assert.doesNotMatch(root.innerHTML, /<Owner>|<Unsafe Workspace>/u);
  assert.match(root.innerHTML, /&lt;Owner&gt;/u);
  assert.match(root.innerHTML, /&lt;Unsafe Workspace&gt;/u);
  renderIdentitySecurityPage(root, {
    account: account(), accountSection: "devices",
    currentSession: session({ session_id: SESSION_ONE, is_current: true }),
    sessions: [session({ session_id: SESSION_ONE, is_current: true }),
      session({ session_id: SESSION_TWO, is_current: false })],
  });
  assert.match(root.innerHTML, /data-session-index="0"/u);
  assert.match(root.innerHTML, /data-session-index="1"/u);
  assert.doesNotMatch(root.innerHTML, new RegExp(`${SESSION_ONE}|${SESSION_TWO}`, "u"));
});

test("workspace controller reuses a key only for an identical failed body", async () => {
  const root = createRoot();
  const attempts = [];
  const keys = ["workspace-key-one", "workspace-key-two"];
  const controller = createIdentityPageController({
    root,
    newIdempotencyKey: () => keys.shift(),
    api: workingApi({
      createWorkspace: async (input) => {
        attempts.push(input);
        if (attempts.length < 3) {
          throw new IdentityApiError({
            code: "workspace.network_unavailable",
            message: "<Retry safely>",
          });
        }
        return workspaceDetailPayload();
      },
    }),
  });
  await controller.mount();
  controller.showWorkspaceCreate();

  const firstBody = {
    kind: "organization",
    name: "<Draft Company>",
    slug: "draft-company",
    region: "cn-north-1",
    serviceMode: "internal_only",
  };
  assert.equal(await controller.createWorkspace(firstBody), false);
  assert.doesNotMatch(root.innerHTML, /<Draft Company>|workspace-key-one/u);
  assert.match(root.innerHTML, /&lt;Draft Company&gt;|&lt;Retry safely&gt;/u);
  assert.equal(await controller.createWorkspace(firstBody), false);
  assert.equal(
    await controller.createWorkspace({ ...firstBody, region: "cn-east-1" }),
    true,
  );

  assert.deepEqual(
    attempts.map((attempt) => attempt.idempotencyKey),
    ["workspace-key-one", "workspace-key-one", "workspace-key-two"],
  );
  assert.match(root.innerHTML, /组织已创建/u);
  assert.doesNotMatch(root.innerHTML, /workspace-key-(?:one|two)|wsp_1111/u);
});

test("other-session revoke confirms and current-session revoke returns to login", async () => {
  const root = createRoot();
  const revoked = [];
  let confirmationCount = 0;
  const api = workingApi({
    revoke: async ({ sessionId }) => {
      revoked.push(sessionId);
      return revokePayload();
    },
  });
  const controller = createIdentityPageController({
    root,
    api,
    confirmAction: () => {
      confirmationCount += 1;
      return true;
    },
  });

  await controller.mount();
  await controller.revokeSession(1);
  assert.equal(confirmationCount, 1);
  assert.deepEqual(revoked, [SESSION_TWO]);

  await controller.revokeSession(0);
  assert.equal(confirmationCount, 1);
  assert.deepEqual(revoked, [SESSION_TWO, SESSION_ONE]);
  assert.match(root.innerHTML, /data-identity-login-form/u);
  assert.doesNotMatch(root.innerHTML, /账号保护状态/u);
});

test("cancelled other-session revoke does not call the API", async () => {
  const root = createRoot();
  let revokeCount = 0;
  const controller = createIdentityPageController({
    root,
    api: workingApi({ revoke: async () => (revokeCount += 1) }),
    confirmAction: () => false,
  });
  await controller.mount();
  assert.equal(await controller.revokeSession(1), false);
  assert.equal(revokeCount, 0);
});

test("password rotation clears fields and reloads replacement session state", async () => {
  const passwordInputs = [{ value: "CurrentSecret" }, { value: "NewSecret" }, { value: "NewSecret" }];
  const root = createRoot(passwordInputs);
  let sessionsCalls = 0;
  const api = workingApi({
    sessions: async () => {
      sessionsCalls += 1;
      return sessionListPayload(
        sessionsCalls === 1
          ? [
              session({ session_id: SESSION_ONE, is_current: true }),
              session({ session_id: SESSION_TWO, is_current: false }),
            ]
          : [session({ session_id: "gis_33333333333333333333333333333333", is_current: true })],
      );
    },
  });
  const controller = createIdentityPageController({ root, api });

  await controller.mount();
  const result = await controller.rotatePassword({
    currentPassword: "CurrentSecret",
    newPassword: "NewSecret",
    confirmPassword: "NewSecret",
  });

  assert.equal(result, true);
  assert.deepEqual(passwordInputs.map((input) => input.value), ["", "", ""]);
  assert.match(root.innerHTML, /密码已更新，其他设备已退出/u);
  await controller.showAccountSection("devices");
  assert.match(root.innerHTML, /当前没有其他设备登录/u);
  assert.doesNotMatch(root.innerHTML, new RegExp(SESSION_TWO, "u"));
});

test("logout and expired sessions scrub protected DOM and password fields", async () => {
  const passwordInputs = [{ value: "MustBeCleared" }];
  const root = createRoot(passwordInputs);
  const controller = createIdentityPageController({
    root,
    api: workingApi({
      logout: async () => {
        throw new IdentityApiError({ status: 401, code: "identity.session_invalid" });
      },
    }),
  });
  await controller.mount();
  await controller.logout();

  assert.equal(passwordInputs[0].value, "");
  assert.match(root.innerHTML, /data-identity-login-form/u);
  assert.doesNotMatch(root.innerHTML, /账号保护状态/u);

  const expired = createIdentityPageController({
    root,
    api: workingApi({
      me: async () => {
        throw new IdentityApiError({ status: 401, code: "identity.session_invalid" });
      },
    }),
  });
  await expired.mount();
  assert.match(root.innerHTML, /data-identity-login-form/u);
});

test("late protected responses cannot repopulate the page after logout", async () => {
  const root = createRoot();
  const meDeferred = deferred();
  const api = workingApi({ me: async () => meDeferred.promise });
  const controller = createIdentityPageController({ root, api });

  const mounting = controller.mount();
  await controller.logout();
  meDeferred.resolve(accountSessionPayload());
  await mounting;

  assert.match(root.innerHTML, /data-identity-login-form/u);
  assert.doesNotMatch(root.innerHTML, /账号保护状态|owner@example.com/u);
});

test("login renders before session probe and typing invalidates late responses", async () => {
  for (const expired of [false, true]) {
    const root = createRoot();
    const events = new Map();
    root.addEventListener = (name, handler) => events.set(name, handler);
    const pending = deferred();
    let signal;
    const controller = createIdentityPageController({ root, api: workingApi({
      me: async (input) => {
        signal = input.signal;
        await pending.promise;
        if (expired) throw new IdentityApiError({ status: 401, code: "identity.session_invalid" });
        return accountSessionPayload();
      },
    }) });
    const mounted = controller.mount();
    assert.match(root.innerHTML, /data-identity-login-form/u);
    assert.doesNotMatch(root.innerHTML, /disabled/u);
    events.get("input")({ target: { closest: () => true } });
    const current = root.innerHTML;
    assert.equal(signal.aborted, true);
    pending.resolve();
    await mounted;
    assert.equal(root.innerHTML, current);
    controller.destroy();
  }
});

test("organization form and client need only a name; technical defaults stay server-side", async () => {
  const root = createRoot();
  const controller = createIdentityPageController({ root, api: workingApi() });
  await controller.mount();
  controller.showWorkspaceCreate();
  assert.match(root.innerHTML, /name="name"/u);
  assert.doesNotMatch(root.innerHTML, /name="(?:slug|region|serviceMode)"/u);
  controller.destroy();
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-global-csrf=workspace-csrf" };
  const calls = installFetch(() => jsonResponse(workspaceDetailPayload(), { status: 201 }));
  try {
    await createAccountWorkspace({ controlApiUrl: CONTROL_URL, kind: "organization",
      name: "研发团队", idempotencyKey: "name-only-workspace-0001" });
    assert.deepEqual(JSON.parse(calls[0].options.body), { kind: "organization", name: "研发团队" });
  } finally {
    globalThis.document = originalDocument;
  }
});

test("logout waits for an in-flight login before clearing the issued session", async () => {
  const root = createRoot();
  const loginDeferred = deferred();
  const operations = [];
  const controller = createIdentityPageController({
    root,
    api: workingApi({
      login: async () => {
        operations.push("login-started");
        return loginDeferred.promise;
      },
      logout: async () => {
        operations.push("logout-finished");
        return logoutPayload();
      },
    }),
  });

  const login = controller.login({ email: "owner@example.com", password: "PrivatePassword" });
  const logout = controller.logout();
  loginDeferred.resolve(accountSessionPayload());
  await Promise.all([login, logout]);

  assert.deepEqual(operations, ["login-started", "logout-finished"]);
  assert.match(root.innerHTML, /data-identity-login-form/u);
  assert.doesNotMatch(root.innerHTML, /账号保护状态|PrivatePassword/u);
});

test("logout shows an explicit pending state until the server session is closed", async () => {
  const root = createRoot();
  const logoutDeferred = deferred();
  const controller = createIdentityPageController({
    root,
    api: workingApi({ logout: async () => logoutDeferred.promise }),
  });

  await controller.mount();
  const pending = controller.logout();
  assert.match(root.innerHTML, /正在退出登录/u);
  assert.doesNotMatch(root.innerHTML, /data-identity-login-form/u);

  logoutDeferred.resolve(logoutPayload());
  await pending;
  assert.match(root.innerHTML, /data-identity-login-form/u);
});

test("safe page errors escape HTML and expose only request_id", async () => {
  const root = createRoot();
  const controller = createIdentityPageController({
    root,
    api: workingApi({
      login: async () => {
        throw new IdentityApiError({
          status: 401,
          code: "identity.login_invalid",
          message: "<img src=x onerror=unsafe()>",
          requestId: "req_<unsafe>",
          traceId: "trace_must_not_render",
        });
      },
    }),
  });

  await controller.login({ email: "owner@example.com", password: "PrivatePassword" });
  assert.doesNotMatch(root.innerHTML, /<img|trace_must_not_render|PrivatePassword/u);
  assert.match(root.innerHTML, /&lt;img src=x onerror=unsafe\(\)&gt;/u);
  assert.match(root.innerHTML, /request_id: req_&lt;unsafe&gt;/u);
});

test("identity page keeps keyboard, focus, and mobile layout contracts", async () => {
  const [html, css] = await Promise.all([
    readFile(new URL("../../identity.html", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/identity.css", import.meta.url), "utf8"),
  ]);
  const root = createRoot();
  renderIdentitySecurityPage(root, {
    accountSection: "password",
    account: account(),
    currentSession: session({ session_id: SESSION_ONE, is_current: true }),
    sessions: [session({ session_id: SESSION_ONE, is_current: true })],
  });

  assert.match(html, /name="viewport"/u);
  assert.match(html, /name="referrer" content="no-referrer"/u);
  assert.match(css, /:focus-visible/u);
  assert.match(css, /@media \(max-width: 520px\)/u);
  assert.match(css, /min-width: 320px/u);
  assert.match(css, /\.identity-billing-table-wrap,\s*\.identity-invoice-table-wrap\s*\{[^}]*contain:\s*inline-size paint/su);
  assert.match(css, /\.identity-billing-table-wrap\s*\{[^}]*overflow-x:\s*auto/su);
  assert.match(css, /\.identity-invoice-table-wrap\s*\{[^}]*overflow-x:\s*auto/su);
  assert.doesNotMatch(css, /\.identity-payment-/u);
  assert.match(root.innerHTML, /type="button" data-identity-action="logout"/u);
  assert.match(root.innerHTML, /type="submit"/u);
});

function installFetch(responseFactory) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return responseFactory(url, options);
  };
  return calls;
}

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    headers: new Map(),
    async json() {
      return structuredClone(payload);
    },
  };
}

function accountSessionResponse() {
  return jsonResponse(accountSessionPayload());
}

function sessionListResponse() {
  return jsonResponse(sessionListPayload());
}

function accountSessionPayload() {
  return {
    account: account(),
    current_session: session({ session_id: SESSION_ONE, is_current: true }),
    request_id: "req_identity",
    trace_id: "trc_identity",
  };
}

function sessionListPayload(customSessions = null) {
  const sessions = customSessions ?? [
    session({ session_id: SESSION_ONE, is_current: true }),
    session({ session_id: SESSION_TWO, is_current: false, device_family: "firefox" }),
  ];
  return {
    sessions,
    limit: 20,
    offset: 0,
    total: sessions.length,
    request_id: "req_sessions",
    trace_id: "trc_sessions",
  };
}

function workspaceDetailPayload(customWorkspace = null) {
  return {
    workspace: customWorkspace ?? workspace(),
    request_id: "req_workspace",
    trace_id: "trc_workspace",
  };
}

function workspaceListPayload(customWorkspaces = null) {
  const workspaces = customWorkspaces ?? [workspace()];
  return {
    workspaces,
    limit: 20,
    offset: 0,
    total: workspaces.length,
    request_id: "req_workspaces",
    trace_id: "trc_workspaces",
  };
}

function pendingChallengePayload(purpose) {
  return {
    intent_id:
      purpose === "registration"
        ? "rgi_11111111111111111111111111111111"
        : "pri_11111111111111111111111111111111",
    purpose,
    status: "pending",
    expires_at: "2026-08-21T01:15:00Z",
    resend_available_at: "2026-08-21T01:01:00Z",
    request_id: "req_pending",
    trace_id: "trc_pending",
  };
}

function logoutPayload() {
  return { logged_out: true, request_id: "req_logout", trace_id: "trc_logout" };
}

function revokePayload() {
  return { revoked: true, request_id: "req_revoke", trace_id: "trc_revoke" };
}

function rotationPayload() {
  return {
    rotated: true,
    account: account(),
    current_session: session({
      session_id: "gis_33333333333333333333333333333333",
      is_current: true,
    }),
    request_id: "req_rotate",
    trace_id: "trc_rotate",
  };
}

function account(overrides = {}) {
  return {
    email: "owner@example.com",
    display_name: "Owner",
    status: "active",
    mfa_configured: false,
    ...overrides,
  };
}

function session(overrides = {}) {
  return {
    session_id: SESSION_ONE,
    device_family: "chrome",
    created_at: "2026-08-20T01:00:00Z",
    last_seen_at: "2026-08-20T02:00:00Z",
    expires_at: "2026-08-21T01:00:00Z",
    is_current: false,
    assurance_level: "password",
    auth_method: "password",
    trusted_device_id: null,
    revoke_generation: 1,
    ...overrides,
  };
}

function workspace(overrides = {}) {
  return {
    workspace_id: WORKSPACE_ONE,
    kind: "organization",
    status: "active",
    profile_state: "confirmed",
    name: "Acme AI",
    slug: "acme-ai",
    region: "cn-north-1",
    service_mode: "internal_only",
    environment: { code: "dev", status: "active", origin: "system_created" },
    provisioning: { status: "core_committed" },
    revision: 1,
    ...overrides,
  };
}

function workingApi(overrides = {}) {
  return {
    login: async () => accountSessionPayload(),
    register: async () => pendingChallengePayload("registration"),
    verify: async () => accountSessionPayload(),
    resend: async () => pendingChallengePayload("registration"),
    requestReset: async () => pendingChallengePayload("password_reset"),
    confirmReset: async () => accountSessionPayload(),
    me: async () => accountSessionPayload(),
    sessions: async () => sessionListPayload(),
    workspaces: async () => workspaceListPayload(),
    createWorkspace: async () => workspaceDetailPayload(),
    revoke: async () => revokePayload(),
    logout: async () => logoutPayload(),
    rotate: async () => rotationPayload(),
    ...overrides,
  };
}

function createRoot(passwordInputs = [], codeInputs = []) {
  return {
    innerHTML: "",
    addEventListener() {},
    contains() {
      return true;
    },
    querySelectorAll(selector) {
      if (selector === 'input[type="password"]') return passwordInputs;
      if (selector === 'input[name="code"]') return codeInputs;
      if (selector === 'input[autocomplete="one-time-code"]') return codeInputs;
      return [];
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

test("registration exposes eyes, privacy, captcha and immediate duplicate-safe pending state", async () => {
  const root = createRoot();
  const pending = deferred();
  let calls = 0;
  const controller = createIdentityPageController({ root, api: workingApi({
    registrationPolicy: async () => ({ human_verification: true, email_verification: false }),
    registrationCaptcha: async () => ({ challenge_id: `cap_${"a".repeat(32)}`,
      image: "data:image/png;base64,aGVsbG8=" }),
    register: async (input) => {
      calls++;
      assert.equal(input.captchaAnswer, "23456");
      return pending.promise;
    },
  }) });
  await controller.showRegistration();
  assert.equal((root.innerHTML.match(/data-password-toggle/gu) ?? []).length, 2);
  assert.match(root.innerHTML, /href="\/privacy.html"/u);
  assert.match(root.innerHTML, /刷新验证码/u);
  const input = { email: "trial@example.test", displayName: "Trial", password: "TrialSecret!2026",
    confirmPassword: "TrialSecret!2026", consent: true, captchaAnswer: "23456" };
  const first = controller.registerAccount(input);
  assert.match(root.innerHTML, /aria-busy="true"/u);
  assert.match(root.innerHTML, /正在注册/u);
  assert.doesNotMatch(root.innerHTML, /TrialSecret/u);
  assert.equal(await controller.registerAccount(input), false);
  pending.resolve(accountSessionPayload());
  assert.equal(await first, true);
  assert.equal(calls, 1);
  assert.match(root.innerHTML, /注册成功/u);
  assert.doesNotMatch(root.innerHTML, /邮箱已验证/u);
});

test("stale registration setup cannot replace login or restore captcha", async () => {
  const root = createRoot();
  const policy = deferred();
  const controller = createIdentityPageController({ root, api: workingApi({
    registrationPolicy: () => policy.promise,
  }) });
  const loading = controller.mount({ registration: true });
  controller.showLogin();
  policy.resolve({ human_verification: false, email_verification: false });
  assert.equal(await loading, false);
  assert.match(root.innerHTML, /data-identity-login-form/u);
});

test("registration keeps required fields usable while policy is loading", async () => {
  const root = createRoot();
  const policy = deferred();
  const events = new Map();
  root.addEventListener = (name, handler) => events.set(name, handler);
  const controller = createIdentityPageController({ root, api: workingApi({
    registrationPolicy: () => policy.promise,
  }) });
  const loading = controller.mount({ registration: true });
  assert.match(root.innerHTML, /identity-registration-fields/iu);
  assert.match(root.innerHTML, /正在准备本地注册验证/iu);
  assert.doesNotMatch(root.innerHTML, /<fieldset[^>]*disabled/iu);
  assert.match(root.innerHTML, /<button class="identity-primary" type="submit" disabled>/u);
  events.get("input")({ target: {
    name: "email", value: "draft@example.test",
    closest: (selector) => selector === "[data-identity-registration-form]",
  } });
  events.get("input")({ target: {
    name: "displayName", value: "Draft owner",
    closest: (selector) => selector === "[data-identity-registration-form]",
  } });
  policy.resolve({ human_verification: false, email_verification: false });
  assert.equal(await loading, true);
  assert.match(root.innerHTML, /value="draft@example\.test"/u);
  assert.match(root.innerHTML, /value="Draft owner"/u);
  assert.doesNotMatch(root.innerHTML, /正在准备本地注册验证/iu);
  assert.doesNotMatch(root.innerHTML, /<button class="identity-primary" type="submit" disabled>/u);
});

test("new accounts start with organization creation and secondary account security", async () => {
  const root = createRoot();
  const controller = createIdentityPageController({ root, api: workingApi({
    workspaces: async () => ({ workspaces: [] }),
  }) });
  await controller.mount();
  assert.match(root.innerHTML, /identity-organization-main/u);
  assert.match(root.innerHTML, /创建组织/u);
  assert.doesNotMatch(root.innerHTML, /账号保护状态|通知中心|空间类型|我的空间/u);
  await controller.showAccountSecurity();
  assert.match(root.innerHTML, /账号保护状态|返回组织开通/u);
  controller.showOrganizationStart();
  assert.match(root.innerHTML, /identity-organization-main/u);
  controller.showWorkspaceCreate();
  assert.match(root.innerHTML, /data-workspace-create-form/u);
  assert.doesNotMatch(root.innerHTML, /data-workspace-kind="personal"/u);
  controller.cancelWorkspaceCreate();
  assert.doesNotMatch(root.innerHTML, /data-workspace-create-form/u);
  controller.destroy();
});

test("failed workspace discovery never masquerades as an empty organization list", async () => {
  const root = createRoot();
  const controller = createIdentityPageController({ root, api: workingApi({
    workspaces: async () => { throw new IdentityApiError({ code: "workspace.unavailable", message: "暂时不可用" }); },
  }) });
  await controller.mount();
  assert.doesNotMatch(root.innerHTML, /identity-organization-main/u);
  assert.match(root.innerHTML, /暂时不可用/u);
  controller.destroy();
});

test("organization creation prevents duplicate writes and restores failed drafts", async () => {
  const root = createRoot();
  const pending = deferred();
  let calls = 0;
  const controller = createIdentityPageController({ root, api: workingApi({
    workspaces: async () => ({ workspaces: [] }),
    createWorkspace: async () => { calls++; await pending.promise; throw new IdentityApiError({
      code: "workspace.unavailable", message: "稍后重试", requestId: "req-test",
    }); },
  }) });
  await controller.mount();
  controller.showWorkspaceCreate();
  const fields = { kind: "organization", name: "<Trial>", slug: "trial-company",
    region: "cn-north-1", serviceMode: "internal_only" };
  const first = controller.createWorkspace(fields);
  assert.match(root.innerHTML, /正在创建|aria-busy="true"/u);
  assert.equal(await controller.createWorkspace(fields), false);
  assert.equal(controller.cancelWorkspaceCreate(), false);
  assert.equal(controller.showAccountSecurity(), false);
  pending.resolve();
  assert.equal(await first, false);
  assert.equal(calls, 1);
  assert.match(root.innerHTML, /data-workspace-create-form|稍后重试/u);
  assert.match(root.innerHTML, /&lt;Trial&gt;/u);
  assert.doesNotMatch(root.innerHTML, /<Trial>|aria-busy="true"/u);
  controller.destroy();
});

for (const scenario of ["ready", "permission-drift", "logout", "other-workspace", "switch-failed"]) {
  test(`organization console entry checks authorization before redirect: ${scenario}`, async () => {
    const root = createRoot();
    const sessionStarted = deferred();
    const pendingSession = deferred();
    let created = false;
    let switched = false;
    let redirected = 0;
    let auxiliaryReads = 0;
    const authorization = {
      permission_codes: [], permission_revision: scenario === "permission-drift" ? 0 : 1, membership_revision: 1,
      permission_fingerprint: "f".repeat(64),
    };
    const controller = createIdentityPageController({
      root, channelFactory: () => null,
      onWorkspaceCreated: () => { redirected++; },
      api: workingApi({
        workspaces: async () => workspaceListPayload(created ? [workspace()] : []),
        identityWorkspaces: async () => ({
          workspaces: created ? [{ ...workspace(), role: "owner" }] : [],
        }),
        createWorkspace: async () => { created = true; return workspaceDetailPayload(); },
        switchWorkspace: async () => {
          if (scenario === "switch-failed") throw new IdentityApiError({ status: 503 });
          switched = true;
          return {};
        },
        workspaceSession: async () => {
          if (!switched) throw new IdentityApiError({ status: 401 });
          const result = {
            context: { ...workspace(), role: "owner",
              workspace_id: scenario === "other-workspace" ? "wsp_other" : WORKSPACE_ONE },
            authorization,
          };
          sessionStarted.resolve(result);
          return scenario === "logout" ? pendingSession.promise : result;
        },
        memberships: async () => { auxiliaryReads++; return { memberships: [] }; },
        workspaceNavigation: async () => { auxiliaryReads++; return {}; },
        onboarding: async () => { auxiliaryReads++; return {}; },
        notifications: async () => { auxiliaryReads++; return {}; },
      }),
    });
    try {
      await controller.mount();
      controller.showWorkspaceCreate();
      const creating = controller.createWorkspace({
        kind: "organization", name: "Trial", slug: "trial-company",
        region: "cn-north-1", serviceMode: "internal_only",
      });
      if (scenario === "logout") {
        const session = await sessionStarted.promise;
        await controller.logout();
        pendingSession.resolve(session);
      }
      assert.equal(await creating, scenario === "ready");
      assert.equal(redirected, scenario === "ready" ? 1 : 0);
      assert.equal(auxiliaryReads, 0);
      if (scenario === "logout") assert.match(root.innerHTML, /data-identity-login-form/u);
    } finally { controller.destroy(); }
  });
}

function restoreProperty(name, descriptor) {
  if (descriptor) {
    Object.defineProperty(globalThis, name, descriptor);
  } else {
    delete globalThis[name];
  }
}
