import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { renderWorkspaceConnectionStatus, workspaceSetupStepComplete } from "../workspace-navigation/renderWorkspaceNavigation.js";

// 模型别名面板的环境和状态常量只定义展示边界，写入仍依赖后端权限校验。
const ENVIRONMENT_LABELS = {
  dev: "开发",
  test: "测试",
  prod: "生产",
};

/**
 * 渲染模型别名面板。
 *
 * admin/developer 可读，只有 admin 可写；前端按角色隐藏写入口，后端仍负责最终权限校验。
 */
export function renderModelAliasPanel(modelAliasesState, modelsState, authContext, environmentState, workspaceTaskHomeState, i18n) {
  // 权限快照缺少 routing.read 时零 DOM，不根据角色名恢复入口。
  if (!hasPermission(authContext, PERMISSIONS.ROUTING_READ)) {
    return "";
  }
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  if (!modelAliasesState) {
    return `
      <div class="panel-heading">
        <h2 id="model-aliases-title">模型路由</h2>
        <p class="panel-help">这里是管理员维护的统一路由入口，不是让员工改写官方模型名。员工个人 KEY 会自动使用默认路由；只有工具强制要求模型名时才填写。支持 OpenAI 兼容模式的客户端可通过 <code>/v1/models</code> 自动发现入口。</p>
        <p>正在加载模型路由...</p>
      </div>
      <div class="empty-state" role="status">正在加载...</div>
    `;
  }
  // 模型路由结果决定上游调用，异常时必须用统一错误码停止。
  if (modelAliasesState.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="model-aliases-title">模型路由</h2>
        <p>模型路由暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(modelAliasesState.message)} request_id: ${escapeHtml(modelAliasesState.requestId ?? "-")}
        <button type="button" data-resource-retry>重试</button>
      </div>
    `;
  }

  const aliases = modelAliasesState.model_aliases ?? [];
  const canWrite = hasPermission(authContext, PERMISSIONS.ROUTING_WRITE);
  const prerequisiteReady = !workspaceTaskHomeState || workspaceSetupStepComplete(workspaceTaskHomeState, "model_catalog_ready");
  const activeModels = modelsState?.state === "ok"
    ? (modelsState.models ?? []).filter((model) => model.status === "active")
    : [];
  return `
    <div class="panel-heading model-aliases-heading">
      <div>
        <h2 id="model-aliases-title">模型路由</h2>
      </div>
      <span class="model-aliases-count">${aliases.length} 条路由</span>
    </div>
    ${renderWorkspaceConnectionStatus(workspaceTaskHomeState, "workspace.gateway.aliases", i18n)}
    ${renderActionError(modelAliasesState.actionError)}
    ${modelAliasesState.feedback ? `<p class="model-alias-feedback" role="status">${escapeHtml(modelAliasesState.feedback)}</p>` : ""}
    ${canWrite ? renderRouteDependencies(modelsState, environmentState, authContext) : ""}
    ${canWrite ? `<details class="model-create-disclosure"${aliases.length ? "" : " open"}><summary>添加路由</summary>${renderCreateModelAliasForm(modelsState, environmentState, modelAliasesState.feedback)}</details>` : ""}
    ${aliases.length && hasPermission(authContext, PERMISSIONS.VIRTUAL_KEY_READ) ? `<a class="${prerequisiteReady ? "console-flow-next" : "console-flow-related"}" href="#workspace.gateway.virtual-keys">${prerequisiteReady ? "下一步：创建个人 KEY" : "个人 KEY"}</a>` : ""}
    <div class="model-aliases-grid" role="table" aria-label="当前租户模型别名">
      <div class="model-alias-row model-alias-head ${canWrite ? "" : "model-alias-readonly-row"}" role="row">
        <span>统一入口名称</span>
        <span>环境</span>
        <span>默认模型</span>
        <span>备用模型</span>
        <span>状态</span>
        <span>更新时间</span>
        ${canWrite ? "<span>操作</span>" : ""}
      </div>
      ${aliases.length ? aliases.map((item) => renderModelAliasRow(item, canWrite, activeModels, modelsState?.models ?? [])).join("") : renderEmptyRow()}
    </div>
  `;
}

function renderRouteDependencies(modelsState, environmentState, authContext) {
  if (modelsState?.state === "error" || environmentState?.state === "error") {
    return '<div class="empty-state error-state" role="alert">路由所需配置加载失败。<button type="button" data-resource-retry>重试</button></div>';
  }
  if (modelsState?.state !== "ok" || environmentState?.state !== "ok") {
    return '<p role="status">正在加载路由所需配置...</p>';
  }
  if (!(environmentState.environments ?? []).some((item) => item.status === "active")) {
    return '<p class="empty-state" role="status">暂无可用运行环境。请联系平台管理员完成运行环境配置。</p>';
  }
  if (!(modelsState.models ?? []).some((item) => item.status === "active")) {
    return '<p class="empty-state" role="status">暂无已启用的模型。'
      + (hasPermission(authContext, PERMISSIONS.MODEL_READ)
        ? '<a href="#workspace.gateway.models">配置模型</a>' : '请联系组织管理员。') + '</p>';
  }
  return "";
}

/**
 * 渲染新建模型别名表单。
 *
 * 只允许选择当前租户 active 模型作为默认/备用模型，减少提交后被后端拒绝的无效配置。
 */
function renderCreateModelAliasForm(modelsState, environmentState, feedback) {
  if (modelsState?.state === "error" || environmentState?.state === "error") {
    return "";
  }
  const models = modelsState?.state === "ok" ? modelsState.models ?? [] : [];
  const activeModels = models.filter((model) => model.status === "active");
  const defaultOptions = activeModels.length
    ? `${activeModels.length > 1 ? '<option value="">请选择调用模型</option>' : ""}${activeModels.map((model) => renderModelOption(model, activeModels.length === 1 ? model.model_id : "")).join("")}`
    : `<option value="">请先创建并启用模型</option>`;
  const fallbackOptions = [
    `<option value="">不配置备用模型</option>`,
    ...activeModels.map((model) => renderModelOption(model, "")),
  ].join("");
  const environments = environmentState?.state === "ok"
    ? (environmentState.environments ?? []).filter((item) => item.status === "active")
    : [];
  return `
    <form class="create-model-alias-form" id="create-model-alias-form">
      <h3>新增模型路由</h3>
      <label>
        <span>统一入口名称</span>
        <input name="alias" maxlength="120" required pattern="[a-z0-9](?:[a-z0-9._]|-){0,119}" title="小写字母或数字开头，可包含小写字母、数字、点、下划线和短横线" aria-describedby="model-alias-name-format" placeholder="company-default" />
        <small id="model-alias-name-format">小写字母或数字开头，可包含小写字母、数字、点、下划线和短横线；例如 company-default。员工通常不需要手填。</small>
      </label>
      <label>
        <span>调用模型</span>
        <select name="defaultModelId" required ${activeModels.length ? "" : "disabled"}>
          ${defaultOptions}
        </select>
      </label>
      ${environments.length === 1
        ? `<input type="hidden" name="environmentId" value="${escapeAttribute(environments[0].environment_id)}" />`
        : `<label><span>运行环境</span><select name="environmentId" required ${environments.length ? "" : "disabled"}>
          <option value="">${environments.length ? "请选择运行环境" : "暂无可用运行环境"}</option>
          ${environments.map((item) => renderOption(item.environment_id, item.name, "")).join("")}
        </select></label>`}
      <details class="model-advanced"><summary>高级设置</summary><div class="model-advanced-fields">
      <label>
        <span>备用模型</span>
        <select name="fallbackModelId" ${activeModels.length ? "" : "disabled"}>
          ${fallbackOptions}
        </select>
      </label>
      <label class="model-alias-description-field">
        <span>备注</span>
        <input name="description" maxlength="240" placeholder="默认聊天入口、测试环境候选或迁移兼容别名" />
      </label>
      </div></details>
      <div class="model-alias-actions">
        <button type="submit" ${activeModels.length && environments.length ? "" : "disabled"}>创建路由</button>
        <span class="model-alias-feedback" data-model-setup-feedback role="status">${escapeHtml(feedback ?? "")}</span>
        <button type="button" data-resource-retry data-model-setup-reload hidden>刷新列表</button>
      </div>
    </form>
  `;
}

/**
 * 渲染单条模型别名。
 *
 * 该行展示 alias、环境、默认模型和备用模型，帮助开发者解释 Gateway 最终解析路径。
 */
function renderModelAliasRow(item, canWrite, activeModels, models) {
  const nextStatus = item.status === "active" ? "disabled" : "active";
  const statusLabel = item.status === "active" ? "禁用" : "启用";
  return `
    <div class="model-alias-row ${canWrite ? "" : "model-alias-readonly-row"}" role="row">
      <span class="model-alias-identity">
        <span class="model-alias-field-label">可选模型入口</span>
        <strong>${escapeHtml(item.alias)}</strong>
        ${item.description ? `<em>${escapeHtml(item.description)}</em>` : ""}
      </span>
      <span><span class="model-alias-field-label">环境</span>${escapeHtml(item.environment_name ?? ENVIRONMENT_LABELS[item.environment] ?? item.environment)}</span>
      <span><span class="model-alias-field-label">实际调用模型</span>${renderModelSummary(item.default_internal_model_id, models.find((model) => model.model_id === item.default_model_id)?.provider_name)}</span>
      <span><span class="model-alias-field-label">备用模型</span>${renderFallbackSummary(item, models)}</span>
      <span><span class="model-alias-field-label">状态</span><mark class="status-mark status-${escapeAttribute(item.status)}">${escapeHtml(item.status === "active" ? "已启用" : item.status === "disabled" ? "已停用" : "状态未知")}</mark></span>
      <time datetime="${escapeAttribute(item.updated_at)}"><span class="model-alias-field-label">更新时间</span>${escapeHtml(formatDateTime(item.updated_at))}</time>
      ${
        canWrite
          ? `
            <div class="model-alias-row-actions">
              <form
                data-model-alias-status-form
                data-model-alias-id="${escapeAttribute(item.model_alias_id)}"
                data-routing-policy-version="${escapeAttribute(item.routing_policy?.version ?? "")}"
              >
                <input type="hidden" name="status" value="${escapeAttribute(nextStatus)}" />
                <button type="submit">${statusLabel}</button>
              </form>
              ${renderFallbackPolicyForm(item, activeModels)}
            </div>
          `
          : ""
      }
    </div>
  `;
}

/**
 * 展示备用模型及其请求后 Fallback 策略状态。
 *
 * 调用前健康过滤直接选备用模型时不会显示“已触发”，这里只呈现静态配置。
 */
function renderFallbackSummary(item, models) {
  const model = renderModelSummary(item.fallback_internal_model_id, models.find((candidate) => candidate.model_id === item.fallback_model_id)?.provider_name);
  const policy = item.fallback_policy;
  if (!policy) {
    return item.fallback_internal_model_id ? `${model}<em class="fallback-policy-state">自动切换未启用</em>` : '<em>未配置备用模型</em>';
  }
  const status = policy.status === "enabled" ? "已启用" : "已停用";
  return `
    ${model}
    <em class="fallback-policy-state status-${escapeAttribute(policy.status)}">
      ${status} · v${escapeHtml(policy.version)} · ${escapeHtml(policy.trigger_conditions.map(fallbackConditionLabel).join(" / "))}
    </em>
  `;
}

/**
 * 为管理员渲染单行 Fallback 配置折叠区；developer 只看到上方只读摘要。
 */
function renderFallbackPolicyForm(item, activeModels) {
  const policy = item.fallback_policy;
  const enabled = policy?.status === "enabled";
  const conditions = new Set(policy?.trigger_conditions ?? [
    "upstream_timeout",
    "upstream_rate_limited",
    "upstream_5xx",
  ]);
  const candidates = activeModels.filter((model) => model.model_id !== item.default_model_id);
  const options = [
    `<option value="">不配置备用模型</option>`,
    ...candidates.map((model) => renderModelOption(model, item.fallback_model_id ?? "")),
  ].join("");
  return `
    <details class="fallback-policy-editor">
      <summary title="配置非流式请求失败后的备用模型">故障切换</summary>
      <form
        data-fallback-policy-form
        data-model-alias-id="${escapeAttribute(item.model_alias_id)}"
        data-routing-policy-version="${escapeAttribute(item.routing_policy?.version ?? "")}"
      >
        <label class="fallback-policy-toggle">
          <input type="checkbox" name="enabled" ${enabled ? "checked" : ""} />
          <span>启用</span>
        </label>
        <label>
          <span>备用模型</span>
          <select name="fallbackModelId">${options}</select>
        </label>
        <fieldset>
          <legend>触发条件</legend>
          ${renderConditionCheckbox("upstream_timeout", "超时", conditions)}
          ${renderConditionCheckbox("upstream_rate_limited", "限流", conditions)}
          ${renderConditionCheckbox("upstream_5xx", "5xx", conditions)}
        </fieldset>
        <label class="fallback-policy-toggle">
          <input type="checkbox" name="clearFallbackModel" />
          <span>清除备用模型</span>
        </label>
        <button type="submit">保存切换策略</button>
      </form>
    </details>
  `;
}

/** 渲染单个触发条件复选框，条件值只来自后端支持的固定枚举。 */
function renderConditionCheckbox(value, label, selected) {
  return `
    <label>
      <input type="checkbox" name="triggerCondition" value="${escapeAttribute(value)}" ${selected.has(value) ? "checked" : ""} />
      <span>${escapeHtml(label)}</span>
    </label>
  `;
}

/** 把策略条件枚举转换成紧凑中文标签。 */
function fallbackConditionLabel(value) {
  return {
    upstream_timeout: "超时",
    upstream_rate_limited: "限流",
    upstream_5xx: "5xx",
  }[value] ?? value;
}

/**
 * 渲染别名目标模型摘要。
 *
 * 空备用模型用短横线表示，避免把“未配置备用”和“读取失败”混在一起。
 */
function renderModelSummary(internalModelId, providerName) {
  if (!internalModelId) {
    return `<em>-</em>`;
  }
  return `
    <span class="model-alias-target">
      <strong>${escapeHtml(internalModelId)}</strong>
      ${providerName ? `<em>${escapeHtml(providerName)}</em>` : ""}
    </span>
  `;
}

/**
 * 渲染模型别名操作错误。
 *
 * 统一显示 request_id，便于在控制面日志中追踪失败原因。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="model-alias-action-error" role="alert">
      ${escapeHtml(error.message ?? "模型别名操作失败")} request_id: ${escapeHtml(error.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染模型别名空列表行。
 *
 * 使用表格行语义保持列表布局稳定。
 */
function renderEmptyRow() {
  return `
    <div class="model-alias-row model-alias-empty" role="row">
      <span>尚未添加路由。</span>
    </div>
  `;
}

/**
 * 渲染模型选择项。
 *
 * label 同时包含内部模型 ID 和 Provider code，减少管理员选错上游的概率。
 */
function renderModelOption(model, selectedValue) {
  const selected = model.model_id === selectedValue ? " selected" : "";
  const label = [model.internal_model_id, model.provider_name].filter(Boolean).join(" / ");
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  return `<option value="${escapeAttribute(model.model_id)}"${selected}>${escapeHtml(label)}</option>`;
}

/**
 * 渲染通用 select option。
 *
 * value 和 label 均做转义，防止模型别名或描述中的特殊字符影响 HTML。
 */
function renderOption(value, label, selectedValue) {
  const selected = value === selectedValue ? " selected" : "";
  return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(label)}</option>`;
}

/**
 * 格式化更新时间。
 *
 * 控制台列表只展示秒级时间，完整排障仍以后端日志和数据库时间为准。
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
 * 别名、描述、Provider code 均可能来自用户配置，必须统一转义。
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
 * 在文本转义基础上处理反引号，保护 data-* 属性和 datetime 属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
