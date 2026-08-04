/**
 * 模块职责：维护 PAGE-085 权限矩阵的读取、草稿、CAS 写入和迟到响应生命周期。
 * 安全边界：tenant、授权指纹和 grantable 上限只消费后端快照；缺项、跨身份或畸形 DTO 均失败关闭。
 */

import {
  createPermissionRequestId,
  fixedRoleCodes,
  isMatrixHash,
} from "../../lib/permissions.js";

// 以下常量冻结页面入口、公开 ID、权限码和风险枚举，避免服务端异常值进入 owner 或 DOM。
const ROLE_PERMISSION_NAVIGATION = "role-permissions";
const SAFE_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,256}$/;
const PERMISSION_CODE_PATTERN = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const RISK_LEVELS = new Set(["read", "write", "critical"]);

/**
 * 创建 PAGE-085 的内存状态与 GET/PATCH 双生命周期。
 *
 * 完整矩阵、草稿、CAS revision 和 operation request_id 都只驻留闭包；离页、注销或授权指纹变化
 * 必须调用 clear。GET 与 PATCH 使用独立 AbortController，定时读取不能取消结果未知的写操作。
 */
export function createRolePermissionPageState({ requestIdFactory = createPermissionRequestId } = {}) {
  let matrix = null;
  let latestMatrix = null;
  let selectedRoleId = null;
  let categoryFilter = "all";
  let draft = null;
  let status = "idle";
  let loadError = null;
  let actionError = null;
  let feedback = "";
  let serverChanged = false;
  let loadRevision = 0;
  let mutationRevision = 0;
  let loadOperation = null;
  let mutationOperation = null;
  let pendingMutation = null;

  /** 销毁当前身份的全部矩阵事实，并使忽略 AbortSignal 的迟到 Promise 同样失效。 */
  function clear() {
    loadOperation?.controller.abort();
    mutationOperation?.controller.abort();
    loadRevision += 1;
    mutationRevision += 1;
    loadOperation = null;
    mutationOperation = null;
    pendingMutation = null;
    matrix = null;
    latestMatrix = null;
    selectedRoleId = null;
    categoryFilter = "all";
    draft = null;
    status = "idle";
    loadError = null;
    actionError = null;
    feedback = "";
    serverChanged = false;
  }

  /** 为一次只读矩阵刷新冻结完整授权 owner。 */
  function beginLoad(owner) {
    const normalizedOwner = normalizeLoadOwner(owner);
    loadOperation?.controller.abort();
    loadRevision += 1;
    if (!normalizedOwner) {
      loadOperation = null;
      return null;
    }
    const operation = Object.freeze({
      revision: loadRevision,
      ...normalizedOwner,
      controller: new AbortController(),
    });
    loadOperation = operation;
    if (!matrix) status = "loading";
    loadError = null;
    return operation;
  }

  /** 核对 GET 操作、授权身份、导航、租户和主应用刷新代次。 */
  function isLoadCurrent(operation, owner) {
    const normalizedOwner = normalizeLoadOwner(owner);
    return Boolean(
      operation
      && normalizedOwner
      && loadOperation === operation
      && operation.revision === loadRevision
      && operation.identity === normalizedOwner.identity
      && operation.tenantId === normalizedOwner.tenantId
      && operation.navigation === normalizedOwner.navigation
      && operation.refreshGeneration === normalizedOwner.refreshGeneration,
    );
  }

  /**
   * 接受经过完整结构校验的权限矩阵。
   *
   * 有未保存草稿时只记录服务器新版本，不覆盖草稿；无草稿时原子替换整包目录。
   */
  function acceptLoad(operation, payload, owner) {
    if (!isLoadCurrent(operation, owner)) return false;
    const normalized = normalizeMatrix(payload, operation.tenantId);
    if (!normalized) {
      rejectLoad(operation, {
        code: "rbac.matrix_integrity_failed",
        message: "权限矩阵响应不完整",
        requestId: payload?.request_id ?? "-",
      }, owner);
      return false;
    }
    if (draft && matrix) {
      latestMatrix = normalized;
      const latestRole = normalized.roles.find((role) => role.role_id === draft.roleId);
      serverChanged = !latestRole
        || latestRole.permission_revision !== draft.baseRevision
        || latestRole.matrix_sha256 !== draft.baseHash;
    } else {
      matrix = normalized;
      latestMatrix = null;
      selectedRoleId = normalized.roles.some((role) => role.role_id === selectedRoleId)
        ? selectedRoleId
        : (normalized.roles.find((role) => !role.immutable)?.role_id ?? normalized.roles[0]?.role_id ?? null);
      serverChanged = false;
    }
    status = "ok";
    loadError = null;
    loadOperation = null;
    return true;
  }

  /** 将当前 GET 的安全错误写入状态；已有草稿和矩阵不会被依赖故障清空。 */
  function rejectLoad(operation, error, owner) {
    if (!isLoadCurrent(operation, owner)) return false;
    loadOperation = null;
    // Abort 仅表示生命周期已取消，不覆盖已有矩阵，也不展示为后端错误。
    if (error?.name === "AbortError") return false;
    loadError = safeError(error, "rbac.unavailable", "权限矩阵暂不可用");
    status = matrix ? "ok" : "error";
    return true;
  }

  /** 选择角色；存在其他角色未保存草稿时拒绝切换，避免静默丢失修改。 */
  function selectRole(roleId) {
    const role = findRole(roleId);
    if (!role || mutationOperation || (draft && draft.roleId !== role.role_id)) return false;
    selectedRoleId = role.role_id;
    actionError = null;
    feedback = "";
    return true;
  }

  /** 应用权限分类筛选；未知分类收敛为 all，不影响真实矩阵集合。 */
  function setCategoryFilter(category) {
    const normalized = text(category).trim();
    const categories = new Set(matrix?.permissions.map((permission) => permission.category) ?? []);
    categoryFilter = normalized === "all" || categories.has(normalized) ? normalized : "all";
    return categoryFilter;
  }

  /**
   * 编辑一个角色的单项授权。
   *
   * 可编辑边界只消费后端 grantable_permission_codes；admin immutable 和越界 code 均失败关闭。
   */
  function setPermission(roleId, permissionCode, granted) {
    const role = findRole(roleId);
    const code = text(permissionCode).trim();
    if (
      !role
      || role.immutable
      || !role.grantable_permission_codes.includes(code)
      || mutationOperation
      || serverChanged
      || (draft && draft.roleId !== role.role_id)
    ) return false;
    selectedRoleId = role.role_id;
    const baseCodes = role.permission_codes;
    const nextCodes = new Set(draft?.permissionCodes ?? baseCodes);
    if (granted) nextCodes.add(code);
    else nextCodes.delete(code);
    const normalizedCodes = [...nextCodes].sort();
    if (sameStrings(normalizedCodes, baseCodes)) {
      draft = null;
      pendingMutation = null;
    } else {
      draft = {
        roleId: role.role_id,
        baseRevision: role.permission_revision,
        baseHash: role.matrix_sha256,
        permissionCodes: normalizedCodes,
      };
      // 用户在一次已确定结果之后继续编辑属于新意图，不得复用旧 operation request_id。
      if (pendingMutation && pendingMutation.fingerprint !== mutationFingerprint(draft)) {
        pendingMutation = null;
      }
    }
    actionError = null;
    feedback = "";
    return true;
  }

  /** 放弃未保存草稿；若定时 GET 已发现新版本，则同时采用最新安全快照。 */
  function discardDraft() {
    if (mutationOperation) return false;
    draft = null;
    pendingMutation = null;
    actionError = null;
    feedback = "";
    if (latestMatrix) {
      matrix = latestMatrix;
      latestMatrix = null;
    }
    serverChanged = false;
    return true;
  }

  /**
   * 冻结一次 PATCH ticket。
   *
   * 网络结果未知时，相同规范草稿复用 request_id；新编辑或明确 HTTP 结果使用新 ID。
   */
  function beginMutation(owner) {
    const normalizedOwner = normalizeActionOwner(owner);
    if (!normalizedOwner || !draft || mutationOperation || serverChanged) return null;
    const role = findRole(draft.roleId);
    // immutable 或 revision 已漂移时不创建 PATCH ticket，避免前端绕过 admin/CAS 保护。
    if (!role || role.immutable || role.permission_revision !== draft.baseRevision) return null;
    const fingerprint = mutationFingerprint(draft);
    if (!pendingMutation || pendingMutation.fingerprint !== fingerprint) {
      pendingMutation = {
        fingerprint,
        requestId: requestIdFactory("patch"),
      };
    }
    mutationRevision += 1;
    mutationOperation = Object.freeze({
      revision: mutationRevision,
      ...normalizedOwner,
      roleId: draft.roleId,
      expectedRevision: draft.baseRevision,
      basePermissionCodes: Object.freeze([...role.permission_codes]),
      permissionCodes: Object.freeze([...draft.permissionCodes]),
      fingerprint,
      requestId: pendingMutation.requestId,
      controller: new AbortController(),
    });
    actionError = null;
    feedback = "正在保存角色权限";
    return mutationOperation;
  }

  /** 判断 PATCH 结果是否仍属于当前授权 owner 和冻结草稿。 */
  function isMutationCurrent(operation, owner) {
    const normalizedOwner = normalizeActionOwner(owner);
    return Boolean(
      operation
      && normalizedOwner
      && mutationOperation === operation
      && operation.revision === mutationRevision
      && operation.identity === normalizedOwner.identity
      && operation.tenantId === normalizedOwner.tenantId
      && operation.navigation === normalizedOwner.navigation
      && draft
      && operation.fingerprint === mutationFingerprint(draft),
    );
  }

  /** 接受 PATCH 成功并只替换目标角色；响应角色仍需复用 GET 的完整约束校验。 */
  function acceptMutation(operation, payload, owner) {
    if (!isMutationCurrent(operation, owner)) return false;
    const currentRole = findRole(operation.roleId);
    const rawRole = payload?.role ?? payload?.updated_role;
    const role = normalizeRole(rawRole, operation.tenantId, new Set(matrix.permissions.map((item) => item.code)));
    const expectedChanged = !sameStrings(operation.permissionCodes, operation.basePermissionCodes);
    const expectedRevision = operation.expectedRevision + (expectedChanged ? 1 : 0);
    if (
      !currentRole
      || !role
      || role.role_id !== operation.roleId
      || role.code !== currentRole.code
      || role.name !== currentRole.name
      || role.immutable !== currentRole.immutable
      || !sameStrings(role.grantable_permission_codes, currentRole.grantable_permission_codes)
      || !sameStrings(role.permission_codes, operation.permissionCodes)
      || role.permission_revision !== expectedRevision
      || payload?.changed !== expectedChanged
      || (expectedChanged ? !safeId(payload?.event_id) : payload?.event_id !== null)
    ) {
      rejectMutation(operation, {
        code: "rbac.matrix_integrity_failed",
        message: "权限更新响应不完整",
        requestId: payload?.request_id ?? operation.requestId,
        status: 500,
      }, owner);
      return false;
    }
    matrix = {
      ...matrix,
      roles: matrix.roles.map((item) => item.role_id === role.role_id ? role : item),
      request_id: payload?.request_id ?? matrix.request_id,
      trace_id: payload?.trace_id ?? matrix.trace_id,
    };
    draft = null;
    latestMatrix = null;
    pendingMutation = null;
    mutationOperation = null;
    serverChanged = false;
    actionError = null;
    feedback = payload?.changed === false ? "权限未变化" : "角色权限已保存";
    return true;
  }

  /**
   * 处理 PATCH 失败。
   *
   * 网络结果未知保留稳定 request_id；明确 HTTP 错误释放 ticket。409 保留草稿并要求重新加载。
   */
  function rejectMutation(operation, error, owner) {
    if (!isMutationCurrent(operation, owner)) return false;
    mutationOperation = null;
    // 用户离页或身份刷新导致的取消不应清空草稿，也不产生伪造的保存失败反馈。
    if (error?.name === "AbortError") return false;
    const isNetworkUnknown = !Number.isInteger(error?.status) || error.status <= 0;
    if (!isNetworkUnknown) pendingMutation = null;
    actionError = safeError(error, "rbac.unavailable", "角色权限保存失败");
    feedback = "";
    // 409 证明服务器 revision 已前进，保留草稿但锁定再次提交，等待显式刷新。
    if (error?.status === 409) serverChanged = true;
    return true;
  }

  /** 返回不含 controller、Token 和完整 owner 的只读渲染快照。 */
  function snapshot() {
    const selectedRole = findRole(selectedRoleId);
    return {
      state: status,
      catalogRevision: matrix?.catalog_revision ?? null,
      permissions: matrix?.permissions ?? [],
      roles: matrix?.roles ?? [],
      selectedRoleId,
      selectedRole,
      categoryFilter,
      draftPermissionCodes: draft?.roleId === selectedRoleId ? [...draft.permissionCodes] : null,
      dirty: Boolean(draft),
      busy: Boolean(mutationOperation),
      serverChanged,
      loadError,
      actionError,
      feedback,
      requestId: matrix?.request_id ?? loadError?.requestId ?? "-",
      traceId: matrix?.trace_id ?? "-",
    };
  }

  /** 按稳定 ID 查找当前租户矩阵角色；未命中返回 null，绝不按角色 code 猜测。 */
  function findRole(roleId) {
    return matrix?.roles.find((role) => role.role_id === roleId) ?? null;
  }

  return {
    clear,
    beginLoad,
    isLoadCurrent,
    acceptLoad,
    rejectLoad,
    selectRole,
    setCategoryFilter,
    setPermission,
    discardDraft,
    beginMutation,
    isMutationCurrent,
    acceptMutation,
    rejectMutation,
    snapshot,
  };
}

/** 规范整个 GET 响应；六角色、tenant owner、权限集合和 hash 任一异常都整包拒绝。 */
function normalizeMatrix(payload, tenantId) {
  const catalogRevision = Number(payload?.catalog_revision);
  if (!Number.isSafeInteger(catalogRevision) || catalogRevision < 1 || !Array.isArray(payload?.permissions) || !Array.isArray(payload?.roles)) {
    return null;
  }
  const permissions = payload.permissions.map((permission) => normalizePermission(permission, tenantId));
  // 任一权限 DTO 无效就拒绝整包，不能把截断目录当作权限撤销或安全快照。
  if (permissions.some((permission) => !permission)) return null;
  const permissionCodes = new Set(permissions.map((permission) => permission.code));
  const permissionIds = new Set(permissions.map((permission) => permission.permission_id));
  // code 与 ID 均须一一对应，重复项可能造成 checkbox 与 PATCH 集合解释不一致。
  if (permissionCodes.size !== permissions.length || permissionIds.size !== permissions.length) return null;
  const roles = payload.roles.map((role) => normalizeRole(role, tenantId, permissionCodes));
  // 六角色任一项损坏时整包失败关闭，不能只渲染仍可解析的角色列。
  if (roles.some((role) => !role)) return null;
  const roleCodes = roles.map((role) => role.code);
  const roleIds = new Set(roles.map((role) => role.role_id));
  if (
    new Set(roleCodes).size !== roles.length
    || roleIds.size !== roles.length
    || !sameStrings([...roleCodes].sort(), [...fixedRoleCodes()].sort())
  ) {
    return null;
  }
  const order = new Map(fixedRoleCodes().map((code, index) => [code, index]));
  return Object.freeze({
    catalog_revision: catalogRevision,
    permissions: Object.freeze([...permissions].sort((left, right) => (
      left.category.localeCompare(right.category) || left.code.localeCompare(right.code)
    ))),
    roles: Object.freeze([...roles].sort((left, right) => order.get(left.code) - order.get(right.code))),
    request_id: safeId(payload?.request_id) || "-",
    trace_id: safeId(payload?.trace_id) || "-",
  });
}

/** 规范 Permission DTO，禁止跨租户、动态状态和不受支持风险枚举进入 DOM。 */
function normalizePermission(permission, tenantId) {
  const permissionId = safeId(permission?.permission_id);
  const ownerTenantId = safeId(permission?.tenant_id);
  const code = text(permission?.code).trim();
  const category = boundedText(permission?.category, 80);
  const name = boundedText(permission?.name, 160);
  const description = boundedText(permission?.description, 500);
  const riskLevel = text(permission?.risk_level).trim();
  if (
    !permissionId
    || ownerTenantId !== tenantId
    || !PERMISSION_CODE_PATTERN.test(code)
    || !category
    || !name
    || !description
    || !RISK_LEVELS.has(riskLevel)
    || permission?.status !== "active"
  ) return null;
  return Object.freeze({
    permission_id: permissionId,
    tenant_id: ownerTenantId,
    code,
    category,
    name,
    description,
    risk_level: riskLevel,
    status: "active",
  });
}

/** 规范角色矩阵 DTO；grantable 是前端唯一可编辑上限来源。 */
function normalizeRole(role, tenantId, catalogCodes) {
  const roleId = safeId(role?.role_id);
  const ownerTenantId = safeId(role?.tenant_id);
  const code = text(role?.code).trim();
  const name = boundedText(role?.name, 160);
  const revision = Number(role?.permission_revision);
  const permissionCodes = normalizeCodeSubset(role?.permission_codes, catalogCodes);
  const grantableCodes = normalizeCodeSubset(role?.grantable_permission_codes, catalogCodes);
  const immutable = role?.immutable === true || role?.is_permission_immutable === true;
  const completeCatalog = [...catalogCodes].sort();
  if (
    !roleId
    || ownerTenantId !== tenantId
    || !fixedRoleCodes().includes(code)
    || !name
    || !Number.isSafeInteger(revision)
    || revision < 1
    || !isMatrixHash(role?.matrix_sha256)
    || !permissionCodes
    || !grantableCodes
    || (!immutable && permissionCodes.some((permission) => !grantableCodes.includes(permission)))
    || (code === "admin") !== immutable
    || (immutable && (
      !sameStrings(permissionCodes, completeCatalog)
      || !sameStrings(grantableCodes, completeCatalog)
    ))
  ) return null;
  return Object.freeze({
    role_id: roleId,
    tenant_id: ownerTenantId,
    code,
    name,
    permission_revision: revision,
    matrix_sha256: role.matrix_sha256,
    immutable,
    permission_codes: Object.freeze(permissionCodes),
    grantable_permission_codes: Object.freeze(grantableCodes),
  });
}

/** 规范权限子集；未知、重复或非数组值全部拒绝。 */
function normalizeCodeSubset(values, catalogCodes) {
  if (!Array.isArray(values) || values.length > catalogCodes.size) return null;
  const normalized = values.map((value) => text(value).trim());
  if (
    normalized.some((value) => !catalogCodes.has(value))
    || new Set(normalized).size !== normalized.length
  ) return null;
  return [...normalized].sort();
}

/** 规范 GET owner；完整身份和租户只来自认证上下文，不能从 API payload 恢复。 */
function normalizeLoadOwner(owner) {
  const actionOwner = normalizeActionOwner(owner);
  if (!actionOwner || !Number.isSafeInteger(owner?.refreshGeneration) || owner.refreshGeneration < 1) return null;
  return { ...actionOwner, refreshGeneration: owner.refreshGeneration };
}

/** 规范写 owner；只有后端授权的 PAGE-085 当前导航可拥有 PATCH。 */
function normalizeActionOwner(owner) {
  const identity = text(owner?.identity);
  const tenantId = safeId(owner?.tenantId);
  const navigation = text(owner?.navigation);
  // 写 owner 必须同时绑定授权指纹、tenant 和 PAGE-085 入口，避免跨页或跨租户复用 ticket。
  return identity && tenantId && navigation === ROLE_PERMISSION_NAVIGATION
    ? { identity, tenantId, navigation }
    : null;
}

/** 为 CAS 草稿生成规范指纹，不含完整角色目录或用户信息。 */
function mutationFingerprint(value) {
  return JSON.stringify([value.roleId, value.baseRevision, value.permissionCodes]);
}

/** 把异常收敛为页面允许展示的统一字段。 */
function safeError(error, fallbackCode, fallbackMessage) {
  return Object.freeze({
    status: Number.isInteger(error?.status) ? error.status : 0,
    code: /^[a-z][a-z0-9_.]{1,127}$/.test(text(error?.code)) ? error.code : fallbackCode,
    message: boundedText(error?.message, 300) || fallbackMessage,
    requestId: safeId(error?.requestId ?? error?.request_id) || "-",
    traceId: safeId(error?.traceId ?? error?.trace_id) || "-",
  });
}

/** 判断两个已排序字符串数组是否完全一致。 */
function sameStrings(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

/** 只允许公开有界 ID 进入页面状态。 */
function safeId(value) {
  const normalized = text(value).trim();
  return SAFE_ID_PATTERN.test(normalized) ? normalized : "";
}

/** 读取有界展示文本，控制字符和超长文本整项拒绝。 */
function boundedText(value, maximum) {
  const normalized = text(value).trim();
  return normalized && normalized.length <= maximum && !/[\u0000-\u001f\u007f]/.test(normalized)
    ? normalized
    : "";
}

/** 把可空值稳定转换为字符串。 */
function text(value) {
  return String(value ?? "");
}
