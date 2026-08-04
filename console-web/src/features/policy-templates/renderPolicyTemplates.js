/**
 * 模块职责：渲染 PAGE-095 策略模板目录、绑定、精确影响、确认应用和结果状态。
 * 安全边界：所有动态文本统一转义；nonce、CAS、hash、Token、tenant 和策略 DSL 不进入 DOM。
 */

// 兼容性状态仅负责展示后端裁决，不在浏览器侧据此补权限或推导可应用结论。
const STATUS_LABELS = Object.freeze({
  unbound: "待绑定",
  ready: "可应用",
  ready_with_warnings: "可应用，有提醒",
  incompatible: "不可应用",
});
// 动作标签只映射后端固定枚举，未知动作保持原值以暴露协议不兼容而非静默误导。
const ACTION_LABELS = Object.freeze({
  deny: "拒绝",
  mask: "脱敏",
  route_to: "定向路由",
  allow: "允许",
  log_only: "仅记录",
});

/**
 * 渲染策略模板页面。
 *
 * 参数 state 是状态机的公开快照；返回完整 panel HTML。未授权时返回空字符串，确保直接路由
 * 不产生模板名称、绑定选项、prefetch 或写按钮，真实 403 仍由后端执行。
 */
export function renderPolicyTemplates(state = {}) {
  if (!state.authorized) return "";
  const selected = state.selectedTemplate;
  return `
    <section class="policy-template-page" data-policy-template-page aria-labelledby="policy-template-title">
      <header class="policy-template-page__header">
        <div>
          <p class="eyebrow">PAGE-095</p>
          <h2 id="policy-template-title">行业与场景策略模板</h2>
        </div>
        <button class="icon-button policy-template-page__refresh" type="button"
          data-policy-template-action="refresh" title="刷新模板" aria-label="刷新模板"
          ${state.loadingCatalog || state.applying ? "disabled" : ""}>↻</button>
      </header>
      ${renderFeedback(state)}
      ${state.loadingCatalog && !state.templates?.length
        ? renderLoading()
        : renderWorkspace(state, selected)}
      ${renderConfirmation(state)}
    </section>
  `;
}

/** 渲染目录与详情的双栏工作区；移动端由 CSS 变为单列，不改变 DOM 操作顺序。 */
function renderWorkspace(state, selected) {
  if (!Array.isArray(state.templates) || state.templates.length === 0) {
    return `
      <div class="empty-state policy-template-page__empty">
        <strong>暂无可用模板</strong>
      </div>
    `;
  }
  return `
    <div class="policy-template-workspace">
      <nav class="policy-template-catalog" aria-label="策略模板目录">
        ${state.templates.map((item) => renderCatalogItem(item, state.selectedTemplateId)).join("")}
      </nav>
      <div class="policy-template-detail">
        ${selected ? renderSelectedTemplate(state, selected) : ""}
      </div>
    </div>
  `;
}

/** 渲染单条目录项；button 保持键盘和触屏原生语义。 */
function renderCatalogItem(template, selectedTemplateId) {
  const active = template.policyTemplateId === selectedTemplateId;
  return `
    <button class="policy-template-catalog__item${active ? " is-active" : ""}" type="button"
      data-policy-template-select="${escapeAttribute(template.policyTemplateId)}"
      aria-current="${active ? "page" : "false"}">
      <span class="policy-template-catalog__name">${escapeHtml(template.name)}</span>
      <span class="status-pill status-pill--${escapeAttribute(statusTone(template.compatibility.status))}">
        ${escapeHtml(STATUS_LABELS[template.compatibility.status] ?? "状态未知")}
      </span>
      <span class="policy-template-catalog__description">${escapeHtml(template.description)}</span>
    </button>
  `;
}

/** 渲染当前模板的绑定表单、兼容性摘要与精确预览。 */
function renderSelectedTemplate(state, template) {
  return `
    <header class="policy-template-detail__header">
      <div>
        <div class="policy-template-detail__meta">
          <span>${escapeHtml(categoryLabel(template.category))}</span>
          <span>v${escapeHtml(String(template.version))}</span>
          <span>优先级 ${escapeHtml(String(template.priority))}</span>
          <span>${escapeHtml(ACTION_LABELS[template.actionType] ?? template.actionType)}</span>
        </div>
        <h3>${escapeHtml(template.name)}</h3>
        <p>${escapeHtml(template.description)}</p>
      </div>
    </header>
    <form class="policy-template-bindings" data-policy-template-bindings novalidate>
      ${renderProjectBinding(state, template)}
      ${renderAliasBinding(state, template)}
      ${renderThresholdBinding(state, template)}
      <div class="policy-template-bindings__actions">
        <button class="button button--primary" type="submit"
          ${state.loadingPreview || state.applying ? "disabled" : ""}>
          ${state.loadingPreview ? "正在预览" : "影响预览"}
        </button>
      </div>
    </form>
    ${renderCompatibility(template.compatibility)}
    ${state.preview ? renderPreviewResult(state) : renderPreviewPlaceholder()}
  `;
}

/** 渲染必选 Project；选项只来自模板 API 的同租户最小目录。 */
function renderProjectBinding(state, template) {
  if (!template.requiredBindings.includes("project_id")) return "";
  return `
    <label class="field">
      <span>项目</span>
      <select name="project_id" data-policy-template-binding="project_id" required
        ${state.applying ? "disabled" : ""}>
        <option value="">请选择项目</option>
        ${template.bindingOptions.projects.map((option) => `
          <option value="${escapeAttribute(option.project_id)}"
            ${option.project_id === state.bindings.project_id ? "selected" : ""}>
            ${escapeHtml(option.name)}
          </option>
        `).join("")}
      </select>
    </label>
  `;
}

/** 仅高安全模板显示 Alias 选择；环境 code 是安全上下文而非可编辑字段。 */
function renderAliasBinding(state, template) {
  if (!template.requiredBindings.includes("model_alias_id")) return "";
  return `
    <label class="field">
      <span>模型别名</span>
      <select name="model_alias_id" data-policy-template-binding="model_alias_id" required
        ${state.applying ? "disabled" : ""}>
        <option value="">请选择模型别名</option>
        ${template.bindingOptions.modelAliases.map((option) => `
          <option value="${escapeAttribute(option.model_alias_id)}"
            ${option.model_alias_id === state.bindings.model_alias_id ? "selected" : ""}>
            ${escapeHtml(option.alias)}${option.environment_code ? ` · ${escapeHtml(option.environment_code)}` : ""}
          </option>
        `).join("")}
      </select>
    </label>
  `;
}

/** 仅预算防爆模板显示 CNY Decimal 阈值；浏览器不查询或展示当前预算余额。 */
function renderThresholdBinding(state, template) {
  if (!template.requiredBindings.includes("threshold_cny")) return "";
  return `
    <label class="field">
      <span>预算阈值（CNY）</span>
      <input type="text" name="threshold_cny" data-policy-template-binding="threshold_cny"
        inputmode="decimal" autocomplete="off" maxlength="32" required
        value="${escapeAttribute(state.bindings.threshold_cny ?? "")}"
        ${state.applying ? "disabled" : ""}>
    </label>
  `;
}

/** 渲染目录级或预览级适配结论；原因码是后端固定枚举，不替代实际授权。 */
function renderCompatibility(compatibility) {
  const reasons = Array.isArray(compatibility.reasonCodes) ? compatibility.reasonCodes : [];
  const warnings = Array.isArray(compatibility.warningCodes) ? compatibility.warningCodes : [];
  if (!reasons.length && !warnings.length) return "";
  return `
    <section class="policy-template-compatibility" aria-label="适配结论">
      ${reasons.map((code) => `
        <p class="policy-template-compatibility__reason">${escapeHtml(reasonLabel(code))}</p>
      `).join("")}
      ${warnings.map((code) => `
        <p class="policy-template-compatibility__warning">${escapeHtml(reasonLabel(code))}</p>
      `).join("")}
    </section>
  `;
}

/** 根据精确响应选择影响表或无写凭证的 incompatible 阻断态。 */
function renderPreviewResult(state) {
  return state.preview.impact
    ? renderImpact(state)
    : renderIncompatiblePreview(state.preview.template.compatibility);
}

/** 渲染精确影响；完整求值顺序由后端给出，页面只读展示。 */
function renderImpact(state) {
  const preview = state.preview;
  const impact = preview.impact;
  const canApply = state.canApplyPermission && preview.template.compatibility.canApply;
  const remediation = remediationFor(preview.template.compatibility.reasonCodes);
  return `
    <section class="policy-template-impact" aria-labelledby="policy-template-impact-title">
      <header class="policy-template-impact__header">
        <div>
          <h4 id="policy-template-impact-title">影响预览</h4>
          <p>${escapeHtml(String(impact.before_policy_count))} 条 → ${escapeHtml(String(impact.after_policy_count))} 条策略</p>
        </div>
        <span class="status-pill status-pill--${escapeAttribute(statusTone(preview.template.compatibility.status))}">
          ${escapeHtml(STATUS_LABELS[preview.template.compatibility.status] ?? "状态未知")}
        </span>
      </header>
      ${renderCompatibility(preview.template.compatibility)}
      <div class="policy-template-impact__table-wrap">
        <table class="data-table policy-template-impact__table">
          <thead><tr><th>顺序</th><th>策略</th><th>优先级</th><th>动作</th><th>影响</th></tr></thead>
          <tbody>
            ${impact.evaluation_order.map((item, index) => `
              <tr class="${item.generated ? "is-generated" : ""}">
                <td>${index + 1}</td>
                <td>${item.generated ? "本次模板" : escapeHtml(shortId(item.policy_id))}</td>
                <td>${escapeHtml(String(item.priority))}</td>
                <td>${escapeHtml(ACTION_LABELS[item.action_type] ?? item.action_type)}</td>
                <td>${escapeHtml(shadowingLabel(item.shadowing))}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
      <dl class="policy-template-impact__facts">
        <div><dt>回滚基线</dt><dd>${escapeHtml(shortId(impact.rollback_target_snapshot_id))}</dd></div>
        <div><dt>目标版本</dt><dd>v${escapeHtml(String(impact.expected_head_version + 1))}</dd></div>
        ${impact.derived_target_model_id
          ? `<div><dt>目标模型</dt><dd>${escapeHtml(shortId(impact.derived_target_model_id))}</dd></div>`
          : ""}
        ${impact.allowed_model_alias_names?.length
          ? `<div><dt>允许别名</dt><dd>${impact.allowed_model_alias_names.map(escapeHtml).join("、")}</dd></div>`
          : ""}
      </dl>
      <div class="policy-template-impact__actions">
        ${canApply
          ? `<button class="button button--danger" type="button" data-policy-template-action="confirm">应用模板</button>`
          : remediation
            ? `<button class="button button--secondary" type="button"
                data-policy-template-remediation="${escapeAttribute(remediation.target)}">
                ${escapeHtml(remediation.label)}
              </button>`
            : `<span class="policy-template-impact__readonly">当前权限或适配状态不允许应用</span>`}
      </div>
    </section>
  `;
}

/**
 * 渲染无法构造影响图的阻断态。
 *
 * 后端此时刻意不签发 nonce/CAS；页面只展示稳定原因，并把修复按钮限制为后端已授权的现有导航。
 */
function renderIncompatiblePreview(compatibility) {
  const remediation = remediationFor(compatibility.reasonCodes);
  return `
    <section class="policy-template-impact policy-template-impact--incompatible"
      aria-labelledby="policy-template-impact-title">
      <header class="policy-template-impact__header">
        <div>
          <h4 id="policy-template-impact-title">影响预览</h4>
          <p>当前依赖条件不足，未生成可应用计划</p>
        </div>
        <span class="status-pill status-pill--danger">不可应用</span>
      </header>
      ${renderCompatibility(compatibility)}
      <div class="policy-template-impact__actions">
        ${remediation
          ? `<button class="button button--secondary" type="button"
              data-policy-template-remediation="${escapeAttribute(remediation.target)}">
              ${escapeHtml(remediation.label)}
            </button>`
          : `<span class="policy-template-impact__readonly">修复依赖后重新预览</span>`}
      </div>
    </section>
  `;
}

/** 尚未精确预览时保留稳定高度，避免表单交互导致布局跳动。 */
function renderPreviewPlaceholder() {
  return `
    <section class="policy-template-impact policy-template-impact--empty" aria-label="影响预览">
      <span>尚无影响预览</span>
    </section>
  `;
}

/** 渲染确认对话框；原因随关闭、离页或成功应用立即销毁。 */
function renderConfirmation(state) {
  if (!state.confirmation || !state.preview) return "";
  const template = state.preview.template;
  const impact = state.preview.impact;
  const generatedPolicy = impact.evaluation_order.find((item) => item.generated);
  const project = template.bindingOptions.projects.find(
    (item) => item.project_id === impact.project_id,
  );
  const warningLabels = template.compatibility.warningCodes.map(reasonLabel);
  const warningText = warningLabels.length
    ? `${warningLabels.join("；")}（${impact.shadowing_policy_ids.length} 条现有策略）`
    : "无";
  return `
    <div class="modal-backdrop" data-policy-template-modal>
      <section class="modal policy-template-confirm" role="dialog" aria-modal="true"
        aria-labelledby="policy-template-confirm-title">
        <header class="modal__header">
          <div>
            <p class="eyebrow">关键命令</p>
            <h3 id="policy-template-confirm-title">应用${escapeHtml(template.name)}</h3>
          </div>
          <button class="icon-button" type="button" aria-label="关闭"
            data-policy-template-action="close" ${state.applying ? "disabled" : ""}>×</button>
        </header>
        <dl class="policy-template-confirm__summary">
          <div><dt>适配状态</dt><dd>${escapeHtml(STATUS_LABELS[template.compatibility.status] ?? "状态未知")}</dd></div>
          <div><dt>受影响 Project</dt><dd>${escapeHtml(project?.name ?? impact.project_id)} <code>${escapeHtml(impact.project_id)}</code></dd></div>
          <div><dt>生成 Policy</dt><dd><code>${escapeHtml(generatedPolicy?.policy_id ?? "-")}</code></dd></div>
          <div><dt>动作</dt><dd>${escapeHtml(ACTION_LABELS[generatedPolicy?.action_type] ?? generatedPolicy?.action_type ?? "-")}</dd></div>
          <div><dt>优先级</dt><dd>${escapeHtml(String(generatedPolicy?.priority ?? "-"))}</dd></div>
          <div><dt>策略数量</dt><dd>${escapeHtml(String(impact.before_policy_count))} → ${escapeHtml(String(impact.after_policy_count))}</dd></div>
          <div><dt>新 Snapshot</dt><dd>v${escapeHtml(String(impact.expected_head_version + 1))}</dd></div>
          <div><dt>回滚目标</dt><dd><code>${escapeHtml(impact.rollback_target_snapshot_id)}</code></dd></div>
          <div class="policy-template-confirm__warning"><dt>遮蔽提醒</dt><dd>${escapeHtml(warningText)}</dd></div>
        </dl>
        <label class="field">
          <span>变更原因</span>
          <textarea name="reason" data-policy-template-reason maxlength="256" rows="4"
            ${state.applying ? "disabled" : ""}>${escapeHtml(state.confirmation.reason ?? "")}</textarea>
        </label>
        <footer class="modal__actions">
          <button class="button button--secondary" type="button" data-policy-template-action="close"
            ${state.applying ? "disabled" : ""}>取消</button>
          <button class="button button--danger" type="button" data-policy-template-action="apply"
            ${state.applying || !String(state.confirmation.reason ?? "").trim() ? "disabled" : ""}>
            ${state.applying ? "正在应用" : "确认应用"}
          </button>
        </footer>
      </section>
    </div>
  `;
}

/** 渲染统一错误或成功结果；只展示公开 request_id，不展示 hash、nonce 或绑定正文。 */
function renderFeedback(state) {
  if (state.pageError) {
    return `
      <div class="feedback feedback--error" role="alert">
        <strong>${escapeHtml(state.pageError.message)}</strong>
        ${state.pageError.suggestion ? `<span>${escapeHtml(state.pageError.suggestion)}</span>` : ""}
        <code>${escapeHtml(state.pageError.requestId ?? "-")}</code>
      </div>
    `;
  }
  if (state.result) {
    return `
      <div class="feedback feedback--success policy-template-result" role="status">
        <strong>模板已应用</strong>
        <dl class="policy-template-result__facts">
          <div><dt>Application ID</dt><dd><code>${escapeHtml(state.result.applicationId)}</code></dd></div>
          <div><dt>Snapshot</dt><dd><code>${escapeHtml(state.result.snapshotId)}</code> · v${escapeHtml(String(state.result.snapshotVersion))}</dd></div>
        </dl>
        <button class="button button--secondary" type="button" data-policy-template-action="open-snapshot"
          data-snapshot-id="${escapeAttribute(state.result.snapshotId)}">查看策略快照</button>
      </div>
    `;
  }
  return "";
}

/** 首次目录读取使用稳定骨架，不以动态文字改变工具区尺寸。 */
function renderLoading() {
  return `
    <div class="policy-template-loading" role="status" aria-label="正在加载策略模板">
      <span></span><span></span><span></span><span></span><span></span>
    </div>
  `;
}

/** 将 compatibility 状态映射为现有 status-pill 色调。 */
function statusTone(status) {
  if (status === "ready") return "success";
  if (status === "ready_with_warnings" || status === "unbound") return "warning";
  return "danger";
}

/** 固定类别中文标签；未知值按安全短文本展示。 */
function categoryLabel(value) {
  return ({ cost: "成本", security: "安全", budget: "预算", privacy: "隐私" })[value] ?? value;
}

/** 固定遮蔽结论中文标签。 */
function shadowingLabel(value) {
  return ({ none: "无", warning: "提醒", blocking: "阻断" })[value] ?? "未知";
}

/** 原因码转为紧凑中文；未知固定码仍可供支持人员定位。 */
function reasonLabel(code) {
  const labels = {
    bindings_required: "请选择模板要求的绑定",
    snapshot_baseline_required: "需要先发布策略基线",
    missing_project: "项目不可用",
    missing_model_alias: "模型别名不可用",
    no_eligible_model: "暂无合格模型",
    missing_budget_context: "预算上下文不可用",
    policy_mask_capability_required: "数据面脱敏能力未就绪",
    shadowed_by_policy: "现有策略会改变模板效果",
    policy_shadowing_warning: "现有策略会改变模板效果",
    active_template_application_exists: "该项目已有未撤销的模板应用",
    policy_quota_exceeded: "活跃策略数量已达到上限",
    policy_shadowing_conflict: "现有策略会阻断或绕过模板效果",
    active_environment_required: "需要至少一个可用环境",
    active_model_required: "需要至少一个可用模型",
    active_virtual_key_required: "项目需要可用的 Virtual Key",
    budget_coverage_required: "项目与 Virtual Key 需要有效预算覆盖",
    domestic_alias_coverage_required: "所有可用环境都需要同名国产模型别名",
    domestic_alias_limit_exceeded: "国产模型别名数量超过模板上限",
    domestic_provider_required: "模型必须来自允许的中国境内官方或云服务 Provider",
    eligible_low_cost_model_required: "暂无同时满足能力、健康和价格要求的模型",
    environment_limit_exceeded: "可用环境数量超过模板上限",
    policy_mask_runtime_required: "Gateway 脱敏运行能力尚未全部就绪",
    provider_adapter_required: "候选 Provider Adapter 当前不可用",
    domestic_expression_limit_exceeded: "国产模型条件超过安全表达式上限",
  };
  return labels[code] ?? String(code).replaceAll("_", " ");
}

/** 把固定阻断原因映射到已有治理页；未知原因不生成任意导航目标。 */
function remediationFor(reasonCodes) {
  const codes = new Set(Array.isArray(reasonCodes) ? reasonCodes : []);
  const choices = [
    ["snapshot_baseline_required", "policy-snapshots", "前往策略快照"],
    ["active_environment_required", "environments", "前往环境管理"],
    ["environment_limit_exceeded", "environments", "前往环境管理"],
    ["provider_adapter_required", "providers", "前往 Provider 管理"],
    ["domestic_provider_required", "providers", "前往 Provider 管理"],
    ["active_model_required", "models", "前往模型目录"],
    ["eligible_low_cost_model_required", "models", "前往模型目录"],
    ["domestic_alias_coverage_required", "model-aliases", "前往模型别名"],
    ["domestic_alias_limit_exceeded", "model-aliases", "前往模型别名"],
    ["domestic_expression_limit_exceeded", "model-aliases", "前往模型别名"],
    ["active_virtual_key_required", "virtual-keys", "前往 Virtual Key"],
    ["budget_coverage_required", "budgets", "前往预算中心"],
    ["policy_mask_runtime_required", "system-status", "前往系统状态"],
    ["policy_quota_exceeded", "policies", "前往策略管理"],
    ["policy_shadowing_conflict", "policies", "前往策略管理"],
    ["active_template_application_exists", "policies", "前往策略管理"],
  ];
  const match = choices.find(([code]) => codes.has(code));
  return match ? { target: match[1], label: match[2] } : null;
}

/** 资源 ID 仅展示首尾，避免紧凑视图溢出；完整 ID 不用于按钮文案。 */
function shortId(value) {
  const normalized = String(value ?? "");
  return normalized.length > 20 ? `${normalized.slice(0, 10)}…${normalized.slice(-6)}` : normalized;
}

/** 转义 HTML 文本节点。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 属性值与文本使用同一完整转义，阻止后端短文本突破 DOM。 */
function escapeAttribute(value) {
  return escapeHtml(value);
}
