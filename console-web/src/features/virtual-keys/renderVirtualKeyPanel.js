/**
 * 模块职责：渲染 Virtual Key 管理面板，并提供 Project/Application 归属绑定选择。
 * 维护边界：前端只展示脱敏 Key 元数据和一次性创建明文；Key 明文、哈希、租户权限与归属状态必须由后端控制。
 * 来源文件：console-web/src/features/virtual-keys/renderVirtualKeyPanel.js
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { icon } from "../public-home/icons.js";
import { renderWorkspaceConnectionStatus, workspaceSetupStepComplete } from "../workspace-navigation/renderWorkspaceNavigation.js";

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
  // 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
  if (virtualKeysState.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="virtual-keys-title">个人 KEY</h2>
        <p>个人 KEY 暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(virtualKeysState.message)} request_id: ${escapeHtml(virtualKeysState.requestId ?? "-")}
        <button type="button" data-resource-retry>重试</button>
      </div>
    `;
  }

  const keys = virtualKeysState.virtual_keys ?? [];
  const canWrite = hasPermission(authContext, PERMISSIONS.VIRTUAL_KEY_WRITE);
  const canManageWorkspace = hasPermission(authContext, PERMISSIONS.USER_MANAGE);
  const keyTitle = canManageWorkspace ? "组织 KEY" : "个人 KEY";
  const createKeyLabel = canManageWorkspace ? "创建组织 KEY" : "创建个人 KEY";
  const readyForConnection = ["provider_connected", "model_catalog_ready", "project_key_ready"]
    .every((code) => workspaceSetupStepComplete(workspaceTaskHomeState, code));
  return `
    <div class="panel-heading virtual-keys-heading">
      <div>
        <h2 id="virtual-keys-title">${keyTitle}</h2>
      </div>
      <span class="virtual-keys-count">${keys.length} 把 KEY</span>
    </div>
    ${canManageWorkspace ? renderWorkspaceConnectionStatus(workspaceTaskHomeState, "workspace.gateway.virtual-keys", i18n) : ""}
    ${renderActionError(virtualKeysState.actionError)}
    <p class="virtual-key-owner-note">${canManageWorkspace ? "组织管理员管理组织 KEY；后台成员登录后自行创建和管理个人 KEY。" : "你可以创建、查看和撤销自己的 MetaHub KEY。"}</p>
    ${canWrite && canManageWorkspace ? renderEnvironmentReadiness(environmentState) : ""}
    ${canWrite ? `<details class="key-create-disclosure"${!keys.length || virtualKeysState.keyCreateOpen ? " open" : ""}><summary>${createKeyLabel}</summary>
    <form class="create-virtual-key-form" id="create-virtual-key-form">
      <label><span>KEY 名称（可选）</span>
        <input name="name" maxlength="80" value="${canManageWorkspace ? "组织 KEY" : "个人 KEY"}" placeholder="${canManageWorkspace ? "例如：报表服务 KEY" : "例如：我的开发 KEY"}" required />
      </label>
      ${canManageWorkspace ? `<details class="virtual-key-advanced"><summary>高级设置</summary><div class="virtual-key-advanced-fields">
      <label>
        <span>环境</span>
        <select name="environmentId" ${activeEnvironments(environmentState).length ? "" : "disabled"}>
          ${renderEnvironmentOptions(environmentState)}
        </select>
      </label>
      ${renderProjectBindingFields(projectState)}
      <label>
        <span>备注</span>
        <input name="description" maxlength="240" placeholder="用途、系统或负责人" />
      </label>
      </div></details>
      ` : `<p class="virtual-key-owner-note">组织默认环境、模型与路由会自动应用，无需额外选择。</p>`}
      <div class="virtual-key-actions">
        <button type="submit" ${canManageWorkspace && !activeEnvironments(environmentState).length ? "disabled" : ""}>${createKeyLabel}</button>
        <span class="virtual-key-feedback" role="status">${escapeHtml(virtualKeysState.feedback ?? "")}</span>
      </div>
    </form></details>` : ""}
    ${canWrite ? renderOneTimeSecret(virtualKeysState.created, canManageWorkspace) : ""}
    ${keys.length && hasPermission(authContext, PERMISSIONS.DEVELOPER_PORTAL_READ) ? `<a class="${readyForConnection ? "console-flow-next" : "console-flow-related"}" href="#workspace.developer.sdk">${readyForConnection ? "下一步：" : ""}接入信息与调用示例</a>` : ''}
    ${`<div class="virtual-keys-grid" id="issued-keys" role="table" aria-label="已创建的 MetaHub KEY">
      <div class="virtual-key-row virtual-key-head" role="row">
        <span>名称</span>
        <span>前缀</span>
        <span>环境</span>
        <span>归属范围</span>
        <span>状态</span>
        <span>最近使用</span>
        <span>限制</span>
        <span>操作</span>
      </div>
      ${keys.length ? keys.map((key) => renderVirtualKeyRow(key, canWrite, !canManageWorkspace)).join("") : renderEmptyRow()}
    </div>`}
  `;
}

/**
 * 渲染 Project/Application 绑定选择器。
 * Project/Application 来自后端当前租户列表，前端不接受也不提交 tenant_id。
 */
function renderProjectBindingFields(projectState) {
  const projects = projectState?.projects ?? [];
  const applications = projectState?.applications ?? [];
  // 归属列表失败不能阻塞 Key 页面，但必须阻止用户误以为能安全选择项目。
  if (projectState?.state === "error") {
    return `
      <div class="virtual-key-binding-warning" role="alert">项目归属暂时无法加载
        <button type="button" data-resource-retry>重试</button>
      </div>
    `;
  }
  return `
    <label>
      <span>项目（可选）</span>
      <select name="projectId">
        <option value="">不绑定项目</option>
        ${projects
          .filter((item) => item.status === "active")
          .map((item) => `<option value="${escapeAttribute(item.project_id)}">${escapeHtml(item.name)} / ${escapeHtml(item.project_code)}</option>`)
          .join("")}
      </select>
    </label>
    <label>
      <span>应用（可选）</span>
      <select name="applicationId">
        <option value="">不绑定应用</option>
        ${applications
          .filter((item) => item.status === "active")
          .map((item) => `<option value="${escapeAttribute(item.application_id)}">${escapeHtml(item.name)} / ${escapeHtml(item.project_name ?? item.project_code ?? "-")}</option>`)
          .join("")}
      </select>
    </label>
  `;
}

/**
 * 渲染创建成功后的一次性明文 Key。
 * 明文只来自刚创建的响应，刷新后不会再出现；页面文案提醒用户立即保存。
 */
function renderOneTimeSecret(created, organizationKey = false) {
  if (!created?.plainValue) {
    return "";
  }
  const systemKey = Boolean(created.applicationName || created.projectName);
  const owner = created.applicationName || created.projectName || (organizationKey ? "当前组织" : "本人");
  return `
    <div class="one-time-secret" role="status" aria-label="一次性 Virtual Key 明文">
      <div>
        <strong>${created.operation === "rotated" ? "KEY 已轮换" : "KEY 已创建"} · 只显示一次</strong>
        <p class="one-time-secret-owner">${systemKey ? "绑定对象" : "KEY 归属"}：${escapeHtml(owner)}${created.keyName ? ` · ${escapeHtml(created.keyName)}` : ""}</p>
        <p>${systemKey ? "请先复制保存，将 KEY 配置到对应应用或项目。" : organizationKey ? "请先复制保存，仅配置到授权的组织系统中。" : "请先复制保存；关闭或刷新后无法再次查看此 KEY。"}工具要求时再填写模型名称；各工具接入要求见“接入信息”。</p>
      </div>
      <code>${escapeHtml(created.plainValue)}</code>
      <div class="virtual-key-actions">
        <button type="button" data-copy-virtual-key>复制</button>
        <button type="button" data-dismiss-virtual-key-secret>隐藏</button>
      </div>
    </div>
  `;
}

/**
 * 渲染 Virtual Key 操作错误。
 * 保留 request_id，便于从控制台提示追踪后端失败日志。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="virtual-key-action-error" role="alert">
      ${escapeHtml(error.message ?? "Virtual Key 操作失败")} request_id: ${escapeHtml(error.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染单条 Virtual Key。
 * 列表只展示 key_prefix、状态、环境、归属和最近使用时间，不展示明文 Key 或哈希。
 */
function renderVirtualKeyRow(key, canWrite, personalOnly) {
  const nextStatus = key.status === "active" ? "disabled" : "active";
  const actionLabel = key.status === "active" ? "禁用" : "启用";
  return `
    <div class="virtual-key-row" data-virtual-key-row="${escapeAttribute(key.virtual_key_id)}" role="row">
      <span class="virtual-key-name"><span class="virtual-key-field-label">KEY 名称</span>
        <strong>${escapeHtml(key.name)}</strong>
        ${key.description ? `<em>${escapeHtml(key.description)}</em>` : ""}
      </span>
      <span><span class="virtual-key-field-label">密钥前缀</span><code>${escapeHtml(key.key_prefix)}</code></span>
      <span><span class="virtual-key-field-label">环境</span>${escapeHtml(key.environment_name ?? ENVIRONMENT_LABELS[key.environment] ?? key.environment)}</span>
      <span class="virtual-key-binding"><span class="virtual-key-field-label">归属范围</span>
        ${key.consumer_id ? `<strong>历史成员关联</strong><em>${key.consumer_status === "active" ? "使用者已启用" : "使用者已停用"}</em>` : ""}
        ${key.project_id ? `<strong>${escapeHtml(key.project_name ?? "已关联项目")}</strong>` : ""}
        ${key.application_id ? `<em>${escapeHtml(key.application_name ?? "已关联应用")}</em>` : ""}
        ${!key.consumer_id && !key.project_id && !key.application_id ? `<em>${personalOnly ? "本人" : "当前组织"}</em>` : ""}
      </span>
      <span><span class="virtual-key-field-label">状态</span><mark class="status-mark status-${escapeHtml(key.status)}">${escapeHtml(key.status === "active" ? "已启用" : key.status === "disabled" ? "已停用" : "状态未知")}</mark></span>
      <span><span class="virtual-key-field-label">最近使用</span>${key.last_used_at ? escapeHtml(formatDateTime(key.last_used_at)) : "尚未使用"}</span>
      <div class="virtual-key-limits-cell">
        ${renderLimitSummary(key.rate_limit_rule)}
        ${canWrite ? `<details class="virtual-key-limit-editor"><summary>调整限制</summary><form class="virtual-key-limit-form" data-virtual-key-limits-form data-virtual-key-id="${escapeAttribute(key.virtual_key_id)}">
          ${renderLimitInput("rpmLimit", "RPM", key.rate_limit_rule?.rpm_limit)}
          ${renderLimitInput("tpmLimit", "TPM", key.rate_limit_rule?.tpm_limit)}
          ${renderLimitInput("concurrencyLimit", "并发", key.rate_limit_rule?.concurrency_limit)}
          <button type="submit">保存限制</button>
        </form></details>` : ""}
      </div>
      <span class="virtual-key-row-actions">
        ${canWrite && Number.isSafeInteger(key.credential_revision) && key.status === "active" ? `<button type="button" data-rotate-virtual-key="${escapeAttribute(key.virtual_key_id)}" data-key-revision="${key.credential_revision}" title="轮换 KEY，旧 KEY 将立即失效" aria-label="轮换 ${escapeAttribute(key.name)}">${icon("rotate-cw")}</button>` : ""}
        ${canWrite ? `<form data-virtual-key-form data-virtual-key-id="${escapeAttribute(key.virtual_key_id)}">
          <input type="hidden" name="status" value="${escapeAttribute(nextStatus)}" />
          <button type="submit">${actionLabel}</button>
        </form>` : ""}
      </span>
    </div>
  `;
}

function renderEnvironmentReadiness(state) {
  if (state?.state === "error") {
    return '<p class="empty-state error-state" role="alert">运行环境加载失败，暂不能创建组织 KEY。<button type="button" data-resource-retry>重试</button></p>';
  }
  if (state?.state !== "ok") {
    return '<p role="status">正在加载运行环境...</p>';
  }
  return activeEnvironments(state).length ? "" : '<p role="status">暂无可用运行环境，暂不能创建组织 KEY。</p>';
}

/**
 * 渲染 Virtual Key 当前限制摘要。
 * 空规则或空字段统一展示“不限制”，避免用户把 0 误解成无限制。
 */
function renderLimitSummary(rule) {
  if ([rule?.rpm_limit, rule?.tpm_limit, rule?.concurrency_limit].every((value) => value === null || value === undefined)) {
    return '<p class="virtual-key-limit-summary">不限速</p>';
  }
  return `
    <p class="virtual-key-limit-summary">
      RPM ${escapeHtml(limitText(rule?.rpm_limit))} ·
      TPM ${escapeHtml(limitText(rule?.tpm_limit))} ·
      并发 ${escapeHtml(limitText(rule?.concurrency_limit))}
    </p>
  `;
}

/**
 * 渲染单个限制输入框。
 * 输入框允许留空，提交后由 API client 转成 null，表示清除该限制。
 */
function renderLimitInput(name, label, value) {
  return `
    <label>
      <span>${escapeHtml(label)}</span>
      <input name="${escapeAttribute(name)}" type="number" min="1" inputmode="numeric" value="${escapeAttribute(value ?? "")}" placeholder="不限" />
    </label>
  `;
}

/**
 * 把限制数值转换成列表摘要文本。
 */
function limitText(value) {
  if (value === null || value === undefined || value === "") {
    return "不限制";
  }
  return String(value);
}

/**
 * 渲染 Virtual Key 空列表行。
 * 使用表格行结构保持列表布局稳定。
 */
function renderEmptyRow() {
  return `
    <div class="virtual-key-row virtual-key-empty" role="row">
      <span>尚未创建 KEY。</span>
    </div>
  `;
}

/**
 * 渲染环境选项。
 * value 使用后端枚举，label 使用中文文案，避免提交展示文本。
 */
function renderEnvironmentOptions(environmentState) {
  const environments = activeEnvironments(environmentState);
  if (!environments.length) {
    return `<option value="">请先创建 active Environment</option>`;
  }
  return environments
    .map((item) => `<option value="${escapeAttribute(item.environment_id)}">${escapeHtml(item.name)} / ${escapeHtml(item.code)}</option>`)
    .join("");
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
