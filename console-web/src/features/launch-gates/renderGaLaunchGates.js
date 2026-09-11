/** User-facing projection of the final GA boundary. */
export function renderGaLaunchGates(projection = {}) {
  const gates = Array.isArray(projection.gates) ? projection.gates : [];
  const candidate = projection.candidate;
  const launch = projection.launch;
  // Evidence is collected before the GA launch row exists.  Prefer the
  // candidate-stage projection and merge the launch projection after promotion.
  const evidence = new Set([
    ...(projection.evidence ?? []),
    ...(launch?.evidence ?? []),
  ].map((item) => item.ga_gate_id));
  const passed = gates.filter((gate) => evidence.has(gate.ga_gate_id)).length;
  const required = gates.length || 20;
  const status = launch?.status ?? (candidate ? "待发布" : "待准备");
  const statusClass = /launched|approved/u.test(String(launch?.status)) ? "is-ok"
    : /rolled_back|failed/u.test(String(launch?.status)) ? "is-danger" : "is-warn";
  const next = !launch ? (candidate ? "完成 GA 门禁证据后即可发布" : "等待 RC 形成可发布候选")
    : launch.status === "approved" ? "可以执行 GA 发布"
      : launch.status === "launched" ? (launch.review ? "发布后复核已完成" : "发布已完成，等待发布后复盘")
        : launch.status === "rollback_pending" ? "等待第二位操作员复核回滚"
          : launch.status === "rolled_back" ? (launch.review ? "回滚复核已完成" : "完成回滚后的发布复盘")
            : "请查看阻塞项并重新验证";
  return `<section class="platform-band platform-ga-summary" data-ga-launch-summary>
    <header class="platform-section-heading"><div><p>正式发布</p><h2>GA 发布状态</h2></div><span class="platform-status ${statusClass}">${escapeHtml(statusLabel(status))}</span></header>
    <p class="platform-ga-next" role="status">${escapeHtml(next)}</p>
    <div class="platform-metrics platform-ga-metrics" aria-label="GA 发布摘要">
      <div><span>新鲜门禁</span><strong>${passed} / ${required}</strong><small>全部通过后才可发布</small></div>
      <div><span>稳定性</span><strong>72 小时</strong><small>必须有连续稳定证据</small></div>
      <div><span>回滚</span><strong>双人复核</strong><small>申请人与验证人必须不同</small></div>
    </div>
    <details class="platform-ga-gates"><summary>查看门禁状态</summary><ul>${gates.map((gate) => `<li><span>${escapeHtml(gateLabel(gate.gate_code))}</span><strong class="platform-status ${evidence.has(gate.ga_gate_id) ? "is-ok" : "is-warn"}">${evidence.has(gate.ga_gate_id) ? "已通过" : "待验证"}</strong></li>`).join("")}</ul></details>
    ${nextAction(projection, gates, passed)}
  </section>`;
}

function nextAction(projection, gates, passed) {
  const candidate = projection.candidate;
  const launch = projection.launch;
  if (!candidate) return "";
  if (!launch && passed < gates.length) {
    const existing = new Set([
      ...(projection.evidence ?? []),
      ...(launch?.evidence ?? []),
    ].map((item) => item.ga_gate_id));
    const gate = gates.find((item) => !existing.has(item.ga_gate_id));
    const source = (candidate.evidence ?? []).find((item) => item.gate_code === gate?.gate_code);
    return gaEvidenceForm(candidate, gate, source);
  }
  if (!launch && passed === gates.length) return gaLaunchForm(candidate);
  if (launch?.status === "rollback_pending") {
    const rollback = (launch.rollbacks ?? []).find((item) => item.status === "pending");
    return rollback ? gaRollbackVerifyForm(launch, rollback) : "";
  }
  if ((launch?.status === "launched" || launch?.status === "rolled_back") && !launch.review) {
    return gaReviewForm(launch);
  }
  if (launch && ["approved", "launched"].includes(launch.status)) return gaRollbackRequestForm(launch);
  return "";
}

function gaEvidenceForm(candidate, gate, source) {
  if (!gate) return `<p class="platform-empty">GA 门禁目录尚未加载。</p>`;
  return `<details class="platform-ga-action" open><summary>提交下一项 GA 证据：${escapeHtml(gateLabel(gate.gate_code))}</summary>
    <form data-ga-evidence-form autocomplete="off">
      <input type="hidden" name="gaGateId" value="${escapeHtml(gate.ga_gate_id)}" /><input type="hidden" name="launchCandidateId" value="${escapeHtml(candidate.launch_candidate_id)}" /><input type="hidden" name="expectedRevision" value="${candidate.revision}" />
      <label><span>工作空间 ID</span><input name="workspaceId" value="${escapeHtml(source?.workspace_id ?? "")}" required /></label>
      <label><span>证据 SHA-256</span><input name="artifactSha256" value="${escapeHtml(source?.artifact_sha256 ?? "")}" pattern="[0-9a-f]{64}" required /></label>
      <label><span>验证器</span><input name="verifierCode" value="${escapeHtml(gate.verifier_code ?? source?.verifier_code ?? "")}" required /></label>
      <label><span>指标值（如有）</span><input name="metricValue" type="number" step="any" value="${source?.metric_value == null ? "" : escapeHtml(source.metric_value)}" /></label><label><span>指标单位（如有）</span><input name="metricUnit" value="${escapeHtml(source?.metric_unit ?? "")}" /></label>
      <label><span>环境 SHA-256</span><input name="environmentSha256" pattern="[0-9a-f]{64}" required /></label><label><span>来源 SHA-256</span><input name="provenanceSha256" pattern="[0-9a-f]{64}" required /></label>
      <label><span>开始时间</span><input name="startedAt" type="datetime-local" required /></label><label><span>完成时间</span><input name="finishedAt" type="datetime-local" required /></label>
      <label><span>签名</span><input name="signature" data-platform-sensitive autocomplete="off" required /></label>${stepUpFields()}
      <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认证据来自对应 RC 且仍在有效期内</span></label><button class="platform-primary" type="submit">提交证据</button>
    </form></details>`;
}

function gaLaunchForm(candidate) {
  return `<section class="platform-ga-action"><h3>执行 GA 发布</h3><p>20 项门禁已通过。发布操作需要二次身份验证。</p><form data-ga-launch-form autocomplete="off"><input type="hidden" name="launchCandidateId" value="${escapeHtml(candidate.launch_candidate_id)}" /><input type="hidden" name="expectedRevision" value="${candidate.revision}" />${stepUpFields()}<label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认按当前候选正式发布</span></label><button class="platform-primary" type="submit">执行 GA 发布</button></form></section>`;
}

function gaRollbackRequestForm(launch) {
  return `<details class="platform-ga-action"><summary>申请 GA 回滚</summary><form data-ga-rollback-request-form autocomplete="off"><input type="hidden" name="gaLaunchId" value="${escapeHtml(launch.ga_launch_id)}" /><input type="hidden" name="expectedRevision" value="${launch.revision}" /><label><span>触发原因</span><select name="triggerCode"><option value="operator_request">操作员申请</option><option value="health_slo">健康指标</option><option value="security_finding">安全发现</option></select></label><label><span>触发 SHA-256</span><input name="triggerSha256" pattern="[0-9a-f]{64}" required /></label><label><span>检查点 SHA-256</span><input name="checkpointSha256" pattern="[0-9a-f]{64}" required /></label><label><span>作业回执 SHA-256</span><input name="jobReceiptSha256" pattern="[0-9a-f]{64}" required /></label>${stepUpFields()}<label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认申请回滚</span></label><button class="platform-danger" type="submit">申请回滚</button></form></details>`;
}

function gaRollbackVerifyForm(launch, rollback) {
  return `<section class="platform-ga-action"><h3>独立复核回滚</h3><form data-ga-rollback-verify-form autocomplete="off"><input type="hidden" name="gaLaunchId" value="${escapeHtml(launch.ga_launch_id)}" /><input type="hidden" name="gaRollbackId" value="${escapeHtml(rollback.ga_rollback_id)}" /><input type="hidden" name="expectedRevision" value="${launch.revision}" /><label><span>复核结果</span><select name="outcome"><option value="passed">通过</option><option value="failed">不通过</option></select></label><label><span>复核 SHA-256</span><input name="verificationSha256" pattern="[0-9a-f]{64}" required /></label>${stepUpFields()}<label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认由另一位操作员独立复核</span></label><button class="platform-primary" type="submit">提交复核</button></form></section>`;
}

function gaReviewForm(launch) {
  const conclusion = launch.status === "rolled_back" ? "ga_rolled_back" : "ga_launched";
  return `<section class="platform-ga-action"><h3>发布后复盘</h3><form data-ga-review-form autocomplete="off"><input type="hidden" name="gaLaunchId" value="${escapeHtml(launch.ga_launch_id)}" /><input type="hidden" name="expectedRevision" value="${launch.revision}" /><input type="hidden" name="conclusion" value="${conclusion}" /><label><span>复盘摘要 SHA-256</span><input name="summarySha256" pattern="[0-9a-f]{64}" required /></label><label><span>行动项 SHA-256</span><input name="actionItemsSha256" pattern="[0-9a-f]{64}" required /></label>${stepUpFields()}<label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认复盘记录完整</span></label><button class="platform-primary" type="submit">完成复盘</button></form></section>`;
}

function stepUpFields() {
  return `<label><span>验证方式</span><select name="method"><option value="totp">动态验证码</option><option value="webauthn">安全密钥</option></select></label><label><span>动态验证码</span><input name="verificationCode" data-platform-sensitive inputmode="numeric" autocomplete="one-time-code" /></label>`;
}

function statusLabel(value) {
  return ({ approved: "已批准", launched: "已发布", rollback_pending: "回滚待复核", rolled_back: "已回滚", failed: "失败" })[value] ?? value;
}
function gateLabel(value) {
  return ({ long_stability: "72 小时稳定性", disaster_recovery: "灾备", tenant_isolation: "租户隔离", supply_chain: "供应链" })[value] ?? String(value).replaceAll("_", " ");
}
function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}
