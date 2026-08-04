import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

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
export function renderModelAliasPanel(modelAliasesState, modelsState, authContext, environmentState) {
  // 权限快照缺少 routing.read 时零 DOM，不根据角色名恢复入口。
  if (!hasPermission(authContext, PERMISSIONS.ROUTING_READ)) {
    return "";
  }
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  if (!modelAliasesState) {
    return `
      <div class="panel-heading">
        <h2 id="model-aliases-title">模型别名</h2>
        <p>正在读取当前租户的最小路由配置。</p>
      </div>
      <div class="empty-state">等待 /model-aliases 响应。</div>
    `;
  }
  // 模型路由结果决定上游调用，异常时必须用统一错误码停止。
  if (modelAliasesState.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="model-aliases-title">模型别名</h2>
        <p>模型别名配置暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(modelAliasesState.message)} request_id: ${escapeHtml(modelAliasesState.requestId ?? "-")}
      </div>
    `;
  }

  const aliases = modelAliasesState.model_aliases ?? [];
  const canWrite = hasPermission(authContext, PERMISSIONS.ROUTING_WRITE);
  const activeModels = modelsState?.state === "ok"
    ? (modelsState.models ?? []).filter((model) => model.status === "active")
    : [];
  return `
    <div class="panel-heading model-aliases-heading">
      <div>
        <h2 id="model-aliases-title">模型别名</h2>
        <p>将稳定调用名映射到默认模型和备用模型，Gateway 按 Virtual Key 环境解析。</p>
      </div>
      <span class="model-aliases-count">${aliases.length} 个别名</span>
    </div>
    ${renderActionError(modelAliasesState.actionError)}
    ${canWrite ? renderCreateModelAliasForm(modelsState, environmentState, modelAliasesState.feedback) : ""}
    <div class="model-aliases-grid" role="table" aria-label="当前租户模型别名">
      <div class="model-alias-row model-alias-head ${canWrite ? "" : "model-alias-readonly-row"}" role="row">
        <span>别名</span>
        <span>环境</span>
        <span>默认模型</span>
        <span>备用模型</span>
        <span>状态</span>
        <span>更新时间</span>
        ${canWrite ? "<span>操作</span>" : ""}
      </div>
      ${aliases.length ? aliases.map((item) => renderModelAliasRow(item, canWrite, activeModels)).join("") : renderEmptyRow()}
    </div>
  `;
}

/**
 * 渲染新建模型别名表单。
 *
 * 只允许选择当前租户 active 模型作为默认/备用模型，减少提交后被后端拒绝的无效配置。
 */
function renderCreateModelAliasForm(modelsState, environmentState, feedback) {
  const models = modelsState?.state === "ok" ? modelsState.models ?? [] : [];
  const activeModels = models.filter((model) => model.status === "active");
  const defaultOptions = activeModels.length
    ? activeModels.map((model, index) => renderModelOption(model, index === 0 ? model.model_id : "")).join("")
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
      <h3>新增模型别名</h3>
      <label>
        <span>别名</span>
        <input name="alias" maxlength="120" required placeholder="company-default" />
      </label>
      <label>
        <span>环境</span>
        <select name="environmentId" ${environments.length ? "" : "disabled"}>
          ${environments.length
            ? environments.map((item) => renderOption(item.environment_id, `${item.name} / ${item.code}`, environments[0].environment_id)).join("")
            : `<option value="">请先创建 active Environment</option>`}
        </select>
      </label>
      <label>
        <span>默认模型</span>
        <select name="defaultModelId" ${activeModels.length ? "" : "disabled"}>
          ${defaultOptions}
        </select>
      </label>
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
      <div class="model-alias-actions">
        <button type="submit" ${activeModels.length && environments.length ? "" : "disabled"}>创建别名</button>
        <span class="model-alias-feedback" role="status">${escapeHtml(feedback ?? "")}</span>
      </div>
    </form>
  `;
}

/**
 * 渲染单条模型别名。
 *
 * 该行展示 alias、环境、默认模型和备用模型，帮助开发者解释 Gateway 最终解析路径。
 */
function renderModelAliasRow(item, canWrite, activeModels) {
  const nextStatus = item.status === "active" ? "disabled" : "active";
  const statusLabel = item.status === "active" ? "禁用" : "启用";
  return `
    <div class="model-alias-row ${canWrite ? "" : "model-alias-readonly-row"}" role="row">
      <span class="model-alias-identity">
        <strong>${escapeHtml(item.alias)}</strong>
        <em>${escapeHtml(item.description || item.model_alias_id)}</em>
      </span>
      <span>${escapeHtml(item.environment_name ?? ENVIRONMENT_LABELS[item.environment] ?? item.environment)}</span>
      <span>${renderModelSummary(item.default_internal_model_id, item.default_provider_code)}</span>
      <span>${renderFallbackSummary(item)}</span>
      <span><mark class="status-mark status-${escapeAttribute(item.status)}">${escapeHtml(item.status)}</mark></span>
      <time datetime="${escapeAttribute(item.updated_at)}">${escapeHtml(formatDateTime(item.updated_at))}</time>
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
function renderFallbackSummary(item) {
  const model = renderModelSummary(item.fallback_internal_model_id, item.fallback_provider_code);
  const policy = item.fallback_policy;
  if (!policy) {
    return `${model}<em class="fallback-policy-state">未启用策略</em>`;
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
      <summary title="配置非流式 Fallback">Fallback</summary>
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
        <button type="submit">保存 Fallback</button>
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
function renderModelSummary(internalModelId, providerCode) {
  if (!internalModelId) {
    return `<em>-</em>`;
  }
  return `
    <span class="model-alias-target">
      <strong>${escapeHtml(internalModelId)}</strong>
      <em>${escapeHtml(providerCode ?? "-")}</em>
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
      <span>当前租户还没有模型别名。</span>
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
  const label = `${model.internal_model_id} / ${model.provider_code}`;
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
