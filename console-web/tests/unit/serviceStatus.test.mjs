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

test("fetchHealthWithNetworkRetry serializes two timed-out attempts with independent abort signals", async () => {
  const requestIds = [];
  const signals = [];
  let timersCleared = 0;
  let active = 0;
  let maxActive = 0;
  const pending = fetchHealthWithNetworkRetry(
    "http://control.local/health/live",
    "bounded_health",
    {
      timeoutMs: 1,
      fetchOperation: async (_url, options) => {
        requestIds.push(options.headers["X-Request-ID"]);
        signals.push(options.signal);
        active += 1;
        maxActive = Math.max(maxActive, active);
        return new Promise((_, reject) => {
          options.signal.addEventListener("abort", () => {
            setTimeout(() => {
              active -= 1;
              reject(new DOMException("aborted", "AbortError"));
            }, 5);
          }, { once: true });
        });
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
  assert.equal(maxActive, 1);
  assert.equal(active, 0);
});

test("fetchHealthWithNetworkRetry keeps a late success on attempt one and does not retry", async () => {
  const requestIds = [];
  const signals = [];
  let timersCleared = 0;
  let resolveFetch;
  const result = fetchHealthWithNetworkRetry(
    "http://control.local/health/ready",
    "late_health",
    {
      timeoutMs: 1,
      fetchOperation: async (_url, options) => {
        requestIds.push(options.headers["X-Request-ID"]);
        signals.push(options.signal);
        return new Promise((resolve) => { resolveFetch = resolve; });
      },
      setTimeoutOperation(callback) {
        queueMicrotask(() => {
          callback();
          queueMicrotask(() => resolveFetch({ ok: true }));
        });
        return 1;
      },
      clearTimeoutOperation() { timersCleared += 1; },
    },
  );

  assert.deepEqual(await result, { ok: true });
  assert.equal(requestIds.length, 1);
  assert.match(requestIds[0], /_attempt_1$/);
  assert.equal(signals[0].aborted, true);
  assert.equal(timersCleared, 1);
});

test("fetchHealthWithNetworkRetry clears the timer after an immediate success", async () => {
  let timerCallback;
  let timersCleared = 0;
  let signal;
  const response = await fetchHealthWithNetworkRetry(
    "http://control.local/version",
    "healthy",
    {
      fetchOperation: async (_url, options) => {
        signal = options.signal;
        return { ok: true };
      },
      setTimeoutOperation(callback) {
        timerCallback = callback;
        return 9;
      },
      clearTimeoutOperation(handle) {
        assert.equal(handle, 9);
        timersCleared += 1;
        timerCallback = null;
      },
    },
  );

  assert.deepEqual(response, { ok: true });
  assert.equal(timersCleared, 1);
  assert.equal(timerCallback, null);
  assert.equal(signal.aborted, false);
});
