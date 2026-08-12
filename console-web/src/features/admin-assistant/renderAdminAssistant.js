import { adminAssistantSourceAccess } from "../../lib/adminAssistant.js";
import { adminAssistantPageOwner } from "./adminAssistantPageState.js";

/** 四个建议是公开固定文案；按钮只填充 textarea，不自动发送，也不进入 URL、Storage 或遥测。 */
export const ADMIN_ASSISTANT_SUGGESTIONS = Object.freeze([
  "过去 7 天调用情况如何？",
  "解释最近 24 小时的主要错误。",
  "过去 30 天有哪些成本优化建议？",
  "解释当前策略状态和需要人工复核的优先级碰撞。",
]);

// 查询类型标签只描述后端已判定的四类只读意图，不参与权限或意图推断。
const QUERY_TYPE_LABELS = Object.freeze({
  usage_statistics: "调用统计",
  error_explanation: "错误解释",
  cost_advice: "成本建议",
  policy_explanation: "策略解释",
});
// 脱敏标签来自固定协议枚举，禁止把被省略的原始正文重新拼入页面。
const REDACTION_LABELS = Object.freeze({
  content_omitted: "正文与原始内容已省略",
  credentials_omitted: "凭据、Token 与密钥已省略",
  raw_provider_error_omitted: "Provider 原始错误已省略",
});

/**
 * 渲染 PAGE-107 安全骨架。
 *
 * question、summary、claim、recommendation、source label 和错误正文都不进入返回的 HTML 字符串；它们只在
 * DOM 就位后由 hydrateAdminAssistantText 使用 textContent/value 注入。无完整 owner 时返回零 DOM。
 */
export function renderAdminAssistant(state, authContext, consoleShellState) {
  if (!adminAssistantPageOwner(authContext, consoleShellState)) return "";
  const safeState = state ?? {};
  const response = safeState.response ?? null;
  const busy = safeState.busy === true;
  return `
    <div class="admin-assistant" data-admin-assistant-root data-admin-assistant-state="${escapeAttribute(safeState.status ?? "idle")}">
      <header class="admin-assistant-toolbar">
        <div>
          <div class="admin-assistant-title-row">
            <h2 id="admin-assistant-title">管理助手</h2>
            <span class="admin-assistant-readonly">只读</span>
          </div>
          <p>当前租户 · 确定性查询</p>
        </div>
        <button type="button" class="admin-assistant-icon-command" data-admin-assistant-clear
          title="清空问题与回答" aria-label="清空问题与回答" ${safeState.hasContent ? "" : "disabled"}>×</button>
      </header>

      <div class="admin-assistant-workspace">
        <section class="admin-assistant-composer" aria-labelledby="admin-assistant-question-title">
          <div class="admin-assistant-section-heading">
            <h3 id="admin-assistant-question-title">问题</h3>
            <span>最多 2,000 字符</span>
          </div>
          <div class="admin-assistant-suggestions" aria-label="问题建议">
            ${ADMIN_ASSISTANT_SUGGESTIONS.map((label, index) => `
              <button type="button" data-admin-assistant-suggestion="${index}" ${busy ? "disabled" : ""}>
                ${escapeHtml(label)}
              </button>
            `).join("")}
          </div>
          <form data-admin-assistant-form autocomplete="off">
            <label for="admin-assistant-question" class="sr-only">只读查询问题</label>
            <textarea id="admin-assistant-question" name="question" rows="7" maxlength="2000"
              data-admin-assistant-question placeholder="询问调用统计、错误、成本或策略" ${busy ? "disabled" : ""}></textarea>
            <div class="admin-assistant-form-actions">
              <button type="submit" class="primary" data-admin-assistant-submit
                title="发送查询" aria-label="发送查询" ${safeState.canSubmit ? "" : "disabled"}>
                <span aria-hidden="true">↑</span><span>发送</span>
              </button>
              <button type="button" data-admin-assistant-cancel title="取消等待" aria-label="取消等待"
                ${busy ? "" : "disabled"}>
                <span aria-hidden="true">■</span><span>取消</span>
              </button>
            </div>
          </form>
          <div class="admin-assistant-live" data-admin-assistant-live role="status" aria-live="polite"></div>
        </section>

        <section class="admin-assistant-result" aria-labelledby="admin-assistant-result-title" aria-busy="${busy}">
          <div class="admin-assistant-section-heading">
            <h3 id="admin-assistant-result-title">回答与来源</h3>
            ${response ? `<span>${escapeHtml(QUERY_TYPE_LABELS[response.query_type] ?? "只读查询")}</span>` : ""}
          </div>
          ${renderResult(safeState, authContext, consoleShellState)}
        </section>
      </div>
    </div>
  `;
}

/**
 * 把服务端正文注入已经授权的当前 PAGE-107 DOM。
 * 不解析 Markdown、不调用 innerHTML，也不把正文复制到 attribute、dataset、URL 或 Storage。
 */
export function hydrateAdminAssistantText(root, state) {
  const scope = root?.querySelector ? root : null;
  if (!scope) return false;
  const safeState = state ?? {};
  const response = safeState.response ?? null;
  const page = scope.matches?.("[data-admin-assistant-root]")
    ? scope
    : scope.querySelector("[data-admin-assistant-root]");
  if (!page) return false;
  page.dataset.adminAssistantState = String(safeState.status ?? "idle");
  const textarea = page.querySelector("[data-admin-assistant-question]");
  if (textarea) textarea.value = String(safeState.draft ?? "");
  if (textarea) textarea.disabled = safeState.busy === true;
  const submit = page.querySelector("[data-admin-assistant-submit]");
  if (submit) submit.disabled = safeState.canSubmit !== true;
  const cancel = page.querySelector("[data-admin-assistant-cancel]");
  if (cancel) cancel.disabled = safeState.busy !== true;
  const clear = page.querySelector("[data-admin-assistant-clear]");
  if (clear) clear.disabled = safeState.hasContent !== true;
  for (const suggestion of page.querySelectorAll("[data-admin-assistant-suggestion]")) {
    suggestion.disabled = safeState.busy === true;
  }
  page.querySelector(".admin-assistant-result")?.setAttribute?.("aria-busy", safeState.busy ? "true" : "false");
  setText(page, "[data-admin-assistant-live]", statusText(safeState));
  setText(page, "[data-admin-assistant-error-message]", safeState.error?.message ?? "");
  setText(page, "[data-admin-assistant-error-code]", safeState.error?.code ?? "");
  setText(page, "[data-admin-assistant-error-request]", safeState.error?.requestId ?? "");
  setText(page, "[data-admin-assistant-summary]", response?.answer?.summary ?? "");

  for (const element of page.querySelectorAll("[data-admin-assistant-claim-index]")) {
    const index = safeIndex(element.getAttribute("data-admin-assistant-claim-index"));
    const claim = index === null ? null : response?.answer?.claims?.[index];
    setText(element, "[data-admin-assistant-claim-label]", claim?.label ?? "");
    setText(element, "[data-admin-assistant-claim-value]", claim ? formatClaimValue(claim) : "");
  }
  for (const element of page.querySelectorAll("[data-admin-assistant-recommendation-index]")) {
    const index = safeIndex(element.getAttribute("data-admin-assistant-recommendation-index"));
    setText(element, "[data-admin-assistant-recommendation-message]", index === null
      ? ""
      : (response?.answer?.recommendations?.[index]?.message ?? ""));
  }
  for (const element of page.querySelectorAll("[data-admin-assistant-source-index]")) {
    const index = safeIndex(element.getAttribute("data-admin-assistant-source-index"));
    const source = index === null ? null : response?.sources?.[index];
    setText(element, "[data-admin-assistant-source-label]", source?.label ?? "");
    setText(element, "[data-admin-assistant-source-detail]", source ? sourceDetails(source) : "");
  }
  setText(page, "[data-admin-assistant-query-id]", response?.assistant_query_id ?? "");
  setText(page, "[data-admin-assistant-audit-id]", response?.assistant_audit_id ?? "");
  setText(page, "[data-admin-assistant-request-id]", response?.request_id ?? "");
  setText(page, "[data-admin-assistant-trace-id]", response?.trace_id ?? "");
  return true;
}

/** 只生成不含服务端正文的结果骨架，实际文本稍后通过 textContent 注入。 */
function renderResult(state, authContext, consoleShellState) {
  // 错误骨架只预留固定公开字段，内部异常与原始上游响应都不能进入 HTML 字符串。
  if (state.error) {
    return `
      <div class="admin-assistant-error" role="alert">
        <strong>${state.status === "uncertain" ? "响应状态未知，请勿直接重复发送" : "查询未完成"}</strong>
        <p data-admin-assistant-error-message data-admin-assistant-sensitive></p>
        <div><code data-admin-assistant-error-code></code><code data-admin-assistant-error-request></code></div>
      </div>
    `;
  }
  if (!state.response) {
    return `<div class="admin-assistant-empty">${state.busy ? "正在核对权限、事实与审计状态" : "尚无回答"}</div>`;
  }
  const response = state.response;
  return `
    <div class="admin-assistant-answer" data-admin-assistant-sensitive>
      <p class="admin-assistant-summary" data-admin-assistant-summary></p>
      <dl class="admin-assistant-claims">
        ${response.answer.claims.map((_, index) => `
          <div data-admin-assistant-claim-index="${index}">
            <dt data-admin-assistant-claim-label></dt>
            <dd data-admin-assistant-claim-value></dd>
          </div>
        `).join("")}
      </dl>
      ${response.answer.recommendations.length ? `
        <section class="admin-assistant-recommendations" aria-labelledby="admin-assistant-recommendations-title">
          <h4 id="admin-assistant-recommendations-title">建议</h4>
          <ul>
            ${response.answer.recommendations.map((_, index) => `
              <li data-admin-assistant-recommendation-index="${index}">
                <span data-admin-assistant-recommendation-message></span>
              </li>
            `).join("")}
          </ul>
        </section>
      ` : ""}
      <section class="admin-assistant-sources" aria-labelledby="admin-assistant-sources-title">
        <h4 id="admin-assistant-sources-title">数据来源</h4>
        <ul>
          ${response.sources.map((source, index) => renderSource(
            source,
            index,
            authContext,
            consoleShellState,
          )).join("")}
        </ul>
      </section>
      <section class="admin-assistant-redactions" aria-labelledby="admin-assistant-redactions-title">
        <h4 id="admin-assistant-redactions-title">脱敏</h4>
        <ul>${response.redactions.map((code) => `<li>${escapeHtml(REDACTION_LABELS[code] ?? "受保护内容已省略")}</li>`).join("")}</ul>
      </section>
      <dl class="admin-assistant-chain">
        <div><dt>query_id</dt><dd><code data-admin-assistant-query-id></code></dd></div>
        <div><dt>audit_id</dt><dd><code data-admin-assistant-audit-id></code></dd></div>
        <div><dt>request_id</dt><dd><code data-admin-assistant-request-id></code></dd></div>
        <div><dt>trace_id</dt><dd><code data-admin-assistant-trace-id></code></dd></div>
      </dl>
    </div>
  `;
}

/** 依据当前权限与导航快照决定来源是否可点击，href 不从 DOM 数据反向读取。 */
function renderSource(source, index, authContext, consoleShellState) {
  const access = adminAssistantSourceAccess(source, authContext, consoleShellState);
  const body = `
    <strong data-admin-assistant-source-label></strong>
    <small data-admin-assistant-source-detail></small>
  `;
  return access.allowed
    ? `<li><a href="${escapeAttribute(access.href)}" data-admin-assistant-source-index="${index}">${body}</a></li>`
    : `<li><span class="admin-assistant-source-static" data-admin-assistant-source-index="${index}" aria-disabled="true">${body}</span></li>`;
}

/** 将内部有限状态映射为公开提示，不回显问题、回答或上游错误。 */
function statusText(state) {
  if (state.status === "submitting") return "正在查询并写入审计";
  if (state.status === "answered") return "回答已生成并完成审计";
  if (state.status === "uncertain") return "响应状态未知；请先清空或修改问题";
  // 失败态统一使用固定提示，详细公开错误由受约束字段单独展示。
  if (state.status === "error") return "查询未完成";
  if (state.status === "draft") return "问题尚未发送";
  return "";
}

/** 格式化已经通过响应合同校验的 claim 数值和固定单位。 */
function formatClaimValue(claim) {
  return claim.unit ? `${String(claim.value)} ${claim.unit}` : String(claim.value);
}

/** 组合来源时间窗、计数和快照时间，不包含查询正文或数据库原始记录。 */
function sourceDetails(source) {
  return `${source.window_started_at} 至 ${source.window_ended_at} · ${source.record_count} 条 · 快照 ${source.snapshot_at}`;
}

/** 统一通过 textContent 写入不可信文本，避免任何 HTML 解释路径。 */
function setText(root, selector, value) {
  const target = root?.matches?.(selector) ? root : root?.querySelector?.(selector);
  if (target) target.textContent = String(value ?? "");
}

/** 将 DOM 下标限制为规范非负整数，拒绝对象键和宽松数值解析。 */
function safeIndex(value) {
  return /^(?:0|[1-9][0-9]*)$/.test(String(value ?? "")) ? Number(value) : null;
}

/** 转义仅用于固定骨架内的协议枚举和建议文案，不用于注入服务端正文。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 在 HTML 转义基础上额外处理模板属性中的反引号。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
