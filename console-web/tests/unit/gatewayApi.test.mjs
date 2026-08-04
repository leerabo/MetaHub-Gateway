// 本单测覆盖网关调试请求封装，确保 OpenAI-compatible 请求、增量 SSE、取消和有界预览保持稳定。
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  GatewayApiError,
  sendChatCompletion,
  STREAM_PREVIEW_MAX_BYTES,
} from "../../src/lib/gatewayApi.js";

// 保存 Node 原生 fetch，避免单测桩污染同进程内其他测试文件。
const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

/**
 * 创建只暴露 body.getReader() 的响应桩；text() 一旦被调用就抛错，防止实现退回整包缓冲。
 */
function createStreamResponse(chunks, { requestId = "req_stream", traceId = "trc_stream" } = {}) {
  let index = 0;
  let released = false;
  let cancelled = false;
  const response = {
    ok: true,
    status: 200,
    headers: new Headers({
      "X-Request-ID": requestId,
      "X-Trace-ID": traceId,
    }),
    body: {
      getReader() {
        return {
          async read() {
            // 每次 read 只返回一个传输块，测试可据此断言 onStreamUpdate 的真实增量调用次数。
            if (index >= chunks.length) {
              return { done: true, value: undefined };
            }
            const value = chunks[index];
            index += 1;
            return { done: false, value };
          },
          async cancel() {
            cancelled = true;
          },
          releaseLock() {
            released = true;
          },
        };
      },
    },
    async text() {
      // 触发此分支表示实现退化为整包缓冲，测试必须立即失败并指出原因。
      throw new Error("流式实现不得调用 response.text()");
    },
  };
  return {
    response,
    state: {
      released: () => released,
      cancelled: () => cancelled,
    },
  };
}

test("sendChatCompletion sends OpenAI compatible JSON request", async () => {
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return {
      ok: true,
      headers: new Headers({
        "X-Request-ID": "req_chat",
        "X-Trace-ID": "trc_chat",
      }),
      async json() {
        // 非流式夹具只返回可审计 ID 和用量，不包含认证字段或用户输入明文。
        return { id: "chatcmpl_1", usage: { prompt_tokens: 1, completion_tokens: 2 } };
      },
    };
  };

  const controller = new AbortController();
  const result = await sendChatCompletion({
    gatewayUrl: "http://gateway.local/",
    virtualKey: "mhv_test_key",
    model: "default-chat",
    message: "hello",
    stream: false,
    signal: controller.signal,
  });

  assert.equal(captured.url, "http://gateway.local/v1/chat/completions");
  assert.equal(captured.options.headers.Authorization, "Bearer mhv_test_key");
  assert.equal(captured.options.signal, controller.signal);
  assert.deepEqual(JSON.parse(captured.options.body).messages, [{ role: "user", content: "hello" }]);
  assert.equal(result.mode, "json");
  assert.equal(result.requestId, "req_chat");
  assert.match(result.preview, /chatcmpl_1/);
});

test("sendChatCompletion incrementally decodes UTF-8 chunks and reports live counters", async () => {
  const encoder = new TextEncoder();
  const source = 'data: {"delta":"你好"}\n\ndata: [DONE]\n\n';
  const encoded = encoder.encode(source);
  const firstChineseByte = encoder.encode('data: {"delta":"').byteLength;
  const chunks = [
    encoded.slice(0, firstChineseByte + 1),
    encoded.slice(firstChineseByte + 1, firstChineseByte + 4),
    encoded.slice(firstChineseByte + 4),
  ];
  const stream = createStreamResponse(chunks);
  globalThis.fetch = async () => stream.response;
  const updates = [];

  const result = await sendChatCompletion({
    gatewayUrl: "http://gateway.local",
    virtualKey: "mhv_test_key",
    model: "default-chat",
    message: "hello",
    stream: true,
    onStreamUpdate: (snapshot) => updates.push(snapshot),
  });

  assert.equal(updates.length, chunks.length);
  assert.deepEqual(updates.map((item) => item.chunkCount), [1, 2, 3]);
  assert.equal(result.mode, "stream");
  assert.equal(result.status, "completed");
  assert.equal(result.traceId, "trc_stream");
  assert.equal(result.byteCount, encoded.byteLength);
  assert.match(result.preview, /你好/);
  assert.match(result.preview, /data: \[DONE\]/);
  assert.equal(stream.state.released(), true);
  assert.equal(stream.state.cancelled(), false);
});

test("sendChatCompletion parses a standard SSE error and preserves received preview", async () => {
  const encoder = new TextEncoder();
  const source = [
    'data: {"delta":"partial"}\n\n',
    "event:error\n",
    'data: {"error":{"code":"gateway.upstream_stream_timeout","message":"Upstream stream timed out.","request_id":"req_error","trace_id":"trc_error"}}\n\n',
  ].join("");
  const encoded = encoder.encode(source);
  const stream = createStreamResponse([
    encoded.slice(0, 19),
    encoded.slice(19, 47),
    encoded.slice(47),
  ]);
  globalThis.fetch = async () => stream.response;
  const updates = [];

  await assert.rejects(
    () => sendChatCompletion({
      gatewayUrl: "http://gateway.local",
      virtualKey: "mhv_test_key",
      model: "default-chat",
      message: "hello",
      stream: true,
      onStreamUpdate: (snapshot) => updates.push(snapshot),
    }),
    (error) => {
      assert.ok(error instanceof GatewayApiError);
      assert.equal(error.code, "gateway.upstream_stream_timeout");
      assert.equal(error.requestId, "req_error");
      assert.equal(error.traceId, "trc_error");
      assert.match(error.preview, /partial/);
      assert.match(error.preview, /event:error/);
      assert.equal(error.byteCount, encoded.byteLength);
      return true;
    },
  );

  assert.equal(updates.at(-1).status, "failed");
  assert.equal(stream.state.cancelled(), true);
  assert.equal(stream.state.released(), true);
});

test("sendChatCompletion keeps only the latest 256 KiB preview while continuing reads", async () => {
  const encoder = new TextEncoder();
  const oldContent = `old-prefix-${"a".repeat(STREAM_PREVIEW_MAX_BYTES)}`;
  const latestContent = "-latest-tail";
  const chunks = [encoder.encode(oldContent), encoder.encode(latestContent)];
  const stream = createStreamResponse(chunks);
  globalThis.fetch = async () => stream.response;
  const updates = [];

  const result = await sendChatCompletion({
    gatewayUrl: "http://gateway.local",
    virtualKey: "mhv_test_key",
    model: "default-chat",
    message: "hello",
    stream: true,
    onStreamUpdate: (snapshot) => updates.push(snapshot),
  });

  assert.equal(result.previewTruncated, true);
  assert.ok(encoder.encode(result.preview).byteLength <= STREAM_PREVIEW_MAX_BYTES);
  assert.doesNotMatch(result.preview, /old-prefix/);
  assert.match(result.preview, /latest-tail$/);
  assert.equal(result.byteCount, chunks[0].byteLength + chunks[1].byteLength);
  assert.equal(updates.length, 2);
});

test("sendChatCompletion propagates AbortSignal cancellation as AbortError", async () => {
  const controller = new AbortController();
  let capturedSignal;
  let released = false;
  globalThis.fetch = async (_url, options) => {
    capturedSignal = options.signal;
    return {
      ok: true,
      status: 200,
      headers: new Headers(),
      body: {
        getReader() {
          return {
            read() {
              return new Promise((_resolve, reject) => {
                options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
              });
            },
            async cancel() {},
            releaseLock() {
              released = true;
            },
          };
        },
      },
    };
  };

  const pending = sendChatCompletion({
    gatewayUrl: "http://gateway.local",
    virtualKey: "mhv_test_key",
    model: "default-chat",
    message: "hello",
    stream: true,
    signal: controller.signal,
  });
  await Promise.resolve();
  controller.abort();

  await assert.rejects(pending, (error) => error?.name === "AbortError");
  assert.equal(capturedSignal, controller.signal);
  assert.equal(released, true);
});

test("sendChatCompletion maps unified gateway errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: new Headers(),
    async json() {
      return {
        code: "gateway.model_missing",
        message: "Model is required.",
        request_id: "req_error",
        trace_id: "trc_error",
        suggestion: "Pass a model alias in the request body.",
        diagnosis: {
          rule_id: "gateway_request_invalid",
          category: "request_validation",
          reason: "缺少模型",
          suggestion: "补充 model",
          action: "修正请求体",
          severity: "error",
          retryable: false,
        },
      };
    },
  });

  await assert.rejects(
    () => sendChatCompletion({
      gatewayUrl: "http://gateway.local",
      virtualKey: "mhv_test_key",
      model: "",
      message: "hello",
      stream: false,
    }),
    (error) => {
      assert.ok(error instanceof GatewayApiError);
      assert.equal(error.code, "gateway.model_missing");
      assert.equal(error.requestId, "req_error");
      assert.equal(error.traceId, "trc_error");
      assert.equal(error.diagnosis.rule_id, "gateway_request_invalid");
      return true;
    },
  );
});
