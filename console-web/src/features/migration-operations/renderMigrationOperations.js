/** PRD-131 compact Workspace Migration Center renderer. */

import { migrationOperationPageOwner } from "./migrationOperationPageState.js";

const SOURCE_LABELS = Object.freeze({
  legacy_tenant: "旧租户",
  direct_provider: "模型直连",
  private_deployment: "私有部署",
});
const DOMAIN_LABELS = Object.freeze({
  audit: "审计",
  edge: "Edge",
  identity: "身份",
  ledger: "账本",
  policy: "策略",
  provider_reference: "Provider 引用",
  workspace: "Workspace",
});
const STATUS_LABELS = Object.freeze({
  ready: "可执行",
  blocked: "已阻断",
  validation_failed: "验证失败",
  observing: "观察中",
  completed: "已切换",
  failed: "失败",
  rolling_back: "回退中",
  rolled_back: "已回退",
  verified: "已验证",
  open: "待关闭",
});
const FEEDBACK_LABELS = Object.freeze({
  "migration.assessment_created": "只读评估已生成",
  "migration.assessment_replayed": "已恢复同一评估结果",
  "migration.run_created": "迁移运行已建立并进入观察",
  "migration.run_replayed": "已恢复同一运行结果",
  "migration.cutover_completed": "切换已完成",
  "migration.rolled_back": "回退与一致性复核已完成",
  "migration.differences_support_scoped": "当前为 Support 授权的脱敏差异视图",
});

export function renderMigrationOperations(state, authContext, consoleShellState) {
  const owner = migrationOperationPageOwner(authContext, consoleShellState);
  if (!owner) return "";
  const view = state ?? emptyView();
  const assessment = view.assessment;
  const run = view.run;
  return `<div class="migration-operations" data-migration-root>
    <header class="migration-toolbar">
      <div>
        <p class="migration-eyebrow">Workspace migration</p>
        <h2 id="migration-operations-title">迁移中心</h2>
        <p>${assessment ? escapeHtml(SOURCE_LABELS[assessment.source_kind]) : "尚未建立评估"} · ${run ? escapeHtml(STATUS_LABELS[run.status] ?? run.status) : "无活动运行"}</p>
      </div>
      <span class="migration-access-mode">${owner.canManage ? "可管理" : "只读"}</span>
    </header>
    ${renderFeedback(view.feedback)}
    ${renderStages(view)}
    <section class="migration-section" aria-labelledby="migration-assessment-title">
      <div class="migration-section-heading">
        <div><h3 id="migration-assessment-title">来源评估</h3>${assessment ? `<code>${escapeHtml(assessment.assessment_id)}</code>` : ""}</div>
        ${assessment ? statusBadge(assessment.status) : ""}
      </div>
      ${assessment ? renderAssessment(assessment) : renderEmptyAssessment(owner)}
      ${owner.canManage && (!run || run.status === "rolled_back")
        ? renderAssessmentForm(view, Boolean(assessment)) : ""}
    </section>
    ${assessment ? renderDifferences(view, owner) : ""}
    ${assessment ? renderPlanAndRun(view, owner) : ""}
    ${renderTransitionDialog(view, owner)}
  </div>`;
}

function renderStages(view) {
  const assessment = view.assessment;
  const run = view.run;
  const differenceTarget = run?.run_id ?? assessment?.assessment_id;
  const differencesClear = Boolean(
    differenceTarget && view.differencesFor === differenceTarget && view.differences.length === 0,
  );
  const stages = [
    ["评估", Boolean(assessment)],
    ["差异", differencesClear],
    ["运行", Boolean(run)],
    ["切换 / 回退", ["completed", "rolled_back"].includes(run?.status)],
  ];
  return `<ol class="migration-stages" aria-label="迁移阶段">${stages.map(([label, complete], index) => `
    <li class="${complete ? "complete" : "pending"}"><span>${index + 1}</span><strong>${label}</strong></li>
  `).join("")}</ol>`;
}

function renderEmptyAssessment(owner) {
  return `<div class="migration-empty"><strong>${owner.canManage ? "等待评估输入" : "当前 Workspace 尚无会话内评估"}</strong>${owner.canManage ? "" : "<span>由 Workspace owner 或 admin 创建评估后可查看脱敏差异。</span>"}</div>`;
}

function renderAssessmentForm(view, collapsed) {
  const content = `<form class="migration-assessment-form" data-migration-assessment-form autocomplete="off" novalidate>
    <div class="migration-form-grid">
      <label><span>来源类型</span><select name="source_kind" required${view.busy.assessment ? " disabled" : ""}>
        <option value="legacy_tenant">旧租户</option>
        <option value="direct_provider">模型直连</option>
        <option value="private_deployment">私有部署</option>
      </select></label>
      <label><span>Manifest 版本</span><input name="manifest_version" value="legacy-v1" minlength="3" maxlength="40" pattern="[a-z0-9][a-z0-9._-]+" required${view.busy.assessment ? " disabled" : ""}></label>
      <label class="migration-span-2"><span>Manifest SHA-256</span><input name="source_manifest_sha256" minlength="64" maxlength="64" pattern="[0-9a-f]{64}" spellcheck="false" required${view.busy.assessment ? " disabled" : ""}></label>
      <fieldset class="migration-domain-fieldset migration-span-2"><legend>迁移域</legend><div>${Object.entries(DOMAIN_LABELS).map(([value, label]) => `<label><input type="checkbox" name="domains" value="${value}" checked${view.busy.assessment ? " disabled" : ""}><span>${label}</span></label>`).join("")}</div></fieldset>
      <label><span>评估原因</span><select name="reason_code" required${view.busy.assessment ? " disabled" : ""}>
        <option value="initial_assessment">首次评估</option>
        <option value="reassessment">重新评估</option>
        <option value="cutover_precheck">切换前复核</option>
      </select></label>
      <label class="migration-confirm"><input type="checkbox" name="confirmed" required${view.busy.assessment ? " disabled" : ""}><span>确认使用当前 Workspace 的固定只读适配器</span></label>
    </div>
    <div class="migration-form-actions"><button type="submit" class="primary"${view.busy.assessment ? " disabled" : ""}>${view.busy.assessment ? "评估中" : "生成评估"}</button></div>
  </form>`;
  return collapsed
    ? `<details class="migration-new-assessment"><summary>新建评估</summary>${content}</details>`
    : content;
}

function renderAssessment(assessment) {
  return `<div class="migration-assessment-summary">
    <dl class="migration-facts">
      ${fact("对象", integer(assessment.object_count))}
      ${fact("阻断差异", integer(assessment.blocking_difference_count))}
      ${fact("需重录凭据", integer(assessment.credential_reentry_required_count))}
      ${fact("权限扩大", integer(assessment.permission_expansion_count))}
      ${fact("Revision", integer(assessment.revision))}
      ${fact("评估完成", formatTime(assessment.completed_at))}
    </dl>
    <div class="migration-table-scroll"><table class="migration-table migration-snapshot-table">
      <thead><tr><th>域</th><th>对象</th><th>Sequence</th><th>Watermark</th><th>Digest</th><th>结果</th></tr></thead>
      <tbody>${assessment.snapshots.map((snapshot) => `<tr>
        <th data-label="域">${escapeHtml(DOMAIN_LABELS[snapshot.domain_code] ?? snapshot.domain_code)}</th>
        <td data-label="对象">${integer(snapshot.source_count)} / ${integer(snapshot.target_count)}</td>
        <td data-label="Sequence">${integer(snapshot.source_sequence)} / ${integer(snapshot.target_sequence)}</td>
        <td data-label="Watermark">${integer(snapshot.source_watermark)} / ${integer(snapshot.target_watermark)}</td>
        <td data-label="Digest"><code>${shortHash(snapshot.source_sha256)}</code><span class="migration-pair-separator">/</span><code>${shortHash(snapshot.target_sha256)}</code></td>
        <td data-label="结果">${statusBadge(snapshot.matches ? "verified" : "validation_failed")}</td>
      </tr>`).join("")}</tbody>
    </table></div>
  </div>`;
}

function renderDifferences(view, owner) {
  const items = view.differences ?? [];
  const target = view.run?.run_id ?? view.assessment.assessment_id;
  const loaded = view.differencesFor === target;
  return `<section class="migration-section" aria-labelledby="migration-differences-title">
    <div class="migration-section-heading"><div><h3 id="migration-differences-title">差异</h3><span>${loaded ? `${items.length} 项` : "待读取"}</span></div><button type="button" data-migration-refresh-differences${view.busy.differences ? " disabled" : ""} aria-label="刷新差异" title="刷新差异">↻</button></div>
    ${loaded && items.length === 0 ? `<div class="migration-clear-state"><strong>无阻断差异</strong><span>目标计数、摘要、序列、水位与权限集合一致。</span></div>` : ""}
    ${loaded && items.length ? `<div class="migration-table-scroll"><table class="migration-table migration-difference-table">
      <thead><tr><th>域</th><th>差异</th><th>对象</th><th>Sequence</th><th>摘要</th><th>处置</th></tr></thead>
      <tbody>${items.map((item) => `<tr>
        <th data-label="域">${escapeHtml(DOMAIN_LABELS[item.domain_code] ?? item.domain_code)}</th>
        <td data-label="差异">${escapeHtml(differenceLabel(item.difference_code))}</td>
        <td data-label="对象">${integer(item.source_count)} / ${integer(item.target_count)}</td>
        <td data-label="Sequence">${integer(item.source_sequence)} / ${integer(item.target_sequence)}</td>
        <td data-label="摘要"><code>${shortHash(item.source_sha256)}</code><span class="migration-pair-separator">/</span><code>${shortHash(item.target_sha256)}</code></td>
        <td data-label="处置">${escapeHtml(remediationLabel(item.remediation_code))}</td>
      </tr>`).join("")}</tbody>
    </table></div>` : ""}
    ${!loaded ? `<div class="migration-empty"><span>${view.busy.differences ? "正在读取脱敏差异" : owner.canManage ? "刷新以读取当前评估差异" : "刷新以读取公开差异"}</span></div>` : ""}
  </section>`;
}

function renderPlanAndRun(view, owner) {
  const assessment = view.assessment;
  const plan = assessment.plan;
  const run = view.run;
  const canStart = owner.canManage && !run && assessment.status === "ready"
    && assessment.blocking_difference_count === 0;
  return `<section class="migration-section" aria-labelledby="migration-plan-title">
    <div class="migration-section-heading"><div><h3 id="migration-plan-title">计划与运行</h3><code>${escapeHtml(plan.plan_id)}</code></div>${statusBadge(run?.status ?? plan.status)}</div>
    <dl class="migration-plan-strip">
      ${fact("Generation", integer(run?.generation ?? plan.generation))}
      ${fact("计划停机", duration(plan.planned_downtime_seconds))}
      ${fact("回退时限", duration(plan.rollback_deadline_seconds))}
      ${fact("运行 Revision", run ? integer(run.revision) : "-")}
    </dl>
    ${canStart ? `<form class="migration-run-form" data-migration-run-form>
      <label><span>运行原因</span><select name="reason_code"${view.busy.run ? " disabled" : ""}><option value="customer_approved">客户已批准</option><option value="scheduled_window">计划窗口</option><option value="recovery_retry">恢复重试</option></select></label>
      <label class="migration-confirm"><input type="checkbox" name="confirmed" required${view.busy.run ? " disabled" : ""}><span>确认按当前评估 Revision 建立运行</span></label>
      <button type="submit" class="primary"${view.busy.run ? " disabled" : ""}>${view.busy.run ? "建立中" : "建立运行"}</button>
    </form>` : ""}
    ${!owner.canManage && !run ? `<div class="migration-empty"><span>当前权限仅允许查看评估与差异。</span></div>` : ""}
    ${run ? renderRun(run, view, owner) : ""}
  </section>`;
}

function renderRun(run, view, owner) {
  const canCutover = owner.canManage && run.status === "observing"
    && run.blocking_difference_count === 0 && run.rollback_plan?.status === "ready";
  const canRollback = owner.canManage && ["observing", "completed", "failed"].includes(run.status)
    && run.rollback_plan?.status === "ready";
  return `<div class="migration-run" data-migration-run="${escapeAttribute(run.run_id)}">
    <div class="migration-run-heading"><div><strong>${escapeHtml(run.run_id)}</strong><span>freeze generation ${integer(run.freeze_generation)}</span></div><div class="migration-run-actions">${canCutover ? `<button type="button" class="primary" data-migration-open-transition="cutover"${view.busy.transition ? " disabled" : ""}>执行切换</button>` : ""}${canRollback ? `<button type="button" class="danger" data-migration-open-transition="rollback"${view.busy.transition ? " disabled" : ""}>执行回退</button>` : ""}</div></div>
    <div class="migration-table-scroll"><table class="migration-table migration-checkpoint-table">
      <thead><tr><th>#</th><th>阶段</th><th>Watermark</th><th>输入</th><th>输出</th><th>状态</th></tr></thead>
      <tbody>${run.checkpoints.map((checkpoint) => `<tr>
        <td data-label="#">${integer(checkpoint.phase_sequence)}</td>
        <th data-label="阶段">${escapeHtml(phaseLabel(checkpoint.phase_code))}</th>
        <td data-label="Watermark">${integer(checkpoint.source_watermark)} / ${integer(checkpoint.target_watermark)}</td>
        <td data-label="输入"><code>${shortHash(checkpoint.input_sha256)}</code></td>
        <td data-label="输出"><code>${shortHash(checkpoint.output_sha256)}</code></td>
        <td data-label="状态">${statusBadge(checkpoint.status)}</td>
      </tr>`).join("")}</tbody>
    </table></div>
    ${run.rollback_plan ? `<dl class="migration-rollback-facts">${fact("最后安全检查点", run.rollback_plan.last_safe_checkpoint_id)}${fact("补偿序列", run.rollback_plan.compensation_order.map(phaseLabel).join(" → "))}${fact("回退计划", STATUS_LABELS[run.rollback_plan.status] ?? run.rollback_plan.status)}</dl>` : ""}
  </div>`;
}

function renderTransitionDialog(view, owner) {
  const dialog = view.dialog;
  const run = view.run;
  if (!owner.canManage || !dialog || !run) return "";
  const cutover = dialog.action === "cutover";
  const reasons = cutover
    ? [["customer_approved", "客户已批准"], ["scheduled_window", "计划窗口"], ["validation_recovered", "验证已恢复"]]
    : [["customer_requested_rollback", "客户要求回退"], ["validation_failed", "验证失败"], ["observation_failed", "观察失败"]];
  return `<dialog class="migration-dialog" data-migration-dialog aria-labelledby="migration-dialog-title" aria-busy="${dialog.busy}">
    <header><div><h3 id="migration-dialog-title">${cutover ? "确认执行切换" : "确认执行回退"}</h3><span>${escapeHtml(run.run_id)} · revision ${integer(run.revision)}</span></div><button type="button" class="migration-dialog-close" data-migration-close-transition aria-label="关闭" title="关闭"${dialog.busy ? " disabled" : ""}>×</button></header>
    ${dialog.error ? renderFeedback(dialog.error, "migration-dialog-error") : ""}
    <form data-migration-transition-form autocomplete="off" novalidate>
      <input type="hidden" name="action" value="${escapeAttribute(dialog.action)}">
      <label><span>原因</span><select name="reason_code" required${dialog.busy ? " disabled" : ""}>${reasons.map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select></label>
      <label><span>二次验证方式</span><select name="method" data-migration-step-up-method required${dialog.busy ? " disabled" : ""}><option value="totp">Authenticator 验证码</option><option value="webauthn">安全密钥</option><option value="recovery_code">恢复码</option></select></label>
      <label data-migration-step-up-code><span>验证码</span><input name="verification_code" type="password" maxlength="64" autocomplete="one-time-code" data-migration-sensitive required${dialog.busy ? " disabled" : ""}></label>
      <label class="migration-confirm"><input type="checkbox" name="confirmed" required${dialog.busy ? " disabled" : ""}><span>${cutover ? "确认冻结检查点与回退计划有效，并切换当前 Workspace" : "确认按补偿序列恢复上一安全 head"}</span></label>
      <div class="migration-dialog-actions"><button type="button" data-migration-close-transition${dialog.busy ? " disabled" : ""}>取消</button><button type="submit" class="${cutover ? "primary" : "danger"}"${dialog.busy ? " disabled" : ""}>${dialog.busy ? "验证中" : cutover ? "验证并切换" : "验证并回退"}</button></div>
    </form>
  </dialog>`;
}

function renderFeedback(feedback, className = "migration-feedback") {
  if (!feedback) return "";
  const label = feedback.kind === "success"
    ? FEEDBACK_LABELS[feedback.code] ?? feedback.code
    : errorLabel(feedback.code);
  return `<div class="${className} ${feedback.kind === "error" ? "error" : "success"}" role="${feedback.kind === "error" ? "alert" : "status"}"><strong>${escapeHtml(label)}</strong><span>code: ${escapeHtml(feedback.code)} · request_id: ${escapeHtml(feedback.requestId ?? "-")}</span></div>`;
}

function errorLabel(code) {
  const labels = {
    "migration.request_invalid": "迁移请求字段无效",
    "migration.response_invalid": "迁移响应合同无效",
    "migration.permission_denied": "当前成员没有迁移管理权限",
    "migration.not_found": "当前 Workspace 中找不到迁移记录",
    "migration.revision_conflict": "迁移状态已变化，请重新评估",
    "migration.state_conflict": "当前迁移状态不允许此操作",
    "migration.validation_changed": "验证结果已变化，切换已阻断",
    "migration.unavailable": "迁移服务暂时不可用",
    "step_up.required": "需要完成二次验证",
    "step_up.invalid": "二次验证授权无效",
    "workspace_session.csrf_invalid": "Workspace 安全校验已失效",
  };
  return labels[code] ?? "迁移操作未完成";
}

function fact(label, value) {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`;
}

function statusBadge(status) {
  const tone = ["ready", "verified", "completed", "rolled_back"].includes(status)
    ? "good" : ["validation_failed", "failed", "blocked"].includes(status) ? "danger" : "pending";
  return `<span class="migration-status tone-${tone}">${escapeHtml(STATUS_LABELS[status] ?? status)}</span>`;
}

function differenceLabel(code) {
  return ({
    count_mismatch: "对象数不一致",
    hash_mismatch: "摘要不一致",
    sequence_mismatch: "序列不一致",
    watermark_mismatch: "水位不一致",
    permission_expansion: "权限扩大",
  })[code] ?? code;
}

function remediationLabel(code) {
  return ({
    rebuild_target_projection: "重建目标投影",
    replay_missing_sequence: "重放缺失序列",
    wait_for_watermark: "等待水位追平",
    reassess_source: "重新评估来源",
    review_role_mapping: "复核角色映射",
  })[code] ?? code;
}

function phaseLabel(code) {
  return ({
    assessment: "评估",
    backfill: "回填",
    dual_read: "双读",
    freeze: "冻结",
    cutover: "切换",
    observe: "观察",
    rollback: "回退",
    freeze_writes: "冻结写入",
    restore_head: "恢复 head",
    replay_receipts: "重放 receipt",
    verify_owner: "复核 owner",
  })[code] ?? code;
}

function shortHash(value) {
  const text = String(value ?? "");
  return escapeHtml(text.length === 64 ? `${text.slice(0, 10)}…${text.slice(-6)}` : text);
}

function duration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "-";
  if (value >= 3600 && value % 3600 === 0) return `${value / 3600} 小时`;
  if (value >= 60 && value % 60 === 0) return `${value / 60} 分钟`;
  return `${value} 秒`;
}

function integer(value) {
  return Number.isSafeInteger(Number(value)) ? String(Number(value)) : "-";
}

function formatTime(value) {
  const date = new Date(String(value ?? ""));
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
}

function emptyView() {
  return { assessment: null, run: null, differences: [], differencesFor: null,
    feedback: null, dialog: null, busy: {} };
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
