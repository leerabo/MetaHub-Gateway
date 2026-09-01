/** Dense Platform Channels projection for PRD-139/142. */

import { renderBrandEditor } from "../brand-domains/renderBrandEditor.js";

export function renderChannelManagement(state = {}, permissions = new Set()) {
  const partners = state.channels?.items ?? [];
  const statements = state.channelStatements?.items ?? [];
  const pending = partners.flatMap((partner) => partner.pending_attributions.map(
    (item) => ({ ...item, partner }),
  ));
  const canRead = permissions.has("platform.channel.read");
  const canManage = permissions.has("platform.channel.manage");
  const canLead = permissions.has("platform.channel.lead.register");
  const canRequest = permissions.has("platform.channel.attribution.request");
  const canApprove = permissions.has("platform.channel.attribution.approve");
  const canStatements = permissions.has("platform.channel.statement.read");
  return `<div data-platform-channels-root>
    <header class="platform-page-heading">
      <div><p>Partner operations</p><h1>Channels</h1></div>
      <button class="platform-icon-button" type="button" data-platform-action="channels-refresh"
        title="Refresh channels" aria-label="Refresh channels" ${state.loading ? "disabled" : ""}>&#8635;</button>
    </header>
    ${state.channelIssuedReferral ? issuedReferral(state.channelIssuedReferral) : ""}
    ${state.channelLatestLead || state.channelLatestAttribution ? recentActivity(state) : ""}
    <section class="platform-metrics" aria-label="Channel summary">
      ${metric("Partners", state.channels?.total ?? 0, canRead ? "authorized directory" : "restricted")}
      ${metric("Members", partners.reduce((sum, item) => sum + item.memberships.filter((member) => member.status === "active").length, 0), "active on page")}
      ${metric("Pending", pending.length, "attribution approvals")}
      ${metric("Statements", state.channelStatements?.total ?? 0, canStatements ? "reconciled" : "restricted")}
    </section>
    ${canRead ? directory(partners, state, canManage, permissions) : ""}
    ${canManage ? createPartnerForm(state.loading) : ""}
    ${canLead || canRequest ? intakeForms(partners, state.loading, canLead, canRequest) : ""}
    ${canApprove ? pendingApprovals(pending, state.loading) : ""}
    ${canStatements ? statementSection(statements, state) : ""}
  </div>`;
}

function directory(partners, state, canManage, permissions) {
  const filters = state.channelFilters ?? {};
  return `<section class="platform-band platform-channel-directory">
    <div class="platform-section-heading"><div><p>Owner-qualified catalog</p><h2>Partner directory</h2></div><span>${number(partners.length)} rows</span></div>
    <form class="platform-filter-form" data-platform-channel-filter-form autocomplete="off">
      ${select("Status", "status", filters.status, [["", "All"], ["active", "Active"], ["suspended", "Suspended"], ["retired", "Retired"]])}
      ${select("Tier", "tierCode", filters.tierCode, [["", "All"], ["registered", "Registered"], ["silver", "Silver"], ["gold", "Gold"], ["strategic", "Strategic"]])}
      ${select("Region", "regionScope", filters.regionScope, [["", "All"], ["global", "Global"], ["cn", "CN"], ["apac", "APAC"], ["emea", "EMEA"], ["americas", "Americas"]])}
      <button class="platform-primary" type="submit" ${state.loading ? "disabled" : ""}>Filter</button>
    </form>
    <div class="platform-channel-list">${partners.map((partner) => partnerCard(partner, state.loading, canManage, permissions, state.brandDomains?.channel)).join("") || '<div class="platform-empty">No partners match the current filters.</div>'}</div>
    ${renderSelectedChannelBrand(partners, state.brandDomains?.channel)}
    ${pagination("channel", state.channels, state.loading)}
  </section>`;
}

function partnerCard(partner, loading, canManage, permissions, channelBrand) {
  const members = partner.memberships.filter((item) => item.status === "active");
  return `<article class="platform-channel-partner">
    <header><div><strong>${escapeHtml(partner.display_name)}</strong><small>${escapeHtml(partner.partner_code)} · ${shortId(partner.channel_partner_id)}</small></div><div>${status(partner.status)}<span>Rev ${number(partner.revision)}</span></div></header>
    <dl class="platform-channel-facts">
      <div><dt>Tier</dt><dd>${escapeHtml(label(partner.tier_code))}</dd></div>
      <div><dt>Region</dt><dd>${escapeHtml(label(partner.region_scope))}</dd></div>
      <div><dt>Contract</dt><dd><code>${shortHash(partner.contract_sha256)}</code></dd></div>
      <div><dt>Referral</dt><dd>${partner.referral ? `${status(partner.referral.status)} · ...${escapeHtml(partner.referral.masked_suffix)}` : "Not issued"}</dd></div>
    </dl>
    <div class="platform-channel-subgrid">
      <section><h3>Commission rules</h3>${partner.commission_rules.map((rule) => `<div class="platform-channel-rule"><strong>${escapeHtml(rule.currency)} · ${(rule.commission_bps / 100).toFixed(2)}%</strong><span>v${number(rule.version)} · ${shortId(rule.channel_commission_rule_snapshot_id)}</span></div>`).join("") || '<span class="platform-restricted">None</span>'}</section>
      <section><h3>Memberships</h3>${members.map((member) => `<div class="platform-channel-member"><span>${escapeHtml(label(member.role_code))}<small>${shortId(member.account_id)}</small></span>${canManage ? revokeForm(partner, member, loading) : ""}</div>`).join("") || '<span class="platform-restricted">None</span>'}</section>
    </div>
    <div class="platform-channel-card-actions">
      ${permissions.has("platform.channel_brand.read") ? `<button type="button" data-platform-action="channel-brand-select" data-partner-id="${escapeAttribute(partner.channel_partner_id)}"${loading ? " disabled" : ""}>${channelBrand?.partnerId === partner.channel_partner_id ? "刷新外观" : "管理外观"}</button>` : ""}
    </div>
    ${canManage ? `<details><summary>Partner controls</summary>${partnerControls(partner, loading)}</details>` : ""}
  </article>`;
}

function renderSelectedChannelBrand(partners, channelBrand) {
  if (!channelBrand?.partnerId) return "";
  const partner = partners.find((item) => item.channel_partner_id === channelBrand.partnerId);
  if (!partner) return "";
  return `<section class="platform-channel-brand-detail"><div class="platform-section-heading"><div><p>Scoped ChannelBrand</p><h2>${escapeHtml(partner.display_name)} 外观</h2></div><span>${escapeHtml(partner.partner_code)}</span></div>${renderBrandEditor(channelBrand, { scope: "channel-brand", kicker: "Partner host identity", title: "渠道品牌", allowDomain: true })}</section>`;
}

function partnerControls(partner, loading) {
  const disabled = loading ? " disabled" : "";
  return `<div class="platform-channel-controls">
    <form data-platform-channel-partner-update-form data-platform-channel-write-form autocomplete="off">
      <h3>Catalog</h3>${partnerHidden(partner)}
      <label><span>Display name</span><input name="displayName" maxlength="120" value="${escapeAttribute(partner.display_name)}" required${disabled} /></label>
      ${select("Tier", "tierCode", partner.tier_code, [["registered", "Registered"], ["silver", "Silver"], ["gold", "Gold"], ["strategic", "Strategic"]], disabled)}
      ${select("Region", "regionScope", partner.region_scope, [["global", "Global"], ["cn", "CN"], ["apac", "APAC"], ["emea", "EMEA"], ["americas", "Americas"]], disabled)}
      ${select("Status", "status", partner.status, [["active", "Active"], ["suspended", "Suspended"], ["retired", "Retired"]], disabled)}
      ${stepUp(disabled)}${confirm("Update partner", disabled)}
    </form>
    <form data-platform-channel-membership-form data-platform-channel-write-form autocomplete="off">
      <h3>Membership</h3>${partnerHidden(partner)}
      <label><span>Global account ID</span><input name="accountId" pattern="gac_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>
      ${select("Role", "roleCode", "partner_operations", [["partner_operations", "Operations"], ["partner_finance", "Finance"], ["partner_owner", "Owner"]], disabled)}
      ${stepUp(disabled)}${confirm("Upsert member", disabled)}
    </form>
    <form data-platform-channel-referral-form data-platform-channel-write-form autocomplete="off">
      <h3>Referral code</h3>${partnerHidden(partner)}
      <label><span>New code</span><input type="password" name="referralCode" minlength="20" maxlength="128" pattern="[A-Za-z0-9_-]+" autocomplete="new-password" data-platform-sensitive required${disabled} /></label>
      <label><span>Usage limit</span><input type="number" name="usageLimit" min="1" max="1000000" value="100" required${disabled} /></label>
      <label><span>Expires at</span><input type="datetime-local" name="expiresAt"${disabled} /></label>
      ${stepUp(disabled)}${confirm("Rotate code", disabled)}
    </form>
  </div>`;
}

function revokeForm(partner, member, loading) {
  const disabled = loading ? " disabled" : "";
  return `<form class="platform-channel-revoke" data-platform-channel-membership-revoke-form data-platform-channel-write-form autocomplete="off">
    ${partnerHidden(partner)}<input type="hidden" name="membershipId" value="${escapeAttribute(member.channel_membership_id)}" />
    ${stepUp(disabled, true)}<label><input type="checkbox" name="confirmed" required${disabled} /><span>Confirm</span></label>
    <button type="submit" title="Revoke membership" aria-label="Revoke membership"${disabled}>&times;</button>
  </form>`;
}

function createPartnerForm(loading) {
  const disabled = loading ? " disabled" : "";
  return `<section class="platform-band platform-channel-create">
    <div class="platform-section-heading"><div><p>Controlled creation</p><h2>New partner</h2></div><span>If-Match &quot;0&quot;</span></div>
    <form data-platform-channel-partner-create-form data-platform-channel-write-form autocomplete="off">
      <label><span>Owner Workspace ID</span><input name="ownerWorkspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>
      <label><span>Owner account ID</span><input name="ownerAccountId" pattern="gac_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>
      <label><span>Partner code</span><input name="partnerCode" pattern="[a-z0-9][a-z0-9._-]{0,79}" maxlength="80" required${disabled} /></label>
      <label><span>Display name</span><input name="displayName" maxlength="120" required${disabled} /></label>
      ${select("Tier", "tierCode", "registered", [["registered", "Registered"], ["silver", "Silver"], ["gold", "Gold"], ["strategic", "Strategic"]], disabled)}
      ${select("Region", "regionScope", "global", [["global", "Global"], ["cn", "CN"], ["apac", "APAC"], ["emea", "EMEA"], ["americas", "Americas"]], disabled)}
      <label class="platform-channel-hash"><span>Contract SHA-256</span><input name="contractSha256" pattern="[0-9a-f]{64}" minlength="64" maxlength="64" required${disabled} /></label>
      ${stepUp(disabled)}${confirm("Create partner", disabled)}
    </form>
  </section>`;
}

function intakeForms(partners, loading, canLead, canRequest) {
  const active = partners.filter((item) => item.status === "active");
  if (!active.length) return "";
  const disabled = loading ? " disabled" : "";
  const partnerOptions = active.map((partner) => `<option value="${escapeAttribute(partner.channel_partner_id)}" data-channel-partner-revision="${escapeAttribute(partner.revision)}">${escapeHtml(partner.display_name)} · Rev ${number(partner.revision)}</option>`).join("");
  return `<section class="platform-channel-intake">
    ${canLead ? `<div class="platform-band"><div class="platform-section-heading"><div><p>Encrypted contact</p><h2>Register lead</h2></div></div><form data-platform-channel-lead-form data-platform-channel-write-form autocomplete="off">
      <label><span>Partner</span><select name="partnerId" data-channel-partner-select${disabled}>${partnerOptions}</select></label><label><span>Partner revision</span><input name="expectedRevision" data-channel-partner-revision-target type="number" min="1" value="${escapeAttribute(active[0].revision)}" required readonly${disabled} /></label>
      <label><span>Lead code</span><input name="leadCode" maxlength="80" pattern="[a-z0-9][a-z0-9._-]{0,79}" required${disabled} /></label><label><span>Region</span><input name="regionCode" maxlength="16" pattern="[a-z0-9][a-z0-9-]{1,15}" required${disabled} /></label>
      <label><span>Email</span><input name="contactEmail" type="email" maxlength="320" data-platform-sensitive${disabled} /></label><label><span>Phone</span><input name="contactPhone" maxlength="20" data-platform-sensitive${disabled} /></label>
      <label><span>Source SHA-256</span><input name="sourceSha256" pattern="[0-9a-f]{64}" minlength="64" maxlength="64" required${disabled} /></label><label><span>Candidate Workspace ID</span><input name="candidateWorkspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36"${disabled} /></label>
      ${stepUp(disabled)}${confirm("Register lead", disabled)}</form></div>` : ""}
    ${canRequest ? `<div class="platform-band"><div class="platform-section-heading"><div><p>Quoted attribution</p><h2>Request attribution</h2></div></div><form data-platform-channel-attribution-form data-platform-channel-write-form autocomplete="off">
      <label><span>Partner</span><select name="partnerId" data-channel-partner-select${disabled}>${partnerOptions}</select></label><label><span>Target Workspace ID</span><input name="targetWorkspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>
      <label><span>Lead ID</span><input name="leadId" pattern="cld_[0-9a-f]{32}" maxlength="36"${disabled} /></label><label><span>Rule snapshot ID</span><select name="ruleId" data-channel-rule-select${disabled}>${active.flatMap((partner, partnerIndex) => partner.commission_rules.map((rule) => `<option value="${escapeAttribute(rule.channel_commission_rule_snapshot_id)}" data-channel-partner-id="${escapeAttribute(partner.channel_partner_id)}"${partnerIndex === 0 ? "" : " disabled"}>${escapeHtml(partner.display_name)} · ${escapeHtml(rule.currency)} v${number(rule.version)}</option>`)).join("")}</select></label>
      <label><span>Active attribution revision</span><input name="expectedRevision" type="number" min="0" value="0" required${disabled} /></label><label><span>Starts at</span><input name="startsAt" type="datetime-local" required${disabled} /></label>
      <label><span>Reason</span><input name="reasonCode" value="channel.assignment" pattern="[a-z][a-z0-9_.-]{2,79}" required${disabled} /></label>${stepUp(disabled)}${confirm("Request attribution", disabled)}</form></div>` : ""}
  </section>`;
}

function pendingApprovals(items, loading) {
  if (!items.length) return "";
  const disabled = loading ? " disabled" : "";
  return `<section class="platform-band platform-channel-approvals"><div class="platform-section-heading"><div><p>Independent owners</p><h2>Pending attribution</h2></div><span>${number(items.length)} open</span></div>
    <div class="platform-table-wrap"><table><thead><tr><th>Partner / Workspace</th><th>Generation</th><th>Approvals</th><th>Starts</th><th>Decision</th></tr></thead><tbody>${items.map((item) => `<tr>
      <td data-label="Partner / Workspace"><strong>${escapeHtml(item.partner.display_name)}</strong><small>${shortId(item.target_workspace_id)}</small></td><td data-label="Generation">${number(item.approval_generation)}<small>Rev ${number(item.revision)}</small></td><td data-label="Approvals">${number(item.approval_count)} / ${number(item.required_approvals)}</td><td data-label="Starts">${formatTime(item.starts_at)}</td>
      <td data-label="Decision"><form data-platform-channel-approval-form data-platform-channel-write-form autocomplete="off"><input type="hidden" name="attributionId" value="${escapeAttribute(item.workspace_attribution_id)}" /><input type="hidden" name="expectedRevision" value="${number(item.revision)}" />${select("Decision", "decision", "approved", [["approved", "Approve"], ["rejected", "Reject"]], disabled)}<label><span>Reason</span><input name="reasonCode" value="owner.approved" pattern="[a-z][a-z0-9_.-]{2,79}" required${disabled} /></label>${stepUp(disabled, true)}${confirm("Submit", disabled)}</form></td>
    </tr>`).join("")}</tbody></table></div></section>`;
}

function statementSection(items, state) {
  const filters = state.channelStatementFilters ?? {};
  return `<section class="platform-band platform-channel-statements"><div class="platform-section-heading"><div><p>Reconciliation projection</p><h2>Channel statements</h2></div><span>${number(state.channelStatements?.total ?? 0)} statements</span></div>
    <form class="platform-filter-form" data-platform-channel-statement-filter-form autocomplete="off"><label><span>Partner ID</span><input name="partnerId" value="${escapeAttribute(filters.partnerId ?? "")}" pattern="par_[0-9a-f]{32}" maxlength="36" /></label>${select("Currency", "currency", filters.currency, [["", "All"], ["CNY", "CNY"], ["USD", "USD"]])}<button class="platform-primary" type="submit" ${state.loading ? "disabled" : ""}>Filter</button></form>
    <div class="platform-table-wrap"><table><thead><tr><th>Partner / Period</th><th>Currency</th><th>Source total</th><th>Commission</th><th>Lines</th><th>Reconciliation</th><th>Status</th></tr></thead><tbody>${items.map((item) => `<tr><td data-label="Partner / Period"><strong>${shortId(item.channel_partner_id)}</strong><small>${formatDate(item.period_start)} - ${formatDate(item.period_end)}</small></td><td data-label="Currency">${escapeHtml(item.currency)}</td><td data-label="Source total">${formatMicros(item.total_amount_micros)}</td><td data-label="Commission">${formatMicros(item.total_commission_micros)}</td><td data-label="Lines">${number(item.line_count)}</td><td data-label="Reconciliation"><code>${shortHash(item.reconciliation_sha256)}</code></td><td data-label="Status">${status(item.status)}<small>Rev ${number(item.close_revision)}</small></td></tr>`).join("") || '<tr><td colspan="7" class="platform-empty">No statements match the current filters.</td></tr>'}</tbody></table></div>${pagination("channel-statement", state.channelStatements, state.loading)}</section>`;
}

function issuedReferral(value) { return `<section class="platform-channel-issued" role="status"><div><strong>Referral code issued</strong><code>${escapeHtml(value)}</code></div><button type="button" data-platform-action="channel-referral-dismiss" title="Dismiss" aria-label="Dismiss">&times;</button></section>`; }
function recentActivity(state) { return `<section class="platform-channel-recent" role="status">${state.channelLatestLead ? `<span>Lead <strong>${shortId(state.channelLatestLead.channel_lead_id)}</strong> registered</span>` : ""}${state.channelLatestAttribution ? `<span>Attribution <strong>${shortId(state.channelLatestAttribution.workspace_attribution_id)}</strong> · ${escapeHtml(label(state.channelLatestAttribution.status))}</span>` : ""}</section>`; }
function partnerHidden(partner) { return `<input type="hidden" name="partnerId" value="${escapeAttribute(partner.channel_partner_id)}" /><input type="hidden" name="expectedRevision" value="${number(partner.revision)}" />`; }
function stepUp(disabled, compact = false) { return `<label class="${compact ? "platform-channel-compact" : ""}"><span>Step-up</span><select name="method" data-channel-step-up-method${disabled}><option value="totp">TOTP</option><option value="webauthn">Security key</option></select></label><label data-platform-step-up-code class="${compact ? "platform-channel-compact" : ""}"><span>TOTP code</span><input type="password" name="verificationCode" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" autocomplete="one-time-code" data-platform-sensitive required${disabled} /></label>`; }
function confirm(text, disabled) { return `<label class="platform-check"><input type="checkbox" name="confirmed" required${disabled} /><span>Confirm command</span></label><button class="platform-primary" type="submit"${disabled}>${escapeHtml(text)}</button>`; }
function select(labelText, name, current, values, disabled = "") { return `<label><span>${escapeHtml(labelText)}</span><select name="${escapeAttribute(name)}"${disabled}>${values.map(([value, text]) => `<option value="${escapeAttribute(value)}" ${value === (current ?? "") ? "selected" : ""}>${escapeHtml(text)}</option>`).join("")}</select></label>`; }
function pagination(prefix, response, loading) { return response ? `<div class="platform-pagination"><span>Offset ${number(response.offset)} · Limit ${number(response.limit)}</span><div><button type="button" data-platform-action="${prefix}-previous" ${response.offset > 0 && !loading ? "" : "disabled"}>Previous</button><button type="button" data-platform-action="${prefix}-next" ${response.offset + response.limit < response.total && !loading ? "" : "disabled"}>Next</button></div></div>` : ""; }
function metric(name, value, detail) { return `<div><span>${escapeHtml(name)}</span><strong>${number(value)}</strong><small>${escapeHtml(detail)}</small></div>`; }
function status(value) { const text = String(value ?? "unknown"); const kind = new Set(["active", "closed"]).has(text) ? "is-ok" : new Set(["pending_approval", "open", "imported", "suspended"]).has(text) ? "is-warn" : "is-danger"; return `<span class="platform-status ${kind}">${escapeHtml(label(text))}</span>`; }
function label(value) { return String(value ?? "-").replaceAll("_", " "); }
function formatMicros(value) { return new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(Number(value) / 1_000_000); }
function formatDate(value) { const date = new Date(value); return Number.isFinite(date.valueOf()) ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date) : "-"; }
function formatTime(value) { const date = new Date(value); return Number.isFinite(date.valueOf()) ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date) : "-"; }
function shortId(value) { const text = String(value ?? ""); return text.length > 18 ? `${text.slice(0, 12)}...${text.slice(-4)}` : text; }
function shortHash(value) { const text = String(value ?? ""); return text.length === 64 ? `${text.slice(0, 12)}...${text.slice(-8)}` : "-"; }
function number(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]); }
function escapeAttribute(value) { return escapeHtml(value); }
