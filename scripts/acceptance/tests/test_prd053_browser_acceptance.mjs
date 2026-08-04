/**
 * PRD-053 Browser 验收器的纯合同测试。
 *
 * 本文件不启动服务、浏览器或 Playwright，只验证 Runtime 前置证据、tenant-qualified DTO、
 * 三角色 UI 门禁源码、统一错误投影、双视口布局、截图命名、Storage 隐私和异常 Session 清理。
 */

import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import {
  BrowserAcceptanceFailure,
  assertBrowserPrivacy,
  assertLayoutMetrics,
  assertSafeEvidence,
  assertViewportContainment,
  assertViewportIntersection,
  browserRequestAllowed,
  canonicalInteger,
  loadRuntimeEvidence,
  normalizeLoopbackOrigin,
  revokeSessionAfterFailure,
  screenshotFilename,
  validateAnomalyApiProjection,
  validateRuntimeEvidence,
  verifyLogoutEvidence,
} from "../prd053-browser-acceptance.mjs";


// 所有 fixture 都使用公开不透明 ID；不包含任何可用 Token、Key、密码、Prompt 或 Response。
const TENANT_ID = "ten_prd053_browser_unit";
const ENVIRONMENT_ID = "env_prd053_browser_unit";
const PROJECT_ID = "prj_prd053_browser_unit";
const APPLICATION_ID = "app_prd053_browser_unit";
const VIRTUAL_KEY_ID = "vk_prd053_browser_unit";
const ANOMALY_ID = "can_0123456789abcdef0123456789abcdef";
const ACTIONABLE_ID = "can_fedcba9876543210fedcba9876543210";
const OVERRIDE_ID = "cro_prd053_browser_unit";
const BROWSER_SCRIPT_URL = new URL("../prd053-browser-acceptance.mjs", import.meta.url);


/** 构造统一 HTTP 摘要，测试只修改状态或错误码，不生成认证材料。 */
function httpEvidence(status, code = undefined, suffix = "unit") {
  return {
    status,
    request_id: `req_prd053_${suffix}`,
    trace_id: `trc_prd053_${suffix}`,
    ...(code ? { code } : {}),
  };
}


/** 构造 Browser 所需的最小完整 Runtime 证据。 */
function runtimeEvidenceFixture() {
  return {
    schema_version: 1,
    prd_id: "PRD-053",
    status: "passed",
    generated_resources: {
      tenant_id: TENANT_ID,
      environment_id: ENVIRONMENT_ID,
      project_id: PROJECT_ID,
      application_id: APPLICATION_ID,
      virtual_key_id: VIRTUAL_KEY_ID,
      cost_anomaly_id: ANOMALY_ID,
      temporary_rate_limit_override_id: OVERRIDE_ID,
      provider_key_id: "pkey_prd053_browser_unit",
    },
    checks: {
      cleanup: {
        session_revoked: true,
        admin_session_revoked: true,
        developer_session_revoked: true,
        viewer_session_revoked: true,
      },
      all_sessions_revoked: true,
      detection: { detector_generated_anomaly: true },
      event: { webhook_hmac_valid: true, webhook_payload_field_count: 20 },
      permissions: {
        admin_read_allowed: true,
        anonymous_denied: true,
        developer_denied: true,
        viewer_denied: true,
      },
      apply: { temporary_limit_active: true, idempotent_replay_verified: true },
      release: { temporary_limit_released: true, mitigated_history_preserved: true },
      gateway_blocked: {
        ...httpEvidence(429, "gateway.rate_limit_exceeded", "gateway_blocked"),
        provider_attempt_count_zero: true,
      },
      gateway_restored: {
        ...httpEvidence(200, undefined, "gateway_restored"),
        release_restored_gateway: true,
        provider_attempt_recorded: true,
      },
    },
    http: {
      anomalies_admin_list: httpEvidence(200, undefined, "admin_list"),
      anomalies_developer_list: httpEvidence(403, "auth.permission_denied", "developer_list"),
      anomalies_viewer_list: httpEvidence(403, "auth.permission_denied", "viewer_list"),
      anomaly_developer_patch: httpEvidence(403, "auth.permission_denied", "developer_patch"),
      anomaly_viewer_patch: httpEvidence(403, "auth.permission_denied", "viewer_patch"),
      provider_key_create: httpEvidence(201, undefined, "provider_key_create"),
      gateway_rate_limited: {
        ...httpEvidence(429, "gateway.rate_limit_exceeded", "gateway_blocked"),
        provider_attempt_count_zero: true,
      },
      gateway_after_release: {
        ...httpEvidence(200, undefined, "gateway_restored"),
        release_restored_gateway: true,
        provider_attempt_recorded: true,
      },
    },
  };
}


/** 构造一条 canonical 成本异常 DTO；覆盖项仅用于单点失败测试。 */
function anomalyFixture(overrides = {}) {
  return {
    cost_anomaly_id: ANOMALY_ID,
    tenant_id: TENANT_ID,
    environment_id: ENVIRONMENT_ID,
    rule_type: "virtual_key_spend_spike",
    scope_type: "virtual_key",
    scope_id: VIRTUAL_KEY_ID,
    virtual_key_id: VIRTUAL_KEY_ID,
    severity: "critical",
    status: "mitigated",
    current_cost_micros: "1000000",
    baseline_total_micros: "28800000",
    baseline_average_micros: "100000",
    observed_ratio_millis: "10000",
    threshold_multiplier_millis: "5000",
    minimum_cost_micros: "10000",
    actions: [
      { action_type: "apply_temporary_rate_limit" },
      { action_type: "release_temporary_rate_limit" },
    ],
    temporary_rate_limit_override: {
      temporary_rate_limit_override_id: OVERRIDE_ID,
      effective_status: "released",
    },
    ...overrides,
  };
}


/** 构造完整 GET 响应，第二条 open 事实用于锁定 admin 动作对话框入口。 */
function anomalyDocumentFixture() {
  return {
    cost_anomalies: [
      anomalyFixture(),
      anomalyFixture({
        cost_anomaly_id: ACTIONABLE_ID,
        status: "open",
        mitigation_eligible: true,
        temporary_rate_limit_override: null,
        actions: [],
      }),
    ],
    summary: {
      detector_status: "healthy",
      open_count: 1,
      critical_count: 2,
      mitigated_count: 1,
      active_override_count: 0,
    },
    total: 2,
    limit: 20,
    has_more: false,
    next_cursor: null,
    request_id: "req_prd053_browser_list_unit",
    trace_id: "trc_prd053_browser_list_unit",
  };
}


/** 返回 Browser 隐私验证器接受的零命中指标。 */
function privacyMetricsFixture() {
  return {
    password_input_count: 0,
    full_virtual_key_pattern_count: 0,
    provider_key_pattern_count: 0,
    bearer_pattern_count: 0,
    sensitive_query_count: 0,
    forbidden_dom_attribute_count: 0,
    forbidden_storage_key_count: 0,
    forbidden_auth_context_field_count: 0,
    known_sensitive_hit_count: 0,
    local_storage_entry_count: 0,
    local_auth_slot_count: 0,
    session_auth_slot_count: 2,
    session_token_present: true,
    session_token_exposed: false,
  };
}


test("Runtime 前置证据必须同时证明 detector、Webhook、RBAC、429、release 和 Session 清理", () => {
  const projection = validateRuntimeEvidence(runtimeEvidenceFixture());
  assert.deepEqual(projection, {
    tenantId: TENANT_ID,
    environmentId: ENVIRONMENT_ID,
    projectId: PROJECT_ID,
    applicationId: APPLICATION_ID,
    virtualKeyId: VIRTUAL_KEY_ID,
    anomalyId: ANOMALY_ID,
    overrideId: OVERRIDE_ID,
  });

  const mutations = [
    (value) => { value.checks.cleanup.viewer_session_revoked = false; },
    (value) => { value.checks.permissions.developer_denied = false; },
    (value) => { value.checks.gateway_blocked.provider_attempt_count_zero = false; },
    (value) => { value.http.anomaly_viewer_patch.code = "other.code"; },
    (value) => { value.schema_version = 999; },
    (value) => { delete value.http.gateway_rate_limited; },
    (value) => { value.http.gateway_after_release.trace_id = "trc_other"; },
  ];
  for (const mutate of mutations) {
    const value = runtimeEvidenceFixture();
    mutate(value);
    assert.throws(() => validateRuntimeEvidence(value), BrowserAcceptanceFailure);
  }
});


test("Runtime 加载器只消费同批次 canonical 文件名并拒绝旧别名", async () => {
  const sandboxTmp = path.resolve(process.env.TEMP ?? process.env.TMP ?? "");
  const expectedTmp = path.resolve(".tmp");
  assert.equal(
    sandboxTmp === expectedTmp || sandboxTmp.startsWith(`${expectedTmp}${path.sep}`),
    true,
  );
  const artifactDirectory = await mkdtemp(path.join(sandboxTmp, "prd053-browser-contract-"));
  try {
    const legacyPath = path.join(artifactDirectory, "runtime-evidence.json");
    const canonicalPath = path.join(artifactDirectory, "runtime-acceptance.json");
    await writeFile(legacyPath, JSON.stringify(runtimeEvidenceFixture()), "utf8");
    await assert.rejects(loadRuntimeEvidence(artifactDirectory), BrowserAcceptanceFailure);
    await writeFile(canonicalPath, JSON.stringify(runtimeEvidenceFixture()), "utf8");
    const projection = await loadRuntimeEvidence(artifactDirectory);
    assert.deepEqual(projection, {
      tenantId: TENANT_ID,
      environmentId: ENVIRONMENT_ID,
      projectId: PROJECT_ID,
      applicationId: APPLICATION_ID,
      virtualKeyId: VIRTUAL_KEY_ID,
      anomalyId: ANOMALY_ID,
      overrideId: OVERRIDE_ID,
    });
  } finally {
    // 只删除本测试刚创建的随机目录，不能扫描或清理 .tmp 下的既有验收证据。
    await rm(artifactDirectory, { recursive: true, force: true });
  }
});


test("成本异常 API 投影保持 tenant 隔离、canonical 金额和 release 历史", () => {
  const runtime = validateRuntimeEvidence(runtimeEvidenceFixture());
  const projection = validateAnomalyApiProjection(anomalyDocumentFixture(), runtime);
  assert.equal(projection.target.cost_anomaly_id, ANOMALY_ID);
  assert.equal(projection.actionableId, ACTIONABLE_ID);
  assert.deepEqual(projection.evidence, {
    tenant_qualified_items: true,
    canonical_amounts_verified: true,
    detector_summary_verified: true,
    released_mitigation_visible: true,
    immutable_action_history_visible: true,
    visible_anomaly_count: 2,
    request_id: "req_prd053_browser_list_unit",
    trace_id: "trc_prd053_browser_list_unit",
  });
});


test("非工作时段规则允许 observed_ratio_millis 为 null", () => {
  const runtime = validateRuntimeEvidence(runtimeEvidenceFixture());
  const document = anomalyDocumentFixture();
  document.cost_anomalies[0].rule_type = "virtual_key_off_hours";
  document.cost_anomalies[0].observed_ratio_millis = null;
  assert.doesNotThrow(() => validateAnomalyApiProjection(document, runtime));
});


test("消费突增规则拒绝 observed_ratio_millis 为 null", () => {
  const runtime = validateRuntimeEvidence(runtimeEvidenceFixture());
  const document = anomalyDocumentFixture();
  document.cost_anomalies[0].observed_ratio_millis = null;
  assert.throws(() => validateAnomalyApiProjection(document, runtime), BrowserAcceptanceFailure);
});


test("跨租户、非 canonical 金额、未知枚举和缺失动作历史必须失败关闭", () => {
  const runtime = validateRuntimeEvidence(runtimeEvidenceFixture());
  const mutations = [
    (value) => { value.cost_anomalies[1].tenant_id = "ten_other"; },
    (value) => { value.cost_anomalies[0].current_cost_micros = "01"; },
    (value) => { value.cost_anomalies[0].rule_type = "machine_learning"; },
    (value) => { value.cost_anomalies[0].actions = []; },
    (value) => { value.cost_anomalies[1].mitigation_eligible = false; },
  ];
  for (const mutate of mutations) {
    const value = anomalyDocumentFixture();
    mutate(value);
    assert.throws(() => validateAnomalyApiProjection(value, runtime), BrowserAcceptanceFailure);
  }
});


test("canonical 整数字符串不经 Number 且拒绝前导零、负数、小数和指数", () => {
  assert.equal(canonicalInteger("999999999999999999999999999999"), "999999999999999999999999999999");
  for (const value of ["", "01", "-1", "1.0", "1e3", true, null]) {
    assert.throws(() => canonicalInteger(value), BrowserAcceptanceFailure);
  }
});


test("双视口布局门禁拒绝 document、容器、控件和区域任一溢出", () => {
  const valid = {
    viewport_width: 390,
    viewport_height: 844,
    document_scroll_width: 390,
    body_scroll_width: 390,
    root_left: 0,
    root_right: 390,
    root_client_width: 390,
    root_scroll_width: 390,
    clipped_control_count: 0,
    clipped_content_count: 0,
    horizontal_container_overflow_count: 0,
    control_overlap_count: 0,
    region_overlap_count: 0,
  };
  assert.deepEqual(assertLayoutMetrics(valid, { width: 390, height: 844 }), {
    horizontal_overflow: false,
    clipped_control_count: 0,
    clipped_content_count: 0,
    overlap_count: 0,
  });
  for (const field of [
    "clipped_control_count",
    "clipped_content_count",
    "horizontal_container_overflow_count",
    "control_overlap_count",
    "region_overlap_count",
  ]) assert.throws(() => assertLayoutMetrics({ ...valid, [field]: 1 }, { width: 390, height: 844 }), BrowserAcceptanceFailure);
  assert.throws(() => assertLayoutMetrics({ ...valid, document_scroll_width: 420 }, { width: 390, height: 844 }), BrowserAcceptanceFailure);
});


test("截图锚点必须相交，动作弹窗还必须完整落在双视口内", () => {
  const desktop = { width: 1280, height: 720 };
  assert.deepEqual(
    assertViewportIntersection({ x: 20, y: 700, width: 300, height: 80 }, desktop),
    { anchor_intersects_viewport: true },
  );
  assert.deepEqual(
    assertViewportContainment({ x: 14, y: 14, width: 560, height: 692 }, desktop),
    { anchor_fully_contained: true },
  );
  assert.throws(
    () => assertViewportContainment({ x: 20, y: 700, width: 300, height: 80 }, desktop),
    BrowserAcceptanceFailure,
  );
  assert.throws(
    () => assertViewportContainment({ x: -2, y: 14, width: 300, height: 80 }, desktop),
    BrowserAcceptanceFailure,
  );
  assert.equal(screenshotFilename("page077-list", "desktop-1280x720"), "screenshots/page077-list-desktop-1280x720.png");
  assert.equal(screenshotFilename("action-dialog", "mobile-390x844"), "screenshots/action-dialog-mobile-390x844.png");
  assert.equal(screenshotFilename("request-id-error", "mobile-390x844"), "screenshots/request-id-error-mobile-390x844.png");
  assert.throws(() => screenshotFilename("../escape", "desktop-1280x720"), BrowserAcceptanceFailure);
  assert.throws(() => screenshotFilename("page077-list", "tablet"), BrowserAcceptanceFailure);
  assert.throws(
    () => assertViewportIntersection({ x: 391, y: 10, width: 20, height: 20 }, { width: 390, height: 844 }),
    BrowserAcceptanceFailure,
  );
});


test("Browser 隐私门禁要求仅有两个 sessionStorage 认证槽位且无敏感字段", () => {
  assert.deepEqual(assertBrowserPrivacy(privacyMetricsFixture()), {
    sensitive_dom_hits: 0,
    sensitive_url_hits: 0,
    forbidden_storage_hits: 0,
    auth_storage_contract_valid: true,
  });
  for (const field of [
    "provider_key_pattern_count",
    "sensitive_query_count",
    "forbidden_dom_attribute_count",
    "forbidden_storage_key_count",
    "forbidden_auth_context_field_count",
    "known_sensitive_hit_count",
    "local_storage_entry_count",
  ]) assert.throws(() => assertBrowserPrivacy({ ...privacyMetricsFixture(), [field]: 1 }), BrowserAcceptanceFailure);
});


test("最终证据允许不透明 Provider Key ID，但拒绝 cursor、幂等键和敏感正文槽位", () => {
  assert.doesNotThrow(() => assertSafeEvidence({
    provider_key_id: "pkey_safe_identifier",
    response_count: 2,
    request_id: "req_safe",
  }));
  for (const key of [
    "provider_key",
    "next_cursor",
    "idempotency_key",
    "dismiss_reason",
    "prompt",
    "response",
    "response_body",
  ]) assert.throws(() => assertSafeEvidence({ [key]: "must-not-pass" }), BrowserAcceptanceFailure);
});


test("浏览器网络只允许显式 HTTP 回环 origin 且拒绝敏感查询键", () => {
  const allowed = new Set(["http://localhost:5173", "http://127.0.0.1:8001"]);
  assert.equal(browserRequestAllowed("http://localhost:5173/#cost-anomalies", allowed), true);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/cost-anomalies?limit=20", allowed), true);
  assert.equal(browserRequestAllowed("http://127.0.0.1:8001/cost-anomalies?cursor=opaque", allowed), false);
  assert.equal(browserRequestAllowed("https://example.com/cost-anomalies", allowed), false);
  assert.equal(normalizeLoopbackOrigin("http://localhost:5173", "console"), "http://localhost:5173");
  assert.throws(() => normalizeLoopbackOrigin("http://example.com:5173", "console"), BrowserAcceptanceFailure);
});


test("登出证据同时验证服务端撤销、request_id/trace_id 和双 Storage 清理", () => {
  const evidence = verifyLogoutEvidence({
    status: 200,
    headers: { "x-request-id": "req_logout_unit", "x-trace-id": "trc_logout_unit" },
    payload: { logged_out: true, request_id: "req_logout_unit", trace_id: "trc_logout_unit" },
    storageState: { localStorage: [true, true], sessionStorage: [true, true] },
  });
  assert.equal(evidence.logged_out, true);
  assert.equal(evidence.browser_auth_cleared, true);
  assert.throws(() => verifyLogoutEvidence({
    status: 200,
    headers: { "x-request-id": "req_logout_unit", "x-trace-id": "trc_logout_unit" },
    payload: { logged_out: true, request_id: "req_logout_unit", trace_id: "trc_other" },
    storageState: { localStorage: [true, true], sessionStorage: [true, true] },
  }), BrowserAcceptanceFailure);
});


test("异常清理只在页面上下文使用 Token，并仅返回服务端撤销布尔结论", async () => {
  const sessionValues = new Map([
    ["metahub.auth_token", "unit-browser-session"],
    ["metahub.auth_context", "{}"],
  ]);
  const localValues = new Map();
  // Storage 替身只实现验收器实际调用的读删合同，避免测试保存或输出伪 Token。
  const storage = (values) => ({
    getItem(key) { return values.get(key) ?? null; },
    removeItem(key) { values.delete(key); },
  });
  let authorization = null;
  const page = {
    async evaluate(callback, argument) {
      const replacements = new Map([
        ["sessionStorage", storage(sessionValues)],
        ["localStorage", storage(localValues)],
        ["fetch", async (_url, options) => {
          authorization = options.headers.Authorization;
          return { status: 200, async json() { return { logged_out: true }; } };
        }],
      ]);
      const previous = new Map();
      for (const [name, value] of replacements) {
        previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
        Object.defineProperty(globalThis, name, { configurable: true, value });
      }
      try {
        return await callback(argument);
      } finally {
        for (const [name, descriptor] of previous) {
          if (descriptor) Object.defineProperty(globalThis, name, descriptor);
          else delete globalThis[name];
        }
      }
    },
  };
  assert.equal(await revokeSessionAfterFailure(page, { controlOrigin: "http://127.0.0.1:8001" }), true);
  assert.equal(authorization, "Bearer unit-browser-session");
  assert.equal(sessionValues.size, 0);
});


test("真实验收源码冻结三角色直达、request_id 错误、双视口和失败清理路径", async () => {
  const source = await readFile(BROWSER_SCRIPT_URL, "utf8");
  assert.match(source, /for \(const identity of restrictedCredentials\)/);
  assert.match(source, /page\.goto\(`\$\{config\.consoleOrigin\}\/\#cost-anomalies`/);
  assert.match(source, /tracker\.get_count === before/);
  assert.match(source, /\.cost-anomaly-error/);
  assert.match(source, /text\.includes\(ERROR_REQUEST_ID\)/);
  assert.match(source, /expected_http_console_error_count/);
  assert.match(source, /network\.expectHttpConsoleError\(503\)/);
  assert.match(source, /"page077-list"/);
  assert.match(source, /"page077-detail"/);
  assert.match(source, /"page078-alerts"/);
  assert.match(source, /"action-dialog"/);
  assert.match(source, /"request-id-error"/);
  assert.match(source, /data-cost-anomaly-action="apply_temporary_rate_limit"/);
  assert.match(source, /getAttribute\("aria-pressed"\) === "true"[\s\S]*data-cost-anomaly-id/);
  assert.match(source, /\.cost-anomaly-list \[data-cost-anomaly-select=/);
  assert.match(source, /selection_click_failed[\s\S]*selection_refresh_timeout[\s\S]*dialog_visibility_timeout[\s\S]*capture_failed/);
  assert.match(source, /requireFullContainment \? assertViewportContainment/);
  assert.match(source, /readLayoutMetrics\(page, requireFullContainment \? anchorSelector : "body"\)/);
  assert.match(source, /waitForFunction\([\s\S]*window\.location\.hash !== "#cost-anomalies"/);
  assert.match(source, /readPrivacyMetrics\(page, sensitiveValues\)/);
  assert.match(source, /entries = await readdir\(screenshotsDirectory\)/);
  assert.match(source, /if \(sessionNeedsRevocation && page\)[\s\S]*revokeSessionAfterFailure\(page, config\)/);
  assert.doesNotMatch(source, /return \{[^}]*token/s);
});