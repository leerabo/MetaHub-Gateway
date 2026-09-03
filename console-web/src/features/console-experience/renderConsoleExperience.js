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
});

export function renderConsoleExperience(state, { domain, view } = {}) {
  const domainLabel = domain === "platform" ? "平台" : "Workspace";
  if (view === "overview") return renderOverview(state, domainLabel);
  if (view === "admin-assistant") return renderAssistant(state, domainLabel, domain);
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

function renderOverview(state, domainLabel) {
  const overview = state?.overview;
  const status = state?.status ?? "idle";
  if (!overview) return shellHeading(domainLabel, "总览", stateBlock(status, state?.error));
  const sections = overview.sections.map((section) => `
    <section class="console-experience-section" data-freshness="${escapeAttribute(section.freshness)}">
      <div class="console-experience-section-heading">
        <div><p>${escapeHtml(label(section.source_code))}</p><h2>${freshnessLabel(section.freshness)}</h2></div>
        <span>Revision ${Number(section.source_revision)}</span>
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
  `).join("");
  return `
    <header class="console-experience-heading">
      <div><p>${escapeHtml(domainLabel)}运营</p><h1>${escapeHtml(domainLabel)}总览</h1></div>
      <div class="console-experience-heading-actions">
        <span class="console-experience-status is-${escapeAttribute(overview.status)}">${overviewStatus(overview.status)}</span>
        <button type="button" data-console-experience-action="refresh">刷新</button>
      </div>
    </header>
    ${stateBlock(status, state?.error)}
    <div class="console-experience-section-list">${sections || empty("当前没有可用摘要。")}</div>
  `;
}

function renderAssistant(state, domainLabel, domain) {
  const response = state?.assistant?.response;
  return `
    <header class="console-experience-heading">
      <div><p>${escapeHtml(domainLabel)}数据域</p><h1>${escapeHtml(domainLabel)}管理助手</h1></div>
      <span class="console-experience-status">单轮查询</span>
    </header>
    ${stateBlock(state?.status, state?.error)}
    <form class="console-experience-assistant" data-console-experience-assistant-form>
      <label for="${domain}-console-question">问题</label>
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
          ${response.citations.map((citation) => `
            <a href="#${encodeURIComponent(citation.navigation_id)}"
              data-console-experience-citation="${escapeAttribute(citation.navigation_id)}">
              ${escapeHtml(label(citation.label_code))}
            </a>
          `).join("")}
        </nav>
      </section>
    ` : ""}
  `;
}

function renderApprovals(state, domainLabel) {
  const items = state?.approvals?.items ?? [];
  const filters = state?.approvalFilters ?? {};
  return `
    <header class="console-experience-heading">
      <div><p>${escapeHtml(domainLabel)}审批域</p><h1>${escapeHtml(domainLabel)}审批队列</h1></div>
      <button type="button" data-console-experience-action="refresh">刷新</button>
    </header>
    ${stateBlock(state?.status, state?.error)}
    <form class="console-experience-filter" data-console-experience-approval-filter>
      <label><span>状态</span><select name="status">
        ${options([["", "全部"], ["pending", "待处理"], ["approved", "已批准"], ["rejected", "已拒绝"], ["expired", "已过期"]], filters.status)}
      </select></label>
      <label><span>风险</span><select name="risk">
        ${options([["", "全部"], ["standard", "标准"], ["high", "高"], ["critical", "关键"]], filters.risk)}
      </select></label>
      <label><span>来源域</span><input name="sourceDomain" maxlength="80"
        value="${escapeAttribute(filters.sourceDomain ?? "")}" /></label>
      <button type="submit" ${state?.busy ? "disabled" : ""}>应用筛选</button>
    </form>
    <div class="console-experience-table-wrap">
      <table>
        <thead><tr><th>事项</th><th>来源</th><th>风险</th><th>状态</th><th>截止时间</th></tr></thead>
        <tbody>${items.map((item) => `
          <tr>
            <td><strong>${escapeHtml(label(item.summary_code))}</strong><small>${escapeHtml(label(item.decision_contract_code))}</small></td>
            <td>${escapeHtml(label(item.source_domain))}<small>${escapeHtml(label(item.source_type))} · r${Number(item.source_revision)}</small></td>
            <td><span class="console-experience-risk is-${escapeAttribute(item.risk_band)}">${riskLabel(item.risk_band)}</span></td>
            <td>${approvalStatus(item.status)}</td>
            <td>${formatTime(item.deadline_at)}</td>
          </tr>
        `).join("") || '<tr><td colspan="5">当前筛选条件下没有审批事项。</td></tr>'}</tbody>
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
function label(value) { const text = String(value ?? ""); return LABELS[text] ?? text.split(/[._-]/u).filter(Boolean).map((part) => part[0]?.toUpperCase() + part.slice(1)).join(" "); }
function freshnessLabel(value) { return ({ fresh: "数据已更新", stale: "最近快照", unavailable: "来源不可用" })[value] ?? "状态待确认"; }
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
