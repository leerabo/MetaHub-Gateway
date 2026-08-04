/**
 * 负责把进程环境转换为经过边界校验的 Node 示例配置，禁止回显任何原始配置值。
 */

import { ClientError } from "./errors.js";

// 默认值与数值边界和开发者门户元数据保持一致，避免客户端产生无上限等待或重试。
const DEFAULT_BASE_URL = "http://localhost:8000/v1";
const DEFAULT_CHAT_MODEL = "default-chat";
const DEFAULT_TIMEOUT_SECONDS = 30;
const DEFAULT_MAX_RETRIES = 2;
const MIN_TIMEOUT_SECONDS = 0.1;
const MAX_TIMEOUT_SECONDS = 300;
const MAX_RETRIES = 10;

/**
 * 读取必填文本变量，只返回固定错误码，避免把 Virtual Key 或其他环境值带入异常。
 */
function requiredText(environ, name, errorCode) {
  const value = environ?.[name];
  // Virtual Key 缺失或全为空白都在发起网络请求前拒绝，错误中不保留原值。
  if (typeof value !== "string" || value.trim() === "") {
    throw new ClientError(errorCode);
  }
  return value.trim();
}

/**
 * 读取可选文本变量，空值回落到公开默认值，不保存调用方传入的空白内容。
 */
function optionalText(environ, name, defaultValue) {
  const value = environ?.[name];
  if (typeof value !== "string" || value.trim() === "") {
    return defaultValue;
  }
  return value.trim();
}

/**
 * 校验并规范化 Gateway 地址，拒绝非 HTTP 协议、缺少主机和 URL 内嵌凭据。
 */
function normalizeBaseUrl(environ) {
  const rawValue = optionalText(environ, "METAHUB_BASE_URL", DEFAULT_BASE_URL);
  let parsed;
  try {
    parsed = new URL(rawValue);
  } catch {
    // URL 解析异常统一映射为固定代码，不回显可能包含凭据的原始地址。
    throw new ClientError("client.invalid_base_url");
  }
  // URL 中的用户名或密码容易进入代理日志和诊断输出，因此必须在建连前拒绝。
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    !parsed.hostname ||
    parsed.username ||
    parsed.password
  ) {
    // 协议、主机或内嵌凭据任一不合规时，必须在客户端建连前终止。
    throw new ClientError("client.invalid_base_url");
  }
  return rawValue.replace(/\/+$/, "");
}

/**
 * 解析流式总时限和 SDK 毫秒超时，拒绝 NaN、Infinity、空值及范围外数字。
 */
function parseTimeoutSeconds(environ) {
  const rawValue =
    environ?.METAHUB_TIMEOUT_SECONDS === undefined
      ? String(DEFAULT_TIMEOUT_SECONDS)
      : String(environ.METAHUB_TIMEOUT_SECONDS).trim();
  const value = Number(rawValue);
  if (
    !Number.isFinite(value) ||
    value < MIN_TIMEOUT_SECONDS ||
    value > MAX_TIMEOUT_SECONDS
  ) {
    // 超时非法时只返回固定类别，禁止把环境变量原值带入诊断输出。
    throw new ClientError("client.invalid_timeout");
  }
  return value;
}

/**
 * 解析官方 SDK 自动重试次数，只允许零到十的十进制整数。
 */
function parseMaxRetries(environ) {
  const rawValue =
    environ?.METAHUB_MAX_RETRIES === undefined
      ? String(DEFAULT_MAX_RETRIES)
      : String(environ.METAHUB_MAX_RETRIES).trim();
  // 使用字符串语法约束可拒绝小数、指数、符号和隐式数值转换造成的歧义。
  if (!/^(?:0|[1-9]\d*)$/.test(rawValue)) {
    throw new ClientError("client.invalid_retries");
  }
  const value = Number(rawValue);
  // 重试次数超过上限会放大上游流量和成本，因此只返回固定错误代码。
  if (value > MAX_RETRIES) {
    throw new ClientError("client.invalid_retries");
  }
  return value;
}

/**
 * 从可注入环境映射构造不可变配置，便于测试且不会修改全局 process.env。
 */
export function loadConfig(environ = process.env) {
  return Object.freeze({
    baseUrl: normalizeBaseUrl(environ),
    virtualKey: requiredText(
      environ,
      "METAHUB_VIRTUAL_KEY",
      "client.missing_virtual_key",
    ),
    chatModel: optionalText(environ, "METAHUB_CHAT_MODEL", DEFAULT_CHAT_MODEL),
    timeoutSeconds: parseTimeoutSeconds(environ),
    timeoutMilliseconds: parseTimeoutSeconds(environ) * 1000,
    maxRetries: parseMaxRetries(environ),
  });
}
