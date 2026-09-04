/** PRD-079 Console 专项验证请求合同、CAS、模态框私有状态与权限失败关闭边界。 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  datetimeLocalToUtc,
  planRequestFromFormData,
} from "../../src/features/plan-billing/bindPlanBilling.js";
import {
  createPlanPageState,
  planPageOwner,
} from "../../src/features/plan-billing/planBillingPageState.js";
import { renderPlanManagement } from "../../src/features/plan-billing/renderPlanBilling.js";
import {
  canonicalCnyInput,
  installWorkspaceQuotaBusyEscapeGuard as installPlanBillingBusyEscapeGuard,
  quotaAdjustmentRequestFromFormData,
  trapWorkspaceQuotaDialogFocus as trapPlanBillingDialogFocus,
} from "../../src/features/workspace-quota/bindWorkspaceQuota.js";
import {
  createQuotaPageState,
  quotaPageOwner,
} from "../../src/features/workspace-quota/workspaceQuotaPageState.js";
import { renderWorkspaceQuota } from "../../src/features/workspace-quota/renderWorkspaceQuota.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  getCurrentPlan,
  normalizePlanResponse,
  patchCurrentPlan,
} from "../../src/lib/planBilling.js";
import {
  createQuotaAdjustment,
  getQuotaBuckets,
  normalizeQuotaAdjustmentResponse,
  normalizeQuotaBucketsResponse,
} from "../../src/lib/workspaceQuota.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 固定租户、套餐和完整权限集合用于构造可重复的授权边界测试样本。
const TENANT_ID = "tenant_plan_a";
const PLAN_ID = `pln_${"a".repeat(32)}`;
const FULL_PERMISSIONS = [
  PERMISSIONS.BILLING_PLAN_READ,
  PERMISSIONS.BILLING_PLAN_WRITE,
  PERMISSIONS.BILLING_QUOTA_READ,
  PERMISSIONS.BILLING_QUOTA_WRITE,
];
// 四类额度桶顺序必须与后端合同一致，测试不得自行增删业务类型。
const TYPES = ["trial", "free", "gift", "paid"];

/** 构造测试认证上下文，并通过权限快照生成与生产一致的授权身份。 */
function authContext({
  permissions = FULL_PERMISSIONS,
  tenantId = TENANT_ID,
  sessionId = "session_plan_a",
  revision = 15,
  fingerprint = "plan_billing_fingerprint_15",
} = {}) {
  // 权限快照是测试授权身份的唯一来源，禁止直接伪造派生权限字段。
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, display_name: "Plan tenant" },
    user: {
      tenant_id: tenantId,
      user_id: "user_plan_a",
      email: "plan@example.test",
      role: "finance_admin",
    },
    session: { session_id: sessionId, session_revision: 3 },
  }, permissions, {
    permissionRevision: revision,
    authorizationFingerprint: fingerprint,
  });
}

/** 构造指定页面的控制台导航快照，保持权限修订与指纹镜像一致。 */
function shellState(page, context = authContext()) {
  const read = page === "plans" ? PERMISSIONS.BILLING_PLAN_READ : PERMISSIONS.BILLING_QUOTA_READ;
  return {
    state: "ok",
    activeNavigationItemId: page,
    navigation: {
      active_navigation_item_id: page,
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: page,
        target_section: page,
        label: page,
        group: "调用与成本",
        visible: true,
        disabled: false,
        required_permissions: [read],
      }],
    },
  };
}

/** 构造满足套餐响应合同的基准事实，并允许测试覆盖目标字段。 */
function plan(overrides = {}) {
  return {
    plan_id: PLAN_ID,
    tenant_id: TENANT_ID,
    display_name: "标准套餐",
    base_tier: "free",
    effective_tier: "free",
    status: "active",
    trial_status: "not_started",
    trial_started_at: null,
    trial_ends_at: null,
    trial_cycle: 0,
    state_revision: 1,
    quota_generation: 1,
    created_at: "2026-08-14T08:00:00Z",
    updated_at: "2026-08-14T08:00:00Z",
    ...overrides,
  };
}

/** 构造指定类型的额度桶事实，保持金额守恒和稳定标识格式。 */
function bucket(quotaType, overrides = {}) {
  const index = TYPES.indexOf(quotaType) + 1;
  return {
    quota_bucket_id: `qbk_${String(index).repeat(32)}`,
    tenant_id: TENANT_ID,
    plan_id: PLAN_ID,
    quota_type: quotaType,
    granted: "10.000000",
    consumed: "2.000000",
    reserved: "1.000000",
    available: "7.000000",
    status: quotaType === "trial" ? "expired" : "active",
    expires_at: null,
    state_revision: 1,
    allocation_consumed: "2.000000",
    allocation_count: 2,
    created_at: "2026-08-14T08:00:00Z",
    updated_at: "2026-08-14T08:00:00Z",
    ...overrides,
  };
}

/** 构造套餐接口完整响应信封，供 exact 合同与租户边界测试使用。 */
function planPayload(overrides = {}) {
  return {
    plan: plan(),
    quota_buckets: TYPES.map((type) => bucket(type)),
    trial_cycle_changed: false,
    request_id: "request_plan_get_a",
    trace_id: "trace_plan_get_a",
    ...overrides,
  };
}

/** 构造额度调整公开事实，不包含提交时的私有原因或幂等键。 */
function adjustment(overrides = {}) {
  return {
    quota_adjustment_id: `qadj_${"b".repeat(32)}`,
    tenant_id: TENANT_ID,
    plan_id: PLAN_ID,
    quota_type: "paid",
    direction: "credit",
    amount: "3.000000",
    actor_user_id: "user_plan_a",
    operation_request_id: "request_quota_operation_a",
    resulting_bucket_revision: 2,
    created_at: "2026-08-14T09:00:00Z",
    ...overrides,
  };
}

/** 构造额度查询完整响应信封，保持四类桶与链路标识齐全。 */
function quotaPayload(overrides = {}) {
  return {
    plan: plan(),
    quota_buckets: TYPES.map((type) => bucket(type)),
    overage: { amount: "0.500000", settlement_count: 1 },
    recent_adjustments: [adjustment()],
    request_id: "request_quota_get_a",
    trace_id: "trace_quota_get_a",
    ...overrides,
  };
}

/** 构造带严格缓存和链路响应头的 JSON 测试响应。 */
function jsonResponse(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
      ...headers,
    },
  });
}

/** 在单个测试回调期间替换 fetch，并保证结束后恢复全局实现。 */
async function withFetch(handler, callback) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  try {
    return await callback();
  } finally {
    globalThis.fetch = original;
  }
}

test("PRD-079 permissions remain four independent backend facts", () => {
  assert.equal(PERMISSIONS.BILLING_PLAN_READ, "billing.plan.read");
  assert.equal(PERMISSIONS.BILLING_PLAN_WRITE, "billing.plan.write");
  assert.equal(PERMISSIONS.BILLING_QUOTA_READ, "billing.quota.read");
  assert.equal(PERMISSIONS.BILLING_QUOTA_WRITE, "billing.quota.write");
});

test("Plan and Quota GET use no tenant query and require exact no-store envelopes", async () => {
  const calls = [];
  await withFetch(async (url, init) => {
    calls.push({ url, init });
    return url.endsWith("/plans") ? jsonResponse(planPayload()) : jsonResponse(quotaPayload());
  }, async () => {
    const currentPlan = await getCurrentPlan({
      controlApiUrl: "http://control.test",
      token: "synthetic-token",
      tenantId: TENANT_ID,
      requestId: "request_plan_attempt_a",
    });
    const quotas = await getQuotaBuckets({
      controlApiUrl: "http://control.test",
      token: "synthetic-token",
      tenantId: TENANT_ID,
      requestId: "request_quota_attempt_a",
    });
    assert.equal(currentPlan.plan.plan_id, PLAN_ID);
    assert.equal(quotas.overage.amount, "0.500000");
  });
  assert.deepEqual(calls.map((call) => call.url), [
    "http://control.test/plans",
    "http://control.test/quota-buckets",
  ]);
  assert.ok(calls.every((call) => call.init.cache === "no-store" && call.init.credentials === "omit"));
  assert.ok(calls.every((call) => !call.url.includes("tenant")));

  await withFetch(async () => jsonResponse(planPayload(), 200, { "Cache-Control": "public" }), async () => {
    await assert.rejects(
      () => getCurrentPlan({
        controlApiUrl: "http://control.test",
        token: "synthetic-token",
        tenantId: TENANT_ID,
      }),
      (error) => error instanceof AuthApiError && error.code === "plan.response_invalid",
    );
  });
});

test("exact DTO validation rejects unknown fields, owner drift and broken amount equations", () => {
  assert.equal(normalizePlanResponse({ ...planPayload(), secret: "forbidden" }, TENANT_ID), null);
  assert.equal(normalizePlanResponse(planPayload({
    plan: plan({ tenant_id: "tenant_other" }),
  }), TENANT_ID), null);
  assert.equal(normalizeQuotaBucketsResponse(quotaPayload({
    quota_buckets: TYPES.map((type) => bucket(type, type === "paid" ? { available: "8.000000" } : {})),
  }), TENANT_ID), null);
  assert.equal(normalizeQuotaBucketsResponse(quotaPayload({
    overage: { amount: "0.500000", settlement_count: 1, paid: "0.500000" },
  }), TENANT_ID), null);
});

test("Plan PATCH sends only the five CAS fields and accepts the next revision", async () => {
  let captured;
  const request = {
    expected_revision: 1,
    display_name: "企业套餐",
    base_tier: "paid",
    status: "active",
    trial_ends_at: null,
  };
  const responsePayload = planPayload({
    plan: plan({
      display_name: "企业套餐",
      base_tier: "paid",
      effective_tier: "paid",
      state_revision: 2,
      updated_at: "2026-08-14T09:00:00Z",
    }),
    quota_buckets: TYPES.map((type) => bucket(type, { state_revision: 2 })),
    request_id: "request_plan_patch_a",
    trace_id: "trace_plan_patch_a",
  });
  await withFetch(async (url, init) => {
    captured = { url, init };
    return jsonResponse(responsePayload);
  }, async () => {
    const result = await patchCurrentPlan({
      controlApiUrl: "http://control.test",
      token: "synthetic-token",
      tenantId: TENANT_ID,
      request,
      requestId: "request_plan_patch_attempt_a",
    });
    assert.equal(result.plan.state_revision, 2);
  });
  assert.equal(captured.url, "http://control.test/plans");
  assert.equal(captured.init.method, "PATCH");
  assert.deepEqual(Object.keys(JSON.parse(captured.init.body)).sort(), [
    "base_tier", "display_name", "expected_revision", "status", "trial_ends_at",
  ]);
  assert.equal(JSON.parse(captured.init.body).tenant_id, undefined);
});

test("Plan PATCH accepts preserved trial evidence when an active trial is ended", async () => {
  const request = {
    expected_revision: 4,
    display_name: "企业套餐",
    base_tier: "paid",
    status: "active",
    trial_ends_at: null,
  };
  const responsePayload = planPayload({
    plan: plan({
      display_name: "企业套餐",
      base_tier: "paid",
      effective_tier: "paid",
      trial_status: "expired",
      trial_started_at: "2026-08-10T08:00:00Z",
      trial_ends_at: "2026-08-20T08:00:00Z",
      trial_cycle: 1,
      state_revision: 5,
      updated_at: "2026-08-14T09:00:00Z",
    }),
    quota_buckets: TYPES.map((type) => bucket(type, {
      status: type === "trial" ? "expired" : "active",
      expires_at: type === "trial" ? "2026-08-20T08:00:00Z" : null,
      state_revision: 2,
    })),
    request_id: "request_plan_trial_end_a",
    trace_id: "trace_plan_trial_end_a",
  });
  await withFetch(async () => jsonResponse(responsePayload), async () => {
    const result = await patchCurrentPlan({
      controlApiUrl: "http://control.test",
      token: "synthetic-token",
      tenantId: TENANT_ID,
      request,
      requestId: "request_plan_trial_end_attempt_a",
    });
    assert.equal(result.plan.trial_status, "expired");
    assert.equal(result.plan.trial_ends_at, "2026-08-20T08:00:00Z");
  });
  assert.equal(normalizePlanResponse(planPayload({
    plan: plan({ trial_status: "expired", trial_cycle: 1 }),
  }), TENANT_ID), null);
});

test("Quota POST freezes one visible idempotency key and validates the returned owner", async () => {
  let captured;
  const request = {
    quota_type: "paid",
    direction: "credit",
    amount: "3.000000",
    reason: "quota_adjustment",
    expected_bucket_revision: 1,
  };
  const responsePayload = {
    adjustment: adjustment(),
    quota_bucket: bucket("paid", {
      granted: "13.000000",
      available: "10.000000",
      state_revision: 2,
      updated_at: "2026-08-14T09:00:00Z",
    }),
    plan: plan({ state_revision: 2, updated_at: "2026-08-14T09:00:00Z" }),
    idempotent_replay: false,
    request_id: "request_quota_post_a",
    trace_id: "trace_quota_post_a",
  };
  await withFetch(async (url, init) => {
    captured = { url, init };
    return jsonResponse(responsePayload, 201);
  }, async () => {
    const result = await createQuotaAdjustment({
      controlApiUrl: "http://control.test",
      token: "synthetic-token",
      tenantId: TENANT_ID,
      request,
      idempotencyKey: "quota-adjustment-test-a",
      requestId: "request_quota_post_attempt_a",
    });
    assert.equal(result.quota_bucket.state_revision, 2);
  });
  assert.equal(captured.url, "http://control.test/quota-adjustments");
  assert.equal(captured.init.headers["Idempotency-Key"], "quota-adjustment-test-a");
  assert.deepEqual(Object.keys(JSON.parse(captured.init.body)).sort(), [
    "amount", "direction", "expected_bucket_revision", "quota_type", "reason",
  ]);
  assert.equal(JSON.parse(captured.init.body).tenant_id, undefined);

  // 同一 key 的 200 重放返回原调整事实和当前 Bucket；后续合法写入可推进当前 revision。
  const replayPayload = {
    ...responsePayload,
    quota_bucket: bucket("paid", {
      granted: "13.000000",
      available: "10.000000",
      state_revision: 5,
      updated_at: "2026-08-14T09:05:00Z",
    }),
    plan: plan({ state_revision: 5, updated_at: "2026-08-14T09:05:00Z" }),
    idempotent_replay: true,
    request_id: "request_quota_replay_a",
    trace_id: "trace_quota_replay_a",
  };
  await withFetch(async () => jsonResponse(replayPayload, 200), async () => {
    const replayed = await createQuotaAdjustment({
      controlApiUrl: "http://control.test",
      token: "synthetic-token",
      tenantId: TENANT_ID,
      request,
      idempotencyKey: "quota-adjustment-test-a",
      requestId: "request_quota_replay_attempt_a",
    });
    assert.equal(replayed.adjustment.resulting_bucket_revision, 2);
    assert.equal(replayed.quota_bucket.state_revision, 5);
  });

  assert.equal(normalizeQuotaAdjustmentResponse({
    ...replayPayload,
    idempotent_replay: false,
  }, TENANT_ID, request), null);
});

test("page owners fail closed across permission, navigation, tenant and revision drift", () => {
  const context = authContext();
  const plans = shellState("plans", context);
  const quotas = shellState("quota-buckets", context);
  assert.ok(planPageOwner(context, plans));
  assert.ok(planPageOwner(context, plans, { mode: "write" }));
  assert.ok(quotaPageOwner(context, quotas, { mode: "write" }));

  const readOnly = authContext({ permissions: [PERMISSIONS.BILLING_PLAN_READ] });
  assert.ok(planPageOwner(readOnly, shellState("plans", readOnly)));
  assert.equal(planPageOwner(readOnly, shellState("plans", readOnly), { mode: "write" }), null);
  assert.equal(planPageOwner(context, quotas), null);

  const changed = authContext({ revision: 16, fingerprint: "plan_billing_fingerprint_16" });
  assert.equal(planPageOwner(changed, plans), null);
  const otherTenant = authContext({
    tenantId: "tenant_plan_b",
    fingerprint: "plan_billing_fingerprint_tenant_b",
  });
  assert.equal(planPageOwner(otherTenant, plans), null);
});

test("Plan state preserves CAS draft, blocks shell replacement and rejects stale operations", () => {
  const context = authContext();
  const shell = shellState("plans", context);
  const owner = planPageOwner(context, shell);
  const writer = planPageOwner(context, shell, { mode: "write" });
  const state = createPlanPageState();
  assert.equal(state.enter(owner), true);
  const load = state.beginLoad(owner);
  const normalized = normalizePlanResponse(planPayload(), TENANT_ID);
  assert.equal(state.acceptLoad(load, normalized, owner), true);
  state.finishLoad(load);
  const request = {
    expected_revision: 1,
    display_name: "企业套餐",
    base_tier: "paid",
    status: "active",
    trial_ends_at: null,
  };
  assert.equal(state.replaceDraft(request), true);
  assert.equal(state.snapshot().dirty, true);
  assert.equal(state.blocksShellDomReplacement(), true);
  assert.equal(state.openConfirmation(writer, request), true);
  const save = state.beginSave(writer);
  assert.equal(save.request.expected_revision, 1);

  const changedContext = authContext({ sessionId: "session_plan_b" });
  const changedOwner = planPageOwner(changedContext, shellState("plans", changedContext));
  assert.equal(state.enter(changedOwner), true);
  assert.equal(save.controller.signal.aborted, true);
  assert.equal(state.acceptSave(save, normalized, writer), false);
  assert.equal(state.snapshot().plan, null);
});

test("Quota state never serializes reason or idempotency key and reuses both after unknown result", () => {
  const context = authContext();
  const shell = shellState("quota-buckets", context);
  const owner = quotaPageOwner(context, shell);
  const writer = quotaPageOwner(context, shell, { mode: "write" });
  let keyCalls = 0;
  const state = createQuotaPageState({
    createIdempotencyKey: () => {
      keyCalls += 1;
      return "quota-adjustment-private-key";
    },
  });
  state.enter(owner);
  const load = state.beginLoad(owner);
  const normalized = normalizeQuotaBucketsResponse(quotaPayload(), TENANT_ID);
  assert.equal(state.acceptLoad(load, normalized, owner), true);
  state.finishLoad(load);
  assert.equal(state.openAdjustment(writer, "paid"), true);
  const request = {
    quota_type: "paid",
    direction: "credit",
    amount: "3.000000",
    reason: "private_dialog_reason",
    expected_bucket_revision: 1,
  };
  const first = state.beginAdjustment(writer, request);
  assert.equal(first.idempotencyKey, "quota-adjustment-private-key");
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /private_dialog_reason|private-key/);
  assert.equal(state.rejectAdjustment(first, new AuthApiError({
    status: 503,
    code: "quota.unavailable",
    message: "ignored",
    requestId: "request_quota_retry_a",
    traceId: "trace_quota_retry_a",
  }), writer), true);
  state.finishAdjustment(first);
  const retry = state.beginAdjustment(writer, null, { retry: true });
  assert.equal(retry.idempotencyKey, first.idempotencyKey);
  assert.deepEqual(retry.request, first.request);
  assert.equal(keyCalls, 1);
  state.clear();
  assert.equal(retry.controller.signal.aborted, true);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /private_dialog_reason|private-key/);
});

test("form parsers canonicalize money and derive revision only from the current bucket", () => {
  assert.equal(canonicalCnyInput("12.3"), "12.300000");
  assert.equal(canonicalCnyInput("1e2"), "");
  const local = "2026-08-20T12:34:56";
  assert.match(datetimeLocalToUtc(local), /^2026-08-20T\d{2}:34:56Z$/);

  const planForm = new FormData();
  planForm.set("display_name", "企业套餐");
  planForm.set("base_tier", "paid");
  planForm.set("status", "active");
  planForm.set("trial_ends_at", "");
  assert.deepEqual(planRequestFromFormData(planForm, 1), {
    expected_revision: 1,
    display_name: "企业套餐",
    base_tier: "paid",
    status: "active",
    trial_ends_at: null,
  });

  const quotaForm = new FormData();
  quotaForm.set("quota_type", "gift");
  quotaForm.set("direction", "debit");
  quotaForm.set("amount", "1.25");
  quotaForm.set("reason", "quota_adjustment");
  const parsed = quotaAdjustmentRequestFromFormData(quotaForm, TYPES.map((type) => bucket(type)));
  assert.equal(parsed.amount, "1.250000");
  assert.equal(parsed.expected_bucket_revision, 1);
  assert.equal(parsed.quota_type, "gift");
});

test("rendering prunes write commands and never renders the private adjustment reason or key", () => {
  const readOnly = authContext({ permissions: [PERMISSIONS.BILLING_PLAN_READ] });
  const planShell = shellState("plans", readOnly);
  const planState = createPlanPageState();
  const planOwner = planPageOwner(readOnly, planShell);
  planState.enter(planOwner);
  const load = planState.beginLoad(planOwner);
  planState.acceptLoad(load, normalizePlanResponse(planPayload(), TENANT_ID), planOwner);
  planState.finishLoad(load);
  const readOnlyHtml = renderPlanManagement(planState.snapshot(), readOnly, planShell);
  assert.match(readOnlyHtml, /只读套餐视图/);
  assert.doesNotMatch(readOnlyHtml, /data-plan-form|data-plan-confirm-submit/);

  const context = authContext();
  const quotaShell = shellState("quota-buckets", context);
  const quotaState = createQuotaPageState({ createIdempotencyKey: () => "quota-adjustment-private-key" });
  const quotaOwner = quotaPageOwner(context, quotaShell);
  const quotaWriter = quotaPageOwner(context, quotaShell, { mode: "write" });
  quotaState.enter(quotaOwner);
  const quotaLoad = quotaState.beginLoad(quotaOwner);
  quotaState.acceptLoad(quotaLoad, normalizeQuotaBucketsResponse(quotaPayload(), TENANT_ID), quotaOwner);
  quotaState.finishLoad(quotaLoad);
  quotaState.openAdjustment(quotaWriter, "paid");
  const operation = quotaState.beginAdjustment(quotaWriter, {
    quota_type: "paid",
    direction: "credit",
    amount: "3.000000",
    reason: "private_dialog_reason",
    expected_bucket_revision: 1,
  });
  quotaState.rejectAdjustment(operation, new AuthApiError({
    status: 503,
    code: "quota.unavailable",
    message: "ignored",
    requestId: "request_quota_retry_b",
    traceId: "trace_quota_retry_b",
  }), quotaWriter);
  quotaState.finishAdjustment(operation);
  const quotaHtml = renderWorkspaceQuota(quotaState.snapshot(), context, quotaShell);
  assert.match(quotaHtml, /data-quota-adjustment-retry/);
  assert.doesNotMatch(quotaHtml, /private_dialog_reason|private-key/);
});

test("Dialog focus trap wraps first and last enabled controls", () => {
  const first = { offsetParent: {}, focused: false, focus() { this.focused = true; } };
  const last = { offsetParent: {}, focused: false, focus() { this.focused = true; } };
  const dialog = { querySelectorAll: () => [first, last] };
  let prevented = false;
  trapPlanBillingDialogFocus(dialog, {
    shiftKey: false,
    preventDefault() { prevented = true; },
  }, { activeElement: last });
  assert.equal(prevented, true);
  assert.equal(first.focused, true);
});

test("busy Dialog Escape guard survives node replacement and leaves idle Escape untouched", () => {
  const listeners = [];
  let busyDialog = { open: true };
  const documentRef = {
    addEventListener(type, listener, options) {
      listeners.push({ type, listener, options });
    },
    querySelector() {
      return busyDialog;
    },
  };
  assert.equal(installPlanBillingBusyEscapeGuard(documentRef), true);
  assert.equal(installPlanBillingBusyEscapeGuard(documentRef), false);
  assert.equal(listeners.length, 1);
  assert.deepEqual(listeners[0].options, { capture: true });

  let prevented = false;
  listeners[0].listener({ key: "Escape", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);

  // 重渲染后守卫每次查询当前节点；空闲 Dialog 不匹配 selector，保留原生关闭语义。
  busyDialog = null;
  prevented = false;
  listeners[0].listener({ key: "Escape", preventDefault() { prevented = true; } });
  assert.equal(prevented, false);
});

test("main lifecycle retires PAGE-110 while preserving Workspace quota cleanup and no-storage boundaries", async () => {
  const [mainSource, stateSource] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/workspace-quota/workspaceQuotaPageState.js", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /clearWorkspaceQuotaStateOnIdentityChange/);
  assert.match(mainSource, /itemId !== "quota-buckets"/);
  assert.match(mainSource, /blocksShellDomReplacement\(\)/);
  assert.doesNotMatch(mainSource, /planManagementPageState|loadPlanManagementState|\/plans/);
  assert.doesNotMatch(stateSource, /localStorage|sessionStorage|indexedDB/);
});
