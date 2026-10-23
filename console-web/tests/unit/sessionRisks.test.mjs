// PRD-070 Console 专项：strict API、双权限 owner、私有 cursor/幂等、状态矩阵和响应式 DOM 合同。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { AuthApiError } from "../../src/lib/auth.js";
import {
  applySessionRiskAction,
  availableSessionRiskActions,
  listSessionRisks,
  normalizeSessionRiskActionBody,
  normalizeSessionRiskActionResponse,
  normalizeSessionRiskListResponse,
  shouldRetainSessionRiskIdempotencyKey,
} from "../../src/lib/sessionRisks.js";
import {
  createSessionRiskPageState,
  sessionRiskManageIdentitySnapshot,
  sessionRiskPageOwner,
} from "../../src/features/session-risks/sessionRiskPageState.js";
import { renderSessionRisks } from "../../src/features/session-risks/renderSessionRisks.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";

// 资源常量全部符合后端 canonical wire 形状，负例在各测试中单独构造。
const SCORE_ID = `srs_${"1".repeat(32)}`;
const FREEZE_ID = `srf_${"2".repeat(32)}`;
const ACTION_ID = `sra_${"3".repeat(32)}`;
const CHAT_SESSION_ID = `chs_${"4".repeat(32)}`;
const VIRTUAL_KEY_ID = "55555555-5555-4555-8555-555555555555";

function workspaceRiskShell(codes = [PERMISSIONS.SESSION_RISK_READ, PERMISSIONS.SESSION_RISK_MANAGE]) {
  const identity = { account: { status: "active" }, current_session: {
    session_id: "risk_global_session", is_current: true, revoke_generation: 1,
    expires_at: "2099-01-01T00:00:00Z",
  } };
  const context = { workspace_id: `wsp_${"7".repeat(32)}`, role: "owner",
    session_status: "active", expires_at: "2099-01-01T00:00:00Z" };
  const authorization = { permission_codes: [...codes].sort(), permission_revision: 1,
    membership_revision: 1, permission_fingerprint: "a".repeat(64) };
  return { state: "ok", workspaceNavigationContract: true, workspaceIdentity: identity,
    workspaceContext: context, workspaceAuthorization: authorization,
    workspaceOwner: workspaceNavigationOwner(identity, { context, authorization }),
    activeNavigationItemId: "session-risks", activeRouteCode: "workspace.governance.session-risks",
    navigation: { ...authorization, active_navigation_item_id: "session-risks",
      active_route_code: "workspace.governance.session-risks", navigation_items: [{
        id: "session-risks", route_code: "workspace.governance.session-risks",
        target_section: "session-risks", visible: true, disabled: false,
      }] } };
}

test("Workspace risks render populated state and discard responses from a replaced session", () => {
  const shell = workspaceRiskShell();
  const owner = sessionRiskPageOwner(shell.workspaceAuthorization, shell);
  assert.equal(owner?.workspaceId, shell.workspaceContext.workspace_id);
  assert.equal(Object.hasOwn(owner, "tenantId"), false);
  const state = createSessionRiskPageState();
  assert.equal(state.enter(owner), true);
  const read = state.beginListOperation(owner, 1);
  assert.equal(state.acceptPage(read, normalizeSessionRiskListResponse(listFixture()), owner, 1), true);
  state.selectRisk(SCORE_ID);
  assert.match(renderSessionRisks(state.snapshot(), shell.workspaceAuthorization, shell), /data-session-risk-action="freeze_session"/u);
  const late = state.beginListOperation(owner, 2);
  const next = workspaceRiskShell();
  next.workspaceIdentity.current_session.session_id = "replacement_global_session";
  next.workspaceOwner = workspaceNavigationOwner(next.workspaceIdentity, {
    context: next.workspaceContext, authorization: next.workspaceAuthorization,
  });
  const nextOwner = sessionRiskPageOwner(next.workspaceAuthorization, next);
  assert.equal(state.enter(nextOwner), true);
  assert.equal(late.controller.signal.aborted, true);
  assert.equal(state.acceptPage(late, normalizeSessionRiskListResponse(listFixture()), nextOwner, 2), false);
  assert.equal(state.snapshot().session_risks.length, 0);
});

test("Workspace risk owners reject expiry, navigation drift and manage-only authority", () => {
  for (const change of [
    s => { s.workspaceContext.expires_at = "2000-01-01T00:00:00Z"; },
    s => { s.workspaceIdentity.current_session.expires_at = "2000-01-01T00:00:00Z"; },
    s => { s.workspaceIdentity.current_session.revoke_generation += 1; },
    s => { s.navigation.permission_fingerprint = "b".repeat(64); },
    s => { s.activeRouteCode = "workspace.home.overview"; },
    s => { s.navigation.navigation_items[0].disabled = true; },
  ]) {
    const shell = workspaceRiskShell();
    change(shell);
    assert.equal(sessionRiskPageOwner(shell.workspaceAuthorization, shell), null);
  }
  const shell = workspaceRiskShell();
  assert.equal(sessionRiskPageOwner({ ...shell.workspaceAuthorization, membership_revision: 2 }, shell), null);
  for (const codes of [[], [PERMISSIONS.SESSION_RISK_MANAGE]]) {
    const denied = workspaceRiskShell(codes);
    assert.equal(sessionRiskPageOwner(denied.workspaceAuthorization, denied), null);
  }
  const readOnly = workspaceRiskShell([PERMISSIONS.SESSION_RISK_READ]);
  assert.equal(sessionRiskPageOwner(readOnly.workspaceAuthorization, readOnly).canManage, false);
  assert.equal(sessionRiskPageOwner(readOnly.workspaceAuthorization, readOnly, { requireManage: true }), null);
});

test("Workspace risk clients use Cookie reads and exact CSRF writes without a Bearer", async (t) => {
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  t.after(() => { globalThis.fetch = previousFetch; globalThis.document = previousDocument; });
  const csrf = "W".repeat(43);
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${csrf}` };
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return jsonResponse(listFixture());
  };
  await listSessionRisks({ controlApiUrl: "https://control.test", authentication: "workspace" });
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.headers.Authorization, undefined);
  const action = { controlApiUrl: "https://control.test", authentication: "workspace",
    sessionRiskScoreId: SCORE_ID, action: "freeze_session", reasonCode: "confirmed_attack",
    expectedRevision: 3, idempotencyKey: "workspace-risk-action-0001" };
  // A malformed server response must still be rejected after the correct authenticated request.
  await assert.rejects(applySessionRiskAction(action), e => e.code === "session_risk.response_invalid");
  assert.equal(calls[1].options.credentials, "include");
  assert.equal(calls[1].options.headers["X-CSRF-Token"], csrf);
  assert.equal(new Headers(calls[1].options.headers).get("Idempotency-Key"), action.idempotencyKey);
  assert.equal(calls[1].options.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    action: "freeze_session", reason_code: "confirmed_attack", expected_revision: 3,
  });
  globalThis.document.cookie = "";
  await assert.rejects(applySessionRiskAction(action));
  await assert.rejects(listSessionRisks({ controlApiUrl: "https://control.test", authentication: "workspace", token: "mixed" }));
  assert.equal(calls.length, 2);
});

/** 构造 fetch 可用 JSON 响应。 */
function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  // 测试替身只暴露生产 client 真正读取的响应表面，避免依赖浏览器宽松行为。
  return {
    ok,
    status,
    headers: { get: () => null },
    async json() {
      return payload;
    },
  };
}

/** 构造含完整后端权限镜像的认证 owner。 */
function authContext({
  permissionCodes = [
    PERMISSIONS.REQUEST_LOG_READ,
    PERMISSIONS.SESSION_RISK_MANAGE,
    PERMISSIONS.SESSION_RISK_READ,
  ],
  tenantId = "tenant_a",
  sessionId = "session_a",
  fingerprint = "test_session_risk_owner_a",
} = {}) {
  // owner 镜像必须在构造权限快照之前固定，避免测试绕过真实 tenant 一致性检查。
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId },
    user: {
      tenant_id: tenantId,
      user_id: "usr_admin",
      email: "admin@metahub.local",
      role: "security_admin",
    },
    session: { session_id: sessionId, session_revision: 1 },
  }, permissionCodes, { permissionRevision: 7, authorizationFingerprint: fingerprint });
}

/** 构造与 /auth/me 镜像完全一致的后端导航快照。 */
function shellState(context = authContext()) {
  return {
    state: "ok",
    activeNavigationItemId: "session-risks",
    navigation: {
      active_navigation_item_id: "session-risks",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: "session-risks",
        target_section: "session-risks",
        group: "security-governance",
        group_label: "安全与治理",
        label: "高危会话",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.SESSION_RISK_READ],
      }],
    },
  };
}

/** 构造一条 exact-key SessionRiskInfo。 */
function riskFixture(overrides = {}) {
  return {
    session_risk_score_id: SCORE_ID,
    scope_type: "enterprise_chat",
    scope_reference: CHAT_SESSION_ID,
    virtual_key_id: VIRTUAL_KEY_ID,
    status: "high_risk",
    score: 7600,
    score_scale: 10000,
    risky_turn_count: 2,
    signal_counts: { jailbreak: 2, sensitive: 1, tool_inducement: 0 },
    state_revision: 3,
    threshold_version: "session-risk-threshold-v1",
    last_source_request_id: "req_session_risk_3",
    window_expires_at: "2026-08-04T05:00:00Z",
    first_high_risk_at: "2026-08-04T04:20:00Z",
    frozen_at: null,
    updated_at: "2026-08-04T04:22:00Z",
    freezes: [],
    actions: [],
    ...overrides,
  };
}

/**
 * 构造一条 exact-key append-only action。
 * 参数：只允许测试覆盖固定 wire 字段。返回：与默认 risk revision 对应的不可变证据形状。
 */
function actionFixture(overrides = {}) {
  // 默认动作与 riskFixture 的 revision 3 对应，调用方可覆盖为后续幂等回放事实。
  return {
    session_risk_action_id: ACTION_ID,
    action: "freeze_session",
    reason_code: "confirmed_attack",
    actor_type: "user",
    actor_reference: "admin@metahub.local",
    session_risk_freeze_id: FREEZE_ID,
    status_before: "high_risk",
    status_after: "frozen",
    score_snapshot: 7600,
    state_revision: 4,
    source_request_id: "req_session_risk_action",
    created_at: "2026-08-04T04:25:00Z",
    ...overrides,
  };
}

/** 构造列表根响应，cursor 只供私有状态测试。 */
function listFixture(overrides = {}) {
  return {
    runtime_status: "active",
    summary: { high_risk_count: 1, frozen_count: 0, resolved_count: 0 },
    session_risks: [riskFixture()],
    next_cursor: null,
    request_id: "req_session_risk_list",
    trace_id: "trc_session_risk_list",
    ...overrides,
  };
}

test("PRD-070 list client sends only frozen query and exact request headers", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(listFixture());
  };

  const result = await listSessionRisks({
    controlApiUrl: "http://control.local",
    token: "session-token",
    status: "high_risk",
    scopeType: "enterprise_chat",
    virtualKeyId: VIRTUAL_KEY_ID,
    fromTime: "2026-08-04T00:00:00Z",
    toTime: "2026-08-04T06:00:00Z",
    limit: 20,
    cursor: "opaque.cursor.signature",
    operationRequestId: "req_console_session_list",
    operationTraceId: "trc_console_session_list",
    tenantId: "tenant_injected",
    actorUserId: "usr_injected",
    includePrompt: true,
  });

  assert.equal(result.state, "ok");
  assert.equal(Object.isFrozen(result.session_risks[0]), true);
  assert.equal(calls.length, 1);
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, "/security/session-risks");
  assert.equal(url.searchParams.get("scope_type"), "enterprise_chat");
  assert.equal(url.searchParams.get("cursor"), "opaque.cursor.signature");
  assert.equal(url.searchParams.has("tenant_id"), false);
  assert.equal(url.searchParams.has("actor_user_id"), false);
  assert.equal(url.searchParams.has("include_prompt"), false);
  assert.equal(calls[0].options.headers.Authorization, "Bearer session-token");
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_console_session_list");
  assert.equal(calls[0].options.cache, "no-store");
});

test("PRD-070 strict client rejects unknown nested fields and full API HMAC", async (t) => {
  const variants = [];
  variants.push({ ...listFixture(), prompt: "must-not-pass" });
  variants.push({ ...listFixture(), summary: { ...listFixture().summary, unknown: 1 } });
  variants.push({
    ...listFixture(),
    session_risks: [riskFixture({ signal_counts: { ...riskFixture().signal_counts, fragment: "x" } })],
  });
  variants.push({
    ...listFixture(),
    session_risks: [riskFixture({ actions: [{ ...actionFixture(), network_meta: "x" }] })],
  });
  variants.push({
    ...listFixture(),
    session_risks: [riskFixture({
      scope_type: "api_session",
      scope_reference: `srh_${"a".repeat(64)}`,
    })],
  });
  variants.push({
    ...listFixture(),
    session_risks: [riskFixture({ virtual_key_id: "VK_NON_CANONICAL" })],
  });
  for (const payload of variants) {
    assert.equal(normalizeSessionRiskListResponse(payload), null);
  }

  let fetchCount = 0;
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse(variants[0]);
  };
  await assert.rejects(
    listSessionRisks({ controlApiUrl: "http://control.local", token: "token" }),
    (error) => error instanceof AuthApiError && error.code === "session_risk.response_invalid",
  );
  assert.equal(fetchCount, 1);
});

test("PRD-070 action client emits POST exact body and validates returned state matrix", async (t) => {
  const calls = [];
  const action = actionFixture();
  const updated = riskFixture({
    status: "frozen",
    state_revision: 4,
    window_expires_at: null,
    frozen_at: "2026-08-04T04:25:00Z",
    updated_at: "2026-08-04T04:25:00Z",
    freezes: [{
      session_risk_freeze_id: FREEZE_ID,
      target_type: "session",
      status: "active",
      state_revision: 4,
      created_at: "2026-08-04T04:25:00Z",
      released_at: null,
    }],
    actions: [action],
  });
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      session_risk: updated,
      action,
      idempotent_replay: false,
      request_id: "req_session_risk_action",
      trace_id: "trc_session_risk_action",
    });
  };

  const result = await applySessionRiskAction({
    controlApiUrl: "http://control.local",
    token: "session-token",
    sessionRiskScoreId: SCORE_ID,
    action: "freeze_session",
    reasonCode: "confirmed_attack",
    expectedRevision: 3,
    idempotencyKey: "sr_action_12345678",
    operationRequestId: "req_console_action",
    operationTraceId: "trc_console_action",
    targetId: "vk_injected",
    tenantId: "tenant_injected",
  });

  assert.equal(result.session_risk.status, "frozen");
  assert.equal(calls[0].url, `http://control.local/security/session-risks/${SCORE_ID}/action`);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(new Headers(calls[0].options.headers).get("Idempotency-Key"), "sr_action_12345678");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    action: "freeze_session",
    reason_code: "confirmed_attack",
    expected_revision: 3,
  });
  assert.doesNotMatch(calls[0].options.body, /tenant|actor|target|prompt/i);
});

test("PRD-070 action matrix rejects ambiguous intent before fetch and retains uncertain key", async (t) => {
  let fetchCount = 0;
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse({});
  };
  assert.throws(
    () => normalizeSessionRiskActionBody({
      action: "freeze_session",
      reasonCode: "false_positive",
      expectedRevision: 3,
    }),
    (error) => error instanceof AuthApiError && error.code === "session_risk.payload_invalid",
  );
  await assert.rejects(
    applySessionRiskAction({
      controlApiUrl: "http://control.local",
      token: "token",
      sessionRiskScoreId: "srs_bad/path",
      action: "dismiss",
      reasonCode: "false_positive",
      expectedRevision: 3,
      idempotencyKey: "sr_action_12345678",
    }),
    (error) => error instanceof AuthApiError && error.code === "session_risk.not_found",
  );
  await assert.rejects(
    listSessionRisks({
      controlApiUrl: "http://control.local",
      token: "token",
      virtualKeyId: "55555555-5555-4555-8555-55555555555A",
    }),
    (error) => error instanceof AuthApiError && error.code === "session_risk.query_invalid",
  );
  assert.equal(fetchCount, 0);
  assert.equal(shouldRetainSessionRiskIdempotencyKey(new TypeError("network")), true);
  assert.equal(shouldRetainSessionRiskIdempotencyKey(new AuthApiError({ status: 503 })), true);
  assert.equal(shouldRetainSessionRiskIdempotencyKey(new AuthApiError({ status: 409 })), false);
});

test("PRD-070 wire contract accepts redacted API scope, nullable terminal window and later replay", () => {
  const apiRisk = riskFixture({
    scope_type: "api_session",
    scope_reference: `api_${"a".repeat(16)}`,
  });
  assert.ok(normalizeSessionRiskListResponse(listFixture({ session_risks: [apiRisk] })));
  assert.equal(normalizeSessionRiskListResponse(listFixture({
    session_risks: [riskFixture({ window_expires_at: null })],
  })), null);

  const originalAction = actionFixture();
  const laterAction = actionFixture({
    session_risk_action_id: `sra_${"6".repeat(32)}`,
    action: "release_session",
    reason_code: "incident_contained",
    status_before: "frozen",
    status_after: "resolved",
    state_revision: 5,
  });
  const replayRisk = riskFixture({
    status: "resolved",
    state_revision: 5,
    window_expires_at: null,
    frozen_at: "2026-08-04T04:25:00Z",
    freezes: [{
      session_risk_freeze_id: FREEZE_ID,
      target_type: "session",
      status: "released",
      state_revision: 2,
      created_at: "2026-08-04T04:25:00Z",
      released_at: "2026-08-04T04:30:00Z",
    }],
    actions: [laterAction, originalAction],
  });
  const normalized = normalizeSessionRiskActionResponse({
    session_risk: replayRisk,
    action: originalAction,
    idempotent_replay: true,
    request_id: "req_session_risk_replay",
    trace_id: "trc_session_risk_replay",
  }, {
    scoreId: SCORE_ID,
    action: "freeze_session",
    reasonCode: "confirmed_attack",
    expectedRevision: 3,
  });
  assert.equal(normalized?.session_risk.state_revision, 5);
  assert.equal(normalized?.action.state_revision, 4);
});

test("PRD-070 query rejects an empty half-open time range before fetch", async (t) => {
  let fetchCount = 0;
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse({});
  };

  await assert.rejects(
    listSessionRisks({
      controlApiUrl: "http://control.local",
      token: "token",
      fromTime: "2026-08-04T04:00:00Z",
      toTime: "2026-08-04T04:00:00Z",
    }),
    (error) => error instanceof AuthApiError && error.code === "session_risk.query_invalid",
  );
  assert.equal(fetchCount, 0);
});

test("PRD-070 page state hides cursor idempotency owner and rejects late operations", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = sessionRiskPageOwner(context, shell);
  const state = createSessionRiskPageState();
  assert.equal(state.enter(owner), true);
  const operation = state.beginListOperation(owner, 1);
  const payload = normalizeSessionRiskListResponse(listFixture({ next_cursor: "signed.cursor.secret" }));
  assert.equal(state.acceptPage(operation, payload, owner, 1), true);
  assert.equal(state.selectRisk(SCORE_ID), true);
  assert.equal(state.openAction({
    sessionRiskScoreId: SCORE_ID,
    action: "freeze_session",
    idempotencyKey: "sr_action_private_1234",
    operationRequestId: "req_private_action",
    operationTraceId: "trc_private_action",
  }), true);

  const snapshotText = JSON.stringify(state.snapshot());
  assert.equal(snapshotText.includes("signed.cursor.secret"), false);
  assert.equal(snapshotText.includes("sr_action_private_1234"), false);
  assert.equal(snapshotText.includes("req_private_action"), false);
  assert.equal(snapshotText.includes(context.authorization_fingerprint), false);
  const payloadForPost = state.beginAction({ reasonCode: "confirmed_attack" });
  assert.equal(payloadForPost.idempotencyKey, "sr_action_private_1234");
  const actionOperation = state.beginActionOperation(owner);
  assert.equal(state.isActionOperationCurrent(actionOperation, owner), true);

  const changedContext = authContext({
    sessionId: "session_b",
    fingerprint: "test_session_risk_owner_b",
  });
  const changedOwner = sessionRiskPageOwner(changedContext, shellState(changedContext));
  assert.equal(state.isActionOperationCurrent(actionOperation, changedOwner), false);
  state.enter(changedOwner);
  assert.equal(actionOperation.controller.signal.aborted, true);
  assert.equal(state.snapshot().session_risks.length, 0);
  assert.deepEqual(state.snapshot().filters, { limit: 20 });
});

test("PRD-070 read and manage remain independent in owner renderer and action DOM", () => {
  const fullContext = authContext();
  const fullShell = shellState(fullContext);
  const state = createSessionRiskPageState();
  const owner = sessionRiskPageOwner(fullContext, fullShell);
  state.enter(owner);
  const operation = state.beginListOperation(owner, 1);
  state.acceptPage(operation, normalizeSessionRiskListResponse(listFixture()), owner, 1);
  state.selectRisk(SCORE_ID);
  const fullHtml = renderSessionRisks(state.snapshot(), fullContext, fullShell);
  assert.match(fullHtml, /data-session-risk-action="freeze_session"/);

  const readContext = authContext({
    permissionCodes: [PERMISSIONS.SESSION_RISK_READ],
    fingerprint: "test_session_risk_read_only",
  });
  const readShell = shellState(readContext);
  const readState = createSessionRiskPageState();
  const readOwner = sessionRiskPageOwner(readContext, readShell);
  readState.enter(readOwner);
  const readOperation = readState.beginListOperation(readOwner, 1);
  readState.acceptPage(readOperation, normalizeSessionRiskListResponse(listFixture()), readOwner, 1);
  readState.selectRisk(SCORE_ID);
  const readHtml = renderSessionRisks(readState.snapshot(), readContext, readShell);
  assert.match(readHtml, /高危会话/);
  assert.doesNotMatch(readHtml, /data-session-risk-action=/);
  assert.doesNotMatch(readHtml, /session-risk-action-dialog/);

  const manageOnly = authContext({
    permissionCodes: [PERMISSIONS.SESSION_RISK_MANAGE],
    fingerprint: "test_session_risk_manage_only",
  });
  assert.equal(sessionRiskManageIdentitySnapshot(manageOnly), "");
  assert.equal(sessionRiskPageOwner(manageOnly, shellState(manageOnly)), null);
  assert.equal(renderSessionRisks(state.snapshot(), manageOnly, shellState(manageOnly)), "");
});

test("PRD-070 renderer escapes actor evidence and exposes no cursor or complete API digest", () => {
  const context = authContext();
  const shell = shellState(context);
  const unsafeAction = actionFixture({
    action: "high_risk",
    reason_code: "threshold_reached",
    actor_type: "system",
    actor_reference: "<script>alert(1)</script>",
    session_risk_freeze_id: null,
    status_before: "monitoring",
    status_after: "high_risk",
    state_revision: 3,
    source_request_id: "req_xss_safe",
  });
  const payload = normalizeSessionRiskListResponse(listFixture({
    session_risks: [riskFixture({ actions: [unsafeAction] })],
    next_cursor: "cursor-never-in-dom",
  }));
  const state = createSessionRiskPageState();
  const owner = sessionRiskPageOwner(context, shell);
  state.enter(owner);
  const operation = state.beginListOperation(owner, 1);
  state.acceptPage(operation, payload, owner, 1);
  state.selectRisk(SCORE_ID);
  const html = renderSessionRisks(state.snapshot(), context, shell);
  assert.equal(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"), true);
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /cursor-never-in-dom/);
  assert.doesNotMatch(html, /srh_[0-9a-f]{64}/);
  assert.doesNotMatch(html, /Prompt|Response|Idempotency-Key|authorization_fingerprint/);
});

test("PRD-070 action availability respects independent session and Key overlays", () => {
  assert.deepEqual(availableSessionRiskActions(riskFixture()), [
    "freeze_session",
    "freeze_virtual_key",
    "dismiss",
  ]);
  const frozen = riskFixture({
    status: "frozen",
    window_expires_at: null,
    frozen_at: "2026-08-04T04:25:00Z",
    freezes: [{
      session_risk_freeze_id: FREEZE_ID,
      target_type: "session",
      status: "active",
      state_revision: 4,
      created_at: "2026-08-04T04:25:00Z",
      released_at: null,
    }],
  });
  assert.deepEqual(availableSessionRiskActions(frozen), [
    "freeze_virtual_key",
    "release_session",
  ]);
  const keyOnlyFrozen = riskFixture({
    status: "frozen",
    window_expires_at: null,
    frozen_at: "2026-08-04T04:25:00Z",
    freezes: [{
      session_risk_freeze_id: `srf_${"7".repeat(32)}`,
      target_type: "virtual_key",
      status: "active",
      state_revision: 4,
      created_at: "2026-08-04T04:25:00Z",
      released_at: null,
    }],
  });
  assert.deepEqual(availableSessionRiskActions(keyOnlyFrozen), [
    "freeze_session",
    "release_virtual_key",
  ]);
});

test("PRD-070 shell wiring and responsive CSS freeze PAGE-098 lifecycle", async () => {
  const [mainSource, developerStart, permissions, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/permissions.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /loadSessionRisksState\(consoleShellState, refreshId\)/);
  assert.match(mainSource, /bindSessionRiskPageInteractions\(consoleShellState\)/);
  assert.match(mainSource, /clearSessionRiskStateOnIdentityChange/);
  assert.match(mainSource, /currentSessionRiskManageOwner/);
  assert.match(developerStart, /case "session-risks":/);
  assert.match(developerStart, /renderSessionRisksSection/);
  assert.match(permissions, /SESSION_RISK_READ: "session_risk\.read"/);
  assert.match(permissions, /SESSION_RISK_MANAGE: "session_risk\.manage"/);
  assert.match(css, /\.session-risk-workspace\s*\{/);
  const filterRule = css.match(/\.session-risk-filters\s*\{([^}]*)\}/)?.[1] ?? "";
  // 页面位于带侧栏的窄主栏内，筛选布局必须随容器换行，不能只依赖视口断点。
  assert.match(filterRule, /repeat\(auto-fit,\s*minmax\(180px,\s*1fr\)\)/);
  assert.match(css, /@media \(max-width: 780px\)/);
  assert.match(css, /@media \(max-width: 560px\)/);
  const dialogRule = css.match(/\.session-risk-action-dialog\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.match(dialogRule, /position:\s*fixed/);
  assert.match(dialogRule, /max-height:\s*min\(720px, calc\(100dvh - 28px\)\)/);
  assert.match(dialogRule, /overflow:\s*auto/);
  assert.equal((css.match(/{/g) ?? []).length, (css.match(/}/g) ?? []).length);
});
