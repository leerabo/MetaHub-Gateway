/**
 * 渲染系统健康面板。
 *
 * 该面板展示 Gateway、Control API 和依赖 readiness；它只用于本地/运维观测，不作为
 * 业务权限判断依据。
 */
export function renderSystemStatusPanel(systemHealth) {
  if (!systemHealth) {
    return `
      <div class="panel-heading">
        <h2 id="system-status-title">系统状态</h2>
        <p>正在读取 live、ready 和 version。</p>
      </div>
      <div class="empty-state">等待健康检查响应。</div>
    `;
  }

  const services = systemHealth.services ?? [];
  return `
    <div class="panel-heading">
      <h2 id="system-status-title">系统状态</h2>
      <p>区分进程存活和依赖可用，便于上线前检查。</p>
    </div>
    <div class="health-grid">
      ${services.map(renderServiceHealth).join("")}
    </div>
  `;
}

/**
 * 渲染单个服务健康结果。
 *
 * 服务不可达时仍展示 request_id 或错误摘要，方便定位是网关、控制面还是控制台异常。
 */
function renderServiceHealth(service) {
  if (service.state === "error") {
    return `
      <article class="health-card">
        <div class="health-card-heading">
          <h3>${escapeHtml(service.name)}</h3>
          <span class="health-badge badge-fail">不可用</span>
        </div>
        <p class="health-message">${escapeHtml(service.message)}</p>
      </article>
    `;
  }
  const readyBadge = service.ready?.ready ? "badge-ok" : "badge-fail";
  const liveBadge = service.live?.live ? "badge-ok" : "badge-fail";
  return `
    <article class="health-card">
      <div class="health-card-heading">
        <h3>${escapeHtml(service.name)}</h3>
        <span class="health-badge ${readyBadge}">${service.ready?.ready ? "ready" : "not ready"}</span>
      </div>
      <dl class="health-meta">
        <div><dt>live</dt><dd><span class="health-badge ${liveBadge}">${service.live?.live ? "ok" : "fail"}</span></dd></div>
        <div><dt>version</dt><dd>${escapeHtml(service.version?.version ?? "-")}</dd></div>
        <div><dt>commit</dt><dd>${escapeHtml(service.version?.build_commit ?? "-")}</dd></div>
        <div><dt>trace_id</dt><dd>${escapeHtml(service.ready?.trace_id ?? service.live?.trace_id ?? "-")}</dd></div>
      </dl>
      <div class="dependency-list">
        ${(service.ready?.checks ?? []).map(renderDependency).join("") || '<span class="dependency muted">无依赖检查</span>'}
      </div>
    </article>
  `;
}

/**
 * 渲染 readiness 依赖项。
 *
 * 每个依赖只展示安全状态和延迟，不暴露数据库 URL、Provider Key 等敏感配置。
 */
function renderDependency(item) {
  const badgeClass = item.status === "ok" ? "badge-ok" : item.status === "fail" ? "badge-fail" : "badge-skip";
  return `
    <span class="dependency">
      <strong>${escapeHtml(item.name)}</strong>
      <em class="${badgeClass}">${escapeHtml(item.status)}</em>
    </span>
  `;
}

/**
 * HTML 文本转义。
 *
 * 健康检查 message 可能来自异常消息，进入页面前必须转义。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
