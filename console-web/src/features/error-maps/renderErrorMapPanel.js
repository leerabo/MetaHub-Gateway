/**
 * 模块职责：渲染 PAGE-081 Provider 错误归一规则列表、筛选栏与单层编辑对话框。
 * 安全边界：viewer 返回零 DOM；服务端字段统一转义；前端只编辑匹配条件，权限、租户隔离、
 * guard 不可覆盖和乐观锁均由 Control API 再次强制执行。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// tenant 规则输出只能选择这五类；鉴权、超时与 5xx 由平台 guard 独占。
export const TENANT_OUTPUT_CODES = Object.freeze([
  "gateway.upstream_rate_limited",
  "gateway.upstream_quota_exceeded",
  "gateway.upstream_model_not_found",
  "gateway.upstream_resource_not_found",
  "gateway.upstream_request_invalid",
]);

// 输出标签覆盖内建 guard 与租户可选错误码，仅用于展示，不改变 Gateway 映射结果。
const OUTPUT_LABELS = Object.freeze({
  "gateway.upstream_auth_failed": "上游鉴权失败",
  "gateway.upstream_rate_limited": "上游限流",
  "gateway.upstream_quota_exceeded": "配额不足",
  "gateway.upstream_model_not_found": "模型不存在",
  "gateway.upstream_resource_not_found": "资源不存在",
  "gateway.upstream_timeout": "上游超时",
  "gateway.upstream_request_invalid": "请求无效",
  "gateway.upstream_unavailable": "上游不可用",
  "gateway.upstream_error": "上游错误",
});

/**
 * 渲染当前租户规则页。
 * admin 可以打开写对话框；developer 只有筛选与刷新，viewer/未知角色不会产生任何页面节点。
 */
export function renderErrorMapPanel(state, authContext, filters = {}) {
  // 缺少后端读取权限必须返回零 DOM，避免隐藏按钮之外仍发生规则预取或信息暴露。
  if (!hasPermission(authContext, PERMISSIONS.PROVIDER_ERROR_MAP_READ)) {
    return "";
  }
  if (!state) {
    return renderLoading();
  }
  // 列表读取失败时只渲染统一安全错误和冻结引用，不回显底层响应或历史规则正文。
  if (state.state === "error") {
    return `${renderFatalError(state.refreshError)}${renderReference(state.reference, [], state.head)}`;
  }
  const rules = Array.isArray(state.rules) ? state.rules : [];
  const providers = Array.isArray(state.providers) ? state.providers : [];
  const canWrite = hasPermission(authContext, PERMISSIONS.PROVIDER_ERROR_MAP_WRITE);
  return `
    <div class="error-map-heading">
      <div>
        <h2 id="error-maps-title">Provider 错误规则</h2>
        <p>当前租户覆盖层与平台内置归一规则。</p>
      </div>
      ${canWrite ? '<button type="button" id="error-map-create">新建规则</button>' : ""}
    </div>
    ${renderHead(state.head)}
    ${renderReference(state.reference, rules, state.head)}
    ${renderRefreshError(state.refreshError)}
    ${renderFilters(filters, providers)}
    <div class="error-map-table" role="table" aria-label="Provider 错误归一规则">
      <div class="error-map-row error-map-head" role="row">
        <span>规则 / 来源</span><span>Provider</span><span>匹配条件</span><span>统一错误</span><span>优先级 / 版本</span><span>状态</span>${canWrite ? "<span>操作</span>" : ""}
      </div>
      ${rules.length ? rules.map((rule) => renderRuleRow(rule, canWrite, state.reference)).join("") : renderEmptyRow()}
    </div>
    ${renderPager(state)}
    ${renderDialog(state.dialog, providers, canWrite)}
  `;
}

/** 把表单值转换为后端可编辑字段；tenant_id、actor、version、hash 和 source 均不读取。 */
export function errorMapDraftFromFormData(data) {
  const fieldPattern = optionalFormText(data.get("fieldPattern"));
  const messagePattern = optionalFormText(data.get("messagePattern"));
  const statuses = parseHttpStatusesInput(data.get("httpStatuses"));
  return {
    provider_id: optionalFormText(data.get("providerId")),
    priority: Number(String(data.get("priority") ?? "")),
    http_statuses: statuses,
    http_status_class: optionalFormText(data.get("httpStatusClass")),
    error_field: fieldPattern ? optionalFormText(data.get("errorField")) : null,
    field_operator: fieldPattern ? optionalFormText(data.get("fieldOperator")) : null,
    field_pattern: fieldPattern,
    message_operator: messagePattern ? "contains" : null,
    message_pattern: messagePattern,
    output_error_code: String(data.get("outputErrorCode") ?? "").trim(),
    status: String(data.get("status") ?? "active").trim(),
  };
}

/**
 * 解析逗号分隔 HTTP 状态输入。
 * 空值表示没有精确状态条件；重复项去重并排序，非法项返回 NaN 供 constraint validation 拦截。
 */
export function parseHttpStatusesInput(value) {
  const normalized = String(value ?? "").trim();
  if (!normalized) {
    return [];
  }
  const values = normalized.split(",").map((item) => item.trim());
  if (values.some((item) => !/^\d{3}$/.test(item))) {
    return [Number.NaN];
  }
  return [...new Set(values.map(Number))].sort((left, right) => left - right);
}

/** 页面头只陈述配置 revision 与目录版本，不推断任何 Gateway 实例的加载进度。 */
function renderHead(head = {}) {
  const revision = Number.isSafeInteger(head.revision) ? head.revision : "-";
  return `
    <dl class="error-map-head-facts">
      <div><dt>配置 revision</dt><dd>${escapeHtml(revision)}</dd></div>
      <div><dt>内置目录</dt><dd><code>${escapeHtml(head.builtin_catalog_version || "-")}</code></dd></div>
      <div><dt>更新时间</dt><dd>${escapeHtml(formatTime(head.updated_at))}</dd></div>
    </dl>
  `;
}

/**
 * 展示日志 Fact 冻结身份与当前管理目录的差异。
 * 这里不重算历史匹配：tenant 显示请求时/当前资源版本，builtin 显示请求时/当前不可变目录版本。
 */
function renderReference(reference, rules, head = {}) {
  if (!reference || !reference.errorMapId) {
    return "";
  }
  const currentRule = rules.find((rule) => (
    rule.error_map_id === reference.errorMapId && rule.source === reference.source
  ));
  let versionComparison = "";
  let resolution = "";
  // tenant 引用只比较同租户稳定资源版本，绝不使用当前规则重算历史请求结果。
  if (reference.source === "tenant") {
    const currentVersion = Number.isSafeInteger(currentRule?.version) && currentRule.version > 0
      ? `v${currentRule.version}`
      : "当前资源不可见";
    versionComparison = `请求时版本 v${reference.requestedVersion} / 当前版本 ${currentVersion}`;
    resolution = currentRule ? "已按当前租户精确读取稳定资源" : "当前租户未返回该稳定资源";
  } else if (reference.source === "builtin") {
    const currentCatalog = head?.builtin_catalog_version || "当前目录未知";
    versionComparison = `请求时目录 ${reference.requestedCatalogVersion} / 当前目录 ${currentCatalog}`;
    resolution = currentRule ? "已定位平台只读条目" : "当前目录未返回该只读条目";
  } else {
    return "";
  }
  return `
    <section class="error-map-reference" role="status" aria-labelledby="error-map-reference-title">
      <strong id="error-map-reference-title">日志中的归一化规则</strong>
      <code>${escapeHtml(reference.errorMapId)}</code>
      <span>${escapeHtml(versionComparison)}</span>
      <span>${escapeHtml(resolution)}</span>
      <em>历史结果以日志中冻结的 ProviderErrorFact 为准。</em>
    </section>
  `;
}

/** 筛选只使用 API 白名单，不提供 tenant owner 输入。 */
function renderFilters(filters, providers) {
  return `
    <form class="error-map-filters" id="error-map-filter-form">
      <label><span>Provider</span><select name="providerId">
        <option value="">全部 Provider</option>
        ${providers.map((provider) => `<option value="${escapeAttribute(provider.provider_id)}" ${selected(filters.providerId, provider.provider_id)}>${escapeHtml(provider.name || provider.provider_code || provider.provider_id)}</option>`).join("")}
      </select></label>
      <label><span>来源</span><select name="source">
        <option value="" ${selected(filters.source, "")}>全部来源</option>
        <option value="tenant" ${selected(filters.source, "tenant")}>租户规则</option>
        <option value="builtin" ${selected(filters.source, "builtin")}>平台内置</option>
      </select></label>
      <label><span>状态</span><select name="status">
        <option value="" ${selected(filters.status, "")}>全部状态</option>
        <option value="active" ${selected(filters.status, "active")}>已启用</option>
        <option value="disabled" ${selected(filters.status, "disabled")}>已停用</option>
      </select></label>
      <label><span>统一错误</span><select name="outputErrorCode">
        <option value="">全部错误码</option>
        ${Object.keys(OUTPUT_LABELS).map((code) => `<option value="${escapeAttribute(code)}" ${selected(filters.outputErrorCode, code)}>${escapeHtml(OUTPUT_LABELS[code])}</option>`).join("")}
      </select></label>
      <div class="error-map-filter-actions">
        <button type="submit">查询</button>
        <button type="button" id="error-map-filter-reset">清空</button>
        <button type="button" id="error-map-refresh">刷新</button>
      </div>
    </form>
  `;
}

/** 渲染一条规则；pattern 只作为管理配置展示，所有文本都经过 HTML/attribute 转义。 */
function renderRuleRow(rule, canWrite, reference) {
  const tenantWritable = canWrite && rule.source === "tenant" && rule.readonly !== true;
  const nextStatus = rule.status === "active" ? "disabled" : "active";
  const referenced = reference?.source === rule.source && reference?.errorMapId === rule.error_map_id;
  return `
    <div class="error-map-row${rule.status === "disabled" ? " is-disabled" : ""}${referenced ? " is-reference" : ""}" role="row" data-error-map-row="${escapeAttribute(rule.error_map_id)}"${referenced ? ' data-error-map-reference="true" tabindex="-1"' : ""}>
      <span data-label="规则 / 来源"><strong><code>${escapeHtml(rule.error_map_id)}</code></strong><em>${escapeHtml(sourceLabel(rule.source))}</em></span>
      <span data-label="Provider"><strong>${escapeHtml(rule.provider_name || rule.provider_code || (rule.provider_id ? rule.provider_id : "全部 Provider"))}</strong>${rule.provider_id ? `<code>${escapeHtml(rule.provider_id)}</code>` : ""}</span>
      <span data-label="匹配条件" class="error-map-condition">${renderConditions(rule)}</span>
      <span data-label="统一错误"><strong>${escapeHtml(OUTPUT_LABELS[rule.output_error_code] || rule.output_error_code || "-")}</strong><code>${escapeHtml(rule.output_error_code || "-")}</code><em>${rule.retryable ? "可重试" : "不可重试"}${rule.user_action ? ` · ${escapeHtml(rule.user_action)}` : ""}</em></span>
      <span data-label="优先级 / 版本"><strong>${escapeHtml(rule.source === "tenant" ? rule.priority : `#${rule.ordinal || rule.priority || "-"}`)}</strong><em>v${escapeHtml(rule.version || "-")}</em></span>
      <span data-label="状态"><span class="error-map-status status-${escapeAttribute(rule.status)}">${escapeHtml(statusLabel(rule.status))}</span></span>
      ${canWrite ? `<span data-label="操作" class="error-map-actions">${tenantWritable ? `
        <button type="button" data-error-map-edit="${escapeAttribute(rule.error_map_id)}">编辑</button>
        <button type="button" data-error-map-status="${escapeAttribute(rule.error_map_id)}" data-next-status="${escapeAttribute(nextStatus)}">${nextStatus === "active" ? "启用" : "停用"}</button>
      ` : '<span class="readonly-label">只读</span>'}</span>` : ""}
    </div>
  `;
}

/** 把 AND 条件压缩成可扫描文本；未知值仍安全展示，不在浏览器执行 pattern。 */
function renderConditions(rule) {
  const parts = [];
  if (Array.isArray(rule.http_statuses) && rule.http_statuses.length) {
    parts.push(`HTTP ${rule.http_statuses.join(" / ")}`);
  }
  if (rule.http_status_class) {
    parts.push(`HTTP ${rule.http_status_class}`);
  }
  // 字段和值只按转义文本展示，浏览器不执行或解释租户提供的 pattern。
  if (rule.error_field && rule.field_pattern) {
    parts.push(`${rule.error_field} ${rule.field_operator || "equals"} ${rule.field_pattern}`);
  }
  if (rule.message_pattern) {
    parts.push(`message contains ${rule.message_pattern}`);
  }
  return parts.length
    ? parts.map((part) => `<code>${escapeHtml(part)}</code>`).join("<b>AND</b>")
    : "<em>平台兜底条件</em>";
}

/** 渲染唯一单层 dialog；busy 时禁用取消和 Escape，避免结果未知时销毁幂等 owner。 */
function renderDialog(dialog, providers, canWrite) {
  if (!dialog || !canWrite) {
    return "";
  }
  const draft = dialog.draft ?? {};
  const title = dialog.mode === "create" ? "新建错误规则" : "编辑错误规则";
  // 仅向验收器重复页面已公开的冻结 CAS 版本，禁止把 tenant、pattern 或凭据放入 DOM 属性。
  const versionAttribute = dialog.mode === "edit"
    ? ` data-error-map-version="${escapeAttribute(dialog.baseVersion ?? "")}"`
    : "";
  return `
    <div class="error-map-dialog-backdrop" data-error-map-dialog-backdrop>
      <section class="error-map-dialog" role="dialog" aria-modal="true" aria-labelledby="error-map-dialog-title" aria-busy="${dialog.busy ? "true" : "false"}"${versionAttribute}>
        <div class="error-map-dialog-heading">
          <div><h3 id="error-map-dialog-title">${title}</h3>${dialog.errorMapId ? `<code>${escapeHtml(dialog.errorMapId)} · v${escapeHtml(dialog.baseVersion)}</code>` : ""}</div>
          <button type="button" class="icon-button" data-error-map-close aria-label="关闭" title="关闭" ${dialog.busy ? "disabled" : ""}>×</button>
        </div>
        <form id="error-map-editor-form">
          <div class="error-map-editor-grid">
            <label><span>Provider 范围</span><select name="providerId" autofocus ${dialog.busy || dialog.conflicted ? "disabled" : ""}>
              <option value="">当前租户全部 Provider</option>
              ${providers.map((provider) => `<option value="${escapeAttribute(provider.provider_id)}" ${selected(draft.provider_id, provider.provider_id)}>${escapeHtml(provider.name || provider.provider_code || provider.provider_id)}</option>`).join("")}
            </select></label>
            <label><span>优先级</span><input name="priority" type="number" min="1" max="1000" required value="${escapeAttribute(draft.priority ?? 100)}" ${dialog.busy || dialog.conflicted ? "disabled" : ""} /></label>
            <label><span>HTTP 状态</span><input name="httpStatuses" inputmode="numeric" maxlength="63" placeholder="例如 400, 404" value="${escapeAttribute(Array.isArray(draft.http_statuses) ? draft.http_statuses.join(", ") : "")}" ${dialog.busy || dialog.conflicted ? "disabled" : ""} /></label>
            <label><span>HTTP 状态类</span><select name="httpStatusClass" ${dialog.busy || dialog.conflicted ? "disabled" : ""}>
              <option value="">不限制</option><option value="4xx" ${selected(draft.http_status_class, "4xx")}>4xx</option>
            </select></label>
            <label><span>错误字段</span><select name="errorField" ${dialog.busy || dialog.conflicted ? "disabled" : ""}>
              <option value="">不使用字段条件</option>
              ${["error.code", "error.type", "code", "type"].map((value) => `<option value="${value}" ${selected(draft.error_field, value)}>${value}</option>`).join("")}
            </select></label>
            <label><span>字段操作</span><select name="fieldOperator" ${dialog.busy || dialog.conflicted ? "disabled" : ""}>
              <option value="equals" ${selected(draft.field_operator, "equals")}>等于</option>
              <option value="contains" ${selected(draft.field_operator, "contains")}>包含</option>
            </select></label>
            <label class="error-map-wide"><span>字段模式</span><input name="fieldPattern" maxlength="256" value="${escapeAttribute(draft.field_pattern ?? "")}" ${dialog.busy || dialog.conflicted ? "disabled" : ""} /></label>
            <label class="error-map-wide"><span>Message 包含</span><input name="messagePattern" maxlength="256" value="${escapeAttribute(draft.message_pattern ?? "")}" ${dialog.busy || dialog.conflicted ? "disabled" : ""} /></label>
            <label><span>统一错误</span><select name="outputErrorCode" required ${dialog.busy || dialog.conflicted ? "disabled" : ""}>
              ${TENANT_OUTPUT_CODES.map((code) => `<option value="${code}" ${selected(draft.output_error_code, code)}>${escapeHtml(OUTPUT_LABELS[code])}</option>`).join("")}
            </select></label>
            <label><span>状态</span><select name="status" ${dialog.busy || dialog.conflicted ? "disabled" : ""}>
              <option value="active" ${selected(draft.status, "active")}>启用</option><option value="disabled" ${selected(draft.status, "disabled")}>停用</option>
            </select></label>
          </div>
          ${renderDialogFeedback(dialog)}
          <div class="error-map-dialog-actions">
            ${dialog.conflicted && dialog.sideLatest ? '<button type="button" data-error-map-adopt>载入最新版本</button>' : ""}
            <button type="button" data-error-map-cancel ${dialog.busy ? "disabled" : ""}>取消</button>
            <button type="submit" ${!dialog.dirty || dialog.busy || dialog.conflicted ? "disabled" : ""}>${dialog.busy ? "保存中" : "保存"}</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

/** 对话框只展示统一错误和 request_id，不渲染后端 details 或响应原文。 */
function renderDialogFeedback(dialog) {
  if (dialog.actionError) {
    return `<div class="error-map-dialog-feedback error-state" role="alert" tabindex="-1"><strong>${escapeHtml(dialog.actionError.code)}</strong><span>${escapeHtml(dialog.actionError.message)}</span>${dialog.actionError.suggestion ? `<span>${escapeHtml(dialog.actionError.suggestion)}</span>` : ""}<code>request_id: ${escapeHtml(dialog.actionError.requestId || "-")}</code></div>`;
  }
  return dialog.feedback
    ? `<div class="error-map-dialog-feedback" role="status" aria-live="polite" tabindex="-1">${escapeHtml(dialog.feedback)}</div>`
    : "";
}

/** 已有规则时刷新错误保持列表和编辑上下文，只显示 request_id。 */
function renderRefreshError(error) {
  return error
    ? `<div class="error-map-refresh-error" role="status">刷新失败，当前规则仍保留。request_id: ${escapeHtml(error.requestId || "-")}</div>`
    : "";
}

/** 首次读取占位不伪造目录版本、revision 或规则数量。 */
function renderLoading() {
  return '<div class="error-map-heading"><div><h2 id="error-maps-title">Provider 错误规则</h2><p>正在读取当前租户规则。</p></div></div><div class="empty-state">等待 /error-maps 响应。</div>';
}

/** 首次读取失败只展示统一错误与 request_id。 */
function renderFatalError(error) {
  return `<div class="error-map-heading"><div><h2 id="error-maps-title">Provider 错误规则</h2><p>错误规则暂不可用。</p></div></div><div class="empty-state error-state" role="alert">${escapeHtml(error?.message || "错误规则读取失败")} request_id: ${escapeHtml(error?.requestId || "-")}</div>`;
}

/** 空结果使用一条表格行，保持桌面和移动布局尺寸稳定。 */
function renderEmptyRow() {
  return '<div class="error-map-row error-map-empty" role="row"><span>当前筛选没有错误规则。</span></div>';
}

/** 分页只使用响应中的 total/limit/offset；按钮命令由主应用重新读取当前租户。 */
function renderPager(state) {
  const offset = nonnegativeInteger(state.offset);
  const limit = positiveInteger(state.limit, 100);
  const total = nonnegativeInteger(state.total);
  return `<div class="error-map-pager"><button type="button" id="error-map-prev" ${offset <= 0 ? "disabled" : ""}>上一页</button><span>${total ? `${offset + 1}-${Math.min(offset + limit, total)} / ${total}` : "0 / 0"}</span><button type="button" id="error-map-next" ${offset + limit >= total ? "disabled" : ""}>下一页</button></div>`;
}

/** 来源枚举转为产品文案；未知来源仍展示原值以便发现合同漂移。 */
function sourceLabel(value) {
  return { tenant: "租户覆盖", builtin: "平台内置", guard: "安全 guard" }[value] ?? value ?? "-";
}

/** 状态枚举转为中文；未知值不伪装成启用。 */
function statusLabel(value) {
  return { active: "已启用", disabled: "已停用" }[value] ?? value ?? "-";
}

/** 时间展示只做安全本地化，非法时间保持占位。 */
function formatTime(value) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "-" : parsed.toLocaleString("zh-CN", { hour12: false });
}

/** 生成严格 selected 属性，避免服务端值插入属性结构。 */
function selected(current, expected) {
  return String(current ?? "") === String(expected ?? "") ? "selected" : "";
}

/** 表单空字符串转换为 null，避免把未配置条件错误提交为空 pattern。 */
function optionalFormText(value) {
  const normalized = String(value ?? "").trim();
  return normalized || null;
}

/** 分页值只接受非负安全整数。 */
function nonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

/** 分页大小只接受正安全整数。 */
function positiveInteger(value, fallback) {
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

/** HTML 文本转义，防止 Provider、pattern、规则 ID 或错误消息破坏 DOM。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 属性上下文额外转义反引号，保护 value 与 data-* 属性。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
