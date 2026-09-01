/** Dense, role-clipped PRD-141 Platform Growth workbench. */

const TAB_LABELS = Object.freeze({
  funnels: "Funnels",
  cohorts: "Cohorts",
  campaigns: "Campaigns",
  experiments: "Experiments",
  attribution: "Attribution",
});
const STAGES = ["registered", "activated", "trial_started", "paid", "retained_30d"];

export function renderPlatformGrowth(state = {}, permissions = new Set()) {
  const response = state.data?.[state.tab] ?? null;
  const freshness = response?.source_freshness ?? null;
  return `<div data-platform-growth-root data-phase="${escapeAttribute(state.phase ?? "idle")}">
    <header class="platform-page-heading platform-growth-heading">
      <div><p>Growth operations</p><h1>增长漏斗与归因</h1></div>
      <span>${freshness ? `${status(freshness.status)} ${number(freshness.coverage_bps / 100)}% coverage` : "Privacy-safe projections"}</span>
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
    ? [["none", "None"], ["source", "Source"], ["channel", "Channel"], ["region", "Region"], ["plan_band", "Plan band"]]
    : [["none", "None"], ["region", "Region"], ["plan_band", "Plan band"]];
  return `${sectionHeading("Funnel snapshots", "五阶段转化", response)}
    <form class="platform-filter-form platform-growth-filter" data-platform-growth-filter-form data-growth-tab="funnels" autocomplete="off">
      ${select("View", "view", filters.view ?? (full ? "overview" : "adoption"), full
        ? [["overview", "Overview"], ["adoption", "Adoption"], ["retention", "Retention"]]
        : [["adoption", "Adoption"], ["retention", "Retention"]])}
      ${dateInput("From", "from", filters.from)}${dateInput("To", "to", filters.to)}
      ${select("Bucket", "bucket", filters.bucket ?? "day", [["day", "Day"], ["week", "Week"], ["month", "Month"]])}
      ${select("Dimension", "dimension", filters.dimension ?? "none", dimensions)}
      <label><span>Dimension value</span><input name="dimensionValue" maxlength="80" value="${escapeAttribute(filters.dimension_value ?? "")}" /></label>
      <button class="platform-primary" type="submit" ${busy(state, "read-funnels")}>查询</button>
    </form>
    ${response ? funnelTable(response.items) : loadingOrEmpty(state, "read-funnels", "暂无漏斗快照。")}
    ${pagination(state, "funnels")}`;
}

function funnelTable(items) {
  if (!items.length) return empty("暂无漏斗快照。");
  return `<div class="platform-table-shell"><table class="platform-growth-table"><thead><tr><th>Bucket</th><th>Dimension</th>${STAGES.map((stage) => `<th>${escapeHtml(label(stage))}</th>`).join("")}<th>Coverage</th><th>Contribution</th></tr></thead><tbody>
    ${items.map((item) => `<tr><td>${escapeHtml(item.bucket_start)}<small>${escapeHtml(item.bucket)}</small></td><td>${escapeHtml(item.dimension_value ?? label(item.dimension))}</td>${item.stages.map((stage) => `<td>${item.suppressed ? "-" : number(stage.subject_count)}<small>${stage.conversion_from_previous_bps === null ? "-" : percent(stage.conversion_from_previous_bps)}</small></td>`).join("")}<td>${percent(item.coverage_bps)}${item.suppressed ? `<small>${escapeHtml(label(item.suppression_code))}</small>` : ""}</td><td>${money(item.contribution_minor, item.currency)}</td></tr>`).join("")}
  </tbody></table></div>`;
}

function cohortsView(state, permissions) {
  const filters = state.filters?.cohorts ?? {};
  const response = state.data?.cohorts;
  const full = permissions.has("platform.growth.read");
  const dimensions = full
    ? [["none", "None"], ["source", "Source"], ["channel", "Channel"], ["region", "Region"], ["plan_band", "Plan band"]]
    : [["none", "None"], ["region", "Region"], ["plan_band", "Plan band"]];
  return `${sectionHeading("Cohort projection", "留存与流失", response)}
    <form class="platform-filter-form platform-growth-filter" data-platform-growth-filter-form data-growth-tab="cohorts" autocomplete="off">
      ${select("View", "view", filters.view ?? "retention", [["adoption", "Adoption"], ["retention", "Retention"]])}
      ${select("Cohort", "cohortKind", filters.cohort_kind ?? "signup_week", [["signup_week", "Signup week"], ["activation_week", "Activation week"], ["first_paid_month", "First paid month"], ["campaign", "Campaign"]])}
      ${dateInput("From", "from", filters.from)}${dateInput("To", "to", filters.to)}
      <label><span>Age buckets</span><input name="ageBuckets" type="number" min="1" max="24" value="${escapeAttribute(filters.age_buckets ?? "12")}" /></label>
      ${select("Dimension", "dimension", filters.dimension ?? "none", dimensions)}
      <label><span>Dimension value</span><input name="dimensionValue" maxlength="80" value="${escapeAttribute(filters.dimension_value ?? "")}" /></label>
      <button class="platform-primary" type="submit" ${busy(state, "read-cohorts")}>查询</button>
    </form>
    ${response ? cohortTable(response.items) : loadingOrEmpty(state, "read-cohorts", "暂无 cohort 快照。")}
    ${pagination(state, "cohorts")}`;
}

function cohortTable(items) {
  if (!items.length) return empty("暂无 cohort 快照。");
  return `<div class="platform-table-shell"><table class="platform-growth-table"><thead><tr><th>Cohort</th><th>Age</th><th>Dimension</th><th>Eligible</th><th>Retained</th><th>Churned</th><th>Retention</th><th>Coverage</th></tr></thead><tbody>
    ${items.map((item) => `<tr><td>${escapeHtml(item.cohort_bucket)}<small>${escapeHtml(label(item.cohort_kind))}</small></td><td>${number(item.age_bucket)}</td><td>${escapeHtml(item.dimension_value ?? label(item.dimension))}</td><td>${nullableNumber(item.eligible_count)}</td><td>${nullableNumber(item.retained_count)}</td><td>${nullableNumber(item.churned_count)}</td><td>${nullablePercent(item.retention_bps)}</td><td>${percent(item.coverage_bps)}${item.suppressed ? `<small>${escapeHtml(label(item.suppression_code))}</small>` : ""}</td></tr>`).join("")}
  </tbody></table></div>`;
}

function campaignsView(state, permissions) {
  const filters = state.filters?.campaigns ?? {};
  const response = state.data?.campaigns;
  const canManage = permissions.has("platform.growth.campaign.manage");
  return `${sectionHeading("Campaign registry", "获客活动", response)}
    <form class="platform-filter-form platform-growth-filter platform-growth-filter-compact" data-platform-growth-filter-form data-growth-tab="campaigns" autocomplete="off">
      ${select("Status", "status", filters.status ?? "", [["", "All"], ["draft", "Draft"], ["scheduled", "Scheduled"], ["active", "Active"], ["paused", "Paused"], ["completed", "Completed"], ["archived", "Archived"]])}
      <label><span>Source ID</span><input name="source" pattern="acs_[0-9a-f]{32}" value="${escapeAttribute(filters.source ?? "")}" /></label>
      <label><span>Channel</span><input name="channel" pattern="[a-z0-9][a-z0-9._-]{0,39}" value="${escapeAttribute(filters.channel ?? "")}" /></label>
      ${dateInput("From", "from", filters.from)}${dateInput("To", "to", filters.to)}
      <button class="platform-primary" type="submit" ${busy(state, "read-campaigns")}>筛选</button>
    </form>
    ${canManage ? campaignEditor(null, state) : ""}
    <div class="platform-growth-records">${response?.items?.map((item) => campaignRecord(item, canManage, state)).join("") ?? loadingOrEmpty(state, "read-campaigns", "暂无活动。")}</div>
    ${pagination(state, "campaigns")}`;
}

function campaignRecord(item, canManage, state) {
  const actions = canManage ? item.action_capabilities : [];
  return `<article><header><div><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(shortId(item.campaign_id))} · ${escapeHtml(item.channel_code)}</small></div>${status(item.status)}</header>
    <dl><div><dt>Target</dt><dd>${escapeHtml(label(item.target_stage))}</dd></div><div><dt>Conversions</dt><dd>${number(item.conversion_count)}</dd></div><div><dt>Spend</dt><dd>${money(item.spend_minor, item.currency)}</dd></div><div><dt>Outcome</dt><dd>${nullablePercent(item.outcome_bps)}</dd></div></dl>
    ${actions.includes("update") ? campaignEditor(item, state) : ""}
    ${actions.filter((action) => action !== "update").length ? `<div class="platform-growth-actions">${actions.filter((action) => action !== "update").map((action) => transitionForm("campaign", action, item.campaign_id, item.revision, state)).join("")}</div>` : ""}
  </article>`;
}

function campaignEditor(item, state) {
  const create = !item;
  return `<form class="platform-growth-command" data-platform-growth-campaign-form autocomplete="off">
    <h3>${create ? "创建活动" : "更新活动"}</h3><input type="hidden" name="operation" value="${create ? "create" : "update"}" />
    ${create ? "" : `<input type="hidden" name="campaignId" value="${escapeAttribute(item.campaign_id)}" />`}<input type="hidden" name="expectedRevision" value="${create ? 0 : integerValue(item.revision)}" />
    <label><span>Name</span><input name="displayName" maxlength="120" value="${escapeAttribute(item?.display_name ?? "")}" required /></label>
    <label><span>Source ID</span><input name="sourceId" pattern="acs_[0-9a-f]{32}" value="${escapeAttribute(item?.acquisition_source_id ?? "")}" required /></label>
    <label><span>Channel</span><input name="channelCode" pattern="[a-z0-9][a-z0-9._-]{0,39}" value="${escapeAttribute(item?.channel_code ?? "")}" required /></label>
    ${select("Target", "targetStage", item?.target_stage ?? "activated", STAGES.map((stage) => [stage, label(stage)]))}
    ${dateTimeInput("Starts", "startsAt", item?.starts_at)}${dateTimeInput("Ends", "endsAt", item?.ends_at)}
    <label><span>Budget minor</span><input name="budgetMinor" type="number" min="0" value="${numberInput(item?.budget_minor, 0)}" required /></label>
    <label><span>Currency</span><input name="currency" pattern="[A-Z]{3}" maxlength="3" value="${escapeAttribute(item?.currency ?? "CNY")}" required /></label>
    ${stepUpFields()}${confirmation(create ? "创建" : "更新", busy(state, "write-campaign"))}
  </form>`;
}

function experimentsView(state, permissions) {
  const filters = state.filters?.experiments ?? {};
  const response = state.data?.experiments;
  const canManage = permissions.has("platform.growth.experiment.manage");
  return `${sectionHeading("Experiment registry", "增长实验", response)}
    <form class="platform-filter-form platform-growth-filter platform-growth-filter-compact" data-platform-growth-filter-form data-growth-tab="experiments" autocomplete="off">
      ${select("Status", "status", filters.status ?? "", [["", "All"], ["draft", "Draft"], ["scheduled", "Scheduled"], ["running", "Running"], ["paused", "Paused"], ["decided", "Decided"], ["stopped", "Stopped"], ["archived", "Archived"]])}
      <label><span>Surface</span><input name="surface" maxlength="80" value="${escapeAttribute(filters.surface ?? "")}" /></label>
      <label><span>Metric</span><input name="metric" maxlength="80" value="${escapeAttribute(filters.metric ?? "")}" /></label>
      ${dateInput("From", "from", filters.from)}${dateInput("To", "to", filters.to)}
      <button class="platform-primary" type="submit" ${busy(state, "read-experiments")}>筛选</button>
    </form>
    ${canManage ? experimentEditor(null, state) : ""}
    <div class="platform-growth-records">${response?.items?.map((item) => experimentRecord(item, canManage, response.source_freshness?.watermark, state)).join("") ?? loadingOrEmpty(state, "read-experiments", "暂无实验。")}</div>
    ${pagination(state, "experiments")}`;
}

function experimentRecord(item, canManage, watermark, state) {
  const actions = canManage ? item.action_capabilities : [];
  return `<article><header><div><strong>${escapeHtml(item.hypothesis_code)}</strong><small>${escapeHtml(item.surface_code)} · ${escapeHtml(shortId(item.experiment_id))}</small></div>${status(item.status)}</header>
    <dl><div><dt>Metric</dt><dd>${escapeHtml(item.primary_metric)}</dd></div><div><dt>Assigned</dt><dd>${number(item.assigned_count)}</dd></div><div><dt>Control / variant</dt><dd>${number(item.control_count)} / ${number(item.variant_count)}</dd></div><div><dt>Decision</dt><dd>${escapeHtml(label(item.decision))}</dd></div></dl>
    ${actions.includes("update") ? experimentEditor(item, state) : ""}
    ${actions.filter((action) => action !== "update").length ? `<div class="platform-growth-actions">${actions.filter((action) => action !== "update").map((action) => transitionForm("experiment", action, item.experiment_id, item.revision, state, ["start", "decide"].includes(action) ? watermark : null)).join("")}</div>` : ""}
  </article>`;
}

function experimentEditor(item, state) {
  const create = !item;
  return `<form class="platform-growth-command platform-growth-experiment-command" data-platform-growth-experiment-form autocomplete="off">
    <h3>${create ? "创建实验" : "更新实验"}</h3><input type="hidden" name="operation" value="${create ? "create" : "update"}" />
    ${create ? "" : `<input type="hidden" name="experimentId" value="${escapeAttribute(item.experiment_id)}" />`}<input type="hidden" name="expectedRevision" value="${create ? 0 : integerValue(item.revision)}" />
    ${textInput("Hypothesis", "hypothesisCode", item?.hypothesis_code ?? "", 80)}${textInput("Surface", "surfaceCode", item?.surface_code ?? "", 80)}${textInput("Metric", "primaryMetric", item?.primary_metric ?? "", 80)}
    ${textInput("Control", "controlCode", item?.control_code ?? "control", 40)}${textInput("Variant", "variantCode", item?.variant_code ?? "variant", 40)}
    <label><span>Control bps</span><input name="controlAllocationBps" type="number" min="1" max="9999" value="${numberInput(item?.control_allocation_bps, 5000)}" required /></label>
    <label><span>Variant bps</span><input name="variantAllocationBps" type="number" min="1" max="9999" value="${numberInput(item?.variant_allocation_bps, 5000)}" required /></label>
    ${textInput("Eligibility version", "eligibilityPolicyVersion", item?.eligibility_policy_version ?? "growth-eligibility-v2", 80)}${textInput("Consent version", "consentVersion", item?.consent_version ?? "analytics-v1", 80)}
    <label><span>Sample cap</span><input name="sampleCap" type="number" min="20" max="10000000" value="${numberInput(item?.sample_cap, 1000)}" required /></label>
    ${dateTimeInput("Starts", "startsAt", item?.starts_at)}${dateTimeInput("Ends", "endsAt", item?.ends_at)}
    ${textInput("Guardrail", "guardrailCode", item?.guardrail_code ?? "safety.default", 80)}${textInput("Decision policy", "decisionPolicyVersion", item?.decision_policy_version ?? "fixed-horizon-v1", 80)}
    ${stepUpFields()}${confirmation(create ? "创建" : "更新", busy(state, "write-experiment"))}
  </form>`;
}

function attributionView(state, permissions) {
  const filters = state.filters?.attribution ?? {};
  const response = state.data?.attribution;
  const channelOnly = permissions.has("platform.growth.channel_attribution_read")
    && !permissions.has("platform.growth.read");
  return `${sectionHeading("Attribution projection", "归因结果", response)}
    <form class="platform-filter-form platform-growth-filter" data-platform-growth-filter-form data-growth-tab="attribution" autocomplete="off">
      ${select("View", "view", channelOnly ? "channel_summary" : (filters.view ?? "summary"), channelOnly
        ? [["channel_summary", "Channel summary"]]
        : [["summary", "Summary"], ["campaign", "Campaign"], ["channel", "Channel"]])}
      ${dateInput("From", "from", filters.from)}${dateInput("To", "to", filters.to)}
      ${select("Stage", "stage", filters.stage ?? "", [["", "All"], ...STAGES.map((stage) => [stage, label(stage)])])}
      ${channelOnly ? "" : `<label><span>Source ID</span><input name="source" pattern="acs_[0-9a-f]{32}" value="${escapeAttribute(filters.source ?? "")}" /></label><label><span>Channel</span><input name="channel" maxlength="80" value="${escapeAttribute(filters.channel ?? "")}" /></label><label><span>Campaign ID</span><input name="campaign" pattern="gca_[0-9a-f]{32}" value="${escapeAttribute(filters.campaign ?? "")}" /></label>`}
      <button class="platform-primary" type="submit" ${busy(state, "read-attribution")}>查询</button>
    </form>
    ${response ? attributionTable(response.items, channelOnly) : loadingOrEmpty(state, "read-attribution", "暂无归因结果。")}
    ${pagination(state, "attribution")}`;
}

function attributionTable(items, channelOnly) {
  if (!items.length) return empty("暂无归因结果。");
  return `<div class="platform-table-shell"><table class="platform-growth-table"><thead><tr><th>Safe ID</th><th>Stage</th><th>Conversions</th>${channelOnly ? "" : "<th>Value</th>"}<th>Coverage</th><th>Source</th></tr></thead><tbody>
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
    <div class="platform-section-heading"><div><p>Controlled artifact</p><h2>导出操作</h2></div>${current ? status(current.status) : ""}</div>
    <div class="platform-growth-export-grid">
      ${canRequest ? exportRequestForm(state) : ""}
      <div class="platform-growth-export-status">
        <form data-platform-growth-export-lookup-form autocomplete="off"><label><span>Export ID</span><input name="exportId" pattern="gxp_[0-9a-f]{32}" value="${escapeAttribute(current?.export_id ?? "")}" required /></label><button type="submit" ${busy(state, "read-export")}>刷新</button></form>
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
    ${select("Dataset", "queryKind", active, Object.entries(TAB_LABELS).map(([value, text]) => [value, text]))}
    ${select("View", "view", view, [["overview", "Overview"], ["adoption", "Adoption"], ["retention", "Retention"], ["summary", "Summary"], ["campaign", "Campaign"], ["channel", "Channel"], ["channel_summary", "Channel summary"]])}
    ${dateInput("From", "fromDate", filters.from, true)}${dateInput("To", "toDate", filters.to, true)}
    ${select("Bucket", "bucket", active === "funnels" ? (filters.bucket ?? "day") : "", [["", "Not applicable"], ["day", "Day"], ["week", "Week"], ["month", "Month"]])}
    ${select("Dimension", "dimension", filters.dimension ?? "none", [["none", "None"], ["source", "Source"], ["channel", "Channel"], ["region", "Region"], ["plan_band", "Plan band"]])}
    ${textInput("Formula", "formulaVersion", formulaFor(active), 80)}
    ${select("Sensitivity", "sensitivity", "aggregate", [["aggregate", "Aggregate"], ["high_cardinality", "High cardinality"]])}
    ${stepUpFields()}${confirmation("提交申请", busy(state, "write-export"))}</form>`;
}

function exportFacts(item) {
  return `<dl><div><dt>Export</dt><dd><code>${escapeHtml(shortId(item.export_id))}</code></dd></div><div><dt>Dataset</dt><dd>${escapeHtml(label(item.query_kind))}</dd></div><div><dt>Rows / cells</dt><dd>${nullableNumber(item.row_count)} / ${nullableNumber(item.cell_count)}</dd></div><div><dt>Revision</dt><dd>${number(item.revision)}</dd></div><div><dt>Expires</dt><dd>${escapeHtml(formatTime(item.expires_at))}</dd></div><div><dt>Downloads</dt><dd>${number(item.download_count)}</dd></div></dl>`;
}

function exportApprovalForm(item, state) {
  return `<form class="platform-growth-inline-form" data-platform-growth-export-approval-form autocomplete="off"><input type="hidden" name="exportId" value="${escapeAttribute(item.export_id)}" /><input type="hidden" name="expectedRevision" value="${integerValue(item.revision)}" />
    ${select("Decision", "operation", "approve", [["approve", "Approve"], ["reject", "Reject"]])}${stepUpFields()}${confirmation("提交审批", busy(state, "write-approval"))}</form>`;
}

function exportDownloadForm(item, state) {
  return `<form class="platform-growth-inline-form" data-platform-growth-export-download-form autocomplete="off"><input type="hidden" name="exportId" value="${escapeAttribute(item.export_id)}" />${stepUpFields()}${confirmation("下载 CSV", busy(state, "write-download"))}</form>`;
}

function transitionForm(kind, action, id, revision, state, watermark = null) {
  return `<form data-platform-growth-${kind}-transition-form autocomplete="off"><input type="hidden" name="operation" value="${escapeAttribute(action)}" /><input type="hidden" name="${kind}Id" value="${escapeAttribute(id)}" /><input type="hidden" name="expectedRevision" value="${integerValue(revision)}" />${watermark ? `<input type="hidden" name="sourceWatermark" value="${escapeAttribute(watermark)}" />` : ""}${stepUpFields()}${confirmation(label(action), busy(state, `write-${kind}`))}</form>`;
}

function sectionHeading(kicker, title, response) {
  return `<div class="platform-section-heading"><div><p>${escapeHtml(kicker)}</p><h2>${escapeHtml(title)}</h2></div><span>${response ? `${number(response.items.length)} rows · ${escapeHtml(formatTime(response.generated_at))}` : "No projection loaded"}</span></div>`;
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
  return `${select("Step-up", "method", "totp", [["totp", "TOTP"], ["webauthn", "Security key"]])}<label data-platform-growth-step-up-code><span>TOTP</span><input type="password" name="verificationCode" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" data-platform-sensitive required /></label>`;
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
