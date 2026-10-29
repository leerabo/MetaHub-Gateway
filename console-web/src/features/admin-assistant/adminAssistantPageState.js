// 本模块只维护 PAGE-107 的浏览器内存状态与授权 owner，不持久化问题、回答或认证凭据。
import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 导航标识必须与后端返回的受权菜单精确一致，前端不得凭路由存在自行放行页面。
const NAVIGATION_ID = "admin-assistant";

/**
 * PAGE-107 的基础读取身份只绑定专用 query permission。
 * 角色名称不参与判断；完整权限集合仍会单独进入 page owner，使任一源权限撤销都淘汰旧回答。
 */
export function adminAssistantReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.ADMIN_ASSISTANT_QUERY);
}

/**
 * 合并 /auth/me 与 /console/navigation 为 PAGE-107 完整 owner。
 *
 * tenant、用户、Session、权限全集、revision、fingerprint、active item 或导航合同任一漂移都返回 null。
 * 这只是浏览器内存所有权判断；真实 base/source 权限仍由 POST 在读取正文前及事务内重复校验。
 */
export function adminAssistantPageOwner(authContext, consoleShellState) {
  const identity = adminAssistantReadIdentitySnapshot(authContext);
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
    !identity
    || !tenantId
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== NAVIGATION_ID
    || navigation?.active_navigation_item_id !== NAVIGATION_ID
    || item?.target_section !== NAVIGATION_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(requiredPermissions) !== JSON.stringify([PERMISSIONS.ADMIN_ASSISTANT_QUERY])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(authPermissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    identity,
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
  });
}

/**
 * 创建 PAGE-107 的单轮查询内存仓库。
 *
 * question、answer、source label、AbortController 和 operation ticket 都留在闭包；snapshot 仅供当前页面
 * 同步 render/hydrate。离页后 leave 会推进 generation、取消请求并从 DOM 清除全部正文。
 */
export function createAdminAssistantPageState({ clearDom = () => {} } = {}) {
  let ownerKey = "";
  let generation = 0;
  let draft = "";
  let status = "idle";
  let response = null;
  let error = null;
  let operation = null;

  /** 新 owner 只有完整导航和认证快照都成立时才可进入。 */
  function enter(owner) {
    const nextOwnerKey = ownerKeyFor(owner);
    if (!nextOwnerKey) {
      leave();
      return false;
    }
    if (ownerKey && ownerKey !== nextOwnerKey) leave();
    ownerKey = nextOwnerKey;
    return true;
  }

  /**
   * 离开 owner：先让 generation/ticket 失效并 abort，再擦除正文和 DOM。
   * generation 在 abort 前推进，确保同步 Abort 监听器也无法把旧响应写回清理中的页面。
   */
  function leave() {
    invalidate({ resetOwner: true });
  }

  /** 用户清空仍保留当前 owner，但会取消在途 attempt；服务端已经接收的 attempt 仍按后端规则完成审计。 */
  function clear(owner) {
    if (owner && !ownerMatches(owner)) return false;
    invalidate({ resetOwner: false });
    return true;
  }

  /** 输入变化开始新单轮问题，上一轮 answer 不会被拼回新请求或作为会话历史保留。 */
  function setDraft(owner, value) {
    if (!ownerMatches(owner) || operation || typeof value !== "string" || [...value].length > 2000) return false;
    // 编辑新问题时可暂时对照上一轮结果；真正 beginQuery 会先清回答，且请求体仍只有当前 question。
    draft = value;
    status = value.trim() ? "draft" : "idle";
    return true;
  }

  /** 单个 owner 同时只允许一个 POST；这只是防连点体验，不提供后端幂等或审计合并。 */
  function beginQuery(owner) {
    if (!ownerMatches(owner) || operation || !draft.trim()) return null;
    generation += 1;
    response = null;
    error = null;
    status = "submitting";
    const controller = new AbortController();
    operation = Object.freeze({
      kind: "query",
      ownerKey,
      generation,
      question: draft,
      controller,
      signal: controller.signal,
    });
    return operation;
  }

  /** 取消只停止浏览器等待并淘汰回调，不声称撤销服务端已经开始的独立审计 attempt。 */
  function cancel(owner) {
    if (!ownerMatches(owner) || !operation) return false;
    const controller = operation.controller;
    operation = null;
    generation += 1;
    controller.abort();
    status = draft.trim() ? "draft" : "idle";
    error = null;
    response = null;
    return true;
  }

  /** exact client DTO 只有在 ticket 与实时 owner 都未漂移时才能成为可见回答。 */
  function acceptQuery(ticket, value, owner) {
    if (!isCurrent(ticket, owner) || !value?.answer || !Array.isArray(value.sources)) return false;
    operation = null;
    response = value;
    error = null;
    status = "answered";
    return true;
  }

  /**
   * 错误只保存 client 已折叠的固定公开字段。网络结果未知时禁止自动重试；用户必须显式清空或修改问题，
   * 因为后端不接受 Idempotency-Key，重复 attempt 会合法地产生第二组审计事实。
   */
  function rejectQuery(ticket, candidate, owner) {
    if (!isCurrent(ticket, owner)) return false;
    operation = null;
    response = null;
    // 主动取消只清理当前浏览器等待，不把它伪装成服务端失败或允许旧回调恢复正文。
    if (isAdminAssistantAbortError(candidate)) {
      status = draft.trim() ? "draft" : "idle";
      error = null;
      return true;
    }
    error = Object.freeze({
      status: Number(candidate?.status) || 0,
      code: safeErrorCode(candidate?.code),
      message: safeMessage(candidate?.message),
      requestId: safeChain(candidate?.requestId),
      traceId: safeChain(candidate?.traceId),
    });
    status = error.code === "admin_assistant.network_unavailable" ? "uncertain" : "error";
    return true;
  }

  /** 当前 source 只能按响应数组下标读取，DOM dataset 不能提供或替换 source/href。 */
  function sourceAt(owner, index) {
    if (!ownerMatches(owner) || !Number.isSafeInteger(index) || index < 0) return null;
    return response?.sources?.[index] ?? null;
  }

  /** background refresh 同 owner 时保留正文；refresh 仍先重读 auth/navigation，以便动态撤权触发 leave。 */
  function blocksShellDomReplacement() {
    return Boolean(ownerKey && (operation || draft || response || error));
  }

  function snapshot() {
    return Object.freeze({
      status,
      draft,
      response,
      error,
      busy: Boolean(operation),
      canSubmit: Boolean(ownerKey && !operation && draft.trim()),
      hasContent: Boolean(draft || response || error || operation),
    });
  }

  /** 同时核对 ticket、generation 与实时 owner，阻止撤权或切租户后的迟到响应回写。 */
  function isCurrent(ticket, owner) {
    return Boolean(
      ticket
      && operation === ticket
      && !ticket.signal.aborted
      && ticket.generation === generation
      && ticket.ownerKey === ownerKey
      && ownerMatches(owner),
    );
  }

  /** 把调用方提供的最新授权上下文与闭包 owner 做精确匹配，不接受角色名推断。 */
  function ownerMatches(owner) {
    return Boolean(ownerKey && ownerKey === ownerKeyFor(owner));
  }

  /** 使在途操作失效并擦除敏感正文；离页时还会一并清空 owner。 */
  function invalidate({ resetOwner }) {
    generation += 1;
    const controller = operation?.controller;
    operation = null;
    controller?.abort();
    draft = "";
    status = "idle";
    response = null;
    error = null;
    if (resetOwner) ownerKey = "";
    clearDom();
  }

  return Object.freeze({
    enter,
    leave,
    clear,
    setDraft,
    beginQuery,
    cancel,
    acceptQuery,
    rejectQuery,
    sourceAt,
    snapshot,
    blocksShellDomReplacement,
    isCurrent,
  });
}

/** 仅识别浏览器标准 AbortError，其他网络异常仍按结果未知的固定错误处理。 */
export function isAdminAssistantAbortError(error) {
  return error?.name === "AbortError";
}

/** 生成只存在内存中的 owner 键，缺少任一认证或导航事实就拒绝建立所有权。 */
function ownerKeyFor(owner) {
  // owner 必须同时具备认证身份、租户和后端导航证明，缺项时不能展示或接收任何正文。
  if (!owner?.identity || !owner?.tenantId || !owner?.navigationOwner) return "";
  return JSON.stringify([
    owner.identity,
    owner.tenantId,
    owner.permissionCodes,
    owner.navigationOwner,
  ]);
}

/** 要求用户声明和租户上下文镜像一致，避免前端在歧义租户下保留查询结果。 */
function mirroredTenantId(authContext) {
  const userTenant = String(authContext?.user?.tenant_id ?? "");
  const tenant = String(authContext?.tenant?.tenant_id ?? "");
  // 仅接受两处完全一致且格式受限的 tenant_id，页面状态不能自行修补或猜测租户。
  return userTenant && userTenant === tenant && /^[A-Za-z0-9_-]{1,128}$/.test(userTenant)
    ? userTenant
    : "";
}

/** 规范化后端权限快照并拒绝空集、重复项或非法权限码。 */
function normalizePermissionCodes(value) {
  if (!Array.isArray(value) || value.length === 0) return null;
  const normalized = value.map((code) => String(code ?? ""));
  if (
    normalized.some((code) => !/^[a-z][a-z0-9_.]{1,127}$/.test(code))
    || new Set(normalized).size !== normalized.length
  ) return null;
  return normalized.sort();
}

/** 仅允许认证域和管理助手域的公开错误码进入页面状态。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "");
  // 非白名单错误码统一收敛，避免上游或内部异常标识泄漏到浏览器。
  return /^(?:auth|admin_assistant)\.[a-z0-9_.]{1,120}$/.test(normalized)
    ? normalized
    : "admin_assistant.source_unavailable";
}

/** 校验公开错误文案长度和控制字符，非法输入替换为固定消息。 */
function safeMessage(value) {
  const normalized = String(value ?? "");
  return normalized && normalized.length <= 512 && !/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/.test(normalized)
    ? normalized
    : "管理助手请求失败";
}

/** 仅保留可公开展示的 request_id/trace_id 字符，异常链路值使用占位符。 */
function safeChain(value) {
  const normalized = String(value ?? "");
  return /^[A-Za-z0-9_.:-]{1,160}$/.test(normalized) ? normalized : "-";
}
