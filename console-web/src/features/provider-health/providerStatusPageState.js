import {
  authorizationIdentitySnapshot,
  hasPermission,
  PERMISSIONS,
} from "../../lib/permissions.js";

/** Provider 状态页允许进入 URL 的固定筛选顺序，确保等价筛选得到同一个 owner key。 */
const FILTER_KEYS = ["provider_id", "provider_type", "health_status", "provider_status", "limit", "offset"];

/**
 * 生成完整授权身份摘要。
 * authorizationIdentitySnapshot 已包含 tenant/user/session/permission revision/fingerprint；第二项权限单独校验，
 * 防止只持有健康权限的旧 Session 创建请求 ticket。
 */
export function providerStatusIdentitySnapshot(authContext) {
  if (!hasPermission(authContext, PERMISSIONS.REQUEST_LOG_READ)) return "";
  // 第二项调用日志权限成立后仍由共享 helper 复核租户、用户、Session、revision 与健康读权限。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.PROVIDER_HEALTH_READ);
}

/** 归一化状态页筛选，未知键和空值不会进入请求身份或网络参数。 */
export function canonicalProviderStatusFilters(filters = {}) {
  const normalized = {};
  for (const key of FILTER_KEYS) {
    const value = filters[key];
    if (value === undefined || value === null || String(value).trim() === "") continue;
    if (["limit", "offset"].includes(key)) {
      // 只把规范十进制转换为 Number；指数、符号或浮点保持原文交给后端统一分页错误处理，不能静默改义。
      const text = String(value).trim();
      normalized[key] = /^(0|[1-9]\d*)$/.test(text) && Number.isSafeInteger(Number(text))
        ? Number(text)
        : text;
      continue;
    }
    // provider_id 是大小写敏感的资源标识；只有固定枚举筛选才做小写归一化。
    normalized[key] = key === "provider_id"
      ? String(value).trim()
      : String(value).trim().toLowerCase();
  }
  return Object.freeze(normalized);
}

/** 把固定顺序筛选编码为响应 owner key，不包含 tenant_id、Token 或业务响应。 */
export function providerStatusFilterKey(filters = {}) {
  const canonical = canonicalProviderStatusFilters(filters);
  return JSON.stringify(FILTER_KEYS.filter((key) => key in canonical).map((key) => [key, canonical[key]]));
}

/**
 * 创建 PAGE-088 GET 生命周期。
 * Abort 负责尽快停止网络，generation 与完整 owner 负责拒绝无法真正取消的迟到成功、401/403 和普通错误。
 */
export function createProviderStatusPageState() {
  let generation = 0;
  let operation = null;

  /** 当前 owner 必须与 ticket 的每个身份维度一致，不能仅比较 AbortSignal。 */
  function owns(ticket, current) {
    return Boolean(
      ticket
      && operation === ticket
      && ticket.generation === generation
      && !ticket.controller.signal.aborted
      && ticket.identity === current?.identity
      && ticket.navigation === current?.navigation
      && ticket.filterKey === current?.filterKey
      && ticket.token === current?.token,
    );
  }

  return {
    /** 开始新读取前同步取消旧读取，并冻结完整身份、导航和筛选。 */
    begin({ identity, navigation, filters, token }) {
      if (!identity || navigation !== "provider-health" || !token) return null;
      operation?.controller.abort();
      const canonicalFilters = canonicalProviderStatusFilters(filters);
      operation = Object.freeze({
        generation: ++generation,
        identity,
        navigation,
        filters: canonicalFilters,
        filterKey: providerStatusFilterKey(canonicalFilters),
        token,
        controller: new AbortController(),
      });
      return operation;
    },

    /** 接纳响应前由调用方传入最新 owner；成功和错误必须走同一判断。 */
    owns,

    /** 当前请求结束后释放引用；较老请求不得清理后创建的新 ticket。 */
    finish(ticket) {
      if (operation === ticket && ticket.generation === generation) operation = null;
    },

    /** 离页、筛选、登出、权限变化或探测写操作开始时立即淘汰全部旧回调。 */
    clear() {
      generation += 1;
      operation?.controller.abort();
      operation = null;
    },

    /** 测试快照故意排除 Token、identity、筛选正文和 AbortController。 */
    snapshot() {
      return Object.freeze({ generation, pending: Boolean(operation) });
    },
  };
}
