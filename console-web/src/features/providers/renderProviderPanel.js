import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { renderProviderCatalog } from "./renderProviderCatalog.js";
import { renderWorkspaceProviderEngineering } from "../workspace-provider-engineering/renderWorkspaceProviderEngineering.js";

// Provider 面板的类型标签用于控制台展示，Provider Key 明文永远不应进入前端列表状态。

const PROVIDER_LABELS = {
  openai_compatible: "OpenAI 兼容",
  zhipu: "智谱",
  dashscope: "通义千问",
  wenxin: "文心",
  volcengine: "火山",
  hunyuan: "混元",
  deepseek: "DeepSeek",
  custom: "自定义",
  migration_compatible: "迁移兼容（聚合入口）",
};

// 认证方式标签只展示 Provider 接入类型，不暴露任何上游密钥或 Authorization 内容。
const AUTH_LABELS = {
  none: "无认证",
  bearer: "API KEY（Bearer）",
  custom_header: "自定义 Header",
};

// Provider 类型选项限定 MVP 支持范围，新增厂商必须同时补后端白名单和测试。
const PROVIDER_OPTIONS = [
  "openai_compatible",
  "zhipu",
  "dashscope",
  "wenxin",
  "volcengine",
  "hunyuan",
  "deepseek",
  "custom",
  "migration_compatible",
];

/**
 * 渲染 Provider 管理面板。
 *
 * 控制台只对 admin 显示该区域；这只是交互层隐藏，后端 Provider API 仍会强制校验
 * admin 权限和当前 tenant_id。
 */
export function renderProviderPanel(providersState, authContext) {
  const canReadProviders = hasPermission(authContext, PERMISSIONS.PROVIDER_READ);
  const canReadCatalog = hasPermission(authContext, PERMISSIONS.PROVIDER_CATALOG_READ);
  if (!canReadProviders && !canReadCatalog) {
    return "";
  }
  const catalogState = providersState?.providerCatalog ?? null;
  const activeTab = canReadProviders ? catalogState?.activeTab ?? "providers" : "catalog";
  const tabs = canReadCatalog && canReadProviders ? `
    <div class="provider-tabs" aria-label="模型服务操作">
      ${activeTab === "catalog"
        ? '<button type="button" data-provider-tab="providers">返回已接入服务</button>'
        : '<button type="button" data-provider-tab="catalog">添加模型服务</button>'}
    </div>` : "";
  if (activeTab === "catalog" && canReadCatalog) {
    return `
      <div class="panel-heading providers-heading"><div><h2 id="providers-title">添加模型服务</h2></div></div>
      ${tabs}
      ${renderProviderCatalog(catalogState, authContext)}
    `;
  }
  if (!canReadProviders) return "";
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  if (!providersState) {
    return `
      <div class="panel-heading">
        <h2 id="providers-title">模型服务</h2>
        <p>正在加载模型服务...</p>
      </div>
      <div class="empty-state" role="status">正在加载...</div>
    `;
  }
  // Provider 判断会影响上游归因和密钥健康，错误路径必须保持脱敏。
  if (providersState.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="providers-title">模型服务</h2>
        <p>模型服务暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(providersState.message)} request_id: ${escapeHtml(providersState.requestId ?? "-")}
        <button type="button" data-resource-retry>重试</button>
      </div>
    `;
  }

  const providers = providersState.providers ?? [];
  const currentProviders = providers.filter((provider) => provider.status !== "disabled");
  const disabledProviders = providers.filter((provider) => provider.status === "disabled");
  const providerKeysByProviderId = providersState.providerKeysByProviderId ?? {};
  const providerEngineering = providersState.providerEngineering ?? null;
  const canReadEngineering = hasPermission(
    authContext,
    PERMISSIONS.WORKSPACE_PROVIDER_ERROR_OVERRIDE_READ,
  );
  const selectedEngineeringProvider = providers.find(
    (provider) => canReadEngineering
      && provider.provider_id === providerEngineering?.providerId,
  ) ?? null;
  const canWrite = hasPermission(authContext, PERMISSIONS.PROVIDER_WRITE);
  const renderRows = (items) => items.map((provider) => renderProviderRow(
    provider,
    providerKeysByProviderId[provider.provider_id],
    canWrite,
    canReadEngineering,
    provider.provider_id === selectedEngineeringProvider?.provider_id,
    provider.provider_id === providersState.setupProviderId,
    hasPermission(authContext, PERMISSIONS.MODEL_READ),
  )).join("");
  const disabledOpen = disabledProviders.some((provider) =>
    provider.provider_id === providersState.setupProviderId
    || provider.provider_id === selectedEngineeringProvider?.provider_id
    || providerKeysByProviderId[provider.provider_id]?.state === "error");
  const tableHeading = `<div class="provider-row provider-head" role="row">
    <span>模型服务</span><span>类型</span><span>Base URL</span>
    <span>健康</span><span>最近检查</span><span>操作</span>
  </div>`;
  return `
    <div class="panel-heading providers-heading">
      <div>
        <h2 id="providers-title">模型服务</h2>
      </div>
      <span class="providers-count">${providers.length} 个服务</span>
    </div>
    ${tabs}
    ${providersState.feedback ? `<p class="provider-feedback" role="status">${escapeHtml(providersState.feedback)}</p>` : ""}
    ${renderActionError(providersState.actionError)}
    ${renderTestResult(providersState.lastTestResult)}
    ${canWrite ? `<details class="provider-custom-setup"><summary>自定义接入（中转站 / OpenAI 兼容地址）</summary><p class="provider-custom-help">可直接填写任意自定义中转站或兼容接口，不限于下面的品牌目录。</p><form class="create-provider-form" id="create-provider-form">
      <h3>自定义模型服务</h3>
      <label>
        <span>服务名称</span>
        <input name="name" maxlength="120" required placeholder="公司模型服务" />
      </label>
      <label>
        <span>服务地址</span>
        <input name="baseUrl" type="url" required placeholder="https://api.example.com/v1" />
      </label>
      <details class="provider-custom-advanced"><summary>高级设置</summary><div class="model-advanced-fields">
      <label>
        <span>内部标识（可选）</span>
        <input name="providerCode" maxlength="64" pattern="[a-z][a-z0-9_]{1,63}" title="小写字母开头，2-64 位小写字母、数字或下划线" placeholder="自动生成" />
      </label>
      <label>
        <span>接口协议</span>
        <select name="providerType">
          ${PROVIDER_OPTIONS.map((type) => renderOption(type, PROVIDER_LABELS[type], "openai_compatible")).join("")}
        </select>
      </label>
      <label>
        <span>认证方式</span>
        <select name="authType">
          ${renderOption("bearer", AUTH_LABELS.bearer, "bearer")}
          ${renderOption("none", AUTH_LABELS.none, "bearer")}
          ${renderOption("custom_header", AUTH_LABELS.custom_header, "bearer")}
        </select>
      </label>
      <label class="provider-description-field">
        <span>备注（可选）</span>
        <input name="description" maxlength="240" placeholder="生产候选、测试环境或本地服务" />
      </label>
      </div></details>
      <div class="provider-actions">
        <button type="submit">添加模型服务</button>
        <span class="provider-feedback" role="status">${escapeHtml(providersState.feedback ?? "")}</span>
      </div>
    </form></details>` : ""}
    <div class="providers-grid" role="table" aria-label="已接入模型服务">
      ${tableHeading}
      ${
        currentProviders.length ? renderRows(currentProviders)
          : disabledProviders.length ? '<p class="empty-state">暂无启用中的模型服务。</p>' : renderEmptyRow()
      }
    </div>
    ${disabledProviders.length ? `<details class="provider-disabled-services"${disabledOpen ? " open" : ""}>
      <summary>已停用服务 (${disabledProviders.length})</summary>
      <div class="providers-grid" role="table" aria-label="已停用模型服务">${tableHeading}${renderRows(disabledProviders)}</div>
    </details>` : ""}
    ${canReadEngineering ? `<div data-workspace-provider-engineering-host>
      ${selectedEngineeringProvider
        ? renderWorkspaceProviderEngineering(providerEngineering, selectedEngineeringProvider)
        : ""}
    </div>` : ""}
  `;
}

/**
 * 渲染单个 Provider 行。
 *
 * 行内只展示 Endpoint 元数据和健康状态；启用/禁用通过隐藏字段提交目标状态，后端
 * 负责二次校验。
 */
function renderProviderRow(provider, keyState, canWrite, canReadEngineering, engineeringSelected, setupSelected, canReadModels) {
  const nextStatus = provider.status === "active" ? "disabled" : "active";
  const statusLabel = provider.status === "active" ? "禁用" : "启用";
  const activeKeyCount = (keyState?.provider_keys ?? []).filter((key) => key.status === "active").length;
  const missingKey = provider.auth_type !== "none" && keyState?.state === "ok"
    && activeKeyCount === 0;
  const canTest = provider.status === "active" && (provider.auth_type === "none"
    || (keyState?.state === "ok" && (keyState.provider_keys ?? []).some((key) => key.status === "active")));
  const needsTest = canTest && provider.health_status !== "healthy";
  return `
    <div class="provider-row" role="row">
      <span class="provider-identity">
        <strong>${escapeHtml(provider.name)}</strong>
        <em>
          ${provider.status === "active" ? "已启用" : "已停用"}
        </em>
        ${provider.auth_type !== "none" && keyState?.state === "ok"
          ? `<small>${activeKeyCount} 把可用 KEY</small>` : ""}
      </span>
      <span>${escapeHtml(PROVIDER_LABELS[provider.provider_type] ?? provider.provider_type)}</span>
      <code title="${escapeAttribute(provider.base_url)}">${escapeHtml(provider.base_url)}</code>
      <span>
        <mark class="status-mark status-${escapeHtml(provider.health_status)}">
          ${missingKey ? "待配置密钥" : escapeHtml(({ healthy: "可用", unhealthy: "异常", degraded: "需关注", unknown: "未检查" })[provider.health_status] ?? provider.health_status)}
        </mark>
      </span>
      <span title="${escapeAttribute(provider.last_test_error || "")}">
        ${escapeHtml(formatDateTime(provider.last_tested_at))}
      </span>
      <div class="provider-row-actions">
        ${canWrite && provider.auth_type !== "none" ? `<button type="button" class="${needsTest ? "" : "provider-setup-action"}"
          data-provider-key-open="${escapeAttribute(provider.provider_id)}"
          aria-controls="provider-keypool-${escapeAttribute(provider.provider_id)}"
          aria-expanded="${setupSelected ? "true" : "false"}">${missingKey ? "配置 API KEY" : "管理 API KEY"}</button>` : ""}
        ${canWrite && needsTest ? `<form data-provider-test-form data-provider-id="${escapeAttribute(provider.provider_id)}"><button class="provider-setup-action" type="submit">测试连接</button></form>` : ""}
        ${canReadModels && canTest && provider.health_status === "healthy" ? '<a href="#workspace.gateway.models">配置可用模型</a>' : ""}
        ${canWrite || canReadEngineering ? `<details class="provider-maintenance"${engineeringSelected ? " open" : ""}><summary>管理服务</summary><div class="provider-maintenance-actions">
        ${canReadEngineering ? `<button type="button" data-workspace-provider-engineering-open="${escapeAttribute(provider.provider_id)}"
          aria-expanded="${engineeringSelected ? "true" : "false"}">兼容与错误规则</button>` : ""}
        ${canWrite ? `${!needsTest ? `<form data-provider-test-form data-provider-id="${escapeAttribute(provider.provider_id)}">
          <button type="submit">测试连接</button>
        </form>` : ""}
        <form data-provider-status-form data-provider-id="${escapeAttribute(provider.provider_id)}">
          <input type="hidden" name="status" value="${escapeAttribute(nextStatus)}" />
          <button type="submit">${statusLabel}</button>
        </form>` : ""}
        </div></details>` : ""}
      </div>
    </div>
    ${canWrite && provider.auth_type !== "none" && keyState?.state === "error" ? renderProviderKeyError(keyState) : ""}
    ${renderProviderKeypool(provider, keyState, canWrite, setupSelected, canReadModels)}
  `;
}

/**
 * 渲染 Provider 的 KeyPool 区域。
 *
 * 新增 Key 表单使用 password 输入框；已有 Key 只展示 key_prefix/fingerprint 等
 * 脱敏信息，不回显 Provider Key 明文。
 */
function renderProviderKeypool(provider, keyState, canWrite, setupSelected, canReadModels) {
  if (provider.auth_type === "none") return "";
  const keys = keyState?.provider_keys ?? [];
  return `
    <details class="provider-keypool"${canWrite ? " data-provider-key-controlled" : ""} id="provider-keypool-${escapeAttribute(provider.provider_id)}" data-provider-keypool="${escapeAttribute(provider.provider_id)}"${setupSelected ? " open" : ""}>
      <summary class="provider-keypool-heading">
        <strong>${canWrite ? "收起密钥配置" : `${escapeHtml(provider.name)} · API KEY`}</strong>
        <span>${keyState?.state === "ok" ? `${keys.length} 个 Key` : keyState?.state === "error" ? "加载失败" : "加载中"}</span>
      </summary>
      ${canWrite ? `<form
        class="provider-key-form"
        data-provider-key-create-form
        data-provider-id="${escapeAttribute(provider.provider_id)}"
      >
        <label>
          <span>厂家 API KEY</span>
          <input name="plainValue" type="password" required autocomplete="new-password" placeholder="sk-..." />
        </label>
        <details class="provider-key-weight"><summary>高级设置</summary><div class="provider-key-options"><label>
          <span>名称</span>
          <input name="name" maxlength="80" required value="主密钥" />
        </label><label>
          <span>权重</span>
          <input name="weight" type="number" min="1" max="10000" value="100" />
        </label></div></details>
        <button type="submit">保存密钥</button>
      </form>` : ""}
      ${keyState?.state === "error" ? (canWrite ? "" : renderProviderKeyError(keyState)) : keyState?.state === "ok" ? renderProviderKeyRows(keys, canWrite) : '<p role="status">正在加载密钥...</p>'}
      ${canReadModels && provider.status === "active" && provider.health_status === "healthy" && keys.some((key) => key.status === "active") ? '<a class="console-flow-next" href="#workspace.gateway.models">下一步：配置可用模型</a>' : ""}
    </details>
  `;
}

/**
 * 渲染 Provider Key 列表。
 *
 * 空列表使用独立提示，避免用户误以为 KeyPool 请求失败。
 */
function renderProviderKeyRows(keys, canWrite) {
  if (!keys.length) {
    // Provider 判断会影响上游归因和密钥健康，错误路径必须保持脱敏。
    return "";
  }
  return `
    <div class="provider-key-grid" aria-label="Provider KeyPool">
      <div class="provider-key-row provider-key-head">
        <span>Key</span>
        <span>状态</span>
        <span>权重</span>
        <span>最近使用</span>
        <span>最近错误</span>
        <span>操作</span>
      </div>
      ${keys.map((key) => renderProviderKeyRow(key, canWrite)).join("")}
    </div>
  `;
}

/**
 * 渲染单条 Provider Key。
 *
 * 状态切换只提交目标状态，不提交任何明文密钥字段。
 */
function renderProviderKeyRow(providerKey, canWrite) {
  const nextStatus = providerKey.status === "active" ? "disabled" : "active";
  const actionLabel = providerKey.status === "active" ? "禁用" : "启用";
  return `
    <div class="provider-key-row">
      <span class="provider-key-name">
        <strong>${escapeHtml(providerKey.name)}</strong>
        <em>${escapeHtml(providerKey.key_prefix || "-")}</em>
      </span>
      <span>
        <mark class="status-mark status-${escapeHtml(providerKey.status)}">${escapeHtml(({ active: "可用", disabled: "已停用" })[providerKey.status] ?? "不可用")}</mark>
      </span>
      <span>${escapeHtml(providerKey.weight)}</span>
      <span>${escapeHtml(formatDateTime(providerKey.last_used_at))}</span>
      <span title="${escapeAttribute(providerKey.last_error_message || "")}">
        ${escapeHtml(providerKey.last_error_code || "-")}
      </span>
      ${canWrite ? `<form
        class="provider-key-action"
        data-provider-key-status-form
        data-provider-key-id="${escapeAttribute(providerKey.provider_key_id)}"
      >
        <input type="hidden" name="status" value="${escapeAttribute(nextStatus)}" />
        <button type="submit">${actionLabel}</button>
      </form>` : ""}
    </div>
  `;
}

/**
 * 渲染 KeyPool 读取失败态。
 *
 * 保留 request_id，方便从控制台直接定位控制面错误日志。
 */
function renderProviderKeyError(error) {
  return `
    <div class="provider-key-empty error-state">
      密钥列表暂不可用 request_id: ${escapeHtml(error.requestId ?? "-")}
      <button type="button" data-resource-retry>重试</button>
    </div>
  `;
}

/**
 * 渲染 Provider 操作错误。
 *
 * 该错误来自创建、启停或连通性测试等动作，统一通过 role=alert 暴露给辅助技术。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="provider-action-error" role="alert">
      ${escapeHtml(error.message ?? "Provider 操作失败")} request_id: ${escapeHtml(error.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染 Provider 连通性测试结果。
 *
 * 结果只包含 HTTP 状态、延迟和安全错误摘要，不展示 Provider Key 或请求头。
 */
function renderTestResult(result) {
  if (!result) {
    return "";
  }
  const successful = result.status === "passed" || result.status === "success";
  const title = successful ? "连接成功" : "连接未通过";
  const guidance = successful ? "模型服务已接通，可以继续配置可用模型。"
    : result.httpStatus === 401 ? "厂家 API KEY 未通过验证。请打开「管理 API KEY」检查或更换密钥。"
      : result.httpStatus === 403 ? "厂家拒绝了访问。请检查密钥权限或服务区域。"
        : result.httpStatus === 429 ? "厂家暂时限制了请求。请检查额度，稍后再测试连接。"
          : "暂时无法连接模型服务。请检查服务地址和网络后重试。";
  return `
    <div class="provider-test-result status-${escapeAttribute(result.status)}" role="status">
      <strong>${escapeHtml(result.providerName ?? "模型服务")} · ${title}</strong>
      <span>${guidance}</span>
      <details class="provider-result-details"><summary>查看诊断信息</summary>
        <span>${escapeHtml(result.message ?? "")}</span>
        <small>HTTP ${escapeHtml(result.httpStatus ?? "-")} · ${escapeHtml(result.latencyMs ?? 0)} ms</small>
        ${Number.isSafeInteger(result.attemptCount) && result.attemptCount > 1
          ? `<small>已尝试 ${escapeHtml(result.attemptCount)} 把 KEY，以上为最终代表性结果</small>` : ""}
      </details>
    </div>
  `;
}

/**
 * 渲染 Provider 空列表行。
 *
 * 使用 table row 语义保持网格结构稳定。
 */
function renderEmptyRow() {
  return `
    <div class="provider-row provider-empty" role="row">
      <span>尚未接入模型服务。请选择厂家，配置你已有的 API KEY。</span>
    </div>
  `;
}

/**
 * 渲染 select option。
 *
 * value 和 label 都经过转义，防止配置项中的特殊字符破坏 HTML。
 */
function renderOption(value, label, selectedValue) {
  const selected = value === selectedValue ? " selected" : "";
  return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(label)}</option>`;
}

/**
 * 格式化 ISO 时间。
 *
 * 列表空间有限，只保留秒级 UTC 文本。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
}

/**
 * HTML 文本转义。
 *
 * Provider 名称、URL、错误信息都来自用户或上游配置，渲染前必须转义。
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
 * 在文本转义基础上额外处理反引号，保护 data-* 和 title 属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
