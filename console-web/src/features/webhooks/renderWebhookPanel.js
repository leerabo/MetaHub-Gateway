import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 事件标签只翻译后端白名单，签名、订阅匹配和投递资格仍由服务端决定。
const EVENT_TYPE_LABELS = {
  "budget.threshold_reached": "预算达到告警",
  "budget.exhausted": "预算耗尽",
  "security.dlp_matched": "DLP 命中",
  "security.dlp_unavailable": "DLP 不可用",
  "security.event_pushed": "SIEM 安全事件",
  "gateway.request_failed": "调用失败",
  "webhook.delivery_failed": "Webhook 投递失败",
  "webhook.replayed": "Webhook 已重放",
  "role.permission_changed": "角色权限已变更",
};

// Webhook 状态标签统一发送结果和 Endpoint 状态文案，不能暴露接收端响应正文。
const STATUS_LABELS = {
  active: "启用",
  disabled: "停用",
  pending: "发送中",
  success: "成功",
  failed: "失败",
};

/**
 * 渲染 Webhook Endpoint 设置与发送记录面板。
 * 前端只做 admin/developer 的体验层展示；真实角色权限、tenant_id 边界、签名密钥和发送记录都由后端 /webhooks 校验。
 */
export function renderWebhookPanel(webhookState, authContext) {
  // Webhook 页面只消费后端 webhook.manage，后端仍必须拒绝手写越权请求。
  if (!hasPermission(authContext, PERMISSIONS.WEBHOOK_MANAGE)) {
    return "";
  }
  if (!webhookState) {
    return renderLoadingPanel();
  }
  // API 错误必须展示 request_id，方便运维把控制台提示和后端 Webhook 日志串起来。
  if (webhookState.state === "error") {
    return renderErrorPanel(webhookState);
  }

  const payload = webhookState.webhooks ?? webhookState;
  const endpoints = payload.webhooks ?? [];
  const eventTypes = (payload.event_types ?? []).filter((item) => item !== "webhook.test");
  const selectedEndpointId = webhookState.selectedEndpointId ?? endpoints[0]?.webhook_endpoint_id ?? null;
  return `
    <div class="panel-heading webhook-heading">
      <div>
        <h2 id="webhooks-title">Webhook 设置</h2>
        <p>管理企业回调与 SIEM 安全事件投递；发送记录保留每次重试摘要。</p>
      </div>
      <span class="webhook-count">${escapeHtml(endpoints.length)} 个 Endpoint</span>
    </div>
    ${renderActionError(webhookState.actionError)}
    ${renderFeedback(webhookState.feedback)}
    ${renderCreatedSecret(webhookState.created)}
    ${renderWebhookForm(eventTypes)}
    ${renderEndpointTable(endpoints, selectedEndpointId)}
    ${renderDeliveryRecords(
      webhookState.deliveryDetails,
      webhookState.deliveryFilters,
      eventTypes,
      endpoints.find((endpoint) => endpoint.webhook_endpoint_id === selectedEndpointId),
    )}
  `;
}

/**
 * 渲染 Webhook API 加载态，避免导航切换时出现空白面板。
 */
function renderLoadingPanel() {
  return `
    <div class="panel-heading">
      <h2 id="webhooks-title">Webhook 设置</h2>
      <p>正在读取当前租户的 Endpoint 和发送记录。</p>
    </div>
    <div class="empty-state">等待 /webhooks 响应。</div>
  `;
}

/**
 * 渲染 Webhook API 错误状态，并保留 request_id 便于定位权限、数据库或投递服务异常。
 */
function renderErrorPanel(state) {
  return `
    <div class="panel-heading">
      <h2 id="webhooks-title">Webhook 设置</h2>
      <p>Webhook 暂不可用。</p>
    </div>
    <div class="empty-state error-state">
      ${escapeHtml(state.message ?? "Webhook 读取失败")} request_id: ${escapeHtml(state.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染创建 Endpoint 表单。
 * 表单不接收 signing_secret；secret 由后端生成并仅在创建响应中返回一次。
 */
function renderWebhookForm(eventTypes) {
  return `
    <form class="webhook-form" id="webhook-form">
      <h3>新增 Endpoint</h3>
      <label>
        <span>名称</span>
        <input name="name" placeholder="例如：运维告警系统" required />
      </label>
      <label class="webhook-url-field">
        <span>Endpoint URL</span>
        <input name="endpointUrl" type="url" placeholder="https://ops.example.com/metahub/webhook" required />
      </label>
      <fieldset class="webhook-format-field">
        <legend>投递格式</legend>
        <div class="webhook-format-options">
          <label>
            <input class="webhook-format-radio" type="radio" name="format" value="json" checked />
            <span>JSON</span>
          </label>
          <label>
            <input class="webhook-format-radio" type="radio" name="format" value="cef" />
            <span>CEF</span>
          </label>
        </div>
      </fieldset>
      <label class="webhook-security-rate-field">
        <span>安全事件 / 分钟</span>
        <input name="securityEventRateLimitPerMinute" type="number" min="1" max="600" step="1" value="60" required />
      </label>
      <fieldset class="webhook-event-field">
        <legend>订阅事件</legend>
        ${eventTypes.map(renderEventCheckbox).join("")}
      </fieldset>
      <label>
        <span>超时 ms</span>
        <input name="timeoutMs" type="number" min="1000" max="30000" step="1000" value="5000" />
      </label>
      <label>
        <span>状态</span>
        <select name="status">
          <option value="active">启用</option>
          <option value="disabled">停用</option>
        </select>
      </label>
      <div class="webhook-actions">
        <button type="submit">保存 Endpoint</button>
        <span>创建后只显示一次签名密钥。</span>
      </div>
    </form>
  `;
}

/**
 * 渲染事件类型复选框，默认选中常用治理事件，排除 webhook.test 测试事件。
 */
function renderEventCheckbox(eventType) {
  const checked = ["budget.threshold_reached", "budget.exhausted", "security.dlp_matched", "gateway.request_failed"].includes(eventType)
    ? " checked"
    : "";
  return `
    <label>
      <input type="checkbox" name="eventTypes" value="${escapeAttribute(eventType)}"${checked} />
      <span>${escapeHtml(EVENT_TYPE_LABELS[eventType] ?? eventType)}</span>
    </label>
  `;
}

/**
 * 同步 PAGE-113 格式控件。CEF 只能保留标准 SIEM 事件；切回 JSON 只恢复编辑，不暗自扩订阅。
 */
export function applyWebhookFormatMode(form, format) {
  const normalizedFormat = format === "cef" ? "cef" : "json";
  for (const input of form?.querySelectorAll?.('input[name="eventTypes"]') ?? []) {
    const isSecurityEvent = input.value === "security.event_pushed";
    if (normalizedFormat === "cef") {
      input.checked = isSecurityEvent;
    }
    input.disabled = normalizedFormat === "cef" && !isSecurityEvent;
  }
  return normalizedFormat;
}

/**
 * 渲染创建成功后的一次性 signing secret。
 * 该值只来自刚创建的响应，不会出现在列表接口、发送记录或 console 事件中。
 */
function renderCreatedSecret(created) {
  if (!created?.signingSecret) {
    return "";
  }
  return `
    <div class="webhook-secret-box" role="status">
      <div>
        <strong>${escapeHtml(created.reason === "rotated" ? "新签名密钥仅显示一次" : "签名密钥仅显示一次")}</strong>
        <code>${escapeHtml(created.signingSecret)}</code>
      </div>
      <button type="button" data-copy-webhook-secret="${escapeAttribute(created.signingSecret)}">复制</button>
      <button type="button" data-dismiss-webhook-secret>隐藏</button>
    </div>
  `;
}

/**
 * 渲染 Endpoint 表格，展示订阅、密钥指纹和最近投递状态。
 */
function renderEndpointTable(endpoints, selectedEndpointId) {
  return `
    <div class="webhook-endpoint-table" role="table" aria-label="Webhook Endpoint">
      <div class="webhook-endpoint-row webhook-endpoint-head" role="row">
        <span role="columnheader">Endpoint</span>
        <span role="columnheader">订阅事件</span>
        <span role="columnheader">状态</span>
        <span role="columnheader">签名指纹</span>
        <span role="columnheader">最近投递</span>
        <span role="columnheader">操作</span>
      </div>
      ${endpoints.length ? endpoints.map((endpoint) => renderEndpointRow(endpoint, selectedEndpointId)).join("") : renderEmptyEndpointRow()}
    </div>
  `;
}

/**
 * 渲染单个 Endpoint 行，并提供测试发送按钮。
 */
function renderEndpointRow(endpoint, selectedEndpointId) {
  const selected = endpoint.webhook_endpoint_id === selectedEndpointId;
  return `
    <div class="webhook-endpoint-row${selected ? " is-selected" : ""}" role="row">
      <span class="webhook-endpoint-name" role="cell" data-label="Endpoint">
        <strong>${escapeHtml(endpoint.name)}</strong>
        <em>${escapeHtml(endpoint.endpoint_url)}</em>
        <code>${escapeHtml(endpoint.webhook_endpoint_id)}</code>
        ${renderWireMeta(endpoint)}
      </span>
      <span class="webhook-event-list" role="cell" data-label="订阅事件">${(endpoint.event_types ?? []).map(renderEventPill).join("")}</span>
      <span role="cell" data-label="状态"><mark class="status-${escapeAttribute(endpoint.status)}">${escapeHtml(STATUS_LABELS[endpoint.status] ?? endpoint.status)}</mark></span>
      <span class="webhook-secret-meta" role="cell" data-label="签名指纹">
        <code>${escapeHtml(endpoint.secret_prefix ?? "-")}</code>
        <em>${escapeHtml(endpoint.secret_fingerprint ?? "-")}</em>
        <em>版本 ${escapeHtml(endpoint.secret_version ?? 1)}</em>
      </span>
      <span role="cell" data-label="最近投递">
        <mark class="status-${escapeAttribute(endpoint.last_delivery_status ?? "pending")}">
          ${escapeHtml(STATUS_LABELS[endpoint.last_delivery_status] ?? endpoint.last_delivery_status ?? "暂无")}
        </mark>
        <em>${escapeHtml(formatDateTime(endpoint.last_delivery_at))}</em>
      </span>
      <span role="cell" data-label="操作">
        <button type="button" data-webhook-test="${escapeAttribute(endpoint.webhook_endpoint_id)}">发送测试</button>
        <button type="button" data-webhook-details="${escapeAttribute(endpoint.webhook_endpoint_id)}" aria-pressed="${selected}">
          查看记录
        </button>
        <button type="button" data-webhook-rotate="${escapeAttribute(endpoint.webhook_endpoint_id)}">
          轮换 Secret
        </button>
      </span>
    </div>
  `;
}

/**
 * 渲染发送记录区域，合并列表接口中的最近记录和刚刚测试的最新结果。
 */
function renderDeliveryRecords(deliveryDetails, filters, eventTypes, endpoint) {
  if (!endpoint) {
    return "";
  }
  const deliveries = deliveryDetails?.deliveries ?? [];
  // 详情只使用后端按当前筛选返回的数据，避免最近操作结果绕过 event/status 筛选混入列表。
  const unique = dedupeDeliveries(deliveries).slice(0, 100);
  return `
    <section class="webhook-delivery-section" aria-labelledby="webhook-delivery-title">
      <div class="panel-heading compact-heading">
        <h3 id="webhook-delivery-title">${escapeHtml(endpoint.name)} · 最近发送记录</h3>
        <p>最多展示 100 条；失败记录可幂等重放，本阶段不做对象存储死信桶。</p>
      </div>
      ${renderDeliveryFilters(filters, eventTypes, endpoint.webhook_endpoint_id)}
      <div class="webhook-delivery-table" role="table" aria-label="Webhook 发送记录">
        <div class="webhook-delivery-row webhook-delivery-head" role="row">
          <span role="columnheader">事件</span>
          <span role="columnheader">状态</span>
          <span role="columnheader">尝试</span>
          <span role="columnheader">HTTP</span>
          <span role="columnheader">错误</span>
          <span role="columnheader">操作</span>
        </div>
        ${unique.length ? unique.map(renderDeliveryRow).join("") : renderEmptyDeliveryRow()}
      </div>
    </section>
  `;
}

/**
 * 渲染当前 Endpoint 的历史筛选工具栏。
 * 筛选只提交 event_type/status，limit 固定为 100，租户与 Endpoint 归属仍由后端校验。
 */
function renderDeliveryFilters(filters, eventTypes, endpointId) {
  const activeEventType = filters?.eventType ?? "";
  const activeStatus = filters?.status ?? "all";
  return `
    <form class="webhook-delivery-filters" id="webhook-delivery-filters" data-webhook-endpoint-id="${escapeAttribute(endpointId)}">
      <label>
        <span>事件类型</span>
        <select name="eventType">
          <option value="">全部事件</option>
          <option value="webhook.test"${activeEventType === "webhook.test" ? " selected" : ""}>Webhook 测试</option>
          ${eventTypes
            .map(
              (eventType) =>
                `<option value="${escapeAttribute(eventType)}"${activeEventType === eventType ? " selected" : ""}>${escapeHtml(EVENT_TYPE_LABELS[eventType] ?? eventType)}</option>`,
            )
            .join("")}
        </select>
      </label>
      <label>
        <span>投递状态</span>
        <select name="status">
          ${[
            ["all", "全部状态"],
            ["failed", "失败"],
            ["success", "成功"],
            ["pending", "发送中"],
          ]
            .map(([value, label]) => `<option value="${value}"${activeStatus === value ? " selected" : ""}>${label}</option>`)
            .join("")}
        </select>
      </label>
      <button type="submit">应用筛选</button>
      <button type="button" data-webhook-refresh-details="${escapeAttribute(endpointId)}">刷新</button>
    </form>
  `;
}

/**
 * 渲染单条发送记录和逐次 attempt 摘要。
 */
function renderDeliveryRow(delivery) {
  return `
    <div class="webhook-delivery-row" role="row">
      <span role="cell" data-label="事件">
        <strong>${escapeHtml(EVENT_TYPE_LABELS[delivery.event_type] ?? delivery.event_type)}</strong>
        <em>${escapeHtml(delivery.event_id)}</em>
        <time datetime="${escapeAttribute(delivery.event_timestamp)}">${escapeHtml(formatDateTime(delivery.event_timestamp))}</time>
        ${renderWireMeta(delivery)}
      </span>
      <span role="cell" data-label="状态"><mark class="status-${escapeAttribute(delivery.status)}">${escapeHtml(STATUS_LABELS[delivery.status] ?? delivery.status)}</mark></span>
      <span role="cell" data-label="尝试">${escapeHtml(delivery.attempt_count)} / ${escapeHtml(delivery.max_attempts)}${renderAttempts(delivery.attempts ?? [])}</span>
      <span role="cell" data-label="HTTP">${escapeHtml(delivery.http_status ?? "-")}</span>
      <span role="cell" data-label="错误">${escapeHtml(delivery.error_code ?? "-")} ${escapeHtml(delivery.error_summary ?? "")}</span>
      <span role="cell" data-label="操作">
        ${
          delivery.status === "failed"
            ? `<button type="button" data-webhook-replay="${escapeAttribute(delivery.webhook_delivery_id)}" data-webhook-endpoint-id="${escapeAttribute(delivery.webhook_endpoint_id)}">重放</button>`
            : "-"
        }
        <em>${escapeHtml(delivery.delivery_kind === "replay" ? "重放" : "原始")} · Secret v${escapeHtml(delivery.secret_version ?? 1)}</em>
      </span>
    </div>
  `;
}

/**
 * 渲染 attempt 小列表，只展示安全错误摘要，不展示响应正文或签名头。
 */
function renderAttempts(attempts) {
  if (!attempts.length) {
    return "";
  }
  return `
    <ol class="webhook-attempt-list">
      ${attempts
        .map(
          (attempt) => `
            <li>
              <mark class="status-${escapeAttribute(attempt.status)}">${escapeHtml(STATUS_LABELS[attempt.status] ?? attempt.status)}</mark>
              <span>${escapeHtml(attempt.http_status ?? attempt.error_code ?? "-")}</span>
            </li>
          `,
        )
        .join("")}
    </ol>
  `;
}

/**
 * 渲染事件类型标签。
 * 标签只展示后端白名单事件名称对应的中文短名，避免把 payload 内容或业务系统地址带入列表。
 */
function renderEventPill(eventType) {
  return `<mark>${escapeHtml(EVENT_TYPE_LABELS[eventType] ?? eventType)}</mark>`;
}

/** 渲染后端冻结的 wire 格式与安全事件上限，不读取 wire body、签名或 rate claim。 */
function renderWireMeta(value) {
  const format = value?.format === "cef" ? "CEF" : "JSON";
  const limit = Number.isInteger(value?.security_event_rate_limit_per_minute)
    ? value.security_event_rate_limit_per_minute
    : "-";
  return `
    <span class="webhook-wire-meta">
      <mark class="webhook-format-badge">${format}</mark>
      <em>安全事件 ${escapeHtml(limit)} / 分钟</em>
    </span>
  `;
}

/**
 * 渲染空 Endpoint 行，保持表格结构稳定。
 */
function renderEmptyEndpointRow() {
  return `
    <div class="webhook-endpoint-row webhook-empty" role="row">
      <span>当前租户尚未创建 Webhook Endpoint。</span>
    </div>
  `;
}

/**
 * 渲染空发送记录行，避免用户误以为列表接口失败。
 */
function renderEmptyDeliveryRow() {
  return `
    <div class="webhook-delivery-row webhook-empty" role="row">
      <span>尚无发送记录，可先创建 Endpoint 并发送测试事件。</span>
    </div>
  `;
}

/**
 * 渲染 Webhook 操作错误，错误文本只来自统一错误结构。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="webhook-action-error" role="alert">
      ${escapeHtml(error.message ?? "Webhook 操作失败")} request_id: ${escapeHtml(error.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染 Webhook 操作反馈，例如创建成功、复制成功或测试完成。
 */
function renderFeedback(feedback) {
  if (!feedback) {
    return "";
  }
  const message = typeof feedback === "string" ? feedback : feedback.message;
  const requestId = typeof feedback === "string" ? null : feedback.requestId;
  return `<div class="webhook-feedback" role="status">${escapeHtml(message ?? "")}${
    requestId ? ` request_id: ${escapeHtml(requestId)}` : ""
  }</div>`;
}

/**
 * 去重发送记录，优先保留刚刚测试得到的最新 delivery 对象。
 */
function dedupeDeliveries(deliveries) {
  const seen = new Set();
  return deliveries.filter((delivery) => {
    if (!delivery?.webhook_delivery_id || seen.has(delivery.webhook_delivery_id)) {
      return false;
    }
    seen.add(delivery.webhook_delivery_id);
    return true;
  });
}

/**
 * 格式化 ISO 时间，避免表格中出现过长的小数秒。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
}

/**
 * HTML 文本转义，防止 Endpoint 名称、URL 或错误摘要破坏页面结构。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * HTML 属性转义，额外处理反引号以保护 data-* 属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
