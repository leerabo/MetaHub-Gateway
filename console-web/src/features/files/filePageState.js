/**
 * 模块职责：维护 PAGE-101/PAGE-102 的授权 owner、私有 cursor、文件安全摘要和上传生命周期。
 * 安全边界：tenant、授权指纹、cursor、AbortController 与原生 File 均不进入公开 snapshot；离页、
 * 注销、401/403、权限降级或 owner 漂移会推进代次并销毁上一租户文件名与处理事实。
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

// 导航 ID 同时进入后端 target_section 和页面 owner，手写其他 hash 不能复用文件状态。
const NAVIGATION_ID = "files";
// 列表默认值是公开查询合同；opaque cursor 另存私有闭包，不混入该对象。
const DEFAULT_QUERY = Object.freeze({ status: "", limit: 25 });
// 状态筛选只允许持久状态枚举，未知 DOM 值收敛为空而不是传给后端猜测。
const STATUS_VALUES = new Set(["processing", "ready", "blocked", "failed"]);

/** 返回只要求 file.read 的完整授权身份快照。 */
export function fileReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.FILE_READ);
}

/** 返回同时具备 file.read/file.write 时的写身份；write-only 自定义角色失败关闭。 */
export function fileWriteIdentitySnapshot(authContext) {
  if (!hasEveryPermission(authContext, [PERMISSIONS.FILE_READ, PERMISSIONS.FILE_WRITE])) return "";
  return JSON.stringify([
    authorizationIdentitySnapshot(authContext, PERMISSIONS.FILE_READ),
    authorizationIdentitySnapshot(authContext, PERMISSIONS.FILE_WRITE),
  ]);
}

/**
 * 从实时认证与后端导航构造文件工作台 owner。
 *
 * @param {object|null} authContext 当前 /auth/me 授权快照。
 * @param {object|null} consoleShellState 当前后端授权导航快照。
 * @param {object} options requireWrite=true 时额外要求 file.write。
 * @returns {object|null} 不含 Token 的冻结 owner；任一导航/权限镜像漂移返回 null。
 *
 * 前端 owner 只阻止无权预取和迟到回写，GET/POST 仍由 Control API 独立执行 RBAC 与 tenant 条件。
 */
export function filePageOwner(authContext, consoleShellState, { requireWrite = false } = {}) {
  const identity = fileReadIdentitySnapshot(authContext);
  const writeIdentity = fileWriteIdentitySnapshot(authContext);
  const tenantId = mirroredTenantId(authContext);
  const navigation = consoleShellState?.navigation;
  const navigationItem = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((item) => item?.id === NAVIGATION_ID)
    : null;
  const requiredPermissions = normalizePermissionCodes(navigationItem?.required_permissions);
  const navigationPermissionCodes = normalizePermissionCodes(navigation?.permission_codes);
  const authPermissionCodes = permissionCodesFromAuthContext(authContext);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const canWrite = Boolean(writeIdentity);
  if (
    !identity
    || !tenantId
    || !hasPermission(authContext, PERMISSIONS.FILE_READ)
    || (requireWrite && !canWrite)
    || consoleShellState?.activeNavigationItemId !== NAVIGATION_ID
    || navigation?.active_navigation_item_id !== NAVIGATION_ID
    || navigationItem?.target_section !== NAVIGATION_ID
    || navigationItem?.visible === false
    || navigationItem?.disabled === true
    || !requiredPermissions.includes(PERMISSIONS.FILE_READ)
    || JSON.stringify(navigationPermissionCodes) !== JSON.stringify(authPermissionCodes)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    identity,
    writeIdentity,
    tenantId,
    navigation: NAVIGATION_ID,
    navigationOwner: JSON.stringify([
      navigationItem.id,
      navigationItem.target_section,
      requiredPermissions,
      revision,
      fingerprint,
    ]),
    canWrite,
  });
}

/**
 * 创建文件工作台私有状态机。
 *
 * @returns {object} 通过显式 ticket 变更的页面状态；公开 snapshot 不含 cursor 或授权 owner。
 */
export function createFilePageState() {
  let ownerKey = "";
  let ownerCanWrite = false;
  let queryState = { ...DEFAULT_QUERY };
  let cursor = null;
  let cursorStack = [];
  let page = null;
  let loadError = null;
  let feedback = null;
  let listGeneration = 0;
  let listOperation = null;
  let uploadGeneration = 0;
  let uploadOperation = null;

  /** 绑定当前 read owner；owner 或 write 投影变化时先清空全部上一租户事实。 */
  function enter(owner) {
    const nextOwnerKey = keyFromOwner(owner);
    if (!nextOwnerKey) {
      clear();
      return false;
    }
    if (ownerKey !== nextOwnerKey || ownerCanWrite !== owner.canWrite) {
      resetAll();
      ownerKey = nextOwnerKey;
      ownerCanWrite = owner.canWrite === true;
    }
    return true;
  }

  /** 离页、登出、认证失败或撤权时取消请求并销毁文件名、列表和链路摘要。 */
  function clear() {
    resetAll();
    ownerKey = "";
    ownerCanWrite = false;
  }

  /** 返回 GET 所需的固定筛选和私有 cursor；调用方不得把 cursor 传给 renderer。 */
  function query() {
    return { ...queryState, cursor };
  }

  /** 设置固定状态筛选并回到首屏，未知 DOM 值收敛为全部状态。 */
  function setStatus(status) {
    invalidateList();
    queryState = {
      status: STATUS_VALUES.has(String(status ?? "").trim()) ? String(status).trim() : "",
      limit: DEFAULT_QUERY.limit,
    };
    cursor = null;
    cursorStack = [];
    page = null;
    loadError = null;
  }

  /** 清空状态筛选并复用相同失效边界。 */
  function resetStatus() {
    setStatus("");
  }

  /** 开始一次列表 GET，票据绑定 owner 与全局 refresh generation。 */
  function beginListOperation(owner, refreshGeneration) {
    const key = keyFromOwner(owner);
    if (
      !key
      || key !== ownerKey
      || !Number.isSafeInteger(refreshGeneration)
      || refreshGeneration < 1
    ) return null;
    abortListOperation();
    listGeneration += 1;
    listOperation = Object.freeze({
      ownerKey: key,
      generation: listGeneration,
      refreshGeneration,
      controller: new AbortController(),
    });
    return listOperation;
  }

  /** 核验列表票据、owner、刷新代次与 Abort 状态仍完全一致。 */
  function isListOperationCurrent(operation, owner, refreshGeneration) {
    return Boolean(
      operation
      && operation === listOperation
      && !operation.controller.signal.aborted
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && operation.generation === listGeneration
      && operation.refreshGeneration === refreshGeneration,
    );
  }

  /** 接受 strict client 投影的列表，把 opaque cursor 仅保存在私有 page 中。 */
  function acceptPage(operation, payload, owner, refreshGeneration) {
    if (
      !isListOperationCurrent(operation, owner, refreshGeneration)
      || payload?.state !== "ok"
      || !Array.isArray(payload.files)
    ) return false;
    page = {
      files: payload.files,
      nextCursor: payload.next_cursor,
      requestId: payload.request_id,
      traceId: payload.trace_id,
    };
    loadError = null;
    return true;
  }

  /** 拒绝当前列表 GET，只保存固定 code/message/request_id，不保留旧 owner 数据。 */
  function rejectPage(operation, error, owner, refreshGeneration) {
    if (!isListOperationCurrent(operation, owner, refreshGeneration)) return false;
    page = null;
    loadError = fixedPageError(error, "文件处理记录读取失败");
    return true;
  }

  /** 仅结束当前列表票据；迟到 finally 不能清除下一次 GET。 */
  function finishListOperation(operation) {
    if (listOperation === operation) listOperation = null;
  }

  /** 使用当前响应的私有 next_cursor 进入下一页。 */
  function nextPage() {
    if (!page?.nextCursor || listOperation || uploadOperation) return false;
    cursorStack.push(cursor);
    cursor = page.nextCursor;
    page = null;
    loadError = null;
    return true;
  }

  /** 从私有 cursor 栈恢复上一页；cursor 永不经过按钮 dataset。 */
  function previousPage() {
    if (!cursorStack.length || listOperation || uploadOperation) return false;
    cursor = cursorStack.pop() ?? null;
    page = null;
    loadError = null;
    return true;
  }

  /** 开始一次上传；只冻结 owner 和代次，不保存 File、文件名或文件正文。 */
  function beginUploadOperation(owner) {
    const key = keyFromOwner(owner);
    if (!ownerCanWrite || !owner?.canWrite || !key || key !== ownerKey || uploadOperation) return null;
    uploadGeneration += 1;
    uploadOperation = Object.freeze({
      ownerKey: key,
      generation: uploadGeneration,
      controller: new AbortController(),
    });
    feedback = null;
    return uploadOperation;
  }

  /** 核验上传仍属于当前 read+write owner；撤权会改变 owner key 并拒绝迟到结果。 */
  function isUploadOperationCurrent(operation, owner) {
    return Boolean(
      operation
      && operation === uploadOperation
      && !operation.controller.signal.aborted
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && operation.generation === uploadGeneration
      && owner?.canWrite === true,
    );
  }

  /** 接受上传终态，只保存 file_id 和链路反馈；列表由随后 GET 权威刷新。 */
  function acceptUpload(operation, payload, owner) {
    if (
      !isUploadOperationCurrent(operation, owner)
      || payload?.state !== "ok"
      || !payload.file?.file_id
    ) return false;
    feedback = Object.freeze({
      state: "success",
      code: "file.uploaded",
      message: "文件上传、文本解析和安全预扫描已完成",
      requestId: safeId(payload.request_id) || "-",
      fileId: safeId(payload.file.file_id) || "-",
    });
    return true;
  }

  /** 保存业务上传失败的固定安全反馈；文件名、解析器异常和规则信息不会进入状态。 */
  function rejectUpload(operation, error, owner) {
    if (!isUploadOperationCurrent(operation, owner)) return false;
    feedback = Object.freeze({
      state: "error",
      ...fixedPageError(error, "文件上传失败"),
    });
    return true;
  }

  /** 结束当前上传 ticket；迟到 finally 不得解除下一次上传的按钮锁。 */
  function finishUploadOperation(operation) {
    if (uploadOperation === operation) uploadOperation = null;
  }

  /** 返回上传请求是否仍未决，供定时全页刷新和重复提交门禁使用。 */
  function isUploadBusy() {
    return Boolean(uploadOperation);
  }

  /** 输出 renderer 安全快照；owner、指纹、cursor、controller 和 File 永不暴露。 */
  function snapshot() {
    return Object.freeze({
      state: loadError ? "error" : page ? "ok" : "loading",
      files: page?.files ?? Object.freeze([]),
      requestId: page?.requestId ?? loadError?.requestId ?? "-",
      traceId: page?.traceId ?? "-",
      error: loadError,
      feedback,
      status: queryState.status,
      canPrevious: cursorStack.length > 0,
      canNext: Boolean(page?.nextCursor),
      uploadBusy: Boolean(uploadOperation),
      canWrite: ownerCanWrite,
    });
  }

  /** 取消列表和上传并重置 owner 下全部业务事实。 */
  function resetAll() {
    abortListOperation();
    abortUploadOperation();
    queryState = { ...DEFAULT_QUERY };
    cursor = null;
    cursorStack = [];
    page = null;
    loadError = null;
    feedback = null;
  }

  /** 取消旧列表并推进代次，使忽略 AbortSignal 的回调也无法被接受。 */
  function invalidateList() {
    listGeneration += 1;
    abortListOperation();
  }

  /** 中止当前列表 ticket。 */
  function abortListOperation() {
    listOperation?.controller.abort();
    listOperation = null;
  }

  /** 中止当前上传 ticket 并推进代次。 */
  function abortUploadOperation() {
    uploadGeneration += 1;
    uploadOperation?.controller.abort();
    uploadOperation = null;
  }

  return Object.freeze({
    enter,
    clear,
    query,
    setStatus,
    resetStatus,
    beginListOperation,
    isListOperationCurrent,
    acceptPage,
    rejectPage,
    finishListOperation,
    nextPage,
    previousPage,
    beginUploadOperation,
    isUploadOperationCurrent,
    acceptUpload,
    rejectUpload,
    finishUploadOperation,
    isUploadBusy,
    snapshot,
  });
}

/** 识别页面生命周期主动 Abort，避免把正常离页显示成依赖故障。 */
export function isFileAbortError(error) {
  return error?.name === "AbortError";
}

/** 把 owner 转为只供闭包等值比较的键，Token 不参与也不会持久化。 */
function keyFromOwner(owner) {
  if (
    !owner?.identity
    || !owner?.tenantId
    || owner.navigation !== NAVIGATION_ID
    || !owner.navigationOwner
  ) return "";
  return JSON.stringify([
    owner.identity,
    owner.writeIdentity,
    owner.tenantId,
    owner.navigation,
    owner.navigationOwner,
    owner.canWrite === true,
  ]);
}

/** 读取 user/tenant 双镜像；冲突时不能任选其一。 */
function mirroredTenantId(authContext) {
  const userTenantId = String(authContext?.user?.tenant_id ?? "").trim();
  const tenantId = String(authContext?.tenant?.tenant_id ?? "").trim();
  // 双镜像冲突意味着认证上下文不自洽，不能任选其一建立 owner。
  if (userTenantId && tenantId && userTenantId !== tenantId) return "";
  // 兼容只缺一个历史镜像，但两个都缺失时仍返回空字符串并失败关闭。
  return userTenantId || tenantId;
}

/** 规范权限数组；重复、非法或空集合都失败关闭。 */
function normalizePermissionCodes(value) {
  if (!Array.isArray(value) || !value.length) return [];
  const normalized = value.map((item) => String(item ?? "").trim());
  if (
    normalized.some((item) => !/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(item))
    || new Set(normalized).size !== normalized.length
  ) return [];
  return normalized.sort();
}

/** 创建只含固定公开字段的页面错误。 */
function fixedPageError(error, fallback) {
  const code = safeErrorCode(error?.code) || "file.storage_unavailable";
  const messages = {
    "auth.permission_denied": "当前账号没有文件操作权限",
    "infra.validation_failed": "文件筛选或分页游标无效",
    "file.multipart_invalid": "文件上传请求格式无效",
    "file.name_invalid": "文件名不符合安全要求",
    "file.unsupported_format": "仅支持 PDF、DOCX 和 UTF-8 TXT",
    "file.too_large": "文件超过 20 MiB 上限",
    "file.processing_busy": "文件解析任务繁忙，请稍后重试",
    "file.structure_invalid": "文件结构无效或包含不受支持的内容",
    "file.no_extractable_text": "文件中没有可提取文本",
    "file.extract_limit_exceeded": "文件页数或抽取文本超过安全上限",
    "file.parse_failed": "文件文本解析失败",
    "file.dlp_blocked": "文件命中当前租户 DLP 阻断规则，正文未保存",
    "file.dlp_unavailable": "DLP 预扫描暂不可用，文件已失败关闭",
    "file.storage_unavailable": "文件存储暂不可用",
    "file.integrity_failed": "文件处理状态完整性校验失败",
    "file.not_found": "当前租户中找不到该文件",
    "file.response_invalid": "文件响应合同不完整",
  };
  return {
    code,
    message: messages[code] ?? fallback,
    requestId: safeId(error?.requestId) || "-",
  };
}

/** 只允许统一错误码进入公开反馈。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "").trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}

/** 只允许有界资源、request 或 trace ID 进入公开快照。 */
function safeId(value) {
  const normalized = String(value ?? "").trim();
  return /^[A-Za-z0-9_.:-]{1,160}$/.test(normalized) ? normalized : "";
}
