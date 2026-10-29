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
      <div><p>合作渠道管理</p><h1>渠道合作方</h1></div>
      <button class="platform-icon-button" type="button" data-platform-action="channels-refresh"
        title="刷新渠道" aria-label="刷新渠道" ${state.loading ? "disabled" : ""}>&#8635;</button>
    </header>
    ${state.channelIssuedReferral ? issuedReferral(state.channelIssuedReferral) : ""}
    ${state.channelLatestLead || state.channelLatestAttribution ? recentActivity(state) : ""}
    <section class="platform-metrics" aria-label="渠道摘要">
      ${metric("合作方", state.channels?.total ?? 0, canRead ? "目录中的合作方" : "暂无权限")}
      ${metric("有效成员", partners.reduce((sum, item) => sum + item.memberships.filter((member) => member.status === "active").length, 0), "当前页")}
      ${metric("待处理", pending.length, "待审批归因")}
      ${metric("结算单", state.channelStatements?.total ?? 0, canStatements ? "已同步记录" : "暂无权限")}
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
    <div class="platform-section-heading"><div><p>已授权目录</p><h2>合作方目录</h2></div><span>${number(partners.length)} 条</span></div>
    <form class="platform-filter-form" data-platform-channel-filter-form autocomplete="off">
      ${select("状态", "status", filters.status, [["", "全部"], ["active", "正常"], ["suspended", "已暂停"], ["retired", "已退出"]])}
      ${select("级别", "tierCode", filters.tierCode, [["", "全部"], ["registered", "已登记"], ["silver", "银牌"], ["gold", "金牌"], ["strategic", "战略合作"]])}
      ${select("区域", "regionScope", filters.regionScope, [["", "全部"], ["global", "全球"], ["cn", "中国大陆"], ["apac", "亚太"], ["emea", "欧洲中东非洲"], ["americas", "美洲"]])}
      <button class="platform-primary" type="submit" ${state.loading ? "disabled" : ""}>筛选</button>
    </form>
    <div class="platform-channel-list">${partners.map((partner) => partnerCard(partner, state.loading, canManage, permissions, state.brandDomains?.channel)).join("") || '<div class="platform-empty">当前筛选下没有合作方。</div>'}</div>
    ${renderSelectedChannelBrand(partners, state.brandDomains?.channel)}
    ${pagination("channel", state.channels, state.loading)}
  </section>`;
}

function partnerCard(partner, loading, canManage, permissions, channelBrand) {
  const members = partner.memberships.filter((item) => item.status === "active");
  return `<article class="platform-channel-partner">
    <header><div><strong>${escapeHtml(partner.display_name)}</strong><small>${escapeHtml(partner.partner_code)} · ${shortId(partner.channel_partner_id)}</small></div><div>${status(partner.status)}<span>Rev ${number(partner.revision)}</span></div></header>
    <dl class="platform-channel-facts">
      <div><dt>级别</dt><dd>${escapeHtml(label(partner.tier_code))}</dd></div>
      <div><dt>区域</dt><dd>${escapeHtml(label(partner.region_scope))}</dd></div>
      <div><dt>协议</dt><dd><details class="platform-audit-technical"><summary>技术信息</summary><code>${shortHash(partner.contract_sha256)}</code></details></dd></div>
      <div><dt>推荐码</dt><dd>${partner.referral ? `${status(partner.referral.status)} · ...${escapeHtml(partner.referral.masked_suffix)}` : "未创建"}</dd></div>
    </dl>
    <div class="platform-channel-subgrid">
      <section><h3>佣金规则</h3>${partner.commission_rules.map((rule) => `<div class="platform-channel-rule"><strong>${escapeHtml(rule.currency)} · ${(rule.commission_bps / 100).toFixed(2)}%</strong><span>第 ${number(rule.version)} 版</span></div>`).join("") || '<span class="platform-restricted">暂无规则</span>'}</section>
      <section><h3>已加入成员</h3>${members.map((member) => `<div class="platform-channel-member"><span>${escapeHtml(label(member.role_code))}<small>成员已关联</small></span>${canManage ? revokeForm(partner, member, loading) : ""}</div>`).join("") || '<span class="platform-restricted">暂无成员</span>'}</section>
    </div>
    <div class="platform-channel-card-actions">
      ${permissions.has("platform.channel_brand.read") ? `<button type="button" data-platform-action="channel-brand-select" data-partner-id="${escapeAttribute(partner.channel_partner_id)}"${loading ? " disabled" : ""}>${channelBrand?.partnerId === partner.channel_partner_id ? "刷新外观" : "管理外观"}</button>` : ""}
    </div>
    ${canManage ? `<details><summary>管理合作方</summary>${partnerControls(partner, loading)}</details>` : ""}
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
      <h3>基本信息</h3>${partnerHidden(partner)}
      <label><span>显示名称</span><input name="displayName" maxlength="120" value="${escapeAttribute(partner.display_name)}" required${disabled} /></label>
      ${select("级别", "tierCode", partner.tier_code, [["registered", "已登记"], ["silver", "银牌"], ["gold", "金牌"], ["strategic", "战略合作"]], disabled)}
      ${select("区域", "regionScope", partner.region_scope, [["global", "全球"], ["cn", "中国大陆"], ["apac", "亚太"], ["emea", "欧洲中东非洲"], ["americas", "美洲"]], disabled)}
      ${select("状态", "status", partner.status, [["active", "正常"], ["suspended", "已暂停"], ["retired", "已退出"]], disabled)}
      ${stepUp(disabled)}${confirm("保存信息", disabled)}
    </form>
    <form data-platform-channel-membership-form data-platform-channel-write-form autocomplete="off">
      <h3>成员关联</h3>${partnerHidden(partner)}
      <label><span>成员账号 ID</span><input name="accountId" pattern="gac_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>
      ${select("角色", "roleCode", "partner_operations", [["partner_operations", "运营"], ["partner_finance", "财务"], ["partner_owner", "负责人"]], disabled)}
      ${stepUp(disabled)}${confirm("保存成员", disabled)}
    </form>
    <form data-platform-channel-referral-form data-platform-channel-write-form autocomplete="off">
      <h3>推荐码</h3>${partnerHidden(partner)}
      <label><span>新推荐码</span><input type="password" name="referralCode" minlength="20" maxlength="128" pattern="[A-Za-z0-9_-]+" autocomplete="new-password" data-platform-sensitive required${disabled} /></label>
      <label><span>使用次数上限</span><input type="number" name="usageLimit" min="1" max="1000000" value="100" required${disabled} /></label>
      <label><span>有效期</span><input type="datetime-local" name="expiresAt"${disabled} /></label>
      ${stepUp(disabled)}${confirm("更新推荐码", disabled)}
    </form>
  </div>`;
}

function revokeForm(partner, member, loading) {
  const disabled = loading ? " disabled" : "";
  return `<form class="platform-channel-revoke" data-platform-channel-membership-revoke-form data-platform-channel-write-form autocomplete="off">
    ${partnerHidden(partner)}<input type="hidden" name="membershipId" value="${escapeAttribute(member.channel_membership_id)}" />
    ${stepUp(disabled, true)}<label><input type="checkbox" name="confirmed" required${disabled} /><span>确认移除</span></label>
    <button type="submit" title="移除成员" aria-label="移除成员"${disabled}>&times;</button>
  </form>`;
}

function createPartnerForm(loading) {
  const disabled = loading ? " disabled" : "";
  return `<details class="platform-band platform-collapsed-section platform-channel-create"><summary><span>＋ 新增合作方</span><small>只有接入新渠道时才需要展开</small></summary><div class="platform-section-heading"><div><p>受控创建</p><h2>新增合作方</h2></div><span>新建</span></div>
    <form data-platform-channel-partner-create-form data-platform-channel-write-form autocomplete="off">
      <label><span>所属组织 ID</span><input name="ownerWorkspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>
      <label><span>负责人账号 ID</span><input name="ownerAccountId" pattern="gac_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>
      <label><span>合作方编码</span><input name="partnerCode" pattern="[a-z0-9][a-z0-9._-]{0,79}" maxlength="80" required${disabled} /></label>
      <label><span>显示名称</span><input name="displayName" maxlength="120" required${disabled} /></label>
      ${select("级别", "tierCode", "registered", [["registered", "已登记"], ["silver", "银牌"], ["gold", "金牌"], ["strategic", "战略合作"]], disabled)}
      ${select("区域", "regionScope", "global", [["global", "全球"], ["cn", "中国大陆"], ["apac", "亚太"], ["emea", "欧洲中东非洲"], ["americas", "美洲"]], disabled)}
      <label class="platform-channel-hash"><span>协议 SHA-256</span><input name="contractSha256" pattern="[0-9a-f]{64}" minlength="64" maxlength="64" required${disabled} /></label>
      ${stepUp(disabled)}${confirm("创建合作方", disabled)}
    </form></details>`;
}

function intakeForms(partners, loading, canLead, canRequest) {
  const active = partners.filter((item) => item.status === "active");
  if (!active.length) return "";
  const disabled = loading ? " disabled" : "";
  const partnerOptions = active.map((partner) => `<option value="${escapeAttribute(partner.channel_partner_id)}" data-channel-partner-revision="${escapeAttribute(partner.revision)}">${escapeHtml(partner.display_name)} · Rev ${number(partner.revision)}</option>`).join("");
  return `<details class="platform-band platform-collapsed-section platform-channel-intake"><summary><span>＋ 新增线索或归因</span><small>渠道合作开始后才需要使用</small></summary><section class="platform-channel-intake">
    ${canLead ? `<div class="platform-band"><div class="platform-section-heading"><div><p>加密联系人</p><h2>登记线索</h2></div></div><form data-platform-channel-lead-form data-platform-channel-write-form autocomplete="off">
      <label><span>合作方</span><select name="partnerId" data-channel-partner-select${disabled}>${partnerOptions}</select></label><label><span>合作方版本</span><input name="expectedRevision" data-channel-partner-revision-target type="number" min="1" value="${escapeAttribute(active[0].revision)}" required readonly${disabled} /></label>
      <label><span>线索编码</span><input name="leadCode" maxlength="80" pattern="[a-z0-9][a-z0-9._-]{0,79}" required${disabled} /></label><label><span>区域</span><input name="regionCode" maxlength="16" pattern="[a-z0-9][a-z0-9-]{1,15}" required${disabled} /></label>
      <label><span>邮箱</span><input name="contactEmail" type="email" maxlength="320" data-platform-sensitive${disabled} /></label><label><span>电话</span><input name="contactPhone" maxlength="20" data-platform-sensitive${disabled} /></label>
      <label><span>来源 SHA-256</span><input name="sourceSha256" pattern="[0-9a-f]{64}" minlength="64" maxlength="64" required${disabled} /></label><label><span>候选组织 ID</span><input name="candidateWorkspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36"${disabled} /></label>
      ${stepUp(disabled)}${confirm("登记线索", disabled)}</form></div>` : ""}
    ${canRequest ? `<div class="platform-band"><div class="platform-section-heading"><div><p>报价归因</p><h2>申请归因</h2></div></div><form data-platform-channel-attribution-form data-platform-channel-write-form autocomplete="off">
      <label><span>合作方</span><select name="partnerId" data-channel-partner-select${disabled}>${partnerOptions}</select></label><label><span>目标组织 ID</span><input name="targetWorkspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>
      <label><span>线索 ID</span><input name="leadId" pattern="cld_[0-9a-f]{32}" maxlength="36"${disabled} /></label><label><span>规则版本</span><select name="ruleId" data-channel-rule-select${disabled}>${active.flatMap((partner, partnerIndex) => partner.commission_rules.map((rule) => `<option value="${escapeAttribute(rule.channel_commission_rule_snapshot_id)}" data-channel-partner-id="${escapeAttribute(partner.channel_partner_id)}"${partnerIndex === 0 ? "" : " disabled"}>${escapeHtml(partner.display_name)} · ${escapeHtml(rule.currency)} 第 ${number(rule.version)} 版</option>`)).join("")}</select></label>
      <label><span>归因版本</span><input name="expectedRevision" type="number" min="0" value="0" required${disabled} /></label><label><span>生效时间</span><input name="startsAt" type="datetime-local" required${disabled} /></label>
      <label><span>原因</span><input name="reasonCode" value="channel.assignment" pattern="[a-z][a-z0-9_.-]{2,79}" required${disabled} /></label>${stepUp(disabled)}${confirm("申请归因", disabled)}</form></div>` : ""}
  </section></details>`;
}

function pendingApprovals(items, loading) {
  if (!items.length) return "";
  const disabled = loading ? " disabled" : "";
  return `<section class="platform-band platform-channel-approvals"><div class="platform-section-heading"><div><p>独立负责人确认</p><h2>待处理归因</h2></div><span>${number(items.length)} 条待处理</span></div>
    <div class="platform-table-wrap"><table><thead><tr><th>合作方 / 组织</th><th>版本</th><th>审批数</th><th>生效时间</th><th>决定</th></tr></thead><tbody>${items.map((item) => `<tr>
      <td data-label="合作方 / 组织"><strong>${escapeHtml(item.partner.display_name)}</strong><small>${shortId(item.target_workspace_id)}</small></td><td data-label="版本">${number(item.approval_generation)}<small>修订 ${number(item.revision)}</small></td><td data-label="审批数">${number(item.approval_count)} / ${number(item.required_approvals)}</td><td data-label="生效时间">${formatTime(item.starts_at)}</td>
      <td data-label="决定"><form data-platform-channel-approval-form data-platform-channel-write-form autocomplete="off"><input type="hidden" name="attributionId" value="${escapeAttribute(item.workspace_attribution_id)}" /><input type="hidden" name="expectedRevision" value="${number(item.revision)}" />${select("决定", "decision", "approved", [["approved", "通过"], ["rejected", "拒绝"]], disabled)}<label><span>原因</span><input name="reasonCode" value="owner.approved" pattern="[a-z][a-z0-9_.-]{2,79}" required${disabled} /></label>${stepUp(disabled, true)}${confirm("提交", disabled)}</form></td>
    </tr>`).join("")}</tbody></table></div></section>`;
}

function statementSection(items, state) {
  const filters = state.channelStatementFilters ?? {};
  return `<section class="platform-band platform-channel-statements"><div class="platform-section-heading"><div><p>对账投影</p><h2>渠道结算单</h2></div><span>${number(state.channelStatements?.total ?? 0)} 条记录</span></div>
    <form class="platform-filter-form" data-platform-channel-statement-filter-form autocomplete="off"><label><span>合作方 ID</span><input name="partnerId" value="${escapeAttribute(filters.partnerId ?? "")}" pattern="par_[0-9a-f]{32}" maxlength="36" /></label>${select("币种", "currency", filters.currency, [["", "全部"], ["CNY", "CNY"], ["USD", "USD"]])}<button class="platform-primary" type="submit" ${state.loading ? "disabled" : ""}>筛选</button></form>
    <div class="platform-table-wrap"><table><thead><tr><th>合作方 / 周期</th><th>币种</th><th>来源金额</th><th>佣金</th><th>明细数</th><th>对账标记</th><th>状态</th></tr></thead><tbody>${items.map((item) => `<tr><td data-label="合作方 / 周期"><strong>${shortId(item.channel_partner_id)}</strong><small>${formatDate(item.period_start)} - ${formatDate(item.period_end)}</small></td><td data-label="币种">${escapeHtml(item.currency)}</td><td data-label="来源金额">${formatMicros(item.total_amount_micros)}</td><td data-label="佣金">${formatMicros(item.total_commission_micros)}</td><td data-label="明细数">${number(item.line_count)}</td><td data-label="对账标记"><details class="platform-audit-technical"><summary>技术信息</summary><code>${shortHash(item.reconciliation_sha256)}</code></details></td><td data-label="状态">${status(item.status)}<small>修订 ${number(item.close_revision)}</small></td></tr>`).join("") || '<tr><td colspan="7" class="platform-empty">当前筛选下没有结算单。</td></tr>'}</tbody></table></div>${pagination("channel-statement", state.channelStatements, state.loading)}</section>`;
}

function issuedReferral(value) { return `<section class="platform-channel-issued" role="status"><div><strong>Referral code issued</strong><code>${escapeHtml(value)}</code></div><button type="button" data-platform-action="channel-referral-dismiss" title="Dismiss" aria-label="Dismiss">&times;</button></section>`; }
function recentActivity(state) { return `<section class="platform-channel-recent" role="status">${state.channelLatestLead ? `<span>Lead <strong>${shortId(state.channelLatestLead.channel_lead_id)}</strong> registered</span>` : ""}${state.channelLatestAttribution ? `<span>Attribution <strong>${shortId(state.channelLatestAttribution.workspace_attribution_id)}</strong> · ${escapeHtml(label(state.channelLatestAttribution.status))}</span>` : ""}</section>`; }
function partnerHidden(partner) { return `<input type="hidden" name="partnerId" value="${escapeAttribute(partner.channel_partner_id)}" /><input type="hidden" name="expectedRevision" value="${number(partner.revision)}" />`; }
function stepUp(disabled, compact = false) { return `<label class="${compact ? "platform-channel-compact" : ""}"><span>验证方式</span><select name="method" data-channel-step-up-method${disabled}><option value="totp">动态验证码</option><option value="webauthn">安全密钥</option></select></label><label data-platform-step-up-code class="${compact ? "platform-channel-compact" : ""}"><span>验证码</span><input type="password" name="verificationCode" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" autocomplete="one-time-code" data-platform-sensitive required${disabled} /></label>`; }
function confirm(text, disabled) { return `<label class="platform-check"><input type="checkbox" name="confirmed" required${disabled} /><span>确认本次操作</span></label><button class="platform-primary" type="submit"${disabled}>${escapeHtml(text)}</button>`; }
function select(labelText, name, current, values, disabled = "") { return `<label><span>${escapeHtml(labelText)}</span><select name="${escapeAttribute(name)}"${disabled}>${values.map(([value, text]) => `<option value="${escapeAttribute(value)}" ${value === (current ?? "") ? "selected" : ""}>${escapeHtml(text)}</option>`).join("")}</select></label>`; }
function pagination(prefix, response, loading) { return response ? `<div class="platform-pagination"><span>第 ${number(Math.floor(response.offset / response.limit) + 1)} 页 · 每页 ${number(response.limit)} 条</span><div><button type="button" data-platform-action="${prefix}-previous" ${response.offset > 0 && !loading ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="${prefix}-next" ${response.offset + response.limit < response.total && !loading ? "" : "disabled"}>下一页</button></div></div>` : ""; }
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
