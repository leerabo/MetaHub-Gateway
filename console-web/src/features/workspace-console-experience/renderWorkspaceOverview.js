/** Only display facts supplied by the current authorized organization projection. */
export function renderWorkspaceOverview(state, { hiddenSources = [], roleCode = "", workspaceName = "" } = {}) {
  const overview = state?.overview;
  const sections = new Map((overview?.sections ?? [])
    .filter((section) => !hiddenSources.includes(section.source_code))
    .map((section) => [section.source_code, section]));
  const metric = (source, code) => sections.get(source)?.metrics?.find((item) => item.metric_code === code)?.value;
  const card = (source, code, title, note, route) => !sections.has(source) ? "" :
    `<a class="workspace-overview-card" href="#${route}"><span>${title}</span><strong>${formatCount(metric(source, code))}</strong><small>${note}</small></a>`;
  const period = ["day", "month", "all"].includes(state?.overviewPeriod) ? state.overviewPeriod : "month";
  const periodLabel = { day: "今日", month: "本月", all: "累计" }[period];
  const usage = (code) => metric("usage_cost", `usage_cost.${period}_${code}`);
  const updated = overview?.observed_at && Number.isFinite(Date.parse(overview.observed_at))
    ? new Date(overview.observed_at).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }) : "";
  const isManager = ["owner", "admin"].includes(roleCode);
  const requestRoute = isManager ? "workspace.observability.requests" : "workspace.finance.usage";
  const statusMessage = state?.status === "loading" ? "正在读取组织概览…"
    : ["partial", "stale"].includes(state?.status) ? "部分数据暂未更新，显示最近一次可用结果。"
      : state?.status === "ready" ? "" : state?.error?.message || "当前无法读取组织概览，请刷新重试。";
  const attention = [
    [usage("failures"), `${periodLabel}失败请求`, requestRoute],
    [metric("provider_health", "provider_health.needs_check"), "待检查模型服务", "workspace.gateway.provider-health"],
  ].filter(([value]) => Number(value) > 0);
  return `<div class="workspace-overview">
    <header class="workspace-overview-header">
      <div><h2 class="workspace-overview-name">${escapeHtml(workspaceName || "组织空间")}</h2><span>组织规模与模型使用情况</span></div>
      <div class="workspace-overview-header-actions">${updated ? `<small>更新于 ${escapeHtml(updated)}</small>` : ""}
        <button type="button" data-console-experience-action="refresh">刷新</button></div>
    </header>
    ${statusMessage ? `<p class="console-experience-state" role="status">${escapeHtml(statusMessage)}</p>` : ""}
    ${overview ? `
      ${sections.has("role_tasks") || sections.has("provider_health") ? `<section aria-label="组织规模"><div class="workspace-overview-cards">
        ${card("role_tasks", "role_tasks.records", "成员人数", "当前在组织中的成员", "workspace.settings.members")}
        ${card("role_tasks", "role_tasks.projects", "项目", "未归档项目", "workspace.gateway.projects")}
        ${card("role_tasks", "role_tasks.applications", "应用", "未归档应用", "workspace.gateway.projects")}
        ${card("provider_health", "provider_health.models", "大模型", `已启用 ${formatCount(metric("provider_health", "provider_health.active_models"))} 个`, "workspace.gateway.models")}
      </div></section>` : ""}
      ${sections.has("usage_cost") ? `<section aria-labelledby="workspace-overview-usage-title">
        <div class="workspace-overview-section-title"><h2 id="workspace-overview-usage-title">用量与消费</h2>
          <div class="workspace-overview-period" role="group" aria-label="统计时间范围">
            ${Object.entries({ day: "今日", month: "本月", all: "累计" }).map(([value, label]) =>
              `<button type="button" data-workspace-overview-period="${value}" aria-pressed="${period === value}">${label}</button>`).join("")}
          </div></div>
        <div class="workspace-overview-cards">
          ${card("usage_cost", `usage_cost.${period}_requests`, "请求次数", `${periodLabel}成功和失败请求`, requestRoute)}
          ${card("usage_cost", `usage_cost.${period}_tokens`, "Token 使用量", `${periodLabel}已入账用量`, "workspace.finance.usage")}
          <a class="workspace-overview-card" href="#workspace.finance.usage"><span>人民币消费</span><strong class="is-money">${formatMoney(usage("cny_micros"), "CNY")}</strong><small>${periodLabel}已入账调用成本</small></a>
          <a class="workspace-overview-card" href="#workspace.finance.usage"><span>美元消费</span><strong class="is-money">${formatMoney(usage("usd_micros"), "USD")}</strong><small>${periodLabel}已入账调用成本</small></a>
        </div>
        <p class="workspace-overview-footnote">按北京时间统计，消费按币种分别汇总。${Number(usage("estimated")) > 0 ? `包含 ${formatCount(usage("estimated"))} 笔估算用量。` : ""}
          <a href="#workspace.finance.usage">查看用量与消费明细 →</a></p>
      </section>` : ""}
      <div class="workspace-overview-lower">
        <section class="workspace-overview-panel"><h2>需要关注</h2>
          ${attention.length ? `<ul class="workspace-overview-attention">${attention.map(([value, title, route]) =>
            `<li><span>${title}</span><strong>${formatCount(value)}</strong><a href="#${route}">查看 →</a></li>`).join("")}</ul>`
            : `<p>${sections.has("usage_cost") || sections.has("provider_health") ? "已读取的数据中没有发现失败请求或待检查服务。" : "暂无可查看的运行数据。"}</p>`}
        </section>
        <section class="workspace-overview-panel"><h2>常用管理</h2><nav class="workspace-overview-links" aria-label="组织常用入口">
          ${isManager ? `<a href="#workspace.gateway.aliases">模型路由 <span>候选顺序与故障切换 →</span></a>` : ""}
          ${sections.has("usage_cost") && (isManager || roleCode === "finance_admin") ? `<a href="#workspace.finance.budgets">预算管理 <span>设置调用成本上限 →</span></a>` : ""}
          ${sections.has("role_tasks") ? `<a href="#workspace.settings.members">成员管理 <span>邀请成员与分配权限 →</span></a>` : ""}
        </nav></section>
      </div>` : ""}
  </div>`;
}

function formatCount(value) { return value == null ? "—" : Number(value).toLocaleString("zh-CN"); }
function formatMoney(value, currency) {
  return value == null ? "—" : new Intl.NumberFormat("zh-CN", {
    style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 4,
  }).format(Number(value) / 1_000_000);
}
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]); }
