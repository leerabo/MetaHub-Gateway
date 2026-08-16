/** PAGE-114 compact three-tab governance surface. */

import { mcpToolPageOwner } from "./mcpToolPageState.js";

const STATUS_LABELS = Object.freeze({
  pending_sync: "待同步",
  active: "已启用",
  sync_failed: "同步失败",
  disabled: "已停用",
  review_required: "待审核",
  approval_pending: "待审批",
  queued: "已排队",
  probing: "校验中",
  calling: "调用中",
  succeeded: "成功",
  failed: "失败",
  blocked: "已阻断",
  rejected: "已拒绝",
  outcome_unknown: "结果未知",
});

/** Render nothing unless both auth and backend navigation still own PAGE-114. */
export function renderMcpTools(state, authContext, consoleShellState) {
  const owner = mcpToolPageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  return `
    <div class="mcp-workspace" data-mcp-root>
      <header class="mcp-toolbar">
        <div>
          <h2 id="mcp-tools-title">MCP 工具</h2>
          <span>当前租户 · Streamable HTTP</span>
        </div>
        <div class="mcp-toolbar-actions">
          <button type="button" class="mcp-icon-command" data-mcp-refresh title="刷新当前视图" aria-label="刷新当前视图"${state.busy ? " disabled" : ""}>&#8635;</button>
          ${owner.canManageServers ? `<button type="button" class="primary" data-mcp-open-register${state.busy ? " disabled" : ""}>注册 Server</button>` : ""}
        </div>
      </header>
      ${renderFeedback(state.feedback)}
      <div class="mcp-tabs" role="tablist" aria-label="MCP 治理视图">
        ${tabButton("servers", "Servers", state.activeTab)}
        ${tabButton("tools", "Tools", state.activeTab)}
        ${tabButton("invocations", "Invocations", state.activeTab)}
      </div>
      <section class="mcp-tab-panel" role="tabpanel" aria-labelledby="mcp-tab-${escapeAttribute(state.activeTab)}">
        ${renderActiveTab(state, owner)}
      </section>
      ${renderRegisterDialog(state, owner)}
      ${renderToolDialog(state, owner)}
    </div>
  `;
}

function tabButton(tab, label, activeTab) {
  const active = tab === activeTab;
  return `
    <button type="button" id="mcp-tab-${tab}" role="tab" data-mcp-tab="${tab}" aria-selected="${active}" tabindex="${active ? "0" : "-1"}">${label}</button>
  `;
}

function renderActiveTab(state, owner) {
  const tab = state.tabs?.[state.activeTab];
  if (!tab || tab.state === "idle" || tab.state === "loading") {
    return `<div class="mcp-empty" role="status">正在读取 ${escapeHtml(state.activeTab ?? "MCP")} 安全摘要</div>`;
  }
  if (tab.state === "error") return renderError(tab.error);
  const content = state.activeTab === "servers"
    ? renderServers(tab.items, owner, state.busy)
    : state.activeTab === "tools"
      ? renderTools(tab.items, owner, state.busy)
      : renderInvocations(tab.items);
  return `${content}${renderPagination(tab, state.busy)}`;
}

function renderPagination(tab, busy) {
  if (!tab.canPrevious && !tab.canNext) return "";
  return `
    <nav class="mcp-pagination" aria-label="MCP 列表分页">
      <button type="button" data-mcp-previous-page${tab.canPrevious && !busy ? "" : " disabled"}>上一页</button>
      <button type="button" data-mcp-next-page${tab.canNext && !busy ? "" : " disabled"}>下一页</button>
    </nav>
  `;
}

function renderServers(items, owner, busy) {
  if (!items.length) return `<div class="mcp-empty">尚未注册 MCP Server</div>`;
  return `
    <div class="mcp-table-scroll">
      <table class="mcp-table mcp-server-table">
        <thead><tr><th>Server</th><th>连接摘要</th><th>状态</th><th>工具</th><th>最近更新</th><th><span class="sr-only">操作</span></th></tr></thead>
        <tbody>${items.map((server) => `
          <tr>
            <th data-label="Server"><strong>${escapeHtml(server.name)}</strong><code>${escapeHtml(server.mcp_server_id)}</code></th>
            <td data-label="连接摘要"><span>${escapeHtml(server.endpoint_hint)}</span><small>${server.endpoint_has_path ? "含路径" : "根路径"} · ${escapeHtml(server.transport)}</small></td>
            <td data-label="状态"><span class="mcp-status tone-${escapeAttribute(server.status)}">${statusLabel(server.status)}</span><small>${escapeHtml(server.protocol_version ?? "-")}</small></td>
            <td data-label="工具"><strong>${numberText(server.tool_count)}</strong><small>启用 ${numberText(server.active_tool_count)}</small></td>
            <td data-label="最近更新">${formatTime(server.updated_at)}</td>
            <td data-label="操作" class="mcp-action-cell"><div class="mcp-server-actions">
              ${owner.canManageServers && owner.canManagePolicies ? `<button type="button" class="mcp-icon-command" data-mcp-refresh-server="${escapeAttribute(server.mcp_server_id)}" title="刷新工具" aria-label="刷新 ${escapeAttribute(server.name)} 的工具"${busy || server.status === "disabled" ? " disabled" : ""}>&#8635;</button>` : ""}
              ${owner.canManageServers ? renderServerStatusCommand(server, busy) : ""}
            </div></td>
          </tr>
        `).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderServerStatusCommand(server, busy) {
  const enabling = server.status === "disabled";
  const command = enabling ? "启用" : "停用";
  return `<button type="button" class="mcp-icon-command" data-mcp-server-status="${escapeAttribute(server.mcp_server_id)}" data-mcp-next-status="${enabling ? "active" : "disabled"}" title="${command} Server" aria-label="${command} ${escapeAttribute(server.name)} Server"${busy ? " disabled" : ""}>&#x23FB;</button>`;
}

function renderTools(items, owner, busy) {
  if (!items.length) return `<div class="mcp-empty">尚未发现 MCP Tool</div>`;
  return `
    <div class="mcp-table-scroll">
      <table class="mcp-table mcp-tool-table">
        <thead><tr><th>Tool</th><th>Schema</th><th>治理</th><th>授权</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${items.map((tool) => `
          <tr>
            <th data-label="Tool"><strong>${escapeHtml(tool.title === "-" ? tool.remote_name : tool.title)}</strong><span>${escapeHtml(tool.remote_name)}</span><code>${escapeHtml(tool.mcp_tool_id)}</code></th>
            <td data-label="Schema"><code>${shortHash(tool.input_schema_effective_sha256)}</code><small>schema r${numberText(tool.schema_revision)}</small></td>
            <td data-label="治理"><span>${tool.risk_level === "high" ? "高危" : "标准"}</span><small>${tool.approval_required ? "需要审批" : "无需审批"} · policy r${numberText(tool.policy_revision)}</small></td>
            <td data-label="授权"><span>${numberText(tool.allowed_role_count)} 角色 / ${numberText(tool.allowed_user_count)} 用户</span><small>${tool.required_permission_codes.length ? tool.required_permission_codes.map(escapeHtml).join(" · ") : "仅 mcp.invoke"}</small></td>
            <td data-label="状态"><span class="mcp-status tone-${escapeAttribute(tool.status)}">${statusLabel(tool.status)}</span></td>
            <td data-label="操作" class="mcp-tool-actions">
              <button type="button" data-mcp-tool-detail="${escapeAttribute(tool.mcp_tool_id)}"${busy ? " disabled" : ""}>查看详情</button>
              ${owner.canManagePolicies ? `<button type="button" data-mcp-tool-policy="${escapeAttribute(tool.mcp_tool_id)}"${busy ? " disabled" : ""}>编辑策略</button>` : ""}
            </td>
          </tr>
        `).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderInvocations(items) {
  if (!items.length) return `<div class="mcp-empty">暂无 MCP 调用记录</div>`;
  return `
    <div class="mcp-invocation-list">
      ${items.map((invocation) => `
        <article class="mcp-invocation-row">
          <div class="mcp-invocation-owner">
            <code>${escapeHtml(invocation.mcp_invocation_id)}</code>
            <span class="mcp-status tone-${escapeAttribute(invocation.status)}">${statusLabel(invocation.status)}</span>
            <time>${formatTime(invocation.created_at)}</time>
          </div>
          <dl class="mcp-invocation-facts">
            <div><dt>Tool</dt><dd><code>${escapeHtml(invocation.mcp_tool_id)}</code></dd></div>
            <div><dt>审批</dt><dd>${invocation.approval_required ? escapeHtml(invocation.approval_task_id ?? "待创建") : "无需审批"}</dd></div>
            <div><dt>Input DLP</dt><dd>${dlpText(invocation.input_dlp_status, invocation.input_dlp_match_count)}</dd></div>
            <div><dt>Output DLP</dt><dd>${dlpText(invocation.output_dlp_status, invocation.output_dlp_match_count)}</dd></div>
            <div><dt>脱敏</dt><dd>${numberText(invocation.redaction_count)}</dd></div>
            <div><dt>Attempts</dt><dd>${numberText(invocation.probe_attempt_count)} / ${numberText(invocation.call_attempt_count)}</dd></div>
            <div><dt>耗时</dt><dd>${nullableNumber(invocation.duration_ms, " ms")}</dd></div>
            <div><dt>Bytes</dt><dd>${nullableNumber(invocation.request_bytes)} / ${nullableNumber(invocation.response_bytes)}</dd></div>
            <div><dt>调用事实</dt><dd>${escapeHtml(invocation.execution_status)} · ${nullableNumber(invocation.tool_call_count)}</dd></div>
            <div><dt>成本</dt><dd>${escapeHtml(invocation.cost_status)}</dd></div>
            <div><dt>错误</dt><dd><code>${escapeHtml(invocation.error_code ?? "-")}</code></dd></div>
          </dl>
        </article>
      `).join("")}
    </div>
  `;
}

function renderRegisterDialog(state, owner) {
  if (!owner.canManageServers || !state.registerDialog?.open) return "";
  const busy = state.registerDialog.state === "submitting";
  return `
    <dialog class="mcp-dialog" data-mcp-dialog="register" aria-labelledby="mcp-register-title" aria-busy="${busy}">
      <header><div><h3 id="mcp-register-title">注册 MCP Server</h3><span>Streamable HTTP</span></div><button type="button" class="mcp-icon-command" data-mcp-close-dialog title="关闭" aria-label="关闭"${busy ? " disabled" : ""}>&times;</button></header>
      ${state.registerDialog.error ? renderError(state.registerDialog.error) : ""}
      <form data-mcp-register-form novalidate autocomplete="off">
        <label class="mcp-field"><span>名称</span><input name="name" maxlength="80" required${busy ? " disabled" : ""}></label>
        <label class="mcp-field"><span>Endpoint URL</span><input name="endpoint_url" type="url" maxlength="2048" autocomplete="off" spellcheck="false" required${busy ? " disabled" : ""}></label>
        <label class="mcp-field"><span>认证</span><select name="auth_type"${busy ? " disabled" : ""}><option value="none">无认证</option><option value="bearer">Bearer Token</option></select></label>
        <label class="mcp-field" data-mcp-bearer-field hidden><span>Bearer Token</span><input name="bearer_token" type="password" maxlength="8192" autocomplete="new-password" spellcheck="false"${busy ? " disabled" : ""}></label>
        <p class="mcp-form-error" data-mcp-form-error role="alert"></p>
        <div class="mcp-dialog-actions"><button type="button" data-mcp-close-dialog${busy ? " disabled" : ""}>取消</button><button type="submit" class="primary"${busy ? " disabled" : ""}>${busy ? "正在提交" : "注册"}</button></div>
      </form>
    </dialog>
  `;
}

function renderToolDialog(state, owner) {
  const dialog = state.toolDialog;
  if (!dialog?.open || (dialog.kind === "policy" && !owner.canManagePolicies)) return "";
  const busy = ["loading", "submitting"].includes(dialog.state);
  const title = dialog.kind === "policy" ? "编辑 Tool 策略" : "Tool Schema 详情";
  return `
    <dialog class="mcp-dialog mcp-tool-dialog" data-mcp-dialog="${escapeAttribute(dialog.kind)}" aria-labelledby="mcp-tool-dialog-title" aria-busy="${busy}">
      <header><div><h3 id="mcp-tool-dialog-title">${title}</h3><code>${escapeHtml(dialog.toolId)}</code></div><button type="button" class="mcp-icon-command" data-mcp-close-dialog title="关闭" aria-label="关闭"${busy ? " disabled" : ""}>&times;</button></header>
      ${dialog.state === "loading" ? `<div class="mcp-empty" role="status">正在按权限读取详情</div>` : ""}
      ${dialog.error ? renderError(dialog.error) : ""}
      ${dialog.tool && dialog.kind === "detail" ? renderSchemaDetail(dialog.tool) : ""}
      ${dialog.tool && dialog.kind === "policy" && dialog.state !== "error" ? renderPolicyForm(dialog.tool, busy) : ""}
      ${dialog.state !== "loading" && !busy ? `<div class="mcp-dialog-actions"><button type="button" data-mcp-close-dialog>关闭</button></div>` : ""}
    </dialog>
  `;
}

function renderSchemaDetail(tool) {
  return `
    <div class="mcp-tool-detail">
      <dl class="mcp-summary-strip">
        <div><dt>Remote name</dt><dd>${escapeHtml(tool.remote_name)}</dd></div>
        <div><dt>Schema revision</dt><dd>${numberText(tool.schema_revision)}</dd></div>
        <div><dt>Policy revision</dt><dd>${numberText(tool.policy_revision)}</dd></div>
        <div><dt>状态</dt><dd>${statusLabel(tool.status)}</dd></div>
      </dl>
      ${tool.description ? `<section class="mcp-description"><h4>受控描述</h4><p>${escapeHtml(tool.description)}</p></section>` : ""}
      ${schemaBlock("Input · Original", tool.input_schema_original)}
      ${schemaBlock("Input · Effective", tool.input_schema_effective)}
      ${tool.output_schema_original ? schemaBlock("Output · Original", tool.output_schema_original) : ""}
      ${tool.output_schema_effective ? schemaBlock("Output · Effective", tool.output_schema_effective) : ""}
    </div>
  `;
}

function schemaBlock(label, value) {
  return `<section class="mcp-schema-block"><h4>${escapeHtml(label)}</h4><pre tabindex="0"><code>${escapeHtml(value)}</code></pre></section>`;
}

function renderPolicyForm(tool, busy) {
  return `
    <form data-mcp-policy-form novalidate>
      <input type="hidden" name="expected_policy_revision" value="${numberText(tool.policy_revision)}">
      <div class="mcp-policy-grid">
        <label class="mcp-field"><span>状态</span><select name="status"${busy ? " disabled" : ""}>${option("review_required", "待审核", tool.status)}${option("active", "已启用", tool.status)}${option("disabled", "已停用", tool.status)}</select></label>
        <label class="mcp-field"><span>风险</span><select name="risk_level"${busy ? " disabled" : ""}>${option("standard", "标准", tool.risk_level)}${option("high", "高危", tool.risk_level)}</select></label>
        <label class="mcp-field"><span>审批有效期（秒）</span><input name="approval_expires_seconds" type="number" min="300" max="86400" step="1" value="${numberText(tool.approval_expires_seconds)}"${busy ? " disabled" : ""}></label>
        <label class="mcp-toggle"><input name="approval_required" type="checkbox"${tool.approval_required ? " checked" : ""}${busy ? " disabled" : ""}><span>调用前需要审批</span></label>
        <label class="mcp-field"><span>附加 permission codes（每行一个）</span><textarea name="required_permission_codes" rows="3" maxlength="1024"${busy ? " disabled" : ""}>${escapeHtml(tool.required_permission_codes.join("\n"))}</textarea></label>
        <label class="mcp-field"><span>允许角色 IDs（每行一个）</span><textarea name="allowed_role_ids" rows="3" maxlength="4096"${busy ? " disabled" : ""}>${escapeHtml(tool.allowed_role_ids.join("\n"))}</textarea></label>
        <label class="mcp-field"><span>允许用户 IDs（每行一个）</span><textarea name="allowed_user_ids" rows="3" maxlength="4096"${busy ? " disabled" : ""}>${escapeHtml(tool.allowed_user_ids.join("\n"))}</textarea></label>
        <label class="mcp-field"><span>结果脱敏 JSON Pointers（每行一个）</span><textarea name="result_redaction_pointers" rows="3" maxlength="16384"${busy ? " disabled" : ""}>${escapeHtml(tool.result_redaction_pointers.join("\n"))}</textarea></label>
      </div>
      <p class="mcp-form-error" data-mcp-form-error role="alert"></p>
      <div class="mcp-dialog-actions"><button type="button" data-mcp-close-dialog${busy ? " disabled" : ""}>取消</button><button type="submit" class="primary"${busy ? " disabled" : ""}>${busy ? "正在保存" : "保存策略"}</button></div>
    </form>
  `;
}

function renderFeedback(feedback) {
  if (!feedback) return `<div class="mcp-feedback" role="status" aria-live="polite"></div>`;
  if (feedback.kind === "error") {
    return `<div class="mcp-feedback is-error" role="alert"><code>${escapeHtml(feedback.code)}</code> · request_id: ${escapeHtml(feedback.requestId)}</div>`;
  }
  const label = feedback.code === "mcp.policy_updated"
    ? "策略已更新"
    : feedback.code === "mcp.refresh_enqueued"
      ? "刷新任务已提交"
      : feedback.code === "mcp.server_enabled"
        ? "Server 已启用"
        : feedback.code === "mcp.server_disabled"
          ? "Server 已停用"
          : "Server 注册任务已提交";
  return `<div class="mcp-feedback" role="status">${label} · <code>${escapeHtml(feedback.objectId)}</code> · request_id: ${escapeHtml(feedback.requestId)}</div>`;
}

function renderError(error) {
  return `<div class="mcp-error" role="alert"><code>${escapeHtml(error?.code ?? "mcp.unavailable")}</code><span>request_id: ${escapeHtml(error?.requestId ?? "-")}</span></div>`;
}

function option(value, label, current) {
  return `<option value="${value}"${current === value ? " selected" : ""}>${label}</option>`;
}

function shortHash(value) {
  const normalized = String(value ?? "");
  return /^[0-9a-f]{64}$/.test(normalized) ? `${normalized.slice(0, 12)}...` : "-";
}

function statusLabel(value) {
  return escapeHtml(STATUS_LABELS[value] ?? value ?? "未知");
}

function dlpText(status, matches) {
  return `${escapeHtml(status ?? "未扫描")} · ${matches == null ? "-" : numberText(matches)}`;
}

function nullableNumber(value, suffix = "") {
  return value == null ? "-" : `${numberText(value)}${suffix}`;
}

function numberText(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? String(number) : "-";
}

function formatTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "-" : escapeHtml(parsed.toLocaleString("zh-CN", { hour12: false }));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
