// 本单测覆盖服务状态读取逻辑，确保网关、控制面和控制台健康探测结果可解释。
import assert from "node:assert/strict";
import test from "node:test";

import { getServiceStatuses } from "../../src/lib/serviceStatus.js";

test("getServiceStatuses returns ok states with request ids", async () => {
  globalThis.fetch = async () => ({
    ok: true,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      // 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
      return { status: "ok", request_id: "req_body" };
    },
  });

  const statuses = await getServiceStatuses({
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
  });

  assert.equal(statuses.length, 3);
  assert.equal(statuses[0].state, "ok");
  assert.equal(statuses[0].requestId, "req_body");
  assert.equal(statuses[2].state, "ok");
});

test("getServiceStatuses degrades when services are unavailable", async () => {
  globalThis.fetch = async () => {
    // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    throw new Error("network down");
  };

  const statuses = await getServiceStatuses({
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
  });

  assert.equal(statuses[0].state, "error");
  assert.equal(statuses[1].state, "error");
  assert.equal(statuses[2].state, "ok");
});
