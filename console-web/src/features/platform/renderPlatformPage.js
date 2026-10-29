// 平台渲染器只消费严格安全 DTO；所有服务端字符串转义后进入 DOM，不展示内部地址、Provider 或签名。
import { NOTIFICATION_EVENT_PARAMETERS } from "../../lib/notifications.js";
import { renderThemeToggle } from "../../lib/theme.js";
import { icon } from "../public-home/icons.js";
import { renderLaunchGates } from "../launch-gates/renderLaunchGates.js";
import { renderGaLaunchGates } from "../launch-gates/renderGaLaunchGates.js";
import { renderChannelManagement } from "../channels/renderChannelManagement.js";
import { renderBrandEditor } from "../brand-domains/renderBrandEditor.js";
import { renderPlatformCustomerSuccess } from "../platform-customer-success/renderPlatformCustomerSuccess.js";
import { renderPlatformGrowth } from "../platform-growth/renderPlatformGrowth.js";
import { renderPlatformProviderEngineering } from "../platform-provider-engineering/renderPlatformProviderEngineering.js";
import { renderPlatformCommercialAccounting } from "../platform-commercial-accounting/renderPlatformCommercialAccounting.js";
import { renderPlatformConsoleExperience } from "../platform-console-experience/renderPlatformConsoleExperience.js";
import { renderConsoleMigration } from "../console-migration/renderConsoleMigration.js";
import {
  edgeCommandLabel,
  edgeDiagnosticStatusLabel,
  edgeHealthLabel,
  renderEdgeHealthScore,
} from "../edge-operations/edgeHealthPresentation.js";

const ROLE_OPTIONS = [
  "platform_owner",
  "platform_ops",
  "platform_security",
  "platform_billing",
  "platform_support",
  "platform_verification",
  "platform_compliance",
  "platform_privacy",
  "release_manager",
  "platform_provider_engineer",
  "platform_channel",
  "platform_customer_success",
  "platform_growth",
  "platform_communications",
];

const NAVIGATION_GROUPS = [
  ["运营工作台", "activity", [["overview", "平台总览"], ["approvals", "审批队列"], ["workspaces", "组织目录"], ["customer-success", "客户成功"], ["support", "支持协作"]]],
  ["网关与基础设施", "cpu", [["infrastructure", "基础设施"], ["gateway", "Gateway 集群"], ["edge-fleet", "私有 Gateway"], ["edge-upgrades", "网关升级"], ["edge-licenses", "网关许可证"], ["edge-backups", "网关备份"], ["offline-bundles", "离线交换"]]],
  ["服务运行", "activity", [["job-runtime", "任务运行时"], ["config-distribution", "配置分发"], ["observability", "可观测性"], ["status-incidents", "状态与事件"], ["capacity", "容量与韧性"], ["disaster-recovery", "灾难恢复"]]],
  ["账务与增长", "chart-no-axes-combined", [["billing", "账单与计量"], ["commercial-accounting", "商业账务"], ["channels", "渠道管理"], ["growth", "增长分析"]]],
  ["安全与治理", "shield-check", [["operators", "操作员"], ["verification", "企业认证"], ["abuse-risk", "滥用风控"], ["audit", "平台审计"], ["compliance", "合规复核"], ["privacy", "隐私请求"], ["data-residency", "数据驻留"], ["security-assurance", "安全评估"]]],
  ["发布与供应商", "git-branch", [["feature-rollouts", "功能发布"], ["launch-gates", "发布门禁"], ["console-migration", "控制台迁移"], ["releases", "接口与 SDK 发布"], ["providers", "供应商"], ["provider-engineering", "供应商工程"], ["provider-catalog", "供应商目录"]]],
  ["品牌与通知", "layers", [["branding", "平台品牌"], ["mail-settings", "邮件服务"], ["notification-templates", "通知模板"]]],
];

export function renderPlatformGate(container, state = {}) {
  container.innerHTML = `
    <div class="platform-gate">
      <header class="platform-gate-brand">
        <span class="platform-mark" aria-hidden="true">M</span>
        <div><strong>MetaHub</strong><span>平台管理</span></div>
        ${renderThemeToggle()}
      </header>
      <section class="platform-gate-panel" aria-labelledby="platform-gate-title">
        <p>平台身份</p>
        <h1 id="platform-gate-title">进入工作台</h1>
        ${feedback(state)}
        <form data-platform-session-form autocomplete="off">
          ${state.mfaRequired ? '<label><span>动态验证码</span><input name="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required autofocus /></label>' : '<p>默认使用账号密码登录；仅在手动启用 MFA 后验证动态验证码。</p>'}
          <button class="platform-primary" type="submit" ${state.loading ? 'disabled' : ''}>${state.mfaRequired ? '验证并进入' : '进入工作台'}</button>
        </form>
        <a href="identity.html?view=login">登录账号</a>
        <a href="identity.html?view=account#security">返回账号安全</a>
      </section>
    </div>`;
}

export function renderPlatformWorkspace(container, state) {
  // Preserve a user's expanded group when an async refresh updates this view.
  const previousView = container.querySelector?.('[data-platform-view][aria-current="page"]')?.dataset.platformView;
  const expandedGroup = previousView === state.view
    ? Array.from(container.querySelectorAll?.('.platform-nav-group[open]') ?? [])
      .map((group) => group.querySelector('[data-platform-view]')?.dataset.platformView)
    : null;
  const context = state.session?.context;
  const operator = context?.operator ?? {};
  const permissions = new Set(context?.permissions ?? []);
  const disasterRecoveryBusy = Boolean(
    state.loading && (state.disasterRecoveryIntent || state.statusIncidentIntent
      || state.providerCatalogPublishIntent || state.developerContractIntent
      || state.securityAssuranceIntent || state.launchIntent
      || state.edgeUpgradeIntent || state.edgeLicenseIntent
      || state.offlineBundleIntent)
  );
  container.innerHTML = `
    <div class="platform-shell">
      <header class="platform-topbar">
        <a class="platform-wordmark" href="platform.html"><span class="platform-mark" aria-hidden="true">M</span><strong>MetaHub</strong></a>
        <div class="platform-operator-summary"><span>${escapeHtml(operator.display_name ?? "-")}</span><strong>${escapeHtml((operator.roles ?? []).map(roleLabel).join(" · "))}</strong></div>
        <div class="platform-topbar-actions">
          ${renderThemeToggle()}
          <button class="platform-icon-button platform-navigation-toggle" type="button" data-platform-action="toggle-navigation" aria-label="${state.navigationOpen ? "收起平台导航" : "展开平台导航"}" title="${state.navigationOpen ? "收起平台导航" : "展开平台导航"}" aria-expanded="${Boolean(state.navigationOpen)}" aria-controls="platform-navigation">${icon("layers")}</button>
          <button class="platform-icon-button" type="button" data-platform-action="refresh" title="刷新" aria-label="刷新" ${disasterRecoveryBusy ? "disabled" : ""}>${icon("rotate-cw")}</button>
          <a href="identity.html?view=account#security">账号安全</a>
        </div>
      </header>
      <div class="platform-body">
        <aside id="platform-navigation" class="platform-sidebar${state.navigationOpen ? " is-open" : ""}" aria-label="平台导航">
          ${renderNavigation(state.navigation ?? [], state.view, disasterRecoveryBusy)}
        </aside>
        <section class="platform-content" aria-live="polite" aria-busy="${Boolean(state.loading)}" tabindex="-1">
          ${feedback(state)}
          ${pendingConfirmation(state)}
          ${renderView(state, permissions)}
        </section>
      </div>
    </div>`;
  if (expandedGroup) {
    container.querySelectorAll?.('.platform-nav-group').forEach((group) => {
      group.open = expandedGroup.includes(group.querySelector('[data-platform-view]')?.dataset.platformView);
    });
  }
}

function renderView(state, permissions) {
  if (new Set(["overview", "approvals"]).has(state.view)) {
    return renderPlatformConsoleExperience(state.consoleExperience, state.view);
  }
  if (state.view === "console-migration") {
    return renderConsoleMigration(state.consoleMigration, permissions);
  }
  if (state.view === "infrastructure") return infrastructureView(state, permissions);
  if (state.view === "gateway") return gatewayView(state, permissions);
  if (state.view === "edge-fleet") return edgeFleetView(state, permissions);
  if (state.view === "edge-upgrades") return edgeUpgradesView(state, permissions);
  if (state.view === "edge-licenses") return edgeLicensesView(state, permissions);
  if (state.view === "edge-backups") return edgeBackupsView(state);
  if (state.view === "offline-bundles") return offlineBundlesView(state, permissions);
  if (state.view === "job-runtime") return jobRuntimeView(state, permissions);
  if (state.view === "feature-rollouts") return featureRolloutsView(state, permissions);
  if (state.view === "config-distribution") return configDistributionView(state, permissions);
  if (state.view === "observability") return observabilityView(state, permissions);
  if (state.view === "disaster-recovery") return disasterRecoveryView(state, permissions);
  if (state.view === "status-incidents") return statusIncidentsView(state, permissions);
  if (state.view === "capacity") return capacityView(state, permissions);
  if (state.view === "data-residency") return dataResidencyView(state);
  if (state.view === "security-assurance") return securityAssuranceView(state, permissions);
  if (state.view === "launch-gates") {
    return `${renderLaunchGates(state, permissions)}${state.gaLaunchGovernance ? renderGaLaunchGates(state.gaLaunchGovernance) : ""}`;
  }
  if (state.view === "abuse-risk") return abuseRiskView(state, permissions);
  if (state.view === "operators") return operatorsView(state, permissions);
  if (state.view === "mail-settings") return mailSettingsView(state);
  if (state.view === "workspaces") return workspacesView(state, permissions);
  if (state.view === "support") return supportView(state, permissions);
  if (state.view === "audit") return auditView(state);
  if (state.view === "billing") return billingView(state, permissions);
  if (state.view === "privacy") return privacyView(state);
  if (state.view === "compliance") return complianceView(state, permissions);
  if (state.view === "verification") return verificationView(state, permissions);
  if (state.view === "notification-templates") return notificationTemplatesView(state, permissions);
  if (state.view === "provider-catalog") return providerCatalogView(state, permissions);
  if (state.view === "provider-engineering") {
    return renderPlatformProviderEngineering(state.providerEngineering, permissions);
  }
  if (state.view === "commercial-accounting") {
    return renderPlatformCommercialAccounting(state.commercialAccounting, permissions);
  }
  if (state.view === "releases") return developerContractsView(state, permissions);
  if (state.view === "channels") return renderChannelManagement(state, permissions);
  if (state.view === "branding") return renderPlatformBranding(state);
  if (state.view === "customer-success") {
    return renderPlatformCustomerSuccess(
      state.customerSuccess,
      permissions,
      state.session?.context,
    );
  }
  if (state.view === "growth") {
    return renderPlatformGrowth(state.growth, permissions);
  }
  return domainPlaceholderView(state);
}

function renderPlatformBranding(state) {
  return `<header class="platform-page-heading"><div><p>平台负责人</p><h1>平台品牌</h1></div><span>独立发布域</span></header>
    ${renderBrandEditor(state.brandDomains?.platform, {
      scope: "platform-brand",
      kicker: "SaaS public identity",
      title: "登录与平台外观",
      allowDomain: true,
    })}`;
}

function providerCatalogView(state, permissions) {
  const response = state.providerCatalog ?? {};
  const current = response.current ?? null;
  const recent = Array.isArray(response.recent_versions) ? response.recent_versions : [];
  const canManage = permissions.has("platform.provider_catalog.manage");
  const securityProjection = response.security_projection === true;
  return `
    <header class="platform-page-heading">
      <div><p>签名发布通道</p><h1>供应商目录</h1></div>
      <span>${securityProjection ? "安全投影" : "发布管理"}</span>
    </header>
    <section class="platform-metrics" aria-label="供应商目录发布摘要">
      ${metric("代数", current?.generation ?? 0, "不可变当前版本")}
      ${metric("条目", current?.entry_count ?? 0, "已发布矩阵")}
      ${metric("兼容性", current?.compatibility_change_count ?? 0, "当前版本变更")}
      ${metric("最近版本", recent.length, "保留版本数")}
    </section>
    <section class="platform-band platform-provider-catalog-head">
      <div class="platform-section-heading"><div><p>已验证发布</p><h2>当前版本</h2></div>${current ? status("active") : ""}</div>
      ${current ? `<dl>
        <div><dt>版本</dt><dd>${escapeHtml(current.version)}</dd></div>
        <div><dt>发布代数</dt><dd>${number(current.generation)}</dd></div>
        <div><dt>目录校验</dt><dd><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(shortHash(current.manifest_sha256))}</code></details></dd></div>
        <div><dt>签名配置</dt><dd><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(current.key_id)}</code><code>${escapeHtml(current.signature_algorithm)}</code></details></dd></div>
        <div><dt>签名证明</dt><dd><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(shortHash(current.signature_sha256))}</code></details></dd></div>
        <div><dt>发布时间</dt><dd>${formatTime(current.published_at)}</dd></div>
      </dl>` : '<p class="platform-empty">供应商目录暂不可用。</p>'}
    </section>
    <section class="platform-band platform-provider-catalog-history">
      <div class="platform-section-heading"><div><p>不可变历史</p><h2>最近版本</h2></div><span>${number(recent.length)} 个版本</span></div>
      <div class="platform-table-wrap"><table class="platform-provider-catalog-table">
        <thead><tr><th>版本</th><th>代数</th><th>条目</th><th>兼容性变更</th><th>目录校验</th><th>发布时间</th></tr></thead>
        <tbody>${recent.map((item) => `<tr>
          <td data-label="版本"><strong>${escapeHtml(item.version)}</strong></td>
          <td data-label="代数">${number(item.generation)}</td>
          <td data-label="条目">${number(item.entry_count)}</td>
          <td data-label="兼容性变更">${number(item.compatibility_change_count)}</td>
          <td data-label="目录校验"><details class="platform-audit-technical"><summary>查看</summary><code>${escapeHtml(shortHash(item.manifest_sha256))}</code></details></td>
          <td data-label="发布时间">${formatTime(item.published_at)}</td>
        </tr>`).join("") || emptyRow("暂无目录版本", 6)}</tbody>
      </table></div>
    </section>
    ${canManage ? `<details class="platform-band platform-collapsed-section platform-provider-catalog-release">
      <summary>发布供应商目录 <span>只有目录内容或签名发生变化时展开</span></summary>
      <form data-platform-provider-catalog-form autocomplete="off">
        <label class="platform-provider-catalog-manifest" data-contract-label="Canonical manifest"><span>规范清单</span><textarea name="manifest" rows="12" maxlength="250000" spellcheck="false" required placeholder='{"schema_version":1,"version":"YYYY.MM.DD.N","entries":[]}'></textarea></label>
        <label><span>签名密钥编号</span><input name="keyId" pattern="[A-Za-z0-9][A-Za-z0-9._:-]{2,127}" maxlength="128" required /></label>
        <label><span>签名内容</span><input name="signature" pattern="[A-Za-z0-9_-]{86}" minlength="86" maxlength="86" autocomplete="off" required /></label>
        <label><span>发布原因</span><select name="reasonCode"><option value="catalog_release">目录更新</option><option value="compliance_update">合规更新</option><option value="endpoint_update">服务地址更新</option></select></label>
        <label class="platform-check platform-provider-catalog-confirm"><input name="confirmed" type="checkbox" required /><span>确认版本、签名、区域与兼容变更已完成发布审核</span></label>
        <button class="platform-primary" type="submit" ${state.loading || !current ? "disabled" : ""}>发布版本</button>
      </form>
    </details>` : ""}
  `;
}

function dataResidencyView(state) {
  const response = state.residencyEvidence ?? { items: [], next_cursor: null };
  const items = Array.isArray(response.items) ? response.items : [];
  const filters = state.residencyEvidenceFilters ?? {};
  const compliant = items.filter((item) => item.outcome === "compliant").length;
  const blocked = items.filter((item) => item.outcome === "blocked").length;
  const violations = items.filter((item) => item.outcome === "violation").length;
  const page = state.residencyEvidenceCursorHistory.length + 1;
  return `
    <header class="platform-page-heading">
      <div><p>区域与数据位置</p><h1>数据驻留</h1></div>
      <span>按组织策略记录</span>
    </header>
    <section class="platform-metrics" aria-label="当前页驻留证据摘要">
      ${metric("合规", compliant, "当前页")}
      ${metric("阻断", blocked, "失败关闭")}
      ${metric("违规", violations, "需要处置")}
    </section>
    <section class="platform-band platform-residency-evidence">
      <div class="platform-section-heading">
        <div><p>已记录的区域与存储位置</p><h2>驻留观测</h2></div>
        <span>第 ${page} 页 · ${number(items.length)} 条</span>
      </div>
      <form class="platform-filter-form" data-platform-residency-filter-form autocomplete="off">
        <label class="platform-filter-search"><span>组织编号</span><input name="workspaceId" value="${escapeAttribute(filters.workspaceId ?? "")}" pattern="wsp_[0-9a-f]{32}" maxlength="36" placeholder="留空查看全部" /></label>
        <label><span>组件</span><select name="component">${filterOptions(filters.component ?? "", [
          ["", "全部组件"], ["gateway", "网关"], ["database", "数据库"],
          ["object_storage", "对象存储"], ["backup", "备份"],
          ["job_runtime", "后台任务"], ["disaster_recovery", "灾备"],
          ["tenant_data", "组织数据"],
        ])}</select></label>
        <label><span>结论</span><select name="outcome">${filterOptions(filters.outcome ?? "", [
          ["", "全部结论"], ["compliant", "合规"], ["migration_required", "需要迁移"],
          ["blocked", "已阻断"], ["violation", "违规"],
        ])}</select></label>
        <button class="platform-primary" type="submit" ${state.loading ? "disabled" : ""}>筛选</button>
      </form>
      <div class="platform-table-wrap">
        <table class="platform-residency-table">
        <thead><tr><th>组织</th><th>组件</th><th>区域</th><th>策略</th><th>结论</th><th>观测进度</th><th>证明</th><th>时间</th></tr></thead>
          <tbody>${items.map(residencyEvidenceRow).join("") || emptyRow("当前筛选下没有驻留证据", 8)}</tbody>
        </table>
      </div>
      <div class="platform-pagination">
        <span>显示组件、区域、策略版本和结论</span>
        <div>
          <button type="button" data-platform-action="residency-evidence-previous" ${page > 1 && !state.loading ? "" : "disabled"}>上一页</button>
          <button type="button" data-platform-action="residency-evidence-next" ${response.next_cursor && !state.loading ? "" : "disabled"}>下一页</button>
        </div>
      </div>
    </section>
  `;
}

function residencyEvidenceRow(item) {
  const violation = item.violation_code ? residencyViolationLabel(item.violation_code) : "无";
  return `<tr>
    <td data-label="组织"><strong>${escapeHtml(shortWorkspace(item.workspace_id))}</strong><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(item.workspace_id)}</code></details></td>
    <td data-label="组件"><strong>${escapeHtml(residencyComponentLabel(item.component_code))}</strong><small>${escapeHtml(statusLabel(item.storage_class))}</small></td>
    <td data-label="区域">${escapeHtml(item.region_code)}</td>
    <td data-label="策略">版本 ${number(item.policy_generation)}</td>
    <td data-label="结论">${status(item.outcome)}<small>${escapeHtml(violation)}</small></td>
    <td data-label="观测进度">${number(item.observed_watermark)}</td>
    <td data-label="证明"><details class="platform-audit-technical"><summary>查看</summary><code>${escapeHtml(item.manifest_sha256 ?? "-")}</code></details></td>
    <td data-label="时间">${formatTime(item.observed_at)}</td>
  </tr>`;
}

function residencyComponentLabel(value) {
  return ({
    gateway: "网关",
    database: "数据库",
    object_storage: "对象存储",
    backup: "备份",
    job_runtime: "后台任务",
    disaster_recovery: "灾备",
    tenant_data: "组织数据",
  })[value] ?? "未知组件";
}

function residencyViolationLabel(value) {
  return ({
    region_not_allowed: "区域 不在允许组",
    generation_stale: "Generation 过期",
    policy_expired: "策略已过期",
    policy_unavailable: "策略不可用",
    hold_active: "法律保全生效",
  })[value] ?? "未知结论";
}

function statusIncidentsView(state, permissions) {
  const response = state.statusIncidents ?? {};
  const components = Array.isArray(response.components) ? response.components : [];
  const incidents = Array.isArray(response.incidents) ? response.incidents : [];
  const maintenance = Array.isArray(response.maintenance_windows)
    ? response.maintenance_windows : [];
  const active = incidents.filter((item) => !new Set(["resolved", "postmortem_published"]).has(item.status));
  const canWrite = permissions.has("platform.status.write");
  const canApprove = permissions.has("platform.status.security_approve");
  return `
    <header class="platform-page-heading">
      <div><p>服务公开状态与故障记录</p><h1>服务状态</h1></div>
      <button class="platform-icon-button" type="button" data-platform-action="status-refresh"
        title="刷新状态" aria-label="刷新状态"
        ${state.loading ? "disabled" : ""}>↻</button>
    </header>
    <section class="platform-metrics" aria-label="服务状态摘要">
      ${metric("服务组件", components.length, "公开目录")}
      ${metric("处理中故障", active.length, "当前记录")}
      ${metric("维护安排", maintenance.filter((item) => item.status === "planned").length, "已排期")}
      ${metric("状态版本", response.bundle_generation ?? 0, response.bundle_generation ? "已发布" : "暂无发布")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading"><div><p>对外展示的服务组件</p><h2>当前服务状态</h2></div></div>
      <div class="platform-table-wrap"><table class="platform-status-component-table">
        <thead><tr><th>组件</th><th>分组</th><th>状态</th><th>版本</th></tr></thead>
        <tbody>${components.map((item) => `<tr>
          <td data-label="组件"><strong>${escapeHtml(item.name)}</strong><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(item.code)}</code></details></td>
          <td data-label="分组">${escapeHtml(platformServiceGroupLabel(item.group))}</td>
          <td data-label="状态">${status(item.status)}</td>
          <td data-label="版本">${number(item.revision)}</td>
        </tr>`).join("") || '<tr><td colspan="4" class="platform-empty">暂无服务组件。</td></tr>'}</tbody>
      </table></div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading"><div><p>公开故障记录</p><h2>故障时间线</h2></div><span>${number(incidents.length)} 条记录</span></div>
      <div class="platform-status-incident-list">
        ${incidents.map((item) => statusIncidentItem(item, { canWrite, canApprove, loading: state.loading })).join("")
          || '<p class="platform-empty">暂无故障记录。</p>'}
      </div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading"><div><p>计划内服务维护</p><h2>维护安排</h2></div><span>${number(maintenance.length)} 个安排</span></div>
      <div class="platform-status-maintenance-list">
        ${maintenance.map((item) => `<article><header><strong>${escapeHtml(item.title)}</strong>${status(item.status)}</header>
          <small>${formatTime(item.starts_at)} → ${formatTime(item.ends_at)}</small>
          <p>${escapeHtml(item.message)}</p></article>`).join("") || '<p class="platform-empty">暂无维护安排。</p>'}
      </div>
    </section>
    ${canWrite ? statusIncidentCreateForm(state.loading) : ""}
    ${canWrite ? statusMaintenanceCreateForm(state.loading) : ""}`;
}

function statusIncidentItem(item, access) {
  const latest = item.updates.at(-1);
  const approval = access.canApprove && item.security_approval_available
    ? statusIncidentMutationForm(item, "security-approval", "通过公开审核", access.loading)
    : "";
  const update = access.canWrite && new Set(["published", "monitoring"]).has(item.status)
    ? statusIncidentMutationForm(item, "update", "更新故障状态", access.loading)
    : "";
  const postmortem = access.canWrite && item.status === "resolved" && item.postmortem === null
    ? statusPostmortemForm(item, access.loading) : "";
  return `<article class="platform-status-incident">
    <header><div><strong>${escapeHtml(item.title)}</strong><small>${incidentClassificationLabel(item.classification)} · ${incidentVisibilityLabel(item.visibility)}</small></div>${status(item.status)}</header>
    <p>${latest ? escapeHtml(latest.message) : "等待安全复核。"}</p>
    <small>确认于 ${formatTime(item.acknowledged_at)}</small>
    <details><summary>${number(item.updates.length)} 条更新</summary>
      <ol>${item.updates.map((entry) => `<li><time>${formatTime(entry.published_at)}</time><p>${escapeHtml(entry.message)}</p></li>`).join("")}</ol>
    </details>${approval}${update}${postmortem}
  </article>`;
}

function statusIncidentMutationForm(item, action, label, loading) {
  return `<details class="platform-inline-technical-form"><summary>${escapeHtml(label)}</summary><form data-platform-status-incident-form>
    <input type="hidden" name="action" value="${escapeAttribute(action)}" />
    <input type="hidden" name="incidentId" value="${escapeAttribute(item.incident_id)}" />
    <input type="hidden" name="revision" value="${number(item.revision)}" />
    ${action === "update" ? '<label><span>下一状态</span><select name="status"><option value="monitoring">持续观察</option><option value="resolved">已解决</option></select></label>' : ""}
    <label><span>公开更新</span><textarea name="publicMessage" minlength="10" maxlength="2000" required></textarea></label>
    <label><span>影响组件</span><input name="affectedComponents" placeholder="可留空" /></label>
    <label><span>影响区域</span><input name="affectedRegions" placeholder="可留空" /></label>
    <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认公开范围</span></label>
    <button class="platform-primary" type="submit" ${loading ? "disabled" : ""}>保存更新</button>
  </form></details>`;
}

function statusPostmortemForm(item, loading) {
  return `<details><summary>填写复盘</summary><form data-platform-status-postmortem-form>
    <input type="hidden" name="incidentId" value="${escapeAttribute(item.incident_id)}" />
    <input type="hidden" name="revision" value="${number(item.revision)}" />
    <label><span>公开总结</span><textarea name="summary" minlength="20" maxlength="4000" required></textarea></label>
    <label><span>时间线</span><textarea name="timeline" required></textarea></label>
    <label><span>原因分类</span><select name="causeCategory">${["capacity", "dependency", "deployment", "configuration", "software", "network", "other"].map((value) => `<option value="${value}">${statusLabel(value)}</option>`).join("")}</select></label>
    <label><span>改进措施</span><textarea name="correctiveActions" required></textarea></label>
    <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认公开范围</span></label>
    <button class="platform-primary" type="submit" ${loading ? "disabled" : ""}>发布复盘</button>
  </form></details>`;
}

function statusIncidentCreateForm(loading) {
  return `<details class="platform-band platform-collapsed-section"><summary>登记服务故障 <span>仅在需要发布新的故障通知时展开</span></summary>
    <form data-platform-status-create-form>
      <label><span>组织编号</span><input name="ownerWorkspaceId" pattern="wsp_[0-9a-f]{32}" required /></label>
      <label><span>标题</span><input name="title" minlength="5" maxlength="160" required /></label>
      <label><span>类型</span><select name="classification"><option value="reliability">稳定性</option><option value="security">安全</option></select></label>
      <label><span>可见范围</span><select name="visibility"><option value="public">公开</option><option value="workspace">组织内</option></select></label>
      <label><span>严重程度</span><select name="severity"><option value="minor">一般</option><option value="major">重要</option><option value="critical">严重</option></select></label>
      <label><span>确认时间</span><input name="acknowledgedAt" type="datetime-local" required /></label>
      <label><span>首次公开说明</span><textarea name="initialUpdate" maxlength="2000"></textarea></label>
      <label><span>影响组件</span><input name="affectedComponents" /></label>
      <label><span>影响区域</span><input name="affectedRegions" /></label>
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认公开范围</span></label>
      <button class="platform-primary" type="submit" ${loading ? "disabled" : ""}>发布故障通知</button>
    </form></details>`;
}

function statusMaintenanceCreateForm(loading) {
  return `<details class="platform-band platform-collapsed-section"><summary>登记维护安排 <span>仅在需要发布维护通知时展开</span></summary>
    <form data-platform-maintenance-form>
      <label><span>组织编号</span><input name="ownerWorkspaceId" pattern="wsp_[0-9a-f]{32}" required /></label>
      <label><span>标题</span><input name="title" minlength="5" maxlength="160" required /></label>
      <label><span>公开说明</span><textarea name="publicMessage" minlength="10" maxlength="2000" required></textarea></label>
      <label><span>可见范围</span><select name="visibility"><option value="public">公开</option><option value="workspace">组织内</option></select></label>
      <label><span>开始时间</span><input name="startsAt" type="datetime-local" required /></label>
      <label><span>结束时间</span><input name="endsAt" type="datetime-local" required /></label>
      <label><span>影响组件</span><input name="affectedComponents" /></label>
      <label><span>影响区域</span><input name="affectedRegions" /></label>
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认维护安排</span></label>
      <button class="platform-primary" type="submit" ${loading ? "disabled" : ""}>发布维护通知</button>
    </form></details>`;
}

function incidentClassificationLabel(value) {
  return ({ reliability: "稳定性", security: "安全" })[value] ?? statusLabel(value);
}

function incidentVisibilityLabel(value) {
  return ({ public: "公开", workspace: "组织内" })[value] ?? statusLabel(value);
}

function platformServiceGroupLabel(value) {
  return ({ gateway: "网关", control: "管理控制台", identity: "身份服务", billing: "账务", integrations: "集成" })[value] ?? statusLabel(value);
}

function capacityView(state, permissions) {
  const response = state.capacity ?? {};
  const economicsResponse = state.unitEconomics ?? {};
  const services = Array.isArray(response.services) ? response.services : [];
  const scenarios = Array.isArray(response.scenarios) ? response.scenarios : [];
  const baselines = Array.isArray(response.baselines) ? response.baselines : [];
  const forecasts = Array.isArray(response.forecasts) ? response.forecasts : [];
  const loadTests = Array.isArray(response.load_tests) ? response.load_tests : [];
  const faults = Array.isArray(response.chaos_faults) ? response.chaos_faults : [];
  const experiments = Array.isArray(response.chaos_experiments)
    ? response.chaos_experiments : [];
  const blocks = Array.isArray(response.rollout_blocks) ? response.rollout_blocks : [];
  const economics = Array.isArray(economicsResponse.items) ? economicsResponse.items : [];
  const canRead = permissions.has("platform.capacity.read");
  const canTest = permissions.has("platform.capacity.test");
  const canChaos = permissions.has("platform.capacity.chaos");
  const canReadEconomics = permissions.has("platform.unit_economics.read");
  return `
    <header class="platform-page-heading">
      <div><p>服务容量、性能与稳定性</p><h1>容量与韧性</h1></div>
      <button class="platform-icon-button" type="button" data-platform-action="capacity-refresh"
        title="刷新容量状态" aria-label="刷新容量状态"
        ${state.loading ? "disabled" : ""}>↻</button>
    </header>
    ${canRead ? `<section class="platform-metrics" aria-label="Capacity summary">
      ${metric("服务记录", services.length, "当前区域")}
      ${metric("已达阈值", services.filter((item) => item.threshold_reached).length, "需要关注")}
      ${metric("运行中的测试", loadTests.filter((item) => new Set(["queued", "running"]).has(item.status)).length, "隔离执行")}
      ${metric("发布阻断", blocks.filter((item) => item.status === "active").length, "性能回退")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading"><div><p>按服务和区域汇总</p><h2>服务余量</h2></div></div>
      <div class="platform-table-wrap"><table class="platform-capacity-table">
        <thead><tr><th>服务 / 区域</th><th>当前需求</th><th>可用容量</th><th>剩余容量</th><th>阈值 / 目标</th><th>建议</th></tr></thead>
        <tbody>${services.map((item) => `<tr>
          <td data-label="服务 / 区域"><strong>${escapeHtml(capacityServiceLabel(item.service_code))}</strong><small>${escapeHtml(item.region_code)} · 版本 ${number(item.revision)}</small></td>
          <td data-label="当前需求"><strong>${number(item.demand_rps)} RPS</strong><small>已使用 ${basisPoints(item.utilization_bps)}</small></td>
          <td data-label="可用容量"><strong>${number(item.capacity_rps)} RPS</strong></td>
          <td data-label="剩余容量"><strong>${number(item.headroom_rps)} RPS</strong>${status(item.threshold_reached ? "threshold_reached" : "healthy")}</td>
          <td data-label="阈值 / 目标"><strong>${basisPoints(item.threshold_bps)}</strong><small>P95 ≤ ${number(item.slo_p95_ms)} 毫秒</small></td>
          <td data-label="建议">${status(capacityRecommendationLabel(item.recommendation_code))}</td>
        </tr>`).join("") || emptyRow("暂无容量预测。", 6)}</tbody>
      </table></div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading"><div><p>历史测量与未来估算</p><h2>基线与预测</h2></div><span>${number(baselines.length)} 个基线 · ${number(forecasts.length)} 个预测</span></div>
      <div class="platform-capacity-split">
        <div class="platform-table-wrap"><table class="platform-baseline-table">
          <thead><tr><th>基线</th><th>延迟</th><th>吞吐 / 错误率</th></tr></thead>
        <tbody>${baselines.map((item) => `<tr><td data-label="基线"><strong>${escapeHtml(item.scenario_code)}</strong><small>${escapeHtml(capacityEnvironmentLabel(item.environment_class))} · 版本 ${number(item.version)} · ${number(item.sample_count)} 个样本</small></td><td data-label="延迟"><strong>${number(item.p95_ms)} 毫秒 P95</strong><small>${number(item.p50_ms)} / ${number(item.p99_ms)} 毫秒</small></td><td data-label="吞吐 / 错误率"><strong>${decimalRps(item.throughput_rps_milli)} RPS</strong><small>错误率 ${basisPoints(item.error_rate_bps)}</small></td></tr>`).join("") || emptyRow("暂无基线数据。", 3)}</tbody>
        </table></div>
        <div class="platform-table-wrap"><table class="platform-forecast-table">
          <thead><tr><th>预测</th><th>需求 / 容量</th><th>可信度</th><th>任务</th></tr></thead>
          <tbody>${forecasts.map((item) => `<tr><td data-label="预测"><strong>${escapeHtml(capacityServiceLabel(item.service_code))} · ${number(item.horizon_days)} 天</strong><small>${escapeHtml(item.region_code)}</small>${status(item.status)}</td><td data-label="需求 / 容量"><strong>${number(item.predicted_demand_rps ?? 0)} / ${number(item.predicted_capacity_rps ?? 0)} RPS</strong></td><td data-label="可信度"><strong>${item.confidence_bps === null ? "待计算" : basisPoints(item.confidence_bps)}</strong><small>${escapeHtml(capacityRecommendationLabel(item.recommendation_code ?? "pending"))}</small></td><td data-label="任务"><strong>${shortReference(item.job_id)}</strong><small>版本 ${number(item.revision)}</small></td></tr>`).join("") || emptyRow("暂无预测数据。", 4)}</tbody>
        </table></div>
      </div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading"><div><p>只记录汇总结果</p><h2>负载与稳定性测试</h2></div><span>${number(loadTests.length)} 次</span></div>
      <div class="platform-table-wrap"><table class="platform-load-table">
        <thead><tr><th>场景</th><th>测试负载</th><th>进度</th><th>性能</th><th>连接与公平性</th></tr></thead>
        <tbody>${loadTests.map((item) => `<tr><td data-label="场景"><strong>${escapeHtml(item.scenario_code)}</strong><small>${escapeHtml(capacityEnvironmentLabel(item.environment_class))} · ${escapeHtml(item.region_code)}</small>${status(item.status)}</td><td data-label="测试负载"><strong>${number(item.synthetic_tenant_count)} 个模拟组织 · ${number(item.target_rps)} RPS</strong><small>${number(item.duration_seconds)} 秒</small></td><td data-label="进度"><strong>${shortReference(item.job_id)}</strong><small>${escapeHtml(statusLabel(item.safe_outcome_code ?? "queued"))}</small></td><td data-label="性能"><strong>${item.p95_ms === null ? "待计算" : `${number(item.p95_ms)} 毫秒 P95`}</strong><small>${escapeHtml(statusLabel(item.regression_status ?? "pending"))}</small></td><td data-label="连接与公平性"><strong>连接 ${number(item.open_connection_delta ?? 0)} · 句柄 ${number(item.handle_delta ?? 0)}</strong><small>公平性 ${item.fairness_jain_millionths === null ? "待计算" : basisPoints(Math.round(item.fairness_jain_millionths / 100))}</small></td></tr>`).join("") || emptyRow("暂无负载测试记录。", 5)}</tbody>
      </table></div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading"><div><p>需要双人确认的故障演练</p><h2>故障演练</h2></div><span>${number(experiments.length)} 次</span></div>
      <div class="platform-table-wrap"><table class="platform-chaos-table">
        <thead><tr><th>故障 / 范围</th><th>目标</th><th>审批</th><th>进度</th><th>操作</th></tr></thead>
        <tbody>${experiments.map((item) => capacityChaosRow(item, { canChaos, loading: state.loading })).join("") || emptyRow("暂无故障演练记录。", 5)}</tbody>
      </table></div>
    </section>
    ${canTest ? capacityTestForms(services, scenarios, state.loading) : ""}
    ${canChaos ? capacityChaosCreateForm(faults, services, state.loading) : ""}` : ""}
    ${canReadEconomics ? capacityEconomicsView(economics, economicsResponse.total) : ""}`;
}

function capacityChaosRow(item, access) {
  const stopping = ["queued", "running"].includes(item.status) && item.stop_reason_code === "operator_kill";
  const canApprove = access.canChaos && item.status === "pending_approval";
  const canKill = access.canChaos && !stopping && new Set(["pending_approval", "queued", "running"]).has(item.status);
  const approvalForm = canApprove ? `<form data-platform-capacity-chaos-form class="platform-capacity-inline-command">
    <input type="hidden" name="action" value="approve" /><input type="hidden" name="experimentId" value="${escapeAttribute(item.chaos_experiment_id)}" /><input type="hidden" name="revision" value="${number(item.revision)}" /><input type="hidden" name="reasonCode" value="release_validation" /><label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认审批</span></label><button type="submit" ${access.loading ? "disabled" : ""}>通过</button></form>` : "";
  const killForm = canKill ? `<form data-platform-capacity-chaos-form class="platform-capacity-inline-command">
    <input type="hidden" name="action" value="kill" /><input type="hidden" name="experimentId" value="${escapeAttribute(item.chaos_experiment_id)}" /><input type="hidden" name="revision" value="${number(item.revision)}" /><select name="reasonCode" aria-label="终止原因"><option value="operator_containment">人工控制</option><option value="slo_containment">服务目标保护</option></select><label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认终止</span></label><button class="platform-danger" type="submit" ${access.loading ? "disabled" : ""}>终止</button></form>` : "";
  return `<tr>
    <td data-label="故障 / 范围"><strong>${escapeHtml(item.fault_code)}</strong><small>${escapeHtml(item.service_code)} · ${escapeHtml(item.environment_class)} · ${escapeHtml(item.region_code)} · ${number(item.blast_tenant_count)} 个模拟组织</small>${status(stopping ? "stopping" : item.status)}</td>
    <td data-label="目标"><strong>p95 ${number(item.objective_p95_ms)} 毫秒</strong><small>错误率 ${basisPoints(item.objective_error_rate_bps)} · 停止阈值 ${basisPoints(item.stop_error_rate_bps)}</small></td>
    <td data-label="审批"><strong>${number(item.approvals.length)} / ${item.environment_class === "production" ? 2 : 0}</strong><small>当前授权人</small></td>
    <td data-label="进度"><strong>第 ${number(item.kill_generation)} 代</strong><small>${escapeHtml(item.stop_reason_code ?? "待处理")} · ${item.objective_met === null ? "待确认" : item.objective_met ? "已达到目标" : "未达到目标"}</small></td>
    <td data-label="操作">${approvalForm}${killForm || (!approvalForm ? `<span class="platform-restricted">${stopping ? "等待终止确认" : "已结束"}</span>` : "")}</td>
  </tr>`;
}

function capacityTestForms(services, scenarios, loading) {
  const activeScenarios = scenarios.filter((item) => item.status === "active");
  return `<details class="platform-band platform-collapsed-section"><summary>运行容量测试 <span>需要安排压测或预测时展开</span></summary>
    <div class="platform-capacity-command-grid">
      <form data-platform-capacity-forecast-form>
        <h3>容量预测</h3>
        <label><span>服务 / 区域</span><select name="capacityTarget">${services.map((item) => `<option value="${escapeAttribute(`${item.service_code}|${item.region_code}|${item.revision}`)}">${escapeHtml(item.service_code)} · ${escapeHtml(item.region_code)} · rev ${number(item.revision)}</option>`).join("")}</select></label>
        <label><span>预测周期</span><select name="horizonDays"><option value="30">30 天</option><option value="90">90 天</option></select></label>
        <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认生成预测</span></label><button class="platform-primary" type="submit" ${loading ? "disabled" : ""}>开始预测</button>
      </form>
      <form data-platform-capacity-load-form>
        <h3>负载与稳定性测试</h3>
        <label><span>场景</span><select name="scenarioCode">${activeScenarios.map((item) => `<option value="${escapeAttribute(item.scenario_code)}">${escapeHtml(item.scenario_code)} · ${escapeHtml(item.run_kind)}</option>`).join("")}</select></label>
        <label><span>环境</span><select name="environmentClass"><option value="dev">开发</option><option value="staging">预发布</option><option value="production">生产</option></select></label>
        <label><span>区域</span><select name="regionCode">${[...new Set(services.map((item) => item.region_code))].map((value) => `<option value="${escapeAttribute(value)}">${escapeHtml(value)}</option>`).join("")}</select></label>
        <label><span>模拟组织数</span><input name="syntheticTenantCount" type="number" min="1" max="500" value="100" required /></label>
        <label><span>目标 RPS</span><input name="targetRps" type="number" min="1" max="100000" value="1600" required /></label>
        <label><span>持续时间（秒）</span><input name="durationSeconds" type="number" min="10" max="259200" value="60" required /></label>
        <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认隔离执行</span></label><button class="platform-primary" type="submit" ${loading ? "disabled" : ""}>开始测试</button>
      </form>
    </div>
  </details>`;
}

function capacityChaosCreateForm(faults, services, loading) {
  const regions = [...new Set(services.map((item) => item.region_code))];
  return `<details class="platform-band platform-collapsed-section"><summary>创建故障演练 <span>只有需要验证故障切换时展开</span></summary>
    <form data-platform-capacity-chaos-form class="platform-capacity-chaos-create">
      <input type="hidden" name="action" value="create" />
      <label><span>故障类型</span><select name="faultCode">${faults.filter((item) => item.status === "active").map((item) => `<option value="${escapeAttribute(item.fault_code)}">${escapeHtml(item.fault_code)}</option>`).join("")}</select></label>
      <label><span>服务</span><select name="serviceCode"><option value="gateway">网关</option><option value="control">管理控制台</option><option value="status">状态服务</option><option value="shared">共享服务</option></select></label>
      <label><span>环境</span><select name="environmentClass"><option value="staging">预发布环境</option><option value="production">生产环境</option></select></label>
      <label><span>区域</span><select name="regionCode">${regions.map((value) => `<option value="${escapeAttribute(value)}">${escapeHtml(value)}</option>`).join("")}</select></label>
      <label><span>影响组织数</span><input name="blastTenantCount" type="number" min="1" max="100" value="10" required /></label>
      <label><span>持续时间（秒）</span><input name="durationSeconds" type="number" min="10" max="3600" value="60" required /></label>
      <label><span>目标 P95（毫秒）</span><input name="objectiveP95Ms" type="number" min="1" max="60000" value="2000" required /></label>
      <label><span>目标错误率</span><input name="objectiveErrorRateBps" type="number" min="0" max="10000" value="100" required /></label>
      <label><span>停止错误率</span><input name="stopErrorRateBps" type="number" min="1" max="10000" value="500" required /></label>
      <label><span>原因</span><select name="reasonCode"><option value="resilience_validation">稳定性验证</option><option value="release_validation">发布验证</option></select></label>
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认影响范围和停止条件</span></label><button class="platform-danger" type="submit" ${loading ? "disabled" : ""}>创建演练</button>
    </form>
  </details>`;
}

function capacityEconomicsView(items, total) {
  return `<section class="platform-band"><div class="platform-section-heading"><div><p>汇总用量与成本</p><h2>单位成本</h2></div><span>${number(total ?? items.length)} 条记录</span></div>
    <div class="platform-table-wrap"><table class="platform-economics-table">
      <thead><tr><th>周期 / 服务</th><th>请求数</th><th>Token</th><th>分摊成本</th><th>单位成本</th><th>记录时间</th></tr></thead>
      <tbody>${items.map((item) => `<tr><td data-label="Period / service"><strong>${escapeHtml(item.billing_period)} · ${escapeHtml(item.service_code)}</strong><small>allocation v${number(item.allocation_version)}</small></td><td data-label="Requests"><strong>${number(item.request_count)}</strong></td><td data-label="Tokens"><strong>${number(item.total_tokens)}</strong><small>${number(item.input_tokens)} in · ${number(item.output_tokens)} out</small></td><td data-label="Allocated cost"><strong>${number(item.allocated_cost_micros)} micros</strong><small>${escapeHtml(item.currency)}</small></td><td data-label="Unit costs"><strong>${number(item.cost_per_request_micros ?? 0)} / request</strong><small>${number(item.cost_per_million_tokens_micros ?? 0)} / 1M tokens</small></td><td data-label="Watermark"><strong>${shortHash(item.usage_watermark)}</strong><small>${formatTime(item.created_at)}</small></td></tr>`).join("") || emptyRow("暂无单位成本记录。", 6)}</tbody>
    </table></div>
  </section>`;
}

function basisPoints(value) { return `${(Number(value ?? 0) / 100).toFixed(2)}%`; }
function decimalRps(value) { return (Number(value ?? 0) / 1000).toFixed(3); }
function capacityServiceLabel(value) {
  return ({ gateway: "网关", control: "管理控制台", status: "状态服务", shared: "共享服务" })[value] ?? String(value ?? "未知服务").replaceAll("_", " ");
}
function capacityEnvironmentLabel(value) {
  return ({ dev: "开发环境", staging: "预发布环境", production: "生产环境" })[value] ?? statusLabel(value);
}
function capacityRecommendationLabel(value) {
  return ({ hold: "保持现状", scale_out: "扩容", scale_in: "缩容", investigate: "需要调查", pending: "待评估" })[value] ?? statusLabel(value);
}

function disasterRecoveryView(state, permissions) {
  const response = state.disasterRecoveryStatus ?? {};
  const backupResponse = state.disasterRecoveryBackups ?? {};
  const regions = Array.isArray(response.regions) ? response.regions : [];
  const plans = Array.isArray(response.plans) ? response.plans : [];
  const runs = Array.isArray(response.recent_runs) ? response.recent_runs : [];
  const recoveryPoints = Array.isArray(backupResponse.items) ? backupResponse.items : [];
  const objectives = response.objectives ?? {};
  const canExecute = permissions.has("platform.dr.execute");
  const canReview = permissions.has("platform.dr.approve")
    && permissions.has("platform.dr.security_review");
  return `
    <header class="platform-page-heading">
      <div><p>区域、备份与切换保护</p><h1>灾难恢复</h1></div>
      <button class="platform-icon-button" type="button" data-platform-action="dr-refresh"
        title="刷新灾难恢复状态" aria-label="刷新灾难恢复状态" ${state.loading ? "disabled" : ""}>↻</button>
    </header>
    <section class="platform-metrics platform-dr-metrics" aria-label="灾难恢复目标">
      ${metric("控制面 RPO", Math.round(Number(objectives.control_rpo_seconds ?? 0) / 60), "分钟上限")}
      ${metric("控制面 RTO", Math.round(Number(objectives.control_rto_seconds ?? 0) / 60), "分钟上限")}
      ${metric("网关 RTO", Math.round(Number(objectives.gateway_rto_seconds ?? 0) / 60), "分钟上限")}
      ${metric("最近恢复点", objectives.latest_recovery_age_seconds ?? 0,
        objectives.recovery_ready ? "秒 · 可恢复" : "秒 · 未就绪")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>当前写入区域与切换保护</p><h2>区域状态</h2></div>
        <span>${number(regions.filter((item) => item.health_status === "healthy").length)} / ${number(regions.length)} 健康</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-dr-region-table">
          <thead><tr><th>区域</th><th>驻留组</th><th>健康</th><th>写入状态</th><th>切换版本</th></tr></thead>
          <tbody>${regions.map(drRegionRow).join("") || emptyRow("暂无 区域 状态", 5)}</tbody>
        </table>
      </div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>经过确认的切换方案</p><h2>切换计划与审批</h2></div>
        <span>${canReview ? "安全复核权限" : canExecute ? "执行权限" : "只读"}</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-dr-plan-table">
          <thead><tr><th>计划</th><th>路径 / 驻留</th><th>目标</th><th>停止条件</th><th>双人控制</th></tr></thead>
          <tbody>${plans.map((plan) => drPlanRow(plan, regions, runs, {
            canExecute,
            canReview,
            loading: state.loading,
            operatorId: state.session?.context?.operator?.operator_id,
          })).join("") || emptyRow("暂无灾难恢复计划", 5)}</tbody>
        </table>
      </div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>切换执行记录</p><h2>最近执行</h2></div>
        <span>${number(runs.length)} 次</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-dr-run-table">
          <thead><tr><th>执行</th><th>审批</th><th>Checkpoint</th><th>目标结果</th><th>恢复证据</th></tr></thead>
          <tbody>${runs.map(drRunRow).join("") || emptyRow("暂无灾难恢复执行", 5)}</tbody>
        </table>
      </div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>已验证的备份记录</p><h2>恢复点</h2></div>
        <span>${number(backupResponse.total ?? recoveryPoints.length)} 个</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-dr-backup-table">
          <thead><tr><th>范围</th><th>Schema</th><th>完整性</th><th>Manifest</th><th>年龄</th></tr></thead>
          <tbody>${recoveryPoints.map(drRecoveryPointRow).join("") || emptyRow("暂无已验证恢复点", 5)}</tbody>
        </table>
      </div>
    </section>`;
}

function drRegionRow(item) {
  return `<tr>
    <td data-label="区域"><strong>${escapeHtml(item.region_code)}</strong><small>${formatTime(item.updated_at)}</small></td>
    <td data-label="驻留组"><strong>${escapeHtml(item.residency_group)}</strong></td>
    <td data-label="健康">${status(item.health_status)}</td>
    <td data-label="Writer">${status(item.writer_status)}</td>
    <td data-label="Epoch / Revision"><strong>${number(item.failover_epoch)}</strong><small>rev ${number(item.revision)}</small></td>
  </tr>`;
}

function drPlanRow(plan, regions, runs, access) {
  const source = regions.find((item) => item.region_code === plan.source_region_code);
  const pending = runs.find((item) => item.failover_plan_id === plan.failover_plan_id
    && item.plan_version === plan.version && item.status === "planned");
  const controls = [];
  if (access.canReview && pending
    && pending.approvals.some((item) => item.approval_kind === "execution")
    && !pending.approvals.some((item) => item.approval_kind === "security_review")
    && !pending.approvals.some((item) => item.operator_id === access.operatorId)) {
    controls.push(drCommandForm(plan, source, pending.run_kind, "security_review", access.loading, pending));
  }
  if (access.canExecute && !pending && plan.status === "active") {
    controls.push(drCommandForm(plan, source, "drill", "execution", access.loading));
    controls.push(drCommandForm(plan, source, "failover", "execution", access.loading));
  }
  return `<tr>
    <td data-label="计划"><strong>${escapeHtml(plan.plan_code)}</strong><small>v${number(plan.version)} · ${escapeHtml(plan.component_profile)}</small>${status(plan.status)}<small>${shortHash(plan.manifest_sha256)}</small></td>
    <td data-label="路径 / 驻留"><strong>${escapeHtml(plan.source_region_code)} → ${escapeHtml(plan.target_region_code)}</strong><small>${escapeHtml(plan.residency_group)}</small></td>
    <td data-label="目标"><strong>RPO ${number(plan.rpo_seconds / 60)} 分钟</strong><small>控制面 ${number(plan.control_rto_seconds / 60)} 分钟 · 网关 ${number(plan.gateway_rto_seconds / 60)} 分钟</small></td>
    <td data-label="停止条件"><strong>脑裂 / 完整性保护</strong><small>${escapeHtml(statusLabel(plan.stop_condition_code))}</small></td>
    <td data-label="双人控制"><div class="platform-dr-actions">${controls.join("") || '<span class="platform-restricted">只读或等待状态收敛</span>'}</div></td>
  </tr>`;
}

function drCommandForm(plan, source, runKind, voteKind, loading, pending = null) {
  const review = voteKind === "security_review";
  const failover = runKind === "failover";
  const expectedEpoch = pending?.expected_epoch ?? source?.failover_epoch ?? 0;
  const reason = pending?.reason_code ?? (failover ? "regional_outage" : "quarterly_drill");
  const label = review ? "安全复核" : failover ? "发起切换" : "发起演练";
  const reasonOptions = failover
    ? [["regional_outage", "区域故障"], ["control_recovery", "Control 恢复"], ["security_containment", "安全处置"]]
    : [["quarterly_drill", "季度演练"], ["control_recovery", "Control 恢复"]];
  return `<details class="platform-dr-command" ${loading ? "open data-busy=\"true\"" : ""}>
    <summary>${escapeHtml(label)}</summary>
    <dl>
      ${fact("Source", plan.source_region_code)}
      ${fact("Target", plan.target_region_code)}
      ${fact("Residency", plan.residency_group)}
      ${fact("Stop", "Split brain or integrity failure")}
    </dl>
    <form data-platform-dr-command-form autocomplete="off">
      <input type="hidden" name="runKind" value="${escapeAttribute(runKind)}" />
      <input type="hidden" name="voteKind" value="${escapeAttribute(voteKind)}" />
      <input type="hidden" name="regionId" value="${escapeAttribute(source?.region_id ?? "")}" />
      <input type="hidden" name="failoverPlanId" value="${escapeAttribute(plan.failover_plan_id)}" />
      <input type="hidden" name="planVersion" value="${number(plan.version)}" />
      <input type="hidden" name="expectedEpoch" value="${number(expectedEpoch)}" />
      <label><span>原因</span><select name="reasonCode" ${review ? "disabled" : ""}>${filterOptions(reason, reasonOptions)}</select></label>
      ${review ? `<input type="hidden" name="reasonCode" value="${escapeAttribute(reason)}" />` : ""}
      <label class="platform-check"><input type="checkbox" name="confirmed" required ${loading ? "disabled" : ""} /><span>确认 v${number(plan.version)} / epoch ${number(expectedEpoch)}</span></label>
      <button class="${failover ? "platform-danger" : "platform-primary"}" type="submit" ${loading ? "disabled" : ""}>${escapeHtml(label)}</button>
    </form>
  </details>`;
}

function drRunRow(item) {
  const approvalLabels = item.approvals.map((approval) => `${approval.approval_kind} · ${shortReference(approval.operator_id)}`);
  const evidence = item.restore_evidence;
  return `<tr>
    <td data-label="执行"><strong>${escapeHtml(item.run_kind)} · ${escapeHtml(item.source_region_code)} → ${escapeHtml(item.target_region_code)}</strong><small>${shortReference(item.disaster_recovery_run_id)} · rev ${number(item.revision)}</small>${status(item.status)}</td>
    <td data-label="审批"><strong>${number(item.approvals.length)} / 2</strong><small>${escapeHtml(approvalLabels.join(" · ") || "等待执行票")}</small></td>
    <td data-label="Checkpoint"><strong>${escapeHtml(item.checkpoint)}</strong><small>epoch ${number(item.expected_epoch)} → ${number(item.fencing_epoch)}</small></td>
    <td data-label="目标结果"><strong>${escapeHtml(item.safe_outcome_code ?? "pending")}</strong><small>${item.completed_at ? formatTime(item.completed_at) : "执行中或待审批"}</small></td>
    <td data-label="恢复证据">${evidence.length ? `<details class="platform-dr-evidence"><summary>${number(evidence.length)} 条已投影证据</summary><ul>${evidence.map((entry) => `<li><strong>${escapeHtml(entry.component_code)}</strong><span>${shortWorkspace(entry.workspace_id)} · ${escapeHtml(entry.integrity_status)} · RPO ${escapeHtml(entry.rpo_outcome)} · RTO ${escapeHtml(entry.rto_outcome)}</span><small>${number(entry.duration_seconds)} s · ${shortHash(entry.manifest_sha256)}</small></li>`).join("")}</ul></details>` : '<span class="platform-restricted">尚无证据</span>'}</td>
  </tr>`;
}

function drRecoveryPointRow(item) {
  return `<tr>
    <td data-label="范围"><strong>${escapeHtml(item.scope_kind)}</strong><small>${item.workspace_id ? shortWorkspace(item.workspace_id) : "platform"}</small></td>
    <td data-label="Schema"><strong>${escapeHtml(item.schema_head)}</strong></td>
    <td data-label="完整性">${status(item.integrity_status)}<small>${formatTime(item.source_created_at)}</small></td>
    <td data-label="Manifest"><strong>${shortHash(item.manifest_sha256)}</strong></td>
    <td data-label="年龄"><strong>${number(item.age_seconds)} s</strong></td>
  </tr>`;
}

function abuseRiskView(state, permissions) {
  const response = state.riskCases ?? {};
  const cases = Array.isArray(response.items) ? response.items : [];
  const support = response.projection === "support";
  const canDecide = !support && permissions.has("platform.risk.decide");
  return `
    <header class="platform-page-heading">
      <div><p>滥用与风险</p><h1>滥用与风险控制</h1></div>
      <span>${support ? "支持投影" : "安全投影"}</span>
    </header>
    <section class="platform-metrics" aria-label="风险案件概览">
      ${metric("用例数", response.total ?? cases.length, "当前筛选")}
      ${metric("Open", cases.filter((item) => item.status === "open").length, "待分诊")}
      ${metric("Restricted", cases.filter((item) => item.status === "restricted").length, "限制生效")}
      ${metric("Appeals", cases.reduce((total, item) => total + item.appeals.length, 0), "申诉记录")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>风险处理队列</p><h2>风险案件</h2></div>
        <form class="platform-risk-filter" data-platform-risk-filter-form>
          <label><span>状态</span><select name="status">${riskStatusOptions(state.riskStatus)}</select></label>
          <button type="submit" ${state.loading ? "disabled" : ""}>筛选</button>
        </form>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-risk-table">
          <thead><tr><th>案件 / 组织</th><th>状态 / 风险</th><th>信号</th><th>限制 / 申诉</th><th>决策</th></tr></thead>
          <tbody>${cases.map((item) => riskCaseRow(item, { support, canDecide, loading: state.loading })).join("") || emptyRow("暂无风险案件", 5)}</tbody>
        </table>
      </div>
      <div class="platform-pagination platform-risk-pagination">
        <button type="button" data-platform-action="risk-previous" ${state.riskCursorHistory?.length ? "" : "disabled"}>上一页</button>
        <span>${number(cases.length)} 条</span>
        <button type="button" data-platform-action="risk-next" ${response.next_cursor ? "" : "disabled"}>下一页</button>
      </div>
    </section>
  `;
}

function riskCaseRow(item, { support, canDecide, loading }) {
  const score = support ? "已隐藏" : statusLabel(item.score_band ?? "unknown");
  const signals = support
    ? '<span class="platform-restricted">支持投影不显示</span>'
    : item.signal_families.map((value) => `<span>${escapeHtml(statusLabel(value))}</span>`).join("")
      || '<span class="platform-restricted">无</span>';
  return `
    <tr>
      <td data-label="案件 / 组织"><strong>${escapeHtml(shortReference(item.risk_case_id))}</strong><span>${escapeHtml(shortWorkspace(item.workspace_id))} · rev ${number(item.revision)}</span><small>${escapeHtml(formatTime(item.updated_at))}</small></td>
      <td data-label="状态 / 风险">${status(item.status)}<span>${escapeHtml(score)}</span><small>${escapeHtml(statusLabel(item.safe_reason_family))}</small></td>
      <td data-label="信号"><div class="platform-tag-list platform-risk-signals">${signals}</div></td>
      <td data-label="限制 / 申诉">${riskRestriction(item.restriction)}${riskAppealTimeline(item.appeals)}</td>
      <td data-label="决策">${canDecide ? riskDecisionControls(item, loading) : '<span class="platform-restricted">只读</span>'}</td>
    </tr>
  `;
}

function riskRestriction(restriction) {
  if (!restriction) return '<div class="platform-risk-restriction"><strong>无限制</strong></div>';
  return `<dl class="platform-risk-restriction">
    <div><dt>类型</dt><dd>${escapeHtml(statusLabel(restriction.restriction_kind))}</dd></div>
    <div><dt>状态</dt><dd>${status(restriction.status)}</dd></div>
    <div><dt>截止</dt><dd>${escapeHtml(formatTime(restriction.expires_at))}</dd></div>
  </dl>`;
}

function riskAppealTimeline(appeals) {
  if (!appeals.length) return '<p class="platform-restricted">无申诉</p>';
  return `<ol class="platform-risk-appeals" aria-label="申诉时间线">${appeals.map((appeal) => `
    <li><div><strong>${escapeHtml(statusLabel(appeal.category))}</strong>${status(appeal.status)}</div><small>${escapeHtml(formatTime(appeal.created_at))}${appeal.resolution_reason ? ` · ${escapeHtml(statusLabel(appeal.resolution_reason))}` : ""}</small></li>
  `).join("")}</ol>`;
}

function riskDecisionControls(item, loading) {
  if (new Set(["cleared", "closed"]).has(item.status)) {
    return '<span class="platform-restricted">已终结</span>';
  }
  const hidden = `<input type="hidden" name="caseId" value="${escapeAttribute(item.risk_case_id)}" /><input type="hidden" name="revision" value="${number(item.revision)}" />`;
  const review = item.status === "open" ? `
    <form data-platform-risk-decision-form>${hidden}<input type="hidden" name="decision" value="required_review" /><input type="hidden" name="reasonCode" value="manual_review" /><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认复核</span></label><button type="submit" ${loading ? "disabled" : ""}>进入复核</button></form>` : "";
  const restrict = item.status !== "restricted" ? `
    <form data-platform-risk-decision-form>${hidden}<input type="hidden" name="decision" value="restricted" /><input type="hidden" name="reasonCode" value="automation_confirmed" /><label><span>限制</span><select name="restrictionKind"><option value="api_calls">API 调用</option><option value="resource_creation">资源创建</option><option value="workspace_access">组织 访问</option></select></label><label><span>分钟</span><input name="restrictionMinutes" type="number" min="5" max="10080" value="30" required /></label><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认限制</span></label><button class="platform-danger" type="submit" ${loading ? "disabled" : ""}>限制</button></form>` : "";
  const clear = `
    <form data-platform-risk-decision-form>${hidden}<input type="hidden" name="decision" value="cleared" /><label><span>清除原因</span><select name="reasonCode"><option value="false_positive">误报</option><option value="appeal_approved">申诉通过</option></select></label><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认清除</span></label><button type="submit" ${loading ? "disabled" : ""}>清除</button></form>`;
  return `<div class="platform-risk-actions">${review}${restrict}${clear}</div>`;
}

function jobRuntimeView(state, permissions) {
  const response = state.jobQueues ?? {};
  const queues = Array.isArray(response.queues) ? response.queues : [];
  const jobs = Array.isArray(response.recent_jobs) ? response.recent_jobs : [];
  const types = Array.isArray(response.job_types) ? response.job_types : [];
  const canManage = permissions.has("platform.job.manage");
  const canManageTypes = permissions.has("platform.job.type_manage");
  return `
    <header class="platform-page-heading">
      <div><p>任务调度与执行</p><h1>任务运行时</h1></div>
      <span>${number(jobs.length)} 个任务</span>
    </header>
    <section class="platform-band platform-job-queues" aria-label="队列状态">
      <div class="platform-job-queue-grid">
        ${queues.map(jobQueueSummary).join("") || '<p class="platform-empty">暂无队列状态</p>'}
      </div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>后台任务</p><h2>任务</h2></div>
        <form class="platform-job-filter" data-platform-job-filter-form>
          <label><span>状态</span><select name="status">${jobStatusOptions(state.jobStatus)}</select></label>
          <button type="submit">筛选</button>
        </form>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-job-table">
          <thead><tr><th>任务</th><th>队列 / 优先级</th><th>状态</th><th>尝试</th><th>调度</th><th>操作</th></tr></thead>
          <tbody>${jobs.map(jobRow).join("") || emptyRow("暂无任务", 6)}</tbody>
        </table>
      </div>
    </section>
    ${state.selectedJob ? jobDetailView(state.selectedJob, canManage, state.loading) : ""}
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>任务目录</p><h2>任务类型</h2></div>
        <span>${number(types.length)} 个类型</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-job-type-table">
          <thead><tr><th>类型</th><th>队列</th><th>资源</th><th>执行门限</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>${types.map((item) => jobTypeRow(item, canManageTypes, state.loading)).join("") || emptyRow("暂无任务类型", 6)}</tbody>
        </table>
      </div>
    </section>`;
}

function jobQueueSummary(queue) {
  return `<div>
    <span>${escapeHtml(queueLabel(queue.queue_code))}</span>
    <strong>${number(queue.queued_count)}</strong>
    <small>运行 ${number(queue.active_lease_count)} · 死信 ${number(queue.dead_letter_count)}</small>
    ${queue.external_wait_count ? `<small>外部执行 ${number(queue.external_wait_count)}</small>` : ""}
    <small>最久 ${queue.oldest_wait_seconds === null ? "-" : `${number(queue.oldest_wait_seconds)}s`}</small>
  </div>`;
}

function jobRow(job) {
  return `<tr>
    <td data-label="任务"><strong>${escapeHtml(job.type_code)}</strong><small>${escapeHtml(job.job_id)}</small></td>
    <td data-label="队列 / 优先级"><strong>${escapeHtml(queueLabel(job.queue_code))}</strong><small>P${number(job.priority)}</small></td>
    <td data-label="状态">${status(job.status)}<small>${escapeHtml(job.safe_outcome_code ?? "-")}</small></td>
    <td data-label="尝试"><strong>${number(job.attempt_count)} / ${number(job.max_attempts)}</strong><small>generation ${number(job.generation)}</small></td>
    <td data-label="调度"><strong>${formatTime(job.scheduled_at)}</strong><small>rev ${number(job.revision)}</small></td>
    <td data-label="操作"><button type="button" data-platform-action="open-job" data-job-id="${escapeAttribute(job.job_id)}">查看</button></td>
  </tr>`;
}

function jobDetailView(detail, canManage, loading) {
  const job = detail.job;
  const attempts = Array.isArray(detail.attempts) ? detail.attempts : [];
  const checkpoints = Array.isArray(detail.checkpoints) ? detail.checkpoints : [];
  const retryable = !detail.external_wait && new Set(["failed", "dead_lettered"]).has(job.status);
  const cancellable = new Set(["queued", "leased", "running"]).has(job.status);
  return `<section class="platform-band platform-job-detail">
    <div class="platform-section-heading">
      <div><p>${escapeHtml(job.job_id)}</p><h2>${escapeHtml(job.type_code)}</h2></div>
      <button class="platform-icon-button" type="button" data-platform-action="close-job" title="关闭" aria-label="关闭">×</button>
    </div>
    <div class="platform-job-detail-grid">
      ${detail.external_wait ? jobExternalWait(detail.external_wait, job.status) : ""}
      <div>
        <h3>Attempt 时间线</h3>
        <ol class="platform-job-timeline">${attempts.map(jobAttempt).join("") || "<li>暂无 Attempt</li>"}</ol>
      </div>
      <div>
        <h3>Checkpoint</h3>
        <ol class="platform-job-checkpoints">${checkpoints.map(jobCheckpoint).join("") || "<li>暂无 Checkpoint</li>"}</ol>
      </div>
    </div>
    ${canManage && (retryable || cancellable) ? `<form class="platform-job-command" data-platform-job-command-form>
      <input type="hidden" name="jobId" value="${escapeAttribute(job.job_id)}" />
      <input type="hidden" name="revision" value="${number(job.revision)}" />
      <input type="hidden" name="action" value="${retryable ? "retry" : "cancel"}" />
      <label><span>原因</span><select name="reasonCode">${retryable
        ? '<option value="operator_retry">操作员重试</option><option value="incident_recovery">事件恢复</option>'
        : '<option value="operator_cancelled">操作员取消</option><option value="security_containment">安全处置</option>'}</select></label>
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认操作</span></label>
      <button class="${retryable ? "platform-primary" : "platform-danger"}" type="submit" ${loading ? "disabled" : ""}>${retryable ? "重试" : "取消任务"}</button>
    </form>` : ""}
  </section>`;
}

function jobExternalWait(wait, jobStatus) {
  const label = wait.status === "completed" ? "执行已结束"
    : jobStatus === "cancel_requested" ? "等待停止确认"
    : wait.failure_count && wait.safe_outcome_code ? "连接中断，等待重试"
    : wait.status === "leased" ? "正在查询执行结果" : "等待执行结果";
  return `<div data-platform-external-wait>
    <h3>外部执行</h3><p role="status">${label}</p>
    <dl>
      <dt>结果查询</dt><dd>${number(wait.observation_count)} 次</dd>
      <dt>恢复次数</dt><dd>${number(wait.failure_count)}</dd>
      <dt>最近更新</dt><dd>${formatTime(wait.updated_at)}</dd>
      ${wait.status !== "completed" ? `<dt>下次查询</dt><dd>${formatTime(wait.next_poll_at)}</dd>` : ""}
    </dl>
  </div>`;
}

function jobAttempt(attempt) {
  return `<li><div>${status(attempt.status)}<strong>#${number(attempt.attempt_number)}</strong></div>
    <span>${formatTime(attempt.leased_at)}</span>
    <small>${escapeHtml(attempt.safe_outcome_code ?? "-")} · generation ${number(attempt.generation)}</small></li>`;
}

function jobCheckpoint(checkpoint) {
  return `<li><div><strong>#${number(checkpoint.sequence_number)}</strong><span>generation ${number(checkpoint.generation)}</span></div>
    <small>${escapeHtml(checkpoint.checkpoint_sha256.slice(0, 12))} · ${formatTime(checkpoint.created_at)}</small></li>`;
}

function jobTypeRow(item, canManage, loading) {
  const action = item.status === "active" ? "disable" : "activate";
  return `<tr>
    <td data-label="类型"><strong>${escapeHtml(item.type_code)}</strong><small>contract v${number(item.contract_version)}</small></td>
    <td data-label="队列">${escapeHtml(queueLabel(item.queue_code))}</td>
    <td data-label="资源">${escapeHtml(item.resource_class)}</td>
    <td data-label="执行门限"><strong>${number(item.max_attempts)} 次</strong><small>${number(item.timeout_seconds)}s</small></td>
    <td data-label="状态">${status(item.status)}<small>rev ${number(item.revision)}</small></td>
    <td data-label="操作">${canManage ? `<form class="platform-job-type-command" data-platform-job-type-form>
      <input type="hidden" name="typeCode" value="${escapeAttribute(item.type_code)}" />
      <input type="hidden" name="revision" value="${number(item.revision)}" />
      <input type="hidden" name="action" value="${action}" />
      <input type="hidden" name="reasonCode" value="${action === "activate" ? "release_activation" : "operational_pause"}" />
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认</span></label>
      <button type="submit" ${loading ? "disabled" : ""}>${action === "activate" ? "启用" : "停用"}</button>
    </form>` : "-"}</td>
  </tr>`;
}

function jobStatusOptions(selected) {
  return [
    ["", "全部"], ["queued", "排队"], ["leased", "已领取"], ["running", "运行中"],
    ["cancel_requested", "取消中"], ["succeeded", "成功"], ["failed", "失败"],
    ["cancelled", "已取消"], ["dead_lettered", "死信"],
  ].map(([value, label]) => `<option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>`).join("");
}

function featureRolloutsView(state, permissions) {
  const response = state.featureFlags ?? {};
  const flags = Array.isArray(response.flags) ? response.flags : [];
  const canWrite = permissions.has("platform.feature.write");
  const canApprove = permissions.has("platform.feature.approve");
  const canKill = permissions.has("platform.feature.kill");
  return `
    <header class="platform-page-heading">
      <div><p>服务端配置发布</p><h1>功能开关与渐进发布</h1></div>
      <span>${number(response.total ?? 0)} 个 Flag</span>
    </header>
    <section class="platform-metrics" aria-label="发布概览">
      ${metric("活跃功能", flags.filter((item) => item.status === "active").length, "可发布")}
      ${metric("P0", flags.filter((item) => item.risk_level === "p0").length, "双人审批")}
      ${metric("Paused", flags.filter((item) => item.latest_rollout?.status === "paused").length, "健康门禁")}
      ${metric("Exposures", flags.reduce((sum, item) => sum + item.exposure_count, 0), "不可变事实")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>版本管理</p><h2>发布目录</h2></div>
        <form class="platform-feature-filter" data-platform-feature-filter-form>
          <label><span>状态</span><select name="status">${featureStatusOptions(state.featureStatus)}</select></label>
          <label><span>风险</span><select name="riskLevel">${featureRiskOptions(state.featureRiskLevel)}</select></label>
          <button type="submit">筛选</button>
        </form>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-feature-table">
          <thead><tr><th>Flag / Head</th><th>版本与规则</th><th>发布阶梯</th><th>审批 / 健康</th><th>曝光</th><th>操作</th></tr></thead>
          <tbody>${flags.map((item) => featureFlagRow(
    item,
    { canWrite, canApprove, canKill, loading: state.loading },
  )).join("") || emptyRow("暂无功能开关", 6)}</tbody>
        </table>
      </div>
    </section>
    ${canWrite ? featureCreateForm() : ""}`;
}

function configDistributionView(state, permissions) {
  const response = state.configDistribution ?? {};
  const items = Array.isArray(response.items) ? response.items : [];
  const canRebuild = permissions.has("platform.config_distribution.rebuild");
  const gatewayCount = items.reduce((sum, item) => sum + item.gateway_count, 0);
  const staleCount = items.reduce((sum, item) => sum + item.stale_gateway_count, 0);
  const laggingCount = items.filter(
    (item) => item.minimum_applied_generation < item.generation,
  ).length;
  return `
    <header class="platform-page-heading">
      <div><p>配置快照</p><h1>配置分发</h1></div>
      <span>${number(response.total ?? 0)} 个工作区</span>
    </header>
    <section class="platform-metrics" aria-label="配置分发概览">
      ${metric("组织", items.length, "已分配 Cell")}
      ${metric("Gateways", gatewayCount, "已观测实例")}
      ${metric("ACK lag", laggingCount, "落后工作区")}
      ${metric("过期节点", staleCount, "需要重新同步")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>配置同步进度</p><h2>工作区水位</h2></div>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-config-table">
          <thead><tr><th>工作区 / 单元</th><th>当前版本</th><th>确认水位</th><th>最低可用 / 过期</th><th>最近重建</th><th>操作</th></tr></thead>
          <tbody>${items.map((item) => configDistributionRow(
    item,
    { canRebuild, loading: state.loading },
  )).join("") || emptyRow("暂无配置分发水位", 6)}</tbody>
        </table>
      </div>
    </section>`;
}

function configDistributionRow(item, access) {
  const rebuild = item.latest_rebuild;
  const ackLag = Math.max(0, item.generation - item.minimum_applied_generation);
  const stale = item.stale_gateway_count > 0;
  return `<tr>
    <td data-label="工作区 / 单元"><strong>${escapeHtml(item.workspace_id)}</strong><small>${escapeHtml(item.cell_code)}</small><small>${number(item.gateway_count)} 个网关</small></td>
    <td data-label="当前版本"><strong>版本 ${number(item.generation)}</strong><small>撤销水位 ${number(item.revocation_watermark)}</small></td>
    <td data-label="确认水位"><strong>${number(item.minimum_applied_generation)}</strong><small>落后 ${number(ackLag)} · ${formatTime(item.latest_ack_at)}</small></td>
    <td data-label="最低可用 / 过期"><strong>最低可用 ${number(item.minimum_lkg_generation)}</strong>${status(stale ? "过期" : "正常")}<small>${number(item.stale_gateway_count)} 个网关过期</small></td>
    <td data-label="最近重建">${rebuild ? `<strong>版本 ${number(rebuild.target_generation)}</strong>${status(rebuild.status)}<small>修订 ${number(rebuild.revision)} · ${formatTime(rebuild.created_at)}</small><small>结果 ${rebuild.result_manifest_sha256 ? escapeHtml(rebuild.result_manifest_sha256.slice(0, 12)) : "-"}</small>` : '<span class="platform-restricted">尚无重建</span>'}</td>
    <td data-label="操作">${access.canRebuild ? `<form class="platform-config-rebuild" data-platform-config-rebuild-form>
      <input type="hidden" name="workspaceId" value="${escapeAttribute(item.workspace_id)}" />
      <label><span>原因</span><select name="reasonCode"><option value="operator_rebuild">运维重建</option><option value="consistency_repair">一致性修复</option></select></label>
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认</span></label>
      <button type="submit" ${access.loading ? "disabled" : ""}>重建</button>
    </form>` : '<span class="platform-restricted">只读</span>'}</td>
  </tr>`;
}

function observabilityView(state, permissions) {
  const serviceResponse = state.observabilityServices ?? {};
  const sloResponse = state.serviceLevelObjectives ?? {};
  const budgetResponse = state.errorBudgets ?? {};
  const services = Array.isArray(serviceResponse.items) ? serviceResponse.items : [];
  const slos = Array.isArray(sloResponse.items) ? sloResponse.items : [];
  const budgets = Array.isArray(budgetResponse.items) ? budgetResponse.items : [];
  const alerts = Array.isArray(budgetResponse.alerts) ? budgetResponse.alerts : [];
  const canManage = permissions.has("platform.observability.manage");
  const canAck = permissions.has("platform.observability.alert_ack");
  const projection = serviceResponse.projection === "security" ? "安全投影" : "运维投影";
  return `
    <header class="platform-page-heading">
      <div><p>服务运行</p><h1>服务可观测性</h1></div>
      <span>${escapeHtml(projection)}</span>
    </header>
    <section class="platform-metrics" aria-label="可观测性概览">
      ${metric("服务数", services.length, "服务目录")}
      ${metric("健康服务", services.filter((item) => item.health_status === "healthy").length, "当前正常")}
      ${metric("服务目标", slos.length, "已配置")}
      ${metric("待处理告警", alerts.filter((item) => item.state !== "resolved").length, "需要关注")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>运行指标</p><h2>服务目录</h2></div>
        <span>${number(services.length)} 项服务</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-observability-table">
          <thead><tr><th>服务</th><th>健康</th><th>流量</th><th>错误率</th><th>P95 延迟</th></tr></thead>
          <tbody>${services.map(observableServiceRow).join("") || emptyRow("暂无服务遥测", 5)}</tbody>
        </table>
      </div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>服务目标</p><h2>SLO</h2></div>
        <span>${number(sloResponse.total ?? 0)} 个定义</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-slo-table">
          <thead><tr><th>SLO / 版本</th><th>SLI</th><th>窗口 / 目标</th><th>燃烧门限</th><th>操作</th></tr></thead>
          <tbody>${slos.map((item) => sloRow(item, canManage, state.loading)).join("") || emptyRow("暂无 SLO", 5)}</tbody>
        </table>
      </div>
    </section>
    ${canManage ? sloEditor(null, services, state.loading) : ""}
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>可用额度</p><h2>错误预算</h2></div>
        <span>${number(budgets.length)} 个窗口</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-budget-table">
          <thead><tr><th>SLO / 窗口</th><th>请求</th><th>允许 / 已消耗</th><th>剩余</th><th>燃烧率</th></tr></thead>
          <tbody>${budgets.map(errorBudgetRow).join("") || emptyRow("等待聚合作业", 5)}</tbody>
        </table>
      </div>
    </section>
    <section class="platform-band platform-alert-timeline">
      <div class="platform-section-heading">
        <div><p>告警记录</p><h2>告警时间线</h2></div>
        <span>${number(alerts.length)} 条</span>
      </div>
      <ol>${alerts.map((item) => alertTimelineItem(item, canAck, state.loading)).join("")
        || "<li class=\"platform-empty\">暂无告警</li>"}</ol>
    </section>`;
}

function observableServiceRow(item) {
  return `<tr>
    <td data-label="服务"><strong>${escapeHtml(item.display_name)}</strong><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(item.service_code)}</code><code>Tier ${number(item.service_tier)}</code><code>${escapeHtml(item.golden_signals.join(" · "))}</code></details></td>
    <td data-label="健康">${status(item.health_status)}</td>
    <td data-label="流量"><strong>${number(item.total_count)}</strong><small>当前分钟聚合</small></td>
    <td data-label="错误率"><strong>${bps(item.error_rate_bps)}</strong><small>${number(item.error_rate_bps)} bps</small></td>
    <td data-label="P95 延迟"><strong>${number(item.latency_p95_ms)} ms</strong></td>
  </tr>`;
}

function sloRow(item, canManage, loading) {
  return `<tr>
    <td data-label="SLO / 版本"><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(item.slo_code)}</small><small>v${number(item.version_number)} · ${item.is_current ? "HEAD" : "历史"}</small></td>
    <td data-label="SLI"><strong>${escapeHtml(item.sli_kind)}</strong><small>${escapeHtml(item.operation_code)} · ${escapeHtml(item.region_code)}</small><small>${escapeHtml(item.signal_class)}</small></td>
    <td data-label="窗口 / 目标"><strong>${number(item.window_minutes)} min</strong><small>${bps(item.target_bps)} 目标${item.latency_threshold_ms === null ? "" : ` · ≤ ${number(item.latency_threshold_ms)} ms`}</small></td>
    <td data-label="燃烧门限"><strong>${bps(item.burn_warning_bps)}</strong><small>critical ${bps(item.burn_critical_bps)}</small></td>
    <td data-label="操作">${canManage && item.is_current
    ? sloEditor(item, [], loading)
    : '<span class="platform-restricted">只读</span>'}</td>
  </tr>`;
}

function sloEditor(item, services, loading) {
  const creating = item === null;
  const serviceOptions = creating
    ? services.map((service) => `<option value="${escapeAttribute(service.service_code)}">${escapeHtml(service.display_name)}</option>`).join("")
    : `<option value="${escapeAttribute(item.service_code)}">${escapeHtml(item.service_code)}</option>`;
  const content = `<form data-platform-slo-form>
    <input type="hidden" name="previousVersion" value="${creating ? 0 : Number(item.version_number)}" />
    <label><span>服务</span><select name="serviceCode" required>${serviceOptions}</select></label>
    <label><span>SLO code</span><input name="sloCode" value="${escapeAttribute(item?.slo_code ?? "")}" required /></label>
    <label><span>名称</span><input name="displayName" value="${escapeAttribute(item?.display_name ?? "")}" maxlength="120" required /></label>
    <label><span>信号分类</span><select name="signalClass"><option value="reliability" ${item?.signal_class === "security" ? "" : "selected"}>可靠性</option><option value="security" ${item?.signal_class === "security" ? "selected" : ""}>安全</option></select></label>
    <label><span>SLI</span><select name="sliKind"><option value="availability" ${item?.sli_kind === "latency" ? "" : "selected"}>可用性</option><option value="latency" ${item?.sli_kind === "latency" ? "selected" : ""}>延迟</option></select></label>
    <label><span>调用类型</span><input name="operationCode" value="${escapeAttribute(item?.operation_code ?? "chat.completions")}" required /></label>
    <label><span>区域</span><input name="regionCode" value="${escapeAttribute(item?.region_code ?? "global")}" required /></label>
    <label><span>窗口分钟</span><input name="windowMinutes" type="number" min="5" max="43200" value="${Number(item?.window_minutes ?? 5)}" required /></label>
    <label><span>目标 bps</span><input name="targetBps" type="number" min="1" max="10000" value="${Number(item?.target_bps ?? 9990)}" required /></label>
    <label><span>延迟门限 ms</span><select name="latencyThresholdMs">${[10, 50, 100, 250, 500, 1000, 2500, 5000].map((value) => `<option value="${value}" ${value === item?.latency_threshold_ms ? "selected" : ""}>${value}</option>`).join("")}</select></label>
    <label><span>警告阈值</span><input name="burnWarningBps" type="number" min="10000" value="${Number(item?.burn_warning_bps ?? 20000)}" required /></label>
    <label><span>严重阈值</span><input name="burnCriticalBps" type="number" min="10001" value="${Number(item?.burn_critical_bps ?? 50000)}" required /></label>
    <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认版本定义</span></label>
    <button class="platform-primary" type="submit" ${loading ? "disabled" : ""}>${creating ? "创建 SLO" : "追加版本"}</button>
  </form>`;
  if (creating) {
    return `<details class="platform-band platform-collapsed-section platform-slo-editor"><summary>创建服务目标 <span>需要监控新的服务时展开</span></summary>${content}</details>`;
  }
  return `<details class="platform-slo-version-editor"><summary>追加版本</summary>${content}</details>`;
}

function errorBudgetRow(item) {
  const max = Math.max(item.allowed_bad_count, 1);
  const value = Math.min(item.consumed_bad_count, max);
  return `<tr>
    <td data-label="SLO / 窗口"><strong>${escapeHtml(item.slo_code)} · v${number(item.slo_version)}</strong><small>${formatTime(item.window_started_at)} → ${formatTime(item.window_ended_at)}</small>${status(item.health_status)}</td>
    <td data-label="请求"><strong>${number(item.good_count)} / ${number(item.total_count)} 成功</strong></td>
    <td data-label="允许 / 已消耗"><progress max="${max}" value="${value}" aria-label="预算消耗 ${number(item.consumed_bad_count)} / ${number(item.allowed_bad_count)}"></progress><small>${number(item.consumed_bad_count)} / ${number(item.allowed_bad_count)}</small></td>
    <td data-label="剩余"><strong>${number(item.remaining_bad_count)}</strong><small>错误请求额度</small></td>
    <td data-label="燃烧率"><strong>${bps(item.burn_rate_bps)}</strong><small>computed ${formatTime(item.computed_at)}</small></td>
  </tr>`;
}

function alertTimelineItem(item, canAck, loading) {
  return `<li>
    <div><span>${formatTime(item.last_seen_at)}</span>${status(item.state)}${status(item.severity)}</div>
    <strong>${escapeHtml(item.slo_code)}</strong>
    <small>${escapeHtml(item.safe_reason_code)} · ${escapeHtml(item.service_code)} / ${escapeHtml(item.region_code)} · rev ${number(item.revision)}</small>
    <small>${item.latest_acknowledgement_at ? `ACK ${formatTime(item.latest_acknowledgement_at)}` : "尚未确认"}</small>
    ${canAck && item.state !== "resolved" ? `<form data-platform-alert-ack-form>
      <input type="hidden" name="alertId" value="${escapeAttribute(item.alert_id)}" />
      <input type="hidden" name="revision" value="${number(item.revision)}" />
      <label><span>原因</span><select name="reasonCode"><option value="investigating">调查中</option><option value="known_issue">已知问题</option><option value="mitigating">缓解中</option></select></label>
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认</span></label>
      <button type="submit" ${loading ? "disabled" : ""}>记录 ACK</button>
    </form>` : ""}
  </li>`;
}

function bps(value) {
  const numeric = Number(value);
  const percent = Number.isFinite(numeric) ? numeric / 100 : 0;
  return `${percent.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`;
}

function featureFlagRow(flag, access) {
  const rollout = flag.latest_rollout;
  const versions = Array.isArray(flag.versions) ? flag.versions : [];
  const latestVersion = versions[0] ?? {
    version_number: flag.current_version,
    rules: flag.rules,
    exposure_count: 0,
  };
  return `<tr>
    <td data-label="Flag / Head"><strong>${escapeHtml(flag.display_name)}</strong><small>${escapeHtml(flag.flag_code)}</small><small>head v${number(flag.current_version)} · rev ${number(flag.revision)}</small>${status(flag.status)}</td>
    <td data-label="版本与规则">
      <div class="platform-feature-versions">${versions.map((version) => featureVersion(flag, version)).join("")}</div>
      ${access.canWrite && flag.status === "active" ? featureVersionForm(flag, latestVersion, access.loading) : ""}
    </td>
    <td data-label="发布阶梯">${rollout ? `<strong>${number(rollout.target_percentage_bps) / 100}%</strong><small>${escapeHtml(rollout.environment_code)} · ${escapeHtml(rollout.region_code)}</small><small>${escapeHtml(rollout.action)} · ${formatTime(rollout.updated_at)}</small>${status(rollout.status)}` : '<span class="platform-restricted">尚未发布</span>'}</td>
    <td data-label="审批 / 健康">${rollout ? `<strong>${number(rollout.approval_count)} / ${flag.risk_level === "p0" ? "2" : "0"} 票</strong><small>${escapeHtml(rollout.health_status)} · watermark ${rollout.health_source_watermark === null ? "-" : number(rollout.health_source_watermark)}</small><small>rollout rev ${number(rollout.revision)}</small>` : "-"}</td>
    <td data-label="曝光"><strong>${number(flag.exposure_count)}</strong><small>${versions.map((version) => `v${number(version.version_number)} ${number(version.exposure_count)}`).join(" · ") || "-"}</small></td>
    <td data-label="操作"><div class="platform-feature-actions">${featureActions(flag, latestVersion, access)}</div></td>
  </tr>`;
}

function featureVersion(flag, version) {
  const rules = version.rules;
  const label = version.version_number === flag.current_version ? "HEAD" : "CANDIDATE";
  return `<div><strong>v${number(version.version_number)} · ${label}</strong><small>${number(rules.percentage_bps) / 100}% · ${escapeHtml(rules.environments.join(", "))}</small><small>${escapeHtml(rules.regions.join(", ") || "all regions")}${rules.cohort_code ? ` · ${escapeHtml(rules.cohort_code)}` : ""}</small></div>`;
}

function featureActions(flag, latestVersion, access) {
  if (flag.status !== "active") return '<span class="platform-restricted">已关闭</span>';
  const rollout = flag.latest_rollout;
  const active = new Set(["pending_approval", "scheduled", "running"]).has(rollout?.status);
  const forms = [];
  if (access.canWrite && !active) {
    forms.push(featureStartForm(flag, latestVersion, access.loading));
  }
  if (access.canApprove && rollout?.status === "pending_approval") {
    forms.push(featureRolloutCommandForm(flag, rollout, "approve", "审批", access.loading));
  }
  if (access.canWrite && new Set(["scheduled", "running"]).has(rollout?.status)) {
    forms.push(featureRolloutCommandForm(flag, rollout, "pause", "暂停", access.loading));
  }
  if (access.canWrite && rollout?.status === "paused") {
    forms.push(featureRolloutCommandForm(flag, rollout, "resume", "恢复", access.loading));
  }
  if (access.canWrite && rollout?.status === "completed" && flag.current_version > 1) {
    forms.push(featureRolloutCommandForm(flag, rollout, "rollback", "回滚", access.loading));
  }
  if (access.canKill) forms.push(featureKillForm(flag, access.loading));
  return forms.join("") || '<span class="platform-restricted">只读</span>';
}

function featureStartForm(flag, version, loading) {
  const rules = version.rules;
  return `<form data-platform-feature-command-form>
    ${featureCommandFields(flag, null, "start", flag.revision)}
    <input type="hidden" name="versionNumber" value="${number(version.version_number)}" />
      <label><span>阶梯 bps</span><input name="percentageBps" type="number" min="0" max="${escapeAttribute(String(rules.percentage_bps))}" step="100" value="${escapeAttribute(String(rules.percentage_bps))}" required /></label>
    <label><span>环境</span><select name="environmentCode">${rules.environments.map((item) => `<option value="${escapeAttribute(item)}">${escapeHtml(item)}</option>`).join("")}</select></label>
    <label><span>区域</span><input name="regionCode" value="${escapeAttribute(rules.regions[0] ?? "global")}" required /></label>
    ${featureConfirm("开始", loading, "platform-primary")}
  </form>`;
}

function featureRolloutCommandForm(flag, rollout, action, label, loading) {
  return `<form data-platform-feature-command-form>
    ${featureCommandFields(flag, rollout.rollout_id, action, rollout.revision)}
    ${featureConfirm(label, loading, action === "rollback" ? "platform-danger" : "")}
  </form>`;
}

function featureCommandFields(flag, rolloutId, action, revision) {
  return `<input type="hidden" name="flagId" value="${escapeAttribute(flag.flag_id)}" />
    <input type="hidden" name="rolloutId" value="${escapeAttribute(rolloutId ?? "")}" />
    <input type="hidden" name="action" value="${escapeAttribute(action)}" />
    <input type="hidden" name="revision" value="${number(revision)}" />`;
}

function featureConfirm(label, loading, className = "") {
  return `<label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认</span></label><button class="${className}" type="submit" ${loading ? "disabled" : ""}>${escapeHtml(label)}</button>`;
}

function featureKillForm(flag, loading) {
  return `<form data-platform-feature-kill-form>
    <input type="hidden" name="flagId" value="${escapeAttribute(flag.flag_id)}" />
    <input type="hidden" name="revision" value="${number(flag.revision)}" />
    <label><span>Kill 原因</span><select name="reasonCode"><option value="incident_containment">事件处置</option><option value="security_containment">安全处置</option><option value="release_rollback">发布回滚</option></select></label>
    ${featureConfirm("Kill", loading, "platform-danger")}
  </form>`;
}

function featureVersionForm(flag, version, loading) {
  const rules = version.rules;
  return `<details class="platform-feature-version-editor"><summary>追加版本</summary>
    <form data-platform-feature-create-form>
      <input type="hidden" name="action" value="version" />
      <input type="hidden" name="flagId" value="${escapeAttribute(flag.flag_id)}" />
      <input type="hidden" name="revision" value="${number(flag.revision)}" />
      <input type="hidden" name="flagCode" value="${escapeAttribute(flag.flag_code)}" />
      <input type="hidden" name="displayName" value="${escapeAttribute(flag.display_name)}" />
      <input type="hidden" name="riskLevel" value="${escapeAttribute(flag.risk_level)}" />
      ${featureRuleFields(rules)}
      ${featureConfirm("保存 v" + (version.version_number + 1), loading, "platform-primary")}
    </form>
  </details>`;
}

function featureCreateForm() {
  return `<section class="platform-band platform-feature-create">
    <div class="platform-section-heading"><div><p>受控配置</p><h2>创建功能开关</h2></div></div>
    <form data-platform-feature-create-form autocomplete="off">
      <input type="hidden" name="action" value="create" />
      <input type="hidden" name="flagId" value="" />
      <input type="hidden" name="revision" value="" />
      <label><span>Flag code</span><input name="flagCode" pattern="[a-z0-9][a-z0-9_.-]{2,79}" required /></label>
      <label><span>显示名称</span><input name="displayName" maxlength="120" required /></label>
      <label><span>风险等级</span><select name="riskLevel"><option value="standard">标准</option><option value="p0">最高</option></select></label>
      ${featureRuleFields({ environments: ["production"], regions: [], cohort_code: null, percentage_bps: 10000, prerequisite_flag_code: null })}
      ${featureConfirm("创建", false, "platform-primary")}
    </form>
  </section>`;
}

function featureRuleFields(rules) {
  return `<fieldset class="platform-feature-environments"><legend>环境</legend>${["development", "staging", "production"].map((environment) => `<label class="platform-check"><input type="checkbox" name="environments" value="${environment}" ${rules.environments.includes(environment) ? "checked" : ""} /><span>${environment}</span></label>`).join("")}</fieldset>
    <label><span>区域（逗号分隔）</span><input name="regions" value="${escapeAttribute(rules.regions.join(","))}" /></label>
    <label><span>最大比例 bps</span><input name="percentageBps" type="number" min="0" max="10000" step="100" value="${escapeAttribute(String(rules.percentage_bps))}" required /></label>
    <label><span>Cohort code</span><input name="cohortCode" value="${escapeAttribute(rules.cohort_code ?? "")}" /></label>
    <label><span>前置 Flag</span><input name="prerequisiteFlagCode" value="${escapeAttribute(rules.prerequisite_flag_code ?? "")}" /></label>`;
}

function featureStatusOptions(selected) {
  return [["", "全部"], ["active", "启用"], ["killed", "已停止"], ["disabled", "已停用"]]
    .map(([value, label]) => `<option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>`).join("");
}

function featureRiskOptions(selected) {
  return [["", "全部"], ["standard", "标准"], ["p0", "最高"]]
    .map(([value, label]) => `<option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>`).join("");
}

function queueLabel(value) {
  return ({ critical: "紧急", standard: "标准", bulk: "批量" })[value] ?? "未知";
}

function notificationTemplatesView(state, permissions) {
  const response = state.notificationTemplates ?? {};
  const templates = Array.isArray(response.templates) ? response.templates : [];
  const canWrite = permissions.has("platform.notification_template.write");
  const defaultEvent = "invoice.issued";
  return `
    <header class="platform-page-heading">
      <div><p>通知内容</p><h1>通知模板</h1></div>
      <span>目录修订版 ${number(response.catalog_revision ?? 0)}</span>
    </header>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>历史版本</p><h2>模板版本</h2></div>
        <span>${number(templates.length)} 个版本</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-notification-template-table">
          <thead><tr><th>模板</th><th>事件</th><th>渠道 / 语言</th><th>版本</th><th>状态</th><th>正文摘要</th></tr></thead>
          <tbody>${templates.map(notificationTemplateRow).join("") || emptyRow("暂无模板版本", 6)}</tbody>
        </table>
      </div>
    </section>
    ${canWrite ? `
      <section class="platform-band platform-notification-editor">
        <div class="platform-section-heading">
          <div><p>受控编辑</p><h2>创建新版本</h2></div>
        </div>
        <form data-platform-notification-template-form autocomplete="off">
          <label><span>模板代码</span><input name="templateCode" pattern="[a-z0-9][a-z0-9_.-]{2,79}" maxlength="80" value="invoice_issued" required /></label>
          <label><span>事件</span><select name="eventType">${notificationEventOptions(defaultEvent)}</select></label>
          <label><span>渠道</span><select name="channel"><option value="in_app">站内</option><option value="email" selected>邮件</option><option value="webhook">Webhook</option></select></label>
          <label><span>语言</span><select name="locale"><option value="en">英语</option><option value="zh-CN">简体中文</option></select></label>
          <label class="platform-notification-parameters"><span>允许参数</span><output data-template-parameters>${escapeHtml(NOTIFICATION_EVENT_PARAMETERS[defaultEvent].join(", "))}</output></label>
          <label class="platform-notification-subject"><span>主题</span><input name="subjectTemplate" maxlength="200" value="Invoice {{invoice_number_masked}} issued" required /></label>
          <label class="platform-notification-body"><span>正文</span><textarea name="bodyTemplate" maxlength="4000" rows="6" required>Invoice {{invoice_number_masked}} for {{total}} {{currency}} was issued at {{issued_at}}.</textarea></label>
          <label><span>版本状态</span><select name="status"><option value="draft">草稿</option><option value="published">发布</option></select></label>
          <button class="platform-primary" type="submit" ${state.loading ? "disabled" : ""}>创建版本</button>
        </form>
      </section>
    ` : ""}
  `;
}

function notificationTemplateRow(template) {
  return `<tr>
    <td data-label="模板"><strong>${escapeHtml(notificationEventLabel(template.event_type))}</strong></td>
    <td data-label="事件">${escapeHtml(notificationEventLabel(template.event_type))}</td>
    <td data-label="渠道 / 语言"><strong>${escapeHtml(notificationChannelLabel(template.channel))}</strong><small>${escapeHtml(notificationLocaleLabel(template.locale))}</small></td>
    <td data-label="版本"><strong>v${number(template.version)}</strong></td>
    <td data-label="状态">${status(template.status)}<small>${formatTime(template.published_at ?? template.created_at)}</small></td>
    <td data-label="正文摘要"><strong>${escapeHtml(template.subject_template)}</strong></td>
  </tr>`;
}

function notificationEventOptions(selected) {
  return Object.keys(NOTIFICATION_EVENT_PARAMETERS)
    .map((eventType) => `<option value="${escapeAttribute(eventType)}" ${eventType === selected ? "selected" : ""}>${escapeHtml(notificationEventLabel(eventType))}</option>`)
    .join("");
}

function notificationEventLabel(value) {
  return ({
    "security.sign_in_risk": "登录风险",
    "subscription.status_changed": "订阅状态变更",
    "verification.status_changed": "认证状态变更",
    "payment.status_changed": "支付状态变更",
    "invoice.issued": "发票开具",
    "invoice.reversed": "发票冲红",
    "infrastructure.backup_completed": "备份完成",
    "gateway.health_changed": "网关健康变更",
  })[value] ?? "未知事件";
}

function notificationChannelLabel(value) {
  return ({ in_app: "站内", email: "邮件", webhook: "Webhook" })[value] ?? String(value ?? "未知渠道");
}

function notificationLocaleLabel(value) {
  return ({ "zh-CN": "简体中文", en: "英语" })[value] ?? String(value ?? "未知语言");
}

/** 渲染企业认证审核队列，案件投影不显示 组织、主体明文或对象引用。 */
function verificationView(state, permissions) {
  const cases = state.verification?.cases ?? [];
  const canReview = permissions.has("platform.verification.review");
  return `
    <header class="platform-page-heading"><div><p>企业身份</p><h1>企业认证审核</h1></div><span>${number(state.verification?.total ?? 0)} 个案件</span></header>
    <section class="platform-band">
      <form class="platform-billing-filter" data-platform-verification-filter-form>
        <label><span>案件状态</span><select name="status"><option value="" ${state.verificationStatus ? "" : "selected"}>全部</option>${verificationStatusOptions(state.verificationStatus)}</select></label>
        <button type="submit">筛选</button>
      </form>
      <div class="platform-table-wrap"><table class="platform-verification-table">
        <thead><tr><th>案件</th><th>主体</th><th>状态</th><th>扫描</th><th>证据</th><th>操作</th></tr></thead>
        <tbody>${cases.map((item) => verificationRow(item, canReview)).join("") || emptyRow("暂无认证案件", 6)}</tbody>
      </table></div>
      <div class="platform-pagination"><span>第 ${number(Math.floor((state.verificationOffset ?? 0) / 20) + 1)} 页</span><div><button type="button" data-platform-action="verification-previous" ${(state.verificationOffset ?? 0) > 0 ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="verification-next" ${(state.verificationOffset ?? 0) + cases.length < (state.verification?.total ?? 0) ? "" : "disabled"}>下一页</button></div></div>
    </section>`;
}

/** 渲染单个安全案件；证据 ID 和审核控件仅对动态 reviewer 权限可见。 */
function verificationRow(item, canReview) {
  const active = new Set(["submitted", "in_review"]).has(item.status);
  const evidence = item.evidence_items ?? [];
  return `<tr>
    <td data-label="案件"><strong>${escapeHtml(item.business_verification_intent_id)}</strong><small>资料修订 ${number(item.profile_revision)} · 案件修订 ${number(item.revision)}</small></td>
    <td data-label="主体"><strong>${escapeHtml(item.country_code)}</strong><small>${escapeHtml(entityTypeLabel(item.entity_type))}</small></td>
    <td data-label="状态">${status(item.status)}<small>${formatTime(item.submitted_at)}</small></td>
    <td data-label="扫描"><strong>${escapeHtml(scanStateLabel(item.evidence))}</strong><small>${number(item.evidence?.evidence_count ?? 0)} 份</small></td>
    <td data-label="证据"><div class="platform-evidence-list">${evidence.map((entry) => `<div><span>${escapeHtml(evidenceTypeLabel(entry.evidence_type))} · ${escapeHtml(entry.mime_type)} · ${number(entry.size_bytes)} 字节</span>${canReview && active ? `<button class="platform-icon-button" type="button" data-platform-action="verification-evidence-token" data-intent-id="${escapeAttribute(item.business_verification_intent_id)}" data-evidence-id="${escapeAttribute(entry.verification_evidence_id)}" title="签发证据授权" aria-label="签发证据授权">↓</button>` : ""}</div>`).join("") || '<span class="platform-restricted">暂无有效证据</span>'}</div></td>
    <td data-label="操作">${canReview && active ? `<div class="platform-verification-actions"><button class="platform-primary" type="button" data-platform-action="verification-approve" data-intent-id="${escapeAttribute(item.business_verification_intent_id)}" data-revision="${number(item.revision)}">批准</button><form data-platform-verification-reject-form><input name="intentId" type="hidden" value="${escapeAttribute(item.business_verification_intent_id)}" /><input name="revision" type="hidden" value="${number(item.revision)}" /><select name="reasonCode"><option value="document_invalid">证件无效</option><option value="identity_mismatch">主体不一致</option><option value="scan_failed">扫描失败</option><option value="profile_changed">资料已变化</option></select><button type="submit">拒绝</button></form></div>` : '<span class="platform-restricted">只读</span>'}</td>
  </tr>`;
}

function infrastructureView(state, permissions) {
  const value = state.infrastructure;
  const resources = value?.resources ?? [];
  const backup = state.backup?.backup;
  return `
    <header class="platform-page-heading"><div><p>平台运行</p><h1>基础设施</h1></div><span>${status(value?.overall_status ?? "unavailable")}</span></header>
    <section class="platform-metrics" aria-label="恢复目标">
      ${metric("组件", resources.length, "受控生产依赖")}
      ${metric("RPO", value?.rpo_minutes ?? 0, "分钟")}
      ${metric("RTO", value?.rto_minutes ?? 0, "分钟")}
    </section>
    <section class="platform-band platform-infrastructure-status">
      <div class="platform-section-heading"><div><p>运行检查</p><h2>组件状态</h2></div><span>${number(resources.filter((item) => item.verified).length)} 个已验证</span></div>
      <div class="platform-table-wrap"><table><thead><tr><th>组件</th><th>后端</th><th>状态</th><th>延迟</th><th>最近验证</th></tr></thead><tbody>${resources.map(infrastructureResourceRow).join("") || emptyRow("暂无基础设施状态", 5)}</tbody></table></div>
    </section>
    ${(state.session?.context?.operator?.roles ?? state.session?.context?.roles ?? []).includes("platform_owner") ? '<section class="platform-band" data-system-updates aria-label="系统升级">加载系统升级状态…</section>' : ""}
    ${permissions.has("platform.backup.create") ? backupForm() : ""}
    ${backup ? backupPanel(backup) : ""}`;
}

function infrastructureResourceRow(resource) {
  return `<tr>
    <td data-label="组件"><strong>${escapeHtml(infrastructureKindLabel(resource.resource_kind))}</strong><small>${escapeHtml(resource.version_summary ?? "-")}</small></td>
    <td data-label="后端">${escapeHtml(statusLabel(resource.provider_code))}</td>
    <td data-label="状态">${status(resource.status)}${resource.error_code ? `<small>${escapeHtml(resource.error_code)}</small>` : ""}</td>
    <td data-label="延迟">${resource.latency_ms === null ? "-" : `${number(resource.latency_ms)} ms`}</td>
    <td data-label="最近验证">${formatTime(resource.last_verified_at)}</td>
  </tr>`;
}

// Gateway 视图仅展示有界 Cell 摘要，并按实时权限裁剪 placement 写表单。
function gatewayView(state, permissions) {
  const cells = state.gatewayCells?.cells ?? [];
  const active = cells.filter((cell) => cell.status === "active" && cell.health_status === "healthy");
  const placement = state.gatewayPlacement?.placement;
  return `
    <header class="platform-page-heading"><div><p>网关运行</p><h1>网关集群</h1></div><span>${number(active.length)} 个可用节点</span></header>
    <section class="platform-metrics" aria-label="网关集群概览">
      ${metric("节点", cells.length, "区域节点")}
      ${metric("可用", active.length, "健康且接流")}
      ${metric("配置水位", Math.max(0, ...cells.map((cell) => cell.config_watermark)), "最新 generation")}
    </section>
    <section class="platform-band platform-gateway-cells">
      <div class="platform-section-heading"><div><p>运行节点</p><h2>节点状态</h2></div><span>${number(cells.length)} 个节点</span></div>
      <div class="platform-table-wrap"><table class="platform-gateway-table"><thead><tr><th>节点</th><th>区域</th><th>容量</th><th>服务状态</th><th>健康</th><th>版本</th><th>配置</th></tr></thead><tbody>${cells.map(gatewayCellRow).join("") || emptyRow("暂无 Gateway 节点", 7)}</tbody></table></div>
    </section>
    ${permissions.has("platform.gateway.manage") ? gatewayPlacementForm(active) : ""}
    ${placement ? gatewayPlacementResult(placement) : ""}`;
}

function edgeLicensesView(state, permissions) {
  const licenses = state.edgeLicenses?.licenses ?? [];
  const canIssue = permissions.has("platform.edge_license.issue");
  const canRevoke = permissions.has("platform.edge_license.revoke");
  const disabled = state.loading ? " disabled" : "";
  return `
    <header class="platform-page-heading">
      <div><p>网关授权</p><h1>网关许可证</h1></div>
      <button type="button" class="platform-icon-button" data-platform-action="edge-licenses-refresh" title="刷新 License" aria-label="刷新 License"${disabled}>↻</button>
    </header>
    ${canIssue ? `<section class="platform-band platform-edge-license-issue">
      <div class="platform-section-heading"><div><p>授权管理</p><h2>签发与扩容</h2></div><span>${state.edgeLicenseIntent ? "正在提交" : "账务"}</span></div>
      <form data-platform-edge-license-issue-form autocomplete="off">
        <label><span>Deployment ID</span><input name="deploymentId" pattern="edp_[0-9a-f]{32}" maxlength="36" required${disabled}></label>
        <label><span>节点上限</span><input name="nodeLimit" type="number" min="1" max="100000" value="3" required${disabled}></label>
        <label><span>并发请求上限</span><input name="concurrentRequestLimit" type="number" min="1" max="10000000" value="100" required${disabled}></label>
        <label><span>到期时间</span><input name="expiresAt" type="datetime-local" value="${escapeAttribute(defaultLicenseExpiry())}" required${disabled}></label>
        <label><span>宽限天数</span><input name="graceDays" type="number" min="7" max="30" value="7" required${disabled}></label>
        <label><span>当前 revision</span><input name="expectedRevision" type="number" min="1" placeholder="首次签发留空"${disabled}></label>
        <label><span>正式订单 ID</span><input name="sourceOrderId" pattern="ord_[0-9a-f]{32}" maxlength="36" placeholder="无付款要求时留空"${disabled}></label>
        <label><span>原因</span><select name="reasonCode" required${disabled}><option value="commercial_activation">商业激活</option><option value="capacity_expansion">容量扩展</option><option value="renewal">续期</option></select></label>
        <fieldset><legend>功能</legend>
          ${[["advanced_routing", "高级路由"], ["audit_export", "审计导出"], ["diagnostic_support", "诊断支持"], ["policy_enforcement", "策略执行"], ["telemetry_export", "遥测导出"]].map(([code, label]) => `<label><input type="checkbox" name="features" value="${code}"${disabled}><span>${label}</span></label>`).join("")}
        </fieldset>
        <label class="platform-edge-confirm"><input type="checkbox" name="confirmed" required${disabled}><span>确认商业依据、容量和有效期</span></label>
        <button class="platform-primary" type="submit"${disabled}>签发或扩容</button>
      </form>
    </section>` : ""}
    <section class="platform-band platform-edge-licenses">
      <div class="platform-section-heading"><div><p>安全摘要</p><h2>许可证清单</h2></div><span>${number(licenses.length)} 项</span></div>
      ${licenses.length ? `<div class="platform-table-wrap"><table class="platform-edge-license-table"><thead><tr><th>部署</th><th>状态</th><th>容量 / 用量</th><th>有效期 / 宽限</th><th>确认状态</th><th>操作</th></tr></thead><tbody>${licenses.map((license) => edgeLicenseRow(license, canRevoke, state.loading)).join("")}</tbody></table></div>` : '<div class="platform-empty">尚无 Gateway 许可证</div>'}
    </section>`;
}

function edgeBackupsView(state) {
  const backups = state.edgeBackups?.statuses ?? [];
  const current = backups.filter((item) => item.status === "current").length;
  const attention = backups.filter((item) => ["overdue", "failed"].includes(item.status)).length;
  const missed = backups.reduce((total, item) => total + item.recent_drills.filter(
    (drill) => drill.objective_outcome === "missed",
  ).length, 0);
  return `
    <header class="platform-page-heading">
      <div><p>私有网关稳定性</p><h1>网关备份</h1></div>
      <button type="button" class="platform-icon-button" data-platform-action="edge-backups-refresh" title="刷新备份状态" aria-label="刷新备份状态"${state.loading ? " disabled" : ""}>↻</button>
    </header>
    <section class="platform-metrics" aria-label="Gateway backup summary">
      ${metric("RPO current", current, `${number(backups.length)} 个 Deployment`)}
      ${metric("Attention", attention, "逾期或最近失败")}
      ${metric("Objective misses", missed, "季度隔离演练")}
    </section>
    <section class="platform-band platform-edge-backups">
      <div class="platform-section-heading"><div><p>云端安全摘要</p><h2>备份与演练摘要</h2></div><span>${number(backups.length)} 项</span></div>
      ${state.loading && !state.edgeBackups ? '<div class="platform-empty" role="status">正在读取备份状态</div>' : backups.length ? `<div class="platform-table-wrap"><table class="platform-edge-backup-table"><thead><tr><th>Deployment</th><th>RPO</th><th>最近备份</th><th>验证 / 保留</th><th>季度演练</th></tr></thead><tbody>${backups.map(platformEdgeBackupRow).join("")}</tbody></table></div>` : '<div class="platform-empty">尚无 Gateway 备份状态</div>'}
    </section>`;
}

function platformEdgeBackupRow(item) {
  const backup = item.latest_backup;
  const drill = item.recent_drills[0] ?? null;
  const tone = item.status === "current" ? "healthy"
    : item.status === "never" ? "unknown" : item.status === "overdue" ? "warning" : "critical";
  return `<tr>
    <td data-label="Deployment"><strong>${escapeHtml(shortReference(item.deployment_id))}</strong><small>generation ${number(item.latest_source_generation)} · r${number(item.revision)}</small></td>
    <td data-label="RPO"><span class="platform-status tone-${tone}">${escapeHtml(platformBackupStatusLabel(item.status))}</span><small>${platformDuration(item.rpo_objective_seconds)} · ${formatOptionalTime(item.last_success_at)}</small></td>
    <td data-label="最近备份"><strong>${backup ? escapeHtml(shortReference(backup.backup_id)) : "-"}</strong><small>${backup ? `${escapeHtml(backup.storage_kind)} · ${escapeHtml(backup.size_bucket)} · ${number(backup.chunk_count)} chunks` : "尚未上报"}</small></td>
    <td data-label="验证 / 保留"><strong>${item.last_check_outcome === "verified" ? "通过" : item.last_check_outcome === "failed" ? "失败" : "未验证"}</strong><small>${backup ? formatTime(backup.retention_until) : "-"}</small></td>
    <td data-label="季度演练"><strong>${drill ? `${escapeHtml(drill.quarter_code)} · ${escapeHtml(platformDrillStatusLabel(drill.status))}` : "尚无演练"}</strong><small>${drill ? `${drill.actual_rpo_seconds === null ? "RPO -" : `RPO ${platformDuration(drill.actual_rpo_seconds)}`} · ${drill.actual_rto_seconds === null ? "RTO -" : `RTO ${platformDuration(drill.actual_rto_seconds)}`} · ${drill.production_untouched ? "生产未触碰" : "异常"}` : "-"}</small></td>
  </tr>`;
}

function offlineBundlesView(state, permissions) {
  const items = state.offlineBundles?.items ?? [];
  const pending = items.flatMap((item) => item.state.requests
    .filter((request) => request.status === "pending_platform")
    .map((request) => ({ item, request })));
  const bundles = items.flatMap((item) => item.state.bundles
    .map((bundle) => ({ item, bundle })));
  const unresolved = items.reduce((total, item) => total + item.state.conflicts.filter(
    (conflict) => conflict.resolution === null,
  ).length, 0);
  const canManage = permissions.has("platform.edge_offline_bundle.manage");
  return `
    <header class="platform-page-heading">
      <div><p>离线环境操作</p><h1>离线交换</h1></div>
      <button type="button" class="platform-icon-button" data-platform-action="offline-bundles-refresh" title="刷新离线交换" aria-label="刷新离线交换"${state.loading ? " disabled" : ""}>↻</button>
    </header>
    <section class="platform-metrics" aria-label="Offline exchange summary">
      ${metric("待审批", pending.length, "双人复核")}
      ${metric("未解决冲突", unresolved, "需要人工处理")}
      ${metric("交换记录", bundles.length, `${number(items.length)} 个部署`)}
    </section>
    <section class="platform-band platform-offline-approvals">
      <div class="platform-section-heading"><div><p>双人控制</p><h2>审批队列</h2></div><span>${number(pending.length)} 条待处理</span></div>
      ${pending.length ? `<div class="platform-table-wrap"><table class="platform-offline-table"><thead><tr><th>组织 / 部署</th><th>用途</th><th>安全摘要</th><th>决定</th></tr></thead><tbody>${pending.map(({ item, request }) => platformOfflineApprovalRow(item, request, canManage, state.loading)).join("")}</tbody></table></div>` : '<div class="platform-empty">暂无待处理的离线交换请求</div>'}
    </section>
    <section class="platform-band platform-offline-exchanges">
      <div class="platform-section-heading"><div><p>安全摘要</p><h2>交换进度</h2></div><span>${number(bundles.length)} 条记录</span></div>
      ${bundles.length ? `<div class="platform-table-wrap"><table class="platform-offline-table"><thead><tr><th>部署</th><th>方向 / 域</th><th>传输</th><th>清单 / 有效期</th><th>冲突</th></tr></thead><tbody>${bundles.map(({ item, bundle }) => platformOfflineBundleRow(item, bundle)).join("")}</tbody></table></div>` : '<div class="platform-empty">暂无离线介质交换记录</div>'}
    </section>`;
}

function platformOfflineApprovalRow(item, request, canManage, loading) {
  const bundle = item.state.bundles.find(
    (candidate) => candidate.bundle_id === request.target_bundle_id,
  ) ?? null;
  const disabled = loading ? " disabled" : "";
  return `<tr>
    <td data-label="组织 / 部署"><strong>${escapeHtml(shortReference(item.workspace_id))}</strong><small>${escapeHtml(shortReference(request.deployment_id))}</small></td>
    <td data-label="Intent"><strong>${escapeHtml(platformOfflineOperationLabel(request.operation))}</strong><small>r${number(request.revision)} · ${formatTime(request.requested_at)}</small></td>
    <td data-label="安全摘要"><strong>${bundle ? `${escapeHtml(platformOfflineDirectionLabel(bundle.direction))} · ${bundle.domains.map(platformOfflineDomainLabel).join(" / ")}` : "处理包"}</strong><small>${bundle ? `${number(bundle.entry_count)} 项 · ${formatBytes(bundle.encrypted_size_bytes)} · ${escapeHtml(bundle.manifest_sha256.slice(0, 16))}` : escapeHtml(shortReference(request.target_conflict_id ?? "-"))}</small></td>
    <td data-label="决定">${canManage ? `<form data-platform-offline-decision-form autocomplete="off">
      <input type="hidden" name="requestId" value="${escapeAttribute(request.request_id)}">
      <input type="hidden" name="expectedRevision" value="${number(request.revision)}">
      <label><span>Decision</span><select name="action"${disabled}><option value="approve">Approve</option><option value="reject">Reject</option></select></label>
      <label><span>原因</span><select name="reasonCode"${disabled}><option value="dual_control_approved">双人复核通过</option><option value="security_review_rejected">安全复核未通过</option></select></label>
      <label class="platform-edge-confirm"><input type="checkbox" name="confirmed" required${disabled}><span>Confirm owner, direction, domains and expiry</span></label>
      <button class="platform-primary" type="submit"${disabled}>Submit</button>
    </form>` : "Read only"}</td>
  </tr>`;
}

function platformOfflineBundleRow(item, bundle) {
  const conflicts = item.state.conflicts.filter(
    (conflict) => conflict.bundle_id === bundle.bundle_id,
  );
  const unresolved = conflicts.filter((conflict) => conflict.resolution === null).length;
  return `<tr>
    <td data-label="Deployment"><strong>${escapeHtml(shortReference(bundle.deployment_id))}</strong><small>${escapeHtml(shortReference(item.workspace_id))} · g${number(bundle.producer_generation)}</small></td>
    <td data-label="Direction / domains"><strong>${escapeHtml(platformOfflineDirectionLabel(bundle.direction))}</strong><small>${bundle.domains.map(platformOfflineDomainLabel).join(" / ")}</small></td>
    <td data-label="Transfer"><strong>${escapeHtml(platformOfflineTransferLabel(bundle.transfer_status))}</strong><small>${number(bundle.next_sequence)} / ${number(bundle.chunk_count)} chunks</small></td>
    <td data-label="Manifest / expiry"><strong>${escapeHtml(bundle.manifest_sha256.slice(0, 16))}</strong><small>${formatTime(bundle.expires_at)}</small></td>
    <td data-label="Conflicts"><strong>${unresolved ? `${number(unresolved)} unresolved` : "Clear"}</strong><small>${conflicts.map((conflict) => conflict.resolution ? platformOfflineResolutionLabel(conflict.resolution) : platformOfflineDomainLabel(conflict.domain)).join(" / ") || "-"}</small></td>
  </tr>`;
}

function platformOfflineOperationLabel(value) {
  return ({ export: "Cloud → Edge export", import: "Edge → Cloud import",
    resolve: "Conflict resolution" })[value] ?? value;
}

function platformOfflineDirectionLabel(value) {
  return value === "cloud_to_edge" ? "Cloud → Edge" : "Edge → Cloud";
}

function platformOfflineDomainLabel(value) {
  return ({ desired_state: "Desired State", license: "License", release: "版本",
    status: "状态", usage: "Usage", audit: "Audit", validation: "Validation" })[
    value
  ] ?? value;
}

function platformOfflineTransferLabel(value) {
  return ({ prepared: "Prepared", transferring: "Transferring", verified: "Verified",
    applied: "Applied", conflict: "Conflict", failed: "失败" })[value] ?? value;
}

function platformOfflineResolutionLabel(value) {
  return ({ keep_cloud: "Keep Cloud", keep_local: "Keep Local",
    supersede_with_new: "Superseded" })[value] ?? value;
}

function platformBackupStatusLabel(value) {
  return ({ current: "RPO 正常", overdue: "RPO 逾期", failed: "最近失败", never: "尚无备份" })[
    value
  ] ?? "未知";
}

function platformDrillStatusLabel(value) {
  return ({ requested: "待审批", approved: "已批准", running: "进行中", succeeded: "通过", failed: "失败" })[
    value
  ] ?? "未知";
}

function platformDuration(value) {
  if (!Number.isSafeInteger(value) || value < 0) return "-";
  if (value % 86_400 === 0) return `${value / 86_400} 天`;
  if (value % 3_600 === 0) return `${value / 3_600} 小时`;
  if (value % 60 === 0) return `${value / 60} 分钟`;
  return `${value} 秒`;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "-";
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${Math.round(bytes)} B`;
}

function edgeLicenseRow(license, canRevoke, loading) {
  const usage = license.latest_usage;
  const ack = license.last_acknowledgement;
  const active = license.status === "active";
  return `<tr>
    <td data-label="Deployment"><strong>${escapeHtml(shortReference(license.deployment_id))}</strong><small>generation ${number(license.generation)} · r${number(license.revision)}</small></td>
    <td data-label="状态"><span class="platform-status tone-${active ? "healthy" : "critical"}">${active ? "有效" : "已吊销"}</span><small>${escapeHtml(license.revoked_reason_code ?? "-")}</small></td>
    <td data-label="容量 / 用量"><strong>${number(usage?.active_node_units ?? 0)} / ${number(license.capacity.node_limit)} 节点</strong><small>${number(usage?.active_request_units ?? 0)} / ${number(license.capacity.concurrent_request_limit)} 并发 · 拒绝 ${number(usage?.denied_count ?? 0)}</small></td>
    <td data-label="有效期 / 宽限"><strong>${formatTime(license.expires_at)}</strong><small>${formatTime(license.grace_ends_at)}</small></td>
    <td data-label="ACK"><strong>${ack ? escapeHtml(ack.decision_status) : "未确认"}</strong><small>${ack ? formatTime(ack.acknowledged_at) : "-"}</small></td>
    <td data-label="操作">${canRevoke && active ? `<form data-platform-edge-license-revoke-form autocomplete="off">
      <input type="hidden" name="licenseId" value="${escapeAttribute(license.license_id)}">
      <input type="hidden" name="expectedRevision" value="${number(license.revision)}">
      <label><span>原因</span><select name="reasonCode"${loading ? " disabled" : ""}><option value="security_revocation">安全吊销</option><option value="commercial_termination">商业终止</option><option value="deployment_retired">Deployment 退役</option></select></label>
      <label class="platform-edge-confirm"><input type="checkbox" name="confirmed" required${loading ? " disabled" : ""}><span>确认立即吊销</span></label>
      <button class="platform-danger" type="submit"${loading ? " disabled" : ""}>吊销</button>
    </form>` : "-"}</td>
  </tr>`;
}

function defaultLicenseExpiry() {
  return new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString().slice(0, 16);
}

function edgeUpgradesView(state, permissions) {
  const releases = state.gatewayReleases?.releases ?? [];
  const rollouts = state.fleetRollouts?.rollouts ?? [];
  const released = releases.filter((item) => item.status === "released");
  const active = rollouts.filter((item) => new Set(["preparing", "running", "paused"])
    .has(item.status));
  const canSubmit = permissions.has("platform.edge_release.manage");
  const canApprove = permissions.has("platform.edge_release.approve");
  const canManageRollout = permissions.has("platform.edge_rollout.manage");
  const canReadRollout = permissions.has("platform.edge_rollout.read");
  return `
    <header class="platform-page-heading">
      <div><p>私有网关发布</p><h1>网关升级</h1></div>
      <button type="button" class="platform-icon-button" data-platform-action="edge-upgrades-refresh" title="刷新升级状态" aria-label="刷新升级状态"${state.loading ? " disabled" : ""}>↻</button>
    </header>
    <section class="platform-metrics" aria-label="Fleet upgrade summary">
      ${metric("已发布", released.length, `${releases.length} 个签名版本`)}
      ${metric("进行中批次", active.length, `${rollouts.length} 个发布批次`)}
      ${metric("失败目标", rollouts.reduce((total, item) => total + item.failed_count, 0), "达到阈值会自动暂停")}
    </section>
    ${state.loading && !state.gatewayReleases ? '<div class="platform-empty" role="status">正在读取 Fleet upgrade 状态</div>' : `
      ${canSubmit ? edgeReleaseSubmitForm(state.loading) : ""}
      ${edgeReleaseTable(releases, state, canApprove, canSubmit)}
      ${canReadRollout ? edgeRolloutControl(released, state, canManageRollout) : ""}
      ${canReadRollout ? edgeRolloutList(rollouts, state, canManageRollout) : ""}
    `}`;
}

function edgeReleaseSubmitForm(loading) {
  return `<section class="platform-edge-upgrade-section" aria-labelledby="edge-release-submit-title">
      <header><div><p>已签名发布包</p><h2 id="edge-release-submit-title">提交发布包</h2></div></header>
    <form class="platform-edge-upgrade-form" data-platform-edge-release-submit-form autocomplete="off">
      <label><span>安装包发布 ID</span><input name="installerReleaseId" pattern="irl_[0-9a-f]{32}" required /></label>
      <label><span>Channel</span><select name="releaseChannel"><option value="stable">Stable</option><option value="preview">Preview</option><option value="extended">Extended</option></select></label>
      <label><span>Schema from</span><input name="schemaFrom" type="number" min="1" max="9999" value="1" required /></label>
      <label><span>Schema to</span><input name="schemaTo" type="number" min="1" max="9999" value="1" required /></label>
      <label class="platform-edge-upgrade-wide"><span>Compatible versions</span><input name="compatibleFromVersions" placeholder="1.0.0, 1.1.0" required /></label>
      <label><span>Reason code</span><input name="reasonCode" value="scheduled_release" pattern="[a-z][a-z0-9_.-]{1,79}" required /></label>
      <label class="platform-edge-upgrade-check"><input name="downMigrationSupported" type="checkbox" /><span>支持 N-1 down migration</span></label>
      <label class="platform-edge-upgrade-check"><input name="confirmed" type="checkbox" required /><span>已核对签名、SBOM 与兼容矩阵</span></label>
      <button type="submit" class="platform-primary"${loading ? " disabled" : ""}>提交审批</button>
    </form>
  </section>`;
}

function edgeReleaseTable(releases, state, canApprove, canWithdraw) {
  const operatorId = String(state.session?.context?.operator?.operator_id ?? "");
  return `<section class="platform-edge-upgrade-section" aria-labelledby="edge-release-list-title">
    <header><div><p>双人审批</p><h2 id="edge-release-list-title">发布列表</h2></div></header>
    <div class="platform-table-scroll"><table class="platform-table platform-edge-release-table">
      <thead><tr><th>发布包</th><th>渠道</th><th>版本结构</th><th>状态</th><th>提交人</th><th>操作</th></tr></thead>
      <tbody>${releases.map((item) => {
        const selfSubmitted = operatorId && operatorId === item.submitted_by_operator_id;
        return `<tr>
    <td data-label="版本"><strong>${escapeHtml(item.version)}</strong><small>${escapeHtml(item.release_id)} · ${number(item.artifact_count)} 个文件</small></td>
          <td data-label="Channel">${escapeHtml(item.release_channel)}</td>
          <td data-label="Schema">${number(item.schema_from)} → ${number(item.schema_to)}${item.down_migration_supported ? " · N-1" : ""}</td>
          <td data-label="状态">${status(item.status)}</td>
          <td data-label="提交"><span>${formatTime(item.submitted_at)}</span><small>${selfSubmitted ? "由当前操作员提交" : escapeHtml(item.submitted_by_operator_id)}</small></td>
          <td data-label="操作" class="platform-edge-release-actions">
            ${canApprove && item.status === "approval_pending"
              ? edgeReleaseDecisionForm(item, "approve", "审批", state.loading, selfSubmitted) : ""}
            ${canWithdraw && new Set(["approval_pending", "released"]).has(item.status)
              ? edgeReleaseDecisionForm(item, "withdraw", "撤回", state.loading, false) : ""}
          </td>
        </tr>`;
      }).join("") || emptyRow("暂无 Gateway Release", 6)}</tbody>
    </table></div>
  </section>`;
}

function edgeReleaseDecisionForm(release, action, label, loading, selfSubmitted) {
  const disabled = loading || selfSubmitted;
  return `<form data-platform-edge-release-decision-form autocomplete="off">
    <input type="hidden" name="action" value="${action}" />
    <input type="hidden" name="releaseId" value="${escapeAttribute(release.release_id)}" />
    <input type="hidden" name="expectedRevision" value="${number(release.revision)}" />
    <label><span>验证方式</span><select name="method"${disabled ? " disabled" : ""}><option value="totp">TOTP</option><option value="webauthn">安全密钥</option><option value="recovery_code">恢复码</option></select></label>
    <label data-platform-step-up-code><span>二次验证码</span><input type="password" name="verificationCode" data-platform-sensitive autocomplete="one-time-code" required${disabled ? " disabled" : ""} /></label>
    <label><span>Reason code</span><input name="reasonCode" value="${action === "approve" ? "release_approved" : "release_withdrawn"}" pattern="[a-z][a-z0-9_.-]{1,79}" required${disabled ? " disabled" : ""} /></label>
    <label class="platform-edge-upgrade-check"><input name="confirmed" type="checkbox" required${disabled ? " disabled" : ""} /><span>${selfSubmitted ? "必须由另一位发布负责人审批" : "确认执行"}</span></label>
    <button type="submit"${action === "approve" ? ' class="platform-primary"' : ""}${disabled ? " disabled" : ""}>${label}</button>
  </form>`;
}

function edgeRolloutControl(releases, state, canManage) {
  const releaseOptions = releases.map((item) => `<option value="${escapeAttribute(item.release_id)}">${escapeHtml(item.version)} · ${escapeHtml(item.release_channel)}</option>`).join("");
  const compatibility = state.fleetCompatibility;
  return `<section class="platform-edge-upgrade-section" aria-labelledby="edge-rollout-control-title">
    <header><div><p>兼容性与分批发布</p><h2 id="edge-rollout-control-title">分批发布控制</h2></div></header>
    <div class="platform-edge-rollout-controls">
      <form data-platform-edge-compatibility-form autocomplete="off">
        <h3>兼容性预检</h3>
        <label><span>发布包</span><select name="releaseId" required>${releaseOptions}</select></label>
        <label><span>Deployment ID</span><input name="deploymentId" pattern="edp_[0-9a-f]{32}" required /></label>
        <button type="submit"${state.loading || !releaseOptions ? " disabled" : ""}>检查</button>
        ${compatibility ? `<div class="platform-edge-compatibility tone-${compatibility.compatible ? "applied" : "failed"}" role="status"><strong>${compatibility.compatible ? "兼容" : "已阻止"}</strong><span>${compatibility.reason_codes.length ? compatibility.reason_codes.map(escapeHtml).join(" / ") : "all_checks_passed"}</span><small>${escapeHtml(compatibility.deployment_id)} · ${escapeHtml(compatibility.current_version)} → ${escapeHtml(compatibility.release_version ?? "-")}</small></div>` : ""}
      </form>
      ${canManage ? `<form data-platform-edge-rollout-create-form autocomplete="off">
        <h3>创建 rollout</h3>
        <label><span>发布包</span><select name="releaseId" required>${releaseOptions}</select></label>
        <label class="platform-edge-upgrade-wide"><span>Deployment IDs</span><textarea name="deploymentIds" rows="3" placeholder="edp_... , edp_..." required></textarea></label>
        <label><span>Canary bps</span><input name="canaryBasisPoints" type="number" min="100" max="10000" value="100" required /></label>
        <label><span>观察秒数</span><input name="observationSeconds" type="number" min="1800" max="86400" value="1800" required /></label>
        <label><span>批次大小</span><input name="batchSize" type="number" min="1" max="1000" value="50" required /></label>
        <label><span>失败阈值 bps</span><input name="failureThresholdBasisPoints" type="number" min="1" max="10000" value="1000" required /></label>
        <label><span>Reason code</span><input name="reasonCode" value="scheduled_upgrade" pattern="[a-z][a-z0-9_.-]{1,79}" required /></label>
        <label class="platform-edge-upgrade-check"><input name="confirmed" type="checkbox" required /><span>确认目标、窗口与备份门禁</span></label>
        <button type="submit" class="platform-primary"${state.loading || !releaseOptions ? " disabled" : ""}>创建 rollout</button>
      </form>` : ""}
    </div>
  </section>`;
}

function edgeRolloutList(rollouts, state, canManage) {
  return `<section class="platform-edge-upgrade-section" aria-labelledby="edge-rollout-list-title">
    <header><div><p>按版本执行</p><h2 id="edge-rollout-list-title">分批发布列表</h2></div></header>
    <div class="platform-edge-rollout-list">${rollouts.map((rollout) => `
      <article class="platform-edge-rollout">
        <header><div><strong>${escapeHtml(rollout.release_version)}</strong><code>${escapeHtml(rollout.rollout_id)}</code></div>${status(rollout.status)}</header>
        <dl class="platform-fact-grid">
          <div><dt>发布代数</dt><dd>${number(rollout.generation)} / cancel ${number(rollout.cancel_watermark)}</dd></div>
          <div><dt>Targets</dt><dd>${number(rollout.succeeded_count)} succeeded · ${number(rollout.failed_count)} failed · ${number(rollout.target_count)} total</dd></div>
          <div><dt>Canary</dt><dd>${basisPoints(rollout.canary_basis_points)} · ${number(rollout.observation_seconds)}s</dd></div>
          <div><dt>Pause reason</dt><dd>${escapeHtml(rollout.pause_reason_code ?? "-")}</dd></div>
        </dl>
        ${canManage && new Set(["preparing", "running", "paused"]).has(rollout.status)
          ? edgeRolloutCommandForm(rollout, state.loading) : ""}
        <details><summary>批次 (${number(rollout.batches.length)})</summary>${edgeRolloutBatchTable(rollout.batches)}</details>
        <details><summary>目标 (${number(rollout.targets.length)})</summary>${edgeRolloutTargetTable(rollout.targets)}</details>
      </article>`).join("") || '<div class="platform-empty">暂无 Fleet rollout</div>'}</div>
  </section>`;
}

function edgeRolloutCommandForm(rollout, loading) {
  const action = rollout.status === "paused" ? "resume" : "pause";
  return `<form class="platform-edge-rollout-command" data-platform-edge-rollout-command-form autocomplete="off">
    <input type="hidden" name="rolloutId" value="${escapeAttribute(rollout.rollout_id)}" />
    <input type="hidden" name="action" value="${action}" />
    <input type="hidden" name="expectedRevision" value="${number(rollout.revision)}" />
    <label><span>Reason code</span><input name="reasonCode" value="${action === "pause" ? "operator_pause" : "operator_resume"}" pattern="[a-z][a-z0-9_.-]{1,79}" required /></label>
    <label class="platform-edge-upgrade-check"><input name="confirmed" type="checkbox" required /><span>确认 ${action === "pause" ? "暂停后续批次" : "恢复门禁调度"}</span></label>
    <button type="submit"${action === "resume" ? ' class="platform-primary"' : ""}${loading ? " disabled" : ""}>${action === "pause" ? "暂停" : "恢复"}</button>
  </form>`;
}

function edgeRolloutBatchTable(items) {
  return `<div class="platform-table-scroll"><table class="platform-table platform-edge-batch-table"><thead><tr><th>批次</th><th>类型</th><th>状态</th><th>成功 / 失败 / 总数</th><th>观察截止</th></tr></thead><tbody>${items.map((item) => `<tr><td data-label="批次">${number(item.sequence)}</td><td data-label="类型">${escapeHtml(item.batch_kind)}</td><td data-label="状态">${status(item.status)}</td><td data-label="结果">${number(item.succeeded_count)} / ${number(item.failed_count)} / ${number(item.target_count)}</td><td data-label="观察截止">${formatTime(item.observe_until)}</td></tr>`).join("") || emptyRow("尚未生成批次", 5)}</tbody></table></div>`;
}

function edgeRolloutTargetTable(items) {
  return `<div class="platform-table-scroll"><table class="platform-table platform-edge-target-table"><thead><tr><th>Deployment</th><th>批次</th><th>版本</th><th>状态</th><th>失败原因</th></tr></thead><tbody>${items.map((item) => `<tr><td data-label="Deployment"><code>${escapeHtml(item.deployment_id)}</code></td><td data-label="批次">${number(item.batch_sequence)}</td><td data-label="版本">${escapeHtml(item.current_version)} → ${escapeHtml(item.target_version)}</td><td data-label="状态">${status(item.status)}</td><td data-label="失败原因">${escapeHtml(item.failure_reason_code ?? "-")}</td></tr>`).join("") || emptyRow("暂无目标", 5)}</tbody></table></div>`;
}

function edgeFleetView(state, permissions) {
  const items = state.edgeFleet?.deployments ?? [];
  const filters = state.edgeFleetFilters ?? {};
  const page = (state.edgeFleetCursorHistory?.length ?? 0) + 1;
  const canManage = permissions.has("platform.edge_fleet.manage");
  return `
    <header class="platform-page-heading"><div><p>客户自有运行环境</p><h1>私有 Gateway 集群</h1></div><span>${number(items.length)} 个 Deployment</span></header>
    <section class="platform-band">
      <form class="platform-edge-fleet-filter" data-platform-edge-fleet-filter-form>
        <label><span>状态</span><select name="status">${edgeFleetStatusOptions(filters.status)}</select></label>
        <label><span>区域</span><input name="region" value="${escapeAttribute(filters.region ?? "")}" pattern="[a-z0-9][a-z0-9-]{1,39}" maxlength="40" autocomplete="off" /></label>
        <label><span>健康</span><select name="health">${edgeFleetHealthOptions(filters.health)}</select></label>
        <button type="submit"${state.loading ? " disabled" : ""}>筛选</button>
      </form>
      ${state.loading && !state.edgeFleet ? '<div class="platform-empty" role="status">正在读取 Fleet 摘要</div>' : `
        <div class="platform-table-wrap"><table class="platform-edge-fleet-table">
          <thead><tr><th>部署</th><th>区域 / 模式</th><th>状态</th><th>健康分</th><th>告警</th><th>证书</th><th>代理版本</th><th>目标 / 已应用 / 最近可用</th><th>遥测延迟</th><th>投影版本</th></tr></thead>
          <tbody>${items.map(edgeFleetRow).join("") || emptyRow("暂无私有 Gateway Deployment", 10)}</tbody>
        </table></div>
        <div class="platform-pagination"><span>第 ${number(page)} 页</span><div><button type="button" data-platform-action="edge-fleet-previous" ${(state.edgeFleetCursorHistory?.length ?? 0) ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="edge-fleet-next" ${state.edgeFleet?.next_cursor ? "" : "disabled"}>下一页</button></div></div>`}
    </section>
    ${canManage ? edgeDiagnosticControlView(state, items) : ""}`;
}

function edgeFleetRow(item) {
  const takeover = item.maintenance_mode
    ? "维护模式"
    : item.status === "pending"
      ? "待接管"
      : ["offline", "critical", "revoked", "retired"].includes(item.health_code)
        ? "已停止接管"
        : item.mode === "connected" && item.status === "active"
          ? "正常接管"
          : "未接管";
  return `<tr>
    <td data-label="Deployment"><strong>${escapeHtml(shortReference(item.deployment_id))}</strong><small>${formatTime(item.updated_at)}</small></td>
    <td data-label="区域 / 模式"><strong>${escapeHtml(item.region_code)}</strong><small>${escapeHtml(item.mode)}</small></td>
    <td data-label="状态">${status(item.status)}<small>${takeover}</small></td>
    <td data-label="健康分">${renderEdgeHealthScore(item.health_score, item.health_code, { compact: true })}<small>${escapeHtml(edgeHealthLabel(item.health_code))}</small></td>
    <td data-label="告警">${status(item.alert_code)}</td>
    <td data-label="证书">${status(item.certificate_signal)}</td>
    <td data-label="Agent"><strong>${escapeHtml(item.agent_version ?? "-")}</strong></td>
    <td data-label="期望 / 已应用 / 最低可用"><strong>${number(item.desired_generation)} / ${number(item.applied_generation)} / ${number(item.lkg_generation)}</strong></td>
    <td data-label="遥测延迟"><strong>${item.telemetry_lag_seconds === null ? "-" : `${number(item.telemetry_lag_seconds)} s`}</strong></td>
    <td data-label="投影版本"><strong>${number(item.index_revision)}</strong><small>health ${number(item.source_health_generation)}</small></td>
  </tr>`;
}

function edgeFleetStatusOptions(selected) {
  return [
    ["", "全部"], ["pending", "待注册"], ["active", "活跃"],
    ["revoked", "已吊销"], ["retired", "已退役"],
  ].map(([value, label]) => `<option value="${value}"${selected === value ? " selected" : ""}>${label}</option>`).join("");
}

function edgeFleetHealthOptions(selected) {
  return [
    ["", "全部"], ["healthy", "健康"], ["degraded", "需关注"],
    ["critical", "严重"], ["offline", "离线"], ["revoked", "已吊销"],
    ["unknown", "未知"],
  ].map(([value, label]) => `<option value="${value}"${selected === value ? " selected" : ""}>${label}</option>`).join("");
}

function edgeDiagnosticControlView(state, deployments) {
  const disabled = state.loading ? " disabled" : "";
  const deploymentOptions = (selectedId = "") => deployments.map((item) => (
    `<option value="${escapeAttribute(item.deployment_id)}"${item.deployment_id === selectedId ? " selected" : ""}>${escapeHtml(shortReference(item.deployment_id))} · ${escapeHtml(item.region_code)}</option>`
  )).join("");
  const grant = state.edgeDiagnosticGrant;
  const command = state.edgeDiagnosticCommand;
  return `<section class="platform-band platform-edge-diagnostics">
    <div class="platform-section-heading"><div><p>受控诊断</p><h2>受控远程诊断</h2></div><span>${state.edgeDiagnosticIntent ? "正在提交" : "待命"}</span></div>
    <div class="platform-edge-diagnostic-forms">
      <form data-platform-edge-diagnostic-request-form autocomplete="off">
        <h3>请求客户授权</h3>
        <label><span>Deployment</span><select name="deploymentId" required${disabled}>${deploymentOptions()}</select></label>
        <label><span>命令</span><select name="commandCode" required${disabled}><option value="runtime.version">运行时版本</option><option value="runtime.watermarks">同步水位</option><option value="runtime.disk_budget">磁盘预算</option><option value="runtime.connectivity_summary">连接摘要</option></select></label>
        <fieldset data-edge-command-parameter="runtime.watermarks" hidden>
          <legend>水位</legend>
          <label><input type="checkbox" name="watermarkCode" value="desired_generation" checked><span>Desired</span></label>
          <label><input type="checkbox" name="watermarkCode" value="applied_generation" checked><span>Applied</span></label>
      <label><input type="checkbox" name="watermarkCode" value="lkg_generation" checked><span>最低可用版本</span></label>
          <label><input type="checkbox" name="watermarkCode" value="telemetry_health_sequence"><span>健康状态 sequence</span></label>
          <label><input type="checkbox" name="watermarkCode" value="telemetry_failure_sequence"><span>Failure sequence</span></label>
        </fieldset>
        <label data-edge-command-parameter="runtime.disk_budget" hidden><span>卷</span><select name="volumeCode"><option value="runtime">Runtime</option><option value="state">State</option><option value="support_bundle">Support bundle</option></select></label>
        <label data-edge-command-parameter="runtime.connectivity_summary" hidden><span>目标</span><select name="targetCode"><option value="control_plane">Control plane</option><option value="provider_pool">Provider pool</option></select></label>
        <label><span>原因</span><select name="reasonCode" required${disabled}><option value="customer_reported_issue">客户报告问题</option><option value="incident_investigation">事件调查</option><option value="fleet_health_followup">Fleet 健康跟进</option></select></label>
        <label><span>最多执行</span><select name="maxRuns" required${disabled}><option value="1">1 次</option><option value="2">2 次</option><option value="3">3 次</option></select></label>
        <label class="platform-edge-confirm"><input type="checkbox" name="confirmed" required${disabled}><span>确认提交客户授权请求</span></label>
        <button class="platform-primary" type="submit"${disabled}>请求授权</button>
      </form>
      <form data-platform-edge-diagnostic-run-form autocomplete="off">
        <h3>执行已批准授权</h3>
        <label><span>Deployment</span><select name="deploymentId" required${disabled}>${deploymentOptions(grant?.deployment_id)}</select></label>
        <label><span>Diagnostic Grant ID</span><input name="grantId" value="${escapeAttribute(grant?.diagnostic_grant_id ?? "")}" pattern="dgr_[0-9a-f]{32}" maxlength="36" autocomplete="off" required${disabled}></label>
        <label><span>截止时间</span><select name="deadlineSeconds" required${disabled}><option value="30">30 秒</option><option value="60">60 秒</option><option value="120">120 秒</option></select></label>
        <label><span>原因</span><select name="reasonCode" required${disabled}><option value="approved_diagnostic">已批准诊断</option><option value="incident_investigation">事件调查</option><option value="fleet_health_followup">Fleet 健康跟进</option></select></label>
        <label><span>二次验证</span><select name="method" required${disabled}><option value="totp">Authenticator 验证码</option><option value="webauthn">安全密钥</option><option value="recovery_code">恢复码</option></select></label>
        <label data-platform-step-up-code><span>验证码</span><input name="verificationCode" type="password" maxlength="20" autocomplete="one-time-code" inputmode="numeric" data-platform-sensitive required${disabled}></label>
        <label class="platform-edge-confirm"><input type="checkbox" name="confirmed" required${disabled}><span>确认执行该短时只读命令</span></label>
        <button class="platform-primary" type="submit"${disabled}>验证并执行</button>
      </form>
    </div>
    ${grant ? edgeDiagnosticGrantSummary(grant) : ""}
    ${command ? edgeDiagnosticCommandSummary(command, disabled) : ""}
  </section>`;
}

function edgeDiagnosticGrantSummary(grant) {
  return `<div class="platform-edge-diagnostic-summary">
    <div><span>授权</span><strong>${escapeHtml(grant.diagnostic_grant_id)}</strong></div>
    <div><span>命令</span><strong>${escapeHtml(edgeCommandLabel(grant.command_code))}</strong></div>
    <div><span>状态</span><strong>${escapeHtml(edgeDiagnosticStatusLabel(grant.status))}</strong></div>
    <div><span>次数</span><strong>${number(grant.run_count)} / ${number(grant.max_runs)}</strong></div>
    <div><span>到期</span><strong>${formatOptionalTime(grant.expires_at)}</strong></div>
  </div>`;
}

function edgeDiagnosticCommandSummary(command, disabled) {
  const cancellable = new Set(["authorized", "leased", "running"]).has(command.status);
  return `<div class="platform-edge-diagnostic-command">
    <div class="platform-section-heading"><div><p>${escapeHtml(command.diagnostic_command_id)}</p><h3>${escapeHtml(edgeCommandLabel(command.command_code))}</h3></div><span>${escapeHtml(edgeDiagnosticStatusLabel(command.status))}</span></div>
    <dl><div><dt>发布代数</dt><dd>${number(command.generation)}</dd></div><div><dt>Deadline</dt><dd>${formatTime(command.deadline_at)}</dd></div><div><dt>修订号</dt><dd>${number(command.revision)}</dd></div></dl>
    ${cancellable ? `<form data-platform-edge-diagnostic-cancel-form>
      <input name="deploymentId" type="hidden" value="${escapeAttribute(command.deployment_id)}">
      <input name="commandId" type="hidden" value="${escapeAttribute(command.diagnostic_command_id)}">
      <label><span>取消原因</span><select name="reasonCode"${disabled}><option value="operator_cancelled">运维取消</option><option value="incident_resolved">事件已解决</option><option value="customer_revoked">客户已撤销</option></select></label>
      <label class="platform-edge-confirm"><input type="checkbox" name="confirmed" required${disabled}><span>确认取消命令</span></label>
      <button type="submit"${disabled}>取消命令</button>
    </form>` : ""}
  </div>`;
}

// Cell 行保持固定枚举列，避免把拓扑地址或客户信息扩展进运维表格。
function gatewayCellRow(cell) {
  return `<tr>
    <td data-label="Cell"><strong>${escapeHtml(cell.cell_code)}</strong><small>${escapeHtml(statusLabel(cell.capacity_class))}</small></td>
    <td data-label="区域">${escapeHtml(cell.region_code)}</td>
    <td data-label="容量">${status(cell.capacity_band)}</td>
    <td data-label="服务状态">${status(cell.status)}</td>
    <td data-label="健康">${status(cell.health_status)}</td>
    <td data-label="版本">${number(cell.release_watermark)}</td>
    <td data-label="Config">${number(cell.config_watermark)}</td>
  </tr>`;
}

// Placement 表单要求显式 revision、驻留版本和确认，写入仍由服务端 CAS 与权限复核裁决。
function gatewayPlacementForm(cells) {
  const options = cells.map((cell) => `<option value="${escapeAttribute(cell.cell_code)}">${escapeHtml(cell.cell_code)} · ${escapeHtml(cell.region_code)}</option>`).join("");
  return `
    <section class="platform-band platform-gateway-placement">
      <div class="platform-section-heading"><div><p>已签名的组织部署</p><h2>发布 组织 placement</h2></div><span>组织 路由</span></div>
      <form data-platform-gateway-placement-form autocomplete="off">
        <label><span>组织 ID</span><input name="workspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required /></label>
        <label><span>当前 revision</span><input name="revision" type="number" min="0" value="0" required /></label>
        <label><span>Primary Cell</span><select name="primaryCellCode" required>${options}</select></label>
        <label><span>Failover Cell</span><select name="failoverCellCode"><option value="">禁止 failover</option>${options}</select></label>
        <label><span>Residency policy</span><input name="residencyPolicyVersion" value="" placeholder="cn-residency-v1" maxlength="80" required /></label>
        <label><span>变更原因</span><select name="reasonCode"><option value="initial_placement">Initial placement</option><option value="capacity_rebalance">Capacity rebalance</option><option value="regional_recovery">区域al recovery</option></select></label>
        <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认发布新 generation</span></label>
        <button class="platform-primary" type="submit">发布 placement</button>
      </form>
    </section>`;
}

// 写入结果只回显安全 placement 投影和 generation，不渲染签名制品。
function gatewayPlacementResult(placement) {
  return `
    <section class="platform-band platform-gateway-result">
      <div class="platform-section-heading"><div><p>当前状态</p><h2>${escapeHtml(shortWorkspace(placement.workspace_id))}</h2></div>${status("active")}</div>
      <dl class="platform-fact-grid">
        ${fact("Primary", `${placement.primary_cell_code} · ${placement.primary_region_code}`)}
        ${fact("Failover", placement.failover_cell_code ?? "禁止")}
        ${fact("修订号", String(placement.revision))}
        ${fact("发布代数", String(placement.snapshot_generation))}
      </dl>
    </section>`;
}

function backupForm() {
  return `
    <section class="platform-band platform-backup-create">
      <div class="platform-section-heading"><div><p>已验证恢复</p><h2>创建备份</h2></div><span>隔离恢复门禁</span></div>
      <form data-platform-backup-form autocomplete="off">
        <label><span>备份类型</span><select name="backupType"><option value="full">完整备份</option><option value="incremental">增量备份</option></select></label>
        <label><span>保留天数</span><input name="retentionDays" type="number" min="1" max="3650" value="30" required /></label>
        <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认创建平台备份</span></label>
        <button class="platform-primary" type="submit">创建备份</button>
      </form>
    </section>`;
}

function backupPanel(backup) {
  const verification = backup.verification ?? {};
  return `
    <section class="platform-band platform-backup-detail">
      <div class="platform-section-heading"><div><p>备份集</p><h2>${escapeHtml(backup.backup_id)}</h2></div><div class="platform-row-actions">${status(backup.status)}<button class="platform-icon-button" type="button" data-platform-action="backup-refresh" title="刷新备份状态" aria-label="刷新备份状态">↻</button></div></div>
      <dl>
        ${fact("来源", statusLabel(backup.source_backend))}
        ${fact("水位", backup.source_watermark ?? "-")}
        ${fact("Schema head", backup.schema_head ?? "-")}
        ${fact("内容摘要", shortHash(backup.content_sha256))}
        ${fact("保留至", formatTime(backup.retention_until))}
        ${fact("恢复校验", statusLabel(verification.status ?? "pending"))}
      </dl>
      ${backup.error_code ? `<div class="platform-feedback is-error"><strong>${escapeHtml(backup.error_code)}</strong></div>` : ""}
    </section>`;
}

function overviewView(state) {
  const operatorCount = state.operators?.total ?? 0;
  const grants = state.grants?.grants ?? [];
  const active = grants.filter((item) => item.status === "active").length;
  const pending = grants.filter((item) => item.status === "pending_approvals").length;
  return `
    <header class="platform-page-heading"><div><p>运营概览</p><h1>平台工作台</h1></div><span>会话至 ${formatTime(state.session?.context?.expires_at)}</span></header>
    <section class="platform-metrics" aria-label="平台摘要">
      ${metric("操作员", operatorCount, "已纳入平台 IAM")}
      ${metric("有效支持授权", active, "短时诊断访问")}
      ${metric("待审批", pending, "客户与平台双域")}
    </section>
    <section class="platform-band"><div class="platform-section-heading"><div><p>权限</p><h2>当前授权</h2></div></div><div class="platform-chip-row">${(state.session?.context?.operator?.roles ?? []).map((role) => `<span>${escapeHtml(roleLabel(role))}</span>`).join("")}</div></section>`;
}

function operatorsView(state, permissions) {
  const operators = state.operators?.operators ?? [];
  return `
    <header class="platform-page-heading"><div><p>平台身份与权限</p><h1>操作员</h1></div><span>${operators.length} 个账号</span></header>
    ${permissions.has("platform.operator.manage") ? registrationPolicyForm(state) + operatorForm() : ""}
    <section class="platform-band"><div class="platform-table-wrap"><table><thead><tr><th>操作员</th><th>角色</th><th>状态</th><th>修订号</th><th><span class="platform-visually-hidden">操作</span></th></tr></thead><tbody>${operators.map(operatorRow).join("") || emptyRow("暂无操作员")}</tbody></table></div></section>`;
}

function mailSettingsView(state) {
  const saved = state.mailSettings;
  if (!saved) return `<section class="platform-band">${state.loading
    ? '<p role="status">正在加载邮件服务配置…</p>'
    : '<p>邮件服务配置尚未加载成功。</p><button type="button" data-platform-action="refresh">重新加载</button>'}</section>`;
  const settings = state.mailDraft ?? saved;
  const disabled = state.loading ? " disabled" : "";
  const testDisabled = state.loading || saved.revision < 1 || !saved.password_set ? " disabled" : "";
  const test = state.mailTest;
  return `
    <header class="platform-page-heading"><div><p>品牌与通知</p><h1>邮件服务</h1><p>为注册验证、邮箱验证和密码找回配置发信服务。</p></div><span class="platform-chip">${saved.enabled ? "已启用" : "未启用"}</span></header>
    <section class="platform-band platform-smtp-panel" aria-labelledby="smtp-settings-title">
      <div class="platform-section-heading"><div><h2 id="smtp-settings-title">SMTP 配置</h2><p>保存后即时生效，无需重启。密码加密保存，不会回显。</p></div></div>
      <form class="platform-smtp-form" data-platform-mail-settings-form autocomplete="off">
        <fieldset class="platform-smtp-fields"${disabled}>
          <label class="platform-smtp-enabled"><input type="checkbox" name="enabled"${settings.enabled ? " checked" : ""}><span>启用验证码邮件发送</span></label>
          <label><span>SMTP 服务器</span><input name="host" value="${escapeHtml(settings.host)}" required maxlength="253" placeholder="例如 smtp.example.com" autocomplete="off"><small>填写主机名，不含 https://、端口或路径。</small></label>
          <label><span>连接端口</span><input name="port" value="${Number(settings.port) || 587}" type="number" min="1" max="65535" required><small>STARTTLS 常用 587，TLS 常用 465；以邮件服务商要求为准。</small></label>
          <label><span>加密方式</span><select name="security"><option value="starttls"${settings.security === "starttls" ? " selected" : ""}>STARTTLS</option><option value="tls"${settings.security === "tls" ? " selected" : ""}>TLS / SSL</option></select><small>验证服务器证书，支持两种加密连接方式。</small></label>
          <label><span>SMTP 登录账号</span><input name="username" value="${escapeHtml(settings.username)}" required maxlength="320" autocomplete="off" placeholder="邮件服务提供的登录账号"></label>
          <label><span>密码或授权码</span><input name="password" type="password" maxlength="1024" autocomplete="new-password" ${saved.password_set ? 'placeholder="已保存；留空保留原密码"' : 'required placeholder="填写 SMTP 密码或邮箱授权码"'}><small>${saved.password_set ? "已保存密码。更换服务器或账号时须重新填写。" : "部分邮箱需要使用单独生成的 SMTP 授权码。"}</small></label>
          <label><span>发件邮箱</span><input name="sender" value="${escapeHtml(settings.sender)}" type="email" required maxlength="320" placeholder="security@example.com"><small>必须是邮件服务允许发送的地址。</small></label>
          <label><span>连接超时（秒）</span><input name="timeout_seconds" value="${Number(settings.timeout_seconds) || 10}" type="number" min="1" max="30" required></label>
        </fieldset>
        <div class="platform-smtp-actions"><button class="platform-primary" type="submit"${disabled}>${state.loading ? "处理中…" : "保存配置"}</button><a href="#operators">设置注册验证要求 →</a></div>
      </form>
    </section>
    <section class="platform-band platform-smtp-panel" aria-labelledby="smtp-test-title">
      <div class="platform-section-heading"><div><h2 id="smtp-test-title">验证发信配置</h2><p>先保存配置再测试。未启用邮件发送时，也可以测试已保存的配置。</p></div></div>
      <div class="platform-smtp-actions"><button type="button" data-smtp-connection-test${testDisabled}>测试连接与认证</button><span>只检查连接、加密和认证，不发送邮件。</span></div>
      <form class="platform-smtp-test-form" data-platform-mail-test-form>
        <label><span>测试收件邮箱</span><input name="recipient" type="email" value="${escapeHtml(state.mailRecipient ?? "")}" required maxlength="320" placeholder="填写你能接收邮件的地址"${disabled}></label>
        <button type="submit" class="platform-primary"${testDisabled}>发送测试邮件</button>
      </form>
      ${test ? `<div class="platform-smtp-result" role="status" data-tone="${test.status === "passed" ? "success" : "error"}"><strong>${test.status === "passed" ? "测试成功" : "测试失败"}</strong><p>${escapeHtml(test.message)}</p></div>` : ""}
    </section>`;
}

function registrationPolicyForm(state) {
  const policy = state.registrationPolicy;
  if (!policy) return "";
  return `<section class="platform-band platform-form-band">
    <div class="platform-section-heading"><div><p>SaaS 平台</p><h2>注册验证设置</h2></div><span>修订 ${number(policy.revision)}</span></div>
    <form class="platform-inline-form" data-platform-registration-policy-form>
      <fieldset ${state.loading ? "disabled" : ""}><legend>验证要求</legend><div class="platform-role-grid">
        <label><input name="emailVerification" type="checkbox" ${policy.email_verification ? "checked" : ""} /><span>邮箱验证</span></label>
        <label><input name="humanVerification" type="checkbox" ${policy.human_verification ? "checked" : ""} /><span>基础图形验证码</span></label>
      </div></fieldset>
      <span>邮件服务：${policy.email_delivery_available ? "可用" : "未配置"} · <a href="#mail-settings">配置 SMTP 邮件服务 →</a></span>
      <button class="platform-primary" type="submit" ${state.loading ? "disabled" : ""}>保存设置</button>
    </form></section>`;
}

function operatorForm() {
  return `
    <section class="platform-band platform-form-band"><div class="platform-section-heading"><div><p>委派</p><h2>新增操作员</h2></div></div>
      <form class="platform-inline-form" data-operator-form autocomplete="off">
        <label><span>Global Account ID</span><input name="accountId" pattern="gac_[0-9a-f]{32}" maxlength="36" required /></label>
        <fieldset><legend>角色</legend><div class="platform-role-grid">${ROLE_OPTIONS.map((role) => `<label><input type="checkbox" name="roles" value="${role}" /><span>${escapeHtml(roleLabel(role))}</span></label>`).join("")}</div></fieldset>
        <button class="platform-primary" type="submit">创建操作员</button>
      </form>
    </section>`;
}

function operatorRow(operator) {
  const disabled = operator.status !== "active";
  return `<tr><td><strong>${escapeHtml(operator.display_name)}</strong><small>${escapeHtml(operator.operator_id)}</small></td><td>${(operator.roles ?? []).map((role) => `<span class="platform-role">${escapeHtml(roleLabel(role))}</span>`).join(" ")}</td><td>${status(operator.status)}</td><td>${number(operator.revision)}</td><td><button class="platform-icon-button" type="button" data-platform-action="toggle-operator" data-operator-id="${escapeAttribute(operator.operator_id)}" data-revision="${number(operator.revision)}" data-status="${disabled ? "active" : "disabled"}" data-roles="${escapeAttribute((operator.roles ?? []).join(","))}" title="${disabled ? "启用" : "停用"}" aria-label="${disabled ? "启用" : "停用"}">${disabled ? "▷" : "×"}</button></td></tr>`;
}

function workspacesView(state, permissions) {
  if (state.workspaceDetail) return workspaceDetailView(state, permissions);
  const items = state.workspaces?.items ?? [];
  const filters = state.workspaceFilters ?? {};
  return `
    <header class="platform-page-heading"><div><p>组织管理</p><h1>组织目录</h1></div><span>当前页 ${number(items.length)} 条</span></header>
    <section class="platform-band platform-workspace-tools">
      <form class="platform-filter-form" data-workspace-filter-form autocomplete="off">
        <label class="platform-filter-search"><span>搜索组织</span><input name="search" value="${escapeAttribute(filters.search ?? "")}" placeholder="名称或组织标识" maxlength="80" /></label>
        <label><span>状态</span><select name="operational_status">${filterOptions(filters.operational_status, [["", "全部"], ["active", "活跃"], ["suspended", "已暂停"], ["deletion_pending", "等待删除"], ["deleted", "已删除"], ["migration_conflict", "迁移异常"]])}</select></label>
        <label><span>区域</span><input name="region" value="${escapeAttribute(filters.region ?? "")}" placeholder="cn-north-1" maxlength="40" /></label>
        <label><span>风险标签</span><select name="risk_tag">${filterOptions(filters.risk_tag, [["", "全部"], ["security_review", "安全复核"], ["compliance_hold", "合规关注"], ["payment_attention", "账务关注"], ["adoption_attention", "使用关注"]])}</select></label>
        <label><span>健康状态</span><select name="health_status">${filterOptions(filters.health_status, [["", "全部"], ["fresh", "正常"], ["stale", "数据滞后"], ["not_ready", "未就绪"], ["unavailable", "不可用"]])}</select></label>
        <label><span>健康风险</span><select name="health_risk">${filterOptions(filters.health_risk, [["", "全部"], ["healthy", "健康"], ["watch", "需关注"], ["at_risk", "有风险"], ["critical", "严重"]])}</select></label>
        <label><span>类型</span><select name="kind">${filterOptions(filters.kind, [["", "全部"], ["organization", "组织"]])}</select></label>
        <button class="platform-primary" type="submit">筛选</button>
      </form>
    </section>
    <section class="platform-band platform-workspace-directory">
      <div class="platform-table-wrap"><table class="platform-workspace-table"><thead><tr><th>组织</th><th>状态</th><th>区域</th><th>健康</th><th>风险</th><th>成员 / 活跃会话</th><th><span class="platform-visually-hidden">操作</span></th></tr></thead><tbody>${items.map(workspaceRow).join("") || emptyRow("没有符合条件的组织", 7)}</tbody></table></div>
      <div class="platform-pagination"><span>第 ${number((state.workspaceCursorHistory?.length ?? 0) + 1)} 页</span><div><button type="button" data-platform-action="workspace-previous" ${state.workspaceCursorHistory?.length ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="workspace-next" ${state.workspaces?.has_more ? "" : "disabled"}>下一页</button></div></div>
    </section>`;
}

function workspaceRow(workspace) {
  const health = workspace.health ?? {};
  const tags = workspace.platform_risk_tags;
  return `<tr>
    <td data-label="组织"><strong>${escapeHtml(workspace.display_name)}</strong><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(workspace.workspace_id)}</code>${workspace.slug ? `<code>${escapeHtml(workspace.slug)}</code>` : ""}</details></td>
    <td data-label="状态">${status(workspace.operational_status)}<small>Rev ${number(workspace.operational_revision)}</small></td>
    <td data-label="区域"><strong>${escapeHtml(workspace.region_code ?? "未配置")}</strong><small>${escapeHtml(kindLabel(workspace.kind))}</small></td>
    <td data-label="健康">${status(health.score_band ?? health.status)}<small>${escapeHtml(statusLabel(health.status ?? "not_ready"))}</small></td>
    <td data-label="风险">${Array.isArray(tags) ? riskTagList(tags) : '<span class="platform-restricted">受限</span>'}</td>
    <td data-label="成员 / Session"><strong>${number(workspace.member_count)} / ${number(workspace.active_session_count)}</strong></td>
    <td data-label="操作"><div class="platform-row-actions"><button type="button" data-platform-action="open-workspace" data-workspace-id="${escapeAttribute(workspace.workspace_id)}">查看详情</button></div></td>
  </tr>`;
}

function workspaceDetailView(state, permissions) {
  const workspace = state.workspaceDetail.workspace;
  const health = state.workspaceHealth?.health ?? workspace.health ?? {};
  const actions = state.workspaceDetail.recent_actions ?? [];
  const events = state.workspaceEvents?.events ?? [];
  const canLifecycle = permissions.has("platform.workspace.lifecycle.manage");
  const riskOptions = allowedRiskOptions(permissions);
  const canContact = permissions.has("platform.workspace.lifecycle.manage")
    || permissions.has("platform.customer_success.read");
  return `
    <header class="platform-page-heading platform-workspace-heading"><div><button class="platform-back-button" type="button" data-platform-action="close-workspace">← 返回目录</button><p>${escapeHtml(kindLabel(workspace.kind))}</p><h1>${escapeHtml(workspace.display_name)}</h1><small>${workspace.slug ? escapeHtml(workspace.slug) : "组织空间"}</small></div><div class="platform-detail-status">${status(workspace.operational_status)}<span>组织状态已更新 ${number(workspace.operational_revision)} 次</span></div></header>
    ${canLifecycle ? lifecycleCommands(workspace) : ""}
    ${canLifecycle ? privateGatewayCapabilityCommand(workspace) : ""}
    <section class="platform-band platform-workspace-summary">
      <div class="platform-section-heading"><div><p>运营事实</p><h2>运营摘要</h2></div><span>更新于 ${formatTime(workspace.updated_at)}</span></div>
      <dl class="platform-fact-grid">
        ${fact("区域", workspace.region_code ?? "未配置")}
        ${fact("驻留策略", workspace.residency_policy_version ?? "未配置")}
        ${fact("成员", number(workspace.member_count))}
        ${fact("活跃会话", number(workspace.active_session_count))}
        ${fact("数据生命周期", statusLabel(workspace.data_lifecycle_status))}
        ${fact("Gateway", statusLabel(workspace.gateway_status))}
        ${fact("部署", statusLabel(workspace.deployment_status))}
        ${workspace.plan_status ? fact("套餐", statusLabel(workspace.plan_status)) : ""}
      </dl>
    </section>
    <section class="platform-band platform-health-detail">
      <div class="platform-section-heading"><div><p>健康状态</p><h2>健康投影</h2></div>${status(health.status)}</div>
      <dl class="platform-fact-grid">
        ${fact("健康等级", statusLabel(health.score_band ?? "not_ready"))}
        ${fact("风险等级", statusLabel(health.risk_level ?? "not_ready"))}
        ${fact("评估时间", formatTime(health.evaluated_at))}
        ${fact("健康风险", Array.isArray(health.risk_tags) ? health.risk_tags.map(statusLabel).join("、") || "无" : "受限")}
      </dl>
    </section>
    ${riskOptions.length && Array.isArray(workspace.platform_risk_tags) ? riskControls(workspace, riskOptions) : ""}
    ${canContact && workspace.owner_contact_status ? notificationControls(workspace) : ""}
    <section class="platform-band platform-detail-history">
      <div class="platform-history-column"><div class="platform-section-heading"><div><p>最近动作</p><h2>最近动作</h2></div></div>${actionTimeline(actions)}</div>
      <div class="platform-history-column"><div class="platform-section-heading"><div><p>事件时间线</p><h2>事件时间线</h2></div></div>${eventTimeline(events)}</div>
    </section>`;
}

function privateGatewayCapabilityCommand(workspace) {
  const enabled = Boolean(workspace.private_gateway_enabled);
  return `<section class="platform-command-bar"><div><strong>私有 Gateway</strong><span>${enabled ? "已为该组织开通" : "该组织尚未开通"}</span></div><button class="${enabled ? "platform-danger" : "platform-primary"}" type="button" data-platform-action="toggle-private-gateway">${enabled ? "关闭" : "开通"}</button></section>`;
}

function lifecycleCommands(workspace) {
  const canSuspend = workspace.operational_status === "active";
  const canRestore = workspace.operational_status === "suspended";
  if (!canSuspend && !canRestore) return "";
  const action = canSuspend ? "suspend" : "restore";
  return `<section class="platform-command-bar"><div><strong>${canSuspend ? "冻结组织" : "恢复组织"}</strong><span>${canSuspend ? "保留客户数据并撤销活跃组织会话" : "旧会话不会恢复"}</span></div><button class="${canSuspend ? "platform-danger" : "platform-primary"}" type="button" data-platform-action="prepare-workspace-lifecycle" data-lifecycle-action="${action}">${canSuspend ? "⏸ 冻结" : "▶ 恢复"}</button></section>`;
}

function riskControls(workspace, options) {
  const tags = workspace.platform_risk_tags ?? [];
  return `<section class="platform-band platform-operation-grid">
    <div><div class="platform-section-heading"><div><p>风险处理</p><h2>平台风险标签</h2></div><span>风险版本 ${number(workspace.risk_revision)}</span></div><div class="platform-active-risk-tags">${tags.map((tag) => `<span>${escapeHtml(statusLabel(tag))}<button type="button" data-platform-action="remove-workspace-risk" data-tag-code="${escapeAttribute(tag)}" data-reason-code="${escapeAttribute(riskReason(tag))}" title="移除标签" aria-label="移除 ${escapeAttribute(statusLabel(tag))}">×</button></span>`).join("") || '<span class="platform-restricted">暂无标签</span>'}</div></div>
    <form data-workspace-risk-form autocomplete="off"><label><span>标签</span><select name="tagCode">${filterOptions("", options)}</select></label><label><span>原因</span><select name="reasonCode">${filterOptions("", [["security_review", "安全复核"], ["compliance_review", "Compliance review"], ["payment_follow_up", "Payment follow up"], ["adoption_follow_up", "Adoption follow up"]])}</select></label><button class="platform-primary" type="submit">添加标签</button></form>
  </section>`;
}

function notificationControls(workspace) {
  const lastNotificationStatus = workspace.last_notification_status ?? "none";
  return `<section class="platform-band platform-operation-grid">
    <div><div class="platform-section-heading"><div><p>联系负责人</p><h2>联系 负责人</h2></div></div><dl class="platform-fact-grid platform-fact-grid-compact">${fact("联系状态", statusLabel(workspace.owner_contact_status))}<div><dt>最近通知</dt><dd>${status(lastNotificationStatus)}</dd></div></dl></div>
    <form data-workspace-notification-form autocomplete="off"><label><span>模板</span><select name="templateCode">${filterOptions("", [["workspace_operational_notice", "Operational notice"], ["workspace_risk_follow_up", "Risk follow up"], ["workspace_service_update", "Service update"]])}</select></label><label><span>原因</span><select name="reasonCode">${filterOptions("", [["operational_notice", "Operational notice"], ["risk_follow_up", "Risk follow up"], ["service_update", "Service update"]])}</select></label><button class="platform-primary" type="submit" ${workspace.owner_contact_status === "available" ? "" : "disabled"}>加入投递队列</button></form>
  </section>`;
}

function actionTimeline(actions) {
  return `<ol class="platform-timeline">${actions.map((action) => `<li><time>${formatTime(action.created_at)}</time><div><strong>${escapeHtml(actionLabel(action.action_type))}</strong><span>${escapeHtml(action.reason_code)}</span><small>Result rev ${number(action.result_revision)}</small></div></li>`).join("") || '<li class="platform-empty">暂无平台动作</li>'}</ol>`;
}

function eventTimeline(events) {
  return `<ol class="platform-timeline">${events.map((event) => `<li><time>${formatTime(event.occurred_at)}</time><div><strong>${escapeHtml(actionLabel(event.event_type))}</strong><span>${escapeHtml(event.reason_code)}</span><small>Operational ${number(event.operational_revision)} · Risk ${number(event.risk_revision)}</small></div></li>`).join("") || '<li class="platform-empty">暂无运营事件</li>'}</ol>`;
}

function supportView(state, permissions) {
  const grants = state.grants?.grants ?? [];
  const tickets = state.supportTickets?.items ?? [];
  const selected = tickets.find((item) => item.ticket_id === state.selectedSupportTicketId)
    ?? tickets[0] ?? null;
  return `
    <header class="platform-page-heading"><div><p>平台支持</p><h1>支持授权与协作</h1></div><span>${tickets.length} 个工单 · ${grants.length} 条授权</span></header>
    ${permissions.has("platform.support.ticket.read") ? platformSupportTicketQueue(state, tickets, selected, permissions) : ""}
    ${permissions.has("platform.support.request") ? supportForms(permissions, selected?.workspace_id) : ""}
    ${state.diagnostics ? diagnostics(state.diagnostics) : ""}
    <section class="platform-band"><div class="platform-section-heading"><div><p>支持授权</p><h2>诊断访问授权</h2></div><span>${grants.length} 条</span></div><div class="platform-table-wrap"><table><thead><tr><th>组织空间</th><th>状态</th><th>待审批</th><th>到期</th><th><span class="platform-visually-hidden">操作</span></th></tr></thead><tbody>${grants.map((grant) => grantRow(grant, permissions)).join("") || emptyRow("暂无支持授权")}</tbody></table></div></section>`;
}

function platformSupportTicketQueue(state, tickets, selected, permissions) {
  return `<section class="platform-band platform-support-tickets">
    <div class="platform-section-heading"><div><p>待处理事项</p><h2>客户工单队列</h2></div><span>${tickets.filter((item) => item.severity === "p1").length} 个高优先级</span></div>
    <form class="platform-support-ticket-filter" data-platform-support-ticket-filter-form>
      <label><span>状态</span><select name="status">${supportOptions(state.supportTicketFilters?.status, [["", "全部"], ["open", "待受理"], ["triaged", "已分诊"], ["waiting_customer", "待客户"], ["waiting_platform", "待平台"], ["resolved", "已解决"], ["closed", "已关闭"]])}</select></label>
      <label><span>优先级</span><select name="severity">${supportOptions(state.supportTicketFilters?.severity, [["", "全部"], ["p1", "P1"], ["p2", "P2"], ["p3", "P3"], ["p4", "P4"]])}</select></label>
      <label><span>组织编号</span><input name="workspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" value="${escapeAttribute(state.supportTicketFilters?.workspaceId ?? "")}" /></label>
      <button type="submit">筛选</button>
    </form>
    <div class="platform-support-ticket-layout">
      <div class="platform-support-ticket-list" role="list" aria-label="支持工单">
        ${tickets.map((ticket) => platformSupportTicketButton(ticket, selected?.ticket_id)).join("") || '<p class="platform-empty">当前筛选下没有工单</p>'}
      </div>
      <div class="platform-support-ticket-detail">${selected ? platformSupportTicketDetail(selected, permissions) : '<p class="platform-empty">选择工单查看协作详情</p>'}</div>
    </div>
  </section>`;
}

function platformSupportTicketButton(ticket, selectedId) {
  return `<button type="button" role="listitem" data-platform-action="support-ticket-select" data-ticket-id="${escapeAttribute(ticket.ticket_id)}" class="${ticket.ticket_id === selectedId ? "is-active" : ""}">
    <span><b data-severity="${escapeAttribute(ticket.severity)}">${escapeHtml(ticket.severity.toUpperCase())}</b><strong>${escapeHtml(ticket.subject)}</strong></span>
    <small>组织空间 · ${escapeHtml(supportStatusLabel(ticket.status))}</small>
    <time>${formatTime(ticket.updated_at)}</time>
  </button>`;
}

function platformSupportTicketDetail(ticket, permissions) {
  const canManage = permissions.has("platform.support.ticket.manage");
  const canManageSla = permissions.has("platform.support.sla.manage");
  return `<article>
    <header><div><span>${escapeHtml(ticket.severity.toUpperCase())} · ${escapeHtml(ticket.category)}</span><h3>${escapeHtml(ticket.subject)}</h3><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(ticket.ticket_id)}</code><code>${escapeHtml(ticket.workspace_id)}</code></details></div>${status(ticket.status)}</header>
    <dl class="platform-support-sla${ticket.sla.state === "running" && Date.parse(ticket.sla.risk_at) <= Date.now() ? " is-risk" : ""}">
      <div><dt>SLA</dt><dd>${escapeHtml(ticket.sla.state)}</dd></div><div><dt>风险阈值</dt><dd>${formatTime(ticket.sla.risk_at)}</dd></div><div><dt>解决时限</dt><dd>${formatTime(ticket.sla.resolution_deadline_at)}</dd></div><div><dt>升级</dt><dd>${number(ticket.sla.escalation_generation)} / ${number(ticket.sla.generation)}</dd></div>
    </dl>
    <section class="platform-support-section"><div class="platform-section-heading"><h4>协作时间线</h4><span>${ticket.messages.length} 条</span></div>
      <ol class="platform-support-timeline">${ticket.messages.map(platformSupportMessage).join("") || '<li class="platform-empty">暂无消息</li>'}</ol>
      ${canManage && ticket.status !== "closed" ? platformSupportMessageForm(ticket) : ""}
    </section>
    <section class="platform-support-section"><div class="platform-section-heading"><h4>安全诊断附件</h4><span>${ticket.attachments.length} 个</span></div>
      <div class="platform-support-attachments">${ticket.attachments.map((item) => `<div data-status="${escapeAttribute(item.status)}"><strong>${escapeHtml(supportAttachmentLabel(item.status))}</strong><span>${escapeHtml(item.intent_code)} · ${escapeHtml(item.media_type)}</span><small>${number(item.size_bytes)} bytes · ${escapeHtml(item.scan_outcome ?? "pending")}</small></div>`).join("") || '<p class="platform-empty">暂无诊断附件</p>'}</div>
    </section>
    <section class="platform-support-section"><div class="platform-section-heading"><h4>升级记录</h4><span>${ticket.escalations.length} 次</span></div>
      <ol class="platform-support-escalations">${ticket.escalations.map((item) => `<li><strong>${escapeHtml(item.source_code)}</strong><span>${escapeHtml(item.reason_code)} · ${escapeHtml(item.status)}</span><time>${formatTime(item.created_at)}</time></li>`).join("") || '<li class="platform-empty">暂无升级</li>'}</ol>
      ${canManage && !["resolved", "closed"].includes(ticket.status) ? platformSupportEscalationForm(ticket) : ""}
    </section>
    ${canManage ? platformSupportWorkflowForms(ticket, canManageSla) : '<p class="platform-restricted">当前为只读支持队列</p>'}
  </article>`;
}

function platformSupportMessage(item) {
  return `<li class="${item.visibility === "internal" ? "is-internal" : ""}"><div><strong>${item.visibility === "internal" ? "内部备注" : item.author_domain === "customer" ? "客户" : "平台支持"}</strong><time>${formatTime(item.created_at)}</time></div><p>${escapeHtml(item.body)}</p></li>`;
}

function platformSupportMessageForm(ticket) {
  return `<form class="platform-support-message-form" data-platform-support-message-form autocomplete="off"><input name="ticketId" type="hidden" value="${escapeAttribute(ticket.ticket_id)}" /><input name="revision" type="hidden" value="${number(ticket.revision)}" /><label><span>可见性</span><select name="visibility"><option value="customer">客户可见回复</option><option value="internal">内部备注</option></select></label><label><span>消息</span><textarea name="message" minlength="1" maxlength="8000" rows="3" required></textarea></label><button class="platform-primary" type="submit">发送</button></form>`;
}

function platformSupportWorkflowForms(ticket, canManageSla) {
  return `<section class="platform-support-operations">
    <form data-platform-support-workflow-form autocomplete="off"><h4>流程与分派</h4><input name="ticketId" type="hidden" value="${escapeAttribute(ticket.ticket_id)}" /><input name="revision" type="hidden" value="${number(ticket.revision)}" /><label><span>状态</span><select name="status">${supportOptions(ticket.status, [["open", "待受理"], ["triaged", "已分诊"], ["waiting_customer", "待客户"], ["waiting_platform", "待平台"], ["resolved", "已解决"], ["closed", "已关闭"]])}</select></label><label><span>处理人 Operator ID</span><input name="assigneeOperatorId" pattern="pto_[0-9a-f]{32}" maxlength="36" value="${escapeAttribute(ticket.assignee_operator_id ?? "")}" /></label><button type="submit">保存流程</button></form>
    ${canManageSla ? `<form data-platform-support-sla-form><h4>SLA 时钟</h4><input name="ticketId" type="hidden" value="${escapeAttribute(ticket.ticket_id)}" /><input name="revision" type="hidden" value="${number(ticket.revision)}" /><label><span>动作</span><select name="slaAction"><option value="pause_policy">策略暂停</option><option value="resume">恢复计时</option></select></label><label><span>原因</span><select name="reasonCode"><option value="approved_maintenance">批准维护</option><option value="external_dependency">外部依赖</option><option value="support_resume">恢复处理</option></select></label><button type="submit">更新 SLA</button></form>` : ""}
  </section>`;
}

function platformSupportEscalationForm(ticket) {
  return `<form class="platform-support-escalation-form" data-platform-support-escalation-form><input name="ticketId" type="hidden" value="${escapeAttribute(ticket.ticket_id)}" /><input name="revision" type="hidden" value="${number(ticket.revision)}" /><label><span>升级原因</span><select name="reasonCode"><option value="platform_manual_review">人工升级</option><option value="security_impact">安全影响</option><option value="sla_intervention">SLA 干预</option></select></label><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认升级</span></label><button type="submit">升级工单</button></form>`;
}

function supportOptions(selected, options) {
  return options.map(([value, label]) => `<option value="${escapeAttribute(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function supportStatusLabel(value) { return ({ open: "待受理", triaged: "已分诊", waiting_customer: "待客户", waiting_platform: "待平台", resolved: "已解决", closed: "已关闭" })[value] ?? value; }
function supportAttachmentLabel(value) { return ({ quarantined: "隔离扫描中", available: "扫描通过", rejected: "已拒绝", scan_timeout: "扫描超时" })[value] ?? value; }

function supportForms(permissions, workspaceId = "") {
  return `<section class="platform-band platform-form-split">
    <form data-support-grant-form autocomplete="off"><div class="platform-section-heading"><div><p>临时授权</p><h2>申请诊断访问</h2></div></div><label><span>组织编号</span><input name="workspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" value="${escapeAttribute(workspaceId)}" required /></label><label><span>申请原因</span><select name="reasonCode"><option value="workspace_diagnostics">组织诊断</option><option value="incident_response">故障处理</option></select></label><label><span>有效时长（分钟）</span><input name="durationMinutes" type="number" min="1" max="30" value="15" required /></label><button class="platform-primary" type="submit">提交申请</button></form>
    ${permissions.has("platform.break_glass.use") ? `<form data-break-glass-form autocomplete="off"><div class="platform-section-heading"><div><p>紧急访问</p><h2>紧急诊断</h2></div></div><label><span>组织编号</span><input name="workspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required /></label><label><span>申请原因</span><select name="reasonCode"><option value="incident_containment">控制故障影响</option><option value="service_recovery">恢复服务</option></select></label><label><span>动态验证码</span><input name="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认启动紧急访问</span></label><button class="platform-danger" type="submit">启动紧急诊断</button></form>` : ""}
  </section>`;
}

function grantRow(grant, permissions) {
  const canApprove = permissions.has("platform.support.approve") && grant.status === "pending_approvals" && grant.missing_approvals?.includes("platform");
  const canOpen = grant.status === "active";
  const canRevoke = ["active", "pending_approvals"].includes(grant.status);
  return `<tr><td><strong>组织空间</strong><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(grant.workspace_id)}</code><code>${escapeHtml(grant.grant_id)}</code></details></td><td>${status(grant.status)}</td><td>${escapeHtml((grant.missing_approvals ?? []).map(domainLabel).join("、") || "无")}</td><td>${formatTime(grant.expires_at)}</td><td><div class="platform-row-actions">${canApprove ? `<button type="button" data-platform-action="approve-grant" data-grant-id="${escapeAttribute(grant.grant_id)}">批准</button>` : ""}${canOpen ? `<button type="button" data-platform-action="open-support" data-grant-id="${escapeAttribute(grant.grant_id)}">打开诊断</button>` : ""}${canRevoke ? `<button class="is-danger" type="button" data-platform-action="prepare-revoke" data-grant-id="${escapeAttribute(grant.grant_id)}">撤销</button>` : ""}</div></td></tr>`;
}

function diagnostics(value) {
  return `<section class="platform-band platform-diagnostics"><div class="platform-section-heading"><div><p>支持会话</p><h2>组织诊断</h2></div><span>${status(value.operational_status)}</span></div><dl><div><dt>组织</dt><dd>${escapeHtml(value.workspace_id)}</dd></div><div><dt>区域</dt><dd>${escapeHtml(value.region_code ?? "-")}</dd></div><div><dt>开通状态</dt><dd>${escapeHtml(value.provisioning_status)}</dd></div><div><dt>健康状态</dt><dd>${escapeHtml(value.health_band ?? "-")}</dd></div><div><dt>有效成员数</dt><dd>${number(value.active_memberships)}</dd></div></dl></section>`;
}

function auditView(state) {
  const entries = state.audit?.entries ?? [];
  return `<header class="platform-page-heading"><div><p>操作记录</p><h1>平台审计</h1></div><span>${entries.length} 条记录</span></header><section class="platform-band"><ol class="platform-timeline">${entries.map((entry) => `<li><time>${formatTime(entry.occurred_at)}</time><div><strong>${escapeHtml(actionLabel(entry.action))}</strong><span>${escapeHtml(auditObjectLabel(entry.object_type))}</span><small>${escapeHtml(auditReasonLabel(entry.reason_code))}</small><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(entry.object_id ?? "-")}</code><code>${escapeHtml(entry.reason_code ?? "-")}</code></details></div></li>`).join("") || '<li class="platform-empty">暂无审计记录</li>'}</ol></section>`;
}

function billingView(state, permissions) {
  const canReadBilling = permissions.has("platform.billing.read");
  const canReadCatalog = permissions.has("platform.catalog.read");
  const catalogs = state.plans?.plans ?? [];
  const versions = catalogs.flatMap((catalog) => (catalog.versions ?? []).map((version) => ({
    ...version,
    plan_code: catalog.plan_code,
    display_name: catalog.display_name,
    catalog_status: catalog.status,
  })));
  return `
    <header class="platform-page-heading"><div><p>平台计费</p><h1>${canReadBilling ? "平台账单" : "套餐目录"}</h1></div><span>${canReadBilling ? `${number(state.billing?.cycles?.length ?? 0)} 个周期` : `${number(catalogs.length)} 个目录 · ${number(versions.length)} 个版本`}</span></header>
    ${canReadBilling ? platformMetering(state, permissions) : ""}
    ${canReadBilling ? platformBillingCycles(state, permissions) : ""}
    ${canReadBilling ? platformRefundQueue(state, permissions) : ""}
    ${canReadBilling ? platformInvoiceReconciliation(state, permissions) : ""}
    ${canReadCatalog ? platformCatalog(catalogs, versions, permissions) : ""}
  `;
}

function platformMetering(state, permissions) {
  const meter = state.metering;
  const fact = meter?.blocked_fact;
  const disabled = state.loading ? "disabled" : "";
  const labels = { idle: "已处理完毕", pending: "待处理", blocked: "需要对账处理" };
  const errors = {
    "platform_metering.owner_invalid": "缺少可用的历史订阅归属",
    "platform_metering.source_invalid": "原始用量不符合计量要求",
    "platform_metering.meter_unknown": "计量定义不可用",
    "platform_metering.checkpoint_conflict": "计量检查点冲突",
    "platform_metering.unavailable": "计量服务暂不可用",
  };
  const canWrite = permissions.has("platform.billing.adjust") && fact?.workspace_id;
  return `<section class="platform-band platform-metering" aria-labelledby="platform-metering-title">
    <div class="platform-section-heading"><div><h2 id="platform-metering-title">计量与恢复</h2></div>
      <button class="platform-icon-button" type="button" data-platform-action="metering-refresh"
        title="刷新计量状态" aria-label="刷新计量状态" ${disabled}>${icon("rotate-cw")}</button></div>
    ${meter ? `<dl class="platform-metering-summary">
      <div><dt>状态</dt><dd>${labels[meter.state] ?? "状态未知"}</dd></div>
      <div><dt>待处理事实</dt><dd>${number(meter.pending_count)}</dd></div>
      <div><dt>已处理序号</dt><dd>${number(meter.checkpoint)}</dd></div></dl>`
    : `<p role="status">${state.loading ? "正在读取计量状态" : "计量状态尚未加载"}</p>`}
    ${fact ? `<dl class="platform-metering-fact">
      <div><dt>组织</dt><dd>${escapeHtml(fact.workspace_id ?? "归属未确定")}</dd></div>
      <div><dt>发生时间</dt><dd>${formatTime(fact.occurred_at)}</dd></div>
      <div><dt>失败原因</dt><dd>${escapeHtml(errors[fact.error_code] ?? "需要核对计量事实")}</dd></div>
      <div><dt>源序号 / 尝试次数</dt><dd>${number(fact.source_sequence)} / ${number(fact.attempt_count)}</dd></div></dl>` : ""}
    ${canWrite ? `<form class="platform-meter-recovery-form" data-platform-meter-recovery-form autocomplete="off">
      <input type="hidden" name="meterOutboxId" value="${escapeAttribute(fact.meter_outbox_id)}" />
      <input type="hidden" name="sourceSequence" value="${escapeAttribute(fact.source_sequence)}" />
      <input type="hidden" name="attemptCount" value="${escapeAttribute(fact.attempt_count)}" />
      <input type="hidden" name="sourceSha256" value="${escapeAttribute(fact.source_sha256)}" />
      <label><span>处理方式</span><select name="action" required ${disabled}>
        <option value="">请选择处理方式</option><option value="retry">重新投影</option>
        ${fact.can_archive_internal_cost ? '<option value="internal_cost_only">仅内部成本归档，不进入平台账单</option>' : ""}</select></label>
      <label class="platform-check"><input name="confirmed" type="checkbox" required ${disabled} />
        <span>已核对当前事实及处理方式；归档不生成平台应收</span></label>
      <button class="platform-primary" type="submit" ${disabled}>${state.meterRecoveryIntent && state.loading ? "正在提交" : "提交处理"}</button>
    </form>` : ""}
    ${meter?.recent_recoveries?.length ? `<h3>最近处理记录</h3><div class="platform-table-wrap"><table>
      <thead><tr><th>源序号</th><th>组织</th><th>处理方式</th><th>时间</th></tr></thead><tbody>
      ${meter.recent_recoveries.map((item) => `<tr><td>${number(item.source_sequence)}</td>
        <td>${escapeHtml(item.workspace_id)}</td><td>${item.action === "retry" ? "重新投影" : "仅内部成本归档"}</td>
        <td>${formatTime(item.created_at)}</td></tr>`).join("")}</tbody></table></div>` : ""}
  </section>`;
}

function complianceView(state, permissions) {
  const response = state.complianceControls;
  const controls = Array.isArray(response?.controls) ? response.controls : [];
  const gaps = controls.filter((item) => new Set([
    "partially_implemented", "not_implemented", "evidence_stale",
  ]).has(item.report_outcome)).length;
  const failures = controls.filter((item) => Number(item.evidence?.failed_count ?? 0) > 0).length;
  const stale = controls.filter((item) => item.evidence?.freshness === "expired"
    || item.report_outcome === "evidence_stale").length;
  return `
    <header class="platform-page-heading">
      <div><p>合规证据</p><h1>合规复核</h1></div>
      <button class="platform-icon-button" type="button" data-platform-action="compliance-refresh"
        title="刷新控制证据" aria-label="刷新控制证据" ${state.loading ? "disabled" : ""}>&#8635;</button>
    </header>
    <p class="platform-compliance-disclaimer">仅展示控制与证据事实，不代表外部认证结论。</p>
    <section class="platform-metrics platform-compliance-metrics" aria-label="控制证据摘要">
      ${metric("控制项", controls.length, response?.control_set_version ?? "尚未读取")}
      ${metric("缺口", gaps, "部分实施、缺失或过期")}
      ${metric("失败", failures, "失败样本")}
      ${metric("过期", stale, "已过期证据")}
    </section>
    <section class="platform-band platform-compliance-controls">
      <div class="platform-section-heading"><div><p>负责人确认的状态</p><h2>控制覆盖与恢复状态</h2></div><span>${response?.workspace_id ? shortWorkspace(response.workspace_id) : "控制目录"}</span></div>
      <form class="platform-compliance-filter" data-platform-compliance-filter-form autocomplete="off">
        <label><span>组织 ID</span><input name="workspaceId" value="${escapeAttribute(state.complianceWorkspaceId ?? "")}" pattern="wsp_[0-9a-f]{32}" placeholder="留空查看控制目录" /></label>
        <button type="submit" ${state.loading ? "disabled" : ""}>筛选</button>
      </form>
      <div class="platform-table-wrap"><table class="platform-compliance-table">
        <thead><tr><th>控制与映射</th><th>结论</th><th>证据与时效</th><th>例外</th><th>整改</th></tr></thead>
        <tbody>${controls.map(complianceControlRow).join("") || emptyRow(state.loading ? "正在读取控制事实" : "暂无控制事实", 5)}</tbody>
      </table></div>
    </section>
    ${platformInvoiceReconciliation(state, permissions)}
  `;
}

function complianceControlRow(item) {
  const control = item.control;
  return `<tr>
    <td data-label="控制与映射"><strong>${escapeHtml(control.title)}</strong><code>${escapeHtml(control.control_code)} · v${number(control.control_version)}</code><small>${escapeHtml(complianceMappings(control.mappings))}</small></td>
    <td data-label="结论">${complianceOutcome(item.report_outcome)}<small>${escapeHtml(item.explanation_code ?? "目录事实")}</small></td>
    <td data-label="证据与时效">${complianceEvidence(item.evidence)}</td>
    <td data-label="例外">${complianceException(item.exception)}</td>
    <td data-label="整改">${complianceRemediation(item.remediation)}</td>
  </tr>`;
}

function complianceMappings(items) {
  return Array.isArray(items) && items.length
    ? items.map((item) => `${item.framework_code} ${item.clause_code}`).join(" · ")
    : "无框架映射";
}

function complianceOutcome(value) {
  if (value === null || value === undefined) {
    return '<span class="platform-status">目录范围</span>';
  }
  const label = ({
    implemented: "已实施",
    partially_implemented: "部分实施",
    not_implemented: "未实施",
    not_applicable: "不适用",
    exception_active: "有效例外",
    evidence_stale: "证据过期",
  })[value] ?? value;
  const kind = value === "implemented" || value === "not_applicable"
    ? "is-ok"
    : value === "exception_active" || value === "partially_implemented"
      ? "is-warn" : "is-danger";
  return `<span class="platform-status ${kind}">${escapeHtml(label)}</span>`;
}

function complianceEvidence(evidence) {
  if (!evidence) return '<span class="platform-restricted">无 组织 证据</span>';
  return `<strong>${escapeHtml(statusLabel(evidence.outcome))} · ${escapeHtml(statusLabel(evidence.freshness))}</strong>
    <small>${number(evidence.failed_count)} / ${number(evidence.sample_count)} 失败样本</small>
    <small>${escapeHtml(evidence.collector_version)} · ${escapeHtml(shortReference(evidence.collector_service_account_id))}</small>
    <small>${escapeHtml(evidence.classification)} · ${shortHash(evidence.artifact_sha256)}</small>
    <small>有效至 ${formatTime(evidence.valid_until)}</small>`;
}

function complianceException(exception) {
  if (!exception) return '<span class="platform-restricted">无</span>';
  return `${status(exception.status)}<small>${escapeHtml(exception.reason_code)}</small><small>${escapeHtml(exception.compensating_controls.join(" · "))}</small><small>到期 ${formatTime(exception.expires_at)}</small>`;
}

function complianceRemediation(remediation) {
  if (!remediation) return '<span class="platform-restricted">无</span>';
  return `${status(remediation.status)}<small>${escapeHtml(remediation.owner_code)}</small><small>${escapeHtml(shortReference(remediation.remediation_task_id))}</small><small>期限 ${formatTime(remediation.due_at)}</small>`;
}

function platformBillingCycles(state, permissions) {
  const cycles = state.billing?.cycles ?? [];
  const adjustable = cycles.flatMap((cycle) => (
    cycle.status === "open" ? [] : cycle.meters
      .filter((meter) => meter.summary_sha256)
      .map((meter) => ({ cycle, meter }))
  ));
  return `
    <section class="platform-band platform-billing-cycles">
      <div class="platform-section-heading"><div><p>周期对账</p><h2>周期与差异状态</h2></div><span>${number(cycles.filter((cycle) => cycle.status === "reconciliation_required").length)} 个待对账</span></div>
      <form class="platform-billing-filter" data-platform-billing-filter-form>
        <label><span>周期状态</span><select name="status"><option value="" ${state.billingStatus ? "" : "selected"}>全部</option>${billingStatusOptions(state.billingStatus)}</select></label>
        <button type="submit">筛选</button>
      </form>
      <div class="platform-table-wrap"><table class="platform-billing-table"><thead><tr><th>组织</th><th>周期</th><th>状态</th><th>请求</th><th>Token</th><th>有效金额</th><th>更新</th></tr></thead><tbody>${cycles.map(platformBillingCycleRow).join("") || emptyRow("暂无账单周期", 7)}</tbody></table></div>
      <div class="platform-pagination"><span>第 ${number((state.billingCursorHistory?.length ?? 0) + 1)} 页</span><div><button type="button" data-platform-action="billing-previous" ${state.billingCursorHistory?.length ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="billing-next" ${state.billing?.next_cursor ? "" : "disabled"}>下一页</button></div></div>
    </section>
    ${permissions.has("platform.billing.adjust") ? platformBillingAdjustmentForm(adjustable) : ""}
  `;
}

function platformBillingCycleRow(cycle) {
  const requests = cycle.meters.find((meter) => meter.meter_code === "api_requests");
  const tokens = cycle.meters.find((meter) => meter.meter_code === "total_tokens");
  return `<tr>
    <td data-label="组织"><strong>${escapeHtml(cycle.workspace_id)}</strong><small>${escapeHtml(cycle.billing_cycle_id)}</small></td>
    <td data-label="周期"><strong>${formatDate(cycle.period_started_at)} - ${formatDate(cycle.period_ends_at)}</strong><small>Rev ${number(cycle.revision)} · watermark ${number(cycle.source_watermark)}</small></td>
    <td data-label="状态">${status(cycle.status)}</td>
    <td data-label="请求"><strong>${number(requests?.usage_quantity)} / ${number(requests?.allocated_quantity)}</strong><small>超额 ${number(requests?.overage_quantity)}</small></td>
    <td data-label="Token"><strong>${number(tokens?.usage_quantity)}</strong><small>调整 ${signedNumber(tokens?.adjustment_quantity)}</small></td>
    <td data-label="有效金额"><strong>${escapeHtml(requests ? `${requests.currency} ${requests.effective_amount}` : "-")}</strong></td>
    <td data-label="更新">${formatTime(cycle.updated_at)}</td>
  </tr>`;
}

function platformBillingAdjustmentForm(targets) {
  const content = `
    <section class="platform-band platform-billing-adjustment">
      <div class="platform-section-heading"><div><p>账单调整</p><h2>追加账单调整</h2></div><span>保留历史记录</span></div>
      ${targets.length ? `<form data-platform-billing-adjustment-form autocomplete="off">
        <label class="platform-billing-target"><span>账单摘要</span><select name="targetKey" required>${targets.map(({ cycle, meter }) => `<option value="${escapeAttribute(`${cycle.billing_cycle_id}:${meter.meter_code}`)}">${escapeHtml(shortWorkspace(cycle.workspace_id))} · ${escapeHtml(meterLabel(meter.meter_code))} · ${escapeHtml(billingStatusLabel(cycle.status))}</option>`).join("")}</select></label>
        <label><span>方向</span><select name="reasonCode"><option value="manual_credit">贷记</option><option value="manual_debit">借记</option></select></label>
        <label><span>数量</span><input name="quantity" type="number" min="0" max="1000000000000" step="1" value="0" required /></label>
        <label><span>金额</span><input name="amount" inputmode="decimal" pattern="(?:0|[1-9][0-9]{0,10})\.[0-9]{2}" value="0.00" required /></label>
        <button class="platform-primary" type="submit">追加调整</button>
      </form>` : '<p class="platform-empty">当前页没有可调整的已冻结摘要。</p>'}
    </section>
  `;
  return targets.length ? content : `<details class="platform-collapsed-section"><summary>账单调整 <span>当前没有待调整账单</span></summary>${content}</details>`;
}

function platformRefundQueue(state, permissions) {
  const refunds = state.refunds?.refunds ?? [];
  const canReview = permissions.has("platform.billing.adjust");
  const content = `
    <section class="platform-band platform-refund-queue">
      <div class="platform-section-heading"><div><p>退款处理</p><h2>退款审核队列</h2></div><span>${number(refunds.length)} 笔</span></div>
      <form class="platform-billing-filter" data-platform-refund-filter-form>
        <label><span>退款状态</span><select name="status">${refundStatusOptions(state.refundStatus)}</select></label>
        <button type="submit">查询</button>
      </form>
      <div class="platform-table-wrap">
        <table class="platform-refund-table">
          <thead><tr><th>退款 / 订单</th><th>金额</th><th>原因</th><th>状态</th><th>申请时间</th><th>审核</th></tr></thead>
          <tbody>${refunds.map((refund) => platformRefundRow(refund, canReview)).join("") || emptyRow("当前筛选下没有退款申请", 6)}</tbody>
        </table>
      </div>
    </section>
  `;
  return refunds.length ? content : `<details class="platform-collapsed-section"><summary>退款审核 <span>当前没有待审核退款</span></summary>${content}</details>`;
}

function platformRefundRow(refund, canReview) {
  const requested = refund.status === "requested";
  return `
    <tr>
      <td data-label="退款 / 订单"><strong>${escapeHtml(refund.refund_id)}</strong><small>${escapeHtml(refund.order_id)}</small></td>
      <td data-label="金额"><strong>${escapeHtml(formatMinorMoney(refund.amount_minor, refund.currency))}</strong><small>原币最小单位 ${number(refund.amount_minor)}</small></td>
      <td data-label="原因">${escapeHtml(refundReasonLabel(refund.reason_code))}</td>
      <td data-label="状态"><span class="platform-status">${escapeHtml(refundStatusLabel(refund.status))}</span></td>
      <td data-label="申请时间">${formatTime(refund.requested_at)}</td>
      <td data-label="审核">${canReview && requested ? `
        <div class="platform-refund-actions">
          <button class="platform-primary" type="button" data-platform-action="refund-approve" data-refund-id="${escapeAttribute(refund.refund_id)}" data-revision="${number(refund.state_revision)}">批准</button>
          <form data-platform-refund-reject-form autocomplete="off">
            <input name="refundId" type="hidden" value="${escapeAttribute(refund.refund_id)}" />
            <input name="revision" type="hidden" value="${number(refund.state_revision)}" />
            <select name="reasonCode" aria-label="拒绝原因"><option value="amount_invalid">金额不符</option><option value="evidence_missing">证据不足</option></select>
            <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认</span></label>
            <button type="submit">拒绝</button>
          </form>
        </div>` : '<span class="platform-restricted">只读</span>'}</td>
    </tr>
  `;
}

function platformInvoiceReconciliation(state, permissions) {
  const roles = new Set(state.session?.context?.operator?.roles ?? []);
  const canOperate = (roles.has("platform_billing") && permissions.has("platform.billing.read"))
    || (roles.has("platform_compliance") && permissions.has("platform.compliance.read"));
  if (!canOperate) return "";
  const response = state.reconciliation ?? {};
  const run = response.reconciliation ?? null;
  const differences = Array.isArray(response.differences) ? response.differences : [];
  const reversal = state.reversal?.reversal ?? null;
  return `
    <section class="platform-band platform-reconciliation">
      <div class="platform-section-heading"><div><p>已冻结的对账进度</p><h2>订单、支付、Meter 与发票对账</h2></div>${run ? status(run.status) : ""}</div>
      ${run ? renderReconciliationRun(run) : `<form class="platform-reconciliation-create" data-platform-reconciliation-form>
        <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认冻结当前五类事实水位并启动对账</span></label>
        <button class="platform-primary" type="submit">创建对账</button>
      </form>`}
      ${run ? renderReconciliationDifferences(state, differences) : ""}
    </section>
    <section class="platform-band platform-invoice-reversal">
      <div class="platform-section-heading"><div><p>双域审批</p><h2>发票红字冲销</h2></div>${reversal ? status(reversal.status) : ""}</div>
      <div class="platform-reversal-forms">
        <form data-platform-reversal-form autocomplete="off">
          <label><span>发票申请 ID</span><input name="invoiceId" pattern="ivr_[0-9a-f]{32}" maxlength="36" required /></label>
          <label><span>冲红原因</span><select name="reasonCode"><option value="invoice_error">开票错误</option><option value="customer_correction">客户资料更正</option><option value="contract_canceled">合同取消</option></select></label>
          <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认提交双域审批</span></label>
          <button class="platform-danger" type="submit">创建冲红申请</button>
        </form>
        <form data-platform-reversal-review-form autocomplete="off">
          <label><span>冲红申请 ID</span><input name="reversalId" pattern="irr_[0-9a-f]{32}" maxlength="36" value="${escapeAttribute(reversal?.invoice_reversal_request_id ?? "")}" required /></label>
          <label><span>审批决定</span><select name="decision"><option value="approve">批准</option><option value="reject">拒绝</option></select></label>
          <label><span>审批依据</span><select name="reasonCode"><option value="facts_confirmed">事实已核验</option><option value="policy_confirmed">政策已核验</option><option value="evidence_missing">证据不足</option><option value="policy_blocked">政策阻断</option></select></label>
          <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认以当前账务或合规身份审批</span></label>
          <button class="platform-primary" type="submit">提交审批</button>
        </form>
      </div>
      ${reversal ? `<div class="platform-reversal-summary"><strong>${escapeHtml(reversalStatusLabel(reversal.status))}</strong><span>${escapeHtml(reversalReasonLabel(reversal.reason_code))} · 已批准域 ${escapeHtml(reversal.approved_domains.map(approvalDomainLabel).join("、") || "无")}</span><small>状态版本 ${number(reversal.state_revision)} · ${formatTime(reversal.updated_at)}</small></div>` : ""}
    </section>
  `;
}

function renderReconciliationRun(run) {
  return `
    <div class="platform-reconciliation-summary">
      <div><span>订单</span><strong>${number(run.order_watermark)}</strong></div>
      <div><span>支付</span><strong>${number(run.payment_watermark)}</strong></div>
      <div><span>Meter</span><strong>${number(run.meter_watermark)}</strong></div>
      <div><span>结算单</span><strong>${number(run.statement_watermark)}</strong></div>
      <div><span>发票</span><strong>${number(run.invoice_watermark)}</strong></div>
      <button class="platform-icon-button" type="button" data-platform-action="reconciliation-refresh" title="刷新对账" aria-label="刷新对账">↻</button>
    </div>
  `;
}

function renderReconciliationDifferences(state, differences) {
  return `
    <div class="platform-table-wrap">
      <table class="platform-reconciliation-table">
        <thead><tr><th>差异类型</th><th>左侧事实</th><th>右侧事实</th><th>原币差额</th><th>状态</th><th>发现时间</th></tr></thead>
        <tbody>${differences.map((item) => `<tr>
          <td data-label="差异类型"><strong>${escapeHtml(differenceTypeLabel(item.difference_type))}</strong></td>
          <td data-label="左侧事实"><strong>${escapeHtml(sourceTypeLabel(item.left_source_type))}</strong><small>${escapeHtml(shortReference(item.left_source_id))}</small></td>
          <td data-label="右侧事实"><strong>${escapeHtml(sourceTypeLabel(item.right_source_type))}</strong><small>${escapeHtml(shortReference(item.right_source_id))}</small></td>
          <td data-label="原币差额">${escapeHtml(formatMinorMoney(item.amount_difference_minor, item.currency))}</td>
          <td data-label="状态">${status(item.status)}</td>
          <td data-label="发现时间">${formatTime(item.detected_at)}</td>
        </tr>`).join("") || emptyRow("当前水位未发现差异", 6)}</tbody>
      </table>
    </div>
    <div class="platform-pagination"><span>第 ${number((state.reconciliationCursorHistory?.length ?? 0) + 1)} 页</span><div><button type="button" data-platform-action="reconciliation-previous" ${state.reconciliationCursorHistory?.length ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="reconciliation-next" ${state.reconciliation?.next_cursor ? "" : "disabled"}>下一页</button></div></div>
  `;
}

function platformCatalog(catalogs, versions, permissions) {
  return `
    ${permissions.has("platform.catalog.write") ? `<details class="platform-collapsed-section"><summary>创建套餐版本 <span>只有需要新增套餐时展开</span></summary>${platformPlanForm()}</details>` : ""}
    <section class="platform-band platform-plan-versions">
      <div class="platform-section-heading"><div><p>历史版本</p><h2>套餐版本记录</h2></div><span>${number(catalogs.length)} 个目录 · ${number(versions.length)} 个版本</span></div>
      <div class="platform-table-wrap"><table class="platform-plan-table"><thead><tr><th>套餐</th><th>版本</th><th>状态</th><th>价格</th><th>试用</th><th>权益</th></tr></thead><tbody>${versions.map(platformPlanRow).join("") || emptyRow("暂无套餐版本", 6)}</tbody></table></div>
    </section>
  `;
}

function platformPlanForm() {
  return `
    <section class="platform-band platform-plan-publisher">
      <div class="platform-section-heading"><div><p>套餐管理</p><h2>创建套餐版本</h2></div></div>
      <form data-platform-plan-form autocomplete="off">
        <label><span>Plan code</span><input name="planCode" pattern="[a-z0-9][a-z0-9_-]{1,39}" maxlength="40" required /></label>
        <label><span>展示名称</span><input name="displayName" maxlength="80" required /></label>
        <label><span>展示顺序</span><input name="displayOrder" type="number" min="0" max="10000" value="100" required /></label>
        <label><span>计费周期</span><select name="billingPeriod"><option value="monthly">每月</option><option value="yearly">每年</option><option value="none">免费</option></select></label>
        <label><span>币种</span><select name="currency"><option value="CNY">CNY</option><option value="USD">USD</option></select></label>
        <label><span>单价</span><input name="unitPrice" inputmode="decimal" pattern="(?:0|[1-9][0-9]{0,10})\.[0-9]{2}" value="0.00" required /></label>
        <label><span>试用天数</span><input name="trialDays" type="number" min="0" max="90" value="0" required /></label>
        <label><span>月请求数</span><input name="gatewayRequests" type="number" min="0" max="1000000000" value="10000" required /></label>
        <label><span>组织 席位</span><input name="workspaceSeats" type="number" min="1" max="100000" value="5" required /></label>
        <label><span>模型别名</span><input name="modelAliases" type="number" min="1" max="100000" value="10" required /></label>
        <label><span>留存天数</span><input name="retentionDays" type="number" min="1" max="3650" value="30" required /></label>
        <fieldset class="platform-plan-switches"><legend>发布选项</legend><label class="platform-check"><input name="prioritySupport" type="checkbox" /><span>优先支持</span></label><label class="platform-check"><input name="publish" type="checkbox" /><span>立即发布</span></label></fieldset>
        <button class="platform-primary" type="submit">创建版本</button>
      </form>
    </section>
  `;
}

function platformPlanRow(version) {
  const entitlements = version.entitlements ?? {};
  return `<tr>
    <td data-label="套餐"><strong>${escapeHtml(version.display_name)}</strong><small>${escapeHtml(version.plan_code)}</small></td>
    <td data-label="版本"><strong>v${number(version.version_number)}</strong><small>${escapeHtml(version.plan_version_id)}</small></td>
    <td data-label="状态">${status(version.status)}<small>${escapeHtml(version.catalog_status)}</small></td>
    <td data-label="价格"><strong>${escapeHtml(version.billing_period === "none" ? "免费" : `${version.currency} ${version.unit_price}`)}</strong><small>${escapeHtml(statusLabel(version.billing_period))}</small></td>
    <td data-label="试用">${number(version.trial_days)} 天</td>
    <td data-label="权益"><strong>${number(entitlements.workspace_seats)} 席位 · ${number(entitlements.gateway_requests_per_month)} 次/月</strong><small>${number(entitlements.model_aliases)} 别名 · ${number(entitlements.retention_days)} 天留存${entitlements.priority_support ? " · 优先支持" : ""}</small></td>
  </tr>`;
}

function securityAssuranceView(state, permissions) {
  const assessment = state.securityAssurance ?? null;
  const runs = assessment?.runs ?? [];
  const findings = assessment?.findings ?? [];
  const failed = runs.filter((item) => item.outcome !== "passed").length;
  const open = findings.filter((item) => item.status !== "closed").length;
  const cleaned = runs.filter((item) => item.cleanup_receipt_sha256).length;
  const canRun = permissions.has("platform.assurance.run");
  const canRemediate = permissions.has("platform.assurance.remediate");
  return `
    <header class="platform-page-heading">
      <div><p>固定规则与负责人确认</p><h1>安全复核</h1></div>
      ${assessment ? `<button class="platform-icon-button" type="button"
        data-platform-action="assurance-refresh" title="Refresh assessment"
        aria-label="Refresh assessment" ${state.loading ? "disabled" : ""}>&#8635;</button>` : ""}
    </header>
    <section class="platform-band platform-assurance-lookup">
      <div class="platform-section-heading"><div><p>评估编号</p><h2>查找评估</h2></div></div>
      <form data-platform-assurance-lookup-form autocomplete="off">
        <label><span>评估编号</span><input name="assessmentId"
          value="${escapeAttribute(state.securityAssuranceLookup ?? "")}"
          pattern="sas_[0-9a-f]{32}" maxlength="36" required /></label>
        <button type="submit" ${state.loading ? "disabled" : ""}>查询</button>
      </form>
    </section>
    ${canRun ? securityAssuranceStartForm(state.loading) : ""}
    ${assessment ? `
      <section class="platform-metrics platform-assurance-metrics" aria-label="Assessment summary">
        ${metric("发布代数", assessment.generation, assessment.profile_code.replaceAll("_", " "))}
        ${metric("Scenarios", runs.length, `${number(failed)} non-passing`)}
        ${metric("Open findings", open, `${number(findings.length)} total`)}
        ${metric("Cleanup receipts", cleaned, `${number(runs.length)} scenario runs`)}
      </section>
      ${securityAssessmentSummary(assessment)}
      ${securityAssuranceRuns(runs)}
      ${securityAssuranceFindings(findings, canRemediate, state.loading)}
    ` : '<section class="platform-band platform-domain-empty"><strong>尚未选择评估</strong></section>'}
  `;
}

function securityAssuranceStartForm(loading) {
  const disabled = loading ? " disabled" : "";
  return `<section class="platform-band platform-assurance-start">
    <div class="platform-section-heading"><div><p>已签名场景目录</p><h2>开始评估</h2></div><span>评估配置 v1</span></div>
    <form data-platform-assurance-start-form autocomplete="off">
      <label><span>组织 ID</span><input name="workspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>
      <fieldset class="platform-assurance-profiles"><legend>评估配置</legend>
        <label><input type="radio" name="profileCode" value="incremental_release" checked${disabled} /><span>增量发布</span></label>
        <label><input type="radio" name="profileCode" value="quarterly_full"${disabled} /><span>季度全面评估</span></label>
      </fieldset>
      ${assuranceDigestInput("codeTreeSha256", "代码校验值", disabled)}
      ${assuranceDigestInput("migrationSetSha256", "迁移集 SHA-256", disabled)}
      ${assuranceDigestInput("contractSetSha256", "契约集 SHA-256", disabled)}
      ${assuranceDigestInput("artifactSha256", "产物 SHA-256", disabled)}
      ${assuranceStepUpFields(disabled)}
      <label class="platform-check platform-assurance-confirm"><input type="checkbox" name="confirmed" required${disabled} /><span>确认执行已签名评估配置</span></label>
      <button class="platform-primary" type="submit"${disabled}>开始评估</button>
    </form>
  </section>`;
}

function assuranceDigestInput(name, label, disabled) {
  return `<label><span>${escapeHtml(label)}</span><input name="${escapeAttribute(name)}"
    pattern="[0-9a-f]{64}" minlength="64" maxlength="64" spellcheck="false" required${disabled} /></label>`;
}

function assuranceStepUpFields(disabled) {
  return `<label><span>再次验证方式</span><select name="method"${disabled}>
      <option value="totp">动态验证码</option><option value="webauthn">安全密钥</option>
    </select></label>
    <label data-platform-step-up-code><span>验证码</span><input name="verificationCode"
      type="password" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}"
      maxlength="6" data-platform-sensitive required${disabled} /></label>`;
}

function securityAssessmentSummary(assessment) {
  return `<section class="platform-band platform-assurance-summary">
    <div class="platform-section-heading"><div><p>${escapeHtml(assessment.security_assessment_id)}</p><h2>评估状态</h2></div>${status(assessment.status)}</div>
    <dl>
      <div><dt>组织</dt><dd><code>${escapeHtml(assessment.workspace_id)}</code></dd></div>
      <div><dt>Profile</dt><dd>${escapeHtml(assessment.profile_code.replaceAll("_", " "))} v${number(assessment.profile_version)}</dd></div>
      <div><dt>Requested</dt><dd>${formatTime(assessment.requested_at)}</dd></div>
      <div><dt>Finished</dt><dd>${assessment.finished_at ? formatTime(assessment.finished_at) : "Pending"}</dd></div>
      <div><dt>Profile proof</dt><dd><code>${escapeHtml(shortHash(assessment.profile_signature_sha256))}</code></dd></div>
      <div><dt>Scope proof</dt><dd><code>${escapeHtml(shortHash(assessment.scope_sha256))}</code></dd></div>
      <div><dt>Environment receipt</dt><dd><code>${escapeHtml(shortHash(assessment.environment_receipt_sha256))}</code></dd></div>
      <div><dt>Summary proof</dt><dd><code>${escapeHtml(shortHash(assessment.summary_sha256))}</code></dd></div>
    </dl>
  </section>`;
}

function securityAssuranceRuns(runs) {
  return `<section class="platform-band platform-assurance-runs">
    <div class="platform-section-heading"><div><p>负责人目录</p><h2>场景运行</h2></div><span>${number(runs.length)} runs</span></div>
    <div class="platform-table-wrap"><table>
      <thead><tr><th>Scenario</th><th>负责人 / target</th><th>契约测试</th><th>Outcome</th><th>耗时</th><th>Evidence / cleanup</th></tr></thead>
      <tbody>${runs.map((item) => `<tr>
        <td data-label="Scenario"><strong>${escapeHtml(item.scenario_code.replaceAll("_", " "))}</strong><small>${escapeHtml(item.weakness_code)}</small></td>
        <td data-label="负责人 / target"><strong>${escapeHtml(item.owner_kind.replaceAll("_", " "))}</strong><small>${escapeHtml(item.target_kind.replaceAll("_", " "))}</small></td>
        <td data-label="契约测试"><code>${escapeHtml(item.contract_code)}</code></td>
        <td data-label="Outcome">${status(item.outcome)}</td>
        <td data-label="耗时">${number(item.duration_ms)} ms</td>
        <td data-label="Evidence / cleanup"><code>${escapeHtml(shortHash(item.evidence_sha256))}</code><small>cleanup ${escapeHtml(shortHash(item.cleanup_receipt_sha256))}</small></td>
      </tr>`).join("") || emptyRow("No scenario runs", 6)}</tbody>
    </table></div>
  </section>`;
}

function securityAssuranceFindings(findings, canRemediate, loading) {
  return `<section class="platform-band platform-assurance-findings">
    <div class="platform-section-heading"><div><p>不可变证据</p><h2>问题与改进</h2></div><span>${number(findings.length)} findings</span></div>
    <div class="platform-assurance-finding-list">
      ${findings.map((item) => securityAssuranceFinding(item, canRemediate, loading)).join("")
        || '<p class="platform-empty">No findings.</p>'}
    </div>
  </section>`;
}

function securityAssuranceFinding(item, canRemediate, loading) {
  const proposed = [...item.remediations].reverse().find((entry) => entry.outcome === "proposed");
  const overdue = item.status !== "closed" && Date.parse(item.sla_deadline_at) < Date.now();
  const history = item.remediations.length
    ? item.remediations.map((entry) => `<li><strong>${escapeHtml(entry.outcome.replaceAll("_", " "))}</strong><code>${escapeHtml(shortHash(entry.change_sha256))}</code><small>${formatTime(entry.verified_at ?? entry.created_at)}</small></li>`).join("")
    : '<li><span>No remediation history</span></li>';
  const risks = item.risk_acceptances.length
    ? item.risk_acceptances.map((entry) => `<li>${status(entry.status)}<code>${escapeHtml(shortHash(entry.scope_sha256))}</code><small>expires ${formatTime(entry.expires_at)}</small></li>`).join("")
    : '<li><span>No risk acceptance</span></li>';
  return `<article class="platform-assurance-finding is-${escapeAttribute(item.severity)}">
    <header><div><strong>${escapeHtml(item.weakness_code.replaceAll("_", " "))}</strong><small>${escapeHtml(item.pen_test_finding_id)} · ${escapeHtml(item.contract_code)}</small></div><div>${status(item.severity)}${status(item.status)}</div></header>
    <dl><div><dt>负责人</dt><dd>${escapeHtml(item.owner_kind.replaceAll("_", " "))}</dd></div>
      <div><dt>SLA</dt><dd class="${overdue ? "is-overdue" : ""}">${formatTime(item.sla_deadline_at)}</dd></div>
      <div><dt>验证证据</dt><dd><code>${escapeHtml(shortHash(item.evidence_sha256))}</code></dd></div>
      <div><dt>修订号</dt><dd>${number(item.revision)}</dd></div></dl>
    <div class="platform-assurance-related"><div><h3>Remediation history</h3><ul>${history}</ul></div><div><h3>Risk acceptance</h3><ul>${risks}</ul></div></div>
    ${canRemediate && new Set(["open", "risk_accepted"]).has(item.status)
      ? securityRemediationForm(item, "propose", null, loading) : ""}
    ${canRemediate && item.status === "remediation_pending" && proposed
      ? securityRemediationForm(item, "verify", proposed, loading) : ""}
  </article>`;
}

function securityRemediationForm(finding, operation, proposal, loading) {
  const disabled = loading ? " disabled" : "";
  return `<form data-platform-assurance-remediation-form autocomplete="off">
    <input type="hidden" name="operation" value="${escapeAttribute(operation)}" />
    <input type="hidden" name="findingId" value="${escapeAttribute(finding.pen_test_finding_id)}" />
    <input type="hidden" name="expectedRevision" value="${number(finding.revision)}" />
    ${operation === "propose" ? assuranceDigestInput("changeSha256", "Change SHA-256", disabled)
      : `<input type="hidden" name="remediationId" value="${escapeAttribute(proposal.remediation_id)}" />
        <label><span>Verification assessment ID</span><input name="verificationAssessmentId" pattern="sas_[0-9a-f]{32}" maxlength="36" required${disabled} /></label>`}
    ${assuranceStepUpFields(disabled)}
    <label class="platform-check"><input type="checkbox" name="confirmed" required${disabled} /><span>Confirm ${escapeHtml(operation)}</span></label>
    <button type="submit"${disabled}>${operation === "propose" ? "Propose remediation" : "Verify with new generation"}</button>
  </form>`;
}

function developerContractsView(state, permissions) {
  const workspace = state.developerContracts;
  const versions = workspace?.versions ?? [];
  const releases = workspace?.sdk_releases ?? [];
  const notices = workspace?.deprecations ?? [];
  const supplyChain = workspace?.supply_chain ?? {};
  const canManage = permissions.has("platform.developer_contract.manage");
  const canApprove = permissions.has("platform.developer_contract.approve");
  const published = versions.filter((item) => item.status === "published");
  const pending = versions.filter((item) => item.status === "pending_approval");
  return `
    <header class="platform-page-heading">
      <div><p>发布管理</p><h1>接口与 SDK 发布</h1></div>
      <span>${canManage ? "可管理" : "只读"}</span>
    </header>
    <section class="platform-metrics" aria-label="发布概览">
      ${metric("接口版本", versions.length, `${pending.length} 个待审批`)}
      ${metric("SDK 发布", releases.filter((item) => item.status === "published").length, `${releases.length} 个构建`)}
      ${metric("弃用通知", notices.filter((item) => !new Set(["sunset", "withdrawn"]).has(item.status)).length, `${notices.length} 条记录`)}
      ${metric("安全检查", releases.filter((item) => supplyChain[item.sdk_release_id]?.report?.gate_outcome === "passed").length, `${releases.filter((item) => item.status === "published").length} 个已发布`)}
    </section>
    ${workspace ? "" : `<section class="platform-band"><p class="platform-empty">正在读取发布目录…</p></section>`}
    ${canManage ? developerContractBuildForm(state.loading) : ""}
    <section class="platform-band platform-developer-contract-versions">
      <div class="platform-section-heading"><div><p>版本目录</p><h2>接口版本</h2></div><span>${number(versions.length)} 个版本</span></div>
      ${versions.length ? developerContractVersionTable(versions, canApprove, state.loading) : '<p class="platform-empty">暂无接口版本。</p>'}
    </section>
    <section class="platform-band platform-developer-contract-sdks">
      <div class="platform-section-heading"><div><p>客户端发布</p><h2>SDK 版本</h2></div><span>${number(releases.length)} 个版本</span></div>
      ${releases.length ? developerContractSdkTable(releases, canManage, state.loading, supplyChain) : '<p class="platform-empty">暂无 SDK 版本。</p>'}
    </section>
    <section class="platform-band platform-supply-chain">
      <div class="platform-section-heading"><div><p>发布安全检查</p><h2>安全检查结果</h2></div><span>${number(Object.keys(supplyChain).length)} 份报告</span></div>
      ${platformSupplyChainEvidence(releases, supplyChain, permissions, state.loading)}
    </section>
    <section class="platform-band platform-developer-contract-deprecations">
      <div class="platform-section-heading"><div><p>版本生命周期</p><h2>弃用通知</h2></div><span>${number(notices.length)} 条通知</span></div>
      ${canManage && published.length >= 2 ? developerDeprecationCreateForm(published, state.loading) : ""}
      ${notices.length ? developerDeprecationTable(notices, canManage, canApprove, state.loading) : '<p class="platform-empty">暂无弃用通知。</p>'}
    </section>`;
}

function developerContractBuildForm(loading) {
  const disabled = loading ? " disabled" : "";
  return `
    <details class="platform-band platform-collapsed-section platform-developer-contract-build">
      <summary>创建发布版本 <span>准备新接口或 SDK 版本时展开</span></summary>
      <form data-platform-developer-contract-build-form class="platform-developer-contract-form">
        <label><span>API version</span><input name="apiVersion" inputmode="decimal" placeholder="1.2.0" pattern="(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)" required${disabled} /></label>
        <label><span>Python SDK 版本</span><input name="pythonSdkVersion" inputmode="decimal" placeholder="1.2.0" pattern="(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)" required${disabled} /></label>
        <label><span>JavaScript SDK 版本</span><input name="javascriptSdkVersion" inputmode="decimal" placeholder="1.2.0" pattern="(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)" required${disabled} /></label>
        <label><span>构建序号</span><input name="producerRevision" type="number" min="1" step="1" value="1" required${disabled} /></label>
        <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled} /><span>确认使用当前发布目录</span></label>
        <button class="platform-primary" type="submit"${disabled}>创建版本</button>
      </form>
    </details>`;
}

function developerContractVersionTable(versions, canApprove, loading) {
  return `<div class="platform-table-wrap"><table class="platform-developer-contract-table">
    <thead><tr><th>版本</th><th>状态</th><th>兼容性</th><th>校验结果</th><th>审批</th></tr></thead>
    <tbody>${versions.map((item) => `<tr>
      <td data-label="版本"><strong>${escapeHtml(item.version)}</strong><details class="platform-audit-technical"><summary>技术信息</summary><code>registry r${number(item.registry_revision)}</code><code>state r${number(item.revision)}</code></details></td>
      <td data-label="状态">${status(item.status)}<small>${formatTime(item.published_at ?? item.created_at)}</small></td>
      <td data-label="兼容性"><strong>${escapeHtml(item.compatibility_outcome)}</strong><small>${escapeHtml(item.breaking_reasons.join(", ") || "无破坏性变更")}</small></td>
      <td data-label="校验结果"><details class="platform-audit-technical"><summary>查看校验</summary><code>${escapeHtml(shortHash(item.contract_sha256))}</code><code>${escapeHtml(shortHash(item.compatibility_baseline_sha256))}</code></details></td>
      <td data-label="审批">${item.status === "pending_approval" && canApprove ? developerContractApproveForm(item, loading) : '<span class="platform-restricted">无需操作</span>'}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function developerContractApproveForm(item, loading) {
  const disabled = loading ? " disabled" : "";
  return `<form data-platform-developer-contract-approve-form class="platform-developer-contract-inline-form">
    <input name="apiVersionId" type="hidden" value="${escapeAttribute(item.api_version_id)}" />
    <input name="expectedRevision" type="hidden" value="${number(item.revision)}" />
    <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled} /><span>Independent approval</span></label>
    <button class="platform-primary" type="submit"${disabled}>Approve</button>
  </form>`;
}

function developerContractSdkTable(releases, canManage, loading, supplyChain) {
  return `<div class="platform-table-wrap"><table class="platform-developer-contract-table">
    <thead><tr><th>SDK</th><th>契约测试</th><th>Smoke</th><th>Signature summary</th><th>Security gate</th><th>Action</th></tr></thead>
    <tbody>${releases.map((item) => `<tr>
      <td data-label="SDK"><strong>${escapeHtml(developerLanguage(item.language))} ${escapeHtml(item.sdk_version)}</strong><small>${escapeHtml(item.runtime_version)} · ${status(item.status)}</small></td>
      <td data-label="契约测试"><span>API ${escapeHtml(item.api_version)}</span><small><code>${escapeHtml(shortHash(item.contract_sha256))}</code></small></td>
      <td data-label="Smoke"><strong>Windows ${escapeHtml(item.smoke_matrix.windows)}</strong><small>Linux ${escapeHtml(item.smoke_matrix.linux)} · N/N-1</small></td>
      <td data-label="签名摘要"><code>${escapeHtml(shortHash(item.signature_sha256))}</code><small>凭据 ${escapeHtml(shortHash(item.artifact_receipt_sha256))} · ${escapeHtml(item.signing_key_id ?? "待生成")}</small></td>
      <td data-label="Security gate">${platformSupplyChainCell(item, supplyChain[item.sdk_release_id])}</td>
      <td data-label="操作">${item.status === "published" && canManage ? developerSdkWithdrawForm(item, loading) : '<span class="platform-restricted">无需操作</span>'}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function platformSupplyChainCell(release, evidence) {
  if (release.status !== "published") return status("pending");
  if (!evidence || evidence.status === "missing") {
    return `${status("proof_missing")}<small>Install blocked</small>`;
  }
  return `${status(evidence.report.gate_outcome)}<small>${number(evidence.report.blocking_finding_count)} blocking</small>`;
}

function platformSupplyChainEvidence(releases, supplyChain, permissions, loading) {
  const published = releases.filter((item) => item.status === "published");
  if (!published.length) return '<p class="platform-empty">暂无已发布产物。</p>';
  const canRequest = permissions.has("platform.supply_chain.exception");
  const canApprove = permissions.has("platform.supply_chain.approve");
  return `<div class="platform-supply-chain-list">${published.map((release) => {
    const evidence = supplyChain[release.sdk_release_id];
    if (!evidence || evidence.status === "missing") {
      return `<article class="platform-supply-chain-release is-blocked">
        <header><div><strong>${escapeHtml(developerLanguage(release.language))} ${escapeHtml(release.sdk_version)}</strong><small>${escapeHtml(shortHash(release.artifact_sha256))}</small></div>${status("proof_missing")}</header>
        <p class="platform-empty">No attestation is available; installation remains blocked.</p>
      </article>`;
    }
    const report = evidence.report;
    const provenance = evidence.provenance;
    return `<article class="platform-supply-chain-release is-${escapeAttribute(report.gate_outcome)}">
      <header><div><strong>${escapeHtml(developerLanguage(report.language))} ${escapeHtml(report.sdk_version)}</strong><small>${escapeHtml(shortHash(report.artifact_sha256))} · ${formatTime(report.generated_at)}</small></div>${status(report.gate_outcome)}</header>
      <dl>
        <div><dt>SBOM</dt><dd>${escapeHtml(report.sbom.format)} ${escapeHtml(report.sbom.format_version)} · ${number(report.sbom.component_count)} components</dd></div>
        <div><dt>Attestation</dt><dd>${escapeHtml(report.signature.verification_outcome)} / client ${escapeHtml(report.signature.client_verification_outcome)}</dd></div>
        <div><dt>Provenance</dt><dd>${provenance.builder_verified ? "verified" : "unverified"} · ${provenance.reproducible ? "reproducible" : "not reproducible"}</dd></div>
        <div><dt>Gate</dt><dd>${number(report.blocking_finding_count)} blocking findings</dd></div>
      </dl>
      ${report.vulnerabilities.length ? `<div class="platform-supply-chain-findings">${report.vulnerabilities.map((item) => platformSupplyChainFinding(item, canRequest, canApprove, loading)).join("")}</div>` : '<p class="platform-empty">暂无待处理项。</p>'}
    </article>`;
  }).join("")}</div>`;
}

function platformSupplyChainFinding(item, canRequest, canApprove, loading) {
  const disabled = loading ? " disabled" : "";
  const exceptions = item.exceptions.map((entry) => `<li>
    <span>${status(entry.status)} ${escapeHtml(entry.reason_code)}</span>
    <small>${escapeHtml(shortWorkspace(entry.workspace_id))} · expires ${formatTime(entry.expires_at)} · r${number(entry.revision)}</small>
    ${entry.status === "pending" && canApprove ? `<form data-platform-supply-chain-exception-form class="platform-supply-chain-inline-form">
      <input name="operation" type="hidden" value="approve" />
      <input name="findingId" type="hidden" value="${escapeAttribute(item.vulnerability_finding_id)}" />
      <input name="securityExceptionId" type="hidden" value="${escapeAttribute(entry.security_exception_id)}" />
      <input name="expectedRevision" type="hidden" value="${number(entry.revision)}" />
      <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled} /><span>Independent approval</span></label>
      <button type="submit"${disabled}>Approve</button>
    </form>` : ""}
  </li>`).join("") || '<li><span class="platform-restricted">No exception</span></li>';
  const patches = item.patch_releases.map((entry) => `<li>
    <span>${status(entry.status)} ${escapeHtml(shortWorkspace(entry.workspace_id))}</span>
    <small>fixed ${escapeHtml(shortHash(entry.fixed_artifact_sha256))} · rollback ${escapeHtml(shortHash(entry.rollback_artifact_sha256))} · cloud/edge N/N-1 passed</small>
  </li>`).join("") || '<li><span class="platform-restricted">No patch release</span></li>';
  const requestForm = canRequest ? `<details class="platform-supply-chain-command">
    <summary>Request exception</summary>
    <form data-platform-supply-chain-exception-form autocomplete="off">
      <input name="operation" type="hidden" value="request" />
      <input name="findingId" type="hidden" value="${escapeAttribute(item.vulnerability_finding_id)}" />
      <label><span>组织 ID</span><input name="workspaceId" pattern="wsp_[0-9a-f]{32}" required${disabled} /></label>
      <label><span>原因</span><select name="reasonCode" required${disabled}><option value="vendor_fix_pending">等待供应商修复</option><option value="mitigated_exposure">风险已缓解</option><option value="emergency_continuity">保障业务连续性</option></select></label>
      <fieldset><legend>Compensating controls</legend><label class="platform-check"><input name="controls" type="checkbox" value="network_isolation"${disabled} /><span>Network isolation</span></label><label class="platform-check"><input name="controls" type="checkbox" value="runtime_detection"${disabled} /><span>Runtime detection</span></label><label class="platform-check"><input name="controls" type="checkbox" value="feature_disablement"${disabled} /><span>Feature disablement</span></label></fieldset>
      <label><span>Starts at</span><input name="startsAt" type="datetime-local" required${disabled} /></label>
      <label><span>Expires at</span><input name="expiresAt" type="datetime-local" required${disabled} /></label>
      <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled} /><span>Confirm bounded scope</span></label>
      <button type="submit"${disabled}>Request</button>
    </form>
  </details>` : "";
  return `<section class="platform-supply-chain-finding">
    <header><div><strong>${escapeHtml(item.advisory_code)}</strong><small>generation ${number(item.evaluation_generation)} · SLA ${formatTime(item.sla_deadline_at)}</small></div>${status(item.severity)}</header>
    <div class="platform-supply-chain-related"><div><h3>Exceptions</h3><ul>${exceptions}</ul></div><div><h3>Patches</h3><ul>${patches}</ul></div></div>
    ${requestForm}
  </section>`;
}

function developerSdkWithdrawForm(item, loading) {
  const disabled = loading ? " disabled" : "";
  return `<form data-platform-developer-sdk-withdraw-form class="platform-developer-contract-inline-form">
    <input name="sdkReleaseId" type="hidden" value="${escapeAttribute(item.sdk_release_id)}" />
    <input name="expectedRevision" type="hidden" value="${number(item.revision)}" />
    <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled} /><span>Confirm withdrawal</span></label>
    <button type="submit"${disabled}>Withdraw</button>
  </form>`;
}

function developerDeprecationCreateForm(versions, loading) {
  const disabled = loading ? " disabled" : "";
  const options = versions.map((item) => `<option value="${escapeAttribute(item.api_version_id)}">${escapeHtml(item.version)}</option>`).join("");
  return `<form data-platform-developer-deprecation-create-form class="platform-developer-deprecation-form">
    <label><span>Deprecated API</span><select name="apiVersionId" required${disabled}>${options}</select></label>
    <label><span>Replacement API</span><select name="replacementApiVersionId" required${disabled}>${options}</select></label>
    <label><span>Migration guide</span><input name="migrationGuideCode" value="api-migration-guide" pattern="[a-z0-9][a-z0-9._-]{2,79}" required${disabled} /></label>
    <label><span>Notice days</span><input name="minimumNoticeDays" type="number" min="90" max="730" value="90" required${disabled} /></label>
    <label><span>Sunset at</span><input name="sunsetAt" type="datetime-local" required${disabled} /></label>
    <label class="platform-developer-contract-operations"><span>Affected public operations</span><textarea name="affectedOperations" rows="3" placeholder="developer.api_versions.list" required${disabled}></textarea></label>
    <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled} /><span>Confirm notice draft</span></label>
    <button type="submit"${disabled}>Create notice</button>
  </form>`;
}

function developerDeprecationTable(notices, canManage, canApprove, loading) {
  return `<div class="platform-table-wrap"><table class="platform-developer-contract-table">
    <thead><tr><th>Migration</th><th>Notice</th><th>Watermarks</th><th>Deadline</th><th>Action</th></tr></thead>
    <tbody>${notices.map((item) => `<tr>
      <td data-label="Migration"><strong>${escapeHtml(item.api_version)} to ${escapeHtml(item.replacement_api_version)}</strong><small><code>${escapeHtml(item.migration_guide_code)}</code></small></td>
      <td data-label="Notice">${status(item.status)}<small>${number(item.minimum_notice_days)} days · ${number(item.affected_operations.length)} operations</small></td>
      <td data-label="Watermarks"><span>notifications ${number(item.notification_watermark)}</span><small>adoption ${number(item.adoption_watermark)}</small></td>
      <td data-label="Deadline">${formatTime(item.sunset_at)}<small>revision ${number(item.revision)}</small></td>
      <td data-label="Action">${developerDeprecationActions(item, canManage, canApprove, loading)}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function developerDeprecationActions(item, canManage, canApprove, loading) {
  const actions = [];
  if (item.status === "draft" && canApprove) actions.push(["announce", "Announce"]);
  if (new Set(["announced", "sunset_scheduled"]).has(item.status) && canManage) {
    actions.push(["withdraw", "Withdraw"]);
  }
  if (item.status === "sunset_scheduled" && canApprove) actions.push(["sunset", "Sunset"]);
  if (!actions.length) return '<span class="platform-restricted">无需操作</span>';
  return `<div class="platform-developer-contract-actions">${actions.map(([action, label]) => {
    const disabled = loading ? " disabled" : "";
    const gates = action === "sunset" ? `
      <label><span>Notifications</span><input name="notificationWatermark" type="number" min="1" required${disabled} /></label>
      <label><span>Adoption</span><input name="adoptionWatermark" type="number" min="0" required${disabled} /></label>` : "";
    return `<form data-platform-developer-deprecation-action-form class="platform-developer-contract-inline-form">
      <input name="noticeId" type="hidden" value="${escapeAttribute(item.deprecation_notice_id)}" />
      <input name="expectedRevision" type="hidden" value="${number(item.revision)}" />
      <input name="action" type="hidden" value="${escapeAttribute(action)}" />
      ${gates}
      <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled} /><span>Confirm ${escapeHtml(action)}</span></label>
      <button type="submit"${disabled}>${escapeHtml(label)}</button>
    </form>`;
  }).join("")}</div>`;
}

function developerLanguage(value) {
  return value === "javascript" ? "JavaScript" : value === "python" ? "Python" : value;
}

function privacyView(state) {
  const items = Array.isArray(state.privacyQueue?.items) ? state.privacyQueue.items : [];
  const active = items.filter((item) => new Set([
    "received", "identity_verified", "collecting", "reviewing", "ready",
  ]).has(item.status));
  const warnings = active.filter((item) => item.warning_sent).length;
  const overdue = active.filter((item) => Date.parse(item.deadline_at) < Date.now()).length;
  return `
    <header class="platform-page-heading">
      <div><p>隐私处理</p><h1>隐私请求队列</h1></div>
      <button class="platform-icon-button" type="button" data-platform-action="privacy-refresh"
        title="刷新隐私请求队列" aria-label="刷新隐私请求队列" ${state.loading ? "disabled" : ""}>&#8635;</button>
    </header>
    <section class="platform-metrics platform-privacy-metrics" aria-label="隐私请求队列摘要">
      ${metric("Active", active.length, "open requests")}
      ${metric("Deadline warning", warnings, "warning dispatched")}
      ${metric("Overdue", overdue, "past legal deadline")}
      ${metric("Queue", items.length, "minimal projections")}
    </section>
    <section class="platform-band platform-privacy-queue">
      <div class="platform-section-heading"><div><p>最小化处理视图</p><h2>分域处理进度</h2></div><span>${number(items.length)} 项</span></div>
      <div class="platform-table-wrap"><table class="platform-privacy-table">
        <thead><tr><th>请求</th><th>状态</th><th>期限</th><th>分域进度</th><th>法律结果</th></tr></thead>
        <tbody>${items.map(privacyQueueRow).join("") || emptyRow("当前没有隐私请求", 5)}</tbody>
      </table></div>
    </section>
  `;
}

function privacyQueueRow(item) {
  const completed = item.domain_progress.filter((entry) => entry.status === "completed").length;
  const failed = item.domain_progress.filter((entry) => entry.status === "failed").length;
  const legal = item.legal_outcomes.length
    ? [...new Set(item.legal_outcomes)].map((outcome) => status(outcome)).join("")
    : '<span class="platform-restricted">无</span>';
  return `<tr>
    <td data-label="请求"><strong><code>${escapeHtml(item.request_code)}</code></strong><small>${escapeHtml(privacyKindLabel(item.request_kind))}</small></td>
    <td data-label="状态">${status(item.status)}${item.warning_sent ? '<small class="platform-privacy-warning">Deadline warning</small>' : ""}</td>
    <td data-label="期限"><strong>${formatTime(item.deadline_at)}</strong><small>收到 ${formatTime(item.received_at)}</small></td>
    <td data-label="分域进度"><strong>${number(completed)} / ${number(item.domain_progress.length)}</strong><small>${failed ? `${number(failed)} failed` : privacyActiveDomains(item.domain_progress)}</small></td>
    <td data-label="法律结果"><div class="platform-privacy-outcomes">${legal}</div></td>
  </tr>`;
}

function privacyActiveDomains(items) {
  const active = items.filter((item) => new Set(["pending", "running"]).has(item.status));
  return active.length ? active.map((item) => privacyOwnerLabel(item.owner_domain)).join(" · ") : "all terminal";
}

function privacyKindLabel(value) {
  return ({ export: "数据导出", rectify: "数据更正", erase: "数据删除" })[value] ?? value;
}

function privacyOwnerLabel(value) {
  return ({ identity: "Identity", membership: "Membership", audit: "Audit", billing: "Billing", support: "Support", edge: "Edge", data_governance: "Governance" })[value] ?? value;
}

function domainPlaceholderView(state) {
  const item = (state.navigation ?? []).find((candidate) => candidate.id === state.view);
  return `
    <header class="platform-page-heading"><div><p>平台域</p><h1>${escapeHtml(item?.label ?? "平台域")}</h1></div></header>
    <section class="platform-band platform-domain-empty" role="status">
      <strong>暂无数据</strong>
      <p>当前职责域还没有可验证的记录。返回平台总览查看待处理事项，或稍后刷新本页。</p>
      <button type="button" class="platform-secondary" data-platform-view="overview">返回平台总览</button>
    </section>`;
}

function pendingConfirmation(state) {
  if (!state.pendingAction) return "";
  if (/^workspace-(?:suspend|restore)$/u.test(state.pendingAction.kind)) {
    const suspend = state.pendingAction.kind === "workspace-suspend";
    const reasons = suspend
      ? [["security_containment", "Security containment"], ["terms_enforcement", "Terms enforcement"], ["operational_protection", "Operational protection"]]
      : [["security_cleared", "Security cleared"], ["terms_resolved", "Terms resolved"], ["operational_recovery", "Operational recovery"]];
    return `<section class="platform-confirm platform-lifecycle-confirm" role="alert"><div><strong>${suspend ? "确认冻结组织" : "确认恢复组织"}</strong><span>${escapeHtml(state.pendingAction.id)} · Rev ${number(state.pendingAction.revision)}</span></div><form data-workspace-lifecycle-form autocomplete="off"><label><span>原因</span><select name="reasonCode">${filterOptions("", reasons)}</select></label><button type="button" data-platform-action="cancel-confirm">取消</button><button class="${suspend ? "platform-danger" : "platform-primary"}" type="submit">${suspend ? "确认冻结" : "确认恢复"}</button></form></section>`;
  }
  return `<section class="platform-confirm" role="alert"><div><strong>撤销支持授权</strong><span>${escapeHtml(state.pendingAction.id)}</span></div><div><button type="button" data-platform-action="cancel-confirm">取消</button><button class="platform-danger" type="button" data-platform-action="confirm-revoke" data-grant-id="${escapeAttribute(state.pendingAction.id)}">确认撤销</button></div></section>`;
}

function feedback(state) {
  if (state.error) {
    const conflict = /platform\.workspace_(?:revision|risk)_conflict/u.test(state.error.code ?? "")
      ? "状态已被其他操作更新，请刷新后重试。"
      : "";
    const message = state.error.code === "platform.permission_denied"
      ? "当前账号没有平台管理权限，请使用 SaaS 管理员账号登录。"
      : state.error.message;
    return `<div class="platform-feedback is-error" role="alert"><strong>${escapeHtml(message)}</strong>${conflict ? `<span>${escapeHtml(conflict)}</span>` : ""}${state.error.requestId ? `<details><summary>诊断信息</summary><span>请求编号 ${escapeHtml(state.error.requestId)}</span></details>` : ""}</div>`;
  }
  if (state.loading) return '<div class="platform-feedback" role="status">正在刷新…</div>';
  if (state.feedback) return `<div class="platform-feedback is-ok" role="status">${escapeHtml(state.feedback)}</div>`;
  return "";
}

function renderNavigation(items, current, disabled) {
  const remaining = new Map(items.map((item) => [item.id, item]));
  const groups = NAVIGATION_GROUPS.map(([label, symbol, entries]) => {
    const links = entries.flatMap(([id, name]) => {
      const item = remaining.get(id);
      if (!item) return [];
      remaining.delete(id);
      return [navItem({ ...item, label: name }, current, disabled, symbol)];
    });
    if (!links.length) return "";
    const isCurrentGroup = entries.some(([id]) => id === current);
    return `<details class="platform-nav-group"${isCurrentGroup ? " open" : ""}>
      <summary>${icon(symbol)}<span>${label}</span><span class="platform-nav-chevron" aria-hidden="true">⌄</span></summary>
      <div class="platform-nav-group-items">${links.join("")}</div>
    </details>`;
  });
  const leftovers = [...remaining.values()].map((item) => navItem(item, current, disabled)).join("");
  return groups.join("") + (leftovers ? `<details class="platform-nav-group" open><summary>${icon("layers")}<span>其他</span><span class="platform-nav-chevron" aria-hidden="true">⌄</span></summary><div class="platform-nav-group-items">${leftovers}</div></details>` : "");
}

function navItem(item, current, disabled = false, symbol = "layers") {
  const view = item.id;
  return `<button type="button" data-platform-view="${escapeAttribute(view)}" class="${view === current ? "is-active" : ""}" ${view === current ? 'aria-current="page"' : ""} ${disabled ? "disabled" : ""}>${icon(symbol)}<span>${escapeHtml(item.label)}</span></button>`;
}

function metric(label, value, detail) {
  return `<div><span>${escapeHtml(label)}</span><strong>${number(value)}</strong><small>${escapeHtml(detail)}</small></div>`;
}

function status(value) {
  const text = String(value ?? "unknown");
  const kind = /(?:^|_)(?:revoked|expired|disabled|critical|suspended|deleted|conflict|unavailable|unhealthy|failed|fenced|blocked|violation)(?:$|_)/u.test(text)
    ? "is-danger"
    : /(?:^|_)(?:pending|watch|stale|at_risk|not_ready|not_verified|not_configured|inactive|migration_required)(?:$|_)/u.test(text)
      ? "is-warn"
      : /(?:^|_)(?:active|healthy|ready|reviewed|fresh|verified|compliant)(?:$|_)/u.test(text)
        ? "is-ok"
        : "";
  return `<span class="platform-status ${kind}" data-status-code="${escapeAttribute(text)}">${escapeHtml(statusLabel(text))}</span>`;
}

function roleLabel(value) { return ({ platform_owner: "平台负责人", platform_ops: "运营管理员", platform_security: "安全管理员", platform_billing: "账务管理员", platform_support: "支持专员", platform_verification: "认证审核员", platform_compliance: "合规管理员", platform_privacy: "隐私管理员", release_manager: "发布管理员", platform_provider_engineer: "供应商工程师", platform_channel: "渠道管理员", platform_customer_success: "客户成功经理", platform_growth: "增长运营", platform_communications: "通知管理员" })[value] ?? String(value ?? "").replaceAll("_", " "); }
function domainLabel(value) { return value === "workspace" ? "客户" : value === "platform" ? "平台" : value; }
function statusLabel(value) {
  const text = String(value ?? "unknown");
  return ({
    active: "活跃",
    published: "已发布",
    draft: "草稿",
    available: "可用",
    healthy: "健康",
    ready: "就绪",
    not_ready: "未就绪",
    not_verified: "未验证",
    inactive: "未启用",
    unhealthy: "不健康",
    pending: "待处理",
    pending_approval: "待审批",
    pending_approvals: "待审批",
    connected: "已连接",
    operational: "正常",
    disconnected: "未连接",
    offline: "离线",
    degraded: "需关注",
    critical: "严重",
    unknown: "未知",
    not_configured: "未配置",
    unavailable: "不可用",
    failed: "失败",
    succeeded: "已完成",
    running: "运行中",
    queued: "排队中",
    disabled: "已停用",
    revoked: "已吊销",
    retired: "已退役",
    stale: "已过期",
    fresh: "最新",
    at_risk: "有风险",
    objective_met: "达到目标",
    objective_missed: "未达到目标",
    threshold_reached: "已达阈值",
    operator_kill: "人工停止",
    stopping: "停止中",
    hold: "保持现状",
    scale_out: "扩容",
    scale_in: "缩容",
    investigate: "需要调查",
  })[text] ?? text.replaceAll("_", " ");
}
function riskStatusOptions(current) { return [["", "全部"], ["open", "待处理"], ["under_review", "复核中"], ["restricted", "已限制"], ["cleared", "已清除"], ["closed", "已关闭"]].map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join(""); }
function billingStatusLabel(value) { return ({ open: "进行中", closed: "已关账", reconciliation_required: "需要对账" })[value] ?? statusLabel(value); }
function billingStatusOptions(current) { return [["open", "进行中"], ["closed", "已关账"], ["reconciliation_required", "需要对账"]].map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join(""); }
function refundStatusLabel(value) { return ({ requested: "待审核", processing: "处理中", succeeded: "已退款", failed: "失败", rejected: "已拒绝" })[value] ?? statusLabel(value); }
function refundStatusOptions(current) { return [["requested", "待审核"], ["processing", "处理中"], ["succeeded", "已退款"], ["failed", "失败"], ["rejected", "已拒绝"]].map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join(""); }
function refundReasonLabel(value) { return ({ customer_request: "客户申请", service_failure: "服务故障", duplicate_charge: "重复扣款" })[value] ?? statusLabel(value); }
function differenceTypeLabel(value) { return ({ order_payment_amount_mismatch: "订单与支付金额不一致", paid_order_missing_capture: "已支付订单缺少扣款", closed_cycle_missing_statement: "关账周期缺少结算单", meter_summary_watermark_mismatch: "Meter 摘要水位不一致", issued_invoice_missing_artifact: "已开票但缺少产物" })[value] ?? "未知差异"; }
function sourceTypeLabel(value) { return ({ order: "订单", billing_cycle: "账单周期", meter_summary: "Meter 摘要", invoice_request: "发票申请", payment_intent: "支付意图", billing_statement: "结算单", meter_reading: "Meter 读数", invoice_artifact: "发票产物" })[value] ?? "无"; }
function reversalStatusLabel(value) { return ({ pending_approvals: "等待账务与合规审批", queued: "已进入 Provider 队列", completed: "冲红完成", rejected: "已拒绝" })[value] ?? statusLabel(value); }
function reversalReasonLabel(value) { return ({ invoice_error: "开票错误", customer_correction: "客户资料更正", contract_canceled: "合同取消" })[value] ?? statusLabel(value); }
function approvalDomainLabel(value) { return value === "billing" ? "账务" : value === "compliance" ? "合规" : "未知"; }
function formatMinorMoney(value, currency) { const amount = Number(value); return Number.isSafeInteger(amount) ? new Intl.NumberFormat("zh-CN", { style: "currency", currency: new Set(["CNY", "USD"]).has(currency) ? currency : "CNY", currencyDisplay: "code" }).format(amount / 100) : "-"; }
/** 构造审核状态筛选选项，选择值只能来自后端固定枚举。 */
function verificationStatusOptions(current) { return [["submitted", "待审核"], ["in_review", "审核中"], ["approved", "已通过"], ["rejected", "已拒绝"], ["expired", "已过期"]].map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join(""); }
/** 将主体类型映射为安全文案，不显示法定名称或登记号。 */
function entityTypeLabel(value) { return ({ company: "企业", nonprofit: "非营利组织", government: "政府机构" })[value] ?? "未知主体"; }
/** 将证据类型映射为固定标签，不显示文件名和受控对象引用。 */
function evidenceTypeLabel(value) { return ({ business_license: "营业执照", registration_certificate: "登记证书", authorization_letter: "授权文件" })[value] ?? "证据"; }
/** 汇总证据扫描结论，Platform 列表不暴露各扫描器内部错误。 */
function scanStateLabel(value) {
  if (!value || !value.evidence_count) return "无证据";
  if (value.malware_status === "passed" && value.dlp_status === "passed") return "已通过";
  if (value.malware_status === "blocked" || value.dlp_status === "blocked") return "已阻断";
  if (value.malware_status === "pending" || value.dlp_status === "pending") return "扫描中";
  return "扫描失败";
}
function meterLabel(value) { return ({ api_requests: "API 请求", input_tokens: "输入 Token", output_tokens: "输出 Token", total_tokens: "总 Token" })[value] ?? statusLabel(value); }
function actionLabel(value) {
  const normalized = String(value ?? "").replaceAll("_", " ").replaceAll(".", " ").replaceAll("/", " ").replace(/\s+/g, " ").trim().toLowerCase();
  const labels = {
    "platform session issued": "登录成功",
    "session issued": "登录成功",
    "session revoked": "登录失效",
    "status read": "查看服务状态",
    "status updated": "更新服务状态",
    "workspace created": "创建组织",
    "workspace updated": "更新组织",
    "billing adjusted": "调整账务",
  };
  return labels[normalized] ?? (normalized || "操作");
}

function auditObjectLabel(value) {
  return ({
    platform_session: "平台登录",
    status_projection: "服务状态",
    workspace: "组织",
    workspace_member: "组织成员",
    billing_cycle: "账务周期",
    gateway: "网关",
  })[value] ?? String(value ?? "系统对象");
}

function auditReasonLabel(value) {
  return ({
    mfa_verified: "已完成安全验证",
    public_communication: "公开状态更新",
    user_action: "用户操作",
    system: "系统操作",
  })[value] ?? (value ? String(value).replaceAll("_", " ") : "无附加说明");
}
function kindLabel(value) { return value === "organization" ? "组织" : value; }
function infrastructureKindLabel(value) { return ({ database_cluster: "数据库集群", cache_cluster: "缓存集群", object_store: "对象存储", key_ring: "密钥环" })[value] ?? statusLabel(value); }
function shortHash(value) { const text = String(value ?? ""); return text.length === 64 ? `${text.slice(0, 12)}…${text.slice(-8)}` : "-"; }
function emptyRow(label, columns = 5) { return `<tr><td colspan="${number(columns)}" class="platform-empty"><span class="platform-empty-label">${escapeHtml(label)}</span></td></tr>`; }
function number(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0"; }
function signedNumber(value) { const amount = Number(value); return `${amount > 0 ? "+" : ""}${number(amount)}`; }
function formatDate(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date); }
function formatTime(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date); }
function formatOptionalTime(value) { return value ? formatTime(value) : "-"; }
function shortWorkspace(value) { const text = String(value ?? ""); return text.length > 16 ? `${text.slice(0, 12)}…${text.slice(-4)}` : text; }
function shortReference(value) { const text = String(value ?? ""); return text.length > 20 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text || "-"; }
function fact(label, value) { return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`; }
function filterOptions(current, options) { return options.map(([value, label]) => `<option value="${escapeAttribute(value)}" ${value === current ? "selected" : ""}>${escapeHtml(label)}</option>`).join(""); }
function riskTagList(tags) { return tags.length ? `<div class="platform-tag-list">${tags.map((tag) => `<span>${escapeHtml(statusLabel(tag))}</span>`).join("")}</div>` : '<span class="platform-restricted">无</span>'; }
function allowedRiskOptions(permissions) {
  const options = [];
  if (permissions.has("platform.workspace.risk.manage")) options.push(["security_review", "安全复核"], ["compliance_hold", "Compliance hold"]);
  if (permissions.has("platform.customer_success.read")) options.push(["payment_attention", "账务待跟进"], ["adoption_attention", "使用情况待改善"]);
  return options;
}
function riskReason(tag) { return ({ security_review: "security_review", compliance_hold: "compliance_review", payment_attention: "payment_follow_up", adoption_attention: "adoption_follow_up" })[tag] ?? "security_review"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value); }
