/**
 * 模块职责：渲染当前租户 DLP 规则、状态和受权限约束的编辑入口。
 * 安全边界：规则正文、租户隔离和正则安全由后端校验；缺少权限时页面返回零 DOM。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 规则类型与动作标签只映射后端稳定枚举，未知值不得被当作新的放行策略。
const RULE_TYPE_LABELS = {
  keyword: "关键词",
  regex: "正则",
};

const ACTION_LABELS = {
  allow: "放行",
  log_only: "试运行记录",
  block: "阻断",
};

// 检测方向标签与后端 scan_scope 白名单一致，未知值只按转义文本展示，不能被解释成双向策略。
const SCAN_SCOPE_LABELS = {
  input: "输入",
  output: "输出",
  both: "双向",
};

// DLP 状态标签用于规则列表展示，实际启停状态以租户内规则记录为准。
const STATUS_LABELS = {
  active: "启用",
  disabled: "停用",
};

/**
 * 渲染 DLP 规则管理面板。
 * 面板只对 admin 可见；前端隐藏入口只是体验优化，真实读写权限、tenant_id 边界和正则安全校验仍由 /dlp/rules 后端强制完成。
 */
export function renderDlpPanel(dlpState, authContext) {
  // 只消费后端 DLP 读权限；角色名称不再参与页面授权结论。
  if (!hasPermission(authContext, PERMISSIONS.DLP_READ)) {
    return "";
  }
  if (!dlpState) {
    return renderLoadingPanel();
  }
  // 诊断编号保留在状态对象和后端日志中，普通用户只看到可执行的提示。
  if (dlpState.state === "error") {
    return renderErrorPanel(dlpState);
  }

  const rules = dlpState.rules?.dlp_rules ?? dlpState.dlp_rules ?? [];
  const canWrite = hasPermission(authContext, PERMISSIONS.DLP_WRITE);
  return `
    <div class="panel-heading dlp-heading">
      <div>
        <h2 id="dlp-rules-title">DLP 规则</h2>
        <p>按租户配置输入、输出或双向关键词和正则规则；普通调用日志只展示命中摘要，不展示正文。</p>
      </div>
      <div class="dlp-heading-actions"><span class="dlp-count">${escapeHtml(rules.length)} 条规则</span>${canWrite ? '<button type="button" data-dlp-create-open>新增规则</button>' : ""}</div>
    </div>
    ${renderActionError(dlpState.actionError)}
    ${renderFeedback(dlpState.feedback)}
    ${renderScopeFilter(dlpState.scanScopeFilter)}
    ${canWrite ? `<dialog class="dlp-create-dialog" data-dlp-create-dialog aria-labelledby="dlp-create-title"${dlpState.actionError ? " data-dlp-auto-open" : ""}>${renderCreateForm()}</dialog>` : renderReadOnlyNotice()}
    ${renderRuleTable(rules, canWrite)}
  `;
}

/**
 * 渲染检测方向筛选；空值代表全部，实际枚举和 tenant_id 边界仍由后端列表接口校验。
 */
function renderScopeFilter(selectedScope) {
  return `
    <form class="dlp-rule-filters" id="dlp-rule-filters">
      <label>
        <span>检测方向</span>
        <select name="scanScope" id="dlp-scan-scope-filter">
          ${renderOption("", "全部方向", selectedScope ?? "")}
          ${renderOption("input", "输入", selectedScope ?? "")}
          ${renderOption("output", "输出", selectedScope ?? "")}
          ${renderOption("both", "双向", selectedScope ?? "")}
        </select>
      </label>
    </form>
  `;
}

/**
 * 渲染 DLP 加载态，避免导航切换时安全治理区域出现空白。
 */
function renderLoadingPanel() {
  return `
    <div class="panel-heading">
      <h2 id="dlp-rules-title">DLP 规则</h2>
      <p>正在读取当前租户的 DLP 规则。</p>
    </div>
    <div class="empty-state">等待 /dlp/rules 响应。</div>
  `;
}

/**
 * 渲染 DLP API 错误状态，避免把内部追踪编号直接暴露给普通用户。
 */
function renderErrorPanel(state) {
  return `
    <div class="panel-heading">
      <h2 id="dlp-rules-title">DLP 规则</h2>
      <p>DLP 规则暂不可用。</p>
    </div>
    <div class="empty-state error-state">
      ${escapeHtml(state.message ?? "DLP 规则读取失败，请稍后重试。以下提示仍无法解决时，请联系管理员。")}
    </div>
  `;
}

/**
 * 渲染创建 DLP 规则表单。
 * pattern 只提交给管理 API；页面不把该字段写入 console 事件 payload，也不在调用日志详情中展示命中文本。
 */
function renderCreateForm() {
  return `
    <form class="dlp-rule-form" id="dlp-rule-form">
      <div class="dlp-dialog-heading"><h3 id="dlp-create-title">新增规则</h3><button type="button" class="icon-button" data-dlp-create-close aria-label="关闭">×</button></div>
      <label>
        <span>名称</span>
        <input name="name" placeholder="例如：高风险关键词" required />
      </label>
      <label>
        <span>类型</span>
        <select name="ruleType">
          <option value="keyword">关键词</option>
          <option value="regex">正则</option>
        </select>
      </label>
      <label>
        <span>检测方向</span>
        <select name="scanScope">
          <option value="input" selected>输入</option>
          <option value="output">输出</option>
          <option value="both">双向</option>
        </select>
      </label>
      <label class="dlp-pattern-field">
        <span>匹配内容</span>
        <textarea name="pattern" rows="3" placeholder="关键词或安全正则表达式" required></textarea>
      </label>
      <label>
        <span>动作</span>
        <select name="action">
          <option value="log_only">试运行记录</option>
          <option value="block">阻断</option>
          <option value="allow">放行</option>
        </select>
      </label>
      <label>
        <span>优先级</span>
        <input name="priority" type="number" value="100" min="1" max="10000" />
      </label>
      <label>
        <span>状态</span>
        <select name="status">
          <option value="active">启用</option>
          <option value="disabled">停用</option>
        </select>
      </label>
      <label class="dlp-description-field">
        <span>描述</span>
        <input name="description" placeholder="规则目的或审批说明" />
      </label>
      <div class="dlp-actions">
        <button type="submit">保存规则</button>
        <button type="button" data-dlp-create-close>取消</button>
      </div>
    </form>
  `;
}

/**
 * 渲染只读提示；M0 中 DLP 规则只有 admin 能读写。
 */
function renderReadOnlyNotice() {
  return `
    <div class="dlp-readonly">
      当前角色只能查看安全治理结果，DLP 规则创建和调整必须由管理员完成。
    </div>
  `;
}

/**
 * 渲染 DLP 规则表格，规则 pattern 只在 admin 管理页展示，不进入普通日志详情。
 */
function renderRuleTable(rules, canWrite) {
  return `
    <div class="dlp-rule-table" role="table" aria-label="DLP 规则">
      <div class="dlp-rule-row dlp-rule-head" role="row">
        <span>规则</span>
        <span>类型</span>
        <span>方向</span>
        <span>动作</span>
        <span>状态</span>
        <span>优先级</span>
        <span>匹配内容</span>
        <span>更新</span>
      </div>
      ${rules.length ? rules.map((rule) => renderRuleRow(rule, canWrite)).join("") : renderEmptyRuleRow()}
    </div>
  `;
}

/**
 * 渲染单条 DLP 规则行，并在 admin 场景下提供动作、状态和优先级快速调整表单。
 */
function renderRuleRow(rule, canWrite) {
  return `
    <div class="dlp-rule-row" role="row">
      <span class="dlp-rule-name">
        <strong>${escapeHtml(rule.name)}</strong>
      </span>
      <span>${escapeHtml(RULE_TYPE_LABELS[rule.rule_type] ?? rule.rule_type)}</span>
      <span><mark class="dlp-scope-${escapeAttribute(rule.scan_scope)}">${escapeHtml(SCAN_SCOPE_LABELS[rule.scan_scope] ?? rule.scan_scope)}</mark></span>
      <span><mark class="dlp-action-${escapeAttribute(rule.action)}">${escapeHtml(ACTION_LABELS[rule.action] ?? rule.action)}</mark></span>
      <span><mark class="status-${escapeAttribute(rule.status)}">${escapeHtml(STATUS_LABELS[rule.status] ?? rule.status)}</mark></span>
      <span>${escapeHtml(rule.priority ?? "-")}</span>
      <code title="${escapeAttribute(rule.pattern)}">${escapeHtml(rule.pattern)}</code>
      <span>${canWrite ? renderUpdateForm(rule) : formatDateTime(rule.updated_at)}</span>
    </div>
  `;
}

/**
 * 渲染规则快捷更新表单。
 * 方向会改变输入和输出两条热路径，因此与动作、状态一并显式提交；pattern 仍不提供快捷编辑，避免误改复杂正则。
 */
function renderUpdateForm(rule) {
  return `
    <form class="dlp-rule-update-form" data-dlp-rule-update data-dlp-rule-id="${escapeAttribute(rule.dlp_rule_id)}">
      <select name="action" aria-label="DLP 动作">
        ${renderOption("log_only", "试运行", rule.action)}
        ${renderOption("block", "阻断", rule.action)}
        ${renderOption("allow", "放行", rule.action)}
      </select>
      <select name="scanScope" aria-label="DLP 检测方向">
        ${renderOption("input", "输入", rule.scan_scope ?? "input")}
        ${renderOption("output", "输出", rule.scan_scope ?? "input")}
        ${renderOption("both", "双向", rule.scan_scope ?? "input")}
      </select>
      <select name="status" aria-label="DLP 状态">
        ${renderOption("active", "启用", rule.status)}
        ${renderOption("disabled", "停用", rule.status)}
      </select>
      <input name="priority" aria-label="DLP 优先级" type="number" value="${escapeAttribute(rule.priority ?? 100)}" min="1" max="10000" />
      <button type="submit">更新</button>
    </form>
  `;
}

/**
 * 渲染空规则行，保持表格结构稳定并提醒管理员从试运行规则开始。
 */
function renderEmptyRuleRow() {
  return `
    <div class="dlp-rule-row dlp-rule-empty" role="row">
      <span>暂无规则。</span>
    </div>
  `;
}

/**
 * 渲染 DLP 操作错误，只展示用户可以采取的处理提示。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="dlp-action-error" role="alert">
      ${escapeHtml(error.message ?? "DLP 操作失败，请检查输入后重试。")}
    </div>
  `;
}

/**
 * 渲染 DLP 操作反馈，例如创建成功或更新成功。
 */
function renderFeedback(feedback) {
  return feedback ? `<div class="dlp-feedback">${escapeHtml(feedback)}</div>` : "";
}

/**
 * 渲染 select option，并保持当前规则值的选中状态。
 */
function renderOption(value, label, selectedValue) {
  const selected = value === (selectedValue ?? "") ? " selected" : "";
  return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(label)}</option>`;
}

/**
 * 格式化更新时间，避免表格中出现过长的小数秒。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
}

/**
 * HTML 文本转义，防止规则名称、pattern 或后端错误消息破坏页面结构。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * HTML 属性转义，额外处理反引号以保护 data-* 与 title 属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
