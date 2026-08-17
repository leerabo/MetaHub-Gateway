/** PAGE-118 只持有当前租户请求；身份漂移时先清空数据与 DOM，再中止旧请求。 */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 页面标识只接受后端导航镜像中的客户健康入口，不能由前端角色自行推导权限。
const PAGE_ID = "customer-health";

/** 生成包含客户健康读取权限的完整身份快照，用于拒绝权限漂移后的迟到响应。 */
export function customerHealthReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.CUSTOMER_HEALTH_READ);
}

/** 仅当实时权限与后端导航镜像完全一致时构造页面归属，任何字段漂移都失败关闭。 */
export function customerHealthPageOwner(authContext, consoleShellState) {
  const readIdentity = customerHealthReadIdentitySnapshot(authContext);
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
  if (
    !readIdentity
    || !tenantId
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== PAGE_ID
    || navigation?.active_navigation_item_id !== PAGE_ID
    || item?.target_section !== PAGE_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.CUSTOMER_HEALTH_READ])
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

/** 创建租户绑定的页面状态机，以请求票据隔离刷新、切租户和退出登录产生的迟到响应。 */
export function createCustomerHealthPageState({ clearDom = () => {} } = {}) {
  let ownerKey = "";
  let state = "loading";
  let data = null;
  let error = null;
  let generation = 0;
  let operation = null;

  /** 进入指定 owner；归属变化时同步销毁旧租户数据和待完成请求。 */
  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    return true;
  }

  /** 为当前 owner 创建唯一加载票据，并中止同页上一代仍在执行的请求。 */
  function beginLoad(owner, refreshGeneration) {
    if (!owns(owner) || !Number.isSafeInteger(refreshGeneration) || refreshGeneration < 1) return null;
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

  /** 同时核对 owner、页面代次和刷新代次，证明返回结果仍属于当前请求。 */
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

  /** 只接纳当前票据且 tenant_id 精确相等的六信号响应，拒绝跨租户数据进入状态。 */
  function accept(ticket, response, owner, refreshGeneration) {
    if (
      !isCurrent(ticket, owner, refreshGeneration)
      || response?.tenant_id !== owner?.tenantId
      || !Array.isArray(response?.signals)
      || response.signals.length !== 6
    ) return false;
    data = response;
    state = "ready";
    error = null;
    return true;
  }

  /** 只把当前票据的固定错误摘要写入页面，旧请求错误不得覆盖新身份状态。 */
  function reject(ticket, cause, owner, refreshGeneration) {
    if (!isCurrent(ticket, owner, refreshGeneration)) return false;
    data = null;
    state = "error";
    error = fixedError(cause);
    return true;
  }

  /** 仅清理仍为当前代次的操作引用，避免旧请求结束时误删新请求票据。 */
  function finish(ticket) {
    if (operation === ticket) operation = null;
  }

  /** 原子清空 owner、数据、错误和 DOM，随后中止已失效的网络请求。 */
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
      pending?.controller.abort();
    }
  }

  /** 返回只读页面快照，防止渲染器直接改写请求生命周期状态。 */
  function snapshot() {
    return Object.freeze({ state, data, error });
  }

  /** 检查候选 owner 是否与当前租户和授权导航归属完全一致。 */
  function owns(owner) {
    const key = ownerKeyFor(owner);
    return Boolean(key && key === ownerKey);
  }

  return Object.freeze({ enter, beginLoad, isCurrent, accept, reject, finish, clear, snapshot });
}

/** 识别主动取消错误，避免身份切换产生的正常中止被展示为服务失败。 */
export function isCustomerHealthAbortError(error) {
  return error?.name === "AbortError";
}

/** 将租户、身份和后端导航归属编码为稳定键，任一变化都会形成不同 owner。 */
function ownerKeyFor(owner) {
  if (
    !owner
    || owner.page !== PAGE_ID
    || !owner.tenantId
    || !owner.readIdentity
    || !owner.navigationOwner
  ) return "";
  // owner 键只包含公开标识与授权快照，不保存 Token、用户正文或响应数据。
  return JSON.stringify([owner.page, owner.tenantId, owner.readIdentity, owner.navigationOwner]);
}

/** 仅接受用户租户与租户上下文完全相同的标识，缺失或不一致时失败关闭。 */
function mirroredTenantId(authContext) {
  const userTenantId = String(authContext?.user?.tenant_id ?? "").trim();
  const tenantId = String(authContext?.tenant?.tenant_id ?? "").trim();
  // 两处 tenant_id 必须同时存在且完全相同，禁止任一镜像单独决定页面归属。
  return userTenantId && tenantId && userTenantId === tenantId ? tenantId : "";
}

/** 规范化权限集合并拒绝空值与重复项，保证后端镜像比较不受顺序影响。 */
function normalizePermissionCodes(value) {
  if (!Array.isArray(value) || value.some((code) => typeof code !== "string" || !code)) return null;
  const normalized = [...new Set(value)].sort();
  return normalized.length === value.length ? normalized : null;
}

/** 只保留固定格式的错误码和 request_id，不把异常正文或敏感字段带入页面。 */
function fixedError(error) {
  const code = String(error?.code ?? "customer_health.unavailable");
  const requestId = String(error?.requestId ?? "-");
  return Object.freeze({
    code: /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(code)
      ? code
      : "customer_health.unavailable",
    requestId: /^[A-Za-z0-9_.:-]{1,160}$/.test(requestId) ? requestId : "-",
  });
}
