/**
 * 模块职责：渲染 PAGE-074 FinOps 正式账本和 PAGE-075 分录详情。
 * 安全边界：页面只消费后端已脱敏 DTO；前端角色判断只改善体验，所有读取、调整、
 * 冲正、租户隔离和字段所有权必须由 Control API 强制执行。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 财务类型和证据状态使用稳定中文标签，未知值仍会安全显示原始枚举，便于兼容后续只读类型。
const ENTRY_TYPE_LABELS = {
  usage_charge: "Usage 扣费",
  manual_debit: "人工借记",
  manual_credit: "人工贷记",
  reversal: "冲正",
  gift_credit: "赠送额度",
  trial_credit: "试用额度",
};
// 价格证据标签明确区分未定价和仅历史金额，避免页面把零金额误解为免费或可按当前价格重算。
const EVIDENCE_LABELS = {
  priced: "已定价快照",
  unpriced: "当时无可用价格",
  legacy_amount_only: "仅历史金额证据",
  not_applicable: "不适用",
};
// 额度标签只展示后端冻结枚举，浏览器不能通过该映射推导或修改真实财务归属。
const QUOTA_LABELS = {
  paid: "付费",
  free: "免费",
  trial: "试用",
  gift: "赠送",
};

/**
 * 渲染管理员 FinOps 工作区。
 *
 * @param {object|null} finOpsState 列表、筛选、选择、分页和调整对话框状态。
 * @param {object|null} authContext 当前认证上下文。
 * @returns {string} 已完整转义的 HTML；非管理员返回空字符串。
 */
export function renderFinOpsLedger(finOpsState, authContext) {
  // 缺少后端账本读权限时零 DOM；FinanceAdmin 等角色不再被旧 admin 判断错误拦截。
  if (!hasPermission(authContext, PERMISSIONS.BILLING_FINOPS_READ)) {
    return "";
  }
  const canWrite = hasPermission(authContext, PERMISSIONS.BILLING_FINOPS_WRITE);
  if (!finOpsState) {
    return renderLoadingState();
  }

  const filters = finOpsState.filters ?? {};
  // 错误态只展示统一 code、文案和 request_id，不渲染可能残留的旧租户列表或详情。
  if (finOpsState.state === "error") {
    return renderErrorState(finOpsState, filters);
  }

  const entries = Array.isArray(finOpsState.ledger_entries) ? finOpsState.ledger_entries : [];
  const selectedEntry = finOpsState.selectedEntry
    ?? entries.find((entry) => entry.finops_ledger_entry_id === finOpsState.selectedEntryId)
    ?? null;
  return `
    <div class="panel-heading finops-heading">
      <div>
        <h2 id="finops-ledger-title">FinOps 正式账本</h2>
        <p>按入账时的原币金额和价格证据追踪 Usage、调整与冲正。</p>
      </div>
      <div class="finops-heading-actions">
        <span class="finops-entry-count">${escapeHtml(finOpsState.total ?? entries.length)} 条</span>
        ${canWrite ? '<button type="button" id="finops-open-adjustment">新增调整</button>' : ""}
      </div>
    </div>
    ${renderFeedback(finOpsState.feedback)}
    ${renderActionError(finOpsState.actionError)}
    ${renderFilterForm(filters)}
    ${renderTotals(finOpsState.totals)}
    <div class="finops-workspace${selectedEntry ? " has-detail" : ""}">
      ${renderLedgerTable(entries, selectedEntry?.finops_ledger_entry_id, canWrite)}
      ${renderDetailPanel(selectedEntry)}
    </div>
    ${renderPager(finOpsState)}
    ${canWrite ? renderAdjustmentDialog(finOpsState.dialog) : ""}
  `;
}

/**
 * 渲染首次读取状态，保持标题和工作区高度稳定，避免导航后页面跳动。
 */
function renderLoadingState() {
  return `
    <div class="panel-heading finops-heading">
      <div><h2 id="finops-ledger-title">FinOps 正式账本</h2><p>正在读取当前租户的正式分录。</p></div>
    </div>
    <div class="finops-loading" role="status" aria-live="polite">正在加载账本...</div>
  `;
}

/**
 * 渲染读取失败状态；保留筛选和重置入口，避免错误筛选把管理员困在空白页。
 */
function renderErrorState(state, filters) {
  return `
    <div class="panel-heading finops-heading">
      <div><h2 id="finops-ledger-title">FinOps 正式账本</h2><p>当前租户账本暂不可用。</p></div>
    </div>
    <div class="finops-error" role="alert">
      <strong>${escapeHtml(state.code ?? "finops.ledger_unavailable")}</strong>
      <span>${escapeHtml(state.message ?? "账本读取失败")}</span>
      <code>request_id: ${escapeHtml(state.requestId ?? "-")}</code>
    </div>
    ${renderFilterForm(filters)}
  `;
}

/**
 * 渲染后端白名单对应的账本筛选表单。
 * tenant_id、actor、任意排序字段和 cursor 不进入表单，cursor 仅由主应用内存维护。
 */
function renderFilterForm(filters) {
  return `
    <form class="finops-filter-form" id="finops-filter-form">
      <label><span>开始日期</span><input type="date" name="startDate" value="${escapeAttribute(filters.startDate ?? "")}" /></label>
      <label><span>结束日期</span><input type="date" name="endDate" value="${escapeAttribute(filters.endDate ?? "")}" /></label>
      <label>
        <span>分录类型</span>
        <select name="entryType">${renderOptions(
          ["usage_charge", "manual_debit", "manual_credit", "reversal", "gift_credit", "trial_credit"],
          filters.entryType,
          ENTRY_TYPE_LABELS,
        )}</select>
      </label>
      <label><span>方向</span><select name="direction">${renderOptions(["debit", "credit"], filters.direction)}</select></label>
      <label><span>额度类型</span><select name="quotaType">${renderOptions(["paid", "free", "trial", "gift"], filters.quotaType, QUOTA_LABELS)}</select></label>
      <label><span>request_id</span><input name="requestId" value="${escapeAttribute(filters.requestId ?? "")}" placeholder="req_..." /></label>
      <label><span>价格快照</span><input name="priceSnapshotId" value="${escapeAttribute(filters.priceSnapshotId ?? "")}" placeholder="prs_..." /></label>
      <label><span>关联分录</span><input name="relatedEntryId" value="${escapeAttribute(filters.relatedEntryId ?? "")}" placeholder="fle_..." /></label>
      <div class="finops-filter-actions">
        <button type="submit">筛选</button>
        <button type="button" id="finops-filter-reset">清空</button>
      </div>
    </form>
  `;
}

/**
 * 渲染按原币分组的借记、贷记和净额汇总。
 * 所有金额按服务端字符串原样展示，浏览器不使用 Number 重算财务结果。
 */
function renderTotals(totals = {}) {
  const byCurrency = totals?.by_currency && typeof totals.by_currency === "object"
    ? totals.by_currency
    : {};
  const currencies = Object.keys(byCurrency).sort();
  return `
    <section class="finops-summary-strip" aria-label="账本汇总">
      ${currencies.length
        ? currencies.map((currency) => renderCurrencyTotal(currency, byCurrency[currency])).join("")
        : renderCurrencyTotal("CNY", { debit: "0.000000", credit: "0.000000", net: "0.000000" })}
      <div class="finops-summary-item">
        <span>可折算净成本 CNY</span><strong>${escapeHtml(totals.net_cost_cny ?? "0.000000")}</strong>
      </div>
      <div class="finops-summary-item">
        <span>未折算分录</span><strong>${escapeHtml(totals.unnormalized_entry_count ?? 0)}</strong>
      </div>
    </section>
  `;
}

/**
 * 渲染一个币种的服务端汇总，不推导汇率或改变小数位。
 */
function renderCurrencyTotal(currency, totals = {}) {
  return `
    <div class="finops-summary-item finops-currency-total">
      <span>${escapeHtml(currency)}</span>
      <strong>${escapeHtml(totals.net ?? "0.000000")}</strong>
      <em>借 ${escapeHtml(totals.debit ?? "0.000000")} / 贷 ${escapeHtml(totals.credit ?? "0.000000")}</em>
    </div>
  `;
}

/**
 * 渲染固定列账本表；每一行使用显式查看和冲正命令，避免整行点击影响键盘操作。
 */
function renderLedgerTable(entries, selectedEntryId, canWrite) {
  return `
    <div class="finops-ledger-table" role="table" aria-label="当前租户正式账本">
      <div class="finops-ledger-row finops-ledger-head" role="row">
        <span>时间</span><span>类型</span><span>方向</span><span>金额</span><span>额度</span><span>request_id</span><span>价格证据</span><span>操作</span>
      </div>
      ${entries.length ? entries.map((entry) => renderLedgerRow(entry, selectedEntryId, canWrite)).join("") : renderEmptyRow()}
    </div>
  `;
}

/**
 * 渲染一条分录的安全摘要。
 * reason、raw usage、Prompt/Response、密钥和 Endpoint 不进入列表或 data 属性。
 */
function renderLedgerRow(entry, selectedEntryId, canWrite) {
  const entryId = entry.finops_ledger_entry_id ?? "-";
  const selected = entryId === selectedEntryId;
  const canReverse = entry.entry_type !== "reversal" && String(entry.amount ?? "0") !== "0.000000";
  return `
    <div class="finops-ledger-row${selected ? " is-selected" : ""}" role="row" data-finops-ledger-row>
      <time data-label="时间" datetime="${escapeAttribute(entry.created_at ?? "")}">${escapeHtml(formatDateTime(entry.created_at))}</time>
      <span data-label="类型"><strong>${escapeHtml(ENTRY_TYPE_LABELS[entry.entry_type] ?? entry.entry_type ?? "-")}</strong><code>${escapeHtml(entryId)}</code>${renderOutputSafetyDisputeBadge(entry.output_safety_dispute)}</span>
      <span data-label="方向"><mark class="finops-direction direction-${escapeAttribute(entry.direction ?? "unknown")}">${escapeHtml(entry.direction ?? "-")}</mark></span>
      <span data-label="金额"><strong>${escapeHtml(entry.amount ?? "0.000000")}</strong> ${escapeHtml(entry.currency ?? "-")}</span>
      <span data-label="额度">${escapeHtml(QUOTA_LABELS[entry.quota_type] ?? entry.quota_type ?? "-")}</span>
      <span data-label="request_id"><code>${escapeHtml(entry.source_request_id ?? "-")}</code></span>
      <span data-label="价格证据">${renderEvidenceBadge(entry.price_evidence_status)}</span>
      <span class="finops-row-actions" data-label="操作">
        <button type="button" data-finops-entry-id="${escapeAttribute(entryId)}" aria-pressed="${selected ? "true" : "false"}">查看</button>
        ${canWrite && canReverse ? `<button type="button" data-finops-reverse-entry-id="${escapeAttribute(entryId)}">冲正</button>` : ""}
      </span>
    </div>
  `;
}

/**
 * 渲染空账本占位，保留表格网格宽度和分页位置。
 */
function renderEmptyRow() {
  return `<div class="finops-ledger-row finops-ledger-empty" role="row"><span>当前筛选下没有正式分录。</span></div>`;
}

/**
 * 渲染 PAGE-075 详情区域；未选择分录时显示稳定占位，不自动选第一行。
 */
function renderDetailPanel(entry) {
  if (!entry) {
    return `
      <aside class="finops-detail-panel is-empty" aria-label="分录详情">
        <strong>分录详情</strong><p>选择一条分录查看入账时证据。</p>
      </aside>
    `;
  }
  const legacyWarning = entry.price_evidence_status === "legacy_amount_only"
    ? `<div class="finops-evidence-warning" role="note">仅历史金额证据，不可按单价重算。</div>`
    : "";
  return `
    <aside class="finops-detail-panel" aria-labelledby="finops-detail-title">
      <div class="finops-detail-heading">
        <div><h3 id="finops-detail-title">分录详情</h3></div>
        <button type="button" id="finops-close-detail" class="finops-icon-button" aria-label="关闭详情" title="关闭详情">×</button>
      </div>
      ${legacyWarning}
      ${renderOutputSafetyDisputeNotice(entry.output_safety_dispute)}
      <dl class="finops-detail-list">
        ${renderDetailPair("分录 ID", entry.finops_ledger_entry_id)}
        ${renderDetailPair("类型", ENTRY_TYPE_LABELS[entry.entry_type] ?? entry.entry_type)}
        ${renderDetailPair("方向 / 金额", `${entry.direction ?? "-"} · ${entry.amount ?? "0.000000"} ${entry.currency ?? "-"}`)}
        ${renderRequestDetailPair(entry.source_request_id)}
        ${renderDetailPair("来源 trace_id", entry.source_trace_id)}
        ${renderDetailPair("操作 request_id", entry.operation_request_id)}
        ${renderDetailPair("操作 trace_id", entry.operation_trace_id)}
        ${renderDetailPair("Usage ID", entry.usage_ledger_id)}
        ${renderDetailPair("价格快照", entry.price_snapshot_id)}
        ${renderDetailPair("价格版本", entry.price_schema_version)}
        ${renderDetailPair("快照来源", entry.price_snapshot_source)}
        ${renderDetailPair("价格证据", EVIDENCE_LABELS[entry.price_evidence_status] ?? entry.price_evidence_status)}
        ${renderDetailPair("模型 / Provider", `${entry.model_id ?? entry.resolved_model ?? "-"} / ${entry.provider_id ?? entry.provider_code ?? "-"}`)}
        ${renderDetailPair("Project / Application", `${entry.project_id ?? "-"} / ${entry.application_id ?? "-"}`)}
        ${renderDetailPair("Environment", entry.environment_id)}
        ${renderDetailPair("Token", `${entry.total_tokens ?? 0} total / ${entry.cache_tokens ?? 0} cache`)}
        ${renderDetailPair("计量来源", entry.estimated ? "估算" : "Provider Usage")}
        ${renderDetailPair("操作者", renderActorLabel(entry))}
        ${renderDetailPair("调整原因", entry.reason)}
        ${renderDetailPair("冲正目标", entry.reversal_of_entry_id)}
        ${renderDetailPair("输出安全扫描", entry.output_safety_dispute?.output_safety_scan_id)}
        ${renderDetailPair("输出安全复核", entry.output_safety_dispute?.review_status)}
        ${renderDetailPair("入账时间", formatDateTime(entry.created_at))}
      </dl>
    </aside>
  `;
}

/**
 * 渲染 FinOps 列表最小争议徽标。
 * 参数：后端只读 dispute 投影。返回：固定中文标签；不读取规则、暴露计数或输出正文。
 */
function renderOutputSafetyDisputeBadge(dispute) {
  if (!dispute || dispute.review_status !== "dispute_pending") return "";
  const eligible = dispute.reversal_eligible === true ? " / 可冲正" : "";
  return `<mark class="finops-output-safety-badge">输出安全争议待复核${eligible}</mark>`;
}

/** 详情复用同一最小投影，零金额 dispute 不会因前端金额推导而显示“可冲正”。 */
function renderOutputSafetyDisputeNotice(dispute) {
  if (!dispute || dispute.review_status !== "dispute_pending") return "";
  const eligible = dispute.reversal_eligible === true ? "，可冲正" : "";
  return `<div class="finops-output-safety-notice" role="note">输出安全争议待复核${eligible}</div>`;
}

/**
 * 渲染详情键值对；空值不生成 DOM，减少把未授权字段误当成空值展示的机会。
 */
function renderDetailPair(label, value) {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`;
}

/**
 * 读取后端正式 DTO 的嵌套 actor，同时兼容早期扁平字段。
 * 嵌套对象优先，避免同一响应同时存在新旧字段时展示过期身份信息；所有值仍由
 * renderDetailPair 统一转义，前端不会把 actor 当成权限依据。
 *
 * @param {object|null} entry 后端返回的正式账本分录。
 * @returns {string|undefined} 可展示的操作者标识。
 */
function renderActorLabel(entry) {
  return entry?.actor?.display_name
    ?? entry?.actor?.email
    ?? entry?.actor?.user_id
    ?? entry?.actor_display_name
    ?? entry?.actor_email
    ?? entry?.actor_user_id;
}

/**
 * 渲染可跳转到 Request Logs 的来源请求。
 * ID 同时作为可见文本和固定 data 属性保存，均完整转义；导航后仍由后端按当前 tenant 查询。
 */
function renderRequestDetailPair(value) {
  if (!value) {
    return "";
  }
  return `<div><dt>来源 request_id</dt><dd><button type="button" class="finops-request-link" data-finops-request-id="${escapeAttribute(value)}">${escapeHtml(value)}</button></dd></div>`;
}

/**
 * 渲染 opaque cursor 分页。
 * cursor 本身只保存在主应用内存，不放进 data 属性、URL hash 或 DOM。
 */
function renderPager(state) {
  return `
    <div class="finops-pager">
      <button type="button" id="finops-prev" ${state.canPrevious ? "" : "disabled"}>上一页</button>
      <span>${escapeHtml(state.ledger_entries?.length ?? 0)} 条 / 当前快照共 ${escapeHtml(state.total ?? 0)} 条</span>
      <button type="button" id="finops-next" ${state.canNext ? "" : "disabled"}>下一页</button>
    </div>
  `;
}

/**
 * 渲染新增调整/冲正对话框；未打开时不创建包含 reason 的输入 DOM。
 */
function renderAdjustmentDialog(dialog) {
  if (!dialog?.open) {
    return "";
  }
  const type = dialog.adjustmentType ?? (dialog.targetEntryId ? "reversal" : "manual_credit");
  const reversal = type === "reversal";
  return `
    <dialog class="finops-adjustment-dialog" id="finops-adjustment-dialog" open aria-labelledby="finops-adjustment-title">
      <form id="finops-adjustment-form">
        <div class="finops-dialog-heading">
          <div><span>只追加财务操作</span><h3 id="finops-adjustment-title">${reversal ? "创建冲正" : "新增调整"}</h3></div>
          <button type="button" id="finops-close-adjustment" class="finops-icon-button" aria-label="关闭调整" title="关闭调整" ${dialog.submitting ? "disabled" : ""}>×</button>
        </div>
        ${dialog.error ? renderDialogError(dialog.error) : ""}
        <label>
          <span>调整类型</span>
          <select name="adjustmentType" ${reversal ? "disabled" : ""}>
            ${renderOptions(reversal ? ["reversal"] : ["manual_debit", "manual_credit", "gift_credit", "trial_credit"], type, ENTRY_TYPE_LABELS, false)}
          </select>
        </label>
        ${reversal ? `<div class="finops-target-entry"><span>目标分录</span><code>${escapeHtml(dialog.targetEntryId ?? "-")}</code></div>` : ""}
        <label><span>金额</span><input name="amount" inputmode="decimal" autocomplete="off" value="${escapeAttribute(dialog.amount ?? "")}" placeholder="0.000001" required /></label>
        ${reversal ? "" : `<label><span>关联 request_id（可选）</span><input name="relatedRequestId" value="${escapeAttribute(dialog.relatedRequestId ?? "")}" placeholder="req_..." /></label>`}
        <label><span>原因</span><textarea name="reason" rows="4" maxlength="500" required>${escapeHtml(dialog.reason ?? "")}</textarea></label>
        <div class="finops-dialog-actions">
          <button type="button" id="finops-cancel-adjustment" ${dialog.submitting ? "disabled" : ""}>取消</button>
          <button type="submit" ${dialog.submitting ? "disabled aria-busy=\"true\"" : ""}>${dialog.submitting ? "正在提交" : "确认入账"}</button>
        </div>
      </form>
    </dialog>
  `;
}

/**
 * 渲染调整失败的统一错误结构，不反射请求 body、reason 或异常堆栈。
 */
function renderDialogError(error) {
  return `
    <div class="finops-dialog-error" role="alert">
      <strong>${escapeHtml(error.code ?? "finops.adjustment_failed")}</strong>
      <span>${escapeHtml(error.message ?? "调整提交失败")}</span>
      <code>request_id: ${escapeHtml(error.requestId ?? "-")}</code>
    </div>
  `;
}

/**
 * 渲染提交成功反馈；幂等重放会明确标注，但不展示幂等键原文。
 */
function renderFeedback(feedback) {
  if (!feedback) {
    return "";
  }
  return `
    <div class="finops-feedback" role="status" aria-live="polite">
      ${escapeHtml(feedback.message ?? "调整已入账")}
      ${feedback.idempotentReplay ? "（幂等重放）" : ""}
      · request_id: ${escapeHtml(feedback.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染列表或交互级错误，用于游标过期、筛选失败等不应打开对话框的场景。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="finops-action-error" role="alert">
      ${escapeHtml(error.code ?? "finops.ledger_unavailable")} · ${escapeHtml(error.message ?? "账本操作失败")}
      · request_id: ${escapeHtml(error.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染价格证据标签；legacy_amount_only 使用独立样式，避免与正常 priced 混淆。
 */
function renderEvidenceBadge(status) {
  const safeStatus = String(status ?? "not_applicable");
  return `<mark class="finops-evidence evidence-${escapeAttribute(safeStatus)}">${escapeHtml(EVIDENCE_LABELS[safeStatus] ?? safeStatus)}</mark>`;
}

/**
 * 渲染带“全部”占位的固定枚举选项。
 */
function renderOptions(values, selectedValue, labels = {}, includeAll = true) {
  return (includeAll ? ["", ...values] : values)
    .map((value) => {
      const selected = value === (selectedValue ?? "") ? " selected" : "";
      return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(value ? labels[value] ?? value : "全部")}</option>`;
    })
    .join("");
}

/**
 * 格式化 ISO 时间；非法或空值返回稳定占位，不调用本地财务计算。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 转义服务端文本和用户输入，阻止 ID、原因或名称闭合 HTML 节点。
 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * 属性值沿用完整 HTML 转义，并额外处理反引号，保护 data-* 和 datetime 属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
