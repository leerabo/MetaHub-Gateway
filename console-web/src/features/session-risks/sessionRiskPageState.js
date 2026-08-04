/**
 * 模块职责：维护 PAGE-098 的授权 owner、筛选、opaque cursor、详情、动作草稿和异步生命周期。
 * 安全边界：cursor、幂等键、operation request/trace、授权指纹和 AbortController 只存在闭包内存；
 * snapshot 永不暴露这些值，离页、换租户、换 Session、权限变化或筛选变化会统一销毁。
 */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasEveryPermission,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";
import {
  availableSessionRiskActions,
  sessionRiskReasonCodes,
} from "../../lib/sessionRisks.js";

// 页面只保存冻结查询白名单；空 status 代表后端默认 high_risk,frozen，而不是“没有风险”。
const NAVIGATION_ID = "session-risks";
const DEFAULT_FILTERS = Object.freeze({ limit: 20 });
const STATUS_VALUES = new Set(["high_risk", "frozen", "resolved", "expired"]);
const SCOPE_VALUES = new Set(["enterprise_chat", "api_session"]);
// 兼容现有 UUID 与历史前缀主键，但拒绝斜杠、空白和控制字符，避免 DOM 注入值改变 query 语义。
const VIRTUAL_KEY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;

/**
 * 从实时认证与后端导航快照构造 PAGE-098 owner。
 *
 * @param {object|null} authContext 当前 /auth/me 权限快照。
 * @param {object|null} consoleShellState 当前后端授权导航。
 * @param {object} options requireManage=true 时同时要求 read/manage。
 * @returns {object|null} 不含 Token 的只读 owner；任一镜像漂移返回 null。
 *
 * 前端 owner 只阻止无权预取和迟到回写，不能替代 GET/POST 的后端 RBAC 与事务内复核。
 */
export function sessionRiskPageOwner(
  authContext,
  consoleShellState,
  { requireManage = false } = {},
) {
  const identity = authorizationIdentitySnapshot(authContext, PERMISSIONS.SESSION_RISK_READ);
  const tenantId = mirroredTenantId(authContext);
  const navigation = consoleShellState?.navigation;
  const navigationItem = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((item) => item?.id === NAVIGATION_ID)
    : null;
  const requiredPermissions = normalizeRequiredPermissions(navigationItem?.required_permissions);
  const authPermissionCodes = permissionCodesFromAuthContext(authContext);
  const navigationPermissionCodes = normalizeRequiredPermissions(navigation?.permission_codes);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const canManage = hasEveryPermission(authContext, [
    PERMISSIONS.SESSION_RISK_READ,
    PERMISSIONS.SESSION_RISK_MANAGE,
  ]);
  if (
    !identity
    || !tenantId
    || !hasPermission(authContext, PERMISSIONS.SESSION_RISK_READ)
    || (requireManage && !canManage)
    || consoleShellState?.activeNavigationItemId !== NAVIGATION_ID
    || navigation?.active_navigation_item_id !== NAVIGATION_ID
    || navigationItem?.target_section !== NAVIGATION_ID
    || navigationItem?.visible === false
    || navigationItem?.disabled === true
    || !requiredPermissions.includes(PERMISSIONS.SESSION_RISK_READ)
    || JSON.stringify(navigationPermissionCodes) !== JSON.stringify(authPermissionCodes)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) {
    // 后端导航、实时权限或 tenant 镜像任一不完整时，不生成 PAGE-098 DOM 和网络 owner。
    return null;
  }
  const navigationOwner = JSON.stringify([
    navigationItem.id,
    navigationItem.target_section,
    requiredPermissions,
    revision,
    fingerprint,
  ]);
  return Object.freeze({
    identity,
    tenantId,
    navigation: NAVIGATION_ID,
    navigationOwner,
    canManage,
  });
}

/** 返回只要求 read 的身份快照；tenant、用户、Session、revision 或指纹变化都会改变结果。 */
export function sessionRiskReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.SESSION_RISK_READ);
}

/** 返回同时要求 read/manage 的身份快照；manage-only 自定义角色固定得到空值。 */
export function sessionRiskManageIdentitySnapshot(authContext) {
  if (!hasEveryPermission(authContext, [
    PERMISSIONS.SESSION_RISK_READ,
    PERMISSIONS.SESSION_RISK_MANAGE,
  ])) return "";
  return JSON.stringify([
    authorizationIdentitySnapshot(authContext, PERMISSIONS.SESSION_RISK_READ),
    authorizationIdentitySnapshot(authContext, PERMISSIONS.SESSION_RISK_MANAGE),
  ]);
}

/**
 * 创建 PAGE-098 私有页面状态机。
 *
 * @returns {object} 只能通过显式方法变更的状态；snapshot 排除 cursor、幂等键和 owner 指纹。
 */
export function createSessionRiskPageState() {
  let ownerKey = "";
  let ownerCanManage = false;
  let filters = { ...DEFAULT_FILTERS };
  let cursor = null;
  let cursorStack = [];
  let page = null;
  let loadError = null;
  let selectedRiskId = null;
  let actionDialog = null;
  let feedback = null;
  let pageError = null;
  let listRevision = 0;
  let listOperation = null;
  let actionRevision = 0;
  let actionOperation = null;

  /** 绑定当前 read owner；身份、权限或导航 owner 变化时先销毁上一租户全部事实。 */
  function enter(owner) {
    const nextOwnerKey = keyFromOwner(owner);
    if (!nextOwnerKey) {
      leave();
      return false;
    }
    if (ownerKey !== nextOwnerKey || ownerCanManage !== owner.canManage) {
      resetAll();
      ownerKey = nextOwnerKey;
      ownerCanManage = owner.canManage === true;
    }
    return true;
  }

  /** 离页、登出、401、403 或导航裁剪时取消请求并清空 owner 与全部业务事实。 */
  function leave() {
    resetAll();
    ownerKey = "";
    ownerCanManage = false;
  }

  /** clear 是主应用统一生命周期入口，语义与 leave 相同。 */
  function clear() {
    leave();
  }

  /** 返回下一次 GET 的筛选白名单与当前内存 cursor。 */
  function query() {
    return { ...filters, cursor };
  }

  /** 应用新筛选并销毁 cursor、详情、Dialog、幂等键和所有异步 owner。 */
  function setFilters(values) {
    invalidateOperations();
    filters = normalizeFilters(values);
    cursor = null;
    cursorStack = [];
    page = null;
    loadError = null;
    selectedRiskId = null;
    actionDialog = null;
    feedback = null;
    pageError = null;
  }

  /** 清空筛选并复用完整快照失效规则。 */
  function resetFilters() {
    setFilters(DEFAULT_FILTERS);
  }

  /** 开始一次列表 GET，并用 refreshGeneration 淘汰较早的全页刷新。 */
  function beginListOperation(owner, refreshGeneration) {
    const key = keyFromOwner(owner);
    if (
      !key
      || key !== ownerKey
      || !Number.isSafeInteger(refreshGeneration)
      || refreshGeneration < 1
    ) return null;
    abortListOperation();
    listRevision += 1;
    listOperation = Object.freeze({
      revision: listRevision,
      ownerKey: key,
      refreshGeneration,
      controller: new AbortController(),
    });
    return listOperation;
  }

  /** 核对 GET ticket、完整 owner 与刷新代次仍属于当前页面。 */
  function isListOperationCurrent(operation, owner, refreshGeneration) {
    return Boolean(
      operation
      && listOperation === operation
      && !operation.controller.signal.aborted
      && operation.revision === listRevision
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && operation.refreshGeneration === refreshGeneration,
    );
  }

  /** 接受已由 strict client 深度投影的列表，并把 cursor 留在私有闭包。 */
  function acceptPage(operation, payload, owner, refreshGeneration) {
    if (
      !isListOperationCurrent(operation, owner, refreshGeneration)
      || payload?.state !== "ok"
      || !Array.isArray(payload.session_risks)
    ) return false;
    page = {
      runtimeStatus: payload.runtime_status,
      summary: payload.summary,
      sessionRisks: payload.session_risks,
      nextCursor: payload.next_cursor,
      requestId: payload.request_id,
      traceId: payload.trace_id,
    };
    loadError = null;
    if (selectedRiskId && !findCurrentRisk(selectedRiskId)) {
      // 新快照不含旧选择时清除 Dialog 与键，避免对页外 revision 提交动作。
      selectedRiskId = null;
      actionDialog = null;
      pageError = fixedPageError(
        "session_risk.not_found",
        "原高危会话已不在当前快照页，请重新选择",
        payload.request_id,
      );
    }
    if (actionDialog) {
      const actionRisk = findCurrentRisk(actionDialog.sessionRiskScoreId);
      if (
        !actionRisk
        || actionRisk.state_revision !== actionDialog.expectedRevision
        || !availableSessionRiskActions(actionRisk).includes(actionDialog.action)
      ) {
        // 明确刷新已证明 revision/状态变化，旧意图不能改用新 revision 静默重放。
        actionDialog = null;
        pageError = fixedPageError(
          "session_risk.state_conflict",
          "会话状态已变化，请重新选择处置",
          payload.request_id,
        );
      }
    }
    return true;
  }

  /** 接受列表统一错误，只保留固定 code 文案和 request_id，不保留响应正文。 */
  function rejectPage(operation, error, owner, refreshGeneration) {
    if (!isListOperationCurrent(operation, owner, refreshGeneration)) return false;
    page = null;
    selectedRiskId = null;
    actionDialog = null;
    feedback = null;
    loadError = fixedPageError(
      safeErrorCode(error?.code) || "session_risk.unavailable",
      fixedErrorMessage(error?.code, "高危会话读取失败"),
      safeId(error?.requestId) || "-",
    );
    return true;
  }

  /** 只结束调用方自己的 GET，旧 finally 不得清除后来刷新创建的新 ticket。 */
  function finishListOperation(operation) {
    if (listOperation === operation) listOperation = null;
  }

  /** 使用后端签发的下一页 cursor；DOM 和 snapshot 都只暴露 canNext。 */
  function nextPage() {
    if (!page?.nextCursor) return false;
    invalidateOperations();
    cursorStack = [...cursorStack, cursor];
    cursor = page.nextCursor;
    page = null;
    selectedRiskId = null;
    actionDialog = null;
    feedback = null;
    pageError = null;
    return true;
  }

  /** 从内存 cursor 栈返回上一页；null 明确代表无 cursor 的第一页。 */
  function previousPage() {
    if (!cursorStack.length) return false;
    invalidateOperations();
    cursor = cursorStack[cursorStack.length - 1] ?? null;
    cursorStack = cursorStack.slice(0, -1);
    page = null;
    selectedRiskId = null;
    actionDialog = null;
    feedback = null;
    pageError = null;
    return true;
  }

  /**
   * 只允许选择当前 tenant-qualified 页内的 score ID。
   * 参数：DOM 提交的 score ID。返回：当前页存在并完成选择时为 true，否则记录固定页错误。
   */
  function selectRisk(sessionRiskScoreId) {
    const risk = findCurrentRisk(String(sessionRiskScoreId ?? ""));
    if (!risk) {
      pageError = fixedPageError(
        "session_risk.not_found",
        "当前页找不到该高危会话",
        page?.requestId ?? "-",
      );
      return false;
    }
    selectedRiskId = risk.session_risk_score_id;
    actionDialog = null;
    pageError = null;
    return true;
  }

  /** 关闭详情时同时丢弃未提交处置意图。 */
  function closeDetail() {
    abortActionOperation();
    selectedRiskId = null;
    actionDialog = null;
  }

  /**
   * 为当前页 score 打开固定动作 Dialog，并冻结 revision、幂等键和 operation IDs。
   * DOM 传入的动作会再次与当前状态、overlay 和双权限 owner 交叉验证。
   */
  function openAction({
    sessionRiskScoreId,
    action,
    idempotencyKey,
    operationRequestId,
    operationTraceId,
  }) {
    const risk = findCurrentRisk(String(sessionRiskScoreId ?? ""));
    const normalizedAction = String(action ?? "");
    if (
      !ownerCanManage
      || !risk
      || !availableSessionRiskActions(risk).includes(normalizedAction)
    ) {
      pageError = fixedPageError(
        "session_risk.state_conflict",
        "当前会话状态不支持该处置",
        page?.requestId ?? "-",
      );
      return false;
    }
    const reasons = sessionRiskReasonCodes(normalizedAction);
    selectedRiskId = risk.session_risk_score_id;
    actionDialog = {
      sessionRiskScoreId: risk.session_risk_score_id,
      expectedRevision: risk.state_revision,
      action: normalizedAction,
      reasonCode: reasons[0],
      submitting: false,
      error: null,
      idempotencyKey,
      operationRequestId,
      operationTraceId,
    };
    feedback = null;
    pageError = null;
    return true;
  }

  /** 冻结一次 Dialog 提交，并返回不含 tenant、actor 或 target ID 的 API 参数。 */
  function beginAction({ reasonCode } = {}) {
    if (!actionDialog || actionDialog.submitting || !ownerCanManage) return null;
    const risk = findCurrentRisk(actionDialog.sessionRiskScoreId);
    const normalizedReason = String(reasonCode ?? actionDialog.reasonCode ?? "");
    if (
      !risk
      || risk.state_revision !== actionDialog.expectedRevision
      || !availableSessionRiskActions(risk).includes(actionDialog.action)
      || !sessionRiskReasonCodes(actionDialog.action).includes(normalizedReason)
    ) return null;
    actionDialog = {
      ...actionDialog,
      reasonCode: normalizedReason,
      submitting: true,
      error: null,
    };
    return {
      sessionRiskScoreId: actionDialog.sessionRiskScoreId,
      action: actionDialog.action,
      reasonCode: actionDialog.reasonCode,
      expectedRevision: actionDialog.expectedRevision,
      idempotencyKey: actionDialog.idempotencyKey,
      operationRequestId: actionDialog.operationRequestId,
      operationTraceId: actionDialog.operationTraceId,
    };
  }

  /**
   * 开始独立 POST owner。
   * 参数：实时 read+manage owner。返回：绑定当前动作代次的 AbortController，非法 owner 返回 null。
   */
  function beginActionOperation(owner) {
    const key = keyFromOwner(owner);
    if (!key || key !== ownerKey || owner?.canManage !== true || !actionDialog?.submitting) return null;
    abortActionOperation();
    actionRevision += 1;
    actionOperation = Object.freeze({
      revision: actionRevision,
      ownerKey: key,
      controller: new AbortController(),
    });
    return actionOperation;
  }

  /** 核对 POST 仍属于同一 tenant、Session、权限、导航和动作代次。 */
  function isActionOperationCurrent(operation, owner) {
    return Boolean(
      operation
      && actionOperation === operation
      && !operation.controller.signal.aborted
      && operation.revision === actionRevision
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && owner?.canManage === true,
    );
  }

  /** 接受 action 结果并替换当前页同 ID 项；汇总随后由主应用重新 GET。 */
  function acceptAction(operation, result, owner) {
    if (!isActionOperationCurrent(operation, owner) || result?.state !== "ok") return false;
    const updated = result.session_risk;
    // 只能替换当前 tenant-qualified 页已经存在的 score，迟到或跨页结果一律丢弃。
    if (!findCurrentRisk(updated?.session_risk_score_id)) return false;
    page = {
      ...page,
      sessionRisks: page.sessionRisks.map((item) => (
        item.session_risk_score_id === updated.session_risk_score_id ? updated : item
      )),
    };
    selectedRiskId = updated.session_risk_score_id;
    actionDialog = null;
    pageError = null;
    feedback = {
      message: result.idempotent_replay ? "风险处置已确认" : "风险处置已提交",
      idempotentReplay: result.idempotent_replay === true,
      requestId: result.request_id,
    };
    return true;
  }

  /**
   * 接受 POST 错误并恢复 Dialog。
   * uncertain=true 保留原键；明确 4xx 使用 replacement IDs，防止修正后的意图复用旧摘要。
   */
  function rejectAction(operation, error, owner, { retainIdempotencyKey, replacementIds } = {}) {
    if (!isActionOperationCurrent(operation, owner) || !actionDialog) return false;
    actionDialog = {
      ...actionDialog,
      submitting: false,
      error: fixedPageError(
        safeErrorCode(error?.code) || "session_risk.unavailable",
        fixedErrorMessage(error?.code, "风险处置提交失败"),
        safeId(error?.requestId) || "-",
      ),
      ...(retainIdempotencyKey
        ? {}
        : {
            idempotencyKey: replacementIds?.idempotencyKey,
            operationRequestId: replacementIds?.operationRequestId,
            operationTraceId: replacementIds?.operationTraceId,
          }),
    };
    return true;
  }

  /** 只结束调用方自己的 POST，防止旧 finally 清除下一次处置 owner。 */
  function finishActionOperation(operation) {
    if (actionOperation === operation) actionOperation = null;
  }

  /** 关闭 Dialog 会丢弃原因、revision、幂等键和 operation IDs。 */
  function closeAction() {
    abortActionOperation();
    actionDialog = null;
  }

  /** 告知定时刷新当前是否有结果未知的治理写入，避免重建动作 DOM。 */
  function isActionBusy() {
    return Boolean(actionOperation || actionDialog?.submitting);
  }

  /**
   * 构造只含可展示字段的页面快照。
   * cursor、nextCursor 原文、幂等键、operation IDs、owner、指纹和 controller 被有意排除。
   */
  function snapshot() {
    const risks = page?.sessionRisks ?? [];
    const selectedRisk = selectedRiskId
      ? risks.find((item) => item.session_risk_score_id === selectedRiskId) ?? null
      : null;
    return Object.freeze({
      state: loadError ? "error" : page ? "ok" : "loading",
      filters: Object.freeze({ ...filters }),
      runtime_status: page?.runtimeStatus ?? "unavailable",
      summary: page?.summary ?? Object.freeze({
        high_risk_count: 0,
        frozen_count: 0,
        resolved_count: 0,
      }),
      session_risks: page?.sessionRisks ?? Object.freeze([]),
      request_id: page?.requestId ?? loadError?.requestId ?? "-",
      trace_id: page?.traceId ?? "-",
      canPrevious: cursorStack.length > 0,
      canNext: Boolean(page?.nextCursor),
      selectedRisk,
      canManage: ownerCanManage,
      loadError: loadError ? Object.freeze({ ...loadError }) : null,
      pageError: pageError ? Object.freeze({ ...pageError }) : null,
      feedback: feedback ? Object.freeze({ ...feedback }) : null,
      actionDialog: actionDialog
        ? Object.freeze({
            open: true,
            action: actionDialog.action,
            reasonCode: actionDialog.reasonCode,
            submitting: actionDialog.submitting,
            error: actionDialog.error ? Object.freeze({ ...actionDialog.error }) : null,
          })
        : null,
    });
  }

  /** 在当前响应页内查找 score，绝不读取页外缓存。 */
  function findCurrentRisk(sessionRiskScoreId) {
    return page?.sessionRisks?.find(
      (item) => item.session_risk_score_id === sessionRiskScoreId,
    ) ?? null;
  }

  /** 取消列表 GET 并推进 revision；动作 POST 使用独立 owner。 */
  function abortListOperation() {
    listOperation?.controller.abort();
    listOperation = null;
    listRevision += 1;
  }

  /** 取消动作 POST 并推进 revision；用于离页、筛选和身份变化。 */
  function abortActionOperation() {
    actionOperation?.controller.abort();
    actionOperation = null;
    actionRevision += 1;
  }

  /** 同时终止 GET/POST，使忽略 AbortSignal 的迟到 Promise 也无法通过 revision 门禁。 */
  function invalidateOperations() {
    abortListOperation();
    abortActionOperation();
  }

  /** 重置 owner 下全部业务内存；调用方随后决定是否绑定新 owner。 */
  function resetAll() {
    invalidateOperations();
    filters = { ...DEFAULT_FILTERS };
    cursor = null;
    cursorStack = [];
    page = null;
    loadError = null;
    selectedRiskId = null;
    actionDialog = null;
    feedback = null;
    pageError = null;
  }

  return Object.freeze({
    enter,
    leave,
    clear,
    query,
    setFilters,
    resetFilters,
    beginListOperation,
    isListOperationCurrent,
    acceptPage,
    rejectPage,
    finishListOperation,
    nextPage,
    previousPage,
    selectRisk,
    closeDetail,
    openAction,
    beginAction,
    beginActionOperation,
    isActionOperationCurrent,
    acceptAction,
    rejectAction,
    finishActionOperation,
    closeAction,
    isActionBusy,
    snapshot,
  });
}

/** 识别由页面生命周期主动触发的 Abort，不把正常取消展示为依赖故障。 */
export function isSessionRiskAbortError(error) {
  return error?.name === "AbortError";
}

/** 把完整 owner 转为仅供闭包等值比较的内存键。 */
function keyFromOwner(owner) {
  if (
    !owner?.identity
    || !owner?.tenantId
    || owner?.navigation !== NAVIGATION_ID
    || !owner?.navigationOwner
  ) return "";
  return JSON.stringify([
    owner.identity,
    owner.tenantId,
    owner.navigation,
    owner.navigationOwner,
    owner.canManage === true,
  ]);
}

/** 读取 user/tenant 双镜像，二者不一致时失败关闭。 */
function mirroredTenantId(authContext) {
  const userTenantId = String(authContext?.user?.tenant_id ?? "").trim();
  const tenantId = String(authContext?.tenant?.tenant_id ?? "").trim();
  // user/tenant 双镜像冲突时不能任选其一，否则会把上一租户状态带入新 owner。
  if (userTenantId && tenantId && userTenantId !== tenantId) return "";
  // 兼容旧登录响应只缺一个镜像，但两个都缺失时仍返回空 owner。
  return userTenantId || tenantId;
}

/** 规范后端权限数组；重复、非法或空集合都拒绝。 */
function normalizeRequiredPermissions(value) {
  if (!Array.isArray(value) || !value.length) return [];
  const normalized = value.map((item) => String(item ?? "").trim());
  if (
    normalized.some((item) => !/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(item))
    || new Set(normalized).size !== normalized.length
  ) return [];
  return normalized.sort();
}

/** 把筛选收敛为固定字段和枚举；非法 DOM 注入不会进入 API URL。 */
function normalizeFilters(values = {}) {
  const limit = Number.isSafeInteger(values.limit) && values.limit >= 1 && values.limit <= 100
    ? values.limit
    : DEFAULT_FILTERS.limit;
  const virtualKeyId = String(values.virtualKeyId ?? "").trim();
  return {
    status: normalizeEnum(values.status, STATUS_VALUES),
    scopeType: normalizeEnum(values.scopeType, SCOPE_VALUES),
    virtualKeyId: VIRTUAL_KEY_ID_PATTERN.test(virtualKeyId) ? virtualKeyId : "",
    fromTime: normalizeIsoTime(values.fromTime),
    toTime: normalizeIsoTime(values.toTime),
    limit,
  };
}

/** 把可空枚举限制在固定集合，未知值收敛为空筛选。 */
function normalizeEnum(value, allowedValues) {
  const normalized = String(value ?? "").trim();
  return allowedValues.has(normalized) ? normalized : "";
}

/** 只保留规范 UTC ISO 时间；空值表示不筛选。 */
function normalizeIsoTime(value) {
  const normalized = String(value ?? "").trim();
  if (!normalized) return "";
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(normalized)
    && !Number.isNaN(Date.parse(normalized))
    ? normalized
    : "";
}

/** 构造只含固定公开字段的页面错误。 */
function fixedPageError(code, message, requestId) {
  return {
    code: safeErrorCode(code) || "session_risk.unavailable",
    message,
    requestId: safeId(requestId) || "-",
  };
}

/** 按错误码选择固定中文文案，不接纳服务端自由 message。 */
function fixedErrorMessage(code, fallback) {
  const messages = {
    "auth.permission_denied": "当前账号没有会话风险权限",
    "session_risk.query_invalid": "高危会话筛选不合法",
    "session_risk.not_found": "当前页面找不到该高危会话",
    "session_risk.payload_invalid": "风险处置请求不合法",
    "session_risk.idempotency_invalid": "风险处置幂等标识不合法",
    "session_risk.idempotency_conflict": "该处置标识已用于其他意图",
    "session_risk.state_conflict": "会话状态已变化，请刷新后重试",
    "session_risk.target_already_frozen": "该冻结目标已经生效",
    "session_risk.store_unavailable": "风险状态存储暂不可用",
    "session_risk.evidence_unavailable": "风险处置证据暂无法提交",
    "session_risk.response_invalid": "高危会话响应合同不完整",
  };
  return messages[code] ?? fallback;
}

/** 只允许统一错误码进入页面状态。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "").trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}

/** 只允许有界对象或 request_id 进入错误与反馈。 */
function safeId(value) {
  const normalized = String(value ?? "").trim();
  return /^[A-Za-z0-9_.:-]{1,160}$/.test(normalized) ? normalized : "";
}
