import assert from "node:assert/strict";
import test from "node:test";

import { IdentityApiError } from "../../src/lib/identity.js";
import {
  getWorkspaceOnboarding,
  recheckWorkspaceOnboarding,
  updateWorkspaceOnboardingStep,
  workspaceOnboardingHref,
} from "../../src/lib/workspaces.js";
import { WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../src/lib/workspaceNavigation.js";
import { createIdentityPageController as createAccountController } from "../../src/features/identity/identityPageState.js";
const createIdentityPageController = (options) => createAccountController({ initialSection: "onboarding", ...options });
import { renderIdentitySecurityPage as renderAccountPage } from "../../src/features/identity/renderIdentityPage.js";
const renderIdentitySecurityPage = (root, state) => renderAccountPage(root, { accountSection: "onboarding", ...state });

const CONTROL_URL = "http://control.local";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const NOW = "2026-08-22T08:00:00Z";
const AUTHORIZATION_FINGERPRINT = "f".repeat(64);
const STEP_DEFINITIONS = [
  ["workspace_profile", "确认空间资料", "confirm", "#workspace-profile"],
  ["environment", "准备开发环境", "confirm", "#workspace-environments"],
  ["provider", "接入模型服务", "configure", "#workspace-providers"],
  ["model", "添加可用模型", "configure", "#workspace-models"],
  ["model_alias", "配置模型别名", "configure", "#workspace-model-aliases"],
  ["project_application", "建立业务归属", "configure", "#workspace-projects"],
  ["virtual_key", "创建调用凭据", "configure", "#workspace-virtual-keys"],
  ["first_call", "完成首次调用", "recheck", "#workspace-first-call"],
];

test("PRD-097 onboarding clients use Workspace cookies, CSRF, CAS, and exact write bodies", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace-csrf" };
  const responses = [
    onboardingPayload(),
    onboardingPayload({ profileComplete: true, revision: 2, generation: 1 }),
    onboardingPayload({ profileComplete: true, revision: 3, generation: 2 }),
  ];
  const calls = installFetch(() => jsonResponse(responses.shift()));
  try {
    await getWorkspaceOnboarding({ controlApiUrl: CONTROL_URL });
    await updateWorkspaceOnboardingStep({
      controlApiUrl: CONTROL_URL,
      stepId: "workspace_profile",
      action: "confirm",
      reasonCode: "user_confirmed",
      revision: 1,
      idempotencyKey: "onboarding:confirm-0001",
    });
    await recheckWorkspaceOnboarding({
      controlApiUrl: CONTROL_URL,
      revision: 2,
      idempotencyKey: "onboarding:recheck-0001",
    });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(
    calls.map((call) => [new URL(call.url).pathname, call.options.method ?? "GET"]),
    [
      ["/workspaces/current/onboarding", "GET"],
      ["/workspaces/current/onboarding/steps/workspace_profile", "PATCH"],
      ["/workspaces/current/onboarding/recheck", "POST"],
    ],
  );
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[1].options.headers["If-Match"], '"1"');
  assert.equal(calls[1].options.headers["Idempotency-Key"], "onboarding:confirm-0001");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    action: "confirm",
    reason_code: "user_confirmed",
  });
  assert.equal(calls[2].options.headers["If-Match"], '"2"');
  assert.equal(calls[2].options.body, undefined);
  assert.equal(calls[2].options.headers["Content-Type"], undefined);
  for (const call of calls) {
    assert.equal("Authorization" in call.options.headers, false);
    assert.doesNotMatch(call.options.body ?? "", /workspace_id|tenant_id|status|source|completed/u);
  }
});

test("PRD-097 client rejects invalid mutation pairs and drifted projections before use", async () => {
  let fetchCount = 0;
  installFetch(() => {
    fetchCount += 1;
    return jsonResponse(onboardingPayload());
  });
  await assert.rejects(
    updateWorkspaceOnboardingStep({
      controlApiUrl: CONTROL_URL,
      stepId: "provider",
      action: "confirm",
      reasonCode: "do_later",
      revision: 1,
      idempotencyKey: "onboarding:invalid-0001",
    }),
    (error) => error.code === "workspace.request_invalid",
  );
  assert.equal(fetchCount, 0);

  installFetch(() => jsonResponse({ ...onboardingPayload(), tenant_id: "private-owner" }));
  await assert.rejects(
    getWorkspaceOnboarding({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "workspace_session.response_invalid",
  );

  const reordered = onboardingPayload();
  [reordered.steps[0], reordered.steps[1]] = [reordered.steps[1], reordered.steps[0]];
  installFetch(() => jsonResponse(reordered));
  await assert.rejects(
    getWorkspaceOnboarding({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "workspace_session.response_invalid",
  );
});

test("PRD-097 controller renders eight steps and reuses an uncertain mutation key", async () => {
  const root = createRoot();
  const attempts = [];
  let updateCount = 0;
  const confirmed = onboardingPayload({ profileComplete: true, revision: 2, generation: 1 });
  const controller = createIdentityPageController({
    root,
    newIdempotencyKey: () => "onboarding:controller-0001",
    api: workingApi({
      updateOnboardingStep: async (input) => {
        attempts.push(input);
        updateCount += 1;
        if (updateCount === 1) {
          throw new IdentityApiError({
            code: "workspace_session.unavailable",
            message: "暂时无法连接空间服务。",
          });
        }
        return confirmed;
      },
    }),
  });

  assert.equal(await controller.mount(), true);
  assert.match(root.innerHTML, /初始化中心/u);
  assert.match(root.innerHTML, /<details class="identity-onboarding-steps-details">/u);
  assert.match(root.innerHTML, /查看全部初始化步骤（0\/8）/u);
  assert.equal((root.innerHTML.match(/data-onboarding-step-index=/gu) ?? []).length, 8);
  assert.equal(await controller.confirmOnboardingStep(), false);
  assert.equal(await controller.confirmOnboardingStep(), true);
  assert.equal(attempts[0].idempotencyKey, attempts[1].idempotencyKey);
  assert.equal(attempts[0].revision, 1);
  assert.deepEqual(
    { stepId: attempts[0].stepId, action: attempts[0].action, reasonCode: attempts[0].reasonCode },
    { stepId: "workspace_profile", action: "confirm", reasonCode: "user_confirmed" },
  );
  assert.match(root.innerHTML, /1\/8/u);
  assert.doesNotMatch(root.innerHTML, /workspace_id|tenant_id|revision|generation/iu);
  controller.destroy();
});

test("PRD-097 renderer hides organization profile mutations from developers", () => {
  const root = createRoot();
  const state = securityState({ role: "developer", onboarding: onboardingPayload() });
  renderIdentitySecurityPage(root, state);
  const panel = root.innerHTML.slice(root.innerHTML.indexOf("data-onboarding-step-panel"));
  assert.doesNotMatch(panel.split("</div>\n    </div>", 1)[0], /confirm-onboarding-step|defer-onboarding-step/u);
  assert.match(panel, /href="\/console\.html#tenant"/u);
});

test("PRD-097 all onboarding links resolve to registered console pages", () => {
  const sources = ["tenant", "environments", "providers", "models", "model-aliases",
    "projects", "virtual-keys", "sdk-examples"];
  const onboarding = onboardingPayload();
  for (const [index, step] of onboarding.steps.entries()) {
    const href = `/console.html#${sources[index]}`;
    assert.ok(Object.hasOwn(WORKSPACE_SOURCE_ROUTE_REGISTRY, sources[index]));
    assert.equal(workspaceOnboardingHref(step), href);
    const root = createRoot();
    step.action = "view";
    renderIdentitySecurityPage(root, {
      ...securityState({ onboarding }), onboardingSelectedStepId: step.id,
    });
    assert.ok(root.innerHTML.includes(`class="identity-primary identity-onboarding-link" href="${href}"`));
    assert.doesNotMatch(root.innerHTML, /href="\/#workspace-/u);
  }
});

test("PRD-097 unknown and mismatched onboarding destinations cannot become links", () => {
  for (const navigation_target of ["https://example.test", "//example.test", "javascript:alert(1)",
    "#workspace-models", "#workspace-providers\" onclick=\"alert(1)"]) {
    const onboarding = onboardingPayload();
    const step = onboarding.steps[2];
    step.navigation_target = navigation_target;
    assert.equal(workspaceOnboardingHref(step), null);
    const root = createRoot();
    renderIdentitySecurityPage(root, {
      ...securityState({ onboarding }), onboardingSelectedStepId: step.id,
    });
    assert.doesNotMatch(root.innerHTML, /identity-onboarding-link/u);
  }
  assert.equal(workspaceOnboardingHref({ id: "unknown", navigation_target: "#workspace-providers" }), null);
  assert.equal(workspaceOnboardingHref(null), null);
});

test("PRD-097 destroy aborts onboarding load and fences its late projection", async () => {
  const pending = deferred();
  let capturedSignal = null;
  const root = createRoot();
  const controller = createIdentityPageController({
    root,
    api: workingApi({
      onboarding: ({ signal }) => {
        capturedSignal = signal;
        return pending.promise;
      },
    }),
  });
  const mounted = controller.mount();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(capturedSignal.aborted, false);
  controller.destroy();
  assert.equal(capturedSignal.aborted, true);
  pending.resolve(onboardingPayload());
  await mounted;
  assert.equal(root.innerHTML, "");
});

function onboardingPayload({ profileComplete = false, revision = 1, generation = 0 } = {}) {
  const statuses = profileComplete
    ? ["complete", "current", ...Array(6).fill("blocked")]
    : ["current", ...Array(7).fill("blocked")];
  const steps = STEP_DEFINITIONS.map(([id, title, defaultAction, target], index) => {
    const status = statuses[index];
    return {
      id,
      order: index + 1,
      title,
      status,
      source: id === "workspace_profile"
        ? profileComplete ? "user_confirmed" : "system_created"
        : id === "environment" ? "system_created" : "none",
      action: status === "complete" ? "view" : defaultAction,
      navigation_target: target,
      blocking_code: status === "blocked" ? `requires.${STEP_DEFINITIONS[index - 1][0]}` : null,
      last_checked_at: NOW,
    };
  });
  return {
    workspace_kind: "organization",
    completed_steps: profileComplete ? 1 : 0,
    total_steps: 8,
    status: "incomplete",
    generation,
    revision,
    activated_at: null,
    evaluated_at: NOW,
    steps,
    request_id: "req_onboarding",
    trace_id: "trc_onboarding",
  };
}

function workingApi(overrides = {}) {
  const state = securityState({ onboarding: onboardingPayload() });
  return {
    me: async () => ({
      account: state.account,
      current_session: state.currentSession,
      request_id: "req_me",
      trace_id: "trc_me",
    }),
    sessions: async () => ({ sessions: state.sessions }),
    workspaces: async () => ({ workspaces: state.workspaces }),
    identityWorkspaces: async () => ({ workspaces: state.workspaceChoices }),
    workspaceSession: async () => ({
      context: state.workspaceContext,
      authorization: workspaceAuthorization(),
    }),
    memberships: async () => ({ memberships: [] }),
    workspaceNavigation: async () => workspaceNavigationProjection(),
    onboarding: async () => onboardingPayload(),
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

function securityState({ role = "owner", onboarding = null } = {}) {
  const currentSession = {
    session_id: "private-session",
    device_family: "chrome",
    created_at: NOW,
    last_seen_at: NOW,
    expires_at: "2026-08-23T08:00:00Z",
    is_current: true,
  };
  const workspace = {
    workspace_id: WORKSPACE_ID,
    kind: "organization",
    name: "Acme AI",
    slug: "acme-ai",
    role,
  };
  return {
    account: {
      email: "owner@example.com",
      display_name: "Owner",
      status: "active",
      mfa_configured: false,
    },
    currentSession,
    sessions: [currentSession],
    workspaces: [],
    workspaceChoices: [workspace],
    workspaceContext: {
      ...workspace,
      session_status: "active",
      expires_at: "2026-08-23T08:00:00Z",
    },
    memberships: [],
    navigationItems: [],
    workspaceView: "summary",
    onboarding,
    onboardingSelectedStepId: "workspace_profile",
    onboardingBusy: false,
    onboardingError: null,
  };
}

function createRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    contains() {
      return true;
    },
    querySelector() {
      return { focus() {} };
    },
    querySelectorAll() {
      return [];
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
    headers: { get: () => null },
    async json() {
      return structuredClone(payload);
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
