/** 验证 PRD-078 控制台协议、状态并发与权限降级边界，测试数据不得包含真实凭据。 */
/** PRD-078 PAGE-108/109 Console contracts. Sensitive samples remain in memory only. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  approvalActionAccess,
  approvalPageOwner,
  createApprovalPageState,
} from "../../src/features/approvals/approvalPageState.js";
import {
  bindApprovalInteractions,
  clearApprovalDom,
  restoreApprovalActionFocus,
} from "../../src/features/approvals/bindApprovals.js";
import { renderApprovals } from "../../src/features/approvals/renderApprovals.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  decideApproval,
  getApproval,
  listApprovals,
  normalizeAction,
  normalizeApprovalDetail,
  normalizeApprovalList,
  stepUpApproval,
} from "../../src/lib/approvals.js";
import {
  parseApprovalCardHash,
  parseNavigationHash,
} from "../../src/lib/consoleShell.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const APPROVAL_ID = `apr_${"a".repeat(32)}`;
const DECISION_ID = `apd_${"b".repeat(32)}`;
const TENANT_ID = "tenant_approval_a";
const FULL_PERMISSIONS = [
  PERMISSIONS.WORKSPACE_CONSOLE_APPROVAL_READ,
  PERMISSIONS.APPROVAL_READ,
  PERMISSIONS.APPROVAL_DECIDE,
  PERMISSIONS.BILLING_BUDGET_READ,
  PERMISSIONS.BILLING_BUDGET_WRITE,
  PERMISSIONS.SESSION_RISK_READ,
  PERMISSIONS.SESSION_RISK_MANAGE,
];

/**
 * 构造或验证 authContext 对应 PRD-078 场景；测试数据保持租户隔离并使用虚构凭据。
 */
function authContext(overrides = {}) {
  const tenantId = overrides.tenantId ?? TENANT_ID;
  // 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, display_name: "Approval tenant" },
    user: {
      tenant_id: tenantId,
      user_id: overrides.userId ?? "usr_approval_a",
      email: "approval@example.test",
      role: overrides.role ?? "security_admin",
    },
    session: {
      session_id: overrides.sessionId ?? "ses_approval_a",
      session_revision: overrides.sessionRevision ?? 4,
    },
  }, overrides.permissions ?? FULL_PERMISSIONS, {
    permissionRevision: overrides.permissionRevision ?? 14,
    authorizationFingerprint: overrides.fingerprint ?? "approval_fingerprint_14",
  });
}

/**
 * 构造或验证 shellState 对应 PRD-078 场景；测试数据保持租户隔离并使用虚构凭据。
 */
function shellState(context = authContext()) {
  return {
    state: "ok",
    activeNavigationItemId: "approvals",
    navigation: {
      active_navigation_item_id: "approvals",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: "approvals",
        target_section: "approvals",
        label: "Approvals",
        group: "Enterprise",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.APPROVAL_READ],
      }],
    },
  };
}

/**
 * 构造或验证 pendingSummary 对应 PRD-078 场景；测试数据保持租户隔离并使用虚构凭据。
 */
function pendingSummary(overrides = {}) {
  return {
    approval_id: APPROVAL_ID,
    target_type: "session_risk_action",
    target_action: "freeze_session",
    risk_level: "high",
    status: "pending",
    resolution_reason: null,
    state_revision: 1,
    writeback_status: "pending",
    required_permission_code: PERMISSIONS.SESSION_RISK_MANAGE,
    requires_step_up: true,
    created_at: "2026-08-11T00:00:00Z",
    expires_at: "2026-08-12T00:00:00Z",
    decided_at: null,
    ...overrides,
  };
}

/**
 * 构造或验证 pendingDetail 对应 PRD-078 场景；测试数据保持租户隔离并使用虚构凭据。
 */
function pendingDetail(overrides = {}) {
  const approval = overrides.approval ?? pendingSummary();
  return {
    approval,
    target_summary: {
      target_type: approval.target_type,
      target_id: "risk_target_a",
      target_action: approval.target_action,
      target_version: "revision:7",
    },
    allowed_actions: ["approve", "reject"],
    decisions: [],
    request_id: "req_approval_detail",
    trace_id: "trc_approval_detail",
    ...overrides,
  };
}

/**
 * 构造或验证 approvedActionPayload 对应 PRD-078 场景；测试数据保持租户隔离并使用虚构凭据。
 */
function approvedActionPayload(overrides = {}) {
  return {
    approval: pendingSummary({
      status: "approved",
      resolution_reason: "approved",
      state_revision: 2,
      writeback_status: "completed",
      decided_at: "2026-08-11T01:00:00Z",
    }),
    decision: {
      decision_id: DECISION_ID,
      decision: "approve",
      reason_code: "approved_business_need",
      actor_type: "user",
      actor_id: "usr_approval_a",
      revision_before: 1,
      revision_after: 2,
      step_up_used: true,
      writeback_status: "completed",
      created_at: "2026-08-11T01:00:00Z",
    },
    idempotent_replay: false,
    request_id: "req_approval_decide",
    trace_id: "trc_approval_decide",
    ...overrides,
  };
}

/**
 * 构造或验证 httpResponse 对应 PRD-078 场景；测试数据保持租户隔离并使用虚构凭据。
 */
function httpResponse(payload, { status = 200, headers = {} } = {}) {
  const values = new Map(Object.entries({
    "x-request-id": payload.request_id,
    "x-trace-id": payload.trace_id,
    "cache-control": "no-store, private",
    pragma: "no-cache",
    vary: "Authorization",
    ...Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value])),
  }));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => values.get(String(name).toLowerCase()) ?? null },
    async json() { return payload; },
  };
}

/** 构造最小审批对话框 DOM 替身，覆盖焦点、忙碌和密码擦除边界。 */
function approvalDialogHarness({ busy = false, requiresStepUp = true } = {}) {
  const ownerDocument = { activeElement: null };
  /** 创建带事件和焦点行为的内存节点，不保存真实浏览器凭据或业务正文。 */
  const makeNode = ({ dataset = {}, value = "", disabled = false } = {}) => {
    const listeners = new Map();
    return {
      dataset,
      value,
      disabled,
      hidden: false,
      ownerDocument,
      addEventListener(type, handler) {
        const handlers = listeners.get(type) ?? [];
        handlers.push(handler);
        listeners.set(type, handlers);
      },
      async emit(type, event = {}) {
        for (const handler of listeners.get(type) ?? []) await handler(event);
      },
      focus() {
        ownerDocument.activeElement = this;
      },
    };
  };
  const form = makeNode();
  const reason = makeNode({ value: "approved_business_need", disabled: busy });
  const password = requiresStepUp ? makeNode({ value: "", disabled: busy }) : null;
  const headingClose = makeNode({ disabled: busy });
  const cancel = makeNode({ disabled: busy });
  const submit = makeNode({ disabled: busy });
  const trigger = makeNode({
    dataset: { approvalAction: "approve", approvalTask: APPROVAL_ID },
  });
  const focusable = [headingClose, reason, ...(password ? [password] : []), cancel, submit];
  const dialog = makeNode({
    dataset: {
      approvalTask: APPROVAL_ID,
      approvalDialogAction: "approve",
      approvalBusy: busy ? "true" : "false",
    },
  });
  dialog.open = false;
  dialog.showModalCalls = 0;
  dialog.closeCalls = 0;
  dialog.showModal = () => {
    dialog.showModalCalls += 1;
    dialog.open = true;
  };
  dialog.close = () => {
    dialog.closeCalls += 1;
    dialog.open = false;
  };
  dialog.querySelector = (selector) => {
    if (selector === "[data-approval-password]") return password;
    if (selector === "[data-approval-reason]") return reason;
    if (selector === "button[type='submit']:not([disabled])") return busy ? null : submit;
    return null;
  };
  dialog.querySelectorAll = () => focusable;
  const page = {
    isConnected: true,
    removed: false,
    matches: (selector) => selector === "[data-approvals-root]",
    querySelector(selector) {
      if (selector === "[data-approval-dialog]") return dialog;
      if (selector === "[data-approval-action-form]") return form;
      if (selector === "[data-approval-password]") return password;
      if (selector === "[data-approval-reason]") return reason;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-approval-dialog-close]") return [headingClose, cancel];
      if (selector === "[data-approval-action]") return [trigger];
      return [];
    },
    remove() {
      this.removed = true;
    },
  };
  return {
    cancel,
    dialog,
    focusable,
    headingClose,
    ownerDocument,
    page,
    password,
    reason,
    submit,
    trigger,
  };
}

/** 在测试作用域替换倒计时并始终恢复全局计时器，避免跨用例状态污染。 */
async function withApprovalTimers(callback) {
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  globalThis.setInterval = () => 78;
  globalThis.clearInterval = () => {};
  try {
    return await callback();
  } finally {
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
  }
}

test("list and detail clients send private read requests and accept exact DTOs", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    const payload = calls.length === 1
      ? {
          approvals: [pendingSummary()],
          next_cursor: "opaque_cursor_a",
          request_id: "req_approval_list",
          trace_id: "trc_approval_list",
        }
      : pendingDetail();
    return httpResponse(payload);
  };

  const listed = await listApprovals({
    controlApiUrl: "http://control.local",
    token: "session-token",
    targetType: "session_risk_action",
  });
  const detail = await getApproval({
    controlApiUrl: "http://control.local",
    token: "session-token",
    approvalId: APPROVAL_ID,
  });

  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, "http://control.local/approvals?status=pending&target_type=session_risk_action&limit=50");
  assert.equal(calls[1].url, `http://control.local/approvals/${APPROVAL_ID}`);
  for (const call of calls) {
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.credentials, "omit");
    assert.equal(call.options.redirect, "error");
    assert.equal(call.options.headers.Authorization, "Bearer session-token");
  }
  assert.equal(listed.approvals[0].approval_id, APPROVAL_ID);
  assert.equal(detail.target_summary.target_id, "risk_target_a");
  assert.equal(Object.isFrozen(listed), true);
  assert.equal(Object.isFrozen(detail), true);
});

test("step-up requires HTTP 200 and decision carries one idempotency key without tenant facts", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return calls.length === 1
      ? httpResponse({
          step_up_token: "s".repeat(48),
          expires_at: "2026-08-11T00:05:00Z",
          request_id: "req_approval_step_up",
          trace_id: "trc_approval_step_up",
        }, { status: 200 })
      : httpResponse(approvedActionPayload());
  };

  const challenge = await stepUpApproval({
    controlApiUrl: "http://control.local",
    token: "session-token",
    approvalId: APPROVAL_ID,
    action: "approve",
    password: "in-memory-password-canary",
  });
  const result = await decideApproval({
    controlApiUrl: "http://control.local",
    token: "session-token",
    approvalId: APPROVAL_ID,
    action: "approve",
    expectedRevision: 1,
    reasonCode: "approved_business_need",
    idempotencyKey: "123e4567-e89b-42d3-a456-426614174000",
    stepUpToken: challenge.step_up_token,
  });

  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    action: "approve",
    password: "in-memory-password-canary",
  });
  assert.equal(calls[0].options.headers["Idempotency-Key"], undefined);
  assert.equal(calls[1].options.headers["Idempotency-Key"], "123e4567-e89b-42d3-a456-426614174000");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    expected_revision: 1,
    reason_code: "approved_business_need",
    step_up_token: "s".repeat(48),
  });
  assert.doesNotMatch(calls[1].options.body, /tenant_id|actor_id|permission_codes/);
  assert.equal(result.approval.status, "approved");

  globalThis.fetch = async () => httpResponse({
    step_up_token: "s".repeat(48),
    expires_at: "2026-08-11T00:05:00Z",
    request_id: "req_approval_step_up_201",
    trace_id: "trc_approval_step_up_201",
  }, { status: 201 });
  await assert.rejects(
    () => stepUpApproval({
      controlApiUrl: "http://control.local",
      token: "session-token",
      approvalId: APPROVAL_ID,
      action: "approve",
      password: "one-use",
    }),
    (error) => error instanceof AuthApiError && error.code === "approval.response_invalid",
  );
});

test("clients reject extra DTO fields, semantic drift and non-private response headers", async () => {
  assert.throws(
    () => normalizeApprovalList({
      approvals: [pendingSummary()],
      next_cursor: null,
      request_id: "req_list_extra",
      trace_id: "trc_list_extra",
      tenant_id: TENANT_ID,
    }),
    (error) => error.code === "approval.response_invalid",
  );
  assert.throws(
    () => normalizeApprovalDetail({ ...pendingDetail(), allowed_actions: ["approve", "approve"] }, APPROVAL_ID),
    (error) => error.code === "approval.response_invalid",
  );
  assert.throws(
    () => normalizeAction({
      ...approvedActionPayload(),
      decision: { ...approvedActionPayload().decision, decision: "reject", reason_code: "risk_unacceptable" },
    }, APPROVAL_ID, "approve", 1),
    (error) => error.code === "approval.response_invalid",
  );

  const payload = {
    approvals: [pendingSummary()],
    next_cursor: null,
    request_id: "req_private_header",
    trace_id: "trc_private_header",
  };
  const invalidHeaders = [
    { "cache-control": "public" },
    { pragma: "" },
    { vary: "Origin" },
    { vary: "Authorization, Accept-Encoding" },
    { "x-request-id": "req_mismatch" },
    { "x-trace-id": "" },
  ];
  for (const headers of invalidHeaders) {
    globalThis.fetch = async () => httpResponse(payload, { headers });
    await assert.rejects(
      () => listApprovals({ controlApiUrl: "http://control.local", token: "token" }),
      (error) => error instanceof AuthApiError && error.code === "approval.response_invalid",
    );
  }
});

test("detail accepts asynchronous writeback progress without rewriting the decision snapshot", () => {
  const decision = {
    ...approvedActionPayload().decision,
    writeback_status: "pending",
  };
  for (const writebackStatus of ["completed", "failed"]) {
    const approval = {
      ...approvedActionPayload().approval,
      writeback_status: writebackStatus,
    };
    const detail = pendingDetail({
      approval,
      allowed_actions: [],
      decisions: [decision],
    });
    assert.equal(
      normalizeApprovalDetail(detail, APPROVAL_ID).approval.writeback_status,
      writebackStatus,
    );
  }

  const invalid = pendingDetail({
    approval: { ...approvedActionPayload().approval, writeback_status: "failed" },
    allowed_actions: [],
    decisions: [{ ...decision, writeback_status: "completed" }],
  });
  assert.throws(
    () => normalizeApprovalDetail(invalid, APPROVAL_ID),
    (error) => error.code === "approval.response_invalid",
  );
});

test("page owner and actions intersect approval permissions with the target domain", () => {
  const readOnly = authContext({
    permissions: [PERMISSIONS.APPROVAL_READ, PERMISSIONS.SESSION_RISK_READ],
  });
  const readOwner = approvalPageOwner(readOnly, shellState(readOnly));
  assert.ok(readOwner);
  assert.equal(readOwner.canDecide, false);
  assert.deepEqual(readOwner.readableTargets, ["session_risk_action"]);
  assert.deepEqual(readOwner.writableTargets, []);
  assert.equal(approvalActionAccess(pendingSummary(), readOnly, "approve"), false);

  const noTargetWrite = authContext({
    permissions: [
      PERMISSIONS.APPROVAL_READ,
      PERMISSIONS.APPROVAL_DECIDE,
      PERMISSIONS.SESSION_RISK_READ,
    ],
  });
  assert.equal(approvalActionAccess(pendingSummary(), noTargetWrite, "approve"), false);

  const full = authContext();
  const owner = approvalPageOwner(full, shellState(full));
  assert.deepEqual(owner.readableTargets, ["budget_block_release", "session_risk_action"]);
  assert.deepEqual(owner.writableTargets, ["budget_block_release", "session_risk_action"]);
  assert.equal(approvalActionAccess(pendingSummary(), full, "approve"), true);

  const missingBase = authContext({ permissions: [PERMISSIONS.SESSION_RISK_READ] });
  assert.equal(approvalPageOwner(missingBase, shellState(missingBase)), null);
});

test("owner drift aborts stale generations and private cursors stay outside snapshots", () => {
  let cleared = 0;
  const state = createApprovalPageState({ clearDom: () => { cleared += 1; } });
  const contextA = authContext();
  const ownerA = approvalPageOwner(contextA, shellState(contextA));
  assert.equal(state.enter(ownerA), true);
  const operationA = state.beginList(ownerA, 10);
  assert.ok(operationA);
  assert.equal(state.acceptList(operationA, {
    approvals: [pendingSummary()],
    next_cursor: "private_cursor_canary",
  }, ownerA, 10), true);
  state.finishList(operationA);
  assert.equal(state.snapshot().canNext, true);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /private_cursor_canary/);

  const pagingOperation = state.beginList(ownerA, 11);
  const contextB = authContext({
    tenantId: "tenant_approval_b",
    userId: "usr_approval_b",
    fingerprint: "approval_fingerprint_b",
  });
  const ownerB = approvalPageOwner(contextB, shellState(contextB));
  assert.equal(state.enter(ownerB), true);
  assert.equal(pagingOperation.signal.aborted, true);
  assert.equal(state.acceptList(pagingOperation, {
    approvals: [pendingSummary()],
    next_cursor: null,
  }, ownerA, 11), false);
  assert.ok(cleared >= 1);
});

test("approval action dialog uses showModal and chooses risk-appropriate initial focus", async () => {
  await withApprovalTimers(async () => {
    const highRisk = approvalDialogHarness();
    const openDescriptions = [];
    assert.equal(bindApprovalInteractions({
      onOpenAction(description) { openDescriptions.push(description); },
    }, highRisk.page), true);
    assert.equal(highRisk.dialog.showModalCalls, 1);
    assert.equal(highRisk.dialog.open, true);
    assert.equal(highRisk.ownerDocument.activeElement, highRisk.password);
    await highRisk.trigger.emit("click");
    assert.deepEqual(openDescriptions, [{ approvalId: APPROVAL_ID, action: "approve" }]);
    assert.equal(Object.isFrozen(openDescriptions[0]), true);

    const standard = approvalDialogHarness({ requiresStepUp: false });
    assert.equal(bindApprovalInteractions({}, standard.page), true);
    assert.equal(standard.dialog.showModalCalls, 1);
    assert.equal(standard.ownerDocument.activeElement, standard.reason);
  });

  const context = authContext();
  const shell = shellState(context);
  const owner = approvalPageOwner(context, shell);
  const state = createApprovalPageState();
  state.enter(owner);
  const operation = state.beginList(owner, 31);
  state.acceptList(operation, { approvals: [pendingSummary()], next_cursor: null }, owner, 31);
  state.finishList(operation);
  state.selectApproval(APPROVAL_ID);
  const detailOperation = state.beginDetail(owner, 32);
  state.acceptDetail(detailOperation, pendingDetail(), owner, 32);
  state.finishDetail(detailOperation);
  state.openAction(owner, "approve");
  const html = renderApprovals(state.snapshot(), context, shell);
  assert.match(html, /<dialog class="approval-dialog" data-approval-dialog/);
  assert.doesNotMatch(html, /<dialog[^>]*\sopen(?:\s|=|>)/);
  assert.match(html, new RegExp(`data-approval-task="${APPROVAL_ID}"`));
});

test("approval dialog contains Tab and closes idle Escape only after clearing password", async () => {
  await withApprovalTimers(async () => {
    const harness = approvalDialogHarness();
    harness.password.value = "escape-password-canary";
    const closes = [];
    bindApprovalInteractions({
      onCloseAction(description) {
        closes.push(description);
      },
    }, harness.page);

    harness.ownerDocument.activeElement = harness.submit;
    let forwardPrevented = false;
    await harness.dialog.emit("keydown", {
      key: "Tab",
      shiftKey: false,
      preventDefault() { forwardPrevented = true; },
    });
    assert.equal(forwardPrevented, true);
    assert.equal(harness.ownerDocument.activeElement, harness.headingClose);

    let backwardPrevented = false;
    await harness.dialog.emit("keydown", {
      key: "Tab",
      shiftKey: true,
      preventDefault() { backwardPrevented = true; },
    });
    assert.equal(backwardPrevented, true);
    assert.equal(harness.ownerDocument.activeElement, harness.submit);

    let escapePrevented = false;
    await harness.dialog.emit("keydown", {
      key: "Escape",
      preventDefault() { escapePrevented = true; },
    });
    assert.equal(escapePrevented, true);
    assert.equal(harness.password.value, "");
    assert.equal(harness.dialog.closeCalls, 1);
    assert.deepEqual(closes, [{ approvalId: APPROVAL_ID, action: "approve" }]);

    const nativeCancel = approvalDialogHarness();
    nativeCancel.password.value = "cancel-password-canary";
    let nativeCancelCalls = 0;
    bindApprovalInteractions({
      onCloseAction() { nativeCancelCalls += 1; },
    }, nativeCancel.page);
    let cancelPrevented = false;
    await nativeCancel.dialog.emit("cancel", {
      preventDefault() { cancelPrevented = true; },
    });
    assert.equal(cancelPrevented, true);
    assert.equal(nativeCancelCalls, 1);
    assert.equal(nativeCancel.password.value, "");
    assert.equal(nativeCancel.dialog.closeCalls, 1);
  });
});

test("submitting approval dialog prevents cancel and clearApprovalDom closes and erases it", async () => {
  await withApprovalTimers(async () => {
    const harness = approvalDialogHarness({ busy: true });
    harness.password.value = "busy-password-canary";
    let closeCalls = 0;
    bindApprovalInteractions({
      isActionBusy: () => true,
      onCloseAction() { closeCalls += 1; },
    }, harness.page);
    let cancelPrevented = false;
    await harness.dialog.emit("cancel", {
      preventDefault() { cancelPrevented = true; },
    });
    assert.equal(cancelPrevented, true);
    let escapePrevented = false;
    await harness.dialog.emit("keydown", {
      key: "Escape",
      preventDefault() { escapePrevented = true; },
    });
    assert.equal(escapePrevented, true);
    assert.equal(closeCalls, 0);
    assert.equal(harness.dialog.open, true);
    assert.equal(harness.password.value, "busy-password-canary");

    assert.equal(clearApprovalDom(harness.page), true);
    assert.equal(harness.password.value, "");
    assert.equal(harness.dialog.closeCalls, 1);
    assert.equal(harness.page.removed, true);
  });
});

test("approval focus restoration requires the same owner, task and action", () => {
  let focused = 0;
  const button = {
    dataset: { approvalTask: APPROVAL_ID, approvalAction: "reject" },
    disabled: false,
    focus() { focused += 1; },
  };
  const page = {
    matches: (selector) => selector === "[data-approvals-root]",
    querySelector: () => null,
    querySelectorAll: (selector) => selector === "[data-approval-action]" ? [button] : [],
  };
  const description = Object.freeze({
    approvalId: APPROVAL_ID,
    action: "reject",
    ownerKey: "owner-a",
  });
  assert.equal(restoreApprovalActionFocus(description, "owner-b", page), false);
  assert.equal(focused, 0);
  assert.equal(restoreApprovalActionFocus(description, "owner-a", page), true);
  assert.equal(focused, 1);
  button.dataset.approvalTask = `apr_${"c".repeat(32)}`;
  assert.equal(restoreApprovalActionFocus(description, "owner-a", page), false);
  assert.equal(focused, 1);
});

test("password is erased immediately and action secrets never enter snapshot, DOM, Storage or logs", async () => {
  const passwordInput = { value: "password-canary" };
  let submitHandler;
  const form = {
    addEventListener(type, handler) {
      if (type === "submit") submitHandler = handler;
    },
  };
  const page = {
    isConnected: true,
    matches: () => true,
    querySelector(selector) {
      if (selector === "[data-approval-action-form]") return form;
      if (selector === "[data-approval-password]") return passwordInput;
      return null;
    },
    querySelectorAll: () => [],
  };
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  globalThis.setInterval = () => 77;
  globalThis.clearInterval = () => {};
  try {
    let received;
    assert.equal(bindApprovalInteractions({
      async onSubmitAction(password) {
        assert.equal(passwordInput.value, "");
        received = password;
      },
    }, page), true);
    await submitHandler({ preventDefault() {} });
    assert.equal(received, "password-canary");
    assert.equal(passwordInput.value, "");
  } finally {
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
  }

  const context = authContext();
  const shell = shellState(context);
  const owner = approvalPageOwner(context, shell);
  const state = createApprovalPageState({
    createIdempotencyKey: () => "123e4567-e89b-42d3-a456-426614174000",
  });
  state.enter(owner);
  const listOperation = state.beginList(owner, 20);
  state.acceptList(listOperation, { approvals: [pendingSummary()], next_cursor: null }, owner, 20);
  state.finishList(listOperation);
  assert.equal(state.selectApproval(APPROVAL_ID), true);
  const detailOperation = state.beginDetail(owner, 21);
  state.acceptDetail(detailOperation, pendingDetail(), owner, 21);
  state.finishDetail(detailOperation);
  assert.equal(state.openAction(owner, "approve"), true);
  const actionOperation = state.beginAction(owner);
  assert.ok(actionOperation);
  const snapshot = JSON.stringify(state.snapshot());
  assert.doesNotMatch(snapshot, /123e4567|password-canary|step[_-]?up[_-]?token/i);
  const html = renderApprovals(state.snapshot(), context, shell);
  assert.doesNotMatch(html, /123e4567|password-canary|step[_-]?up[_-]?token/i);

  let removed = false;
  const sensitiveNode = { textContent: "sensitive-dom-canary" };
  const removablePage = {
    matches: () => true,
    querySelector: (selector) => selector === "[data-approval-password]" ? passwordInput : null,
    querySelectorAll: (selector) => selector === "[data-approval-sensitive]" ? [sensitiveNode] : [],
    remove: () => { removed = true; },
  };
  passwordInput.value = "second-password-canary";
  assert.equal(clearApprovalDom(removablePage), true);
  assert.equal(passwordInput.value, "");
  assert.equal(sensitiveNode.textContent, "");
  assert.equal(removed, true);

  const implementation = await Promise.all([
    readFile(new URL("../../src/lib/approvals.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/approvals/approvalPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/approvals/bindApprovals.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/approvals/renderApprovals.js", import.meta.url), "utf8"),
  ]).then((parts) => parts.join("\n"));
  assert.doesNotMatch(implementation, /localStorage|sessionStorage|indexedDB|caches\.open|console\.(?:log|error|warn)/);
});

test("hash route accepts one canonical approval_id and rejects extras or credential-like values", () => {
  const valid = `#approvals?approval_id=${APPROVAL_ID}`;
  assert.deepEqual(parseNavigationHash(valid), { value: "approvals", rejected: false });
  assert.equal(parseApprovalCardHash(valid), APPROVAL_ID);
  assert.equal(parseApprovalCardHash(`#approvals?approval_id=${APPROVAL_ID}&token=secret`), "");
  assert.equal(parseApprovalCardHash("#approvals?approval_id=apr_not_hex"), "");
  assert.equal(parseApprovalCardHash(`#usage?approval_id=${APPROVAL_ID}`), "");
  assert.equal(parseNavigationHash(`#approvals?approval_id=${APPROVAL_ID}&approval_id=${APPROVAL_ID}`).rejected, true);
});

test("Console shell renders approvals only from the exact backend navigation owner", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = approvalPageOwner(context, shell);
  const state = createApprovalPageState();
  state.enter(owner);
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: context,
    consoleShellState: shell,
    approvalsState: state.snapshot(),
  });
  assert.match(container.innerHTML, /approvals-panel/);
  assert.match(container.innerHTML, /data-console-experience-approval-filter/);

  const denied = authContext({ permissions: [PERMISSIONS.SESSION_RISK_READ] });
  const deniedContainer = { innerHTML: "" };
  renderDeveloperStart(deniedContainer, {
    authContext: denied,
    consoleShellState: shellState(denied),
    approvalsState: state.snapshot(),
  });
  assert.doesNotMatch(deniedContainer.innerHTML, /data-console-experience-approval-filter/);
});

test("main lifecycle and CSS freeze cleanup, detail refresh and 1280/390 layouts", async () => {
  const [main, css, binder, renderer, pageState] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/console-experience.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/approvals/bindApprovals.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/approvals/renderApprovals.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/approvals/approvalPageState.js", import.meta.url), "utf8"),
  ]);

  assert.doesNotMatch(main, /approvalPageState|loadApprovalsState|lib\/approvals/);
  assert.match(main, /addEventListener\("hashchange"[\s\S]*workspaceConsoleExperienceController\.leave\(\)[\s\S]*bindNavigationHashRefresh/);
  assert.match(main, /pagehide[\s\S]*workspaceConsoleExperienceController\.leave\(\)/);
  assert.match(main, /case "approvals":[\s\S]*loadWorkspaceConsoleExperienceState\([\s\S]*"approvals"/);
  assert.match(main, /onApprovalFilter\(filters\)[\s\S]*workspaceConsoleExperienceController\.configureApprovals\(filters\)/);
  assert.match(pageState, /clearDom\(\)[\s\S]*operation\.controller\.abort\(\)/);
  assert.match(binder, /passwordInput\.value\s*=\s*""[\s\S]*onSubmitAction/);
  assert.match(binder, /addEventListener\("cancel", requestClose\)/);
  assert.match(binder, /dialog\.showModal\(\)/);
  assert.match(binder, /event\.key === "Tab"[\s\S]*trapDialogFocus/);
  assert.doesNotMatch(renderer, /<dialog[^`\n>]*\sopen(?:\s|=|>)/);
  assert.match(css, /\.console-experience-table-wrap\s*\{[^}]*overflow-x:\s*auto/s);
  assert.match(css, /\.console-experience-pagination button\s*\{[^}]*width:\s*36px;[^}]*min-width:\s*36px/s);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*\.console-experience-filter\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/s);
});
