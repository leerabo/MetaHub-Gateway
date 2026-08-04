/**
 * 模块职责：管理 PAGE-083 的安全列表、opaque cursor、重放/归档动作与有界轮询生命周期。
 * 安全边界：响应先经过字段白名单再进入内存；cursor、幂等键、AbortController、轮询 ticket
 * 和操作 request/trace 均不进入渲染快照，也不写 URL、Storage、日志或 DOM。
 */

import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";

// 默认筛选定义页面初始查询身份，不包含 tenant_id 或任意可伪造操作者字段。
const DEFAULT_FILTERS = Object.freeze({
  status: "all",
  eventType: "",
  webhookEndpointId: "",
  limit: 50,
});
// 查询状态白名单必须与后端筛选枚举一致，未知值在发起网络请求前失败关闭。
const STATUS_VALUES = new Set([
  "all",
  "dead_letter",
  "replay_pending",
  "replayed",
  "archived",
  "expired",
]);
// 响应状态白名单决定动作是否可见，合同外状态只能显示且不得获得治理命令。
const DEAD_LETTER_EVENT_STATUSES = new Set([
  "dead_letter",
  "replay_pending",
  "replayed",
  "archived",
  "expired",
]);
// 动作集合只用于前端零预取和交互收敛，最终权限仍由后端独立校验。
const ACTION_VALUES = new Set(["replay", "archive"]);

/**
 * 创建独立的 Webhook 死信页面状态。
 *
 * @param {object} [timerOptions] 可注入计时器，仅用于无真实等待的单元测试。
 * @returns {object} PAGE-083 状态机；调用方只能通过公开方法改变状态。
 */
export function createWebhookDeadLetterPageState(timerOptions = {}) {
  const setTimer = timerOptions.setTimer ?? globalThis.setTimeout?.bind(globalThis);
  const clearTimer = timerOptions.clearTimer ?? globalThis.clearTimeout?.bind(globalThis);
  let filters = { ...DEFAULT_FILTERS };
  let cursor = null;
  let cursorStack = [];
  let page = null;
  let listError = null;
  let feedback = null;
  let archiveDialogId = null;
  let busyEventId = null;
  let busyAction = null;

  // 结果未知的 replay 必须复用同一键；Map 的值永远不会通过 snapshot 暴露。
  const replayKeys = new Map();
  let listRevision = 0;
  let listOperation = null;
  let actionRevision = 0;
  let actionOperation = null;
  let pollRevision = 0;
  let pollTimer = null;
  let pollAttempts = 0;

  /**
   * 销毁当前身份的全部页面事实。
   * revision 先推进、再 abort/clear timer，确保忽略 AbortSignal 的迟到 Promise 和计时器也失权。
   */
  function clear() {
    listRevision += 1;
    actionRevision += 1;
    pollRevision += 1;
    listOperation?.controller.abort();
    actionOperation?.controller.abort();
    listOperation = null;
    actionOperation = null;
    if (pollTimer !== null && clearTimer) {
      clearTimer(pollTimer);
    }
    pollTimer = null;
    pollAttempts = 0;
    filters = { ...DEFAULT_FILTERS };
    cursor = null;
    cursorStack = [];
    page = null;
    listError = null;
    feedback = null;
    archiveDialogId = null;
    busyEventId = null;
    busyAction = null;
    replayKeys.clear();
  }

  /** 返回 GET 允许的固定筛选和闭包 cursor，不添加 tenant、actor 或排序字段。 */
  function query() {
    return { ...filters, cursor };
  }

  /**
   * 应用新筛选并回到第一页。
   * 旧 cursor、反馈、确认框和轮询都绑定旧快照，必须同步失效。
   */
  function setFilters(values = {}) {
    abortListOperation();
    cancelPoll();
    filters = normalizeFilters(values);
    cursor = null;
    cursorStack = [];
    page = null;
    listError = null;
    feedback = null;
    archiveDialogId = null;
  }

  /**
   * 接受后端列表并逐字段净化，任何额外 payload/URL/lease 字段都不会进入页面内存。
   * filters 必须采用服务端规范化回显；菜单只从当前页安全摘要派生，不能依赖合同外目录字段。
   */
  function acceptPage(payload) {
    const events = Array.isArray(payload?.dead_letters)
      ? payload.dead_letters.map(sanitizeDeadLetterEvent).filter(Boolean)
      : [];
    const nextCursor = safeText(payload?.next_cursor, 4096) || null;
    filters = normalizeResponseFilters(payload?.filters);
    page = {
      deadLetterEvents: events,
      eventTypes: deriveEventTypes(events),
      webhookEndpoints: deriveWebhookEndpoints(events),
      hasMore: Boolean(nextCursor),
      nextCursor,
      requestId: safeText(payload?.request_id, 160) || "-",
      traceId: safeText(payload?.trace_id, 160) || "-",
    };
    listError = null;
    // 服务端新快照不再包含确认对象时，关闭确认框，防止对页外 ID 继续归档。
    if (archiveDialogId && !findEvent(archiveDialogId)) {
      archiveDialogId = null;
    }
    if (!events.some((event) => event.status === "replay_pending")) {
      cancelPoll();
    }
    return snapshot();
  }

  /** 保存公开列表错误；已有安全快照保留为 partial，避免瞬时网络错误清空治理证据。 */
  function rejectList(error) {
    listError = publicError(error, "webhook.dead_letter_unavailable", "Webhook 死信暂不可用");
    return snapshot();
  }

  /** 进入后端签发的下一页；cursor 只进入闭包栈，不进入 DOM。 */
  function nextPage() {
    if (!page?.hasMore || !page.nextCursor) {
      return false;
    }
    abortListOperation();
    cancelPoll();
    cursorStack = [...cursorStack, cursor];
    cursor = page.nextCursor;
    page = null;
    listError = null;
    archiveDialogId = null;
    return true;
  }

  /** 返回闭包 cursor 栈中的上一页；第一页由 null 表示。 */
  function previousPage() {
    if (!cursorStack.length) {
      return false;
    }
    abortListOperation();
    cancelPoll();
    cursor = cursorStack[cursorStack.length - 1] ?? null;
    cursorStack = cursorStack.slice(0, -1);
    page = null;
    listError = null;
    archiveDialogId = null;
    return true;
  }

  /**
   * 为当前页可重放对象冻结来源 delivery 和幂等键。
   * DOM 注入的 ID、缺少后端 allowed_actions 或非 dead_letter 状态都会失败关闭。
   */
  function prepareReplay(deadLetterEventId, keyFactory) {
    const event = findEvent(deadLetterEventId);
    if (!event || event.status !== "dead_letter" || !event.allowed_actions.includes("replay")) {
      feedback = {
        kind: "error",
        code: "webhook.dead_letter_not_replayable",
        message: "当前死信不可重放",
        requestId: page?.requestId ?? "-",
      };
      return null;
    }
    const deliveryId = event.webhook_delivery_id;
    if (!replayKeys.has(deliveryId)) {
      replayKeys.set(deliveryId, keyFactory());
    }
    return {
      deadLetterEventId: event.dead_letter_event_id,
      webhookDeliveryId: deliveryId,
      idempotencyKey: replayKeys.get(deliveryId),
    };
  }

  /** 确定重放成功或确定性失败后删除旧键；网络不确定时调用方不得调用本函数。 */
  function releaseReplayKey(webhookDeliveryId) {
    replayKeys.delete(String(webhookDeliveryId ?? ""));
  }

  /** 打开当前页可归档对象的确认框；后端 allowed_actions 是前端命令可见性的唯一来源。 */
  function openArchive(deadLetterEventId) {
    const event = findEvent(deadLetterEventId);
    if (!event || !event.allowed_actions.includes("archive")) {
      feedback = {
        kind: "error",
        code: "webhook.dead_letter_not_archivable",
        message: "当前死信不可归档",
        requestId: page?.requestId ?? "-",
      };
      return false;
    }
    archiveDialogId = event.dead_letter_event_id;
    feedback = null;
    return true;
  }

  /** 关闭归档确认框；调用方在局部重绘后负责把焦点恢复到原命令按钮。 */
  function closeArchive() {
    const closedId = archiveDialogId;
    archiveDialogId = null;
    return closedId;
  }

  /** 从内存确认框恢复归档 ID，不接受表单或 data 属性覆盖。 */
  function archiveIntent() {
    const event = archiveDialogId ? findEvent(archiveDialogId) : null;
    return event?.allowed_actions.includes("archive")
      ? { deadLetterEventId: event.dead_letter_event_id }
      : null;
  }

  /**
   * 开始列表读取，冻结身份、导航和刷新代次并取消上一轮 GET。
   * 不完整 owner 失败关闭，因而其他导航和 viewer 不会创建 AbortController 或网络请求。
   */
  function beginListOperation(owner) {
    const normalizedOwner = normalizeListOwner(owner);
    if (!normalizedOwner) {
      return null;
    }
    abortListOperation();
    listRevision += 1;
    listOperation = Object.freeze({
      revision: listRevision,
      controller: new AbortController(),
      ...normalizedOwner,
    });
    return listOperation;
  }

  /** 严格比较 GET 的对象引用、revision、身份、导航和刷新代次，拒绝迟到成功与迟到错误。 */
  function isListOperationCurrent(operation, owner) {
    const current = normalizeListOwner(owner);
    return Boolean(
      operation
      && current
      && listOperation === operation
      && operation.revision === listRevision
      && operation.identity === current.identity
      && operation.navigation === current.navigation
      && operation.refreshGeneration === current.refreshGeneration,
    );
  }

  /** 只结束调用方自己的 GET，旧 finally 不能释放新读取的 controller。 */
  function finishListOperation(operation) {
    if (listOperation === operation) {
      listOperation = null;
    }
  }

  /**
   * 开始 replay/archive 写操作并冻结 owner。
   * 同一页面只允许一个动作在途，避免双击制造不同业务意图；后端幂等和状态机仍是最终边界。
   */
  function beginActionOperation(owner, { action, deadLetterEventId }) {
    const normalizedOwner = normalizeActionOwner(owner);
    const event = findEvent(deadLetterEventId);
    if (
      !normalizedOwner
      || !event
      || !ACTION_VALUES.has(action)
      || !event.allowed_actions.includes(action)
      || actionOperation
    ) {
      return null;
    }
    actionRevision += 1;
    busyEventId = event.dead_letter_event_id;
    busyAction = action;
    if (action === "archive") {
      // 归档提交后立即关闭确认框，防止网络在途时同一表单产生第二次 submit。
      archiveDialogId = null;
    }
    actionOperation = Object.freeze({
      revision: actionRevision,
      controller: new AbortController(),
      action,
      deadLetterEventId: event.dead_letter_event_id,
      ...normalizedOwner,
    });
    return actionOperation;
  }

  /** 严格比较写操作 owner；身份或导航变化、clear 或后发动作都会让旧回调失权。 */
  function isActionOperationCurrent(operation, owner) {
    const current = normalizeActionOwner(owner);
    return Boolean(
      operation
      && current
      && actionOperation === operation
      && operation.revision === actionRevision
      && operation.identity === current.identity
      && operation.navigation === current.navigation,
    );
  }

  /**
   * 接受安全动作响应、替换当前页同 ID 项并生成 request_id 反馈。
   * 响应中额外 payload/lease 字段会在 sanitizeDeadLetterEvent 中被丢弃。
   */
  function acceptAction(result, action) {
    const event = sanitizeDeadLetterEvent(result?.dead_letter);
    if (event && page?.deadLetterEvents.some((item) => item.dead_letter_event_id === event.dead_letter_event_id)) {
      const events = page.deadLetterEvents.map((item) => (
        item.dead_letter_event_id === event.dead_letter_event_id ? event : item
      ));
      page = {
        ...page,
        deadLetterEvents: events,
        eventTypes: deriveEventTypes(events),
        webhookEndpoints: deriveWebhookEndpoints(events),
        requestId: safeText(result?.request_id, 160) || page.requestId,
        traceId: safeText(result?.trace_id, 160) || page.traceId,
      };
    }
    if (action === "replay" && event?.webhook_delivery_id) {
      releaseReplayKey(event.webhook_delivery_id);
      cancelPoll();
    }
    archiveDialogId = null;
    feedback = {
      kind: "success",
      code: "",
      message: action === "archive"
        ? "死信已归档"
        : (event?.status === "replayed" ? "重放已完成" : "重放意图已提交"),
      requestId: safeText(result?.request_id, 160) || "-",
    };
    return snapshot();
  }

  /** 保存公开动作错误；错误对象不保留响应正文、请求 body 或幂等键。 */
  function rejectAction(error) {
    const archiveFailure = busyAction === "archive";
    feedback = {
      kind: "error",
      ...publicError(
        error,
        archiveFailure ? "webhook.dead_letter_unavailable" : "webhook.replay_unavailable",
        archiveFailure ? "Webhook 死信归档失败" : "Webhook 死信重放失败",
      ),
    };
    return snapshot();
  }

  /** 只结束调用方自己的动作；旧 finally 不能覆盖新动作 busy 状态。 */
  function finishActionOperation(operation) {
    if (actionOperation === operation) {
      actionOperation = null;
      busyEventId = null;
      busyAction = null;
    }
  }

  /** 返回是否存在未完成治理写入，供全页定时刷新避免重建结果未知的动作。 */
  function isActionBusy() {
    return Boolean(actionOperation);
  }

  /** 返回当前安全快照中是否含 replay_pending，用于决定是否启动短轮询。 */
  function hasReplayPending() {
    return Boolean(page?.deadLetterEvents.some((event) => event.status === "replay_pending"));
  }

  /**
   * 安排一次有界轮询回调。
   * 每个 pending 周期最多 maxAttempts 次；同一时刻只有一个 timer，clear/离页会推进 revision 使回调失权。
   */
  function schedulePoll(callback, { delayMs = 1500, maxAttempts = 8 } = {}) {
    if (!hasReplayPending() || pollTimer !== null || pollAttempts >= maxAttempts || !setTimer) {
      return false;
    }
    const revision = pollRevision;
    pollAttempts += 1;
    pollTimer = setTimer(() => {
      pollTimer = null;
      if (revision !== pollRevision) {
        return;
      }
      callback({ revision, attempt: pollAttempts });
    }, delayMs);
    return true;
  }

  /** 取消轮询并恢复预算；筛选、离页、身份变化和终态响应都必须调用。 */
  function cancelPoll() {
    pollRevision += 1;
    if (pollTimer !== null && clearTimer) {
      clearTimer(pollTimer);
    }
    pollTimer = null;
    pollAttempts = 0;
  }

  /**
   * 构造只含可展示安全字段的快照。
   * cursor、next_cursor、幂等键、controller、owner、poll ticket 和 action request/trace 均被排除。
   */
  function snapshot() {
    const events = page?.deadLetterEvents ?? [];
    const archiveEvent = archiveDialogId ? findEvent(archiveDialogId) : null;
    return {
      state: page ? (listError ? "partial" : "ok") : (listError ? "error" : "loading"),
      filters: { ...filters },
      dead_letter_events: events.map(cloneDeadLetterEvent),
      event_types: [...(page?.eventTypes ?? [])],
      webhook_endpoints: (page?.webhookEndpoints ?? []).map((endpoint) => ({ ...endpoint })),
      has_more: Boolean(page?.hasMore),
      canPrevious: cursorStack.length > 0,
      canNext: Boolean(page?.hasMore && page.nextCursor),
      request_id: page?.requestId ?? listError?.requestId ?? "-",
      trace_id: page?.traceId ?? "-",
      error: listError ? { ...listError } : null,
      feedback: feedback ? { ...feedback } : null,
      archiveDialog: archiveEvent ? { event: cloneDeadLetterEvent(archiveEvent) } : null,
      busyEventId,
      busyAction,
      pollExhausted: hasReplayPending() && pollAttempts >= 8,
    };
  }

  /** 在当前 tenant-qualified 页内按死信 ID 查找对象；绝不访问页外缓存。 */
  function findEvent(deadLetterEventId) {
    const normalized = String(deadLetterEventId ?? "");
    return page?.deadLetterEvents.find((event) => event.dead_letter_event_id === normalized) ?? null;
  }

  /** 取消列表读取并推进 revision；写操作使用独立 owner，不受普通列表刷新接管。 */
  function abortListOperation() {
    listOperation?.controller.abort();
    listOperation = null;
    listRevision += 1;
  }

  return {
    clear,
    query,
    setFilters,
    acceptPage,
    rejectList,
    nextPage,
    previousPage,
    prepareReplay,
    releaseReplayKey,
    openArchive,
    closeArchive,
    archiveIntent,
    beginListOperation,
    isListOperationCurrent,
    finishListOperation,
    beginActionOperation,
    isActionOperationCurrent,
    acceptAction,
    rejectAction,
    finishActionOperation,
    isActionBusy,
    hasReplayPending,
    schedulePoll,
    cancelPoll,
    snapshot,
  };
}

/**
 * 从认证上下文构造 admin/developer 身份 owner。
 * tenant、user、role、session_id、session_revision 全部参与序列化，任一变化都会淘汰旧响应。
 */
export function webhookDeadLetterIdentitySnapshot(authContext) {
  // owner 绑定 webhook.manage 与矩阵版本，后端仍必须独立执行权限和 tenant 校验。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.WEBHOOK_MANAGE);
}

/** 判断异常是否来自页面主动 Abort；只有该类错误可静默丢弃。 */
export function isWebhookDeadLetterAbortError(error) {
  return error?.name === "AbortError";
}

/** 规范化筛选白名单；未知状态收敛为 all，过长自由文本不进入请求。 */
function normalizeFilters(values) {
  const status = safeText(values?.status, 40);
  const limit = Number.isInteger(values?.limit) && values.limit >= 1 && values.limit <= 100
    ? values.limit
    : DEFAULT_FILTERS.limit;
  return {
    status: STATUS_VALUES.has(status) ? status : "all",
    eventType: safeText(values?.eventType, 160),
    webhookEndpointId: safeText(values?.webhookEndpointId, 160),
    limit,
  };
}

/**
 * 把服务端 filters 回显映射回页面查询模型。
 * 服务端是租户隔离和参数规范化的最终边界，因此翻页必须沿用其回显，而不是继续信任原始表单值。
 */
function normalizeResponseFilters(values) {
  return normalizeFilters({
    status: values?.status,
    eventType: values?.event_type,
    webhookEndpointId: values?.webhook_endpoint_id,
    limit: values?.limit,
  });
}

/** 规范化列表 owner；refreshGeneration 阻止旧列表覆盖同身份的较新刷新。 */
function normalizeListOwner(owner) {
  const actionOwner = normalizeActionOwner(owner);
  if (!actionOwner || !Number.isSafeInteger(owner?.refreshGeneration)) {
    return null;
  }
  return { ...actionOwner, refreshGeneration: owner.refreshGeneration };
}

/** 规范化动作 owner；只有完整 manager 身份且仍在 PAGE-083 时才允许网络回调。 */
function normalizeActionOwner(owner) {
  const identity = String(owner?.identity ?? "");
  const navigation = String(owner?.navigation ?? "");
  return identity && navigation === "webhook-dead-letters" ? { identity, navigation } : null;
}

/**
 * 从后端 DTO 复制可展示白名单。
 * 该函数故意没有 payload、URL、tenant_id、幂等键、签名、Secret、claim 或 lease 字段。
 */
function sanitizeDeadLetterEvent(value) {
  if (!value || typeof value !== "object") {
    return null;
  }
  const deadLetterEventId = safeText(value.dead_letter_event_id, 160);
  const webhookDeliveryId = safeText(value.webhook_delivery_id, 160);
  if (!deadLetterEventId || !webhookDeliveryId) {
    return null;
  }
  return {
    dead_letter_event_id: deadLetterEventId,
    webhook_delivery_id: webhookDeliveryId,
    webhook_endpoint_id: safeText(value.webhook_endpoint_id, 160),
    webhook_endpoint_name: safeText(value.webhook_endpoint_name ?? value.endpoint_name, 160),
    event_id: safeText(value.event_id, 160),
    event_type: safeText(value.event_type, 160),
    status: DEAD_LETTER_EVENT_STATUSES.has(String(value.status ?? ""))
      ? String(value.status)
      : "unknown",
    failure_error_code: safeText(value.failure_error_code, 160),
    failure_http_status: safeInteger(value.failure_http_status),
    failed_at: safeText(value.failed_at, 80),
    expires_at: safeText(value.expires_at, 80),
    current_replay_delivery_id: safeText(value.current_replay_delivery_id, 160),
    replay_requested_at: safeText(value.replay_requested_at, 80),
    replay_dispatch_attempts: safeInteger(value.replay_dispatch_attempts) ?? 0,
    last_replay_error_code: safeText(value.last_replay_error_code, 160),
    replayed_at: safeText(value.replayed_at, 80),
    archived_at: safeText(value.archived_at, 80),
    attempt_count: safeInteger(value.latest_attempt?.attempt_no) ?? 0,
    latest_attempt: sanitizeLatestAttempt(value.latest_attempt),
    replayable: Boolean(value.replayable),
    allowed_actions: DEAD_LETTER_EVENT_STATUSES.has(String(value.status ?? ""))
      && Array.isArray(value.allowed_actions)
      ? value.allowed_actions.filter((action) => ACTION_VALUES.has(action))
      : [],
  };
}

/** 只复制后端 attempt_no/created_at 固定摘要，不保留接收端正文、error_summary 或原始异常。 */
function sanitizeLatestAttempt(value) {
  if (!value || typeof value !== "object") {
    return null;
  }
  return {
    attempt_no: safeInteger(value.attempt_no) ?? 0,
    status: safeText(value.status, 40),
    http_status: safeInteger(value.http_status),
    error_code: safeText(value.error_code, 160),
    created_at: safeText(value.created_at, 80),
  };
}

/** 复制已净化事件，避免渲染调用方改写状态机内部对象。 */
function cloneDeadLetterEvent(event) {
  return {
    ...event,
    allowed_actions: [...event.allowed_actions],
    latest_attempt: event.latest_attempt ? { ...event.latest_attempt } : null,
  };
}

/** 从已净化的当前页记录派生事件类型菜单；不接受响应中的合同外聚合字段。 */
function deriveEventTypes(events) {
  return [...new Set(events.map((event) => event.event_type).filter(Boolean))];
}

/**
 * 从已净化的当前页记录派生 Endpoint 菜单并按 ID 去重。
 * 名称缺失时回退到不透明 ID，避免额外请求 Endpoint 目录造成越权预取或跨页状态残留。
 */
function deriveWebhookEndpoints(events) {
  const endpoints = new Map();
  for (const event of events) {
    if (event.webhook_endpoint_id && !endpoints.has(event.webhook_endpoint_id)) {
      endpoints.set(event.webhook_endpoint_id, {
        webhook_endpoint_id: event.webhook_endpoint_id,
        name: event.webhook_endpoint_name || event.webhook_endpoint_id,
      });
    }
  }
  return [...endpoints.values()];
}

/** 把统一错误裁剪成公开 code/message/request_id，不保留 diagnosis 或任意响应正文。 */
function publicError(error, fallbackCode, fallbackMessage) {
  const hasPublicContract = Boolean(safeText(error?.code, 160) && safeText(error?.requestId, 160));
  return {
    code: safeText(error?.code, 160) || fallbackCode,
    message: hasPublicContract ? (safeText(error?.message, 500) || fallbackMessage) : fallbackMessage,
    requestId: safeText(error?.requestId, 160) || "-",
  };
}

/** 把身份标量转换成有界字符串；Session revision 允许后端返回整数或字符串。 */
function identityText(value, maxLength = 160) {
  return typeof value === "string" || typeof value === "number"
    ? String(value).trim().slice(0, maxLength)
    : "";
}
/** 把未知值约束为有界字符串，防止大对象或正文意外进入页面状态。 */
function safeText(value, maxLength) {
  return typeof value === "string" ? value.trim().slice(0, maxLength) : "";
}

/** 只接受 JavaScript 安全整数；HTTP 状态和 attempt 数不允许隐式字符串转换。 */
function safeInteger(value) {
  return Number.isSafeInteger(value) ? value : null;
}
