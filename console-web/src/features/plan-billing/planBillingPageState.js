/**
 * PAGE-110/111 的授权 owner、CAS 草稿、Dialog 私有意图与异步代次。
 *
 * 完整权限集合、Controller、调整 reason 和幂等键只驻留闭包；snapshot 只暴露当前页面
 * 可展示的套餐、额度、公开表单选择和固定错误。
 */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";
import {
  createQuotaAdjustmentIdempotencyKey,
  normalizePlanPatchRequest,
  normalizeQuotaAdjustmentRequest,
} from "../../lib/planBilling.js";

// 页面定义只声明后端权限码映射，角色名称或前端可见性不能替代授权事实。
const PAGE_DEFINITIONS = Object.freeze({
  plans: Object.freeze({
    read: PERMISSIONS.BILLING_PLAN_READ,
    write: PERMISSIONS.BILLING_PLAN_WRITE,
  }),
  "quota-buckets": Object.freeze({
    read: PERMISSIONS.BILLING_QUOTA_READ,
    write: PERMISSIONS.BILLING_QUOTA_WRITE,
  }),
});

// 固定错误文案收敛服务失败信息，避免把上游原始正文带入控制台页面。
const FIXED_MESSAGES = Object.freeze({
  "auth.permission_denied": "当前账号没有所需的套餐或额度权限",
  "plan.request_invalid": "套餐请求不合法",
  "plan.not_found": "当前租户没有套餐",
  "plan.revision_conflict": "套餐版本已变化，请重新读取",
  "plan.suspended": "当前套餐已暂停",
  "plan.unavailable": "套餐服务暂不可用",
  "plan.response_invalid": "套餐响应合同不完整",
  "quota.request_invalid": "额度请求不合法",
  "quota.adjustment_conflict": "额度已变化或当前余额不足",
  "quota.not_found": "当前租户没有对应额度桶",
  "quota.unavailable": "额度服务暂不可用",
  "quota.response_invalid": "额度响应合同不完整",
});

/** PAGE-110 读取身份；角色名不能补足后端权限事实。 */
export function planReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.BILLING_PLAN_READ);
}

/** PAGE-111 读取身份；额度写权限不隐含读取权限。 */
export function quotaReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.BILLING_QUOTA_READ);
}

/** 构造 PAGE-110 完整 owner，写模式还要求实时 plan.write。 */
export function planPageOwner(authContext, consoleShellState, { mode = "read" } = {}) {
  return billingPageOwner(authContext, consoleShellState, "plans", mode);
}

/** 构造 PAGE-111 完整 owner，写模式还要求实时 quota.write。 */
export function quotaPageOwner(authContext, consoleShellState, { mode = "read" } = {}) {
  return billingPageOwner(authContext, consoleShellState, "quota-buckets", mode);
}

/** 校验认证、租户、权限与导航镜像，全部一致时才建立页面所有者。 */
function billingPageOwner(authContext, consoleShellState, page, mode) {
  const definition = PAGE_DEFINITIONS[page];
  if (!definition || !["read", "write"].includes(mode)) return null;
  const readIdentity = authorizationIdentitySnapshot(authContext, definition.read);
  const writeIdentity = hasPermission(authContext, definition.write)
    ? authorizationIdentitySnapshot(authContext, definition.write)
    : "";
  const tenantId = mirroredTenantId(authContext);
  const authPermissions = permissionCodesFromAuthContext(authContext);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === page)
    : null;
  const required = normalizePermissionCodes(item?.required_permissions);
  const navigationPermissions = normalizePermissionCodes(navigation?.permission_codes);
  if (
    !readIdentity
    || !tenantId
    || (mode === "write" && !writeIdentity)
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== page
    || navigation?.active_navigation_item_id !== page
    || item?.target_section !== page
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([definition.read])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(authPermissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    page,
    tenantId,
    readIdentity,
    writeIdentity,
    canWrite: Boolean(writeIdentity),
    permissionCodes: Object.freeze([...authPermissions]),
    navigationOwner: JSON.stringify([
      item.id,
      item.target_section,
      required,
      authPermissions,
      revision,
      fingerprint,
    ]),
  });
}

/** 创建套餐读取与保存双通道状态机，并隔离确认框草稿和异步操作代次。 */
export function createPlanPageState({ clearDom = () => {} } = {}) {
  let ownerKey = "";
  let canWrite = false;
  let loadState = "loading";
  let loadError = null;
  let plan = null;
  let buckets = Object.freeze([]);
  let baseline = null;
  let draft = null;
  let dirty = false;
  let conflict = false;
  let confirmOpen = false;
  let saveState = "idle";
  let saveError = null;
  let feedback = null;
  let generations = { load: 0, save: 0 };
  let operations = { load: null, save: null };

  /** 进入套餐所有者作用域，身份漂移或权限收窄时清除旧状态。 */
  function enter(owner) {
    const key = ownerKeyFor(owner, "plans");
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    canWrite = owner.canWrite;
    if (!canWrite) clearMutation();
    return true;
  }

  /** 清空套餐页面公开与私有状态，并中止所有仍在运行的操作。 */
  function clear() {
    const pending = Object.values(operations).filter(Boolean);
    ownerKey = "";
    canWrite = false;
    loadState = "loading";
    loadError = null;
    plan = null;
    buckets = Object.freeze([]);
    baseline = null;
    draft = null;
    dirty = false;
    conflict = false;
    confirmOpen = false;
    saveState = "idle";
    saveError = null;
    feedback = null;
    operations = { load: null, save: null };
    generations = { load: generations.load + 1, save: generations.save + 1 };
    try {
      clearDom();
    } finally {
      for (const operation of pending) operation.controller.abort();
    }
  }

  /** 返回套餐页面只读快照，不暴露控制器或内部操作代次。 */
  function snapshot() {
    return Object.freeze({
      state: loadState,
      error: loadError,
      plan,
      quotaBuckets: buckets,
      draft: draft ? Object.freeze({ ...draft }) : null,
      dirty,
      conflict,
      confirmOpen,
      saveState,
      saveError,
      feedback,
      canWrite,
    });
  }

  /** 在当前所有者下启动套餐读取，受保护草稿除非重建基线不得覆盖。 */
  function beginLoad(owner, { rebase = false } = {}) {
    if (!ownerMatches(owner, "plans") || operations.save) return null;
    if (blocksShellDomReplacement() && !rebase) return null;
    if (!plan) loadState = "loading";
    loadError = null;
    return begin("load", owner, { rebase: rebase === true });
  }

  /** 仅接纳当前代次且租户一致的套餐响应，并重建编辑基线。 */
  function acceptLoad(operation, response, owner) {
    // 套餐读取必须同时匹配当前操作代次和认证租户，否则失败关闭。
    if (!isCurrent("load", operation, owner, "plans") || !validPlanResponse(response, owner.tenantId)) {
      return false;
    }
    plan = response.plan;
    buckets = Object.freeze([...response.quota_buckets]);
    baseline = planDraft(plan);
    draft = baseline;
    dirty = false;
    conflict = false;
    confirmOpen = false;
    saveState = "idle";
    saveError = null;
    loadState = "ready";
    loadError = null;
    return true;
  }

  /** 仅为当前套餐读取记录固定错误，保留仍受保护的本地草稿。 */
  function rejectLoad(operation, error, owner) {
    if (!isCurrent("load", operation, owner, "plans")) return false;
    loadState = "error";
    loadError = fixedError(error, "plan.unavailable");
    if (!dirty && !conflict) {
      plan = null;
      buckets = Object.freeze([]);
      baseline = null;
      draft = null;
    }
    return true;
  }

  /** 以严格请求合同替换套餐草稿，并依据当前基线计算脏状态。 */
  function replaceDraft(request) {
    if (!canWrite || operations.save || conflict || confirmOpen) return false;
    let normalized;
    try {
      normalized = normalizePlanPatchRequest(request);
    } catch {
      return false;
    }
    if (normalized.expected_revision !== (plan?.state_revision ?? 0)) return false;
    draft = normalized;
    dirty = !baseline || JSON.stringify(draft) !== JSON.stringify(baseline);
    saveState = "idle";
    saveError = null;
    feedback = null;
    return true;
  }

  /** 当前可写所有者提交有效脏草稿时打开套餐确认对话框。 */
  function openConfirmation(owner, request) {
    if (!ownerMatches(owner, "plans") || !canWrite || operations.save || conflict) return false;
    if (!replaceDraft(request) || !dirty) return false;
    confirmOpen = true;
    return true;
  }

  /** 仅允许当前所有者在没有保存操作时关闭套餐确认对话框。 */
  function closeConfirmation(owner) {
    if (!ownerMatches(owner, "plans") || operations.save || !confirmOpen) return false;
    confirmOpen = false;
    return true;
  }

  /** 放弃当前套餐草稿并恢复已验证基线，不发起任何网络请求。 */
  function discardDraft(owner) {
    if (!ownerMatches(owner, "plans") || !canWrite || operations.save || confirmOpen) return false;
    draft = baseline;
    dirty = false;
    conflict = false;
    saveState = "idle";
    saveError = null;
    feedback = null;
    return true;
  }

  /** 冻结当前套餐草稿并启动保存代次，前置条件不足时拒绝提交。 */
  function beginSave(owner) {
    if (
      !ownerMatches(owner, "plans")
      || !canWrite
      || !confirmOpen
      || !dirty
      || !draft
      || conflict
      || operations.save
    ) return null;
    saveState = "saving";
    saveError = null;
    feedback = null;
    return begin("save", owner, { request: draft });
  }

  /** 仅接纳当前保存代次及同租户响应，并提交新的套餐基线。 */
  function acceptSave(operation, response, owner) {
    // 套餐保存响应必须同时匹配当前操作代次和认证租户，拒绝陈旧或串租户结果。
    if (!isCurrent("save", operation, owner, "plans") || !validPlanResponse(response, owner.tenantId)) {
      return false;
    }
    plan = response.plan;
    buckets = Object.freeze([...response.quota_buckets]);
    baseline = planDraft(plan);
    draft = baseline;
    dirty = false;
    conflict = false;
    confirmOpen = false;
    saveState = "saved";
    saveError = null;
    feedback = Object.freeze({
      message: response.trial_cycle_changed ? "套餐已保存，试用周期已更新" : "套餐已保存",
      requestId: response.request_id,
    });
    loadState = "ready";
    return true;
  }

  /** 将当前套餐保存失败收敛为固定状态，冲突和未知结果要求重读。 */
  function rejectSave(operation, error, owner) {
    if (!isCurrent("save", operation, owner, "plans")) return false;
    saveError = fixedError(error, "plan.unavailable");
    confirmOpen = false;
    saveState = [409, 503].includes(Number(error?.status)) ? "conflict" : "error";
    conflict = [409, 503].includes(Number(error?.status));
    return true;
  }

  /** 完成指定套餐读取代次，仅清理仍与当前操作相同的槽位。 */
  function finishLoad(operation) {
    finish("load", operation);
  }

  /** 完成指定套餐保存代次，仅清理仍与当前操作相同的槽位。 */
  function finishSave(operation) {
    finish("save", operation);
  }

  /** 判断套餐读取操作是否仍属于当前页面所有者和最新代次。 */
  function isLoadCurrent(operation, owner) {
    return isCurrent("load", operation, owner, "plans");
  }

  /** 判断套餐保存操作是否仍属于当前页面所有者和最新代次。 */
  function isSaveCurrent(operation, owner) {
    return isCurrent("save", operation, owner, "plans");
  }

  /** 判断草稿、冲突或保存操作是否禁止外壳替换套餐页面节点。 */
  function blocksShellDomReplacement() {
    return Boolean(dirty || conflict || confirmOpen || operations.save);
  }

  /** 清除套餐变更私有状态并中止保存，但保留最近已验证事实。 */
  function clearMutation() {
    const pending = operations.save;
    operations.save = null;
    generations.save += 1;
    baseline = planDraft(plan);
    draft = baseline;
    dirty = false;
    conflict = false;
    confirmOpen = false;
    saveState = "idle";
    saveError = null;
    feedback = null;
    pending?.controller.abort();
  }

  /** 为套餐操作分配新代次与中止控制器，并替换同通道旧操作。 */
  function begin(lane, owner, extras = {}) {
    operations[lane]?.controller.abort();
    generations[lane] += 1;
    const operation = Object.freeze({
      lane,
      generation: generations[lane],
      ownerKey,
      controller: new AbortController(),
      ...extras,
    });
    operations[lane] = operation;
    return operation;
  }

  /** 核对套餐操作对象、代次和完整所有者键，防止接纳陈旧响应。 */
  function isCurrent(lane, operation, owner, page) {
    return Boolean(
      operation
      && operations[lane] === operation
      && operation.generation === generations[lane]
      && operation.ownerKey === ownerKey
      && ownerKeyFor(owner, page) === ownerKey,
    );
  }

  /** 仅在操作对象仍占用对应通道时完成套餐操作。 */
  function finish(lane, operation) {
    if (operations[lane] === operation) operations[lane] = null;
  }

  /** 比较套餐页面完整所有者键，避免只按租户或页面名称放行。 */
  function ownerMatches(owner, page) {
    return Boolean(ownerKey && ownerKeyFor(owner, page) === ownerKey);
  }

  return Object.freeze({
    enter,
    clear,
    snapshot,
    beginLoad,
    acceptLoad,
    rejectLoad,
    finishLoad,
    isLoadCurrent,
    replaceDraft,
    openConfirmation,
    closeConfirmation,
    discardDraft,
    beginSave,
    acceptSave,
    rejectSave,
    finishSave,
    isSaveCurrent,
    blocksShellDomReplacement,
  });
}

/** 创建额度读取与调整双通道状态机，私有原因和幂等键绝不进入公开快照。 */
export function createQuotaPageState({
  clearDom = () => {},
  createIdempotencyKey = createQuotaAdjustmentIdempotencyKey,
} = {}) {
  let ownerKey = "";
  let canWrite = false;
  let loadState = "loading";
  let loadError = null;
  let plan = null;
  let buckets = Object.freeze([]);
  let overage = Object.freeze({ amount: "0.000000", settlement_count: 0 });
  let adjustments = Object.freeze([]);
  let dialog = { open: false, quotaType: "paid", direction: "credit", amount: "" };
  let adjustmentState = "idle";
  let adjustmentError = null;
  let feedback = null;
  let privateIntent = null;
  let privateIdempotencyKey = "";
  let generations = { load: 0, adjustment: 0 };
  let operations = { load: null, adjustment: null };

  /** 进入额度所有者作用域，身份漂移或权限收窄时擦除私有意图。 */
  function enter(owner) {
    const key = ownerKeyFor(owner, "quota-buckets");
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    canWrite = owner.canWrite;
    if (!canWrite) clearDialog();
    return true;
  }

  /** 清空额度页面状态与私有意图，并中止所有仍在运行的操作。 */
  function clear() {
    const pending = Object.values(operations).filter(Boolean);
    ownerKey = "";
    canWrite = false;
    loadState = "loading";
    loadError = null;
    plan = null;
    buckets = Object.freeze([]);
    overage = Object.freeze({ amount: "0.000000", settlement_count: 0 });
    adjustments = Object.freeze([]);
    dialog = { open: false, quotaType: "paid", direction: "credit", amount: "" };
    adjustmentState = "idle";
    adjustmentError = null;
    feedback = null;
    privateIntent = null;
    privateIdempotencyKey = "";
    operations = { load: null, adjustment: null };
    generations = { load: generations.load + 1, adjustment: generations.adjustment + 1 };
    try {
      clearDom();
    } finally {
      for (const operation of pending) operation.controller.abort();
    }
  }

  /** 返回额度页面只读快照，明确排除原因、幂等键和控制器。 */
  function snapshot() {
    return Object.freeze({
      state: loadState,
      error: loadError,
      plan,
      quotaBuckets: buckets,
      overage,
      recentAdjustments: adjustments,
      dialog: Object.freeze({ ...dialog }),
      adjustmentState,
      adjustmentError,
      feedback,
      canWrite,
    });
  }

  /** 在当前所有者且没有调整对话框时启动额度读取代次。 */
  function beginLoad(owner) {
    if (!ownerMatches(owner) || operations.adjustment || dialog.open) return null;
    if (!plan) loadState = "loading";
    loadError = null;
    return begin("load", {});
  }

  /** 仅接纳当前代次且租户一致的额度响应，并替换公开事实。 */
  function acceptLoad(operation, response, owner) {
    // 额度读取必须同时匹配当前操作代次和认证租户，否则失败关闭。
    if (!isCurrent("load", operation, owner) || !validQuotaResponse(response, owner.tenantId)) {
      return false;
    }
    plan = response.plan;
    buckets = Object.freeze([...response.quota_buckets]);
    overage = response.overage;
    adjustments = Object.freeze([...response.recent_adjustments]);
    loadState = "ready";
    loadError = null;
    return true;
  }

  /** 仅为当前额度读取记录固定错误，打开的调整对话框保持隔离。 */
  function rejectLoad(operation, error, owner) {
    if (!isCurrent("load", operation, owner)) return false;
    loadState = "error";
    loadError = fixedError(error, "quota.unavailable");
    if (!dialog.open) {
      plan = null;
      buckets = Object.freeze([]);
      adjustments = Object.freeze([]);
    }
    return true;
  }

  /** 为当前可写所有者打开指定额度桶的调整对话框。 */
  function openAdjustment(owner, quotaType = "paid") {
    if (
      !ownerMatches(owner)
      || !canWrite
      || operations.adjustment
      || !buckets.some((item) => item.quota_type === quotaType)
    ) return false;
    clearPrivateIntent();
    dialog = { open: true, quotaType, direction: "credit", amount: "" };
    adjustmentState = "idle";
    adjustmentError = null;
    feedback = null;
    return true;
  }

  /** 仅在没有提交操作时关闭额度调整框并销毁私有意图。 */
  function closeAdjustment(owner) {
    if (!ownerMatches(owner) || operations.adjustment || !dialog.open) return false;
    clearDialog();
    return true;
  }

  /** 启动额度调整或同意图重试，原因与幂等键仅驻留闭包。 */
  function beginAdjustment(owner, request = null, { retry = false } = {}) {
    if (!ownerMatches(owner) || !canWrite || !dialog.open || operations.adjustment) return null;
    let normalized;
    let idempotencyKey;
    if (retry) {
      if (adjustmentState !== "unknown_result" || !privateIntent || !privateIdempotencyKey) return null;
      normalized = privateIntent;
      idempotencyKey = privateIdempotencyKey;
    } else {
      try {
        normalized = normalizeQuotaAdjustmentRequest(request);
      } catch {
        return null;
      }
      const bucket = buckets.find((item) => item.quota_type === normalized.quota_type);
      if (!bucket || normalized.expected_bucket_revision !== bucket.state_revision) return null;
      idempotencyKey = createIdempotencyKey();
      privateIntent = normalized;
      privateIdempotencyKey = idempotencyKey;
    }
    dialog = {
      open: true,
      quotaType: normalized.quota_type,
      direction: normalized.direction,
      amount: normalized.amount,
    };
    adjustmentState = "submitting";
    adjustmentError = null;
    feedback = null;
    return begin("adjustment", { request: normalized, idempotencyKey });
  }

  /** 仅接纳当前代次和同租户调整响应，再更新对应额度桶。 */
  function acceptAdjustment(operation, response, owner) {
    if (
      !isCurrent("adjustment", operation, owner)
      || response?.plan?.tenant_id !== owner.tenantId
      || response?.quota_bucket?.tenant_id !== owner.tenantId
      || response?.adjustment?.tenant_id !== owner.tenantId
    ) return false;
    plan = response.plan;
    buckets = Object.freeze(buckets.map((item) => (
      item.quota_type === response.quota_bucket.quota_type ? response.quota_bucket : item
    )));
    adjustments = Object.freeze([
      response.adjustment,
      ...adjustments.filter((item) => item.quota_adjustment_id !== response.adjustment.quota_adjustment_id),
    ].slice(0, 50));
    adjustmentState = "saved";
    adjustmentError = null;
    feedback = Object.freeze({
      message: response.idempotent_replay ? "已恢复同一额度调整" : "额度调整已提交",
      requestId: response.request_id,
    });
    clearPrivateIntent();
    dialog = { open: false, quotaType: "paid", direction: "credit", amount: "" };
    return true;
  }

  /** 收敛当前额度调整失败；未知结果保留同一私有意图供重试。 */
  function rejectAdjustment(operation, error, owner) {
    if (!isCurrent("adjustment", operation, owner)) return false;
    adjustmentError = fixedError(error, "quota.unavailable");
    // 服务端错误代表提交结果未知，必须保留原意图和幂等键以便同一请求重试。
    if (Number(error?.status) >= 500) {
      adjustmentState = "unknown_result";
    } else if (Number(error?.status) === 409) {
      adjustmentState = "error";
      feedback = Object.freeze({
        message: adjustmentError.message,
        requestId: adjustmentError.requestId,
        error: true,
      });
      dialog = { open: false, quotaType: "paid", direction: "credit", amount: "" };
      clearPrivateIntent();
    } else {
      adjustmentState = "error";
      clearPrivateIntent();
    }
    return true;
  }

  /** 完成指定额度读取代次，仅清理仍与当前操作相同的槽位。 */
  function finishLoad(operation) {
    finish("load", operation);
  }

  /** 完成指定额度调整代次，仅清理仍与当前操作相同的槽位。 */
  function finishAdjustment(operation) {
    finish("adjustment", operation);
  }

  /** 判断额度读取操作是否仍属于当前页面所有者和最新代次。 */
  function isLoadCurrent(operation, owner) {
    return isCurrent("load", operation, owner);
  }

  /** 判断额度调整操作是否仍属于当前页面所有者和最新代次。 */
  function isAdjustmentCurrent(operation, owner) {
    return isCurrent("adjustment", operation, owner);
  }

  /** 判断打开的调整框或提交操作是否禁止外壳替换额度页面节点。 */
  function blocksShellDomReplacement() {
    return Boolean(dialog.open || operations.adjustment);
  }

  /** 擦除额度原因与幂等键，确保它们不会残留到后续对话框。 */
  function clearPrivateIntent() {
    privateIntent = null;
    privateIdempotencyKey = "";
  }

  /** 关闭额度对话框、取消在途调整并清除全部私有提交状态。 */
  function clearDialog() {
    const pending = operations.adjustment;
    operations.adjustment = null;
    generations.adjustment += 1;
    dialog = { open: false, quotaType: "paid", direction: "credit", amount: "" };
    adjustmentState = "idle";
    adjustmentError = null;
    clearPrivateIntent();
    pending?.controller.abort();
  }

  /** 为额度操作分配新代次与中止控制器，并替换同通道旧操作。 */
  function begin(lane, extras) {
    operations[lane]?.controller.abort();
    generations[lane] += 1;
    const operation = Object.freeze({
      lane,
      generation: generations[lane],
      ownerKey,
      controller: new AbortController(),
      ...extras,
    });
    operations[lane] = operation;
    return operation;
  }

  /** 核对额度操作对象、代次和完整所有者键，防止接纳陈旧响应。 */
  function isCurrent(lane, operation, owner) {
    return Boolean(
      operation
      && operations[lane] === operation
      && operation.generation === generations[lane]
      && operation.ownerKey === ownerKey
      && ownerKeyFor(owner, "quota-buckets") === ownerKey,
    );
  }

  /** 仅在操作对象仍占用对应通道时完成额度操作。 */
  function finish(lane, operation) {
    if (operations[lane] === operation) operations[lane] = null;
  }

  /** 比较额度页面完整所有者键，避免只按租户或页面名称放行。 */
  function ownerMatches(owner) {
    return Boolean(ownerKey && ownerKeyFor(owner, "quota-buckets") === ownerKey);
  }

  return Object.freeze({
    enter,
    clear,
    snapshot,
    beginLoad,
    acceptLoad,
    rejectLoad,
    finishLoad,
    isLoadCurrent,
    openAdjustment,
    closeAdjustment,
    beginAdjustment,
    acceptAdjustment,
    rejectAdjustment,
    finishAdjustment,
    isAdjustmentCurrent,
    blocksShellDomReplacement,
  });
}

/** 判断套餐额度请求是否由主动中止结束，调用方据此忽略陈旧结果。 */
export function isPlanBillingAbortError(error) {
  return error?.name === "AbortError";
}

/** 从已验证套餐事实构造完整 CAS 草稿，不携带响应额外字段。 */
function planDraft(plan) {
  return Object.freeze({
    expected_revision: plan?.state_revision ?? 0,
    display_name: plan?.display_name ?? "",
    base_tier: plan?.base_tier ?? "free",
    status: plan?.status ?? "active",
    trial_ends_at: plan?.trial_status === "active" ? plan.trial_ends_at : null,
  });
}

/** 校验套餐响应及其额度桶均属于请求认证租户。 */
function validPlanResponse(response, tenantId) {
  return Boolean(
    response
    && (response.plan === null || response.plan?.tenant_id === tenantId)
    && Array.isArray(response.quota_buckets)
    && response.quota_buckets.every((item) => item?.tenant_id === tenantId),
  );
}

/** 校验额度响应的套餐、桶和调整记录均属于请求认证租户。 */
function validQuotaResponse(response, tenantId) {
  return Boolean(
    validPlanResponse(response, tenantId)
    && response.overage
    && Array.isArray(response.recent_adjustments)
    && response.recent_adjustments.every((item) => item?.tenant_id === tenantId),
  );
}

/** 将完整页面所有者事实编码为稳定键，缺少任一边界字段即拒绝。 */
function ownerKeyFor(owner, page) {
  if (
    !owner
    || owner.page !== page
    || !owner.tenantId
    || !owner.readIdentity
    || !owner.navigationOwner
    || !Array.isArray(owner.permissionCodes)
  ) return "";
  return JSON.stringify([
    owner.page,
    owner.tenantId,
    owner.readIdentity,
    owner.writeIdentity,
    owner.navigationOwner,
    owner.permissionCodes,
  ]);
}

/** 读取认证上下文的双份租户镜像，二者冲突时失败关闭。 */
function mirroredTenantId(authContext) {
  const user = String(authContext?.user?.tenant_id ?? "").trim();
  const tenant = String(authContext?.tenant?.tenant_id ?? "").trim();
  // 用户与租户声明必须镜像一致，任何跨租户漂移都拒绝建立页面所有者。
  if (user && tenant && user !== tenant) return "";
  const value = user || tenant;
  return /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value) ? value : "";
}

/** 规范化权限码集合并拒绝非法格式或重复项，供镜像精确比较。 */
function normalizePermissionCodes(values) {
  if (!Array.isArray(values)) return null;
  const normalized = values.map((value) => String(value ?? ""));
  if (
    normalized.some((value) => !/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(value))
    || new Set(normalized).size !== normalized.length
  ) return null;
  return [...normalized].sort();
}

/** 将未知服务错误收敛为允许展示的固定文案和安全请求标识。 */
function fixedError(error, fallbackCode) {
  const code = Object.hasOwn(FIXED_MESSAGES, error?.code) ? error.code : fallbackCode;
  const requestId = /^[A-Za-z0-9_.:-]{1,160}$/.test(String(error?.requestId ?? ""))
    ? error.requestId
    : "-";
  return Object.freeze({ code, message: FIXED_MESSAGES[code], requestId });
}
