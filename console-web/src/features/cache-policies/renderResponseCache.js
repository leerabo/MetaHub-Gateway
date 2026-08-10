/**
 * PAGE-106 响应缓存策略工作台渲染器。
 *
 * 只消费 owner 状态机已经验过的 Alias、策略、计数和固定错误；缓存正文、请求内容、
 * Key、digest、Credential 与内部账务 owner 不属于本组件输入，也没有对应模板插槽。
 */

import { responseCachePageOwner } from "./responseCachePageState.js";

// 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。
const REASON_LABELS = Object.freeze({
  manual_finops: "财务治理",
  manual_security: "安全处置",
  manual_configuration: "配置变更",
  manual_other: "其他人工操作",
});

// 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。
const STATUS_LABELS = Object.freeze({
  enabled: "已启用",
  disabled: "已停用",
});

/** 仅在后端 response-cache 导航 owner 完整时输出 PAGE-106 DOM。 */
export function renderResponseCache(state, authContext, consoleShellState) {
  const owner = responseCachePageOwner(authContext, consoleShellState);
  if (!owner) return "";
  const view = state ?? {
    state: "loading",
    items: [],
    globalEnabled: false,
    canWrite: owner.canWrite,
    canInvalidate: owner.canInvalidate,
  };
  return `
    <div class="response-cache-workspace" data-response-cache-root>
      <header class="response-cache-toolbar">
        <div>
          <h2 id="response-cache-title">响应缓存</h2>
          <span>${escapeHtml(owner.tenantId)}</span>
        </div>
        <div class="response-cache-toolbar-actions">
          <span class="response-cache-global-state tone-${view.globalEnabled ? "enabled" : "disabled"}">
            ${view.globalEnabled ? "全局可用" : "全局未启用"}
          </span>
          <button type="button" class="icon-command" data-response-cache-refresh title="刷新" aria-label="刷新响应缓存策略">↻</button>
          ${owner.canInvalidate
            ? `<button type="button" class="response-cache-invalidate-command" data-response-cache-invalidate-open${stateBlocksInvalidation(view) ? " disabled" : ""}>失效缓存</button>`
            : ""}
        </div>
      </header>
      ${renderActionFeedback(view)}
      <div class="response-cache-layout">
        ${renderPolicyList(view)}
        ${renderPolicyEditor(view, owner)}
      </div>
      ${renderInvalidationDialog(view, owner)}
    </div>
  `;
}

/** 未保存策略、CAS 冲突或在途保存期间禁止并行开启失效意图。 */
function stateBlocksInvalidation(state) {
  return Boolean(
    state.dirty
    || state.conflict
    || state.editorState === "saving"
    || state.editorState === "unknown_result",
  );
}

/** 渲染列表级错误、保存计数与失效计数，服务端 message/diagnosis 不进入页面。 */
function renderActionFeedback(state) {
  const editor = state.editorFeedback;
  const invalidation = state.invalidateFeedback;
  const error = state.error;
  // 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。
  if (error) return renderError(error, "响应缓存策略读取失败");
  if (editor) {
    const verb = editor.replay ? "已确认此前保存结果" : (editor.changed ? "策略已保存" : "策略无变化");
    return `
      <div class="response-cache-feedback" role="status" aria-live="polite">
        ${escapeHtml(verb)} · 失效条目 ${editor.invalidatedEntryCount} · 围栏填充 ${editor.fencedFillCount}
        · ${renderChainId(editor.requestId)}
      </div>
    `;
  }
  if (invalidation) {
    return `
      <div class="response-cache-feedback" role="status" aria-live="polite">
        ${invalidation.replay ? "已确认此前失效结果" : "缓存失效已提交"}
        · 匹配 ${invalidation.matchedEntryCount} · 删除 ${invalidation.deletedEntryCount}
        · 围栏填充 ${invalidation.fencedFillCount} · ${renderChainId(invalidation.requestId)}
      </div>
    `;
  }
  return `<div class="response-cache-feedback" role="status" aria-live="polite"></div>`;
}

/** 渲染 active Alias 与可空策略摘要；按钮只携带公开 Alias ID。 */
function renderPolicyList(state) {
  if (state.state === "error" && !state.items?.length) {
    return `<section class="response-cache-list">${renderError(state.error, "策略列表读取失败")}</section>`;
  }
  const items = Array.isArray(state.items) ? state.items : [];
  return `
    <section class="response-cache-list" aria-labelledby="response-cache-list-title">
      <div class="response-cache-section-heading">
        <h3 id="response-cache-list-title">Alias 策略</h3><span>${items.length} 项</span>
      </div>
      ${state.state === "loading" && !items.length
        ? `<div class="response-cache-empty">正在读取策略</div>`
        : items.length
          ? `<div class="response-cache-table" role="table">
              <div class="response-cache-row is-head" role="row">
                <span>环境 / Alias</span><span>状态</span><span>TTL</span><span>可用条目</span><span>收费</span><span>操作</span>
              </div>
              ${items.map((item) => renderPolicyRow(item, state.selectedAliasId)).join("")}
            </div>`
          : `<div class="response-cache-empty">当前租户没有可配置的 active Alias</div>`}
      <div class="response-cache-pager">
        <button type="button" data-response-cache-previous${state.canPrevious ? "" : " disabled"}>上一页</button>
        <button type="button" data-response-cache-next${state.canNext ? "" : " disabled"}>下一页</button>
      </div>
    </section>
  `;
}

/** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
function renderPolicyRow(item, selectedAliasId) {
  const policy = item.cache_policy;
  const status = policy?.status ?? "disabled";
  return `
    <div class="response-cache-row${item.model_alias_id === selectedAliasId ? " is-selected" : ""}" role="row">
      <span data-label="环境 / Alias"><strong>${escapeHtml(item.alias)}</strong><small>${escapeHtml(item.environment_code)}</small></span>
      <span data-label="状态"><b class="response-cache-policy-state tone-${escapeAttribute(status)}">${escapeHtml(STATUS_LABELS[status])}</b></span>
      <span data-label="TTL">${policy ? `${policy.ttl_seconds} 秒` : "-"}</span>
      <span data-label="可用条目">${policy?.ready_entry_count ?? 0}</span>
      <span data-label="收费">${policy?.charge_cache_hits ? "收费" : "免费"}</span>
      <span data-label="操作"><button type="button" class="icon-command" data-response-cache-select="${escapeAttribute(item.model_alias_id)}" title="编辑策略" aria-label="编辑 ${escapeAttribute(item.alias)} 的响应缓存策略">→</button></span>
    </div>
  `;
}

/** 渲染当前 Alias 的完整 CAS 编辑器；无 write 时仍保留只读策略事实。 */
function renderPolicyEditor(state, owner) {
  const editor = state.editor;
  if (!editor) {
    return `<section class="response-cache-editor"><div class="response-cache-empty">选择一个 Alias 编辑策略</div></section>`;
  }
  const item = state.items.find((candidate) => candidate.model_alias_id === editor.model_alias_id);
  const disabled = !owner.canWrite || state.editorState === "saving" || state.conflict;
  const enabled = editor.status === "enabled";
  return `
    <section class="response-cache-editor" aria-labelledby="response-cache-editor-title">
      <div class="response-cache-editor-heading">
        <div><h3 id="response-cache-editor-title">${escapeHtml(item?.alias ?? editor.model_alias_id)}</h3><span>${escapeHtml(item?.environment_code ?? "-")}</span></div>
        <b>v${editor.expected_version}</b>
      </div>
      ${!state.globalEnabled
        ? `<div class="response-cache-configuration-warning" role="status">全局缓存能力当前不可用</div>`
        : ""}
      <form class="response-cache-policy-form" data-response-cache-policy-form novalidate>
        <label class="response-cache-toggle span-2">
          <input type="checkbox" name="enabled"${enabled ? " checked" : ""}${disabled ? " disabled" : ""}>
          <span>启用此 Alias 的响应缓存</span>
        </label>
        <label class="response-cache-field">
          <span>TTL（秒）</span>
          <input type="number" inputmode="numeric" name="ttl_seconds" min="60" max="86400" step="1" value="${editor.ttl_seconds}" required${disabled ? " disabled" : ""}>
        </label>
        <label class="response-cache-field">
          <span>Data class</span>
          <input name="data_class" maxlength="64" pattern="[a-z0-9][a-z0-9._-]{0,63}" value="${escapeAttribute(editor.data_class ?? "")}" autocomplete="off"${disabled ? " disabled" : ""}>
        </label>
        <label class="response-cache-field span-2">
          <span>Machine tags</span>
          <input name="tags" maxlength="1039" value="${escapeAttribute(editor.tags.join(", "))}" autocomplete="off"${disabled ? " disabled" : ""}>
        </label>
        <label class="response-cache-toggle">
          <input type="checkbox" name="single_data_class_confirmed"${editor.single_data_class_confirmed ? " checked" : ""}${disabled || !enabled ? " disabled" : ""}>
          <span>此 Alias 只承载一个可信 data class</span>
        </label>
        <label class="response-cache-toggle">
          <input type="checkbox" name="charge_cache_hits"${editor.charge_cache_hits ? " checked" : ""}${disabled ? " disabled" : ""}>
          <span>缓存命中计费</span>
        </label>
        <div class="response-cache-editor-live span-2${state.editorError ? " is-error" : ""}" role="status" aria-live="polite">
          ${renderEditorStatus(state)}
        </div>
        <div class="response-cache-editor-actions span-2">
          ${state.conflict
            ? `<button type="button" data-response-cache-rebase>重新读取并保留草稿</button>`
            : ""}
          ${owner.canWrite
            ? `<button type="button" data-response-cache-discard${state.dirty && !state.conflict ? "" : " disabled"}>放弃更改</button>
               <button type="submit" class="primary"${state.dirty && state.editorState !== "saving" && !state.conflict ? "" : " disabled"}>${state.editorState === "saving" ? "正在保存" : state.editorState === "unknown_result" ? "重试同一保存" : "保存策略"}</button>`
            : `<span class="response-cache-readonly">只读</span>`}
        </div>
      </form>
    </section>
  `;
}

/** 保存态只展示本地固定文案、code 和 request_id。 */
function renderEditorStatus(state) {
  if (state.editorState === "saving") return "正在保存策略";
  if (state.editorState === "unknown_result") {
    return `${escapeHtml(state.editorError?.message ?? "保存结果待确认")} · ${escapeHtml(state.editorError?.code ?? "response_cache.unavailable")} · ${renderChainId(state.editorError?.requestId)}`;
  }
  // 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。
  if (state.editorError) {
    return `${escapeHtml(state.editorError.message)} · ${escapeHtml(state.editorError.code)} · ${renderChainId(state.editorError.requestId)}`;
  }
  if (state.dirty) return "有未保存更改";
  return "";
}

/** 失效 Dialog 只枚举当前 GET 派生的 selector，不展示额外模型、Virtual Key 或 data class。 */
function renderInvalidationDialog(state, owner) {
  if (!owner.canInvalidate || !state.invalidateOpen) return "";
  const selectors = Array.isArray(state.selectors) ? state.selectors : [];
  const submitting = state.invalidateState === "submitting";
  return `
    <div class="response-cache-dialog-backdrop" data-response-cache-dialog-backdrop>
      <section class="response-cache-dialog" role="dialog" aria-modal="true" aria-labelledby="response-cache-invalidate-title">
        <header>
          <h3 id="response-cache-invalidate-title">失效响应缓存</h3>
          <button type="button" class="icon-command" data-response-cache-invalidate-close title="关闭" aria-label="关闭失效对话框"${submitting ? " disabled" : ""}>×</button>
        </header>
        <form data-response-cache-invalidate-form novalidate>
          <fieldset class="response-cache-segmented">
            <legend>范围</legend>
            <label><input type="radio" name="scope" value="all" checked><span>全部</span></label>
            <label><input type="radio" name="scope" value="tags"${selectors.length ? "" : " disabled"}><span>标签组合</span></label>
          </fieldset>
          <fieldset class="response-cache-selector-list" data-response-cache-selector-list disabled>
            <legend>同时匹配</legend>
            ${selectors.length
              ? selectors.map((selector) => `<label><input type="checkbox" name="selector" value="${escapeAttribute(selector)}"><code>${escapeHtml(selector)}</code></label>`).join("")
              : `<span>没有可用 selector</span>`}
          </fieldset>
          <label class="response-cache-field">
            <span>原因</span>
            <select name="reason_code">
              ${Object.entries(REASON_LABELS).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}
            </select>
          </label>
          <div class="response-cache-invalidate-live${state.invalidateError ? " is-error" : ""}" role="status" aria-live="polite">
            ${state.invalidateError
              ? `${escapeHtml(state.invalidateError.message)} · ${escapeHtml(state.invalidateError.code)} · ${renderChainId(state.invalidateError.requestId)}`
              : submitting ? "正在提交失效操作" : ""}
          </div>
          <div class="response-cache-dialog-actions">
            <button type="button" data-response-cache-invalidate-cancel${submitting ? " disabled" : ""}>取消</button>
            <button type="submit" class="danger"${submitting ? " disabled" : ""}>${state.invalidateState === "unknown_result" ? "重试同一失效" : "确认失效"}</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

/** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
function renderError(error, fallback) {
  return `
    <div class="response-cache-error" role="alert">
      <strong>${escapeHtml(error?.message ?? fallback)}</strong>
      <code>${escapeHtml(error?.code ?? "response_cache.unavailable")}</code>
      ${renderChainId(error?.requestId)}
    </div>
  `;
}

/** 链路 ID 在交互页面可见，但由 Browser 验收的截图专用样式独立脱敏。 */
function renderChainId(value) {
  return `<span data-response-cache-chain-id>request_id: ${escapeHtml(value ?? "-")}</span>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
