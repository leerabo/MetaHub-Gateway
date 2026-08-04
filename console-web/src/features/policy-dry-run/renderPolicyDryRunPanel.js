/**
 * 模块职责：渲染策略模拟器、维护非敏感选择状态，并提供身份/异步响应门禁。
 * 安全边界：本模块不保存 Prompt；所有后端值先转义，前端权限快照不替代后端权限校验。
 */

import {
  authorizationIdentitySnapshot,
  hasPermission,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 状态常量只用于前端失败关闭与展示过滤，真实权限及租户归属始终由后端重新校验。
const ACTIVE_STATUS = "active";
// 覆盖项只按正式 DTO 的固定键翻译，避免把后端未知字符串直接当作产品文案或 HTML 结构。
const COVERAGE_LABELS = Object.freeze({
  policy: "Policy",
  model_provider_routing: "模型与 Provider 配置路由",
  chat_capability: "Chat 能力",
  dlp: "DLP",
  tenant_project_budget: "租户与 Project 预算",
  virtual_key_application_budget: "Virtual Key / Application 预算",
  rpm_tpm_concurrency_limits: "RPM / TPM / 并发限流",
  provider_credentials_keypool: "Provider 凭据与 KeyPool",
  real_provider: "真实 Provider",
  application_budget: "Application 预算",
  virtual_key_budget: "Virtual Key 预算",
  real_provider_call: "真实 Provider 调用",
});
// 本期“已模拟”集合必须显式列举，未实现的 KeyPool、真实 Provider 等能力只能显示为未覆盖。
const SIMULATED_COVERAGE_KEYS = Object.freeze([
  "policy",
  "model_provider_routing",
  "chat_capability",
  "dlp",
  "tenant_project_budget",
]);

/**
 * 创建不含 Prompt 的页面状态。
 * Prompt 只允许驻留真实 textarea；结果也只能是后端已经脱敏的 PolicyExplanation。
 */
export function createEmptyPolicyDryRunState() {
  return {
    status: "idle",
    environmentId: "",
    projectId: "",
    model: "",
    dataClass: "confidential",
    maxOutputTokens: "512",
    result: null,
    error: null,
  };
}

/**
 * 从认证上下文建立 tenant+user+role+session 身份快照。
 * 任一字段变化都必须使旧请求失效，避免跨身份恢复解释结果。
 */
export function policyDryRunIdentitySnapshot(authContext) {
  // 模拟器 owner 绑定 policy.simulate、revision 与指纹，降权后旧 Prompt 结果不能写回页面。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.POLICY_SIMULATE);
}

/**
 * 创建单次提交生命周期。
 * begin 会取消旧请求；isCurrent 同时核对 revision、身份和导航，旧响应不得写回 DOM。
 */
export function createPolicyDryRunLifecycle() {
  let revision = 0;
  let current = null;
  return {
    begin({ identity, navigation }) {
      revision += 1;
      current?.controller.abort();
      const controller = new AbortController();
      current = {
        revision,
        identity: text(identity),
        navigation: text(navigation),
        controller,
        signal: controller.signal,
      };
      return current;
    },
    isCurrent(operation, owner) {
      return Boolean(
        operation
        && current === operation
        && operation.revision === revision
        && !operation.signal.aborted
        && operation.identity === text(owner?.identity)
        && operation.navigation === text(owner?.navigation),
      );
    },
    finish(operation) {
      if (current === operation) current = null;
    },
    invalidate() {
      revision += 1;
      current?.controller.abort();
      current = null;
    },
    isBusy() {
      return Boolean(current);
    },
  };
}

/**
 * 判断当前后端导航和身份是否允许展示/提交模拟器。
 * 该函数只是前端防旧响应门禁，POST 接口仍必须使用 ``require_policy_simulator``。
 */
export function shouldOwnPolicyDryRun({ canSimulate, activeNavigationItemId, identity }) {
  return canSimulate === true
    && activeNavigationItemId === "policy-simulator"
    && Boolean(identity);
}

/**
 * 渲染策略模拟器。
 * 上下文列表只消费当前刷新已授权数据；Prompt 不从 state/result 恢复，也不写入 value 属性。
 */
export function renderPolicyDryRunPanel(state, context, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.POLICY_SIMULATE)) return "";
  const safeState = state ?? createEmptyPolicyDryRunState();
  const environments = activeEnvironments(context?.environmentState);
  const projects = activeProjects(context?.projectState);
  const models = selectableModels(
    context?.modelAliasesState,
    context?.modelsState,
    safeState.environmentId,
    environments,
  );
  const busy = safeState.status === "submitting";
  return `
    <div class="policy-dry-run-heading">
      <div>
        <h2 id="policy-dry-run-title">策略模拟器</h2>
        <p>当前 active 候选</p>
      </div>
      <span class="policy-dry-run-status" data-status="${escapeAttribute(safeState.status)}" tabindex="-1">${statusLabel(safeState.status)}</span>
    </div>
    ${renderError(safeState.error)}
    <form id="policy-dry-run-form" autocomplete="off">
      <fieldset ${busy ? "disabled" : ""}>
        <legend>请求上下文</legend>
        <label><span>Environment</span>
          <select name="environmentId" required>${renderOptions(environments, safeState.environmentId, "environment_id", environmentLabel, "请选择 Environment")}</select>
        </label>
        <label><span>Project</span>
          <select name="projectId" required>${renderOptions(projects, safeState.projectId, "project_id", projectLabel, "请选择 Project")}</select>
        </label>
        <label><span>模型</span>
          <select name="model" required>${renderOptions(models, safeState.model, "value", (item) => item.label, "请选择模型")}</select>
        </label>
        <label><span>数据等级</span>
          <input name="dataClass" maxlength="64" required value="${escapeAttribute(safeState.dataClass)}" />
        </label>
        <label><span>最大输出 Token</span>
          <input name="maxOutputTokens" type="number" min="1" max="131072" step="1" value="${escapeAttribute(safeState.maxOutputTokens)}" />
        </label>
        <label class="policy-dry-run-prompt"><span>Prompt</span>
          <textarea name="prompt" maxlength="20000" rows="7" required spellcheck="false" autocomplete="off"></textarea>
        </label>
      </fieldset>
      <div class="policy-dry-run-actions">
        <button type="submit" ${busy ? "disabled" : ""}>${busy ? "预演中" : "运行预演"}</button>
        <button type="button" data-policy-dry-run-clear ${busy ? "disabled" : ""}>清空</button>
        <span role="status" aria-live="polite">${busy ? "正在计算" : ""}</span>
      </div>
    </form>
    ${renderExplanation(safeState.result)}
  `;
}

/**
 * 从表单读取非敏感选择，供主应用在局部重绘后保留。
 * 返回值故意没有 Prompt 字段，调用方必须只在 submit 当前栈单独读取 Prompt。
 */
export function policyDryRunSelectionsFromForm(form) {
  const data = new FormData(form);
  return {
    environmentId: text(data.get("environmentId")),
    projectId: text(data.get("projectId")),
    model: text(data.get("model")),
    dataClass: text(data.get("dataClass")),
    maxOutputTokens: text(data.get("maxOutputTokens")),
  };
}

/** 渲染后端统一错误，不包含请求正文或浏览器异常对象的字符串化结果。 */
function renderError(error) {
  if (!error) return "";
  return `<div class="policy-dry-run-error" role="alert" tabindex="-1">
    <strong>${escapeHtml(error.code ?? "policy.dry_run_unavailable")}</strong>
    <span>${escapeHtml(error.message ?? "策略预演暂不可用")}</span>
    <code>request_id: ${escapeHtml(error.requestId ?? error.request_id ?? "-")}</code>
  </div>`;
}

/** 渲染完整解释，所有分区均为同级带状布局，避免卡片嵌套。 */
function renderExplanation(result) {
  const explanation = result?.policy_explanation;
  if (!explanation) return `<div class="policy-dry-run-empty" aria-live="polite"></div>`;
  const compatible = explanation.runtime_compatible === true;
  const coverageComplete = explanation.coverage_complete === true;
  return `<div class="policy-dry-run-result" aria-live="polite">
    <section class="policy-dry-run-summary" aria-labelledby="policy-dry-run-summary-title">
      <div><h3 id="policy-dry-run-summary-title" tabindex="-1">预演结果</h3><strong>${escapeHtml(outcomeLabel(explanation.outcome))}</strong></div>
      <dl>
        <div><dt>Policy 决策</dt><dd>${escapeHtml(explanation.policy?.decision ?? explanation.decision ?? "-")}</dd></div>
        <div><dt>模拟错误</dt><dd><code>${escapeHtml(explanation.would_error_code ?? "无")}</code></dd></div>
        <div><dt>解释 ID</dt><dd><code>${escapeHtml(explanation.policy_explanation_id ?? "-")}</code></dd></div>
        <div><dt>request_id</dt><dd><code>${escapeHtml(result.request_id ?? "-")}</code></dd></div>
        <div><dt>trace_id</dt><dd><code>${escapeHtml(result.trace_id ?? "-")}</code></dd></div>
      </dl>
    </section>
    <section class="policy-dry-run-compatibility" aria-labelledby="policy-dry-run-compatibility-title">
      <h3 id="policy-dry-run-compatibility-title">兼容性与覆盖</h3>
      <div class="policy-dry-run-flags">
        <span data-kind="${compatible ? "ok" : "warning"}">${compatible ? "当前发布合同兼容" : "当前发布合同不兼容"}</span>
        <span data-kind="${coverageComplete ? "ok" : "partial"}">${coverageComplete ? "完整覆盖" : "部分覆盖"}</span>
      </div>
      <p><code>${escapeHtml((explanation.runtime_incompatibility_codes ?? []).join(", ") || "none")}</code></p>
      ${renderCompatibilityFacts(explanation)}
      ${renderCoverage(explanation.coverage)}
    </section>
    ${renderDecisionTree(explanation.decision_tree)}
    ${renderPolicyChecks(explanation.policy)}
    <div class="policy-dry-run-governance-grid">
      ${renderRoute(explanation.route)}
      ${renderDlp(explanation.dlp)}
      ${renderBudget(explanation.budget)}
    </div>
  </div>`;
}

/** 渲染候选来源和发布快照对照，明确兼容性与 Gateway 完整等价不是同一结论。 */
function renderCompatibilityFacts(explanation) {
  return `<dl class="policy-dry-run-contract">
    <div><dt>候选来源</dt><dd>${escapeHtml(explanation?.policy_source ?? "active_candidate")}</dd></div>
    <div><dt>已发布版本</dt><dd>${escapeHtml(explanation?.published_snapshot_version ?? "无")}</dd></div>
    <div><dt>Gateway 等价度</dt><dd>${escapeHtml(explanation?.gateway_parity ?? "partial")}</dd></div>
  </dl>`;
}

/** 渲染正式覆盖矩阵，不把未识别对象或任意 JSON 直接序列化到 DOM。 */
function renderCoverage(coverage) {
  const declaredSimulated = stringList(coverage?.simulated ?? coverage?.evaluated_stages);
  const simulatedKeys = declaredSimulated.length
    ? declaredSimulated
    : SIMULATED_COVERAGE_KEYS.filter((key) => coverage?.[key] === true);
  const declaredMissing = stringList(
    coverage?.not_simulated ?? coverage?.not_evaluated ?? coverage?.uncovered_scopes,
  );
  const missingKeys = declaredMissing.length
    ? declaredMissing
    : Object.keys(COVERAGE_LABELS).filter((key) => coverage?.[key] === false);
  const simulated = simulatedKeys.map(coverageLabel).filter(Boolean);
  const missing = missingKeys.map(coverageLabel).filter(Boolean);
  return `<dl class="policy-dry-run-coverage">
    <div><dt>已模拟</dt><dd>${escapeHtml(simulated.join(" / ") || "policy / routing / DLP / tenant+project budget")}</dd></div>
    <div><dt>未覆盖</dt><dd>${escapeHtml(missing.join(" / ") || "Virtual Key / Application / KeyPool / 限流")}</dd></div>
  </dl>`;
}

/** 根据 parent_id 渲染有界决策节点；未知字段只作为转义文本，不生成动态 HTML 属性名。 */
function renderDecisionTree(tree) {
  const nodes = Array.isArray(tree?.nodes)
    ? tree.nodes
      .slice(0, 256)
      .map((node, index) => ({ node, index }))
      .sort((left, right) => safeNodeOrder(left.node, left.index) - safeNodeOrder(right.node, right.index))
      .map((entry) => entry.node)
    : [];
  if (!nodes.length) return "";
  const nodesById = new Map();
  for (const node of nodes) {
    const nodeId = text(node?.node_id);
    if (nodeId && !nodesById.has(nodeId)) nodesById.set(nodeId, node);
  }
  return `<section class="policy-dry-run-tree" aria-labelledby="policy-dry-run-tree-title">
    <h3 id="policy-dry-run-tree-title">策略决策树</h3>
    <ol>${nodes.map((node) => `<li data-depth="${safeTreeDepth(node, nodesById)}">
      <span>${escapeHtml(node.node_type ?? node.stage ?? "node")}</span>
      <strong>${escapeHtml(node.status ?? (node.matched === true ? "matched" : "checked"))}</strong>
      ${node.priority == null ? "" : `<small>P${escapeHtml(node.priority)}</small>`}
      ${node.action ? `<code>${escapeHtml(node.action)}</code>` : ""}
      ${node.reason_code ? `<code>${escapeHtml(node.reason_code)}</code>` : ""}
    </li>`).join("")}</ol>
  </section>`;
}

/** 渲染 Policy 检查表，明确规则 ID、优先级、动作和命中结果。 */
function renderPolicyChecks(policy) {
  const checks = Array.isArray(policy?.policy_checks) ? policy.policy_checks.slice(0, 200) : [];
  return `<section class="policy-dry-run-rules" aria-labelledby="policy-dry-run-rules-title">
    <h3 id="policy-dry-run-rules-title">命中规则</h3>
    ${checks.length ? `<div class="policy-dry-run-rule-table" role="table" aria-label="Policy 检查结果">
      <div role="row" class="policy-dry-run-table-head"><span role="columnheader">规则 ID</span><span role="columnheader">优先级</span><span role="columnheader">动作</span><span role="columnheader">结果</span><span role="columnheader">顺序</span></div>
      ${checks.map((item) => `<div role="row"><code role="cell" data-label="规则 ID">${escapeHtml(item.policy_rule_id ?? item.rule_id ?? "-")}</code><span role="cell" data-label="优先级">${escapeHtml(item.priority ?? "-")}</span><code role="cell" data-label="动作">${escapeHtml(item.action_type ?? item.action ?? "-")}</code><strong role="cell" data-label="结果">${item.matched === true ? "命中" : "未命中"}</strong><span role="cell" data-label="顺序">${escapeHtml(item.match_order ?? "-")}</span></div>`).join("")}
    </div>` : `<p>没有已检查的 active 规则</p>`}
  </section>`;
}

/** 渲染配置路由结果，并显式保留策略版本、价格时点与 credential 未评估边界。 */
function renderRoute(route) {
  const candidates = Array.isArray(route?.routing_candidates)
    ? route.routing_candidates.slice(0, 2)
    : Array.isArray(route?.candidates)
      ? route.candidates.slice(0, 2)
      : [];
  return `<section aria-labelledby="policy-dry-run-route-title"><h3 id="policy-dry-run-route-title">路由</h3><dl>
    <div><dt>状态</dt><dd>${escapeHtml(route?.status ?? "not_evaluated")}</dd></div>
    <div><dt>模型</dt><dd>${escapeHtml(route?.internal_model_id ?? route?.selected_internal_model_id ?? route?.selected_model_id ?? "-")}</dd></div>
    <div><dt>Provider</dt><dd>${escapeHtml(route?.provider_code ?? "-")}</dd></div>
    <div><dt>路由模式</dt><dd>${escapeHtml(route?.routing_mode ?? route?.mode ?? "-")}</dd></div>
    <div><dt>策略 ID</dt><dd><code>${escapeHtml(route?.routing_policy_id ?? route?.policy_id ?? "-")}</code></dd></div>
    <div><dt>策略版本</dt><dd>${escapeHtml(versionLabel(route?.routing_policy_version ?? route?.policy_version))}</dd></div>
    <div><dt>价格时点</dt><dd>${escapeHtml(route?.price_as_of ?? "未进行成本比较")}</dd></div>
    <div><dt>Token 估算</dt><dd>${escapeHtml(tokenEstimateLabel(route))}</dd></div>
    <div><dt>Chat 能力</dt><dd>${route?.chat_capability_supported == null ? "not_evaluated" : route.chat_capability_supported ? "支持" : "不支持"}</dd></div>
    <div><dt>凭据</dt><dd>${escapeHtml(route?.provider_credential_status ?? "not_evaluated")}</dd></div>
    <div><dt>选择原因</dt><dd><code>${escapeHtml(route?.selection_reason ?? route?.reason_code ?? route?.routing_reason ?? "-")}</code></dd></div>
  </dl>${renderRouteCandidates(candidates)}</section>`;
}

/** 渲染最多两个冻结候选；字段白名单不包含 URL、auth_type、Key 或健康原始错误。 */
function renderRouteCandidates(candidates) {
  if (!candidates.length) return "";
  return `<div class="policy-dry-run-candidate-table" role="table" aria-label="路由候选">
    <div role="row" class="policy-dry-run-table-head"><span role="columnheader">排名</span><span role="columnheader">角色</span><span role="columnheader">模型</span><span role="columnheader">Provider</span><span role="columnheader">资格</span><span role="columnheader">健康</span><span role="columnheader">KeyPool</span><span role="columnheader">有效价格 / 成本</span><span role="columnheader">原因</span></div>
    ${candidates.map((item) => `<div role="row">
      <span role="cell" data-label="排名">${escapeHtml(item.rank ?? "-")}</span>
      <span role="cell" data-label="角色">${escapeHtml(candidateRoleLabel(item.candidate_role ?? item.role))}</span>
      <code role="cell" data-label="模型">${escapeHtml(item.internal_model_id ?? item.model_id ?? "-")}</code>
      <span role="cell" data-label="Provider">${escapeHtml(item.provider_code ?? item.provider_id ?? "-")}</span>
      <strong role="cell" data-label="资格">${escapeHtml(candidateEligibilityLabel(item))}</strong>
      <span role="cell" data-label="健康">${escapeHtml(candidateHealthLabel(item))}</span>
      <span role="cell" data-label="KeyPool">${escapeHtml(item.keypool_state ?? item.keypool_status ?? item.key_pool_status ?? "not_evaluated")}</span>
      <code role="cell" data-label="有效价格 / 成本">${escapeHtml(candidatePriceLabel(item))}</code>
      <code role="cell" data-label="原因">${escapeHtml(item.selection_reason ?? item.reason_code ?? "-")}</code>
    </div>`).join("")}
  </div>`;
}

/** 策略版本为空时保持短横线，不把 implicit fixed 伪造成 v0。 */
function versionLabel(value) {
  return value === null || value === undefined || value === "" ? "-" : `v${value}`;
}

/** Token 估算仅在成本比较发生时展示；0 是有效值，不能与未评估混淆。 */
function tokenEstimateLabel(route) {
  const input = route?.estimated_input_tokens;
  const output = route?.estimated_output_tokens;
  if (input === null || input === undefined || output === null || output === undefined) {
    return "未评估";
  }
  // 截断标记属于估算事实，必须与输入/输出数量一起呈现，不能静默当作完整估算。
  return `${input} / ${output}${route?.token_estimate_truncated ? " / 已截断" : ""}`;
}

/** 候选角色描述配置身份；初始选中 fallback 角色不等于触发了 PRD-036 Fallback。 */
function candidateRoleLabel(role) {
  return role === "fallback" ? "备用角色（初始候选）" : role === "default" ? "默认角色" : role ?? "-";
}

/** 合并 eligible 和冻结状态为稳定短标签，未知枚举保持可见但不推断为合格。 */
function candidateEligibilityLabel(item) {
  if (item?.eligible === true) return item.status === "selected" ? "合格 / 已选" : "合格";
  if (item?.eligible === false) return "已剔除";
  const labels = { selected: "合格 / 已选", standby: "合格 / 备用", rejected: "已剔除" };
  return labels[item?.status] ?? item?.eligibility ?? item?.status ?? "未知";
}

/** 健康分类与新鲜度共同展示；stale 不能仅显示 healthy 造成可用性误判。 */
function candidateHealthLabel(item) {
  const state = item?.health_state
    ?? item?.provider_health_status
    ?? item?.health_status
    ?? item?.health
    ?? "not_evaluated";
  // 明确 true/1 兼容 JSON 布尔与 SQLite 整数投影，均表示新鲜证据。
  if (item?.health_fresh === true || item?.health_fresh === 1) return `${state} / fresh`;
  // 明确 false/0 表示 stale；null 则保持仅展示健康分类。
  if (item?.health_fresh === false || item?.health_fresh === 0) return `${state} / stale`;
  return state;
}

/**
 * 格式化 Decimal 价格事实，不使用 Number/float 重算。
 * 字符串或数值 0 都是有效零价；只有 null/undefined 才显示缺价，not_evaluated 单独显示未评估。
 */
function candidatePriceLabel(item) {
  const currency = item?.currency ?? item?.price_currency ?? "-";
  // 零金额同样满足非空判断，必须展示原始 Decimal 文本而不是落入“缺价”。
  if (item?.estimated_cost_amount !== null && item?.estimated_cost_amount !== undefined) {
    // 排序与持久化使用后端量化值，浏览器只拼接币种，不能再次舍入。
    return `${item.estimated_cost_amount} ${currency}`;
  }
  const input = item?.input_price_per_1k;
  const output = item?.output_price_per_1k;
  if (input !== null && input !== undefined || output !== null && output !== undefined) {
    return `${input ?? "-"} / ${output ?? "-"} ${currency} / 1K`;
  }
  if (item?.price_status === "not_evaluated" || item?.price_source === "not_evaluated") {
    return "未评估";
  }
  return "缺价";
}

/** 渲染 DLP 安全摘要，后端合同禁止 pattern、命中文本和 hash。 */
function renderDlp(dlp) {
  const source = dlp?.matches ?? dlp?.matched_rules;
  const matches = Array.isArray(source) ? source.slice(0, 100) : [];
  return `<section aria-labelledby="policy-dry-run-dlp-title"><h3 id="policy-dry-run-dlp-title">DLP</h3><dl>
    <div><dt>状态</dt><dd>${escapeHtml(dlp?.status ?? "not_evaluated")}</dd></div>
    <div><dt>命中数</dt><dd>${escapeHtml(dlp?.match_count ?? 0)}</dd></div>
  </dl>${renderDlpMatches(matches)}</section>`;
}

/** DLP 命中仅展示稳定规则 ID、优先级和动作，不显示 pattern、命中文本、位置或 hash。 */
function renderDlpMatches(matches) {
  if (!matches.length) return "";
  return `<div class="policy-dry-run-dlp-table" role="table" aria-label="DLP 命中规则">
    <div role="row" class="policy-dry-run-table-head"><span role="columnheader">规则 ID</span><span role="columnheader">优先级</span><span role="columnheader">动作</span></div>
    ${matches.map((item) => `<div role="row"><code role="cell" data-label="规则 ID">${escapeHtml(item.dlp_rule_id ?? item.rule_id ?? "-")}</code><span role="cell" data-label="优先级">${escapeHtml(item.priority ?? "-")}</span><strong role="cell" data-label="动作">${escapeHtml(item.action ?? "-")}</strong></div>`).join("")}
  </div>`;
}

/** 渲染预算结果；金额保持后端 Decimal 文本，不在前端用浮点数重算。 */
function renderBudget(budget) {
  const rules = Array.isArray(budget?.rules) ? budget.rules.slice(0, 2) : [];
  return `<section aria-labelledby="policy-dry-run-budget-title"><h3 id="policy-dry-run-budget-title">预算</h3><dl>
    <div><dt>状态</dt><dd>${escapeHtml(budget?.status ?? "not_evaluated")}</dd></div>
    <div><dt>估算 Token</dt><dd>${escapeHtml(budget?.estimated_input_tokens ?? "-")} / ${escapeHtml(budget?.estimated_output_tokens ?? "-")}</dd></div>
    <div><dt>估算成本</dt><dd>${escapeHtml(budget?.estimated_cost_cny ?? "-")} CNY</dd></div>
  </dl>${renderBudgetRules(rules)}</section>`;
}

/** 预算规则只展示当前 what-if 结果；金额沿用后端六位 Decimal 文本，不在浏览器重算。 */
function renderBudgetRules(rules) {
  if (!rules.length) return "";
  return `<div class="policy-dry-run-budget-table" role="table" aria-label="预算规则结果">
    <div role="row" class="policy-dry-run-table-head"><span role="columnheader">范围</span><span role="columnheader">状态</span><span role="columnheader">动作</span><span role="columnheader">预计 / 上限</span></div>
    ${rules.map((item) => `<div role="row"><span role="cell" data-label="范围">${escapeHtml(item.scope_type ?? "-")}</span><strong role="cell" data-label="状态">${escapeHtml(item.status ?? item.budget_status ?? "-")}</strong><span role="cell" data-label="动作">${escapeHtml(item.action ?? "-")}</span><code role="cell" data-label="预计 / 上限">${escapeHtml(item.projected_cny ?? "-")} / ${escapeHtml(item.limit_cny ?? "-")}</code></div>`).join("")}
  </div>`;
}

/** 从当前刷新状态提取 active Environment。 */
function activeEnvironments(state) {
  const items = Array.isArray(state?.environments) ? state.environments : [];
  return items.filter((item) => item?.status === ACTIVE_STATUS && item?.environment_id);
}

/** 从当前刷新状态提取 active Project。 */
function activeProjects(state) {
  const items = Array.isArray(state?.projects) ? state.projects : [];
  return items.filter((item) => item?.status === ACTIVE_STATUS && item?.project_id);
}

/** 合并 Environment 对应 alias 与 direct model，去重后形成页面 model 值。 */
function selectableModels(aliasState, modelState, environmentId, environments) {
  const environment = environments.find((item) => item.environment_id === environmentId);
  const environmentCode = text(environment?.code);
  const values = new Map();
  for (const item of Array.isArray(aliasState?.model_aliases) ? aliasState.model_aliases : []) {
    if (item?.status !== ACTIVE_STATUS || !item?.alias) continue;
    if (environmentId && item.environment_id && item.environment_id !== environmentId) continue;
    if (environmentCode && item.environment && item.environment !== environmentCode) continue;
    values.set(text(item.alias), { value: text(item.alias), label: `${text(item.alias)} / 模型别名` });
  }
  for (const item of Array.isArray(modelState?.models) ? modelState.models : []) {
    if (item?.status !== ACTIVE_STATUS) continue;
    const value = text(item.model_alias ?? item.internal_model_id ?? item.model_id);
    if (value && !values.has(value)) values.set(value, { value, label: `${value} / 直接模型` });
  }
  return [...values.values()].sort((left, right) => left.value.localeCompare(right.value));
}

/** 通用安全 option 渲染，值与标签均逐项转义。 */
function renderOptions(items, selected, key, label, placeholder) {
  return `<option value="">${escapeHtml(placeholder)}</option>${items.map((item) => {
    const value = text(item?.[key]);
    return `<option value="${escapeAttribute(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label(item))}</option>`;
  }).join("")}`;
}

function environmentLabel(item) { return text(item?.name ?? item?.code ?? item?.environment_id); }
function projectLabel(item) { return text(item?.name ?? item?.project_code ?? item?.project_id); }
function statusLabel(status) { return status === "submitting" ? "预演中" : status === "success" ? "已完成" : status === "error" ? "失败" : "就绪"; }
function outcomeLabel(value) { return text(value || "completed").replaceAll("_", " "); }
function stringList(value) { return Array.isArray(value) ? value.filter((item) => typeof item === "string").slice(0, 20) : []; }
/** 仅翻译白名单覆盖键，未知后端值返回空文案，不能直接进入页面。 */
function coverageLabel(value) { return COVERAGE_LABELS[value] ?? ""; }
/** 将决策树顺序限制在后端合同范围内，异常值使用稳定回退序号以免扰乱层级。 */
function safeNodeOrder(node, fallback) { const value = Number(node?.order); return Number.isInteger(value) && value >= 0 && value <= 255 ? value : fallback; }
/** 按 parent_id 计算最多四层显示深度；缺失父节点、重复 ID 或环都在当前节点安全截断。 */
function safeTreeDepth(node, nodesById) {
  const seen = new Set();
  let current = node;
  let depth = 0;
  while (depth < 4) {
    const currentId = text(current?.node_id);
    const parentId = text(current?.parent_id);
    if (!parentId || seen.has(currentId) || seen.has(parentId)) break;
    const parent = nodesById.get(parentId);
    if (!parent) break;
    if (currentId) seen.add(currentId);
    depth += 1;
    current = parent;
  }
  return depth;
}
/** 把可空 DTO 字段规范化为字符串，供后续统一转义，绝不在这里拼接可信 HTML。 */
function text(value) { return value == null ? "" : String(value); }
/** 转义所有 HTML 文本上下文特殊字符，防止后端摘要或名称突破租户管理页面边界。 */
function escapeHtml(value) { return text(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;"); }
/** 在 HTML 文本转义基础上补齐属性上下文的反引号，避免动态值形成新属性。 */
function escapeAttribute(value) { return escapeHtml(value).replaceAll("`", "&#96;"); }
