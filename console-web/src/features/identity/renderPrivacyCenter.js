const CANCELLABLE = new Set(["received", "identity_verified", "collecting"]);

export function renderPrivacyCenter(state = {}) {
  if (!state.privacyVisible || !state.workspaceContext) return "";
  const response = state.privacyPreferences;
  const preference = response?.preference ?? null;
  const requests = Array.isArray(state.privacyRequests?.items) ? state.privacyRequests.items : [];
  const busy = state.privacyBusy === true;
  return `
    <section class="identity-privacy" aria-labelledby="identity-privacy-title" aria-busy="${busy}">
      <div class="identity-section-heading">
        <div><p>Privacy center</p><h2 id="identity-privacy-title">隐私与数据请求</h2></div>
        <button class="identity-icon-command" type="button" data-identity-action="refresh-privacy"
          title="刷新隐私中心" aria-label="刷新隐私中心" ${busy ? "disabled" : ""}>&#8635;</button>
      </div>
      ${renderError(state.privacyError)}
      ${preference ? renderPreferences(
        preference, response.consent_records ?? [], busy, state.privacyCanManage === true,
      ) : renderUnavailable(busy)}
      ${state.privacyCanCreate ? renderRequestForm(busy) : ""}
      ${renderRequests(requests, state, busy)}
    </section>
  `;
}

function renderPreferences(preference, consentRecords, busy, canManage) {
  const recent = [...consentRecords].slice(0, 8);
  return `
    <form class="identity-privacy-preferences" data-privacy-preference-form autocomplete="off">
      <div class="identity-privacy-summary">
        <div><span>条款版本</span><strong>${escapeHtml(preference.terms_version)}</strong></div>
        <div><span>语言</span><strong>${escapeHtml(preference.locale)}</strong></div>
        <div><span>修订</span><strong>${number(preference.revision)}</strong></div>
        <div><span>更新时间</span><strong>${formatTime(preference.updated_at)}</strong></div>
      </div>
      <div class="identity-privacy-toggles">
        ${toggle("analyticsEnabled", "产品分析", preference.analytics_enabled, busy || !canManage)}
        ${toggle("productUpdatesEnabled", "产品更新", preference.product_updates_enabled, busy || !canManage)}
        ${toggle("modelImprovementEnabled", "模型改进", preference.model_improvement_enabled, busy || !canManage)}
      </div>
      <input type="hidden" name="termsVersion" value="${escapeAttribute(preference.terms_version)}" />
      <input type="hidden" name="locale" value="${escapeAttribute(preference.locale)}" />
      ${canManage ? `<div class="identity-privacy-actions">
        <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>保存偏好</button>
      </div>` : ""}
    </form>
    <details class="identity-privacy-consents">
      <summary>同意记录 <span>${number(consentRecords.length)}</span></summary>
      <div class="identity-privacy-consent-list">
        ${recent.length ? recent.map(renderConsent).join("") : '<p class="identity-empty">暂无同意记录。</p>'}
      </div>
    </details>
  `;
}

function renderRequestForm(busy) {
  return `
    <form class="identity-privacy-request-form" data-privacy-request-form autocomplete="off">
      <div class="identity-section-heading">
        <div><p>Data subject request</p><h3>新建请求</h3></div>
      </div>
      <fieldset class="identity-privacy-kind">
        <legend>请求类型</legend>
        ${radio("export", "导出", true, busy)}
        ${radio("rectify", "更正", false, busy)}
        ${radio("erase", "删除", false, busy)}
      </fieldset>
      <div class="identity-step-up-fields">
        <label><span>验证方式</span><select name="stepUpMethod" ${busy ? "disabled" : ""}>
          <option value="totp">动态验证码</option>
          <option value="webauthn">安全密钥</option>
          <option value="recovery_code">恢复码</option>
        </select></label>
        <label><span>验证码或恢复码</span><input name="stepUpCode" type="password"
          autocomplete="one-time-code" maxlength="20" ${busy ? "disabled" : ""} /></label>
      </div>
      <label class="identity-confirm"><input name="confirmed" type="checkbox" required ${busy ? "disabled" : ""} /><span>确认提交本人请求</span></label>
      <div class="identity-privacy-actions">
        <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>提交请求</button>
      </div>
    </form>
  `;
}

function renderRequests(requests, state, busy) {
  return `
    <div class="identity-privacy-requests">
      <div class="identity-section-heading">
        <div><p>Request history</p><h3>请求进度</h3></div><span>${number(requests.length)} 项</span>
      </div>
      ${requests.length
        ? requests.map((item) => renderRequest(item, state, busy)).join("")
        : '<p class="identity-empty">暂无数据主体请求。</p>'}
    </div>
  `;
}

function renderRequest(item, state, busy) {
  const canCancel = state.privacyCanCancel && CANCELLABLE.has(item.status);
  const canDownload = item.request_kind === "export" && item.status === "ready"
    && item.identity_export?.status === "ready";
  return `
    <article class="identity-privacy-request">
      <header>
        <div><strong>${escapeHtml(kindLabel(item.request_kind))}</strong><code>${escapeHtml(item.request_code)}</code></div>
        <span class="identity-privacy-status is-${escapeAttribute(statusKind(item.status))}">${escapeHtml(statusLabel(item.status))}</span>
      </header>
      <dl class="identity-privacy-request-meta">
        <div><dt>收到</dt><dd>${formatTime(item.received_at)}</dd></div>
        <div><dt>法定期限</dt><dd>${formatTime(item.deadline_at)}</dd></div>
        <div><dt>修订</dt><dd>${number(item.revision)}</dd></div>
      </dl>
      <div class="identity-privacy-owner-grid" aria-label="分域进度">
        ${item.owner_progress.map(renderOwnerProgress).join("")}
      </div>
      ${renderExceptions(item.retention_exceptions ?? [])}
      ${renderDecisions(item.erasure_decisions ?? [])}
      <div class="identity-privacy-request-actions">
        ${canDownload ? `<button class="identity-primary" type="button" data-identity-action="download-privacy-export"
          data-privacy-request-id="${escapeAttribute(item.data_subject_request_id)}" ${busy ? "disabled" : ""}>下载导出</button>` : ""}
        ${canCancel ? renderCancelForm(item, busy) : ""}
      </div>
    </article>
  `;
}

function renderCancelForm(item, busy) {
  return `
    <form class="identity-privacy-cancel-form" data-privacy-cancel-form autocomplete="off">
      <input name="requestId" type="hidden" value="${escapeAttribute(item.data_subject_request_id)}" />
      <input name="revision" type="hidden" value="${number(item.revision)}" />
      <label><span>验证方式</span><select name="stepUpMethod" ${busy ? "disabled" : ""}>
        <option value="totp">动态验证码</option><option value="webauthn">安全密钥</option><option value="recovery_code">恢复码</option>
      </select></label>
      <label><span>验证码或恢复码</span><input name="stepUpCode" type="password" autocomplete="one-time-code" maxlength="20" ${busy ? "disabled" : ""} /></label>
      <label class="identity-confirm"><input name="confirmed" type="checkbox" required ${busy ? "disabled" : ""} /><span>确认取消</span></label>
      <button class="identity-danger-command" type="submit" ${busy ? "disabled" : ""}>取消请求</button>
    </form>
  `;
}

function renderConsent(item) {
  return `<div><span>${escapeHtml(purposeLabel(item.purpose))}</span><strong>${escapeHtml(decisionLabel(item.decision))}</strong><small>${formatTime(item.decided_at)} · ${escapeHtml(item.terms_version)}</small></div>`;
}

function renderOwnerProgress(item) {
  return `<div><span>${escapeHtml(ownerLabel(item.owner_domain))}</span><strong>${escapeHtml(statusLabel(item.status))}</strong><small>${escapeHtml(item.outcome_code ? outcomeLabel(item.outcome_code) : item.checkpoint_code)}</small></div>`;
}

function renderExceptions(items) {
  if (!items.length) return "";
  return `<div class="identity-privacy-exceptions"><strong>保留例外</strong>${items.map((item) => `<span>${escapeHtml(ownerLabel(item.owner_domain))} · ${escapeHtml(exceptionLabel(item.explanation_code))}${item.expires_at ? ` · ${formatTime(item.expires_at)}` : ""}</span>`).join("")}</div>`;
}

function renderDecisions(items) {
  if (!items.length) return "";
  return `<div class="identity-privacy-decisions"><strong>删除决策</strong>${items.map((item) => `<span>${escapeHtml(ownerLabel(item.owner_domain))} · ${escapeHtml(erasureLabel(item.outcome))}</span>`).join("")}</div>`;
}

function renderUnavailable(busy) {
  return busy ? '<p class="identity-empty">正在读取隐私偏好…</p>' : '<p class="identity-empty">隐私偏好暂时不可用。</p>';
}

function renderError(error) {
  if (!error) return "";
  return `<div class="identity-error" role="alert"><span>${escapeHtml(error.message ?? error)}</span>${error.requestId && error.requestId !== "-" ? `<small>request_id: ${escapeHtml(error.requestId)}</small>` : ""}</div>`;
}

function toggle(name, label, checked, busy) {
  return `<label><input name="${name}" type="checkbox" ${checked ? "checked" : ""} ${busy ? "disabled" : ""} /><span>${escapeHtml(label)}</span><b>${checked ? "已启用" : "已停用"}</b></label>`;
}

function radio(value, label, checked, busy) {
  return `<label><input name="requestKind" type="radio" value="${value}" ${checked ? "checked" : ""} ${busy ? "disabled" : ""} /><span>${escapeHtml(label)}</span></label>`;
}

function statusKind(value) {
  if (new Set(["ready", "completed"]).has(value)) return "ok";
  if (new Set(["failed", "partially_denied", "cancelled"]).has(value)) return "danger";
  return "pending";
}

function kindLabel(value) { return ({ export: "数据导出", rectify: "数据更正", erase: "数据删除" })[value] ?? value; }
function purposeLabel(value) { return ({ privacy_terms: "隐私条款", analytics: "产品分析", product_updates: "产品更新", model_improvement: "模型改进" })[value] ?? value; }
function decisionLabel(value) { return ({ granted: "已同意", denied: "已拒绝", withdrawn: "已撤回" })[value] ?? value; }
function ownerLabel(value) { return ({ identity: "账号", membership: "成员关系", audit: "审计", billing: "账务", support: "支持", edge: "Edge", data_governance: "数据治理" })[value] ?? value; }
function outcomeLabel(value) { return ({ exported: "已导出", rectified: "已更正", erased: "已删除", anonymized: "已匿名化", retained: "已保留", not_found: "无记录" })[value] ?? value; }
function erasureLabel(value) { return ({ erase: "删除", anonymize: "匿名化", retain: "保留", not_found: "无记录" })[value] ?? value; }
function exceptionLabel(value) { return ({ active_legal_hold: "法律保全生效", retention_period_active: "法定保留期生效" })[value] ?? value; }
function statusLabel(value) { return ({ received: "已接收", identity_verified: "身份已验证", collecting: "收集中", reviewing: "复核中", ready: "可下载", completed: "已完成", partially_denied: "部分拒绝", failed: "失败", cancelled: "已取消", pending: "等待中", running: "处理中" })[value] ?? String(value).replaceAll("_", " "); }
function number(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0"; }
function formatTime(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value); }
