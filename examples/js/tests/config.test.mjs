/**
 * 验证环境变量解析、默认值、严格数值边界和配置错误不会回显敏感输入。
 */

import assert from "node:assert/strict";
import test from "node:test";

import { loadConfig } from "../src/config.js";
import { ClientError } from "../src/errors.js";

/**
 * 构造只存在于测试内存中的合法环境映射，避免创建包含 Virtual Key 的本地文件。
 */
function validEnvironment(overrides = {}) {
  return { METAHUB_VIRTUAL_KEY: "virtual-test-secret", ...overrides };
}

test("配置解析全部受支持变量并转换毫秒超时", () => {
  const config = loadConfig(
    validEnvironment({
      METAHUB_BASE_URL: "https://gateway.example.test/v1/",
      METAHUB_CHAT_MODEL: "tenant-chat",
      METAHUB_TIMEOUT_SECONDS: "12.5",
      METAHUB_MAX_RETRIES: "4",
    }),
  );

  assert.deepEqual(config, {
    baseUrl: "https://gateway.example.test/v1",
    virtualKey: "virtual-test-secret",
    chatModel: "tenant-chat",
    timeoutSeconds: 12.5,
    timeoutMilliseconds: 12_500,
    maxRetries: 4,
  });
  assert.equal(Object.isFrozen(config), true);
});

test("仅提供 Virtual Key 时使用文档默认值", () => {
  const config = loadConfig(validEnvironment());

  assert.equal(config.baseUrl, "http://localhost:8000/v1");
  assert.equal(config.chatModel, "default-chat");
  assert.equal(config.timeoutSeconds, 30);
  assert.equal(config.timeoutMilliseconds, 30_000);
  assert.equal(config.maxRetries, 2);
});

test("缺少或空白 Virtual Key 返回固定本地错误", () => {
  for (const environment of [{}, { METAHUB_VIRTUAL_KEY: "   " }]) {
    assert.throws(
      () => loadConfig(environment),
      (error) =>
        error instanceof ClientError && error.code === "client.missing_virtual_key",
    );
  }
});

test("超时只接受 0.1 到 300 的有限数字", () => {
  for (const value of ["", "text", "0", "-1", "300.1", "NaN", "Infinity"]) {
    assert.throws(
      () =>
        loadConfig(validEnvironment({ METAHUB_TIMEOUT_SECONDS: value })),
      (error) =>
        error.code === "client.invalid_timeout" &&
        (value === "" || !error.message.includes(value)),
    );
  }

  assert.equal(
    loadConfig(validEnvironment({ METAHUB_TIMEOUT_SECONDS: "0.1" })).timeoutMilliseconds,
    100,
  );
  assert.equal(
    loadConfig(validEnvironment({ METAHUB_TIMEOUT_SECONDS: "300" })).timeoutMilliseconds,
    300_000,
  );
});

test("重试次数拒绝负数、小数、指数、越界与非数字", () => {
  for (const value of ["", "-1", "1.5", "1e1", "+1", "11", "many"]) {
    assert.throws(
      () => loadConfig(validEnvironment({ METAHUB_MAX_RETRIES: value })),
      (error) =>
        error.code === "client.invalid_retries" &&
        (value === "" || !error.message.includes(value)),
    );
  }

  assert.equal(loadConfig(validEnvironment({ METAHUB_MAX_RETRIES: "0" })).maxRetries, 0);
  assert.equal(loadConfig(validEnvironment({ METAHUB_MAX_RETRIES: "10" })).maxRetries, 10);
});

test("Gateway 地址拒绝非法协议、缺少主机和内嵌凭据", () => {
  const invalidUrls = [
    "gateway.example.test/v1",
    "ftp://gateway.example.test/v1",
    "https://user:secret@gateway.example.test/v1",
  ];

  for (const value of invalidUrls) {
    assert.throws(
      () => loadConfig(validEnvironment({ METAHUB_BASE_URL: value })),
      (error) =>
        error.code === "client.invalid_base_url" && !error.message.includes("user:secret"),
    );
  }
});
