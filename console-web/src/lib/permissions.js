import { AuthApiError } from "./auth.js";

/**
 * 控制台使用的稳定权限码。
 *
 * 这些常量只用于对后端返回的 permission_codes 做精确匹配，绝不根据前端角色名推导权限。
 */
export const PERMISSIONS = Object.freeze({
  CONSOLE_BASIC_READ: "console.basic.read",
  DASHBOARD_READ: "dashboard.read",
  TENANT_SETTINGS_READ: "tenant.settings.read",
  // PAGE-121 tenant data lifecycle capabilities are intentionally independent.
  TENANT_DATA_READ: "tenant_data.read",
  TENANT_DATA_EXPORT: "tenant_data.export",
  TENANT_DATA_DOWNLOAD: "tenant_data.download",
  TENANT_DATA_DELETE: "tenant_data.delete",
  USER_MANAGE: "user.manage",
  RBAC_MATRIX_MANAGE: "rbac.matrix.manage",
  ENVIRONMENT_READ: "environment.read",
  ENVIRONMENT_WRITE: "environment.write",
  PROJECT_READ: "project.read",
  PROJECT_WRITE: "project.write",
  VIRTUAL_KEY_READ: "virtual_key.read",
  VIRTUAL_KEY_WRITE: "virtual_key.write",
  PROVIDER_READ: "provider.read",
  PROVIDER_WRITE: "provider.write",
  PROVIDER_CATALOG_READ: "provider_catalog.read",
  PROVIDER_HEALTH_READ: "provider.health.read",
  PROVIDER_HEALTH_PROBE: "provider.health.probe",
  PROVIDER_ERROR_MAP_READ: "provider_error_map.read",
  PROVIDER_ERROR_MAP_WRITE: "provider_error_map.write",
  MODEL_READ: "model.read",
  MODEL_WRITE: "model.write",
  MODEL_PRICE_READ: "model_price.read",
  MODEL_PRICE_WRITE: "model_price.write",
  ROUTING_READ: "routing.read",
  ROUTING_WRITE: "routing.write",
  MODEL_QUALITY_READ: "model_quality.read",
  MODEL_QUALITY_WRITE: "model_quality.write",
  POLICY_READ: "policy.read",
  POLICY_WRITE: "policy.write",
  POLICY_SIMULATE: "policy.simulate",
  POLICY_PUBLISH: "policy.publish",
  // PAGE-095 读取与应用独立授权；应用仍由后端按模板类别复核领域权限和写锁内撤权。
  POLICY_TEMPLATE_READ: "policy_template.read",
  POLICY_TEMPLATE_APPLY: "policy_template.apply",
  DLP_READ: "dlp.read",
  DLP_WRITE: "dlp.write",
  // PAGE-096 只使用独立分类权限，不能由 DLP 写权限或 security_admin 角色名推导。
  INTENT_RISK_CLASSIFY: "intent_risk.classify",
  // PAGE-098 会话风险读取与处置独立授权；manage 不隐含 read，POST 必须同时具备两项。
  SESSION_RISK_READ: "session_risk.read",
  SESSION_RISK_MANAGE: "session_risk.manage",
  // PAGE-101/PAGE-102 读取与上传独立授权；write 不隐含 read，按钮投影不能替代 POST 后端校验。
  FILE_READ: "file.read",
  FILE_WRITE: "file.write",
  // PAGE-103 扫描会写安全事实并消耗离线 OCR 资源，必须与 file.read 组合且不能由角色名推导。
  FILE_SECURITY_SCAN: "file.security.scan",
  // PAGE-104 read/write are independent; task results additionally require current file.read.
  BATCH_READ: "batch.read",
  BATCH_WRITE: "batch.write",
  // PAGE-105 读取与创建独立授权；创建还需 Project/Environment/Routing/Model 读取组合权限。
  EXPERIMENT_READ: "experiment.read",
  EXPERIMENT_WRITE: "experiment.write",
  // PAGE-106 读取、策略写入和缓存失效三项能力独立；按钮投影不能替代 Control API 实时 RBAC。
  RESPONSE_CACHE_READ: "response_cache.read",
  RESPONSE_CACHE_WRITE: "response_cache.write",
  RESPONSE_CACHE_INVALIDATE: "response_cache.invalidate",
  // PAGE-107 只授予进入只读助手的基础能力；任何来源事实仍要求对应页面的原始读取权限。
  ADMIN_ASSISTANT_QUERY: "admin_assistant.query",
  // PAGE-108/109 的读取与决策独立；每条任务还必须与预算或会话风险目标域权限求交。
  APPROVAL_READ: "approval.read",
  APPROVAL_DECIDE: "approval.decide",
  REQUEST_LOG_READ: "request_log.read",
  REQUEST_EXPLANATION_READ: "request_explanation.read",
  FEEDBACK_USE: "feedback.use",
  BILLING_USAGE_READ: "billing.usage.read",
  BILLING_USAGE_EXPORT: "billing.usage.export",
  BILLING_FINOPS_READ: "billing.finops.read",
  BILLING_FINOPS_WRITE: "billing.finops.write",
  BILLING_BUDGET_READ: "billing.budget.read",
  BILLING_BUDGET_WRITE: "billing.budget.write",
  BILLING_ANOMALY_READ: "billing.anomaly.read",
  BILLING_ANOMALY_WRITE: "billing.anomaly.write",
  // PAGE-110/111 套餐与额度读写四项能力独立授权；前端入口不能替代 Control API 实时 RBAC。
  BILLING_PLAN_READ: "billing.plan.read",
  BILLING_PLAN_WRITE: "billing.plan.write",
  BILLING_QUOTA_READ: "billing.quota.read",
  BILLING_QUOTA_WRITE: "billing.quota.write",
  // PAGE-112 读取、申请和审核独立授权；approve 仍需与 billing.finops.write 求交。
  BILLING_DISPUTE_READ: "billing.dispute.read",
  BILLING_DISPUTE_CREATE: "billing.dispute.create",
  BILLING_DISPUTE_REVIEW: "billing.dispute.review",
  // PAGE-114 MCP 读取、Server 管理、策略管理、调用和审批均为独立后端能力。
  MCP_READ: "mcp.read",
  MCP_SERVER_MANAGE: "mcp.server.manage",
  MCP_POLICY_MANAGE: "mcp.policy.manage",
  MCP_NETWORK_MANAGE: "mcp.network.manage",
  MCP_INVOKE: "mcp.invoke",
  MCP_APPROVE: "mcp.approve",
  // PAGE-116 本地 Endpoint 读取、原子创建和受限测试独立授权，不能借用 Provider/KeyPool 权限。
  LOCAL_ENDPOINT_READ: "local_endpoint.read",
  LOCAL_ENDPOINT_MANAGE: "local_endpoint.manage",
  LOCAL_ENDPOINT_TEST: "local_endpoint.test",
  // PRD-120 Workspace Gateway 读取和管理独立授权；Platform Fleet 使用另一凭据域。
  EDGE_DEPLOYMENT_READ: "edge.deployment.read",
  EDGE_DEPLOYMENT_MANAGE: "edge.deployment.manage",
  // PRD-127 upgrade policy and rollback are intentionally separate from deployment lifecycle.
  EDGE_UPGRADE_READ: "edge.upgrade.read",
  EDGE_UPGRADE_MANAGE: "edge.upgrade.manage",
  EDGE_ROLLBACK_MANAGE: "edge.rollback.manage",
  // PRD-128 License 只控制容量和商业功能；不得参与 RBAC 或推导数据访问权限。
  EDGE_LICENSE_READ: "edge.license.read",
  EDGE_LICENSE_REFRESH: "edge.license.refresh",
  EDGE_LICENSE_IMPORT: "edge.license.import",
  // PRD-129 backup creation, drill management, and restore approval remain independent.
  EDGE_BACKUP_READ: "edge.backup.read",
  EDGE_BACKUP_CREATE: "edge.backup.create",
  EDGE_DRILL_MANAGE: "edge.drill.manage",
  EDGE_RESTORE_APPROVE: "edge.restore.approve",
  // PRD-130 offline media read, transfer, and conflict resolution are independent grants.
  EDGE_OFFLINE_BUNDLE_READ: "edge.offline_bundle.read",
  EDGE_OFFLINE_BUNDLE_TRANSFER: "edge.offline_bundle.transfer",
  EDGE_OFFLINE_CONFLICT_RESOLVE: "edge.offline_conflict.resolve",
  MIGRATION_READ: "migration.read",
  MIGRATION_MANAGE: "migration.manage",
  // PRD-122 遥测状态、策略和账务聚合分别授权，不能从 Deployment 权限推导。
  EDGE_TELEMETRY_READ: "edge.telemetry.read",
  EDGE_TELEMETRY_MANAGE: "edge.telemetry.manage",
  EDGE_USAGE_READ: "edge.usage.read",
  EDGE_SECRET_REFERENCE_READ: "edge.secret_reference.read",
  // PRD-124 安装介质读取和安装生命周期管理独立授权；页面可见性不能替代后端校验。
  EDGE_INSTALLER_READ: "edge.installer.read",
  EDGE_INSTALLATION_MANAGE: "edge.installation.manage",
  // PRD-125 验证、交付签署和支持包摘要分别授权；本地支持包同意仍由客户环境复核。
  EDGE_VALIDATION_READ: "edge.validation.read",
  EDGE_VALIDATION_MANAGE: "edge.validation.manage",
  EDGE_DELIVERY_ACCEPT: "edge.delivery.accept",
  EDGE_SUPPORT_BUNDLE_READ: "edge.support_bundle.read",
  // PRD-126 health and diagnostic approval are independent from Deployment management.
  EDGE_HEALTH_READ: "edge.health.read",
  EDGE_DIAGNOSTIC_READ: "edge.diagnostic.read",
  EDGE_DIAGNOSTIC_APPROVE: "edge.diagnostic.approve",
  // PAGE-117 产品激活漏斗是独立只读 capability；不能由 dashboard、feedback 或角色名推导。
  PRODUCT_ANALYTICS_READ: "product_analytics.read",
  // PAGE-118 客户健康只读取版本化聚合；不能由 Dashboard、Billing、User 或角色名推导。
  CUSTOMER_HEALTH_READ: "customer_health.read",
  // PAGE-119 读取、初始化、客户、白标和两类附件能力分别授权，任何按钮都不能由角色名推导。
  PARTNER_PORTAL_READ: "partner_portal.read",
  PARTNER_PORTAL_CONFIGURE: "partner_portal.configure",
  PARTNER_CUSTOMER_MANAGE: "partner_customer.manage",
  PARTNER_BRANDING_MANAGE: "partner_branding.manage",
  PARTNER_COMMISSION_EXPORT: "partner_commission.export",
  PARTNER_DELIVERY_DOWNLOAD: "partner_delivery.download",
  AUDIT_READ: "audit.read",
  AUDIT_EXPORT: "audit.export",
  WEBHOOK_MANAGE: "webhook.manage",
  DEVELOPER_PORTAL_READ: "developer_portal.read",
  DEBUG_CONSOLE_USE: "debug_console.use",
  // PAGE-093 只凭后端实时 Chat 权限开放；不得由 viewer/developer 角色名在前端补权。
  CHAT_USE: "chat.use",
  // PAGE-094 的读取、写入与 PAGE-093 调用分别授权；三者不能按角色名互相推导或替代。
  PROMPT_TEMPLATE_READ: "prompt_template.read",
  PROMPT_TEMPLATE_WRITE: "prompt_template.write",
  PROMPT_TEMPLATE_USE: "prompt_template.use",
  SYSTEM_CONFIG_READ: "system.config.read",
});

// 这些常量冻结后端权限合同、公开 ID、矩阵哈希和六角色目录；它们只做校验，不按角色补权。
const PERMISSION_CODE_PATTERN = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const SAFE_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,256}$/;
const MATRIX_HASH_PATTERN = /^[0-9a-f]{64}$/;
const FIXED_ROLE_CODES = Object.freeze([
  "admin",
  "developer",
  "viewer",
  "finance_admin",
  "security_admin",
  "ops_admin",
]);

let requestSequence = 0;

/**
 * 从认证响应读取严格权限集合。
 *
 * 顶层字段是新合同的权威镜像，user 内字段只用于同一后端响应的兼容读取；字段缺失、重复、
 * 非规范或两处不一致时返回空集合并失败关闭，绝不根据 role 恢复历史权限。
 */
export function permissionCodesFromAuthContext(authContext) {
  const topLevel = authContext?.permission_codes;
  const userLevel = authContext?.user?.permission_codes;
  const source = topLevel ?? userLevel;
  if (!Array.isArray(source)) return Object.freeze([]);
  const normalized = normalizePermissionCodes(source);
  if (!normalized) return Object.freeze([]);
  if (topLevel !== undefined && userLevel !== undefined) {
    const mirrored = normalizePermissionCodes(userLevel);
    if (!mirrored || JSON.stringify(normalized) !== JSON.stringify(mirrored)) {
      return Object.freeze([]);
    }
  }
  return Object.freeze(normalized);
}

/** 精确判断当前后端权限快照是否包含指定权限，不接受角色名或通配符。 */
export function hasPermission(authContext, permissionCode) {
  if (!PERMISSION_CODE_PATTERN.test(String(permissionCode ?? ""))) return false;
  return permissionCodesFromAuthContext(authContext).includes(permissionCode);
}

/** 判断当前后端权限快照是否同时包含全部指定权限。 */
export function hasEveryPermission(authContext, permissionCodes) {
  return Array.isArray(permissionCodes)
    && permissionCodes.length > 0
    && permissionCodes.every((code) => hasPermission(authContext, code));
}

/**
 * 读取后端签发的授权指纹。
 *
 * 指纹只参与 owner 等值判断，不进入 DOM、日志或遥测；顶层和 user 镜像不一致时失败关闭。
 */
export function authorizationFingerprint(authContext) {
  return mirroredScalar(
    authContext?.authorization_fingerprint,
    authContext?.user?.authorization_fingerprint,
    (value) => SAFE_ID_PATTERN.test(value),
  );
}

/** 读取当前角色矩阵 revision；非法或镜像不一致时返回 0。 */
export function permissionRevision(authContext) {
  const value = mirroredScalar(
    authContext?.permission_revision,
    authContext?.user?.permission_revision,
    (candidate) => Number.isSafeInteger(Number(candidate)) && Number(candidate) >= 1,
  );
  return value ? Number(value) : 0;
}

/**
 * 生成不含 Token 与业务正文的授权 owner。
 *
 * tenant、user、session、角色、revision、指纹及目标 permission 任一改变都会淘汰迟到响应。
 */
export function authorizationIdentitySnapshot(authContext, requiredPermission) {
  if (!hasPermission(authContext, requiredPermission)) return "";
  const tenantId = text(authContext?.user?.tenant_id ?? authContext?.tenant?.tenant_id).trim();
  const userId = text(authContext?.user?.user_id ?? authContext?.user?.email).trim();
  const role = text(authContext?.user?.role).trim();
  const sessionId = text(authContext?.session?.session_id).trim();
  const sessionRevision = text(
    authContext?.session?.session_revision
      ?? authContext?.session?.revision
      ?? authContext?.session_revision,
  ).trim();
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  // owner 只有在身份、Session 和数据库矩阵快照都完整时生成，缺项必须使页面失权。
  return tenantId && userId && role && sessionId && revision && fingerprint
    ? JSON.stringify([
        tenantId,
        userId,
        role,
        sessionId,
        sessionRevision,
        revision,
        fingerprint,
        requiredPermission,
      ])
    : "";
}

/**
 * 校验并裁剪后端导航权限快照。
 *
 * 导航必须与 /auth/me 的 permission 集合、revision 和 fingerprint 完全一致；每个入口还必须声明
 * 非空 required_permissions 且当前用户全部具备。任一合同漂移都整包失败，不回退 required_roles。
 */
export function authorizeNavigationSnapshot(navigation, authContext) {
  const authCodes = permissionCodesFromAuthContext(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const revision = permissionRevision(authContext);
  const navigationCodes = normalizePermissionCodes(navigation?.permission_codes);
  const navigationFingerprint = text(navigation?.authorization_fingerprint).trim();
  const navigationRevision = Number(navigation?.permission_revision);
  if (
    !authCodes.length
    || !fingerprint
    || !revision
    || !navigationCodes
    || JSON.stringify(authCodes) !== JSON.stringify(navigationCodes)
    || navigationFingerprint !== fingerprint
    || navigationRevision !== revision
    || !Array.isArray(navigation?.navigation_items)
  ) {
    // 导航与 /auth/me 任一漂移都拒绝整包，绝不回退 required_roles 或本地角色常量。
    throw permissionClientError("rbac.matrix_integrity_failed", "控制台授权快照不一致", 500);
  }
  const items = navigation.navigation_items.filter((item) => {
    const required = normalizePermissionCodes(item?.required_permissions);
    return required
      && required.length > 0
      && required.every((code) => authCodes.includes(code));
  });
  return Object.freeze({
    ...navigation,
    permission_codes: authCodes,
    permission_revision: revision,
    authorization_fingerprint: fingerprint,
    navigation_items: items,
  });
}

/** 查询当前租户的完整权限目录与六角色矩阵。 */
export async function getPermissions({ controlApiUrl, token, signal, requestId = createPermissionRequestId("get") }) {
  return permissionRequest(`${controlApiUrl}/permissions`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  }, requestId);
}

/**
 * 完整替换一个角色的权限集合。
 *
 * tenant、actor、grantable 边界和角色代码均不由前端提交；permission_codes 在发送前严格去重排序。
 */
export async function replaceRolePermissions({
  controlApiUrl,
  token,
  roleId,
  expectedRevision,
  permissionCodes,
  signal,
  requestId,
}) {
  const normalizedRoleId = text(roleId).trim();
  const normalizedCodes = normalizePermissionCodes(permissionCodes);
  if (
    !SAFE_ID_PATTERN.test(normalizedRoleId)
    || !Number.isSafeInteger(expectedRevision)
    || expectedRevision < 1
    || !normalizedCodes
  ) {
    // 本地校验失败时不产生网络请求，也不把非法 role_id 或权限集合拼入错误正文。
    throw permissionClientError("rbac.payload_invalid", "角色权限请求不合法", 422, requestId);
  }
  const operationRequestId = safeRequestId(requestId) || createPermissionRequestId("patch");
  // PATCH 只提交 CAS revision 与规范权限集合；tenant、actor、role code 和上限均由后端恢复。
  return permissionRequest(
    `${controlApiUrl}/roles/${encodeURIComponent(normalizedRoleId)}/permissions`,
    {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        expected_revision: expectedRevision,
        permission_codes: normalizedCodes,
      }),
      signal,
    },
    operationRequestId,
  );
}

/** 为一次 GET 或 PATCH 生成不含租户和业务值的 request_id。 */
export function createPermissionRequestId(kind = "request") {
  requestSequence += 1;
  const safeKind = /^[a-z]{2,16}$/.test(kind) ? kind : "request";
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "").slice(0, 16)
    ?? Math.random().toString(36).slice(2, 18);
  return `console_rbac_${safeKind}_${Date.now()}_${requestSequence}_${random}`;
}

/** 返回固定六角色代码，仅用于验证 PAGE-085 响应完整性和稳定顺序。 */
export function fixedRoleCodes() {
  return [...FIXED_ROLE_CODES];
}

/** 校验公开矩阵哈希，供页面状态拒绝畸形或截断响应。 */
export function isMatrixHash(value) {
  return MATRIX_HASH_PATTERN.test(text(value));
}

/** 发送权限 API 并只保留统一公开错误字段。 */
async function permissionRequest(url, options, requestId) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": requestId,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 只保留统一公开字段，代理错误页、响应正文和敏感 Header 均不能进入页面状态。
    throw new AuthApiError({
      status: response.status,
      code: safeErrorCode(payload.code) || "rbac.unavailable",
      message: payload.message || `权限接口返回 ${response.status}`,
      requestId: safeRequestId(payload.request_id)
        || safeRequestId(response.headers.get("X-Request-ID"))
        || safeRequestId(requestId)
        || "-",
      traceId: safeRequestId(payload.trace_id)
        || safeRequestId(response.headers.get("X-Trace-ID"))
        || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return {
    state: "ok",
    ...payload,
    etag: text(response.headers.get("ETag")).trim() || null,
  };
}

/** 规范权限数组；重复、乱序输入都拒绝，以便镜像比较不产生双义。 */
function normalizePermissionCodes(values) {
  if (!Array.isArray(values) || values.length > 256) return null;
  const normalized = values.map((value) => text(value).trim());
  if (
    normalized.some((value) => !PERMISSION_CODE_PATTERN.test(value))
    || new Set(normalized).size !== normalized.length
  ) return null;
  return [...normalized].sort();
}

/** 读取顶层和 user 镜像标量，存在双份时要求规范值完全一致。 */
function mirroredScalar(topLevel, userLevel, validator) {
  const top = topLevel === undefined ? "" : text(topLevel).trim();
  const user = userLevel === undefined ? "" : text(userLevel).trim();
  if (top && user && top !== user) return "";
  const value = top || user;
  return value && validator(value) ? value : "";
}

/** 构造本地失败关闭错误，不携带原始响应或调用方输入。 */
function permissionClientError(code, message, status, requestId = "-") {
  return new AuthApiError({
    status,
    code,
    message,
    requestId: safeRequestId(requestId) || "-",
    traceId: "-",
    suggestion: "",
    diagnosis: null,
  });
}

/** 只允许稳定公开错误码进入页面。 */
function safeErrorCode(value) {
  const normalized = text(value).trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}

/** 只允许有界链路标识进入状态和 DOM。 */
function safeRequestId(value) {
  const normalized = text(value).trim();
  return SAFE_ID_PATTERN.test(normalized) ? normalized : "";
}

/** 容错读取 JSON，代理错误页不会作为原始正文进入页面。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/** 把可空值稳定转换为字符串。 */
function text(value) {
  return String(value ?? "");
}
