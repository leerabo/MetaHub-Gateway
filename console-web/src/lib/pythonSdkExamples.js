/**
 * 模块职责：读取 Python 与 JavaScript SDK 示例元数据，并下载对应的受控 ZIP 制品。
 * 维护边界：使用独立的组织 Cookie 或旧登录 Token；tenant_id、源码目录和归档成员由后端恢复与校验。
 */

import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

// 两种语言分别使用固定 ASCII 兜底文件名，避免异常响应头导致路径注入或下载错误语言的文件名。
const DEFAULT_ARCHIVE_NAMES = Object.freeze({
  python: "metahub-python-examples.zip",
  javascript: "metahub-javascript-examples.zip",
});

/**
 * 读取当前租户可见的 Python SDK 示例元数据。
 * 前端不能拼接 tenant_id 或任意目录参数，后端会从认证上下文选择模型别名并返回固定下载地址。
 */
export async function getPythonSdkExample(input) {
  return getSdkExampleMetadata({ ...input, language: "python" });
}

/**
 * 读取当前租户可见的 JavaScript/Node SDK 示例元数据。
 * 浏览器只展示服务端 Node 示例说明，不接收 Virtual Key，也不会生成浏览器直连 Gateway 的代码。
 */
export async function getJavascriptSdkExample(input) {
  return getSdkExampleMetadata({ ...input, language: "javascript" });
}

/**
 * 下载当前版本的 Python 示例 ZIP，并返回 Blob、可信文件名及链路标识。
 * 调用方负责在用户手势内创建临时对象 URL；本函数不会把二进制内容写入页面状态或日志。
 */
export async function downloadPythonSdkExample(input) {
  return downloadSdkExample({ ...input, language: "python" });
}

/**
 * 下载当前版本的 JavaScript/Node 示例 ZIP，并返回 Blob、可信文件名及链路标识。
 * 下载保留当前认证域，禁止把认证信息放入 URL。
 */
export async function downloadJavascriptSdkExample(input) {
  return downloadSdkExample({ ...input, language: "javascript" });
}

/**
 * 按固定语言路径读取 SDK 示例元数据，并给成功状态补充统一的 state 标识。
 * language 只来自模块内两个公开包装函数，不能由页面或 URL 参数直接透传到请求路径。
 */
async function getSdkExampleMetadata({ controlApiUrl, authentication, token, signal, language }) {
  const response = await sdkExampleRequest(
    `${controlApiUrl}/developer/sdk-examples/${language}`,
    { authentication, token, signal },
    language,
  );
  return { state: "ok", ...(await response.json()) };
}

/**
 * 按固定语言路径下载 SDK ZIP，并统一提取 Blob、安全文件名和请求链路标识。
 * 文件名即使来自受信后端也要经过前端白名单复核，代理或网关注入异常响应头时会使用语言专属兜底。
 */
async function downloadSdkExample({ controlApiUrl, authentication, token, signal, language }) {
  const response = await sdkExampleRequest(
    `${controlApiUrl}/developer/sdk-examples/${language}/download`,
    { authentication, token, signal },
    language,
  );
  const contentDisposition = response.headers.get("Content-Disposition") ?? "";
  return {
    blob: await response.blob(),
    filename: safeArchiveFilename(contentDisposition, language),
    requestId: response.headers.get("X-Request-ID") ?? "-",
    traceId: response.headers.get("X-Trace-ID") ?? "-",
  };
}

/**
 * 执行带控制面认证的 SDK 示例请求，并把 JSON 错误体归一为 AuthApiError。
 * 成功响应保持原始 Response，供元数据和二进制下载分别消费；错误消息不得回显响应原文或堆栈。
 */
async function sdkExampleRequest(url, { authentication, token, signal }, language) {
  const response = await authenticatedResourceFetch(url, {
    authentication, token, signal,
    headers: {
      "X-Request-ID": `console_${language}_sdk_${Date.now()}`,
    },
  });
  // 二进制下载失败时控制面仍返回统一 JSON 错误，必须先映射错误再尝试读取 Blob。
  if (!response.ok) {
    const payload = await readJson(response);
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `${sdkLanguageLabel(language)} SDK 示例接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return response;
}

/**
 * 从 Content-Disposition 中提取安全 ZIP 文件名。
 * 只接受 ASCII 字母、数字、点、下划线和连字符；拒绝目录分隔符、控制字符及非 zip 扩展名。
 */
export function safeArchiveFilename(contentDisposition, language = "python") {
  const fallbackName = DEFAULT_ARCHIVE_NAMES[language] ?? DEFAULT_ARCHIVE_NAMES.python;
  const match = String(contentDisposition).match(/filename="?([^";]+)"?/i);
  const candidate = match?.[1]?.trim() ?? "";
  // 文件名必须是单个 ASCII zip 基名，任何目录字符、空白注入或异常扩展名都回退到固定安全值。
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*\.zip$/.test(candidate)) {
    return fallbackName;
  }
  return candidate;
}

/**
 * 把内部语言码转换为固定中文标签，仅用于无 JSON 错误体时的公开兜底消息。
 * 未知语言不会进入请求路径；这里仍使用通用标签，避免将异常值反射到页面。
 */
function sdkLanguageLabel(language) {
  return language === "javascript" ? "JavaScript" : "Python";
}

/**
 * 安全读取统一 JSON 错误体；代理返回空响应或 HTML 时使用空对象进入固定错误提示。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    // 非 JSON 错误体可能包含代理页面或内部细节，不能把原文带入 AuthApiError。
    return {};
  }
}
