/**
 * PRD-061 浏览器验收器纯合同测试。
 * 测试不启动服务、浏览器或文件写入，只验证路径、网络、事件、DTO、布局、隐私和源码门禁。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BrowserAcceptanceFailure,
  VIEWPORTS,
  assertLayoutMetrics,
  assertSafeEvidence,
  buildBrowserEnvironment,
  browserRequestAllowed,
  classifyBrowserConsoleMessage,
  hasExactSearchParams,
  isExactProbeRequest,
  isExactStatusViewedEvent,
  normalizeLoopbackOrigin,
  resolveArtifactDirectory,
  validateProviderStatusPayload,
} from "../../../scripts/acceptance/prd061-browser-acceptance.mjs";

/** 从测试文件位置计算仓库根，避免依赖调用命令的当前工作目录。 */
const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");


/** 构造不含真实业务数据的最小状态响应，覆盖 PAGE-088 强制指标合同。 */
function statusPayloadFixture() {
  return {
    provider_statuses: [{
      provider_id: "provider_contract",
      provider_type: "deepseek",
      recent_failures: [{ failure_kind: "provider" }],
      call_latency_p95_ms: 120,
      stream_first_chunk_p95_ms: 80,
      probe_latency_p95_ms: 60,
      terminal_call_count: 5,
      eligible_call_count: 4,
      success_count: 3,
      provider_failure_count: 1,
      credential_failure_count: 1,
      excluded_call_count: 1,
      success_rate_bps: 7500,
      probe_total: 2,
      probe_success_count: 1,
      probe_failure_count: 1,
      probe_inconclusive_count: 0,
      probe_success_rate_bps: 5000,
      call_latency_sample_count: 1,
      stream_first_chunk_sample_count: 1,
      probe_latency_sample_count: 1,
    }],
    summary: { total: 1 },
    total: 1,
    limit: 50,
    offset: 0,
  };
}


/** 路径合同只接受固定证据根的全新一级 run-id。 */
test("PRD-061 artifact path is fixed under the workspace evidence root", () => {
  const target = path.join(REPOSITORY_ROOT, ".artifacts", "prd061-browser", "run-contract-001");
  assert.equal(resolveArtifactDirectory(target, REPOSITORY_ROOT), target);
  assert.throws(
    () => resolveArtifactDirectory(path.join(REPOSITORY_ROOT, ".artifacts", "outside"), REPOSITORY_ROOT),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "outside_or_nested",
  );
  assert.throws(
    () => resolveArtifactDirectory(path.join(target, "nested"), REPOSITORY_ROOT),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "outside_or_nested",
  );
});


/** 网络合同只放行三个 loopback Origin，且同源 URL 也不能携带敏感查询键。 */
test("PRD-061 loopback and browser request guards fail closed", () => {
  assert.equal(normalizeLoopbackOrigin("http://localhost:5173", 5173), "http://localhost:5173");
  assert.throws(
    () => normalizeLoopbackOrigin("http://127.0.0.1:5173", 5173),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "host",
  );
  assert.throws(
    () => normalizeLoopbackOrigin("https://localhost:5173", 5173),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "protocol",
  );
  const allowed = new Set(["http://localhost:5173", "http://localhost:8001"]);
  assert.equal(browserRequestAllowed("http://localhost:8001/status/providers?limit=50", allowed), true);
  assert.equal(browserRequestAllowed("https://example.com/status/providers", allowed), false);
  assert.equal(browserRequestAllowed("http://localhost:8001/status/providers?tenant_id=other", allowed), false);
  assert.equal(browserRequestAllowed("http://localhost:8001/status/providers?token=value", allowed), false);
});


/** 浏览器子进程环境不得继承宿主机密码、Token、Key 或 Secret，六个目录必须绑定当前批次。 */
test("PRD-061 browser process environment strips secrets and pins writable roots", () => {
  const artifact = path.join(REPOSITORY_ROOT, ".artifacts", "prd061-browser", "run-env-contract");
  const environment = buildBrowserEnvironment({
    PATH: "runtime-path",
    METAHUB_ACCEPTANCE_ADMIN_PASSWORD: "must-not-pass",
    OPENAI_API_KEY: "must-not-pass",
    SESSION_TOKEN: "must-not-pass",
    AWS_ACCESS_KEY_ID: "must-not-pass",
    GITHUB_PAT: "must-not-pass",
    GOOGLE_APPLICATION_CREDENTIALS: "must-not-pass",
    PUBLIC_SETTING: "must-not-pass",
  }, artifact);
  assert.equal(environment.PATH, "runtime-path");
  assert.equal(Object.hasOwn(environment, "PUBLIC_SETTING"), false);
  assert.equal(Object.hasOwn(environment, "METAHUB_ACCEPTANCE_ADMIN_PASSWORD"), false);
  assert.equal(Object.hasOwn(environment, "OPENAI_API_KEY"), false);
  assert.equal(Object.hasOwn(environment, "SESSION_TOKEN"), false);
  assert.equal(Object.hasOwn(environment, "AWS_ACCESS_KEY_ID"), false);
  assert.equal(Object.hasOwn(environment, "GITHUB_PAT"), false);
  assert.equal(Object.hasOwn(environment, "GOOGLE_APPLICATION_CREDENTIALS"), false);
  assert.equal(environment.TEMP, artifact);
  assert.equal(environment.TMP, artifact);
  assert.equal(environment.HOME, path.join(artifact, "browser-home"));
  assert.equal(environment.USERPROFILE, path.join(artifact, "browser-home"));
  assert.equal(environment.LOCALAPPDATA, path.join(artifact, "browser-home", "local"));
  assert.equal(environment.APPDATA, path.join(artifact, "browser-home", "roaming"));
});


/** status.viewed 必须严格匹配五个顶层字段和三个低风险 payload 字段。 */
test("PRD-061 status viewed event rejects provider filter and tenant additions", () => {
  const event = {
    event_type: "status.viewed",
    navigation_item_id: "provider-health",
    target_section: "provider-health",
    path: "/#provider-health",
    payload: { source: "sidebar", hash: "#provider-health", viewport_class: "desktop" },
  };
  assert.equal(isExactStatusViewedEvent(event), true);
  assert.equal(isExactStatusViewedEvent({ ...event, tenant_id: "other" }), false);
  assert.equal(isExactStatusViewedEvent({
    ...event,
    payload: { ...event.payload, source: "direct_hash" },
  }), false);
  assert.equal(isExactStatusViewedEvent({
    ...event,
    payload: { ...event.payload, viewport_class: "mobile" },
  }), false);
  assert.equal(isExactStatusViewedEvent({
    ...event,
    payload: { ...event.payload, provider_type: "deepseek" },
  }), false);
});


/** 状态 DTO 必须同时具备三类 P95、调用计数、探测计数、故障数组和可筛选类型。 */
test("PRD-061 provider status payload contract requires operational metrics", () => {
  assert.deepEqual(validateProviderStatusPayload(statusPayloadFixture()), {
    provider_present: true,
    filterType: "deepseek",
  });
  const missingMetric = structuredClone(statusPayloadFixture());
  delete missingMetric.provider_statuses[0].excluded_call_count;
  assert.throws(
    () => validateProviderStatusPayload(missingMetric),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "metrics",
  );
  const contradictory = structuredClone(statusPayloadFixture());
  contradictory.provider_statuses[0].terminal_call_count = 6;
  assert.throws(
    () => validateProviderStatusPayload(contradictory),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "call_invariant",
  );
  const missingP95 = structuredClone(statusPayloadFixture());
  delete missingP95.provider_statuses[0].probe_latency_p95_ms;
  assert.throws(
    () => validateProviderStatusPayload(missingP95),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "probe_latency_p95_ms",
  );
  const empty = { provider_statuses: [], summary: { total: 0 }, total: 0, limit: 50, offset: 0 };
  assert.deepEqual(validateProviderStatusPayload(empty, { requireProvider: false }), {
    provider_present: false,
    filterType: "",
  });
});


/** 筛选和探测请求必须精确匹配白名单，不允许错值、重复键、额外 query 或非空正文。 */
test("PRD-061 filter and probe request contracts are exact", () => {
  const exactFilter = new URL("http://localhost:8001/status/providers?provider_type=deepseek&limit=50&offset=0");
  assert.equal(hasExactSearchParams(exactFilter, { provider_type: "deepseek", limit: 50, offset: 0 }), true);
  assert.equal(
    hasExactSearchParams(
      new URL(`${exactFilter}&provider_type=zhipu`),
      { provider_type: "deepseek", limit: 50, offset: 0 },
    ),
    false,
  );
  assert.equal(
    hasExactSearchParams(
      new URL(`${exactFilter}&tenant_id=other`),
      { provider_type: "deepseek", limit: 50, offset: 0 },
    ),
    false,
  );
  /** 构造最小 Playwright Request 替身，逐项覆盖探测 URL、方法、正文和 Content-Type。 */
  const request = (overrides = {}) => ({
    url: () => overrides.url || "http://localhost:8001/providers/provider_contract/probe",
    method: () => overrides.method || "POST",
    postData: () => overrides.postData ?? "{}",
    headers: () => ({ "content-type": overrides.contentType || "application/json" }),
  });
  assert.equal(isExactProbeRequest(request(), "http://localhost:8001"), true);
  assert.equal(isExactProbeRequest(request({ url: "http://localhost:8001/providers/p/probe?force=1" }), "http://localhost:8001"), false);
  assert.equal(isExactProbeRequest(request({ postData: '{"provider_key":"x"}' }), "http://localhost:8001"), false);
  assert.equal(isExactProbeRequest(request({ contentType: "text/plain" }), "http://localhost:8001"), false);
});


/** 布局合同拒绝根横向溢出、侧栏遮挡和按钮文本裁切。 */
test("PRD-061 layout contract rejects overflow overlap and clipped buttons", () => {
  const metrics = {
    viewport_width: 1280,
    viewport_height: 720,
    document_scroll_width: 1280,
    body_scroll_width: 1280,
    root_visible: true,
    root_scroll_width: 1000,
    root_client_width: 1000,
    navigation_main_overlap_area: 0,
    overflowing_button_count: 0,
  };
  assert.equal(assertLayoutMetrics(metrics, VIEWPORTS[0]), metrics);
  assert.throws(
    () => assertLayoutMetrics({ ...metrics, root_scroll_width: 1002 }, VIEWPORTS[0]),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "root_overflow",
  );
  assert.throws(
    () => assertLayoutMetrics({ ...metrics, overflowing_button_count: 1 }, VIEWPORTS[0]),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "button_overflow",
  );
});


/** Console 分类只豁免验收器主动制造的状态 503，其他 error 均保持失败。 */
test("PRD-061 browser console classification isolates the simulated 503", () => {
  assert.equal(classifyBrowserConsoleMessage({
    type: "error",
    text: "Failed to load resource: the server responded with a status of 503",
    locationUrl: "http://127.0.0.1:8001/status/providers?limit=50",
  }), "expected_http_503");
  assert.equal(classifyBrowserConsoleMessage({
    type: "error",
    text: "Failed to load resource: the server responded with a status of 503",
    locationUrl: "",
  }), "unexpected_error");
  assert.equal(classifyBrowserConsoleMessage({
    type: "error",
    text: "Unhandled failure",
    locationUrl: "http://127.0.0.1:5173/",
  }), "unexpected_error");
});


/** 证据对象拒绝敏感字段名和运行时敏感原值。 */
test("PRD-061 browser evidence remains summary-only", () => {
  const evidence = {
    prd_id: "PRD-061",
    status: "passed",
    checks: { provider_present: true, query_controls: { applied: true } },
  };
  assert.equal(assertSafeEvidence(evidence, ["not-present"]), evidence);
  assert.throws(
    () => assertSafeEvidence({ filter: { provider_type: "masked" } }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "forbidden_key",
  );
  assert.throws(
    () => assertSafeEvidence({ provider_key: "masked" }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "forbidden_key",
  );
  assert.throws(
    () => assertSafeEvidence(evidence, ["PRD-061"]),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "secret_value",
  );
});


/** 源码门禁固定证据根、六个浏览器环境目录、写一次发布以及 PAGE-088 关键交互。 */
test("PRD-061 acceptance source contains the complete safe browser workflow", async () => {
  const source = await readFile(
    new URL("../../../scripts/acceptance/prd061-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /(?:[\\/])\.tmp(?:[\\/]|["'`])|C:\\\\tmp/i);
  assert.match(source, /\.artifacts["'],\s*["']prd061-browser/);
  assert.match(source, /buildBrowserEnvironment\(process\.env, config\.artifactDirectory\)/);
  assert.match(source, /flag:\s*"wx"/);
  assert.match(source, /\/status\/providers/);
  assert.match(source, /\/providers\\\/\[\^\/\]\+\\\/probe/);
  assert.match(source, /status\.viewed/);
  assert.match(source, /provider-status-loading:not\(\.error-state\)/);
  assert.match(source, /req_prd061_browser_503/);
  assert.match(source, /provider-status-empty/);
  assert.match(source, /assertTransientCanaryAbsent/);
  assert.match(source, /waitForStatusRootReplacement/);
  assert.match(source, /hasExactSearchParams/);
  assert.match(source, /isExactProbeRequest/);
  assert.match(source, /nonTokenSessionEntries/);
  assert.match(source, /inspectPngPixels/);
  assert.match(source, /AUTH_STORAGE_KEYS/);
});
