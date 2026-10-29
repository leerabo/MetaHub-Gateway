/**
 * 验证 SDK 异常分类、请求标识提取优先级和固定错误行不会泄露原始异常内容。
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  APIConnectionError,
  APIConnectionTimeoutError,
  APIError,
} from "openai";

import {
  ClientError,
  StreamError,
  extractRequestId,
  formatError,
  safeToken,
  toSafeFailure,
} from "../src/errors.js";

test("请求标识按 requestID、request_id、header、handshake 顺序提取", () => {
  assert.equal(
    extractRequestId(
      {
        requestID: "req-public",
        request_id: "req-compatible",
        headers: new Headers({ "x-request-id": "req-header" }),
      },
      "req-handshake",
    ),
    "req-public",
  );
  assert.equal(extractRequestId({ request_id: "req-compatible" }), "req-compatible");
  assert.equal(
    extractRequestId({ headers: new Headers({ "x-request-id": "req-header" }) }),
    "req-header",
  );
  assert.equal(extractRequestId({}, "req-handshake"), "req-handshake");
});

test("不安全或超长标识统一退化为占位符", () => {
  assert.equal(safeToken("safe.id:1"), "safe.id:1");
  assert.equal(safeToken("bad\nAuthorization: Bearer secret"), "-");
  assert.equal(safeToken("x".repeat(129)), "-");
  assert.equal(extractRequestId({ requestID: "bad\r\nvalue" }), "-");
});

test("APIError 仅保留公开安全 code、状态和 requestID", () => {
  const error = new APIError(
    429,
    { code: "gateway.rate_limited" },
    "secret prompt and Authorization must not leak",
    new Headers({ "x-request-id": "req-api" }),
  );
  const output = formatError(error);

  assert.equal(
    output,
    "error_code=gateway.rate_limited status=429 request_id=req-api retryable=true",
  );
  assert.doesNotMatch(output, /secret|prompt|Authorization/i);
});

test("非法或缺失 API code 安全退化为 client.api_error", () => {
  const error = new APIError(
    400,
    { code: "bad\ncode" },
    "raw upstream body",
    new Headers({ "x-request-id": "req-safe" }),
  );

  assert.equal(
    formatError(error),
    "error_code=client.api_error status=400 request_id=req-safe retryable=false",
  );
});

test("连接、SDK 超时和未知异常使用固定安全类别", () => {
  const connection = new APIConnectionError({ message: "secret endpoint" });
  const timeout = new APIConnectionTimeoutError({ message: "secret timeout" });
  const unknown = new Error("virtual-test-secret prompt body");

  assert.equal(toSafeFailure(connection).code, "client.connection_failed");
  assert.equal(toSafeFailure(connection).retryable, true);
  assert.equal(toSafeFailure(timeout).code, "client.timeout");
  assert.equal(toSafeFailure(timeout).retryable, true);
  assert.equal(
    formatError(unknown),
    "error_code=client.unexpected_error status=- request_id=- retryable=false",
  );
});

test("流迭代异常优先使用异常 requestID，否则回退握手 request_id", () => {
  const originalWithId = new APIConnectionError({ message: "hidden" });
  originalWithId.requestID = "req-error";
  const originalWithoutId = new APIConnectionError({ message: "hidden" });

  assert.equal(
    toSafeFailure(new StreamError(originalWithId, "req-handshake")).requestId,
    "req-error",
  );
  assert.equal(
    toSafeFailure(new StreamError(originalWithoutId, "req-handshake")).requestId,
    "req-handshake",
  );
});

test("本地错误只输出固定 code 和显式可重试属性", () => {
  assert.equal(
    formatError(
      new ClientError("client.stream_timeout", {
        retryable: true,
        requestId: "req-stream",
      }),
    ),
    "error_code=client.stream_timeout status=- request_id=req-stream retryable=true",
  );
});
