import assert from "node:assert/strict";
import test from "node:test";

import { renderAdminOnboarding } from "../../src/features/admin-onboarding/renderAdminOnboarding.js";
import { getTenantOnboardingSummary } from "../../src/lib/onboarding.js";

test("admin onboarding emphasizes one backend-selected next action", () => {
  const html = renderAdminOnboarding({
    state: "ok",
    completed_steps: 2,
    total_steps: 8,
    is_complete: false,
    steps: [
      { id: "tenant-profile", status: "complete", title: "企业资料", target_hash: "#tenant" },
      {
        id: "provider-keypool",
        status: "current",
        title: "接入模型服务",
        description: "配置安全 KeyPool。",
        target_hash: "#providers",
        action_label: "配置模型服务",
      },
      {
        id: "model-alias",
        status: "blocked",
        title: "准备模型别名",
        target_hash: "#model-aliases",
        blocking_reason: "先完成模型服务接入。",
      },
    ],
  });

  assert.match(html, /已完成 2\/8 项/);
  assert.match(html, /data-console-nav="providers"/);
  assert.equal((html.match(/admin-onboarding-action/g) ?? []).length, 1);
  assert.doesNotMatch(html, /request_id|Token|Provider Key/);
});

test("admin onboarding rejects an unsafe backend navigation target", () => {
  const html = renderAdminOnboarding({
    state: "ok",
    completed_steps: 0,
    total_steps: 1,
    is_complete: false,
    steps: [{
      id: "tenant-profile",
      status: "current",
      title: "企业资料",
      description: "准备企业资料。",
      target_hash: "#providers\" autofocus",
      action_label: "继续",
    }],
  });
  assert.doesNotMatch(html, /data-console-nav/);
  assert.doesNotMatch(html, /autofocus/);
});

test("onboarding API sends only bearer auth and request correlation", async () => {
  const originalFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({
      tenant_id: "tenant_default",
      completed_steps: 0,
      total_steps: 8,
      is_complete: false,
      steps: [],
      request_id: "req_safe",
      trace_id: "trace_safe",
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    const result = await getTenantOnboardingSummary({
      controlApiUrl: "http://127.0.0.1:8001",
      token: "test-session-token",
    });
    assert.equal(result.tenant_id, "tenant_default");
    assert.equal(captured.url, "http://127.0.0.1:8001/tenant/onboarding/summary");
    assert.equal(captured.options.headers.Authorization, "Bearer test-session-token");
    assert.doesNotMatch(captured.url, /tenant_default|token/i);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
