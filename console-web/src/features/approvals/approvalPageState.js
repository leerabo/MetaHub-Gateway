/** 维护审批页面的身份绑定状态与并发代际；前端状态只改善体验，不能替代后端授权。 */
/** PAGE-108/109 的 tenant/RBAC owner、私有 cursor 和三条异步 lane。 */
import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasEveryPermission,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const NAVIGATION_ID = "approvals";
const APPROVAL_ID = /^apr_[0-9a-f]{32}$/;
const PRIVATE_CURSOR = /^[A-Za-z0-9_-]{1,1024}$/;
const FILTER_STATUSES = new Set(["pending", "approved", "rejected", "all"]);
const TARGET_TYPES = new Set(["", "budget_block_release", "session_risk_action"]);
const TARGET_ACCESS = Object.freeze({
  budget_block_release: Object.freeze({
    read: PERMISSIONS.BILLING_BUDGET_READ,
    write: PERMISSIONS.BILLING_BUDGET_WRITE,
  }),
  session_risk_action: Object.freeze({
    read: PERMISSIONS.SESSION_RISK_READ,
    write: PERMISSIONS.SESSION_RISK_MANAGE,
  }),
});
// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const REASONS = Object.freeze({
  approve: new Set(["approved_business_need"]),
  reject: new Set(["risk_unacceptable", "budget_not_approved", "no_longer_needed"]),
});
const ERROR_MESSAGES = Object.freeze({
  "auth.permission_denied": "当前账号没有审批权限",
  "approval.input_invalid": "审批请求不合法",
  "approval.not_found": "当前租户中找不到该审批任务",
  "approval.state_conflict": "审批状态已变化，请刷新后重试",
  "approval.idempotency_conflict": "审批意图与已有操作冲突",
  "approval.target_conflict": "审批目标已变化，请重新发起审批",
  "approval.expired": "审批任务已超时并自动拒绝",
  "approval.step_up_required": "高风险动作需要二次验证",
  "approval.step_up_invalid": "二次验证失败或已失效",
  "approval.step_up_rate_limited": "二次验证尝试过多，请稍后重试",
  "approval.response_invalid": "审批响应合同不完整",
  "approval.unavailable": "审批服务暂不可用",
  "approval.writeback_unavailable": "审批结果正在等待恢复回写",
  "approval.event_unavailable": "审批通知正在等待恢复投递",
});

/** PAGE-108/109 的最小身份；缺 approval.read 时固定为空。 */
export function approvalReadIdentitySnapshot(authContext) {
  // 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.APPROVAL_READ);
}

/** 把 auth/me 与后端 approvals 导航冻结成完整页面 owner。 */
export function approvalPageOwner(authContext, consoleShellState) {
  const readIdentity = approvalReadIdentitySnapshot(authContext);
  const tenantId = mirroredTenantId(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === NAVIGATION_ID)
    : null;
  const required = normalizeCodes(item?.required_permissions);
  const navigationCodes = normalizeCodes(navigation?.permission_codes);
  const authCodes = permissionCodesFromAuthContext(authContext);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  if (
    !readIdentity
    || !tenantId
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== NAVIGATION_ID
    || navigation?.active_navigation_item_id !== NAVIGATION_ID
    || item?.target_section !== NAVIGATION_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.APPROVAL_READ])
    || JSON.stringify(navigationCodes) !== JSON.stringify(authCodes)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    readIdentity,
    tenantId,
    canDecide: hasPermission(authContext, PERMISSIONS.APPROVAL_DECIDE),
    readableTargets: Object.freeze(Object.entries(TARGET_ACCESS)
      .filter(([, access]) => hasPermission(authContext, access.read))
      .map(([type]) => type)),
    writableTargets: Object.freeze(Object.entries(TARGET_ACCESS)
      .filter(([, access]) => hasEveryPermission(authContext, [
        PERMISSIONS.APPROVAL_DECIDE,
        access.read,
        access.write,
      ]))
      .map(([type]) => type)),
    navigationOwner: JSON.stringify([item.id, item.target_section, required, revision, fingerprint]),
  });
}

/** 对单个任务重新计算 base + target action 权限；按钮投影不能替代后端事务内复核。 */
export function approvalActionAccess(summary, authContext, action) {
  const access = TARGET_ACCESS[summary?.target_type];
  return Boolean(
    ["approve", "reject"].includes(action)
    && summary?.status === "pending"
    && access
    && summary.required_permission_code === access.write
    && hasEveryPermission(authContext, [PERMISSIONS.APPROVAL_READ, PERMISSIONS.APPROVAL_DECIDE, access.read, access.write]),
  );
}

/** 创建 list/detail/action 分离、可同步销毁 DOM 的审批页面状态机。 */
export function createApprovalPageState({ clearDom = () => {}, createIdempotencyKey } = {}) {
  let ownerKey = "";
  let ownerCapabilities = emptyCapabilities();
  let listState = "loading";
  let listError = null;
  let approvals = Object.freeze([]);
  let status = "pending";
  let targetType = "";
  let cursor = null;
  let cursorStack = [];
  let nextCursor = null;
  let selectedApprovalId = "";
  let mobileFocus = false;
  let detailState = "idle";
  let detailError = null;
  let detail = null;
  let actionOpen = false;
  let actionType = "";
  let actionReason = "";
  let actionState = "idle";
  let actionError = null;
  let actionRequestId = "";
  let generations = { list: 0, detail: 0, action: 0 };
  let operations = { list: null, detail: null, action: null };

  /**
   * 维护 enter 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    ownerCapabilities = Object.freeze({
      canDecide: owner.canDecide === true,
      readableTargets: Object.freeze([...owner.readableTargets]),
      writableTargets: Object.freeze([...owner.writableTargets]),
    });
    if (!ownerCapabilities.canDecide) closeAction(true);
    return true;
  }

  /** 清空任务、密码所在 DOM、cursor、幂等意图和全部异步 owner，再取消请求。 */
  function clear() {
    const pending = Object.values(operations).filter(Boolean);
    ownerKey = "";
    ownerCapabilities = emptyCapabilities();
    listState = "loading";
    listError = null;
    approvals = Object.freeze([]);
    status = "pending";
    targetType = "";
    cursor = null;
    cursorStack = [];
    nextCursor = null;
    selectedApprovalId = "";
    mobileFocus = false;
    detailState = "idle";
    detailError = null;
    detail = null;
    resetAction();
    operations = { list: null, detail: null, action: null };
    generations = {
      list: generations.list + 1,
      detail: generations.detail + 1,
      action: generations.action + 1,
    };
    try {
      clearDom();
    } finally {
      for (const operation of pending) operation.controller.abort();
    }
  }

  /** snapshot 不包含 cursor、password、step-up token、幂等键、Controller 或权限指纹。 */
  /**
   * 维护 snapshot 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function snapshot() {
    return Object.freeze({
      state: listState,
      error: listError,
      approvals,
      status,
      targetType,
      canPrevious: cursorStack.length > 0,
      canNext: Boolean(nextCursor),
      selectedApprovalId,
      mobileFocus,
      detailState,
      detailError,
      detail,
      canDecide: ownerCapabilities.canDecide,
      writableTargets: ownerCapabilities.writableTargets,
      actionOpen,
      actionType,
      actionReason,
      actionState,
      actionError,
      actionRequestId,
    });
  }

  /**
   * 维护 listQuery 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function listQuery() {
    return Object.freeze({ status, targetType, cursor });
  }

  /**
   * 维护 setFilters 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function setFilters(nextStatus, nextTargetType) {
    if (operations.list || operations.action || actionOpen) return false;
    if (!FILTER_STATUSES.has(nextStatus) || !TARGET_TYPES.has(nextTargetType)) return false;
    if (status === nextStatus && targetType === nextTargetType) return false;
    status = nextStatus;
    targetType = nextTargetType;
    resetListPosition();
    return true;
  }

  /** 同步 hash 内受控任务 ID；非法值由解析器提前丢弃，此处仍独立校验。 */
  function syncMobileFocus(approvalId) {
    const next = APPROVAL_ID.test(String(approvalId ?? "")) ? approvalId : "";
    if (operations.action) return false;
    const changed = selectedApprovalId !== next || mobileFocus !== Boolean(next);
    if (!changed) return false;
    abortLane("detail");
    selectedApprovalId = next;
    mobileFocus = Boolean(next);
    detailState = next ? "loading" : "idle";
    detailError = null;
    detail = null;
    resetAction();
    return true;
  }

  /**
   * 维护 selectApproval 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function selectApproval(approvalId) {
    if (operations.action || !APPROVAL_ID.test(String(approvalId ?? ""))) return false;
    if (!approvals.some((item) => item.approval_id === approvalId)) return false;
    if (selectedApprovalId === approvalId && detailState === "ready") return false;
    abortLane("detail");
    selectedApprovalId = approvalId;
    mobileFocus = false;
    detailState = "loading";
    detailError = null;
    detail = null;
    resetAction();
    return true;
  }

  /**
   * 维护 previousPage 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function previousPage() {
    if (!cursorStack.length || operations.list || operations.action || actionOpen || mobileFocus) return false;
    cursor = cursorStack.pop() ?? null;
    nextCursor = null;
    clearListFacts();
    return true;
  }

  /**
   * 维护 nextPage 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function nextPage() {
    if (!nextCursor || operations.list || operations.action || actionOpen || mobileFocus) return false;
    cursorStack.push(cursor);
    cursor = nextCursor;
    nextCursor = null;
    clearListFacts();
    return true;
  }

  /**
   * 维护 beginList 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function beginList(owner, refreshId) {
    if (!ownerMatches(owner) || !Number.isSafeInteger(refreshId)) return null;
    listState = "loading";
    listError = null;
    return begin("list", { refreshId });
  }

  /**
   * 维护 acceptList 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function acceptList(operation, response, owner, refreshId) {
    if (!isCurrent("list", operation, owner) || operation.refreshId !== refreshId) return false;
    const visible = response.approvals.filter((item) => ownerCapabilities.readableTargets.includes(item.target_type));
    approvals = Object.freeze(visible);
    nextCursor = response.next_cursor;
    listState = "ready";
    listError = null;
    if (!mobileFocus && selectedApprovalId && !visible.some((item) => item.approval_id === selectedApprovalId)) {
      clearSelection();
    }
    return true;
  }

  /**
   * 维护 rejectList 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function rejectList(operation, error, owner) {
    if (!isCurrent("list", operation, owner)) return false;
    listState = "error";
    listError = fixedError(error);
    approvals = Object.freeze([]);
    if (!mobileFocus) clearSelection();
    return true;
  }

  /**
   * 维护 finishList 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function finishList(operation) {
    finish("list", operation);
  }

  /**
   * 维护 beginDetail 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function beginDetail(owner, refreshId) {
    if (!ownerMatches(owner) || !selectedApprovalId || !Number.isSafeInteger(refreshId)) return null;
    detailState = "loading";
    detailError = null;
    return begin("detail", { refreshId, approvalId: selectedApprovalId });
  }

  /**
   * 维护 acceptDetail 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function acceptDetail(operation, response, owner, refreshId) {
    if (
      !isCurrent("detail", operation, owner)
      || operation.refreshId !== refreshId
      || response?.approval?.approval_id !== operation.approvalId
      || !ownerCapabilities.readableTargets.includes(response.approval.target_type)
    ) return false;
    detail = response;
    detailState = "ready";
    detailError = null;
    return true;
  }

  /**
   * 维护 rejectDetail 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function rejectDetail(operation, error, owner) {
    if (!isCurrent("detail", operation, owner)) return false;
    detailState = "error";
    detailError = fixedError(error);
    detail = null;
    resetAction();
    return true;
  }

  /**
   * 维护 finishDetail 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function finishDetail(operation) {
    finish("detail", operation);
  }

  /**
   * 维护 openAction 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function openAction(owner, action) {
    const task = detail?.approval;
    if (
      !ownerMatches(owner)
      || operations.action
      || !task
      || !detail.allowed_actions.includes(action)
      || !ownerCapabilities.writableTargets.includes(task.target_type)
      || !REASONS[action]
    ) return false;
    actionOpen = true;
    actionType = action;
    actionReason = action === "approve" ? "approved_business_need" : "risk_unacceptable";
    actionState = "idle";
    actionError = null;
    actionRequestId = "";
    return true;
  }

  /**
   * 维护 setActionReason 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function setActionReason(reason) {
    if (!actionOpen || operations.action || !REASONS[actionType]?.has(reason)) return false;
    actionReason = reason;
    actionError = null;
    return true;
  }

  /**
   * 维护 closeAction 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function closeAction(force = false) {
    if (operations.action && !force) return false;
    if (force && operations.action) abortLane("action");
    resetAction();
    return true;
  }

  /** 动作 ticket 只持有安全任务意图；password 由绑定层直接交给 main，不进入状态机。 */
  function beginAction(owner) {
    const task = detail?.approval;
    if (
      !ownerMatches(owner)
      || !actionOpen
      || operations.action
      || !task
      || task.approval_id !== selectedApprovalId
      || task.status !== "pending"
      || !detail.allowed_actions.includes(actionType)
      || !REASONS[actionType]?.has(actionReason)
      || !ownerCapabilities.writableTargets.includes(task.target_type)
      || typeof createIdempotencyKey !== "function"
    ) return null;
    let idempotencyKey;
    try {
      idempotencyKey = createIdempotencyKey();
    } catch (error) {
      actionState = "error";
      actionError = fixedError(error);
      return null;
    }
    actionState = "submitting";
    actionError = null;
    actionRequestId = "";
    return begin("action", {
      approvalId: task.approval_id,
      action: actionType,
      reasonCode: actionReason,
      expectedRevision: task.state_revision,
      requiresStepUp: task.requires_step_up,
      idempotencyKey,
    });
  }

  /**
   * 维护 acceptAction 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function acceptAction(operation, response, owner) {
    if (
      !isCurrent("action", operation, owner)
      || response?.approval?.approval_id !== operation.approvalId
      || response?.decision?.decision !== operation.action
    ) return false;
    detail = Object.freeze({
      ...detail,
      approval: response.approval,
      decisions: Object.freeze([response.decision]),
      allowed_actions: Object.freeze([]),
      request_id: response.request_id,
      trace_id: response.trace_id,
    });
    approvals = Object.freeze(approvals.map((item) => (
      item.approval_id === response.approval.approval_id ? response.approval : item
    )));
    detailState = "ready";
    actionOpen = false;
    actionState = "success";
    actionError = null;
    actionRequestId = response.request_id;
    actionType = "";
    actionReason = "";
    return true;
  }

  /**
   * 维护 rejectAction 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function rejectAction(operation, error, owner) {
    if (!isCurrent("action", operation, owner)) return false;
    actionState = "error";
    actionError = fixedError(error);
    actionRequestId = actionError.requestId;
    // 冲突或到期表示当前密码/ticket owner 已失效；关闭 Dialog 后由调用方刷新权威任务。
    if (error?.status === 409) {
      actionOpen = false;
      actionType = "";
      actionReason = "";
    }
    return true;
  }

  function finishAction(operation) {
    finish("action", operation);
  }

  /**
   * 维护 isCurrentOperation 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function isCurrentOperation(lane, operation, owner) {
    return isCurrent(lane, operation, owner);
  }

  /**
   * 维护 isActionBusy 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function isActionBusy() {
    return Boolean(operations.action);
  }

  /**
   * 维护 blocksShellDomReplacement 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function blocksShellDomReplacement() {
    return actionOpen || Boolean(operations.action);
  }

  /**
   * 维护 begin 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function begin(lane, extra) {
    abortLane(lane);
    generations[lane] += 1;
    const controller = new AbortController();
    const operation = Object.freeze({
      lane,
      generation: generations[lane],
      ownerKey,
      controller,
      signal: controller.signal,
      ...extra,
    });
    operations[lane] = operation;
    return operation;
  }

  /**
   * 维护 isCurrent 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function isCurrent(lane, operation, owner) {
    return Boolean(
      operation
      && operations[lane] === operation
      && operation.generation === generations[lane]
      && !operation.signal.aborted
      && ownerMatches(owner),
    );
  }

  /**
   * 维护 finish 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function finish(lane, operation) {
    if (operations[lane] === operation) operations[lane] = null;
  }

  /**
   * 维护 abortLane 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function abortLane(lane) {
    const operation = operations[lane];
    generations[lane] += 1;
    operations[lane] = null;
    operation?.controller?.abort();
  }

  /**
   * 维护 ownerMatches 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function ownerMatches(owner) {
    return Boolean(ownerKey && ownerKey === ownerKeyFor(owner));
  }

  /**
   * 维护 resetListPosition 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function resetListPosition() {
    abortLane("list");
    cursor = null;
    cursorStack = [];
    nextCursor = null;
    approvals = Object.freeze([]);
    listState = "loading";
    clearSelection();
  }

  /**
   * 维护 clearListFacts 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function clearListFacts() {
    abortLane("list");
    approvals = Object.freeze([]);
    listState = "loading";
    listError = null;
    clearSelection();
  }

  /**
   * 维护 clearSelection 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function clearSelection() {
    abortLane("detail");
    selectedApprovalId = "";
    detailState = "idle";
    detailError = null;
    detail = null;
    resetAction();
  }

  /**
   * 维护 resetAction 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
   */
  function resetAction() {
    actionOpen = false;
    actionType = "";
    actionReason = "";
    actionState = "idle";
    actionError = null;
    actionRequestId = "";
  }

  return Object.freeze({
    enter,
    clear,
    snapshot,
    listQuery,
    setFilters,
    syncMobileFocus,
    selectApproval,
    previousPage,
    nextPage,
    beginList,
    acceptList,
    rejectList,
    finishList,
    beginDetail,
    acceptDetail,
    rejectDetail,
    finishDetail,
    openAction,
    setActionReason,
    closeAction,
    beginAction,
    acceptAction,
    rejectAction,
    finishAction,
    isCurrentOperation,
    isActionBusy,
    blocksShellDomReplacement,
  });
}

/**
 * 维护 isApprovalAbortError 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
 */
export function isApprovalAbortError(error) {
  return error?.name === "AbortError";
}

/**
 * 维护 ownerKeyFor 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
 */
function ownerKeyFor(owner) {
  if (
    !owner?.readIdentity
    || !owner?.tenantId
    || !owner?.navigationOwner
    || !Array.isArray(owner.readableTargets)
    || !Array.isArray(owner.writableTargets)
  ) return "";
  return JSON.stringify([
    owner.readIdentity,
    owner.tenantId,
    owner.navigationOwner,
    owner.canDecide === true,
    [...owner.readableTargets].sort(),
    [...owner.writableTargets].sort(),
  ]);
}

/**
 * 维护 mirroredTenantId 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
 */
function mirroredTenantId(authContext) {
  const userTenant = String(authContext?.user?.tenant_id ?? "");
  const tenant = String(authContext?.tenant?.tenant_id ?? "");
  // tenant_id 必须来自可信所有者关系并参与每次读写条件；缺失或不一致时立即失败关闭。
  return userTenant && userTenant === tenant && /^[A-Za-z0-9_.:-]{1,160}$/.test(userTenant)
    ? userTenant
    : "";
}

/**
 * 维护 normalizeCodes 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
 */
function normalizeCodes(value) {
  if (!Array.isArray(value)) return null;
  const normalized = value.map((item) => String(item ?? ""));
  if (normalized.some((item) => !/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(item))) return null;
  if (new Set(normalized).size !== normalized.length) return null;
  return normalized.sort();
}

/**
 * 维护 nullableCursor 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
 */
function nullableCursor(value) {
  return value === null || (typeof value === "string" && PRIVATE_CURSOR.test(value));
}

/**
 * 维护 fixedError 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
 */
function fixedError(error) {
  const code = Object.hasOwn(ERROR_MESSAGES, error?.code) ? error.code : "approval.unavailable";
  return Object.freeze({
    status: Number.isSafeInteger(error?.status) ? error.status : 503,
    code,
    message: ERROR_MESSAGES[code],
    requestId: /^[A-Za-z0-9_.:-]{1,160}$/.test(String(error?.requestId ?? "")) ? error.requestId : "-",
  });
}

/**
 * 维护 emptyCapabilities 对应页面状态；仅当前身份、导航代际和请求所有者可以提交结果。
 */
function emptyCapabilities() {
  return Object.freeze({ canDecide: false, readableTargets: Object.freeze([]), writableTargets: Object.freeze([]) });
}
