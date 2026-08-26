import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

const COMPATIBILITY_LABELS = Object.freeze({
  native: "原生接入",
  openai_compatible: "OpenAI-compatible",
  migration_only: "仅迁移参考",
});

export function renderProviderCatalog(catalogState, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.PROVIDER_CATALOG_READ)) return "";
  if (!catalogState) return '<div class="empty-state">正在读取官方 Provider Catalog。</div>';
  if (catalogState.state === "error") {
    return `<div class="empty-state error-state">${escapeHtml(catalogState.message)} request_id: ${escapeHtml(catalogState.requestId ?? "-")}</div>`;
  }
  const entries = Array.isArray(catalogState.entries) ? catalogState.entries : [];
  const filters = catalogState.filters ?? {};
  const canWrite = hasPermission(authContext, PERMISSIONS.PROVIDER_WRITE);
  return `
    <section class="provider-catalog" aria-labelledby="provider-catalog-title">
      <div class="provider-catalog-heading">
        <div>
          <p>Signed catalog · Generation ${number(catalogState.generation)}</p>
          <h3 id="provider-catalog-title">官方 Provider 目录</h3>
        </div>
        <code title="Manifest SHA-256">${escapeHtml(shortHash(catalogState.manifest_sha256))}</code>
      </div>
      ${renderFeedback(catalogState)}
      ${renderCreatedCheckpoint(catalogState, canWrite)}
      <form class="provider-catalog-filters" data-provider-catalog-filter-form>
        <label><span>状态</span><select name="status">${options(filters.status, [["", "全部"], ["active", "可用"], ["deprecated", "已弃用"], ["retired", "已退役"]])}</select></label>
        <label><span>厂商</span><input name="vendorCode" value="${escapeAttribute(filters.vendorCode ?? "")}" pattern="[a-z0-9][a-z0-9_-]{1,63}" maxlength="64" placeholder="例如 deepseek" /></label>
        <label><span>能力</span><input name="capabilityCode" value="${escapeAttribute(filters.capabilityCode ?? "")}" pattern="[a-z][a-z0-9_]{1,63}" maxlength="64" placeholder="例如 chat" /></label>
        <label><span>区域</span><input name="regionCode" value="${escapeAttribute(filters.regionCode ?? "")}" pattern="[a-z0-9][a-z0-9-]{1,39}" maxlength="40" placeholder="例如 cn-beijing" /></label>
        <button type="submit">筛选</button>
      </form>
      <div class="provider-catalog-list">
        ${entries.map((entry) => renderEntry(entry, catalogState, canWrite)).join("") || '<div class="empty-state">当前筛选下没有目录条目。</div>'}
      </div>
    </section>`;
}

function renderEntry(entry, state, canWrite) {
  const compatibility = state.compatibilityByEntryCode?.[entry.entry_code];
  return `
    <article class="provider-catalog-entry" data-provider-catalog-entry="${escapeAttribute(entry.entry_code)}">
      <header>
        <div>
          <strong>${escapeHtml(entry.display_name)}</strong>
          <span>${escapeHtml(entry.vendor_code)} · ${escapeHtml(serviceClassLabel(entry.service_class))}</span>
        </div>
        <mark class="status-mark status-${escapeAttribute(entry.status)}">${escapeHtml(entry.status)}</mark>
      </header>
      <div class="provider-catalog-meta">
        <span><b>区域</b>${escapeHtml((entry.data_regions ?? []).join(" · "))}</span>
        <span><b>认证</b>${escapeHtml((entry.auth_modes ?? []).join(" / "))}</span>
        <span><b>合规</b>${escapeHtml((entry.compliance_tags ?? []).join(" · "))}</span>
        <span><b>兼容</b>${escapeHtml(COMPATIBILITY_LABELS[entry.compatibility?.level] ?? entry.compatibility?.level ?? "-")}</span>
      </div>
      <div class="provider-catalog-capabilities" aria-label="公开能力">
        ${(entry.capabilities ?? []).map((item) => `<span title="最大上下文 ${number(item.max_context_tokens)} tokens">${escapeHtml(item.code)}${item.supports_stream ? " · stream" : ""}${item.supports_tools ? " · tools" : ""}${item.supports_files ? " · files" : ""}</span>`).join("")}
      </div>
      ${renderCompatibility(entry, compatibility)}
      ${renderEntryAction(entry, state.version, canWrite)}
    </article>`;
}

function renderEntryAction(entry, version, canWrite) {
  if (entry.instantiation_mode === "local_endpoint") {
    return '<div class="provider-catalog-next"><span>本地地址必须走企业网络验证边界。</span><a href="/#local-models">前往本地模型</a></div>';
  }
  if (entry.instantiation_mode === "migration_only") {
    return '<div class="provider-catalog-next"><span>此条目仅说明迁移兼容性，不会创建共享或默认上游。</span></div>';
  }
  if (!canWrite) return '<div class="provider-catalog-next"><span>当前身份拥有目录只读权限。</span></div>';
  const endpoints = Array.isArray(entry.endpoints) ? entry.endpoints : [];
  const capabilities = Array.isArray(entry.capabilities) ? entry.capabilities : [];
  return `<form class="provider-catalog-create" data-provider-catalog-create-form autocomplete="off">
    <input type="hidden" name="entryCode" value="${escapeAttribute(entry.entry_code)}" />
    <input type="hidden" name="version" value="${escapeAttribute(version)}" />
    <label><span>Provider Code</span><input name="providerCode" value="${escapeAttribute(`${entry.entry_code}_official`)}" pattern="[a-z][a-z0-9_]{1,63}" maxlength="64" required /></label>
    <label><span>显示名称</span><input name="name" value="${escapeAttribute(entry.display_name)}" minlength="2" maxlength="120" required /></label>
    <label><span>数据区域</span><select name="regionCode">${endpoints.map((endpoint) => `<option value="${escapeAttribute(endpoint.region_code)}">${escapeHtml(endpoint.region_code)} · ${escapeHtml(endpoint.cloud_service_code)}</option>`).join("")}</select></label>
    <fieldset><legend>能力</legend>${capabilities.map((capability, index) => `<label class="provider-catalog-check"><input type="checkbox" name="capabilityCodes" value="${escapeAttribute(capability.code)}" ${index === 0 ? "checked" : ""} /><span>${escapeHtml(capability.code)}</span></label>`).join("")}</fieldset>
    <label class="provider-catalog-check provider-catalog-confirm"><input type="checkbox" name="confirmed" required /><span>确认以当前版本和区域创建不可变目录实例</span></label>
    <button type="submit">创建 Provider</button>
  </form>`;
}

function renderCompatibility(entry, response) {
  const profile = response?.compatibility ?? entry.compatibility ?? {};
  return `<div class="provider-catalog-compatibility">
    <button type="button" data-provider-catalog-compatibility data-entry-code="${escapeAttribute(entry.entry_code)}">兼容说明</button>
    ${response ? `<div role="status"><strong>${escapeHtml(COMPATIBILITY_LABELS[profile.level] ?? profile.level)}</strong><span>${escapeHtml(profile.doc_code ?? "-")}</span><small>差异：${escapeHtml((profile.request_difference_codes ?? []).join(" · ") || "无")} · 当前实例 ${number(response.current_instances)}</small></div>` : ""}
  </div>`;
}

function renderCreatedCheckpoint(state, canWrite) {
  const instance = state.lastCreated?.instance;
  const run = state.lastPreflight?.run;
  if (!instance) return "";
  return `<section class="provider-catalog-checkpoint" aria-label="目录创建检查点">
    <div><strong>${escapeHtml(instance.provider_code)} 已创建</strong><span>${escapeHtml(instance.entry_code)} · ${escapeHtml(instance.region_code)} · revision ${number(instance.revision)}</span><small>下一步：配置客户自持 Provider Key</small></div>
    ${canWrite ? `<form data-provider-catalog-preflight-form><input type="hidden" name="providerId" value="${escapeAttribute(instance.provider_id)}" /><label><span>超时（秒）</span><input name="timeoutSeconds" type="number" min="0.5" max="25" step="0.5" value="5" /></label><button type="submit">运行预检</button></form>` : ""}
    ${run ? `<div class="provider-catalog-preflight-result"><mark class="status-mark status-${escapeAttribute(run.status)}">${escapeHtml(run.status)}</mark><span>${escapeHtml(run.outcome_code)} · ${number(run.latency_ms)} ms${run.http_status ? ` · HTTP ${number(run.http_status)}` : ""}</span><code>${escapeHtml(shortHash(run.capability_sha256))}</code></div>` : ""}
  </section>`;
}

function renderFeedback(state) {
  if (state.actionError) {
    return `<div class="provider-feedback-error" role="alert"><strong>${escapeHtml(state.actionError.message ?? "操作失败")}</strong><span>request_id: ${escapeHtml(state.actionError.requestId ?? "-")}</span></div>`;
  }
  return state.feedback ? `<div class="provider-feedback" role="status">${escapeHtml(state.feedback)}</div>` : "";
}

function options(current, values) {
  return values.map(([value, label]) => `<option value="${escapeAttribute(value)}" ${value === current ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function serviceClassLabel(value) {
  return ({ official_cloud: "官方云服务", local_template: "本地模板", migration_source: "迁移来源" })[value] ?? value;
}

function shortHash(value) {
  const text = String(value ?? "");
  return /^[0-9a-f]{64}$/u.test(text) ? `${text.slice(0, 12)}…${text.slice(-8)}` : "-";
}

function number(value) {
  return Number.isFinite(Number(value)) ? String(Number(value)) : "-";
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
