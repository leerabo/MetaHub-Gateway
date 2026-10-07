/**
 * 模块职责：维护策略管理页的安全渲染、内存草稿、异步请求门禁和响应式列表结构。
 * 安全边界：前端只消费后端权限快照，表达式不在浏览器执行，租户隔离、权限和模型归属仍由后端校验。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import {
  GUIDED_CONDITION_FIELDS,
  buildGuidedConditionExpression,
  emptyGuidedCondition,
  guidedConditionOperators,
  parseGuidedConditionExpression,
} from "./policyConditionBuilder.js";

// 策略状态、动作和默认值只决定前端展示，后端枚举与 RBAC 始终是权威边界。
const POLICY_STATUSES = ["active", "disabled"];
const POLICY_ACTIONS = ["allow", "deny", "mask", "log_only", "route_to"];
const POLICY_VARIABLES = ["tenant", "env", "project", "model", "data_class", "budget_remaining"];
const DEFAULT_PRIORITY = "100";

// 固定中文标签避免把未知后端枚举直接拼入样式名；未知值仍会作为转义文本显示以便排障。
const STATUS_LABELS = {
  active: "启用（待发布）",
  disabled: "停用",
};
const ACTION_LABELS = {
  allow: "允许",
  deny: "拒绝",
  mask: "脱敏决策",
  log_only: "仅记录",
  route_to: "路由到模型",
};

/**
 * 从认证上下文提取策略页身份。
 * tenant/user 用于草稿隔离，role 用于身份变化清理；缺少任一关键值时不得创建可复用作用域。
 */
export function policyIdentityFromAuthContext(authContext, workspaceOwner = null) {
  if (workspaceOwner?.workspaceId && workspaceOwner?.globalSessionId && workspaceOwner?.role) {
    return normalizePolicyIdentity({
      tenantId: workspaceOwner.workspaceId,
      userId: workspaceOwner.globalSessionId,
      role: workspaceOwner.role,
    });
  }
  return normalizePolicyIdentity({
    tenantId: authContext?.user?.tenant_id ?? authContext?.tenant?.tenant_id,
    userId: authContext?.user?.user_id ?? authContext?.user?.email,
    role: authContext?.user?.role,
  });
}

/**
 * 生成异步请求使用的完整身份快照。
 * role 被纳入快照，确保管理员在请求期间降权后，旧响应无法恢复 Policy DOM 或保存按钮。
 */
export function policyIdentitySnapshot(identity) {
  const normalized = normalizePolicyIdentity(identity);
  return normalized
    ? `${normalized.tenantId}\u0000${normalized.userId}\u0000${normalized.role}`
    : "";
}

/**
 * 创建新建策略的空草稿。
 * priority 保留为字符串以便精确比较表单快照，真正提交时 API 客户端才转换成 number。
 */
export function createEmptyPolicyDraft() {
  return {
    name: "",
    description: "",
    status: "active",
    priority: DEFAULT_PRIORITY,
    conditionExpr: "",
    conditionMode: "guided",
    conditionBuilder: emptyGuidedCondition(),
    actionType: "",
    targetModelId: null,
    version: null,
  };
}

/**
 * 从后端 PolicyInfo 构造可编辑草稿。
 * 只读取公开管理字段，不复制 tenant_id、AST、hash、actor、事件或审计摘要到草稿。
 */
export function policyDraftFromPolicy(policy) {
  const rule = policy?.rule ?? {};
  const action = policy?.action ?? {};
  const conditionExpr = String(rule.condition_expr ?? "");
  const guided = parseGuidedConditionExpression(conditionExpr);
  return {
    name: String(policy?.name ?? ""),
    description: String(policy?.description ?? ""),
    status: POLICY_STATUSES.includes(policy?.status) ? policy.status : "disabled",
    priority: String(policy?.priority ?? DEFAULT_PRIORITY),
    conditionExpr,
    conditionMode: guided ? "guided" : "advanced",
    conditionBuilder: guided ?? emptyGuidedCondition(),
    actionType: POLICY_ACTIONS.includes(action.type ?? action.action_type)
      ? (action.type ?? action.action_type)
      : "allow",
    targetModelId: action.target_model_id ? String(action.target_model_id) : null,
    version: Number(policy?.version ?? 0) || null,
  };
}

/**
 * 创建一个策略编辑作用域。
 * selectedPolicyId 为 null 表示新建；validation 只允许对应当前精确快照的后端校验结果。
 */
export function createEmptyPolicyScope() {
  return {
    selectedPolicyId: null,
    open: false,
    draft: createEmptyPolicyDraft(),
    dirty: false,
    validation: null,
    modeError: "",
  };
}

/**
 * 生成当前编辑表单的精确内存快照。
 * 固定字段顺序同时纳入对象 ID/version，防止不同策略恰好具有相同字段时错误复用旧校验。
 */
export function policyDraftSnapshot(draft, { selectedPolicyId = null } = {}) {
  return JSON.stringify({
    selectedPolicyId: selectedPolicyId ? String(selectedPolicyId) : null,
    version: Number(draft?.version ?? 0) || null,
    name: String(draft?.name ?? ""),
    description: String(draft?.description ?? ""),
    status: String(draft?.status ?? ""),
    priority: String(draft?.priority ?? ""),
    conditionExpr: String(draft?.conditionExpr ?? ""),
    conditionMode: String(draft?.conditionMode ?? "advanced"),
    conditionBuilder: draft?.conditionBuilder ?? emptyGuidedCondition(),
    actionType: String(draft?.actionType ?? ""),
    targetModelId: draft?.targetModelId ? String(draft.targetModelId) : null,
  });
}

/**
 * 创建按 tenant+user 隔离的内存草稿仓库。
 * 仓库不接触 localStorage/sessionStorage；role 或身份变化会清空全部旧作用域，离页由调用方显式 clear。
 */
export function createPolicyDraftMemory() {
  const scopes = new Map();
  let activeSecurityIdentity = "";

  return {
    /** 进入身份作用域；相同 tenant+user+role 的定时刷新复用现有 dirty draft。 */
    enter(identity) {
      const normalized = normalizePolicyIdentity(identity);
      const securityIdentity = policyIdentitySnapshot(normalized);
      if (!normalized || !securityIdentity) {
        // 缺少认证边界时必须丢弃旧草稿，不能让匿名或半登录页面读取上一位管理员输入。
        scopes.clear();
        activeSecurityIdentity = "";
        return null;
      }
      if (activeSecurityIdentity && activeSecurityIdentity !== securityIdentity) {
        // 用户、租户或角色变化必须原子清空旧作用域，避免跨身份恢复表达式正文。
        scopes.clear();
      }
      activeSecurityIdentity = securityIdentity;
      const key = policyDraftScopeKey(normalized);
      if (!scopes.has(key)) {
        scopes.set(key, createEmptyPolicyScope());
      }
      return scopes.get(key);
    },

    /** 读取当前身份作用域；身份不匹配时返回 null 而不是隐式切换。 */
    read(identity) {
      const normalized = normalizePolicyIdentity(identity);
      if (!normalized || policyIdentitySnapshot(normalized) !== activeSecurityIdentity) {
        // 请求回调只能读取开始时同一身份的草稿，不能自动进入新的 tenant/user 作用域。
        return null;
      }
      return scopes.get(policyDraftScopeKey(normalized)) ?? null;
    },

    /** 覆盖当前作用域；写入前再次执行身份门禁，返回实际保存的对象。 */
    write(identity, scope) {
      const current = this.enter(identity);
      if (!current) {
        return null;
      }
      const key = policyDraftScopeKey(normalizePolicyIdentity(identity));
      scopes.set(key, scope);
      return scope;
    },

    /** 清除全部草稿和身份标记，供离页、登出、401 与角色变化路径复用。 */
    clear() {
      scopes.clear();
      activeSecurityIdentity = "";
    },
  };
}

/**
 * 创建 validate/save 共用的单调异步生命周期。
 * 每次 begin/invalidate 都递增 revision 并取消旧 controller；响应还必须同时匹配快照、身份、导航和刷新代次。
 */
export function createPolicyRequestLifecycle() {
  let revision = 0;
  let activeOperation = null;

  return {
    /** 开始新操作并取消旧请求，返回调用 fetch 所需的 AbortSignal 与完整门禁快照。 */
    begin({ kind, snapshot, identity, navigation, generation }) {
      revision += 1;
      activeOperation?.controller.abort();
      const controller = new AbortController();
      activeOperation = {
        kind: String(kind ?? "request"),
        revision,
        controller,
        signal: controller.signal,
        snapshot: String(snapshot ?? ""),
        identity: String(identity ?? ""),
        navigation: String(navigation ?? ""),
        generation: Number(generation ?? 0),
      };
      return activeOperation;
    },

    /** 使当前操作失效并 abort；字段变化、切换策略、离页和身份变化都必须调用。 */
    invalidate() {
      revision += 1;
      activeOperation?.controller.abort();
      activeOperation = null;
      return revision;
    },

    /** 核对旧响应是否仍拥有当前编辑器，任何一个边界不同都拒绝提交到 DOM。 */
    isCurrent(operation, current) {
      return Boolean(
        operation
        && activeOperation === operation
        && !operation.signal.aborted
        && operation.revision === revision
        && operation.snapshot === String(current?.snapshot ?? "")
        && operation.identity === String(current?.identity ?? "")
        && operation.navigation === String(current?.navigation ?? "")
        && operation.generation === Number(current?.generation ?? 0),
      );
    },

    /** 完成当前操作并释放 controller 引用；旧操作调用 finish 不得影响新请求。 */
    finish(operation) {
      if (activeOperation === operation) {
        activeOperation = null;
      }
    },

    /** 返回是否存在未结束操作，供 15 秒定时刷新避免打断已提交但响应未回的保存请求。 */
    isBusy() {
      return Boolean(activeOperation);
    },
  };
}

/**
 * 判断策略 active-only loader 是否允许发起或提交响应。
 * 必须同时满足后端 ``policy.read`` 快照、active navigation、可选 hash、刷新代次和身份一致。
 */
export function shouldLoadPolicies({
  canRead,
  activeNavigationItemId,
  hashNavigation,
  refreshId,
  currentRefreshId,
  requestIdentity,
  currentIdentity,
}) {
  // 权限布尔值只能由调用方根据后端快照计算；任一旧刷新或身份变化都必须停止提交结果。
  return canRead === true
    && activeNavigationItemId === "policies"
    && (!hashNavigation || hashNavigation === "policies")
    && Number(refreshId) === Number(currentRefreshId)
    && Boolean(requestIdentity)
    && requestIdentity === currentIdentity;
}

/**
 * 渲染具有 ``policy.read`` 权限的策略管理面板。
 * 页面所有动态字段均经过上下文转义，不执行 DSL，也不把策略对象序列化到 data-* 或浏览器存储。
 */
export function renderPoliciesPanel(policiesState, modelsState, authContext, resources = {}) {
  if (!hasPermission(authContext, PERMISSIONS.POLICY_READ)) {
    // 前端失败关闭只改善体验；后端 GET 仍按同一稳定权限码独立校验。
    return "";
  }

  const canWrite = hasPermission(authContext, PERMISSIONS.POLICY_WRITE);
  const policies = Array.isArray(policiesState?.policies) ? policiesState.policies : [];
  const activeModels = activeModelsFromState(modelsState);
  const editor = policiesState?.editor ?? { ...createEmptyPolicyScope(), currentSnapshot: "", canSave: false };
  const filters = policiesState?.filters ?? { status: "all", action: "all" };
  const visiblePolicies = filterPolicies(policies, filters);
  const saving = editor.operationStatus === "saving";
  const editorOpen = canWrite && Boolean(editor.open || editor.selectedPolicyId || editor.dirty || editor.validation);

  return `
    <div class="policy-heading">
      <div>
        <h2 id="policies-title">规则概览</h2>
        <p>需要按环境、项目、模型或预算设置特殊处理时，在这里编写规则。保存后还需在“上线与版本”发布，网关才会使用。</p>
      </div>
      <div class="policy-heading-actions">
        <span>${visiblePolicies.length} / ${policies.length} 条</span>
        ${canWrite ? `<button id="policy-new" type="button" ${saving ? "disabled" : ""}>新建自定义规则</button>` : ""}
      </div>
    </div>
    <div class="policy-paths" aria-label="选择策略配置方式">
      ${hasPermission(authContext, PERMISSIONS.POLICY_TEMPLATE_READ) ? '<div><strong>常见场景</strong><p>预算、路由、数据保护等，可先选场景模板，预览影响后直接应用。</p><a href="#workspace.governance.policy-templates">查看场景模板</a></div>' : ""}
      <div><strong>特殊条件</strong><p>模板不适用时，在本页写自定义规则；保存草稿后再发布。</p>${hasPermission(authContext, PERMISSIONS.POLICY_PUBLISH) ? '<a href="#workspace.governance.policy-releases">查看上线与版本</a>' : ""}</div>
    </div>
    ${editorOpen ? "" : renderPolicyStateError(policiesState)}
    ${!editorOpen && policiesState?.feedback ? `<p class="policy-save-feedback" role="status">${escapeHtml(policiesState.feedback)}</p>` : ""}
    ${policies.length ? renderPolicyFilters(filters, saving) : ""}
    ${policies.length ? renderPolicyList(visiblePolicies, activeModels, saving, canWrite) : editorOpen ? "" : '<p class="policy-list-empty">尚无自定义规则。可以直接使用场景模板，也可以新建规则。</p>'}
    ${editorOpen ? renderPolicyEditor(editor, activeModels, policiesState, resources) : ""}
  `;
}

/**
 * 规范化认证身份字段。
 * 原始值只驻留当前内存，不进入 DOM、URL、事件或存储；缺少 tenant/user/role 时返回 null。
 */
function normalizePolicyIdentity(identity) {
  const tenantId = String(identity?.tenantId ?? "").trim();
  const userId = String(identity?.userId ?? "").trim();
  const role = String(identity?.role ?? "").trim();
  // 任一身份字段缺失都不能形成草稿 owner，调用方会清理旧 tenant/user 作用域。
  return tenantId && userId && role ? { tenantId, userId, role } : null;
}

/**
 * 生成 tenant+user 草稿 Map 键。
 * role 不进入草稿键但进入 security identity，角色变化会先清空 Map，兼顾明确隔离与刷新复用。
 */
function policyDraftScopeKey(identity) {
  return `${identity.tenantId}\u0000${identity.userId}`;
}

/**
 * 从已授权模型目录选出 route_to 候选。
 * 只接受 state=ok 且 status=active 的当前租户响应；后端保存时仍会在事务内复核归属和状态。
 */
function activeModelsFromState(modelsState) {
  if (modelsState?.state !== "ok" || !Array.isArray(modelsState.models)) {
    return [];
  }
  return modelsState.models.filter((model) => model?.status === "active" && model?.model_id);
}

/**
 * 按本地状态/动作筛选当前已读取策略。
 * 筛选不改变后端数据，也不参与权限判断；列表原始顺序继续沿用 priority+policy_id。
 */
function filterPolicies(policies, filters) {
  return policies.filter((policy) => {
    const statusMatches = !filters.status || filters.status === "all" || policy.status === filters.status;
    const action = policyAction(policy);
    const actionMatches = !filters.action || filters.action === "all" || action.type === filters.action;
    return statusMatches && actionMatches;
  });
}

/**
 * 渲染加载或写操作错误。
 * 只展示可行动的失败文案；5xx 时旧草稿和同身份缓存列表仍可见但保存必须重新校验。
 */
function renderPolicyStateError(policiesState) {
  const error = policiesState?.actionError ?? (policiesState?.state === "error" ? policiesState : null);
  // 没有统一错误对象时不占用告警区域，避免把普通空列表误报为 policy.unavailable。
  if (!error) {
    return "";
  }
  return `
    <div class="policy-error" role="alert">
      <strong>策略服务暂不可用</strong>
      <span>${escapeHtml(error.message ?? "请稍后重试")}</span>
    </div>
  `;
}

/**
 * 渲染单策略编辑器。
 * 保存按钮只由当前快照的后端 valid=true 结果启用；修改 DOM 属性不能越过事件处理器和后端重编译。
 */
function renderPolicyEditor(editor, activeModels, policiesState, resources) {
  const draft = editor.draft ?? createEmptyPolicyDraft();
  const editing = Boolean(editor.selectedPolicyId);
  const busy = ["validating", "saving"].includes(editor.operationStatus);
  const saving = editor.operationStatus === "saving";
  const routeTo = draft.actionType === "route_to";
  const fieldDisabled = saving ? "disabled" : "";
  const feedback = editor.operationStatus === "validating"
    ? "校验中"
    : editor.operationStatus === "saving"
      ? "保存中"
      : policiesState?.feedback ?? "";

  return `
    <div class="policy-editor-backdrop" data-policy-editor-backdrop>
    <section class="policy-editor" role="dialog" aria-modal="true" aria-labelledby="policy-editor-title" tabindex="-1">
      <div class="policy-editor-heading">
        <div>
          <h3 id="policy-editor-title" tabindex="-1">${editing ? "编辑自定义规则" : "新建自定义规则"}</h3>
          <span>选择条件和处理方式，校验后保存。保存的规则还需在“上线与版本”发布。</span>
        </div>
        <button class="policy-editor-cancel" id="policy-cancel" type="button" aria-label="关闭规则编辑" ${saving ? "disabled" : ""}>×</button>
      </div>
      ${renderPolicyStateError(policiesState)}
      <form id="policy-editor-form">
        <label>
          <span>规则名称</span>
          <input name="name" maxlength="80" required value="${escapeAttribute(draft.name)}" ${fieldDisabled} />
        </label>
        <label class="policy-description-field">
          <span>用途说明（可选）</span>
          <textarea name="description" maxlength="500" rows="2" ${fieldDisabled}>${escapeHtml(draft.description)}</textarea>
        </label>
        <div class="policy-condition-section">
          <div class="policy-condition-section__heading">
            <strong>什么情况下执行？</strong>
            <div class="policy-condition-modes" aria-label="条件填写方式">
              <button type="button" data-policy-condition-mode="guided" aria-pressed="${draft.conditionMode !== "advanced"}" ${busy ? "disabled" : ""}>表单填写</button>
              <button type="button" data-policy-condition-mode="advanced" aria-pressed="${draft.conditionMode === "advanced"}" ${busy ? "disabled" : ""}>高级编辑</button>
            </div>
          </div>
          ${editor.modeError ? `<p class="policy-condition-mode-error" role="alert">${escapeHtml(editor.modeError)}</p>` : ""}
          ${draft.conditionMode === "advanced"
            ? renderAdvancedCondition(draft, fieldDisabled)
            : renderGuidedCondition(draft, resources, fieldDisabled)}
        </div>
        <label>
          <span>条件成立后执行</span>
          <select name="actionType" required ${fieldDisabled}>
            <option value="">请选择处理方式</option>
            ${POLICY_ACTIONS.map((action) => renderOption(action, ACTION_LABELS[action], draft.actionType)).join("")}
          </select>
        </label>
        <p class="policy-action-help">“拒绝”会阻止请求；“仅记录”只留下命中记录；“路由到模型”会把命中请求交给指定模型。</p>
        <label class="policy-route-target" data-policy-route-target-field${routeTo ? "" : " hidden"}>
          <span>目标模型</span>
          <select name="targetModelId" ${routeTo && !saving ? "required" : "disabled"}>
            ${renderModelOptions(activeModels, draft.targetModelId)}
          </select>
        </label>
        <details class="policy-advanced-options">
          <summary>更多设置：状态与执行顺序</summary>
          <div>
            <label>
              <span>状态</span>
              <select name="status" ${fieldDisabled}>
                ${POLICY_STATUSES.map((status) => renderOption(status, STATUS_LABELS[status], draft.status)).join("")}
              </select>
            </label>
            <label>
              <span>执行顺序（数字越小越靠前）</span>
              <input name="priority" type="number" min="1" max="10000" step="1" required value="${escapeAttribute(draft.priority)}" ${fieldDisabled} />
            </label>
          </div>
        </details>
        <div class="policy-editor-actions">
          <button data-policy-validate type="button" ${busy ? "disabled" : ""}>1. 校验规则</button>
          <button data-policy-save type="submit" ${editor.canSave && !busy ? "" : "disabled"}>2. 保存草稿</button>
          <span class="policy-feedback" role="status">${escapeHtml(feedback)}</span>
        </div>
      </form>
      <div class="policy-validation" aria-live="polite">
        ${renderValidationResult(editor.validation)}
      </div>
    </section>
    </div>
  `;
}

function renderGuidedCondition(draft, resources, fieldDisabled) {
  const condition = draft.conditionBuilder ?? emptyGuidedCondition();
  const operators = guidedConditionOperators(condition.field);
  return `
    <div class="policy-guided-condition">
      <label>
        <span>条件类型</span>
        <select name="conditionField" required ${fieldDisabled}>
          <option value="">请选择</option>
          ${GUIDED_CONDITION_FIELDS.map((item) => renderOption(item.value, item.label, condition.field)).join("")}
        </select>
      </label>
      <label>
        <span>比较方式</span>
        <select name="conditionOperator" ${fieldDisabled}>
          ${operators.map((operator) => renderOption(operator, operatorLabel(operator), condition.operator)).join("")}
        </select>
      </label>
      <label>
        <span>${condition.field === "budget_remaining" ? "金额（元）" : "条件值"}</span>
        ${renderGuidedConditionValue(condition, resources, fieldDisabled)}
      </label>
      <p>选择条件、比较方式和值即可；系统会自动生成规则并校验。</p>
    </div>
  `;
}

function renderGuidedConditionValue(condition, resources, fieldDisabled) {
  const state = condition.field === "project" ? resources?.projectsState
    : condition.field === "env" ? resources?.environmentsState : null;
  const source = condition.field === "project" ? state?.projects : state?.environments;
  const items = state?.state === "ok" && Array.isArray(source)
    ? source.filter((item) => item?.status === "active") : [];
  const options = items.map((item) => condition.field === "project"
    ? { value: item.project_id, label: item.name ?? item.project_code ?? item.project_id }
    : { value: item.code, label: item.name ?? item.code }).filter((item) => item.value);
  if (options.length) {
    if (condition.value && !options.some((item) => item.value === condition.value)) {
      options.unshift({ value: condition.value, label: "原条件值（当前列表中不可用）" });
    }
    return `<select name="conditionValue" required ${fieldDisabled}>
      <option value="">请选择${condition.field === "project" ? "项目" : "环境"}</option>
      ${options.map((item) => renderOption(item.value, item.label, condition.value)).join("")}
    </select>`;
  }
  if (!condition.field) {
    return '<input name="conditionValue" placeholder="先选择条件类型" disabled />';
  }
  const numeric = condition.field === "budget_remaining";
  const placeholder = numeric ? "例如 100" : condition.field === "env" ? "例如 prod"
    : condition.field === "project" ? "项目标识" : "例如 sensitive";
  return `<input name="conditionValue" required maxlength="128" value="${escapeAttribute(condition.value)}"
    placeholder="${escapeAttribute(placeholder)}" ${numeric ? 'inputmode="decimal" pattern="(?:0|[1-9][0-9]{0,11})(?:\\.[0-9]{1,12})?" title="请输入非负金额，最多 12 位整数和 12 位小数"' : ""} ${fieldDisabled} />`;
}

function renderAdvancedCondition(draft, fieldDisabled) {
  return `
    <label class="policy-condition-field">
      <span>条件表达式</span>
      <textarea name="conditionExpr" maxlength="1024" rows="5" spellcheck="false" required
        placeholder='env == "prod" and budget_remaining &lt; 100' ${fieldDisabled}>${escapeHtml(draft.conditionExpr)}</textarea>
    </label>
    <p class="policy-condition-help">仅用于多个条件或特殊逻辑。普通规则请选“表单填写”；这里的表达式会由服务端校验。</p>
    <div class="policy-variable-hint" aria-label="可插入的条件变量">
      <span>可插入字段：</span>
      ${POLICY_VARIABLES.map((variable) => `<button type="button" data-policy-variable="${escapeAttribute(variable)}" aria-label="插入变量 ${escapeAttribute(variable)}"><code class="notranslate" translate="no">${escapeHtml(variable)}</code></button>`).join("")}
    </div>
  `;
}

function operatorLabel(value) {
  return ({ "==": "等于", "!=": "不等于", "<": "低于", "<=": "不高于", ">": "高于", ">=": "不低于" })[value] ?? value;
}

/**
 * 渲染 route_to 模型选项。
 * 下拉只包含 active Model，display/id/provider 均逐项转义且不把模型对象写入 data-*。
 */
function renderModelOptions(activeModels, selectedModelId) {
  const options = activeModels.map((model) => {
    const label = model.display_name || model.name || "已配置模型";
    return renderOption(model.model_id, label, selectedModelId);
  });
  return [
    `<option value="">${activeModels.length ? "请选择 active 模型" : "当前没有 active 模型"}</option>`,
    ...options,
  ].join("");
}

/**
 * 渲染后端校验结果。
 * diagnostics、canonical、hash 和 request_id 都按文本转义；表达式从不通过 inner eval 或对象属性执行。
 */
function renderValidationResult(validation) {
  if (!validation) {
    return `<p class="policy-validation-empty">保存前需要校验当前表单。</p>`;
  }
  if (validation.valid !== true) {
    const diagnostics = Array.isArray(validation.diagnostics) ? validation.diagnostics : [];
    return `
      <div class="policy-validation-failed" role="alert">
        <strong>校验未通过</strong>
        <ul>
          ${diagnostics.length
            ? diagnostics.map((diagnostic) => renderDiagnostic(diagnostic)).join("")
            : "<li>策略草稿不符合受限 DSL 规则。</li>"}
        </ul>
      </div>
    `;
  }
  const variables = Array.isArray(validation.variables) ? validation.variables : [];
  return `
    <div class="policy-validation-passed">
      <div><strong>校验通过</strong></div>
      <dl>
        <div><dt>使用条件</dt><dd>${escapeHtml(variables.join(", ") || "未使用额外条件")}</dd></div>
      </dl>
    </div>
  `;
}

/**
 * 渲染单条固定诊断。
 * 位置信息只展示整数区间，message 作为普通文本处理，不能拼成 HTML 或 title 属性。
 */
function renderDiagnostic(diagnostic) {
  return `
    <li>
      <p>${escapeHtml(diagnostic?.message ?? "策略表达式无效")}</p>
    </li>
  `;
}

/**
 * 渲染本地列表筛选器。
 * 筛选值来自固定枚举，不携带 tenant_id，也不会让浏览器重新请求未授权策略数据。
 */
function renderPolicyFilters(filters, disabled) {
  const disabledAttribute = disabled ? "disabled" : "";
  return `
    <form class="policy-filters" id="policy-filter-form">
      <label>
        <span>状态</span>
        <select name="policyStatus" ${disabledAttribute}>
          ${renderOption("all", "全部状态", filters.status)}
          ${POLICY_STATUSES.map((status) => renderOption(status, STATUS_LABELS[status], filters.status)).join("")}
        </select>
      </label>
      <label>
        <span>动作</span>
        <select name="policyAction" ${disabledAttribute}>
          ${renderOption("all", "全部动作", filters.action)}
          ${POLICY_ACTIONS.map((action) => renderOption(action, ACTION_LABELS[action], filters.action)).join("")}
        </select>
      </label>
      <button id="policy-filter-reset" type="button" ${disabledAttribute}>重置</button>
    </form>
  `;
}

/**
 * 渲染策略列表和空态。
 * 行顺序沿用后端 priority+policy_id，长表达式仅在受控 code 容器换行，不撑破页面根节点。
 */
function renderPolicyList(policies, activeModels, disabled, canWrite) {
  return `
    <div class="policy-table" role="table" aria-label="当前租户策略列表">
      <div class="policy-row policy-row-head" role="row">
        <span>优先级 / 名称</span>
        <span>状态</span>
        <span>条件</span>
        <span>动作</span>
        <span>更新时间</span>
        <span>操作</span>
      </div>
      ${policies.length
        ? policies.map((policy) => renderPolicyRow(policy, activeModels, disabled, canWrite)).join("")
        : renderPolicyEmptyRow()}
    </div>
  `;
}

/**
 * 渲染一条策略记录。
 * policy_id 仅作为后端返回的普通 data 属性供编辑命令定位，完整 Policy/DSL JSON 不进入属性或存储。
 */
function renderPolicyRow(policy, activeModels, disabled, canWrite) {
  const rule = policyRule(policy);
  const action = policyAction(policy);
  const status = POLICY_STATUSES.includes(policy?.status) ? policy.status : "disabled";
  const targetLabel = action.type === "route_to"
    ? policyModelLabel(activeModels, action.target_model_id)
    : "-";
  return `
    <div class="policy-row" role="row">
      <span data-label="优先级 / 名称" class="policy-row-name">
        <strong>${escapeHtml(policy?.priority ?? "-")} · ${escapeHtml(policy?.name ?? "-")}</strong>
        <em>${escapeHtml(policy?.description || "未填写说明")}</em>
      </span>
      <span data-label="状态">
        <mark class="policy-status policy-status-${status}">${escapeHtml(STATUS_LABELS[status])}</mark>
      </span>
      <span data-label="条件" class="policy-condition">${renderPolicyConditionSummary(rule.condition_expr)}</span>
      <span data-label="动作" class="policy-action-summary">
        <strong>${escapeHtml(ACTION_LABELS[action.type] ?? action.type ?? "-")}</strong>
        <em>${escapeHtml(targetLabel)}</em>
      </span>
      <time data-label="更新时间" datetime="${escapeAttribute(policy?.updated_at ?? "")}">${escapeHtml(formatDateTime(policy?.updated_at))}</time>
      <span data-label="操作">
        ${canWrite ? `<button data-policy-edit="${escapeAttribute(policy?.policy_id ?? "")}" type="button" ${disabled ? "disabled" : ""}>编辑</button>` : "-"}
      </span>
    </div>
  `;
}

function renderPolicyConditionSummary(expression) {
  const guided = parseGuidedConditionExpression(expression);
  if (guided?.field) {
    const field = GUIDED_CONDITION_FIELDS.find((item) => item.value === guided.field);
    return `<span>${escapeHtml(field?.label ?? guided.field)} ${escapeHtml(operatorLabel(guided.operator))} ${escapeHtml(guided.value)}</span>`;
  }
  return `<code>${escapeHtml(expression ?? "-")}</code>`;
}

/**
 * 渲染策略列表空态。
 * 空态保持与表格相同的稳定宽度，避免筛选变化造成编辑器位置跳动。
 */
function renderPolicyEmptyRow() {
  return `
    <div class="policy-row policy-empty" role="row">
      <span>当前筛选下没有策略。</span>
    </div>
  `;
}

/**
 * 读取 PolicyInfo 中的单条规则。
 * 兼容空对象只用于稳定错误态渲染，后端发现三对象不完整时仍应返回 policy.unavailable。
 */
function policyRule(policy) {
  return policy?.rule && typeof policy.rule === "object" ? policy.rule : {};
}

/**
 * 读取 PolicyInfo 中的单动作并兼容 action_type 字段。
 * 动作枚举只用于文本标签，未知值不会被拼入 class、属性名或可执行代码。
 */
function policyAction(policy) {
  const action = policy?.action && typeof policy.action === "object" ? policy.action : {};
  return {
    type: String(action.type ?? action.action_type ?? ""),
    target_model_id: action.target_model_id ? String(action.target_model_id) : null,
  };
}

/**
 * 查找 route_to 目标的可见标签。
 * 只在当前 active 模型集合中解析展示名；已失效目标显示固定提示并要求管理员重新选择。
 */
function policyModelLabel(activeModels, targetModelId) {
  const model = activeModels.find((item) => item.model_id === targetModelId);
  return model
    ? (model.display_name || model.name || "已配置模型")
    : "目标当前不可用";
}

/**
 * 渲染 select option。
 * value 与 label 分别执行属性/文本转义，避免模型显示名或后端字段打破表单边界。
 */
function renderOption(value, label, selectedValue) {
  const selected = String(value) === String(selectedValue ?? "") ? " selected" : "";
  return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(label)}</option>`;
}

/**
 * 格式化策略更新时间。
 * 页面只压缩 ISO 分隔符，不做本地时区推断，精确审计仍以后端 UTC 时间为准。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
}

/**
 * 转义 HTML 文本。
 * 名称、描述、表达式、模型、诊断和链路标识均可能来自外部输入，必须统一作为纯文本展示。
 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * 转义 HTML 属性值。
 * 在文本转义基础上处理反引号，保护 option value、datetime 和单个 policy_id 命令属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
