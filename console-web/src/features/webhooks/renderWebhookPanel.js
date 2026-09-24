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
  "adapter.contract_failed": "适配器合同验证失败",
  "adapter.spec_updated": "适配器规格已更新",
  "approval.created": "审批已创建",
  "approval.timeout": "审批超时",
  "assistant.queried": "管理助手已查询",
  "audit.exported": "审计记录已导出",
  "audit.recorded": "审计事件已记录",
  "batch_job.completed": "批处理已完成",
  "batch_job.created": "批处理已创建",
  "billing_dispute.created": "账单争议已创建",
  "branding.updated": "品牌配置已更新",
  "budget.blocked": "预算已阻断",
  "budget.scope_exceeded": "预算范围已超限",
  "budget.unblocked": "预算阻断已解除",
  "cache.affinity_selected": "缓存亲和路由已选择",
  "cache.hit": "响应缓存已命中",
  "cache.invalidated": "缓存已失效",
  "chat.message_sent": "对话消息已发送",
  "config.imported": "配置已导入",
  "cost.anomaly_detected": "成本异常已发现",
  "customer.health_changed": "客户健康状态已变更",
  "environment.created": "运行环境已创建",
  "error_map.updated": "错误映射已更新",
  "experiment.completed": "实验已完成",
  "feedback.created": "用户反馈已提交",
  "file.prompt_injection_detected": "文件提示注入已发现",
  "file.scanned": "文件扫描已完成",
  "file.uploaded": "文件已上传",
  "invitation.accepted": "邀请已接受",
  "ledger.entry_created": "账务条目已创建",
  "local_model.called": "本地模型已调用",
  "log.query_executed": "日志查询已执行",
  "mcp.network_blocked": "MCP 网络访问已阻断",
  "mcp.tool_called": "MCP 工具已调用",
  "model.feedback_recorded": "模型反馈已记录",
  "partner.customer_created": "伙伴客户已创建",
  "policy.created": "安全策略已创建",
  "policy.dry_run_executed": "策略模拟已执行",
  "policy.validation_failed": "策略验证失败",
  "policy_snapshot.published": "策略快照已发布",
  "policy_template.applied": "策略模板已应用",
  "price.updated": "价格已更新",
  "product_event.recorded": "产品事件已记录",
  "prompt_template.published": "提示词模板已发布",
  "provider.unhealthy": "模型服务异常",
  "quota.exhausted": "额度已耗尽",
  "request.explanation_viewed": "请求解析已查看",
  "route.fallback_triggered": "备用路由已触发",
  "route.policy_selected": "路由策略已选择",
  "security.intent_detected": "敏感意图已发现",
  "security.output_blocked": "输出已阻断",
  "security.session_high_risk": "高风险会话已发现",
  "stream.timeout": "流式调用超时",
  "tenant.deletion_requested": "组织数据删除已申请",
  "tenant.exported": "组织数据已导出",
  "trial.expired": "试用已到期",
  "usage.exported": "用量已导出",
  "usage.normalized": "用量已归一化",
  "user.invited": "成员已邀请",
};

// Webhook 状态标签统一发送结果和 Endpoint 状态文案，不能暴露接收端响应正文。
const STATUS_LABELS = {
  active: "启用",
  disabled: "停用",
  pending: "发送中",
  success: "成功",
  failed: "失败",
};
const STATUS_LABELS_EN = {
  active: "Active", disabled: "Disabled", pending: "Sending", success: "Success", failed: "Failed",
};

const DEFAULT_EVENT_TYPES = Object.freeze([
  "budget.threshold_reached",
  "budget.exhausted",
  "security.dlp_matched",
  "gateway.request_failed",
]);

/**
  * 渲染 Webhook 端点设置与发送记录面板。
 * 前端只做 admin/developer 的体验层展示；真实角色权限、tenant_id 边界、签名密钥和发送记录都由后端 /webhooks 校验。
 */
export function renderWebhookPanel(webhookState, authContext, i18n) {
  // Webhook 页面只消费后端 webhook.manage，后端仍必须拒绝手写越权请求。
  if (!hasPermission(authContext, PERMISSIONS.WEBHOOK_MANAGE)) {
    return "";
  }
  if (!webhookState) {
    return renderLoadingPanel(i18n);
  }
  // API 错误必须展示 request_id，方便运维把控制台提示和后端 Webhook 日志串起来。
  if (webhookState.state === "error") {
    return renderErrorPanel(webhookState, i18n);
  }

  const payload = webhookState.webhooks ?? webhookState;
  const endpoints = payload.webhooks ?? [];
  const eventTypes = (payload.event_types ?? []).filter((item) => item !== "webhook.test");
  const selectedEndpointId = webhookState.selectedEndpointId ?? endpoints[0]?.webhook_endpoint_id ?? null;
  return `
    <div class="panel-heading webhook-heading">
      <div>
        <h2 id="webhooks-title">${copy(i18n, "Webhook 设置", "Webhook settings")}</h2>
        <p>${copy(i18n, "管理企业回调与 SIEM 安全事件投递；发送记录保留每次重试摘要。", "Manage enterprise callbacks and SIEM security deliveries; each retry keeps a compact delivery summary.")}</p>
      </div>
      <span class="webhook-count">${escapeHtml(endpoints.length)} ${copy(i18n, "个端点", endpoints.length === 1 ? "endpoint" : "endpoints")}</span>
    </div>
    ${renderActionError(webhookState.actionError, i18n)}
    ${renderFeedback(webhookState.feedback, i18n)}
    ${renderCreatedSecret(webhookState.created, i18n)}
    <details class="webhook-create"${endpoints.length ? "" : " open"}>
      <summary>${copy(i18n, "新增端点", "Add endpoint")}</summary>
      ${renderWebhookForm(eventTypes, i18n)}
    </details>
    ${renderEndpointTable(endpoints, selectedEndpointId, i18n)}
      ${renderDeliveryRecords(
      webhookState.deliveryDetails,
      webhookState.deliveryFilters,
      eventTypes,
      endpoints.find((endpoint) => endpoint.webhook_endpoint_id === selectedEndpointId),
      i18n,
    )}
  `;
}

/**
 * 渲染 Webhook API 加载态，避免导航切换时出现空白面板。
 */
function renderLoadingPanel(i18n) {
  return `
    <div class="panel-heading">
      <h2 id="webhooks-title">${copy(i18n, "Webhook 设置", "Webhook settings")}</h2>
      <p>${copy(i18n, "正在读取当前租户的端点和发送记录。", "Loading endpoints and delivery records for this tenant.")}</p>
    </div>
    <div class="empty-state">${copy(i18n, "等待 /webhooks 响应。", "Waiting for the /webhooks response.")}</div>
  `;
}

/**
 * 渲染 Webhook API 错误状态，并保留 request_id 便于定位权限、数据库或投递服务异常。
 */
function renderErrorPanel(state, i18n) {
  return `
    <div class="panel-heading">
      <h2 id="webhooks-title">${copy(i18n, "Webhook 设置", "Webhook settings")}</h2>
      <p>${copy(i18n, "Webhook 暂不可用。", "Webhook is temporarily unavailable.")}</p>
    </div>
    <div class="empty-state error-state">
      ${escapeHtml(state.message ?? copy(i18n, "Webhook 读取失败", "Webhook could not be loaded"))} request_id: ${escapeHtml(state.requestId ?? "-")}
    </div>
  `;
}

/**
  * 渲染创建端点表单。
 * 表单不接收 signing_secret；secret 由后端生成并仅在创建响应中返回一次。
 */
function renderWebhookForm(eventTypes, i18n) {
  const defaultEventCount = eventTypes.filter((eventType) => DEFAULT_EVENT_TYPES.includes(eventType)).length;
  return `
    <form class="webhook-form" id="webhook-form">
      <label>
        <span>${copy(i18n, "名称", "Name")}</span>
        <input name="name" placeholder="${copy(i18n, "例如：运维告警系统", "e.g. Operations alerts")}" required />
      </label>
      <label class="webhook-url-field">
        <span>${copy(i18n, "端点 URL", "Endpoint URL")}</span>
        <input name="endpointUrl" type="url" placeholder="https://ops.example.com/metahub/webhook" required />
      </label>
      <fieldset class="webhook-format-field">
        <legend>${copy(i18n, "投递格式", "Delivery format")}</legend>
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
        <span>${copy(i18n, "安全事件 / 分钟", "Security events / minute")}</span>
        <input name="securityEventRateLimitPerMinute" type="number" min="1" max="600" step="1" value="60" required />
      </label>
      <details class="webhook-event-settings">
        <summary>${copy(i18n, "订阅事件", "Subscribed events")} <span>${defaultEventCount} ${copy(i18n, "个默认事件已选", "default events selected")}</span></summary>
        <fieldset class="webhook-event-field">
          <legend>${copy(i18n, "订阅事件", "Subscribed events")}</legend>
          <label class="webhook-event-search">
            <span>${copy(i18n, "搜索事件", "Search events")}</span>
            <input type="search" data-webhook-event-search autocomplete="off" />
          </label>
          <div class="webhook-event-options">
            ${eventTypes.map((eventType) => renderEventCheckbox(eventType, i18n)).join("")}
            <p data-webhook-event-empty hidden>${copy(i18n, "没有匹配的事件", "No matching events")}</p>
          </div>
        </fieldset>
      </details>
      <label>
        <span>${copy(i18n, "超时 ms", "Timeout (ms)")}</span>
        <input name="timeoutMs" type="number" min="1000" max="30000" step="1000" value="5000" />
      </label>
      <label>
        <span>${copy(i18n, "状态", "Status")}</span>
        <select name="status">
          <option value="active">${copy(i18n, "启用", "Active")}</option>
          <option value="disabled">${copy(i18n, "停用", "Disabled")}</option>
        </select>
      </label>
      <div class="webhook-actions">
        <button type="submit">${copy(i18n, "保存端点", "Save endpoint")}</button>
        <span>${copy(i18n, "创建后只显示一次签名密钥。", "The signing secret is shown only once after creation.")}</span>
      </div>
    </form>
  `;
}

/**
 * 渲染事件类型复选框，默认选中常用治理事件，排除 webhook.test 测试事件。
 */
function renderEventCheckbox(eventType, i18n) {
  const checked = DEFAULT_EVENT_TYPES.includes(eventType)
    ? " checked"
    : "";
  return `
    <label data-webhook-event-option title="${escapeAttribute(eventType)}">
      <input type="checkbox" name="eventTypes" value="${escapeAttribute(eventType)}"${checked} />
      <span>${escapeHtml(eventLabel(eventType, i18n))}</span>
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
function renderCreatedSecret(created, i18n) {
  if (!created?.signingSecret) {
    return "";
  }
  return `
    <div class="webhook-secret-box" role="status">
      <div>
        <strong>${escapeHtml(created.reason === "rotated" ? copy(i18n, "新签名密钥仅显示一次", "The new signing secret is shown once") : copy(i18n, "签名密钥仅显示一次", "The signing secret is shown once"))}</strong>
        <code>${escapeHtml(created.signingSecret)}</code>
      </div>
      <button type="button" data-copy-webhook-secret="${escapeAttribute(created.signingSecret)}">${copy(i18n, "复制", "Copy")}</button>
      <button type="button" data-dismiss-webhook-secret>${copy(i18n, "隐藏", "Hide")}</button>
    </div>
  `;
}

/**
  * 渲染端点表格，展示订阅、密钥指纹和最近投递状态。
 */
function renderEndpointTable(endpoints, selectedEndpointId, i18n) {
  return `
    <div class="webhook-endpoint-table" role="table" aria-label="${escapeAttribute(copy(i18n, "Webhook 端点", "Webhook endpoints"))}">
      <div class="webhook-endpoint-row webhook-endpoint-head" role="row">
        <span role="columnheader">${copy(i18n, "端点", "Endpoint")}</span>
        <span role="columnheader">${copy(i18n, "订阅事件", "Subscribed events")}</span>
        <span role="columnheader">${copy(i18n, "状态", "Status")}</span>
        <span role="columnheader">${copy(i18n, "签名指纹", "Signing fingerprint")}</span>
        <span role="columnheader">${copy(i18n, "最近投递", "Latest delivery")}</span>
        <span role="columnheader">${copy(i18n, "操作", "Actions")}</span>
      </div>
      ${endpoints.length ? endpoints.map((endpoint) => renderEndpointRow(endpoint, selectedEndpointId, i18n)).join("") : renderEmptyEndpointRow(i18n)}
    </div>
  `;
}

/**
  * 渲染单个端点行，并提供测试发送按钮。
 */
function renderEndpointRow(endpoint, selectedEndpointId, i18n) {
  const selected = endpoint.webhook_endpoint_id === selectedEndpointId;
  return `
    <div class="webhook-endpoint-row${selected ? " is-selected" : ""}" role="row">
      <span class="webhook-endpoint-name" role="cell" data-label="${escapeAttribute(copy(i18n, "端点", "Endpoint"))}">
        <strong>${escapeHtml(endpoint.name)}</strong>
        <em>${escapeHtml(endpoint.endpoint_url)}</em>
        <code>${escapeHtml(endpoint.webhook_endpoint_id)}</code>
        ${renderWireMeta(endpoint, i18n)}
      </span>
      <span class="webhook-event-list" role="cell" data-label="${escapeAttribute(copy(i18n, "订阅事件", "Subscribed events"))}">${(endpoint.event_types ?? []).map((eventType) => renderEventPill(eventType, i18n)).join("")}</span>
      <span role="cell" data-label="${escapeAttribute(copy(i18n, "状态", "Status"))}"><mark class="status-${escapeAttribute(endpoint.status)}">${escapeHtml(statusLabel(endpoint.status, i18n))}</mark></span>
      <span class="webhook-secret-meta" role="cell" data-label="${escapeAttribute(copy(i18n, "签名指纹", "Signing fingerprint"))}">
        <code>${escapeHtml(endpoint.secret_prefix ?? "-")}</code>
        <em>${escapeHtml(endpoint.secret_fingerprint ?? "-")}</em>
        <em>${copy(i18n, "版本", "Version")} ${escapeHtml(endpoint.secret_version ?? 1)}</em>
      </span>
      <span role="cell" data-label="${escapeAttribute(copy(i18n, "最近投递", "Latest delivery"))}">
        <mark class="status-${escapeAttribute(endpoint.last_delivery_status ?? "pending")}">
          ${escapeHtml(statusLabel(endpoint.last_delivery_status, i18n, copy(i18n, "暂无", "None")))}
        </mark>
        <em>${escapeHtml(formatDateTime(endpoint.last_delivery_at))}</em>
      </span>
      <span role="cell" data-label="${escapeAttribute(copy(i18n, "操作", "Actions"))}">
        <button type="button" data-webhook-test="${escapeAttribute(endpoint.webhook_endpoint_id)}"${endpoint.status === "disabled" ? ` disabled title="${escapeAttribute(copy(i18n, "端点已停用", "Endpoint is disabled"))}"` : ""}>${copy(i18n, "发送测试", "Send test")}</button>
        <button type="button" data-webhook-details="${escapeAttribute(endpoint.webhook_endpoint_id)}" aria-pressed="${selected}">
          ${copy(i18n, "查看记录", "View records")}
        </button>
        <button type="button" data-webhook-rotate="${escapeAttribute(endpoint.webhook_endpoint_id)}">
          ${copy(i18n, "轮换密钥", "Rotate secret")}
        </button>
      </span>
    </div>
  `;
}

/**
 * 渲染发送记录区域，合并列表接口中的最近记录和刚刚测试的最新结果。
 */
function renderDeliveryRecords(deliveryDetails, filters, eventTypes, endpoint, i18n) {
  if (!endpoint) {
    return "";
  }
  const deliveries = deliveryDetails?.deliveries ?? [];
  // 详情只使用后端按当前筛选返回的数据，避免最近操作结果绕过 event/status 筛选混入列表。
  const unique = dedupeDeliveries(deliveries).slice(0, 100);
  return `
    <section class="webhook-delivery-section" aria-labelledby="webhook-delivery-title">
      <div class="panel-heading compact-heading">
        <h3 id="webhook-delivery-title">${escapeHtml(endpoint.name)} · ${copy(i18n, "最近发送记录", "Recent deliveries")}</h3>
        <p>${copy(i18n, "最近 100 条发送记录", "Latest 100 deliveries")}</p>
      </div>
      ${renderDeliveryFilters(filters, eventTypes, endpoint.webhook_endpoint_id, i18n)}
      <div class="webhook-delivery-table" role="table" aria-label="${escapeAttribute(copy(i18n, "Webhook 发送记录", "Webhook delivery records"))}">
        <div class="webhook-delivery-row webhook-delivery-head" role="row">
          <span role="columnheader">${copy(i18n, "事件", "Event")}</span>
          <span role="columnheader">${copy(i18n, "状态", "Status")}</span>
          <span role="columnheader">${copy(i18n, "尝试", "Attempts")}</span>
          <span role="columnheader">HTTP</span>
          <span role="columnheader">${copy(i18n, "错误", "Error")}</span>
          <span role="columnheader">${copy(i18n, "操作", "Actions")}</span>
        </div>
        ${unique.length ? unique.map((delivery) => renderDeliveryRow(delivery, i18n)).join("") : renderEmptyDeliveryRow(i18n)}
      </div>
    </section>
  `;
}

/**
  * 渲染当前端点的历史筛选工具栏。
  * 筛选只提交 event_type/status，limit 固定为 100，租户与端点归属仍由后端校验。
 */
function renderDeliveryFilters(filters, eventTypes, endpointId, i18n) {
  const activeEventType = filters?.eventType ?? "";
  const activeStatus = filters?.status ?? "all";
  return `
    <form class="webhook-delivery-filters" id="webhook-delivery-filters" data-webhook-endpoint-id="${escapeAttribute(endpointId)}">
      <label>
        <span>${copy(i18n, "事件类型", "Event type")}</span>
        <select name="eventType">
          <option value="">${copy(i18n, "全部事件", "All events")}</option>
          <option value="webhook.test"${activeEventType === "webhook.test" ? " selected" : ""}>${copy(i18n, "Webhook 测试", "Webhook test")}</option>
          ${eventTypes
            .map(
              (eventType) =>
                `<option value="${escapeAttribute(eventType)}"${activeEventType === eventType ? " selected" : ""}>${escapeHtml(eventLabel(eventType, i18n))}</option>`,
            )
            .join("")}
        </select>
      </label>
      <label>
        <span>${copy(i18n, "投递状态", "Delivery status")}</span>
        <select name="status">
          ${[
            ["all", copy(i18n, "全部状态", "All statuses")],
            ["failed", copy(i18n, "失败", "Failed")],
            ["success", copy(i18n, "成功", "Success")],
            ["pending", copy(i18n, "发送中", "Sending")],
          ]
            .map(([value, label]) => `<option value="${value}"${activeStatus === value ? " selected" : ""}>${label}</option>`)
            .join("")}
        </select>
      </label>
      <button type="submit">${copy(i18n, "应用筛选", "Apply filters")}</button>
      <button type="button" data-webhook-refresh-details="${escapeAttribute(endpointId)}">${copy(i18n, "刷新", "Refresh")}</button>
    </form>
  `;
}

/**
 * 渲染单条发送记录和逐次 attempt 摘要。
 */
function renderDeliveryRow(delivery, i18n) {
  return `
    <div class="webhook-delivery-row" role="row">
      <span role="cell" data-label="${escapeAttribute(copy(i18n, "事件", "Event"))}">
        <strong>${escapeHtml(eventLabel(delivery.event_type, i18n))}</strong>
        <em>${escapeHtml(delivery.event_id)}</em>
        <time datetime="${escapeAttribute(delivery.event_timestamp)}">${escapeHtml(formatDateTime(delivery.event_timestamp))}</time>
        ${renderWireMeta(delivery, i18n)}
      </span>
      <span role="cell" data-label="${escapeAttribute(copy(i18n, "状态", "Status"))}"><mark class="status-${escapeAttribute(delivery.status)}">${escapeHtml(statusLabel(delivery.status, i18n))}</mark></span>
      <span role="cell" data-label="${escapeAttribute(copy(i18n, "尝试", "Attempts"))}">${escapeHtml(delivery.attempt_count)} / ${escapeHtml(delivery.max_attempts)}${renderAttempts(delivery.attempts ?? [], i18n)}</span>
      <span role="cell" data-label="HTTP">${escapeHtml(delivery.http_status ?? "-")}</span>
      <span role="cell" data-label="${escapeAttribute(copy(i18n, "错误", "Error"))}">${escapeHtml(delivery.error_code ?? "-")} ${escapeHtml(delivery.error_summary ?? "")}</span>
      <span role="cell" data-label="${escapeAttribute(copy(i18n, "操作", "Actions"))}">
        ${
          delivery.status === "failed"
            ? `<button type="button" data-webhook-replay="${escapeAttribute(delivery.webhook_delivery_id)}" data-webhook-endpoint-id="${escapeAttribute(delivery.webhook_endpoint_id)}">${copy(i18n, "重放", "Replay")}</button>`
            : "-"
        }
        <em>${escapeHtml(delivery.delivery_kind === "replay" ? copy(i18n, "重放", "Replay") : copy(i18n, "原始", "Original"))} · Secret v${escapeHtml(delivery.secret_version ?? 1)}</em>
      </span>
    </div>
  `;
}

/**
 * 渲染 attempt 小列表，只展示安全错误摘要，不展示响应正文或签名头。
 */
function renderAttempts(attempts, i18n) {
  if (!attempts.length) {
    return "";
  }
  return `
    <ol class="webhook-attempt-list">
      ${attempts
        .map(
          (attempt) => `
            <li>
              <mark class="status-${escapeAttribute(attempt.status)}">${escapeHtml(statusLabel(attempt.status, i18n))}</mark>
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
function renderEventPill(eventType, i18n) {
  return `<mark>${escapeHtml(eventLabel(eventType, i18n))}</mark>`;
}

/** 渲染后端冻结的 wire 格式与安全事件上限，不读取 wire body、签名或 rate claim。 */
function renderWireMeta(value, i18n) {
  const format = value?.format === "cef" ? "CEF" : "JSON";
  const limit = Number.isInteger(value?.security_event_rate_limit_per_minute)
    ? value.security_event_rate_limit_per_minute
    : "-";
  return `
    <span class="webhook-wire-meta">
      <mark class="webhook-format-badge">${format}</mark>
      <em>${copy(i18n, "安全事件", "Security events")} ${escapeHtml(limit)} / ${copy(i18n, "分钟", "minute")}</em>
    </span>
  `;
}

/**
  * 渲染空端点行，保持表格结构稳定。
 */
function renderEmptyEndpointRow(i18n) {
  return `
    <div class="webhook-endpoint-row webhook-empty" role="row">
      <span>${copy(i18n, "当前租户尚未创建 Webhook 端点。", "This tenant has no Webhook endpoints yet.")}</span>
    </div>
  `;
}

/**
 * 渲染空发送记录行，避免用户误以为列表接口失败。
 */
function renderEmptyDeliveryRow(i18n) {
  return `
    <div class="webhook-delivery-row webhook-empty" role="row">
      <span>${copy(i18n, "尚无发送记录，可先创建端点并发送测试事件。", "No delivery records yet. Create an endpoint and send a test event first.")}</span>
    </div>
  `;
}

/**
 * 渲染 Webhook 操作错误，错误文本只来自统一错误结构。
 */
function renderActionError(error, i18n) {
  if (!error) {
    return "";
  }
  return `
    <div class="webhook-action-error" role="alert">
      ${escapeHtml(error.message ?? copy(i18n, "Webhook 操作失败", "Webhook action failed"))} request_id: ${escapeHtml(error.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染 Webhook 操作反馈，例如创建成功、复制成功或测试完成。
 */
function renderFeedback(feedback, i18n) {
  if (!feedback) {
    return "";
  }
  const message = typeof feedback === "string" ? feedback : feedback.message;
  const requestId = typeof feedback === "string" ? null : feedback.requestId;
  return `<div class="webhook-feedback" role="status">${escapeHtml(message ?? "")}${
    requestId ? ` request_id: ${escapeHtml(requestId)}` : ""
  }</div>`;
}

function copy(i18n, chinese, english) {
  return i18n?.locale === "en-US" ? english : chinese;
}

function statusLabel(status, i18n, fallback = null) {
  const labels = i18n?.locale === "en-US" ? STATUS_LABELS_EN : STATUS_LABELS;
  return labels[status] ?? fallback ?? status ?? "-";
}

function eventLabel(eventType, i18n) {
  if (i18n?.locale !== "en-US") return EVENT_TYPE_LABELS[eventType] ?? eventType;
  const known = {
    "budget.threshold_reached": "Budget threshold reached", "budget.exhausted": "Budget exhausted",
    "security.dlp_matched": "DLP match", "security.dlp_unavailable": "DLP unavailable",
    "security.event_pushed": "SIEM security event", "gateway.request_failed": "Request failed",
    "webhook.delivery_failed": "Webhook delivery failed", "webhook.replayed": "Webhook replayed",
    "role.permission_changed": "Role permissions changed", "approval.created": "Approval created",
    "approval.timeout": "Approval timed out", "audit.exported": "Audit exported",
    "audit.recorded": "Audit recorded", "billing_dispute.created": "Billing dispute created",
    "cost.anomaly_detected": "Cost anomaly detected", "environment.created": "Environment created",
    "feedback.created": "Feedback submitted", "file.scanned": "File scanned", "file.uploaded": "File uploaded",
    "invitation.accepted": "Invitation accepted", "ledger.entry_created": "Ledger entry created",
    "log.query_executed": "Log query executed", "model.feedback_recorded": "Model feedback recorded",
    "policy.created": "Policy created", "provider.unhealthy": "Provider unhealthy",
    "quota.exhausted": "Quota exhausted", "route.fallback_triggered": "Fallback route triggered",
    "route.policy_selected": "Routing policy selected", "stream.timeout": "Stream timed out",
    "tenant.exported": "Tenant exported", "trial.expired": "Trial expired", "usage.exported": "Usage exported",
    "user.invited": "Member invited",
  };
  return known[eventType] ?? String(eventType ?? "").replaceAll(".", " ").replaceAll("_", " ");
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
