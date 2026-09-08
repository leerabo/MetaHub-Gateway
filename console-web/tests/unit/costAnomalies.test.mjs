// PRD-053 Console 专项：冻结 API 白名单、BigInt 金额、页面 owner、敏感状态销毁和响应式渲染合同。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { AuthApiError } from "../../src/lib/auth.js";
import {
  applyCostAnomalyAction,
  formatCostMicros,
  formatCostRatioMillis,
  listCostAnomalies,
  shouldRetainCostAnomalyIdempotencyKey,
} from "../../src/lib/costAnomalies.js";
import {
  costAnomalyIdentitySnapshot,
  createCostAnomalyPageState,
  isCostAnomalyAbortError,
} from "../../src/features/cost-anomalies/costAnomalyPageState.js";
import { renderCostAnomalies } from "../../src/features/cost-anomalies/renderCostAnomalies.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造只含公开 JSON 的 fetch 响应，避免测试依赖浏览器网络实现。 */
function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    headers: { get: () => null },
    async json() {
      return payload;
    },
  };
}

/** 构造 admin 身份；tenant、用户、角色和 Session revision 均参与页面 owner。 */
function authContext({
  tenantId = "tenant_a",
  userId = "usr_a",
  role = "admin",
  sessionId = "session_a",
  sessionRevision = 1,
  permissionCodes = [PERMISSIONS.BILLING_ANOMALY_READ, PERMISSIONS.BILLING_ANOMALY_WRITE],
} = {}) {
  // 每个用例显式传入异常读写权限，角色字段仅用于展示和 owner 变化断言。
  return withPermissionSnapshot(
    {
      user: { tenant_id: tenantId, user_id: userId, role },
      session: { session_id: sessionId, session_revision: sessionRevision },
    },
    permissionCodes,
  );
}

/** 构造最小但完整的安全异常 DTO，金额始终使用 canonical 十进制字符串。 */
function anomalyFixture(overrides = {}) {
  return {
    cost_anomaly_id: "can_0123456789abcdef0123456789abcdef",
    environment_id: "env_prod",
    environment_name: "生产环境",
    rule_type: "virtual_key_spend_spike",
    scope_type: "virtual_key",
    scope_id: "vk_0123456789abcdef",
    scope_label: "订单应用",
    virtual_key_id: "vk_0123456789abcdef",
    virtual_key_name: "订单 Key",
    virtual_key_prefix: "mh_live_abcd",
    severity: "critical",
    status: "open",
    window_start: "2026-07-27T01:00:00Z",
    window_end: "2026-07-27T01:05:00Z",
    baseline_start: "2026-07-26T01:00:00Z",
    baseline_end: "2026-07-27T01:00:00Z",
    current_cost_micros: "12345678",
    baseline_total_micros: "288000000",
    baseline_average_micros: "1000000",
    observed_ratio_millis: "12345",
    threshold_multiplier_millis: "5000",
    minimum_cost_micros: "10000",
    source_entry_count: 3,
    baseline_bucket_count: 288,
    estimated_entry_count: 0,
    legacy_entry_count: 0,
    affected_virtual_key_count: 1,
    recommended_action: "apply_temporary_rate_limit",
    detection_version: "cost-anomaly-rules-v1",
    event_dispatch_status: "dispatched",
    detected_at: "2026-07-27T01:07:00Z",
    mitigation_eligible: true,
    mitigation_eligible_until: "2026-07-28T01:07:00Z",
    representative_request_id: "req_safe",
    representative_trace_id: "trc_safe",
    actions: [],
    temporary_rate_limit_override: null,
    ...overrides,
  };
}

test("PRD-053 list client sends only frozen filters and never sends tenant or actor", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      cost_anomalies: [],
      summary: {},
      total: 0,
      limit: 20,
      has_more: false,
      next_cursor: null,
      request_id: "req_list",
      trace_id: "trc_list",
    });
  };

  const result = await listCostAnomalies({
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    status: "open",
    ruleType: "virtual_key_spend_spike",
    severity: "critical",
    environmentId: "env_prod",
    virtualKeyId: "vk_safe",
    fromTime: "2026-07-26T00:00:00Z",
    toTime: "2026-07-27T00:00:00Z",
    limit: 20,
    cursor: "opaque.cursor",
    operationRequestId: "req_console_list",
    operationTraceId: "trc_console_list",
    tenantId: "tenant_injected",
    actorUserId: "usr_injected",
  });

  assert.equal(result.state, "ok");
  assert.equal(calls.length, 1);
  const requestUrl = new URL(calls[0].url);
  assert.equal(requestUrl.pathname, "/cost-anomalies");
  assert.equal(requestUrl.searchParams.get("rule_type"), "virtual_key_spend_spike");
  assert.equal(requestUrl.searchParams.get("cursor"), "opaque.cursor");
  assert.equal(requestUrl.searchParams.has("tenant_id"), false);
  assert.equal(requestUrl.searchParams.has("actor_user_id"), false);
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-secret");
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_console_list");
});

test("PRD-053 action client emits exact body and idempotency header", async (t) => {
  const calls = [];
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      cost_anomaly: anomalyFixture({ status: "mitigated" }),
      action: { action_type: "apply_temporary_rate_limit" },
      idempotent_replay: false,
      request_id: "req_action",
      trace_id: "trc_action",
    });
  };

  await applyCostAnomalyAction({
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    anomalyId: "can_0123456789abcdef0123456789abcdef",
    action: "apply_temporary_rate_limit",
    durationMinutes: 30,
    rpmLimit: 10,
    tpmLimit: null,
    concurrencyLimit: 2,
    idempotencyKey: "ca_action_12345678",
    operationRequestId: "req_console_action",
    operationTraceId: "trc_console_action",
    tenantId: "tenant_injected",
    expiresAt: "2099-01-01T00:00:00Z",
  });

  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://control.local/cost-anomalies/can_0123456789abcdef0123456789abcdef/action",
  );
  assert.equal(calls[0].options.method, "PATCH");
  assert.equal(calls[0].options.headers["Idempotency-Key"], "ca_action_12345678");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    action: "apply_temporary_rate_limit",
    duration_minutes: 30,
    rpm_limit: 10,
    tpm_limit: null,
    concurrency_limit: 2,
  });
  assert.equal(calls[0].options.body.includes("tenant"), false);
  assert.equal(calls[0].options.body.includes("expires"), false);
});

test("PRD-053 client rejects ambiguous actions before fetch and preserves uncertain idempotency", async (t) => {
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
    applyCostAnomalyAction({
      controlApiUrl: "http://control.local",
      token: "jwt-secret",
      anomalyId: "can_0123456789abcdef0123456789abcdef",
      action: "apply_temporary_rate_limit",
      durationMinutes: 30,
      rpmLimit: null,
      tpmLimit: null,
      concurrencyLimit: null,
      idempotencyKey: "ca_action_12345678",
    }),
    (error) => error instanceof AuthApiError && error.code === "cost_anomaly.action_invalid",
  );
  await assert.rejects(
    listCostAnomalies({
      controlApiUrl: "http://control.local",
      token: "jwt-secret",
      status: "not-a-status",
    }),
    (error) => error instanceof AuthApiError && error.code === "cost_anomaly.query_invalid",
  );
  assert.equal(fetchCount, 0);
  assert.equal(shouldRetainCostAnomalyIdempotencyKey(new TypeError("network")), true);
  assert.equal(shouldRetainCostAnomalyIdempotencyKey(new AuthApiError({ status: 503 })), true);
  assert.equal(shouldRetainCostAnomalyIdempotencyKey(new AuthApiError({ status: 409 })), false);
});

test("PRD-053 amount and ratio formatting never loses integer precision", () => {
  assert.equal(formatCostMicros("0"), "0.000000");
  assert.equal(formatCostMicros("12345678"), "12.345678");
  assert.equal(formatCostMicros("900719925474099312345678"), "900719925474099312.345678");
  assert.equal(formatCostMicros("01"), "-");
  assert.equal(formatCostMicros(12345678), "-");
  assert.equal(formatCostRatioMillis("12345"), "12.345x");
  assert.equal(formatCostRatioMillis(5000), "5.000x");
  assert.equal(formatCostRatioMillis("-1"), "-");
});

test("PRD-053 page snapshot hides cursor idempotency and operation identifiers", () => {
  const state = createCostAnomalyPageState();
  state.acceptPage({
    cost_anomalies: [anomalyFixture()],
    summary: { detector_status: "healthy" },
    total: 1,
    limit: 20,
    has_more: true,
    next_cursor: "signed.cursor.secret",
    request_id: "req_page",
    trace_id: "trc_page",
  });
  assert.equal(state.selectAnomaly("can_0123456789abcdef0123456789abcdef"), true);
  assert.equal(state.openAction({
    anomalyId: "can_0123456789abcdef0123456789abcdef",
    action: "apply_temporary_rate_limit",
    idempotencyKey: "ca_secret_12345678",
    operationRequestId: "req_secret_action",
    operationTraceId: "trc_secret_action",
  }), true);

  const snapshotText = JSON.stringify(state.snapshot());
  assert.equal(snapshotText.includes("signed.cursor.secret"), false);
  assert.equal(snapshotText.includes("ca_secret_12345678"), false);
  assert.equal(snapshotText.includes("req_secret_action"), false);
  assert.equal(snapshotText.includes("trc_secret_action"), false);

  const payload = state.beginAction({
    durationMinutes: 30,
    rpmLimit: 10,
    tpmLimit: null,
    concurrencyLimit: 2,
  });
  assert.equal(payload.idempotencyKey, "ca_secret_12345678");
  assert.equal(state.snapshot().actionDialog.submitting, true);
  state.clear();
  assert.deepEqual(state.snapshot().filters, { limit: 20 });
  assert.equal(state.snapshot().selectedAnomaly, null);
});

test("PRD-053 list and action owners reject superseded tenant navigation and generation", () => {
  const state = createCostAnomalyPageState();
  const identity = costAnomalyIdentitySnapshot(authContext());
  const listOwner = { identity, navigation: "cost-anomalies", refreshGeneration: 7 };
  const firstList = state.beginListOperation(listOwner);
  const secondList = state.beginListOperation(listOwner);
  assert.equal(firstList.controller.signal.aborted, true);
  assert.equal(state.isListOperationCurrent(firstList, listOwner), false);
  assert.equal(state.isListOperationCurrent(secondList, listOwner), true);
  assert.equal(state.isListOperationCurrent(secondList, { ...listOwner, refreshGeneration: 8 }), false);

  const actionOwner = { identity, navigation: "cost-anomalies" };
  const action = state.beginActionOperation(actionOwner);
  assert.equal(state.isActionOperationCurrent(action, actionOwner), true);
  assert.equal(state.isActionOperationCurrent(action, {
    identity: costAnomalyIdentitySnapshot(authContext({ tenantId: "tenant_b" })),
    navigation: "cost-anomalies",
  }), false);
  assert.equal(state.isActionOperationCurrent(action, { identity, navigation: "overview" }), false);
  state.clear();
  assert.equal(action.controller.signal.aborted, true);
  assert.equal(state.isActionBusy(), false);
});

test("PRD-053 identity requires anomaly permission and includes role session and revision boundaries", () => {
  const admin = costAnomalyIdentitySnapshot(authContext());
  assert.notEqual(admin, "");
  assert.equal(costAnomalyIdentitySnapshot(authContext({
    role: "developer",
    permissionCodes: [PERMISSIONS.CONSOLE_BASIC_READ],
  })), "");
  assert.equal(costAnomalyIdentitySnapshot(authContext({
    role: "admin",
    permissionCodes: [],
  })), "");
  assert.equal(costAnomalyIdentitySnapshot(authContext({ sessionId: "" })), "");
  assert.notEqual(admin, costAnomalyIdentitySnapshot(authContext({ sessionId: "session_b" })));
  assert.notEqual(admin, costAnomalyIdentitySnapshot(authContext({ sessionRevision: 2 })));
});

test("PRD-053 renderer is permission-driven and escapes all dynamic evidence", () => {
  const unsafe = anomalyFixture({
    scope_label: "<script>alert(1)</script>",
    representative_request_id: "req_<img src=x onerror=alert(1)>",
  });
  const html = renderCostAnomalies({
    state: "ok",
    cost_anomalies: [unsafe],
    selectedAnomaly: unsafe,
    summary: {
      detector_status: "healthy",
      open_count: 1,
      critical_count: 1,
      mitigated_count: 0,
      active_override_count: 0,
    },
    total: 1,
    request_id: "req_safe_page",
    filters: { limit: 20 },
  }, authContext());

  assert.match(html, /成本异常检测/);
  assert.match(html, /PAGE-077/);
  assert.match(html, /PAGE-078/);
  assert.equal(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"), true);
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.equal(renderCostAnomalies({ state: "ok" }, authContext({
    role: "developer",
    permissionCodes: [PERMISSIONS.CONSOLE_BASIC_READ],
  })), "");
  assert.equal(renderCostAnomalies({ state: "ok" }, authContext({
    role: "admin",
    permissionCodes: [],
  })), "");
});

test("PRD-053 render and app integration freeze responsive and lifecycle contracts", async () => {
  const [mainSource, renderSource, css, navigationSource] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/console/service.py", import.meta.url), "utf8"),
  ]);

  assert.equal(mainSource.includes("loadCostAnomaliesState(consoleShellState, refreshId)"), true);
  assert.equal(mainSource.includes("beginListOperation(owner)"), true);
  assert.equal(mainSource.includes("beginActionOperation(owner)"), true);
  assert.equal(
    /function clearConsoleSessionState\([\s\S]*?costAnomalyPageState\.clear\(\)/.test(mainSource),
    true,
  );
  assert.equal(mainSource.includes("bindCostAnomalyPageInteractions(consoleShellState)"), true);
  assert.equal(renderSource.includes('case "cost-anomalies"'), true);
  assert.equal(
    renderSource.includes("renderCostAnomaliesSection(options.costAnomaliesState"),
    true,
  );
  const navigationIndex = navigationSource.indexOf('"cost-anomalies"');
  assert.notEqual(navigationIndex, -1);
  assert.equal(
    navigationSource.slice(navigationIndex, navigationIndex + 500).includes('("billing.anomaly.read",)'),
    true,
  );
  assert.equal(css.includes("@media (max-width: 720px)"), true);
  assert.equal(css.includes(".cost-anomaly-workspace"), true);
  assert.equal(css.includes("@media (max-width: 480px)"), true);
  assert.equal(css.includes(".cost-anomaly-detail-list"), true);
  // 只检查动作弹窗自身规则，防止 FinOps 等其他 fixed 弹窗让视口合同误通过。
  const actionDialogRule = css.match(/\.cost-anomaly-action-dialog\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.match(actionDialogRule, /position:\s*fixed;/);
  assert.match(actionDialogRule, /inset:\s*0;/);
  assert.match(actionDialogRule, /z-index:\s*50;/);
  assert.match(actionDialogRule, /margin:\s*auto;/);
  assert.match(actionDialogRule, /max-height:\s*min\(720px, calc\(100dvh - 28px\)\);/);
  assert.match(actionDialogRule, /overflow:\s*auto;/);
  assert.match(actionDialogRule, /box-shadow:\s*0 18px 48px rgba\(24, 33, 47, 0\.28\);/);
  assert.equal((css.match(/{/g) ?? []).length, (css.match(/}/g) ?? []).length);
});

test("PRD-053 abort classification only suppresses explicit lifecycle cancellation", () => {
  assert.equal(isCostAnomalyAbortError({ name: "AbortError" }), true);
  assert.equal(isCostAnomalyAbortError(new TypeError("network failed")), false);
  assert.equal(isCostAnomalyAbortError(null), false);
});
