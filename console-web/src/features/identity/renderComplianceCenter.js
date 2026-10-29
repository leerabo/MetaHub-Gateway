const GAP_OUTCOMES = new Set([
  "partially_implemented", "not_implemented", "evidence_stale",
]);

export function renderComplianceCenter(state = {}) {
  if (!state.complianceVisible || !state.workspaceContext) return "";
  const response = state.complianceReports;
  const report = response?.current ?? null;
  const busy = state.complianceBusy === true;
  return `
    <section class="identity-compliance" aria-labelledby="identity-compliance-title" aria-busy="${busy}">
      <div class="identity-section-heading">
        <div><p>Compliance evidence</p><h2 id="identity-compliance-title">控制事实报告</h2></div>
        <button class="identity-icon-command" type="button" data-identity-action="refresh-compliance"
          title="刷新控制事实报告" aria-label="刷新控制事实报告" ${busy ? "disabled" : ""}>&#8635;</button>
      </div>
      <p class="identity-compliance-disclaimer">报告仅说明当前控制和证据事实，不代表外部认证结论。</p>
      ${renderError(state.complianceError)}
      ${report ? renderReport(report) : renderUnavailable(busy)}
      ${renderHistory(response?.recent ?? [], report)}
    </section>
  `;
}

function renderReport(report) {
  const items = Array.isArray(report.items) ? report.items : [];
  const gaps = items.filter((item) => GAP_OUTCOMES.has(item.report_outcome));
  const remediation = items.filter((item) => item.remediation_task_id !== null);
  return `
    <div class="identity-compliance-summary" data-stale="${report.stale}">
      ${summaryFact("已实施", report.implemented_count)}
      ${summaryFact("差距", report.gap_count)}
      ${summaryFact("过期证据", report.stale_count)}
      ${summaryFact("报告状态", report.stale ? "需刷新" : "当前")}
    </div>
    <dl class="identity-compliance-meta">
      <div><dt>控制集</dt><dd>${escapeHtml(report.control_set_version)}</dd></div>
      <div><dt>报告版本</dt><dd>${number(report.report_version)}</dd></div>
      <div><dt>证据水位</dt><dd>${number(report.evidence_high_watermark)}</dd></div>
      <div><dt>有效期</dt><dd>${formatTime(report.expires_at)}</dd></div>
    </dl>
    <div class="identity-compliance-items">
      <div class="identity-section-heading">
        <div><p>Control outcomes</p><h3>控制结论与恢复动作</h3></div>
        <span>${number(gaps.length)} 项差距 · ${number(remediation.length)} 项整改</span>
      </div>
      ${items.length ? items.map(renderReportItem).join("") : '<p class="identity-empty">当前报告没有控制项。</p>'}
    </div>
  `;
}

function renderReportItem(item) {
  const references = [
    item.compliance_exception_id
      ? `<span>例外 ${escapeHtml(shortReference(item.compliance_exception_id))}</span>` : "",
    item.remediation_task_id
      ? `<span>整改 ${escapeHtml(shortReference(item.remediation_task_id))}</span>` : "",
  ].filter(Boolean).join("");
  return `
    <article class="identity-compliance-item" data-outcome="${escapeAttribute(item.report_outcome)}">
      <header>
        <div><strong>${escapeHtml(item.title)}</strong><code>${escapeHtml(item.control_code)}</code></div>
        <span class="identity-compliance-outcome is-${outcomeKind(item.report_outcome)}">${escapeHtml(outcomeLabel(item.report_outcome))}</span>
      </header>
      <dl>
        <div><dt>原因</dt><dd>${escapeHtml(explanationLabel(item.explanation_code))}</dd></div>
        <div><dt>证据代次</dt><dd>${item.evidence_generation === null ? "无" : number(item.evidence_generation)}</dd></div>
        <div><dt>证据有效期</dt><dd>${item.evidence_valid_until ? formatTime(item.evidence_valid_until) : "无"}</dd></div>
      </dl>
      ${references ? `<div class="identity-compliance-references">${references}</div>` : ""}
    </article>
  `;
}

function renderHistory(recent, current) {
  const items = Array.isArray(recent)
    ? recent.filter((item) => item.compliance_report_id !== current?.compliance_report_id)
    : [];
  if (!items.length) return "";
  return `
    <details class="identity-compliance-history">
      <summary>历史报告 <span>${number(items.length)}</span></summary>
      ${items.map((item) => `<div><strong>版本 ${number(item.report_version)}</strong><span>${escapeHtml(item.control_set_version)}</span><time>${formatTime(item.generated_at)}</time></div>`).join("")}
    </details>
  `;
}

function renderUnavailable(busy) {
  return busy
    ? '<p class="identity-empty">正在读取控制事实报告…</p>'
    : '<p class="identity-empty">当前空间尚无控制事实报告。</p>';
}

function renderError(error) {
  if (!error) return "";
  return `<div class="identity-error" role="alert"><span>${escapeHtml(error.message ?? error)}</span>${error.requestId && error.requestId !== "-" ? `<small>request_id: ${escapeHtml(error.requestId)}</small>` : ""}</div>`;
}

function summaryFact(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${typeof value === "number" ? number(value) : escapeHtml(value)}</strong></div>`;
}

function outcomeKind(value) {
  if (value === "implemented" || value === "not_applicable") return "ok";
  if (value === "exception_active" || value === "partially_implemented") return "warn";
  return "danger";
}

function outcomeLabel(value) {
  return ({
    implemented: "已实施",
    partially_implemented: "部分实施",
    not_implemented: "未实施",
    not_applicable: "不适用",
    exception_active: "有效例外",
    evidence_stale: "证据过期",
  })[value] ?? value;
}

function explanationLabel(value) {
  return String(value ?? "-").replaceAll("_", " ");
}

function shortReference(value) {
  const text = String(value ?? "");
  return text.length > 20 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text || "-";
}

function number(value) {
  return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0";
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function escapeAttribute(value) { return escapeHtml(value); }
