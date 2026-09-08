/**
 * 模块职责：把后端脱敏的 RequestExplanation DTO 渲染为 PAGE-079 单请求决策链。
 * 维护边界：只读取本文件声明的字段白名单；未知字段、原始 JSON、Prompt、Response、
 * raw_usage、Provider 原始错误、Webhook payload/URL 和任何 Credential 永不进入 DOM。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 状态标签只翻译后端白名单枚举，未知值继续走安全回退且不参与权限或决策推导。
const STATE_LABELS = Object.freeze({
  complete: "证据完整",
  partial: "证据部分缺失",
  restricted: "权限受限",
  pending: "等待异步证据",
  not_recorded: "未记录",
  not_applicable: "未执行",
  passed: "通过",
  not_evaluated: "未评估",
  allowed: "通过",
  blocked: "阻断",
  failed: "失败",
  succeeded: "成功",
  success: "成功",
  warning: "告警",
  unknown: "未知",
});

// 阶段标签只映射冻结的决策链枚举，未知阶段保持转义后的安全回退文本。
const STAGE_LABELS = Object.freeze({
  authentication: "鉴权与身份",
  environment: "环境与快照",
  request_validation: "请求校验",
  policy: "策略求值",
  routing: "路由决策",
  capability: "能力校验",
  dlp: "DLP 检查",
  rate_limit: "限流检查",
  budget: "预算准入",
  keypool: "KeyPool 选择",
  provider: "Provider 调用",
  provider_response: "Provider 响应",
  usage_and_finops: "Usage 与 FinOps",
  logs: "日志",
  webhooks: "Webhook",
  logs_and_webhooks: "日志与 Webhook",
});

// 十一区块与服务端 SECTION_ORDER 一致；只用于固定顺序和中文标题，不能由响应动态扩展。
const SECTION_DEFINITIONS = Object.freeze([
  section("authentication", "鉴权与身份"),
  section("rate_limit", "限流检查"),
  section("budget", "预算准入"),
  section("dlp", "DLP 检查"),
  section("policy", "策略求值"),
  section("routing", "路由决策"),
  section("keypool", "KeyPool 选择"),
  section("provider_response", "Provider 响应"),
  section("usage_and_finops", "Usage 与 FinOps"),
  section("logs", "日志与终态"),
  section("webhooks", "Webhook 状态"),
]);

// 每个 fact kind 的 values 使用 exact-key 白名单；未知 kind 和新增字段默认不进入 DOM。
const FACT_VALUE_KEYS = Object.freeze({
  authentication: [
    "virtual_key_id", "virtual_key_prefix", "environment_id", "environment_name",
    "project_id", "project_name", "application_id", "application_name",
    "snapshot_version", "policy_snapshot_status",
  ],
  rate_limit: ["decision", "error_code"],
  budget_evaluation: [
    "budget_rule_id", "scope_type", "scope_id", "budget_date", "spent_before_cny",
    "estimated_request_cost_cny", "projected_cost_cny", "budget_cny",
    "threshold_percent", "budget_status", "action_taken", "reason_code",
  ],
  budget_scope_event: [
    "event_id", "budget_rule_id", "scope_type", "scope_id", "action_taken",
    "posted_cost_micros", "reserved_cost_micros", "estimated_request_cost_micros",
    "projected_cost_micros", "budget_micros",
  ],
  dlp_match: [
    "dlp_rule_id", "rule_name", "rule_type", "action", "message_index",
    "match_text_hash", "match_length",
  ],
  policy_snapshot: [
    "snapshot_version", "snapshot_status", "policy_id", "policy_action_type",
    "policy_target_model_id", "policy_decision", "policy_reason_code",
    "routing_policy_id", "routing_policy_version", "routing_mode",
  ],
  routing_decision: [
    "decision_status", "decision_reason", "requested_model", "selected_model_id",
    "selected_internal_model_id", "provider_id", "provider_code", "error_code",
  ],
  routing_candidate: [
    "model_id", "provider_id", "candidate_role", "configured_priority", "rank",
    "status", "reason_code", "health_state", "health_fresh", "keypool_state",
    "price_available", "price_source", "price_schema_version", "currency",
    "estimated_cost_amount", "quality_component_bps", "cost_component_bps",
    "latency_component_bps", "weighted_score_bps",
  ],
  cache_affinity: [
    "attempt_sequence", "routing_attempt_id", "target_kind", "target_generation",
    "provider_id", "provider_code", "endpoint_preference_applied",
    "endpoint_preference_state", "binding_state_before", "selection_reason",
    "estimated_hit", "outcome_status",
  ],
  routing_attempt: [
    "attempt_sequence", "attempt_role", "trigger_type", "trigger_error_code",
    "model_id", "internal_model_id", "provider_id", "provider_code", "status",
    "error_code", "error_category", "upstream_http_status", "retryable",
    "latency_ms", "usage_status", "estimated_cost_cny",
  ],
  usage: [
    "input_tokens", "output_tokens", "total_tokens", "cache_tokens", "cost_amount",
    "cost_cny", "cost_usd", "currency", "price_schema_version", "price_snapshot_id",
    "estimated", "usage_source", "routing_attempt_id",
  ],
  finops_entry: [
    "ledger_sequence", "event_id", "entry_type", "direction", "amount_micros",
    "currency", "normalized_cny_micros", "cny_normalization_status",
    "price_evidence_status", "price_snapshot_source", "posting_source", "quota_type",
    "price_snapshot_id", "price_schema_version", "input_tokens", "output_tokens",
    "total_tokens", "cache_tokens", "estimated", "status",
  ],
  request_log: [
    "source_status", "error_code", "stream_status", "latency_ms",
    "fallback_triggered", "routing_attempt_count", "final_routing_attempt_id",
  ],
  stream_session: [
    "status", "inter_token_timeout_ms", "chunk_count", "bytes_sent",
    "first_chunk_at", "last_chunk_at", "error_code", "completed_at",
  ],
  webhook_delivery: [
    "event_id", "event_type", "webhook_endpoint_id", "endpoint_name", "status",
    "attempt_count", "max_attempts", "http_status", "duration_ms", "error_code",
    "next_retry_at", "delivery_kind", "replayed_from_delivery_id",
  ],
  webhook_attempt: [
    "webhook_delivery_id", "webhook_endpoint_id", "attempt_no", "status",
    "http_status", "duration_ms", "error_code",
  ],
});

// fact 类型标签用于运维人员快速定位证据来源，不参与字段准入或权限判断。
const FACT_LABELS = Object.freeze({
  authentication: "鉴权快照",
  rate_limit: "限流结果",
  budget_evaluation: "预算评估",
  budget_scope_event: "预算作用域事件",
  dlp_match: "DLP 命中",
  policy_snapshot: "策略快照",
  routing_decision: "路由决策",
  routing_candidate: "路由候选",
  cache_affinity: "缓存亲和选择",
  routing_attempt: "上游尝试",
  usage: "Usage 记录",
  finops_entry: "FinOps 分录",
  request_log: "请求终态",
  stream_session: "流式会话",
  webhook_delivery: "Webhook 投递",
  webhook_attempt: "Webhook 尝试",
});

// 中文标签只影响展示；字段准入始终由 FACT_VALUE_KEYS 决定。
const FIELD_LABELS = Object.freeze({
  virtual_key_id: "Virtual Key ID",
  virtual_key_prefix: "Key 前缀",
  environment_id: "Environment ID",
  environment_name: "Environment",
  project_id: "Project ID",
  project_name: "Project",
  application_id: "Application ID",
  application_name: "Application",
  snapshot_version: "快照版本",
  policy_snapshot_status: "策略快照状态",
  decision: "决策",
  decision_status: "决策状态",
  decision_reason: "决策原因",
  error_code: "错误码",
  reason_code: "原因码",
  status: "状态",
  budget_rule_id: "预算规则 ID",
  scope_type: "作用域类型",
  scope_id: "作用域 ID",
  budget_date: "预算日期",
  spent_before_cny: "请求前已用 CNY",
  estimated_request_cost_cny: "预计请求成本 CNY",
  projected_cost_cny: "预计累计 CNY",
  budget_cny: "预算 CNY",
  threshold_percent: "阈值百分比",
  budget_status: "预算状态",
  action_taken: "执行动作",
  event_id: "事件 ID",
  posted_cost_micros: "已入账 micros",
  reserved_cost_micros: "已预留 micros",
  estimated_request_cost_micros: "预计请求 micros",
  projected_cost_micros: "预计累计 micros",
  budget_micros: "预算 micros",
  dlp_rule_id: "DLP 规则 ID",
  rule_name: "规则",
  rule_type: "规则类型",
  action: "动作",
  message_index: "消息序号",
  match_text_hash: "命中摘要哈希",
  match_length: "命中长度",
  snapshot_status: "快照状态",
  policy_id: "Policy ID",
  policy_action_type: "策略动作",
  policy_target_model_id: "目标模型 ID",
  policy_decision: "策略决策",
  policy_reason_code: "策略原因码",
  routing_policy_id: "RoutingPolicy ID",
  routing_policy_version: "RoutingPolicy 版本",
  routing_mode: "路由模式",
  requested_model: "请求模型",
  selected_model_id: "选中模型 ID",
  selected_internal_model_id: "内部模型 ID",
  provider_id: "Provider ID",
  provider_code: "Provider",
  model_id: "模型 ID",
  internal_model_id: "内部模型 ID",
  candidate_role: "候选角色",
  configured_priority: "配置优先级",
  rank: "排名",
  health_state: "健康状态",
  health_fresh: "健康新鲜度",
  keypool_state: "KeyPool 状态",
  price_available: "价格可用",
  price_source: "价格来源",
  price_schema_version: "价格版本",
  price_snapshot_id: "价格快照 ID",
  currency: "币种",
  estimated_cost_amount: "预计成本",
  quality_component_bps: "质量分量 bps",
  cost_component_bps: "成本分量 bps",
  latency_component_bps: "延迟分量 bps",
  weighted_score_bps: "综合分 bps",
  attempt_sequence: "尝试序号",
  routing_attempt_id: "Attempt ID",
  target_kind: "目标类型",
  target_generation: "目标代次",
  endpoint_preference_applied: "Endpoint 偏好已应用",
  endpoint_preference_state: "Endpoint 偏好状态",
  binding_state_before: "调用前绑定状态",
  selection_reason: "选择原因",
  estimated_hit: "预计命中",
  outcome_status: "结果状态",
  attempt_role: "尝试角色",
  trigger_type: "触发类型",
  trigger_error_code: "触发错误码",
  error_category: "错误分类",
  upstream_http_status: "上游 HTTP 状态",
  retryable: "可重试",
  latency_ms: "耗时 ms",
  usage_status: "Usage 状态",
  estimated_cost_cny: "预计成本 CNY",
  input_tokens: "输入 Token",
  output_tokens: "输出 Token",
  total_tokens: "总 Token",
  cache_tokens: "缓存 Token",
  cost_amount: "成本",
  cost_cny: "成本 CNY",
  cost_usd: "成本 USD",
  estimated: "估算",
  usage_source: "Usage 来源",
  ledger_sequence: "账本序号",
  entry_type: "分录类型",
  direction: "方向",
  amount_micros: "金额 micros",
  normalized_cny_micros: "归一 CNY micros",
  cny_normalization_status: "CNY 归一状态",
  price_evidence_status: "价格证据",
  price_snapshot_source: "价格快照来源",
  posting_source: "入账来源",
  quota_type: "额度类型",
  source_status: "请求终态",
  stream_status: "流式状态",
  fallback_triggered: "触发 Fallback",
  routing_attempt_count: "尝试次数",
  final_routing_attempt_id: "最终 Attempt ID",
  inter_token_timeout_ms: "Token 间超时 ms",
  chunk_count: "Chunk 数",
  bytes_sent: "发送字节",
  first_chunk_at: "首 Chunk 时间",
  last_chunk_at: "末 Chunk 时间",
  completed_at: "完成时间",
  event_type: "事件类型",
  webhook_endpoint_id: "Webhook Endpoint ID",
  endpoint_name: "Endpoint",
  attempt_count: "尝试次数",
  max_attempts: "最大尝试次数",
  http_status: "HTTP 状态",
  duration_ms: "耗时 ms",
  next_retry_at: "下次重试",
  delivery_kind: "投递类型",
  replayed_from_delivery_id: "回放来源 Delivery ID",
  webhook_delivery_id: "Webhook Delivery ID",
  attempt_no: "尝试编号",
});

// 摘要字段固定为后端公开标量，金额字符串和大整数禁止通过 Number 重新计算。
const SUMMARY_FIELDS = Object.freeze([
  field("source_status", "请求终态"),
  field("request_model", "请求模型"),
  field("resolved_model", "解析模型"),
  field("provider_code", "Provider"),
  field("error_code", "错误码"),
  field("latency_ms", "耗时 ms"),
  field("input_tokens", "输入 Token"),
  field("output_tokens", "输出 Token"),
  field("total_tokens", "总 Token"),
  field("estimated_cost_amount", "预计成本"),
  field("currency", "币种"),
  field("environment_id", "Environment ID"),
  field("environment_name", "Environment"),
  field("project_id", "Project ID"),
  field("project_name", "Project"),
  field("application_id", "Application ID"),
  field("application_name", "Application"),
  field("snapshot_version", "快照版本"),
  field("fallback_triggered", "触发 Fallback"),
  field("created_at", "请求时间"),
  field("updated_at", "证据水位"),
]);

/**
 * 渲染 PAGE-079。
 * 前端角色判断只用于零 DOM/零预取体验；真实权限、租户隔离和字段脱敏仍由后端 GET 强制执行。
 */
export function renderRequestExplanationPanel(state, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.REQUEST_EXPLANATION_READ)) {
    return "";
  }
  const currentState = state ?? { state: "idle", requestId: "" };
  return `
    <div class="request-explanation-heading">
      <div>
        <h2 id="request-explanations-title">请求解释</h2>
      </div>
      ${renderTopStatus(currentState)}
    </div>
    ${renderQueryForm(currentState)}
    ${renderExplanationState(currentState)}
  `;
}

/** 查询表单只提交 source_request_id，不包含 tenant、actor 或 raw/include 参数。 */
function renderQueryForm(state) {
  const requestId = state?.draftRequestId ?? state?.requestId ?? "";
  return `
    <form id="request-explanation-form" class="request-explanation-form" novalidate>
      <label>
        <span>request_id</span>
        <input
          name="requestId"
          value="${escapeAttribute(requestId ?? "")}"
          maxlength="128"
          pattern="[A-Za-z0-9_.:-]{1,128}"
          autocomplete="off"
          spellcheck="false"
          placeholder="req_xxx"
          aria-describedby="request-explanation-input-error"
          data-request-explanation-focus="request-id"
        />
      </label>
      <div class="request-explanation-actions">
        <button type="submit" data-request-explanation-focus="query">查询</button>
        <button
          type="button"
          id="request-explanation-refresh"
          data-request-explanation-focus="refresh"
          ${state?.requestId ? "" : "disabled"}
        >刷新</button>
        <button
          type="button"
          id="request-explanation-clear"
          data-request-explanation-focus="clear"
          ${requestId || state?.requestId ? "" : "disabled"}
        >清空</button>
      </div>
      <span id="request-explanation-input-error" class="request-explanation-input-error" aria-live="polite"></span>
    </form>
  `;
}

/** 顶部状态只使用固定枚举，不把后端未知文本拼成 CSS class。 */
function renderTopStatus(state) {
  const explanationStatus = state?.request_explanation?.status;
  const status = state.state === "ok" ? explanationStatus : state.state;
  const safeStatus = statusClass(status);
  const label = state.state === "idle"
    ? "等待查询"
    : state.state === "loading"
      ? "读取中"
      : state.state === "error"
        ? "读取失败"
        : STATE_LABELS[explanationStatus] ?? "已读取";
  return `<span class="request-explanation-status status-${safeStatus}">${escapeHtml(label)}</span>`;
}

/** 按 idle/loading/error/ok 显式渲染，错误态永不复用上一条解释。 */
function renderExplanationState(state) {
  if (state.state === "idle") {
    return `<div class="request-explanation-empty">尚未选择请求。</div>`;
  }
  if (state.state === "loading") {
    return `<div class="request-explanation-empty" aria-live="polite">正在读取 ${renderCode(state.requestId)} 的解释。</div>`;
  }
  // 错误态只展示统一消息和 operation request_id，绝不复用上一条解释或原始响应正文。
  if (state.state === "error") {
    return `
      <div class="request-explanation-body request-explanation-error-body" data-explanation-status="error">
        <aside class="request-explanation-summary" aria-label="请求解释错误摘要">
          <div class="request-explanation-empty error-state" role="alert">
            <strong>${escapeHtml(state.error?.message ?? "请求解释读取失败")}</strong>
            <span>operation request_id: ${renderCode(state.error?.requestId ?? "-")}</span>
          </div>
        </aside>
        <div class="request-explanation-evidence" aria-label="请求解释证据">
          <div class="request-explanation-empty request-explanation-evidence-empty">
            <strong>无可验证证据</strong>
            <span>本次查询未返回请求解释，未保留或复用上一条请求的数据。</span>
          </div>
        </div>
      </div>
    `;
  }
  const explanation = state.request_explanation;
  if (!explanation || typeof explanation !== "object") {
    return `<div class="request-explanation-empty error-state" role="alert">请求解释响应不完整。</div>`;
  }
  return `
    <div class="request-explanation-body" data-explanation-status="${escapeAttribute(statusClass(explanation.status))}">
      <aside class="request-explanation-summary" aria-label="请求解释摘要">
        ${renderSummary(explanation, state)}
      </aside>
      <div class="request-explanation-evidence">
        ${renderDecisionChain(explanation.decision_chain)}
        <div class="request-explanation-sections">
          ${SECTION_DEFINITIONS.map(
            (definition) => renderSection(explanation, definition, state.sectionDisclosure),
          ).join("")}
        </div>
      </div>
    </div>
  `;
}

/** 摘要只展示稳定 ID、完整性和安全业务标量；operation 与 source ID 明确分区。 */
function renderSummary(explanation, state) {
  const summary = objectOrEmpty(explanation.summary);
  const missing = safeStringArray(explanation.missing_sections);
  const truncated = safeStringArray(explanation.truncated_sections);
  return `
    <div class="request-explanation-summary-head">
      <span>源请求</span>
      ${renderCode(explanation.source_request_id ?? state.requestId)}
    </div>
    <dl class="request-explanation-summary-grid">
      ${renderPair("Source trace_id", explanation.source_trace_id)}
      ${renderPair("Explanation ID", explanation.request_explanation_id)}
      ${renderPair("解释完整性", STATE_LABELS[explanation.status] ?? explanation.status)}
      ${renderPair("证据版本", explanation.evidence_version)}
      ${renderPair(
        "查看事件",
        explanation.view_event_status === "pending" ? "已持久化，待投递" : explanation.view_event_status,
      )}
      ${renderPair("采集时间", explanation.captured_at)}
      ${renderPair("Operation request_id", state.request_id)}
      ${renderPair("Operation trace_id", state.trace_id)}
      ${renderKnownFields(summary, SUMMARY_FIELDS)}
    </dl>
    ${renderSectionList("缺失区块", missing, "missing")}
    ${renderSectionList("截断区块", truncated, "truncated")}
  `;
}

/** 决策链保持服务端给出的历史顺序；前端绝不按当前配置或固定架构图重新排序。 */
function renderDecisionChain(chain) {
  if (!Array.isArray(chain) || chain.length === 0) {
    return `
      <section class="request-explanation-chain" aria-labelledby="request-explanation-chain-title">
        <h3 id="request-explanation-chain-title">决策链</h3>
        <div class="request-explanation-inline-empty">当前记录没有可验证的阶段顺序。</div>
      </section>
    `;
  }
  return `
    <section class="request-explanation-chain" aria-labelledby="request-explanation-chain-title">
      <h3 id="request-explanation-chain-title">决策链</h3>
      <ol>
        ${chain.map((entry, index) => renderStage(entry, index)).join("")}
      </ol>
    </section>
  `;
}

/** 单阶段只读取稳定状态、原因和时间，不显示服务端未声明的任意 evidence 对象。 */
function renderStage(entry, index) {
  const value = objectOrEmpty(entry);
  const stage = text(value.stage || value.name || `stage_${index + 1}`);
  const status = text(value.status || value.outcome || value.evidence_state || "unknown");
  return `
    <li class="request-explanation-stage stage-${statusClass(status)}">
      <span class="request-explanation-stage-index">${escapeHtml(index + 1)}</span>
      <div>
        <strong>${escapeHtml(STAGE_LABELS[stage] ?? stage)}</strong>
        <span>${escapeHtml(STATE_LABELS[status] ?? status)}</span>
        ${value.evidence_state
          ? `<em>${escapeHtml(STATE_LABELS[value.evidence_state] ?? value.evidence_state)}</em>`
          : ""}
        ${value.reason_code ? `<code>${escapeHtml(value.reason_code)}</code>` : ""}
        ${value.occurred_at ? `<time>${escapeHtml(value.occurred_at)}</time>` : ""}
        ${renderEvidenceRefs(value.evidence_refs)}
      </div>
    </li>
  `;
}

/** 每个 details 是同级领域带，不嵌套卡片；异常/partial 默认展开，正常证据保持紧凑。 */
function renderSection(explanation, definition, disclosure) {
  const value = objectOrEmpty(explanation[definition.key]);
  const evidenceState = text(value.evidence_state || value.status || "not_recorded");
  const abnormal = !["complete", "allowed", "succeeded", "success", "not_applicable"].includes(evidenceState);
  const disclosureValue = objectOrEmpty(disclosure)[definition.key];
  const open = typeof disclosureValue === "boolean" ? disclosureValue : abnormal;
  const facts = Array.isArray(value.facts)
    ? value.facts.map(renderFact).filter(Boolean).join("")
    : "";
  return `
    <details
      class="request-explanation-section section-${statusClass(evidenceState)}"
      data-request-explanation-section="${escapeAttribute(definition.key)}"
      ${open ? "open" : ""}
    >
      <summary data-request-explanation-focus="section:${escapeAttribute(definition.key)}">
        <span>${escapeHtml(definition.label)}</span>
        <mark>${escapeHtml(STATE_LABELS[evidenceState] ?? evidenceState)}</mark>
      </summary>
      <div class="request-explanation-section-content">
        <dl>
          ${renderPair("原因码", value.reason_code)}
          ${renderPair("结果截断", value.truncated)}
        </dl>
        ${facts
          ? `<div class="request-explanation-facts">${facts}</div>`
          : `<div class="request-explanation-inline-empty">没有可展示的历史证据。</div>`}
      </div>
    </details>
  `;
}

/**
 * 渲染一条服务端安全事实。
 * kind 决定唯一 values 白名单；未知 kind、嵌套对象和额外键全部忽略，绝不做 JSON 展开。
 */
function renderFact(value, index) {
  const fact = objectOrEmpty(value);
  const kind = text(fact.kind);
  const allowedKeys = FACT_VALUE_KEYS[kind];
  if (!Array.isArray(allowedKeys)) {
    return "";
  }
  const values = objectOrEmpty(fact.values);
  const fields = allowedKeys.map((key) => field(key, FIELD_LABELS[key] ?? key));
  const rows = renderKnownFields(values, fields);
  return `
    <article class="request-explanation-record">
      <header>
        <strong>#${escapeHtml(index + 1)} ${escapeHtml(FACT_LABELS[kind] ?? kind)}</strong>
        ${fact.fact_id ? renderCode(fact.fact_id) : ""}
      </header>
      ${fact.occurred_at ? `<time>${escapeHtml(fact.occurred_at)}</time>` : ""}
      ${rows ? `<dl>${rows}</dl>` : `<div class="request-explanation-inline-empty">没有可展示字段。</div>`}
    </article>
  `;
}

/** 阶段证据引用只接受有界字符串数组，不展示对象或未知结构。 */
function renderEvidenceRefs(value) {
  const refs = safeStringArray(value).slice(0, 16);
  if (refs.length === 0) {
    return "";
  }
  return `<span class="request-explanation-evidence-refs">${refs.map(renderCode).join("")}</span>`;
}

/** 按白名单把标量映射为 dt/dd；数组、对象和空值不会被隐式 JSON 化。 */
function renderKnownFields(source, definitions) {
  return definitions
    .map(({ key, label }) => renderPair(label, source?.[key]))
    .filter(Boolean)
    .join("");
}

/** 单个键值对只接受 string/number/boolean，金额字符串不经过 Number 或 float 回算。 */
function renderPair(label, value) {
  if (
    value === null
    || value === undefined
    || value === ""
    || (typeof value === "object")
  ) {
    return "";
  }
  const formatted = typeof value === "boolean" ? (value ? "是" : "否") : String(value);
  return `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(formatted)}</dd>`;
}

/** 缺失/截断列表使用服务端枚举标签，不接受嵌套值。 */
function renderSectionList(label, values, kind) {
  if (values.length === 0) {
    return "";
  }
  return `
    <div class="request-explanation-section-list ${escapeAttribute(kind)}">
      <strong>${escapeHtml(label)}</strong>
      <ul>${values.map((value) => `<li>${escapeHtml(STAGE_LABELS[value] ?? value)}</li>`).join("")}</ul>
    </div>
  `;
}

/** 构造不可变字段定义，避免渲染调用点误把后端 key 当作可见标签。 */
function field(key, label) {
  return Object.freeze({ key, label });
}

/** 构造冻结领域区块定义。 */
function section(key, label) {
  return Object.freeze({ key, label });
}

/** 只接受字符串数组，拒绝对象触发隐式序列化或原始 payload 展开。 */
function safeStringArray(value) {
  return Array.isArray(value)
    ? value.filter((item) => typeof item === "string" && item.length <= 128)
    : [];
}

/** 非普通对象按空对象处理，renderer 继续显示安全空态。 */
function objectOrEmpty(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

/** 状态 class 只允许固定小集合，未知值统一 unknown，避免属性注入。 */
function statusClass(value) {
  const candidate = text(value).toLowerCase();
  return [
    "complete", "partial", "pending", "not_recorded", "not_applicable", "passed",
    "not_evaluated", "allowed", "blocked", "failed", "succeeded", "success",
    "warning", "loading", "idle", "error",
  ].includes(candidate) ? candidate : "unknown";
}

/** code 仍需 HTML 转义；标记只用于可复制 ID，不代表该值可写入 URL 或 Storage。 */
function renderCode(value) {
  return `<code>${escapeHtml(value ?? "-")}</code>`;
}

/** 统一文本转换，不允许 Symbol 或自定义对象直接进入 DOM。 */
function text(value) {
  return value === null || value === undefined ? "" : String(value);
}

/** 转义所有动态文本，防止 Provider、模型、规则和错误标签形成 HTML。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** HTML 属性除通用转义外额外处理反引号。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
