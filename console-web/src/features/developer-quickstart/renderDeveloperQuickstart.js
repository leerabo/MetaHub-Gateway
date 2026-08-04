/**
 * 渲染开发者快速开始页。
 * 页面只展示安全占位和脱敏 Key 前缀；真实 Virtual Key 明文只能由用户本地环境变量保存。
 */
export function renderDeveloperQuickstart(state) {
  if (!state) {
    return renderWaitingState();
  }
  // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
  if (state.state === "error") {
    // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    return renderErrorState(state);
  }
  const examples = Array.isArray(state.examples) ? state.examples : [];
  return `
    <section class="panel quickstart-panel" aria-labelledby="quickstart-title">
      <div class="panel-heading quickstart-heading">
        <div>
          <h2 id="quickstart-title">5分钟快速接入</h2>
          <p>复制示例到本地执行一次非流式 chat 调用；Virtual Key 只通过环境变量读取。</p>
        </div>
        <span class="quickstart-badge">${escapeHtml(state.request_id ?? "-")}</span>
      </div>
      <div class="quickstart-summary" aria-label="快速开始配置摘要">
        ${renderSummaryItem("base_url", state.gateway_base_url)}
        ${renderSummaryItem("model", state.model_alias)}
        ${renderKeyState(state)}
      </div>
      ${renderSteps(state.steps ?? [])}
      ${renderExamples(examples)}
      ${renderCommonErrors(state.common_errors ?? [])}
    </section>
  `;
}

/**
 * 渲染快速开始等待态，避免数据尚未返回时出现空白页面。
 */
function renderWaitingState() {
  return `
    <section class="panel quickstart-panel" aria-labelledby="quickstart-title">
      <div class="panel-heading">
        <h2 id="quickstart-title">5分钟快速接入</h2>
      </div>
      <div class="empty-state">等待 /developer/quickstart-data 响应。</div>
    </section>
  `;
}

/**
 * 渲染快速开始错误态，展示统一错误和 request_id，不暴露堆栈或敏感配置。
 */
function renderErrorState(state) {
  return `
    <section class="panel quickstart-panel" aria-labelledby="quickstart-title">
      <div class="panel-heading">
        <h2 id="quickstart-title">5分钟快速接入</h2>
      </div>
      <div class="quickstart-error" role="alert">
        <strong>${escapeHtml(state.message ?? "快速开始数据不可用")}</strong>
        <span>request_id: ${escapeHtml(state.requestId ?? "-")}</span>
      </div>
    </section>
  `;
}

/**
 * 渲染配置摘要条目，统一处理 base_url、model 和 Key 状态的转义。
 */
function renderSummaryItem(label, value) {
  return `
    <div class="quickstart-summary-item">
      <span>${escapeHtml(label)}</span>
      <code>${escapeHtml(value ?? "-")}</code>
    </div>
  `;
}

/**
 * 渲染 Virtual Key 安全状态，只展示环境变量名或脱敏前缀。
 */
function renderKeyState(state) {
  const status = state.has_active_virtual_key ? "已有 active Key" : "需要创建 Key";
  const value = state.virtual_key_prefix || state.virtual_key_placeholder || "METAHUB_VIRTUAL_KEY";
  return `
    <div class="quickstart-summary-item">
      <span>Virtual Key</span>
      <code>${escapeHtml(value)}</code>
      <em>${escapeHtml(status)}</em>
    </div>
  `;
}

/**
 * 渲染快速开始步骤，状态来自后端，前端不自行推断租户配置是否完成。
 */
function renderSteps(steps) {
  return `
    <ol class="quickstart-steps" aria-label="快速开始步骤">
      ${steps
        .map(
          (step) => `
            <li class="quickstart-step quickstart-step-${escapeAttribute(step.status ?? "missing")}">
              <span>${escapeHtml(step.order ?? "")}</span>
              <div>
                <strong>${escapeHtml(step.title ?? "")}</strong>
                <p>${escapeHtml(step.description ?? "")}</p>
                <a href="${escapeAttribute(step.target_hash ?? "#quickstart")}">${escapeHtml(step.action_label ?? "查看")}</a>
              </div>
            </li>
          `,
        )
        .join("")}
    </ol>
  `;
}

/**
 * 渲染 SDK 示例代码块和复制按钮。
 * 复制按钮只引用代码块 ID，不把代码或 Key 占位塞进 data 属性。
 */
function renderExamples(examples) {
  return `
    <div class="quickstart-examples" aria-label="SDK 示例">
      ${examples.map(renderExample).join("")}
    </div>
  `;
}

/**
 * 渲染单个 SDK 示例卡片，代码块内容统一 HTML 转义。
 */
function renderExample(example) {
  const codeId = `quickstart-code-${safeDomId(example.id ?? "example")}`;
  return `
    <article class="quickstart-example">
      <div class="quickstart-example-heading">
        <div>
          <h3>${escapeHtml(example.title ?? example.language ?? "示例")}</h3>
          <p>${escapeHtml(example.description ?? "")}</p>
        </div>
        <button type="button" data-copy-code-target="${escapeAttribute(codeId)}">${escapeHtml(example.copy_label ?? "复制")}</button>
      </div>
      ${example.install_command ? `<pre class="quickstart-install"><code>${escapeHtml(example.install_command)}</code></pre>` : ""}
      <pre id="${escapeAttribute(codeId)}" class="quickstart-code"><code>${escapeHtml(example.code ?? "")}</code></pre>
      <span class="quickstart-copy-feedback" aria-live="polite"></span>
    </article>
  `;
}

/**
 * 渲染常见错误排查列表，展示公开错误码和修复建议。
 */
function renderCommonErrors(errors) {
  return `
    <section class="quickstart-errors" aria-labelledby="quickstart-errors-title">
      <div class="panel-heading compact-heading">
        <h3 id="quickstart-errors-title">常见错误</h3>
      </div>
      <div class="quickstart-error-list">
        ${errors
          .map(
            (error) => `
              <article>
                <code>${escapeHtml(error.code ?? "")}</code>
                <strong>${escapeHtml(error.title ?? "")}</strong>
                <p>${escapeHtml(error.cause ?? "")}</p>
                <a href="${escapeAttribute(error.link_target ?? "#quickstart")}">${escapeHtml(error.fix ?? "")}</a>
              </article>
            `,
          )
          .join("")}
      </div>
    </section>
  `;
}

/**
 * 生成安全 DOM id 片段，避免后端返回的示例 ID 破坏选择器。
 */
function safeDomId(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, "-");
}

/**
 * 转义 HTML 文本，避免模型别名、错误说明或示例内容造成 XSS。
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
 * 转义 HTML 属性，并额外处理反引号，避免 data 属性或 id 被截断。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
