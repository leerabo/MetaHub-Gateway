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
        <p>正在检查服务状态。</p>
      </div>
      <div class="empty-state">等待健康检查响应。</div>
    `;
  }

  const services = systemHealth.services ?? [];
  return `
    <div class="panel-heading">
      <h2 id="system-status-title">系统状态</h2>
      <p>查看网关和管理服务是否可以正常使用。</p>
    </div>
    <div class="health-grid">
      ${services.map(renderServiceHealth).join("")}
    </div>
  `;
}

/**
 * 渲染单个服务健康结果。
 *
 * 服务不可达时展示可行动的状态摘要，不把内部定位号带给用户。
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
        <span class="health-badge ${readyBadge}">${service.ready?.ready ? "可用" : "暂不可用"}</span>
      </div>
      <dl class="health-meta">
        <div><dt>服务状态</dt><dd><span class="health-badge ${liveBadge}">${service.live?.live ? "运行中" : "已停止"}</span></dd></div>
      </dl>
      <div class="dependency-list">
        ${(service.ready?.checks ?? []).map(renderDependency).join("") || '<span class="dependency muted">暂无详细状态</span>'}
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
  const label = item.status === "ok" ? "正常" : item.status === "fail" ? "异常" : "未检查";
  return `
    <span class="dependency">
      <strong>${escapeHtml(item.name)}</strong>
      <em class="${badgeClass}">${label}</em>
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
