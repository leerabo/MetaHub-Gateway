/** 本地模型服务的安全摘要渲染。页面不接收完整地址和凭据回显。 */

import { localEndpointPageOwner } from "./localEndpointPageState.js";

/** Render the standalone Local Models workspace only for an exact read navigation owner. */
export function renderLocalEndpoints(state, authContext, consoleShellState) {
  const owner = localEndpointPageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  return `
    <div class="local-endpoint-workspace" data-local-endpoint-root>
      <header class="local-endpoint-toolbar">
        <div>
          <p class="eyebrow">本地推理服务</p>
          <h3 id="local-models-title">本地模型服务</h3>
          <p>受控接入 vLLM、Ollama 与 OpenAI 兼容的推理服务。</p>
        </div>
        <div class="local-endpoint-commands">
          <button type="button" class="local-endpoint-icon" data-local-endpoint-refresh title="刷新" aria-label="刷新本地模型服务"${state.busy ? " disabled" : ""}>&#8635;</button>
          ${owner.canManage ? `<button type="button" class="primary" data-local-endpoint-open-create${state.busy ? " disabled" : ""}>创建本地服务</button>` : ""}
        </div>
      </header>
      <form class="local-endpoint-filters" data-local-endpoint-filters>
        <label><span>服务类型</span><select name="engine"${state.busy ? " disabled" : ""}>${option("", "全部", state.filters.engine)}${option("vllm", "vLLM", state.filters.engine)}${option("ollama", "Ollama", state.filters.engine)}${option("openai_compatible", "OpenAI 兼容", state.filters.engine)}</select></label>
        <label><span>状态</span><select name="status"${state.busy ? " disabled" : ""}>${option("", "全部", state.filters.status)}${option("active", "启用", state.filters.status)}${option("disabled", "停用", state.filters.status)}</select></label>
        <span class="local-endpoint-count">${numberText(state.items.length)} / ${numberText(state.totalCount)}</span>
      </form>
      ${renderFeedback(state.feedback)}
      ${renderList(state, owner)}
      ${renderCreateDialog(state, owner)}
    </div>
  `;
}

function renderList(state, owner) {
  if (state.state === "loading") {
    return `<div class="local-endpoint-empty" role="status">正在读取本地模型服务状态</div>`;
  }
  if (state.state === "error") return renderError(state.error);
  if (!state.items.length) {
    return `<div class="local-endpoint-empty">${state.totalCount ? "当前筛选没有匹配项" : "尚未创建本地模型服务"}</div>`;
  }
  return `
    <div class="local-endpoint-table-scroll" tabindex="0" aria-label="本地模型服务列表">
      <table class="local-endpoint-table">
        <thead><tr><th>服务</th><th>服务类型</th><th>模型</th><th>健康</th><th>模型数量</th><th>最近检查</th><th><span class="sr-only">操作</span></th></tr></thead>
        <tbody>${state.items.map((endpoint) => renderRow(endpoint, owner, state)).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderRow(endpoint, owner, state) {
  const testing = state.testingId === endpoint.local_endpoint_id;
  return `
    <tr>
      <th data-label="服务"><strong>${escapeHtml(endpoint.name)}</strong><span>${escapeHtml(endpoint.endpoint_hint)}</span></th>
      <td data-label="服务类型"><strong>${engineLabel(endpoint.engine_kind)}</strong><small>${endpoint.auth_type === "bearer" ? "访问令牌已配置" : "无认证"}</small></td>
      <td data-label="模型"><strong>${escapeHtml(endpoint.initial_model.display_name)}</strong><code>${escapeHtml(endpoint.initial_model.provider_model_id)}</code></td>
      <td data-label="健康"><span class="local-endpoint-status tone-${escapeAttribute(endpoint.health_status)}">${healthLabel(endpoint.health_status)}</span><small>${statusLabel(endpoint.status)}</small></td>
      <td data-label="模型数量"><strong>${numberText(endpoint.discovered_model_count)} 个</strong><small>${escapeHtml(endpoint.catalog_status)}</small></td>
      <td data-label="最近探测"><time>${formatTime(endpoint.last_tested_at)}</time><small>发现 ${formatTime(endpoint.last_discovered_at)}</small></td>
      <td data-label="操作" class="local-endpoint-action">${owner.canTest ? `<button type="button" data-local-endpoint-test="${escapeAttribute(endpoint.local_endpoint_id)}"${state.busy || endpoint.status === "disabled" ? " disabled" : ""}>${testing ? "测试中" : "测试"}</button>` : ""}</td>
    </tr>
  `;
}

function renderCreateDialog(state, owner) {
  if (!owner.canManage || !state.dialog?.open) return "";
  const busy = state.dialog.state === "submitting";
  return `
    <dialog class="local-endpoint-dialog" data-local-endpoint-dialog aria-labelledby="local-endpoint-dialog-title" aria-busy="${busy}">
      <header><div><h3 id="local-endpoint-dialog-title">创建本地模型服务</h3><span>同时保存服务地址和初始模型</span></div><button type="button" class="local-endpoint-icon" data-local-endpoint-close title="关闭" aria-label="关闭"${busy ? " disabled" : ""}>&times;</button></header>
      ${state.dialog.error ? renderError(state.dialog.error) : ""}
      <form data-local-endpoint-create-form novalidate autocomplete="off">
        <div class="local-endpoint-form-grid">
          <input type="hidden" name="provider_code" value="local_inference">
          <label class="local-endpoint-field"><span>名称</span><input name="name" maxlength="80" required${busy ? " disabled" : ""}></label>
        </div>
        <fieldset class="local-endpoint-segmented"${busy ? " disabled" : ""}>
          <legend>服务类型</legend>
          ${radio("engine_kind", "vllm", "vLLM", true)}
          ${radio("engine_kind", "ollama", "Ollama")}
          ${radio("engine_kind", "openai_compatible", "OpenAI 兼容")}
        </fieldset>
        <label class="local-endpoint-field"><span>服务地址</span><input name="base_url" type="url" maxlength="2048" autocomplete="off" spellcheck="false" placeholder="https://example.com/v1" required${busy ? " disabled" : ""}></label>
        <fieldset class="local-endpoint-segmented local-endpoint-auth"${busy ? " disabled" : ""}>
          <legend>认证</legend>
          ${radio("auth_type", "none", "无认证", true)}
          ${radio("auth_type", "bearer", "访问令牌")}
        </fieldset>
        <label class="local-endpoint-field" data-local-endpoint-token-field hidden><span>一次性 Token</span><input name="bearer_token" type="password" maxlength="8192" autocomplete="new-password" spellcheck="false"${busy ? " disabled" : ""}></label>
        <section class="local-endpoint-model-fields" aria-labelledby="local-endpoint-model-title">
          <h4 id="local-endpoint-model-title">初始模型</h4>
          <div class="local-endpoint-form-grid">
            <label class="local-endpoint-field"><span>模型名称</span><input name="provider_model_id" maxlength="160" required${busy ? " disabled" : ""}></label>
            <label class="local-endpoint-field"><span>显示名称</span><input name="display_name" maxlength="160" required${busy ? " disabled" : ""}></label>
            <input type="hidden" name="internal_model_id" value="">
            <input type="hidden" name="context_window" value="32768">
            <label class="local-endpoint-field"><span>输入价 / 百万 Token</span><input name="input_price_per_million" type="number" min="0" max="1000000000" step="0.000001" value="0" required${busy ? " disabled" : ""}></label>
            <label class="local-endpoint-field"><span>缓存命中价 / 百万 Token</span><input name="cache_price_per_million" type="number" min="0" max="1000000000" step="0.000001" value="0" required${busy ? " disabled" : ""}></label>
            <label class="local-endpoint-field"><span>输出价 / 百万 Token</span><input name="output_price_per_million" type="number" min="0" max="1000000000" step="0.000001" value="0" required${busy ? " disabled" : ""}></label>
          </div>
          <input type="hidden" name="currency" value="CNY">
        </section>
        <p class="local-endpoint-form-error" data-local-endpoint-form-error role="alert"></p>
        <div class="local-endpoint-dialog-actions"><button type="button" data-local-endpoint-close${busy ? " disabled" : ""}>取消</button><button type="submit" class="primary"${busy ? " disabled" : ""}>${busy ? "正在创建" : "创建"}</button></div>
      </form>
    </dialog>
  `;
}

function renderFeedback(feedback) {
  if (!feedback) return "";
  const label = feedback.code === "local_endpoint.created"
    ? "本地模型服务已创建"
    : feedback.code === "local_endpoint.replayed"
      ? "已返回原创建结果"
      : feedback.code === "local_endpoint.test_succeeded"
        ? "本地模型服务测试通过"
        : feedback.code;
  return `<div class="local-endpoint-feedback ${feedback.kind}" role="status"><strong>${escapeHtml(label)}</strong></div>`;
}

function renderError(error) {
  return `<div class="local-endpoint-error" role="alert"><strong>${escapeHtml(error?.message ?? "本地模型服务请求失败")}</strong></div>`;
}

function radio(name, value, label, checked = false) {
  return `<label><input type="radio" name="${name}" value="${value}"${checked ? " checked" : ""}><span>${label}</span></label>`;
}

function option(value, label, selected) {
  return `<option value="${value}"${value === selected ? " selected" : ""}>${label}</option>`;
}

function engineLabel(value) {
  return value === "vllm" ? "vLLM" : value === "ollama" ? "Ollama" : "OpenAI 兼容";
}

function healthLabel(value) {
  return ({ healthy: "健康", unhealthy: "异常", observing: "观察中", unknown: "未测试", disabled: "已停用" })[value] ?? "未知";
}

function statusLabel(value) {
  return value === "active" ? "启用" : "停用";
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function numberText(value) {
  return Number.isSafeInteger(value) && value >= 0 ? String(value) : "0";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
