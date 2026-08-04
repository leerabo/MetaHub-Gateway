/**
 * 验证官方客户端构造参数、日志关闭策略和浏览器运行时的强制阻断边界。
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createOpenAIClient } from "../src/client.js";
import { ClientError } from "../src/errors.js";

/**
 * 记录客户端构造选项，确保测试无需访问真实网络或真实凭据。
 */
class RecordingOpenAI {
  /**
   * 保存公开构造参数供断言使用，不实现任何请求方法。
   */
  constructor(options) {
    this.options = options;
  }
}

test("客户端只使用 PRD 允许的公开构造参数", () => {
  const client = createOpenAIClient(
    {
      virtualKey: "virtual-test-secret",
      baseUrl: "https://gateway.example.test/v1",
      timeoutMilliseconds: 12_500,
      maxRetries: 3,
    },
    { OpenAIClass: RecordingOpenAI },
  );

  assert.deepEqual(client.options, {
    apiKey: "virtual-test-secret",
    baseURL: "https://gateway.example.test/v1",
    timeout: 12_500,
    maxRetries: 3,
    logLevel: "off",
  });
  assert.equal("dangerouslyAllowBrowser" in client.options, false);
});

test("浏览器模拟环境在读取 Virtual Key 和构造客户端前失败", () => {
  const previousWindow = globalThis.window;
  let keyWasRead = false;
  let constructorWasCalled = false;
  globalThis.window = {};

  /**
   * 记录构造器是否被误调用，浏览器保护正确时该构造器永远不会执行。
   */
  class ForbiddenOpenAI {
    /**
     * 标记不应发生的浏览器客户端构造，以验证保护顺序。
     */
    constructor() {
      constructorWasCalled = true;
    }
  }

  const config = {
    get virtualKey() {
      keyWasRead = true;
      return "must-not-be-read";
    },
  };

  try {
    assert.throws(
      () => createOpenAIClient(config, { OpenAIClass: ForbiddenOpenAI }),
      (error) =>
        error instanceof ClientError &&
        error.code === "client.browser_runtime_forbidden",
    );
    assert.equal(keyWasRead, false);
    assert.equal(constructorWasCalled, false);
  } finally {
    if (previousWindow === undefined) {
      delete globalThis.window;
    } else {
      globalThis.window = previousWindow;
    }
  }
});
