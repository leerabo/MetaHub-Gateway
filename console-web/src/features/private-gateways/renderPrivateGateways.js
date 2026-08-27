/** PRD-120..125 safe Edge identity, validation, support, and lifecycle views. */

import { privateGatewayPageOwner } from "./privateGatewayPageState.js";

const VALIDATION_CHECK_CODES = Object.freeze([
  "device_certificate", "signed_config_lkg", "provider_connectivity",
  "minimal_gateway_request", "usage_cost_failure_sync", "backup_readiness",
  "capacity_readiness", "performance_baseline",
]);

export function renderPrivateGateways(state, authContext, consoleShellState) {
  const owner = privateGatewayPageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  return `
    <div class="private-gateway-workspace" data-private-gateway-root>
      <header class="private-gateway-toolbar">
        <div><p class="eyebrow">Customer-owned runtime</p><h3 id="private-gateways-title">私有 Gateway</h3><p>设备身份、交付验证、脱敏支持与运行状态</p></div>
        <div class="private-gateway-commands">
          <button type="button" class="private-gateway-icon" data-private-gateway-refresh title="刷新" aria-label="刷新私有 Gateway"${state.busy ? " disabled" : ""}>&#8635;</button>
          ${owner.canManage ? `<button type="button" class="primary" data-private-gateway-open${state.busy ? " disabled" : ""}>创建 Deployment</button>` : ""}
        </div>
      </header>
      ${renderFeedback(state.feedback)}
      ${renderInstallerWorkbench(state, owner)}
      ${renderList(state, owner)}
      ${renderDialog(state, owner)}
      ${renderStateDialog(state, owner)}
      ${renderSecretDialog(state, owner)}
      ${renderAcceptanceDialog(state, owner)}
    </div>`;
}

function renderInstallerWorkbench(state, owner) {
  if (!owner.canReadInstallers || state.state !== "ready") return "";
  const catalog = state.installerCatalog;
  const selection = state.installerSelection;
  const release = catalog.releases.find(
    (item) => item.manifest.release_id === selection.releaseId,
  );
  const deployment = state.deployments.find(
    (item) => item.deployment_id === selection.deploymentId,
  );
  const profile = release?.manifest.profiles.find(
    (item) => item.install_profile_id === selection.profileId,
  );
  if (!catalog.releases.length) {
    return `<section class="private-gateway-installer" aria-labelledby="private-gateway-installer-title">
      <header><div><p class="eyebrow">Installer</p><h4 id="private-gateway-installer-title">安装与接入</h4></div><span class="private-gateway-state-status tone-not_observed">暂无可用版本</span></header>
    </section>`;
  }
  if (!deployment || !release || !profile) {
    return `<section class="private-gateway-installer" aria-labelledby="private-gateway-installer-title">
      <header><div><p class="eyebrow">Installer</p><h4 id="private-gateway-installer-title">安装与接入</h4></div><span class="private-gateway-state-status tone-pending">等待 Deployment</span></header>
      <p class="private-gateway-installer-empty">创建 Deployment 后即可选择安装环境。</p>
    </section>`;
  }
  const workflow = installerWorkflow(state, deployment, release, profile);
  return `<section class="private-gateway-installer" aria-labelledby="private-gateway-installer-title">
    <header>
      <div><p class="eyebrow">Installer</p><h4 id="private-gateway-installer-title">安装与接入</h4><span>${escapeHtml(deployment.display_name)}</span></div>
      <span class="private-gateway-state-status tone-${workflow.tone}">${workflow.label}</span>
    </header>
    ${renderInstallerEnvironment(state, release, profile, owner)}
    ${renderInstallerSteps(workflow)}
    <div class="private-gateway-installer-body">
      ${renderInstallerDownloads(catalog, release, workflow, owner)}
      ${renderPreflight(workflow.preflight)}
      ${renderInstallation(workflow.installation, deployment, workflow)}
    </div>
  </section>`;
}

function renderInstallerEnvironment(state, release, profile, owner) {
  const selection = state.installerSelection;
  const activeDeployments = state.deployments.filter(
    (item) => !["revoked", "retired"].includes(item.status),
  );
  const disabled = state.busy || !owner.canManageInstallations;
  return `<form class="private-gateway-installer-environment" data-private-gateway-installer-environment>
    <label><span>Deployment</span><select name="deployment_id"${disabled ? " disabled" : ""}>${activeDeployments.map((item) => `<option value="${escapeAttribute(item.deployment_id)}"${item.deployment_id === selection.deploymentId ? " selected" : ""}>${escapeHtml(item.display_name)} · ${escapeHtml(item.region_code)}</option>`).join("")}</select></label>
    <label><span>版本</span><select name="release_id"${disabled ? " disabled" : ""}>${state.installerCatalog.releases.map((item) => `<option value="${escapeAttribute(item.manifest.release_id)}"${item.manifest.release_id === selection.releaseId ? " selected" : ""}>v${escapeHtml(item.manifest.version)} · ${installerChannelLabel(item.manifest.channel)}</option>`).join("")}</select></label>
    <label><span>规格</span><select name="profile_id"${disabled ? " disabled" : ""}>${release.manifest.profiles.map((item) => `<option value="${escapeAttribute(item.install_profile_id)}"${item.install_profile_id === selection.profileId ? " selected" : ""}>${escapeHtml(item.display_name)}</option>`).join("")}</select></label>
    <label><span>架构</span><select name="architecture"${disabled ? " disabled" : ""}>${release.manifest.supported_arch.map((item) => `<option value="${escapeAttribute(item)}"${item === selection.architecture ? " selected" : ""}>${escapeHtml(item)}</option>`).join("")}</select></label>
    <dl>
      ${fact("CPU", `${integer(profile.cpu_cores)} cores`)}
      ${fact("内存", `${integer(Math.round(profile.memory_mib / 1024))} GiB`)}
      ${fact("磁盘", `${integer(Math.round(profile.disk_mib / 1024))} GiB`)}
      ${fact("Runtime", `${profile.runtime} ${profile.runtime_min_version}+`)}
      ${fact("端口", profile.required_ports.join(" / "))}
      ${fact("代理 / CA", `${profile.proxy_modes.map(proxyLabel).join(" / ")} · ${caLabel(profile.ca_mode)}`)}
    </dl>
  </form>`;
}

function renderInstallerSteps(workflow) {
  const steps = [
    ["environment", "环境", workflow.environmentLabel],
    ["preflight", "预检", workflow.preflightLabel],
    ["installation", "安装", workflow.installationLabel],
    ["registration", "注册", workflow.registrationLabel],
  ];
  return `<ol class="private-gateway-installer-steps" aria-label="安装进度">${steps.map(([code, label, detail], index) => `
    <li class="tone-${installerStepTone(workflow, code)}"><span>${index + 1}</span><div><strong>${label}</strong><small>${escapeHtml(detail)}</small></div></li>
  `).join("")}</ol>`;
}

function renderInstallerDownloads(catalog, release, workflow, owner) {
  const artifacts = release.manifest.artifacts.map((artifact) => ({
    artifact,
    download: catalog.downloads.find((item) => (
      item.release_id === release.manifest.release_id
      && item.artifact_id === artifact.artifact_id
    )),
  })).filter((item) => item.download);
  const disabled = !owner.canReadInstallers;
  return `<section class="private-gateway-installer-media" aria-label="安装介质">
    <header><div><h5>签名介质</h5><small>v${escapeHtml(release.manifest.version)} · ${escapeHtml(release.signature.key_id)}</small></div><code>${escapeHtml(release.manifest_sha256.slice(0, 16))}</code></header>
    <div class="private-gateway-installer-files">${artifacts.map(({ artifact, download }) => `
      <div><span><strong>${installerArtifactLabel(artifact.kind)}</strong><small>${escapeHtml(download.filename)} · ${formatBytes(download.size_bytes)}</small></span><button type="button" data-private-gateway-installer-download="${escapeAttribute(download.download_path)}" title="下载 ${escapeAttribute(download.filename)}"${disabled ? " disabled" : ""}>下载</button></div>
    `).join("")}</div>
    ${workflow.recoveryDownloadPath ? `<button type="button" class="private-gateway-installer-recovery" data-private-gateway-installer-download="${escapeAttribute(workflow.recoveryDownloadPath)}">重新下载恢复介质</button>` : ""}
  </section>`;
}

function renderPreflight(preflight) {
  if (!preflight) {
    return `<section class="private-gateway-installer-preflight" aria-label="预检结果"><header><div><h5>预检结果</h5><small>尚未收到报告</small></div><span class="private-gateway-state-status tone-not_observed">未运行</span></header></section>`;
  }
  return `<section class="private-gateway-installer-preflight" aria-label="预检结果">
    <header><div><h5>预检结果</h5><small>${formatTime(preflight.completed_at)} · ${secondsFromMs(preflight.duration_ms)}</small></div><span class="private-gateway-state-status tone-${preflight.outcome === "passed" ? "applied" : "failed"}">${preflight.outcome === "passed" ? "通过" : "未通过"}</span></header>
    <ul>${preflight.checks.map((check) => `<li class="tone-${escapeAttribute(check.outcome)}"><span>${preflightCheckLabel(check.code)}</span><strong>${preflightOutcomeLabel(check.outcome)}</strong><small>${escapeHtml(check.reason_code)}</small></li>`).join("")}</ul>
  </section>`;
}

function renderInstallation(installation, deployment, workflow) {
  if (!installation) {
    return `<section class="private-gateway-installer-status" aria-label="安装状态"><header><div><h5>安装状态</h5><small>尚未开始</small></div><span class="private-gateway-state-status tone-not_observed">待安装</span></header></section>`;
  }
  const retained = [
    installation.retain_config ? "配置" : "",
    installation.retain_audit ? "审计" : "",
    installation.retain_secrets ? "Secret" : "",
  ].filter(Boolean);
  return `<section class="private-gateway-installer-status" aria-label="安装状态">
    <header><div><h5>安装状态</h5><small>${formatTime(installation.updated_at)} · revision ${integer(installation.revision)}</small></div><span class="private-gateway-state-status tone-${workflow.tone}">${installerStatusLabel(installation.status)}</span></header>
    <dl>
      ${fact("当前步骤", installationStepLabel(installation.current_step))}
      ${fact("补偿", compensationLabel(installation.compensation_status))}
      ${fact("失败原因", installation.failure_reason_code ?? "-")}
      ${fact("保留", retained.length ? retained.join(" / ") : "不保留")}
      ${fact("注册健康", healthLabel(deployment.health_code))}
      ${fact("最近连接", formatTime(deployment.last_connected_at))}
    </dl>
  </section>`;
}

function installerWorkflow(state, deployment, release, profile) {
  const preflight = state.installerCatalog.preflights.find((item) => (
    item.deployment_id === deployment.deployment_id
    && item.release_id === release.manifest.release_id
    && item.install_profile_id === profile.install_profile_id
  )) ?? null;
  const installation = state.installerCatalog.installations.find((item) => (
    item.deployment_id === deployment.deployment_id
    && item.release_id === release.manifest.release_id
    && item.install_profile_id === profile.install_profile_id
  )) ?? null;
  const failed = preflight?.outcome === "failed" || installation?.status === "failed"
    || installation?.compensation_status === "failed";
  const complete = installation?.status === "active" && deployment.health_code === "healthy";
  const tone = failed ? "failed" : (complete ? "applied" : "pending");
  const recoveryDownloadPath = failed
    ? state.installerCatalog.downloads.find((item) => (
      item.release_id === release.manifest.release_id
      && release.manifest.artifacts.find((artifact) => (
        artifact.artifact_id === item.artifact_id && artifact.kind === "cli"
      ))
    ))?.download_path ?? ""
    : "";
  return Object.freeze({
    preflight,
    installation,
    failed,
    complete,
    tone,
    label: failed ? "需要恢复" : (complete ? "接入完成" : "进行中"),
    environmentLabel: `${release.manifest.version} · ${profile.profile_code}`,
    preflightLabel: preflight ? (preflight.outcome === "passed" ? "已通过" : "检查失败") : "未运行",
    installationLabel: installation ? installerStatusLabel(installation.status) : "未开始",
    registrationLabel: deployment.health_code === "healthy" ? "健康" : healthLabel(deployment.health_code),
    recoveryDownloadPath,
  });
}

function installerStepTone(workflow, code) {
  if (code === "environment") return "applied";
  if (code === "preflight") return workflow.preflight?.outcome === "failed"
    ? "failed" : (workflow.preflight ? "applied" : "pending");
  if (code === "installation") {
    if (workflow.installation?.status === "failed") return "failed";
    return workflow.installation ? (new Set(["active", "uninstalled"]).has(workflow.installation.status) ? "applied" : "pending") : "pending";
  }
  if (workflow.complete) return "applied";
  if (workflow.installation?.status === "failed") return "failed";
  return "pending";
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
      ${renderValidation(item, state, owner)}
      ${renderSupportBundle(item, state, owner)}
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

function renderValidation(deployment, state, owner) {
  if (!owner.canReadValidations) return "";
  const response = state.validationStates?.[deployment.deployment_id] ?? null;
  const validation = response?.validation ?? null;
  const acceptance = response?.delivery_acceptance ?? null;
  const active = deployment.status === "active";
  const busy = state.validationBusy || state.busy;
  if (!validation) {
    return `<section class="private-gateway-validation" aria-label="交付验证">
      <header><div><h5>交付验证</h5><span class="private-gateway-state-status tone-not_observed">尚未运行</span></div>
        ${owner.canManageValidations && active ? `<button type="button" class="primary" data-private-gateway-validation="start" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${busy ? " disabled" : ""}>运行全套验证</button>` : ""}
      </header>
      <div class="private-gateway-state-empty">等待版本化验证计划</div>
    </section>`;
  }
  const pending = validation.status === "requested";
  const failed = validation.outcome === "failed";
  const passed = validation.outcome === "passed";
  const tone = pending ? "pending" : failed ? "failed" : "applied";
  const status = pending ? "执行中" : failed ? "存在失败项" : "全部通过";
  const checksByCode = new Map(validation.effective_checks.map((item) => [item.code, item]));
  return `<section class="private-gateway-validation" aria-label="交付验证">
    <header>
      <div><h5>交付验证</h5><span class="private-gateway-state-status tone-${tone}">${status}</span></div>
      <div class="private-gateway-validation-actions">
        <button type="button" data-private-gateway-validation="refresh" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${busy ? " disabled" : ""}>刷新结果</button>
        ${owner.canManageValidations && failed && !acceptance ? `<button type="button" class="primary" data-private-gateway-validation="retry_failed" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${busy ? " disabled" : ""}>仅重试失败项</button>` : ""}
        ${owner.canAcceptDelivery && passed && !acceptance ? `<button type="button" class="primary" data-private-gateway-acceptance-open data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${busy ? " disabled" : ""}>签署交付</button>` : ""}
      </div>
    </header>
    <dl class="private-gateway-validation-facts">
      ${fact("计划", validation.plan_version)}
      ${fact("Attempt", integer(validation.attempt))}
      ${fact("Generation", `${integer(validation.applied_generation)} / ${integer(validation.desired_generation)}`)}
      ${fact("耗时", validation.duration_ms === null ? "-" : secondsFromMs(validation.duration_ms))}
    </dl>
    <ol class="private-gateway-validation-checks">${VALIDATION_CHECK_CODES.map((code) => {
      const check = checksByCode.get(code) ?? null;
      const waiting = pending && validation.requested_check_codes.includes(code);
      const outcome = waiting ? "pending" : check?.outcome ?? "pending";
      const label = waiting ? "等待执行" : check ? (check.outcome === "passed" ? "通过" : "失败") : "未执行";
      return `<li class="tone-${outcome}"><span>${validationCheckLabel(code)}</span><strong>${label}</strong>${check && !waiting ? `<small>${escapeHtml(check.reason_code)}${check.recovery_action_code ? ` · ${escapeHtml(validationRecoveryLabel(code))}` : ""}</small>` : ""}</li>`;
    }).join("")}</ol>
    ${acceptance ? `<div class="private-gateway-acceptance-summary">
      <strong>交付已签署</strong><span>${formatTime(acceptance.accepted_at)} · ${escapeHtml(acceptance.accepted_by_account_id)}</span><small>revision ${integer(acceptance.deployment_revision)} · generation ${integer(acceptance.applied_generation)}</small>
    </div>` : ""}
  </section>`;
}

function renderSupportBundle(deployment, state, owner) {
  if (!owner.canReadSupportBundles) return "";
  const entry = state.supportBundleStates?.[deployment.deployment_id] ?? null;
  const local = entry?.local ?? null;
  const cloud = entry?.cloud?.bundle ?? null;
  const connected = state.localSession?.deploymentId === deployment.deployment_id;
  const active = deployment.status === "active";
  const busy = state.supportBundleBusy || state.busy;
  const preview = local?.status === "preview_ready";
  const knownBundleId = local?.cloud_object_reference
    ?? cloud?.support_bundle_id
    ?? "";
  const tone = preview ? "pending" : cloud?.status === "ready" ? "applied" : "not_observed";
  const label = preview ? "等待客户批准" : cloud ? supportBundleStatusLabel(cloud.status) : "暂无摘要";
  return `<section class="private-gateway-support" aria-label="脱敏支持包">
    <header>
      <div><h5>脱敏支持包</h5><span class="private-gateway-state-status tone-${tone}">${label}</span></div>
      <div class="private-gateway-support-actions">
        ${owner.canManageSupportBundles && active && !connected ? `<button type="button" data-private-gateway-secret-open="handoff" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${busy ? " disabled" : ""}>连接本地托管</button>` : ""}
        ${owner.canManageSupportBundles && active && connected && !preview ? `<button type="button" class="primary" data-private-gateway-support-bundle="preview" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${busy ? " disabled" : ""}>生成脱敏预览</button>` : ""}
        ${preview ? `<button type="button" data-private-gateway-support-bundle="cancel" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${busy ? " disabled" : ""}>取消</button><button type="button" class="primary" data-private-gateway-support-bundle="approve" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${busy ? " disabled" : ""}>批准加密上传</button>` : ""}
        ${knownBundleId ? `<button type="button" data-private-gateway-support-bundle="refresh" data-deployment-id="${escapeAttribute(deployment.deployment_id)}"${busy ? " disabled" : ""}>刷新云端摘要</button>` : ""}
      </div>
    </header>
    ${preview ? `<div class="private-gateway-support-preview">
      <dl>${fact("扫描", "通过")}${fact("文件", integer(local.manifest.entries.length))}${fact("原始大小", formatBytes(local.manifest.total_uncompressed_bytes))}${fact("压缩大小", formatBytes(local.manifest.archive_size_bytes))}</dl>
      ${renderSupportEntries(local.manifest.entries, true)}
    </div>` : ""}
    ${cloud ? `<div class="private-gateway-support-cloud">
      <dl>${fact("Bundle ID", cloud.support_bundle_id)}${fact("扫描", "通过")}${fact("压缩大小", formatBytes(cloud.archive_size_bytes))}${fact("密文大小", formatBytes(cloud.object_size_bytes))}${fact("批准时间", formatTime(cloud.approved_at))}${fact("到期时间", formatTime(cloud.expires_at))}</dl>
      ${renderSupportEntries(cloud.entries, false)}
    </div>` : ""}
    ${local && !preview && !cloud ? `<div class="private-gateway-state-empty">本地状态：${escapeHtml(localSupportBundleStatusLabel(local.status))}${local.failure_code ? ` · ${escapeHtml(local.failure_code)}` : ""}</div>` : ""}
    ${!local && !cloud ? '<div class="private-gateway-state-empty">尚无当前会话可恢复的支持包摘要</div>' : ""}
  </section>`;
}

function renderSupportEntries(entries, includePath) {
  return `<ul class="private-gateway-support-entries">${entries.map((item) => `<li><span><strong>${supportCollectorLabel(item.collector_code)}</strong><small>${includePath ? `${escapeHtml(item.path)} · ` : ""}${supportClassificationLabel(item.classification)}</small></span><b>${formatBytes(item.size_bytes)}</b></li>`).join("")}</ul>`;
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

function renderAcceptanceDialog(state, owner) {
  const dialog = state.acceptanceDialog;
  if (!owner.canAcceptDelivery || !dialog?.open) return "";
  const submitting = dialog.state === "submitting";
  return `<dialog class="private-gateway-dialog private-gateway-acceptance-dialog" data-private-gateway-acceptance-dialog aria-labelledby="private-gateway-acceptance-dialog-title" aria-busy="${submitting}">
    <header><div><h3 id="private-gateway-acceptance-dialog-title">签署交付</h3><span>绑定当前验证 generation</span></div><button type="button" class="private-gateway-icon" data-private-gateway-acceptance-close title="关闭" aria-label="关闭"${submitting ? " disabled" : ""}>&times;</button></header>
    ${dialog.error ? renderError(dialog.error) : ""}
    <form data-private-gateway-acceptance-form autocomplete="off" novalidate>
      <label><span>二次验证方式</span><select name="method" data-private-gateway-acceptance-method${submitting ? " disabled" : ""}><option value="totp">Authenticator 验证码</option><option value="webauthn">安全密钥</option><option value="recovery_code">恢复码</option></select></label>
      <label data-private-gateway-acceptance-code><span>验证码</span><input name="verification_code" type="password" maxlength="20" autocomplete="one-time-code" data-private-gateway-sensitive required${submitting ? " disabled" : ""}></label>
      <label class="private-gateway-confirm"><input type="checkbox" name="confirmed" required${submitting ? " disabled" : ""}><span>确认当前八项有效检查全部通过并接受本次交付</span></label>
      <p class="private-gateway-form-error" data-private-gateway-acceptance-form-error role="alert"></p>
      <div class="private-gateway-dialog-actions"><button type="button" data-private-gateway-acceptance-close${submitting ? " disabled" : ""}>取消</button><button type="submit" class="primary"${submitting ? " disabled" : ""}>${submitting ? "正在签署" : "验证并签署"}</button></div>
    </form>
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
    "edge.validation_started": "全套交付验证已启动",
    "edge.validation_retry_started": "失败检查项已重新发起",
    "edge.validation_refreshed": "验证结果已刷新",
    "edge.delivery_accepted": "交付已签署",
    "edge.delivery_acceptance_replayed": "已恢复交付签署结果",
    "local_support_bundle.preview_ready": "脱敏支持包预览已生成",
    "local_support_bundle.uploaded": "支持包已批准、加密并上传",
    "local_support_bundle.cancelled": "支持包预览已取消并清理",
    "edge.support_bundle_refreshed": "云端支持包摘要已刷新",
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
function installerChannelLabel(value) { return value === "extended" ? "长期支持" : "稳定"; }
function installerArtifactLabel(value) { return ({ cli: "安装器", compose: "Compose", container_bundle: "离线镜像清单", profile: "环境规格", sbom: "SBOM" })[value] ?? value; }
function installerStatusLabel(value) { return ({ preflight_passed: "预检通过", installing: "安装中", installed: "已安装", connecting: "接入中", active: "运行中", failed: "安装失败", uninstalling: "卸载中", uninstalled: "已卸载" })[value] ?? "未知"; }
function installationStepLabel(value) { return ({ staging_prepared: "暂存区就绪", artifacts_copied: "介质已复制", runtime_validated: "运行时已校验", runtime_started: "运行时已启动", health_verified: "健康已验证", release_committed: "版本已提交", device_registered: "设备已注册", initial_snapshot: "初始状态已同步" })[value] ?? "-"; }
function compensationLabel(value) { return ({ not_required: "无需补偿", pending: "补偿中", completed: "补偿完成", failed: "补偿失败" })[value] ?? "-"; }
function proxyLabel(value) { return value === "http_connect" ? "HTTP Proxy" : "Direct"; }
function caLabel(value) { return value === "system_or_custom" ? "System / Custom CA" : "System CA"; }
function preflightOutcomeLabel(value) { return ({ passed: "通过", warning: "警告", failed: "失败" })[value] ?? "未知"; }
function preflightCheckLabel(value) { return ({ "artifact.integrity": "介质完整性", "host.os": "Linux 基线", "host.architecture": "CPU 架构", "host.root": "Root 权限", "host.cpu": "CPU", "host.memory": "内存", "host.disk": "磁盘", "network.ports": "端口", "network.dns": "DNS", "host.clock": "系统时间", "network.proxy": "代理", "network.ca": "CA", "host.filesystem": "文件系统", "runtime.container": "容器运行时" })[value] ?? value; }
function validationCheckLabel(value) { return ({ device_certificate: "设备证书", signed_config_lkg: "签名配置与 LKG", provider_connectivity: "Provider 连通性", minimal_gateway_request: "最小真实请求", usage_cost_failure_sync: "Usage / 成本 / 失败同步", backup_readiness: "备份就绪", capacity_readiness: "容量就绪", performance_baseline: "性能基线" })[value] ?? value; }
function validationRecoveryLabel(value) { return ({ device_certificate: "轮换或重新签发设备证书", signed_config_lkg: "重新发布配置并确认 LKG", provider_connectivity: "检查本地 Provider 连接", minimal_gateway_request: "检查 Gateway 路由与模型可用性", usage_cost_failure_sync: "恢复遥测同步并重新对账", backup_readiness: "完成备份就绪检查", capacity_readiness: "调整安装规格或容量", performance_baseline: "恢复性能基线后重试" })[value] ?? "按产品恢复动作处理"; }
function supportBundleStatusLabel(value) { return ({ ready: "可供授权支持读取", expired: "已过期", deleted: "已删除" })[value] ?? "未知"; }
function localSupportBundleStatusLabel(value) { return ({ preview_ready: "等待批准", uploading: "加密上传中", uploaded: "已上传", failed: "处理失败", cancelled: "已取消", expired: "已过期" })[value] ?? "未知"; }
function supportCollectorLabel(value) { return ({ "runtime.version": "运行版本", "runtime.watermarks": "同步水位", "runtime.health": "健康摘要", "runtime.errors": "错误码摘要", "runtime.logs": "日志事件摘要" })[value] ?? value; }
function supportClassificationLabel(value) { return ({ version: "版本", watermark: "水位", health: "健康", error_code: "错误码", log_summary: "日志摘要" })[value] ?? value; }
function formatBytes(value) { if (!Number.isSafeInteger(value)) return "-"; if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MiB`; if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`; return `${value} B`; }
function secondsFromMs(value) { return Number.isSafeInteger(value) ? `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)}s` : "-"; }
function formatTime(value) { if (!value) return "-"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date); }
function integer(value) { return Number.isSafeInteger(value) ? String(value) : "-"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value).replaceAll("`", "&#096;"); }
