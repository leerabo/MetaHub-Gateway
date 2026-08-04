// 本单测覆盖 PRD-025/042 Webhook 前端能力，确保基础订阅、详情筛选、重放、轮换和一次性 Secret 符合权限与脱敏边界。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderWebhookPanel } from "../../src/features/webhooks/renderWebhookPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createWebhook,
  listWebhookDeliveries,
  listWebhooks,
  replayWebhook,
  rotateWebhookSecret,
  testWebhook,
} from "../../src/lib/webhooks.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const mainSource = readFileSync(new URL("../../src/app/main.js", import.meta.url), "utf8");

const webhookPayload = {
  webhooks: [
    {
      webhook_endpoint_id: "wh_1",
      tenant_id: "tenant_1",
      name: "运维告警系统",
      endpoint_url: "https://ops.example.com/metahub/webhook",
      event_types: ["budget.threshold_reached", "security.dlp_matched", "gateway.request_failed"],
      secret_prefix: "whsec_abcd",
      secret_fingerprint: "fingerprint_only",
      secret_version: 1,
      status: "active",
      timeout_ms: 5000,
      max_attempts: 3,
      last_delivery_status: "success",
      last_delivery_at: "2026-07-10T08:00:00Z",
      last_success_at: "2026-07-10T08:00:00Z",
      last_failure_at: null,
      created_at: "2026-07-10T07:00:00Z",
      updated_at: "2026-07-10T08:00:00Z",
      recent_deliveries: [
        {
          webhook_delivery_id: "whd_1",
          tenant_id: "tenant_1",
          webhook_endpoint_id: "wh_1",
          event_id: "evt_1",
          event_type: "webhook.test",
          event_timestamp: "2026-07-10T08:00:00Z",
          payload: { event_id: "evt_1", tenant_id: "tenant_1", payload: { source: "control-plane-api" } },
          status: "success",
          attempt_count: 1,
          max_attempts: 3,
          http_status: 204,
          duration_ms: 12,
          error_code: null,
          error_summary: null,
          attempts: [
            {
              webhook_delivery_attempt_id: "whda_1",
              status: "success",
              http_status: 204,
              error_code: null,
            },
          ],
          created_at: "2026-07-10T08:00:00Z",
          updated_at: "2026-07-10T08:00:00Z",
        },
      ],
    },
  ],
  event_types: [
    "webhook.test",
    "budget.threshold_reached",
    "budget.exhausted",
    "security.dlp_matched",
    "gateway.request_failed",
  ],
  request_id: "req_webhooks",
  trace_id: "trc_webhooks",
};

test("webhook client sends list create test detail replay and rotate requests without tenant_id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_webhook"]]),
      async json() {
        return webhookPayload;
      },
    };
  };

  const listed = await listWebhooks({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    status: "all",
    eventType: "gateway.request_failed",
  });
  await createWebhook({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    name: "运维告警系统",
    endpointUrl: "https://ops.example.com/metahub/webhook",
    eventTypes: ["gateway.request_failed", "security.dlp_matched"],
    status: "active",
    timeoutMs: 5000,
  });
  await testWebhook({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    webhookEndpointId: "wh_1",
  });
  await listWebhookDeliveries({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    webhookEndpointId: "wh_1",
    eventType: "gateway.request_failed",
    status: "failed",
    limit: 100,
  });
  await replayWebhook({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    webhookEndpointId: "wh_1",
    webhookDeliveryId: "whd_failed_1",
    idempotencyKey: "console_test_same_key",
  });
  await rotateWebhookSecret({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    webhookEndpointId: "wh_1",
  });

  assert.equal(listed.state, "ok");
  assert.match(calls[0].url, /status=all/);
  assert.match(calls[0].url, /event_type=gateway\.request_failed/);
  assert.match(calls[0].url, /include_deliveries=true/);
  assert.doesNotMatch(calls[0].url, /tenant_id=/);
  assert.equal(calls[1].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    name: "运维告警系统",
    endpoint_url: "https://ops.example.com/metahub/webhook",
    event_types: ["gateway.request_failed", "security.dlp_matched"],
    status: "active",
    timeout_ms: 5000,
  });
  assert.equal(calls[2].options.method, "POST");
  assert.match(calls[2].url, /\/webhooks\/wh_1\/test$/);
  assert.match(calls[3].url, /\/webhooks\/wh_1\/deliveries/);
  assert.match(calls[3].url, /event_type=gateway\.request_failed/);
  assert.match(calls[3].url, /status=failed/);
  assert.match(calls[3].url, /limit=100/);
  assert.doesNotMatch(calls[3].url, /tenant_id=/);
  assert.deepEqual(JSON.parse(calls[4].options.body), {
    webhook_delivery_id: "whd_failed_1",
    idempotency_key: "console_test_same_key",
  });
  assert.doesNotMatch(calls[4].options.body, /tenant_id|signing_secret|payload/);
  assert.match(calls[4].url, /\/webhooks\/wh_1\/replay$/);
  assert.match(calls[5].url, /\/webhooks\/wh_1\/rotate-secret$/);
  assert.equal(calls[5].options.body, undefined);
});

test("webhook client maps unified errors without leaking secret", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "webhook.endpoint_invalid",
        message: "Endpoint URL is invalid.",
        request_id: "req_invalid_webhook",
        trace_id: "trc_invalid_webhook",
      };
    },
  });

  await assert.rejects(
    () =>
      createWebhook({
        controlApiUrl: "http://control.local",
        token: "jwt-token",
        name: "bad",
        endpointUrl: "ftp://example.com/hook",
        eventTypes: ["gateway.request_failed"],
      }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.code, "webhook.endpoint_invalid");
      assert.equal(error.requestId, "req_invalid_webhook");
      assert.doesNotMatch(error.message, /whsec_/);
      return true;
    },
  );
});

test("renderWebhookPanel shows endpoint records and one-time secret only when created", () => {
  const html = renderWebhookPanel(
    {
      state: "ok",
      webhooks: webhookPayload,
      created: {
        signingSecret: "whsec_visible_once",
        webhookEndpointId: "wh_1",
        reason: "rotated",
      },
      selectedEndpointId: "wh_1",
      deliveryFilters: { eventType: "webhook.test", status: "failed" },
      deliveryDetails: {
        deliveries: [
          {
            ...webhookPayload.webhooks[0].recent_deliveries[0],
            webhook_delivery_id: "whd_failed_1",
            status: "failed",
            error_code: "webhook.timeout",
            error_summary: "Receiver timed out.",
            delivery_kind: "replay",
            secret_version: 2,
          },
        ],
      },
      feedback: { message: "Webhook Endpoint 已保存", requestId: "req_prd042_success" },
    },
    withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.WEBHOOK_MANAGE],
    ),
  );

  assert.match(html, /webhook-form/);
  assert.match(html, /运维告警系统/);
  assert.match(html, /发送记录/);
  assert.match(html, /fingerprint_only/);
  assert.match(html, /whsec_visible_once/);
  assert.match(html, /新签名密钥仅显示一次/);
  assert.match(html, /data-webhook-replay="whd_failed_1"/);
  assert.match(html, /data-webhook-rotate="wh_1"/);
  assert.match(html, /webhook-delivery-filters/);
  assert.match(html, /Secret v2/);
  assert.match(html, /Webhook Endpoint 已保存/);
  assert.match(html, /request_id: req_prd042_success/);
  assert.match(html, /role="status"/);
  assert.match(html, /data-label="操作"/);
  assert.match(html, /data-webhook-refresh-details="wh_1"/);
  assert.doesNotMatch(html, /encrypted_secret/);
  assert.doesNotMatch(html, /payload_json|idempotency_key/);
});

test("renderWebhookPanel only offers replay for failed records and caps rendered details at one hundred", () => {
  const deliveries = Array.from({ length: 101 }, (_, index) => ({
    ...webhookPayload.webhooks[0].recent_deliveries[0],
    webhook_delivery_id: `whd_${index}`,
    status: index === 0 ? "success" : "failed",
    delivery_kind: "original",
    secret_version: 1,
  }));
  const html = renderWebhookPanel(
    {
      state: "ok",
      webhooks: webhookPayload,
      selectedEndpointId: "wh_1",
      deliveryDetails: { deliveries },
      deliveryFilters: { eventType: "", status: "all" },
    },
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.WEBHOOK_MANAGE],
    ),
  );

  assert.doesNotMatch(html, /data-webhook-replay="whd_0"/);
  assert.match(html, /data-webhook-replay="whd_1"/);
  assert.match(html, /data-webhook-replay="whd_99"/);
  assert.doesNotMatch(html, /data-webhook-replay="whd_100"/);
});

test("renderWebhookPanel hides for viewer and escapes endpoint content", () => {
  const viewerHtml = renderWebhookPanel(
    { state: "ok", webhooks: webhookPayload },
    withPermissionSnapshot({ user: { role: "viewer" } }, []),
  );
  const developerHtml = renderWebhookPanel(
    {
      state: "ok",
      webhooks: {
        ...webhookPayload,
        webhooks: [
          {
            ...webhookPayload.webhooks[0],
            name: "告警 <script>",
            endpoint_url: "https://ops.example.com/<hook>",
          },
        ],
      },
    },
    withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.WEBHOOK_MANAGE],
    ),
  );

  assert.equal(viewerHtml, "");
  assert.match(developerHtml, /告警 &lt;script&gt;/);
  assert.match(developerHtml, /https:\/\/ops.example.com\/&lt;hook&gt;/);
});

test("renderDeveloperStart renders webhooks active navigation content", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: withPermissionSnapshot({
      user: { display_name: "Developer", email: "dev@example.com", role: "developer", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-10T12:00:00Z" },
    }, [PERMISSIONS.WEBHOOK_MANAGE]),
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "webhooks",
        navigation_items: [
          {
            id: "webhooks",
            label: "Webhook 设置",
            group: "安全与治理",
            target_section: "webhooks",
            description: "Webhook",
            empty_state_hint: "Add webhook",
            order: 89,
          },
        ],
      },
    },
    webhookState: {
      state: "ok",
      webhooks: webhookPayload,
    },
  });

  assert.match(container.innerHTML, /webhooks-panel/);
  assert.match(container.innerHTML, /Webhook 设置/);
  assert.match(container.innerHTML, /运维告警系统/);
});

test("periodic refresh waits for interactive refresh so webhook feedback can render", () => {
  // 一次完整控制台读取可能超过 15 秒；定时器若继续抢占 refreshSequence，会让成功重放和轮换反馈永久不落 DOM。
  assert.match(mainSource, /let activeRefreshCount = 0;/);
  assert.match(mainSource, /activeRefreshCount \+= 1;[\s\S]*finally \{[\s\S]*activeRefreshCount -= 1;/);
  assert.match(mainSource, /window\.setInterval\(\(\) => \{[\s\S]*if \(activeRefreshCount > 0\) \{[\s\S]*return;/);
});

test("identity changes clear webhook secret and prior request feedback together", () => {
  // request_id 虽不是密钥，仍属于上一租户操作上下文，不能在登出后出现在下一位用户页面。
  assert.match(
    mainSource,
    /function clearWebhookSessionState\(\) \{[\s\S]*webhookActionError = null;[\s\S]*webhookFeedback = "";[\s\S]*webhookCreatedSecret = null;/,
  );
});
