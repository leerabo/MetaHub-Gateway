import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { renderFallbackModelPicker } from "./fallbackModelPicker.js";
import { renderWorkspaceConnectionStatus } from "../workspace-navigation/renderWorkspaceNavigation.js";
import {
  ROUTING_MODES,
  ROUTING_MODE_DESCRIPTIONS,
  ROUTING_MODE_LABELS,
} from "../routing-policies/renderRoutingPolicyPanel.js";

const DEFAULT_QUALITY_COST_CONFIG = Object.freeze({
  quality_weight_bps: 5000,
  cost_weight_bps: 3000,
  latency_weight_bps: 2000,
  quality_floor_bps: 6000,
  minimum_sample_count: 20,
});

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
        <p class="panel-help">管理员维护统一路由；工具要求时再填写模型名。</p>
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
        ${escapeHtml(modelAliasesState.message ?? "模型路由读取失败。")}
        <button type="button" data-resource-retry>重试</button>
      </div>
    `;
  }

  const aliases = modelAliasesState.model_aliases ?? [];
  const canWrite = hasPermission(authContext, PERMISSIONS.ROUTING_WRITE);
  const catalogModels = modelsState?.state === "ok"
    ? (modelsState.models ?? []).filter((model) => model.status === "active")
    : [];
  // A route response can arrive with a fresher candidate snapshot than the
  // model list. Merge the safe candidate metadata so prices and names remain
  // visible while the two resource reads settle independently.
  const candidateById = new Map(aliases.flatMap((alias) => alias.routing_policy?.candidates ?? [])
    .map((candidate) => [candidate.model_id, candidate]));
  const mergedCatalogModels = catalogModels.map((model) => {
    const candidate = candidateById.get(model.model_id);
    if (!candidate) return model;
    return {
      ...candidate,
      ...model,
      provider_model_id: model.provider_model_id ?? candidate.provider_model_id,
      input_price_per_1k: model.input_price_per_1k ?? candidate.input_price_per_1k,
      output_price_per_1k: model.output_price_per_1k ?? candidate.output_price_per_1k,
      cache_price_per_1k: model.cache_price_per_1k ?? candidate.cache_price_per_1k,
      currency: model.currency ?? candidate.currency,
    };
  });
  // Keep a route snapshot available while the separate catalog request is
  // still settling. The snapshot contains the readable upstream id and the
  // persisted prices, so the UI never falls back to the database UUID.
  const snapshotModels = [...candidateById.values()].map((candidate) => ({
    ...candidate,
    internal_model_id: candidate.internal_model_id ?? candidate.provider_model_id,
    status: candidate.model_status ?? candidate.status ?? "active",
  }));
  const activeModels = [...new Map(
    [...mergedCatalogModels, ...snapshotModels]
      .filter((model) => model?.model_id && model.status === "active")
      .map((model) => [model.model_id, model]),
  ).values()];
  return `
    <div class="panel-heading model-aliases-heading">
      <div>
        <h2 id="model-aliases-title">模型路由</h2>
      </div>
      <span class="model-aliases-count">${aliases.length} 条路由</span>
      ${canWrite ? '<button type="button" class="model-alias-create-button" data-model-alias-create-open aria-haspopup="dialog">添加路由</button>' : ""}
    </div>
    ${renderWorkspaceConnectionStatus(workspaceTaskHomeState, "workspace.gateway.aliases", i18n)}
    <div class="console-page-toolbar model-alias-toolbar">${renderActionError(modelAliasesState.actionError)}${modelAliasesState.feedback ? `<p class="model-alias-feedback" role="status">${escapeHtml(modelAliasesState.feedback)}</p>` : ""}${canWrite ? renderRouteDependencies(modelsState, environmentState, authContext) : ""}</div>
    <div class="model-aliases-grid" role="table" aria-label="当前租户模型别名">
      <div class="model-alias-row model-alias-head ${canWrite ? "" : "model-alias-readonly-row"}" role="row">
        <span>统一入口名称</span>
        <span>环境</span>
        <span>优先模型</span>
        <span>备用模型</span>
        <span>选择方式</span>
        <span>状态</span>
        <span>更新时间</span>
        ${canWrite ? "<span>操作</span>" : ""}
      </div>
      ${aliases.length ? aliases.map((item) => renderModelAliasRow(
        item,
        canWrite,
        activeModels,
        modelsState?.models ?? [],
        environmentState?.state === "ok" ? environmentState.environments ?? [] : [],
      )).join("") : renderEmptyRow()}
    </div>
    ${canWrite ? renderModelAliasDialog(
      renderCreateModelAliasForm(modelsState, environmentState, modelAliasesState.feedback),
      aliases.map((item) => renderModelAliasEditForm(
        item,
        activeModels,
        environmentState?.state === "ok" ? environmentState.environments ?? [] : [],
      )).join(""),
    ) : ""}
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
        ? '<button type="button" class="primary-command" data-console-nav="models" data-console-route="workspace.gateway.models" data-console-target="workspace.gateway.models">前往模型目录</button>' : '请联系组织管理员。') + '</p>';
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
  const environments = environmentState?.state === "ok"
    ? (environmentState.environments ?? []).filter((item) => item.status === "active")
    : [];
  return `
    <form class="create-model-alias-form" id="create-model-alias-form">
      <label>
        <span>统一入口名称</span>
        <input name="alias" maxlength="120" required pattern="[a-z0-9](?:[a-z0-9._]|-){0,119}" title="小写字母或数字开头，可包含小写字母、数字、点、下划线和短横线" aria-describedby="model-alias-name-format" placeholder="company-default" />
        <small id="model-alias-name-format">请输入小写字母、数字、点、下划线或短横线。</small>
      </label>
      <label>
        <span>调用模型</span>
        <select name="defaultModelId" required ${activeModels.length ? "" : "disabled"}>
          ${defaultOptions}
        </select>
        <small class="model-alias-price-hint" data-model-price-hint>选择模型后显示价格</small>
      </label>
      ${environments.length === 1
        ? `<input type="hidden" name="environmentId" value="${escapeAttribute(environments[0].environment_id)}" />`
        : `<label><span>运行环境</span><select name="environmentId" required ${environments.length ? "" : "disabled"}>
          <option value="">${environments.length ? "请选择运行环境" : "暂无可用运行环境"}</option>
          ${environments.map((item) => renderOption(item.environment_id, item.name, "")).join("")}
        </select></label>`}
      ${renderFallbackModelPicker(activeModels, [], activeModels.length === 1 ? activeModels[0].model_id : "")}
      <label class="model-alias-description-field">
        <span>备注</span>
        <input name="description" maxlength="240" placeholder="例如：默认聊天入口" />
      </label>
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
function renderModelAliasRow(item, canWrite, activeModels, models, environments) {
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
      <span><span class="model-alias-field-label">优先模型</span>${renderModelSummary(resolveModelLabel(item, item.default_model_id, models), resolveModelProvider(item, item.default_model_id, models))}</span>
      <span><span class="model-alias-field-label">备用模型</span>${renderFallbackSummary(item, models)}</span>
      <span class="model-alias-routing-summary"><span class="model-alias-field-label">选择方式</span>${renderRoutingSummary(item)}</span>
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
              <button type="button" data-model-alias-edit-open="${escapeAttribute(item.model_alias_id)}" aria-haspopup="dialog">编辑</button>
            </div>
          `
          : ""
      }
    </div>
  `;
}

function renderModelAliasEditForm(item, activeModels, environments) {
  const environmentOptions = environments.map((environment) => renderOption(
    environment.environment_id,
    environment.name ?? ENVIRONMENT_LABELS[environment.code] ?? environment.code ?? environment.environment_id,
    item.environment_id ?? "",
  )).join("");
  const defaultOptions = activeModels.map((model) => renderModelOption(model, item.default_model_id)).join("");
  const selectedModel = activeModels.find((model) => model.model_id === item.default_model_id);
  const selectedCandidate = item.routing_policy?.candidates?.find(
    (candidate) => candidate.model_id === item.default_model_id,
  );
  const selectedPrice = formatModelPrice(selectedModel ?? selectedCandidate);
  const selectedFallbackIds = item.fallback_model_ids ?? (item.fallback_model_id ? [item.fallback_model_id] : []);
  const routingPolicy = item.routing_policy ?? {};
  const routingMode = routingPolicy.routing_mode ?? item.effective_routing_mode ?? "fixed";
  const qualityConfig = normalizeQualityCostConfig(routingPolicy.quality_cost_config);
  const fallbackPolicy = item.fallback_policy ?? {};
  const fallbackEnabled = fallbackPolicy.status === "enabled";
  return `
    <form class="model-alias-edit-form" data-model-alias-edit-form data-model-alias-id="${escapeAttribute(item.model_alias_id)}" hidden>
      <label><span>统一入口名称</span><input name="alias" value="${escapeAttribute(item.alias ?? "")}" maxlength="120" required pattern="[a-z0-9](?:[a-z0-9._]|-){0,119}" /></label>
      <label><span>调用模型</span><select name="defaultModelId" required>${defaultOptions}</select><small class="model-alias-price-hint" data-model-price-hint>${escapeHtml(selectedPrice)}</small></label>
      ${environmentOptions
        ? `<label><span>运行环境</span><select name="environmentId" required>${environmentOptions}</select></label>`
        : `<input type="hidden" name="environmentId" value="${escapeAttribute(item.environment_id ?? "")}" />`}
      <label><span>路由状态</span><select name="aliasStatus"><option value="active" ${item.status === "active" ? "selected" : ""}>已启用</option><option value="disabled" ${item.status === "disabled" ? "selected" : ""}>已停用</option></select></label>
      <fieldset class="model-alias-routing-mode">
        <legend>选择方式</legend>
        <p class="model-alias-policy-version">当前配置版本：${escapeHtml(routingPolicy.version == null ? "未保存" : `v${routingPolicy.version}`)}</p>
        <div class="routing-mode-segments">
          ${ROUTING_MODES.map((mode) => `
            <label title="${escapeAttribute(ROUTING_MODE_DESCRIPTIONS[mode])}" data-routing-mode="${escapeAttribute(mode)}">
              <input type="radio" name="routingMode" value="${escapeAttribute(mode)}" ${routingMode === mode ? "checked" : ""} />
              <span>${escapeHtml(ROUTING_MODE_LABELS[mode])}</span>
            </label>
          `).join("")}
        </div>
        <p class="model-alias-mode-summary" data-routing-mode-summary>${escapeHtml(ROUTING_MODE_DESCRIPTIONS[routingMode] ?? ROUTING_MODE_DESCRIPTIONS.fixed)}</p>
      </fieldset>
      ${renderFallbackModelPicker(activeModels, selectedFallbackIds, item.default_model_id, routingMode)}
      ${renderAliasQualityCostEditor(qualityConfig, routingMode)}
      <fieldset class="model-alias-fallback-settings">
        <legend>故障切换</legend>
        <p class="model-alias-fallback-hint">当前模型失败后按顺序尝试下一模型，直到成功或全部失败。</p>
        <label class="fallback-policy-toggle">
          <input type="checkbox" name="fallbackEnabled" ${fallbackEnabled ? "checked" : ""} />
          <span>发生错误时使用备用模型</span>
        </label>
        <input type="hidden" name="triggerCondition" value="upstream_any_error" />
      </fieldset>
      <label class="model-alias-description-field"><span>备注</span><input name="description" maxlength="240" value="${escapeAttribute(item.description ?? "")}" /></label>
      <input type="hidden" name="expectedRoutingPolicyVersion" value="${escapeAttribute(item.routing_policy?.version ?? "")}" />
      <div class="model-alias-edit-actions">
        <button type="submit">保存修改</button>
      </div>
      <span data-model-alias-edit-feedback role="status"></span>
    </form>
  `;
}

function renderAliasQualityCostEditor(config, mode) {
  return `
    <fieldset class="model-alias-quality-cost" data-alias-quality-config ${mode === "quality_cost" ? "" : "hidden"}>
      <legend>质量、成本与延迟</legend>
      <input type="hidden" name="qualityWeightPercent" value="${escapeAttribute(formatBpsInput(config.quality_weight_bps))}" />
      <input type="hidden" name="costWeightPercent" value="${escapeAttribute(formatBpsInput(config.cost_weight_bps))}" />
      <input type="hidden" name="latencyWeightPercent" value="${escapeAttribute(formatBpsInput(config.latency_weight_bps))}" />
      <input type="hidden" name="qualityFloorPercent" value="${escapeAttribute(formatBpsInput(config.quality_floor_bps))}" />
      <input type="hidden" name="minimumSampleCount" value="${escapeAttribute(String(config.minimum_sample_count))}" />
      <p class="model-alias-quality-cost-note">系统会综合质量、成本和响应速度自动排序，无需手动调整权重。</p>
    </fieldset>
  `;
}

function normalizeQualityCostConfig(config) {
  const source = config && typeof config === "object" ? config : DEFAULT_QUALITY_COST_CONFIG;
  return Object.fromEntries(Object.keys(DEFAULT_QUALITY_COST_CONFIG).map((key) => [
    key,
    Number.isSafeInteger(source[key]) ? source[key] : DEFAULT_QUALITY_COST_CONFIG[key],
  ]));
}

function formatBpsInput(value) {
  const whole = Math.floor(Number(value) / 100);
  const fraction = String(Number(value) % 100).padStart(2, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""}`;
}

function renderModelAliasDialog(createForm, editForms) {
  return `
    <dialog class="model-alias-dialog" data-model-alias-dialog aria-labelledby="model-alias-dialog-title">
      <div class="model-alias-dialog-content">
        <div class="model-alias-dialog-heading">
          <div><span class="model-alias-dialog-kicker">模型路由</span><h3 id="model-alias-dialog-title" data-model-alias-dialog-title>添加路由</h3></div>
          <button type="button" class="icon-button" data-model-alias-dialog-close aria-label="关闭">×</button>
        </div>
        <div data-model-alias-dialog-view="create">${createForm}</div>
        <div data-model-alias-dialog-view="edit">${editForms}</div>
      </div>
    </dialog>
  `;
}

/**
 * 展示备用模型及其请求后 Fallback 策略状态。
 *
 * 调用前健康过滤直接选备用模型时不会显示“已触发”，这里只呈现静态配置。
 */
function renderFallbackSummary(item, models) {
  const fallbackIds = item.fallback_model_ids ?? (item.fallback_model_id ? [item.fallback_model_id] : []);
  const model = fallbackIds.length
    ? `
      <details class="model-alias-fallback-details">
        <summary>备用模型 ${fallbackIds.length} 个</summary>
        <ol>
          ${fallbackIds.map((modelId) => {
            return `<li>${renderModelSummary(resolveModelLabel(item, modelId, models), resolveModelProvider(item, modelId, models))}</li>`;
          }).join("")}
        </ol>
      </details>
    `
    : '<em>未配置备用模型</em>';
  const policy = item.fallback_policy;
  if (!policy) {
    return fallbackIds.length ? `${model}<em class="fallback-policy-state">自动切换未启用</em>` : model;
  }
  const status = policy.status === "enabled" ? "已启用" : "已停用";
  return `
    ${model}
    <em class="fallback-policy-state status-${escapeAttribute(policy.status)}">
      ${status} · 失败后继续尝试
    </em>
  `;
}

function renderRoutingSummary(item) {
  const policy = item.routing_policy ?? {};
  const mode = policy.routing_mode ?? item.effective_routing_mode ?? "fixed";
  const version = policy.version == null ? "未保存" : `v${policy.version}`;
  return `<strong>${escapeHtml(ROUTING_MODE_LABELS[mode] ?? mode)}</strong><em>${escapeHtml(version)}</em>`;
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
 * Resolve a user-facing model name without ever leaking the database UUID.
 * The model catalog is normally present; the routing policy candidate summary
 * keeps the same readable fallback when a background refresh has not arrived.
 */
function resolveModelLabel(item, modelId, models) {
  const model = models.find((candidate) => candidate.model_id === modelId);
  const candidate = item?.routing_policy?.candidates?.find((entry) => entry.model_id === modelId);
  return firstNonEmpty(
    model?.display_name,
    model?.provider_model_id,
    readableModelId(model?.internal_model_id),
    candidate?.display_name,
    candidate?.provider_model_id,
    readableModelId(candidate?.internal_model_id),
    modelId === item?.default_model_id ? item?.default_internal_model_id : item?.fallback_internal_model_id,
  )
    ?? "未命名模型";
}

function resolveModelProvider(item, modelId, models) {
  const model = models.find((candidate) => candidate.model_id === modelId);
  const candidate = item?.routing_policy?.candidates?.find((entry) => entry.model_id === modelId);
  return model?.provider_name ?? candidate?.provider_name ?? (modelId === item?.default_model_id ? item?.default_provider_name : item?.fallback_provider_name);
}

/**
 * 渲染模型别名操作错误。
 *
 * 普通用户只需要知道操作未完成；请求关联号保留在客户端错误对象和服务端日志中。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="model-alias-action-error" role="alert">
      ${escapeHtml(error.message ?? "模型别名操作失败")}
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
  const label = [firstNonEmpty(model.display_name, model.provider_model_id, readableModelId(model.internal_model_id)), model.provider_name].filter(Boolean).join(" / ");
  const price = formatModelPrice(model);
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  return `<option value="${escapeAttribute(model.model_id)}"${selected} data-price="${escapeAttribute(price)}">${escapeHtml(label)}</option>`;
}

function firstNonEmpty(...values) {
  return values.find((value) => typeof value === "string" && value.trim())?.trim();
}

function readableModelId(value) {
  const text = typeof value === "string" ? value.trim() : "";
  return /^[0-9a-f]{8}-[0-9a-f-]{27,}$/iu.test(text) ? "" : text;
}

function formatModelPrice(model) {
  const input = Number(model?.input_price_per_1k ?? model?.input_price_per_million);
  const output = Number(model?.output_price_per_1k ?? model?.output_price_per_million);
  const cache = Number(model?.cache_price_per_1k ?? model?.cache_price_per_million ?? model?.input_price_per_1k ?? model?.input_price_per_million);
  if (![input, output, cache].every(Number.isFinite)) return "价格未配置";
  const currency = String(model?.currency || "CNY");
  const format = (value) => value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "") || "0";
  return `输入 ${format(input)} / 输出 ${format(output)} / 缓存命中 ${format(cache)} ${currency} / 百万 Token`;
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
