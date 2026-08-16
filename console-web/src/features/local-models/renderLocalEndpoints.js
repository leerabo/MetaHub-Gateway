/** PAGE-116 safe aggregate rendering. Full Endpoint URLs and credentials are never accepted here. */

import { localEndpointPageOwner } from "./localEndpointPageState.js";

/** Render the standalone Local Models workspace only for an exact read navigation owner. */
export function renderLocalEndpoints(state, authContext, consoleShellState) {
  const owner = localEndpointPageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  return `
    <div class="local-endpoint-workspace" data-local-endpoint-root>
      <header class="local-endpoint-toolbar">
        <div>
          <p class="eyebrow">PAGE-116 · Local inference</p>
          <h3 id="local-models-title">本地模型 Endpoint</h3>
          <p>受控接入 vLLM、Ollama 与 OpenAI-compatible 推理服务。</p>
        </div>
        <div class="local-endpoint-commands">
          <button type="button" class="local-endpoint-icon" data-local-endpoint-refresh title="刷新" aria-label="刷新本地 Endpoint"${state.busy ? " disabled" : ""}>&#8635;</button>
          ${owner.canManage ? `<button type="button" class="primary" data-local-endpoint-open-create${state.busy ? " disabled" : ""}>创建 Endpoint</button>` : ""}
        </div>
      </header>
      <form class="local-endpoint-filters" data-local-endpoint-filters>
        <label><span>Engine</span><select name="engine"${state.busy ? " disabled" : ""}>${option("", "全部", state.filters.engine)}${option("vllm", "vLLM", state.filters.engine)}${option("ollama", "Ollama", state.filters.engine)}${option("openai_compatible", "OpenAI-compatible", state.filters.engine)}</select></label>
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
    return `<div class="local-endpoint-empty" role="status">正在读取本地 Endpoint 安全摘要</div>`;
  }
  if (state.state === "error") return renderError(state.error);
  if (!state.items.length) {
    return `<div class="local-endpoint-empty">${state.totalCount ? "当前筛选没有匹配项" : "尚未创建本地模型 Endpoint"}</div>`;
  }
  return `
    <div class="local-endpoint-table-scroll" tabindex="0" aria-label="本地模型 Endpoint 列表">
      <table class="local-endpoint-table">
        <thead><tr><th>Endpoint</th><th>Engine / 认证</th><th>初始模型</th><th>健康</th><th>目录</th><th>最近探测</th><th><span class="sr-only">操作</span></th></tr></thead>
        <tbody>${state.items.map((endpoint) => renderRow(endpoint, owner, state)).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderRow(endpoint, owner, state) {
  const testing = state.testingId === endpoint.local_endpoint_id;
  return `
    <tr>
      <th data-label="Endpoint"><strong>${escapeHtml(endpoint.name)}</strong><span>${escapeHtml(endpoint.endpoint_hint)}</span><code>${escapeHtml(endpoint.local_endpoint_id)}</code></th>
      <td data-label="Engine / 认证"><strong>${engineLabel(endpoint.engine_kind)}</strong><small>${endpoint.auth_type === "bearer" ? "Bearer 已配置" : "无认证"} · r${numberText(endpoint.revision)}</small></td>
      <td data-label="初始模型"><strong>${escapeHtml(endpoint.initial_model.display_name)}</strong><code>${escapeHtml(endpoint.initial_model.provider_model_id)}</code><small>${capabilityText(endpoint.initial_model.capabilities)}</small></td>
      <td data-label="健康"><span class="local-endpoint-status tone-${escapeAttribute(endpoint.health_status)}">${healthLabel(endpoint.health_status)}</span><small>${statusLabel(endpoint.status)}</small></td>
      <td data-label="目录"><strong>r${numberText(endpoint.catalog_revision)}</strong><small>${escapeHtml(endpoint.catalog_status)} · ${numberText(endpoint.discovered_model_count)} 个模型</small></td>
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
      <header><div><h3 id="local-endpoint-dialog-title">创建本地 Endpoint</h3><span>原子创建 Endpoint 与初始模型</span></div><button type="button" class="local-endpoint-icon" data-local-endpoint-close title="关闭" aria-label="关闭"${busy ? " disabled" : ""}>&times;</button></header>
      ${state.dialog.error ? renderError(state.dialog.error) : ""}
      <form data-local-endpoint-create-form novalidate autocomplete="off">
        <div class="local-endpoint-form-grid">
          <label class="local-endpoint-field"><span>Provider code</span><input name="provider_code" value="local_inference" maxlength="64" pattern="[a-z0-9][a-z0-9._-]*" required${busy ? " disabled" : ""}></label>
          <label class="local-endpoint-field"><span>名称</span><input name="name" maxlength="80" required${busy ? " disabled" : ""}></label>
        </div>
        <fieldset class="local-endpoint-segmented"${busy ? " disabled" : ""}>
          <legend>Engine</legend>
          ${radio("engine_kind", "vllm", "vLLM", true)}
          ${radio("engine_kind", "ollama", "Ollama")}
          ${radio("engine_kind", "openai_compatible", "OpenAI-compatible")}
        </fieldset>
        <label class="local-endpoint-field"><span>Endpoint URL</span><input name="base_url" type="url" maxlength="2048" autocomplete="off" spellcheck="false" placeholder="https://inference.internal/v1" required${busy ? " disabled" : ""}></label>
        <fieldset class="local-endpoint-segmented local-endpoint-auth"${busy ? " disabled" : ""}>
          <legend>认证</legend>
          ${radio("auth_type", "none", "无认证", true)}
          ${radio("auth_type", "bearer", "Bearer Token")}
        </fieldset>
        <label class="local-endpoint-field" data-local-endpoint-token-field hidden><span>一次性 Token</span><input name="bearer_token" type="password" maxlength="8192" autocomplete="new-password" spellcheck="false"${busy ? " disabled" : ""}></label>
        <section class="local-endpoint-model-fields" aria-labelledby="local-endpoint-model-title">
          <h4 id="local-endpoint-model-title">初始模型</h4>
          <div class="local-endpoint-form-grid">
            <label class="local-endpoint-field"><span>内部模型 ID</span><input name="internal_model_id" maxlength="160" required${busy ? " disabled" : ""}></label>
            <label class="local-endpoint-field"><span>上游模型 ID</span><input name="provider_model_id" maxlength="160" required${busy ? " disabled" : ""}></label>
            <label class="local-endpoint-field"><span>显示名称</span><input name="display_name" maxlength="160" required${busy ? " disabled" : ""}></label>
            <label class="local-endpoint-field"><span>上下文窗口</span><input name="context_window" type="number" min="1" max="10000000" step="1" value="32768" required${busy ? " disabled" : ""}></label>
            <label class="local-endpoint-field"><span>输入价 / 1K</span><input name="input_price_per_1k" type="number" min="0" max="1000000" step="0.000001" value="0" required${busy ? " disabled" : ""}></label>
            <label class="local-endpoint-field"><span>输出价 / 1K</span><input name="output_price_per_1k" type="number" min="0" max="1000000" step="0.000001" value="0" required${busy ? " disabled" : ""}></label>
          </div>
          <fieldset class="local-endpoint-capabilities"${busy ? " disabled" : ""}>
            <legend>能力</legend>
            <label><input type="checkbox" name="capabilities" value="chat" checked><span>Chat</span></label>
            <label><input type="checkbox" name="capabilities" value="stream" checked><span>Stream</span></label>
            <label><input type="checkbox" name="capabilities" value="embeddings"><span>Embeddings</span></label>
            <label><input type="checkbox" name="capabilities" value="tools"><span>Tools</span></label>
            <label><input type="checkbox" name="capabilities" value="json_mode"><span>JSON mode</span></label>
            <label><input type="checkbox" name="capabilities" value="vision"><span>Vision</span></label>
          </fieldset>
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
    ? "Endpoint 已创建"
    : feedback.code === "local_endpoint.replayed"
      ? "已返回原创建结果"
      : feedback.code === "local_endpoint.test_succeeded"
        ? "Endpoint 测试通过"
        : feedback.code;
  return `<div class="local-endpoint-feedback ${feedback.kind}" role="status"><strong>${escapeHtml(label)}</strong><span>request_id: ${escapeHtml(feedback.requestId)}</span></div>`;
}

function renderError(error) {
  return `<div class="local-endpoint-error" role="alert"><strong>${escapeHtml(error?.message ?? "本地 Endpoint 请求失败")}</strong><span><code>${escapeHtml(error?.code ?? "local_endpoint.unavailable")}</code> · request_id: ${escapeHtml(error?.requestId ?? "-")}</span></div>`;
}

function radio(name, value, label, checked = false) {
  return `<label><input type="radio" name="${name}" value="${value}"${checked ? " checked" : ""}><span>${label}</span></label>`;
}

function option(value, label, selected) {
  return `<option value="${value}"${value === selected ? " selected" : ""}>${label}</option>`;
}

function engineLabel(value) {
  return value === "vllm" ? "vLLM" : value === "ollama" ? "Ollama" : "OpenAI-compatible";
}

function healthLabel(value) {
  return ({ healthy: "健康", unhealthy: "异常", observing: "观察中", unknown: "未测试", disabled: "已停用" })[value] ?? "未知";
}

function statusLabel(value) {
  return value === "active" ? "启用" : "停用";
}

function capabilityText(value) {
  return ["chat", "stream", "embeddings", "tools", "json_mode", "vision"]
    .filter((key) => value?.[key] === true)
    .join(" · ") || "-";
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
