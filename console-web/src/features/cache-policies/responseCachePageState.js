/**
 * PAGE-106 的授权 owner、三通道异步状态和私有写入票据。
 *
 * 安全边界：权限指纹、完整权限集合、AbortController、cursor 与幂等键只驻留闭包；
 * snapshot 只暴露当前已授权页面需要的 Alias、策略配置、计数和固定错误信息。
 */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。
const NAVIGATION_ID = "response-cache";
const RESOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const MACHINE_CODE = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const SELECTOR = /^(?:policy|model_alias|model|virtual_key|data_class|tag):[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const PRIVATE_CURSOR = /^[A-Za-z0-9_-]{1,4096}$/;

const ERROR_MESSAGES = Object.freeze({
  "auth.permission_denied": "当前账号没有所需的响应缓存权限",
  "response_cache.policy_not_found": "当前租户中找不到该缓存策略",
  "response_cache.version_conflict": "缓存策略版本已变化，请重新读取后再保存",
  "response_cache.request_conflict": "该幂等键已用于其他操作意图",
  "response_cache.input_invalid": "响应缓存请求不合法",
  "response_cache.query_invalid": "响应缓存查询不合法",
  "response_cache.configuration_unavailable": "响应缓存尚未完成安全配置",
  "response_cache.unavailable": "响应缓存管理服务暂不可用",
  "response_cache.response_invalid": "响应缓存响应合同不完整",
});

/** PAGE-106 的最小读取身份；角色名不能补足后端权限事实。 */
export function responseCacheReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.RESPONSE_CACHE_READ);
}

/** 单独冻结写能力，write 不隐含 read，页面 owner 仍必须同时具备 read。 */
export function responseCacheWriteIdentitySnapshot(authContext) {
  if (!hasPermission(authContext, PERMISSIONS.RESPONSE_CACHE_WRITE)) return "";
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.RESPONSE_CACHE_WRITE);
}

/** 单独冻结失效能力，invalidate 按钮只是后端权限的 UX 投影。 */
export function responseCacheInvalidateIdentitySnapshot(authContext) {
  if (!hasPermission(authContext, PERMISSIONS.RESPONSE_CACHE_INVALIDATE)) return "";
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.RESPONSE_CACHE_INVALIDATE);
}

/**
 * 合并 auth/me 与后端 navigation 为完整 owner。
 * tenant、Session、权限全集、revision、fingerprint 或导航合同任一漂移都返回 null。
 */
export function responseCachePageOwner(authContext, consoleShellState, { mode = "read" } = {}) {
  const readIdentity = responseCacheReadIdentitySnapshot(authContext);
  const writeIdentity = responseCacheWriteIdentitySnapshot(authContext);
  const invalidateIdentity = responseCacheInvalidateIdentitySnapshot(authContext);
  const tenantId = mirroredTenantId(authContext);
  const authPermissions = permissionCodesFromAuthContext(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === NAVIGATION_ID)
    : null;
  const requiredPermissions = normalizePermissionCodes(item?.required_permissions);
  const navigationPermissions = normalizePermissionCodes(navigation?.permission_codes);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  if (
    !readIdentity
    || !tenantId
    || !["read", "write", "invalidate"].includes(mode)
    || (mode === "write" && !writeIdentity)
    || (mode === "invalidate" && !invalidateIdentity)
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== NAVIGATION_ID
    || navigation?.active_navigation_item_id !== NAVIGATION_ID
    || item?.target_section !== NAVIGATION_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(requiredPermissions) !== JSON.stringify([PERMISSIONS.RESPONSE_CACHE_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(authPermissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    readIdentity,
    writeIdentity,
    invalidateIdentity,
    tenantId,
    permissionCodes: Object.freeze([...authPermissions]),
    navigationOwner: JSON.stringify([
      item.id,
      item.target_section,
      requiredPermissions,
      authPermissions,
      revision,
      fingerprint,
    ]),
    canWrite: Boolean(writeIdentity),
    canInvalidate: Boolean(invalidateIdentity),
  });
}

/** 创建 load/save/invalidate 三个 generation lane 相互隔离的 PAGE-106 状态机。 */
export function createResponseCachePageState({ clearDom = () => {} } = {}) {
  let ownerKey = "";
  let canWrite = false;
  let canInvalidate = false;
  let listState = "loading";
  let listError = null;
  let items = Object.freeze([]);
  let globalEnabled = false;
  let cursor = null;
  let cursorStack = [];
  let nextCursor = null;
  let selectedAliasId = "";
  let editor = null;
  let editorBaseline = null;
  let editorState = "idle";
  let editorError = null;
  let editorFeedback = null;
  let dirty = false;
  let conflict = false;
  let saveRetry = null;
  let invalidateOpen = false;
  let invalidateState = "idle";
  let invalidateError = null;
  let invalidateFeedback = null;
  let invalidateRetry = null;
  let generations = { load: 0, save: 0, invalidate: 0 };
  let operations = { load: null, save: null, invalidate: null };

  /** 进入完整 owner；任何维度漂移都会先擦除事实和 DOM，再取消旧请求。 */
  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    canWrite = owner.canWrite;
    canInvalidate = owner.canInvalidate;
    if (!canWrite) clearEditorMutation();
    if (!canInvalidate) clearInvalidation();
    return true;
  }

  /** 清空所有租户事实和私有票据；clear-before-abort 防止同步 abort 监听器读取旧状态。 */
  function clear() {
    const pending = Object.values(operations).filter(Boolean);
    ownerKey = "";
    canWrite = false;
    canInvalidate = false;
    listState = "loading";
    listError = null;
    items = Object.freeze([]);
    globalEnabled = false;
    cursor = null;
    cursorStack = [];
    nextCursor = null;
    selectedAliasId = "";
    editor = null;
    editorBaseline = null;
    editorState = "idle";
    editorError = null;
    editorFeedback = null;
    dirty = false;
    conflict = false;
    saveRetry = null;
    invalidateOpen = false;
    invalidateState = "idle";
    invalidateError = null;
    invalidateFeedback = null;
    invalidateRetry = null;
    operations = { load: null, save: null, invalidate: null };
    generations = {
      load: generations.load + 1,
      save: generations.save + 1,
      invalidate: generations.invalidate + 1,
    };
    try {
      clearDom();
    } finally {
      for (const operation of pending) operation.controller.abort();
    }
  }

  /** 返回可渲染安全投影；cursor、owner、Controller 与幂等键永不离开闭包。 */
  function snapshot() {
    return Object.freeze({
      state: listState,
      error: listError,
      items,
      globalEnabled,
      canPrevious: cursorStack.length > 0,
      canNext: Boolean(nextCursor),
      selectedAliasId,
      editor: editor ? freezeEditor(editor) : null,
      editorState,
      editorError,
      editorFeedback,
      dirty,
      conflict,
      canWrite,
      canInvalidate,
      invalidateOpen,
      invalidateState,
      invalidateError,
      invalidateFeedback,
      selectors: responseCacheSelectors(items),
    });
  }

  /** 为当前 owner 建立列表 GET；普通刷新不得清除 dirty/conflict 编辑器。 */
  function beginLoad(owner, { rebase = false } = {}) {
    if (!ownerMatches(owner) || operations.save || operations.invalidate) return null;
    if (!items.length) listState = "loading";
    listError = null;
    return begin("load", owner, { cursor, rebase: rebase === true });
  }

  /** 接受 strict GET 后更新列表；只有显式 rebase 才保留草稿并推进 expected_version。 */
  function acceptLoad(operation, response, owner) {
    if (!isCurrent("load", operation, owner) || !validList(response, owner.tenantId)) return false;
    items = Object.freeze(response.items.map(freezeItem));
    globalEnabled = response.global_enabled;
    nextCursor = response.next_cursor;
    listState = "ready";
    listError = null;
    const selected = selectedAliasId
      ? items.find((item) => item.model_alias_id === selectedAliasId) ?? null
      : items[0] ?? null;
    if (!selected) {
      const preserveDraft = editor
        && (dirty || conflict || editorState === "saving" || editorState === "unknown_result");
      if (preserveDraft) {
        // 普通 GET 不得因 Alias 暂时移出 active 集合而删除草稿；显式 rebase 则显示固定 owner 失败。
        if (operation.rebase) {
          editorState = "error";
          editorError = fixedError({
            code: "response_cache.policy_not_found",
            requestId: response.request_id,
          }, "response_cache.policy_not_found");
          saveRetry = null;
        }
        return true;
      }
      selectedAliasId = "";
      resetEditor();
      return true;
    }
    selectedAliasId = selected.model_alias_id;
    const remote = editorFromItem(selected);
    const preserveDraft = editor
      && editor.model_alias_id === selected.model_alias_id
      && (dirty || conflict || editorState === "saving" || editorState === "unknown_result");
    if (operation.rebase && preserveDraft) {
      editor = Object.freeze({ ...editor, expected_version: remote.expected_version });
      editorBaseline = remote;
      editorState = "idle";
      editorError = null;
      conflict = false;
      dirty = !samePolicyIntent(editor, editorBaseline);
      saveRetry = null;
    } else if (!preserveDraft) {
      setEditorBaseline(remote);
    }
    return true;
  }

  /** 列表失败只保留固定错误；已有同 owner 草稿不会被错误响应覆盖。 */
  function rejectLoad(operation, error, owner) {
    if (!isCurrent("load", operation, owner)) return false;
    listState = "error";
    listError = fixedError(error, "response_cache.unavailable");
    if (!dirty && !conflict) {
      items = Object.freeze([]);
      resetEditor();
    }
    return true;
  }

  /** 在没有写请求或草稿时翻到上一私有 cursor。 */
  function previousPage() {
    if (!cursorStack.length || blocksShellDomReplacement() || operations.load) return false;
    cursor = cursorStack.pop() ?? null;
    nextCursor = null;
    items = Object.freeze([]);
    selectedAliasId = "";
    resetEditor();
    listState = "loading";
    return true;
  }

  /** 前进时仅把 opaque cursor 留在闭包，绝不放入 DOM 或 snapshot。 */
  function nextPage() {
    if (!nextCursor || blocksShellDomReplacement() || operations.load) return false;
    cursorStack.push(cursor);
    cursor = nextCursor;
    nextCursor = null;
    items = Object.freeze([]);
    selectedAliasId = "";
    resetEditor();
    listState = "loading";
    return true;
  }

  /** 选择一个已验证 Alias；未保存草稿存在时拒绝静默切换。 */
  function selectAlias(modelAliasId) {
    if (
      dirty
      || conflict
      || invalidateOpen
      || operations.save
      || operations.invalidate
      || !RESOURCE_ID.test(String(modelAliasId ?? ""))
    ) {
      return false;
    }
    const item = items.find((candidate) => candidate.model_alias_id === modelAliasId);
    // 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。
    if (!item || item.model_alias_id === selectedAliasId) return false;
    selectedAliasId = item.model_alias_id;
    setEditorBaseline(editorFromItem(item));
    return true;
  }

  /** 用经过 bind 层 strict 解析的完整 PATCH 请求替换草稿，不接受跨 Alias 注入。 */
  function replaceDraft(request) {
    if (
      !canWrite
      || conflict
      || invalidateOpen
      || operations.save
      || operations.invalidate
      || request?.model_alias_id !== selectedAliasId
    ) return false;
    const next = freezeEditor(request);
    if (!validEditor(next)) return false;
    const oldIntent = editor ? policyIntentKey(editor) : "";
    editor = next;
    dirty = !editorBaseline || !samePolicyIntent(editor, editorBaseline);
    conflict = false;
    editorState = "idle";
    editorError = null;
    editorFeedback = null;
    if (oldIntent !== policyIntentKey(editor)) saveRetry = null;
    return true;
  }

  /** 显式放弃草稿并恢复最近一次服务端基线；未知结果票据同时销毁，避免误重放旧意图。 */
  function discardDraft() {
    if (!canWrite || conflict || invalidateOpen || operations.save || !editorBaseline) return false;
    setEditorBaseline(editorBaseline);
    return true;
  }

  /** 建立保存票据；未知结果且 intent 未变时复用幂等键，每次 HTTP attempt 仍由客户端生成新 request_id。 */
  function beginSave(owner, request, createIdempotencyKey) {
    if (
      !ownerMatches(owner)
      || !owner.canWrite
      || conflict
      || operations.save
      || request?.model_alias_id !== selectedAliasId
      || !validEditor(request)
      || typeof createIdempotencyKey !== "function"
    ) return null;
    const intentKey = policyIntentKey(request);
    const idempotencyKey = saveRetry?.ownerKey === ownerKey
      && saveRetry.intentKey === intentKey
      ? saveRetry.idempotencyKey
      : createIdempotencyKey();
    const operation = begin("save", owner, {
      request: freezeEditor(request),
      intentKey,
      idempotencyKey,
    });
    editorState = "saving";
    editorError = null;
    editorFeedback = null;
    conflict = false;
    return operation;
  }

  /** 保存成功后用服务端 CAS 结果替换本 Alias，并清除草稿和未知结果票据。 */
  function acceptSave(operation, response, owner) {
    if (
      !isCurrent("save", operation, owner)
      || response?.cache_policy?.model_alias_id !== operation.request.model_alias_id
    ) return false;
    items = Object.freeze(items.map((item) => (
      item.model_alias_id === operation.request.model_alias_id
        ? freezeItem({ ...item, cache_policy: response.cache_policy })
        : item
    )));
    const saved = editorFromItem(items.find(
      (item) => item.model_alias_id === operation.request.model_alias_id,
    ));
    setEditorBaseline(saved);
    editorState = "success";
    editorFeedback = Object.freeze({
      requestId: safeChain(response.request_id),
      changed: response.changed === true,
      replay: response.idempotent_replay === true,
      invalidatedEntryCount: safeCount(response.invalidated_entry_count),
      fencedFillCount: safeCount(response.fenced_fill_count),
    });
    saveRetry = null;
    return true;
  }

  /** 409 保留草稿等待显式 rebase；503 保留同 intent 幂等票据以处理未知结果。 */
  function rejectSave(operation, error, owner) {
    if (!isCurrent("save", operation, owner)) return false;
    editorError = fixedError(error, "response_cache.unavailable");
    editorFeedback = null;
    dirty = true;
    // 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。
    if (error?.status === 409 && error?.code === "response_cache.version_conflict") {
      editorState = "conflict";
      conflict = true;
      saveRetry = null;
    } else if (error?.status === 503 && error?.code === "response_cache.unavailable") {
      editorState = "unknown_result";
      conflict = false;
      saveRetry = Object.freeze({
        ownerKey,
        intentKey: operation.intentKey,
        idempotencyKey: operation.idempotencyKey,
      });
    } else {
      editorState = "error";
      conflict = false;
      saveRetry = null;
    }
    return true;
  }

  /** 打开失效 Dialog；只读用户即使手工触发事件也无法创建 DOM 状态或 POST ticket。 */
  function openInvalidation(owner) {
    if (
      !ownerMatches(owner)
      || !owner.canInvalidate
      || dirty
      || conflict
      || operations.save
      || operations.invalidate
    ) return false;
    invalidateOpen = true;
    invalidateState = "idle";
    invalidateError = null;
    invalidateFeedback = null;
    return true;
  }

  /** 关闭 Dialog 会销毁未知结果幂等票据；用户再次打开即视为新意图。 */
  function closeInvalidation(owner) {
    if (!ownerMatches(owner) || operations.invalidate) return false;
    clearInvalidation();
    return true;
  }

  /** 建立失效 POST；selector 必须来自本页已验证白名单且 all scope 不携带 selector。 */
  function beginInvalidation(owner, request, createIdempotencyKey) {
    if (
      !ownerMatches(owner)
      || !owner.canInvalidate
      || !invalidateOpen
      || operations.invalidate
      || !validInvalidation(request, responseCacheSelectors(items))
      || typeof createIdempotencyKey !== "function"
    ) return null;
    const intentKey = JSON.stringify(request);
    const idempotencyKey = invalidateRetry?.ownerKey === ownerKey
      && invalidateRetry.intentKey === intentKey
      ? invalidateRetry.idempotencyKey
      : createIdempotencyKey();
    const operation = begin("invalidate", owner, {
      request: Object.freeze({ ...request, tags: Object.freeze([...request.tags]) }),
      intentKey,
      idempotencyKey,
    });
    invalidateState = "submitting";
    invalidateError = null;
    invalidateFeedback = null;
    return operation;
  }

  /** 失效成功只接纳安全计数和链路 ID；策略 ready count 由后续 fresh GET 重建。 */
  function acceptInvalidation(operation, response, owner) {
    if (!isCurrent("invalidate", operation, owner) || !response?.invalidation) return false;
    invalidateOpen = false;
    invalidateState = "success";
    invalidateError = null;
    invalidateFeedback = Object.freeze({
      requestId: safeChain(response.request_id),
      replay: response.idempotent_replay === true,
      matchedEntryCount: safeCount(response.invalidation.matched_entry_count),
      deletedEntryCount: safeCount(response.invalidation.deleted_entry_count),
      fencedFillCount: safeCount(response.invalidation.fenced_fill_count),
    });
    invalidateRetry = null;
    return true;
  }

  /** 失效失败保留固定 code/request_id；明确 503 可用相同 intent 重试。 */
  function rejectInvalidation(operation, error, owner) {
    if (!isCurrent("invalidate", operation, owner)) return false;
    invalidateState = error?.status === 503 && error?.code === "response_cache.unavailable"
      ? "unknown_result"
      : "error";
    invalidateError = fixedError(error, "response_cache.unavailable");
    invalidateRetry = invalidateState === "unknown_result"
      ? Object.freeze({ ownerKey, intentKey: operation.intentKey, idempotencyKey: operation.idempotencyKey })
      : null;
    return true;
  }

  /** 草稿、冲突、未知结果、Dialog 或在途写均阻止同 owner 定时刷新替换 DOM。 */
  function blocksShellDomReplacement() {
    return Boolean(
      dirty
      || conflict
      || invalidateOpen
      || operations.save
      || operations.invalidate
      || editorState === "unknown_result"
      || invalidateState === "unknown_result",
    );
  }

  /** 三条 lane 共享 generation/owner 判定，但 Controller 和 finally 所有权彼此独立。 */
  function begin(lane, owner, extra) {
    abortLane(lane);
    const controller = new AbortController();
    const operation = Object.freeze({
      lane,
      generation: ++generations[lane],
      ownerKey: ownerKeyFor(owner),
      controller,
      ...extra,
    });
    operations[lane] = operation;
    return operation;
  }

  /** 判断回调仍属于当前 owner、lane、generation 且尚未被 abort。 */
  function isCurrent(lane, operation, owner) {
    return Boolean(
      operation
      && operations[lane] === operation
      && !operation.controller.signal.aborted
      && operation.generation === generations[lane]
      && operation.ownerKey === ownerKeyFor(owner)
      && ownerMatches(owner),
    );
  }

  /** 只结束精确 operation，旧 finally 不得清除新一轮票据。 */
  function finish(lane, operation) {
    if (operations[lane] === operation) operations[lane] = null;
  }

  /** 取消单一 lane 并推进 generation，使迟到 resolve/reject 都失效。 */
  function abortLane(lane) {
    operations[lane]?.controller?.abort();
    operations[lane] = null;
    generations[lane] += 1;
  }

  function ownerMatches(owner) {
    return Boolean(ownerKey && ownerKey === ownerKeyFor(owner));
  }

  /** 清理编辑器写态但保留已授权只读基线。 */
  function clearEditorMutation() {
    abortLane("save");
    if (editorBaseline) editor = editorBaseline;
    editorState = "idle";
    editorError = null;
    editorFeedback = null;
    dirty = false;
    conflict = false;
    saveRetry = null;
  }

  /** 清除失效 Dialog、错误和私有幂等票据。 */
  function clearInvalidation() {
    abortLane("invalidate");
    invalidateOpen = false;
    invalidateState = "idle";
    invalidateError = null;
    invalidateFeedback = null;
    invalidateRetry = null;
  }

  function resetEditor() {
    editor = null;
    editorBaseline = null;
    clearEditorMutation();
  }

  /** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
  function setEditorBaseline(value) {
    const frozen = freezeEditor(value);
    editor = frozen;
    editorBaseline = frozen;
    editorState = "idle";
    editorError = null;
    editorFeedback = null;
    dirty = false;
    conflict = false;
    saveRetry = null;
  }

  return Object.freeze({
    enter,
    clear,
    snapshot,
    query: () => ({ cursor }),
    beginLoad,
    acceptLoad,
    rejectLoad,
    isLoadCurrent: (operation, owner) => isCurrent("load", operation, owner),
    finishLoad: (operation) => finish("load", operation),
    previousPage,
    nextPage,
    selectAlias,
    replaceDraft,
    discardDraft,
    beginSave,
    acceptSave,
    rejectSave,
    isSaveCurrent: (operation, owner) => isCurrent("save", operation, owner),
    finishSave: (operation) => finish("save", operation),
    openInvalidation,
    closeInvalidation,
    beginInvalidation,
    acceptInvalidation,
    rejectInvalidation,
    isInvalidationCurrent: (operation, owner) => isCurrent("invalidate", operation, owner),
    finishInvalidation: (operation) => finish("invalidate", operation),
    blocksShellDomReplacement,
    isActionBusy: () => Boolean(operations.save || operations.invalidate),
  });
}

/** owner key 显式包含三项能力与权限全集，局部撤权也会销毁整个旧 owner。 */
function ownerKeyFor(owner) {
  if (!owner?.readIdentity || !owner?.tenantId || !owner?.navigationOwner) return "";
  return JSON.stringify([
    owner.readIdentity,
    owner.writeIdentity,
    owner.invalidateIdentity,
    owner.tenantId,
    owner.permissionCodes,
    owner.navigationOwner,
  ]);
}

/** 客户端已执行 exact DTO 校验；状态机仍复核 owner、游标和关键集合边界。 */
function validList(value, tenantId) {
  return Boolean(
    value
    && value.tenant_id === tenantId
    && Array.isArray(value.items)
    && value.items.length <= 100
    && value.items.every((item) => item?.tenant_id === tenantId)
    && new Set(value.items.map((item) => item.model_alias_id)).size === value.items.length
    && (value.next_cursor === null || PRIVATE_CURSOR.test(String(value.next_cursor ?? "")))
    && typeof value.global_enabled === "boolean",
  );
}

/** 从可空服务端策略生成完整 PATCH 草稿；新策略必须显式选择 data class 后才能启用。 */
function editorFromItem(item) {
  const policy = item.cache_policy;
  return freezeEditor({
    model_alias_id: item.model_alias_id,
    status: policy?.status ?? "disabled",
    data_class: policy?.data_class ?? null,
    single_data_class_confirmed: policy?.single_data_class_confirmed ?? false,
    ttl_seconds: policy?.ttl_seconds ?? 900,
    charge_cache_hits: policy?.charge_cache_hits ?? false,
    tags: policy?.tags ?? [],
    expected_version: policy?.version ?? 0,
  });
}

/** 校验编辑器投影，防止测试或迟到 DOM 事件绕过 bind 层提交跨 Alias/自由文本。 */
function validEditor(value) {
  return Boolean(
    value
    && RESOURCE_ID.test(String(value.model_alias_id ?? ""))
    && ["enabled", "disabled"].includes(value.status)
    && (value.data_class === null || MACHINE_CODE.test(String(value.data_class ?? "")))
    && typeof value.single_data_class_confirmed === "boolean"
    && Number.isSafeInteger(value.ttl_seconds)
    && value.ttl_seconds >= 60
    && value.ttl_seconds <= 86_400
    && typeof value.charge_cache_hits === "boolean"
    && Array.isArray(value.tags)
    && value.tags.length <= 16
    && value.tags.every((tag) => MACHINE_CODE.test(String(tag)))
    && value.tags.every((tag, index) => index === 0 || value.tags[index - 1] < tag)
    && Number.isSafeInteger(value.expected_version)
    && value.expected_version >= 0
    && (value.status !== "enabled" || (value.data_class && value.single_data_class_confirmed))
    && (value.status !== "disabled" || !value.single_data_class_confirmed),
  );
}

/** UI 只能提交当前列表派生的 selector；高级 API 的额外 selector 不在 PAGE-106 枚举。 */
function validInvalidation(value, allowedSelectors) {
  return Boolean(
    value
    && ["all", "tags"].includes(value.scope)
    && value.match_mode === "all"
    && Array.isArray(value.tags)
    && value.tags.length <= 16
    && value.tags.every((tag) => SELECTOR.test(String(tag)) && allowedSelectors.includes(tag))
    && value.tags.every((tag, index) => index === 0 || value.tags[index - 1] < tag)
    && ["manual_finops", "manual_security", "manual_configuration", "manual_other"]
      .includes(value.reason_code)
    && (value.scope === "all" ? value.tags.length === 0 : value.tags.length > 0),
  );
}

/** 从已验证列表派生稳定 selector，不把 data class 或 tag 值放入持久存储。 */
function responseCacheSelectors(source) {
  const result = new Set();
  for (const item of source) {
    result.add(`model_alias:${item.model_alias_id}`);
    if (item.cache_policy?.cache_policy_id) result.add(`policy:${item.cache_policy.cache_policy_id}`);
    for (const tag of item.cache_policy?.tags ?? []) result.add(`tag:${tag}`);
  }
  return Object.freeze([...result].filter((value) => SELECTOR.test(value)).sort());
}

/** 比较配置 intent，不把 expected_version 当作用户字段；显式 rebase 只推进 CAS 基线。 */
function samePolicyIntent(left, right) {
  return policyIntentKey(left) === policyIntentKey(right);
}

function policyIntentKey(value) {
  return JSON.stringify([
    value?.model_alias_id,
    value?.status,
    value?.data_class,
    value?.single_data_class_confirmed,
    value?.ttl_seconds,
    value?.charge_cache_hits,
    value?.tags,
  ]);
}

/** 冻结 Alias 和可空策略的两层数组，避免渲染期间被调用方原地修改。 */
function freezeItem(item) {
  return Object.freeze({
    ...item,
    cache_policy: item.cache_policy
      ? Object.freeze({ ...item.cache_policy, tags: Object.freeze([...item.cache_policy.tags]) })
      : null,
  });
}

function freezeEditor(value) {
  return Object.freeze({ ...value, tags: Object.freeze([...(value?.tags ?? [])]) });
}

/** 错误投影只保留本地文案、固定 code 和安全 request_id。 */
function fixedError(error, fallbackCode) {
  const candidate = String(error?.code ?? "");
  const code = Object.hasOwn(ERROR_MESSAGES, candidate) ? candidate : fallbackCode;
  return Object.freeze({
    code,
    message: ERROR_MESSAGES[code] ?? ERROR_MESSAGES["response_cache.unavailable"],
    requestId: safeChain(error?.requestId),
  });
}

function safeChain(value) {
  const candidate = String(value ?? "");
  return CHAIN_ID.test(candidate) ? candidate : "-";
}

/** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
function safeCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

/** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
function normalizePermissionCodes(value) {
  if (!Array.isArray(value)) return null;
  const normalized = value.map(String).sort();
  return normalized.length === new Set(normalized).size ? normalized : null;
}

/** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
function mirroredTenantId(authContext) {
  const userTenant = String(authContext?.user?.tenant_id ?? "");
  const tenant = String(authContext?.tenant?.tenant_id ?? "");
  // 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。
  return userTenant && userTenant === tenant && RESOURCE_ID.test(userTenant) ? userTenant : "";
}

/** owner 清理触发的 Abort 不进入 aria-live 错误。 */
/** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
export function isResponseCacheAbortError(error) {
  return error?.name === "AbortError";
}
