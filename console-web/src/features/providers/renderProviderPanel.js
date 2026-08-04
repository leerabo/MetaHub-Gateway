import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// Provider 面板的类型标签用于控制台展示，Provider Key 明文永远不应进入前端列表状态。

const PROVIDER_LABELS = {
  openai_compatible: "OpenAI Compatible",
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
  bearer: "Bearer",
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
  // 缺少后端 provider.read 时零 DOM，不能按角色名推导 Provider 可见性。
  if (!hasPermission(authContext, PERMISSIONS.PROVIDER_READ)) {
    return "";
  }
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  if (!providersState) {
    return `
      <div class="panel-heading">
        <h2 id="providers-title">Provider 管理</h2>
        <p>正在读取当前租户的模型 Endpoint Provider。</p>
      </div>
      <div class="empty-state">等待 /providers 响应。</div>
    `;
  }
  // Provider 判断会影响上游归因和密钥健康，错误路径必须保持脱敏。
  if (providersState.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="providers-title">Provider 管理</h2>
        <p>Provider 管理暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(providersState.message)} request_id: ${escapeHtml(providersState.requestId ?? "-")}
      </div>
    `;
  }

  const providers = providersState.providers ?? [];
  const providerKeysByProviderId = providersState.providerKeysByProviderId ?? {};
  const canWrite = hasPermission(authContext, PERMISSIONS.PROVIDER_WRITE);
  return `
    <div class="panel-heading providers-heading">
      <div>
        <h2 id="providers-title">Provider 管理</h2>
        <p>注册模型厂商、云端 Endpoint 或本地 OpenAI-compatible Endpoint；OpenRouter 与硅基流动不是模型厂商或默认上游，仅在客户明确要求桥接时使用迁移兼容分类。</p>
      </div>
      <span class="providers-count">${providers.length} 个 Provider</span>
    </div>
    ${renderActionError(providersState.actionError)}
    ${renderTestResult(providersState.lastTestResult)}
    ${canWrite ? `<form class="create-provider-form" id="create-provider-form">
      <h3>新增 Provider</h3>
      <label>
        <span>Code</span>
        <input name="providerCode" maxlength="64" required placeholder="deepseek_official" />
      </label>
      <label>
        <span>名称</span>
        <input name="name" maxlength="120" required placeholder="DeepSeek 官方 Endpoint" />
      </label>
      <label>
        <span>类型</span>
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
      <label class="provider-url-field">
        <span>Base URL</span>
        <input name="baseUrl" type="url" required placeholder="https://api.deepseek.com/v1" />
      </label>
      <label class="provider-description-field">
        <span>备注</span>
        <input name="description" maxlength="240" placeholder="生产候选、测试环境或本地服务" />
      </label>
      <div class="provider-actions">
        <button type="submit">创建 Provider</button>
        <span class="provider-feedback" role="status">${escapeHtml(providersState.feedback ?? "")}</span>
      </div>
    </form>` : ""}
    <div class="providers-grid" role="table" aria-label="当前租户 Provider">
      <div class="provider-row provider-head" role="row">
        <span>Provider</span>
        <span>类型</span>
        <span>Base URL</span>
        <span>健康</span>
        <span>最近测试</span>
        <span>操作</span>
      </div>
      ${
        providers.length
          ? providers
              .map((provider) => renderProviderRow(
                provider,
                providerKeysByProviderId[provider.provider_id],
                canWrite,
              ))
              .join("")
          : renderEmptyRow()
      }
    </div>
  `;
}

/**
 * 渲染单个 Provider 行。
 *
 * 行内只展示 Endpoint 元数据和健康状态；启用/禁用通过隐藏字段提交目标状态，后端
 * 负责二次校验。
 */
function renderProviderRow(provider, keyState, canWrite) {
  const nextStatus = provider.status === "active" ? "disabled" : "active";
  const statusLabel = provider.status === "active" ? "禁用" : "启用";
  return `
    <div class="provider-row" role="row">
      <span class="provider-identity">
        <strong>${escapeHtml(provider.name)}</strong>
        <em>
          ${escapeHtml(provider.provider_code)}
          · ${escapeHtml(AUTH_LABELS[provider.auth_type] ?? provider.auth_type)}
          · ${escapeHtml(provider.status)}
        </em>
      </span>
      <span>${escapeHtml(PROVIDER_LABELS[provider.provider_type] ?? provider.provider_type)}</span>
      <code title="${escapeAttribute(provider.base_url)}">${escapeHtml(provider.base_url)}</code>
      <span>
        <mark class="status-mark status-${escapeHtml(provider.health_status)}">
          ${escapeHtml(provider.health_status)}
        </mark>
      </span>
      <span title="${escapeAttribute(provider.last_test_error || "")}">
        ${escapeHtml(formatDateTime(provider.last_tested_at))}
      </span>
      ${canWrite ? `<div class="provider-row-actions">
        <form data-provider-test-form data-provider-id="${escapeAttribute(provider.provider_id)}">
          <button type="submit">测试</button>
        </form>
        <form data-provider-status-form data-provider-id="${escapeAttribute(provider.provider_id)}">
          <input type="hidden" name="status" value="${escapeAttribute(nextStatus)}" />
          <button type="submit">${statusLabel}</button>
        </form>
      </div>` : ""}
    </div>
    ${renderProviderKeypool(provider, keyState, canWrite)}
  `;
}

/**
 * 渲染 Provider 的 KeyPool 区域。
 *
 * 新增 Key 表单使用 password 输入框；已有 Key 只展示 key_prefix/fingerprint 等
 * 脱敏信息，不回显 Provider Key 明文。
 */
function renderProviderKeypool(provider, keyState, canWrite) {
  const keys = keyState?.provider_keys ?? [];
  return `
    <div class="provider-keypool" role="row">
      <div class="provider-keypool-heading">
        <strong>KeyPool</strong>
        <span>${keys.length} 个 Key</span>
      </div>
      ${canWrite ? `<form
        class="provider-key-form"
        data-provider-key-create-form
        data-provider-id="${escapeAttribute(provider.provider_id)}"
      >
        <label>
          <span>名称</span>
          <input name="name" maxlength="80" required placeholder="生产主 Key" />
        </label>
        <label>
          <span>Provider Key</span>
          <input name="plainValue" type="password" required autocomplete="new-password" placeholder="sk-..." />
        </label>
        <label>
          <span>权重</span>
          <input name="weight" type="number" min="1" max="10000" value="100" />
        </label>
        <button type="submit">新增 Key</button>
      </form>` : ""}
      ${keyState?.state === "error" ? renderProviderKeyError(keyState) : renderProviderKeyRows(keys, canWrite)}
    </div>
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
    return `<div class="provider-key-empty">还没有可管理的 Provider Key。</div>`;
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
        <mark class="status-mark status-${escapeHtml(providerKey.status)}">${escapeHtml(providerKey.status)}</mark>
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
      KeyPool 暂不可用 request_id: ${escapeHtml(error.requestId ?? "-")}
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
  return `
    <div class="provider-test-result status-${escapeAttribute(result.status)}" role="status">
      <strong>${escapeHtml(result.providerCode ?? "Provider")} · ${escapeHtml(result.status)}</strong>
      <span>${escapeHtml(result.message ?? "")}</span>
      <small>HTTP ${escapeHtml(result.httpStatus ?? "-")} · ${escapeHtml(result.latencyMs ?? 0)} ms</small>
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
      <span>当前租户还没有 Provider。</span>
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
