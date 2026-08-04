/**
 * 模块职责：维护 PAGE-096 的身份、导航、刷新代次与 Abort 生命周期。
 * 安全边界：状态只能保存分类安全摘要和固定错误，永远不接收或保存 textarea 正文。
 */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 导航 ID、目标区和允许状态均为后端合同镜像，只用于拒绝迟到前端回调，不能替代后端 RBAC。
const NAVIGATION_ID = "intent-security";
const PAGE_STATES = new Set(["idle", "submitting", "success", "error"]);

/**
 * 从实时认证与后端导航快照构造完整页面 owner。
 * tenant、user、Session、权限 revision/fingerprint 由 identity 冻结；导航项和刷新代次再独立绑定。
 */
export function intentSecurityPageOwner(
  authContext,
  consoleShellState,
  { refreshId = 0, currentRefreshId = refreshId } = {},
) {
  const identity = authorizationIdentitySnapshot(authContext, PERMISSIONS.INTENT_RISK_CLASSIFY);
  const tenantId = mirroredTenantId(authContext);
  const activeItemId = String(consoleShellState?.activeNavigationItemId ?? "");
  const navigation = consoleShellState?.navigation;
  const navigationItem = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((item) => item?.id === NAVIGATION_ID)
    : null;
  const requiredPermissions = normalizeRequiredPermissions(navigationItem?.required_permissions);
  const authPermissionCodes = permissionCodesFromAuthContext(authContext);
  const navigationPermissionCodes = normalizeRequiredPermissions(navigation?.permission_codes);
  const authRevision = permissionRevision(authContext);
  const authFingerprint = authorizationFingerprint(authContext);
  if (
    !identity
    || !tenantId
    || !hasPermission(authContext, PERMISSIONS.INTENT_RISK_CLASSIFY)
    || activeItemId !== NAVIGATION_ID
    || navigation?.active_navigation_item_id !== NAVIGATION_ID
    || navigationItem?.target_section !== NAVIGATION_ID
    || navigationItem?.visible === false
    || navigationItem?.disabled === true
    || !requiredPermissions.includes(PERMISSIONS.INTENT_RISK_CLASSIFY)
    || JSON.stringify(navigationPermissionCodes) !== JSON.stringify(authPermissionCodes)
    || Number(navigation?.permission_revision) !== authRevision
    || String(navigation?.authorization_fingerprint ?? "") !== authFingerprint
    || !Number.isSafeInteger(refreshId)
    || !Number.isSafeInteger(currentRefreshId)
  ) {
    // 任一后端导航或实时权限字段不完整都返回空 owner，页面不得产生工作台 DOM 或网络请求。
    return null;
  }
  const navigationOwner = JSON.stringify([
    navigationItem.id,
    navigationItem.target_section,
    requiredPermissions,
    authRevision,
    authFingerprint,
  ]);
  return Object.freeze({
    identity,
    tenantId,
    navigation: NAVIGATION_ID,
    navigationOwner,
    refreshId,
    currentRefreshId,
  });
}

/**
 * 创建只保存安全摘要的 PAGE-096 状态机。
 * enter 绑定页面 owner；begin 为每次 POST 创建独立 AbortController；generation 是迟到响应最终门禁。
 */
export function createIntentSecurityPageState() {
  let ownerKey = "";
  let generation = 0;
  let activeOperation = null;
  let status = "idle";
  let result = null;
  let error = null;

  /** 取消当前请求并推进代次；Abort 只优化资源释放，generation 才负责拒绝忽略取消的 fetch。 */
  function invalidateOperation() {
    activeOperation?.controller.abort();
    activeOperation = null;
    generation += 1;
  }

  /** 清除结果和固定错误，不产生正文恢复或浏览器存储副作用。 */
  function resetView() {
    status = "idle";
    result = null;
    error = null;
  }

  /** 核对 ticket、完整 owner 和刷新代次仍一致。 */
  function operationMatches(operation, owner) {
    return Boolean(
      operation
      && activeOperation === operation
      && !operation.signal.aborted
      && operation.generation === generation
      && operation.ownerKey === ownerKey
      && operation.ownerKey === keyFromOwner(owner)
      && operation.refreshId === owner?.refreshId
      && owner?.refreshId === owner?.currentRefreshId,
    );
  }

  return Object.freeze({
    /** 绑定当前后端授权 owner；身份或导航变化时先取消并销毁上一 owner 的全部页面事实。 */
    enter(owner) {
      const nextOwnerKey = keyFromOwner(owner);
      if (!nextOwnerKey) {
        this.leave();
        return false;
      }
      if (ownerKey !== nextOwnerKey) {
        invalidateOperation();
        resetView();
        ownerKey = nextOwnerKey;
      } else if (activeOperation && activeOperation.refreshId !== owner.refreshId) {
        // 同一身份的新全页刷新也会接管 generation；旧 POST 必须 abort，不能留下永久 submitting 状态。
        invalidateOperation();
        resetView();
      }
      return true;
    },

    /** 开始一次分类；调用方必须先清空 textarea，并且只能传入不含正文的完整 owner。 */
    begin(owner) {
      const nextOwnerKey = keyFromOwner(owner);
      if (!nextOwnerKey || nextOwnerKey !== ownerKey || owner?.refreshId !== owner?.currentRefreshId) {
        // 不完整或迟到 owner 不能创建 Abort ticket，更不能由旧 DOM 发起分类请求。
        return null;
      }
      invalidateOperation();
      const controller = new AbortController();
      activeOperation = Object.freeze({
        generation,
        ownerKey,
        refreshId: owner.refreshId,
        controller,
        signal: controller.signal,
      });
      status = "submitting";
      result = null;
      error = null;
      return activeOperation;
    },

    /** 判断一个异步回调是否仍属于当前 tenant/Session/RBAC/navigation/refresh owner。 */
    isCurrent(operation, owner) {
      return operationMatches(operation, owner);
    },

    /** 接纳已由专用 client 严格投影的成功摘要；状态中不保留请求输入。 */
    accept(operation, payload, owner) {
      if (!operationMatches(operation, owner) || !payload?.intent_risk_result) return false;
      status = "success";
      result = payload;
      error = null;
      activeOperation = null;
      return true;
    },

    /** 接纳固定错误字段；原始异常、response body 和正文都不会被复制进状态。 */
    reject(operation, reason, owner) {
      if (!operationMatches(operation, owner)) return false;
      status = "error";
      result = null;
      error = Object.freeze({
        code: safeErrorCode(reason?.code) || "intent_risk.unavailable",
        message: fixedErrorMessage(reason?.code),
        requestId: safeId(reason?.requestId) || "-",
      });
      activeOperation = null;
      return true;
    },

    /** 用户显式取消当前请求；立即 abort，并把结果、错误和提交态一并清空。 */
    cancel() {
      const hadOperation = Boolean(activeOperation);
      invalidateOperation();
      resetView();
      return hadOperation;
    },

    /** 清空当前安全结果；若请求仍在途则按取消处理，不能保留迟到写回入口。 */
    clear() {
      invalidateOperation();
      resetView();
    },

    /** 离页、登出、401、403 或 owner 漂移时销毁全部页面内存。 */
    leave() {
      invalidateOperation();
      resetView();
      ownerKey = "";
    },

    /** 返回不含正文、Token、owner 指纹和 controller 的只读渲染快照。 */
    snapshot() {
      return Object.freeze({
        status: PAGE_STATES.has(status) ? status : "error",
        result,
        error,
      });
    },

    /** 告知全页定时器当前是否有分类请求，避免重建 DOM 后丢失取消入口。 */
    isBusy() {
      return Boolean(activeOperation);
    },
  });
}

/** 把完整 owner 转为仅用于等值比较的内存键，不把该键暴露到 DOM、日志或 Storage。 */
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
  ]);
}

/** 读取 tenant 镜像并要求 user 与 tenant DTO 一致，防止损坏认证响应形成双 owner。 */
function mirroredTenantId(authContext) {
  const userTenantId = String(authContext?.user?.tenant_id ?? "").trim();
  const tenantId = String(authContext?.tenant?.tenant_id ?? "").trim();
  // 两份 tenant owner 不一致说明认证 DTO 损坏，不能任选一方继续创建页面身份。
  if (userTenantId && tenantId && userTenantId !== tenantId) return "";
  // 兼容只有一处 tenant 镜像的既有认证合同，但绝不从导航或客户端输入恢复 owner。
  return userTenantId || tenantId;
}

/** 规范后端导航声明的权限集合；重复或非法权限使 PAGE-096 失败关闭。 */
function normalizeRequiredPermissions(value) {
  if (!Array.isArray(value) || !value.length) return [];
  const normalized = value.map((item) => String(item ?? "").trim());
  if (
    normalized.some((item) => !/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(item))
    || new Set(normalized).size !== normalized.length
  ) return [];
  return normalized.sort();
}

/** 只允许统一错误码进入状态；非法服务端值使用本地固定兜底。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "").trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}

/** 只允许有界公开 request_id 进入错误区域。 */
function safeId(value) {
  const normalized = String(value ?? "").trim();
  return /^[A-Za-z0-9_.:-]{1,160}$/.test(normalized) ? normalized : "";
}

/** 把错误码映射为固定产品文案，禁止把服务端自由消息或攻击文本带入页面。 */
function fixedErrorMessage(code) {
  const messages = {
    "intent_risk.media_type_unsupported": "分类请求格式不受支持",
    "intent_risk.payload_too_large": "分类文本超过请求上限",
    "intent_risk.payload_invalid": "分类请求结构不合法",
    "intent_risk.text_invalid": "分类文本不合法",
    "intent_risk.classifier_unavailable": "本地分类器暂不可用",
    "intent_risk.result_store_unavailable": "分类结果暂无法保存",
    "intent_risk.response_invalid": "分类响应不完整",
  };
  return messages[code] ?? "语义风险分类请求未完成";
}
