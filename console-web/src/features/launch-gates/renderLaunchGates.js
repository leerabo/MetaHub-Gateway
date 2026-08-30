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
      <div><p>Release governance</p><h1>Launch gates</h1></div>
      <div class="platform-launch-heading-actions"><span class="platform-launch-boundary">RC only</span><button class="platform-icon-button" type="button" data-platform-action="launch-refresh" title="Refresh launch gates" aria-label="Refresh launch gates"${busy ? " disabled" : ""}>&#8635;</button></div>
    </header>
    <section class="platform-metrics platform-launch-metrics" aria-label="Launch readiness summary">
      ${metric("Fresh gates", freshCount, `${gates.length || 20} required`)}
      ${metric("Independent signs", approvedCount, "6 required")}
      ${metric("Completed waves", completedWaves, "4 ordered waves")}
      ${metric("Blocking items", blockers, candidate?.status ?? "no candidate")}
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
    <summary><span>New candidate</span><small>20 gates · 4 waves</small></summary>
    <form data-platform-launch-create-form autocomplete="off">
      <div class="platform-launch-input-grid">
        ${digestInput("Code tree digest", "codeTreeSha256", busy)}
        ${digestInput("Migration set digest", "migrationSetSha256", busy)}
        ${digestInput("Contract set digest", "contractSetSha256", busy)}
        ${digestInput("Test manifest digest", "testManifestSha256", busy)}
      </div>
      <fieldset class="platform-launch-artifact-set"><legend>Gate artifacts</legend>
        <div class="platform-launch-artifact-head"><span>Gate</span><span>Workspace</span><span>Artifact digest</span></div>
        ${LAUNCH_GATE_CODES.map((code) => `<div class="platform-launch-artifact-row"><strong>${escapeHtml(gateLabel(code))}</strong><input name="artifactWorkspace:${escapeAttribute(code)}" required pattern="wsp_[0-9a-f]{32}" placeholder="wsp_…"${disabled(busy)} /><input name="artifactSha256:${escapeAttribute(code)}" required pattern="[0-9a-f]{64}" placeholder="SHA-256"${disabled(busy)} /></div>`).join("")}
      </fieldset>
      <fieldset class="platform-launch-wave-set"><legend>Wave plans</legend>
        ${LAUNCH_WAVES.map((wave) => `<section class="platform-launch-wave-plan"><h3>${escapeHtml(waveLabel(wave))}</h3><div>
          ${textInput("Workspace", `waveWorkspace:${wave}`, "wsp_[0-9a-f]{32}", busy)}
          ${textInput("Cohort digest", `cohortSha256:${wave}`, "[0-9a-f]{64}", busy)}
          ${numberInput("Flag generation", `featureGeneration:${wave}`, 1, 1, busy)}
          ${textInput("Migration plan", `migrationSha256:${wave}`, "[0-9a-f]{64}", busy)}
          ${textInput("Rollback plan", `rollbackSha256:${wave}`, "[0-9a-f]{64}", busy)}
          ${numberInput("Observation seconds", `observationSeconds:${wave}`, 60, 60, busy, 604800)}
        </div></section>`).join("")}
      </fieldset>
      ${stepUpFields(busy)}
      <label class="platform-check platform-launch-confirm"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>Confirm candidate inputs</span></label>
      <button class="platform-primary" type="submit"${disabled(busy)}>Create candidate</button>
    </form>
  </details>`;
}

function candidateSummary(candidate) {
  return `<section class="platform-band platform-launch-candidate">
    <div class="platform-section-heading"><div><p>Generation ${integer(candidate.generation)}</p><h2>Candidate ${escapeHtml(shortReference(candidate.launch_candidate_id))}</h2></div>${status(candidate.status)}</div>
    <dl>
      ${fact("Revision", integer(candidate.revision))}
      ${fact("Composite", shortHash(candidate.candidate_sha256), true)}
      ${fact("Gate policy", shortHash(candidate.gate_policy_set_sha256), true)}
      ${fact("Updated", formatTime(candidate.updated_at))}
      ${fact("Evidence", `${candidate.evidence.length} / 20`)}
      ${fact("Decisions", `${candidate.decisions.length} / 6`)}
      ${fact("Waves", `${candidate.waves.filter((item) => item.status === "completed").length} / 4`)}
      ${fact("Boundary", candidate.review?.conclusion ?? "release_candidate only")}
    </dl>
  </section>`;
}

function noCandidate() {
  return `<section class="platform-band platform-launch-empty"><div class="platform-section-heading"><div><p>Generation</p><h2>No candidate</h2></div>${status("collecting")}</div></section>`;
}

function gateRegistry(gates, candidate, evidence, permissions, busy) {
  const canSubmit = permissions.has("platform.launch.evidence.submit")
    && candidate?.status === "collecting";
  return `<section class="platform-band platform-launch-gates">
    <div class="platform-section-heading"><div><p>Signed verifier receipts</p><h2>Gate registry</h2></div><span>${integer(gates.length)} policies</span></div>
    <div class="platform-table-wrap"><table class="platform-launch-gate-table">
      <thead><tr><th>Gate</th><th>Policy</th><th>Evidence</th><th>Fresh until</th><th>Metric</th></tr></thead>
      <tbody>${gates.map((gate) => gateRow(gate, evidence.get(gate.gate_code))).join("") || emptyRow("Gate registry unavailable", 5)}</tbody>
    </table></div>
    ${canSubmit ? `<div class="platform-launch-evidence-forms">${gates.filter((gate) => !evidence.has(gate.gate_code)).map((gate) => evidenceForm(gate, candidate, busy)).join("")}</div>` : ""}
  </section>`;
}

function gateRow(gate, proof) {
  const freshness = proof ? (freshEvidence(proof) ? "fresh" : "stale") : "pending";
  return `<tr>
    <td data-label="Gate"><strong>${escapeHtml(gateLabel(gate.gate_code))}</strong><small>${escapeHtml(gate.verifier_code)} · v${integer(gate.verifier_version)}</small></td>
    <td data-label="Policy"><code>${escapeHtml(shortHash(gate.policy_sha256))}</code><small>${integer(gate.freshness_seconds)}s freshness</small></td>
    <td data-label="Evidence">${status(proof?.outcome ?? "pending")} ${status(freshness)}</td>
    <td data-label="Fresh until"><strong>${proof ? formatTime(proof.expires_at) : "-"}</strong><small>${proof ? shortReference(proof.workspace_id) : "not submitted"}</small></td>
    <td data-label="Metric"><strong>${proof?.metric_value ?? "-"}</strong><small>${escapeHtml(proof?.metric_unit ?? "exact pass")}</small></td>
  </tr>`;
}

function evidenceForm(gate, candidate, busy) {
  return `<details><summary><span>${escapeHtml(gateLabel(gate.gate_code))}</span><small>Submit signed evidence</small></summary>
    <form data-platform-launch-evidence-form autocomplete="off">
      ${hidden("launchGateId", gate.launch_gate_id)}${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${hidden("verifierCode", gate.verifier_code)}
      ${textInput("Workspace", "workspaceId", "wsp_[0-9a-f]{32}", busy)}
      ${textInput("Artifact digest", "artifactSha256", "[0-9a-f]{64}", busy)}
      <label><span>Outcome</span><select name="outcome"${disabled(busy)}><option value="passed">Passed</option><option value="failed">Failed</option></select></label>
      <label><span>Metric value</span><input name="metricValue" type="number" step="any"${disabled(busy)} /></label>
      <label><span>Metric unit</span><input name="metricUnit" pattern="[a-z][a-z0-9_./%-]{0,31}"${disabled(busy)} /></label>
      ${textInput("Environment digest", "environmentSha256", "[0-9a-f]{64}", busy)}
      ${textInput("Provenance digest", "provenanceSha256", "[0-9a-f]{64}", busy)}
      <label><span>Started</span><input name="startedAt" type="datetime-local" step="1" required${disabled(busy)} /></label>
      <label><span>Finished</span><input name="finishedAt" type="datetime-local" step="1" required${disabled(busy)} /></label>
      <label><span>Verifier signature</span><input name="signature" type="password" autocomplete="off" minlength="43" maxlength="512" required data-platform-sensitive${disabled(busy)} /></label>
      ${stepUpFields(busy)}
      <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>Confirm signed envelope</span></label>
      <button class="platform-primary" type="submit"${disabled(busy)}>Submit evidence</button>
    </form>
  </details>`;
}

function decisionRegistry(candidate, permissions, busy) {
  const decisions = new Map(candidate.decisions.map((item) => [item.actor_kind, item]));
  const canSign = candidate.status === "signing";
  return `<section class="platform-band platform-launch-decisions">
    <div class="platform-section-heading"><div><p>Independent operators</p><h2>Go / no-go decisions</h2></div><span>${integer(candidate.decisions.length)} / 6</span></div>
    <div class="platform-launch-decision-grid">${LAUNCH_ACTORS.map((actor) => {
      const decision = decisions.get(actor);
      const allowed = canSign && !decision && permissions.has(`platform.launch.sign.${actor}`);
      return `<section><header><div><strong>${escapeHtml(actorLabel(actor))}</strong><small>${decision ? shortReference(decision.operator_id) : "Awaiting signer"}</small></div>${status(decision?.decision ?? "pending")}</header>${decision ? `<dl>${fact("Operator rev", integer(decision.operator_revision))}${fact("Reason", decision.reason_code)}${fact("Signed", formatTime(decision.decided_at))}${fact("Receipt", shortHash(decision.signature_sha256), true)}</dl>` : ""}${allowed ? decisionForm(actor, candidate, busy) : ""}</section>`;
    }).join("")}</div>
  </section>`;
}

function decisionForm(actor, candidate, busy) {
  return `<form data-platform-launch-decision-form autocomplete="off">
    ${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${hidden("actorKind", actor)}
    <label><span>Decision</span><select name="approved"${disabled(busy)}><option value="true">Go</option><option value="false">No-go</option></select></label>
    <label><span>Reason code</span><input name="reasonCode" required pattern="[a-z][a-z0-9_.-]{2,79}"${disabled(busy)} /></label>
    ${stepUpFields(busy)}
    <label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>Confirm independent decision</span></label>
    <button type="submit"${disabled(busy)}>Sign decision</button>
  </form>`;
}

function waveRegistry(candidate, permissions, busy) {
  const canExecute = permissions.has("platform.launch.execute");
  return `<section class="platform-band platform-launch-waves">
    <div class="platform-section-heading"><div><p>Ordered rollout</p><h2>Launch waves</h2></div><span>4 fixed stages</span></div>
    <div class="platform-launch-wave-list">${candidate.waves.map((wave) => `<section class="platform-launch-wave-row"><header><span>${integer(wave.wave_sequence)}</span><div><strong>${escapeHtml(waveLabel(wave.wave_kind))}</strong><small>${escapeHtml(shortReference(wave.workspace_id))}</small></div>${status(wave.status)}</header><dl>${fact("Observation", `${integer(wave.observation_seconds)}s`)}${fact("Deadline", formatOptionalTime(wave.observation_deadline_at))}${fact("Flag gen", integer(wave.feature_flag_generation))}${fact("Health", shortHash(wave.health_watermark_sha256), true)}</dl>${canExecute ? waveActions(wave, candidate, busy) : ""}</section>`).join("")}</div>
  </section>`;
}

function waveActions(wave, candidate, busy) {
  const operations = wave.status === "pending" ? ["start_wave"]
    : wave.status === "running" ? ["pause_wave", "complete_wave"]
      : wave.status === "paused" ? ["resume_wave"] : [];
  return operations.length ? `<div class="platform-launch-wave-actions">${operations.map((operation) => `<details><summary>${escapeHtml(operationLabel(operation))}</summary><form data-platform-launch-wave-form autocomplete="off">${hidden("operation", operation)}${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${hidden("waveKind", wave.wave_kind)}${operation === "complete_wave" ? digestInput("Health watermark", "healthWatermarkSha256", busy) : ""}${stepUpFields(busy)}<label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>Confirm ${escapeHtml(operationLabel(operation).toLowerCase())}</span></label><button type="submit"${disabled(busy)}>${escapeHtml(operationLabel(operation))}</button></form></details>`).join("")}</div>` : "";
}

function rollbackRegistry(candidate, permissions, busy) {
  const pending = candidate.rollbacks.filter((item) => item.status === "pending");
  const canRollback = permissions.has("platform.launch.rollback");
  const rollbackStates = new Set(["launching", "paused", "review_pending", "release_candidate"]);
  const eligible = candidate.waves.filter((wave) => new Set(["running", "paused", "completed"]).has(wave.status));
  return `<section class="platform-band platform-launch-rollbacks">
    <div class="platform-section-heading"><div><p>Checkpoint recovery</p><h2>Rollback</h2></div><span>${integer(candidate.rollbacks.length)} records</span></div>
    ${candidate.rollbacks.length ? `<div class="platform-launch-rollback-list">${candidate.rollbacks.map((item) => rollbackRow(item, candidate, canRollback, busy)).join("")}</div>` : '<p class="platform-empty">No rollback records.</p>'}
    ${canRollback && rollbackStates.has(candidate.status) && pending.length === 0 ? `<div class="platform-launch-rollback-requests">${eligible.map((wave) => rollbackRequestForm(wave, candidate, busy)).join("")}</div>` : ""}
  </section>`;
}

function rollbackRow(item, candidate, canRollback, busy) {
  return `<section><header><div><strong>${escapeHtml(waveLabel(item.from_wave_kind))} → ${escapeHtml(waveLabel(item.to_wave_kind))}</strong><small>${escapeHtml(item.trigger_code)} · ${formatTime(item.requested_at)}</small></div>${status(item.status)}</header><dl>${fact("Checkpoint", shortHash(item.checkpoint_sha256), true)}${fact("Job receipt", shortHash(item.job_receipt_sha256), true)}${fact("Verification", shortHash(item.verification_sha256), true)}${fact("Revision", integer(item.revision))}</dl>${canRollback && item.status === "pending" ? rollbackVerifyForm(item, candidate, busy) : ""}</section>`;
}

function rollbackRequestForm(wave, candidate, busy) {
  return `<details><summary><span>Rollback ${escapeHtml(waveLabel(wave.wave_kind))}</span><small>to ${escapeHtml(waveLabel(PREVIOUS_WAVE[wave.wave_kind]))}</small></summary><form data-platform-launch-rollback-request-form autocomplete="off">${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${hidden("fromWaveKind", wave.wave_kind)}${hidden("toWaveKind", PREVIOUS_WAVE[wave.wave_kind])}<label><span>Trigger</span><select name="triggerCode"${disabled(busy)}><option value="gate_failed">Gate failed</option><option value="health_slo">Health SLO</option><option value="payment_failure">Payment failure</option><option value="security_finding">Security finding</option><option value="operator_request">Operator request</option></select></label>${digestInput("Trigger digest", "triggerSha256", busy)}${digestInput("Checkpoint digest", "checkpointSha256", busy)}${digestInput("Job receipt digest", "jobReceiptSha256", busy)}${stepUpFields(busy)}<label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>Confirm rollback request</span></label><button class="platform-danger" type="submit"${disabled(busy)}>Request rollback</button></form></details>`;
}

function rollbackVerifyForm(item, candidate, busy) {
  return `<form data-platform-launch-rollback-verify-form autocomplete="off">${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", item.revision)}${hidden("launchRollbackId", item.launch_rollback_id)}<label><span>Outcome</span><select name="outcome"${disabled(busy)}><option value="passed">Passed</option><option value="failed">Failed</option></select></label>${digestInput("Verification digest", "verificationSha256", busy)}${stepUpFields(busy)}<label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>Confirm independent verification</span></label><button type="submit"${disabled(busy)}>Verify rollback</button></form>`;
}

function reviewRegistry(candidate, permissions, busy) {
  const canReview = permissions.has("platform.launch.execute")
    && candidate.status === "review_pending" && !candidate.review;
  return `<section class="platform-band platform-launch-review">
    <div class="platform-section-heading"><div><p>Post-launch control</p><h2>RC review</h2></div>${status(candidate.review?.conclusion ?? "pending")}</div>
    ${candidate.review ? `<dl>${fact("Conclusion", candidate.review.conclusion)}${fact("Summary", shortHash(candidate.review.summary_sha256), true)}${fact("Actions", shortHash(candidate.review.action_items_sha256), true)}${fact("Reviewed", formatTime(candidate.review.reviewed_at))}</dl>` : '<p class="platform-empty">Review opens after all four waves complete.</p>'}
    ${canReview ? `<form data-platform-launch-review-form autocomplete="off">${hidden("launchCandidateId", candidate.launch_candidate_id)}${hidden("expectedRevision", candidate.revision)}${digestInput("Review summary", "summarySha256", busy)}${digestInput("Action items", "actionItemsSha256", busy)}${stepUpFields(busy)}<label class="platform-check"><input name="confirmed" type="checkbox" required${disabled(busy)} /><span>Confirm release candidate conclusion</span></label><button class="platform-primary" type="submit"${disabled(busy)}>Close RC review</button></form>` : ""}
  </section>`;
}

function stepUpFields(busy) {
  return `<label><span>Step-up method</span><select name="method"${disabled(busy)}><option value="totp">TOTP</option><option value="webauthn">Security key</option></select></label><label><span>TOTP code</span><input name="verificationCode" type="password" inputmode="numeric" autocomplete="one-time-code" minlength="6" maxlength="8" data-platform-sensitive${disabled(busy)} /></label>`;
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
  return `<span class="platform-status ${kind}">${escapeHtml(text.replaceAll("_", " "))}</span>`;
}

function freshEvidence(value) {
  return value?.outcome === "passed" && Number.isFinite(Date.parse(value.expires_at))
    && Date.parse(value.expires_at) > Date.now();
}

function gateLabel(value) {
  return ({
    wb: "WB", api: "API", bb: "BB", gr: "GR", perf: "Performance", web: "Web",
    ui: "UI", ucd: "UCD", comp: "Compatibility", sec: "Security", flow: "Flow",
    migration: "Migration", disaster_recovery: "Disaster recovery", payment: "Payment",
    edge: "Edge", privacy: "Privacy", compliance: "Compliance",
    supply_chain: "Supply chain", tenant_isolation: "Tenant isolation",
    long_stability: "72h stability",
  })[value] ?? String(value).replaceAll("_", " ");
}

function actorLabel(value) {
  return ({ product: "Product", sre: "SRE", security: "Security", test: "Test", finance: "Finance", legal: "Legal" })[value] ?? value;
}

function waveLabel(value) {
  return ({ pre_launch: "Pre-launch", internal: "Internal", personal_pilot: "Personal pilot", enterprise_pilot: "Enterprise pilot", managed_private_pilot: "Managed private pilot" })[value] ?? String(value).replaceAll("_", " ");
}

function operationLabel(value) {
  return ({ start_wave: "Start", pause_wave: "Pause", resume_wave: "Resume", complete_wave: "Complete" })[value] ?? value;
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
function emptyRow(label, columns) { return `<tr><td colspan="${integer(columns)}" class="platform-empty">${escapeHtml(label)}</td></tr>`; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value); }
