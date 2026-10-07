/**
 * 模块职责：渲染 Python 与 JavaScript/Node SDK 示例的分段视图、配置说明、命令和受控制品清单。
 * 维护边界：页面只展示后端返回的安全元数据，不接收 Virtual Key 明文，也不把浏览器直连描述为受支持能力。
 */

// SDK 语言顺序同时决定首屏默认项和键盘循环顺序；固定数组避免后端元数据改变可访问性交互结构。
import { icon } from "../public-home/icons.js";
import { renderWorkspaceConnectionStatus } from "../workspace-navigation/renderWorkspaceNavigation.js";
import { gatewayConnectionAddresses as toolConnectionAddresses } from "../../lib/gatewayConnection.js";

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
export function renderSdkExamples({ pythonState, javascriptState, activeLanguage = "python", workspaceTaskHomeState, i18n } = {}) {
  const normalizedLanguage = normalizeSdkLanguage(activeLanguage);
  return `
    <section class="panel sdk-examples-panel" aria-labelledby="sdk-examples-title">
      <div class="panel-heading sdk-examples-heading">
        <div>
          <h2 id="sdk-examples-title">接入信息</h2>
        </div>
      </div>
      ${renderWorkspaceConnectionStatus(workspaceTaskHomeState, "workspace.developer.sdk", i18n)}
      ${renderConnectionDetails(pythonState?.state === "ok" ? pythonState : javascriptState?.state === "ok" ? javascriptState : pythonState ?? javascriptState)}
      <details class="sdk-developer-details"><summary>开发者 SDK 示例</summary>
      ${renderLanguageTabs(normalizedLanguage)}
      ${renderLanguageTabPanel("python", pythonState, normalizedLanguage)}
      ${renderLanguageTabPanel("javascript", javascriptState, normalizedLanguage)}
      </details>
    </section>
  `;
}

function renderConnectionDetails(state) {
  if (!state) return '<div role="status">正在加载接入信息...</div>';
  if (state.state === "error") return '<div role="alert">接入信息加载失败。<button type="button" data-resource-retry>重试</button></div>';
  if (state.state !== "ok") return '<div role="status">正在加载接入信息...</div>';
  const addresses = toolConnectionAddresses(state.gateway_base_url);
  if (!addresses) return '<div role="alert">网关地址尚未正确配置，请联系管理员。</div>';
  const serviceAddress = addresses.service;
  const localOnly = ["localhost", "127.0.0.1", "[::1]"].includes(new URL(addresses.openai).hostname);
  const modelConfigured = state.chat_model_configured === true && Boolean(state.chat_model_alias);
  // 交付说明不冒充任一工具的导入格式，不读取或持久化员工明文 KEY。
  const handoff = [
    "MetaHub 工具接入说明",
    `统一网关网址：${serviceAddress}`,
    "个人 KEY：粘贴你在组织空间创建的个人 KEY",
    modelConfigured ? `工具要求模型名称时填写：${state.chat_model_alias}` : "模型路由尚未配置，请联系管理员。",
    "只支持开放自定义模型接口的工具。",
    ...(localOnly ? ["此地址仅供本机试用，其他电脑需要管理员提供可访问的网关地址。"] : []),
  ].join("\n");
  return `<div class="connection-quickstart">
    <h3>员工接入</h3>
    <p>使用统一网关地址和个人 KEY；模型名称仅在工具要求时填写。</p>
    <p class="tool-connection-simple"><strong>员工不用填写 MetaHub 环境变量。</strong>在工具的“自定义模型 / API”设置中粘贴地址和 KEY。</p>
    <ol class="tool-connection-contract" aria-label="员工接入信息">
      <li><strong>网关服务地址</strong><span>统一地址</span></li>
      <li><strong>个人 KEY</strong><span>成员在组织空间创建</span></li>
      <li><strong>模型名称（按需）</strong><span>${modelConfigured ? `<code>${escapeHtml(state.chat_model_alias)}</code>` : "管理员配置后显示"}</span></li>
    </ol>
  </div>
  <dl class="tool-connection-details">
    <div class="connection-primary-value"><dt>统一 MetaHub 网关网址</dt><dd><code id="tool-service-address">${escapeHtml(serviceAddress)}</code>
      <button type="button" data-copy-code-target="tool-service-address" title="复制统一网关网址" aria-label="复制统一网关网址">${icon("copy")}</button><span class="quickstart-copy-feedback" role="status"></span>
      <small>按工具需要选择协议地址。</small></dd></div>
    <div><dt>个人 KEY</dt><dd>粘贴成员的个人 KEY</dd></div>
    <div class="quickstart-example"><dt>模型名称（按需）</dt><dd>${modelConfigured ? `<code id="tool-model-name">${escapeHtml(state.chat_model_alias)}</code>
      <button type="button" data-copy-code-target="tool-model-name" title="复制模型名称" aria-label="复制模型名称">${icon("copy")}</button><span class="quickstart-copy-feedback" role="status"></span>` : '<span>尚未配置可用模型路由，请先联系管理员。</span>'}</dd></div>
  </dl>${localOnly ? '<p class="tool-local-notice">当前网关地址仅供本机试用。给其他设备上的员工使用前，需部署可访问的网关地址。</p>' : ''}
  <div class="tool-handoff quickstart-example">
    <button class="tool-handoff-copy" type="button" data-copy-code-target="tool-connection-instructions">复制接入说明给员工</button>
    <span class="quickstart-copy-feedback" role="status"></span>
    <p>说明不含 KEY，请将该员工的个人 KEY 单独交付。</p>
    <details class="tool-handoff-preview"><summary>查看接入说明</summary><pre><code id="tool-connection-instructions">${escapeHtml(handoff)}</code></pre></details>
  </div>
  ${renderToolGuides(addresses, modelConfigured ? state.chat_model_alias : null)}`;
}

/** 工具版本的支持状态独立于路由配置状态，不能用配置完成冒充客户端验收通过。 */
function renderToolGuides(addresses, model) {
  const guides = [
    {
      id: "openai-chat", name: "自定义 OpenAI 接口", status: "接口已支持", kind: "supported",
      text: "在工具的模型设置中选择 OpenAI 兼容接口，将下方地址填入 Base URL，个人 KEY 填入 API Key。需要模型名称时填写上方名称，或使用工具提供的读取模型列表功能。",
      detail: "当前提供 Chat Completions、流式文本响应和模型列表接口。具体工具的编程、文件操作等 Agent 功能仍需单独验证。",
    },
    {
      id: "cursor", name: "Cursor", status: "需验证具体功能", kind: "conditional",
      text: "若当前版本提供自定义 OpenAI 地址、API Key 和模型设置，可复制下方地址，并填写员工个人 KEY 与上方模型名称。",
      detail: "自定义模型能回复消息，不代表 Agent、Tab 补全等功能全部可用；这些功能尚未完成 MetaHub 联调。",
    },
    {
      id: "codex", name: "Codex", status: "客户端待联调", kind: "conditional",
      text: "如果当前 Codex 版本提供自定义 Provider，请使用下方网关地址、同一把个人 KEY 和模型名称。Codex 的配置文件示例只是把这三项保存到它自己的设置中，KEY 由成员在组织空间创建。",
      detail: "网关已提供 Responses 接口，Codex 完整工作流尚未验收，部分高级 Agent 功能仍有暂未支持项。ChatGPT 网页聊天不使用这份配置。",
      documentation: "https://developers.openai.com/codex/config-reference/",
      configuration: model ? [
        `model = ${JSON.stringify(model)}`,
        'model_provider = "metahub"',
        "",
        "[model_providers.metahub]",
        'name = "MetaHub"',
        `base_url = ${JSON.stringify(addresses.openai)}`,
        'env_key = "METAHUB_API_KEY"',
        'wire_api = "responses"',
      ].join("\n") : null,
      configNote: "这是 Codex 的配置方式；员工只需要网关地址、个人 KEY 和模型三项信息。已有配置请合并相应字段。",
    },
    {
      id: "claude-code", name: "Claude Code CLI", status: "客户端待联调", kind: "conditional",
      text: "如果当前 Claude Code 版本提供自定义网关，请使用下方地址、同一把个人 KEY 和模型名称。配置文件示例只是把这三项保存到 Claude Code 自己的设置中，替换 KEY 占位文字后重启即可。",
      detail: "网关已提供 Messages 接口，Claude Code 完整工作流尚未验收，部分高级 Agent 功能仍有暂未支持项。VS Code 扩展的凭据入口不同，请按官方连接说明配置。",
      documentation: "https://code.claude.com/docs/en/llm-gateway-connect",
      configuration: model ? JSON.stringify({ env: {
        ANTHROPIC_BASE_URL: addresses.anthropic,
        ANTHROPIC_AUTH_TOKEN: "粘贴你的个人 KEY",
        ANTHROPIC_MODEL: model,
      } }, null, 2) : null,
      configNote: "只替换 KEY 占位文字即可；这是 Claude Code 保存凭据的方式，不代表还要申请另一把 KEY。请使用个人设置文件，已有设置合并 env 字段。",
    },
    {
      id: "workbuddy", name: "WorkBuddy", status: "待确认版本支持", kind: "conditional",
      text: "需先确认当前版本是否开放自定义模型服务。若提供 OpenAI Chat Completions 接入，可复制下方地址，并填写个人 KEY 与上方模型名称。",
      detail: "MetaHub 尚未完成此工具的 Agent 联调，不能保证所有功能可用。",
    },
    {
      id: "deepseek-harness", name: "DeepSeek Harness / 其他 Agent 工具", status: "待确认版本支持", kind: "conditional",
      text: "不同工具和版本的配置格式可能不同。若支持自定义 OpenAI Chat Completions 接口，可使用同一个网关地址与个人 KEY。",
      detail: "工具调用、执行结果回传和流式事件需要逐项验证。工具未开放自定义接口时，单靠网关无法接入。",
    },
  ];
  return `<section class="tool-compatibility" aria-labelledby="tool-compatibility-title">
    <div class="compact-heading"><h3 id="tool-compatibility-title">选择你使用的工具</h3><span>展开查看接入要求</span></div>
    <p class="tool-compatibility-note">支持自定义模型接口的工具可使用上面的地址和个人 KEY。</p>
    <div class="tool-compatibility-grid">${guides.map((guide) => `<details class="tool-guide" name="metahub-tool-guide" data-tool-guide="${guide.id}">
      <summary><strong>${guide.name}</strong><mark class="compatibility-${guide.kind}">${guide.status}</mark></summary>
      <div class="tool-guide-content"><p>${guide.text}</p>
        ${renderToolCopyValue(`tool-${guide.id}-address`, "填入工具的 Base URL", guide.id === "claude-code" ? addresses.anthropic : addresses.openai)}
        ${guide.configuration ? `${renderToolCopyValue(`tool-${guide.id}-config`, "配置文件示例", guide.configuration, true)}<p>${guide.configNote}</p>` : ""}
        ${!model ? '<p>管理员尚未配置可用模型路由，配置完成后再接入。</p>' : ""}
        <p>${guide.detail}${guide.documentation ? ` <a href="${guide.documentation}" target="_blank" rel="noreferrer">官方配置说明</a>` : ""}</p>
      </div>
    </details>`).join("")}</div>
  </section>`;
}

/** 复用控制台复制反馈，模板只有公开连接信息与占位值，绝不取成员 KEY。 */
function renderToolCopyValue(id, label, value, multiline = false) {
  const code = `<code id="${id}">${escapeHtml(value)}</code>`;
  return `<div class="quickstart-example tool-guide-copy">
    <div class="tool-guide-copy-heading"><span>${label}</span><button type="button" data-copy-code-target="${id}" aria-label="复制${label}">复制</button></div>
    ${multiline ? `<pre>${code}</pre>` : code}
    <span class="quickstart-copy-feedback" role="status"></span>
  </div>`;
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
    <div class="empty-state" role="status">正在加载 ${ui.label} 示例...</div>
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
    <details class="python-sdk-section sdk-advanced-environment"><summary><h4 id="${language}-sdk-env-title">可选：程序化客户端配置</h4></summary>
      <div class="python-sdk-list">
        ${items.map((item) => `
          <article>
            <div><code>${escapeHtml(item.name ?? "")}</code><span>${item.required ? "必填" : "可选"}</span></div>
            <p>${escapeHtml(item.description ?? "")}</p>
            ${item.default_value ? `<small>默认：${escapeHtml(item.default_value)}</small>` : ""}
          </article>
        `).join("")}
      </div>
    </details>
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
