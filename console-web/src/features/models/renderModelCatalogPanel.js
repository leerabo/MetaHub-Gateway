import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { renderWorkspaceConnectionStatus } from "../workspace-navigation/renderWorkspaceNavigation.js";

// 计价选项仍由管理员维护；模型能力由目录和后端路由自动判断。
const CURRENCY_OPTIONS = ["CNY", "USD"];

/**
 * 渲染模型目录面板。
 *
 * admin/developer 可读，只有 admin 可写；前端隐藏写按钮只是体验优化，后端仍会按
 * 登录角色校验创建和更新请求。
 */
export function renderModelCatalogPanel(modelsState, providersState, authContext, workspaceTaskHomeState, i18n) {
  // 权限判断消费后端快照，避免只靠角色名隐藏入口造成授权漂移。
  if (!hasPermission(authContext, PERMISSIONS.MODEL_READ)) {
    return "";
  }
  // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
  if (!modelsState) {
    return `
      <div class="panel-heading">
        <h2 id="models-title">模型目录</h2>
        <p>正在加载模型目录...</p>
      </div>
      <div class="empty-state" role="status">正在加载...</div>
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
        ${escapeHtml(modelsState.message ?? "模型目录读取失败。")}
        <button type="button" data-resource-retry>重试</button>
      </div>
    `;
  }

  const canWrite = hasPermission(authContext, PERMISSIONS.MODEL_WRITE);
  const models = modelsState.models ?? [];
  return `
    <div class="panel-heading models-heading">
      <div>
        <h2 id="models-title">模型目录</h2>
      </div>
      <span class="models-count">${models.length} 个模型</span>
      ${canWrite ? '<button type="button" class="primary-command" data-model-create-open aria-haspopup="dialog">添加模型</button>' : ""}
    </div>
    ${renderWorkspaceConnectionStatus(workspaceTaskHomeState, "workspace.gateway.models", i18n)}
    ${renderActionError(modelsState.actionError)}
    ${modelsState.feedback ? `<p class="model-feedback" role="status">${escapeHtml(modelsState.feedback)}</p>` : ""}
    ${canWrite ? `<dialog class="model-create-dialog" data-model-create-dialog aria-labelledby="model-create-dialog-title"><div class="model-create-dialog-content"><header><div><span class="eyebrow">模型目录</span><h3 id="model-create-dialog-title">添加模型</h3></div><button type="button" class="icon-button" data-model-create-close aria-label="关闭">×</button></header>${renderCreateModelForm(providersState, modelsState.feedback, authContext)}</div></dialog>` : ""}
    <div class="models-grid" role="table" aria-label="当前租户模型目录">
      <div class="model-row model-head ${canWrite ? "" : "model-readonly-row"}" role="row">
        <span>显示名称</span>
        <span>模型服务</span>
        <span>模型名称</span>
        <span>单价 / 百万 Token</span>
        <span>状态</span>
        ${canWrite ? "<span>操作</span>" : ""}
      </div>
      ${models.length ? models.map((model) => renderModelRow(model, canWrite)).join("") : renderEmptyRow()}
    </div>
    ${canWrite ? models.map(renderModelEditDialog).join("") : ""}
  `;
}

/**
 * 渲染新建模型表单。
 *
 * Provider 下拉来自当前租户 Provider 列表；没有 Provider 时禁用提交，避免创建
 * 无法路由的模型目录项。
 */
function renderCreateModelForm(providersState, feedback, authContext) {
  if (providersState?.state === "error") {
    return '<div class="empty-state error-state" role="alert">模型服务列表加载失败。<button type="button" data-resource-retry>重试</button></div>';
  }
  if (providersState?.state !== "ok") return '<p role="status">正在加载模型服务...</p>';
  const providers = (providersState.providers ?? []).filter((provider) => provider.status === "active");
  if (!providers.length) return '<p class="empty-state" role="status">暂无已启用的模型服务。'
    + (hasPermission(authContext, PERMISSIONS.PROVIDER_READ)
      ? '<button type="button" class="empty-state-action" data-console-nav="providers" data-console-route="workspace.gateway.providers" data-console-target="providers">配置模型服务</button>' : '请联系组织管理员。') + '</p>';
  const selectedProviderId = providers.length === 1 ? providers[0].provider_id : "";
  const providerOptions = providers.length
    ? `${providers.length > 1 ? '<option value="">请选择模型服务</option>' : ""}${providers.map((provider) => renderOption(provider.provider_id, provider.name, selectedProviderId)).join("")}`
    : `<option value="">请先接入模型服务</option>`;
  return `
    <form class="create-model-form" id="create-model-form">
      <h3>新增模型</h3>
      <label>
        <span>模型服务</span>
        <select name="providerId" required ${providers.length ? "" : "disabled"}>
          ${providerOptions}
        </select>
      </label>
      <div class="model-discovery-field">
        <label>
          <span>模型名称</span>
          <input name="providerModelId" maxlength="160" required placeholder="从列表选择，或手动输入模型名称" aria-describedby="model-discovery-feedback" />
          <select data-discovered-models aria-label="选择厂家模型" hidden disabled></select>
        </label>
        ${hasPermission(authContext, PERMISSIONS.PROVIDER_HEALTH_PROBE) ? '<button type="button" data-discover-models>自动读取模型列表</button>' : ""}
        <button type="button" data-model-manual hidden>手动填写</button>
        <small id="model-discovery-feedback" data-model-discovery-feedback role="status">读取不到模型列表时可手动填写。</small>
      </div>
      <section class="model-pricing" aria-labelledby="model-pricing-title">
      <div class="model-pricing-heading"><h4 id="model-pricing-title">计价设置</h4><span>填写价格或确认暂按 0 保存</span></div>
      <p class="model-pricing-hint">填写组织实际采用的单价；中转站价格请按实际账单核对。留空或填 0 将按零价格保存。</p>
      <div class="model-price-fields">
      <label>
        <span>输入价格 / 百万 Token</span>
        <input name="inputPricePerMillion" type="number" min="0" step="0.000001" value="0" />
      </label>
      <label>
        <span>输出价格 / 百万 Token</span>
        <input name="outputPricePerMillion" type="number" min="0" step="0.000001" value="0" />
      </label>
      <label>
        <span>缓存命中价格 / 百万 Token</span>
        <input name="cachePricePerMillion" type="number" min="0" step="0.000001" value="0" />
      </label>
      <label>
        <span>币种</span>
        <select name="currency">
          ${CURRENCY_OPTIONS.map((item) => renderOption(item, item, "CNY")).join("")}
        </select>
      </label>
      </div>
      <details class="model-pricing-advanced"><summary>更多选项：价格倍数和备注</summary><div class="model-advanced-fields">
      <label>
        <span>价格倍数</span>
        <input name="priceMultiplier" type="number" min="0" step="0.01" value="1" aria-describedby="model-price-multiplier-help" />
        <small id="model-price-multiplier-help">例如 0.12 表示官方价格的 12%，1.05 表示上浮 5%。</small>
      </label>
      <label class="model-description-field">
        <span>备注</span>
        <input name="description" maxlength="240" placeholder="例如：默认聊天模型" />
      </label>
      </div></details>
      <input type="hidden" name="pricingConfigured" value="false" data-model-pricing-configured />
      <label class="model-unpriced-confirmation">
        <input name="unpricedConfirmed" type="checkbox" />
        <span><strong>价格暂按 0 保存</strong><small>我已了解：模型调用仍会记录 Token 用量，价格之后可以修改。</small></span>
      </label>
      </section>
      <div class="model-actions">
        <button type="submit" ${providers.length ? "" : "disabled"}>创建模型</button>
        <span class="model-feedback" data-model-setup-feedback role="status">${escapeHtml(feedback ?? "")}</span>
        <button type="button" data-resource-retry data-model-setup-reload hidden>刷新列表</button>
      </div>
    </form>
  `;
}

/**
 * 渲染模型目录行。
 *
 * 页面展示用户可理解的模型名称、模型服务、价格和状态；路由所需内部值只留在表单数据中。
 */
function renderModelRow(model, canWrite) {
  const nextStatus = model.status === "active" ? "disabled" : "active";
  const statusLabel = model.status === "active" ? "禁用" : "启用";
  return `
    <div class="model-row ${canWrite ? "" : "model-readonly-row"}" role="row">
      <span class="model-identity"><span class="model-field-label">显示名称</span>
        <strong>${escapeHtml(model.display_name ?? model.internal_model_id ?? model.provider_model_id ?? "未命名模型")}</strong>
      </span>
      <span><span class="model-field-label">模型服务</span>${escapeHtml(model.provider_name ?? "未命名服务")}</span>
      <span><span class="model-field-label">模型名称</span><code title="${escapeAttribute(model.provider_model_id)}">${escapeHtml(model.provider_model_id)}</code></span>
      <span><span class="model-field-label">计价</span>${escapeHtml(formatPrice(model))}</span>
      <span><span class="model-field-label">状态</span><mark class="status-mark status-${escapeAttribute(model.status)}">${escapeHtml(model.status === "active" ? "已启用" : model.status === "disabled" ? "已停用" : "状态未知")}</mark></span>
      ${
        canWrite
          ? `
            <div class="model-row-actions">
              <button type="button" data-model-edit-open="${escapeAttribute(model.model_id)}" aria-haspopup="dialog">编辑</button>
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

function renderModelEditDialog(model) {
  const name = model.display_name || model.internal_model_id || model.provider_model_id || "未命名模型";
  // 旧记录可能没有 price_schema_version，但已经保存了有效价格；这些记录也必须
  // 在编辑窗口中回填，而不能被当成“未配置价格”清空表单。
  const hasPrice = hasConfiguredPrice(model);
  const currency = model.currency || "CNY";
  const currencies = [...new Set([...CURRENCY_OPTIONS, currency])];
  return `<dialog class="model-edit-dialog" data-model-edit-dialog="${escapeAttribute(model.model_id)}" aria-labelledby="model-edit-dialog-title-${escapeAttribute(model.model_id)}">
    <div class="model-edit-dialog-content">
      <div class="model-alias-dialog-heading model-edit-dialog-heading">
        <div><span class="model-alias-dialog-kicker">模型目录</span><h3 id="model-edit-dialog-title-${escapeAttribute(model.model_id)}">编辑模型</h3><p class="model-edit-context" data-user-content="true"><span>服务：${escapeHtml(model.provider_name || "未命名服务")}</span><span>模型标识：${escapeHtml(model.provider_model_id || "未填写")}</span></p></div>
        <button type="button" class="icon-button" data-model-edit-close aria-label="关闭">×</button>
      </div>
      <form class="model-edit-form" data-model-edit-form data-model-id="${escapeAttribute(model.model_id)}">
        <div class="model-edit-fields">
          <label><span>显示名称</span><input name="displayName" value="${escapeAttribute(name)}" maxlength="120" /></label>
          <label><span>模型名称</span><input name="providerModelId" value="${escapeAttribute(model.provider_model_id ?? "")}" maxlength="160" required data-user-content="true" /></label>
        </div>
        <section class="model-edit-pricing">
          <label class="model-edit-price-toggle"><input type="checkbox" name="pricingEnabled" ${hasPrice ? "checked" : ""} /><span>配置价格</span></label>
          <div class="model-edit-price-fields" data-model-edit-price-fields ${hasPrice ? "" : "hidden"}>
            <label><span>输入价格 / 百万 Token</span><input name="inputPricePerMillion" type="number" inputmode="decimal" min="0" step="0.000001" value="${escapeAttribute(hasPrice ? toMillionPrice(model.input_price_per_1k) : "")}" required ${hasPrice ? "" : "disabled"} /></label>
            <label><span>输出价格 / 百万 Token</span><input name="outputPricePerMillion" type="number" inputmode="decimal" min="0" step="0.000001" value="${escapeAttribute(hasPrice ? toMillionPrice(model.output_price_per_1k) : "")}" required ${hasPrice ? "" : "disabled"} /></label>
            <label><span>缓存命中价格 / 百万 Token</span><input name="cachePricePerMillion" type="number" inputmode="decimal" min="0" step="0.000001" value="${escapeAttribute(hasPrice ? toMillionPrice(model.cache_price_per_1k ?? model.input_price_per_1k) : "")}" required ${hasPrice ? "" : "disabled"} /></label>
            <label><span>币种</span><select name="currency" ${hasPrice ? "" : "disabled"}>${currencies.map((item) => renderOption(item, { CNY: "人民币（CNY）", USD: "美元（USD）" }[item] ?? item, currency)).join("")}</select></label>
          </div>
        </section>
        <label><span>价格倍数</span><input name="priceMultiplier" type="number" inputmode="decimal" min="0" step="0.01" value="1" /></label>
        <label><span>备注</span><input name="description" maxlength="240" value="${escapeAttribute(model.description ?? "")}" /></label>
        <p class="model-edit-feedback" data-model-edit-feedback role="status" hidden></p>
        <footer class="model-edit-actions model-alias-edit-actions"><button type="button" data-model-edit-close>取消</button><button type="submit" class="primary-command">保存修改</button></footer>
      </form>
    </div>
  </dialog>`;
}

function hasConfiguredPrice(model) {
  if (Boolean(String(model?.price_schema_version ?? "").trim())) return true;
  return [
    model?.input_price_per_1k ?? model?.input_price_per_million,
    model?.output_price_per_1k ?? model?.output_price_per_million,
    model?.cache_price_per_1k ?? model?.cache_price_per_million,
  ].some((value) => Number.isFinite(Number(value)) && Number(value) >= 0 && String(value).trim() !== "");
}

/**
 * 渲染模型目录操作错误。
 *
 * 普通用户只需要知道操作未完成；请求关联号保留在客户端错误对象和服务端日志中。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="model-action-error" role="alert">
      ${escapeHtml(error.message ?? "模型目录操作失败")}
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
      <span>尚未添加模型。</span>
    </div>
  `;
}

/**
 * 格式化模型价格。
 *
 * 后端字段名保留历史 *_per_1k 兼容性；当前值与界面统一按每百万 Token 处理。
 */
function formatPrice(model) {
  const inputValue = Number(model.input_price_per_1k ?? model.input_price_per_million);
  const outputValue = Number(model.output_price_per_1k ?? model.output_price_per_million);
  const cacheValue = Number(model.cache_price_per_1k ?? model.cache_price_per_million ?? model.input_price_per_1k ?? model.input_price_per_million);
  if (!Number.isFinite(inputValue) || !Number.isFinite(outputValue) || !Number.isFinite(cacheValue)) return "未配置价格";
  // 兼容旧记录：早期表单可能保存了金额但没有内部价格版本，金额本身仍应在目录中显示。
  const hasPriceVersion = Boolean(String(model.price_schema_version ?? "").trim());
  const hasEnteredPrice = [
    model.input_price_per_1k ?? model.input_price_per_million,
    model.output_price_per_1k ?? model.output_price_per_million,
    model.cache_price_per_1k ?? model.cache_price_per_million,
  ].some((value) => Number.isFinite(Number(value)) && Number(value) >= 0 && String(value).trim() !== "");
  if (!hasPriceVersion && !hasEnteredPrice) return "未配置价格";
  const input = toMillionPrice(inputValue);
  const output = toMillionPrice(outputValue);
  // 成本与币种按模型目录口径处理，避免缺价被误显示成免费。
  const cache = toMillionPrice(cacheValue);
  return `输入 ${input} / 输出 ${output} / 缓存命中 ${cache} ${model.currency || "CNY"} / 百万 Token`;
}

function toMillionPrice(value) {
  const number = Number(value);
  return (Number.isFinite(number) ? number : 0).toFixed(6).replace(/0+$/, "").replace(/\.$/, "") || "0";
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
