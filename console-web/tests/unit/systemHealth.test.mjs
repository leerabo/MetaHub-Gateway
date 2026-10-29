// 本单测覆盖系统健康面板数据聚合，确保依赖状态和错误信息不会破坏控制台渲染。
import assert from "node:assert/strict";
import test from "node:test";

import { renderSystemStatusPanel } from "../../src/features/system-status/renderSystemStatusPanel.js";
import { getSystemHealth } from "../../src/lib/systemHealth.js";

test("getSystemHealth treats ready=false as an explainable state", async () => {
  globalThis.fetch = async (url) => ({
    ok: !String(url).endsWith("/health/ready"),
    async json() {
      if (String(url).endsWith("/health/ready")) {
        return {
          ready: false,
          trace_id: "trc_ready",
          checks: [{ name: "database", status: "fail" }],
        };
      }
      if (String(url).endsWith("/health/live")) {
        // 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
        return { live: true, trace_id: "trc_live" };
      }
      return { version: "0.1.0-dev", build_commit: "abc123" };
    },
  });

  const health = await getSystemHealth({
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
  });

  assert.equal(health.services.length, 2);
  assert.equal(health.services[0].ready.ready, false);
  assert.equal(health.services[0].ready.checks[0].status, "fail");
});

test("renderSystemStatusPanel displays service readiness and escapes text", () => {
  const html = renderSystemStatusPanel({
    services: [
      {
        name: "<Gateway>",
        state: "ok",
        live: { live: true, trace_id: "trc_live" },
        ready: {
          ready: false,
          trace_id: "trc_ready",
          checks: [{ name: "database", status: "fail" }],
        },
        version: { version: "0.1.0-dev", build_commit: "abc123" },
      },
    ],
  });

  assert.match(html, /暂不可用/);
  assert.match(html, /database/);
  assert.doesNotMatch(html, /trc_ready/);
  assert.doesNotMatch(html, /<Gateway>/);
  assert.match(html, /&lt;Gateway&gt;/);
});

test("getSystemHealth retries one live network failure and keeps attempts traceable", async () => {
  const attempts = new Map();
  const requestIds = [];
  globalThis.fetch = async (url, options) => {
    const key = String(url);
    const count = attempts.get(key) ?? 0;
    attempts.set(key, count + 1);
    requestIds.push(options.headers["X-Request-ID"]);
    if (key === "http://control.local/health/live" && count === 0) {
      // 首次存活探测模拟瞬时网络失败，验证后续尝试仍能被独立追踪。
      throw new Error("transient");
    }
    return {
      ok: true,
      async json() {
        if (key.endsWith("/health/live")) return { live: true };
        if (key.endsWith("/health/ready")) return { ready: true, checks: [] };
        return { version: "test" };
      },
    };
  };
  const health = await getSystemHealth({
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
  });
  assert.equal(health.services[1].state, "ok");
  assert.equal(attempts.get("http://control.local/health/live"), 2);
  assert.equal(new Set(requestIds).size, requestIds.length);
});
