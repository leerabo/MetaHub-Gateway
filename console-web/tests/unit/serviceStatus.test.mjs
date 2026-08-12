// 本单测覆盖服务状态读取逻辑，确保网关、控制面和控制台健康探测结果可解释。
import assert from "node:assert/strict";
import test from "node:test";

import { fetchHealthWithNetworkRetry } from "../../src/lib/healthRequest.js";
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
  let attempts = 0;
  globalThis.fetch = async () => {
    attempts += 1;
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
  assert.equal(attempts, 4);
});

test("getServiceStatuses recovers one transient network failure with a new request id", async () => {
  const requestIds = [];
  let controlAttempts = 0;
  globalThis.fetch = async (url, options) => {
    requestIds.push(options.headers["X-Request-ID"]);
    if (String(url).startsWith("http://control.local") && controlAttempts++ === 0) {
      // 首次控制面探测模拟瞬时网络失败，验证重试使用新的链路标识。
      throw new Error("transient");
    }
    return {
      ok: true,
      headers: new Map(),
      async json() { return { status: "ok", request_id: "req_health" }; },
    };
  };
  const statuses = await getServiceStatuses({
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
  });
  assert.equal(statuses[1].state, "ok");
  assert.equal(new Set(requestIds).size, requestIds.length);
});

test("fetchHealthWithNetworkRetry bounds two hanging attempts with independent abort signals", async () => {
  const requestIds = [];
  const signals = [];
  let timersCleared = 0;
  const pending = fetchHealthWithNetworkRetry(
    "http://control.local/health/live",
    "bounded_health",
    {
      timeoutMs: 1,
      fetchOperation: async (_url, options) => {
        requestIds.push(options.headers["X-Request-ID"]);
        signals.push(options.signal);
        return new Promise(() => {});
      },
      setTimeoutOperation(callback) {
        queueMicrotask(callback);
        return requestIds.length + 1;
      },
      clearTimeoutOperation() {
        timersCleared += 1;
      },
    },
  );

  await assert.rejects(pending, (error) => error?.name === "AbortError");
  assert.equal(requestIds.length, 2);
  assert.equal(new Set(requestIds).size, 2);
  assert.match(requestIds[0], /_attempt_1$/);
  assert.match(requestIds[1], /_attempt_2$/);
  assert.notEqual(signals[0], signals[1]);
  assert.equal(signals.every((signal) => signal.aborted), true);
  assert.equal(timersCleared, 2);
});
