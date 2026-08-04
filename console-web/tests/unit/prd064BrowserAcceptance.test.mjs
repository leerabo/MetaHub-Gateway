/**
 * PRD-064 PAGE-091 浏览器验收器纯合同测试。
 *
 * 本文件不启动服务、浏览器或文件写入，只验证路径、loopback 网络、报告 DTO、权限导航、
 * 403/503、布局、证据隐私与验收器源码门禁。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BrowserAcceptanceFailure,
  VIEWPORTS,
  adapterContractRequestDescriptor,
  assertLayoutMetrics,
  assertSafeEvidence,
  browserConsolePrivacyViolation,
  browserRequestAllowed,
  buildBrowserEnvironment,
  buildPaginationBackendUrl,
  classifyBrowserConsoleMessage,
  mapControlBackendUrl,
  normalizeLoopbackOrigin,
  pinBrowserWritableRoots,
  resolveArtifactDirectory,
  validateAdapterContractDetailPayload,
  validateAdapterContractListPayload,
  validateFaultDomProjection,
  validateNavigationOwner,
  validateUnifiedErrorPayload,
} from "../../../scripts/acceptance/prd064-browser-acceptance.mjs";

/** 从测试文件位置计算仓库根，避免依赖执行命令当前目录。 */
const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
/** 固定 owner 和事实 ID 只用于纯函数合同，不会联网或落盘。 */
const OWNER_ID = "tenant_contract";
const RUN_ID = "acr_" + "1".repeat(32);
const SECOND_RUN_ID = "acr_" + "2".repeat(32);


/** 构造最小 Playwright Request 替身，Header 值只参与布尔合同判断。 */
function requestStub(url, overrides = {}) {
  return {
    url: () => url,
    method: () => overrides.method ?? "GET",
    headers: () => ({
      authorization: overrides.authorization ?? "Bearer session-contract-value",
      "x-request-id": overrides.requestId
        ?? "console_adapter_contract_list_1785500000000_abc123def456",
    }),
  };
}


/** 构造一条符合冻结 schema 的运行事实。 */
function makeRun(overrides = {}) {
  const failed = overrides.status === "failed";
  const probe = overrides.suite_kind === "probe";
  return {
    adapter_contract_run_id: overrides.adapter_contract_run_id ?? RUN_ID,
    tenant_id: overrides.tenant_id ?? OWNER_ID,
    source_request_id: "req_prd064_contract_source",
    source_trace_id: "trc_prd064_contract_source",
    schema_version: "adapter-contract-report-v1",
    catalog_version: "adapter-spec-v1",
    catalog_hash: "a".repeat(64),
    runner_version: "1.0.0",
    source_revision_hash: "b".repeat(64),
    report_hash: (overrides.adapter_contract_run_id === SECOND_RUN_ID ? "d" : "c").repeat(64),
    suite_kind: probe ? "probe" : "merge_gate",
    mode: probe ? "gateway_probe" : "mock",
    status: failed ? "failed" : "passed",
    coverage_complete: !probe,
    total_cases: 2,
    passed_cases: failed ? 1 : 2,
    failed_cases: failed ? 1 : 0,
    failure_codes: failed ? ["adapter.contract_usage_failed"] : [],
    started_at: overrides.started_at ?? "2026-08-01T00:00:00Z",
    completed_at: overrides.completed_at ?? "2026-08-01T00:00:01Z",
    created_at: overrides.created_at ?? "2026-08-01T00:00:02Z",
    ...overrides,
  };
}


/** 构造列表成功 DTO；调用方可替换 run、分页或链路字段验证失败关闭。 */
function makeListPayload(runs = [makeRun()], overrides = {}) {
  return {
    adapter_contract_runs: runs,
    limit: 50,
    next_cursor: "eyJuZXh0IjoxfQ",
    request_id: "req_prd064_browser_list",
    trace_id: "trc_prd064_browser_list",
    ...overrides,
  };
}


/** 构造一条详情 case，复合 owner、状态和 failure code 与父 run 一致。 */
function makeCase(index, overrides = {}) {
  const failed = overrides.status === "failed";
  return {
    adapter_contract_case_id: "acc_" + String(index).repeat(32),
    tenant_id: OWNER_ID,
    adapter_contract_run_id: RUN_ID,
    contract_case_key: "prd064_contract_case_" + index,
    spec_id: "adapter-spec-zhipu-v1",
    provider_type: "zhipu",
    adapter_name: "openai_compatible",
    adapter_version: "1.0.0",
    case_kind: index === 1 ? "request" : "usage",
    variant: index === 1 ? "bearer" : "standard",
    auth_type: "bearer",
    status: failed ? "failed" : "passed",
    failure_code: failed ? "adapter.contract_usage_failed" : null,
    duration_ms: 12 + index,
    evidence_hash: String(index + 2).repeat(64),
    created_at: "2026-08-01T00:00:02Z",
    ...overrides,
  };
}


/** 构造详情成功 DTO，默认两个 case 全部通过。 */
function makeDetailPayload(overrides = {}) {
  return {
    adapter_contract_run: makeRun(),
    cases: [makeCase(1), makeCase(2)],
    request_id: "req_prd064_browser_detail",
    trace_id: "trc_prd064_browser_detail",
    ...overrides,
  };
}


/** 构造只实现 get 的大小写无关响应头替身。 */
function headersStub(values) {
  const normalized = Object.fromEntries(
    Object.entries(values).map(([key, value]) => [key.toLowerCase(), value]),
  );
  return { get: (name) => normalized[String(name).toLowerCase()] ?? null };
}


/** 证据目录只接受固定根下的全新一级 run-id。 */
test("PRD-064 artifact path is fixed under the dedicated evidence root", () => {
  const target = path.join(REPOSITORY_ROOT, ".artifacts", "prd064-browser", "run-contract-001");
  assert.equal(resolveArtifactDirectory(target, REPOSITORY_ROOT), target);
  for (const rejected of [
    path.join(REPOSITORY_ROOT, ".artifacts", "prd064-browser"),
    path.join(REPOSITORY_ROOT, ".artifacts", "outside", "run-contract-001"),
    path.join(target, "nested"),
  ]) {
    assert.throws(
      () => resolveArtifactDirectory(rejected, REPOSITORY_ROOT),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "outside_or_nested",
    );
  }
});


/** 只放行固定 localhost Origin，列表和详情必须精确匹配参数、Bearer 与 request_id。 */
test("PRD-064 loopback and adapter contract request contracts fail closed", () => {
  const consoleOrigin = normalizeLoopbackOrigin("http://localhost:5173", 5173);
  const controlOrigin = normalizeLoopbackOrigin("http://localhost:8001", 8001);
  assert.equal(consoleOrigin, "http://localhost:5173");
  for (const value of [
    "http://127.0.0.1:5173",
    "https://localhost:5173",
    "http://localhost:5173/path",
    "http://user:pass@localhost:5173",
  ]) assert.throws(() => normalizeLoopbackOrigin(value, 5173), BrowserAcceptanceFailure);

  const allowed = new Set([consoleOrigin, controlOrigin, "http://localhost:8000"]);
  assert.equal(browserRequestAllowed("http://localhost:8001/adapter-contract-runs", allowed), true);
  assert.equal(browserRequestAllowed("https://example.com/adapter-contract-runs", allowed), false);
  assert.equal(browserRequestAllowed(
    "http://localhost:8001/adapter-contract-runs?tenant_id=other",
    allowed,
  ), false);
  assert.equal(browserRequestAllowed(
    "http://localhost:8001/adapter-contract-runs?token=value",
    allowed,
  ), false);

  const list = adapterContractRequestDescriptor(
    requestStub("http://localhost:8001/adapter-contract-runs?suite_kind=merge_gate&limit=50"),
    controlOrigin,
  );
  assert.deepEqual(list, {
    kind: "list",
    suite_kind: "merge_gate",
    status: "",
    mode: "",
    cursor_present: false,
  });
  const filtered = adapterContractRequestDescriptor(
    requestStub(
      "http://localhost:8001/adapter-contract-runs?suite_kind=probe&status=passed&mode=gateway_probe&limit=50&cursor=YWJj",
    ),
    controlOrigin,
  );
  assert.deepEqual(filtered, {
    kind: "list",
    suite_kind: "probe",
    status: "passed",
    mode: "gateway_probe",
    cursor_present: true,
  });
  const paginationBackendUrl = new URL(buildPaginationBackendUrl(
    "http://localhost:8001/adapter-contract-runs?suite_kind=probe&status=passed&mode=gateway_probe&limit=50&cursor=YWJj",
    controlOrigin,
  ));
  assert.equal(paginationBackendUrl.origin, controlOrigin);
  assert.equal(paginationBackendUrl.searchParams.get("limit"), "1");
  assert.equal(paginationBackendUrl.searchParams.get("suite_kind"), "probe");
  assert.equal(paginationBackendUrl.searchParams.get("status"), "passed");
  assert.equal(paginationBackendUrl.searchParams.get("mode"), "gateway_probe");
  assert.equal(paginationBackendUrl.searchParams.get("cursor"), "YWJj");
  const isolatedBackendUrl = new URL(mapControlBackendUrl(
    paginationBackendUrl.toString(),
    controlOrigin,
    "http://localhost:18001",
  ));
  assert.equal(isolatedBackendUrl.origin, "http://localhost:18001");
  assert.equal(isolatedBackendUrl.pathname, "/adapter-contract-runs");
  assert.equal(isolatedBackendUrl.searchParams.get("limit"), "1");
  assert.throws(
    () => mapControlBackendUrl(
      "http://localhost:8000/adapter-contract-runs",
      controlOrigin,
      "http://localhost:18001",
    ),
    BrowserAcceptanceFailure,
  );
  for (const rejected of [
    "http://localhost:8000/adapter-contract-runs?suite_kind=merge_gate&limit=50",
    "http://localhost:8001/adapter-contract-runs/" + RUN_ID + "?suite_kind=merge_gate&limit=50",
    "http://localhost:8001/adapter-contract-runs?suite_kind=merge_gate&limit=50&tenant_id=other",
    "http://localhost:8001/adapter-contract-runs?suite_kind=merge_gate&limit=50#fragment",
  ]) assert.throws(
    () => buildPaginationBackendUrl(rejected, controlOrigin),
    BrowserAcceptanceFailure,
  );
  const detail = adapterContractRequestDescriptor(
    requestStub("http://localhost:8001/adapter-contract-runs/" + RUN_ID, {
      requestId: "console_adapter_contract_detail_1785500000000_abc123def456",
    }),
    controlOrigin,
  );
  assert.deepEqual(detail, { kind: "detail", run_id: RUN_ID });
  for (const request of [
    requestStub("http://localhost:8001/adapter-contract-runs?suite_kind=merge_gate&limit=50&limit=10"),
    requestStub("http://localhost:8001/adapter-contract-runs?suite_kind=unknown&limit=50"),
    requestStub("http://localhost:8001/adapter-contract-runs?suite_kind=merge_gate&limit=50&unknown=x"),
    requestStub("http://localhost:8001/adapter-contract-runs?suite_kind=merge_gate&limit=50", {
      authorization: "",
    }),
    requestStub("http://localhost:8001/adapter-contract-runs?suite_kind=merge_gate&limit=50", {
      requestId: "bad",
    }),
  ]) assert.equal(adapterContractRequestDescriptor(request, controlOrigin), null);
});


/** 浏览器子进程不得继承宿主秘密，六个可写目录必须固定到当前证据批次。 */
test("PRD-064 browser environment strips secrets and pins writable roots", () => {
  const artifact = path.join(REPOSITORY_ROOT, ".artifacts", "prd064-browser", "run-env-contract");
  const environment = buildBrowserEnvironment({
    PATH: "runtime-path",
    METAHUB_ACCEPTANCE_ADMIN_PASSWORD: "must-not-pass",
    OPENAI_API_KEY: "must-not-pass",
    SESSION_TOKEN: "must-not-pass",
    AWS_ACCESS_KEY_ID: "must-not-pass",
    PUBLIC_SETTING: "must-not-pass",
  }, artifact);
  assert.equal(environment.PATH, "runtime-path");
  for (const key of [
    "METAHUB_ACCEPTANCE_ADMIN_PASSWORD",
    "OPENAI_API_KEY",
    "SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "PUBLIC_SETTING",
  ]) assert.equal(Object.hasOwn(environment, key), false);
  assert.equal(environment.TEMP, artifact);
  assert.equal(environment.TMP, artifact);
  assert.equal(environment.HOME, path.join(artifact, "browser-home"));
  assert.equal(environment.USERPROFILE, path.join(artifact, "browser-home"));
  assert.equal(environment.LOCALAPPDATA, path.join(artifact, "browser-home", "local"));
  assert.equal(environment.APPDATA, path.join(artifact, "browser-home", "roaming"));
  const processEnvironment = { PATH: "runtime-path", TEMP: "outside", HOME: "outside" };
  assert.equal(pinBrowserWritableRoots(processEnvironment, artifact), processEnvironment);
  assert.equal(processEnvironment.TEMP, artifact);
  assert.equal(processEnvironment.TMP, artifact);
  assert.equal(processEnvironment.HOME, path.join(artifact, "browser-home"));
});


/** 列表合同验证 owner、筛选、suite/mode 语义、排序和分页，而不返回原始 DTO。 */
test("PRD-064 list payload validates owner, query and immutable run facts", () => {
  const descriptor = {
    kind: "list",
    suite_kind: "merge_gate",
    status: "",
    mode: "",
    cursor_present: false,
  };
  const second = makeRun({
    adapter_contract_run_id: SECOND_RUN_ID,
    started_at: "2026-07-31T23:59:57Z",
    completed_at: "2026-07-31T23:59:58Z",
    created_at: "2026-07-31T23:59:59Z",
  });
  assert.deepEqual(
    validateAdapterContractListPayload(makeListPayload([makeRun(), second]), OWNER_ID, descriptor),
    {
      entry_count: 2,
      has_next_page: true,
      first_status: "passed",
      first_run_id: RUN_ID,
      suite_kind: "merge_gate",
      mode: "",
    },
  );
  const failed = makeRun({ status: "failed" });
  assert.equal(validateAdapterContractListPayload(
    makeListPayload([failed], { next_cursor: null }),
    OWNER_ID,
    { ...descriptor, status: "failed", mode: "mock" },
  ).first_status, "failed");
  const probe = makeRun({ suite_kind: "probe" });
  assert.equal(validateAdapterContractListPayload(
    makeListPayload([probe], { next_cursor: null }),
    OWNER_ID,
    { ...descriptor, suite_kind: "probe", mode: "gateway_probe" },
  ).suite_kind, "probe");
  assert.equal(validateAdapterContractListPayload(
    makeListPayload([], { next_cursor: null }),
    OWNER_ID,
    { ...descriptor, suite_kind: "probe", mode: "mock" },
  ).entry_count, 0);
  // 浏览器专用分页步骤只接受真实 Control 返回的 limit=1，默认生产读取仍由前述 50 合同锁定。
  assert.equal(validateAdapterContractListPayload(
    makeListPayload([makeRun()], { limit: 1 }),
    OWNER_ID,
    descriptor,
    1,
  ).entry_count, 1);
});


/** 串租户、敏感扩展、计数漂移、筛选漂移和乱序任一发生都拒绝整批列表。 */
test("PRD-064 list payload rejects damaged, cross-owner or sensitive contracts", () => {
  const descriptor = {
    kind: "list",
    suite_kind: "merge_gate",
    status: "",
    mode: "",
    cursor_present: false,
  };
  const cases = [];
  cases.push(makeListPayload([makeRun({ tenant_id: "tenant_other" })]));
  const leaked = makeListPayload();
  leaked.adapter_contract_runs[0].provider_key = "masked";
  cases.push(leaked);
  cases.push(makeListPayload([makeRun({ total_cases: 3 })]));
  cases.push(makeListPayload([makeRun({ suite_kind: "probe" })]));
  const duplicate = makeListPayload([makeRun(), makeRun()]);
  cases.push(duplicate);
  const later = makeRun({
    adapter_contract_run_id: SECOND_RUN_ID,
    started_at: "2026-08-01T00:00:03Z",
    completed_at: "2026-08-01T00:00:04Z",
    created_at: "2026-08-01T00:00:05Z",
  });
  cases.push(makeListPayload([makeRun(), later]));
  for (const payload of cases) {
    assert.throws(
      () => validateAdapterContractListPayload(payload, OWNER_ID, descriptor),
      BrowserAcceptanceFailure,
    );
  }
});


/** 详情合同交叉验证父 run、复合 owner、case 唯一性与通过失败计数。 */
test("PRD-064 detail payload validates parent and case facts", () => {
  assert.deepEqual(validateAdapterContractDetailPayload(makeDetailPayload(), OWNER_ID, RUN_ID), {
    case_count: 2,
    provider_count: 1,
    failed_count: 0,
  });
  const failedPayload = makeDetailPayload({
    adapter_contract_run: makeRun({ status: "failed" }),
    cases: [makeCase(1), makeCase(2, { status: "failed" })],
  });
  assert.equal(
    validateAdapterContractDetailPayload(failedPayload, OWNER_ID, RUN_ID).failed_count,
    1,
  );
  const wrongOwner = makeDetailPayload();
  wrongOwner.cases[0].tenant_id = "tenant_other";
  assert.throws(
    () => validateAdapterContractDetailPayload(wrongOwner, OWNER_ID, RUN_ID),
    BrowserAcceptanceFailure,
  );
  const duplicate = makeDetailPayload();
  duplicate.cases[1] = { ...duplicate.cases[0] };
  assert.throws(
    () => validateAdapterContractDetailPayload(duplicate, OWNER_ID, RUN_ID),
    BrowserAcceptanceFailure,
  );
  const leaked = makeDetailPayload();
  leaked.cases[0].response_body = "masked";
  assert.throws(
    () => validateAdapterContractDetailPayload(leaked, OWNER_ID, RUN_ID),
    BrowserAcceptanceFailure,
  );
});


/** 后端导航 owner 与确定性 403/503 都必须携带冻结权限、统一错误和 no-store。 */
test("PRD-064 navigation and deterministic error contracts are explicit", () => {
  assert.equal(validateNavigationOwner({
    navigation_items: [{
      id: "adapter-contract-runs",
      target_section: "adapter-contract-runs",
      visible: true,
      disabled: false,
      required_permissions: ["provider.health.read"],
    }],
  }), true);
  assert.throws(
    () => validateNavigationOwner({ navigation_items: [] }),
    BrowserAcceptanceFailure,
  );
  for (const status of [403, 503]) {
    const suffix = String(status);
    const payload = status === 403
      ? {
          code: "auth.permission_denied",
          message: "当前用户无权查看契约测试报告",
          request_id: "req_prd064_browser_fault_" + suffix,
          trace_id: "trc_prd064_browser_fault_" + suffix,
        }
      : {
          code: "adapter.contract_storage_unavailable",
          message: "契约测试运行暂不可用",
          request_id: "req_prd064_browser_fault_" + suffix,
          trace_id: "trc_prd064_browser_fault_" + suffix,
        };
    const headers = headersStub({
      "Cache-Control": "no-store",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
    });
    assert.deepEqual(validateUnifiedErrorPayload(payload, headers, status), {
      status,
      code_confirmed: true,
      no_store_confirmed: true,
    });
    assert.throws(
      () => validateUnifiedErrorPayload(payload, headersStub({}), status),
      BrowserAcceptanceFailure,
    );
  }
  assert.equal(validateFaultDomProjection({
    error_visible: true,
    request_id_visible: true,
    table_count: 0,
    refresh_count: 0,
    detail_count: 0,
  }, 403), true);
  assert.equal(validateFaultDomProjection({
    error_visible: true,
    request_id_visible: true,
    table_count: 1,
    refresh_count: 1,
    detail_count: 2,
  }, 503), true);
  assert.throws(
    () => validateFaultDomProjection({
      error_visible: true,
      request_id_visible: true,
      table_count: 1,
      refresh_count: 1,
      detail_count: 1,
    }, 403),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "error_dom",
  );
  assert.throws(
    () => validateFaultDomProjection({
      error_visible: true,
      request_id_visible: true,
      table_count: 0,
      refresh_count: 0,
      detail_count: 0,
    }, 503),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "error_dom",
  );
});


/** 布局合同拒绝页面横溢、侧栏遮挡、表格横溢和按钮裁切。 */
test("PRD-064 layout contract covers desktop and mobile overflow", () => {
  const metrics = {
    viewport_width: 1280,
    viewport_height: 720,
    document_scroll_width: 1280,
    body_scroll_width: 1280,
    root_visible: true,
    root_horizontal_inside: true,
    root_scroll_width: 980,
    root_client_width: 980,
    navigation_main_overlap_area: 0,
    overflowing_table_count: 0,
    overflowing_button_count: 0,
    empty_button_count: 0,
    visible_run_count: 50,
    summary_item_count: 4,
  };
  assert.equal(assertLayoutMetrics(metrics, VIEWPORTS[0]), metrics);
  const mobile = {
    ...metrics,
    viewport_width: 390,
    viewport_height: 844,
    document_scroll_width: 390,
    body_scroll_width: 390,
    root_scroll_width: 358,
    root_client_width: 358,
    visible_run_count: 2,
  };
  assert.equal(assertLayoutMetrics(mobile, VIEWPORTS[1]), mobile);
  assert.throws(
    () => assertLayoutMetrics({ ...mobile, overflowing_table_count: 1 }, VIEWPORTS[1]),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "table_overflow",
  );
  assert.throws(
    () => assertLayoutMetrics({ ...metrics, overflowing_button_count: 1 }, VIEWPORTS[0]),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "button_overflow",
  );
});


/** Console 与最终证据都失败关闭，确定性 403/503 只豁免标准浏览器资源错误。 */
test("PRD-064 console and evidence privacy contracts fail closed", () => {
  assert.equal(classifyBrowserConsoleMessage({ type: "log", text: "ready" }, []), "ignored");
  assert.equal(classifyBrowserConsoleMessage(
    { type: "error", text: "Failed to load resource: 403" },
    [403],
  ), "expected_fault");
  assert.equal(classifyBrowserConsoleMessage(
    { type: "error", text: "Failed to load resource: 500" },
    [403],
  ), "unexpected_error");
  assert.equal(browserConsolePrivacyViolation("normal message", ["canary"]), false);
  assert.equal(browserConsolePrivacyViolation("Bearer session-value", []), true);
  assert.equal(browserConsolePrivacyViolation("contains canary", ["canary"]), true);
  const evidence = {
    prd_id: "PRD-064",
    status: "passed",
    checks: {
      report_surface: { pagination_confirmed: true },
      network: { pagination_request_count: 3, isolated_control_proxy_confirmed: true },
    },
  };
  assert.equal(assertSafeEvidence(evidence, ["not-present"]), evidence);
  for (const unsafe of [
    { tenant_id: "masked" },
    { endpoint_url: "masked" },
    { opaque_cursor: "masked" },
    { value: "Bearer session-contract-value" },
  ]) assert.throws(() => assertSafeEvidence(unsafe), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence(evidence, ["PRD-064"]), BrowserAcceptanceFailure);
});


/** 源码门禁锁定专用证据根、链接校验、真实流程、双视口、故障、隐私和注销。 */
test("PRD-064 acceptance source contains the complete safe browser workflow", async () => {
  const source = await readFile(
    new URL("../../../scripts/acceptance/prd064-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /[\\/]\.tmp[\\/]|C:\\\\tmp|\.patch/i);
  assert.match(source, /\.artifacts["'],\s*["']prd064-browser/);
  assert.match(source, /realpath\(directory\)/);
  assert.match(source, /already_exists/);
  assert.match(source, /buildBrowserEnvironment\(process\.env, config\.artifactDirectory\)/);
  assert.match(source, /pinBrowserWritableRoots\(process\.env, config\.artifactDirectory\)/);
  assert.match(source, /flag:\s*"wx"/);
  assert.match(source, /\/adapter-contract-runs/);
  assert.match(source, /data-console-nav="adapter-contract-runs"/);
  assert.match(source, /data-adapter-contract-suite="probe"/);
  assert.match(source, /data-adapter-contract-next/);
  assert.match(source, /data-adapter-contract-detail/);
  assert.match(source, /route\.fetch\(\{ url: backendUrl, maxRedirects: 0 \}\)/);
  assert.match(source, /METAHUB_ACCEPTANCE_CONTROL_BACKEND_URL/);
  assert.match(source, /mapControlBackendUrl/);
  assert.match(source, /target\.searchParams\.set\("limit", "1"\)/);
  assert.match(source, /armFault\(status\)/);
  assert.match(source, /validateFaultDomProjection\(facts, status\)/);
  assert.match(source, /exerciseDeterministicFault\(page, config, identity, tracker, 403\)/);
  assert.match(source, /exerciseDeterministicFault\(page, config, identity, tracker, 503\)/);
  assert.match(source, /desktop-1280x720/);
  assert.match(source, /mobile-390x844/);
  assert.match(source, /inspectPngPixels/);
  assert.match(source, /AUTH_STORAGE_KEYS/);
  assert.match(source, /Promise\.all\(\[\s*page\.waitForResponse[\s\S]*?#logout-button/);
});
