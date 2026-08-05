/**
 * 模块职责：维护 PAGE-103 的授权 owner、ready 文件、严格报告与扫描/导出生命周期。
 * 安全边界：tenant、权限指纹、AbortController 和 CSV Blob 均不进入公开 snapshot；离页、注销、
 * 401/403、tenant/Session/permission revision 漂移会推进代次并销毁上一 owner 的文件名和报告。
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

// 页面拥有独立导航 owner，不能复用 PAGE-101/102 的 files hash 或页面状态。
const NAVIGATION_ID = "file-security";

/** 返回只要求 file.read 的完整授权身份快照。 */
export function fileSecurityReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.FILE_READ);
}

/** 返回同时具备 file.read 与 file.security.scan 的扫描身份；单独 scan 权限失败关闭。 */
export function fileSecurityScanIdentitySnapshot(authContext) {
  if (!hasEveryPermission(authContext, [PERMISSIONS.FILE_READ, PERMISSIONS.FILE_SECURITY_SCAN])) return "";
  return JSON.stringify([
    authorizationIdentitySnapshot(authContext, PERMISSIONS.FILE_READ),
    authorizationIdentitySnapshot(authContext, PERMISSIONS.FILE_SECURITY_SCAN),
  ]);
}

/**
 * 从实时认证与后端导航构造 PAGE-103 owner。
 *
 * @param {object|null} authContext 当前 /auth/me 权限快照。
 * @param {object|null} consoleShellState 当前后端导航快照。
 * @param {object} options requireScan=true 时要求扫描复合权限。
 * @returns {object|null} 不含 Token 的冻结 owner；任一镜像不一致时返回 null。
 *
 * 前端 owner 只用于零预取和迟到回写防护，Control API 仍逐请求执行 tenant 与 RBAC 校验。
 */
export function fileSecurityPageOwner(authContext, consoleShellState, { requireScan = false } = {}) {
  const readIdentity = fileSecurityReadIdentitySnapshot(authContext);
  const scanIdentity = fileSecurityScanIdentitySnapshot(authContext);
  const tenantId = mirroredTenantId(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === NAVIGATION_ID)
    : null;
  const requiredPermissions = normalizePermissionCodes(item?.required_permissions);
  const navigationPermissions = normalizePermissionCodes(navigation?.permission_codes);
  const authPermissions = permissionCodesFromAuthContext(authContext);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const canScan = Boolean(scanIdentity);
  if (
    !readIdentity
    || !tenantId
    || !hasPermission(authContext, PERMISSIONS.FILE_READ)
    || (requireScan && !canScan)
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== NAVIGATION_ID
    || navigation?.active_navigation_item_id !== NAVIGATION_ID
    || item?.target_section !== NAVIGATION_ID
    || item?.visible === false
    || item?.disabled === true
    // PAGE-103 可读与扫描必须分离；导航若错误要求 scan，会让合法只读用户被静默裁剪。
    || JSON.stringify(requiredPermissions) !== JSON.stringify([PERMISSIONS.FILE_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(authPermissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    readIdentity,
    scanIdentity,
    tenantId,
    navigation: NAVIGATION_ID,
    navigationOwner: JSON.stringify([
      item.id,
      item.target_section,
      requiredPermissions,
      revision,
      fingerprint,
    ]),
    canScan,
  });
}

/**
 * 创建 PAGE-103 私有状态机。
 *
 * @returns {object} 只能通过 owner-bound ticket 修改的页面状态。
 */
export function createFileSecurityPageState() {
  let ownerKey = "";
  let ownerCanScan = false;
  let files = Object.freeze([]);
  let selectedFileId = "";
  // opaque cursor 与历史栈只驻留闭包，renderer 只接收可前进/后退布尔值。
  let cursor = null;
  let cursorStack = [];
  let nextCursor = null;
  let loadState = "loading";
  let loadError = null;
  let reportState = "idle";
  let report = null;
  let reportError = null;
  let feedback = null;
  let loadGeneration = 0;
  let loadOperation = null;
  let scanGeneration = 0;
  let scanOperation = null;
  let exportGeneration = 0;
  let exportOperation = null;

  /** 绑定当前 read owner；owner 或 scan 投影变化时先擦除全部上一身份事实。 */
  function enter(owner) {
    const nextKey = keyFromOwner(owner);
    if (!nextKey) {
      clear();
      return false;
    }
    if (nextKey !== ownerKey || ownerCanScan !== owner.canScan) {
      resetAll();
      ownerKey = nextKey;
      ownerCanScan = owner.canScan === true;
    }
    return true;
  }

  /** 离页、登出、撤权或身份漂移时 abort 全部请求并销毁文件名、报告和反馈。 */
  function clear() {
    resetAll();
    ownerKey = "";
    ownerCanScan = false;
  }

  /** 返回列表 GET 使用的私有 cursor；调用方不得把返回值交给 renderer。 */
  function query() {
    return { cursor };
  }

  /** 开始一次 ready 文件 + 当前报告顺序读取，票据绑定全局 refresh generation。 */
  function beginLoadOperation(owner, refreshGeneration) {
    const key = keyFromOwner(owner);
    if (
      !key
      || key !== ownerKey
      || !Number.isSafeInteger(refreshGeneration)
      || refreshGeneration < 1
    ) return null;
    abortLoadOperation();
    loadGeneration += 1;
    loadOperation = Object.freeze({
      ownerKey: key,
      generation: loadGeneration,
      refreshGeneration,
      controller: new AbortController(),
    });
    loadState = "loading";
    loadError = null;
    return loadOperation;
  }

  /** 核验 GET 序列仍属于相同 owner、代次和当前 refresh。 */
  function isLoadOperationCurrent(operation, owner, refreshGeneration) {
    return Boolean(
      operation
      && operation === loadOperation
      && !operation.controller.signal.aborted
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && operation.generation === loadGeneration
      && operation.refreshGeneration === refreshGeneration,
    );
  }

  /** 接受 strict FileInfo ready 列表，并只保留仍存在的选中文件。 */
  function acceptFiles(operation, payload, owner, refreshGeneration) {
    if (
      !isLoadOperationCurrent(operation, owner, refreshGeneration)
      || payload?.state !== "ok"
      || !Array.isArray(payload.files)
      || payload.files.length > 25
      || payload.files.some((file) => file?.status !== "ready" || file?.extract?.status !== "ready")
    ) return false;
    const ids = payload.files.map((file) => file.file_id);
    if (new Set(ids).size !== ids.length) return false;
    files = Object.freeze([...payload.files]);
    nextCursor = payload.next_cursor;
    if (!ids.includes(selectedFileId)) selectedFileId = ids[0] ?? "";
    loadState = "ready";
    loadError = null;
    report = null;
    reportError = null;
    reportState = selectedFileId ? "loading" : "idle";
    return true;
  }

  /** 返回本轮 GET 应读取的 file ID；不从 DOM 或 URL 恢复。 */
  function selectedFile() {
    return selectedFileId;
  }

  /** 接受与选中文件一致的 strict 报告；跨 file ID 响应即使 HTTP 200 也被拒绝。 */
  function acceptReport(operation, payload, owner, refreshGeneration) {
    if (
      !isLoadOperationCurrent(operation, owner, refreshGeneration)
      || payload?.state !== "ok"
      || payload.report?.file_id !== selectedFileId
    ) return false;
    report = payload.report;
    reportState = "ready";
    reportError = null;
    return true;
  }

  /** 当前 detector 报告不存在是独立空态，不与文件不存在或依赖故障混为一谈。 */
  function acceptReportNotFound(operation, owner, refreshGeneration) {
    if (!isLoadOperationCurrent(operation, owner, refreshGeneration) || !selectedFileId) return false;
    report = null;
    reportState = "empty";
    reportError = null;
    return true;
  }

  /** 列表失败会销毁上一 owner 列表与报告，不能把旧文件名和新错误混合展示。 */
  function rejectLoad(operation, error, owner, refreshGeneration) {
    if (!isLoadOperationCurrent(operation, owner, refreshGeneration)) return false;
    files = Object.freeze([]);
    selectedFileId = "";
    report = null;
    reportState = "idle";
    reportError = null;
    loadState = "error";
    loadError = fixedPageError(error, "文件安全记录读取失败");
    return true;
  }

  /** 报告失败只清报告，不丢弃已由同一 owner 验证的 ready 文件列表。 */
  function rejectReport(operation, error, owner, refreshGeneration) {
    if (!isLoadOperationCurrent(operation, owner, refreshGeneration)) return false;
    report = null;
    reportState = "error";
    reportError = fixedPageError(error, "文件安全报告读取失败");
    return true;
  }

  /** 只结束相同 GET ticket，迟到 finally 不能清掉下一轮请求。 */
  function finishLoadOperation(operation) {
    if (loadOperation === operation) loadOperation = null;
  }

  /** 只允许选择当前 strict ready 列表中的 ID，并失效旧报告与在途请求。 */
  function selectFile(fileId) {
    const normalized = String(fileId ?? "").trim();
    if (!files.some((file) => file.file_id === normalized) || normalized === selectedFileId) return false;
    abortLoadOperation();
    abortScanOperation();
    abortExportOperation();
    selectedFileId = normalized;
    report = null;
    reportState = "loading";
    reportError = null;
    feedback = null;
    return true;
  }

  /** 使用当前响应的私有 next_cursor 进入下一页，完整签名永不进入 DOM。 */
  function nextPage() {
    if (!nextCursor || loadOperation || scanOperation || exportOperation) return false;
    cursorStack.push(cursor);
    cursor = nextCursor;
    resetPageForPaging();
    return true;
  }

  /** 从私有 cursor 栈恢复上一页；按钮只表达命令，不承载 cursor。 */
  function previousPage() {
    if (!cursorStack.length || loadOperation || scanOperation || exportOperation) return false;
    cursor = cursorStack.pop() ?? null;
    resetPageForPaging();
    return true;
  }

  /** 开始扫描命令；票据绑定 read+scan owner 与当前选中文件，不保存 Token 或正文。 */
  function beginScanOperation(owner) {
    const key = keyFromOwner(owner);
    if (
      !ownerCanScan
      || owner?.canScan !== true
      || !key
      || key !== ownerKey
      || !selectedFileId
      || scanOperation
      || exportOperation
    ) return null;
    abortLoadOperation();
    scanGeneration += 1;
    scanOperation = Object.freeze({
      ownerKey: key,
      fileId: selectedFileId,
      generation: scanGeneration,
      controller: new AbortController(),
    });
    feedback = null;
    return scanOperation;
  }

  /** 核验扫描 POST 仍属于相同复合权限 owner、file ID 与 generation。 */
  function isScanOperationCurrent(operation, owner) {
    return Boolean(
      operation
      && operation === scanOperation
      && !operation.controller.signal.aborted
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && operation.fileId === selectedFileId
      && operation.generation === scanGeneration
      && owner?.canScan === true,
    );
  }

  /** 接受扫描或幂等复用报告，并只保存固定成功反馈。 */
  function acceptScan(operation, payload, owner) {
    if (
      !isScanOperationCurrent(operation, owner)
      || payload?.state !== "ok"
      || payload.report?.file_id !== operation.fileId
    ) return false;
    report = payload.report;
    reportState = "ready";
    reportError = null;
    feedback = Object.freeze({
      state: "success",
      code: payload.reused ? "file.security_scan_reused" : "file.security_scan_completed",
      message: payload.reused ? "已复用当前检测器的权威安全报告" : "文件安全扫描已完成",
      requestId: safeId(payload.request_id) || "-",
    });
    return true;
  }

  /** 保存扫描失败的固定 code/message/request_id，绝不复制 OCR/PDF/XML 异常。 */
  function rejectScan(operation, error, owner) {
    if (!isScanOperationCurrent(operation, owner)) return false;
    feedback = Object.freeze({ state: "error", ...fixedPageError(error, "文件安全扫描失败") });
    return true;
  }

  /** 结束相同扫描 ticket；旧 finally 不能解除新 POST 的按钮锁。 */
  function finishScanOperation(operation) {
    if (scanOperation === operation) scanOperation = null;
  }

  /** 开始 CSV 下载；只冻结 file ID，不把 Blob 或文件名放入状态机。 */
  function beginExportOperation(owner) {
    const key = keyFromOwner(owner);
    if (
      !key
      || key !== ownerKey
      || !selectedFileId
      || reportState !== "ready"
      || report?.file_id !== selectedFileId
      || exportOperation
      || scanOperation
    ) return null;
    exportGeneration += 1;
    exportOperation = Object.freeze({
      ownerKey: key,
      fileId: selectedFileId,
      generation: exportGeneration,
      controller: new AbortController(),
    });
    feedback = null;
    return exportOperation;
  }

  /** 核验 CSV 调用仍属于当前 read owner 与报告。 */
  function isExportOperationCurrent(operation, owner) {
    return Boolean(
      operation
      && operation === exportOperation
      && !operation.controller.signal.aborted
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && operation.fileId === selectedFileId
      && operation.generation === exportGeneration,
    );
  }

  /** 下载已完成只保存固定反馈，Blob/Object URL 必须已在调用栈内销毁。 */
  function acceptExport(operation, result, owner) {
    if (
      !isExportOperationCurrent(operation, owner)
      || result?.state !== "ok"
      || result.fileId !== operation.fileId
    ) return false;
    feedback = Object.freeze({
      state: "success",
      code: "file.security_report_exported",
      message: "文件安全报告 CSV 已下载",
      requestId: safeId(result.requestId) || "-",
    });
    return true;
  }

  /** CSV 失败只保存固定错误，不保留 Blob、Object URL 或响应正文。 */
  function rejectExport(operation, error, owner) {
    if (!isExportOperationCurrent(operation, owner)) return false;
    feedback = Object.freeze({ state: "error", ...fixedPageError(error, "文件安全报告导出失败") });
    return true;
  }

  /** 结束相同导出 ticket。 */
  function finishExportOperation(operation) {
    if (exportOperation === operation) exportOperation = null;
  }

  /** 返回扫描或导出是否未决，供 15 秒全页刷新避免重建命令 DOM。 */
  function isActionBusy() {
    return Boolean(scanOperation || exportOperation);
  }

  /** 输出 renderer 安全快照；owner、权限指纹、controller 和 CSV Blob 永不暴露。 */
  function snapshot() {
    return Object.freeze({
      state: loadState,
      files,
      selectedFileId,
      error: loadError,
      reportState,
      report,
      reportError,
      feedback,
      canScan: ownerCanScan,
      canPrevious: cursorStack.length > 0,
      canNext: Boolean(nextCursor),
      scanBusy: Boolean(scanOperation),
      exportBusy: Boolean(exportOperation),
    });
  }

  /** owner 失效时统一 abort 并清空所有业务事实。 */
  function resetAll() {
    abortLoadOperation();
    abortScanOperation();
    abortExportOperation();
    files = Object.freeze([]);
    selectedFileId = "";
    cursor = null;
    cursorStack = [];
    nextCursor = null;
    loadState = "loading";
    loadError = null;
    reportState = "idle";
    report = null;
    reportError = null;
    feedback = null;
  }

  /** 翻页时清除上一页文件名和报告，但保留 owner、cursor 历史与固定操作反馈。 */
  function resetPageForPaging() {
    files = Object.freeze([]);
    selectedFileId = "";
    nextCursor = null;
    loadState = "loading";
    loadError = null;
    reportState = "idle";
    report = null;
    reportError = null;
  }

  /** 取消 GET 并推进代次，忽略 AbortSignal 的迟到回调也不能落盘。 */
  function abortLoadOperation() {
    loadGeneration += 1;
    loadOperation?.controller.abort();
    loadOperation = null;
  }

  /** 取消扫描并推进代次。 */
  function abortScanOperation() {
    scanGeneration += 1;
    scanOperation?.controller.abort();
    scanOperation = null;
  }

  /** 取消 CSV 下载并推进代次；已创建的 URL 仍由 lib finally 负责立即 revoke。 */
  function abortExportOperation() {
    exportGeneration += 1;
    exportOperation?.controller.abort();
    exportOperation = null;
  }

  return Object.freeze({
    enter,
    clear,
    query,
    beginLoadOperation,
    isLoadOperationCurrent,
    acceptFiles,
    selectedFile,
    acceptReport,
    acceptReportNotFound,
    rejectLoad,
    rejectReport,
    finishLoadOperation,
    selectFile,
    nextPage,
    previousPage,
    beginScanOperation,
    isScanOperationCurrent,
    acceptScan,
    rejectScan,
    finishScanOperation,
    beginExportOperation,
    isExportOperationCurrent,
    acceptExport,
    rejectExport,
    finishExportOperation,
    isActionBusy,
    snapshot,
  });
}

/** 识别 owner 生命周期主动 Abort，正常离页不应显示成扫描故障。 */
export function isFileSecurityAbortError(error) {
  return error?.name === "AbortError";
}

/** 把不含 Token 的 owner 转为私有等值键。 */
function keyFromOwner(owner) {
  if (
    !owner?.readIdentity
    || !owner?.tenantId
    || owner.navigation !== NAVIGATION_ID
    || !owner.navigationOwner
  ) return "";
  return JSON.stringify([
    owner.readIdentity,
    owner.scanIdentity,
    owner.tenantId,
    owner.navigation,
    owner.navigationOwner,
    owner.canScan === true,
  ]);
}

/** 读取 user/tenant 双镜像；存在冲突时不能任选一个建立 owner。 */
function mirroredTenantId(authContext) {
  const userTenantId = String(authContext?.user?.tenant_id ?? "").trim();
  const tenantId = String(authContext?.tenant?.tenant_id ?? "").trim();
  // tenant 双镜像必须一致；冲突不能通过任意优先级掩盖。
  if (userTenantId && tenantId && userTenantId !== tenantId) {
    // 双镜像冲突说明认证上下文不自洽，不能任选一个 tenant 建立报告 owner。
    return "";
  }
  // 历史上下文允许缺一个镜像；两个都缺失时自然返回空并由 owner 失败关闭。
  return userTenantId || tenantId;
}

/** 规范权限数组；重复、非法或空值都失败关闭。 */
function normalizePermissionCodes(value) {
  if (!Array.isArray(value) || !value.length) return [];
  const normalized = value.map((item) => String(item ?? "").trim());
  if (
    normalized.some((item) => !/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(item))
    || new Set(normalized).size !== normalized.length
  ) return [];
  return normalized.sort();
}

/** 创建只含固定错误码、文案和 request_id 的页面错误。 */
function fixedPageError(error, fallback) {
  const code = safeErrorCode(error?.code) || "file.security_scan_unavailable";
  const messages = {
    "auth.permission_denied": "当前账号没有文件安全操作权限",
    "infra.validation_failed": "文件安全请求格式无效",
    "file.not_found": "当前租户中找不到该文件",
    "file.security_report_not_found": "该文件尚未生成安全报告",
    "file.security_not_ready": "文件尚未达到安全扫描条件",
    "file.security_scan_busy": "安全扫描任务繁忙，请稍后重试",
    "file.security_scan_unavailable": "文件安全扫描暂不可用",
    "file.model_access_blocked": "文件未通过模型上下文准入",
    "file.integrity_failed": "文件安全事实完整性校验失败",
    "file.security_response_invalid": "文件安全响应合同不完整",
    "file.security_export_invalid": "文件安全报告导出响应无效",
  };
  return Object.freeze({
    code,
    message: messages[code] ?? fallback,
    requestId: safeId(error?.requestId) || "-",
  });
}

/** 只允许统一错误码进入公开反馈。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "").trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}

/** 只允许有界资源或链路 ID 进入公开快照。 */
function safeId(value) {
  const normalized = String(value ?? "").trim();
  return /^[A-Za-z0-9_.:-]{1,160}$/.test(normalized) ? normalized : "";
}
