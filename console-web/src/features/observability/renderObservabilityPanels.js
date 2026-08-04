/**
 * 渲染 可观测面板 的 `renderObservabilityPanels` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
export function renderObservabilityPanels(exampleError, options = {}) {
  const gatewayUrl = options.gatewayUrl ?? "http://localhost:8000";
  return `
    <section class="panel api-docs-panel" aria-labelledby="api-docs-title">
      ${renderApiDocsPanel({ gatewayUrl })}
    </section>
    <section class="observability-grid" aria-label="基础可观测预留">
      <section class="panel error-page-panel" aria-labelledby="error-page-title">
        ${renderErrorPagePreview(exampleError)}
      </section>
      <section class="panel gateway-debug-panel" aria-labelledby="gateway-debug-title">
        ${renderGatewayDebugConsole(options.debugState ?? {}, { gatewayUrl })}
      </section>
    </section>
  `;
}

/**
 * 渲染 可观测面板 的 `renderApiDocsPanel` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
export function renderApiDocsPanel({ gatewayUrl }) {
  const baseUrl = `${String(gatewayUrl).replace(/\/$/, "")}/v1`;
  return `
    <div class="panel-heading api-docs-heading">
      <div>
        <h2 id="api-docs-title">API 文档 · OpenAI-Compatible</h2>
        <p>将 OpenAI SDK 的 base_url 指向网关，使用 Virtual Key 作为 Bearer Token。</p>
      </div>
      <code>${escapeHtml(baseUrl)}</code>
    </div>
    <div class="api-docs-grid">
      ${renderApiEndpoint("POST", "/v1/chat/completions", "Chat Completions")}
      ${renderApiEndpoint("POST", "/v1/embeddings", "Embeddings")}
    </div>
    <div class="code-examples" aria-label="SDK 示例">
      <pre><code>${escapeHtml(`from openai import OpenAI

client = OpenAI(
    base_url="${baseUrl}",
    api_key="<virtual_key>",
)

response = client.chat.completions.create(
    model="default-chat",
    messages=[{"role": "user", "content": "你好"}],
)`)}</code></pre>
      <pre><code>${escapeHtml(`import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "${baseUrl}",
  apiKey: process.env.METAHUB_VIRTUAL_KEY,
});

const response = await client.chat.completions.create({
  model: "default-chat",
  messages: [{ role: "user", content: "你好" }],
});`)}</code></pre>
    </div>
  `;
}

/**
 * 渲染 可观测面板 的 `renderErrorPagePreview` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
export function renderErrorPagePreview(error) {
  // 错误页只展示对调用方有帮助的定位字段，不能展示服务端堆栈、Provider Key 或 Token。
  return `
    <div class="panel-heading">
      <h2 id="error-page-title">错误页预览</h2>
      <p>统一展示错误码、request_id、trace_id 和修复建议。</p>
    </div>
    <div class="error-preview" role="group" aria-label="错误详情">
      <div class="error-code">${escapeHtml(error.code)}</div>
      <p>${escapeHtml(error.message)}</p>
      <dl class="trace-fields">
        <div>
          <dt>request_id</dt>
          <dd>${escapeHtml(error.requestId)}</dd>
        </div>
        <div>
          <dt>trace_id</dt>
          <dd>${escapeHtml(error.traceId)}</dd>
        </div>
      </dl>
      <p class="suggestion">${escapeHtml(error.suggestion)}</p>
    </div>
  `;
}

/**
 * 渲染 可观测面板 的 `renderGatewayDebugConsole` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
export function renderGatewayDebugConsole(state = {}, { gatewayUrl = "http://localhost:8000" } = {}) {
  // 调试台不保存 Virtual Key，也不把明文 Key 渲染进状态；刷新后需要重新输入。
  const model = state.model ?? "default-chat";
  const message = state.message ?? "你好，MetaHub";
  const streamChecked = state.stream ? "checked" : "";
  const submitLabel = state.running ? "重新发送" : "发送";
  const stopHidden = state.running && state.stream ? "" : "hidden";
  return `
    <div class="panel-heading">
      <h2 id="gateway-debug-title">OpenAI 调试台</h2>
      <p>发送一次 Chat Completions 请求，响应会返回 request_id/trace_id 便于排障。</p>
    </div>
    <form id="gateway-debug-form" class="debug-form" aria-label="OpenAI 调试台表单" data-gateway-url="${escapeAttribute(gatewayUrl)}">
      <label>
        <span>Virtual Key</span>
        <input name="virtualKey" type="password" autocomplete="off" placeholder="mhv_..." />
      </label>
      <label>
        <span>model</span>
        <input name="model" type="text" value="${escapeAttribute(model)}" />
      </label>
      <label>
        <span>user message</span>
        <textarea name="message" rows="3">${escapeHtml(message)}</textarea>
      </label>
      <label class="checkbox-label">
        <input name="stream" type="checkbox" ${streamChecked} />
        <span>stream</span>
      </label>
      <div class="debug-actions">
        <button type="submit">${escapeHtml(submitLabel)}</button>
        <button
          id="gateway-debug-stop-button"
          type="button"
          title="停止流式请求"
          aria-label="停止流式请求"
          ${stopHidden}
        >■ 停止</button>
        <span id="gateway-debug-feedback" class="gateway-debug-feedback" aria-live="polite">${escapeHtml(state.feedback ?? "")}</span>
      </div>
    </form>
    ${renderDebugResult(state)}
  `;
}

/**
 * 渲染 可观测面板 的 `renderDebugPlaceholder` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
export function renderDebugPlaceholder() {
  return renderGatewayDebugConsole();
}

/**
 * 渲染 可观测面板 的 `renderApiEndpoint` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderApiEndpoint(method, path, label) {
  return `
    <div class="api-endpoint">
      <span>${escapeHtml(method)}</span>
      <code>${escapeHtml(path)}</code>
      <strong>${escapeHtml(label)}</strong>
    </div>
  `;
}

/**
 * 渲染 可观测面板 的 `renderDebugResult` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderDebugResult(state) {
  const hasResult = Boolean(state.result || state.error || state.running);
  const result = state.result ?? streamResultFromError(state.error);
  const status = state.error ? "failed" : result?.status ?? (state.running ? "waiting" : "idle");
  const statusLabel = debugStatusLabel(status);
  const emptyHidden = hasResult ? "hidden" : "";
  const resultHidden = hasResult ? "" : "hidden";
  const errorHidden = state.error ? "" : "hidden";

  // 结果骨架始终存在，运行时可以只更新 textContent/hidden，而不需要把模型输出拼进 innerHTML。
  return `
    <div id="gateway-debug-empty" class="empty-state debug-empty" ${emptyHidden}>等待发送请求。</div>
    <div id="gateway-debug-result" class="debug-result" aria-live="polite" ${resultHidden}>
      <div class="debug-stream-status" role="status">
        <strong id="gateway-debug-status">${escapeHtml(statusLabel)}</strong>
        <span>分片 <b id="gateway-debug-chunk-count">${escapeHtml(result?.chunkCount ?? 0)}</b></span>
        <span>字节 <b id="gateway-debug-byte-count">${escapeHtml(result?.byteCount ?? 0)}</b></span>
      </div>
      <dl class="trace-fields">
        <div>
          <dt>request_id</dt>
          <dd id="gateway-debug-request-id">${escapeHtml(result?.requestId ?? state.error?.requestId ?? "-")}</dd>
        </div>
        <div>
          <dt>trace_id</dt>
          <dd id="gateway-debug-trace-id">${escapeHtml(result?.traceId ?? state.error?.traceId ?? "-")}</dd>
        </div>
      </dl>
      <p id="gateway-debug-truncated" class="suggestion" ${result?.previewTruncated ? "" : "hidden"}>预览已截断，仅保留最新 256 KiB。</p>
      <pre><code id="gateway-debug-preview">${escapeHtml(result?.preview ?? "")}</code></pre>
      <div id="gateway-debug-error" class="debug-result-error" role="alert" ${errorHidden}>
        <strong id="gateway-debug-error-code">${escapeHtml(state.error?.code ?? "gateway.request_failed")}</strong>
        <p id="gateway-debug-error-message">${escapeHtml(state.error?.message ?? "网关请求失败")}</p>
        <p id="gateway-debug-error-suggestion" class="suggestion">${escapeHtml(state.error?.suggestion ?? "")}</p>
      </div>
    </div>
  `;
}

/**
 * 从流式异常中提取可继续展示的有界预览和计数。
 * 关键边界：这里只读取 GatewayApiError 的安全字段，不接收完整响应体或 Authorization。
 */
function streamResultFromError(error) {
  if (!error) {
    return null;
  }
  return {
    status: "failed",
    requestId: error.requestId ?? "-",
    traceId: error.traceId ?? "-",
    preview: error.preview ?? "",
    previewTruncated: Boolean(error.previewTruncated),
    chunkCount: error.chunkCount ?? 0,
    byteCount: error.byteCount ?? 0,
  };
}

/**
 * 把内部流状态映射为稳定中文文案，避免网络状态值直接暴露给用户。
 */
function debugStatusLabel(status) {
  const labels = {
    idle: "等待发送",
    waiting: "等待首个分片",
    streaming: "接收中",
    completed: "已完成",
    failed: "失败",
    stopped: "已停止",
  };
  return labels[status] ?? "处理中";
}

/**
 * 执行 可观测面板 的 `escapeHtml` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * 执行 可观测面板 的 `escapeAttribute` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
