import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// Translate only the catalog's stock names; preserve custom published names.
const CATALOG_NAMES = Object.freeze({
  "Alibaba Cloud Model Studio": "阿里云百炼（通义千问）",
  "OpenAI Platform": "OpenAI（GPT）",
  "xAI API": "xAI（Grok）",
  "Anthropic API": "Anthropic（Claude）",
  "Tencent Hunyuan": "腾讯混元",
  "Moonshot AI": "Kimi（Moonshot）",
  "Volcengine Ark": "火山方舟（豆包）",
  "Baidu Qianfan": "百度千帆",
  "Zhipu AI": "智谱 AI",
  "Local OpenAI-compatible endpoint": "本地模型（OpenAI 兼容）",
  "OpenRouter migration source": "OpenRouter（迁移参考）",
  "SiliconFlow migration source": "硅基流动（迁移参考）",
});

// 自定义中转站属于组织自己的 Provider 配置，不是 SaaS 全局目录条目。
// 将它作为目录中的一个选择项，保持“添加模型服务”只有一个入口，同时不伪造目录签名或厂家元数据。
const CUSTOM_ENTRY = Object.freeze({
  entry_code: "custom_openai_compatible",
  display_name: "自定义中转站 / OpenAI 兼容",
  service_class: "custom",
  status: "active",
  instantiation_mode: "custom",
});

function catalogName(entry) {
  return CATALOG_NAMES[entry.display_name] ?? entry.display_name;
}

export function renderProviderCatalog(catalogState, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.PROVIDER_CATALOG_READ)) return "";
  if (!catalogState) return '<div class="empty-state">正在加载模型厂家...</div>';
  if (catalogState.state === "error") {
    return `<div class="empty-state error-state">${escapeHtml(catalogState.message ?? "模型厂家暂不可用，请稍后重试")}</div>`;
  }
  const entries = Array.isArray(catalogState.entries) ? catalogState.entries : [];
  // 迁移参考条目只服务于平台迁移工作流，不属于组织管理员的可接入服务。
  // 过滤发生在渲染边界，保留后端目录合同和平台侧迁移数据。
  const visibleEntries = [CUSTOM_ENTRY, ...entries.filter((entry) =>
    entry.entry_code !== CUSTOM_ENTRY.entry_code && entry.instantiation_mode !== "migration_only")];
  const canWrite = hasPermission(authContext, PERMISSIONS.PROVIDER_WRITE);
  return `
    <section class="provider-catalog" aria-labelledby="provider-catalog-title">
      <div class="provider-catalog-heading">
        <div>
          <h3 id="provider-catalog-title">选择模型厂家</h3>
          <p>选择要接入的模型服务。已有品牌直接选择；没有品牌的中转站请选择“自定义”。</p>
        </div>
      </div>
      ${renderFeedback(catalogState)}
      <label class="provider-catalog-selection"><span>模型厂家</span>
        <select data-provider-catalog-selection><option value="">请选择</option>
          ${visibleEntries.map((entry) => `<option value="${escapeAttribute(entry.entry_code)}"${entry.entry_code === catalogState.selectedEntryCode ? " selected" : ""}>${escapeHtml(catalogName(entry))}</option>`).join("")}
        </select></label>
      <div class="provider-catalog-list">
        ${visibleEntries.map((entry) => renderEntry(entry, catalogState, canWrite)).join("") || '<div class="empty-state">当前筛选下没有目录条目。</div>'}
      </div>
    </section>`;
}

function renderEntry(entry, state, canWrite) {
  if (entry.entry_code === CUSTOM_ENTRY.entry_code) {
    return `<article class="provider-catalog-entry provider-catalog-custom-entry" data-provider-catalog-entry="${escapeAttribute(entry.entry_code)}"${entry.entry_code === state.selectedEntryCode ? "" : " hidden"}>
      <header><div><strong>${escapeHtml(catalogName(entry))}</strong><span>组织自定义接入</span></div><mark class="status-mark status-active">可用</mark></header>
      ${renderCustomProviderForm(canWrite, state.customDraft)}
    </article>`;
  }
  return `
    <article class="provider-catalog-entry" data-provider-catalog-entry="${escapeAttribute(entry.entry_code)}"${entry.entry_code === state.selectedEntryCode ? "" : " hidden"}>
      <header>
        <div>
          <strong>${escapeHtml(catalogName(entry))}</strong>
          <span>可直接接入</span>
        </div>
        <mark class="status-mark status-${escapeAttribute(entry.status)}">${escapeHtml(({ active: "可接入", deprecated: "已弃用", retired: "已停用" })[entry.status] ?? entry.status)}</mark>
      </header>
      ${renderEntryAction(entry, state.version, canWrite)}
    </article>`;
}

function renderCustomProviderForm(canWrite, draft = {}) {
  if (!canWrite) return '<div class="provider-catalog-next"><span>当前身份只有目录查看权限。</span></div>';
  const name = escapeAttribute(draft?.name ?? "");
  const baseUrl = escapeAttribute(draft?.baseUrl ?? "");
  return `<form class="create-provider-form provider-catalog-custom-form" id="create-provider-form">
    <label><span>服务名称</span><input name="name" maxlength="120" required value="${name}" placeholder="例如：公司中转站" /></label>
    <label><span>服务地址</span><input name="baseUrl" type="url" required value="${baseUrl}" placeholder="https://api.example.com/v1" /></label>
    <p class="provider-catalog-help">不兼容 OpenAI 接口时请联系管理员。</p>
    <div class="provider-actions"><button type="submit">添加模型服务</button></div>
  </form>`;
}

function renderEntryAction(entry, version, canWrite) {
  if (entry.instantiation_mode === "local_endpoint") {
    return '<div class="provider-catalog-next"><button type="button" data-console-nav="local-models" data-console-route="workspace.gateway.private-endpoints" data-console-target="local-models">前往本地模型</button></div>';
  }
  if (!canWrite) return '<div class="provider-catalog-next"><span>当前身份拥有目录只读权限。</span></div>';
  const endpoints = Array.isArray(entry.endpoints) ? entry.endpoints : [];
  const capabilities = Array.isArray(entry.capabilities) ? entry.capabilities : [];
  const defaultRegion = endpoints[0]?.region_code ?? "default";
  return `<form class="provider-catalog-create" data-provider-catalog-create-form autocomplete="off">
    <input type="hidden" name="entryCode" value="${escapeAttribute(entry.entry_code)}" />
    <input type="hidden" name="version" value="${escapeAttribute(version)}" />
    <input type="hidden" name="regionCode" value="${escapeAttribute(defaultRegion)}" />
    <input type="hidden" name="name" value="${escapeAttribute(catalogName(entry))}" />
    ${capabilities.map((capability) => `<input type="hidden" name="capabilityCodes" value="${escapeAttribute(capability.code)}" />`).join("")}
    <input type="hidden" name="confirmed" value="on" />
    <button type="submit">添加并配置密钥</button>
  </form>`;
}

function renderFeedback(state) {
  if (state.actionError) {
    return `<div class="provider-feedback-error" role="alert"><strong>${escapeHtml(state.actionError.message ?? "操作失败")}</strong></div>`;
  }
  return state.feedback ? `<div class="provider-feedback" role="status">${escapeHtml(state.feedback)}</div>` : "";
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
