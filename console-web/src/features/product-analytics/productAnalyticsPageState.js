/** PAGE-117 授权 owner 与只读漏斗生命周期；租户、权限或导航变化都会销毁旧请求和 DOM。 */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 页面标识必须同时匹配 URL hash 与后端导航项，不能只凭前端路由直接获得漏斗读取能力。
const PAGE_ID = "growth-analytics";

/** 从实时权限快照生成读取身份；角色名称只用于展示，不能替代 product_analytics.read。 */
export function productAnalyticsReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.PRODUCT_ANALYTICS_READ);
}

/** 将 PAGE-117 绑定到镜像租户、精确权限快照及后端确认的当前导航项。 */
export function productAnalyticsPageOwner(authContext, consoleShellState) {
  const readIdentity = productAnalyticsReadIdentitySnapshot(authContext);
  const tenantId = mirroredTenantId(authContext);
  const permissions = permissionCodesFromAuthContext(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === PAGE_ID)
    : null;
  const required = normalizePermissionCodes(item?.required_permissions);
  const navigationPermissions = normalizePermissionCodes(navigation?.permission_codes);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  // 任一租户、权限、revision、fingerprint 或导航镜像不一致，都不得构造页面 owner。
  if (
    !readIdentity
    || !tenantId
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== PAGE_ID
    || navigation?.active_navigation_item_id !== PAGE_ID
    || item?.target_section !== PAGE_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.PRODUCT_ANALYTICS_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(permissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    page: PAGE_ID,
    tenantId,
    readIdentity,
    navigationOwner: JSON.stringify([
      item.id,
      item.target_section,
      required,
      permissions,
      revision,
      fingerprint,
    ]),
  });
}

/** 创建仅驻内存的漏斗状态机；owner key、AbortController 和代际不会暴露给渲染层。 */
export function createProductAnalyticsPageState({ clearDom = () => {} } = {}) {
  let ownerKey = "";
  let state = "loading";
  let data = null;
  let error = null;
  let generation = 0;
  let operation = null;

  /** 进入新的授权 owner；无效或变化的 owner 会先销毁旧租户状态和未决请求。 */
  function enter(owner) {
    const key = ownerKeyFor(owner);
    // 无法证明完整 owner 时立即清空，避免保留上一个租户的漏斗 DOM。
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    return true;
  }

  /** 为当前 owner 开始一次读取，并用页面刷新代际淘汰此前尚未完成的请求。 */
  function beginLoad(owner, refreshGeneration) {
    if (!owns(owner) || !Number.isSafeInteger(refreshGeneration) || refreshGeneration < 1) return null;
    // 同一 owner 的新读取也必须中止旧请求，避免较慢响应覆盖用户刚触发的刷新。
    operation?.controller.abort();
    generation += 1;
    operation = Object.freeze({
      ownerKey,
      generation,
      refreshGeneration,
      controller: new AbortController(),
    });
    state = "loading";
    error = null;
    return operation;
  }

  /** 同时核对 ticket、owner、请求代际和 Abort 状态，判断异步结果是否仍可提交。 */
  function isCurrent(ticket, owner, refreshGeneration) {
    return Boolean(
      ticket
      && operation === ticket
      && ticket.ownerKey === ownerKey
      && ticket.ownerKey === ownerKeyFor(owner)
      && ticket.generation === generation
      && ticket.refreshGeneration === refreshGeneration
      && !ticket.controller.signal.aborted,
    );
  }

  /** 只接收当前 ticket 对应租户的完整七阶段响应，迟到或跨租户结果直接丢弃。 */
  function accept(ticket, response, owner, refreshGeneration) {
    // tenant_id 和固定阶段数在状态写入前再次复核，不能只依赖客户端解析器。
    if (
      !isCurrent(ticket, owner, refreshGeneration)
      || response?.tenant_id !== owner?.tenantId
      || !Array.isArray(response?.stages)
      || response.stages.length !== 7
    ) return false;
    data = response;
    state = "ready";
    error = null;
    return true;
  }

  /** 仅为仍属当前 owner 的请求记录脱敏错误；旧请求失败不得覆盖新租户页面。 */
  function reject(ticket, cause, owner, refreshGeneration) {
    if (!isCurrent(ticket, owner, refreshGeneration)) return false;
    data = null;
    state = "error";
    error = fixedError(cause);
    return true;
  }

  /** 仅结束与当前 operation 相同的 ticket，迟到 finally 不能清掉后续请求。 */
  function finish(ticket) {
    if (operation === ticket) operation = null;
  }

  /** 同步清空 owner、数据和 DOM，随后中止旧请求，使身份变化形成强隔离边界。 */
  function clear() {
    const pending = operation;
    ownerKey = "";
    state = "loading";
    data = null;
    error = null;
    operation = null;
    generation += 1;
    try {
      clearDom();
    } finally {
      // 即使 DOM 清理回调异常，也必须中止旧请求，阻止其继续占用或回写旧 owner。
      pending?.controller.abort();
    }
  }

  /** 返回当前可渲染状态的冻结快照，不暴露 operation、令牌或 AbortController。 */
  function snapshot() {
    return Object.freeze({ state, data, error });
  }

  /** 比较完整 owner key，租户、权限 fingerprint 或导航任一变化都会失配。 */
  function owns(owner) {
    const key = ownerKeyFor(owner);
    return Boolean(key && key === ownerKey);
  }

  return Object.freeze({ enter, beginLoad, isCurrent, accept, reject, finish, clear, snapshot });
}

/** 识别由 owner 清理触发的 AbortError，使调用方静默丢弃而不是渲染失败。 */
export function isProductAnalyticsAbortError(error) {
  return error?.name === "AbortError";
}

/** 将经过校验的页面、租户、读取身份和导航 owner 编码为不可猜测的比较键。 */
function ownerKeyFor(owner) {
  // 缺少任一 owner 分量都表示授权证明不完整，不能退化为部分身份比较。
  if (
    !owner
    || owner.page !== PAGE_ID
    || !owner.tenantId
    || !owner.readIdentity
    || !owner.navigationOwner
  ) return "";
  // tenantId 只参与本地 owner 比较，不会作为 query 或请求正文发送给后端。
  return JSON.stringify([owner.page, owner.tenantId, owner.readIdentity, owner.navigationOwner]);
}

/** 只接受 user 与 tenant 两份认证镜像一致的租户标识，拒绝单边缺失或冲突。 */
function mirroredTenantId(authContext) {
  const userTenantId = String(authContext?.user?.tenant_id ?? "").trim();
  const tenantId = String(authContext?.tenant?.tenant_id ?? "").trim();
  // 双份租户镜像不一致时返回空值，让上层销毁页面 owner 而不是猜测可信一侧。
  return userTenantId && tenantId && userTenantId === tenantId ? tenantId : "";
}

/** 规范化后端权限集合并拒绝重复或非字符串项，保证 fingerprint 比较稳定。 */
function normalizePermissionCodes(value) {
  if (!Array.isArray(value) || value.some((code) => typeof code !== "string" || !code)) return null;
  const normalized = [...new Set(value)].sort();
  return normalized.length === value.length ? normalized : null;
}

/** 将异常裁剪为固定错误码和安全 request_id，禁止保存消息、诊断或响应对象。 */
function fixedError(error) {
  const code = String(error?.code ?? "product_analytics.unavailable");
  const requestId = String(error?.requestId ?? "-");
  return Object.freeze({
    code: /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(code)
      ? code
      : "product_analytics.unavailable",
    requestId: /^[A-Za-z0-9_.:-]{1,160}$/.test(requestId) ? requestId : "-",
  });
}
