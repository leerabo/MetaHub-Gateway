const LABELS = Object.freeze({
  "approval.security.pending": "安全事项待审批",
  "security.review": "安全复核",
  approvals: "审批",
  billing: "账务",
  governance: "治理",
  "health.healthy": "运行正常",
  "health.degraded": "运行受限",
  "health.offline": "当前离线",
  "health.revoked": "已撤销",
  "health.unknown": "状态待确认",
  provider: "Provider",
  "provider-health": "Provider 健康",
  security: "安全",
  status: "状态",
  observability: "可观测性",
  jobs: "平台任务",
  capacity: "服务容量",
  channel: "渠道结算",
  customer_success: "客户成功",
  growth: "增长分析",
  branding: "品牌管理",
  provider_engineering: "供应商工程",
  commercial_accounting: "商业账务",
  role_tasks: "空间成员",
  gateway_health: "Gateway 部署",
  provider_health: "Provider 探测",
  usage_cost: "用量统计",
  notifications: "通知",
  deployment: "部署",
  "status.records": "状态组件数",
  "observability.records": "服务目标数",
  "jobs.records": "平台任务数",
  "capacity.records": "容量服务记录数",
  "channel.records": "渠道结算单数",
  "customer_success.records": "客户账号记录数",
  "growth.records": "增长漏斗快照数",
  "branding.records": "品牌数",
  "provider_engineering.records": "适配器发布数",
  "commercial_accounting.records": "平台发票数",
  "role_tasks.records": "空间成员记录数",
  "gateway_health.records": "Gateway 部署数",
  "provider_health.records": "探测记录数",
  "governance.records": "DLP 规则数",
  "approvals.records": "审批事项数",
  "usage_cost.records": "用量记录数",
  "notifications.records": "通知数",
  "deployment.records": "部署记录数",
  "assistant.answer.unsupported": "暂不支持此类查询",
  "assistant.answer.ready": "查询结果",
  "assistant.answer.partial": "部分查询结果",
  ready: "已有记录",
  empty: "暂无记录",
  unavailable: "来源不可用",
  unknown: "待确认",
});

export function renderConsoleExperience(state, {
  domain,
  view,
  resolveCitationNavigation,
  domainLabel = domain === "platform" ? "平台" : "Workspace",
  hiddenOverviewSources = [],
  collapseEmptyOverview = false,
} = {}) {
  if (view === "overview") return renderOverview(state, domainLabel, {
    hiddenSources: hiddenOverviewSources,
    collapseEmpty: collapseEmptyOverview,
  });
  if (view === "admin-assistant") return renderAssistant(state, domainLabel, domain, resolveCitationNavigation);
  if (view === "approvals") return renderApprovals(state, domainLabel);
  if (view === "deployment-summary" && domain === "workspace") return renderDeployments(state);
  return "";
}

export function hydrateConsoleAssistantDraft(root, state) {
  const input = root?.querySelector?.("[data-console-experience-question]");
  if (!input) return false;
  input.value = String(state?.assistant?.draft ?? "");
  return true;
}

export function bindConsoleExperienceInteractions(root, handlers = {}) {
  const assistantForm = root?.querySelector?.("[data-console-experience-assistant-form]");
  if (assistantForm) {
    assistantForm.oninput = (event) => {
      if (event.target?.matches?.("[data-console-experience-question]")) {
        const accepted = handlers.onAssistantDraft?.(event.target.value);
        const submit = assistantForm.querySelector?.("button[type='submit']");
        if (accepted && submit) submit.disabled = !String(event.target.value).trim();
      }
    };
    assistantForm.onsubmit = async (event) => {
      event.preventDefault();
      await handlers.onAssistantSubmit?.();
    };
  }
  const clear = root?.querySelector?.("[data-console-experience-action='clear-assistant']");
  if (clear) clear.onclick = () => handlers.onAssistantClear?.();
  const filter = root?.querySelector?.("[data-console-experience-approval-filter]");
  if (filter) filter.onsubmit = async (event) => {
    event.preventDefault();
    const data = new FormData(filter);
    await handlers.onApprovalFilter?.({
      status: String(data.get("status") ?? ""),
      risk: String(data.get("risk") ?? ""),
      sourceDomain: String(data.get("sourceDomain") ?? ""),
    });
  };
  const previous = root?.querySelector?.("[data-console-experience-action='previous-approvals']");
  if (previous) previous.onclick = async () => handlers.onApprovalPrevious?.();
  const next = root?.querySelector?.("[data-console-experience-action='next-approvals']");
  if (next) next.onclick = async () => handlers.onApprovalNext?.();
  const refresh = root?.querySelector?.("[data-console-experience-action='refresh']");
  if (refresh) refresh.onclick = async () => handlers.onRefresh?.();
  return Boolean(assistantForm || filter || refresh);
}

function renderOverview(state, domainLabel, { hiddenSources = [], collapseEmpty = false } = {}) {
  const overview = state?.overview;
  const status = state?.status ?? "idle";
  if (!overview) return shellHeading(domainLabel, "总览", stateBlock(status, state?.error));
  const hidden = new Set(hiddenSources.map((value) => String(value)));
  const candidateSections = (overview.sections ?? []).filter((section) => !hidden.has(String(section?.source_code ?? "")));
  const sectionIsEmpty = (section) => {
    const metrics = Array.isArray(section?.metrics) ? section.metrics : [];
    return metrics.length > 0 && metrics.every((metric) => metric?.value === null || Number(metric?.value) === 0);
  };
  const renderSection = (section) => `
    <section class="console-experience-section" data-freshness="${escapeAttribute(section.freshness)}" data-source-revision="${escapeAttribute(section.source_revision)}">
      <div class="console-experience-section-heading">
        <div><p>${escapeHtml(label(section.source_code))}</p><h2>${sectionFreshnessLabel(section)}</h2></div>
      </div>
      <div class="console-experience-metrics">
        ${section.metrics.map((metric) => `
          <article>
            <span>${escapeHtml(label(metric.metric_code))}</span>
            <strong>${metric.value === null ? "-" : Number(metric.value).toLocaleString("zh-CN")}</strong>
            <small>${escapeHtml(label(metric.reason_code ?? metric.state_code))}</small>
          </article>
        `).join("")}
      </div>
    </section>
  `;
  const populatedSections = collapseEmpty ? candidateSections.filter((section) => !sectionIsEmpty(section)) : candidateSections;
  const emptySections = collapseEmpty ? candidateSections.filter(sectionIsEmpty) : [];
  const sections = populatedSections.map(renderSection).join("")
    + (emptySections.length ? `
      <details class="console-experience-empty-sections">
        <summary>暂无数据的区域（${emptySections.length}）</summary>
        <div class="console-experience-empty-section-list">${emptySections.map(renderSection).join("")}</div>
      </details>` : "");
  return `
    <header class="console-experience-heading">
      <div><p>${escapeHtml(domainLabel)}运营</p><h1>${escapeHtml(domainLabel)}总览</h1></div>
      <div class="console-experience-heading-actions">
        <span class="console-experience-status is-${escapeAttribute(overview.status)}">${overviewStatus(overview.status)}</span>
        <button type="button" data-console-experience-action="refresh">刷新</button>
      </div>
    </header>
    ${stateBlock(status, state?.error)}
    <div class="console-experience-section-list${populatedSections.length <= 1 ? " is-single-section" : ""}">${sections || empty("当前没有可用摘要。")}</div>
  `;
}

function renderAssistant(state, domainLabel, domain, resolveCitationNavigation = (value) => value) {
  const response = state?.assistant?.response;
  return `
    <header class="console-experience-heading">
      <div><p>${escapeHtml(domainLabel)}数据域</p><h1>${escapeHtml(domainLabel)}管理助手</h1></div>
      <span class="console-experience-status">单轮查询</span>
    </header>
    ${stateBlock(state?.status, state?.error)}
    <form class="console-experience-assistant" data-console-experience-assistant-form>
      <label for="${domain}-console-question">问题</label>
      <p class="console-experience-help">这里填写你想了解的运营问题，使用自然语言即可。管理助手只为当前管理员提供只读摘要，例如“过去 7 天调用量如何？”、“最近有哪些错误？”或“哪里可以降低成本？”。</p>
      <textarea id="${domain}-console-question" data-console-experience-question
        maxlength="2000" rows="4" autocomplete="off" spellcheck="false"></textarea>
      <div class="console-experience-actions">
        <button type="submit" ${state?.assistant?.canSubmit ? "" : "disabled"}>查询</button>
        <button type="button" data-console-experience-action="clear-assistant"
          ${state?.busy ? "disabled" : ""}>清空</button>
      </div>
    </form>
    ${response ? `
      <section class="console-experience-answer" aria-live="polite">
        <div class="console-experience-section-heading">
          <div><p>${queryTypeLabel(response.query_type)}</p><h2>${escapeHtml(label(response.answer_code))}</h2></div>
          <span>${outcomeLabel(response.outcome)}</span>
        </div>
        <dl class="console-experience-claims">
          ${response.claims.map((claim) => `
            <div><dt>${escapeHtml(label(claim.claim_code))}</dt>
              <dd>${claim.value === null ? escapeHtml(label(claim.state_code)) : Number(claim.value).toLocaleString("zh-CN")}</dd></div>
          `).join("") || '<div><dt>结果</dt><dd>没有可展示的聚合事实</dd></div>'}
        </dl>
        <nav class="console-experience-citations" aria-label="引用来源">
          ${response.citations.map((citation) => {
            const target = resolveCitationNavigation(citation.navigation_id);
            const title = escapeHtml(label(citation.source_type));
            return target
              ? `<a href="#${encodeURIComponent(target)}" data-console-experience-citation="${escapeAttribute(target)}">${title}</a>`
              : `<span>${title}</span>`;
          }).join("")}
        </nav>
      </section>
    ` : ""}
  `;
}

function renderApprovals(state, domainLabel) {
  const items = state?.approvals?.items ?? [];
  const filters = state?.approvalFilters ?? {};
  const workspace = domainLabel !== "平台";
  return `
    <header class="console-experience-heading">
      <div>${workspace ? "<p>系统生成的审批记录</p>" : `<p>${escapeHtml(domainLabel)}事务</p><h1>${escapeHtml(domainLabel)}审批队列</h1>`}</div>
      <button type="button" data-console-experience-action="refresh">刷新</button>
    </header>
    ${workspace ? `<p class="console-experience-help">只有受信服务触发预算解封、会话风险处置等操作时，系统才会生成记录。
      目前没有员工手动发起申请的入口；此页仅查看状态，暂不提供同意或驳回操作。</p>` : ""}
    ${stateBlock(state?.status, state?.error)}
    <form class="console-experience-filter" data-console-experience-approval-filter>
      <label><span>状态</span><select name="status">
        ${options([["", "全部"], ["pending", "待处理"], ["approved", "已批准"], ["rejected", "已拒绝"], ["expired", "已过期"]], filters.status)}
      </select></label>
      <label><span>风险</span><select name="risk">
        ${options([["", "全部"], ["standard", "标准"], ["high", "高"], ["critical", "关键"]], filters.risk)}
      </select></label>
      <label><span>事项类型</span><select name="sourceDomain">
        ${options(workspace
          ? [["", "全部"], ["budget", "预算解封"], ["session_risk", "会话风险"], ["mcp", "MCP 工具调用"], ["billing", "账务"], ["privacy", "隐私"], ["compliance", "合规"], ["security", "安全"]]
          : [["", "全部"]], filters.sourceDomain)}
      </select></label>
      <button type="submit" ${state?.busy ? "disabled" : ""}>应用筛选</button>
    </form>
    <div class="console-experience-table-wrap">
      <table>
        <thead><tr><th>事项</th><th>来源</th><th>风险</th><th>状态</th><th>截止时间</th></tr></thead>
        <tbody>${items.map((item) => `
          <tr>
            <td><strong>${escapeHtml(approvalItemTitle(item))}</strong><small>${escapeHtml(approvalActionLabel(item.decision_contract_code))}</small></td>
            <td>${escapeHtml(approvalSourceLabel(item.source_domain))}</td>
            <td><span class="console-experience-risk is-${escapeAttribute(item.risk_band)}">${riskLabel(item.risk_band)}</span></td>
            <td>${approvalStatus(item.status)}</td>
            <td>${formatTime(item.deadline_at)}</td>
          </tr>
        `).join("") || `<tr><td colspan="5">${workspace
          ? "当前没有符合条件的记录。系统触发需要确认的风险操作后，记录会显示在这里。"
          : "当前筛选条件下没有审批事项。"}</td></tr>`}</tbody>
      </table>
    </div>
    <div class="console-experience-pagination">
      <button type="button" data-console-experience-action="previous-approvals"
        ${state?.canPreviousApprovals ? "" : "disabled"} aria-label="上一页">&larr;</button>
      <span>第 ${Number(state?.approvalPage ?? 1)} 页</span>
      <button type="button" data-console-experience-action="next-approvals"
        ${state?.canNextApprovals ? "" : "disabled"} aria-label="下一页">&rarr;</button>
    </div>
  `;
}

function approvalSourceLabel(value) {
  return ({ budget: "预算", session_risk: "会话风险", mcp: "MCP 工具", billing: "账务",
    privacy: "隐私", compliance: "合规", security: "安全" })[value] ?? "其他事项";
}
function approvalItemTitle(item) {
  const base = ({ budget: "预算解封", session_risk: "会话风险处置", mcp: "MCP 工具调用" })[item.source_domain]
    ?? approvalSourceLabel(item.source_domain);
  return `${base} · ${approvalStatus(item.status)}`;
}
function approvalActionLabel(value) {
  return ({ "budget_block_release.release": "解除预算阻断", "mcp_tool.invoke": "执行工具",
    "session_risk_action.freeze_session": "冻结会话", "session_risk_action.freeze_virtual_key": "冻结调用 KEY",
    "session_risk_action.dismiss": "忽略风险", "session_risk_action.release_session": "恢复会话",
    "session_risk_action.release_virtual_key": "恢复调用 KEY" })[value] ?? "查看处理状态";
}

function renderDeployments(state) {
  const summary = state?.deploymentSummary;
  if (!summary) return shellHeading("Workspace", "部署诊断", stateBlock(state?.status, state?.error));
  return `
    <header class="console-experience-heading">
      <div><p>角色裁剪摘要</p><h1>Deployment 状态</h1></div>
      <button type="button" data-console-experience-action="refresh">刷新</button>
    </header>
    ${stateBlock(state?.status, state?.error)}
    <div class="console-experience-table-wrap">
      <table>
        <thead><tr><th>Deployment</th><th>区域</th><th>版本</th><th>兼容性</th><th>健康</th></tr></thead>
        <tbody>${summary.items.map((item) => `
          <tr>
            <td><code>${escapeHtml(shortRef(item.deployment_ref))}</code><small>${deploymentKind(item.deployment_kind)}</small></td>
            <td>${escapeHtml(item.region_code ?? "受限")}</td>
            <td>${releaseLabel(item.release_band)}<small>${generationLabel(item)}</small></td>
            <td>${compatibilityLabel(item.compatibility_band)}</td>
            <td>${healthLabel(item.health_state)}<small>${escapeHtml(label(item.reason_code))}</small></td>
          </tr>
        `).join("") || '<tr><td colspan="5">当前没有可展示的部署摘要。</td></tr>'}</tbody>
      </table>
    </div>
  `;
}

function shellHeading(domain, title, body) {
  return `<header class="console-experience-heading"><div><p>${escapeHtml(domain)}</p><h1>${escapeHtml(title)}</h1></div>
    <button type="button" data-console-experience-action="refresh">刷新</button></header>${body}`;
}
function stateBlock(status, error) {
  if (status === "loading") return '<p class="console-experience-state" role="status">正在读取当前页面...</p>';
  if (status === "permission-revoked") return '<p class="console-experience-state is-error" role="alert">权限已变化，正在重新验证。</p>';
  if (status === "conflict") return '<p class="console-experience-state is-error" role="alert">页面版本已变化，请刷新后继续。</p>';
  if (status === "dependency-unavailable") return '<p class="console-experience-state is-error" role="alert">依赖服务暂不可用，可稍后刷新恢复。</p>';
  if (status === "error") return `<p class="console-experience-state is-error" role="alert">${escapeHtml(error?.message ?? "页面请求未完成。")}</p>`;
  if (status === "partial") return '<p class="console-experience-state is-warning" role="status">部分来源暂不可用，已显示可验证结果。</p>';
  if (status === "stale") return '<p class="console-experience-state is-warning" role="status">当前为最近一次可用快照。</p>';
  return "";
}
function options(items, selected) { return items.map(([value, text]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${text}</option>`).join(""); }
function empty(text) { return `<p class="console-experience-empty">${escapeHtml(text)}</p>`; }
function label(value) { const text = String(value ?? ""); return Object.hasOwn(LABELS, text) ? LABELS[text] : "未识别事项"; }
function freshnessLabel(value) { return ({ fresh: "正常", stale: "最近更新", unavailable: "暂不可用" })[value] ?? "待确认"; }
function sectionFreshnessLabel(section) {
  const metrics = Array.isArray(section?.metrics) ? section.metrics : [];
  const empty = metrics.length > 0 && metrics.every((metric) => metric?.value === null || Number(metric?.value) === 0);
  return empty && section?.freshness === "fresh" ? "暂无记录" : freshnessLabel(section?.freshness);
}
function overviewStatus(value) { return ({ ready: "已就绪", partial: "部分可用", stale: "最近快照" })[value] ?? "状态待确认"; }
function queryTypeLabel(value) { return ({ operations_summary: "运营摘要", security_summary: "安全摘要", billing_summary: "账务摘要", provider_summary: "Provider 摘要", unknown: "未识别问题" })[value] ?? "查询结果"; }
function outcomeLabel(value) { return ({ answered: "已回答", partial: "部分回答", unsupported: "暂不支持" })[value] ?? "已完成"; }
function riskLabel(value) { return ({ standard: "标准", high: "高", critical: "关键" })[value] ?? "未知"; }
function approvalStatus(value) { return ({ pending: "待处理", approved: "已批准", rejected: "已拒绝", expired: "已过期" })[value] ?? "未知"; }
function deploymentKind(value) { return ({ connected: "联网部署", isolated: "隔离部署" })[value] ?? "类型受限"; }
function releaseLabel(value) { return ({ current: "当前版本", behind: "需要更新", unknown: "版本待确认" })[value] ?? "版本待确认"; }
function compatibilityLabel(value) { return ({ compatible: "兼容", attention: "需要关注", blocked: "已阻断", unknown: "待确认" })[value] ?? "待确认"; }
function healthLabel(value) { return ({ healthy: "正常", degraded: "受限", offline: "离线", revoked: "已撤销", unknown: "待确认" })[value] ?? "待确认"; }
function generationLabel(item) { return item.desired_generation === null || item.observed_generation === null ? "代次受限" : `${Number(item.observed_generation)} / ${Number(item.desired_generation)}`; }
function shortRef(value) { const text = String(value ?? ""); return text.length === 64 ? `${text.slice(0, 8)}...${text.slice(-6)}` : "-"; }
function formatTime(value) { const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "-"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]); }
function escapeAttribute(value) { return escapeHtml(value).replace(/\u0060/gu, "&#96;"); }
