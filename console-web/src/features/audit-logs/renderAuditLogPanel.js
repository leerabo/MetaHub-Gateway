import {
  authorizationIdentitySnapshot,
  hasPermission,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 审计页面只改善授权用户体验，真实读取权限和 tenant_id 隔离始终由后端 /audit-logs 校验。
const ACTION_OPTIONS = [
  "provider.created",
  "provider.updated",
  "provider_key.created",
  "provider_key.updated",
  "virtual_key.created",
  "virtual_key.updated",
  "budget_rule.created",
  "budget_rule.updated",
  "dlp_rule.created",
  "dlp_rule.updated",
  "webhook_delivery.replayed",
  "webhook_dead_letter.archived",
  "webhook_endpoint.secret_rotated",
  "usage_export.requested",
  "usage_export.downloaded",
  "policy.created",
  "policy.updated",
  "policy_snapshot.published",
  "policy_snapshot.rolled_back",
  "policy.dry_run_executed",
  "routing_policy.created",
  "routing_policy.updated",
  "model_quality_score.updated",
  "cache_affinity.created",
  "cache_affinity.updated",
  "finops.adjustment_created",
  "cost_anomaly.acknowledged",
  "cost_anomaly.dismissed",
  "cost_anomaly.temporary_rate_limit_applied",
  "cost_anomaly.temporary_rate_limit_released",
  "audit.exported",
];

// 对象类型选项与后端稳定白名单一致，前端不提供任意对象类型输入。
const OBJECT_TYPE_OPTIONS = [
  "provider",
  "provider_key",
  "virtual_key",
  "budget_rule",
  "dlp_rule",
  "webhook_delivery",
  "webhook_dead_letter",
  "webhook_endpoint",
  "usage_export_job",
  "policy",
  "policy_snapshot",
  "policy_explanation",
  "routing_policy",
  "model_quality_score",
  "cache_affinity",
  "finops_adjustment",
  "cost_anomaly",
  "audit_export",
];

const ACTION_LABELS = Object.freeze({
  "provider.created": "模型服务 · 已创建",
  "provider.updated": "模型服务 · 已更新",
  "provider_key.created": "模型服务凭据 · 已创建",
  "provider_key.updated": "模型服务凭据 · 已更新",
  "virtual_key.created": "组织 KEY · 已创建",
  "virtual_key.updated": "组织 KEY · 已更新",
  "budget_rule.created": "预算规则 · 已创建",
  "budget_rule.updated": "预算规则 · 已更新",
  "dlp_rule.created": "内容安全规则 · 已创建",
  "dlp_rule.updated": "内容安全规则 · 已更新",
  "webhook_delivery.replayed": "通知 · 已重试",
  "webhook_dead_letter.archived": "通知记录 · 已归档",
  "webhook_endpoint.secret_rotated": "通知地址 · 凭据已更新",
  "usage_export.requested": "用量导出 · 已申请",
  "usage_export.downloaded": "用量导出 · 已下载",
  "policy.created": "访问策略 · 已创建",
  "policy.updated": "访问策略 · 已更新",
  "policy_snapshot.published": "访问策略 · 已发布",
  "policy_snapshot.rolled_back": "访问策略 · 已回退",
  "policy.dry_run_executed": "访问策略 · 已试运行",
  "routing_policy.created": "路由规则 · 已创建",
  "routing_policy.updated": "路由规则 · 已更新",
  "model_quality_score.updated": "模型质量记录 · 已更新",
  "cache_affinity.created": "缓存偏好 · 已创建",
  "cache_affinity.updated": "缓存偏好 · 已更新",
  "finops.adjustment_created": "账务调整 · 已创建",
  "cost_anomaly.acknowledged": "成本异常 · 已确认",
  "cost_anomaly.dismissed": "成本异常 · 已忽略",
  "cost_anomaly.temporary_rate_limit_applied": "成本异常 · 已启用临时限流",
  "cost_anomaly.temporary_rate_limit_released": "成本异常 · 已解除临时限流",
  "audit.exported": "审计记录 · 已导出",
});
const OBJECT_LABELS = Object.freeze({
  provider: "模型服务",
  provider_key: "模型服务凭据",
  virtual_key: "组织 KEY",
  budget_rule: "预算规则",
  dlp_rule: "内容安全规则",
  webhook_delivery: "通知记录",
  webhook_dead_letter: "失败通知记录",
  webhook_endpoint: "通知地址",
  usage_export_job: "用量导出",
  policy: "访问策略",
  policy_snapshot: "访问策略版本",
  policy_explanation: "访问策略说明",
  routing_policy: "路由规则",
  model_quality_score: "模型质量记录",
  cache_affinity: "缓存偏好",
  finops_adjustment: "账务调整",
  cost_anomaly: "成本异常",
  audit_export: "审计导出",
});

/**
 * 从认证上下文生成 PAGE-084 导出使用的完整身份快照。
 *
 * tenant、用户、角色、Session ID 和 Session revision 全部参与比较；当前后端尚未返回 revision 时保留
 * 空槽位，未来原地续期一旦提供 revision 就会自然淘汰旧 owner。非 admin 或身份不完整时失败关闭。
 */
export function auditExportIdentitySnapshot(authContext) {
  // owner 绑定后端权限 revision 与指纹，撤销 audit.export 后旧下载回调会立即失败关闭。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.AUDIT_EXPORT);
}

/**
 * 创建审计导出独立生命周期。
 *
 * 导出 generation 不复用 15 秒列表 refreshSequence，避免无副作用刷新误取消合法下载；身份、导航、
 * controller 和内部 generation 任一变化都会淘汰迟到成功或失败。Blob 从不保存在生命周期中。
 */
export function createAuditExportLifecycle() {
  let generation = 0;
  let current = null;
  return {
    /** 显式点击时创建唯一 owner；忙碌、非审计导航或不完整 admin 身份都不创建请求。 */
    begin({ identity, navigation }) {
      if (current || !identity || navigation !== "audit-logs") {
        return null;
      }
      const controller = new AbortController();
      generation += 1;
      current = Object.freeze({
        identity: String(identity),
        navigation,
        generation,
        controller,
        signal: controller.signal,
      });
      return current;
    },

    /** 核对完整身份、实际导航、对象引用、generation 与 AbortSignal，旧回调不得写反馈或触发下载。 */
    isCurrent(operation, owner) {
      return Boolean(
        operation
        && current === operation
        && operation.generation === generation
        && operation.identity === String(owner?.identity ?? "")
        && operation.navigation === String(owner?.navigation ?? "")
        && operation.controller === current.controller
        && !operation.signal.aborted,
      );
    },

    /** 只释放调用方自己的 owner；旧 finally 不能结束后来创建的新操作。 */
    finish(operation) {
      if (current === operation) {
        current = null;
      }
    },

    /** 离页、注销、401、pagehide 或身份变化时先推进 generation，再同步 abort 并销毁 owner。 */
    clear() {
      generation += 1;
      current?.controller.abort();
      current = null;
    },

    /** 返回是否存在活动导出，供按钮固定 busy 状态和双击门禁使用。 */
    isBusy() {
      return Boolean(current);
    },
  };
}

/**
 * 渲染管理员审计日志面板。
 *
 * 列表响应已经包含安全 before/after 摘要，展开区域不得再发详情请求，也不得尝试展示
 * Provider Key、Virtual Key、DLP pattern、Prompt 或响应正文等不在审计合同中的字段。
 */
export function renderAuditLogPanel(auditLogsState, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.AUDIT_READ)) {
    return "";
  }
  const canExport = hasPermission(authContext, PERMISSIONS.AUDIT_EXPORT);
  if (!auditLogsState) {
    return renderLoadingState();
  }
  // 错误态必须保留筛选表单与重置入口，管理员才能清除导致失败的条件。
  if (auditLogsState.state === "error") {
    return renderErrorState(auditLogsState, canExport);
  }

  const logs = auditLogsState.audit_logs ?? [];
  return `
    ${renderHeading(logs.length, auditLogsState.total ?? 0, auditLogsState.exportState, canExport)}
    ${renderFilterForm(auditLogsState.filters ?? {})}
    ${renderAuditExportFeedback(auditLogsState.exportState)}
    ${renderAuditTable(logs)}
    ${renderPager(auditLogsState)}
  `;
}

/**
 * 加载态固定占用内容区域，避免定时刷新和导航切换时出现布局跳动。
 */
function renderLoadingState() {
  return `
    <div class="panel-heading"><h2 id="audit-logs-title">审计日志</h2></div>
    <div class="empty-state">正在读取审计日志。</div>
  `;
}

/**
 * 统一错误态只展示可行动的提示，后台日志保留完整定位信息。
 */
function renderErrorState(state, canExport) {
  return `
    ${renderHeading(0, 0, state.exportState, canExport)}
    ${renderFilterForm(state.filters ?? {})}
    ${renderAuditExportFeedback(state.exportState)}
    <div class="empty-state error-state" role="alert">
      <strong>${escapeHtml(state.message ?? "审计日志读取失败")}</strong>
      <span>请稍后重试；如果问题持续，请联系管理员。</span>
    </div>
  `;
}

/**
 * 渲染审计页标题和显式导出命令。
 * 按钮只存在于 admin 面板中，固定 aria-busy 与尺寸；真实权限仍由后端 require_audit_exporter 校验。
 */
function renderHeading(visibleCount, total, exportState = {}, canExport = false) {
  const busy = exportState?.status === "exporting";
  return `
    <div class="panel-heading audit-logs-heading">
      <div>
        <h2 id="audit-logs-title">审计日志</h2>
        <p>管理操作记录</p>
      </div>
      <div class="audit-log-commands">
        <span class="audit-logs-count">${escapeHtml(visibleCount)} / ${escapeHtml(total)}</span>
        ${canExport ? `<button type="button" id="audit-log-export-csv" class="audit-export-button" title="导出当前筛选的 CSV" aria-label="导出当前筛选的 CSV" aria-busy="${busy}" ${busy ? "disabled" : ""}><span aria-hidden="true">↓</span><span>导出 CSV</span></button>` : ""}
      </div>
    </div>
  `;
}

/**
 * 渲染导出专属 aria-live 反馈。
 * 成功只展示导出完成和行数；内部导出编号、摘要和请求号不进入用户界面。
 */
function renderAuditExportFeedback(exportState = {}) {
  const status = exportState?.status ?? "idle";
  if (status === "exporting") {
    return `<div class="audit-export-feedback status-exporting" data-audit-export-feedback role="status" aria-live="polite">正在生成审计 CSV</div>`;
  }
  if (status === "completed") {
    return `
      <div class="audit-export-feedback status-completed" data-audit-export-feedback role="status" aria-live="polite">
        <span>行数: <code>${escapeHtml(exportState.rowCount ?? 0)}</code></span>
        <span>导出已准备完成</span>
      </div>
    `;
  }
  // 错误反馈只呈现固定提示，绝不把服务端内部编号回显到 DOM。
  if (status === "error") {
    return `
      <div class="audit-export-feedback status-error" data-audit-export-feedback role="alert" aria-live="polite">
        <strong>审计 CSV 导出失败</strong>
        <span>请稍后重试；如果问题持续，请联系管理员。</span>
      </div>
    `;
  }
  return `<div class="audit-export-feedback" data-audit-export-feedback role="status" aria-live="polite" hidden></div>`;
}

/**
 * 筛选字段与 GET /audit-logs 参数一一对应；表单只采集条件，offset 归零由 main.js
 * 的提交处理完成，避免旧页码套用到新条件后产生误导性空列表。
 */
function renderFilterForm(filters) {
  return `
    <form class="audit-log-filter-form" id="audit-log-filter-form">
      <label><span>开始时间</span><input type="datetime-local" name="createdFrom" value="${escapeAttribute(toDateTimeLocal(filters.createdFrom))}" /></label>
      <label><span>结束时间</span><input type="datetime-local" name="createdTo" value="${escapeAttribute(toDateTimeLocal(filters.createdTo))}" /></label>
      <label><span>动作</span><select name="action">${renderOptions(ACTION_OPTIONS, filters.action, ACTION_LABELS)}</select></label>
      <label><span>对象类型</span><select name="objectType">${renderOptions(OBJECT_TYPE_OPTIONS, filters.objectType, OBJECT_LABELS)}</select></label>
      <label><span>操作者</span><input name="actorUserId" value="${escapeAttribute(filters.actorUserId ?? "")}" placeholder="账号或邮箱" /></label>
      <label><span>对象</span><input name="objectId" value="${escapeAttribute(filters.objectId ?? "")}" placeholder="模型服务、模型或成员" /></label>
      <div class="audit-log-filter-actions">
        <button type="submit">查询</button>
        <button type="button" id="audit-log-filter-reset">重置</button>
      </div>
    </form>
  `;
}

/**
 * 渲染桌面表头和日志记录；每个移动端字段都带 data-label，窄屏隐藏表头后仍保留语义。
 */
function renderAuditTable(logs) {
  return `
    <div class="audit-log-table" role="table" aria-label="当前租户管理操作审计日志">
      <div class="audit-log-row audit-log-head" role="row">
        <span>时间</span><span>操作者</span><span>动作</span><span>对象</span>
        <span>变更字段</span><span>摘要</span>
      </div>
      ${logs.length ? logs.map(renderAuditRow).join("") : renderEmptyRow()}
    </div>
  `;
}

/**
 * 单条记录只展示管理员需要的业务事实；对象、操作者和请求定位 ID 保留在状态层。
 */
function renderAuditRow(item) {
  return `
    <article class="audit-log-row" role="row">
      <span data-label="时间" class="audit-cell-value"><time datetime="${escapeAttribute(item.created_at)}">${escapeHtml(formatDateTime(item.created_at))}</time></span>
      <span data-label="操作者" class="audit-cell-value"><strong>${escapeHtml(item.actor_email ?? "管理员")}</strong></span>
      <span data-label="动作" class="audit-cell-value">${escapeHtml(ACTION_LABELS[item.action] ?? "已完成管理操作")}</span>
      <span data-label="对象" class="audit-cell-value">
        <span class="audit-object"><strong>${escapeHtml(`${OBJECT_LABELS[item.object_type] ?? "对象"}${displayObjectName(item) ? ` / ${displayObjectName(item)}` : ""}`)}</strong></span>
      </span>
      <span data-label="变更字段" class="audit-cell-value"><span class="audit-changed-fields">${renderChangedFields(item.changed_fields)}</span></span>
      <details data-label="摘要" class="audit-log-summary">
        <summary>展开摘要</summary>
        <div class="audit-summary-grid">
          ${renderSummaryBlock("变更前", item.before_summary)}
          ${renderSummaryBlock("变更后", item.after_summary)}
        </div>
      </details>
    </article>
  `;
}

// 对象名称有时由后端附带短 ID 便于排障；业务页面只显示名称，避免把内部标识误当成业务字段。
function displayObjectName(item) {
  const value = String(item?.object_name ?? "").trim();
  if (!value) return "";
  return value
    .replace(/\s*\[[a-f0-9]{6,16}\]\s*$/i, "")
    .replace(/\s*\([a-f0-9]{6,16}\)\s*$/i, "")
    .trim();
}

/**
 * 空列表仍保留稳定表格区域，分页文本会明确显示 0-0 / 0。
 */
function renderEmptyRow() {
  return `<div class="audit-log-row audit-log-empty" role="row"><span>当前条件下没有审计日志。</span></div>`;
}

/**
 * 上一页/下一页使用固定尺寸图标按钮，并通过 title 与 aria-label 提供可访问名称。
 */
function renderPager(state) {
  const limit = Number(state.limit ?? 20);
  const offset = Number(state.offset ?? 0);
  const total = Number(state.total ?? 0);
  // 数据在翻页期间缩减时，越界页显示 0-0 并保留“上一页”，不能出现 41-10 这类反向范围。
  const pageHasRows = total > 0 && offset < total;
  const start = pageHasRows ? offset + 1 : 0;
  const end = pageHasRows ? Math.min(offset + limit, total) : 0;
  return `
    <div class="audit-log-pager">
      <button type="button" class="audit-pager-button" id="audit-log-prev" title="上一页" aria-label="上一页" ${offset > 0 ? "" : "disabled"}>‹</button>
      <span>${escapeHtml(start)}-${escapeHtml(end)} / ${escapeHtml(total)}</span>
      <button type="button" class="audit-pager-button" id="audit-log-next" title="下一页" aria-label="下一页" ${offset + limit < total ? "" : "disabled"}>›</button>
    </div>
  `;
}

/**
 * 摘要通过 JSON.stringify 生成稳定缩进文本，随后进行 HTML 转义，恶意名称或字符串值
 * 只能作为文本展示，不能注入标签或属性。
 */
function renderSummaryBlock(title, summary) {
  return `<section><h3>${escapeHtml(title)}</h3><pre data-user-content="true">${escapeHtml(JSON.stringify(summary ?? {}, null, 2))}</pre></section>`;
}

/**
 * changed_fields 只显示字段名；空数组表示后端记录异常时采用短横线兜底。
 */
function renderChangedFields(fields) {
  if (!Array.isArray(fields) || fields.length === 0) {
    return "-";
  }
  const labels = { status: "状态", name: "名称", role: "职责", amount: "金额", limit: "限制", configuration: "配置" };
  const known = [...new Set(fields.map((field) => labels[field]).filter(Boolean))];
  return known.length ? known.map((label) => `<span>${escapeHtml(label)}</span>`).join("、") : "已更新";
}

/**
 * 动作和对象类型使用已知稳定枚举，避免自由输入造成拼写错误；空选项表示不过滤。
 */
function renderOptions(options, selectedValue, labels = {}) {
  return ["", ...options]
    .map((value) => {
      const selected = value === (selectedValue ?? "") ? " selected" : "";
      return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(value ? labels[value] ?? "其他操作" : "全部")}</option>`;
    })
    .join("");
}

/**
 * API 时间按 UTC 保存；datetime-local 需要转换为浏览器本地时间后回填，避免刷新后显示偏移。
 */
function toDateTimeLocal(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

/**
 * 使用浏览器本地时区展示 UTC 审计时间；非法值不抛异常，保留原始安全文本便于定位。
 */
function formatDateTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value ?? "-" : date.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 转义 HTML 文本，所有后端返回和筛选回填都必须经过该函数。
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
 * 属性值沿用完整 HTML 转义，阻止 ID、时间或筛选值闭合属性并注入 DOM。
 */
function escapeAttribute(value) {
  return escapeHtml(value);
}
