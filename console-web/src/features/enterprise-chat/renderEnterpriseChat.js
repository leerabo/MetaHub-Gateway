import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

/**
 * 渲染 PAGE-093 企业 Chat。
 *
 * 该函数只处理当前页面内存快照；所有正文都经过 HTML/属性转义，不解析 Markdown、不使用 innerHTML
 * 投影未经转义的模型内容。前端权限只控制 DOM，后端 ``chat.use`` 仍会逐请求复核。
 */
export function renderEnterpriseChat(state, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.CHAT_USE)) return "";
  const safeState = state ?? {};
  const options = safeState.options ?? { projects: [], model_aliases: [], prompt_templates: [] };
  const busy = safeState.busy === true;
  const session = safeState.session ?? null;
  const historyLimitReached = safeState.historyLimitReached === true;
  const selectedTemplate = options.prompt_templates?.find(
    (item) => item.prompt_template_version_id === safeState.selectedPromptTemplateVersionId,
  ) ?? null;
  const recommendedModelIds = selectedTemplate?.recommended_model_alias_ids ?? [];
  const aliases = orderAliasesByRecommendation(options.model_aliases.filter(
    (item) => item.environment_id === safeState.selectedEnvironmentId,
  ), recommendedModelIds);
  const canCreate = !busy
    && !session
    && Boolean(safeState.selectedProjectId)
    && Boolean(safeState.selectedModelAliasId);
  const canSend = !busy
    && safeState.status === "ready"
    && Boolean(session)
    && !historyLimitReached
    && (safeState.selectedPromptTemplateVersionId
      ? safeState.templateReady === true
      : String(safeState.draft ?? "").trim().length > 0);
  return `
    <div class="enterprise-chat" data-enterprise-chat-root>
      <header class="enterprise-chat-toolbar">
        <div class="enterprise-chat-title">
          <h2 id="enterprise-chat-title">企业 Chat</h2>
          ${session ? `<span class="enterprise-chat-session-state">会话有效</span>` : ""}
        </div>
        <div class="enterprise-chat-selectors">
          <label>
            <span>项目与环境</span>
            <select data-enterprise-chat-project ${busy || session ? "disabled" : ""}>
              ${renderProjectOptions(options.projects, safeState)}
            </select>
          </label>
          <label>
            <span>模型</span>
            <select data-enterprise-chat-alias ${busy || session || !aliases.length ? "disabled" : ""}>
              ${renderAliasOptions(aliases, safeState.selectedModelAliasId, recommendedModelIds)}
            </select>
          </label>
          ${safeState.templateUseAuthorized ? `<label>
            <span>Prompt 模板</span>
            <select data-enterprise-chat-template ${busy ? "disabled" : ""}>
              ${renderTemplateOptions(options.prompt_templates, safeState)}
            </select>
          </label>` : ""}
        </div>
        <div class="enterprise-chat-tools">
          <button class="enterprise-chat-icon-button" type="button" data-enterprise-chat-new
            title="新建会话" aria-label="新建会话" ${canCreate ? "" : "disabled"}>＋</button>
          <button class="enterprise-chat-icon-button" type="button" data-enterprise-chat-clear
            title="清空会话" aria-label="清空会话" ${session && !busy ? "" : "disabled"}>×</button>
        </div>
      </header>
      ${renderError(safeState.error)}
      <div class="enterprise-chat-messages" role="log" aria-live="polite" aria-relevant="additions">
        ${renderMessages(safeState.messages)}
      </div>
      <form class="enterprise-chat-composer" data-enterprise-chat-form autocomplete="off">
        ${selectedTemplate || safeState.templateOptionsStale
          ? renderTemplateVariables(selectedTemplate, safeState)
          : ""}
        <label class="enterprise-chat-draft" ${selectedTemplate || safeState.templateOptionsStale ? "hidden" : ""}>
          <span class="sr-only">消息</span>
          <textarea name="message" rows="3" maxlength="${escapeAttribute(safeState.limits?.max_message_chars ?? 8000)}"
            placeholder="输入消息" spellcheck="true" ${session && !busy && safeState.status === "ready" && !selectedTemplate && !safeState.templateOptionsStale ? "" : "disabled"}></textarea>
        </label>
        <button class="enterprise-chat-send" type="submit" title="发送" aria-label="发送"
          ${canSend ? "" : "disabled"}>↑</button>
      </form>
      <div class="enterprise-chat-live-status" data-enterprise-chat-live-status role="status" aria-live="polite">${escapeHtml(statusText(safeState))}</div>
    </div>
  `;
}

/**
 * 使用 DOM 文本 API 注入 Prompt/Response 和草稿。
 *
 * 模板字符串从不包含正文，即使调用方误把 renderer 结果传给 innerHTML，模型输出也不会被解析成标签；
 * hydration 只能在当前页面同步调用，且不会写 URL、Storage、日志、dataset 或属性。
 */
export function hydrateEnterpriseChatText(root, state) {
  const scope = root?.querySelectorAll ? root : null;
  if (!scope) return false;
  const messages = Array.isArray(state?.messages) ? state.messages : [];
  for (const element of scope.querySelectorAll("[data-enterprise-chat-message-index]")) {
    const index = Number(element.getAttribute("data-enterprise-chat-message-index"));
    const body = element.querySelector(".enterprise-chat-message-body");
    if (body && Number.isSafeInteger(index) && index >= 0 && index < messages.length) {
      body.textContent = String(messages[index]?.content ?? "");
    }
  }
  const textarea = scope.querySelector("[data-enterprise-chat-form] textarea[name='message']");
  if (textarea) textarea.value = String(state?.draft ?? "");
  for (const input of scope.querySelectorAll("[data-enterprise-chat-template-variable]")) {
    const name = input.getAttribute("data-enterprise-chat-template-variable");
    input.value = String(state?.templateVariableValues?.[name] ?? "");
  }
  return true;
}

/** 项目选项只显示后端安全目录字段，不显示 Key ID、前缀或数量。 */
function renderProjectOptions(projects, state) {
  if (!Array.isArray(projects) || !projects.length) {
    return `<option value="">暂无可用项目</option>`;
  }
  return projects.map((item) => {
    const selected = item.project_id === state.selectedProjectId
      && item.environment_id === state.selectedEnvironmentId;
    const unavailable = item.availability !== "ready";
    const suffix = item.availability === "ambiguous_key"
      ? " / 运行身份配置冲突"
      : (item.availability === "key_unavailable" ? " / 暂不可用" : "");
    return `<option value="${escapeAttribute(item.project_id)}" data-environment-id="${escapeAttribute(item.environment_id)}"
      ${selected ? "selected" : ""} ${unavailable ? "disabled" : ""}>${escapeHtml(item.name)} / ${escapeHtml(item.environment_name)}${escapeHtml(suffix)}</option>`;
  }).join("");
}

/** 模型选项只消费当前环境的 active 别名安全投影。 */
function renderAliasOptions(aliases, selectedAliasId, recommendedModelIds = []) {
  if (!aliases.length) return `<option value="">暂无可用模型</option>`;
  return aliases.map((item) => (
    `<option value="${escapeAttribute(item.model_alias_id)}" ${item.model_alias_id === selectedAliasId ? "selected" : ""}>${escapeHtml(item.display_name || item.alias)}${recommendedModelIds.includes(item.model_alias_id) ? " · 推荐" : ""}</option>`
  )).join("");
}

/**
 * 渲染当前项目可选模板最小目录。
 *
 * stale 选择以禁用哨兵保留，禁止模板失效后静默选中“无模板”并发送自由 draft。
 */
function renderTemplateOptions(templates, state) {
  const selectedVersionId = state.selectedPromptTemplateVersionId ?? "";
  const applicable = (Array.isArray(templates) ? templates : []).filter((item) => (
    item.project_scope === "all" || item.applicable_project_ids?.includes(state.selectedProjectId)
  ));
  const stale = state.templateOptionsStale && selectedVersionId;
  return `
    <option value="" ${!selectedVersionId ? "selected" : ""}>无模板</option>
    ${stale ? `<option value="${escapeAttribute(selectedVersionId)}" selected disabled>模板已失效，请重新选择</option>` : ""}
    ${applicable.map((item) => `<option value="${escapeAttribute(item.prompt_template_version_id)}"
      ${!stale && item.prompt_template_version_id === selectedVersionId ? "selected" : ""}>
      ${escapeHtml(item.name)} · v${escapeHtml(item.version_number)}
    </option>`).join("")}
  `;
}

/** 渲染变量输入框架；变量值保持空 attribute，随后只通过 input.value hydration 注入。 */
function renderTemplateVariables(template, state) {
  if (state.templateOptionsStale) {
    return `<div class="enterprise-chat-template-stale" role="alert">模板目录已变化，请重新选择模板或无模板</div>`;
  }
  if (!template) return "";
  if (!template.variables.length) {
    return "";
  }
  return `<div class="enterprise-chat-template-variables">
    ${template.variables.map((item) => `<label>
      <span>${escapeHtml(item.label)}${item.required ? " *" : ""}</span>
      <input type="text" maxlength="${escapeAttribute(item.max_chars)}"
        data-enterprise-chat-template-variable="${escapeAttribute(item.name)}"
        ${state.session && !state.busy && state.status === "ready" ? "" : "disabled"}>
    </label>`).join("")}
  </div>`;
}

/** 推荐模型只调整当前下拉展示顺序；状态机的 selectedModelAliasId 从不在此函数中修改。 */
function orderAliasesByRecommendation(aliases, recommendedModelIds) {
  const order = new Map((Array.isArray(recommendedModelIds) ? recommendedModelIds : []).map(
    (modelAliasId, index) => [modelAliasId, index],
  ));
  return aliases.map((item, index) => ({ item, index })).sort((left, right) => {
    const leftRank = order.has(left.item.model_alias_id) ? order.get(left.item.model_alias_id) : Number.MAX_SAFE_INTEGER;
    const rightRank = order.has(right.item.model_alias_id) ? order.get(right.item.model_alias_id) : Number.MAX_SAFE_INTEGER;
    return leftRank - rightRank || left.index - right.index;
  }).map(({ item }) => item);
}

/** 按消息顺序渲染正文与 assistant 治理摘要；正文永远按纯文本处理。 */
function renderMessages(messages) {
  if (!Array.isArray(messages) || !messages.length) {
    return `<div class="enterprise-chat-empty"></div>`;
  }
  return messages.map((item, index) => `
    <article class="enterprise-chat-message" data-role="${item.role === "assistant" ? "assistant" : "user"}"
      data-enterprise-chat-message-index="${index}">
      <div class="enterprise-chat-message-body"></div>
    </article>
  `).join("");
}

/** 错误只展示用户可执行的消息，不把内部调用标识写入聊天窗口。 */
function renderError(error) {
  if (!error) return "";
  return `<div class="enterprise-chat-error" role="alert">
    <span>${escapeHtml(error.message ?? "企业 Chat 暂时不可用")}</span>
  </div>`;
}

/** 映射页面忙状态；不显示功能说明或内部处理阶段。 */
function statusText(state) {
  if (state.operationKind === "options") return "正在加载";
  if (state.operationKind === "create") return "正在创建会话";
  if (state.operationKind === "send") return "正在生成回复";
  if (state.operationKind === "clear") return "正在清空会话";
  if (state.status === "uncertain") return "调用结果无法安全重放，请清空会话";
  // 上限提示由状态机提供固定文案；这里不计算或回显正文长度，避免 Prompt 进入模板或辅助状态。
  if (state.historyLimitReached === true) return state.historyLimitMessage ?? "";
  return "";
}

/** 转义纯文本，保留换行由 CSS white-space 控制。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 属性值复用同一转义规则，禁止 ID 或长度值突破标签边界。 */
function escapeAttribute(value) {
  return escapeHtml(value);
}
