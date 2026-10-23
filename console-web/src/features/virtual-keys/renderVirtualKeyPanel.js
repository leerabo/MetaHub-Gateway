/**
 * 模块职责：渲染 Virtual Key 管理面板，并提供 Project/Application 归属绑定选择。
 * 维护边界：前端只展示脱敏 Key 元数据和一次性创建明文；Key 明文、哈希、租户权限与归属状态必须由后端控制。
 * 来源文件：console-web/src/features/virtual-keys/renderVirtualKeyPanel.js
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { icon } from "../public-home/icons.js";

// 环境标签只用于展示和表单选项，提交值仍使用后端枚举。
const ENVIRONMENT_LABELS = {
  dev: "开发",
  test: "测试",
  prod: "生产",
};

/**
 * 渲染 Virtual Key 管理面板。
 * 只有 admin/developer 显示该区域；后端仍会校验写权限和项目/应用归属，前端隐藏按钮不是安全边界。
 */
export function renderVirtualKeyPanel(virtualKeysState, authContext, projectState, environmentState, workspaceTaskHomeState, i18n) {
  if (!hasPermission(authContext, PERMISSIONS.VIRTUAL_KEY_READ)) {
    return "";
  }
  // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
  if (!virtualKeysState) {
    return `
      <div class="panel-heading">
        <h2 id="virtual-keys-title">个人 KEY</h2>
        <p>正在加载个人 KEY...</p>
      </div>
      <div class="empty-state" role="status">正在加载...</div>
    `;
  }
  // 错误态只展示恢复提示，定位信息进入后台日志。
  if (virtualKeysState.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="virtual-keys-title">个人 KEY</h2>
        <p>个人 KEY 暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(virtualKeysState.message ?? "个人 KEY 暂不可用，请稍后重试")}
        <button type="button" data-resource-retry>重试</button>
      </div>
    `;
  }

  const keys = virtualKeysState.virtual_keys ?? [];
  const canWrite = hasPermission(authContext, PERMISSIONS.VIRTUAL_KEY_WRITE);
  const canManageWorkspace = hasPermission(authContext, PERMISSIONS.USER_MANAGE);
  const canManageKeys = canManageWorkspace && canWrite;
  const personalOnly = !canManageWorkspace;
  const projects = projectState?.state === "ok" ? projectState.projects ?? [] : [];
  const applications = projectState?.state === "ok" ? projectState.applications ?? [] : [];
  const keyTitle = keyCopy(i18n, "我的 KEY", "My keys");
  const createKeyLabel = keyCopy(i18n, "创建 KEY", "Create key");
  const defaultEnvironment = activeEnvironments(environmentState)[0];
  const personalKeyExists = personalOnly
    ? keys.length > 0
    : keys.some((key) => key.owner_user_id === authContext?.user?.user_id);
  return `
    <div class="panel-heading virtual-keys-heading">
      <div>
        <h2 id="virtual-keys-title">${keyTitle}</h2>
        <p class="virtual-key-admin-hint">${canManageWorkspace ? "员工创建自己的 KEY；管理员分配应用后，KEY 才能用于调用。" : "KEY 需管理员分配应用后才能使用；未分配时创建的 KEY 保持禁用。"}</p>
      </div>
      <span class="virtual-keys-count">${keys.length} 把 KEY</span>
      ${canWrite ? `<button type="button" data-open-virtual-key-dialog${personalKeyExists ? ` disabled title="${keyCopy(i18n, "请先删除自己的现有 KEY，再创建新的", "Delete your current key before creating a new one")}"` : ""}>${createKeyLabel}</button>` : ""}
    </div>
    ${!virtualKeysState.keyCreateOpen ? renderActionError(virtualKeysState.actionError) : ""}
    ${canWrite ? renderVirtualKeyDialog(virtualKeysState, defaultEnvironment, i18n) : ""}
    ${`<div class="virtual-keys-grid" id="issued-keys" role="table" aria-label="已创建的 KEY">
      <div class="virtual-key-row virtual-key-head" role="row">
        <span role="columnheader">${keyCopy(i18n, "名称", "Name")}</span>
        <span role="columnheader">KEY</span>
        <span role="columnheader">${keyCopy(i18n, "环境", "Environment")}</span>
        <span role="columnheader">${keyCopy(i18n, "使用者", "User")}</span>
        <span role="columnheader">${keyCopy(i18n, "项目", "Project")}</span>
        <span role="columnheader">${keyCopy(i18n, "应用", "Application")}</span>
        <span role="columnheader">${keyCopy(i18n, "状态", "Status")}</span>
        <span role="columnheader">${keyCopy(i18n, "最近使用", "Last used")}</span>
        <span role="columnheader">${keyCopy(i18n, "限制", "Limits")}</span>
        <span role="columnheader">${keyCopy(i18n, "操作", "Actions")}</span>
      </div>
      ${keys.length ? keys.map((key) => renderVirtualKeyRow(key, canManageKeys, personalOnly, i18n, projects, applications)).join("") : renderEmptyRow(i18n)}
    </div>`}
  `;
}

function renderVirtualKeyDialog(state, defaultEnvironment, i18n) {
  const createKeyLabel = keyCopy(i18n, "创建 KEY", "Create key");
  return `
    <dialog class="virtual-key-dialog" data-virtual-key-dialog${state.keyCreateOpen ? " data-virtual-key-auto-open" : ""} aria-labelledby="virtual-key-dialog-title">
      <div class="virtual-key-dialog-content">
        <div class="virtual-key-dialog-heading">
          <h3 id="virtual-key-dialog-title">${state.created?.plainValue ? keyCopy(i18n, "KEY 已创建", "Key created") : createKeyLabel}</h3>
          <button type="button" class="icon-button" data-virtual-key-dialog-close aria-label="关闭">×</button>
        </div>
        ${renderActionError(state.actionError)}
        ${state.created?.plainValue ? renderOneTimeSecret(state.created, false, i18n) : `
          <form class="create-virtual-key-form" id="create-virtual-key-form">
            <p>创建后须由管理员分配应用，KEY 才能用于调用。</p>
            <label><span>${keyCopy(i18n, "名称（可选）", "Name (optional)")}</span>
              <input name="name" maxlength="80" value="" placeholder="${keyCopy(i18n, "例如：开发用 KEY", "Example: development key")}" />
            </label>
            ${defaultEnvironment?.environment_id ? `<input type="hidden" name="environmentId" value="${escapeAttribute(defaultEnvironment.environment_id)}" />` : ""}
            <div class="virtual-key-actions">
              <button type="submit">${createKeyLabel}</button>
              <span class="virtual-key-feedback" role="status">${escapeHtml(state.feedback ?? "")}</span>
            </div>
          </form>
        `}
      </div>
    </dialog>
  `;
}

/**
 * 渲染创建成功后的一次性明文 Key。
 * 明文只来自刚创建的响应，刷新后不会再出现；页面文案提醒用户立即保存。
 */
function renderOneTimeSecret(created, organizationKey = false, i18n = null) {
  if (!created?.plainValue) {
    return "";
  }
  const systemKey = Boolean(created.applicationName || created.projectName);
  const owner = created.applicationName || created.projectName || (organizationKey ? "当前组织" : "本人");
  return `
    <div class="one-time-secret" role="status" aria-label="一次性 Virtual Key 明文">
      <div>
        <strong>${created.operation === "rotated" ? keyCopy(i18n, "KEY 已轮换", "Key rotated") : keyCopy(i18n, "KEY 已创建", "Key created")} · ${keyCopy(i18n, "只显示一次", "shown once")}</strong>
        <p class="one-time-secret-owner">${systemKey ? keyCopy(i18n, "绑定对象", "Bound to") : keyCopy(i18n, "KEY 归属", "Key owner")}：${escapeHtml(owner)}${created.keyName ? ` · ${escapeHtml(created.keyName)}` : ""}</p>
        <p>${keyCopy(i18n, "仅显示一次，请复制保存。管理员分配应用后才能使用。", "Shown once. Copy and save it now. An administrator must assign an application before use.")}</p>
      </div>
      <code>${escapeHtml(created.plainValue)}</code>
      <div class="virtual-key-actions">
        <button type="button" class="icon-button virtual-key-copy-button" data-copy-virtual-key title="${keyCopy(i18n, "复制 KEY", "Copy key")}" aria-label="${keyCopy(i18n, "复制 KEY", "Copy key")}">${icon("copy")}</button>
        <button type="button" data-dismiss-virtual-key-secret>${keyCopy(i18n, "隐藏", "Hide")}</button>
        <span class="virtual-key-feedback" role="status">${escapeHtml(created.feedback ?? "")}</span>
      </div>
    </div>
  `;
}

/**
 * 渲染 Virtual Key 操作错误；定位信息保留在后台日志。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="virtual-key-action-error" role="alert">
      ${escapeHtml(error.message ?? "Virtual Key 操作失败，请稍后重试")}
    </div>
  `;
}

/**
 * 渲染单条 Virtual Key。
 * 列表只展示 key_prefix、状态、环境、归属和最近使用时间，不展示明文 Key 或哈希。
 */
function renderVirtualKeyRow(key, canManageKeys, personalOnly, i18n = null, projects = [], applications = []) {
  const unassigned = key.status === "disabled" && (!key.project_id || !key.application_id) && key.disabled_reason === "manual" && !key.disabled_at;
  const awaitingActivation = key.status === "disabled" && key.project_id && key.application_id && key.disabled_reason === "manual" && !key.disabled_at;
  const statusLabel = key.status === "active"
    ? keyCopy(i18n, "已启用", "Active")
    : key.disabled_reason === "project_inactive"
      ? keyCopy(i18n, "已暂停", "Paused")
      : key.disabled_reason === "member_removed"
        ? keyCopy(i18n, "成员已移除", "Member removed")
        : unassigned
          ? keyCopy(i18n, "待分配", "Unassigned")
        : awaitingActivation
          ? keyCopy(i18n, "待启用", "Pending activation")
        : keyCopy(i18n, "已停用", "Disabled");
  return `
    <div class="virtual-key-row" data-virtual-key-row="${escapeAttribute(key.virtual_key_id)}" role="row">
      <span class="virtual-key-name" role="cell"><span class="virtual-key-field-label">${keyCopy(i18n, "KEY 名称", "Key name")}</span>
        <strong>${escapeHtml(displayVirtualKeyName(key))}</strong>
        ${key.description ? `<em>${escapeHtml(key.description)}</em>` : ""}
      </span>
      <span class="virtual-key-credential" role="cell"><span class="virtual-key-field-label">KEY</span>
        <span class="virtual-key-prefix-line"><code>${escapeHtml(displayVirtualKeyPrefix(key.key_prefix))}</code>
          ${key.disabled_reason !== "member_removed" && key.has_recoverable_secret ? `<button type="button" class="icon-button virtual-key-copy-saved" data-copy-saved-virtual-key="${escapeAttribute(key.virtual_key_id)}" title="复制 KEY" aria-label="复制 KEY">${icon("copy")}</button>` : ""}
          ${key.status === "active" && !key.has_recoverable_secret ? `<button type="button" class="icon-button virtual-key-copy-saved virtual-key-copy-unavailable" disabled title="${keyCopy(i18n, "此 KEY 无法恢复，请删除后重新创建", "This key cannot be recovered; delete it and create a new one")}" aria-label="${keyCopy(i18n, "复制 KEY 不可用", "Copy unavailable")}">${icon("copy")}</button>` : ""}
        </span>
        <span class="virtual-key-copy-feedback" data-virtual-key-copy-feedback role="status"></span>
        ${key.status === "active" && !key.has_recoverable_secret ? `<em class="virtual-key-copy-unavailable-label">${keyCopy(i18n, "需重新创建", "Recreate")}</em>` : ""}
      </span>
      <span class="virtual-key-environment" role="cell"><span class="virtual-key-field-label">${keyCopy(i18n, "环境", "Environment")}</span>${escapeHtml(key.environment_name ?? environmentLabel(key.environment, i18n) ?? key.environment)}</span>
      <span class="virtual-key-owner" role="cell"><span class="virtual-key-field-label">${keyCopy(i18n, "使用者", "User")}</span>
        ${escapeHtml(key.owner_display_name || (personalOnly ? keyCopy(i18n, "本人", "Me") : keyCopy(i18n, "未提供姓名", "Name unavailable")))}
        ${key.consumer_id ? `<em>${keyCopy(i18n, "历史成员关联", "Former member link")} · ${key.consumer_status === "active" ? keyCopy(i18n, "使用者已启用", "Consumer active") : keyCopy(i18n, "使用者已停用", "Consumer disabled")}</em>` : ""}
      </span>
      <span class="virtual-key-project" role="cell"><span class="virtual-key-field-label">${keyCopy(i18n, "项目", "Project")}</span>
        ${key.project_id ? escapeHtml(key.project_name ?? keyCopy(i18n, "已关联项目", "Linked project")) : keyCopy(i18n, "未分配", "Unassigned")}
      </span>
      <span class="virtual-key-application" role="cell"><span class="virtual-key-field-label">${keyCopy(i18n, "应用", "Application")}</span>
        ${key.application_id ? escapeHtml(key.application_name ?? keyCopy(i18n, "已关联应用", "Linked application")) : keyCopy(i18n, "待分配应用", "Application pending")}
      </span>
      <span role="cell"><span class="virtual-key-field-label">${keyCopy(i18n, "状态", "Status")}</span><mark class="status-mark status-${escapeHtml(key.status)}">${escapeHtml(statusLabel)}</mark>${unassigned ? `<em class="virtual-key-governed-state">${keyCopy(i18n, "管理员分配应用后启用", "Enabled after administrator assigns an application")}</em>` : awaitingActivation ? `<em class="virtual-key-governed-state">${keyCopy(i18n, "请重新保存归属", "Save the assignment again")}</em>` : key.status === "disabled" && key.disabled_reason === "project_inactive" ? `<em class="virtual-key-governed-state">${keyCopy(i18n, "项目或应用已暂停", "Project or application paused")}</em>` : key.status === "disabled" && key.disabled_reason === "manual" ? `<em class="virtual-key-governed-state">${keyCopy(i18n, "无法调用", "Cannot be used")}</em>` : ""}</span>
      <span class="virtual-key-last-used" role="cell"><span class="virtual-key-field-label">${keyCopy(i18n, "最近使用", "Last used")}</span>${key.last_used_at ? escapeHtml(formatDateTime(key.last_used_at)) : keyCopy(i18n, "尚未使用", "Not used yet")}</span>
      <div class="virtual-key-limits-cell" role="cell">
        ${renderLimitSummary(key.rate_limit_rule, i18n)}
      </div>
      <span class="virtual-key-row-actions" role="cell">
        ${canManageKeys ? `<button type="button" data-open-virtual-key-binding-dialog="${escapeAttribute(key.virtual_key_id)}">${keyCopy(i18n, "编辑", "Edit")}</button>` : ""}
        ${canManageKeys && Number.isSafeInteger(key.credential_revision) && key.status === "active" ? `<button type="button" data-rotate-virtual-key="${escapeAttribute(key.virtual_key_id)}" data-key-revision="${key.credential_revision}" title="${keyCopy(i18n, "轮换 KEY，旧 KEY 将立即失效", "Rotate key; the old key stops working immediately")}" aria-label="${keyCopy(i18n, "轮换", "Rotate")} ${escapeAttribute(key.name)}">${icon("rotate-cw")}</button>` : ""}
        ${canManageKeys || personalOnly ? `<form data-virtual-key-form data-personal-key-delete="true" data-virtual-key-id="${escapeAttribute(key.virtual_key_id)}">
          <input type="hidden" name="status" value="disabled" />
          <button type="submit" class="danger-button">删除</button>
        </form>` : ""}
      </span>
    </div>
    ${canManageKeys ? renderBindingDialog(key, projects, applications, i18n) : ""}
  `;
}

function renderBindingDialog(key, projects, applications, i18n = null) {
  const dialogId = `virtual-key-binding-dialog-${String(key.virtual_key_id).replace(/[^a-zA-Z0-9_-]/gu, "-")}`;
  const activeProjects = projects.filter((project) => project.status === "active");
  const projectOptions = [`<option value="">未绑定项目</option>`, ...activeProjects.map((project) =>
    `<option value="${escapeAttribute(project.project_id)}"${project.project_id === key.project_id ? " selected" : ""}>${escapeHtml(project.name)}</option>`)].join("");
  const applicationOptions = ["<option value=\"\">未绑定应用</option>", ...applications.filter((application) => application.status === "active").map((application) =>
    `<option value="${escapeAttribute(application.application_id)}" data-project-id="${escapeAttribute(application.project_id)}"${application.application_id === key.application_id ? " selected" : ""}>${escapeHtml(application.name)}</option>`)].join("");
  return `
    <dialog class="virtual-key-binding-dialog" id="${escapeAttribute(dialogId)}" data-virtual-key-binding-dialog="${escapeAttribute(key.virtual_key_id)}" aria-labelledby="${escapeAttribute(dialogId)}-title">
      <form class="virtual-key-binding-dialog-content" method="dialog" data-virtual-key-binding-form data-virtual-key-id="${escapeAttribute(key.virtual_key_id)}">
        <div class="virtual-key-dialog-heading">
          <h3 id="${escapeAttribute(dialogId)}-title">${keyCopy(i18n, "编辑 KEY", "Edit key")}</h3>
          <button type="button" class="icon-button" data-virtual-key-binding-dialog-close aria-label="关闭">×</button>
        </div>
        <p class="virtual-key-limit-dialog-key">${escapeHtml(displayVirtualKeyName(key))}${key.owner_display_name ? ` · ${escapeHtml(key.owner_display_name)}` : ""}</p>
        <label><span>名称</span><input name="name" maxlength="80" required value="${escapeAttribute(displayVirtualKeyName(key))}" /></label>
        <fieldset class="virtual-key-edit-section">
          <legend>项目与应用</legend>
          <label><span>项目</span><select name="projectId" data-virtual-key-binding-project data-current-project="${escapeAttribute(key.project_id ?? "")}">${projectOptions}</select></label>
          <label><span>应用</span><select name="applicationId" data-virtual-key-binding-application data-current-application="${escapeAttribute(key.application_id ?? "")}">${applicationOptions}</select></label>
          <p data-key-project-loading role="status"></p>
        </fieldset>
        <fieldset class="virtual-key-edit-section">
          <legend>调用限制</legend>
          <div class="virtual-key-limit-fields">
            ${renderLimitInput("rpmLimit", "每分钟请求数", key.rate_limit_rule?.rpm_limit, i18n)}
            ${renderLimitInput("tpmLimit", "每分钟 Token 数", key.rate_limit_rule?.tpm_limit, i18n)}
            ${renderLimitInput("concurrencyLimit", "同时请求数", key.rate_limit_rule?.concurrency_limit, i18n)}
          </div>
        </fieldset>
        <div class="virtual-key-actions">
          <button type="submit">保存</button>
          <button type="button" class="secondary-button" data-virtual-key-binding-dialog-close>取消</button>
          <span class="virtual-key-feedback" role="status"></span>
        </div>
      </form>
    </dialog>
  `;
}

/**
 * 渲染 Virtual Key 当前限制摘要。
 * 空规则或空字段统一展示“不限制”，避免用户把 0 误解成无限制。
 */
function renderLimitSummary(rule, i18n = null) {
  if ([rule?.rpm_limit, rule?.tpm_limit, rule?.concurrency_limit].every((value) => value === null || value === undefined)) {
    return `<p class="virtual-key-limit-summary">${keyCopy(i18n, "不限速", "No limits")}</p>`;
  }
  return `
    <p class="virtual-key-limit-summary">
      RPM ${escapeHtml(limitText(rule?.rpm_limit, i18n))} ·
      TPM ${escapeHtml(limitText(rule?.tpm_limit, i18n))} ·
      ${keyCopy(i18n, "并发", "Concurrency")} ${escapeHtml(limitText(rule?.concurrency_limit, i18n))}
    </p>
  `;
}

/**
 * 渲染单个限制输入框。
 * 输入框允许留空，提交后由 API client 转成 null，表示清除该限制。
 */
function renderLimitInput(name, label, value, i18n = null) {
  return `
    <label>
      <span>${escapeHtml(label)}</span>
      <input name="${escapeAttribute(name)}" type="number" min="1" inputmode="numeric" value="${escapeAttribute(value ?? "")}" placeholder="${keyCopy(i18n, "不限", "No limit")}" />
    </label>
  `;
}

/**
 * 把限制数值转换成列表摘要文本。
 */
function limitText(value, i18n = null) {
  if (value === null || value === undefined || value === "") {
    return keyCopy(i18n, "不限制", "No limit");
  }
  return String(value);
}

/**
 * 渲染 Virtual Key 空列表行。
 * 使用表格行结构保持列表布局稳定。
 */
function renderEmptyRow(i18n = null) {
  return `
    <div class="virtual-key-row virtual-key-empty" role="row">
      <span>${keyCopy(i18n, "尚未创建 KEY。", "No keys created yet.")}</span>
    </div>
  `;
}

/**
 * Hide the local seed identity from organization members. The seed is an
 * implementation detail used to make a fresh local workspace usable; it is
 * not a meaningful name for a customer-managed key.
 */
function displayVirtualKeyName(key) {
  const name = String(key?.name ?? "").trim();
  const prefix = String(key?.key_prefix ?? "").trim();
  if (/^local development virtual key$/iu.test(name) || /^mhv_dev_seed(?:_|$)/iu.test(prefix)) {
    return "默认调用 KEY";
  }
  return name || "未命名 KEY";
}

/**
 * Keep real prefixes useful for distinguishing keys while hiding the seeded
 * development prefix that would otherwise expose an internal fixture name.
 */
function displayVirtualKeyPrefix(prefix) {
  const value = String(prefix ?? "").trim();
  return /^mhv_dev_seed(?:_|$)/iu.test(value) ? "已配置" : value || "-";
}

/**
 * 从后端 Environment 状态中提取 active 环境，禁用项不能作为新 Key 绑定目标。
 */
function activeEnvironments(environmentState) {
  if (environmentState?.state !== "ok" || !Array.isArray(environmentState.environments)) {
    return [];
  }
  return environmentState.environments.filter((item) => item.status === "active");
}

function environmentLabel(value, i18n = null) {
  const labels = {
    dev: keyCopy(i18n, "开发", "Development"),
    test: keyCopy(i18n, "测试", "Test"),
    prod: keyCopy(i18n, "生产", "Production"),
  };
  return labels[value];
}

function keyCopy(i18n, chinese, english) {
  return i18n?.locale === "en-US" ? english : chinese;
}

/**
 * 格式化时间。
 * 表格只展示秒级文本，完整追踪仍依赖后端日志和 request_id。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
}

/**
 * HTML 文本转义。
 * Key 名称、描述和错误信息都可能来自用户输入，必须统一转义。
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
 * HTML 属性转义。
 * data-key-id 和 title 属性额外处理反引号，避免属性注入。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
