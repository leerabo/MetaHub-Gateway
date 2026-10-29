/**
 * 模块职责：管理 PAGE-074/075 的筛选、opaque cursor、选中详情和调整草稿生命周期。
 * 安全边界：cursor、幂等键和调整原因只驻留内存，不写 URL hash、DOM data 属性、
 * localStorage 或日志；登出、换租户、降权和离页必须调用 clear() 销毁状态。
 */

// 默认页长属于前端请求白名单，不包含 tenant、actor 或可由调用方注入的任意排序字段。
const DEFAULT_FILTERS = Object.freeze({ limit: 20 });

/**
 * 创建 FinOps 页面内存。
 *
 * @returns {object} 只通过方法读写的页面状态容器。
 */
export function createFinOpsPageState() {
  let filters = { ...DEFAULT_FILTERS };
  let cursor = null;
  let cursorStack = [];
  let page = null;
  let selectedEntryId = null;
  let dialog = null;
  let actionError = null;
  let feedback = null;

  /**
   * 销毁当前身份下的全部页面事实，特别覆盖 reason、幂等键、cursor 和旧分录引用。
   */
  function clear() {
    filters = { ...DEFAULT_FILTERS };
    cursor = null;
    cursorStack = [];
    page = null;
    selectedEntryId = null;
    dialog = null;
    actionError = null;
    feedback = null;
  }

  /**
   * 返回下一次 GET 的白名单筛选与当前 opaque cursor。
   * 调用方把该对象传给 API client；对象不包含 tenant_id 或 actor。
   */
  function query() {
    return { ...filters, cursor };
  }

  /**
   * 应用新筛选并回到同一快照协议的第一页。
   * 旧 cursor 与选择都和旧筛选绑定，必须同时失效。
   */
  function setFilters(nextFilters) {
    filters = normalizeFilters(nextFilters);
    cursor = null;
    cursorStack = [];
    page = null;
    selectedEntryId = null;
    actionError = null;
    feedback = null;
  }

  /**
   * 清空筛选；显式方法便于交互层表达用户意图并复用安全销毁规则。
   */
  function resetFilters() {
    setFilters(DEFAULT_FILTERS);
  }

  /**
   * 接受一次已通过后端权限与租户校验的账本页。
   * next_cursor 只保存在闭包中；返回给渲染器的 snapshot 不包含 cursor 原文。
   */
  function acceptPage(payload) {
    const entries = Array.isArray(payload?.ledger_entries) ? payload.ledger_entries : [];
    page = {
      ledger_entries: entries,
      totals: payload?.totals ?? {},
      total: payload?.total ?? 0,
      limit: payload?.limit ?? filters.limit,
      has_more: Boolean(payload?.has_more),
      nextCursor: payload?.next_cursor || null,
      request_id: payload?.request_id ?? "-",
      trace_id: payload?.trace_id ?? "-",
    };
    if (selectedEntryId && !entries.some((entry) => entry.finops_ledger_entry_id === selectedEntryId)) {
      // 追加写或筛选变化可能让旧选择离开当前快照页；清空选择比展示过期详情更安全。
      selectedEntryId = null;
      actionError = {
        code: "finops.selection_stale",
        message: "原分录已不在当前账本快照页，请重新选择",
        requestId: payload?.request_id ?? "-",
      };
    }
    return snapshot();
  }

  /**
   * 进入下一页；只有当前后端响应明确给出 next cursor 时才改变状态。
   */
  function nextPage() {
    if (!page?.has_more || !page.nextCursor) {
      return false;
    }
    cursorStack = [...cursorStack, cursor];
    cursor = page.nextCursor;
    page = null;
    selectedEntryId = null;
    actionError = null;
    return true;
  }

  /**
   * 返回上一页；栈中 null 代表首次无 cursor 的第一页，不能序列化到 DOM。
   */
  function previousPage() {
    if (!cursorStack.length) {
      return false;
    }
    cursor = cursorStack[cursorStack.length - 1] ?? null;
    cursorStack = cursorStack.slice(0, -1);
    page = null;
    selectedEntryId = null;
    actionError = null;
    return true;
  }

  /**
   * 选择当前页分录；未知 ID 被拒绝，防止 DOM 手工篡改注入跨页或跨租户标识。
   */
  function selectEntry(entryId) {
    const normalized = String(entryId ?? "");
    if (!page?.ledger_entries.some((entry) => entry.finops_ledger_entry_id === normalized)) {
      actionError = { code: "finops.entry_not_found", message: "当前页找不到该分录", requestId: page?.request_id ?? "-" };
      return false;
    }
    selectedEntryId = normalized;
    actionError = null;
    return true;
  }

  /**
   * 关闭详情并保留筛选、当前 cursor 和账本快照。
   */
  function closeDetail() {
    selectedEntryId = null;
  }

  /**
   * 打开新增调整或针对当前页分录的冲正对话框。
   * 幂等键与 operation IDs 由安全随机客户端生成后传入，仅驻留该对话框生命周期。
   */
  function openDialog({ targetEntryId = null, idempotencyKey, operationRequestId, operationTraceId }) {
    if (targetEntryId && !page?.ledger_entries.some((entry) => entry.finops_ledger_entry_id === targetEntryId)) {
      actionError = { code: "finops.entry_not_found", message: "冲正目标不在当前页", requestId: page?.request_id ?? "-" };
      return false;
    }
    dialog = {
      open: true,
      adjustmentType: targetEntryId ? "reversal" : "manual_credit",
      targetEntryId,
      amount: "",
      relatedRequestId: "",
      reason: "",
      submitting: false,
      error: null,
      idempotencyKey,
      operationRequestId,
      operationTraceId,
    };
    actionError = null;
    return true;
  }

  /**
   * 冻结用户提交草稿并进入 submitting；失败重试会保留同一幂等键和 operation trace。
   */
  function beginSubmit(values) {
    if (!dialog?.open || dialog.submitting) {
      return null;
    }
    dialog = {
      ...dialog,
      adjustmentType: dialog.targetEntryId ? "reversal" : String(values.adjustmentType ?? dialog.adjustmentType),
      amount: String(values.amount ?? ""),
      relatedRequestId: String(values.relatedRequestId ?? ""),
      reason: String(values.reason ?? ""),
      submitting: true,
      error: null,
    };
    return submitPayload();
  }

  /**
   * 返回当前提交所需的内存字段；不把 tenant、actor、currency 或 direction 添加到载荷。
   */
  function submitPayload() {
    if (!dialog?.open) {
      return null;
    }
    return {
      adjustmentType: dialog.adjustmentType,
      amount: dialog.amount,
      reversalOfEntryId: dialog.targetEntryId,
      relatedRequestId: dialog.relatedRequestId,
      reason: dialog.reason,
      idempotencyKey: dialog.idempotencyKey,
      operationRequestId: dialog.operationRequestId,
      operationTraceId: dialog.operationTraceId,
    };
  }

  /**
   * 接受已提交调整，销毁原因和幂等键，并回到第一页读取新分录。
   */
  function acceptAdjustment(result) {
    const newEntryId = result?.ledger_entry?.finops_ledger_entry_id ?? null;
    dialog = null;
    cursor = null;
    cursorStack = [];
    page = null;
    selectedEntryId = newEntryId;
    actionError = null;
    feedback = {
      message: result?.idempotent_replay ? "调整已确认" : "调整已入账",
      idempotentReplay: Boolean(result?.idempotent_replay),
      requestId: result?.request_id ?? "-",
    };
  }

  /**
   * 保存统一提交错误并恢复可重试状态；reason 和幂等键仍只在内存，不进入错误文案。
   */
  function rejectAdjustment(error) {
    if (!dialog?.open) {
      return;
    }
    dialog = {
      ...dialog,
      submitting: false,
      error: {
        code: error?.code ?? "finops.adjustment_failed",
        message: error?.message ?? "调整提交失败",
        requestId: error?.requestId ?? "-",
      },
    };
  }

  /**
   * 关闭对话框并立即丢弃 reason、幂等键和 operation IDs。
   */
  function closeDialog() {
    dialog = null;
  }

  /**
   * 构造渲染快照；故意排除 cursor、幂等键、operation IDs 和未提交草稿的内部字段。
   */
  function snapshot() {
    const entries = page?.ledger_entries ?? [];
    const selectedEntry = entries.find((entry) => entry.finops_ledger_entry_id === selectedEntryId) ?? null;
    return {
      state: "ok",
      ledger_entries: entries,
      totals: page?.totals ?? {},
      total: page?.total ?? 0,
      limit: page?.limit ?? filters.limit,
      has_more: Boolean(page?.has_more),
      canNext: Boolean(page?.has_more && page?.nextCursor),
      canPrevious: cursorStack.length > 0,
      request_id: page?.request_id ?? "-",
      trace_id: page?.trace_id ?? "-",
      filters: { ...filters },
      selectedEntryId,
      selectedEntry,
      dialog: publicDialog(dialog),
      actionError,
      feedback,
    };
  }

  return {
    clear,
    query,
    setFilters,
    resetFilters,
    acceptPage,
    nextPage,
    previousPage,
    selectEntry,
    closeDetail,
    openDialog,
    beginSubmit,
    acceptAdjustment,
    rejectAdjustment,
    closeDialog,
    snapshot,
  };
}

/**
 * 规范化筛选白名单；cursor、tenant 和未知字段不会被保存到页面状态。
 */
function normalizeFilters(value = {}) {
  return {
    startDate: optionalText(value.startDate),
    endDate: optionalText(value.endDate),
    entryType: optionalText(value.entryType),
    direction: optionalText(value.direction),
    quotaType: optionalText(value.quotaType),
    requestId: optionalText(value.requestId),
    priceSnapshotId: optionalText(value.priceSnapshotId),
    relatedEntryId: optionalText(value.relatedEntryId),
    limit: 20,
  };
}

/**
 * 把表单可选值裁剪为空字符串或去空白文本，保持后端筛选哈希稳定。
 */
function optionalText(value) {
  return String(value ?? "").trim();
}

/**
 * 构造渲染所需对话框状态；幂等键和 operation IDs 绝不离开闭包。
 */
function publicDialog(dialog) {
  if (!dialog?.open) {
    return { open: false };
  }
  return {
    open: true,
    adjustmentType: dialog.adjustmentType,
    targetEntryId: dialog.targetEntryId,
    amount: dialog.amount,
    relatedRequestId: dialog.relatedRequestId,
    reason: dialog.reason,
    submitting: dialog.submitting,
    error: dialog.error,
  };
}
