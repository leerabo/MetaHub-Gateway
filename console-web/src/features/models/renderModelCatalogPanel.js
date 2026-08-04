import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 模型目录面板的能力和币种常量用于表单展示，真实模型元数据以控制面接口为准。
const CAPABILITY_OPTIONS = ["chat", "embeddings", "stream", "tools", "json_mode", "vision"];
const CURRENCY_OPTIONS = ["CNY", "USD"];

const CAPABILITY_LABELS = {
  chat: "Chat",
  embeddings: "Embeddings",
  stream: "Stream",
  tools: "Tools",
  json_mode: "JSON",
  vision: "Vision",
};

/**
 * 渲染模型目录面板。
 *
 * admin/developer 可读，只有 admin 可写；前端隐藏写按钮只是体验优化，后端仍会按
 * 登录角色校验创建和更新请求。
 */
export function renderModelCatalogPanel(modelsState, providersState, authContext) {
  // 权限判断消费后端快照，避免只靠角色名隐藏入口造成授权漂移。
  if (!hasPermission(authContext, PERMISSIONS.MODEL_READ)) {
    return "";
  }
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  if (!modelsState) {
    return `
      <div class="panel-heading">
        <h2 id="models-title">模型目录</h2>
        <p>正在读取当前租户 ModelCatalog。</p>
      </div>
      <div class="empty-state">等待 /models 响应。</div>
    `;
  }
  // 模型路由结果决定上游调用，异常时必须用统一错误码停止。
  if (modelsState.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="models-title">模型目录</h2>
        <p>模型目录暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(modelsState.message)} request_id: ${escapeHtml(modelsState.requestId ?? "-")}
      </div>
    `;
  }

  const canWrite = hasPermission(authContext, PERMISSIONS.MODEL_WRITE);
  const models = modelsState.models ?? [];
  return `
    <div class="panel-heading models-heading">
      <div>
        <h2 id="models-title">模型目录</h2>
        <p>统一展示内部标准模型 ID、Provider 上游模型 ID、能力、上下文和价格。</p>
      </div>
      <span class="models-count">${models.length} 个模型</span>
    </div>
    ${renderActionError(modelsState.actionError)}
    ${canWrite ? renderCreateModelForm(providersState, modelsState.feedback) : ""}
    <div class="models-grid" role="table" aria-label="当前租户模型目录">
      <div class="model-row model-head ${canWrite ? "" : "model-readonly-row"}" role="row">
        <span>内部模型</span>
        <span>Provider</span>
        <span>上游模型</span>
        <span>能力</span>
        <span>上下文</span>
        <span>价格</span>
        <span>状态</span>
        ${canWrite ? "<span>操作</span>" : ""}
      </div>
      ${models.length ? models.map((model) => renderModelRow(model, canWrite)).join("") : renderEmptyRow()}
    </div>
  `;
}

/**
 * 渲染新建模型表单。
 *
 * Provider 下拉来自当前租户 Provider 列表；没有 Provider 时禁用提交，避免创建
 * 无法路由的模型目录项。
 */
function renderCreateModelForm(providersState, feedback) {
  const providers = providersState?.state === "ok" ? providersState.providers ?? [] : [];
  const providerOptions = providers.length
    ? providers.map((provider) => renderOption(provider.provider_id, provider.name, providers[0].provider_id)).join("")
    : `<option value="">请先创建 Provider</option>`;
  return `
    <form class="create-model-form" id="create-model-form">
      <h3>新增模型</h3>
      <label>
        <span>内部模型 ID</span>
        <input name="internalModelId" maxlength="120" required placeholder="deepseek-chat" />
      </label>
      <label>
        <span>Provider</span>
        <select name="providerId" ${providers.length ? "" : "disabled"}>
          ${providerOptions}
        </select>
      </label>
      <label>
        <span>上游模型 ID</span>
        <input name="providerModelId" maxlength="160" required placeholder="deepseek-chat" />
      </label>
      <label>
        <span>显示名</span>
        <input name="displayName" maxlength="120" placeholder="DeepSeek Chat" />
      </label>
      <fieldset class="model-capabilities">
        <legend>能力</legend>
        ${CAPABILITY_OPTIONS.map((item) => renderCapabilityCheckbox(item)).join("")}
      </fieldset>
      <label>
        <span>上下文长度</span>
        <input name="contextWindow" type="number" min="0" step="1" value="0" />
      </label>
      <label>
        <span>输入/1K</span>
        <input name="inputPricePer1k" type="number" min="0" step="0.000001" value="0" />
      </label>
      <label>
        <span>输出/1K</span>
        <input name="outputPricePer1k" type="number" min="0" step="0.000001" value="0" />
      </label>
      <label>
        <span>币种</span>
        <select name="currency">
          ${CURRENCY_OPTIONS.map((item) => renderOption(item, item, "CNY")).join("")}
        </select>
      </label>
      <label>
        <span>价格版本</span>
        <input name="priceSchemaVersion" maxlength="80" placeholder="manual-202607" />
      </label>
      <label class="model-description-field">
        <span>备注</span>
        <input name="description" maxlength="240" placeholder="生产候选、测试模型或迁移兼容模型" />
      </label>
      <div class="model-actions">
        <button type="submit" ${providers.length ? "" : "disabled"}>创建模型</button>
        <span class="model-feedback" role="status">${escapeHtml(feedback ?? "")}</span>
      </div>
    </form>
  `;
}

/**
 * 渲染模型目录行。
 *
 * 每行展示内部模型 ID、上游模型 ID、能力、价格和状态，方便开发者核对路由目标。
 */
function renderModelRow(model, canWrite) {
  const nextStatus = model.status === "active" ? "disabled" : "active";
  const statusLabel = model.status === "active" ? "禁用" : "启用";
  return `
    <div class="model-row ${canWrite ? "" : "model-readonly-row"}" role="row">
      <span class="model-identity">
        <strong>${escapeHtml(model.internal_model_id)}</strong>
        <em>${escapeHtml(model.display_name || model.model_id)}</em>
      </span>
      <span>${escapeHtml(model.provider_name)}<em>${escapeHtml(model.provider_code)}</em></span>
      <code title="${escapeAttribute(model.provider_model_id)}">${escapeHtml(model.provider_model_id)}</code>
      <span class="capability-list">${renderCapabilities(model.capabilities)}</span>
      <span>${escapeHtml(model.context_window ?? 0)}</span>
      <span>${escapeHtml(formatPrice(model))}</span>
      <span><mark class="status-mark status-${escapeAttribute(model.status)}">${escapeHtml(model.status)}</mark></span>
      ${
        canWrite
          ? `
            <div class="model-row-actions">
              <form data-model-status-form data-model-id="${escapeAttribute(model.model_id)}">
                <input type="hidden" name="status" value="${escapeAttribute(nextStatus)}" />
                <button type="submit">${statusLabel}</button>
              </form>
            </div>
          `
          : ""
      }
    </div>
  `;
}

/**
 * 渲染模型能力复选框。
 *
 * Chat 和 Stream 作为常用默认能力预选，后端仍会对 capabilities 做结构化校验。
 */
function renderCapabilityCheckbox(value) {
  const checked = value === "chat" || value === "stream" ? " checked" : "";
  return `
    <label>
      <input type="checkbox" name="capabilities" value="${escapeAttribute(value)}"${checked} />
      <span>${escapeHtml(CAPABILITY_LABELS[value] ?? value)}</span>
    </label>
  `;
}

/**
 * 渲染模型能力标签。
 *
 * 只展示明确为 true 的能力，避免 false/null 被误读为可用。
 */
function renderCapabilities(capabilities = {}) {
  const enabled = CAPABILITY_OPTIONS.filter((item) => capabilities[item] === true);
  if (!enabled.length) {
    return `<em>-</em>`;
  }
  return enabled.map((item) => `<mark>${escapeHtml(CAPABILITY_LABELS[item] ?? item)}</mark>`).join("");
}

/**
 * 渲染模型目录操作错误。
 *
 * 保留 request_id，便于用户从控制台错误提示跳到后端日志。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="model-action-error" role="alert">
      ${escapeHtml(error.message ?? "模型目录操作失败")} request_id: ${escapeHtml(error.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染模型空列表行。
 *
 * 保持表格布局稳定，避免空状态造成列宽跳动。
 */
function renderEmptyRow() {
  return `
    <div class="model-row model-empty" role="row">
      <span>当前租户还没有模型目录记录。</span>
    </div>
  `;
}

/**
 * 格式化模型价格。
 *
 * 输入/输出价格都按每 1K token 展示，空值按 0 处理以匹配后端 M0 默认成本模型。
 */
function formatPrice(model) {
  const input = Number(model.input_price_per_1k ?? 0).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  const output = Number(model.output_price_per_1k ?? 0).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  // 成本与币种按模型目录口径处理，避免不同币种或估算值被混成一类。
  return `${input}/${output} ${model.currency || "CNY"}`;
}

/**
 * 渲染 select option。
 *
 * 表单值和显示文本均转义，防止 Provider 或模型名称影响页面结构。
 */
function renderOption(value, label, selectedValue) {
  const selected = value === selectedValue ? " selected" : "";
  return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(label)}</option>`;
}

/**
 * HTML 文本转义。
 *
 * 模型 ID、Provider 名称和描述都可能来自用户输入，渲染前必须统一处理。
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
 * data-*、title 和 value 属性额外转义反引号，避免属性边界被破坏。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
