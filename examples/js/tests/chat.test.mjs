/**
 * 验证非流式可注入入口，并用真实官方 SDK 与本地 HTTP 服务覆盖协议和错误路径。
 */

import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";

import { main, runChat } from "../src/chat.js";
import { createOpenAIClient } from "../src/client.js";
import { loadConfig } from "../src/config.js";
import { ClientError } from "../src/errors.js";

/**
 * 创建内存文本写入器，用于断言 stdout/stderr 且不触碰进程级输出流。
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
 * 在回环地址启动临时 HTTP 服务，返回 OpenAI-compatible base URL 与可靠关闭函数。
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
 * 读取测试请求正文并解析 JSON，确保官方 SDK 真正发送了预期 Chat 参数。
 */
async function readJsonBody(request) {
  let body = "";
  request.setEncoding("utf8");
  for await (const chunk of request) {
    body += chunk;
  }
  return JSON.parse(body);
}

/**
 * 构造指向临时服务的合法配置，关闭重试以便精确验证单次协议行为。
 */
function integrationConfig(baseUrl, overrides = {}) {
  return loadConfig({
    METAHUB_BASE_URL: baseUrl,
    METAHUB_VIRTUAL_KEY: "virtual-integration-secret",
    METAHUB_CHAT_MODEL: "integration-chat",
    METAHUB_TIMEOUT_SECONDS: "1",
    METAHUB_MAX_RETRIES: "0",
    ...overrides,
  });
}

test("runChat 对 fake client 显式使用非流式并只输出正文和 _request_id", async () => {
  let received;
  const client = {
    chat: {
      completions: {
        async create(request) {
          received = request;
          return {
            choices: [{ message: { content: "fake answer" } }],
            _request_id: "req-fake",
            secretField: "must-not-print",
          };
        },
      },
    },
  };
  const stdout = createCapture();

  await runChat({
    client,
    config: { chatModel: "fake-model" },
    prompt: "fake prompt",
    stdout,
  });

  assert.deepEqual(received, {
    model: "fake-model",
    messages: [{ role: "user", content: "fake prompt" }],
    stream: false,
  });
  assert.equal(stdout.text(), "fake answer\nrequest_id=req-fake\n");
  assert.doesNotMatch(stdout.text(), /fake prompt|must-not-print/);
});

test("main 支持 argv、environ、clientFactory 和输出流注入", async () => {
  const stdout = createCapture();
  const stderr = createCapture();
  let receivedConfig;

  const exitCode = await main({
    argv: ["--prompt", "injected prompt"],
    environ: { METAHUB_VIRTUAL_KEY: "virtual-injected-secret" },
    clientFactory(config) {
      receivedConfig = config;
      return {
        chat: {
          completions: {
            async create() {
              return {
                choices: [{ message: { content: "injected answer" } }],
                _request_id: "req-injected",
              };
            },
          },
        },
      };
    },
    stdout,
    stderr,
  });

  assert.equal(exitCode, 0);
  assert.equal(receivedConfig.virtualKey, "virtual-injected-secret");
  assert.equal(stdout.text(), "injected answer\nrequest_id=req-injected\n");
  assert.equal(stderr.text(), "");
});

test("真实 openai 客户端从本地 HTTP 响应提取 _request_id", async () => {
  let receivedBody;
  let receivedAuthorization;
  const fixture = await startServer(async (request, response) => {
    receivedBody = await readJsonBody(request);
    receivedAuthorization = request.headers.authorization;
    response.writeHead(200, {
      "content-type": "application/json",
      "x-request-id": "req-http-success",
      connection: "close",
    });
    response.end(
      JSON.stringify({
        id: "chatcmpl-local",
        object: "chat.completion",
        created: 1,
        model: "integration-chat",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: "HTTP answer" },
            finish_reason: "stop",
          },
        ],
      }),
    );
  });

  try {
    const config = integrationConfig(fixture.baseUrl);
    const stdout = createCapture();
    await runChat({
      client: createOpenAIClient(config),
      config,
      prompt: "local HTTP prompt",
      stdout,
    });

    assert.equal(receivedAuthorization, "Bearer virtual-integration-secret");
    assert.equal(receivedBody.stream, false);
    assert.equal(receivedBody.model, "integration-chat");
    assert.equal(stdout.text(), "HTTP answer\nrequest_id=req-http-success\n");
    assert.doesNotMatch(stdout.text(), /local HTTP prompt|virtual-integration-secret/);
  } finally {
    await fixture.close();
  }
});

test("真实 HTTP 状态错误保留公开 code、status 和响应头 requestID", async () => {
  const fixture = await startServer(async (request, response) => {
    await readJsonBody(request);
    response.writeHead(429, {
      "content-type": "application/json",
      "x-request-id": "req-http-error",
      connection: "close",
    });
    response.end(
      JSON.stringify({
        code: "gateway.rate_limited",
        message: "virtual-integration-secret and prompt must not leak",
        type: "rate_limit_error",
      }),
    );
  });

  try {
    const stdout = createCapture();
    const stderr = createCapture();
    const exitCode = await main({
      argv: ["--prompt", "sensitive prompt"],
      environ: {
        METAHUB_BASE_URL: fixture.baseUrl,
        METAHUB_VIRTUAL_KEY: "virtual-integration-secret",
        METAHUB_MAX_RETRIES: "0",
      },
      stdout,
      stderr,
    });

    assert.equal(exitCode, 1);
    assert.equal(stdout.text(), "");
    assert.equal(
      stderr.text(),
      "error_code=client.api_error status=429 request_id=req-http-error retryable=true\n",
    );
    assert.doesNotMatch(stderr.text(), /sensitive prompt|virtual-integration-secret/);
  } finally {
    await fixture.close();
  }
});

test("空 choices 返回 client.empty_response 且保留成功响应 ID", async () => {
  const client = {
    chat: {
      completions: {
        async create() {
          return { choices: [], _request_id: "req-empty" };
        },
      },
    },
  };

  await assert.rejects(
    runChat({
      client,
      config: { chatModel: "fake-model" },
      prompt: "hidden prompt",
      stdout: createCapture(),
    }),
    (error) =>
      error instanceof ClientError &&
      error.code === "client.empty_response" &&
      error.requestId === "req-empty",
  );
});

test("未知参数和缺少 Prompt 只返回固定本地错误", async () => {
  for (const argv of [[], ["--unknown", "value"], ["--prompt", "   "]]) {
    const stderr = createCapture();
    const exitCode = await main({
      argv,
      environ: { METAHUB_VIRTUAL_KEY: "virtual-test-secret" },
      clientFactory() {
        // 参数校验应先于客户端构造，执行到这里说明调用顺序发生安全回归。
        throw new Error("client factory must not run");
      },
      stdout: createCapture(),
      stderr,
    });

    assert.equal(exitCode, 1);
    assert.match(stderr.text(), /^error_code=client\.(invalid_arguments|missing_prompt) /);
    assert.doesNotMatch(stderr.text(), /value|virtual-test-secret/);
  }
});
