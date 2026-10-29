/**
 * 验证 withResponse 流式协议、逐段输出、握手 ID、断流回退和总时限 abort。
 */

import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";

import { createOpenAIClient } from "../src/client.js";
import { loadConfig } from "../src/config.js";
import { main, runStream } from "../src/stream.js";

/**
 * 创建内存文本写入器，用于精确验证流片段、换行和固定错误输出。
 */
function createCapture() {
  let value = "";
  return {
    write(chunk) {
      value += String(chunk);
    },
    text() {
      return value;
    },
  };
}

/**
 * 在回环地址启动临时 HTTP/SSE 服务，并提供可关闭所有连接的清理函数。
 */
async function startServer(handler) {
  const server = createServer(handler);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  return {
    baseUrl: `http://127.0.0.1:${address.port}/v1`,
    async close() {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

/**
 * 把 OpenAI-compatible JSON 对象编码为本地协议测试使用的单个 SSE data 事件。
 */
function sseEvent(data) {
  return `data: ${JSON.stringify(data)}\n\n`;
}

/**
 * 创建包含文本 delta 的最小 Chat Completion Chunk。
 */
function textChunk(content) {
  return {
    id: "chatcmpl-stream-local",
    object: "chat.completion.chunk",
    created: 1,
    model: "integration-chat",
    choices: [
      {
        index: 0,
        delta: { content },
        finish_reason: null,
      },
    ],
  };
}

/**
 * 读取请求正文，确保 HTTP 服务在返回 SSE 前已经消费完整请求。
 */
async function consumeRequest(request) {
  for await (const _chunk of request) {
    // 请求正文内容不写入日志，服务只需要等待上传结束。
  }
}

/**
 * 构造真实 SDK 流式测试配置，默认关闭重试避免中途断流被重放。
 */
function integrationConfig(baseUrl, timeoutSeconds = "1") {
  return loadConfig({
    METAHUB_BASE_URL: baseUrl,
    METAHUB_VIRTUAL_KEY: "virtual-stream-secret",
    METAHUB_CHAT_MODEL: "integration-chat",
    METAHUB_TIMEOUT_SECONDS: timeoutSeconds,
    METAHUB_MAX_RETRIES: "0",
  });
}

test("fake client 通过 withResponse 顺序输出文本并传递 AbortSignal", async () => {
  let receivedRequest;
  let receivedOptions;
  let withResponseCalled = false;
  const client = {
    chat: {
      completions: {
        create(request, options) {
          receivedRequest = request;
          receivedOptions = options;
          return {
            async withResponse() {
              withResponseCalled = true;
              return {
                request_id: "req-fake-stream",
                data: (async function* () {
                  yield { choices: [] };
                  yield { choices: [{ delta: { content: "A" } }] };
                  yield { choices: [{ delta: { tool_calls: [] } }] };
                  yield { choices: [{ delta: { content: "B" } }] };
                  yield { choices: [{ delta: {}, finish_reason: "stop" }] };
                })(),
              };
            },
          };
        },
      },
    },
  };
  const stdout = createCapture();

  await runStream({
    client,
    config: {
      chatModel: "fake-model",
      timeoutMilliseconds: 1_000,
    },
    prompt: "fake stream prompt",
    stdout,
  });

  assert.equal(withResponseCalled, true);
  assert.equal(receivedRequest.stream, true);
  assert.equal(receivedRequest.model, "fake-model");
  assert.ok(receivedOptions.signal instanceof AbortSignal);
  assert.equal(stdout.text(), "AB\nrequest_id=req-fake-stream\n");
  assert.doesNotMatch(stdout.text(), /fake stream prompt/);
});

test("stream main 支持可注入 run 函数和输出流", async () => {
  const stdout = createCapture();
  const stderr = createCapture();
  let runArguments;

  const exitCode = await main({
    argv: ["--prompt", "injected stream prompt"],
    environ: { METAHUB_VIRTUAL_KEY: "virtual-injected-secret" },
    clientFactory() {
      return { marker: "fake-client" };
    },
    async run(arguments_) {
      runArguments = arguments_;
      arguments_.stdout.write("injected stream\nrequest_id=req-injected-stream\n");
    },
    stdout,
    stderr,
  });

  assert.equal(exitCode, 0);
  assert.equal(runArguments.client.marker, "fake-client");
  assert.equal(runArguments.prompt, "injected stream prompt");
  assert.equal(stdout.text(), "injected stream\nrequest_id=req-injected-stream\n");
  assert.equal(stderr.text(), "");
});

test("真实 openai 客户端消费本地 SSE 并从握手头取得 request_id", async () => {
  let receivedBody = "";
  const fixture = await startServer(async (request, response) => {
    request.setEncoding("utf8");
    for await (const chunk of request) {
      receivedBody += chunk;
    }
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      "x-request-id": "req-sse-success",
      connection: "close",
    });
    response.write(sseEvent({ choices: [] }));
    response.write(sseEvent(textChunk("流式")));
    response.write(sseEvent({ usage: { total_tokens: 3 }, choices: [] }));
    response.write(sseEvent(textChunk("成功")));
    response.write(
      sseEvent({
        ...textChunk(""),
        choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
      }),
    );
    response.end("data: [DONE]\n\n");
  });

  try {
    const config = integrationConfig(fixture.baseUrl);
    const stdout = createCapture();
    await runStream({
      client: createOpenAIClient(config),
      config,
      prompt: "local SSE prompt",
      stdout,
    });

    const parsedBody = JSON.parse(receivedBody);
    assert.equal(parsedBody.stream, true);
    assert.equal(parsedBody.model, "integration-chat");
    assert.equal(stdout.text(), "流式成功\nrequest_id=req-sse-success\n");
    assert.doesNotMatch(stdout.text(), /local SSE prompt|virtual-stream-secret/);
  } finally {
    await fixture.close();
  }
});

test("真实 SSE 中途断流保留已输出文本并回退握手 request_id", async () => {
  const fixture = await startServer(async (request, response) => {
    await consumeRequest(request);
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "x-request-id": "req-sse-disconnect",
      connection: "close",
    });
    response.write(sseEvent(textChunk("partial")));
    setTimeout(() => response.socket?.destroy(), 20);
  });

  try {
    const stdout = createCapture();
    const stderr = createCapture();
    const exitCode = await main({
      argv: ["--prompt", "disconnect prompt"],
      environ: {
        METAHUB_BASE_URL: fixture.baseUrl,
        METAHUB_VIRTUAL_KEY: "virtual-stream-secret",
        METAHUB_CHAT_MODEL: "integration-chat",
        METAHUB_TIMEOUT_SECONDS: "1",
        METAHUB_MAX_RETRIES: "0",
      },
      stdout,
      stderr,
    });

    assert.equal(exitCode, 1);
    assert.equal(stdout.text(), "partial\n");
    assert.match(
      stderr.text(),
      /^error_code=client\.(connection_failed|unexpected_error) status=- request_id=req-sse-disconnect retryable=(true|false)\n$/,
    );
    assert.doesNotMatch(stderr.text(), /disconnect prompt|virtual-stream-secret/);
  } finally {
    await fixture.close();
  }
});

test("真实 SSE 在首个 delta 后停滞会触发总时限 abort 并清理连接", async () => {
  let connectionClosedResolve;
  const connectionClosed = new Promise((resolve) => {
    connectionClosedResolve = resolve;
  });
  const fixture = await startServer(async (request, response) => {
    await consumeRequest(request);
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "x-request-id": "req-sse-timeout",
      connection: "close",
    });
    response.write(sseEvent(textChunk("before-timeout")));
    response.once("close", connectionClosedResolve);
  });

  try {
    const stdout = createCapture();
    const stderr = createCapture();
    const exitCode = await main({
      argv: ["--prompt", "timeout prompt"],
      environ: {
        METAHUB_BASE_URL: fixture.baseUrl,
        METAHUB_VIRTUAL_KEY: "virtual-stream-secret",
        METAHUB_CHAT_MODEL: "integration-chat",
        // 给 SDK 初始化和 Windows 并发调度留出余量，超时仍发生在首段后的停滞阶段。
        METAHUB_TIMEOUT_SECONDS: "2",
        METAHUB_MAX_RETRIES: "0",
      },
      stdout,
      stderr,
    });

    assert.equal(exitCode, 1);
    assert.equal(stdout.text(), "before-timeout\n");
    assert.equal(
      stderr.text(),
      "error_code=client.stream_timeout status=- request_id=req-sse-timeout retryable=true\n",
    );
    await Promise.race([
      connectionClosed,
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("SSE connection was not aborted")), 1_000),
      ),
    ]);
  } finally {
    await fixture.close();
  }
});

test("没有任何文本 delta 的流返回 client.empty_stream", async () => {
  const client = {
    chat: {
      completions: {
        create() {
          return {
            async withResponse() {
              return {
                request_id: "req-empty-stream",
                data: (async function* () {
                  yield { choices: [] };
                  yield { choices: [{ delta: { tool_calls: [] } }] };
                  yield { usage: { total_tokens: 1 } };
                })(),
              };
            },
          };
        },
      },
    },
  };
  const stdout = createCapture();
  const stderr = createCapture();
  const exitCode = await main({
    argv: ["--prompt", "empty stream prompt"],
    environ: { METAHUB_VIRTUAL_KEY: "virtual-test-secret" },
    clientFactory() {
      return client;
    },
    stdout,
    stderr,
  });

  assert.equal(exitCode, 1);
  assert.equal(stdout.text(), "");
  assert.equal(
    stderr.text(),
    "error_code=client.empty_stream status=- request_id=req-empty-stream retryable=false\n",
  );
});
