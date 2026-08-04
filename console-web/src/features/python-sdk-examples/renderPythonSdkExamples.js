/**
 * 模块职责：渲染 Python 与 JavaScript/Node SDK 示例的分段视图、配置说明、命令和受控制品清单。
 * 维护边界：页面只展示后端返回的安全元数据，不接收 Virtual Key 明文，也不把浏览器直连描述为受支持能力。
 */

// SDK 语言顺序同时决定首屏默认项和键盘循环顺序；固定数组避免后端元数据改变可访问性交互结构。
export const SDK_LANGUAGES = Object.freeze(["python", "javascript"]);

// 每种语言的静态 UI 文案集中维护，动态 title/description 仍会经过 HTML 转义后展示。
const LANGUAGE_UI = Object.freeze({
  python: Object.freeze({
    label: "Python",
    defaultTitle: "Python SDK 示例",
    defaultDescription: "使用 OpenAI-compatible Python 客户端接入 MetaHub Gateway。",
    downloadLabel: "下载 Python 示例",
  }),
  javascript: Object.freeze({
    label: "JavaScript",
    defaultTitle: "JavaScript / Node SDK 示例",
    defaultDescription: "使用官方 OpenAI JavaScript SDK 在 Node.js 服务端接入 MetaHub Gateway。",
    downloadLabel: "下载 JavaScript 示例",
  }),
});

/**
 * 渲染 Python/JavaScript 双语言 SDK 示例页。
 * 两个 tabpanel 始终独立渲染，单个语言的等待或错误状态不会清空另一语言已经成功返回的元数据。
 */
export function renderSdkExamples({ pythonState, javascriptState, activeLanguage = "python" } = {}) {
  const normalizedLanguage = normalizeSdkLanguage(activeLanguage);
  return `
    <section class="panel sdk-examples-panel" aria-labelledby="sdk-examples-title">
      <div class="panel-heading sdk-examples-heading">
        <div>
          <h2 id="sdk-examples-title">SDK 示例</h2>
          <p>选择服务端语言，查看可运行示例并下载受控 ZIP 制品。</p>
        </div>
        ${renderLanguageTabs(normalizedLanguage)}
      </div>
      ${renderLanguageTabPanel("python", pythonState, normalizedLanguage)}
      ${renderLanguageTabPanel("javascript", javascriptState, normalizedLanguage)}
    </section>
  `;
}

/**
 * 保留 PRD-038 的 Python 单面板导出，兼容既有测试和只嵌入 Python 内容的旧调用方。
 * 新控制台入口应使用 renderSdkExamples，以获得完整的 tablist 与双语言状态隔离。
 */
export function renderPythonSdkExamples(state) {
  return `
    <section class="panel python-sdk-panel" aria-labelledby="python-sdk-title">
      ${renderLanguageContent("python", state)}
    </section>
  `;
}

/**
 * 提供 JavaScript 单面板渲染入口，便于专项测试完整验证 Node 元数据、转义和安全提示。
 */
export function renderJavascriptSdkExamples(state) {
  return `
    <section class="panel python-sdk-panel javascript-sdk-panel" aria-labelledby="javascript-sdk-title">
      ${renderLanguageContent("javascript", state)}
    </section>
  `;
}

/**
 * 计算 SDK tab 的键盘目标语言。
 * 左右方向键循环移动，Home/End 定位首尾，Enter/Space 激活当前项；其他按键返回 null 交还浏览器处理。
 */
export function sdkLanguageForKey(currentLanguage, key) {
  const current = normalizeSdkLanguage(currentLanguage);
  const currentIndex = SDK_LANGUAGES.indexOf(current);
  // 向右移动时在末尾循环到首项，保证窄屏换行后仍可按逻辑语言顺序导航。
  if (key === "ArrowRight") {
    return SDK_LANGUAGES[(currentIndex + 1) % SDK_LANGUAGES.length];
  }
  // 向左移动时使用加数组长度的方式循环，避免 JavaScript 负数取模产生无效索引。
  if (key === "ArrowLeft") {
    return SDK_LANGUAGES[(currentIndex - 1 + SDK_LANGUAGES.length) % SDK_LANGUAGES.length];
  }
  // Home/End 是 WAI-ARIA tabs 的首尾快捷键，不依赖当前 DOM 排列是否发生移动端换行。
  if (key === "Home") {
    return SDK_LANGUAGES[0];
  }
  if (key === "End") {
    return SDK_LANGUAGES[SDK_LANGUAGES.length - 1];
  }
  // Enter、标准空格和旧浏览器 Spacebar 都激活当前焦点项，不移动到另一种语言。
  if (key === "Enter" || key === " " || key === "Spacebar") {
    return current;
  }
  return null;
}

/**
 * 将任意输入归一为受支持语言；未知值固定回到 Python，确保首屏和登出重置行为可预测。
 */
export function normalizeSdkLanguage(language) {
  return SDK_LANGUAGES.includes(language) ? language : "python";
}

/**
 * 渲染唯一语言前缀的 tablist，aria-controls 与 tabpanel id 一一对应，并使用 roving tabindex。
 */
function renderLanguageTabs(activeLanguage) {
  return `
    <div class="sdk-language-tabs" role="tablist" aria-label="SDK 示例语言">
      ${SDK_LANGUAGES.map((language) => {
        const selected = language === activeLanguage;
        return `
          <button
            type="button"
            id="sdk-language-tab-${language}"
            role="tab"
            aria-selected="${selected}"
            aria-controls="sdk-language-panel-${language}"
            tabindex="${selected ? "0" : "-1"}"
            data-sdk-language-tab="${language}"
          >${LANGUAGE_UI[language].label}</button>
        `;
      }).join("")}
    </div>
  `;
}

/**
 * 渲染单个 tabpanel；inactive 面板使用原生 hidden，避免辅助技术同时朗读两套命令。
 */
function renderLanguageTabPanel(language, state, activeLanguage) {
  const selected = language === activeLanguage;
  return `
    <div
      id="sdk-language-panel-${language}"
      class="python-sdk-panel sdk-language-panel ${language === "javascript" ? "javascript-sdk-panel" : ""}"
      role="tabpanel"
      aria-labelledby="sdk-language-tab-${language}"
      tabindex="0"
      data-sdk-language-panel="${language}"
      ${selected ? "" : "hidden"}
    >
      ${renderLanguageContent(language, state)}
    </div>
  `;
}

/**
 * 按语言渲染等待、错误或成功内容；状态判断仅作用于当前 panel，不读取另一语言的数据。
 */
function renderLanguageContent(language, state) {
  if (!state) {
    return renderWaitingState(language);
  }
  // 控制面错误必须保留 request_id；页面不能回显响应原文、堆栈或其他语言的错误状态。
  if (state.state === "error") {
    return renderErrorState(language, state);
  }
  return renderSuccessState(language, state);
}

/**
 * 渲染一种语言的成功状态，标题、说明、命令和安全提示等后端动态字段全部统一转义。
 */
function renderSuccessState(language, state) {
  const ui = LANGUAGE_UI[language];
  return `
    <div class="panel-heading python-sdk-heading">
      <div>
        <h3 id="${language}-sdk-title">${escapeHtml(state.title ?? ui.defaultTitle)}</h3>
        <p>${escapeHtml(state.description ?? ui.defaultDescription)}</p>
      </div>
      <div class="python-sdk-actions">
        <span class="python-sdk-version">v${escapeHtml(state.version ?? "-")}</span>
        <button
          type="button"
          class="primary-action"
          data-download-sdk-language="${language}"
          ${language === "python" ? "data-download-python-sdk" : ""}
        >
          <span aria-hidden="true">↓</span>
          ${ui.downloadLabel}
        </button>
      </div>
    </div>
    ${language === "javascript" ? renderJavascriptSecurityNotice(state) : ""}
    <p
      class="python-sdk-download-feedback"
      data-sdk-feedback="${language}"
      ${language === "python" ? "data-python-sdk-feedback" : ""}
      aria-live="polite"
    ></p>
    ${renderOverview(language, state)}
    <div class="python-sdk-grid">
      ${renderEnvironmentVariables(language, state.environment_variables ?? [])}
      ${renderCommands(language, state.commands ?? [])}
    </div>
    <div class="python-sdk-grid python-sdk-details-grid">
      ${renderCapabilities(language, state.capabilities ?? [])}
      ${renderFiles(language, state.files ?? [])}
    </div>
  `;
}

/**
 * 渲染元数据尚未返回时的稳定占位，明确当前等待的语言接口且不暗示另一语言不可用。
 */
function renderWaitingState(language) {
  const ui = LANGUAGE_UI[language];
  return `
    <div class="panel-heading"><h3 id="${language}-sdk-title">${ui.defaultTitle}</h3></div>
    <div class="empty-state">等待 /developer/sdk-examples/${language} 响应。</div>
  `;
}

/**
 * 渲染语言专属统一错误状态，仅展示安全消息和 request_id，便于开发者关联控制面日志。
 */
function renderErrorState(language, state) {
  const ui = LANGUAGE_UI[language];
  return `
    <div class="panel-heading"><h3 id="${language}-sdk-title">${ui.defaultTitle}</h3></div>
    <div class="python-sdk-error" role="alert">
      <strong>${escapeHtml(state.message ?? `${ui.label} SDK 示例暂不可用`)}</strong>
      <span>request_id: ${escapeHtml(state.requestId ?? "-")}</span>
    </div>
  `;
}

/**
 * 渲染 Gateway、模型、运行时和仓库路径摘要；JavaScript 独有 runtime 不会污染 Python 契约。
 */
function renderOverview(language, state) {
  const items = language === "javascript"
    ? [
      ["Node 运行时", state.runtime],
      ["Gateway base_url", state.gateway_base_url],
      ["Chat 模型", state.chat_model_alias],
      ["仓库路径", state.repository_path],
    ]
    : [
      ["Gateway base_url", state.gateway_base_url],
      ["Chat 模型", state.chat_model_alias],
      ["Embeddings 模型", state.embedding_model_alias],
      ["仓库路径", state.repository_path],
    ];
  return `
    <dl class="python-sdk-overview" aria-label="${LANGUAGE_UI[language].label} 示例配置摘要">
      ${items.map(([label, value]) => renderOverviewItem(label, value)).join("")}
    </dl>
  `;
}

/**
 * 渲染单个摘要字段，空值统一显示短横线以保持布局稳定，长值只在自身容器内换行。
 */
function renderOverviewItem(label, value) {
  return `<div><dt>${escapeHtml(label)}</dt><dd><code>${escapeHtml(value ?? "-")}</code></dd></div>`;
}

/**
 * 渲染环境变量说明；Virtual Key 仅显示变量名、必填状态和安全描述，绝不显示真实值。
 */
function renderEnvironmentVariables(language, items) {
  return `
    <section class="python-sdk-section" aria-labelledby="${language}-sdk-env-title">
      <div class="compact-heading"><h4 id="${language}-sdk-env-title">环境变量</h4></div>
      <div class="python-sdk-list">
        ${items.map((item) => `
          <article>
            <div><code>${escapeHtml(item.name ?? "")}</code><span>${item.required ? "必填" : "可选"}</span></div>
            <p>${escapeHtml(item.description ?? "")}</p>
            ${item.default_value ? `<small>默认：${escapeHtml(item.default_value)}</small>` : ""}
          </article>
        `).join("")}
      </div>
    </section>
  `;
}

/**
 * 渲染安装、配置与运行命令；按钮只引用同面板 code 节点，不把命令复制到 data 属性。
 */
function renderCommands(language, items) {
  return `
    <section class="python-sdk-section" aria-labelledby="${language}-sdk-commands-title">
      <div class="compact-heading"><h4 id="${language}-sdk-commands-title">运行命令</h4></div>
      <div class="python-sdk-command-list">
        ${items.map((item, index) => {
          const commandId = `${language}-sdk-command-${index + 1}`;
          return `
            <article>
              <div><strong>${escapeHtml(item.label ?? "命令")}</strong><button type="button" data-copy-code-target="${commandId}">复制</button></div>
              <pre><code id="${commandId}">${escapeHtml(item.command ?? "")}</code></pre>
            </article>
          `;
        }).join("")}
      </div>
    </section>
  `;
}

/**
 * 渲染示例能力标签；标签来自后端固定枚举且只用于说明，不参与权限判断或浏览器功能开关。
 */
function renderCapabilities(language, items) {
  return `
    <section class="python-sdk-section" aria-labelledby="${language}-sdk-capabilities-title">
      <div class="compact-heading"><h4 id="${language}-sdk-capabilities-title">覆盖能力</h4></div>
      <ul class="python-sdk-capabilities">
        ${items.map((item) => `<li>${escapeHtml(capabilityLabel(item))}</li>`).join("")}
      </ul>
    </section>
  `;
}

/**
 * 把稳定英文能力码转换为中文说明；未知值保持转义后的原样，便于后端灰度新增能力。
 */
function capabilityLabel(value) {
  const labels = {
    chat: "Chat Completions",
    embeddings: "Embeddings",
    stream: "SSE 流式输出",
    timeout: "请求超时",
    retries: "有限重试",
    request_id: "请求 request_id",
    request_id_error_handling: "错误 request_id",
  };
  return labels[value] ?? String(value);
}

/**
 * 渲染 ZIP 受控文件清单，让开发者下载前可审计制品组成，路径文本不会成为可点击链接。
 */
function renderFiles(language, items) {
  return `
    <section class="python-sdk-section" aria-labelledby="${language}-sdk-files-title">
      <div class="compact-heading"><h4 id="${language}-sdk-files-title">制品文件</h4></div>
      <ul class="python-sdk-files">
        ${items.map((item) => `<li><code>${escapeHtml(item)}</code></li>`).join("")}
      </ul>
    </section>
  `;
}

/**
 * 渲染 JavaScript 专属安全边界。
 * 后端提示存在时优先展示；接口降级时仍用固定文案守住 Node 服务端和长期 Key 边界。
 */
function renderJavascriptSecurityNotice(state) {
  const notice =
    state.security_notice ??
    "不要在浏览器代码中保存或发送长期 Virtual Key，也不要让浏览器直接连接 Gateway。";
  return `
    <aside class="javascript-sdk-security" role="note" aria-label="JavaScript SDK 安全提示">
      <strong>仅用于 Node.js 服务端</strong>
      <p>${escapeHtml(notice)}</p>
    </aside>
  `;
}

/**
 * 转义动态 HTML 文本，避免后端元数据中的标签、属性引号或路径字符破坏控制台 DOM。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
