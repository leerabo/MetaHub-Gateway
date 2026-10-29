// 调试台默认消息只作为空会话展示文案，不包含真实 Prompt、响应正文或用户输入。
const DEFAULT_MESSAGES = '[\n  {"role": "user", "content": "你好，MetaHub"}\n]';

/**
 * 渲染内置 API 调试控制台。
 *
 * 调试台只选择已有 Virtual Key 元数据，不展示也不接收明文 Key；真实调用由控制面后端按登录态、tenant_id 和
 * Virtual Key 记录重新校验并写入调用日志与用量账本。
 */
export function renderDebugConsolePanel(
  state = {},
  { virtualKeysState = null, modelAliasesState = null, environmentState = null } = {},
) {
  const environments = activeEnvironments(environmentState);
  const selectedEnvironmentId = state.environmentId || environments[0]?.environment_id || "";
  const activeKeys = activeVirtualKeys(virtualKeysState, selectedEnvironmentId);
  const activeAliases = activeModelAliases(modelAliasesState, selectedEnvironmentId);
  const selectedVirtualKeyId = state.virtualKeyId || activeKeys[0]?.virtual_key_id || "";
  const selectedModelAlias = state.modelAlias || activeAliases[0]?.alias || "default-chat";
  const messagesText = state.messagesText ?? DEFAULT_MESSAGES;
  return `
    <div class="panel-heading debug-console-heading">
      <div>
        <h2 id="gateway-debug-title">调用测试</h2>
        <p>选择可用的 KEY 和模型，发送一次测试请求。</p>
      </div>
    </div>
    ${renderDependencyWarning(activeKeys, activeAliases)}
    <form id="debug-console-form" class="debug-form debug-console-form">
      <label>
        <span>环境</span>
        <select name="environmentId" ${environments.length ? "" : "disabled"}>
          ${renderEnvironmentOptions(environments, selectedEnvironmentId)}
        </select>
      </label>
      <label>
        <span>使用 KEY</span>
        <select name="virtualKeyId" ${activeKeys.length ? "" : "disabled"}>
          ${renderVirtualKeyOptions(activeKeys, selectedVirtualKeyId)}
        </select>
      </label>
      <label>
        <span>模型</span>
        <select name="modelAlias" ${activeAliases.length ? "" : "disabled"}>
          ${renderModelAliasOptions(activeAliases, selectedModelAlias)}
        </select>
      </label>
      <label class="debug-console-messages">
        <span>消息内容</span>
        <textarea name="messages" rows="8" spellcheck="false">${escapeHtml(messagesText)}</textarea>
      </label>
      <div class="debug-console-params">
        <label>
          <span>temperature</span>
          <input name="temperature" type="number" min="0" max="2" step="0.1" value="${escapeAttribute(state.temperature ?? "0.2")}" />
        </label>
        <label>
          <span>max_tokens</span>
          <input name="maxTokens" type="number" min="1" max="32768" step="1" value="${escapeAttribute(state.maxTokens ?? "256")}" />
        </label>
      </div>
      <div class="debug-actions">
        <button type="submit" ${activeKeys.length && activeAliases.length ? "" : "disabled"}>发送</button>
        <span class="gateway-debug-feedback">${escapeHtml(state.feedback ?? "")}</span>
      </div>
    </form>
    ${renderDebugConsoleResult(state)}
  `;
}

/**
 * 从 Virtual Key 状态中筛出可用于调试台的 active Key。
 *
 * 前端筛选只是降低误操作概率，后端仍会在 /debug/chat-completions 上按 tenant_id 和状态重新校验。
 */
function activeVirtualKeys(virtualKeysState, environmentId) {
  if (virtualKeysState?.state !== "ok" || !Array.isArray(virtualKeysState.virtual_keys)) {
    return [];
  }
  // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
  return virtualKeysState.virtual_keys.filter(
    (item) => item.status === "active" && (!environmentId || item.environment_id === environmentId),
  );
}

/**
 * 从模型别名状态中筛出可用于调试台的 active alias。
 *
 * 调试台展示 alias 而不是内部模型 ID，保持与 Gateway OpenAI-compatible 调用方看到的模型名一致。
 */
function activeModelAliases(modelAliasesState, environmentId) {
  if (modelAliasesState?.state !== "ok" || !Array.isArray(modelAliasesState.model_aliases)) {
    return [];
  }
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  return modelAliasesState.model_aliases.filter(
    (item) => item.status === "active" && (!environmentId || item.environment_id === environmentId),
  );
}

/**
 * 从后端状态提取 active Environment，禁用环境不能在调试台发起调用。
 */
function activeEnvironments(environmentState) {
  if (environmentState?.state !== "ok" || !Array.isArray(environmentState.environments)) {
    return [];
  }
  return environmentState.environments.filter((item) => item.status === "active");
}

/**
 * 渲染调试台 Environment 选项，value 使用稳定 environment_id。
 */
function renderEnvironmentOptions(environments, selectedValue) {
  if (!environments.length) {
    return `<option value="">暂无 active Environment</option>`;
  }
    return environments.map((item) => {
      const selected = item.environment_id === selectedValue ? " selected" : "";
    return `<option value="${escapeAttribute(item.environment_id)}"${selected}>${escapeHtml(item.name)}</option>`;
  }).join("");
}

/**
 * 渲染调试台依赖缺失提示。
 *
 * 依赖缺失时表单按钮禁用，但这只是体验层提示；实际权限与状态仍由后端统一错误码兜底。
 */
function renderDependencyWarning(activeKeys, activeAliases) {
  if (activeKeys.length && activeAliases.length) {
    return "";
  }
  const missing = [];
  // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
  if (!activeKeys.length) {
    missing.push("可用 KEY");
  }
  if (!activeAliases.length) {
    missing.push("可用模型");
  }
  return `
    <div class="debug-console-warning" role="status">
      缺少 ${escapeHtml(missing.join(" / "))}。
    </div>
  `;
}

/**
 * 渲染 Virtual Key 选择项。
 *
 * option 只包含名称、环境和 key_prefix，不包含明文 Key 或 key_hash。
 */
function renderVirtualKeyOptions(keys, selectedValue) {
  if (!keys.length) {
    return `<option value="">暂无 active Key</option>`;
  }
  return keys.map((key) => {
    const selected = key.virtual_key_id === selectedValue ? " selected" : "";
    const label = `${key.name} / ${key.environment}`;
    // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
    return `<option value="${escapeAttribute(key.virtual_key_id)}"${selected}>${escapeHtml(label)}</option>`;
  }).join("");
}

/**
 * 渲染模型别名选择项。
 *
 * label 中展示环境，帮助开发者确认调试请求会进入 dev/test/prod 哪个别名集合。
 */
function renderModelAliasOptions(aliases, selectedValue) {
  if (!aliases.length) {
    return `<option value="default-chat">default-chat</option>`;
  }
  return aliases.map((alias) => {
    const selected = alias.alias === selectedValue ? " selected" : "";
    const label = `${alias.display_name ?? alias.alias} / ${alias.environment}`;
    return `<option value="${escapeAttribute(alias.alias)}"${selected}>${escapeHtml(label)}</option>`;
  }).join("");
}

/**
 * 渲染测试结果或错误。
 */
function renderDebugConsoleResult(state) {
  if (!state.result && !state.error) {
    return `<div class="empty-state debug-empty">等待发送调试请求。</div>`;
  }
  // 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
  if (state.error) {
    return renderDebugConsoleError(state.error);
  }
  return renderDebugConsoleSuccess(state.result);
}

/**
 * 渲染调试成功结果。
 *
 * 跳转按钮只携带 request_id，用于前端切换到调用日志；不会把 Prompt 或响应正文写入 URL。
 */
function renderDebugConsoleSuccess(result) {
  return `
    <div class="debug-result debug-console-result" aria-live="polite">
      <div class="debug-console-latency">响应时间 ${escapeHtml(result.latency_ms ?? 0)} ms</div>
      <div class="debug-console-metrics">
        ${renderMetric("输入令牌", result.usage?.input_tokens ?? 0)}
        ${renderMetric("输出令牌", result.usage?.output_tokens ?? 0)}
        ${renderMetric("总令牌", result.usage?.total_tokens ?? 0)}
        ${renderMetric("成本", `${result.cost?.amount ?? "0"} ${result.cost?.currency ?? "CNY"}`)}
      </div>
      <div class="debug-console-route">
        <span>模型：${escapeHtml(result.resolved_model ?? result.model_alias ?? "默认模型")}</span>
      </div>
      <pre><code>${escapeHtml(result.response_preview ?? "")}</code></pre>
      <button type="button" class="debug-log-link" data-debug-log-request-id="${escapeAttribute(result.request_id ?? "")}">
        查看调用日志
      </button>
    </div>
  `;
}

/**
 * 渲染单个调试指标。
 *
 * 指标值先转文本再转义，避免后端或测试桩返回异常字符影响布局。
 */
function renderMetric(label, value) {
  return `
    <div class="debug-console-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

/**
 * 渲染调试失败结果。
 *
 * 错误详情只来自统一错误体，不展示浏览器异常堆栈、Provider Key、Authorization 或请求正文。
 */
function renderDebugConsoleError(error) {
  return `
    <div class="debug-result debug-result-error" role="alert">
      <strong>测试请求未完成</strong>
      <p>${escapeHtml(error.message ?? "请检查 KEY 和模型配置后重试")}</p>
      <p class="suggestion">${escapeHtml(error.suggestion ?? "")}</p>
      ${renderDiagnosisCard(error.diagnosis)}
    </div>
  `;
}

/**
 * 渲染统一错误诊断卡片。
 *
 * 诊断字段来自后端安全 DTO，仍然逐项转义，避免上游模型或错误文案中的特殊字符破坏页面结构。
 */
function renderDiagnosisCard(diagnosis) {
  if (!diagnosis) {
    return "";
  }
  const retryableLabel = diagnosis.retryable ? "可重试" : "不可直接重试";
  return `
    <section class="diagnosis-card debug-diagnosis-card">
      <div class="diagnosis-card-title">
        <strong>错误诊断</strong>
        <span>${escapeHtml(diagnosis.severity ?? "warning")} / ${escapeHtml(retryableLabel)}</span>
      </div>
      <dl>
        ${renderDiagnosisPair("原因", diagnosis.reason)}
        ${renderDiagnosisPair("建议", diagnosis.suggestion)}
        ${renderDiagnosisPair("操作", diagnosis.action)}
      </dl>
    </section>
  `;
}

/**
 * 渲染诊断卡片里的单个字段，统一兜底空值并转义 HTML。
 */
function renderDiagnosisPair(label, value) {
  return `
    <dt>${escapeHtml(label)}</dt>
    <dd>${escapeHtml(value ?? "-")}</dd>
  `;
}

/**
 * HTML 文本转义。
 *
 * messages 编辑器、Provider 响应预览和错误信息均可能含特殊字符，必须在渲染前统一转义。
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
 * HTML 属性转义。
 *
 * request_id、virtual_key_id 和 alias 会进入 option/button 属性，需额外处理反引号。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
