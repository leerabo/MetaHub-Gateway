export function renderPartnerPage(container, state = {}) {
  if (!container) return;
  container.innerHTML = shell(state);
}

function shell(state) {
  const response = state.response;
  const partner = response?.partner;
  return `<div class="partner-shell" data-partner-phase="${escapeAttribute(state.phase)}">
    <header class="partner-topbar">
      <a class="partner-wordmark" href="/partner.html" aria-label="MetaHub Partner home">
        <span aria-hidden="true">M</span><strong>MetaHub Partner</strong>
      </a>
      <div class="partner-topbar-actions">
        ${partner ? `<span><strong>${escapeHtml(partner.display_name)}</strong><small>${escapeHtml(roleLabel(response.role_code))}</small></span>` : ""}
        <button type="button" data-partner-action="refresh" title="Refresh" aria-label="Refresh" ${state.phase === "loading" ? "disabled" : ""}>&#8635;</button>
        <a href="/identity.html">Account security</a>
      </div>
    </header>
    <div class="partner-layout">
      <aside class="partner-sidebar" aria-label="Partner navigation">
        <div><span>Workspace portfolio</span><strong>${number(response?.total ?? 0)}</strong></div>
        ${partner ? `<dl>
          <div><dt>Partner</dt><dd>${escapeHtml(partner.partner_code)}</dd></div>
          <div><dt>Tier</dt><dd>${escapeHtml(label(partner.tier_code))}</dd></div>
          <div><dt>Region</dt><dd>${escapeHtml(label(partner.region_scope))}</dd></div>
          <div><dt>Status</dt><dd>${status(partner.status)}</dd></div>
        </dl>` : ""}
      </aside>
      <section class="partner-content" aria-live="polite">
        ${feedback(state)}
        ${content(state)}
      </section>
    </div>
  </div>`;
}

function content(state) {
  if (state.phase === "loading" && !state.response) {
    return statePanel("Loading portfolio", "Fetching the current partner projection.");
  }
  if (state.phase === "session") {
    return statePanel("Partner session required", "Open account security and sign in again.", true);
  }
  if (state.phase === "revoked") {
    return statePanel("Partner access revoked", "No active ChannelMembership is available.", true);
  }
  if (state.phase === "unavailable") {
    return statePanel("Partner service unavailable", "The dependency did not return a verified projection.");
  }
  const response = state.response;
  if (!response) return statePanel("No verified data", "Refresh to retry.");
  const finance = response.role_code === "partner_finance";
  const active = response.items.filter((item) => item.operational_status === "active").length;
  const attention = response.items.length - active;
  return `
    <header class="partner-heading">
      <div><p>Partner portfolio</p><h1>Attributed workspaces</h1></div>
      <span>Page ${number(Math.floor(response.offset / response.limit) + 1)}</span>
    </header>
    <section class="partner-metrics" aria-label="Portfolio summary">
      ${metric("Attributed", response.total, "current membership")}
      ${metric("Active", active, "this page")}
      ${metric("Attention", attention, "this page")}
      ${metric("Role", roleLabel(response.role_code), finance ? "statement totals enabled" : "operational projection")}
    </section>
    <section class="partner-band">
      <div class="partner-section-heading">
        <div><p>Owner-qualified projection</p><h2>Workspace directory</h2></div>
        <span>${number(response.items.length)} rows</span>
      </div>
      ${response.items.length ? workspaceTable(response.items, finance) : '<div class="partner-empty"><strong>No attributed workspaces</strong></div>'}
      <div class="partner-pagination">
        <span>Updated from the current membership</span>
        <div>
          <button type="button" data-partner-action="previous" ${response.offset > 0 && state.phase !== "loading" ? "" : "disabled"}>Previous</button>
          <button type="button" data-partner-action="next" ${response.offset + response.limit < response.total && state.phase !== "loading" ? "" : "disabled"}>Next</button>
        </div>
      </div>
    </section>`;
}

function workspaceTable(items, finance) {
  return `<div class="partner-table-wrap"><table class="partner-table">
    <thead><tr><th>Workspace</th><th>Region</th><th>Attribution</th><th>Health</th>${finance ? "<th>Closed statements</th>" : ""}</tr></thead>
    <tbody>${items.map((item) => `<tr>
      <td data-label="Workspace"><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(shortId(item.workspace_id))}</small></td>
      <td data-label="Region">${escapeHtml(item.region_code)}</td>
      <td data-label="Attribution">${formatTime(item.attribution_starts_at)}</td>
      <td data-label="Health">${status(item.operational_status)}</td>
      ${finance ? `<td data-label="Closed statements">${statementTotals(item.statement_totals)}</td>` : ""}
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function statementTotals(totals) {
  if (!totals || !Object.keys(totals).length) return '<span class="partner-muted">None</span>';
  return Object.entries(totals).map(([currency, micros]) => (
    `<span class="partner-money">${escapeHtml(currency)} ${formatMicros(micros)}</span>`
  )).join("");
}

function feedback(state) {
  if (state.stale) {
    return `<div class="partner-feedback is-warn" role="alert"><strong>Stale projection</strong><span>Refresh failed; last verified data remains visible.</span><small>Request ${escapeHtml(state.error?.requestId ?? "-")}</small></div>`;
  }
  if (state.error && !new Set(["session", "revoked", "unavailable"]).has(state.phase)) {
    return `<div class="partner-feedback is-error" role="alert"><strong>${escapeHtml(state.error.message)}</strong><small>Request ${escapeHtml(state.error.requestId)}</small></div>`;
  }
  if (state.phase === "loading" && state.response) {
    return '<div class="partner-feedback" role="status">Refreshing current projection</div>';
  }
  return "";
}

function statePanel(title, detail, action = false) {
  return `<section class="partner-state"><span aria-hidden="true">${action ? "!" : "..."}</span><div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(detail)}</p>${action ? '<a href="/identity.html">Open account security</a>' : ""}</div></section>`;
}

function metric(name, value, detail) {
  return `<div><span>${escapeHtml(name)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></div>`;
}

function status(value) {
  const text = String(value ?? "unknown");
  const kind = text === "active" ? "is-ok"
    : new Set(["suspended", "deletion_pending", "migration_conflict"]).has(text) ? "is-warn"
      : "is-danger";
  return `<span class="partner-status ${kind}">${escapeHtml(label(text))}</span>`;
}

function roleLabel(value) {
  return ({ partner_owner: "Partner owner", partner_operations: "Operations", partner_finance: "Finance" })[value] ?? "Partner";
}

function label(value) {
  return String(value ?? "-").replaceAll("_", " ");
}

function shortId(value) {
  const text = String(value ?? "");
  return text.length > 18 ? `${text.slice(0, 12)}...${text.slice(-4)}` : text;
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isFinite(date.valueOf())
    ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date)
    : "-";
}

function formatMicros(value) {
  return new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(Number(value) / 1_000_000);
}

function number(value) {
  return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function escapeAttribute(value) {
  return escapeHtml(value);
}
