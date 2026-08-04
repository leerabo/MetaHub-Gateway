// PRD-064 PAGE-091 专项：客户端合同、租户隔离、权限、竞态、隐私、XSS 与响应式布局。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  renderAdapterContractRunsLoading,
  renderDeveloperStart,
} from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  adapterContractRunFilterKey,
  adapterContractRunIdentitySnapshot,
  canonicalAdapterContractRunFilters,
  createAdapterContractRunPageState,
} from "../../src/features/adapter-contract-runs/adapterContractRunPageState.js";
import { renderAdapterContractRuns } from "../../src/features/adapter-contract-runs/renderAdapterContractRuns.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  getAdapterContractRun,
  listAdapterContractRuns,
  sanitizeAdapterContractRunDetail,
  sanitizeAdapterContractRunList,
} from "../../src/lib/adapterContractRuns.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 测试固定身份与 hash 均为无敏感占位，用于构造严格成功合同。 */
const TENANT_ID = "tenant_contract_a";
const CATALOG_HASH = "a".repeat(64);
const SOURCE_HASH = "b".repeat(64);
const REPORT_HASH = "c".repeat(64);
const EVIDENCE_HASH = "d".repeat(64);

/** 构造包含 tenant、Session、RBAC revision 和授权指纹的后端身份快照。 */
function contractAuth(permissionCodes = [PERMISSIONS.PROVIDER_HEALTH_READ], overrides = {}) {
  return withPermissionSnapshot({
    user: {
      tenant_id: overrides.tenantId ?? TENANT_ID,
      user_id: overrides.userId ?? "usr_contract_a",
      email: "developer@example.com",
      role: overrides.role ?? "developer",
    },
    session: {
      session_id: overrides.sessionId ?? "ses_contract_a",
      session_revision: overrides.sessionRevision ?? 3,
    },
  }, permissionCodes, {
    permissionRevision: overrides.permissionRevision ?? 11,
    authorizationFingerprint: overrides.authorizationFingerprint ?? "contract_fingerprint_11",
  });
}

/** 构造满足不可变计数与 suite/mode 约束的运行事实。 */
function makeRun(overrides = {}) {
  return {
    adapter_contract_run_id: `acr_${"1".repeat(32)}`,
    tenant_id: TENANT_ID,
    source_request_id: "req_contract_source_1",
    source_trace_id: "trc_contract_source_1",
    schema_version: "adapter-contract-report-v1",
    catalog_version: "adapter-spec-v1",
    catalog_hash: CATALOG_HASH,
    runner_version: "1.0.0",
    suite_kind: "merge_gate",
    mode: "mock",
    source_revision_hash: SOURCE_HASH,
    status: "passed",
    coverage_complete: true,
    total_cases: 2,
    passed_cases: 2,
    failed_cases: 0,
    failure_codes: [],
    report_hash: REPORT_HASH,
    started_at: "2026-08-01T00:00:00Z",
    completed_at: "2026-08-01T00:00:01Z",
    created_at: "2026-08-01T00:00:02Z",
    ...overrides,
  };
}

/** 构造一条安全 case；父 run 与 tenant owner 默认和 makeRun 一致。 */
function makeCase(index, overrides = {}) {
  const hex = index.toString(16).padStart(32, "0");
  return {
    adapter_contract_case_id: `acc_${hex}`,
    tenant_id: TENANT_ID,
    adapter_contract_run_id: `acr_${"1".repeat(32)}`,
    contract_case_key: `prd063_deepseek_minimum_contract:non_stream:success:${index}`,
    spec_id: "adapter-spec-deepseek-v1",
    provider_type: "deepseek",
    adapter_name: "openai_compatible",
    adapter_version: "1.0.0",
    case_kind: "non_stream",
    variant: "success",
    auth_type: null,
    status: "passed",
    failure_code: null,
    duration_ms: 12 + index,
    evidence_hash: EVIDENCE_HASH,
    created_at: "2026-08-01T00:00:02Z",
    ...overrides,
  };
}

/** 构造列表响应，并允许测试覆盖分页、错误和额外敏感字段。 */
function listPayload(overrides = {}) {
  return {
    adapter_contract_runs: [makeRun()],
    limit: 50,
    next_cursor: null,
    request_id: "req_contract_list_1",
    trace_id: "trc_contract_list_1",
    ...overrides,
  };
}

/** 构造父运行、两条安全 case 和统一链路标识组成的完整详情成功响应。 */
function detailPayload(overrides = {}) {
  // 两条 case 与父 run 的 total/passed 计数精确一致，便于单独篡改任一维度验证失败关闭。
  return {
    adapter_contract_run: makeRun(),
    cases: [makeCase(1), makeCase(2, { case_kind: "usage", variant: "standard" })],
    request_id: "req_contract_detail_1",
    trace_id: "trc_contract_detail_1",
    ...overrides,
  };
}

test("contract run list client sends only fixed filters and strips tenant/secrets", async () => {
  let captured;
  globalThis.fetch = async (url, options = {}) => {
    captured = { url: String(url), options };
    const payload = listPayload();
    payload.secret = "response-secret-canary";
    payload.adapter_contract_runs[0].endpoint = "https://private.example";
    payload.adapter_contract_runs[0].prompt = "private-prompt-canary";
    return { ok: true, headers: new Map(), async json() { return payload; } };
  };

  const result = await listAdapterContractRuns({
    controlApiUrl: "http://control.local",
    token: "session-contract-token",
    expectedTenantId: TENANT_ID,
    filters: {
      suite_kind: "merge_gate",
      status: "passed",
      mode: "mock",
      limit: 50,
      tenant_id: "tenant_other",
      endpoint: "https://secret.example",
      api_key: "query-secret-canary",
    },
  });
  const url = new URL(captured.url);
  assert.equal(url.pathname, "/adapter-contract-runs");
  assert.equal([...url.searchParams.keys()].sort().join(","), "limit,mode,status,suite_kind");
  assert.equal(captured.options.headers.Authorization, "Bearer session-contract-token");
  assert.doesNotMatch(captured.url, /tenant_other|secret\.example|query-secret-canary/);
  assert.doesNotMatch(
    JSON.stringify(result),
    /tenant_contract|tenant_other|source_trace|private\.example|private-prompt|response-secret|report_hash|source_revision/,
  );
});

test("contract detail validates path/owner and never retains evidence or response body", async () => {
  let captured;
  globalThis.fetch = async (url, options = {}) => {
    captured = { url: String(url), options };
    const payload = detailPayload();
    payload.cases[0].response = "private-response-canary";
    payload.cases[0].authorization = "Bearer private-token";
    return { ok: true, headers: new Map(), async json() { return payload; } };
  };
  const result = await getAdapterContractRun({
    controlApiUrl: "http://control.local",
    token: "session-contract-token",
    expectedTenantId: TENANT_ID,
    adapterContractRunId: `acr_${"1".repeat(32)}`,
  });
  assert.equal(new URL(captured.url).pathname, `/adapter-contract-runs/acr_${"1".repeat(32)}`);
  assert.equal(captured.options.method, undefined);
  assert.equal(result.cases.length, 2);
  assert.doesNotMatch(
    JSON.stringify(result),
    /tenant_contract|evidence_hash|private-response|private-token|source_trace|report_hash/,
  );
});

test("client rejects tampered filters and run IDs before network without echo", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    // 命中该分支即表示非法筛选或 path 已越过联网前门禁，测试必须立即失败。
    throw new Error("must-not-fetch-private-canary");
  };
  await assert.rejects(
    () => listAdapterContractRuns({
      controlApiUrl: "http://control.local",
      token: "token",
      expectedTenantId: TENANT_ID,
      filters: { status: "passed\nprivate-canary" },
    }),
    (error) => error instanceof AuthApiError
      && error.code === "adapter.contract_filter_invalid"
      && !error.message.includes("private-canary"),
  );
  await assert.rejects(
    () => getAdapterContractRun({
      controlApiUrl: "http://control.local",
      token: "token",
      expectedTenantId: TENANT_ID,
      adapterContractRunId: "../../private-run",
    }),
    (error) => error instanceof AuthApiError && error.code === "adapter.contract_run_id_invalid",
  );
  assert.equal(calls, 0);
});

test("client maps non-JSON errors and fails closed on cross-tenant or damaged success", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 503,
    headers: new Map([["X-Request-ID", "req_contract_proxy"]]),
    async json() { throw new Error("proxy-private-body"); },
  });
  await assert.rejects(
    () => listAdapterContractRuns({
      controlApiUrl: "http://control.local",
      token: "token",
      expectedTenantId: TENANT_ID,
    }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.status, 503);
      assert.equal(error.requestId, "req_contract_proxy");
      assert.doesNotMatch(error.message, /proxy-private-body/);
      return true;
    },
  );

  const crossTenant = listPayload({
    adapter_contract_runs: [makeRun({ tenant_id: "tenant_other" })],
  });
  assert.throws(
    () => sanitizeAdapterContractRunList(crossTenant, new Map(), TENANT_ID),
    (error) => error.code === "adapter.contract_response_invalid",
  );
  const countDrift = listPayload({
    adapter_contract_runs: [makeRun({ total_cases: 3 })],
  });
  assert.throws(
    () => sanitizeAdapterContractRunList(countDrift, new Map(), TENANT_ID),
    (error) => error.status === 502,
  );
  const badDetail = detailPayload({ cases: [makeCase(1)] });
  assert.throws(
    () => sanitizeAdapterContractRunDetail(
      badDetail,
      new Map(),
      TENANT_ID,
      `acr_${"1".repeat(32)}`,
    ),
    (error) => error.code === "adapter.contract_response_invalid",
  );
  const failedWithoutCode = listPayload({
    adapter_contract_runs: [makeRun({
      status: "failed",
      passed_cases: 1,
      failed_cases: 1,
      failure_codes: [],
    })],
  });
  assert.throws(
    () => sanitizeAdapterContractRunList(failedWithoutCode, new Map(), TENANT_ID),
    (error) => error.status === 502,
  );
});

test("client accepts the frozen unexpected-fixture run failure code", () => {
  // Gateway 的 exact-set 门禁会用该码表示多出的幽灵夹具；Control 与 0056 已接受它，
  // PAGE-091 也必须按同一冻结目录展示失败运行，不能把合法报告误判为损坏响应。
  const payload = listPayload({
    adapter_contract_runs: [makeRun({
      status: "failed",
      passed_cases: 1,
      failed_cases: 1,
      failure_codes: ["adapter.contract_fixture_unexpected"],
    })],
  });
  const result = sanitizeAdapterContractRunList(payload, new Map(), TENANT_ID);
  assert.deepEqual(
    result.adapter_contract_runs[0].failure_codes,
    ["adapter.contract_fixture_unexpected"],
  );
});

test("PAGE-091 renders summary, segments, list and provider-grouped detail without execute control", () => {
  const list = sanitizeAdapterContractRunList(listPayload(), new Map(), TENANT_ID);
  const detail = sanitizeAdapterContractRunDetail(
    detailPayload(),
    new Map(),
    TENANT_ID,
    `acr_${"1".repeat(32)}`,
  );
  const html = renderAdapterContractRuns({
    ...list,
    filters: { suite_kind: "merge_gate", status: "", mode: "", limit: 50 },
    latest_merge_gate: list.adapter_contract_runs[0],
    selected_run_id: `acr_${"1".repeat(32)}`,
    detail,
    detail_error: null,
    detail_loading: false,
    can_previous: false,
    can_next: false,
    refreshing: false,
  }, contractAuth());
  assert.match(html, /Adapter 契约测试报告/);
  assert.match(html, /最新合并门禁摘要/);
  assert.match(html, /data-adapter-contract-suite="merge_gate"/);
  assert.match(html, /data-adapter-contract-suite="probe"/);
  assert.match(html, /合并门禁/);
  assert.match(html, /真实探测/);
  assert.match(html, /DeepSeek/);
  assert.match(html, /错误归一|非流式/);
  assert.match(html, /req_contract_source_1/);
  assert.doesNotMatch(html, /运行真实探测|data-adapter-contract-run-probe|method="post"|tenant_contract_a/iu);
});

test("PAGE-091 escapes every dynamic field and keeps sensitive extras out of DOM", () => {
  const canary = `"><img src=x onerror=alert(1)>`;
  const html = renderAdapterContractRuns({
    state: "ok",
    filters: { suite_kind: "merge_gate" },
    latest_merge_gate: null,
    adapter_contract_runs: [{
      ...makeRun(),
      source_request_id: canary,
      catalog_version: canary,
      catalog_hash: CATALOG_HASH,
    }],
    request_id: canary,
    selected_run_id: null,
    can_previous: false,
    can_next: false,
  }, contractAuth());
  assert.match(html, /&quot;&gt;&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img|<script|private-response-canary|Bearer private-token/);
});

test("viewer has zero PAGE-091 DOM and loading projection requires exact backend navigation", () => {
  assert.equal(renderAdapterContractRuns({ state: "ok" }, contractAuth([])), "");
  const shellState = {
    state: "ok",
    activeNavigationItemId: "adapter-contract-runs",
    navigation: {
      active_navigation_item_id: "adapter-contract-runs",
      navigation_items: [{
        id: "adapter-contract-runs",
        label: "CI 测试报告",
        group: "Provider 治理",
        target_section: "adapter-contract-runs",
        required_permissions: [PERMISSIONS.PROVIDER_HEALTH_READ],
        visible: true,
        disabled: false,
      }],
    },
  };
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: contractAuth(),
    consoleShellState: shellState,
    adapterContractRunsState: null,
  });
  assert.match(container.innerHTML, /data-active-navigation="adapter-contract-runs"/);
  assert.match(container.innerHTML, /data-adapter-contract-root/);

  const denied = { innerHTML: "preserved" };
  assert.equal(renderAdapterContractRunsLoading(denied, {
    authContext: contractAuth([]),
    consoleShellState: shellState,
  }), null);
  assert.equal(denied.innerHTML, "preserved");
  const wrongNavigation = { innerHTML: "preserved" };
  assert.equal(renderAdapterContractRunsLoading(wrongNavigation, {
    authContext: contractAuth(),
    consoleShellState: {
      ...shellState,
      navigation: { ...shellState.navigation, navigation_items: [] },
    },
  }), null);
  assert.equal(wrongNavigation.innerHTML, "preserved");
});

test("page state keeps cursor private and supports filter/pagination lifecycle", () => {
  const state = createAdapterContractRunPageState();
  const auth = contractAuth();
  const identity = adapterContractRunIdentitySnapshot(auth);
  const owner = {
    identity,
    navigation: "adapter-contract-runs",
    filterKey: adapterContractRunFilterKey(state.snapshot().filters),
    token: "token-a",
    refreshGeneration: 7,
  };
  const operation = state.beginListOperation(owner);
  assert.ok(operation);
  const payload = sanitizeAdapterContractRunList(
    listPayload({ next_cursor: "opaque_cursor_1" }),
    new Map(),
    TENANT_ID,
  );
  state.acceptPage(payload);
  state.finishListOperation(operation);
  const first = state.snapshot();
  assert.equal(first.can_next, true);
  assert.doesNotMatch(JSON.stringify(first), /opaque_cursor_1|token-a|tenant_contract|contract_fingerprint/);
  assert.equal(state.nextPage(), true);
  assert.equal(state.query().cursor, "opaque_cursor_1");
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /opaque_cursor_1/);
  assert.equal(state.previousPage(), true);
  assert.equal(state.query().cursor, null);

  state.setFilters({ suite_kind: "probe", status: "failed", mode: "gateway_probe", tenant_id: "other" });
  assert.deepEqual(state.snapshot().filters, {
    suite_kind: "probe",
    status: "failed",
    mode: "gateway_probe",
    limit: 50,
  });
  assert.deepEqual(canonicalAdapterContractRunFilters({ suite_kind: "tampered", status: "all" }), {
    suite_kind: "merge_gate",
    status: "",
    mode: "",
    limit: 50,
  });
});

test("page state clears authorized facts and opaque pagination after backend 403", () => {
  const state = createAdapterContractRunPageState();
  const owner = {
    identity: adapterContractRunIdentitySnapshot(contractAuth()),
    navigation: "adapter-contract-runs",
    filterKey: adapterContractRunFilterKey(state.snapshot().filters),
    token: "token-permission-canary",
    refreshGeneration: 17,
  };
  state.acceptPage(sanitizeAdapterContractRunList(
    listPayload({ next_cursor: "opaque_permission_cursor_1" }),
    new Map(),
    TENANT_ID,
  ));
  assert.equal(state.nextPage(), true);
  state.acceptPage(sanitizeAdapterContractRunList(
    listPayload({ next_cursor: "opaque_permission_cursor_2" }),
    new Map(),
    TENANT_ID,
  ));
  const detailOperation = state.beginDetailOperation(owner, `acr_${"1".repeat(32)}`);
  state.acceptDetail(sanitizeAdapterContractRunDetail(
    detailPayload(),
    new Map(),
    TENANT_ID,
    `acr_${"1".repeat(32)}`,
  ));
  state.finishDetailOperation(detailOperation);
  const refreshOperation = state.beginListOperation(owner);
  assert.ok(refreshOperation);

  const snapshot = state.rejectList(new AuthApiError({
    status: 403,
    code: "auth.permission_denied",
    message: "当前用户无权查看契约测试报告",
    requestId: "req_contract_permission_403",
    diagnosis: "response-body-permission-canary",
  }));

  assert.equal(refreshOperation.controller.signal.aborted, true);
  assert.equal(snapshot.state, "error");
  assert.deepEqual(snapshot.adapter_contract_runs, []);
  assert.equal(snapshot.latest_merge_gate, null);
  assert.equal(snapshot.selected_run_id, null);
  assert.equal(snapshot.detail, null);
  assert.equal(snapshot.detail_error, null);
  assert.equal(snapshot.can_previous, false);
  assert.equal(snapshot.can_next, false);
  assert.equal(snapshot.refreshing, false);
  assert.equal(state.query().cursor, null);
  assert.deepEqual(snapshot.error, {
    code: "auth.permission_denied",
    message: "当前用户无权查看契约测试报告",
    requestId: "req_contract_permission_403",
  });
  const serialized = JSON.stringify(snapshot);
  assert.doesNotMatch(
    serialized,
    /token-permission|tenant_contract|opaque_permission|response-body-permission|req_contract_detail_1/,
  );
  const html = renderAdapterContractRuns(snapshot, contractAuth());
  assert.doesNotMatch(html, /adapter-contract-run-table|data-adapter-contract-refresh|data-adapter-contract-detail/);
});

test("detail 403 revokes the whole previously authorized report surface", () => {
  const state = createAdapterContractRunPageState();
  const owner = {
    identity: adapterContractRunIdentitySnapshot(contractAuth()),
    navigation: "adapter-contract-runs",
    filterKey: adapterContractRunFilterKey(state.snapshot().filters),
    token: "token-detail-permission-canary",
    refreshGeneration: 18,
  };
  state.acceptPage(sanitizeAdapterContractRunList(
    listPayload({ next_cursor: "opaque_detail_permission_cursor" }),
    new Map(),
    TENANT_ID,
  ));
  const detailOperation = state.beginDetailOperation(owner, `acr_${"1".repeat(32)}`);
  assert.ok(detailOperation);

  const snapshot = state.rejectDetail(new AuthApiError({
    status: 403,
    code: "auth.permission_denied",
    message: "当前用户无权查看契约测试报告",
    requestId: "req_contract_detail_permission_403",
    diagnosis: "response-body-detail-permission-canary",
  }));

  assert.equal(detailOperation.controller.signal.aborted, true);
  assert.equal(snapshot.state, "error");
  assert.deepEqual(snapshot.adapter_contract_runs, []);
  assert.equal(snapshot.latest_merge_gate, null);
  assert.equal(snapshot.selected_run_id, null);
  assert.equal(snapshot.detail, null);
  assert.equal(snapshot.detail_error, null);
  assert.equal(snapshot.can_previous, false);
  assert.equal(snapshot.can_next, false);
  assert.equal(state.query().cursor, null);
  assert.equal(snapshot.error?.requestId, "req_contract_detail_permission_403");
  assert.doesNotMatch(
    JSON.stringify(snapshot),
    /token-detail-permission|tenant_contract|opaque_detail_permission|response-body-detail-permission/,
  );
});

test("page state preserves only sanitized history as partial after backend 503", () => {
  const state = createAdapterContractRunPageState();
  const owner = {
    identity: adapterContractRunIdentitySnapshot(contractAuth()),
    navigation: "adapter-contract-runs",
    filterKey: adapterContractRunFilterKey(state.snapshot().filters),
    token: "token-storage-canary",
    refreshGeneration: 19,
  };
  state.acceptPage(sanitizeAdapterContractRunList(
    listPayload({ next_cursor: "opaque_storage_cursor_1" }),
    new Map(),
    TENANT_ID,
  ));
  assert.equal(state.nextPage(), true);
  state.acceptPage(sanitizeAdapterContractRunList(
    listPayload({ next_cursor: "opaque_storage_cursor_2" }),
    new Map(),
    TENANT_ID,
  ));
  const detailOperation = state.beginDetailOperation(owner, `acr_${"1".repeat(32)}`);
  state.acceptDetail(sanitizeAdapterContractRunDetail(
    detailPayload(),
    new Map(),
    TENANT_ID,
    `acr_${"1".repeat(32)}`,
  ));
  state.finishDetailOperation(detailOperation);
  const refreshOperation = state.beginListOperation(owner);
  assert.ok(refreshOperation);
  state.rejectList(new AuthApiError({
    status: 503,
    code: "adapter.contract_storage_unavailable",
    message: "契约测试运行暂不可用",
    requestId: "req_contract_storage_503",
    diagnosis: "response-body-storage-canary",
  }));
  state.finishListOperation(refreshOperation);
  const snapshot = state.snapshot();

  assert.equal(refreshOperation.controller.signal.aborted, false);
  assert.equal(snapshot.state, "partial");
  assert.equal(snapshot.adapter_contract_runs.length, 1);
  assert.ok(snapshot.latest_merge_gate);
  assert.equal(snapshot.selected_run_id, `acr_${"1".repeat(32)}`);
  assert.equal(snapshot.detail?.cases.length, 2);
  assert.equal(snapshot.can_previous, true);
  assert.equal(snapshot.can_next, true);
  assert.equal(state.query().cursor, "opaque_storage_cursor_1");
  assert.deepEqual(snapshot.error, {
    code: "adapter.contract_storage_unavailable",
    message: "契约测试运行暂不可用",
    requestId: "req_contract_storage_503",
  });
  const serialized = JSON.stringify(snapshot);
  assert.doesNotMatch(
    serialized,
    /token-storage|tenant_contract|opaque_storage|response-body-storage/,
  );
  const html = renderAdapterContractRuns(snapshot, contractAuth());
  assert.match(html, /role="alert"[\s\S]*req_contract_storage_503/);
  assert.match(html, /adapter-contract-run-table/);
  assert.match(html, /data-adapter-contract-refresh/);
  assert.match(html, /data-adapter-contract-detail/);
});

test("latest merge gate summary is not polluted by filtered or later pages", () => {
  const state = createAdapterContractRunPageState();
  const newest = makeRun({
    adapter_contract_run_id: `acr_${"2".repeat(32)}`,
    completed_at: "2026-08-01T00:02:00Z",
  });
  const older = makeRun({
    adapter_contract_run_id: `acr_${"3".repeat(32)}`,
    completed_at: "2026-08-01T00:01:00Z",
  });

  // 只有无筛选的 merge_gate 第一页能建立租户级最新摘要，响应顺序漂移也按完成时间收敛。
  state.acceptPage({ adapter_contract_runs: [older, newest], next_cursor: "next-page" });
  assert.equal(state.snapshot().latest_merge_gate.adapter_contract_run_id, newest.adapter_contract_run_id);

  state.setFilters({ suite_kind: "merge_gate", status: "failed", mode: "mock" });
  state.acceptPage({
    adapter_contract_runs: [makeRun({
      adapter_contract_run_id: `acr_${"4".repeat(32)}`,
      status: "failed",
      passed_cases: 1,
      failed_cases: 1,
      failure_codes: ["adapter.contract_probe_failed"],
      completed_at: "2026-08-01T00:03:00Z",
    })],
  });
  assert.equal(state.snapshot().latest_merge_gate.adapter_contract_run_id, newest.adapter_contract_run_id);

  state.setFilters({ suite_kind: "merge_gate" });
  state.acceptPage({ adapter_contract_runs: [newest], next_cursor: "next-page" });
  assert.equal(state.nextPage(), true);
  state.acceptPage({ adapter_contract_runs: [older], next_cursor: null });
  assert.equal(state.snapshot().latest_merge_gate.adapter_contract_run_id, newest.adapter_contract_run_id);
});

test("list and detail tickets reject identity navigation filter token and generation drift", () => {
  const state = createAdapterContractRunPageState();
  const identity = adapterContractRunIdentitySnapshot(contractAuth());
  const filters = state.snapshot().filters;
  const owner = {
    identity,
    navigation: "adapter-contract-runs",
    filterKey: adapterContractRunFilterKey(filters),
    token: "token-a",
    refreshGeneration: 9,
  };
  const listOperation = state.beginListOperation(owner);
  assert.equal(state.isListOperationCurrent(listOperation, owner), true);
  assert.equal(state.isListOperationCurrent(listOperation, { ...owner, token: "token-b" }), false);
  assert.equal(state.isListOperationCurrent(listOperation, { ...owner, refreshGeneration: 10 }), false);
  state.acceptPage(sanitizeAdapterContractRunList(listPayload(), new Map(), TENANT_ID));
  state.finishListOperation(listOperation);

  const detailOperation = state.beginDetailOperation(owner, `acr_${"1".repeat(32)}`);
  assert.ok(detailOperation);
  assert.equal(state.isDetailOperationCurrent(detailOperation, owner), true);
  assert.equal(state.isDetailOperationCurrent(detailOperation, { ...owner, identity: `${identity}-changed` }), false);
  assert.equal(state.isDetailOperationCurrent(detailOperation, { ...owner, navigation: "adapter-specs" }), false);
  state.setFilters({ suite_kind: "probe" });
  assert.equal(detailOperation.controller.signal.aborted, true);
  assert.equal(state.isDetailOperationCurrent(detailOperation, owner), false);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /token-a|tenant_contract|contract_fingerprint/);
});

test("PAGE-091 source contains only GET clients and responsive CSS converts both tables on mobile", async () => {
  const [clientSource, mainSource, css] = await Promise.all([
    readFile(new URL("../../src/lib/adapterContractRuns.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(clientSource, /method:\s*["']POST["']/);
  assert.match(mainSource, /currentAdapterContractRunOwner/);
  assert.match(mainSource, /adapterContractRunNavigationOwned/);
  assert.match(mainSource, /listAdapterContractRuns/);
  assert.match(css, /\.adapter-contract-run-row\s*\{[^}]*grid-template-columns:/s);
  assert.match(css, /\.adapter-contract-case-row\s*\{[^}]*grid-template-columns:/s);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.adapter-contract-run-row:not\(\.adapter-contract-run-head\)[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.adapter-contract-case-row:not\(\.adapter-contract-case-head\)[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width: 420px\)[\s\S]*?\.adapter-contract-summary,[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/);
});
