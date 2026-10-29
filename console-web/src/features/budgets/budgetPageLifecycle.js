/**
 * 模块职责：为预算中心提供不含业务数据的身份快照、异步 owner 门禁和表单联动纯函数。
 * 安全边界：本模块不保存 Token、预算金额或租户对象；后端仍负责 RBAC、租户隔离和资源归属校验。
 */

import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";

/**
 * 从认证上下文生成 tenant、用户、角色和 Session 组成的稳定身份快照。
 * 任一身份字段变化都会让旧读取和旧写入结果失效，防止迟到响应写入新会话内存。
 */
export function budgetIdentitySnapshot(authContext) {
  // 预算 owner 必须绑定后端签发的读权限、revision 与指纹，不能再由角色名推导。
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.BILLING_BUDGET_READ);
}

/**
 * 判断 Budget GET 的不可变 owner 是否仍属于当前身份和主应用刷新代次。
 * 该门禁必须在写入规则快照、选择值或错误反馈之前执行；最终 DOM refreshId 检查不能替代它。
 */
export function isBudgetRefreshOwnerCurrent(owner, currentOwner) {
  return Boolean(
    owner
    && owner.identity
    && Number.isSafeInteger(owner.refreshGeneration)
    && owner.identity === text(currentOwner?.identity)
    && owner.refreshGeneration === currentOwner?.refreshGeneration,
  );
}

/**
 * 创建预算写操作生命周期。
 * 每次 begin 都推进 revision；身份、导航或全页刷新代次变化后，迟到的成功和异常都不能改反馈或触发 refresh。
 */
export function createBudgetMutationLifecycle() {
  let revision = 0;
  let current = null;
  return {
    /** 冻结本次写入 owner；不完整身份或非预算页失败关闭。 */
    begin({ identity, navigation, refreshGeneration }) {
      revision += 1;
      const normalizedIdentity = text(identity);
      const normalizedNavigation = text(navigation);
      if (
        !normalizedIdentity
        || normalizedNavigation !== "budgets"
        || !Number.isSafeInteger(refreshGeneration)
      ) {
        current = null;
        return null;
      }
      current = Object.freeze({
        revision,
        identity: normalizedIdentity,
        navigation: normalizedNavigation,
        refreshGeneration,
      });
      return current;
    },

    /** 核对对象引用、revision、身份、导航和刷新代次，任一不一致都拒绝旧回调。 */
    isCurrent(operation, owner) {
      return Boolean(
        operation
        && current === operation
        && operation.revision === revision
        && operation.identity === text(owner?.identity)
        && operation.navigation === text(owner?.navigation)
        && operation.refreshGeneration === owner?.refreshGeneration,
      );
    },

    /** 只结束调用方自己的操作，旧 finally 不能清除后来开始的新操作。 */
    finish(operation) {
      if (current === operation) {
        current = null;
      }
    },

    /** 身份或导航边界变化时同步推进 revision，使无法取消的 fetch 结果也失效。 */
    invalidate() {
      revision += 1;
      current = null;
    },

    /** 供后台定时刷新避让预算写入，避免重建 DOM 后制造重复提交入口。 */
    isBusy() {
      return Boolean(current);
    },
  };
}

/**
 * 从当前 scope 类型和可见选项解析应选 Scope ID 与可信 Environment 提示。
 * 该结果只用于表单联动，后端仍必须从 Key 记录恢复 Environment、Project 和 Application。
 */
export function selectBudgetScopeOption({ scopeType, currentScopeId, options }) {
  const normalizedScopeType = text(scopeType);
  const normalizedScopeId = text(currentScopeId);
  const matching = [];
  let selected = null;
  for (const option of options ?? []) {
    if (text(option?.scopeType) !== normalizedScopeType) {
      continue;
    }
    matching.push(option);
    if (!selected && text(option?.value) === normalizedScopeId) {
      selected = option;
    }
  }
  selected ??= matching[0] ?? null;
  return {
    scopeId: text(selected?.value),
    environmentId: normalizedScopeType === "virtual_key" ? text(selected?.environmentId) : "",
  };
}

/** 把可空输入规范化为字符串，避免 owner 比较发生隐式类型转换。 */
function text(value) {
  return String(value ?? "");
}
