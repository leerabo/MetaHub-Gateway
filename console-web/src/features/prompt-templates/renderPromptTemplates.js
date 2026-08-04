import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

/**
 * 渲染 PAGE-094 Prompt 模板工作区。
 *
 * 该函数只把列表、版本元数据和资源 ID 写入 HTML；解密正文、change summary、变量定义草稿及表单文本值
 * 永远不参与模板字符串。调用方必须在 DOM 就位后立即调用 hydratePromptTemplateSensitiveText。
 */
export function renderPromptTemplates(state, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.PROMPT_TEMPLATE_READ)) return "";
  const safeState = state ?? {};
  const canWrite = hasPermission(authContext, PERMISSIONS.PROMPT_TEMPLATE_WRITE)
    && safeState.canWrite === true;
  const templates = Array.isArray(safeState.templates) ? safeState.templates : [];
  const selected = safeState.selectedTemplateId ?? "";
  return `
    <div class="prompt-templates" data-prompt-templates-root>
      <header class="prompt-template-toolbar">
        <div class="prompt-template-heading">
          <h2 id="prompt-templates-title">Prompt 模板</h2>
          <span>${escapeHtml(safeState.total ?? templates.length)} 个模板</span>
        </div>
        <form class="prompt-template-filters" data-prompt-template-filter-form>
          <label>
            <span class="sr-only">搜索模板</span>
            <input type="search" name="search" maxlength="120" placeholder="搜索名称"
              value="${escapeAttribute(safeState.filters?.search ?? "")}">
          </label>
          <div class="prompt-template-segments" role="group" aria-label="状态筛选">
            ${renderStatusSegment("", "全部", safeState.filters?.status)}
            ${renderStatusSegment("active", "启用", safeState.filters?.status)}
            ${renderStatusSegment("disabled", "停用", safeState.filters?.status)}
          </div>
          <button class="prompt-template-icon-button" type="submit" title="刷新" aria-label="刷新">↻</button>
          ${canWrite ? `<button class="prompt-template-command" type="button" data-prompt-template-create>新建模板</button>` : ""}
        </form>
      </header>
      ${renderError(safeState.pageError)}
      <div class="prompt-template-workspace" data-prompt-template-mobile-view="${selected ? "detail" : "list"}">
        <aside class="prompt-template-list" aria-label="模板列表">
          ${renderTemplateList(templates, selected)}
        </aside>
        <section class="prompt-template-detail" aria-label="模板详情">
          ${renderDetail(safeState, canWrite)}
        </section>
      </div>
      ${renderDialog(safeState.dialog, safeState.options, canWrite)}
    </div>
  `;
}

/**
 * 使用 DOM 文本和值 API 注入解密定义及本地草稿。
 *
 * 每次 hydration 先清空旧敏感节点，防止状态切换后残留；该函数不设置 URL、Storage、dataset 或 innerHTML。
 */
export function hydratePromptTemplateSensitiveText(root, state) {
  const scope = root?.querySelectorAll ? root : null;
  if (!scope) return false;
  clearPromptTemplateSensitiveDom(scope);
  const definition = state?.definition;
  if (definition) {
    const content = scope.querySelector("[data-prompt-template-definition-content]");
    const summary = scope.querySelector("[data-prompt-template-definition-summary]");
    const variables = scope.querySelector("[data-prompt-template-definition-variables]");
    if (content) content.textContent = String(definition.content ?? "");
    if (summary) summary.textContent = String(definition.change_summary ?? "");
    if (variables) appendDefinitionVariables(variables, definition.variables);
  }
  const dialog = state?.dialog;
  if (!dialog) return true;
  for (const [name, value] of Object.entries(dialog.draft ?? {})) {
    // 数组草稿需要由专用 DOM API 分支创建；这里绝不能把变量或绑定序列字符串化进普通字段。
    if (["variables", "project_ids", "recommended_model_alias_ids"].includes(name)) continue;
    const field = scope.querySelector(`[data-prompt-template-dialog] [name='${fieldSelector(name)}']`);
    if (field && "value" in field) field.value = String(value ?? "");
  }
  const variableRoot = scope.querySelector("[data-prompt-template-variable-editor]");
  if (variableRoot) appendVariableEditors(variableRoot, dialog.draft?.variables);
  for (const checkbox of scope.querySelectorAll("[data-prompt-template-project-id]")) {
    checkbox.checked = dialog.draft?.project_ids?.includes(checkbox.value) === true;
    checkbox.disabled = dialog.draft?.project_scope !== "selected";
  }
  for (const checkbox of scope.querySelectorAll("[data-prompt-template-model-id]")) {
    checkbox.checked = dialog.draft?.recommended_model_alias_ids?.includes(checkbox.value) === true;
  }
  const recommendedIds = dialog.draft?.recommended_model_alias_ids ?? [];
  for (const orderLabel of scope.querySelectorAll("[data-prompt-template-model-order]")) {
    const index = recommendedIds.indexOf(orderLabel.getAttribute("data-prompt-template-model-order"));
    orderLabel.textContent = index >= 0 ? `推荐顺序 ${index + 1}` : "";
  }
  for (const button of scope.querySelectorAll("[data-prompt-template-model-move]")) {
    const modelId = button.getAttribute("data-prompt-template-model-move");
    const index = recommendedIds.indexOf(modelId);
    const direction = button.getAttribute("data-direction");
    button.disabled = index < 0
      || (direction === "up" ? index === 0 : index === recommendedIds.length - 1);
  }
  return true;
}

/** 同步清空正文、变量行和表单 value；必须在 abort/离页/注销之前调用。 */
export function clearPromptTemplateSensitiveDom(root) {
  const scope = root?.querySelectorAll ? root : null;
  if (!scope) return false;
  scope.querySelector("[data-prompt-template-definition-content]")?.replaceChildren();
  scope.querySelector("[data-prompt-template-definition-summary]")?.replaceChildren();
  scope.querySelector("[data-prompt-template-definition-variables]")?.replaceChildren();
  scope.querySelector("[data-prompt-template-variable-editor]")?.replaceChildren();
  for (const field of scope.querySelectorAll(
    "[data-prompt-template-dialog] input[type='text'], [data-prompt-template-dialog] textarea",
  )) {
    field.value = "";
  }
  return true;
}

/** 渲染状态分段按钮；筛选值是固定枚举，不承担真实后端授权。 */
function renderStatusSegment(value, label, selected) {
  const active = String(selected ?? "") === value;
  return `<button type="button" data-prompt-template-status="${escapeAttribute(value)}"
    aria-pressed="${active ? "true" : "false"}" class="${active ? "is-active" : ""}">${label}</button>`;
}

/** 渲染紧凑列表；列表安全 DTO 不包含正文和完整变量定义。 */
function renderTemplateList(templates, selectedTemplateId) {
  if (!templates.length) {
    return `<div class="prompt-template-empty"><strong>暂无模板</strong></div>`;
  }
  return templates.map((item) => `
    <button class="prompt-template-list-row ${item.prompt_template_id === selectedTemplateId ? "is-selected" : ""}"
      type="button" data-prompt-template-select="${escapeAttribute(item.prompt_template_id)}"
      aria-current="${item.prompt_template_id === selectedTemplateId ? "true" : "false"}">
      <span class="prompt-template-list-name">${escapeHtml(item.name)}</span>
      <span class="prompt-template-status" data-status="${item.status === "active" ? "active" : "disabled"}">
        ${item.status === "active" ? "启用" : "停用"}
      </span>
      <span>v${escapeHtml(item.current_version_number || "-")}</span>
      <span>${escapeHtml(item.project_count ?? 0)} 项目</span>
      <span>${escapeHtml(item.recommended_model_count ?? 0)} 推荐模型</span>
      <time>${escapeHtml(formatTime(item.updated_at))}</time>
    </button>
  `).join("");
}

/** 渲染模板详情元数据和版本导航；definition 容器始终为空，正文由 hydration 注入。 */
function renderDetail(state, canWrite) {
  const detail = state?.detail;
  if (!detail) {
    return `<div class="prompt-template-empty"><strong>${state?.selectedTemplateId ? "正在读取模板" : "选择一个模板"}</strong></div>`;
  }
  const currentVersion = detail.versions?.find(
    (item) => item.prompt_template_version_id === detail.current_version_id,
  ) ?? null;
  return `
    <div class="prompt-template-detail-head">
      <button class="prompt-template-back" type="button" data-prompt-template-mobile-back
        title="返回列表" aria-label="返回列表">←</button>
      <div>
        <h3>${escapeHtml(detail.name)}</h3>
        <p>${escapeHtml(detail.description || "暂无描述")}</p>
      </div>
      <span class="prompt-template-status" data-status="${detail.status === "active" ? "active" : "disabled"}">
        ${detail.status === "active" ? "启用" : "停用"}
      </span>
      ${canWrite ? `
        <div class="prompt-template-detail-actions">
          <button type="button" data-prompt-template-edit-metadata>编辑元数据</button>
          <button type="button" data-prompt-template-publish>发布新版本</button>
        </div>
      ` : ""}
    </div>
    <dl class="prompt-template-metadata">
      <div><dt>当前版本</dt><dd>v${escapeHtml(detail.current_version_number)}</dd></div>
      <div><dt>metadata revision</dt><dd>${escapeHtml(detail.metadata_revision)}</dd></div>
      <div><dt>适用项目</dt><dd>${escapeHtml(detail.project_count ?? 0)}</dd></div>
      <div><dt>推荐模型</dt><dd>${escapeHtml(detail.recommended_model_count ?? 0)}</dd></div>
      <div><dt>更新时间</dt><dd>${escapeHtml(formatTime(detail.updated_at))}</dd></div>
    </dl>
    <div class="prompt-template-tabs" role="tablist" aria-label="模板详情">
      <button type="button" role="tab" data-prompt-template-tab="current"
        aria-selected="${state.detailTab !== "history" ? "true" : "false"}">当前版本</button>
      <button type="button" role="tab" data-prompt-template-tab="history"
        aria-selected="${state.detailTab === "history" ? "true" : "false"}">版本历史</button>
    </div>
    ${state.detailTab === "history"
      ? renderVersionHistory(detail.versions, state.definition)
      : renderCurrentVersion(currentVersion, state.definition)}
  `;
}

/** 渲染当前版本只读框架；正文、变量 schema 和 change summary 均保留为空 DOM。 */
function renderCurrentVersion(version, definition) {
  if (!version) return `<div class="prompt-template-empty"><strong>当前版本不可用</strong></div>`;
  const loaded = definition?.prompt_template_version_id === version.prompt_template_version_id;
  return `
    <div class="prompt-template-version-toolbar">
      <span>v${escapeHtml(version.version_number)}</span>
      <button type="button" data-prompt-template-load-definition="${escapeAttribute(version.prompt_template_version_id)}">
        ${loaded ? "重新读取定义" : "查看定义"}
      </button>
    </div>
    <section class="prompt-template-definition" aria-label="当前版本定义">
      <h4>Prompt 正文</h4>
      <pre data-prompt-template-definition-content></pre>
      <h4>变量</h4>
      <div class="prompt-template-definition-variables" data-prompt-template-definition-variables></div>
      <h4>变更说明</h4>
      <p data-prompt-template-definition-summary></p>
    </section>
  `;
}

/** 渲染不可变版本历史；历史版本只提供精确 GET，不提供删除或编辑命令。 */
function renderVersionHistory(versions, definition) {
  if (!Array.isArray(versions) || !versions.length) {
    return `<div class="prompt-template-empty"><strong>暂无版本</strong></div>`;
  }
  return `<ol class="prompt-template-history">
    ${versions.map((item) => `
      <li>
        <div><strong>v${escapeHtml(item.version_number)}</strong><time>${escapeHtml(formatTime(item.published_at))}</time></div>
        <span>${escapeHtml(item.variable_count ?? 0)} 个变量</span>
        <span>${item.project_scope === "selected" ? `${escapeHtml(item.project_count ?? 0)} 个项目` : "全部项目"}</span>
        <button type="button" data-prompt-template-load-definition="${escapeAttribute(item.prompt_template_version_id)}">
          ${definition?.prompt_template_version_id === item.prompt_template_version_id ? "重新读取" : "查看"}
        </button>
      </li>
    `).join("")}
  </ol>`;
}

/** 渲染 modal 框架；所有敏感表单 value 和变量行均由 hydration 后置创建。 */
function renderDialog(dialog, options, canWrite) {
  if (!dialog || !canWrite) return "";
  const title = ({ create: "新建模板", metadata: "编辑模板", publish: "发布新版本" })[dialog.mode] ?? "模板";
  return `
    <dialog class="prompt-template-dialog" data-prompt-template-dialog open aria-labelledby="prompt-template-dialog-title">
      <form method="dialog" data-prompt-template-dialog-form autocomplete="off">
        <header>
          <h3 id="prompt-template-dialog-title">${title}</h3>
          <button class="prompt-template-icon-button" type="button" data-prompt-template-dialog-close
            title="关闭" aria-label="关闭" ${dialog.busy ? "disabled" : ""}>×</button>
        </header>
        ${renderError(dialog.actionError)}
        ${dialog.conflicted ? renderConflict(dialog.sideLatest) : ""}
        <fieldset class="prompt-template-dialog-fields" ${dialog.busy ? "disabled" : ""}>
          ${dialog.mode === "metadata" ? renderMetadataFields(dialog) : renderDefinitionFields(dialog, options)}
        </fieldset>
        <footer>
          <span role="status" aria-live="polite">${escapeHtml(dialog.feedback ?? "")}</span>
          <button type="button" data-prompt-template-dialog-close ${dialog.busy ? "disabled" : ""}>取消</button>
          <button class="prompt-template-command" type="submit"
            ${!dialog.dirty || dialog.busy || dialog.conflicted ? "disabled" : ""}>保存</button>
        </footer>
      </form>
    </dialog>
  `;
}

/** 渲染元数据字段空 value 框架；当前 name/description/status 由 hydration 设置。 */
function renderMetadataFields(dialog) {
  return `
    <div class="prompt-template-form-grid">
      <label><span>名称</span><input name="name" type="text" maxlength="120" required></label>
      <label class="prompt-template-wide"><span>描述</span><textarea name="description" maxlength="1000" rows="3"></textarea></label>
      <label><span>状态</span><select name="status">
        <option value="active">启用</option><option value="disabled">停用</option>
      </select></label>
      <span>metadata revision ${escapeHtml(dialog.baseMetadataRevision)}</span>
    </div>
  `;
}

/** 渲染创建/发布定义框架；正文、变量行与 change summary 均保持空值。 */
function renderDefinitionFields(dialog, options) {
  return `
    <div class="prompt-template-form-grid">
      ${dialog.mode === "create" ? `
        <label><span>名称</span><input name="name" type="text" maxlength="120" required></label>
        <label class="prompt-template-wide"><span>描述</span><textarea name="description" maxlength="1000" rows="2"></textarea></label>
      ` : `<span>metadata revision ${escapeHtml(dialog.baseMetadataRevision)}</span>`}
      <label class="prompt-template-wide"><span>Prompt 正文</span>
        <textarea name="content" maxlength="32768" rows="8" required spellcheck="true"></textarea>
      </label>
      <section class="prompt-template-variable-section">
        <div><h4>变量</h4><button type="button" data-prompt-template-add-variable>添加变量</button></div>
        <div data-prompt-template-variable-editor></div>
      </section>
      <label><span>适用范围</span><select name="project_scope">
        <option value="all">全部项目</option><option value="selected">指定项目</option>
      </select></label>
      <fieldset class="prompt-template-binding-list">
        <legend>适用项目</legend>${renderProjectBindings(options?.projects)}
      </fieldset>
      <fieldset class="prompt-template-binding-list">
        <legend>推荐模型</legend>${renderModelBindings(options?.model_aliases)}
      </fieldset>
      <label class="prompt-template-wide"><span>变更说明</span>
        <textarea name="change_summary" maxlength="300" rows="3" required></textarea>
      </label>
    </div>
  `;
}

/** 渲染项目候选安全目录；checked 状态由 hydration 设置，不从草稿插值。 */
function renderProjectBindings(projects) {
  if (!Array.isArray(projects) || !projects.length) return `<span>暂无可用项目</span>`;
  return projects.map((item) => `<label>
    <input type="checkbox" value="${escapeAttribute(item.project_id)}" data-prompt-template-project-id>
    <span>${escapeHtml(item.name)}</span>
  </label>`).join("");
}

/** 渲染模型候选安全目录；推荐顺序由当前表单数组维护，后端不会把它当路由白名单。 */
function renderModelBindings(modelAliases) {
  if (!Array.isArray(modelAliases) || !modelAliases.length) return `<span>暂无可用模型</span>`;
  return modelAliases.map((item) => `<div class="prompt-template-model-binding">
    <label>
      <input type="checkbox" value="${escapeAttribute(item.model_alias_id)}" data-prompt-template-model-id>
      <span>${escapeHtml(item.display_name || item.alias)}</span>
    </label>
    <span data-prompt-template-model-order="${escapeAttribute(item.model_alias_id)}"></span>
    <div>
      <button type="button" data-prompt-template-model-move="${escapeAttribute(item.model_alias_id)}"
        data-direction="up" title="上移推荐顺序" aria-label="上移推荐顺序">↑</button>
      <button type="button" data-prompt-template-model-move="${escapeAttribute(item.model_alias_id)}"
        data-direction="down" title="下移推荐顺序" aria-label="下移推荐顺序">↓</button>
    </div>
  </div>`).join("");
}

/** 渲染 409 冲突提示；最新服务端值只显示 revision，不回显正文或用户草稿。 */
function renderConflict(latest) {
  return `<div class="prompt-template-conflict" role="alert">
    <strong>模板已被其他操作更新</strong>
    <span>${latest ? `服务器 revision ${escapeHtml(latest.metadata_revision)}` : "正在读取服务器最新值"}</span>
    ${latest ? `<button type="button" data-prompt-template-adopt-conflict>采纳最新基线</button>` : ""}
  </div>`;
}

/** 使用 DOM API 创建已发布变量只读列表；变量 label/name 不进入 renderer 字符串。 */
function appendDefinitionVariables(container, values) {
  const variables = Array.isArray(values) ? values : [];
  if (!variables.length) {
    const empty = document.createElement("span");
    empty.textContent = "无变量";
    container.append(empty);
    return;
  }
  const list = document.createElement("dl");
  for (const item of variables) {
    const wrapper = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = String(item.label ?? "");
    description.textContent = `${String(item.name ?? "")} · ${item.required ? "必填" : "选填"} · ${Number(item.max_chars ?? 0)} 字`;
    wrapper.append(term, description);
    list.append(wrapper);
  }
  container.append(list);
}

/** 使用 DOM API 创建变量编辑行；所有草稿文本只写 input.value，不进入 attribute 或 dataset。 */
function appendVariableEditors(container, values) {
  const variables = Array.isArray(values) ? values : [];
  for (const [index, item] of variables.entries()) {
    const row = document.createElement("div");
    row.className = "prompt-template-variable-row";
    appendVariableInput(row, "变量名", "name", item.name, index, "text");
    appendVariableInput(row, "标签", "label", item.label, index, "text");
    appendVariableInput(row, "最大字符", "max_chars", item.max_chars, index, "number");
    const requiredLabel = document.createElement("label");
    const required = document.createElement("input");
    required.type = "checkbox";
    required.checked = item.required === true;
    required.dataset.promptTemplateVariableField = "required";
    required.dataset.promptTemplateVariableIndex = String(index);
    const requiredText = document.createElement("span");
    requiredText.textContent = "必填";
    requiredLabel.append(required, requiredText);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.title = "删除变量";
    remove.setAttribute("aria-label", "删除变量");
    remove.dataset.promptTemplateVariableRemove = String(index);
    remove.textContent = "×";
    row.append(requiredLabel, remove);
    container.append(row);
  }
}

/** 创建单个变量输入；dataset 只保存固定字段名和数字索引，绝不保存变量草稿。 */
function appendVariableInput(row, labelText, fieldName, value, index, type) {
  const label = document.createElement("label");
  const caption = document.createElement("span");
  const input = document.createElement("input");
  caption.textContent = labelText;
  input.type = type;
  input.value = String(value ?? "");
  input.dataset.promptTemplateVariableField = fieldName;
  input.dataset.promptTemplateVariableIndex = String(index);
  if (type === "number") {
    input.min = "1";
    input.max = "8000";
    input.step = "1";
  }
  label.append(caption, input);
  row.append(label);
}

/** 统一渲染公开错误字段；正文、变量值和原始 exception 不参与错误 DOM。 */
function renderError(error) {
  if (!error) return "";
  return `<div class="prompt-template-error" role="alert">
    <strong>${escapeHtml(error.code ?? "prompt_template.unavailable")}</strong>
    <span>${escapeHtml(error.message ?? "Prompt 模板暂时不可用")}</span>
    <code>request_id: ${escapeHtml(error.requestId ?? "-")}</code>
  </div>`;
}

/** 时间只展示有界公开字符串；无效值显示短横线，不做时区猜测。 */
function formatTime(value) {
  const normalized = String(value ?? "").trim();
  if (!normalized) return "-";
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? normalized.slice(0, 24) : date.toLocaleString("zh-CN", { hour12: false });
}

/** 构造 querySelector 的固定字段名；调用点只传内部白名单键。 */
function fieldSelector(value) {
  return String(value ?? "").replaceAll("'", "");
}

/** 转义安全元数据；解密正文和变量草稿禁止调用此函数后拼入 HTML。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 属性值只用于公开资源 ID、固定枚举和安全筛选文本。 */
function escapeAttribute(value) {
  return escapeHtml(value);
}
