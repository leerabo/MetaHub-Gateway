/**
 * 模块职责：渲染 PAGE-096 固定 taxonomy、一次性分类工作台和脱敏结果。
 * 安全边界：renderer 不接收正文状态；textarea 永远为空，所有后端字段都经枚举映射或 HTML 转义。
 */

import {
  intentSecurityPageOwner,
} from "./intentSecurityPageState.js";

// 固定 taxonomy 与展示枚举只解释已返回的安全摘要，不构成可编辑运行配置或前端分类规则。
const TAXONOMY = Object.freeze([
  ["credential_exfiltration", "凭据索取"],
  ["system_prompt_extraction", "系统提示词提取"],
  ["policy_bypass", "安全策略绕过"],
  ["jailbreak", "越狱与指令覆盖"],
]);
// 手工分类结果只有 clean/detected，两者文案不得暗示同步 Gateway 阻断。
const STATUS_LABELS = Object.freeze({
  clean: "未检出风险",
  detected: "需要安全复核",
});
// 风险类型与 Shared taxonomy 精确对应，未知值只作为转义后的合同漂移展示。
const RISK_LABELS = Object.freeze({
  none: "无",
  credential_exfiltration: "凭据索取",
  system_prompt_extraction: "系统提示词提取",
  policy_bypass: "安全策略绕过",
  jailbreak: "越狱与指令覆盖",
});
// Control 手工分类只允许 allow/review，不接受 Gateway 的 observe/block 动作冒充。
const ACTION_LABELS = Object.freeze({
  allow: "放行",
  review: "人工复核",
});

/**
 * 渲染当前授权 owner 的分类工作台。
 * 后端导航项与实时权限快照必须同时有效；任一不匹配返回空字符串，确保无权用户零 DOM。
 */
export function renderIntentSecurity(state, authContext, consoleShellState) {
  if (!intentSecurityPageOwner(authContext, consoleShellState)) return "";
  const safeState = state ?? { status: "idle", result: null, error: null };
  const busy = safeState.status === "submitting";
  return `
    <div class="intent-security-heading">
      <div>
        <h2 id="intent-security-title">敏感意图分类</h2>
        <p>本地轻量规则</p>
      </div>
      <span class="intent-security-status" data-status="${escapeAttribute(safeState.status)}" tabindex="-1">${escapeHtml(pageStatusLabel(safeState.status))}</span>
    </div>
    <section class="intent-security-taxonomy" aria-labelledby="intent-security-taxonomy-title">
      <h3 id="intent-security-taxonomy-title">风险分类</h3>
      <div class="intent-security-taxonomy-list">
        ${TAXONOMY.map(([value, label]) => `<span data-risk-type="${escapeAttribute(value)}">${escapeHtml(label)}</span>`).join("")}
      </div>
    </section>
    ${renderError(safeState.error)}
    <form id="intent-security-form" autocomplete="off">
      <label for="intent-security-text">单次分类文本</label>
      <textarea id="intent-security-text" name="text" maxlength="40000" data-max-code-points="20000" rows="8" required autocomplete="off" spellcheck="false" ${busy ? "disabled" : ""}></textarea>
      <div class="intent-security-actions">
        <button type="submit" ${busy ? "disabled" : ""}>${busy ? "分类中" : "开始分类"}</button>
        <button type="button" data-intent-security-cancel ${busy ? "" : "disabled"}>取消</button>
        <button type="button" data-intent-security-clear ${busy ? "disabled" : ""}>清空结果</button>
        <span role="status" aria-live="polite">${busy ? "正在执行本地规则" : ""}</span>
      </div>
    </form>
    ${renderResult(safeState.result)}
  `;
}

/** 渲染统一固定错误；服务端 message、响应正文和分类输入都不属于页面错误合同。 */
function renderError(error) {
  if (!error) return "";
  return `
    <div class="intent-security-error" role="alert" tabindex="-1">
      <strong>${escapeHtml(error.code ?? "intent_risk.unavailable")}</strong>
      <span>${escapeHtml(error.message ?? "语义风险分类请求未完成")}</span>
      <code>request_id: ${escapeHtml(error.requestId ?? "-")}</code>
    </div>
  `;
}

/** 渲染一次已提交的不可变安全摘要，不读取任何响应扩展或正文派生字段。 */
function renderResult(payload) {
  const result = payload?.intent_risk_result;
  if (!result) return `<div class="intent-security-empty" aria-live="polite"></div>`;
  return `
    <section class="intent-security-result" aria-labelledby="intent-security-result-title">
      <div class="intent-security-result-heading">
        <h3 id="intent-security-result-title" tabindex="-1">分类结果</h3>
        <span data-result-status="${escapeAttribute(result.status)}">${escapeHtml(enumLabel(STATUS_LABELS, result.status))}</span>
      </div>
      <dl class="intent-security-result-grid">
        ${renderPair("风险类型", enumLabel(RISK_LABELS, result.risk_type))}
        ${renderPair("风险分数", `${result.risk_score ?? "-"}/${result.risk_score_scale ?? 10_000}`)}
        ${renderPair("动作", enumLabel(ACTION_LABELS, result.action))}
        ${renderPair("分类模式", result.classification_mode === "manual_sync" ? "手工同步" : result.classification_mode)}
        ${renderPair("原因码", result.reason_code)}
        ${renderPair("结果 ID", result.intent_risk_result_id)}
        ${renderPair("分类器版本", result.detector_version)}
        ${renderPair("规则版本", result.ruleset_version)}
        ${renderPair("输入长度", `${result.input_code_points ?? 0} code points`)}
        ${renderPair("覆盖截断", result.input_truncated === true ? "是" : "否")}
        ${renderPair("分类耗时", `${result.duration_ms ?? 0} ms`)}
        ${renderPair("完成时间", result.completed_at)}
        ${renderPair("request_id", payload.request_id)}
        ${renderPair("trace_id", payload.trace_id)}
      </dl>
    </section>
  `;
}

/** 把 renderer 状态映射为固定短文案，未知状态不输出原值。 */
function pageStatusLabel(value) {
  return {
    idle: "就绪",
    submitting: "提交中",
    success: "已完成",
    error: "失败",
  }[value] ?? "不可用";
}

/** 固定枚举命中时显示产品文案，未知值仍作为转义文本显示以暴露合同漂移。 */
function enumLabel(labels, value) {
  return labels[value] ?? String(value ?? "-");
}

/** 渲染统一详情键值对，值始终经过 HTML 转义。 */
function renderPair(label, value) {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "-")}</dd></div>`;
}

/** 对所有文本节点执行 HTML 转义，避免固定枚举漂移成为注入入口。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 对 data-* 属性额外转义反引号，保持属性边界稳定。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
