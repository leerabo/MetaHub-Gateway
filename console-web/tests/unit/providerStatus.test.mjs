// PRD-061 前端专项覆盖状态客户端、双权限、XSS、指标渲染、owner 生命周期和唯一访问事件。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createProviderStatusVisitTracker } from "../../src/app/main.js";
import {
  renderDeveloperStart,
  renderProviderStatusLoading,
} from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  canonicalProviderStatusFilters,
  createProviderStatusPageState,
  providerStatusFilterKey,
  providerStatusIdentitySnapshot,
} from "../../src/features/provider-health/providerStatusPageState.js";
import { renderProviderStatusPanel } from "../../src/features/provider-health/renderProviderStatusPanel.js";
import { recordConsolePageView } from "../../src/lib/consoleShell.js";
import { listProviderStatuses } from "../../src/lib/providerStatus.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造包含真实 Session、租户和授权 revision 的测试身份，避免状态 owner 依赖角色名补权。 */
function statusAuth(permissionCodes = [
  PERMISSIONS.PROVIDER_HEALTH_READ,
  PERMISSIONS.REQUEST_LOG_READ,
  PERMISSIONS.PROVIDER_HEALTH_PROBE,
], options = {}) {
  // 测试身份也必须显式构造租户、Session 与权限指纹，禁止因角色名得到隐式授权。
  return withPermissionSnapshot({
    user: {
      tenant_id: options.tenantId ?? "tenant_status_1",
      user_id: options.userId ?? "usr_status_1",
      email: "ops@example.com",
      role: options.role ?? "ops_admin",
    },
    session: {
      session_id: options.sessionId ?? "ses_status_1",
      session_revision: options.sessionRevision ?? 1,
    },
  }, permissionCodes, {
    permissionRevision: options.permissionRevision ?? 7,
    authorizationFingerprint: options.authorizationFingerprint ?? "status_fingerprint_7",
  });
}

/** 构造同时覆盖调用、流式、探测与故障历史的安全响应。 */
function statusPayload() {
  return {
    state: "ok",
    provider_statuses: [
      {
        provider_id: "Provider_A",
        tenant_id: "tenant_status_1",
        provider_code: "deepseek_<official>",
        name: "<状态 Provider>",
        provider_type: "deepseek",
        base_url: "https://api.example.com/v1&safe=true",
        auth_type: "bearer",
        provider_status: "active",
        health_status: "healthy",
        effective_health_status: "healthy",
        availability_status: "available",
        is_health_stale: false,
        maintenance_note: "<script>alert(`status-xss`)</script>",
        terminal_call_count: 45,
        eligible_call_count: 40,
        success_count: 39,
        provider_failure_count: 1,
        credential_failure_count: 2,
        excluded_call_count: 5,
        success_rate_bps: 9750,
        probe_total: 5,
        probe_success_count: 4,
        probe_failure_count: 1,
        probe_inconclusive_count: 0,
        probe_success_rate_bps: 8000,
        call_latency_sample_count: 39,
        call_latency_p95_ms: 120,
        stream_first_chunk_sample_count: 6,
        stream_first_chunk_p95_ms: 35,
        probe_latency_sample_count: 5,
        probe_latency_p95_ms: 88,
        latest_call_at: "2026-08-01T00:00:00.000Z",
        latest_probe_at: "2026-08-01T00:01:00.000Z",
        last_tested_at: "2026-08-01T00:01:00.000Z",
        last_probe_latency_ms: 88,
        last_error: {
          source: "non_stream_call",
          failure_kind: "provider",
          eligible_for_success_rate: true,
          error_code: "gateway.upstream_timeout",
          request_id: "req_<latest>",
          trace_id: "trc_latest",
          latency_ms: 5000,
          observed_at: "2026-07-31T23:59:00.000Z",
        },
        recent_failures: [
          {
            source: "stream_call",
            failure_kind: "excluded",
            eligible_for_success_rate: false,
            error_code: "gateway.client_disconnected",
            request_id: "req_</span><script>x</script>",
            trace_id: "trc_failure_1",
            latency_ms: 45,
            observed_at: "2026-07-31T23:58:00.000Z",
          },
        ],
        provider_created_at: "2026-01-01T00:00:00.000Z",
        provider_updated_at: "2026-08-01T00:00:00.000Z",
      },
    ],
    summary: { total: 1, healthy: 1, unhealthy: 0, observing: 0, unknown: 0, disabled: 0 },
    window_started_at: "2026-07-31T01:00:00.000Z",
    window_ended_at: "2026-08-01T01:00:00.000Z",
    history_started_at: "2026-07-25T01:00:00.000Z",
    total: 1,
    limit: 50,
    offset: 0,
    request_id: "req_status",
    trace_id: "trc_status",
    filters: { provider_type: "deepseek", limit: 50, offset: 0 },
  };
}

test("provider status client sends only fixed filters and bearer token", async () => {
  let captured;
  globalThis.fetch = async (url, options = {}) => {
    captured = { url: String(url), options };
    return {
      ok: true,
      headers: new Map(),
      async json() { return statusPayload(); },
    };
  };

  await listProviderStatuses({
    controlApiUrl: "http://control.local",
    token: "session-status-token",
    filters: {
      provider_id: "Provider_A",
      provider_type: "deepseek",
      health_status: "healthy",
      provider_status: "active",
      limit: 50,
      offset: 0,
      tenant_id: "tenant_other",
      window: "365d",
      base_url: "https://secret.example",
      api_key: "status-secret-canary",
    },
  });

  const parsed = new URL(captured.url);
  assert.equal(parsed.pathname, "/status/providers");
  assert.equal(parsed.searchParams.get("provider_id"), "Provider_A");
  assert.equal(parsed.searchParams.get("provider_type"), "deepseek");
  assert.equal(parsed.searchParams.get("limit"), "50");
  assert.equal(parsed.searchParams.has("tenant_id"), false);
  assert.equal(parsed.searchParams.has("window"), false);
  assert.equal(parsed.searchParams.has("base_url"), false);
  assert.equal(parsed.searchParams.has("api_key"), false);
  assert.equal(captured.options.headers.Authorization, "Bearer session-status-token");
  assert.doesNotMatch(captured.url, /status-secret-canary|tenant_other|secret\.example/);
});

test("provider status client maps non-json failures without exposing response body", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 503,
    headers: new Map([["X-Request-ID", "req_status_proxy"]]),
    async json() { throw new Error("proxy-secret-body"); },
  });

  await assert.rejects(
    () => listProviderStatuses({ controlApiUrl: "http://control.local", token: "token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.status, 503);
      assert.equal(error.requestId, "req_status_proxy");
      assert.doesNotMatch(error.message, /proxy-secret-body/);
      return true;
    },
  );
});

test("PAGE-088 renders independent metrics and escapes maintenance and failure fields", () => {
  const html = renderProviderStatusPanel(statusPayload(), statusAuth());

  assert.match(html, /id="provider-health-title">Provider 状态/);
  assert.match(html, /Provider 归因成功率/);
  assert.match(html, /97\.5%/);
  assert.match(html, /非流式调用 P95/);
  assert.match(html, /120 ms/);
  assert.match(html, /流式首块 P95/);
  assert.match(html, /35 ms/);
  assert.match(html, /80% \/ 88 ms/);
  assert.match(html, /Provider 失败/);
  assert.match(html, /探测不确定/);
  assert.match(html, /探测延迟样本/);
  assert.match(html, /最近故障（1）/);
  assert.match(html, /data-provider-probe="Provider_A"/);
  assert.match(html, /value="deepseek" selected/);
  assert.match(html, /&lt;script&gt;alert\(&#096;status-xss&#096;\)&lt;\/script&gt;/);
  assert.match(html, /req_&lt;\/span&gt;&lt;script&gt;x&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<状态 Provider>|<script>|req_<\/span>/);
});

test("PAGE-088 requires both read permissions while loading and errors remain traceable", () => {
  const healthOnly = statusAuth([PERMISSIONS.PROVIDER_HEALTH_READ]);
  const logsOnly = statusAuth([PERMISSIONS.REQUEST_LOG_READ]);
  assert.equal(renderProviderStatusPanel(statusPayload(), healthOnly), "");
  assert.equal(renderProviderStatusPanel(statusPayload(), logsOnly), "");

  const loading = renderProviderStatusPanel(null, statusAuth());
  const error = renderProviderStatusPanel({
    state: "error",
    message: "状态 <读取> 失败",
    requestId: "req_<status>",
    eventWarning: { message: "事件失败", requestId: "req_event" },
  }, statusAuth());
  assert.match(loading, /正在读取 Provider 状态/);
  assert.match(error, /状态 &lt;读取&gt; 失败/);
  assert.match(error, /request_id: req_&lt;status&gt;/);
  assert.match(error, /访问记录暂不可用/);
  assert.doesNotMatch(error, /状态 <读取>/);
});

test("developer shell dispatches PAGE-088 only for the authorized provider-health navigation", () => {
  const container = { innerHTML: "" };
  const authContext = statusAuth();
  renderDeveloperStart(container, {
    authContext,
    consoleShellState: {
      state: "ok",
      activeNavigationItemId: "provider-health",
      navigation: {
        active_navigation_item_id: "provider-health",
        navigation_items: [{
          id: "provider-health",
          label: "Provider 状态",
          group: "可观测性",
          target_section: "provider-health",
          required_permissions: [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ],
          visible: true,
          disabled: false,
          order: 30,
        }],
      },
    },
    providerHealthState: statusPayload(),
  });
  assert.match(container.innerHTML, /data-active-navigation="provider-health"/);
  assert.match(container.innerHTML, /data-provider-status-root/);
  assert.match(container.innerHTML, /Provider 归因成功率/);
});

test("provider status loading projection requires double permission and exact backend navigation", () => {
  const state = {
    state: "ok",
    activeNavigationItemId: "overview",
    navigation: {
      active_navigation_item_id: "overview",
      navigation_items: [{
        id: "provider-health",
        label: "Provider 状态",
        group: "可观测性",
        target_section: "provider-health",
        required_permissions: [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ],
        visible: true,
        disabled: false,
      }],
    },
  };
  const container = { innerHTML: "existing" };
  const projected = renderProviderStatusLoading(container, {
    authContext: statusAuth(),
    consoleShellState: state,
  });
  assert.equal(projected.activeNavigationItemId, "provider-health");
  assert.match(container.innerHTML, /正在读取 Provider 状态/);
  assert.match(container.innerHTML, /data-active-navigation="provider-health"/);
  assert.equal(state.activeNavigationItemId, "overview");

  const denied = { innerHTML: "preserved" };
  assert.equal(renderProviderStatusLoading(denied, {
    authContext: statusAuth([PERMISSIONS.PROVIDER_HEALTH_READ]),
    consoleShellState: state,
  }), null);
  assert.equal(denied.innerHTML, "preserved");
});

test("provider status tickets reject late success and errors across filters identity and token", () => {
  const auth = statusAuth();
  const identity = providerStatusIdentitySnapshot(auth);
  const state = createProviderStatusPageState();
  const first = state.begin({
    identity,
    navigation: "provider-health",
    filters: { provider_id: "Provider_A", provider_type: " DEEPSEEK ", limit: "50", offset: 0 },
    token: "token-a",
  });
  assert.ok(first);
  assert.equal(first.filters.provider_id, "Provider_A");
  assert.equal(first.filters.provider_type, "deepseek");
  assert.equal(first.filters.limit, 50);
  assert.equal(state.owns(first, {
    identity,
    navigation: "provider-health",
    filterKey: providerStatusFilterKey(first.filters),
    token: "token-a",
  }), true);

  const second = state.begin({
    identity,
    navigation: "provider-health",
    filters: { provider_id: "Provider_B", limit: 50, offset: 0 },
    token: "token-a",
  });
  assert.equal(first.controller.signal.aborted, true);
  assert.equal(state.owns(first, {
    identity,
    navigation: "provider-health",
    filterKey: providerStatusFilterKey(first.filters),
    token: "token-a",
  }), false);
  assert.equal(state.owns(second, {
    identity,
    navigation: "provider-health",
    filterKey: providerStatusFilterKey({ provider_id: "Provider_B", limit: 50, offset: 0 }),
    token: "token-a",
  }), true);
  assert.equal(state.owns(second, {
    identity,
    navigation: "provider-health",
    filterKey: providerStatusFilterKey({ provider_id: "Provider_B", limit: 50, offset: 0 }),
    token: "token-b",
  }), false);

  state.clear();
  assert.equal(second.controller.signal.aborted, true);
  assert.deepEqual(Object.keys(state.snapshot()).sort(), ["generation", "pending"]);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /token|Provider|tenant|fingerprint/);
});

test("canonical filters preserve case-sensitive ids and do not normalize invalid pagination", () => {
  assert.deepEqual(canonicalProviderStatusFilters({
    provider_id: " Provider_A ",
    provider_type: " DEEPSEEK ",
    health_status: " Healthy ",
    limit: "050",
    offset: "1e2",
    ignored: "tenant-secret",
  }), {
    provider_id: "Provider_A",
    provider_type: "deepseek",
    health_status: "healthy",
    limit: "050",
    offset: "1e2",
  });
  assert.equal(
    providerStatusFilterKey({ provider_type: "deepseek", limit: 50, offset: 0 }),
    providerStatusFilterKey({ offset: "0", limit: "50", provider_type: " DEEPSEEK " }),
  );
  assert.notEqual(
    providerStatusIdentitySnapshot(statusAuth()),
    providerStatusIdentitySnapshot(statusAuth(undefined, { permissionRevision: 8, authorizationFingerprint: "status_fingerprint_8" })),
  );
});

test("status viewed tracker records once per authorized visit and reopens after leave", async () => {
  const calls = [];
  const tracker = createProviderStatusVisitTracker(async (input) => {
    calls.push(input);
    return { accepted: true };
  });
  await tracker.afterRender({ activeItemId: "provider-health", sessionKey: "identity-a", source: "direct_hash" });
  await tracker.afterRender({ activeItemId: "provider-health", sessionKey: "identity-a", source: "sidebar" });
  assert.equal(calls.length, 1);
  await tracker.afterRender({ activeItemId: "overview", sessionKey: "identity-a", source: "sidebar" });
  await tracker.afterRender({ activeItemId: "provider-health", sessionKey: "identity-a", source: "sidebar" });
  assert.equal(calls.length, 2);
  await tracker.afterRender({ activeItemId: "provider-health", sessionKey: "identity-b", source: "direct_hash" });
  assert.equal(calls.length, 3);
});

test("status viewed event sends exact fixed contract without provider or filter data", async () => {
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url: String(url), body: JSON.parse(options.body) };
    return {
      ok: true,
      headers: new Map(),
      async json() { return { accepted: true, event_id: "evt_status", request_id: "req_event", trace_id: "trc_event" }; },
    };
  };
  await recordConsolePageView({
    controlApiUrl: "http://control.local",
    token: "session-token",
    navigationItemId: "provider-health",
    targetSection: "provider-health",
    path: "/#provider-health",
    eventType: "status.viewed",
    payload: { source: "sidebar", hash: "#provider-health", viewport_class: "mobile" },
  });
  assert.equal(captured.url, "http://control.local/console/events");
  assert.deepEqual(captured.body, {
    event_type: "status.viewed",
    navigation_item_id: "provider-health",
    target_section: "provider-health",
    path: "/#provider-health",
    payload: { source: "sidebar", hash: "#provider-health", viewport_class: "mobile" },
  });
  assert.doesNotMatch(JSON.stringify(captured.body), /tenant_id|provider_id|provider_type|filter|token/i);
});

test("responsive CSS freezes four two and one column status metric tracks", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.provider-status-metrics\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)/s);
  assert.match(css, /@media \(max-width: 900px\)[\s\S]*?\.provider-status-metrics\s*\{\s*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(css, /@media \(max-width: 420px\)[\s\S]*?\.provider-status-metrics,[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(css, /\.provider-status-item\s*\{[^}]*max-width:\s*100%/s);
  assert.match(css, /\.provider-status-failure[^}]*overflow-wrap:\s*anywhere/s);
});
