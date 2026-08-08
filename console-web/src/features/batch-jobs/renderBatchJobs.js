/** PAGE-104 compact Batch workspace renderer. */

import { batchJobPageOwner } from "./batchJobPageState.js";

const STATUS_LABELS = Object.freeze({
  queued: "排队中",
  running: "执行中",
  completed: "已完成",
  partial_failed: "部分失败",
  failed: "失败",
  cancelled: "已取消",
  expired: "已到期",
  leased: "已领取",
  calling: "调用中",
  retry_wait: "等待重试",
  succeeded: "成功",
  outcome_unknown: "待对账",
});
const OPERATION_LABELS = Object.freeze({ summarize: "摘要", translate: "翻译", classify: "分类" });
const PRIORITY_LABELS = Object.freeze({ low: "低", normal: "普通", high: "高" });
const ERROR_LABELS = Object.freeze({
  "batch.authorization_revoked": "执行期间权限已撤销",
  "batch.budget_exceeded": "预算不足",
  "batch.file_security_blocked": "文件未通过安全准入",
  "batch.provider_outcome_unknown": "上游执行结果待对账",
  "batch.result_not_ready": "结果尚未可读",
  "batch.result_unavailable": "结果不可用",
  "batch.options_unavailable": "创建选项暂不可用",
  "batch.worker_unavailable": "批处理服务暂不可用",
});

export function renderBatchJobs(state, authContext, consoleShellState, i18n = null) {
  const owner = batchJobPageOwner(authContext, consoleShellState);
  if (!owner) return "";
  const view = state ?? {
    state: "loading",
    jobs: [],
    selectedJobId: "",
    job: null,
    tasks: [],
    detailState: "idle",
    resultState: "idle",
    canCreate: owner.canCreate,
    canReadResults: owner.canReadResults,
  };
  return `
    <div class="batch-workspace" data-batch-jobs-root data-batch-selected-job="${escapeAttribute(view.selectedJobId ?? "")}">
      <div class="batch-toolbar">
        <div>
          <h2 id="batch-jobs-title">${escapeHtml(t(i18n, "batch.title", "批处理任务"))}</h2>
          <span>${escapeHtml(t(i18n, "batch.scope", "当前租户"))}: ${escapeHtml(owner.tenantId)}</span>
        </div>
        <div class="batch-toolbar-actions">
          <label>
            <span class="sr-only">${escapeHtml(t(i18n, "batch.status_filter", "状态筛选"))}</span>
            <select data-batch-status-filter aria-label="${escapeAttribute(t(i18n, "batch.status_filter", "状态筛选"))}">
              <option value=""${view.status ? "" : " selected"}>全部状态</option>
              ${Object.entries(STATUS_LABELS)
                .filter(([status]) => ["queued", "running", "completed", "partial_failed", "failed", "cancelled", "expired"].includes(status))
                .map(([status, label]) => `<option value="${status}"${view.status === status ? " selected" : ""}>${escapeHtml(label)}</option>`)
                .join("")}
            </select>
          </label>
          <button type="button" class="icon-command" data-batch-refresh title="刷新批处理任务" aria-label="刷新批处理任务">↻</button>
          ${owner.canCreate
            ? `<button type="button" class="batch-create-command" data-batch-create><span aria-hidden="true">+</span><span>新建批处理</span></button>`
            : ""}
        </div>
      </div>
      <div class="batch-action-feedback${view.createError || view.cancelError ? " is-error" : ""}" data-batch-action-feedback role="status" aria-live="polite">${renderActionFeedback(view)}</div>
      ${renderList(view)}
      ${renderDetail(view, owner)}
      ${renderCreateDialog(view, owner)}
    </div>
  `;
}

function renderList(state) {
  if (state.state === "error") return renderError(state.error, "批处理列表读取失败");
  const jobs = Array.isArray(state.jobs) ? state.jobs : [];
  return `
    <section class="batch-list" aria-labelledby="batch-list-title">
      <div class="batch-section-heading">
        <h3 id="batch-list-title">任务列表</h3>
        <span>${jobs.length} 项</span>
      </div>
      ${state.state === "loading"
        ? `<div class="batch-empty">正在读取批处理任务</div>`
        : jobs.length
          ? `<div class="batch-table" role="table">
              <div class="batch-row is-head" role="row">
                <span>名称</span><span>状态</span><span>进度</span><span>优先级</span><span>成本</span><span>创建时间</span><span>操作</span>
              </div>
              ${jobs.map((job) => renderJobRow(job, state.selectedJobId)).join("")}
            </div>`
          : `<div class="batch-empty">当前租户没有批处理任务</div>`}
      <div class="batch-pager">
        <button type="button" data-batch-previous${state.canPrevious ? "" : " disabled"}>上一页</button>
        <button type="button" data-batch-next${state.canNext ? "" : " disabled"}>下一页</button>
      </div>
    </section>
  `;
}

function renderJobRow(job, selectedJobId) {
  const progress = job.progress ?? {};
  const completed = Number(progress.succeeded ?? 0)
    + Number(progress.failed ?? 0)
    + Number(progress.cancelled ?? 0)
    + Number(progress.expired ?? 0)
    + Number(progress.outcome_unknown ?? 0);
  const cost = job.cost ?? {};
  return `
    <div class="batch-row${job.batch_job_id === selectedJobId ? " is-selected" : ""}" role="row">
      <span data-label="名称"><strong>${escapeHtml(job.name)}</strong><code>${escapeHtml(job.batch_job_id)}</code></span>
      <span data-label="状态"><b class="batch-status tone-${escapeAttribute(job.status)}">${escapeHtml(STATUS_LABELS[job.status] ?? job.status)}</b></span>
      <span data-label="进度"><b>${escapeHtml(`${completed}/${progress.total ?? 0}`)}</b><small>${escapeHtml(OPERATION_LABELS[job.operation] ?? job.operation)}</small></span>
      <span data-label="优先级">${escapeHtml(PRIORITY_LABELS[job.priority] ?? job.priority)}</span>
      <span data-label="成本"><b>¥${escapeHtml(cost.posted_cny ?? "0.000000")}</b><small>估算 ¥${escapeHtml(cost.estimated_cny ?? "0.000000")}</small></span>
      <span data-label="创建时间"><time datetime="${escapeAttribute(job.created_at)}">${escapeHtml(formatTime(job.created_at))}</time></span>
      <span data-label="操作"><button type="button" class="icon-command" data-batch-select="${escapeAttribute(job.batch_job_id)}" title="查看详情" aria-label="查看 ${escapeAttribute(job.name)} 详情">→</button></span>
    </div>
  `;
}

function renderDetail(state, owner) {
  if (!state.selectedJobId) return "";
  if (state.detailState === "error") return renderError(state.detailError, "批处理详情读取失败");
  if (state.detailState !== "ready" || !state.job) {
    return `<section class="batch-detail"><div class="batch-empty">正在读取任务详情</div></section>`;
  }
  const job = state.job;
  return `
    <section class="batch-detail" aria-labelledby="batch-detail-title">
      <div class="batch-detail-heading">
        <div>
          <h3 id="batch-detail-title">${escapeHtml(job.name)}</h3>
          <code>${escapeHtml(job.batch_job_id)}</code>
        </div>
        <div class="batch-detail-actions">
          ${["queued", "running"].includes(job.status) && state.canCancel
            ? `<button type="button" data-batch-cancel="${escapeAttribute(job.batch_job_id)}"${state.cancelState === "submitting" ? " disabled" : ""}>${state.cancelState === "submitting" ? "正在取消" : "取消任务"}</button>`
            : ""}
        </div>
      </div>
      <div class="batch-detail-layout">
        <div>
          ${renderJobMetrics(job)}
          ${renderTasks(state, owner)}
        </div>
        ${renderResultRegion(state)}
      </div>
    </section>
  `;
}

function renderJobMetrics(job) {
  const cost = job.cost ?? {};
  return `
    <dl class="batch-metrics">
      <div><dt>状态</dt><dd>${escapeHtml(STATUS_LABELS[job.status] ?? job.status)}</dd></div>
      <div><dt>操作</dt><dd>${escapeHtml(OPERATION_LABELS[job.operation] ?? job.operation)}</dd></div>
      <div><dt>预计成本</dt><dd>¥${escapeHtml(cost.estimated_cny)}</dd></div>
      <div><dt>实际成本</dt><dd>¥${escapeHtml(cost.posted_cny)}</dd></div>
      <div><dt>待对账</dt><dd>${cost.reconciliation_pending ? `¥${escapeHtml(cost.possible_cny)}` : "无"}</dd></div>
      <div><dt>截止时间</dt><dd>${escapeHtml(formatTime(job.deadline_at))}</dd></div>
    </dl>
    ${job.failure_code ? `<div class="batch-fixed-failure"><b>${escapeHtml(ERROR_LABELS[job.failure_code] ?? "批处理执行失败")}</b><code>${escapeHtml(job.failure_code)}</code></div>` : ""}
  `;
}

function renderTasks(state, owner) {
  const tasks = Array.isArray(state.tasks) ? state.tasks : [];
  return `
    <section class="batch-tasks" aria-labelledby="batch-tasks-title">
      <div class="batch-section-heading"><h4 id="batch-tasks-title">文件任务</h4><span>${tasks.length} 项</span></div>
      ${tasks.length
        ? `<div class="batch-task-list">${tasks.map((task) => renderTask(task, owner)).join("")}</div>`
        : `<div class="batch-empty">暂无任务记录</div>`}
      <div class="batch-pager">
        <button type="button" data-batch-task-previous${state.canTaskPrevious ? "" : " disabled"}>上一页</button>
        <button type="button" data-batch-task-next${state.canTaskNext ? "" : " disabled"}>下一页</button>
      </div>
    </section>
  `;
}

function renderTask(task, owner) {
  return `
    <div class="batch-task">
      <div><strong>#${escapeHtml(String(task.task_index + 1))}</strong><code>${escapeHtml(task.file_id)}</code></div>
      <div><b>${escapeHtml(STATUS_LABELS[task.status] ?? task.status)}</b><small>${escapeHtml(`${task.attempt_count} 次尝试`)}</small></div>
      <div><span>${task.input_tokens ?? "-"} / ${task.output_tokens ?? "-"} tokens</span><small>${task.cost_cny ? `¥${escapeHtml(task.cost_cny)}` : "-"}</small></div>
      ${task.failure_code
        ? `<div class="batch-task-failure"><code>${escapeHtml(task.failure_code)}</code><span>${escapeHtml(ERROR_LABELS[task.failure_code] ?? "执行失败")}</span></div>`
        : ""}
      ${owner.canReadResults && task.result_available
        ? `<button type="button" class="icon-command" data-batch-result="${escapeAttribute(task.batch_task_id)}" title="打开结果" aria-label="打开任务结果">↗</button>`
        : ""}
    </div>
  `;
}

function renderResultRegion(state) {
  const error = state.resultState === "error" ? renderError(state.resultError, "结果读取失败") : "";
  return `
    <aside class="batch-result" aria-labelledby="batch-result-title">
      <div class="batch-section-heading">
        <h4 id="batch-result-title">任务结果</h4>
        <button type="button" class="icon-command" data-batch-result-close title="关闭结果" aria-label="关闭结果">×</button>
      </div>
      ${error}
      <div class="batch-result-status" data-batch-result-status role="status" aria-live="polite">${state.resultState === "loading" ? "正在读取结果" : ""}</div>
      <pre data-batch-result-body${state.resultState === "ready" ? "" : " hidden"}></pre>
      ${state.resultRequestId ? `<small>request_id: <code>${escapeHtml(state.resultRequestId)}</code></small>` : ""}
    </aside>
  `;
}

function renderCreateDialog(state, owner) {
  if (!owner.canCreate) return "";
  const options = state.createOptions ?? { projects: [], environments: [], modelAliases: [], files: [] };
  const ready = state.createOptionsState === "ready";
  const firstEnvironmentId = options.environments[0]?.environment_id ?? "";
  const complete = ready
    && options.projects.length > 0
    && options.environments.length > 0
    && options.modelAliases.length > 0
    && options.files.length > 0;
  return `
    <div class="batch-dialog-backdrop" data-batch-create-dialog${state.createOpen === true ? "" : " hidden"}>
      <section class="batch-dialog" role="dialog" aria-modal="true" aria-labelledby="batch-create-title" tabindex="-1">
        <div class="batch-dialog-heading"><h3 id="batch-create-title">新建批处理</h3><button type="button" class="icon-command" data-batch-create-close aria-label="关闭">×</button></div>
        ${state.createOptionsState === "error" ? renderError(state.createOptionsError, "创建选项读取失败") : ""}
        <form data-batch-create-form autocomplete="off"${state.createState === "submitting" ? " aria-busy=\"true\"" : ""}>
          <label>名称<input name="name" maxlength="120" required autocomplete="off"></label>
          <label>项目<select name="project_id" required${options.projects.length ? "" : " disabled"}>${renderProjectOptions(options.projects)}</select></label>
          <label>环境<select name="environment_id" required${options.environments.length ? "" : " disabled"}>${renderEnvironmentOptions(options.environments)}</select></label>
          <label>模型别名<select name="model_alias_id" required${options.modelAliases.length ? "" : " disabled"}>${renderAliasOptions(options.modelAliases, firstEnvironmentId)}</select></label>
          <div class="batch-operation-control" role="group" aria-label="操作类型">
            <label><input type="radio" name="operation" value="summarize" checked>摘要</label>
            <label><input type="radio" name="operation" value="translate">翻译</label>
            <label><input type="radio" name="operation" value="classify">分类</label>
          </div>
          <label data-batch-operation-field="summarize">输出语言（可选）<input name="output_language" maxlength="35" placeholder="zh-CN"></label>
          <label data-batch-operation-field="translate" hidden>目标语言<input name="target_language" maxlength="35" disabled></label>
          <label class="batch-classification-labels" data-batch-operation-field="classify" hidden>分类标签<textarea name="classification_labels" rows="3" maxlength="1619" disabled placeholder="每行一个标签，至少 2 个"></textarea></label>
          <label>优先级<select name="priority"><option value="low">低</option><option value="normal" selected>普通</option><option value="high">高</option></select></label>
          <label>最长执行秒数<input name="max_duration_seconds" type="number" min="30" max="3600" step="1" value="300"></label>
          <label>最大尝试次数<input name="max_attempts" type="number" min="1" max="3" step="1" value="2"></label>
          <label>最大输出 Token<input name="max_output_tokens" type="number" min="16" max="4096" step="16" value="1024"></label>
          <fieldset class="batch-file-selector">
            <legend>文件</legend>
            <span data-batch-file-count>已选择 0 个文件</span>
            <div>${renderFileOptions(options.files)}</div>
          </fieldset>
          <div class="batch-create-live" data-batch-create-live role="status" aria-live="polite"></div>
          <button type="submit"${complete && state.createState !== "submitting" ? "" : " disabled"}>${state.createState === "submitting" ? "正在提交" : "提交任务"}</button>
        </form>
      </section>
    </div>
  `;
}

function renderProjectOptions(projects) {
  if (!projects.length) return `<option value="">暂无可用项目</option>`;
  return projects.map((item) => (
    `<option value="${escapeAttribute(item.project_id)}">${escapeHtml(item.name)}</option>`
  )).join("");
}

function renderEnvironmentOptions(environments) {
  if (!environments.length) return `<option value="">暂无可用环境</option>`;
  return environments.map((item) => (
    `<option value="${escapeAttribute(item.environment_id)}">${escapeHtml(item.name)} / ${escapeHtml(item.code)}</option>`
  )).join("");
}

function renderAliasOptions(aliases, firstEnvironmentId) {
  if (!aliases.length) return `<option value="">暂无可用模型别名</option>`;
  return aliases.map((item) => {
    const available = item.environment_id === firstEnvironmentId;
    return `<option value="${escapeAttribute(item.model_alias_id)}" data-environment-id="${escapeAttribute(item.environment_id)}"${available ? "" : " hidden disabled"}>${escapeHtml(item.label)}</option>`;
  }).join("");
}

function renderFileOptions(files) {
  if (!files.length) return `<div class="batch-empty">暂无通过处理的文件</div>`;
  return files.map((item) => `
    <label><input type="checkbox" name="file_ids" value="${escapeAttribute(item.file_id)}"><span>${escapeHtml(item.name)}</span><code>${escapeHtml(item.file_id)}</code></label>
  `).join("");
}

function renderActionFeedback(state) {
  const error = state.createError || state.cancelError;
  const feedback = state.createFeedback || state.cancelFeedback;
  if (error) return `${escapeHtml(error.code)} · request_id: ${escapeHtml(error.requestId)}`;
  if (feedback) return `${escapeHtml(feedback.code)} · request_id: ${escapeHtml(feedback.requestId)}`;
  return "";
}

function renderError(error, fallback) {
  const code = String(error?.code ?? "batch.worker_unavailable");
  return `<div class="batch-error" role="alert"><b>${escapeHtml(ERROR_LABELS[code] ?? fallback)}</b><code>${escapeHtml(code)}</code><small>request_id: ${escapeHtml(error?.requestId ?? "-")}</small></div>`;
}

function formatTime(value) {
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime())
    ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(parsed)
    : "-";
}

function t(i18n, key, fallback) {
  return i18n?.t?.(key, {}, fallback) ?? fallback;
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
