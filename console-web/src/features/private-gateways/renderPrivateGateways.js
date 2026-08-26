/** PRD-120..123 safe Edge identity, telemetry, and Secret Reference views. */

import { privateGatewayPageOwner } from "./privateGatewayPageState.js";

export function renderPrivateGateways(state, authContext, consoleShellState) {
  const owner = privateGatewayPageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  return `
    <div class="private-gateway-workspace" data-private-gateway-root>
      <header class="private-gateway-toolbar">
        <div><p class="eyebrow">Customer-owned runtime</p><h3 id="private-gateways-title">私有 Gateway</h3><p>设备身份、期望状态、遥测与本地 Secret 引用</p></div>
        <div class="private-gateway-commands">
          <button type="button" class="private-gateway-icon" data-private-gateway-refresh title="刷新" aria-label="刷新私有 Gateway"${state.busy ? " disabled" : ""}>&#8635;</button>
          ${owner.canManage ? `<button type="button" class="primary" data-private-gateway-open${state.busy ? " disabled" : ""}>创建 Deployment</button>` : ""}
        </div>
      </header>
      ${renderFeedback(state.feedback)}
      ${renderList(state, owner)}
      ${renderDialog(state, owner)}
      ${renderStateDialog(state, owner)}
      ${renderSecretDialog(state, owner)}
    </div>`;
}

function renderList(state, owner) {
  if (state.state === "loading") return '<div class="private-gateway-empty" role="status">正在读取私有 Gateway 状态</div>';
  if (state.state === "error") return renderError(state.error);
  if (!state.deployments.length) return '<div class="private-gateway-empty">尚未创建私有 Gateway Deployment</div>';
  return `<div class="private-gateway-list">${state.deployments.map((item) => renderDeployment(item, state, owner)).join("")}</div>`;
}

function renderDeployment(item, state, owner) {
  const certificate = item.current_certificate;
  const active = !new Set(["revoked", "retired"]).has(item.status);
  return `
    <article class="private-gateway-deployment">
      <header>
        <div><span class="private-gateway-status tone-${escapeAttribute(item.health_code)}">${healthLabel(item.health_code)}</span><h4>${escapeHtml(item.display_name)}</h4><code>${escapeHtml(item.deployment_id)}</code></div>
        <dl><div><dt>区域</dt><dd>${escapeHtml(item.region_code)}</dd></div><div><dt>模式</dt><dd>${modeLabel(item.mode)}</dd></div><div><dt>Revision</dt><dd>${integer(item.revision)}</dd></div></dl>
      </header>
      <div class="private-gateway-facts">
        ${fact("设备", item.identity_generation ? `generation ${integer(item.identity_generation)}` : "待注册")}
        ${fact("Agent", item.agent_version ?? "-")}
        ${fact("最近连接", formatTime(item.last_connected_at))}
        ${fact("证书到期", formatTime(item.certificate_expires_at))}
        ${fact("证书状态", certificate ? certificateStatusLabel(certificate.status) : "未签发")}
        ${fact("吊销水位", integer(item.revocation_generation))}
      </div>
      ${renderDesiredState(item, state.desiredStates?.[item.deployment_id] ?? null, state, owner)}
      ${renderTelemetry(item, state.telemetryStates?.[item.deployment_id] ?? null, state, owner)}
      ${renderSecrets(item, state, owner)}
      ${renderTimeline(item.recent_events)}
      ${owner.canManage && active ? `
        <div class="private-gateway-actions">
          <label><span>吊销原因</span><select data-private-gateway-reason="${escapeAttribute(item.deployment_id)}"${state.busy ? " disabled" : ""}>
            <option value="owner_requested">主动停用</option><option value="device_lost">设备遗失</option><option value="security_incident">安全事件</option><option value="deployment_retired">Deployment 退役</option>
          </select></label>
          <button type="button" class="danger" data-private-gateway-revoke="${escapeAttribute(item.deployment_id)}"${state.busy ? " disabled" : ""}>${state.revokingId === item.deployment_id ? "正在吊销" : "吊销"}</button>
        </div>` : ""}
    </article>`;
}

function renderSecrets(deployment, state, owner) {
  if (!owner.canReadSecrets) return "";
  const references = (state.secretReferences ?? []).filter(
    (item) => item.deployment_id === deployment.deployment_id,
  );
  const connected = state.localSession?.deploymentId === deployment.deployment_id;
  const manageable = owner.canManageSecrets && deployment.status !== "revoked"
    && deployment.status !== "retired";
  return `<section class="private-gateway-secrets" aria-label="本地 Secret 引用">
    <header>
      <div><h5>本地 Secret 引用</h5><span class="private-gateway-state-status tone-${connected ? "applied" : "not_observed"}">${connected ? "本地会话有效" : `${references.length} 个引用`}</span></div>
      ${manageable ? (connected
        ? `<button type="button" class="primary" data-private-gateway-secret-open="create" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${state.secretBusy ? " disabled" : ""}>新增 Secret</button>`
        : `<button type="button" data-private-gateway-secret-open="handoff" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${state.secretBusy ? " disabled" : ""}>连接本地托管</button>`)
      : ""}
    </header>
    ${references.length ? `<div class="private-gateway-secret-list">${references.map((reference) => `
      <div class="private-gateway-secret-row">
        <div><strong>${escapeHtml(reference.label)}</strong><code>${escapeHtml(reference.reference_id)}</code><span>${secretKindLabel(reference.kind)}</span></div>
        <dl>
          ${fact("状态", secretStatusLabel(reference.status))}
          ${fact("版本", integer(reference.active_version))}
          ${fact("最后测试", providerTestLabel(reference.last_test_outcome))}
          ${fact("能力", reference.capabilities.length ? reference.capabilities.map(capabilityLabel).join(" / ") : "-")}
          ${fact("更新时间", formatTime(reference.updated_at))}
        </dl>
        ${manageable && connected && reference.status !== "disabled" ? `<div class="private-gateway-secret-actions">
          <button type="button" data-private-gateway-secret-command="test" data-deployment-id="${escapeAttribute(deployment.deployment_id)}" data-reference-id="${escapeAttribute(reference.reference_id)}"${state.secretBusy ? " disabled" : ""}>测试</button>
          <button type="button" data-private-gateway-secret-open="rotate" data-deployment-id="${escapeAttribute(deployment.deployment_id)}" data-reference-id="${escapeAttribute(reference.reference_id)}"${state.secretBusy ? " disabled" : ""}>轮换</button>
          <button type="button" class="danger" data-private-gateway-secret-command="delete" data-deployment-id="${escapeAttribute(deployment.deployment_id)}" data-reference-id="${escapeAttribute(reference.reference_id)}"${state.secretBusy ? " disabled" : ""}>删除</button>
        </div>` : ""}
      </div>`).join("")}</div>` : '<div class="private-gateway-state-empty">尚无本地 Secret 引用</div>'}
  </section>`;
}

function renderTelemetry(deployment, entry, pageState, owner) {
  if (!owner.canReadTelemetry && !owner.canReadUsage) return "";
  if (!entry) {
    return '<section class="private-gateway-telemetry" aria-label="遥测同步"><div class="private-gateway-state-empty">遥测水位尚未读取</div></section>';
  }
  const policy = entry.policy;
  const cursors = entry.cursors ?? [];
  const usage = entry.usage;
  const failures = entry.failures ?? [];
  return `<section class="private-gateway-telemetry" aria-label="遥测同步">
    <header>
      <div><h5>遥测同步</h5><span class="private-gateway-state-status tone-${telemetryTone(entry)}">${telemetryStatus(entry)}</span></div>
      ${policy ? `<span class="private-gateway-policy-revision">Policy r${integer(policy.revision)}</span>` : ""}
    </header>
    ${owner.canManageTelemetry && policy ? renderPolicyControl(
      deployment.deployment_id,
      policy.telemetry_level,
      pageState.busy,
    ) : ""}
    ${owner.canReadTelemetry ? `<dl class="private-gateway-state-diff">
      ${fact("最近 receipt", formatTime(entry.last_receipt_at))}
      ${fact("在线延迟", seconds(entry.lag_seconds))}
      ${fact("积压时长", seconds(entry.backlog_seconds))}
    </dl>
    <div class="private-gateway-cursors" aria-label="同步水位">
      ${cursors.map((cursor) => `<div><strong>${escapeHtml(streamLabel(cursor.stream))}</strong><span>ACK ${integer(cursor.contiguous_sequence)}</span><small>next ${integer(cursor.expected_sequence)} · seen ${integer(cursor.highest_seen_sequence)}</small></div>`).join("") || '<span class="private-gateway-state-empty">尚无同步水位</span>'}
    </div>
    ${renderFailures(failures)}` : ""}
    ${usage ? `<dl class="private-gateway-usage">
      ${fact("请求", integer(usage.request_count))}
      ${fact("Input tokens", integer(usage.input_tokens))}
      ${fact("Output tokens", integer(usage.output_tokens))}
      ${fact("Cache tokens", integer(usage.cache_tokens))}
      ${fact("CNY", micros(usage.cost_cny_micros, "CNY"))}
      ${fact("USD", micros(usage.cost_usd_micros, "USD"))}
    </dl>` : ""}
  </section>`;
}

function renderPolicyControl(deploymentId, current, busy) {
  return `<div class="private-gateway-policy" role="group" aria-label="遥测策略">
    ${["minimal", "operational", "billing"].map((level) => `<button type="button" data-private-gateway-policy data-deployment-id="${escapeAttribute(deploymentId)}" data-level="${level}" aria-pressed="${current === level}"${busy || current === level ? " disabled" : ""}>${policyLabel(level)}</button>`).join("")}
  </div>`;
}

function renderFailures(failures) {
  if (!failures.length) return '<div class="private-gateway-failure-empty">无失败事实</div>';
  return `<div class="private-gateway-failures"><table><thead><tr><th>组件</th><th>失败码</th><th>计数</th></tr></thead><tbody>${failures.map((item) => `<tr><td>${escapeHtml(item.component_code)}</td><td><code>${escapeHtml(item.failure_code)}</code></td><td>${integer(item.count)}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderDesiredState(deployment, entry, pageState, owner) {
  if (!entry) {
    return `<section class="private-gateway-state" aria-label="期望状态"><div class="private-gateway-state-empty">期望状态尚未读取</div></section>`;
  }
  const desired = entry.state;
  const active = deployment.status === "active";
  if (!desired) {
    return `<section class="private-gateway-state" aria-label="期望状态">
      <header><div><h5>期望状态</h5><span class="private-gateway-state-status tone-not_observed">未发布</span></div>${watermarks(0, 0, 0)}</header>
      ${owner.canManage && active ? `<div class="private-gateway-state-actions"><button type="button" class="primary" data-private-gateway-state-open="publish" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${pageState.busy ? " disabled" : ""}>发布期望状态</button></div>` : ""}
    </section>`;
  }
  const pending = desired.status === "approval_pending";
  const failed = desired.last_ack_status === "failed";
  const canRollback = !pending && desired.lkg_generation > 0
    && desired.lkg_generation < desired.generation;
  return `<section class="private-gateway-state" aria-label="期望状态">
    <header>
      <div><h5>期望状态</h5><span class="private-gateway-state-status tone-${escapeAttribute(desiredTone(desired))}">${desiredStatusLabel(desired)}</span></div>
      ${watermarks(desired.desired_generation, desired.applied_generation, desired.lkg_generation)}
    </header>
    <dl class="private-gateway-state-diff">
      ${fact("候选 generation", integer(desired.generation))}
      ${fact("配置来源", `generation ${integer(desired.source_generation)}`)}
      ${fact("变更等级", desired.change_class === "high_risk" ? "高风险" : "标准")}
      ${fact("应用差异", desired.drifted ? `落后 ${integer(Math.max(0, desired.desired_generation - desired.applied_generation))}` : "已收敛")}
      ${fact("最近结果", ackLabel(desired.last_ack_status))}
      ${fact("失败码", desired.last_reason_code)}
    </dl>
    ${desired.rollback_of_generation ? `<p class="private-gateway-rollback-reference">回滚来源 generation ${integer(desired.rollback_of_generation)}</p>` : ""}
    ${owner.canManage && active ? `<div class="private-gateway-state-actions">
      ${!pending ? stateButton("publish", deployment.deployment_id, "发布新状态", pageState.busy) : ""}
      ${pending ? stateButton("approve", deployment.deployment_id, "另一管理员批准", pageState.busy) : ""}
      ${pending ? stateButton("reject", deployment.deployment_id, "拒绝", pageState.busy, "danger") : ""}
      ${canRollback ? stateButton("rollback", deployment.deployment_id, failed ? "从 LKG 恢复" : "回滚到 LKG", pageState.busy, failed ? "danger" : "") : ""}
    </div>` : ""}
  </section>`;
}

function watermarks(desired, applied, lkg) {
  return `<dl class="private-gateway-watermarks">
    <div><dt>期望</dt><dd>${integer(desired)}</dd></div>
    <div><dt>实际</dt><dd>${integer(applied)}</dd></div>
    <div><dt>LKG</dt><dd>${integer(lkg)}</dd></div>
  </dl>`;
}

function stateButton(action, deploymentId, label, disabled, className = "") {
  return `<button type="button"${className ? ` class="${className}"` : ""} data-private-gateway-state-open="${action}" data-deployment-id="${escapeAttribute(deploymentId)}"${disabled ? " disabled" : ""}>${label}</button>`;
}

function renderTimeline(events) {
  if (!events.length) return '<div class="private-gateway-timeline-empty">尚无设备事件</div>';
  return `<ol class="private-gateway-timeline" aria-label="最近设备事件">${events.map((event) => `
    <li><span class="tone-${escapeAttribute(event.outcome_code)}"></span><div><strong>${eventLabel(event.event_type)}</strong><small>${escapeHtml(event.reason_code)} · generation ${integer(event.generation)}</small></div><time>${formatTime(event.occurred_at)}</time></li>
  `).join("")}</ol>`;
}

function renderDialog(state, owner) {
  if (!owner.canManage || !state.dialog?.open) return "";
  const registration = state.registration;
  const submitting = state.dialog.state === "submitting";
  return `
    <dialog class="private-gateway-dialog" data-private-gateway-dialog aria-labelledby="private-gateway-dialog-title" aria-busy="${submitting}">
      <header><div><h3 id="private-gateway-dialog-title">${registration ? "一次性注册材料" : "创建 Deployment"}</h3><span>${registration ? `有效至 ${formatTime(registration.expiresAt)}` : "绑定当前 Workspace"}</span></div><button type="button" class="private-gateway-icon" data-private-gateway-close title="关闭" aria-label="关闭"${submitting ? " disabled" : ""}>&times;</button></header>
      ${state.dialog.error ? renderError(state.dialog.error) : ""}
      ${registration ? renderRegistration(registration) : renderCreateForm(submitting)}
    </dialog>`;
}

function renderStateDialog(state, owner) {
  const dialog = state.stateDialog;
  if (!owner.canManage || !dialog?.open) return "";
  const desired = state.desiredStates?.[dialog.deploymentId]?.state ?? null;
  const submitting = dialog.state === "submitting";
  const labels = {
    publish: ["发布期望状态", "选择已发布的 Workspace 配置快照"],
    approve: ["批准高风险变更", "由另一名管理员确认"],
    reject: ["拒绝高风险变更", "保留当前已发布状态"],
    rollback: ["回滚到 LKG", `generation ${integer(desired?.lkg_generation)}`],
  };
  const [title, subtitle] = labels[dialog.action] ?? ["期望状态操作", "当前 Deployment"];
  return `<dialog class="private-gateway-dialog private-gateway-state-dialog" data-private-gateway-state-dialog aria-labelledby="private-gateway-state-dialog-title" aria-busy="${submitting}">
    <header><div><h3 id="private-gateway-state-dialog-title">${title}</h3><span>${escapeHtml(subtitle)}</span></div><button type="button" class="private-gateway-icon" data-private-gateway-state-close title="关闭" aria-label="关闭"${submitting ? " disabled" : ""}>&times;</button></header>
    ${dialog.error ? renderError(dialog.error) : ""}
    ${renderStateForm(dialog, desired, submitting)}
  </dialog>`;
}

function renderSecretDialog(state, owner) {
  const dialog = state.secretDialog;
  if (!owner.canManageSecrets || !dialog?.open) return "";
  const submitting = dialog.state === "submitting";
  const titles = {
    handoff: ["连接本地托管", "短时安全管理员会话"],
    create: ["新增本地 Secret", "只提交到客户环境"],
    rotate: ["轮换本地 Secret", "旧版本完成 drain 后退役"],
  };
  const [title, subtitle] = titles[dialog.action] ?? ["本地 Secret", "客户环境"];
  return `<dialog class="private-gateway-dialog private-gateway-secret-dialog" data-private-gateway-secret-dialog data-deployment-id="${escapeAttribute(dialog.deploymentId)}" aria-labelledby="private-gateway-secret-dialog-title" aria-busy="${submitting}">
    <header><div><h3 id="private-gateway-secret-dialog-title">${title}</h3><span>${subtitle}</span></div><button type="button" class="private-gateway-icon" data-private-gateway-secret-close title="关闭" aria-label="关闭"${submitting ? " disabled" : ""}>&times;</button></header>
    ${dialog.error ? renderError(dialog.error) : ""}
    ${renderSecretForm(dialog, submitting)}
  </dialog>`;
}

function renderSecretForm(dialog, submitting) {
  if (dialog.action === "handoff") {
    return `<form data-private-gateway-secret-form="handoff" autocomplete="off" novalidate>
      <label><span>本地 Gateway Origin</span><input name="local_api_origin" type="url" value="http://localhost:8000" maxlength="240" autocomplete="off" required${submitting ? " disabled" : ""}></label>
      <label><span>本地管理凭据</span><input name="handoff_credential" type="password" maxlength="512" autocomplete="new-password" data-private-gateway-sensitive required${submitting ? " disabled" : ""}></label>
      ${secretFormFooter("连接", submitting)}
    </form>`;
  }
  if (dialog.action === "create") {
    return `<form data-private-gateway-secret-form="create" autocomplete="off" novalidate>
      <label><span>Provider Connection ID</span><input name="provider_connection_id" pattern="[a-z0-9][a-z0-9_:-]{2,79}" maxlength="80" autocomplete="off" required${submitting ? " disabled" : ""}></label>
      <label><span>标签</span><input name="label" maxlength="80" autocomplete="off" required${submitting ? " disabled" : ""}></label>
      <label><span>类型</span><select name="kind"${submitting ? " disabled" : ""}><option value="api_key">API Key</option><option value="bearer_token">Bearer Token</option><option value="basic_password">Basic Password</option><option value="oauth_client_secret">OAuth Client Secret</option></select></label>
      <label><span>Secret</span><input name="secret_value" type="password" maxlength="65536" autocomplete="new-password" data-private-gateway-sensitive required${submitting ? " disabled" : ""}></label>
      ${secretFormFooter("创建", submitting)}
    </form>`;
  }
  return `<form data-private-gateway-secret-form="rotate" data-reference-id="${escapeAttribute(dialog.referenceId)}" autocomplete="off" novalidate>
    <label><span>新 Secret</span><input name="secret_value" type="password" maxlength="65536" autocomplete="new-password" data-private-gateway-sensitive required${submitting ? " disabled" : ""}></label>
    ${secretFormFooter("轮换", submitting)}
  </form>`;
}

function secretFormFooter(label, submitting) {
  return `<p class="private-gateway-form-error" data-private-gateway-secret-form-error role="alert"></p><div class="private-gateway-dialog-actions"><button type="button" data-private-gateway-secret-close${submitting ? " disabled" : ""}>取消</button><button type="submit" class="primary"${submitting ? " disabled" : ""}>${submitting ? "正在提交" : label}</button></div>`;
}

function renderStateForm(dialog, desired, submitting) {
  let fields = "";
  if (dialog.action === "publish") {
    fields = `<label><span>配置快照 ID</span><input name="source_config_snapshot_id" pattern="csn_[0-9a-f]{32}" maxlength="36" autocomplete="off" required></label>`;
  } else if (dialog.action === "reject") {
    fields = `<label><span>拒绝原因</span><select name="reason_code"><option value="risk_unacceptable">风险不可接受</option><option value="change_not_required">不再需要此变更</option></select></label>`;
  } else if (dialog.action === "rollback") {
    fields = `<label><span>回滚原因</span><select name="reason_code"><option value="${desired?.last_ack_status === "failed" ? "apply_failed_recovery" : "operator_requested"}">${desired?.last_ack_status === "failed" ? "应用失败恢复" : "管理员回滚"}</option></select></label>`;
  }
  return `<form class="private-gateway-state-form" data-private-gateway-state-form data-action="${escapeAttribute(dialog.action)}" autocomplete="off" novalidate>
    ${fields}
    <label class="private-gateway-confirm"><input type="checkbox" name="confirmed" required><span>确认${stateActionLabel(dialog.action)}</span></label>
    <p class="private-gateway-form-error" data-private-gateway-state-form-error role="alert"></p>
    <div class="private-gateway-dialog-actions"><button type="button" data-private-gateway-state-close${submitting ? " disabled" : ""}>取消</button><button type="submit" class="primary"${submitting ? " disabled" : ""}>${submitting ? "正在提交" : stateActionLabel(dialog.action)}</button></div>
  </form>`;
}

function renderCreateForm(submitting) {
  return `<form data-private-gateway-create autocomplete="off" novalidate>
    <div class="private-gateway-form-grid">
      <label><span>显示名称</span><input name="display_name" maxlength="120" minlength="2" required${submitting ? " disabled" : ""}></label>
      <label><span>区域</span><input name="region_code" value="cn-east-1" maxlength="40" pattern="[a-z0-9][a-z0-9-]+" required${submitting ? " disabled" : ""}></label>
    </div>
    <fieldset class="private-gateway-segmented"${submitting ? " disabled" : ""}><legend>连接模式</legend><label><input type="radio" name="mode" value="connected" checked><span>Connected</span></label><label><input type="radio" name="mode" value="isolated"><span>Isolated</span></label></fieldset>
    <label class="private-gateway-ttl"><span>注册窗口</span><select name="token_ttl_seconds"${submitting ? " disabled" : ""}><option value="600">10 分钟</option><option value="300">5 分钟</option><option value="60">1 分钟</option></select></label>
    <p class="private-gateway-form-error" data-private-gateway-form-error role="alert"></p>
    <div class="private-gateway-dialog-actions"><button type="button" data-private-gateway-close${submitting ? " disabled" : ""}>取消</button><button type="submit" class="primary"${submitting ? " disabled" : ""}>${submitting ? "正在创建" : "创建"}</button></div>
  </form>`;
}

function renderRegistration(value) {
  return `<section class="private-gateway-registration" data-private-gateway-one-use>
    <dl><div><dt>Deployment ID</dt><dd><code>${escapeHtml(value.deploymentId)}</code></dd></div><div><dt>Registration token</dt><dd><code>${escapeHtml(value.token)}</code></dd></div><div><dt>Challenge</dt><dd><code>${escapeHtml(value.challenge)}</code></dd></div></dl>
    <div class="private-gateway-dialog-actions"><button type="button" data-private-gateway-close>关闭并清除</button><button type="button" class="primary" data-private-gateway-copy>复制并清除</button></div>
  </section>`;
}

function renderFeedback(feedback) {
  if (!feedback) return "";
  const labels = {
    "edge.registration_created": "Deployment 已创建",
    "edge.registration_replayed": "已恢复 Deployment 结果",
    "edge.registration_copied": "注册材料已复制并从页面清除",
    "edge.deployment_revoked": "Deployment 已吊销",
    "edge.desired_state_published": "期望状态已发布",
    "edge.desired_state_pending": "高风险变更等待另一管理员审批",
    "edge.desired_state_approved": "高风险变更已批准并发布",
    "edge.desired_state_rejected": "高风险变更已拒绝",
    "edge.desired_state_rolled_back": "已从 LKG 创建新的回滚 generation",
    "edge.telemetry_policy_updated": "遥测策略已更新",
    "edge.telemetry_policy_replayed": "已恢复遥测策略结果",
    "local_admin.session_created": "本地安全管理员会话已建立",
    "local_secret.created": "本地 Secret 已创建",
    "local_secret.rotated": "本地 Secret 已完成轮换",
    "local_secret.deleted": "本地 Secret 已删除",
    "local_secret.test_completed": "Provider 连接测试已完成",
  };
  if (feedback.kind === "error") return renderError(feedback);
  return `<div class="private-gateway-feedback" role="status"><strong>${escapeHtml(labels[feedback.code] ?? feedback.code)}</strong><span>request_id: ${escapeHtml(feedback.requestId ?? "-")}</span></div>`;
}

function renderError(error) {
  return `<div class="private-gateway-error" role="alert"><strong>${escapeHtml(error?.message ?? "私有 Gateway 请求失败")}</strong><span><code>${escapeHtml(error?.code ?? "edge.unavailable")}</code> · request_id: ${escapeHtml(error?.requestId ?? "-")}</span></div>`;
}

function fact(label, value) { return `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`; }
function healthLabel(value) { return ({ not_registered: "待注册", healthy: "健康", certificate_expiring: "证书临期", revoked: "已吊销", offline: "离线", unknown: "未知" })[value] ?? "未知"; }
function modeLabel(value) { return value === "isolated" ? "隔离" : "联网"; }
function certificateStatusLabel(value) { return ({ active: "有效", draining: "轮换窗口", superseded: "已替换", revoked: "已吊销", expired: "已过期" })[value] ?? "未知"; }
function desiredTone(value) { if (value.status === "approval_pending") return "pending"; if (value.last_ack_status === "failed") return "failed"; return value.drifted ? "drifted" : "applied"; }
function desiredStatusLabel(value) { if (value.status === "approval_pending") return "等待审批"; if (value.last_ack_status === "failed") return "应用失败"; if (value.drifted) return "等待收敛"; return "已收敛"; }
function ackLabel(value) { return ({ not_observed: "未观测", applied: "已应用", failed: "失败" })[value] ?? "未知"; }
function telemetryTone(value) { if (value.lag_seconds === null) return "not_observed"; if (value.lag_seconds > 60) return "drifted"; return "applied"; }
function telemetryStatus(value) { if (value.last_receipt_at === null) return "未接收"; return value.lag_seconds > 60 ? "延迟" : "已同步"; }
function policyLabel(value) { return ({ minimal: "Minimal", operational: "Operational", billing: "Billing" })[value] ?? "-"; }
function streamLabel(value) { return ({ health: "Health", operational: "Operational", usage: "Usage", failure: "Failure" })[value] ?? value; }
function seconds(value) { return Number.isSafeInteger(value) ? `${value}s` : "-"; }
function micros(value, currency) { return Number.isSafeInteger(value) ? `${currency} ${(value / 1_000_000).toFixed(6)}` : "-"; }
function stateActionLabel(value) { return ({ publish: "发布", approve: "批准", reject: "拒绝", rollback: "回滚" })[value] ?? "提交"; }
function eventLabel(value) { return ({ "edge.registered": "设备注册", "edge.certificate_rotated": "证书轮换", "edge.revoked": "Deployment 吊销", "edge.authentication_failed": "认证失败" })[value] ?? value; }
function secretKindLabel(value) { return ({ api_key: "API Key", bearer_token: "Bearer Token", basic_password: "Basic Password", oauth_client_secret: "OAuth Client Secret" })[value] ?? value; }
function secretStatusLabel(value) { return ({ active: "有效", rotating: "轮换中", degraded: "降级", disabled: "已停用" })[value] ?? value; }
function providerTestLabel(value) { return ({ succeeded: "成功", authentication_failed: "认证失败", authorization_failed: "授权失败", rate_limited: "限流", timeout: "超时", tls_failed: "TLS 失败", network_unreachable: "网络不可达", provider_unavailable: "Provider 不可用", unsupported: "不支持" })[value] ?? "未测试"; }
function capabilityLabel(value) { return ({ chat_completions: "Chat", embeddings: "Embeddings", models: "Models" })[value] ?? value; }
function formatTime(value) { if (!value) return "-"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date); }
function integer(value) { return Number.isSafeInteger(value) ? String(value) : "-"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value).replaceAll("`", "&#096;"); }
