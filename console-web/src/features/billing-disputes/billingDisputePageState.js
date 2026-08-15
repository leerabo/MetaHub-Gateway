/** PAGE-112 authorization owner, private text vault and async generation state. */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";
import {
  createBillingDisputeIdempotencyKey,
  normalizeBillingDisputeCreateRequest,
  normalizeBillingDisputeListFilters,
  normalizeBillingDisputeTransitionRequest,
} from "../../lib/billingDisputes.js";

const PAGE_ID = "billing-disputes";
const DISPUTE_ID = /^bdsp_[0-9a-f]{32}$/;
const ITEM_ID = /^bdsi_[0-9a-f]{32}$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const STATUSES = new Set(["submitted", "under_review", "approved", "rejected", "cancelled"]);
const SCOPES = new Set(["request_ids", "time_range"]);
const REVIEW_ACTIONS = new Set(["start_review", "approve", "reject"]);

const FIXED_MESSAGES = Object.freeze({
  "auth.permission_denied": "当前账号没有所需的账单争议权限",
  "billing_dispute.query_invalid": "账单争议查询不合法",
  "billing_dispute.request_invalid": "账单争议请求不合法",
  "billing_dispute.not_found": "账单争议不存在",
  "billing_dispute.scope_not_found": "没有可争议的计费记录",
  "billing_dispute.scope_too_large": "争议范围超过限制",
  "billing_dispute.hold_conflict": "计费记录已被其他争议暂缓",
  "billing_dispute.revision_conflict": "争议状态已变化，请重新读取",
  "billing_dispute.transition_invalid": "当前状态不允许该操作",
  "billing_dispute.self_review_forbidden": "申请人不能审核自己的争议",
  "billing_dispute.adjustment_invalid": "批准金额不合法",
  "billing_dispute.adjustment_conflict": "可调整金额已变化",
  "billing_dispute.idempotency_conflict": "幂等键已用于其他争议意图",
  "billing_dispute.integrity_failed": "账单争议数据完整性校验失败",
  "billing_dispute.unavailable": "账单争议服务暂不可用",
  "billing_dispute.response_invalid": "账单争议响应合同不完整",
});

/** Read identity used by refresh/login boundaries. */
export function billingDisputeReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.BILLING_DISPUTE_READ);
}

/**
 * Build a PAGE-112 owner from mirrored auth and backend navigation facts.
 * mode=approve additionally requires billing.finops.write.
 */
export function billingDisputePageOwner(authContext, consoleShellState, { mode = "read" } = {}) {
  if (!["read", "create", "review", "approve"].includes(mode)) return null;
  const readIdentity = authorizationIdentitySnapshot(authContext, PERMISSIONS.BILLING_DISPUTE_READ);
  const createIdentity = hasPermission(authContext, PERMISSIONS.BILLING_DISPUTE_CREATE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.BILLING_DISPUTE_CREATE)
    : "";
  const reviewIdentity = hasPermission(authContext, PERMISSIONS.BILLING_DISPUTE_REVIEW)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.BILLING_DISPUTE_REVIEW)
    : "";
  const finOpsWriteIdentity = hasPermission(authContext, PERMISSIONS.BILLING_FINOPS_WRITE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.BILLING_FINOPS_WRITE)
    : "";
  const tenantId = mirroredTenantId(authContext);
  const userId = String(authContext?.user?.user_id ?? "").trim();
  const permissionCodes = permissionCodesFromAuthContext(authContext);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === PAGE_ID)
    : null;
  const required = normalizePermissionCodes(item?.required_permissions);
  const navigationPermissions = normalizePermissionCodes(navigation?.permission_codes);
  if (
    !readIdentity
    || !tenantId
    || !SAFE_ID.test(userId)
    || (mode === "create" && !createIdentity)
    || (["review", "approve"].includes(mode) && !reviewIdentity)
    || (mode === "approve" && !finOpsWriteIdentity)
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== PAGE_ID
    || navigation?.active_navigation_item_id !== PAGE_ID
    || item?.target_section !== PAGE_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.BILLING_DISPUTE_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(permissionCodes)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    page: PAGE_ID,
    tenantId,
    userId,
    readIdentity,
    createIdentity,
    reviewIdentity,
    finOpsWriteIdentity,
    canCreate: Boolean(createIdentity),
    canReview: Boolean(reviewIdentity),
    canApprove: Boolean(reviewIdentity && finOpsWriteIdentity),
    permissionCodes: Object.freeze([...permissionCodes]),
    navigationOwner: JSON.stringify([
      item.id,
      item.target_section,
      required,
      permissionCodes,
      revision,
      fingerprint,
    ]),
  });
}

/** Create the PAGE-112 list/detail/write state while keeping free text and keys private. */
export function createBillingDisputePageState({
  clearDom = () => {},
  createIdempotencyKey = createBillingDisputeIdempotencyKey,
} = {}) {
  let ownerKey = "";
  let capabilities = emptyCapabilities();
  let listState = "loading";
  let listError = null;
  let disputes = Object.freeze([]);
  let nextCursor = null;
  let filters = normalizeBillingDisputeListFilters();
  let selectedId = "";
  let detailState = "idle";
  let detailError = null;
  let detail = null;
  let privateText = { reason: "", decisionReason: "" };
  let revealed = { reason: false, decisionReason: false };
  let dialog = closedDialog();
  let mutationState = "idle";
  let mutationError = null;
  let feedback = null;
  let privateIntent = null;
  let privateIdempotencyKey = "";
  let generations = { list: 0, detail: 0, mutation: 0 };
  let operations = { list: null, detail: null, mutation: null };

  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    capabilities = Object.freeze({
      canCreate: owner.canCreate,
      canReview: owner.canReview,
      canApprove: owner.canApprove,
      userId: owner.userId,
    });
    if (!capabilities.canCreate && dialog.kind === "create") clearDialog();
    if (!capabilities.canReview && REVIEW_ACTIONS.has(dialog.action)) clearDialog();
    if (!capabilities.canApprove && dialog.action === "approve") clearDialog();
    return true;
  }

  function clear() {
    const pending = Object.values(operations).filter(Boolean);
    ownerKey = "";
    capabilities = emptyCapabilities();
    listState = "loading";
    listError = null;
    disputes = Object.freeze([]);
    nextCursor = null;
    filters = normalizeBillingDisputeListFilters();
    selectedId = "";
    detailState = "idle";
    detailError = null;
    detail = null;
    clearPrivateDetail();
    dialog = closedDialog();
    mutationState = "idle";
    mutationError = null;
    feedback = null;
    clearPrivateIntent();
    operations = { list: null, detail: null, mutation: null };
    generations = {
      list: generations.list + 1,
      detail: generations.detail + 1,
      mutation: generations.mutation + 1,
    };
    try {
      clearDom();
    } finally {
      for (const operation of pending) operation.controller.abort();
    }
  }

  function snapshot() {
    return Object.freeze({
      state: listState,
      error: listError,
      disputes,
      nextCursor,
      filters,
      selectedId,
      detailState,
      detailError,
      detail,
      privateTextAvailable: Object.freeze({
        reason: Boolean(privateText.reason),
        decisionReason: Boolean(privateText.decisionReason),
      }),
      revealed: Object.freeze({ ...revealed }),
      dialog: Object.freeze({ ...dialog }),
      mutationState,
      mutationError,
      feedback,
      ...capabilities,
    });
  }

  function setFilters(owner, value) {
    if (!ownerMatches(owner) || operations.list || dialog.open) return false;
    try {
      filters = normalizeBillingDisputeListFilters({ ...value, cursor: null });
    } catch {
      return false;
    }
    nextCursor = null;
    return true;
  }

  function useNextCursor(owner) {
    if (!ownerMatches(owner) || operations.list || !nextCursor || dialog.open) return false;
    filters = Object.freeze({ ...filters, cursor: nextCursor });
    return true;
  }

  function resetCursor(owner) {
    if (!ownerMatches(owner) || operations.list || dialog.open) return false;
    filters = Object.freeze({ ...filters, cursor: null });
    return true;
  }

  function beginList(owner) {
    if (!ownerMatches(owner) || operations.mutation || dialog.open) return null;
    if (!disputes.length) listState = "loading";
    listError = null;
    return begin("list", { filters });
  }

  function acceptList(operation, response, owner) {
    if (!isCurrent("list", operation, owner)) return false;
    const normalized = normalizeListResponse(response, owner.tenantId);
    if (!normalized) return false;
    disputes = normalized.disputes;
    nextCursor = normalized.nextCursor;
    listState = "ready";
    listError = null;
    if (selectedId && !disputes.some((item) => item.billing_dispute_id === selectedId)) {
      closeDetailInternal();
    }
    return true;
  }

  function rejectList(operation, error, owner) {
    if (!isCurrent("list", operation, owner)) return false;
    listState = "error";
    listError = fixedError(error);
    if (!detail) disputes = Object.freeze([]);
    return true;
  }

  function selectDetail(owner, billingDisputeId) {
    const id = String(billingDisputeId ?? "");
    if (!ownerMatches(owner) || !DISPUTE_ID.test(id) || dialog.open) return false;
    operations.detail?.controller.abort();
    operations.detail = null;
    generations.detail += 1;
    selectedId = id;
    detailState = "loading";
    detailError = null;
    detail = null;
    clearPrivateDetail();
    return true;
  }

  function closeDetail(owner) {
    if (!ownerMatches(owner) || operations.mutation || dialog.open) return false;
    closeDetailInternal();
    return true;
  }

  function beginDetail(owner) {
    if (!ownerMatches(owner) || !selectedId || operations.mutation) return null;
    detailState = detail ? "ready" : "loading";
    detailError = null;
    return begin("detail", { billingDisputeId: selectedId });
  }

  function acceptDetail(operation, response, owner) {
    if (!isCurrent("detail", operation, owner) || operation.billingDisputeId !== selectedId) return false;
    const normalized = normalizeDetailResponse(response, owner.tenantId);
    if (!normalized || normalized.detail.billing_dispute_id !== selectedId) return false;
    detail = normalized.detail;
    privateText = normalized.privateText;
    revealed = { reason: false, decisionReason: false };
    detailState = "ready";
    detailError = null;
    disputes = replaceSummary(disputes, detail);
    return true;
  }

  function rejectDetail(operation, error, owner) {
    if (!isCurrent("detail", operation, owner)) return false;
    detailState = "error";
    detailError = fixedError(error);
    detail = null;
    clearPrivateDetail();
    return true;
  }

  function revealText(owner, kind) {
    if (!ownerMatches(owner) || detailState !== "ready" || !["reason", "decisionReason"].includes(kind)) return false;
    if (!privateText[kind]) return false;
    revealed = { ...revealed, [kind]: true };
    return true;
  }

  function collapseText(owner, kind) {
    if (!ownerMatches(owner) || !["reason", "decisionReason"].includes(kind)) return false;
    revealed = { ...revealed, [kind]: false };
    return true;
  }

  function revealedText(owner) {
    if (!ownerMatches(owner)) return Object.freeze({ reason: "", decisionReason: "" });
    return Object.freeze({
      reason: revealed.reason ? privateText.reason : "",
      decisionReason: revealed.decisionReason ? privateText.decisionReason : "",
    });
  }

  function openCreate(owner, scopeType = "request_ids") {
    if (!ownerMatches(owner) || !capabilities.canCreate || !SCOPES.has(scopeType) || operations.mutation) return false;
    clearPrivateIntent();
    clearPrivateDetailReveal();
    dialog = Object.freeze({
      open: true,
      kind: "create",
      action: "",
      targetId: "",
      scopeType,
      expectedRevision: 0,
    });
    mutationState = "idle";
    mutationError = null;
    feedback = null;
    return true;
  }

  function openAction(owner, action) {
    if (!ownerMatches(owner) || !TRANSITION_ALLOWED(action, detail, capabilities)) return false;
    clearPrivateIntent();
    clearPrivateDetailReveal();
    dialog = Object.freeze({
      open: true,
      kind: "action",
      action,
      targetId: detail.billing_dispute_id,
      scopeType: "",
      expectedRevision: detail.state_revision,
    });
    mutationState = "idle";
    mutationError = null;
    feedback = null;
    return true;
  }

  function closeDialogForOwner(owner) {
    if (!ownerMatches(owner) || operations.mutation || !dialog.open) return false;
    clearDialog();
    return true;
  }

  function beginMutation(owner, request = null, { retry = false } = {}) {
    if (!ownerMatches(owner) || !dialog.open || operations.mutation) return null;
    let normalized;
    let idempotencyKey;
    if (retry) {
      if (mutationState !== "unknown_result" || !privateIntent || !privateIdempotencyKey) return null;
      normalized = privateIntent;
      idempotencyKey = privateIdempotencyKey;
    } else {
      try {
        normalized = dialog.kind === "create"
          ? normalizeBillingDisputeCreateRequest(request)
          : normalizeBillingDisputeTransitionRequest(request);
      } catch {
        return null;
      }
      if (!mutationAllowed(owner, normalized)) return null;
      idempotencyKey = createIdempotencyKey();
      privateIntent = normalized;
      privateIdempotencyKey = idempotencyKey;
    }
    mutationState = "submitting";
    mutationError = null;
    feedback = null;
    return begin("mutation", {
      kind: dialog.kind,
      billingDisputeId: dialog.targetId,
      request: normalized,
      idempotencyKey,
    });
  }

  function acceptMutation(operation, response, owner) {
    if (!isCurrent("mutation", operation, owner)) return false;
    const normalized = normalizeDetailResponse(response, owner.tenantId);
    if (!normalized) return false;
    if (operation.kind === "action" && normalized.detail.billing_dispute_id !== operation.billingDisputeId) {
      return false;
    }
    detail = normalized.detail;
    selectedId = detail.billing_dispute_id;
    privateText = normalized.privateText;
    revealed = { reason: false, decisionReason: false };
    detailState = "ready";
    detailError = null;
    disputes = replaceSummary(disputes, detail);
    mutationState = "saved";
    mutationError = null;
    feedback = Object.freeze({
      message: response?.idempotent_replay ? "已恢复同一账单争议操作" : "账单争议操作已提交",
      requestId: safeChain(response?.request_id),
    });
    dialog = closedDialog();
    clearPrivateIntent();
    return true;
  }

  function rejectMutation(operation, error, owner) {
    if (!isCurrent("mutation", operation, owner)) return false;
    mutationError = fixedError(error);
    if (Number(error?.status) >= 500) {
      mutationState = "unknown_result";
    } else {
      mutationState = "error";
      dialog = closedDialog();
      clearPrivateIntent();
      feedback = Object.freeze({
        message: mutationError.message,
        requestId: mutationError.requestId,
        error: true,
      });
    }
    return true;
  }

  function finishList(operation) { finish("list", operation); }
  function finishDetail(operation) { finish("detail", operation); }
  function finishMutation(operation) { finish("mutation", operation); }
  function isListCurrent(operation, owner) { return isCurrent("list", operation, owner); }
  function isDetailCurrent(operation, owner) { return isCurrent("detail", operation, owner); }
  function isMutationCurrent(operation, owner) { return isCurrent("mutation", operation, owner); }

  function blocksShellDomReplacement() {
    return Boolean(dialog.open || operations.mutation || revealed.reason || revealed.decisionReason);
  }

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

  function isCurrent(lane, operation, owner) {
    return Boolean(
      operation
      && operations[lane] === operation
      && operation.generation === generations[lane]
      && operation.ownerKey === ownerKey
      && ownerKeyFor(owner) === ownerKey,
    );
  }

  function finish(lane, operation) {
    if (operations[lane] === operation) operations[lane] = null;
  }

  function ownerMatches(owner) {
    return Boolean(ownerKey && ownerKeyFor(owner) === ownerKey);
  }

  function closeDetailInternal() {
    const pending = operations.detail;
    operations.detail = null;
    generations.detail += 1;
    selectedId = "";
    detailState = "idle";
    detailError = null;
    detail = null;
    clearPrivateDetail();
    pending?.controller.abort();
  }

  function clearPrivateDetail() {
    privateText = { reason: "", decisionReason: "" };
    revealed = { reason: false, decisionReason: false };
  }

  function clearPrivateDetailReveal() {
    revealed = { reason: false, decisionReason: false };
  }

  function clearPrivateIntent() {
    privateIntent = null;
    privateIdempotencyKey = "";
  }

  function clearDialog() {
    const pending = operations.mutation;
    operations.mutation = null;
    generations.mutation += 1;
    dialog = closedDialog();
    mutationState = "idle";
    mutationError = null;
    clearPrivateIntent();
    pending?.controller.abort();
  }

  function mutationAllowed(owner, request) {
    if (dialog.kind === "create") return owner.canCreate === true && SCOPES.has(request.scope_type);
    if (request.action !== dialog.action || request.expected_revision !== dialog.expectedRevision) return false;
    if (request.action === "cancel") return owner.canCreate === true && detail?.created_by_user_id === owner.userId;
    if (request.action === "approve") return owner.canApprove === true && detail?.created_by_user_id !== owner.userId;
    return owner.canReview === true && detail?.created_by_user_id !== owner.userId;
  }

  return Object.freeze({
    enter,
    clear,
    snapshot,
    setFilters,
    useNextCursor,
    resetCursor,
    beginList,
    acceptList,
    rejectList,
    finishList,
    isListCurrent,
    selectDetail,
    closeDetail,
    beginDetail,
    acceptDetail,
    rejectDetail,
    finishDetail,
    isDetailCurrent,
    revealText,
    collapseText,
    revealedText,
    openCreate,
    openAction,
    closeDialog: closeDialogForOwner,
    beginMutation,
    acceptMutation,
    rejectMutation,
    finishMutation,
    isMutationCurrent,
    blocksShellDomReplacement,
  });
}

function TRANSITION_ALLOWED(action, detail, capabilities) {
  if (!detail || !["start_review", "approve", "reject", "cancel"].includes(action)) return false;
  const self = detail.created_by_user_id === capabilities.userId;
  if (action === "cancel") return capabilities.canCreate && self && detail.status === "submitted";
  if (self || !capabilities.canReview) return false;
  if (action === "start_review") return detail.status === "submitted";
  if (action === "approve") return capabilities.canApprove && detail.status === "under_review";
  return detail.status === "under_review";
}

function normalizeListResponse(response, tenantId) {
  const values = response?.billing_disputes;
  if (!Array.isArray(values) || values.length > 100 || !safeChain(response?.request_id) || !safeChain(response?.trace_id)) {
    return null;
  }
  const normalized = values.map((value) => normalizeSummary(value, tenantId));
  if (normalized.some((value) => !value)) return null;
  if (new Set(normalized.map((value) => value.billing_dispute_id)).size !== normalized.length) return null;
  const nextCursor = response.next_cursor ?? null;
  if (!(nextCursor === null || (typeof nextCursor === "string" && nextCursor.length >= 1 && nextCursor.length <= 2048))) {
    return null;
  }
  return Object.freeze({ disputes: Object.freeze(normalized), nextCursor });
}

function normalizeDetailResponse(response, tenantId) {
  const container = response?.dispute;
  const head = container?.summary;
  const summary = normalizeSummary(head, tenantId);
  if (!summary || !safeChain(response?.request_id) || !safeChain(response?.trace_id)) return null;
  const reason = privateReason(container?.reason);
  const decisionReason = container?.decision_reason === null || container?.decision_reason === undefined
    ? ""
    : privateReason(container.decision_reason);
  if (!reason || ((container?.decision_reason !== null && container?.decision_reason !== undefined) && !decisionReason)) return null;
  const items = normalizeChildRecords(container?.items ?? [], "billing_dispute_item_id", ITEM_ID);
  const holds = normalizeChildRecords(container?.holds ?? []);
  const transitions = normalizeChildRecords(container?.transitions ?? []);
  const adjustmentLinks = normalizeChildRecords(container?.adjustment_links ?? []);
  if (!items || !holds || !transitions || !adjustmentLinks || items.length !== summary.item_count) return null;
  const detail = Object.freeze({
    ...summary,
    items,
    holds,
    transitions,
    adjustment_links: adjustmentLinks,
  });
  return Object.freeze({
    detail,
    privateText: Object.freeze({ reason, decisionReason }),
  });
}

function normalizeSummary(value, tenantId) {
  if (
    !value
    || typeof value !== "object"
    || Array.isArray(value)
    || !DISPUTE_ID.test(String(value.billing_dispute_id ?? ""))
    || value.tenant_id !== tenantId
    || !SCOPES.has(value.scope_type)
    || !STATUSES.has(value.status)
    || !Number.isSafeInteger(value.state_revision)
    || value.state_revision < 1
    || !Number.isSafeInteger(value.item_count)
    || value.item_count < 1
    || value.item_count > 100
    || !SAFE_ID.test(String(value.created_by?.user_id ?? ""))
    || !(value.reviewer === null || SAFE_ID.test(String(value.reviewer?.user_id ?? "")))
  ) return null;
  const safe = safePublicRecord(value);
  if (!safe) return null;
  return Object.freeze({
    ...safe,
    billing_dispute_id: value.billing_dispute_id,
    tenant_id: tenantId,
    scope_type: value.scope_type,
    status: value.status,
    state_revision: value.state_revision,
    item_count: value.item_count,
    created_by_user_id: value.created_by.user_id,
    reviewer_user_id: value.reviewer?.user_id ?? null,
  });
}

function normalizeChildRecords(values, idField = "", idPattern = null) {
  if (!Array.isArray(values) || values.length > 500) return null;
  const normalized = values.map((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    if (idField && !idPattern.test(String(value[idField] ?? ""))) return null;
    return safePublicRecord(value);
  });
  if (normalized.some((value) => !value)) return null;
  if (idField && new Set(normalized.map((value) => value[idField])).size !== normalized.length) return null;
  return Object.freeze(normalized.map((value) => Object.freeze(value)));
}

function safePublicRecord(value) {
  const forbidden = /(?:^|_)(?:reason|secret|token|authorization|prompt|response|payload|raw|normalized|idempotency)(?:_|$)/i;
  const output = {};
  for (const [key, item] of Object.entries(value)) {
    if (forbidden.test(key) || ["items", "holds", "transitions", "adjustment_links"].includes(key)) continue;
    if (item === null || ["string", "number", "boolean"].includes(typeof item)) {
      output[key] = item;
      continue;
    }
    if (plainSafeObject(item)) output[key] = Object.freeze({ ...item });
  }
  return output;
}

function plainSafeObject(value) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).length <= 10
    && Object.values(value).every((item) => item === null || ["string", "number", "boolean"].includes(typeof item)),
  );
}

function replaceSummary(values, detail) {
  const summary = normalizeSummary(detail, detail.tenant_id);
  if (!summary) return values;
  const next = [summary, ...values.filter((item) => item.billing_dispute_id !== summary.billing_dispute_id)];
  return Object.freeze(next.slice(0, 100));
}

function privateReason(value) {
  return typeof value === "string" && value.trim() && new TextEncoder().encode(value).byteLength <= 2000
    ? value
    : "";
}

function ownerKeyFor(owner) {
  if (
    !owner
    || owner.page !== PAGE_ID
    || !owner.tenantId
    || !owner.userId
    || !owner.readIdentity
    || !owner.navigationOwner
    || !Array.isArray(owner.permissionCodes)
  ) return "";
  return JSON.stringify([
    owner.page,
    owner.tenantId,
    owner.userId,
    owner.readIdentity,
    owner.createIdentity,
    owner.reviewIdentity,
    owner.finOpsWriteIdentity,
    owner.navigationOwner,
    owner.permissionCodes,
  ]);
}

function mirroredTenantId(authContext) {
  const user = String(authContext?.user?.tenant_id ?? "").trim();
  const tenant = String(authContext?.tenant?.tenant_id ?? "").trim();
  if (user && tenant && user !== tenant) return "";
  const value = user || tenant;
  return SAFE_ID.test(value) ? value : "";
}

function normalizePermissionCodes(values) {
  if (!Array.isArray(values)) return null;
  const normalized = values.map((value) => String(value ?? ""));
  if (
    normalized.some((value) => !/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(value))
    || new Set(normalized).size !== normalized.length
  ) return null;
  return [...normalized].sort();
}

function fixedError(error) {
  const code = Object.hasOwn(FIXED_MESSAGES, error?.code) ? error.code : "billing_dispute.unavailable";
  return Object.freeze({
    code,
    message: FIXED_MESSAGES[code],
    requestId: safeChain(error?.requestId),
    traceId: safeChain(error?.traceId),
  });
}

function safeChain(value) {
  return /^[A-Za-z0-9_.:-]{1,160}$/.test(String(value ?? "")) ? String(value) : "-";
}

function emptyCapabilities() {
  return Object.freeze({ canCreate: false, canReview: false, canApprove: false, userId: "" });
}

function closedDialog() {
  return Object.freeze({
    open: false,
    kind: "",
    action: "",
    targetId: "",
    scopeType: "request_ids",
    expectedRevision: 0,
  });
}
