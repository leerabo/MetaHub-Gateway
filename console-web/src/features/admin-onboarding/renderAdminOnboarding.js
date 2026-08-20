const SAFE_NAVIGATION_ID = /^[a-z][a-z0-9-]{0,63}$/;

/** Render a compact enterprise-admin starting point from server-computed facts. */
export function renderAdminOnboarding(state) {
  if (!state) {
    return `
      <section class="admin-onboarding admin-onboarding-loading" aria-labelledby="admin-onboarding-title" aria-busy="true">
        <div><p class="admin-onboarding-kicker">企业初始化</p><h2 id="admin-onboarding-title">正在确认下一步</h2></div>
      </section>
    `;
  }
  if (state.state === "error") {
    return `
      <section class="admin-onboarding admin-onboarding-error" aria-labelledby="admin-onboarding-title" role="status">
        <div><p class="admin-onboarding-kicker">企业初始化</p><h2 id="admin-onboarding-title">初始化进度暂时不可用</h2><p>${escapeHtml(state.message || "请稍后刷新重试。")}</p></div>
      </section>
    `;
  }

  const steps = Array.isArray(state.steps) ? state.steps : [];
  const mandatory = steps.filter((step) => step.status !== "optional");
  const current = steps.find((step) => step.status === "current") ?? null;
  const completed = Number(state.completed_steps) || 0;
  const total = Math.max(Number(state.total_steps) || mandatory.length, 1);
  const percent = Math.min(100, Math.round((completed / total) * 100));
  const title = state.is_complete ? "基础接入已完成" : "从这里开始配置企业 AI 服务";
  const summary = state.is_complete
    ? "调用链路已经跑通，可以继续完善安全策略、预算和团队协作。"
    : `已完成 ${completed}/${total} 项，按顺序完成可确保调用、用量和成本都能追踪。`;

  return `
    <section class="admin-onboarding" aria-labelledby="admin-onboarding-title">
      <div class="admin-onboarding-heading">
        <div><p class="admin-onboarding-kicker">企业初始化</p><h2 id="admin-onboarding-title">${escapeHtml(title)}</h2><p>${escapeHtml(summary)}</p></div>
        <strong class="admin-onboarding-progress-label">${percent}%</strong>
      </div>
      <div class="admin-onboarding-progress" role="progressbar" aria-label="企业初始化进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%"></span></div>
      ${current ? renderCurrentStep(current) : renderCompleteActions(steps)}
      <details class="admin-onboarding-details">
        <summary>查看全部初始化步骤</summary>
        <ol>${steps.map(renderStep).join("")}</ol>
      </details>
    </section>
  `;
}

function renderCurrentStep(step) {
  const navigationId = navigationIdFromHash(step.target_hash);
  const action = navigationId
    ? `<button type="button" class="primary-button admin-onboarding-action" data-console-nav="${escapeAttribute(navigationId)}" data-console-target="${escapeAttribute(navigationId)}">${escapeHtml(step.action_label)}</button>`
    : "";
  return `
    <div class="admin-onboarding-next">
      <div><span>下一步</span><h3>${escapeHtml(step.title)}</h3><p>${escapeHtml(step.description)}</p></div>
      ${action}
    </div>
  `;
}

function renderCompleteActions(steps) {
  const nextOptional = steps.find((step) => step.status === "optional");
  if (!nextOptional) return "";
  const navigationId = navigationIdFromHash(nextOptional.target_hash);
  return `
    <div class="admin-onboarding-next admin-onboarding-next-optional">
      <div><span>建议完善</span><h3>${escapeHtml(nextOptional.title)}</h3><p>${escapeHtml(nextOptional.description)}</p></div>
      ${navigationId ? `<button type="button" class="secondary-button admin-onboarding-action" data-console-nav="${escapeAttribute(navigationId)}" data-console-target="${escapeAttribute(navigationId)}">${escapeHtml(nextOptional.action_label)}</button>` : ""}
    </div>
  `;
}

function renderStep(step) {
  const label = step.status === "complete" ? "已完成" : step.status === "current" ? "当前" : step.status === "optional" ? "建议" : "待解锁";
  return `
    <li class="admin-onboarding-step status-${escapeAttribute(step.status)}">
      <span class="admin-onboarding-step-mark" aria-hidden="true"></span>
      <div><strong>${escapeHtml(step.title)}</strong><span>${label}</span>${step.blocking_reason ? `<small>${escapeHtml(step.blocking_reason)}</small>` : ""}</div>
    </li>
  `;
}

function navigationIdFromHash(value) {
  const candidate = String(value ?? "").replace(/^#/, "");
  return SAFE_NAVIGATION_ID.test(candidate) ? candidate : "";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
