/**
 * PAGE-090 Adapter 规范页的筛选和请求 owner 生命周期。
 *
 * 状态只驻留内存，不写 URL、Storage 或遥测；Abort 用于尽快停止网络，完整 owner 比较负责拒绝
 * 无法真正取消的迟到成功、401、403 和普通错误。
 */

import {
  authorizationIdentitySnapshot,
  PERMISSIONS,
} from "../../lib/permissions.js";

// 三组白名单分别约束产品视图、后端状态和冻结 Provider 类型，篡改值只能回退而不能扩展查询面。
const VIEWS = new Set(["default", "customer", "migration"]);
const STATUSES = new Set(["", "active", "optional"]);
const PROVIDER_TYPES = new Set([
  "",
  "zhipu",
  "dashscope",
  "wenxin",
  "volcengine",
  "hunyuan",
  "deepseek",
  "openai_compatible",
  "custom",
  "migration_compatible",
]);

/**
 * 生成包含 tenant、user、session、permission revision 和权限指纹的身份摘要。
 * 返回空串表示当前身份不能拥有 PAGE-090 请求，前端不能依据角色名称补权。
 */
export function adapterSpecIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.PROVIDER_HEALTH_READ);
}

/** 归一化只驻留页面内存的三个筛选，篡改值回退到失败关闭的固定默认值。 */
export function canonicalAdapterSpecFilters(filters = {}) {
  const view = String(filters.view ?? "default").trim().toLowerCase();
  const status = String(filters.status ?? "").trim().toLowerCase();
  const providerType = String(filters.provider_type ?? "").trim().toLowerCase();
  return Object.freeze({
    view: VIEWS.has(view) ? view : "default",
    status: STATUSES.has(status) ? status : "",
    provider_type: PROVIDER_TYPES.has(providerType) ? providerType : "",
  });
}

/**
 * 把页面视图映射为后端公开筛选。
 * 客户 Endpoint 同时包含两个 classification，因此读取公开目录后在内存中取并集；tenant_id 永不进入查询。
 */
export function adapterSpecApiFilters(filters = {}) {
  const normalized = canonicalAdapterSpecFilters(filters);
  const query = {};
  if (normalized.view === "default") query.classification = "model_vendor_cloud";
  if (normalized.view === "migration") query.classification = "migration_platform";
  if (normalized.status) query.status = normalized.status;
  return Object.freeze(query);
}

/** 为 owner 生成稳定筛选键；键不包含 Token、租户标识或响应正文。 */
export function adapterSpecFilterKey(filters = {}) {
  const normalized = canonicalAdapterSpecFilters(filters);
  return JSON.stringify([
    ["view", normalized.view],
    ["status", normalized.status],
    ["provider_type", normalized.provider_type],
  ]);
}

/**
 * 创建 PAGE-090 的单请求生命周期仓库。
 * 每次 begin/clear 都推进 generation，使浏览器或测试 stub 忽略 Abort 时仍无法回填旧身份页面。
 */
export function createAdapterSpecPageState() {
  let generation = 0;
  let operation = null;

  /** 比较当前授权 owner 的全部维度；任何一项漂移都拒绝接纳结果。 */
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
    /** 开始当前 owner 的读取，并同步取消此前筛选或身份拥有的请求。 */
    begin({ identity, navigation, filters, token }) {
      if (!identity || navigation !== "adapter-specs" || !token) return null;
      operation?.controller.abort();
      const canonicalFilters = canonicalAdapterSpecFilters(filters);
      operation = Object.freeze({
        generation: ++generation,
        identity,
        navigation,
        filters: canonicalFilters,
        filterKey: adapterSpecFilterKey(canonicalFilters),
        token,
        controller: new AbortController(),
      });
      return operation;
    },

    owns,

    /** 只释放当前 ticket；较慢旧请求不得清空较新的活动 operation。 */
    finish(ticket) {
      if (operation === ticket && ticket.generation === generation) operation = null;
    },

    /** 离页、登出、换租户、权限或筛选变化时立即终止并使全部旧回调失效。 */
    clear() {
      generation += 1;
      operation?.controller.abort();
      operation = null;
    },

    /** 诊断快照故意只暴露代次和忙碌态，避免 Token、身份或筛选进入日志。 */
    snapshot() {
      return Object.freeze({ generation, pending: Boolean(operation) });
    },
  };
}
