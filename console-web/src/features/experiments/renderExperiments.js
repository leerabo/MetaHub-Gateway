/**
 * PAGE-105 紧凑实验列表、详情和权威 A/B 结果渲染器。
 *
 * 只渲染状态机已经按 tenant/RBAC owner 验证的安全 DTO；页面不接收请求明细、正文、
 * Provider Endpoint 或分桶指纹，也不在浏览器侧推断赢家。
 */

import { experimentPageOwner } from "./experimentPageState.js";

// 状态和完成原因沿用后端单向枚举，未知值只作为转义后的诊断文本显示。
const STATUS_LABELS = Object.freeze({
  active: "采样中",
  collecting_feedback: "收集反馈",
  completed: "已完成",
});
const COMPLETION_LABELS = Object.freeze({
  target_reached: "目标样本已满足",
  sampling_deadline_reached: "采样窗口已结束",
});

/** 仅在后端 experiments 导航 owner 完整时输出 PAGE-105 DOM。 */
export function renderExperiments(state, authContext, consoleShellState) {
  const owner = experimentPageOwner(authContext, consoleShellState);
  if (!owner) return "";
  const view = state ?? {
    state: "loading",
    experiments: [],
    selectedExperimentId: "",
    detailState: "idle",
    resultsState: "idle",
    canCreate: owner.canCreate,
  };
  return `
    <div class="experiment-workspace" data-experiments-root data-selected-experiment="${escapeAttribute(view.selectedExperimentId ?? "")}">
      <div class="experiment-toolbar">
        <div>
          <h2 id="experiments-title">模型评测</h2>
          <span>当前租户: ${escapeHtml(owner.tenantId)}</span>
        </div>
        <div class="experiment-toolbar-actions">
          <label>
            <span class="sr-only">实验状态</span>
            <select data-experiment-status aria-label="实验状态">
              <option value=""${view.status ? "" : " selected"}>全部状态</option>
              ${Object.entries(STATUS_LABELS).map(([value, label]) => (
                `<option value="${value}"${view.status === value ? " selected" : ""}>${label}</option>`
              )).join("")}
            </select>
          </label>
          <button type="button" class="icon-command" data-experiment-refresh title="刷新" aria-label="刷新实验">↻</button>
          ${owner.canCreate
            ? `<button type="button" class="experiment-create-command" data-experiment-create><span aria-hidden="true">+</span><span>新建实验</span></button>`
            : ""}
        </div>
      </div>
      <div class="experiment-action-feedback${view.createError ? " is-error" : ""}" role="status" aria-live="polite">
        ${renderCreateFeedback(view)}
      </div>
      ${renderList(view)}
      ${renderDetail(view)}
      ${renderCreateDialog(view, owner)}
    </div>
  `;
}

/** 渲染 loading、error、empty 或带私有分页命令的实验列表。 */
function renderList(state) {
  if (state.state === "error") return renderError(state.error, "实验列表读取失败");
  const experiments = Array.isArray(state.experiments) ? state.experiments : [];
  return `
    <section class="experiment-list" aria-labelledby="experiment-list-title">
      <div class="experiment-section-heading">
        <h3 id="experiment-list-title">实验列表</h3><span>${experiments.length} 项</span>
      </div>
      ${state.state === "loading"
        ? `<div class="experiment-empty">正在读取实验</div>`
        : experiments.length
          ? `<div class="experiment-table" role="table">
              <div class="experiment-row is-head" role="row">
                <span>名称</span><span>状态</span><span>样本</span><span>A / B</span><span>反馈</span><span>创建时间</span><span>操作</span>
              </div>
              ${experiments.map((item) => renderExperimentRow(item, state.selectedExperimentId)).join("")}
            </div>`
          : `<div class="experiment-empty">当前筛选没有实验</div>`}
      <div class="experiment-pager">
        <button type="button" data-experiment-previous${state.canPrevious ? "" : " disabled"}>上一页</button>
        <button type="button" data-experiment-next${state.canNext ? "" : " disabled"}>下一页</button>
      </div>
    </section>
  `;
}

/** 渲染一条已验证 ExperimentInfo，操作按钮只携带公开 experiment_id。 */
function renderExperimentRow(item, selectedId) {
  return `
    <div class="experiment-row${item.experiment_id === selectedId ? " is-selected" : ""}" role="row">
      <span data-label="名称"><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.experiment_id)}</code></span>
      <span data-label="状态"><b class="experiment-status tone-${escapeAttribute(item.status)}">${escapeHtml(STATUS_LABELS[item.status] ?? item.status)}</b></span>
      <span data-label="样本"><b>${item.assigned_count}/${item.target_sample_count}</b><small>${formatBps(item.traffic_sample_bps)} 流量</small></span>
      <span data-label="A / B"><b>${item.model_a.assigned_count} / ${item.model_b.assigned_count}</b><small>${formatBps(item.model_a.allocation_bps)} / ${formatBps(item.model_b.allocation_bps)}</small></span>
      <span data-label="反馈">${item.feedback_collection_enabled ? "开启" : "关闭"}</span>
      <span data-label="创建时间"><time datetime="${escapeAttribute(item.created_at)}">${escapeHtml(formatTime(item.created_at))}</time></span>
      <span data-label="操作"><button type="button" class="icon-command" data-experiment-select="${escapeAttribute(item.experiment_id)}" title="查看详情" aria-label="查看 ${escapeAttribute(item.name)} 详情">→</button></span>
    </div>
  `;
}

/** 渲染选中实验的不可变作用域和统计；未选择时给出明确空态。 */
function renderDetail(state) {
  if (!state.selectedExperimentId) {
    return `<section class="experiment-detail"><div class="experiment-empty">选择一个实验查看配置和 A/B 结果</div></section>`;
  }
  // 详情错误只展示固定错误码和 request_id，不能回退到上一次实验配置。
  if (state.detailState === "error") return renderError(state.detailError, "实验详情读取失败");
  if (state.detailState !== "ready" || !state.experiment) {
    return `<section class="experiment-detail"><div class="experiment-empty">正在读取实验详情</div></section>`;
  }
  const item = state.experiment;
  return `
    <section class="experiment-detail" aria-labelledby="experiment-detail-title">
      <div class="experiment-detail-heading">
        <div><h3 id="experiment-detail-title">${escapeHtml(item.name)}</h3><code>${escapeHtml(item.experiment_id)}</code></div>
        <b class="experiment-status tone-${escapeAttribute(item.status)}">${escapeHtml(STATUS_LABELS[item.status] ?? item.status)}</b>
      </div>
      <dl class="experiment-metadata">
        <div><dt>Project</dt><dd>${escapeHtml(item.project_id)}</dd></div>
        <div><dt>Environment</dt><dd>${escapeHtml(item.environment_id)}</dd></div>
        <div><dt>Alias</dt><dd>${escapeHtml(item.model_alias_id)}</dd></div>
        <div><dt>模型 A</dt><dd>${escapeHtml(item.model_a.model_id)} · ${escapeHtml(item.model_a.provider_code)}</dd></div>
        <div><dt>模型 B</dt><dd>${escapeHtml(item.model_b.model_id)} · ${escapeHtml(item.model_b.provider_code)}</dd></div>
        <div><dt>A / B 目标</dt><dd>${item.model_a.target_count} (${formatBps(item.model_a.allocation_bps)}) / ${item.model_b.target_count} (${formatBps(item.model_b.allocation_bps)})</dd></div>
        <div><dt>目标样本</dt><dd>${item.target_sample_count}</dd></div>
        <div><dt>采样流量</dt><dd>${formatBps(item.traffic_sample_bps)}</dd></div>
        <div><dt>采样时长</dt><dd>${item.sampling_duration_seconds} 秒</dd></div>
        <div><dt>反馈收集</dt><dd>${item.feedback_collection_enabled ? "开启" : "关闭"}</dd></div>
        <div><dt>反馈窗口</dt><dd>${item.feedback_collection_enabled ? `${item.feedback_window_seconds} 秒` : "-"}</dd></div>
        <div><dt>算法版本</dt><dd>v${item.algorithm_version}</dd></div>
        <div><dt>开始时间</dt><dd>${escapeHtml(formatTime(item.started_at))}</dd></div>
        <div><dt>采样截止</dt><dd>${escapeHtml(formatTime(item.sampling_deadline))}</dd></div>
        <div><dt>采样结束</dt><dd>${escapeHtml(formatTime(item.sampling_ended_at))}</dd></div>
        <div><dt>反馈截止</dt><dd>${escapeHtml(formatTime(item.feedback_deadline))}</dd></div>
        <div><dt>结算截止</dt><dd>${escapeHtml(formatTime(item.settlement_deadline))}</dd></div>
      </dl>
      ${renderResults(state.resultsState, state.results, state.resultsError)}
    </section>
  `;
}

/** 渲染同一后端 snapshot 的 provisional/final 结果和待收口计数。 */
function renderResults(resultsState, response, error) {
  if (resultsState === "error") return renderError(error, "实验统计读取失败");
  if (resultsState !== "ready" || !response) {
    return `<div class="experiment-empty">正在计算 A/B 统计</div>`;
  }
  return `
    <section class="experiment-results" aria-labelledby="experiment-results-title">
      <div class="experiment-section-heading">
        <h4 id="experiment-results-title">A/B 结果</h4>
        <span>${response.is_final ? "已冻结" : "进行中"} · facts v${response.facts_version}</span>
      </div>
      <div class="experiment-result-grid">
        ${response.results.map(renderArmResult).join("")}
      </div>
      <div class="experiment-result-foot">
        ${response.completion_reason
          ? `<span>${escapeHtml(COMPLETION_LABELS[response.completion_reason] ?? response.completion_reason)}</span>`
          : `<span>下一状态 ${escapeHtml(formatTime(response.next_state_at))}</span>`}
        <span>执行待收口 ${response.pending_execution_count}</span>
        <span>成本待结算 ${response.pending_settlement_count}</span>
        <time datetime="${escapeAttribute(response.facts_through_at)}">${escapeHtml(formatTime(response.facts_through_at))}</time>
      </div>
    </section>
  `;
}

/** 渲染单个 variant 的成功、质量、延迟、成本和反馈指标，不计算跨 arm 排名。 */
function renderArmResult(result) {
  const label = result.variant === "a" ? "模型 A" : "模型 B";
  const feedbackEnabled = result.feedback_collection_enabled;
  return `
    <article class="experiment-result" aria-label="${label}">
      <header><div><span>${label}</span><strong>${escapeHtml(result.model_id)}</strong></div><code>${escapeHtml(result.provider_code)}</code></header>
      <dl>
        ${metric("分配样本", result.assigned_count)}
        ${metric("Gateway 成功率", formatNullableBps(result.gateway_success_rate_bps))}
        ${metric("模型质量成功率", formatNullableBps(result.quality_success_rate_bps))}
        ${metric("平均延迟", formatMicros(result.average_latency_micros))}
        ${metric("P95 延迟", result.p95_latency_ms === null ? "-" : `${result.p95_latency_ms} ms`)}
        ${metric("已知成本", formatCnyMicros(result.gross_cost_cny_micros))}
        ${metric("平均成本", formatNullableCnyMicros(result.average_cost_cny_micros))}
        ${metric("成本未知", result.cost_unknown_count)}
        ${metric("平均评分", feedbackEnabled && result.feedback_count ? (result.average_rating_milli / 1000).toFixed(3) : "-")}
        ${metric("采纳率", feedbackEnabled && result.feedback_count ? formatBps(result.adoption_rate_bps) : "-")}
      </dl>
      <div class="experiment-result-counts">
        <span>成功 ${result.gateway_success_count}</span>
        <span>失败 ${result.gateway_failure_count}</span>
        <span>未决 ${result.unresolved_count}</span>
        <span>质量 ${result.quality_success_count}/${result.quality_failure_count}</span>
        <span>${feedbackEnabled ? `反馈 ${result.feedback_count}` : "反馈未启用"}</span>
      </div>
    </article>
  `;
}

/** 以稳定 dt/dd 行渲染一个已格式化指标。 */
function metric(label, value) {
  return `<div><dt>${label}</dt><dd>${escapeHtml(String(value))}</dd></div>`;
}

/** 仅为完整创建 composite owner 渲染 Dialog；候选失败时保留可追踪重试入口。 */
function renderCreateDialog(state, owner) {
  if (!owner.canCreate) return "";
  const options = state.createOptions ?? { projects: [], environments: [], modelAliases: [], models: [] };
  const unavailable = state.createOptionsState !== "ready";
  const optionsError = state.createOptionsError;
  return `
    <div class="experiment-dialog-backdrop" data-experiment-dialog${state.createOpen ? "" : " hidden"}>
      <section class="experiment-dialog" role="dialog" aria-modal="true" aria-labelledby="experiment-create-title">
        <div class="experiment-dialog-heading">
          <h3 id="experiment-create-title">新建 A/B 实验</h3>
          <button type="button" class="icon-command" data-experiment-create-close title="关闭" aria-label="关闭创建实验对话框">×</button>
        </div>
        <form data-experiment-create-form novalidate>
          <label class="experiment-field span-2"><span>名称</span><input name="name" required autocomplete="off"></label>
          ${selectField("project_id", "Project", options.projects, unavailable)}
          ${selectField("environment_id", "Environment", options.environments, unavailable)}
          ${selectField("model_alias_id", "Model Alias", options.modelAliases, unavailable)}
          ${selectField("model_a_id", "模型 A", options.models, unavailable)}
          ${selectField("model_b_id", "模型 B", options.models, unavailable)}
          ${numberField("target_sample_count", "目标样本", 2, 100000, 1000)}
          ${numberField("traffic_sample_bps", "采样流量 bps", 1, 10000, 2500)}
          ${numberField("model_a_allocation_bps", "模型 A 分配 bps", 1, 9999, 5000)}
          ${numberField("sampling_duration_seconds", "采样时长（秒）", 60, 604800, 86400)}
          <label class="experiment-toggle"><input type="checkbox" name="feedback_collection_enabled" checked><span>收集反馈</span></label>
          ${numberField("feedback_window_seconds", "反馈窗口（秒）", 60, 604800, 86400)}
          <div class="experiment-dialog-status span-2" data-experiment-create-live role="status" aria-live="polite">
            ${unavailable
              ? `${escapeHtml(optionsError?.message ?? "创建选项暂不可用")} · request_id: ${escapeHtml(optionsError?.requestId ?? "-")}`
              : ""}
          </div>
          <div class="experiment-dialog-actions span-2">
            ${unavailable ? `<button type="button" data-experiment-create-retry>重新加载</button>` : ""}
            <button type="button" data-experiment-create-cancel>取消</button>
            <button type="submit" class="primary"${unavailable || state.createState === "submitting" ? " disabled" : ""}>${state.createState === "submitting" ? "正在创建" : "创建实验"}</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

/** 渲染作用域 select，并把已裁剪 owner 元数据放入 option data 属性供联动过滤。 */
function selectField(name, label, options, disabled) {
  return `<label class="experiment-field"><span>${label}</span><select name="${name}" required${disabled ? " disabled" : ""}><option value="">请选择</option>${options.map((option) => `<option value="${escapeAttribute(option.id)}"${ownerAttributes(option)}>${escapeHtml(option.label)}</option>`).join("")}</select></label>`;
}

/** 只输出 Project/Environment/Provider 白名单元数据，tenant 永不进入 DOM。 */
function ownerAttributes(option) {
  return ["project_id", "environment_id", "environment", "code", "provider_id"]
    .filter((key) => option[key])
    .map((key) => ` data-${key.replaceAll("_", "-")}="${escapeAttribute(option[key])}"`)
    .join("");
}

/** 渲染只允许整数的数值输入，范围与 POST DTO 完全一致。 */
function numberField(name, label, minimum, maximum, value) {
  return `<label class="experiment-field"><span>${label}</span><input type="number" inputmode="numeric" name="${name}" min="${minimum}" max="${maximum}" step="1" value="${value}" required></label>`;
}

/** 渲染创建成功或固定失败反馈，只展示安全 request_id。 */
function renderCreateFeedback(state) {
  if (state.createState === "success") return `实验已创建 · request_id: ${escapeHtml(state.createRequestId ?? "-")}`;
  if (state.createState === "error") return `${escapeHtml(state.createError?.message ?? "实验创建失败")} · request_id: ${escapeHtml(state.createError?.requestId ?? "-")}`;
  return "";
}

/** 渲染统一错误码和 request_id，不输出服务端 message 或 diagnosis。 */
function renderError(error, fallback) {
  return `<div class="experiment-error" role="alert"><strong>${escapeHtml(error?.message ?? fallback)}</strong><code>${escapeHtml(error?.code ?? "experiment.unavailable")}</code><span>request_id: ${escapeHtml(error?.requestId ?? "-")}</span></div>`;
}

/** 将整数 basis points 格式化为固定两位百分比。 */
function formatBps(value) {
  return `${(Number(value) / 100).toFixed(2)}%`;
}

/** 将 nullable basis points 格式化为百分比或短横线。 */
function formatNullableBps(value) {
  return value === null ? "-" : formatBps(value);
}

/** 将微秒整数格式化为毫秒，零样本保持短横线。 */
function formatMicros(value) {
  if (value === null) return "-";
  const micros = BigInt(value);
  return `${micros / 1_000n}.${String(micros % 1_000n).padStart(3, "0")} ms`;
}

/** 将人民币微元整数格式化为六位小数，不使用浮点参与权威计算。 */
function formatCnyMicros(value) {
  const micros = BigInt(value);
  const yuan = micros / 1_000_000n;
  const fraction = String(micros % 1_000_000n).padStart(6, "0");
  return `¥${yuan}.${fraction}`;
}

/** 将 nullable 人民币微元格式化为金额或短横线。 */
function formatNullableCnyMicros(value) {
  return value === null ? "-" : formatCnyMicros(value);
}

/** 将 canonical UTC 时间转换为当前浏览器中文本地时间。 */
function formatTime(value) {
  if (value === null || value === undefined || value === "") return "-";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "-";
}

/** 转义所有 HTML 文本节点，防止租户可控名称形成标记。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 在 HTML 文本转义基础上额外处理模板属性中的反引号。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
