/** Dense, role-clipped PRD-141 Platform Growth workbench. */

const TAB_LABELS = Object.freeze({
  funnels: "转化漏斗",
  cohorts: "留存分析",
  campaigns: "获客活动",
  experiments: "增长实验",
  attribution: "归因结果",
});
const STAGES = ["registered", "activated", "trial_started", "paid", "retained_30d"];

export function renderPlatformGrowth(state = {}, permissions = new Set()) {
  const response = state.data?.[state.tab] ?? null;
  const freshness = response?.source_freshness ?? null;
  return `<div data-platform-growth-root data-phase="${escapeAttribute(state.phase ?? "idle")}">
    <header class="platform-page-heading platform-growth-heading">
      <div><p>增长与归因</p><h1>增长漏斗与归因</h1></div>
      <span>${freshness ? `${status(freshness.status)} 覆盖率 ${number(freshness.coverage_bps / 100)}%` : "隐私保护投影"}</span>
    </header>
    ${phaseBanner(state)}
    <nav class="platform-growth-tabs" role="tablist" aria-label="增长分析视图">
      ${(state.availableTabs ?? []).map((tab) => `<button type="button" role="tab" aria-selected="${state.tab === tab}" data-platform-growth-tab="${tab}">${TAB_LABELS[tab]}</button>`).join("")}
    </nav>
    <section class="platform-band platform-growth-workbench">
      ${renderTab(state, permissions)}
    </section>
    ${renderExportWorkbench(state, permissions)}
  </div>`;
}

function renderTab(state, permissions) {
  if (state.tab === "funnels") return funnelsView(state, permissions);
  if (state.tab === "cohorts") return cohortsView(state, permissions);
  if (state.tab === "campaigns") return campaignsView(state, permissions);
  if (state.tab === "experiments") return experimentsView(state, permissions);
  if (state.tab === "attribution") return attributionView(state, permissions);
  return empty("当前身份没有可用的增长视图。");
}

function funnelsView(state, permissions) {
  const filters = state.filters?.funnels ?? {};
  const response = state.data?.funnels;
  const full = permissions.has("platform.growth.read");
  const dimensions = full
    ? [["none", "不区分"], ["source", "来源"], ["channel", "渠道"], ["region", "区域"], ["plan_band", "套餐"]]
    : [["none", "不区分"], ["region", "区域"], ["plan_band", "套餐"]];
  return `${sectionHeading("漏斗快照", "五阶段转化", response)}
    <form class="platform-filter-form platform-growth-filter" data-platform-growth-filter-form data-growth-tab="funnels" autocomplete="off">
      ${select("视图", "view", filters.view ?? (full ? "overview" : "adoption"), full
        ? [["overview", "总览"], ["adoption", "转化"], ["retention", "留存"]]
        : [["adoption", "转化"], ["retention", "留存"]])}
      ${dateInput("开始日期", "from", filters.from)}${dateInput("结束日期", "to", filters.to)}
      ${select("统计粒度", "bucket", filters.bucket ?? "day", [["day", "日"], ["week", "周"], ["month", "月"]])}
      ${select("维度", "dimension", filters.dimension ?? "none", dimensions)}
      <label><span>维度值</span><input name="dimensionValue" maxlength="80" value="${escapeAttribute(filters.dimension_value ?? "")}" /></label>
      <button class="platform-primary" type="submit" ${busy(state, "read-funnels")}>查询</button>
    </form>
    ${response ? funnelTable(response.items) : loadingOrEmpty(state, "read-funnels", "暂无漏斗快照。")}
    ${pagination(state, "funnels")}`;
}

function funnelTable(items) {
  if (!items.length) return empty("暂无漏斗快照。");
  return `<div class="platform-table-shell"><table class="platform-growth-table"><thead><tr><th>时间段</th><th>维度</th>${STAGES.map((stage) => `<th>${escapeHtml(label(stage))}</th>`).join("")}<th>覆盖率</th><th>贡献</th></tr></thead><tbody>
    ${items.map((item) => `<tr><td>${escapeHtml(item.bucket_start)}<small>${escapeHtml(item.bucket)}</small></td><td>${escapeHtml(item.dimension_value ?? label(item.dimension))}</td>${item.stages.map((stage) => `<td>${item.suppressed ? "-" : number(stage.subject_count)}<small>${stage.conversion_from_previous_bps === null ? "-" : percent(stage.conversion_from_previous_bps)}</small></td>`).join("")}<td>${percent(item.coverage_bps)}${item.suppressed ? `<small>${escapeHtml(label(item.suppression_code))}</small>` : ""}</td><td>${money(item.contribution_minor, item.currency)}</td></tr>`).join("")}
  </tbody></table></div>`;
}

function cohortsView(state, permissions) {
  const filters = state.filters?.cohorts ?? {};
  const response = state.data?.cohorts;
  const full = permissions.has("platform.growth.read");
  const dimensions = full
    ? [["none", "不区分"], ["source", "来源"], ["channel", "渠道"], ["region", "区域"], ["plan_band", "套餐"]]
    : [["none", "不区分"], ["region", "区域"], ["plan_band", "套餐"]];
  return `${sectionHeading("留存投影", "留存与流失", response)}
    <form class="platform-filter-form platform-growth-filter" data-platform-growth-filter-form data-growth-tab="cohorts" autocomplete="off">
      ${select("视图", "view", filters.view ?? "retention", [["adoption", "转化"], ["retention", "留存"]])}
      ${select("分组方式", "cohortKind", filters.cohort_kind ?? "signup_week", [["signup_week", "注册周"], ["activation_week", "激活周"], ["first_paid_month", "首次付费月"], ["campaign", "活动"]])}
      ${dateInput("开始日期", "from", filters.from)}${dateInput("结束日期", "to", filters.to)}
      <label><span>时间段数量</span><input name="ageBuckets" type="number" min="1" max="24" value="${escapeAttribute(filters.age_buckets ?? "12")}" /></label>
      ${select("维度", "dimension", filters.dimension ?? "none", dimensions)}
      <label><span>维度值</span><input name="dimensionValue" maxlength="80" value="${escapeAttribute(filters.dimension_value ?? "")}" /></label>
      <button class="platform-primary" type="submit" ${busy(state, "read-cohorts")}>查询</button>
    </form>
    ${response ? cohortTable(response.items) : loadingOrEmpty(state, "read-cohorts", "暂无 cohort 快照。")}
    ${pagination(state, "cohorts")}`;
}

function cohortTable(items) {
  if (!items.length) return empty("暂无 cohort 快照。");
  return `<div class="platform-table-shell"><table class="platform-growth-table"><thead><tr><th>分组</th><th>周期</th><th>维度</th><th>符合条件</th><th>留存</th><th>流失</th><th>留存率</th><th>覆盖率</th></tr></thead><tbody>
    ${items.map((item) => `<tr><td>${escapeHtml(item.cohort_bucket)}<small>${escapeHtml(label(item.cohort_kind))}</small></td><td>${number(item.age_bucket)}</td><td>${escapeHtml(item.dimension_value ?? label(item.dimension))}</td><td>${nullableNumber(item.eligible_count)}</td><td>${nullableNumber(item.retained_count)}</td><td>${nullableNumber(item.churned_count)}</td><td>${nullablePercent(item.retention_bps)}</td><td>${percent(item.coverage_bps)}${item.suppressed ? `<small>${escapeHtml(label(item.suppression_code))}</small>` : ""}</td></tr>`).join("")}
  </tbody></table></div>`;
}

function campaignsView(state, permissions) {
  const filters = state.filters?.campaigns ?? {};
  const response = state.data?.campaigns;
  const canManage = permissions.has("platform.growth.campaign.manage");
  return `${sectionHeading("活动目录", "获客活动", response)}
    <form class="platform-filter-form platform-growth-filter platform-growth-filter-compact" data-platform-growth-filter-form data-growth-tab="campaigns" autocomplete="off">
      ${select("状态", "status", filters.status ?? "", [["", "全部"], ["draft", "草稿"], ["scheduled", "已排期"], ["active", "进行中"], ["paused", "已暂停"], ["completed", "已完成"], ["archived", "已归档"]])}
      <label><span>来源 ID</span><input name="source" pattern="acs_[0-9a-f]{32}" value="${escapeAttribute(filters.source ?? "")}" /></label>
      <label><span>渠道</span><input name="channel" pattern="[a-z0-9][a-z0-9._-]{0,39}" value="${escapeAttribute(filters.channel ?? "")}" /></label>
      ${dateInput("开始日期", "from", filters.from)}${dateInput("结束日期", "to", filters.to)}
      <button class="platform-primary" type="submit" ${busy(state, "read-campaigns")}>筛选</button>
    </form>
    ${canManage ? `<details class="platform-collapsed-section"><summary><span>＋ 新建活动</span><small>需要发布新的获客活动时展开</small></summary>${campaignEditor(null, state)}</details>` : ""}
    <div class="platform-growth-records">${response?.items?.map((item) => campaignRecord(item, canManage, state)).join("") ?? loadingOrEmpty(state, "read-campaigns", "暂无活动。")}</div>
    ${pagination(state, "campaigns")}`;
}

function campaignRecord(item, canManage, state) {
  const actions = canManage ? item.action_capabilities : [];
  return `<article><header><div><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(shortId(item.campaign_id))} · ${escapeHtml(item.channel_code)}</small></div>${status(item.status)}</header>
    <dl><div><dt>目标阶段</dt><dd>${escapeHtml(label(item.target_stage))}</dd></div><div><dt>转化数</dt><dd>${number(item.conversion_count)}</dd></div><div><dt>投入</dt><dd>${money(item.spend_minor, item.currency)}</dd></div><div><dt>结果</dt><dd>${nullablePercent(item.outcome_bps)}</dd></div></dl>
    ${actions.includes("update") ? campaignEditor(item, state) : ""}
    ${actions.filter((action) => action !== "update").length ? `<div class="platform-growth-actions">${actions.filter((action) => action !== "update").map((action) => transitionForm("campaign", action, item.campaign_id, item.revision, state)).join("")}</div>` : ""}
  </article>`;
}

function campaignEditor(item, state) {
  const create = !item;
  return `<form class="platform-growth-command" data-platform-growth-campaign-form autocomplete="off">
    <h3>${create ? "创建活动" : "更新活动"}</h3><input type="hidden" name="operation" value="${create ? "create" : "update"}" />
    ${create ? "" : `<input type="hidden" name="campaignId" value="${escapeAttribute(item.campaign_id)}" />`}<input type="hidden" name="expectedRevision" value="${create ? 0 : integerValue(item.revision)}" />
    <label><span>名称</span><input name="displayName" maxlength="120" value="${escapeAttribute(item?.display_name ?? "")}" required /></label>
    <label><span>来源</span><input name="sourceId" pattern="acs_[0-9a-f]{32}" value="${escapeAttribute(item?.acquisition_source_id ?? "")}" required /></label>
    <label><span>渠道</span><input name="channelCode" pattern="[a-z0-9][a-z0-9._-]{0,39}" value="${escapeAttribute(item?.channel_code ?? "")}" required /></label>
    ${select("Target", "targetStage", item?.target_stage ?? "activated", STAGES.map((stage) => [stage, label(stage)]))}
    ${dateTimeInput("开始时间", "startsAt", item?.starts_at)}${dateTimeInput("结束时间", "endsAt", item?.ends_at)}
    <label><span>预算（分）</span><input name="budgetMinor" type="number" min="0" value="${numberInput(item?.budget_minor, 0)}" required /></label>
    <label><span>币种</span><input name="currency" pattern="[A-Z]{3}" maxlength="3" value="${escapeAttribute(item?.currency ?? "CNY")}" required /></label>
    ${stepUpFields()}${confirmation(create ? "创建" : "更新", busy(state, "write-campaign"))}
  </form>`;
}

function experimentsView(state, permissions) {
  const filters = state.filters?.experiments ?? {};
  const response = state.data?.experiments;
  const canManage = permissions.has("platform.growth.experiment.manage");
  return `${sectionHeading("实验目录", "增长实验", response)}
    <form class="platform-filter-form platform-growth-filter platform-growth-filter-compact" data-platform-growth-filter-form data-growth-tab="experiments" autocomplete="off">
      ${select("状态", "status", filters.status ?? "", [["", "全部"], ["draft", "草稿"], ["scheduled", "已排期"], ["running", "运行中"], ["paused", "已暂停"], ["decided", "已决策"], ["stopped", "已停止"], ["archived", "已归档"]])}
      <label><span>展示位置</span><input name="surface" maxlength="80" value="${escapeAttribute(filters.surface ?? "")}" /></label>
      <label><span>指标</span><input name="metric" maxlength="80" value="${escapeAttribute(filters.metric ?? "")}" /></label>
      ${dateInput("开始日期", "from", filters.from)}${dateInput("结束日期", "to", filters.to)}
      <button class="platform-primary" type="submit" ${busy(state, "read-experiments")}>筛选</button>
    </form>
    ${canManage ? `<details class="platform-collapsed-section"><summary><span>＋ 新建实验</span><small>需要创建新的对照实验时展开</small></summary>${experimentEditor(null, state)}</details>` : ""}
    <div class="platform-growth-records">${response?.items?.map((item) => experimentRecord(item, canManage, response.source_freshness?.watermark, state)).join("") ?? loadingOrEmpty(state, "read-experiments", "暂无实验。")}</div>
    ${pagination(state, "experiments")}`;
}

function experimentRecord(item, canManage, watermark, state) {
  const actions = canManage ? item.action_capabilities : [];
  return `<article><header><div><strong>${escapeHtml(item.hypothesis_code)}</strong><small>${escapeHtml(item.surface_code)} · ${escapeHtml(shortId(item.experiment_id))}</small></div>${status(item.status)}</header>
    <dl><div><dt>指标</dt><dd>${escapeHtml(item.primary_metric)}</dd></div><div><dt>已分配</dt><dd>${number(item.assigned_count)}</dd></div><div><dt>对照 / 变体</dt><dd>${number(item.control_count)} / ${number(item.variant_count)}</dd></div><div><dt>结论</dt><dd>${escapeHtml(label(item.decision))}</dd></div></dl>
    ${actions.includes("update") ? experimentEditor(item, state) : ""}
    ${actions.filter((action) => action !== "update").length ? `<div class="platform-growth-actions">${actions.filter((action) => action !== "update").map((action) => transitionForm("experiment", action, item.experiment_id, item.revision, state, ["start", "decide"].includes(action) ? watermark : null)).join("")}</div>` : ""}
  </article>`;
}

function experimentEditor(item, state) {
  const create = !item;
  return `<form class="platform-growth-command platform-growth-experiment-command" data-platform-growth-experiment-form autocomplete="off">
    <h3>${create ? "创建实验" : "更新实验"}</h3><input type="hidden" name="operation" value="${create ? "create" : "update"}" />
    ${create ? "" : `<input type="hidden" name="experimentId" value="${escapeAttribute(item.experiment_id)}" />`}<input type="hidden" name="expectedRevision" value="${create ? 0 : integerValue(item.revision)}" />
    ${textInput("假设", "hypothesisCode", item?.hypothesis_code ?? "", 80)}${textInput("展示位置", "surfaceCode", item?.surface_code ?? "", 80)}${textInput("指标", "primaryMetric", item?.primary_metric ?? "", 80)}
    ${textInput("对照组", "controlCode", item?.control_code ?? "control", 40)}${textInput("变体组", "variantCode", item?.variant_code ?? "variant", 40)}
    <label><span>对照比例（基点）</span><input name="controlAllocationBps" type="number" min="1" max="9999" value="${numberInput(item?.control_allocation_bps, 5000)}" required /></label>
    <label><span>变体比例（基点）</span><input name="variantAllocationBps" type="number" min="1" max="9999" value="${numberInput(item?.variant_allocation_bps, 5000)}" required /></label>
    ${textInput("资格规则版本", "eligibilityPolicyVersion", item?.eligibility_policy_version ?? "growth-eligibility-v2", 80)}${textInput("同意版本", "consentVersion", item?.consent_version ?? "analytics-v1", 80)}
    <label><span>样本上限</span><input name="sampleCap" type="number" min="20" max="10000000" value="${numberInput(item?.sample_cap, 1000)}" required /></label>
    ${dateTimeInput("开始时间", "startsAt", item?.starts_at)}${dateTimeInput("结束时间", "endsAt", item?.ends_at)}
    ${textInput("安全规则", "guardrailCode", item?.guardrail_code ?? "safety.default", 80)}${textInput("决策规则版本", "decisionPolicyVersion", item?.decision_policy_version ?? "fixed-horizon-v1", 80)}
    ${stepUpFields()}${confirmation(create ? "创建" : "更新", busy(state, "write-experiment"))}
  </form>`;
}

function attributionView(state, permissions) {
  const filters = state.filters?.attribution ?? {};
  const response = state.data?.attribution;
  const channelOnly = permissions.has("platform.growth.channel_attribution_read")
    && !permissions.has("platform.growth.read");
  return `${sectionHeading("归因投影", "归因结果", response)}
    <form class="platform-filter-form platform-growth-filter" data-platform-growth-filter-form data-growth-tab="attribution" autocomplete="off">
      ${select("视图", "view", channelOnly ? "channel_summary" : (filters.view ?? "summary"), channelOnly
        ? [["channel_summary", "渠道汇总"]]
        : [["summary", "汇总"], ["campaign", "活动"], ["channel", "渠道"]])}
      ${dateInput("开始日期", "from", filters.from)}${dateInput("结束日期", "to", filters.to)}
      ${select("阶段", "stage", filters.stage ?? "", [["", "全部"], ...STAGES.map((stage) => [stage, label(stage)])])}
      ${channelOnly ? "" : `<label><span>来源</span><input name="source" pattern="acs_[0-9a-f]{32}" value="${escapeAttribute(filters.source ?? "")}" /></label><label><span>渠道</span><input name="channel" maxlength="80" value="${escapeAttribute(filters.channel ?? "")}" /></label><label><span>活动</span><input name="campaign" pattern="gca_[0-9a-f]{32}" value="${escapeAttribute(filters.campaign ?? "")}" /></label>`}
      <button class="platform-primary" type="submit" ${busy(state, "read-attribution")}>查询</button>
    </form>
    ${response ? attributionTable(response.items, channelOnly) : loadingOrEmpty(state, "read-attribution", "暂无归因结果。")}
    ${pagination(state, "attribution")}`;
}

function attributionTable(items, channelOnly) {
  if (!items.length) return empty("暂无归因结果。");
  return `<div class="platform-table-shell"><table class="platform-growth-table"><thead><tr><th>记录</th><th>阶段</th><th>转化数</th>${channelOnly ? "" : "<th>价值</th>"}<th>覆盖率</th><th>来源</th></tr></thead><tbody>
    ${items.map((item) => `<tr><td><code>${escapeHtml(shortId(item.safe_id))}</code></td><td>${escapeHtml(label(item.conversion_stage))}</td><td>${nullableNumber(item.conversion_count)}</td>${channelOnly ? "" : `<td>${money(item.value_minor, item.currency)}</td>`}<td>${percent(item.coverage_bps)}${item.suppressed ? `<small>${escapeHtml(label(item.suppression_code))}</small>` : ""}</td><td><code>${escapeHtml(shortId(item.channel_partner_id ?? item.campaign_id ?? item.source_id ?? "direct"))}</code></td></tr>`).join("")}
  </tbody></table></div>`;
}

function renderExportWorkbench(state, permissions) {
  const canRequest = permissions.has("platform.growth.export.request");
  const canApprove = permissions.has("platform.growth.export.approve");
  const canDownload = permissions.has("platform.growth.export.download");
  if (!canRequest && !canApprove && !canDownload) return "";
  const current = state.export;
  return `<section class="platform-band platform-growth-export">
    <div class="platform-section-heading"><div><p>受控导出</p><h2>导出操作</h2></div>${current ? status(current.status) : ""}</div>
    <div class="platform-growth-export-grid">
      ${canRequest ? exportRequestForm(state) : ""}
      <div class="platform-growth-export-status">
        <form data-platform-growth-export-lookup-form autocomplete="off"><label><span>导出编号</span><input name="exportId" pattern="gxp_[0-9a-f]{32}" value="${escapeAttribute(current?.export_id ?? "")}" required /></label><button type="submit" ${busy(state, "read-export")}>刷新</button></form>
        ${current ? exportFacts(current) : empty("暂无导出状态。")}
        ${current && canApprove && current.status === "policy_review" ? exportApprovalForm(current, state) : ""}
        ${current && canDownload && current.status === "ready" ? exportDownloadForm(current, state) : ""}
      </div>
    </div>
  </section>`;
}

function exportRequestForm(state) {
  const active = state.tab;
  const filters = state.filters?.[active] ?? {};
  const view = active === "attribution" ? (filters.view ?? "summary")
    : active === "funnels" ? (filters.view ?? "overview")
      : active === "cohorts" ? (filters.view ?? "retention") : "overview";
  return `<form class="platform-growth-command" data-platform-growth-export-form autocomplete="off"><h3>申请导出</h3>
    ${select("数据集", "queryKind", active, Object.entries(TAB_LABELS).map(([value, text]) => [value, text]))}
    ${select("视图", "view", view, [["overview", "总览"], ["adoption", "转化"], ["retention", "留存"], ["summary", "汇总"], ["campaign", "活动"], ["channel", "渠道"], ["channel_summary", "渠道汇总"]])}
    ${dateInput("开始日期", "fromDate", filters.from, true)}${dateInput("结束日期", "toDate", filters.to, true)}
    ${select("统计粒度", "bucket", active === "funnels" ? (filters.bucket ?? "day") : "", [["", "不适用"], ["day", "日"], ["week", "周"], ["month", "月"]])}
    ${select("维度", "dimension", filters.dimension ?? "none", [["none", "不区分"], ["source", "来源"], ["channel", "渠道"], ["region", "区域"], ["plan_band", "套餐"]])}
    ${textInput("计算规则", "formulaVersion", formulaFor(active), 80)}
    ${select("数据粒度", "sensitivity", "aggregate", [["aggregate", "汇总"], ["high_cardinality", "高基数"]])}
    ${stepUpFields()}${confirmation("提交申请", busy(state, "write-export"))}</form>`;
}

function exportFacts(item) {
  return `<dl><div><dt>导出编号</dt><dd><code>${escapeHtml(shortId(item.export_id))}</code></dd></div><div><dt>数据集</dt><dd>${escapeHtml(label(item.query_kind))}</dd></div><div><dt>行数 / 单元格</dt><dd>${nullableNumber(item.row_count)} / ${nullableNumber(item.cell_count)}</dd></div><div><dt>版本</dt><dd>${number(item.revision)}</dd></div><div><dt>有效期</dt><dd>${escapeHtml(formatTime(item.expires_at))}</dd></div><div><dt>下载次数</dt><dd>${number(item.download_count)}</dd></div></dl>`;
}

function exportApprovalForm(item, state) {
  return `<form class="platform-growth-inline-form" data-platform-growth-export-approval-form autocomplete="off"><input type="hidden" name="exportId" value="${escapeAttribute(item.export_id)}" /><input type="hidden" name="expectedRevision" value="${integerValue(item.revision)}" />
    ${select("决定", "operation", "approve", [["approve", "通过"], ["reject", "拒绝"]])}${stepUpFields()}${confirmation("提交审批", busy(state, "write-approval"))}</form>`;
}

function exportDownloadForm(item, state) {
  return `<form class="platform-growth-inline-form" data-platform-growth-export-download-form autocomplete="off"><input type="hidden" name="exportId" value="${escapeAttribute(item.export_id)}" />${stepUpFields()}${confirmation("下载 CSV", busy(state, "write-download"))}</form>`;
}

function transitionForm(kind, action, id, revision, state, watermark = null) {
  return `<form data-platform-growth-${kind}-transition-form autocomplete="off"><input type="hidden" name="operation" value="${escapeAttribute(action)}" /><input type="hidden" name="${kind}Id" value="${escapeAttribute(id)}" /><input type="hidden" name="expectedRevision" value="${integerValue(revision)}" />${watermark ? `<input type="hidden" name="sourceWatermark" value="${escapeAttribute(watermark)}" />` : ""}${stepUpFields()}${confirmation(label(action), busy(state, `write-${kind}`))}</form>`;
}

function sectionHeading(kicker, title, response) {
  return `<div class="platform-section-heading"><div><p>${escapeHtml(kicker)}</p><h2>${escapeHtml(title)}</h2></div><span>${response ? `${number(response.items.length)} 条 · ${escapeHtml(formatTime(response.generated_at))}` : "暂无投影"}</span></div>`;
}

function pagination(state, tab) {
  const previous = (state.histories?.[tab]?.length ?? 0) > 0;
  const next = Boolean(state.data?.[tab]?.next_cursor);
  return `<nav class="platform-pagination" aria-label="${escapeAttribute(TAB_LABELS[tab])} 分页"><span>第 ${number((state.histories?.[tab]?.length ?? 0) + 1)} 页</span><div><button type="button" data-platform-action="growth-previous" ${previous && !state.loading?.length ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="growth-next" ${next && !state.loading?.length ? "" : "disabled"}>下一页</button></div></nav>`;
}

function phaseBanner(state) {
  if (state.feedback) return `<p class="platform-feedback" role="status">${escapeHtml(state.feedback)}</p>`;
  if (state.phase === "loading") return '<p class="platform-feedback" role="status">正在读取增长投影…</p>';
  if (state.phase === "stale") return '<p class="platform-warning" role="status">当前显示最后一份可信投影，来源正在追平。</p>';
  if (state.phase === "partial") return '<p class="platform-warning" role="status">当前投影来源覆盖不完整，缺失值未按零处理。</p>';
  if (state.phase === "permission_revoked") return '<p class="platform-error" role="alert">权限已撤销，增长数据已清空并正在刷新导航。</p>';
  if (state.phase === "revision_conflict") return '<p class="platform-error" role="alert">对象或来源水位已变化，请刷新后重试。</p>';
  if (state.phase === "dependency_unavailable") return '<p class="platform-error" role="alert">增长数据依赖暂不可用，未提交部分结果。</p>';
  if (state.error) return `<p class="platform-error" role="alert">${escapeHtml(state.error.message)} <code>${escapeHtml(state.error.code)}</code> <small>${escapeHtml(state.error.requestId)}</small></p>`;
  return "";
}

function stepUpFields() {
  return `${select("验证方式", "method", "totp", [["totp", "动态验证码"], ["webauthn", "安全密钥"]])}<label data-platform-growth-step-up-code><span>验证码</span><input type="password" name="verificationCode" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" data-platform-sensitive required /></label>`;
}

function confirmation(text, disabled) {
  return `<label class="platform-check"><input type="checkbox" name="confirmed" required ${disabled ? "disabled" : ""}/><span>确认命令</span></label><button class="platform-primary" type="submit" ${disabled}>${escapeHtml(text)}</button>`;
}

function select(text, name, current, values) {
  return `<label><span>${escapeHtml(text)}</span><select name="${escapeAttribute(name)}">${values.map(([value, item]) => `<option value="${escapeAttribute(value)}" ${value === (current ?? "") ? "selected" : ""}>${escapeHtml(item)}</option>`).join("")}</select></label>`;
}

function textInput(text, name, value, maxlength) {
  return `<label><span>${escapeHtml(text)}</span><input name="${escapeAttribute(name)}" maxlength="${number(maxlength)}" value="${escapeAttribute(value)}" required /></label>`;
}

function dateInput(text, name, value, required = false) {
  return `<label><span>${escapeHtml(text)}</span><input name="${escapeAttribute(name)}" type="date" value="${escapeAttribute(value ?? "")}" ${required ? "required" : ""}/></label>`;
}

function dateTimeInput(text, name, value) {
  return `<label><span>${escapeHtml(text)}</span><input name="${escapeAttribute(name)}" type="datetime-local" step="1" value="${escapeAttribute(toDateTimeLocal(value))}" required /></label>`;
}

function busy(state, lane) {
  return state.loading?.includes(lane) ? "disabled" : "";
}

function loadingOrEmpty(state, lane, text) {
  return state.loading?.includes(lane)
    ? '<p class="platform-empty" role="status">正在读取可信投影…</p>' : empty(text);
}

function empty(text) {
  return `<p class="platform-empty">${escapeHtml(text)}</p>`;
}

function status(value) {
  const text = String(value ?? "unknown");
  const kind = new Set(["active", "running", "ready", "fresh", "downloaded", "completed", "approved"]).has(text)
    ? "is-ok" : new Set(["draft", "scheduled", "paused", "partial", "stale", "policy_review", "building", "requested"]).has(text)
      ? "is-warn" : "is-danger";
  return `<span class="platform-status ${kind}">${escapeHtml(label(text))}</span>`;
}

function formulaFor(tab) {
  if (tab === "funnels") return "growth-funnel-v2";
  if (tab === "cohorts") return "growth-cohort-v2";
  if (tab === "attribution") return "last-eligible-touch-30d-v1";
  return "platform-growth-v1";
}

function money(value, currency) {
  if (value === null || value === undefined || !currency) return "-";
  return `${escapeHtml(currency)} ${new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value) / 100)}`;
}

function nullableNumber(value) {
  return value === null || value === undefined ? "-" : number(value);
}

function nullablePercent(value) {
  return value === null || value === undefined ? "-" : percent(value);
}

function percent(value) {
  return `${new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: 2 }).format(Number(value) / 100)}%`;
}

function number(value) {
  return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0";
}

function numberInput(value, fallback) {
  return Number.isSafeInteger(value) ? String(value) : String(fallback);
}

function integerValue(value) {
  return Number.isSafeInteger(Number(value)) ? String(Number(value)) : "0";
}

function label(value) {
  return String(value ?? "-").replaceAll("_", " ");
}

function shortId(value) {
  const text = String(value ?? "");
  return text.length > 20 ? `${text.slice(0, 12)}...${text.slice(-4)}` : text;
}

function formatTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isFinite(parsed.valueOf())
    ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(parsed)
    : "-";
}

function toDateTimeLocal(value) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isFinite(parsed.valueOf()) ? parsed.toISOString().slice(0, 19) : "";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function escapeAttribute(value) {
  return escapeHtml(value);
}
