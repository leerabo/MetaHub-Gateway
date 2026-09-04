import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { renderIdentitySecurityPage } from "../../src/features/identity/renderIdentityPage.js";
import {
  IdentityApiError,
  beginIdentityAccountRecovery,
  beginIdentityWebAuthnStepUp,
  completeIdentityAccountRecovery,
  completeIdentityWebAuthnRegistration,
  createIdentityMfaEnrollment,
  createIdentityStepUpGrant,
  createWebAuthnCredential,
  getIdentitySecurity,
  getWebAuthnAssertion,
  revokeIdentityTrustedDevice,
  trustIdentityDevice,
} from "../../src/lib/identity.js";
import {
  getWorkspaceConditionalAccess,
  simulateWorkspaceConditionalAccess,
  updateWorkspaceConditionalAccess,
} from "../../src/lib/conditionalAccess.js";

const CONTROL_URL = "http://control.local";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const MEMBERSHIP_ID = `wmb_${"2".repeat(32)}`;
const SESSION_ID = `gis_${"3".repeat(32)}`;
const FACTOR_ID = `mfa_${"4".repeat(32)}`;
const DEVICE_ID = `trd_${"5".repeat(32)}`;
const CHALLENGE_ID = `mfc_${"6".repeat(32)}`;
const GRANT_ID = `sug_${"7".repeat(32)}`;
const POLICY_ID = `cap_${"8".repeat(32)}`;
const AUTHORIZATION_FINGERPRINT = "f".repeat(64);
const SIMULATION_ID = `cas_${"9".repeat(32)}`;
const RECOVERY_CODES = Object.freeze(Array.from({ length: 10 }, (_, index) => {
  const marker = "ABCDEFGHJKLM"[index];
  return ["MHRC", `TEST${marker}`, `SAFE${marker}`].join("-");
}));

test("PRD-116 identity clients keep MFA, device, and step-up secrets in exact cookie requests", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = {
    cookie: "__Host-metahub-global-csrf=global-csrf; __Host-metahub-workspace-csrf=workspace-csrf",
  };
  const calls = installFetch((url) => {
    const path = new URL(String(url)).pathname;
    if (path.endsWith("/mfa/enrollments")) return jsonResponse(recoveryEnrollmentPayload());
    if (path.endsWith("/mfa/challenges")) return jsonResponse(webauthnChallengePayload());
    if (path.endsWith("/trusted-devices")) return jsonResponse(trustedDeviceResponse());
    if (path.includes("/trusted-devices/")) return jsonResponse(revokePayload());
    if (path.endsWith("/step-up")) return jsonResponse(stepUpPayload());
    return jsonResponse(identitySecurityPayload());
  });
  try {
    const enrollment = await createIdentityMfaEnrollment({
      controlApiUrl: CONTROL_URL,
      method: "recovery_codes",
      currentPassword: "PrivateCurrentPassword",
    });
    await getIdentitySecurity({ controlApiUrl: CONTROL_URL });
    await beginIdentityWebAuthnStepUp({ controlApiUrl: CONTROL_URL });
    await trustIdentityDevice({
      controlApiUrl: CONTROL_URL,
      label: "Work laptop",
      totpCode: "123456",
    });
    await revokeIdentityTrustedDevice({
      controlApiUrl: CONTROL_URL,
      trustedDeviceId: DEVICE_ID,
    });
    const grant = await createIdentityStepUpGrant({
      controlApiUrl: CONTROL_URL,
      method: "recovery_code",
      workspaceId: WORKSPACE_ID,
      actionCode: "conditional_access.manage",
      resourceType: "conditional_access_policy",
      resourceId: WORKSPACE_ID,
      code: RECOVERY_CODES[0],
    });

    assert.deepEqual(enrollment.recovery_codes, RECOVERY_CODES);
    assert.equal(grant.grant, "G".repeat(43));
    assert.deepEqual(
      calls.map(({ url, options }) => [new URL(url).pathname, options.method]),
      [
        ["/identity/mfa/enrollments", "POST"],
        ["/identity/security", "GET"],
        ["/identity/mfa/challenges", "POST"],
        ["/identity/trusted-devices", "POST"],
        [`/identity/trusted-devices/${DEVICE_ID}`, "DELETE"],
        ["/identity/step-up", "POST"],
      ],
    );
    assert.equal(calls[0].options.headers["X-CSRF-Token"], "global-csrf");
    assert.equal(calls[1].options.headers["X-CSRF-Token"], undefined);
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      method: "recovery_codes",
      current_password: "PrivateCurrentPassword",
    });
    assert.deepEqual(JSON.parse(calls[5].options.body), {
      method: "recovery_code",
      workspace_id: WORKSPACE_ID,
      action_code: "conditional_access.manage",
      resource_type: "conditional_access_policy",
      resource_id: WORKSPACE_ID,
      code: RECOVERY_CODES[0],
    });
    for (const call of calls) {
      assert.equal(call.options.credentials, "include");
      assert.equal(call.options.cache, "no-store");
      assert.equal("Authorization" in call.options.headers, false);
    }
  } finally {
    globalThis.document = originalDocument;
  }
});

test("PRD-116 WebAuthn adapters decode options and serialize registration and assertion bytes", async () => {
  const creationCalls = [];
  const registration = await createWebAuthnCredential(
    {
      rp: { id: "control.local", name: "MetaHub" },
      user: { id: "AQID", name: "owner@example.com", displayName: "Owner" },
      challenge: "BAUG",
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      timeout: 300000,
      excludeCredentials: [{ type: "public-key", id: "BwgJ" }],
      authenticatorSelection: { userVerification: "required" },
      attestation: "none",
    },
    {
      async create(options) {
        creationCalls.push(options);
        return registrationCredential();
      },
    },
  );
  assert.deepEqual([...creationCalls[0].publicKey.challenge], [4, 5, 6]);
  assert.deepEqual([...creationCalls[0].publicKey.user.id], [1, 2, 3]);
  assert.deepEqual([...creationCalls[0].publicKey.excludeCredentials[0].id], [7, 8, 9]);
  assert.equal(registration.type, "public-key");
  assert.equal(registration.rawId, "AQIDBA");
  assert.equal(registration.response.attestationObject, "CAkK");

  const assertionCalls = [];
  const assertion = await getWebAuthnAssertion(
    {
      challenge: "CwwN",
      rpId: "control.local",
      timeout: 300000,
      userVerification: "required",
      allowCredentials: [{ type: "public-key", id: "Dg8Q" }],
    },
    {
      async get(options) {
        assertionCalls.push(options);
        return assertionCredential();
      },
    },
  );
  assert.deepEqual([...assertionCalls[0].publicKey.challenge], [11, 12, 13]);
  assert.deepEqual([...assertionCalls[0].publicKey.allowCredentials[0].id], [14, 15, 16]);
  assert.equal(assertion.response.authenticatorData, "ERIT");
  assert.equal(assertion.response.signature, "FBUW");
});

test("PRD-116 WebAuthn completion client sends only challenge and browser credential", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-global-csrf=global-csrf" };
  const calls = installFetch(() => jsonResponse(webauthnCompletionPayload()));
  try {
    const result = await completeIdentityWebAuthnRegistration({
      controlApiUrl: CONTROL_URL,
      challengeId: CHALLENGE_ID,
      credential: registrationCredentialJson(),
    });
    assert.equal(result.factor_type, "webauthn");
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      operation: "complete_registration",
      challenge_id: CHALLENGE_ID,
      credential: registrationCredentialJson(),
    });
  } finally {
    globalThis.document = originalDocument;
  }
});

test("PRD-116 controlled recovery client sends exact delayed dual-proof requests", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-global-csrf=global-csrf" };
  const calls = installFetch((_url, options) => {
    const operation = JSON.parse(options.body).operation;
    return jsonResponse(operation === "begin_recovery"
      ? recoveryChallengePayload()
      : recoveryCompletionPayload());
  });
  try {
    const pending = await beginIdentityAccountRecovery({
      controlApiUrl: CONTROL_URL,
      currentPassword: "PrivateCurrentPassword",
    });
    const completed = await completeIdentityAccountRecovery({
      controlApiUrl: CONTROL_URL,
      challengeId: pending.challenge_id,
      notificationCode: "123456",
      recoveryCode: RECOVERY_CODES[1],
    });
    assert.equal(pending.status, "pending");
    assert.equal(completed.assurance_level, "recovery");
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      operation: "begin_recovery",
      current_password: "PrivateCurrentPassword",
    });
    assert.deepEqual(JSON.parse(calls[1].options.body), {
      operation: "complete_recovery",
      challenge_id: CHALLENGE_ID,
      notification_code: "123456",
      recovery_code: RECOVERY_CODES[1],
    });
    for (const call of calls) {
      assert.equal(call.options.credentials, "include");
      assert.equal(call.options.cache, "no-store");
      assert.equal(call.options.headers["X-CSRF-Token"], "global-csrf");
      assert.equal("Authorization" in call.options.headers, false);
    }
  } finally {
    globalThis.document = originalDocument;
  }
});

test("PRD-116 conditional-access client binds CSRF and one-shot grant only to PATCH", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace-csrf" };
  const calls = installFetch((_url, options) => options.method === "POST"
    ? jsonResponse(conditionalSimulationPayload())
    : jsonResponse(conditionalPolicyPayload()));
  const stepUpGrant = "S".repeat(43);
  try {
    await getWorkspaceConditionalAccess({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID });
    await simulateWorkspaceConditionalAccess({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
    });
    await updateWorkspaceConditionalAccess({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      policy: {
        expectedRevision: 1,
        mode: "audit",
        requireMfa: true,
        requireStepUpForHighRisk: true,
        allowTrustedDevices: false,
        regionRule: "any",
        networkRule: "trusted_only",
        allowedCidrs: ["127.0.0.0/8"],
        exceptionMembershipIds: [MEMBERSHIP_ID],
      },
      stepUpGrant,
    });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[0].options.headers["X-Step-Up-Grant"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[1].options.headers["X-Step-Up-Grant"], undefined);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[2].options.headers["X-Step-Up-Grant"], stepUpGrant);
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    expected_revision: 1,
    mode: "audit",
    require_mfa: true,
    require_step_up_for_high_risk: true,
    allow_trusted_devices: false,
    region_rule: "any",
    network_rule: "trusted_only",
    allowed_cidrs: ["127.0.0.0/8"],
    exception_membership_ids: [MEMBERSHIP_ID],
  });
});

test("PRD-116 renderer shows security facts without credential or grant identifiers", () => {
  const root = createRoot();
  renderIdentitySecurityPage(root, {
    account: account(),
    currentSession: session(),
    sessions: [session()],
    identitySecurity: identitySecurityProjection(),
    mfaEnrollmentMethod: "webauthn",
    factorDisableIndex: null,
    recoveryCodes: RECOVERY_CODES,
    platformAccess: false,
    workspaces: [],
    workspaceChoices: [],
    workspaceContext: workspaceContext(),
    memberships: [membership()],
    navigationItems: [],
    conditionalAccessVisible: true,
    conditionalAccessCanManage: true,
    conditionalAccess: conditionalProjection(),
  });

  assert.match(root.innerHTML, /验证因子与恢复/u);
  assert.match(root.innerHTML, /受信设备/u);
  assert.match(root.innerHTML, /条件访问/u);
  assert.match(root.innerHTML, /data-identity-recovery-dialog/u);
  assert.match(root.innerHTML, /data-identity-recovery-begin-form/u);
  assert.doesNotMatch(root.innerHTML, /data-identity-factor-disable-form/u);
  assert.match(root.innerHTML, new RegExp(RECOVERY_CODES[0], "u"));
  for (const privateValue of [FACTOR_ID, DEVICE_ID, POLICY_ID, SIMULATION_ID, GRANT_ID]) {
    assert.doesNotMatch(root.innerHTML, new RegExp(privateValue, "u"));
  }
  assert.doesNotMatch(root.innerHTML, /X-Step-Up-Grant|credential_id|public_key|sign_count/u);
});

test("PRD-116 controller clears one-time recovery codes from DOM on close and destroy", async () => {
  const root = createRoot();
  let securityReads = 0;
  const controller = createIdentityPageController({
    root,
    api: controllerApi({
      security: async () => {
        securityReads += 1;
        return identitySecurityPayload();
      },
      createMfa: async () => recoveryEnrollmentPayload(),
    }),
  });
  await controller.mount();
  const input = { currentPassword: "PrivateCurrentPassword", method: "recovery_codes" };
  assert.equal(await controller.beginMfaEnrollment(input), true);
  input.currentPassword = "";
  assert.match(root.innerHTML, new RegExp(RECOVERY_CODES[0], "u"));
  controller.closeRecoveryCodes();
  assert.doesNotMatch(root.innerHTML, new RegExp(RECOVERY_CODES[0], "u"));
  assert.ok(securityReads >= 2);
  controller.destroy();
  assert.equal(root.innerHTML, "");
});

test("PRD-116 controller keeps delayed recovery proofs transient and reloads the rotated session", async () => {
  const root = createRoot();
  const writes = [];
  let projection = identitySecurityPayload();
  let current = session();
  const controller = createIdentityPageController({
    root,
    api: controllerApi({
      me: async () => ({ account: account(), current_session: current }),
      sessions: async () => ({ sessions: [current] }),
      security: async () => projection,
      beginRecovery: async (input) => {
        writes.push(["begin", { ...input }]);
        const response = recoveryChallengePayload();
        projection = {
          ...projection,
          recovery_challenge: recoveryChallengeProjection(),
        };
        return response;
      },
      completeRecovery: async (input) => {
        writes.push(["complete", { ...input }]);
        current = recoverySession();
        projection = recoveredIdentitySecurityPayload();
        return recoveryCompletionPayload();
      },
    }),
  });
  await controller.mount();
  assert.match(root.innerHTML, /data-identity-recovery-begin-form/u);
  assert.equal(await controller.beginAccountRecovery({
    currentPassword: "PrivateCurrentPassword",
    confirmed: true,
  }), true);
  assert.match(root.innerHTML, /data-identity-recovery-complete-form/u);
  assert.doesNotMatch(root.innerHTML, /PrivateCurrentPassword/u);

  const input = {
    notificationCode: "123456",
    recoveryCode: RECOVERY_CODES[2],
    confirmed: true,
  };
  assert.equal(await controller.completeAccountRecovery(input), true);
  assert.equal(input.notificationCode, "");
  assert.equal(input.recoveryCode, "");
  assert.equal(writes[1][1].notificationCode, "123456");
  assert.equal(writes[1][1].recoveryCode, RECOVERY_CODES[2]);
  assert.doesNotMatch(root.innerHTML, /123456|MHRC-[A-Z0-9]{5}-[A-Z0-9]{5}/u);
  assert.match(root.innerHTML, /恢复保证/u);
  assert.match(root.innerHTML, /data-identity-mfa-enrollment-form/u);
  controller.destroy();
});

test("PRD-116 recovery assurance requires an explicit workspace re-entry", async () => {
  const root = createRoot();
  let workspaceSessionReads = 0;
  let workspaceSwitches = 0;
  const controller = createIdentityPageController({
    root,
    api: controllerApi({
      me: async () => ({ account: account(), current_session: recoverySession() }),
      sessions: async () => ({ sessions: [recoverySession()] }),
      security: async () => recoveredIdentitySecurityPayload(),
      identityWorkspaces: async () => ({ workspaces: [workspaceChoice()] }),
      workspaceSession: async () => {
        workspaceSessionReads += 1;
        throw new IdentityApiError({
          status: 401,
          code: "workspace.session_missing",
          message: "Workspace session is unavailable.",
        });
      },
      switchWorkspace: async () => {
        workspaceSwitches += 1;
        return workspaceSessionResponse();
      },
    }),
  });

  await controller.mount();

  assert.equal(workspaceSessionReads, 1);
  assert.equal(workspaceSwitches, 0);
  assert.match(root.innerHTML, /data-identity-action="switch-workspace"/u);
  assert.doesNotMatch(root.innerHTML, /data-conditional-access-form/u);
  controller.destroy();
});

test("PRD-116 controller consumes step-up inline and scrubs proof and grant from DOM", async () => {
  const root = createRoot();
  const writes = [];
  const activity = [];
  let workspaceSessionReads = 0;
  const controller = createIdentityPageController({
    root,
    api: controllerApi({
      identityWorkspaces: async () => ({ workspaces: [workspaceChoice()] }),
      workspaceSession: async () => {
        workspaceSessionReads += 1;
        return workspaceSessionResponse();
      },
      switchWorkspace: async ({ workspaceId }) => {
        activity.push(["switch_workspace", workspaceId]);
        return { context: workspaceSessionContext() };
      },
      memberships: async () => ({ memberships: [membership()] }),
      workspaceNavigation: async () => workspaceNavigationProjection(),
      conditionalAccess: async () => {
        activity.push(["conditional_read", WORKSPACE_ID]);
        return conditionalAccessProjection();
      },
      stepUp: async (input) => {
        activity.push(["step_up", input.workspaceId]);
        writes.push(["step_up", { ...input }]);
        return stepUpPayload();
      },
      updateConditionalAccess: async (input) => {
        activity.push(["policy", input.workspaceId]);
        writes.push(["policy", { ...input }]);
        return conditionalPolicyPayload();
      },
    }),
  });
  await controller.mount();
  assert.match(root.innerHTML, /data-conditional-access-form/u);
  activity.length = 0;
  const input = {
    mode: "audit",
    requireMfa: true,
    requireStepUpForHighRisk: true,
    allowTrustedDevices: false,
    regionRule: "any",
    networkRule: "any",
    allowedCidrs: "",
    exceptionMembershipIds: [],
    method: "totp",
    code: "123456",
    confirmed: false,
  };
  assert.equal(await controller.saveConditionalAccess(input), true);
  assert.equal(input.code, "");
  assert.equal(writes[0][1].code, "123456");
  assert.equal(writes[1][1].stepUpGrant, "G".repeat(43));
  assert.deepEqual(activity, [
    ["step_up", WORKSPACE_ID],
    ["policy", WORKSPACE_ID],
    ["switch_workspace", WORKSPACE_ID],
    ["conditional_read", WORKSPACE_ID],
  ]);
  assert.equal(workspaceSessionReads, 2);
  assert.doesNotMatch(root.innerHTML, /123456|GGGGGGGG/u);
  controller.destroy();
});

test("PRD-146 Identity trusts only WorkspaceSession permissions and fails closed on drift", async () => {
  let conditionalReads = 0;
  const legacyRoot = createRoot();
  const legacyController = createIdentityPageController({
    root: legacyRoot,
    api: controllerApi({
      identityWorkspaces: async () => ({ workspaces: [workspaceChoice()] }),
      workspaceSession: async () => workspaceSessionResponse([]),
      memberships: async () => ({ memberships: [membership()] }),
      workspaceNavigation: async () => ({
        ...workspaceNavigationProjection(),
        permission_codes: ["conditional_access.read", "conditional_access.manage"],
      }),
      conditionalAccess: async () => {
        conditionalReads += 1;
        return conditionalAccessProjection();
      },
    }),
  });

  assert.equal(await legacyController.mount(), true);
  assert.equal(conditionalReads, 0);
  assert.doesNotMatch(legacyRoot.innerHTML, /data-conditional-access-form/u);
  legacyController.destroy();

  let sessionReads = 0;
  const driftRoot = createRoot();
  const driftController = createIdentityPageController({
    root: driftRoot,
    api: controllerApi({
      identityWorkspaces: async () => ({ workspaces: [workspaceChoice()] }),
      workspaceSession: async () => {
        sessionReads += 1;
        return workspaceSessionResponse();
      },
      memberships: async () => ({ memberships: [membership()] }),
      workspaceNavigation: async () => ({
        ...workspaceNavigationProjection(),
        permission_fingerprint: "0".repeat(64),
      }),
      conditionalAccess: async () => {
        conditionalReads += 1;
        return conditionalAccessProjection();
      },
    }),
  });

  assert.equal(await driftController.mount(), false);
  assert.equal(sessionReads, 2);
  assert.equal(conditionalReads, 0);
  assert.doesNotMatch(driftRoot.innerHTML, /data-conditional-access-form/u);
  assert.match(driftRoot.innerHTML, /空间权限已变化/u);
  driftController.destroy();
});

test("PRD-116 identity entry destroys sensitive state on pagehide and clients avoid browser storage", async () => {
  const main = await readFile(new URL("../../src/features/identity/main.js", import.meta.url), "utf8");
  const identityClient = await readFile(new URL("../../src/lib/identity.js", import.meta.url), "utf8");
  const conditionalClient = await readFile(
    new URL("../../src/lib/conditionalAccess.js", import.meta.url),
    "utf8",
  );
  assert.match(main, /addEventListener\("pagehide", \(\) => controller\.destroy\(\)/u);
  assert.doesNotMatch(identityClient, /localStorage|sessionStorage/u);
  assert.doesNotMatch(conditionalClient, /localStorage|sessionStorage/u);
});

function identitySecurityProjection() {
  const payload = identitySecurityPayload();
  return {
    factors: payload.factors,
    recovery_challenge: payload.recovery_challenge,
    recovery_code_set: payload.recovery_code_set,
    trusted_devices: payload.trusted_devices,
    sessions: payload.sessions,
  };
}

function conditionalProjection() {
  const payload = conditionalPolicyPayload();
  return { policy: payload.policy, latest_simulation: payload.latest_simulation };
}

function identitySecurityPayload() {
  return {
    factors: [{
      factor_id: FACTOR_ID,
      factor_type: "totp",
      status: "active",
      revision: 2,
      verified_at: "2026-08-25T01:00:00Z",
      created_at: "2026-08-25T00:55:00Z",
    }],
    recovery_code_set: {
      recovery_code_set_id: `rcs_${"a".repeat(32)}`,
      status: "active",
      revision: 1,
      remaining_count: 10,
      generated_at: "2026-08-25T01:05:00Z",
    },
    recovery_challenge: null,
    trusted_devices: [trustedDevice()],
    sessions: [session()],
    request_id: "req_security",
    trace_id: "trc_security",
  };
}

function recoveryEnrollmentPayload() {
  return {
    method: "recovery_codes",
    factor_id: null,
    challenge_id: null,
    public_key_options: null,
    secret: null,
    provisioning_uri: null,
    recovery_codes: [...RECOVERY_CODES],
    expires_at: null,
    request_id: "req_recovery",
    trace_id: "trc_recovery",
  };
}

function webauthnChallengePayload() {
  return {
    challenge_id: CHALLENGE_ID,
    purpose: "step_up",
    method: "webauthn",
    public_key_options: {
      challenge: "AQID",
      rpId: "control.local",
      timeout: 300000,
      userVerification: "required",
    },
    expires_at: "2026-08-25T01:10:00Z",
    request_id: "req_challenge",
    trace_id: "trc_challenge",
  };
}

function recoveryChallengePayload() {
  return {
    challenge_id: CHALLENGE_ID,
    purpose: "recovery",
    status: "pending",
    ready_at: "2026-08-26T01:10:00Z",
    expires_at: "2026-08-27T01:10:00Z",
    request_id: "req_recovery_begin",
    trace_id: "trc_recovery_begin",
  };
}

function recoveryChallengeProjection() {
  const payload = recoveryChallengePayload();
  return {
    challenge_id: payload.challenge_id,
    status: payload.status,
    ready_at: payload.ready_at,
    expires_at: payload.expires_at,
  };
}

function recoveryCompletionPayload() {
  return {
    recovered: true,
    assurance_level: "recovery",
    completed_at: "2026-08-26T01:10:00Z",
    request_id: "req_recovery_complete",
    trace_id: "trc_recovery_complete",
  };
}

function recoveredIdentitySecurityPayload() {
  const payload = identitySecurityPayload();
  return {
    ...payload,
    factors: payload.factors.map((factor) => ({
      ...factor,
      status: "disabled",
      revision: factor.revision + 1,
    })),
    recovery_challenge: null,
    recovery_code_set: {
      ...payload.recovery_code_set,
      status: "revoked",
      revision: payload.recovery_code_set.revision + 1,
      remaining_count: 9,
    },
    trusted_devices: payload.trusted_devices.map((device) => ({
      ...device,
      status: "revoked",
      revision: device.revision + 1,
    })),
    sessions: [recoverySession()],
  };
}

function webauthnCompletionPayload() {
  return {
    factor_id: FACTOR_ID,
    factor_type: "webauthn",
    status: "active",
    revision: 2,
    verified_at: "2026-08-25T01:06:00Z",
    request_id: "req_complete",
    trace_id: "trc_complete",
  };
}

function trustedDeviceResponse() {
  return {
    trusted_device: trustedDevice(),
    request_id: "req_device",
    trace_id: "trc_device",
  };
}

function trustedDevice() {
  return {
    trusted_device_id: DEVICE_ID,
    label: "Work laptop",
    device_family: "chrome",
    trust_level: "mfa_verified",
    status: "active",
    revision: 1,
    expires_at: "2026-09-24T01:00:00Z",
    last_used_at: "2026-08-25T01:00:00Z",
    created_at: "2026-08-25T01:00:00Z",
  };
}

function stepUpPayload() {
  return {
    grant: "G".repeat(43),
    grant_id: GRANT_ID,
    action_code: "conditional_access.manage",
    resource_type: "conditional_access_policy",
    resource_id: WORKSPACE_ID,
    auth_method: "recovery_code",
    assurance_level: "recovery",
    expires_at: "2026-08-25T01:10:00Z",
    request_id: "req_step_up",
    trace_id: "trc_step_up",
  };
}

function conditionalPolicyPayload() {
  return {
    policy: conditionalPolicy(),
    latest_simulation: conditionalSimulation(),
    request_id: "req_policy",
    trace_id: "trc_policy",
  };
}

function conditionalSimulationPayload() {
  return {
    policy: conditionalPolicy(),
    simulation: conditionalSimulation(),
    request_id: "req_simulation",
    trace_id: "trc_simulation",
  };
}

function conditionalAccessProjection() {
  return { policy: conditionalPolicy(), latest_simulation: conditionalSimulation() };
}

function conditionalPolicy() {
  return {
    policy_id: POLICY_ID,
    workspace_id: WORKSPACE_ID,
    mode: "audit",
    require_mfa: true,
    require_step_up_for_high_risk: true,
    allow_trusted_devices: false,
    region_rule: "any",
    network_rule: "any",
    allowed_cidrs: [],
    exception_membership_ids: [],
    revision: 1,
    simulation_watermark_at: "2026-08-25T01:00:00Z",
    updated_at: "2026-08-25T01:00:00Z",
  };
}

function conditionalSimulation() {
  return {
    simulation_id: SIMULATION_ID,
    policy_revision: 1,
    outcome_code: "simulation.ready",
    affected_session_count: 1,
    owner_ready: true,
    simulated_at: "2026-08-25T01:00:00Z",
  };
}

function account() {
  return {
    email: "owner@example.com",
    display_name: "Owner",
    status: "active",
    mfa_configured: true,
  };
}

function session() {
  return {
    session_id: SESSION_ID,
    device_family: "chrome",
    created_at: "2026-08-25T00:00:00Z",
    last_seen_at: "2026-08-25T01:00:00Z",
    expires_at: "2026-08-26T00:00:00Z",
    is_current: true,
    assurance_level: "mfa",
    auth_method: "totp",
    trusted_device_id: DEVICE_ID,
    revoke_generation: 1,
  };
}

function recoverySession() {
  return {
    ...session(),
    session_id: `gis_${"d".repeat(32)}`,
    assurance_level: "recovery",
    auth_method: "recovery_code",
    trusted_device_id: null,
    revoke_generation: 1,
  };
}

function workspaceContext() {
  return {
    workspace_id: WORKSPACE_ID,
    tenant_id: `tenant_${"1".repeat(20)}`,
    kind: "organization",
    name: "Acme",
    slug: "acme",
    role: "owner",
    membership_id: MEMBERSHIP_ID,
    permission_codes: ["conditional_access.manage", "conditional_access.read"],
    permission_revision: 27,
    authorization_fingerprint: AUTHORIZATION_FINGERPRINT,
    session_id: `wss_${"b".repeat(32)}`,
    expires_at: "2026-08-26T00:00:00Z",
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
  const context = workspaceContext();
  return {
    workspace_id: context.workspace_id,
    kind: context.kind,
    name: context.name,
    slug: context.slug,
    role: context.role,
    session_status: "active",
    expires_at: context.expires_at,
  };
}

function workspaceSessionResponse(permissionCodes = workspaceContext().permission_codes) {
  const context = workspaceContext();
  return {
    context: workspaceSessionContext(),
    authorization: {
      permission_codes: [...permissionCodes],
      permission_revision: context.permission_revision,
      permission_fingerprint: context.authorization_fingerprint,
      membership_revision: 9,
    },
  };
}

function workspaceNavigationProjection() {
  const context = workspaceContext();
  return {
    groups: [],
    membership_revision: 9,
    permission_revision: context.permission_revision,
    permission_fingerprint: context.authorization_fingerprint,
  };
}

function membership() {
  return {
    membership_id: MEMBERSHIP_ID,
    display_name: "Owner",
    role: "owner",
    status: "active",
    is_current: true,
    can_manage: false,
  };
}

function controllerApi(overrides = {}) {
  return {
    me: async () => ({ account: account(), current_session: session() }),
    sessions: async () => ({ sessions: [session()] }),
    security: async () => identitySecurityPayload(),
    workspaces: async () => ({ workspaces: [] }),
    platformAccess: async () => ({ available: false }),
    logout: async () => ({ logged_out: true }),
    ...overrides,
  };
}

function registrationCredential() {
  return {
    id: "credential-id",
    rawId: Uint8Array.from([1, 2, 3, 4]).buffer,
    type: "public-key",
    authenticatorAttachment: "platform",
    response: {
      clientDataJSON: Uint8Array.from([5, 6, 7]).buffer,
      attestationObject: Uint8Array.from([8, 9, 10]).buffer,
      getTransports: () => ["internal"],
    },
    getClientExtensionResults: () => ({}),
  };
}

function assertionCredential() {
  return {
    id: "credential-id",
    rawId: Uint8Array.from([1, 2, 3, 4]).buffer,
    type: "public-key",
    authenticatorAttachment: "platform",
    response: {
      clientDataJSON: Uint8Array.from([5, 6, 7]).buffer,
      authenticatorData: Uint8Array.from([17, 18, 19]).buffer,
      signature: Uint8Array.from([20, 21, 22]).buffer,
      userHandle: null,
    },
    getClientExtensionResults: () => ({}),
  };
}

function registrationCredentialJson() {
  return {
    id: "credential-id",
    rawId: "AQIDBA",
    type: "public-key",
    authenticatorAttachment: "platform",
    response: {
      clientDataJSON: "BQYH",
      attestationObject: "CAkK",
      transports: ["internal"],
    },
    clientExtensionResults: {},
  };
}

function revokePayload() {
  return { revoked: true, request_id: "req_revoke", trace_id: "trc_revoke" };
}

function createRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    contains() {
      return true;
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
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
    async json() {
      return structuredClone(payload);
    },
  };
}
