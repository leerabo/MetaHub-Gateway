/** Dense PRD-138 Platform work surface for signed gates, waves, rollback, and RC review. */

import {
  LAUNCH_ACTORS,
  LAUNCH_GATE_CODES,
  LAUNCH_WAVES,
} from "../../lib/launchGovernance.js";

const PREVIOUS_WAVE = Object.freeze({
  internal: "pre_launch",
  personal_pilot: "internal",
  enterprise_pilot: "personal_pilot",
  managed_private_pilot: "enterprise_pilot",
});

export function renderLaunchGates(state, permissions) {
  const projection = state.launchGovernance ?? { gates: [], candidate: null };
  const gates = Array.isArray(projection.gates) ? projection.gates : [];
  const candidate = projection.candidate ?? null;
  const evidence = new Map((candidate?.evidence ?? []).map((item) => [item.gate_code, item]));
  const busy = Boolean(state.loading || state.launchIntent);
  const freshCount = gates.filter((gate) => freshEvidence(evidence.get(gate.gate_code))).length;
  const approvedCount = (candidate?.decisions ?? []).filter(
    (item) => item.decision === "approved",
  ).length;
  const completedWaves = (candidate?.waves ?? []).filter(
    (item) => item.status === "completed",
  ).length;
  const blockers = candidate ? Math.max(0, gates.length - freshCount)
    + Math.max(0, 6 - approvedCount) : gates.length || 20;
  return `<div class="platform-launch" data-platform-launch-root>
    <header class="platform-page-heading">
      <div><p>发布治理</p><h1>发布门禁</h1></div>
      <div class="platform-launch-heading-actions"><span class="platform-launch-boundary">候选发布阶段</span><button class="platform-icon-button" type="button" data-platform-action="launch-refresh" title="刷新发布门禁" aria-label="刷新发布门禁"${busy ? " disabled" : ""}>&#8635;</button></div>
    </header>
    <section class="platform-metrics platform-launch-metrics" aria-label="发布就绪摘要">
      ${metric("有效门禁", freshCount, `需完成 ${gates.length || 20} 项`)}
      ${metric("独立签署", approvedCount, "需 6 人签署")}
      ${metric("已完成批次", completedWaves, "共 4 个顺序批次")}
      ${metric("待解决项", blockers, candidate?.status ?? "暂无候选版本")}
    </section>
    ${permissions.has("platform.launch.create") ? candidateCreation(gates, busy) : ""}
    ${candidate ? candidateSummary(candidate) : noCandidate()}
    ${gateRegistry(gates, candidate, evidence, permissions, busy)}
    ${candidate ? decisionRegistry(candidate, permissions, busy) : ""}
    ${candidate ? waveRegistry(candidate, permissions, busy) : ""}
    ${candidate ? rollbackRegistry(candidate, permissions, busy) : ""}
    ${candidate ? reviewRegistry(candidate, permissions, busy) : ""}
  </div>`;
}

function candidateCreation(gates, busy) {
  if (gates.length !== 20) return "";
  return `<details class="platform-band platform-launch-create">
    <summary><span>创建候选版本</span><small>20 项门禁 · 4 个批次</small></summary>
    <form data-platform-launch-create-form autocomplete="off">
      <div class="platform-launch-input-grid">
        ${digestInput("代码校验值", "codeTreeSha256", busy)}
        ${digestInput("迁移集校验值", "migrationSetSha256", busy)}
        ${digestInput("契约集校验值", "contractSetSha256", busy)}
        ${digestInput("测试清单校验值", "testManifestSha256", busy)}
      </div>
      <fieldset class="platform-launch-artifact-set"><legend>门禁产物</legend>
        <div class="platform-launch-artifact-head"><span>门禁项</span><span>组织空间</span><span>产物校验值</span></div>
        ${LAUNCH_GATE_CODES.map((code) => `<div class="platform-launch-artifact-row"><strong>${escapeHtml(gateLabel(code))}</strong><input name="artifactWorkspace:${escapeAttribute(code)}" required pattern="wsp_[0-9a-f]{32}" placeholder="wsp_…"${disabled(busy)} /><input name="artifactSha256:${escapeAttribute(code)}" required pattern="[0-9a-f]{64}" placeholder="SHA-256"${disabled(busy)} /></div>`).join("")}
      </fieldset>
      <fieldset class="platform-launch-wave-set"><legend>发布批次计划</legend>
        ${LAUNCH_WAVES.map((wave) => `<section class="platform-launch-wave-plan"><h3>${escapeHtml(waveLabel(wave))}</h3><div>
          ${textInput("组织空间", `waveWorkspace:${wave}`, "wsp_[0-9a-f]{32}", busy)}
          ${textInput("分组校验值", `cohortSha256:${wave}`, "[0-9a-f]{64}", busy)}
          ${numberInput("功能标记代数", `featureGeneration:${wave}`, 1, 1, busy)}
          ${textInput("迁移计划", `migrationSha256:${wave}`, "[0-9a-f]{64}", busy)}
          ${textInput("回滚计划", `rollbackSha256:${wave}`, "[0-9a-f]{64}", busy)}
          ${numberInput("观察时长（秒）", `observationSeconds:${wave}`, 60, 60, busy, 604800)}
        </div></section>`).join("")}
      </fieldset>
      ${stepUpFields(busy)}
      <label class="platform-check platform-launch-confirm"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>确认候选版本配置</span></label>
      <button class="platform-primary" type="submit"${disabled(busy)}>创建候选版本</button>
    </form>
  </details>`;
}

function candidateSummary(candidate) {
  return `<section class="platform-band platform-launch-candidate">
    <div class="platform-section-heading"><div><p>Generation ${integer(candidate.generation)}</p><h2>Candidate ${escapeHtml(shortReference(candidate.launch_candidate_id))}</h2></div>${status(candidate.status)}</div>
    <dl>
      ${fact("修订号", integer(candidate.revision))}
      ${fact("组合校验", shortHash(candidate.candidate_sha256), true)}
      ${fact("门禁规则", shortHash(candidate.gate_policy_set_sha256), true)}
      ${fact("更新时间", formatTime(candidate.updated_at))}
      ${fact("验证证据", `${candidate.evidence.length} / 20`)}
      ${fact("签署结论", `${candidate.decisions.length} / 6`)}
      ${fact("批次", `${candidate.waves.filter((item) => item.status === "completed").length} / 4`)}
      ${fact("发布范围", candidate.review?.conclusion ?? "release_candidate only")}
    </dl>
  </section>`;
}

function noCandidate() {
  return `<section class="platform-band platform-launch-empty"><div class="platform-section-heading"><div><p>发布代数</p><h2>暂无候选版本</h2></div>${status("collecting")}</div></section>`;
}

function gateRegistry(gates, candidate, evidence, permissions, busy) {
  const canSubmit = permissions.has("platform.launch.evidence.submit")
    && candidate?.status === "collecting";
  return `<section class="platform-band platform-launch-gates">
    <div class="platform-section-heading"><div><p>已签名验证结果</p><h2>门禁清单</h2></div><span>${integer(gates.length)} policies</span></div>
    <div class="platform-table-wrap"><table class="platform-launch-gate-table">
      <thead><tr><th>门禁项</th><th>规则校验</th><th>验证证据</th><th>有效期</th><th>指标</th></tr></thead>
      <tbody>${gates.map((gate) => gateRow(gate, evidence.get(gate.gate_code))).join("") || emptyRow("门禁清单暂时不可用", 5)}</tbody>
    </table></div>
    ${canSubmit ? `<div class="platform-launch-evidence-forms">${gates.filter((gate) => !evidence.has(gate.gate_code)).map((gate) => evidenceForm(gate, candidate, busy)).join("")}</div>` : ""}
  </section>`;
}

function gateRow(gate, proof) {
  const freshness = proof ? (freshEvidence(proof) ? "fresh" : "stale") : "pending";
  return `<tr>
    <td data-label="门禁项"><strong>${escapeHtml(gateLabel(gate.gate_code))}</strong><small>${escapeHtml(gate.verifier_code)} · v${integer(gate.verifier_version)}</small></td>
    <td data-label="规则校验"><code>${escapeHtml(shortHash(gate.policy_sha256))}</code><small>${integer(gate.freshness_seconds)} 秒有效期</small></td>
    <td data-label="验证证据">${status(proof?.outcome ?? "pending")} ${status(freshness)}</td>
    <td data-label="有效期"><strong>${proof ? formatTime(proof.expires_at) : "-"}</strong><small>${proof ? shortReference(proof.workspace_id) : "尚未提交"}</small></td>
    <td data-label="指标"><strong>${proof?.metric_value ?? "-"}</strong><small>${escapeHtml(proof?.metric_unit ?? "全部通过")}</small></td>
  </tr>`;
}

function evidenceForm(gate, candidate, busy) {
  return `<details><summary><span>${escapeHtml(gateLabel(gate.gate_code))}</span><small>Submit signed evidence</small></summary>
    <form data-platform-launch-evidence-form autocomplete="off">
      ${hidden("launchGateId", gate.launch_gate_id)}${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${hidden("verifierCode", gate.verifier_code)}
      ${textInput("组织空间", "workspaceId", "wsp_[0-9a-f]{32}", busy)}
      ${textInput("产物校验值", "artifactSha256", "[0-9a-f]{64}", busy)}
      <label><span>结果</span><select name="outcome"${disabled(busy)}><option value="passed">通过</option><option value="failed">失败</option></select></label>
      <label><span>Metric value</span><input name="metricValue" type="number" step="any"${disabled(busy)} /></label>
      <label><span>Metric unit</span><input name="metricUnit" pattern="[a-z][a-z0-9_./%-]{0,31}"${disabled(busy)} /></label>
      ${textInput("Environment digest", "environmentSha256", "[0-9a-f]{64}", busy)}
      ${textInput("Provenance digest", "provenanceSha256", "[0-9a-f]{64}", busy)}
      <label><span>Started</span><input name="startedAt" type="datetime-local" step="1" required${disabled(busy)} /></label>
      <label><span>Finished</span><input name="finishedAt" type="datetime-local" step="1" required${disabled(busy)} /></label>
      <label><span>Verifier signature</span><input name="signature" type="password" autocomplete="off" minlength="43" maxlength="512" required data-platform-sensitive${disabled(busy)} /></label>
      ${stepUpFields(busy)}
      <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>确认已签名证明</span></label>
      <button class="platform-primary" type="submit"${disabled(busy)}>提交验证证据</button>
    </form>
  </details>`;
}

function decisionRegistry(candidate, permissions, busy) {
  const decisions = new Map(candidate.decisions.map((item) => [item.actor_kind, item]));
  const canSign = candidate.status === "signing";
  return `<section class="platform-band platform-launch-decisions">
    <div class="platform-section-heading"><div><p>独立操作员</p><h2>发布审批</h2></div><span>${integer(candidate.decisions.length)} / 6</span></div>
    <div class="platform-launch-decision-grid">${LAUNCH_ACTORS.map((actor) => {
      const decision = decisions.get(actor);
      const allowed = canSign && !decision && permissions.has(`platform.launch.sign.${actor}`);
      return `<section><header><div><strong>${escapeHtml(actorLabel(actor))}</strong><small>${decision ? shortReference(decision.operator_id) : "等待签署"}</small></div>${status(decision?.decision ?? "pending")}</header>${decision ? `<dl>${fact("操作员修订号", integer(decision.operator_revision))}${fact("原因", decision.reason_code)}${fact("签署时间", formatTime(decision.decided_at))}${fact("签署证明", shortHash(decision.signature_sha256), true)}</dl>` : ""}${allowed ? decisionForm(actor, candidate, busy) : ""}</section>`;
    }).join("")}</div>
  </section>`;
}

function decisionForm(actor, candidate, busy) {
  return `<form data-platform-launch-decision-form autocomplete="off">
    ${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${hidden("actorKind", actor)}
    <label><span>结论</span><select name="approved"${disabled(busy)}><option value="true">批准发布</option><option value="false">不批准</option></select></label>
    <label><span>原因代码</span><input name="reasonCode" required pattern="[a-z][a-z0-9_.-]{2,79}"${disabled(busy)} /></label>
    ${stepUpFields(busy)}
    <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>确认独立审批结论</span></label>
    <button type="submit"${disabled(busy)}>签署结论</button>
  </form>`;
}

function waveRegistry(candidate, permissions, busy) {
  const canExecute = permissions.has("platform.launch.execute");
  return `<section class="platform-band platform-launch-waves">
    <div class="platform-section-heading"><div><p>按批次发布</p><h2>发布批次</h2></div><span>4 个固定阶段</span></div>
    <div class="platform-launch-wave-list">${candidate.waves.map((wave) => `<section class="platform-launch-wave-row"><header><span>${integer(wave.wave_sequence)}</span><div><strong>${escapeHtml(waveLabel(wave.wave_kind))}</strong><small>${escapeHtml(shortReference(wave.workspace_id))}</small></div>${status(wave.status)}</header><dl>${fact("观察时长", `${integer(wave.observation_seconds)}s`)}${fact("截止时间", formatOptionalTime(wave.observation_deadline_at))}${fact("功能标记代数", integer(wave.feature_flag_generation))}${fact("健康状态", shortHash(wave.health_watermark_sha256), true)}</dl>${canExecute ? waveActions(wave, candidate, busy) : ""}</section>`).join("")}</div>
  </section>`;
}

function waveActions(wave, candidate, busy) {
  const operations = wave.status === "pending" ? ["start_wave"]
    : wave.status === "running" ? ["pause_wave", "complete_wave"]
      : wave.status === "paused" ? ["resume_wave"] : [];
  return operations.length ? `<div class="platform-launch-wave-actions">${operations.map((operation) => `<details><summary>${escapeHtml(operationLabel(operation))}</summary><form data-platform-launch-wave-form autocomplete="off">${hidden("operation", operation)}${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${hidden("waveKind", wave.wave_kind)}${operation === "complete_wave" ? digestInput("健康校验值", "healthWatermarkSha256", busy) : ""}${stepUpFields(busy)}<label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>Confirm ${escapeHtml(operationLabel(operation).toLowerCase())}</span></label><button type="submit"${disabled(busy)}>${escapeHtml(operationLabel(operation))}</button></form></details>`).join("")}</div>` : "";
}

function rollbackRegistry(candidate, permissions, busy) {
  const pending = candidate.rollbacks.filter((item) => item.status === "pending");
  const canRollback = permissions.has("platform.launch.rollback");
  const rollbackStates = new Set(["launching", "paused", "review_pending", "release_candidate"]);
  const eligible = candidate.waves.filter((wave) => new Set(["running", "paused", "completed"]).has(wave.status));
  return `<section class="platform-band platform-launch-rollbacks">
    <div class="platform-section-heading"><div><p>检查点恢复</p><h2>回滚</h2></div><span>${integer(candidate.rollbacks.length)} records</span></div>
    ${candidate.rollbacks.length ? `<div class="platform-launch-rollback-list">${candidate.rollbacks.map((item) => rollbackRow(item, candidate, canRollback, busy)).join("")}</div>` : '<p class="platform-empty">暂无回滚记录。</p>'}
    ${canRollback && rollbackStates.has(candidate.status) && pending.length === 0 ? `<div class="platform-launch-rollback-requests">${eligible.map((wave) => rollbackRequestForm(wave, candidate, busy)).join("")}</div>` : ""}
  </section>`;
}

function rollbackRow(item, candidate, canRollback, busy) {
  return `<section><header><div><strong>${escapeHtml(waveLabel(item.from_wave_kind))} → ${escapeHtml(waveLabel(item.to_wave_kind))}</strong><small>${escapeHtml(item.trigger_code)} · ${formatTime(item.requested_at)}</small></div>${status(item.status)}</header><dl>${fact("检查点", shortHash(item.checkpoint_sha256), true)}${fact("任务证明", shortHash(item.job_receipt_sha256), true)}${fact("验证结果", shortHash(item.verification_sha256), true)}${fact("修订号", integer(item.revision))}</dl>${canRollback && item.status === "pending" ? rollbackVerifyForm(item, candidate, busy) : ""}</section>`;
}

function rollbackRequestForm(wave, candidate, busy) {
  return `<details><summary><span>Rollback ${escapeHtml(waveLabel(wave.wave_kind))}</span><small>to ${escapeHtml(waveLabel(PREVIOUS_WAVE[wave.wave_kind]))}</small></summary><form data-platform-launch-rollback-request-form autocomplete="off">${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${hidden("fromWaveKind", wave.wave_kind)}${hidden("toWaveKind", PREVIOUS_WAVE[wave.wave_kind])}<label><span>触发原因</span><select name="triggerCode"${disabled(busy)}><option value="gate_failed">门禁失败</option><option value="health_slo">服务健康目标</option><option value="payment_failure">支付失败</option><option value="security_finding">安全问题</option><option value="operator_request">操作员申请</option></select></label>${digestInput("Trigger digest", "triggerSha256", busy)}${digestInput("Checkpoint digest", "checkpointSha256", busy)}${digestInput("Job receipt digest", "jobReceiptSha256", busy)}${stepUpFields(busy)}<label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>确认回滚申请</span></label><button class="platform-danger" type="submit"${disabled(busy)}>申请回滚</button></form></details>`;
}

function rollbackVerifyForm(item, candidate, busy) {
  return `<form data-platform-launch-rollback-verify-form autocomplete="off">${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", item.revision)}${hidden("launchRollbackId", item.launch_rollback_id)}<label><span>结果</span><select name="outcome"${disabled(busy)}><option value="passed">通过</option><option value="failed">失败</option></select></label>${digestInput("Verification digest", "verificationSha256", busy)}${stepUpFields(busy)}<label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>确认独立验证</span></label><button type="submit"${disabled(busy)}>验证回滚</button></form>`;
}

function reviewRegistry(candidate, permissions, busy) {
  const canReview = permissions.has("platform.launch.execute")
    && candidate.status === "review_pending" && !candidate.review;
  return `<section class="platform-band platform-launch-review">
    <div class="platform-section-heading"><div><p>发布后复核</p><h2>候选版本复核</h2></div>${status(candidate.review?.conclusion ?? "pending")}</div>
    ${candidate.review ? `<dl>${fact("结论", candidate.review.conclusion)}${fact("摘要", shortHash(candidate.review.summary_sha256), true)}${fact("后续操作", shortHash(candidate.review.action_items_sha256), true)}${fact("复核时间", formatTime(candidate.review.reviewed_at))}</dl>` : '<p class="platform-empty">四个批次全部完成后可进行复核。</p>'}
    ${canReview ? `<form data-platform-launch-review-form autocomplete="off">${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${digestInput("复核摘要", "summarySha256", busy)}${digestInput("后续事项", "actionItemsSha256", busy)}${stepUpFields(busy)}<label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>确认候选版本结论</span></label><button class="platform-primary" type="submit"${disabled(busy)}>完成候选版本复核</button></form>` : ""}
  </section>`;
}

function stepUpFields(busy) {
  return `<label><span>再次验证方式</span><select name="method"${disabled(busy)}><option value="totp">TOTP</option><option value="webauthn">安全密钥</option></select></label><label><span>动态验证码</span><input name="verificationCode" type="password" inputmode="numeric" autocomplete="one-time-code" minlength="6" maxlength="8" data-platform-sensitive${disabled(busy)} /></label>`;
}

function digestInput(label, name, busy) {
  return textInput(label, name, "[0-9a-f]{64}", busy);
}

function textInput(label, name, pattern, busy) {
  return `<label><span>${escapeHtml(label)}</span><input name="${escapeAttribute(name)}" required pattern="${escapeAttribute(pattern)}"${disabled(busy)} /></label>`;
}

function numberInput(label, name, value, minimum, busy, maximum = null) {
  return `<label><span>${escapeHtml(label)}</span><input name="${escapeAttribute(name)}" type="number" required value="${integer(value)}" min="${integer(minimum)}"${maximum === null ? "" : ` max="${integer(maximum)}"`}${disabled(busy)} /></label>`;
}

function hidden(name, value) {
  return `<input name="${escapeAttribute(name)}" type="hidden" value="${escapeAttribute(value)}" />`;
}

function metric(label, value, detail) {
  return `<div><span>${escapeHtml(label)}</span><strong>${integer(value)}</strong><small>${escapeHtml(detail)}</small></div>`;
}

function fact(label, value, code = false) {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${code ? `<code>${escapeHtml(value)}</code>` : escapeHtml(value)}</dd></div>`;
}

function status(value) {
  const text = String(value ?? "unknown");
  const kind = /passed|approved|completed|fresh|ready|release_candidate/u.test(text)
    ? "is-ok" : /pending|collecting|signing|paused|review_pending|stale/u.test(text)
      ? "is-warn" : /failed|rejected|rolled_back|rollback_pending/u.test(text)
        ? "is-danger" : "";
  const label = ({ pending: "待提交", collecting: "准备中", fresh: "有效", stale: "已过期", passed: "通过", failed: "失败", approved: "已批准", rejected: "已拒绝", completed: "已完成", running: "运行中", paused: "已暂停", signing: "待签署", release_candidate: "候选版本", unknown: "未知" })[text] ?? text.replaceAll("_", " ");
  return `<span class="platform-status ${kind}">${escapeHtml(label)}</span>`;
}

function freshEvidence(value) {
  return value?.outcome === "passed" && Number.isFinite(Date.parse(value.expires_at))
    && Date.parse(value.expires_at) > Date.now();
}

function gateLabel(value) {
  return ({
    wb: "WB", api: "API", bb: "BB", gr: "GR", perf: "性能", web: "Web",
    ui: "UI", ucd: "UCD", comp: "兼容性", sec: "安全", flow: "流程",
    migration: "迁移", disaster_recovery: "灾难恢复", payment: "支付",
    edge: "私有网关", privacy: "隐私", compliance: "合规",
    supply_chain: "供应链", tenant_isolation: "组织隔离",
    long_stability: "72h stability",
  })[value] ?? String(value).replaceAll("_", " ");
}

function actorLabel(value) {
  return ({ product: "产品", sre: "SRE", security: "安全", test: "测试", finance: "财务", legal: "法务" })[value] ?? value;
}

function waveLabel(value) {
  return ({ pre_launch: "发布前", internal: "内部", personal_pilot: "个人试用", enterprise_pilot: "企业试用", managed_private_pilot: "托管私有试用" })[value] ?? String(value).replaceAll("_", " ");
}

function operationLabel(value) {
  return ({ start_wave: "开始", pause_wave: "暂停", resume_wave: "继续", complete_wave: "完成" })[value] ?? value;
}

function shortHash(value) {
  const text = String(value ?? "");
  return text.length === 64 ? `${text.slice(0, 10)}…${text.slice(-8)}` : "-";
}

function shortReference(value) {
  const text = String(value ?? "");
  return text.length > 18 ? `${text.slice(0, 9)}…${text.slice(-5)}` : text || "-";
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function formatOptionalTime(value) {
  return value ? formatTime(value) : "-";
}

function integer(value) {
  return Number.isSafeInteger(Number(value)) ? String(Number(value)) : "0";
}

function disabled(value) { return value ? " disabled" : ""; }
function emptyRow(label, columns) { return `<tr><td colspan="${integer(columns)}" class="platform-empty"><span class="platform-empty-label">${escapeHtml(label)}</span></td></tr>`; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value); }
