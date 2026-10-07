/** 组织管理员可读的模型服务状态；异常规则不进入默认页面。 */

export function renderWorkspaceProviderEngineering(state = {}, provider = {}) {
  const response = state.response;
  const compatibility = response?.compatibility ?? null;
  const overrides = response?.overrides ?? [];
  const head = response?.head ?? null;
  return `<section class="workspace-provider-engineering" data-workspace-provider-engineering-root
    aria-labelledby="workspace-provider-engineering-title">
    <header class="workspace-provider-engineering-heading">
      <div><p>模型服务设置</p>
        <h3 id="workspace-provider-engineering-title">服务状态</h3>
        <span>${escapeHtml(provider.name ?? provider.provider_code ?? state.providerId)}</span></div>
      <div class="workspace-provider-engineering-heading-actions">
        <button type="button" data-workspace-provider-engineering-action="refresh" ${state.loading ? "disabled" : ""}>刷新</button>
        <button type="button" data-workspace-provider-engineering-action="close">关闭</button>
      </div>
    </header>
    ${notice(state)}
    ${compatibility ? compatibilityView(compatibility) : loadingState(state)}
    ${response && state.canManage ? `<details class="workspace-provider-advanced"><summary>高级异常处理</summary>${overrideWorkspace(state, overrides, head)}</details>` : ""}
  </section>`;
}

export function renderWorkspaceProviderErrorMaps(state = {}) {
  const response = state.response;
  const overrides = response?.overrides ?? [];
  const head = response?.head ?? null;
  return `<section class="panel workspace-provider-engineering workspace-provider-error-maps"
    data-workspace-provider-error-maps-root data-workspace-provider-engineering-root
    aria-labelledby="workspace-provider-error-maps-title">
    <header class="workspace-provider-engineering-heading">
      <div><p>组织范围 / 模型服务设置</p>
        <h2 id="workspace-provider-error-maps-title">高级异常处理</h2>
        <span>仅在需要调整异常处理方式时使用。</span></div>
      <div class="workspace-provider-engineering-heading-actions">
        <button type="button" data-workspace-provider-engineering-action="refresh"
          ${state.loading ? "disabled" : ""}>刷新</button>
      </div>
    </header>
    ${notice(state)}
    ${response
      ? overrideWorkspace(state, overrides, head, { globalScope: true })
      : '<div class="empty-state">正在读取模型服务错误规则。</div>'}
  </section>`;
}

function compatibilityView(item) {
  return `<section class="workspace-provider-compatibility" aria-label="模型服务状态">
    <div><span>连接状态</span>${status(item.health_status)}<small>${formatTime(item.projected_at)}</small></div>
    <div><span>使用状态</span><strong>${item.compatibility_status === "compatible" ? "可正常使用" : "需要关注"}</strong></div>
    <div><span>建议处理</span><strong>${item.compatibility_status === "compatible" ? "可以配置模型" : "检查服务地址或密钥"}</strong></div>
  </section>`;
}

function overrideWorkspace(state, overrides, head, { globalScope = false } = {}) {
  const filters = state.filters ?? {};
  return `<section class="workspace-provider-overrides" aria-labelledby="workspace-provider-overrides-title">
    <div class="workspace-provider-overrides-title"><div><p>匹配条件</p>
      <h4 id="workspace-provider-overrides-title">异常处理</h4></div>
      <span>${number(overrides.length)} 条规则</span></div>
    <form class="workspace-provider-override-filters" data-workspace-provider-override-filter-form>
      <label><span>状态</span><select name="status">${options(filters.status, [["", "全部"], ["active", "启用"], ["disabled", "停用"], ["archived", "归档"]])}</select></label>
      <button type="submit" ${state.loading ? "disabled" : ""}>筛选</button>
    </form>
    <div class="workspace-provider-override-list">
      ${overrides.map((item) => overrideRow(
        item,
        state.canManage,
        head?.revision,
        state.loading,
        globalScope,
      )).join("")
        || '<div class="empty-state">当前筛选没有匹配的错误处理规则。</div>'}
    </div>
    <div class="workspace-provider-override-pagination">
        <span>按更新时间翻页</span><div>
        <button type="button" data-workspace-provider-engineering-action="previous"
          ${state.history?.length && !state.loading ? "" : "disabled"}>上一页</button>
        <button type="button" data-workspace-provider-engineering-action="next"
          ${state.response?.next_cursor && !state.loading ? "" : "disabled"}>下一页</button>
      </div>
    </div>
    ${state.canManage
      ? createForm(state.providerId, head?.revision, state.loading, globalScope)
      : ""}
  </section>`;
}

function overrideRow(item, canManage, headRevision, busy, showProvider = false) {
  return `<article class="workspace-provider-override" data-workspace-provider-override="${escapeAttribute(item.provider_error_override_id)}">
    <header><div><strong>${escapeHtml(item.output_error_code)}</strong>
      <span>${showProvider ? `${escapeHtml(item.provider_name ?? "组织默认服务")} · ` : ""}优先级 ${number(item.priority)}</span></div>${status(item.status)}</header>
    <dl>
      <div><dt>HTTP 状态</dt><dd>${escapeHtml(item.http_statuses.join(", ") || item.http_status_class || "-")}</dd></div>
      <div><dt>错误字段</dt><dd>${escapeHtml([item.error_field, item.field_operator].filter(Boolean).join(" · ") || "-")}</dd></div>
      <div><dt>匹配内容</dt><dd>${escapeHtml(item.field_pattern ?? "-")}</dd></div>
      <div><dt>更新时间</dt><dd>${formatTime(item.updated_at)}</dd></div>
    </dl>
    ${canManage ? `<div class="workspace-provider-override-actions">
      ${(item.actions ?? []).filter((action) => new Set(["enable", "disable", "archive"]).has(action))
        .map((action) => transitionForm(item, action, headRevision, busy)).join("")}
      ${(item.actions ?? []).includes("update") ? updateForm(item, headRevision, busy) : ""}
    </div>` : ""}
  </article>`;
}

function transitionForm(item, action, headRevision, busy) {
  return `<form data-workspace-provider-override-transition-form autocomplete="off">
    <input type="hidden" name="action" value="${escapeAttribute(action)}" />
    <input type="hidden" name="overrideId" value="${escapeAttribute(item.provider_error_override_id)}" />
    <input type="hidden" name="expectedVersion" value="${number(item.version)}" />
    <input type="hidden" name="headRevision" value="${number(headRevision)}" />
    ${stepUpFields()}
    <button type="submit" ${busy ? "disabled" : ""}>${escapeHtml(actionLabel(action))}</button>
  </form>`;
}

function updateForm(item, headRevision, busy) {
  return `<details><summary>编辑规则</summary>
    <form data-workspace-provider-override-update-form autocomplete="off">
      <input type="hidden" name="overrideId" value="${escapeAttribute(item.provider_error_override_id)}" />
      <input type="hidden" name="providerId" value="${escapeAttribute(item.provider_id)}" />
      <input type="hidden" name="expectedVersion" value="${number(item.version)}" />
      <input type="hidden" name="headRevision" value="${number(headRevision)}" />
      ${conditionFields(item)}${stepUpFields()}
      <button type="submit" ${busy ? "disabled" : ""}>保存规则</button>
    </form></details>`;
}

function createForm(providerId, headRevision, busy, globalScope = false) {
  return `<details class="workspace-provider-override-create"><summary>新建规则</summary>
    <form data-workspace-provider-override-create-form autocomplete="off">
      ${globalScope
        ? `<label><span>模型服务编号</span><input name="providerId" maxlength="128"
          pattern="[A-Za-z0-9][A-Za-z0-9._:-]{0,127}" required /></label>`
        : `<input type="hidden" name="providerId" value="${escapeAttribute(providerId)}" />`}
      <input type="hidden" name="headRevision" value="${number(headRevision)}" />
      ${conditionFields({ priority: 500, http_statuses: [], output_error_code: "provider_error" })}
      ${stepUpFields()}
      <button type="submit" ${busy ? "disabled" : ""}>Create override</button>
    </form></details>`;
}

function conditionFields(item) {
  return `<label><span>优先级</span><input name="priority" type="number" min="1" max="1000" value="${number(item.priority)}" required /></label>
    <label><span>HTTP 状态码</span><input name="httpStatuses" maxlength="127" value="${escapeAttribute((item.http_statuses ?? []).join(","))}" placeholder="400、404、429" /></label>
    <label><span>状态码范围</span><select name="httpStatusClass">${options(item.http_status_class, [["", "不限制"], ["4xx", "4xx"]])}</select></label>
    <label><span>错误字段</span><select name="errorField">${options(item.error_field, [["", "不限制"], ["error.code", "错误代码"], ["error.type", "错误类型"], ["code", "代码"], ["type", "类型"]])}</select></label>
    <label><span>匹配方式</span><select name="fieldOperator">${options(item.field_operator, [["", "不限制"], ["equals", "完全匹配"], ["contains", "包含"]])}</select></label>
    <label><span>匹配内容</span><input name="fieldPattern" maxlength="240" value="${escapeAttribute(item.field_pattern ?? "")}" /></label>
    <label><span>统一错误码</span><input name="outputErrorCode" maxlength="128" pattern="[A-Za-z0-9][A-Za-z0-9._:-]{0,127}" value="${escapeAttribute(item.output_error_code ?? "")}" required /></label>`;
}

function stepUpFields() {
  return `<label><span>安全验证方式</span><select name="method" data-workspace-provider-step-up-method>
      <option value="totp">验证器代码</option><option value="webauthn">安全密钥</option></select></label>
    <label data-workspace-provider-step-up-code><span>验证码</span>
      <input name="verificationCode" data-workspace-provider-sensitive type="password"
        inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label>`;
}

function loadingState(state) {
  if (state.phase === "loading") return '<div class="empty-state">正在读取模型服务设置。</div>';
  if (state.phase === "permission_revoked") return '<div class="empty-state error-state">当前组织职责无权读取模型服务错误规则。</div>';
  return '<div class="empty-state">模型服务设置尚未加载。</div>';
}

function notice(state) {
  if (state.error) {
    return `<div class="provider-action-error" role="alert">${escapeHtml(state.error.message ?? "模型服务设置操作失败")}</div>`;
  }
  return state.feedback ? `<div class="provider-test-result" role="status">${escapeHtml(state.feedback)}</div>` : "";
}

function actionLabel(value) {
  return { enable: "启用", disable: "停用", archive: "归档" }[value] ?? value;
}

function status(value) {
  const normalized = String(value ?? "unknown");
  return `<mark class="status-mark status-${escapeAttribute(normalized)}">${escapeHtml({ active: "启用", disabled: "停用", archived: "已归档" }[normalized] ?? normalized)}</mark>`;
}

function options(selected, values) {
  return values.map(([value, label]) => `<option value="${escapeAttribute(value)}"${String(selected ?? "") === value ? " selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function formatTime(value) {
  const date = new Date(String(value ?? ""));
  return Number.isFinite(date.getTime()) ? date.toISOString().replace("T", " ").slice(0, 19) + "Z" : "-";
}

function number(value) {
  return Number.isFinite(Number(value)) ? String(Number(value)) : "0";
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
