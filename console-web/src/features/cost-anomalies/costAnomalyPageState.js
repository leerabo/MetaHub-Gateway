/**
 * 模块职责：管理 PAGE-077/078 的筛选、opaque cursor、详情、动作草稿和异步请求 owner。
 * 安全边界：cursor、幂等键和 operation request/trace 只保存在闭包内存，不写 URL、DOM、
 * localStorage 或日志；离页、登出、换租户、换用户、Session 变化或降权必须调用 clear()。
 */

import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";

// 页面默认只读取有界数量；调用方额外传入 tenant、actor 或排序字段会被规范化白名单丢弃。
const DEFAULT_FILTERS = Object.freeze({ limit: 20 });
const STATUS_VALUES = new Set(["open", "acknowledged", "dismissed", "mitigated"]);
const RULE_TYPE_VALUES = new Set([
  "tenant_spend_spike",
  "virtual_key_spend_spike",
  "virtual_key_off_hours",
]);
const SEVERITY_VALUES = new Set(["warning", "critical"]);
const ACTION_VALUES = new Set([
  "acknowledge",
  "dismiss",
  "apply_temporary_rate_limit",
  "release_temporary_rate_limit",
]);

/**
 * 创建成本异常页面内存与双请求生命周期。
 *
 * @returns {object} 只能通过方法访问的页面状态；snapshot 故意排除 cursor 和幂等信息。
 */
export function createCostAnomalyPageState() {
  let filters = { ...DEFAULT_FILTERS };
  let cursor = null;
  let cursorStack = [];
  let page = null;
  let selectedAnomalyId = null;
  let actionDialog = null;
  let actionError = null;
  let feedback = null;

  // GET 与 PATCH 使用独立 controller；列表刷新不能取消结果未知的治理写入。
  let listRevision = 0;
  let listOperation = null;
  let actionRevision = 0;
  let actionOperation = null;

  /**
   * 销毁当前身份下的全部业务事实和异步 owner。
   * 先递增 revision 再 abort，使忽略 AbortSignal 的迟到 Promise 也无法通过 isCurrent 门禁。
   */
  function clear() {
    invalidateOperations();
    filters = { ...DEFAULT_FILTERS };
    cursor = null;
    cursorStack = [];
    page = null;
    selectedAnomalyId = null;
    actionDialog = null;
    actionError = null;
    feedback = null;
  }

  /** 返回下一次 GET 使用的筛选白名单和当前内存 cursor，不包含任何权限字段。 */
  function query() {
    return { ...filters, cursor };
  }

  /**
   * 应用新筛选并回到第一页。
   * cursor、详情和动作草稿都绑定旧快照，必须同时失效，防止对已离开筛选的异常执行动作。
   */
  function setFilters(nextFilters) {
    abortListOperation();
    filters = normalizeFilters(nextFilters);
    cursor = null;
    cursorStack = [];
    page = null;
    selectedAnomalyId = null;
    actionDialog = null;
    actionError = null;
    feedback = null;
  }

  /** 清空所有筛选并复用相同的快照失效规则。 */
  function resetFilters() {
    setFilters(DEFAULT_FILTERS);
  }

  /**
   * 接受一次已通过后端 admin、tenant 和 cursor 校验的列表页。
   * next_cursor 只存入闭包内部；渲染快照仅暴露 canNext，不包含 cursor 原文。
   */
  function acceptPage(payload) {
    const anomalies = Array.isArray(payload?.cost_anomalies) ? payload.cost_anomalies : [];
    page = {
      cost_anomalies: anomalies,
      summary: payload?.summary ?? {},
      total: Number.isSafeInteger(payload?.total) ? payload.total : 0,
      limit: Number.isSafeInteger(payload?.limit) ? payload.limit : filters.limit,
      hasMore: Boolean(payload?.has_more),
      nextCursor: typeof payload?.next_cursor === "string" && payload.next_cursor
        ? payload.next_cursor
        : null,
      requestId: payload?.request_id ?? "-",
      traceId: payload?.trace_id ?? "-",
    };
    if (selectedAnomalyId && !findCurrentAnomaly(selectedAnomalyId)) {
      // 新快照不含旧详情时必须清除动作草稿，不能让过期 ID 继续提交 PATCH。
      selectedAnomalyId = null;
      actionDialog = null;
      actionError = {
        code: "cost_anomaly.not_found",
        message: "原成本异常已不在当前快照页，请重新选择",
        requestId: page.requestId,
      };
    }
    return snapshot();
  }

  /** 进入后端签发的下一页；没有有效 next cursor 时保持原状态。 */
  function nextPage() {
    if (!page?.hasMore || !page.nextCursor) {
      return false;
    }
    abortListOperation();
    cursorStack = [...cursorStack, cursor];
    cursor = page.nextCursor;
    page = null;
    selectedAnomalyId = null;
    actionDialog = null;
    actionError = null;
    return true;
  }

  /** 从内存栈回到上一页；栈中的 null 代表无 cursor 的第一页。 */
  function previousPage() {
    if (!cursorStack.length) {
      return false;
    }
    abortListOperation();
    cursor = cursorStack[cursorStack.length - 1] ?? null;
    cursorStack = cursorStack.slice(0, -1);
    page = null;
    selectedAnomalyId = null;
    actionDialog = null;
    actionError = null;
    return true;
  }

  /**
   * 选择当前页异常。
   * DOM 手工注入的 ID 若不属于当前 tenant-qualified 响应页会被拒绝，不能直接进入 PATCH 路径。
   */
  function selectAnomaly(anomalyId) {
    const normalized = String(anomalyId ?? "");
    if (!findCurrentAnomaly(normalized)) {
      actionError = {
        code: "cost_anomaly.not_found",
        message: "当前页找不到该成本异常",
        requestId: page?.requestId ?? "-",
      };
      return false;
    }
    selectedAnomalyId = normalized;
    actionDialog = null;
    actionError = null;
    return true;
  }

  /** 关闭详情时一并销毁未提交动作草稿，避免隐藏 DOM 中残留限流值。 */
  function closeDetail() {
    selectedAnomalyId = null;
    actionDialog = null;
  }

  /**
   * 为当前页异常打开动作表单，并冻结本次意图的幂等键和 operation IDs。
   * 这些标识只驻留闭包，snapshot 仅暴露可编辑业务值和提交状态。
   */
  function openAction({ anomalyId, action, idempotencyKey, operationRequestId, operationTraceId }) {
    const normalizedId = String(anomalyId ?? "");
    const normalizedAction = String(action ?? "");
    if (!findCurrentAnomaly(normalizedId) || !ACTION_VALUES.has(normalizedAction)) {
      actionError = {
        code: "cost_anomaly.action_invalid",
        message: "当前异常不支持该操作",
        requestId: page?.requestId ?? "-",
      };
      return false;
    }
    selectedAnomalyId = normalizedId;
    actionDialog = {
      open: true,
      anomalyId: normalizedId,
      action: normalizedAction,
      dismissReason: normalizedAction === "dismiss" ? "false_positive" : "",
      durationMinutes: normalizedAction === "apply_temporary_rate_limit" ? "30" : "",
      rpmLimit: normalizedAction === "apply_temporary_rate_limit" ? "10" : "",
      tpmLimit: "",
      concurrencyLimit: normalizedAction === "apply_temporary_rate_limit" ? "2" : "",
      submitting: false,
      error: null,
      idempotencyKey,
      operationRequestId,
      operationTraceId,
    };
    actionError = null;
    feedback = null;
    return true;
  }

  /**
   * 冻结一次提交草稿并返回 API 白名单。
   * 双击或无对话框时返回 null；幂等键不进入渲染快照，只在该返回对象中短暂交给 API client。
   */
  function beginAction(values) {
    if (!actionDialog?.open || actionDialog.submitting) {
      return null;
    }
    actionDialog = {
      ...actionDialog,
      dismissReason: String(values?.dismissReason ?? actionDialog.dismissReason ?? ""),
      durationMinutes: values?.durationMinutes ?? actionDialog.durationMinutes,
      rpmLimit: values?.rpmLimit ?? actionDialog.rpmLimit,
      tpmLimit: values?.tpmLimit ?? actionDialog.tpmLimit,
      concurrencyLimit: values?.concurrencyLimit ?? actionDialog.concurrencyLimit,
      submitting: true,
      error: null,
    };
    return actionPayload();
  }

  /** 返回当前动作所需的精确客户端参数，不添加 tenant、actor、status 或 expires_at。 */
  function actionPayload() {
    if (!actionDialog?.open) {
      return null;
    }
    return {
      anomalyId: actionDialog.anomalyId,
      action: actionDialog.action,
      dismissReason: actionDialog.dismissReason,
      durationMinutes: actionDialog.durationMinutes,
      rpmLimit: actionDialog.rpmLimit,
      tpmLimit: actionDialog.tpmLimit,
      concurrencyLimit: actionDialog.concurrencyLimit,
      idempotencyKey: actionDialog.idempotencyKey,
      operationRequestId: actionDialog.operationRequestId,
      operationTraceId: actionDialog.operationTraceId,
    };
  }

  /**
   * 接受成功或幂等恢复结果。
   * 先销毁动作草稿和键，再以响应中的当前异常替换本页同 ID 项；最终汇总仍由调用方重新 GET。
   */
  function acceptAction(result) {
    const updated = result?.cost_anomaly;
    // 只替换当前 tenant-qualified 页中同 ID 的安全 DTO，页外响应不能扩充本地作用域。
    if (updated?.cost_anomaly_id && page?.cost_anomalies) {
      page = {
        ...page,
        cost_anomalies: page.cost_anomalies.map((item) => (
          item?.cost_anomaly_id === updated.cost_anomaly_id ? updated : item
        )),
      };
      selectedAnomalyId = updated.cost_anomaly_id;
    }
    actionDialog = null;
    actionError = null;
    feedback = {
      message: result?.idempotent_replay ? "治理动作已确认" : "治理动作已提交",
      idempotentReplay: Boolean(result?.idempotent_replay),
      requestId: result?.request_id ?? "-",
    };
    return snapshot();
  }

  /**
   * 保存公开统一错误并恢复提交按钮。
   * uncertain=true 保留原键；明确 4xx 使用调用方提供的新随机标识，让用户修正参数后形成新意图。
   */
  function rejectAction(error, { retainIdempotencyKey, replacementIds } = {}) {
    if (!actionDialog?.open) {
      return;
    }
    actionDialog = {
      ...actionDialog,
      submitting: false,
      error: {
        code: error?.code ?? "cost_anomaly.unavailable",
        message: error?.message ?? "成本异常动作提交失败",
        requestId: error?.requestId ?? "-",
      },
      ...(retainIdempotencyKey
        ? {}
        : {
            idempotencyKey: replacementIds?.idempotencyKey,
            operationRequestId: replacementIds?.operationRequestId,
            operationTraceId: replacementIds?.operationTraceId,
          }),
    };
  }

  /** 关闭动作表单并立即丢弃限流草稿、原因、幂等键和 operation IDs。 */
  function closeAction() {
    actionDialog = null;
  }

  /**
   * 开始一次 GET，先取消上一轮列表读取并冻结完整 owner。
   * 不完整身份、非目标导航或非法刷新代次均失败关闭，不会创建网络 controller。
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

  /** 核对 GET 的对象引用、revision、身份、导航和刷新代次，拒绝迟到响应写入新会话。 */
  function isListOperationCurrent(operation, owner) {
    const currentOwner = normalizeListOwner(owner);
    return Boolean(
      operation
      && currentOwner
      && listOperation === operation
      && operation.revision === listRevision
      && operation.identity === currentOwner.identity
      && operation.navigation === currentOwner.navigation
      && operation.refreshGeneration === currentOwner.refreshGeneration,
    );
  }

  /** 只结束调用方自己的 GET；旧 finally 不能清掉后来刷新创建的新 controller。 */
  function finishListOperation(operation) {
    if (listOperation === operation) {
      listOperation = null;
    }
  }

  /**
   * 开始一次 PATCH；写操作 owner 只绑定身份和导航，不绑定定时刷新代次。
   * 这样同页后台 GET 可以刷新可见证据，却不能取消或接管结果未知的治理写入。
   */
  function beginActionOperation(owner) {
    const normalizedOwner = normalizeActionOwner(owner);
    if (!normalizedOwner) {
      return null;
    }
    actionOperation?.controller.abort();
    actionRevision += 1;
    actionOperation = Object.freeze({
      revision: actionRevision,
      controller: new AbortController(),
      ...normalizedOwner,
    });
    return actionOperation;
  }

  /** 核对 PATCH 仍属于相同 admin 身份和成本异常导航，防止换租户或离页回写。 */
  function isActionOperationCurrent(operation, owner) {
    const currentOwner = normalizeActionOwner(owner);
    return Boolean(
      operation
      && currentOwner
      && actionOperation === operation
      && operation.revision === actionRevision
      && operation.identity === currentOwner.identity
      && operation.navigation === currentOwner.navigation,
    );
  }

  /** 只结束同一 PATCH owner；后来的动作不会被旧 finally 清除。 */
  function finishActionOperation(operation) {
    if (actionOperation === operation) {
      actionOperation = null;
    }
  }

  /** 暴露写操作忙状态，供定时刷新避免重建正在提交的动作表单。 */
  function isActionBusy() {
    return Boolean(actionOperation || actionDialog?.submitting);
  }

  /**
   * 构造只含可展示字段的状态快照。
   * cursor、nextCursor、幂等键、operation IDs 和 AbortController 都被有意排除。
   */
  function snapshot() {
    const anomalies = page?.cost_anomalies ?? [];
    const selectedAnomaly = selectedAnomalyId
      ? anomalies.find((item) => item?.cost_anomaly_id === selectedAnomalyId) ?? null
      : null;
    return {
      state: page ? "ok" : "loading",
      filters: { ...filters },
      cost_anomalies: anomalies,
      summary: page?.summary ?? {},
      total: page?.total ?? 0,
      limit: page?.limit ?? filters.limit,
      request_id: page?.requestId ?? "-",
      trace_id: page?.traceId ?? "-",
      canPrevious: cursorStack.length > 0,
      canNext: Boolean(page?.hasMore && page?.nextCursor),
      selectedAnomaly,
      actionDialog: actionDialog
        ? {
            open: true,
            anomalyId: actionDialog.anomalyId,
            action: actionDialog.action,
            dismissReason: actionDialog.dismissReason,
            durationMinutes: actionDialog.durationMinutes,
            rpmLimit: actionDialog.rpmLimit,
            tpmLimit: actionDialog.tpmLimit,
            concurrencyLimit: actionDialog.concurrencyLimit,
            submitting: actionDialog.submitting,
            error: actionDialog.error ? { ...actionDialog.error } : null,
          }
        : null,
      actionError: actionError ? { ...actionError } : null,
      feedback: feedback ? { ...feedback } : null,
    };
  }

  /** 在当前 tenant-qualified 响应页中按 ID 查找异常；永远不访问页外缓存。 */
  function findCurrentAnomaly(anomalyId) {
    return page?.cost_anomalies?.find((item) => item?.cost_anomaly_id === anomalyId) ?? null;
  }

  /** 取消当前列表读取并推进 revision；不影响独立 PATCH。 */
  function abortListOperation() {
    listOperation?.controller.abort();
    listOperation = null;
    listRevision += 1;
  }

  /** 同时取消 GET/PATCH 并推进两套 revision，供身份、导航和 pagehide 边界调用。 */
  function invalidateOperations() {
    listOperation?.controller.abort();
    actionOperation?.controller.abort();
    listOperation = null;
    actionOperation = null;
    listRevision += 1;
    actionRevision += 1;
  }

  return {
    clear,
    query,
    setFilters,
    resetFilters,
    acceptPage,
    nextPage,
    previousPage,
    selectAnomaly,
    closeDetail,
    openAction,
    beginAction,
    acceptAction,
    rejectAction,
    closeAction,
    beginListOperation,
    isListOperationCurrent,
    finishListOperation,
    beginActionOperation,
    isActionOperationCurrent,
    finishActionOperation,
    isActionBusy,
    snapshot,
  };
}

/**
 * 从认证上下文构造 admin-only 身份快照。
 * tenant、用户、角色和 Session 任一缺失/变化都会使旧异步 owner 失效；可选的
 * Session revision 一旦由认证合同提供，也必须参与 owner 比较。
 */
export function costAnomalyIdentitySnapshot(authContext) {
  // 成本异常 owner 绑定后端读权限快照；矩阵变更后旧列表和动作回调都不能写入新页面。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.BILLING_ANOMALY_READ);
}

/** 识别由离页、换身份或新刷新主动触发的 Abort，避免把正常生命周期显示成依赖故障。 */
export function isCostAnomalyAbortError(error) {
  return error?.name === "AbortError";
}

/** 只保留 GET 允许的固定筛选字段，并把非法枚举收敛为空值交由用户重新选择。 */
function normalizeFilters(values = {}) {
  const limit = Number.isInteger(values.limit) && values.limit >= 1 && values.limit <= 100
    ? values.limit
    : DEFAULT_FILTERS.limit;
  return {
    status: normalizeEnum(values.status, STATUS_VALUES),
    ruleType: normalizeEnum(values.ruleType, RULE_TYPE_VALUES),
    severity: normalizeEnum(values.severity, SEVERITY_VALUES),
    environmentId: text(values.environmentId).trim(),
    virtualKeyId: text(values.virtualKeyId).trim(),
    fromTime: text(values.fromTime).trim(),
    toTime: text(values.toTime).trim(),
    limit,
  };
}

/** 把可空值规范化为已知枚举；未知值不进入查询 URL。 */
function normalizeEnum(value, allowedValues) {
  const normalized = text(value).trim();
  return allowedValues.has(normalized) ? normalized : "";
}

/** 规范化列表 owner；refreshGeneration 防止较早刷新覆盖后发起的新页面快照。 */
function normalizeListOwner(owner) {
  const actionOwner = normalizeActionOwner(owner);
  if (!actionOwner || !Number.isSafeInteger(owner?.refreshGeneration)) {
    return null;
  }
  return { ...actionOwner, refreshGeneration: owner.refreshGeneration };
}

/** 规范化动作 owner；只有完整 admin 身份且仍在目标导航时允许写操作回调。 */
function normalizeActionOwner(owner) {
  const identity = text(owner?.identity);
  const navigation = text(owner?.navigation);
  if (!identity || navigation !== "cost-anomalies") {
    return null;
  }
  return { identity, navigation };
}

/** 把可空输入稳定转换为字符串，避免 owner 比较触发隐式类型差异。 */
function text(value) {
  return String(value ?? "");
}
