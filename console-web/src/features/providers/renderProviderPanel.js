import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { renderProviderCatalog } from "./renderProviderCatalog.js";
import { renderWorkspaceProviderEngineering } from "../workspace-provider-engineering/renderWorkspaceProviderEngineering.js";

// Provider 面板的类型标签用于控制台展示，Provider Key 明文永远不应进入前端列表状态。

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
  const catalogOnly = !canReadProviders && canReadCatalog;
  const tabs = canReadCatalog && canReadProviders ? `
    <div class="provider-tabs" aria-label="模型服务操作">
      <button type="button" class="provider-add-button" data-provider-catalog-open aria-haspopup="dialog">添加模型服务</button>
    </div>` : "";
  if (catalogOnly) {
    return `
      <div class="panel-heading providers-heading"><div><h2 id="providers-title">添加模型服务</h2></div></div>
      ${renderProviderCatalog(catalogState, authContext)}
    `;
  }
  if (!canReadProviders) return "";
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
        ${escapeHtml(providersState.message ?? "模型服务读取失败。")}
        <button type="button" data-resource-retry>重试</button>
      </div>
    `;
  }

  // 防止迟到响应或异常接口重复投影同一个 Provider 行。
  const providers = [...new Map(
    (Array.isArray(providersState.providers) ? providersState.providers : [])
      .filter((provider) => provider?.provider_id)
      .map((provider) => [provider.provider_id, provider]),
  ).values()];
  const currentProviders = providers.filter((provider) => provider.status !== "disabled");
  const disabledProviders = providers.filter((provider) => provider.status === "disabled");
  const providerKeysByProviderId = providersState.providerKeysByProviderId ?? {};
  const optimisticKeyProviderIds = new Set(providersState.optimisticKeyProviderIds ?? []);
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
    hasPermission(authContext, PERMISSIONS.MODEL_READ),
    optimisticKeyProviderIds.has(provider.provider_id),
  )).join("");
  const disabledOpen = disabledProviders.some((provider) =>
    provider.provider_id === providersState.setupProviderId
    || provider.provider_id === selectedEngineeringProvider?.provider_id
    || providerKeysByProviderId[provider.provider_id]?.state === "error");
  const tableHeading = `<div class="provider-row provider-head" role="row">
    <span>模型服务</span><span>服务地址</span><span>状态</span>
    <span>最近检查</span><span>操作</span>
  </div>`;
  return `
    <div class="panel-heading providers-heading">
      <div>
        <h2 id="providers-title">模型服务</h2>
      </div>
      <span class="providers-count">${providers.length} 个服务</span>
      ${tabs}
    </div>
    ${providersState.feedback ? `<p class="provider-feedback" role="status">${escapeHtml(providersState.feedback)}</p>` : ""}
    ${renderActionError(providersState.actionError)}
    ${renderTestResult(providersState.lastTestResult)}
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
    ${canReadCatalog ? `
      <dialog class="provider-catalog-dialog" data-provider-catalog-dialog aria-labelledby="provider-catalog-dialog-title">
        <div class="provider-catalog-dialog-content">
          <div class="provider-catalog-dialog-heading">
            <div><span class="provider-test-dialog-kicker">模型服务</span><h3 id="provider-catalog-dialog-title">添加模型服务</h3></div>
            <button type="button" class="icon-button" data-provider-catalog-close aria-label="关闭">×</button>
          </div>
          ${renderProviderCatalog(catalogState, authContext)}
        </div>
      </dialog>` : ""}
  `;
}

/**
 * 渲染单个 Provider 行。
 *
 * 行内只展示 Endpoint 元数据和健康状态；启用/禁用通过隐藏字段提交目标状态，后端
 * 负责二次校验。
 */
function renderProviderRow(provider, keyState, canWrite, canReadEngineering, engineeringSelected, canReadModels, optimisticKeyReady = false) {
  const nextStatus = provider.status === "active" ? "disabled" : "active";
  const statusLabel = provider.status === "active" ? "禁用" : "启用";
  const providerKeys = keyState?.provider_keys ?? [];
  const configuredKeyCount = providerKeys.length;
  const activeKeyCount = providerKeys.filter((key) => key.status === "active").length;
  const missingKey = provider.auth_type !== "none" && keyState?.state === "ok"
    && configuredKeyCount === 0 && !optimisticKeyReady;
  const hasConfiguredKey = configuredKeyCount > 0 || optimisticKeyReady;
  const keyUnavailable = provider.auth_type !== "none" && keyState?.state === "ok"
    && configuredKeyCount > 0 && activeKeyCount === 0 && !optimisticKeyReady;
  const canTest = provider.status === "active" && (provider.auth_type === "none"
    || hasConfiguredKey);
  const needsTest = canTest && provider.health_status !== "healthy";
  return `
    <div class="provider-row" role="row" data-provider-row-id="${escapeAttribute(provider.provider_id)}">
      <span class="provider-identity">
        <strong>${escapeHtml(displayProviderName(provider))}</strong>
        <em>
          ${provider.status === "active" ? "已启用" : "已停用"}
        </em>
        ${provider.auth_type !== "none" && keyState?.state === "ok"
          ? `<small>${configuredKeyCount} 把 API KEY${keyUnavailable ? "（当前不可用）" : ""}</small>` : ""}
      </span>
      <code class="provider-url" dir="ltr" title="${escapeAttribute(provider.base_url)}">${escapeHtml(displayProviderUrl(provider.base_url))}</code>
      <span>
        <mark class="status-mark status-${escapeHtml(keyUnavailable ? "disabled" : provider.health_status)}">
          ${missingKey ? "待配置密钥" : keyUnavailable ? "密钥不可用" : escapeHtml(({ healthy: "可用", unhealthy: "异常", degraded: "需关注", unknown: "未检查" })[provider.health_status] ?? provider.health_status)}
        </mark>
      </span>
      <span title="${escapeAttribute(provider.last_test_error || "")}">
        ${escapeHtml(formatDateTime(provider.last_tested_at))}
      </span>
      <div class="provider-row-actions">
        <div class="provider-primary-actions">
        ${canWrite ? `<button type="button" class="provider-main-action"
          data-provider-edit-open="${escapeAttribute(provider.provider_id)}"
          aria-haspopup="dialog">编辑</button>` : ""}
        ${canWrite && provider.auth_type !== "none" ? `<button type="button" class="provider-main-action${missingKey ? " provider-setup-action" : ""}"
          data-provider-key-open="${escapeAttribute(provider.provider_id)}"
          aria-haspopup="dialog">${missingKey ? "配置 API KEY" : "管理 API KEY"}</button>` : ""}
        ${canReadModels && canTest && provider.health_status === "healthy" ? '<button type="button" class="provider-main-action" data-console-nav="models" data-console-route="workspace.gateway.models" data-console-target="workspace.gateway.models">配置模型</button>' : ""}
        </div>
        ${canWrite || canReadEngineering ? `<div class="provider-maintenance-actions" aria-label="服务操作">
        ${canWrite ? `${canTest ? `<form data-provider-test-form data-provider-id="${escapeAttribute(provider.provider_id)}">
          <button${needsTest ? ' class="provider-setup-action"' : ' class="provider-secondary-action"'} type="submit">测试连接</button>
        </form>` : ""}
        <form data-provider-status-form data-provider-id="${escapeAttribute(provider.provider_id)}">
          <input type="hidden" name="status" value="${escapeAttribute(nextStatus)}" />
          <button type="submit" class="provider-secondary-action">${statusLabel}</button>
        </form>` : ""}
        ${canWrite ? `<form data-provider-delete-form data-provider-id="${escapeAttribute(provider.provider_id)}">
          <button type="submit" class="danger-button">删除服务</button>
        </form>` : ""}
        </div>` : ""}
      </div>
    </div>
    ${canWrite && provider.auth_type !== "none" && keyState?.state === "error" ? renderProviderKeyError(keyState) : ""}
    ${renderProviderKeyDialog(provider, keyState, canWrite, canReadModels)}
    ${renderProviderEditDialog(provider, canWrite)}
  `;
}

function displayProviderName(provider) {
  const name = String(provider?.name ?? "").trim();
  const code = String(provider?.provider_code ?? "").trim();
  if (code === "local_openai" && /^local openai compatible endpoint$/iu.test(name)) {
    return "默认模型服务";
  }
  return name || "未命名模型服务";
}

// Endpoint values are validated by the API. Strip accidental display whitespace
// so a URL can never appear different from the value the user entered.
function displayProviderUrl(value) {
  return String(value ?? "").replace(/\s+/gu, "");
}

/**
 * 渲染 Provider 的 KeyPool 区域。
 *
 * 新增 Key 表单使用 password 输入框；已有 Key 只展示 key_prefix/fingerprint 等
 * 脱敏信息，不回显 Provider Key 明文。
 */
function renderProviderKeyDialog(provider, keyState, canWrite, canReadModels) {
  if (provider.auth_type === "none") return "";
  const keys = keyState?.provider_keys ?? [];
  return `
    <dialog class="provider-key-dialog" data-provider-key-dialog data-provider-id="${escapeAttribute(provider.provider_id)}" aria-labelledby="provider-key-dialog-title-${escapeAttribute(provider.provider_id)}">
      <div class="provider-key-dialog-content">
        <div class="provider-key-dialog-heading">
          <div><h3 id="provider-key-dialog-title-${escapeAttribute(provider.provider_id)}">${escapeHtml(displayProviderName(provider))} · API KEY</h3><p>填写厂家提供的 API KEY，保存后即可测试连接。</p></div>
          <button type="button" class="icon-button" data-provider-key-cancel aria-label="关闭">×</button>
        </div>
      ${canWrite ? `<form
        class="provider-key-form"
        data-provider-key-create-form
        data-provider-id="${escapeAttribute(provider.provider_id)}"
      >
        <label>
          <span>API KEY</span>
          <input name="plainValue" type="password" required autocomplete="new-password" placeholder="粘贴 API KEY" />
        </label>
        <button type="submit">保存</button>
      </form>` : ""}
      <div class="provider-key-dialog-keys">
        <h4>已配置的 API KEY</h4>
        ${keyState?.state === "error" ? renderProviderKeyError(keyState) : keyState?.state === "ok" ? renderProviderKeyRows(keys, canWrite) : '<p role="status">正在读取...</p>'}
      </div>
      </div>
    </dialog>
  `;
}

function renderProviderEditDialog(provider, canWrite) {
  if (!canWrite) return "";
  return `
    <dialog class="provider-edit-dialog" data-provider-edit-dialog data-provider-id="${escapeAttribute(provider.provider_id)}" aria-labelledby="provider-edit-dialog-title-${escapeAttribute(provider.provider_id)}">
      <form class="provider-edit-form" data-provider-edit-form data-provider-id="${escapeAttribute(provider.provider_id)}">
        <div class="provider-edit-dialog-heading">
          <div><span class="provider-test-dialog-kicker">模型服务</span><h3 id="provider-edit-dialog-title-${escapeAttribute(provider.provider_id)}">编辑服务</h3></div>
          <button type="button" class="icon-button" data-provider-edit-close aria-label="关闭">×</button>
        </div>
        <label><span>服务名称</span><input name="name" required maxlength="120" value="${escapeAttribute(displayProviderName(provider))}" /></label>
        <label><span>服务地址</span><input name="baseUrl" type="url" required value="${escapeAttribute(provider.base_url)}" /></label>
        <div class="provider-edit-actions">
          <button type="button" class="provider-secondary-action" data-provider-edit-close>取消</button>
          <button type="submit" class="provider-setup-action">保存</button>
        </div>
      </form>
    </dialog>
  `;
}

/**
 * 渲染 Provider Key 列表。
 *
 * 空列表使用独立提示，避免用户误以为 KeyPool 请求失败。
 */
function renderProviderKeyRows(keys, canWrite) {
  if (!keys.length) {
    return '<p class="provider-key-empty">还没有配置 API KEY。</p>';
  }
  return `
    <div class="provider-key-grid" aria-label="已配置的 API KEY">
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
        <em title="${escapeAttribute(providerKey.key_prefix || "")}">${escapeHtml(displayKeyPrefix(providerKey.key_prefix))}</em>
      </span>
      <span>
        <mark class="status-mark status-${escapeHtml(providerKey.status)}">${escapeHtml(({ active: "可用", disabled: "已停用" })[providerKey.status] ?? "不可用")}</mark>
      </span>
      <span>${escapeHtml(providerKey.weight)}</span>
      <span>${escapeHtml(formatDateTime(providerKey.last_used_at))}</span>
      <span title="${escapeAttribute(providerKey.last_error_message || "")}">
        ${providerKey.last_error_code ? "连接异常" : "-"}
      </span>
      <span class="provider-key-actions">
        ${canWrite ? `<form
          class="provider-key-action"
          data-provider-key-status-form
          data-provider-key-id="${escapeAttribute(providerKey.provider_key_id)}"
        >
          <input type="hidden" name="status" value="${escapeAttribute(nextStatus)}" />
          <button type="submit">${actionLabel}</button>
        </form>` : ""}
        ${canWrite ? `<form
          class="provider-key-action"
          data-provider-key-delete-form
          data-provider-key-id="${escapeAttribute(providerKey.provider_key_id)}"
        >
          <button type="submit" class="danger-button">删除</button>
        </form>` : ""}
      </span>
    </div>
  `;
}

function displayKeyPrefix(value) {
  const prefix = String(value ?? "");
  if (prefix.length <= 18) return prefix || "-";
  return `${prefix.slice(0, 10)}…${prefix.slice(-5)}`;
}

/**
 * 渲染 KeyPool 读取失败态。
 *
 * 普通用户只需要知道密钥列表暂不可用；关联号保留在状态对象和服务端日志中。
 */
function renderProviderKeyError(error) {
  return `
    <div class="provider-key-empty error-state">
      密钥列表暂不可用
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
      ${error.category ? `<strong>${escapeHtml(error.category)}</strong>` : ""}
      ${escapeHtml(error.message ?? "模型服务操作失败")}
      ${error.detail ? `<small>${escapeHtml(error.detail)}</small>` : ""}
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
  const diagnosis = providerProbeDiagnosis(result);
  const guidance = successful ? "模型服务已接通。" : diagnosis.guidance;
  const latency = Number.isFinite(result.latencyMs) ? ` · 响应 ${Math.round(result.latencyMs)} ms` : "";
  return `
    <dialog class="provider-test-dialog provider-test-result status-${escapeAttribute(result.status)}" data-provider-test-dialog data-provider-test-auto-open aria-labelledby="provider-test-dialog-title">
      <div class="provider-test-dialog-content">
        <div class="provider-test-dialog-heading">
          <div>
            <span class="provider-test-dialog-kicker">连接测试完成</span>
            <h3 id="provider-test-dialog-title">${escapeHtml(result.providerName ?? "模型服务")} · ${title}</h3>
          </div>
          <button type="button" class="icon-button" data-provider-test-close aria-label="关闭">×</button>
        </div>
        ${!successful ? `<p class="provider-test-diagnosis"><strong>问题归类：${escapeHtml(diagnosis.category)}</strong></p>` : ""}
        <p>${escapeHtml(guidance)}</p>
        ${latency ? `<small>${escapeHtml(latency)}</small>` : ""}
        <div class="provider-test-dialog-actions">
          <button type="button" data-provider-test-close>知道了</button>
        </div>
      </div>
    </dialog>
  `;
}

/** 将探测事实翻译成用户能采取行动的三类问题，避免把内部错误码直接抛给用户。 */
function providerProbeDiagnosis(result) {
  const code = String(result?.errorCode ?? "");
  const status = Number(result?.httpStatus);
  if (code === "authentication_failed" || status === 401 || status === 403) {
    return {
      category: "大模型服务器端 · API KEY 或权限",
      guidance: "模型服务器拒绝了这把 API KEY。请检查 KEY 是否正确、是否已启用，以及服务区域或权限是否匹配。",
    };
  }
  if (["endpoint_invalid", "endpoint_blocked", "redirect_rejected"].includes(code)) {
    return {
      category: "填写或网络设置",
      guidance: "服务地址无法按当前格式访问。请检查地址是否完整（包含 https:// 和正确的接口路径）。",
    };
  }
  if (code === "timeout") {
    return {
      category: "大模型服务器端 · 响应超时",
      guidance: "模型服务器在规定时间内没有响应。这里检查的是模型 API 接口，不是网页首页；请确认服务地址包含正确的 API 路径（例如 /v1），并从当前网络访问该接口。",
    };
  }
  if (["connect_failed", "tls_failed", "transport_error", "upstream_5xx", "probe_http_error", "rate_limited"].includes(code)
    || [429, 500, 502, 503, 504].includes(status)) {
    return {
      category: "大模型服务器端",
      guidance: status === 429
        ? "模型服务器暂时限制了请求，请稍后再试。"
        : "模型服务器返回了异常响应，请检查服务状态或稍后再试。",
    };
  }
  if (code === "catalog_invalid") {
    return {
      category: "大模型服务器端 · 返回内容异常",
      guidance: "模型服务器返回的内容无法识别，请确认该地址提供兼容的模型接口。",
    };
  }
  return {
    category: "MetaHub 系统",
    guidance: "MetaHub 没有拿到完整的测试结果，请稍后重试；如果持续失败，请联系管理员。",
  };
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
