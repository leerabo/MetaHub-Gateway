// PRD-035 前端单测覆盖健康 API、脱敏渲染、角色边界和探测请求体。
import assert from "node:assert/strict";
import test from "node:test";

import {
  renderProviderHealthDashboardCard,
  renderProviderHealthPanel,
} from "../../src/features/provider-health/renderProviderHealthPanel.js";
import { listProviderHealth, probeProvider } from "../../src/lib/providerHealth.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const healthPayload = {
  provider_health: [
    {
      provider_id: "provider_health_1",
      tenant_id: "tenant_1",
      provider_code: "deepseek_official",
      name: "<Health Provider>",
      provider_type: "deepseek",
      base_url: "https://api.example.com/v1?secret=removed",
      provider_status: "active",
      health_status: "unhealthy",
      effective_health_status: "unhealthy",
      is_health_stale: false,
      last_success_at: null,
      last_probe_latency_ms: 120,
      last_probe_error_code: "authentication_failed",
    },
  ],
  summary: { total: 1, active: 1, healthy: 0, unhealthy: 1, observing: 0, unknown: 0, disabled: 0 },
  request_id: "req_health",
  trace_id: "trc_health",
};

test("provider health client filters by query and sends an empty probe body", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map(),
      async json() {
        return String(url).endsWith("/probe")
          ? { provider_health: healthPayload.provider_health[0], result_status: "failed", attempts: [] }
          : healthPayload;
      },
    };
  };

  await listProviderHealth({
    controlApiUrl: "http://control",
    token: "session-token",
    filters: { health_status: "unhealthy", limit: 5 },
  });
  await probeProvider({
    controlApiUrl: "http://control",
    token: "session-token",
    providerId: "provider_health_1",
  });

  assert.match(calls[0].url, /health_status=unhealthy/);
  assert.match(calls[0].url, /limit=5/);
  assert.equal(calls[1].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[1].options.body), {});
  assert.equal(calls[1].options.headers.Authorization, "Bearer session-token");
  assert.doesNotMatch(calls[1].options.body, /tenant|url|token/i);
});

test("provider health panel escapes data and viewer renders nothing", () => {
  const state = { state: "ok", ...healthPayload };
  const adminHtml = renderProviderHealthPanel(
    state,
    withPermissionSnapshot(
      { user: { role: "ops_admin" } },
      [PERMISSIONS.PROVIDER_HEALTH_PROBE, PERMISSIONS.PROVIDER_HEALTH_READ],
    ),
  );
  const viewerHtml = renderProviderHealthPanel(
    state,
    withPermissionSnapshot({ user: { role: "viewer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  );
  const dashboardHtml = renderProviderHealthDashboardCard(
    state,
    withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ],
    ),
    {
      navigation: {
        navigation_items: [
          { id: "provider-health", target_section: "provider-health", visible: true, disabled: false },
        ],
      },
    },
  );

  assert.match(adminHtml, /Provider&gt;/);
  assert.doesNotMatch(adminHtml, /<Health Provider>/);
  assert.match(adminHtml, /authentication_failed/);
  assert.match(adminHtml, /data-provider-probe="provider_health_1"/);
  assert.equal(viewerHtml, "");
  assert.match(dashboardHtml, /异常 1/);
  assert.match(dashboardHtml, /#provider-health/);
});

test("provider health dashboard hides PAGE-088 link without logs permission or navigation owner", () => {
  const healthOnly = withPermissionSnapshot(
    { user: { role: "developer" } },
    [PERMISSIONS.PROVIDER_HEALTH_READ],
  );
  const noLogsLink = renderProviderHealthDashboardCard(
    { state: "ok", ...healthPayload },
    healthOnly,
    { navigation: { navigation_items: [{ id: "provider-health", target_section: "provider-health" }] } },
  );
  const noNavigationLink = renderProviderHealthDashboardCard(
    { state: "ok", ...healthPayload },
    withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ],
    ),
    { navigation: { navigation_items: [] } },
  );
  assert.match(noLogsLink, /Provider 健康/);
  assert.doesNotMatch(noLogsLink, /#provider-health/);
  assert.doesNotMatch(noNavigationLink, /#provider-health/);
});
