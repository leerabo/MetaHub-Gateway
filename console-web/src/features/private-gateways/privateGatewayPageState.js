/** PRD-120..130 owner-bound Edge offline exchange, backup, and lifecycle state. */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";
import { workspaceConsoleSessionFromShell } from "../../lib/workspaceConsoleSession.js";
import { workspaceResourcePageOwner } from "../../lib/workspaceResourcePage.js";

const PAGE_ID = "private-gateways";
const DEPLOYMENT_ID = /^edp_[0-9a-f]{32}$/u;
const REFERENCE_ID = /^lsr_[0-9a-f]{32}$/u;
const INSTALLER_RELEASE_ID = /^irl_[0-9a-f]{32}$/u;
const INSTALL_PROFILE_ID = /^ipr_[0-9a-f]{32}$/u;
const PREFLIGHT_REPORT_ID = /^pfr_[0-9a-f]{32}$/u;
const INSTALLATION_ID = /^ins_[0-9a-f]{32}$/u;
const VALIDATION_RUN_ID = /^dvr_[0-9a-f]{32}$/u;
const SUPPORT_BUNDLE_ID = /^esb_[0-9a-f]{32}$/u;
const DIAGNOSTIC_GRANT_ID = /^dgr_[0-9a-f]{32}$/u;
const BACKUP_ID = /^ebk_[0-9a-f]{32}$/u;
const DRILL_ID = /^pdd_[0-9a-f]{32}$/u;
const RESTORE_ID = /^lrj_[0-9a-f]{32}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const OWNER_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const SAFE_LABEL = /^[A-Za-z0-9][A-Za-z0-9 ._:/()-]{0,79}$/u;
const LOCAL_ORIGIN = /^https?:\/\/[A-Za-z0-9.:[\]-]{1,253}(?::[0-9]{1,5})?$/u;
const CHAIN_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;

const ERROR_MESSAGES = Object.freeze({
  "workspace_session.required": "请先在账号安全页选择 Workspace",
  "workspace_session.invalid": "Workspace 会话已失效，请重新选择空间",
  "workspace_session.expired": "Workspace 会话已过期，请重新选择空间",
  "workspace_session.revoked": "Workspace 会话已撤销，请重新选择空间",
  "workspace_session.permission_denied": "当前成员没有私有 Gateway 权限",
  "workspace_session.credential_conflict": "检测到冲突的登录凭据，请重新进入 Workspace",
  "edge.permission_denied": "当前成员没有私有 Gateway 权限",
  "edge.authentication_failed": "私有 Gateway 认证状态已失效",
  "edge.not_found": "当前 Workspace 中找不到该 Deployment",
  "edge.deployment_not_found": "当前 Workspace 中找不到该 Deployment",
  "edge.conflict": "Deployment 状态已变化，请刷新后重试",
  "edge.revision_conflict": "期望状态已变化，请刷新后重试",
  "edge.state_conflict": "当前期望状态不允许此操作，请刷新后重试",
  "edge.idempotency_conflict": "该操作标识已用于其他意图",
  "edge.request_invalid": "私有 Gateway 请求不合法",
  "edge.response_invalid": "私有 Gateway 响应合同不完整",
  "edge.unavailable": "私有 Gateway 服务暂不可用",
  "edge_upgrade.permission_denied": "当前成员没有升级管理权限",
  "edge_upgrade.request_invalid": "升级请求字段不合法",
  "edge_upgrade.response_invalid": "升级响应合同不完整",
  "edge_upgrade.not_found": "当前 Deployment 尚无升级状态",
  "edge_upgrade.revision_conflict": "升级策略已变化，请刷新后重试",
  "edge_upgrade.idempotency_conflict": "该操作标识已用于另一升级意图",
  "edge_upgrade.head_not_found": "当前 Deployment 尚无可回滚版本",
  "edge_upgrade.maintenance_closed": "当前维护窗口或冻结策略不允许回滚",
  "edge_upgrade.maintenance_invalid": "维护窗口字段或冻结区间无效",
  "edge_upgrade.rollback_unavailable": "LKG 或 N-1 数据迁移路径不可用",
  "edge_upgrade.rollback_not_found": "找不到指定的回滚目标",
  "edge_upgrade.state_conflict": "升级状态已变化，请刷新后重试",
  "edge_upgrade.version_incompatible": "目标版本与当前版本不兼容",
  "edge_upgrade.unavailable": "升级服务暂不可用",
  "edge_upgrade.network_unavailable": "升级服务暂不可达",
  "edge_upgrade.request_failed": "升级请求未完成",
  "edge_license.permission_denied": "当前成员没有 License 权限",
  "edge_license.request_invalid": "License 请求字段不合法",
  "edge_license.response_invalid": "License 响应合同不完整",
  "edge_license.not_found": "当前 Deployment 尚未签发 License",
  "edge_license.revoked": "License 已被明确吊销，不能在线刷新",
  "edge_license.revision_conflict": "License 已变化，请刷新后重试",
  "edge_license.idempotency_conflict": "该操作标识已用于另一 License 意图",
  "edge_license.integrity_failed": "License 完整性校验失败",
  "edge_license.state_conflict": "License 状态已变化，请刷新后重试",
  "edge_license.unavailable": "License 服务暂不可用",
  "edge_license.network_unavailable": "License 服务暂不可达",
  "edge_license.request_failed": "License 请求未完成",
  "edge_backup.permission_denied": "当前成员没有备份或灾备演练权限",
  "edge_backup.request_invalid": "备份或灾备演练请求字段不合法",
  "edge_backup.response_invalid": "备份状态响应合同不完整",
  "edge_backup.not_found": "当前 Deployment 尚无可用备份",
  "edge_backup.drill_not_found": "恢复演练不存在或不属于当前 Workspace",
  "edge_backup.approval_conflict": "恢复演练必须由另一位授权成员审批",
  "edge_backup.revision_conflict": "恢复演练状态已变化，请刷新后重试",
  "edge_backup.idempotency_conflict": "该操作标识已用于另一恢复意图",
  "edge_backup.state_conflict": "当前备份或演练状态不允许此操作",
  "edge_backup.network_unavailable": "备份状态服务暂不可达",
  "edge_backup.request_failed": "备份或灾备演练请求未完成",
  "local_backup.request_invalid": "本地备份参数不合法",
  "local_backup.response_invalid": "本地备份响应合同不完整",
  "local_backup.network_unavailable": "本地备份服务暂不可达",
  "local_backup.not_found": "本地备份不存在或不属于当前 Workspace",
  "local_backup.storage_unavailable": "客户备份存储当前不可用",
  "local_restore.request_invalid": "本地恢复参数不合法",
  "local_restore.response_invalid": "本地恢复响应合同不完整",
  "local_restore.network_unavailable": "本地恢复服务暂不可达",
  "local_restore.not_found": "本地恢复记录不存在",
  "local_restore.integrity_failed": "备份完整性验证失败，未触碰活动数据",
  "local_restore.owner_mismatch": "备份 owner 与当前 Workspace 不一致",
  "local_restore.schema_incompatible": "备份 Schema 与目标版本不兼容",
  "local_restore.apply_failed": "恢复应用失败，活动数据保持不变",
  "edge.installer_not_found": "安装介质已下架或不可用",
  "edge.installer_integrity_failed": "安装介质完整性校验失败",
  "edge.preflight_invalid": "预检报告无效",
  "edge.preflight_failed": "预检未通过，安装已阻止",
  "edge.preflight_not_found": "预检报告不存在或已失效",
  "edge.installation_not_found": "安装记录不存在",
  "edge.validation_not_ready": "当前 Deployment 尚未满足验证前置条件",
  "edge.validation_not_found": "验证记录不存在或已失效",
  "edge.validation_conflict": "相同验证计划正在执行，请刷新结果",
  "edge.validation_state_conflict": "验证依据已变化，请重新运行全套验证",
  "edge.validation_report_invalid": "Agent 返回的验证证据不完整",
  "edge.validation_failed": "仍有检查项未通过，不能签署交付",
  "edge.validation_integrity_failed": "验证证据链完整性校验失败",
  "edge.delivery_already_accepted": "该验证结果已完成交付签署",
  "edge.delivery_acceptance_not_found": "交付签署记录不存在",
  "edge.support_bundle_invalid": "支持包合同或完整性校验失败",
  "edge.support_bundle_not_found": "支持包不存在或不属于当前 Workspace",
  "edge.support_bundle_conflict": "支持包状态已变化，请刷新后重试",
  "edge.support_bundle_expired": "支持包已过期",
  "edge.support_bundle_integrity_failed": "支持包对象完整性校验失败",
  "edge.support_bundle_storage_unavailable": "支持包存储暂不可用",
  "edge.diagnostic_not_found": "诊断授权不存在或不属于当前 Workspace",
  "edge.diagnostic_state_conflict": "诊断授权状态已变化，请刷新后重试",
  "edge.diagnostic_authority_changed": "诊断授权主体权限已变化，请重新发起授权",
  "edge.diagnostic_expired": "诊断授权已过期，请重新发起授权",
  "edge.diagnostic_request_invalid": "诊断授权请求不合法",
  "edge.diagnostic_response_invalid": "诊断授权响应合同不完整",
  "edge.diagnostic_unavailable": "远程诊断服务暂不可用",
  "step_up.required": "签署交付前需要二次验证",
  "step_up.invalid": "二次验证已失效，请重新验证",
  "step_up.unavailable": "二次验证暂不可用",
  "identity.request_invalid": "二次验证码格式无效",
  "identity.mfa_verification_failed": "二次验证失败",
  "identity.webauthn_unavailable": "当前浏览器无法使用安全密钥",
  "conditional_access.forbidden": "当前条件访问策略不允许签署交付",
  "local_admin.authentication_failed": "本地管理凭据无效或会话已过期",
  "local_admin.rate_limited": "本地管理尝试过多，请稍后重试",
  "local_admin.origin_invalid": "本地 Gateway 不允许当前控制台 Origin",
  "local_admin.network_denied": "当前网络不能访问本地管理接口",
  "local_admin.request_invalid": "本地管理请求不合法",
  "local_admin.response_invalid": "本地管理响应合同不完整",
  "local_secret.content_type_invalid": "本地 Secret 请求格式不受支持",
  "local_secret.payload_invalid": "本地 Secret 字段不合法",
  "local_secret.not_found": "本地 Secret 引用不存在",
  "local_secret.conflict": "本地 Secret 状态已变化，请刷新后重试",
  "local_secret.revision_conflict": "本地 Secret 版本已变化，请刷新后重试",
  "local_secret.rotation_conflict": "本地 Secret 正在轮换",
  "local_secret.rotation_in_progress": "本地 Secret 正在轮换",
  "local_secret.in_use": "该 Secret 仍被配置、LKG、回滚或备份引用",
  "local_secret.network_unavailable": "本地 Gateway 暂不可达",
  "local_secret.request_failed": "本地 Secret 请求未完成",
  "local_secret.response_invalid": "本地 Secret 响应合同不完整",
  "local_secret.unavailable": "本地 Secret 托管暂不可用",
  "local_support_bundle.content_type_invalid": "本地支持包请求格式不受支持",
  "local_support_bundle.request_invalid": "本地支持包请求不合法",
  "local_support_bundle.response_invalid": "本地支持包响应合同不完整",
  "local_support_bundle.not_found": "本地支持包不存在或已清理",
  "local_support_bundle.expired": "本地支持包预览已过期",
  "local_support_bundle.state_conflict": "本地支持包状态已变化",
  "local_support_bundle.revision_conflict": "本地支持包 revision 已变化",
  "local_support_bundle.revision_invalid": "本地支持包 revision 无效",
  "local_support_bundle.sensitive_detected": "支持包扫描发现敏感信息，已阻止上传",
  "local_support_bundle.size_exceeded": "支持包超过 100 MiB 上限",
  "local_support_bundle.collection_failed": "支持包采集未完成",
  "local_support_bundle.generation_failed": "支持包生成未完成",
  "local_support_bundle.encryption_failed": "支持包加密未完成",
  "local_support_bundle.upload_failed": "支持包上传未完成",
  "local_support_bundle.receipt_invalid": "支持包云端回执无效",
  "local_support_bundle.integrity_failed": "本地支持包完整性校验失败",
  "local_support_bundle.cleanup_failed": "支持包已处理，但本地清理未完成",
  "local_support_bundle.unavailable": "本地支持包服务暂不可用",
});

export function privateGatewayReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_DEPLOYMENT_READ);
}

export function privateGatewayPageOwner(authContext, consoleShellState) {
  if (consoleShellState?.workspaceNavigationContract === true) {
    return workspacePrivateGatewayPageOwner(authContext, consoleShellState);
  }
  const readIdentity = privateGatewayReadIdentitySnapshot(authContext);
  const manageIdentity = hasPermission(authContext, PERMISSIONS.EDGE_DEPLOYMENT_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_DEPLOYMENT_MANAGE)
    : "";
  const brandingReadIdentity = hasPermission(authContext, PERMISSIONS.DEPLOYMENT_BRANDING_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.DEPLOYMENT_BRANDING_READ)
    : "";
  const brandingManageIdentity = hasPermission(authContext, PERMISSIONS.DEPLOYMENT_BRANDING_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.DEPLOYMENT_BRANDING_MANAGE)
    : "";
  const telemetryReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_TELEMETRY_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_TELEMETRY_READ)
    : "";
  const telemetryManageIdentity = hasPermission(authContext, PERMISSIONS.EDGE_TELEMETRY_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_TELEMETRY_MANAGE)
    : "";
  const usageReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_USAGE_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_USAGE_READ)
    : "";
  const secretReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_SECRET_REFERENCE_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_SECRET_REFERENCE_READ)
    : "";
  const installerReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_INSTALLER_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_INSTALLER_READ)
    : "";
  const installationManageIdentity = hasPermission(authContext, PERMISSIONS.EDGE_INSTALLATION_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_INSTALLATION_MANAGE)
    : "";
  const validationReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_VALIDATION_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_VALIDATION_READ)
    : "";
  const validationManageIdentity = hasPermission(authContext, PERMISSIONS.EDGE_VALIDATION_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_VALIDATION_MANAGE)
    : "";
  const deliveryAcceptIdentity = hasPermission(authContext, PERMISSIONS.EDGE_DELIVERY_ACCEPT)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_DELIVERY_ACCEPT)
    : "";
  const supportBundleReadIdentity = hasPermission(
    authContext,
    PERMISSIONS.EDGE_SUPPORT_BUNDLE_READ,
  ) ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_SUPPORT_BUNDLE_READ) : "";
  const healthReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_HEALTH_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_HEALTH_READ)
    : "";
  const diagnosticReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_DIAGNOSTIC_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_DIAGNOSTIC_READ)
    : "";
  const diagnosticApproveIdentity = hasPermission(
    authContext,
    PERMISSIONS.EDGE_DIAGNOSTIC_APPROVE,
  ) ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_DIAGNOSTIC_APPROVE) : "";
  const upgradeReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_UPGRADE_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_UPGRADE_READ) : "";
  const upgradeManageIdentity = hasPermission(authContext, PERMISSIONS.EDGE_UPGRADE_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_UPGRADE_MANAGE) : "";
  const rollbackManageIdentity = hasPermission(authContext, PERMISSIONS.EDGE_ROLLBACK_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_ROLLBACK_MANAGE) : "";
  const licenseReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_LICENSE_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_LICENSE_READ) : "";
  const licenseRefreshIdentity = hasPermission(authContext, PERMISSIONS.EDGE_LICENSE_REFRESH)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_LICENSE_REFRESH) : "";
  const licenseImportIdentity = hasPermission(authContext, PERMISSIONS.EDGE_LICENSE_IMPORT)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_LICENSE_IMPORT) : "";
  const backupReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_BACKUP_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_BACKUP_READ) : "";
  const backupCreateIdentity = hasPermission(authContext, PERMISSIONS.EDGE_BACKUP_CREATE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_BACKUP_CREATE) : "";
  const drillManageIdentity = hasPermission(authContext, PERMISSIONS.EDGE_DRILL_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_DRILL_MANAGE) : "";
  const restoreApproveIdentity = hasPermission(authContext, PERMISSIONS.EDGE_RESTORE_APPROVE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_RESTORE_APPROVE) : "";
  const offlineReadIdentity = hasPermission(authContext, PERMISSIONS.EDGE_OFFLINE_BUNDLE_READ)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_OFFLINE_BUNDLE_READ) : "";
  const offlineTransferIdentity = hasPermission(
    authContext, PERMISSIONS.EDGE_OFFLINE_BUNDLE_TRANSFER,
  ) ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_OFFLINE_BUNDLE_TRANSFER) : "";
  const offlineResolveIdentity = hasPermission(
    authContext, PERMISSIONS.EDGE_OFFLINE_CONFLICT_RESOLVE,
  ) ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_OFFLINE_CONFLICT_RESOLVE) : "";
  const tenantId = mirroredTenantId(authContext);
  const accountId = mirroredAccountId(authContext);
  const role = String(authContext?.user?.role ?? "");
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
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.EDGE_DEPLOYMENT_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(permissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    page: PAGE_ID,
    tenantId,
    readIdentity,
    manageIdentity,
    brandingReadIdentity,
    brandingManageIdentity,
    telemetryReadIdentity,
    telemetryManageIdentity,
    usageReadIdentity,
    secretReadIdentity,
    installerReadIdentity,
    installationManageIdentity,
    validationReadIdentity,
    validationManageIdentity,
    deliveryAcceptIdentity,
    supportBundleReadIdentity,
    healthReadIdentity,
    diagnosticReadIdentity,
    diagnosticApproveIdentity,
    upgradeReadIdentity,
    upgradeManageIdentity,
    rollbackManageIdentity,
    licenseReadIdentity,
    licenseRefreshIdentity,
    licenseImportIdentity,
    backupReadIdentity,
    backupCreateIdentity,
    drillManageIdentity,
    restoreApproveIdentity,
    offlineReadIdentity,
    offlineTransferIdentity,
    offlineResolveIdentity,
    accountId,
    role,
    navigationOwner: JSON.stringify([
      item.id, item.target_section, required, permissions, revision, fingerprint,
    ]),
    canManage: Boolean(manageIdentity),
    canReadBranding: Boolean(brandingReadIdentity),
    canManageBranding: Boolean(brandingReadIdentity && brandingManageIdentity),
    canReadTelemetry: Boolean(telemetryReadIdentity),
    canManageTelemetry: Boolean(telemetryManageIdentity),
    canReadUsage: Boolean(usageReadIdentity),
    canReadSecrets: Boolean(secretReadIdentity),
    canManageSecrets: Boolean(secretReadIdentity && accountId && role === "security_admin"),
    canReadInstallers: Boolean(installerReadIdentity),
    canManageInstallations: Boolean(installerReadIdentity && installationManageIdentity),
    canReadValidations: Boolean(validationReadIdentity),
    canManageValidations: Boolean(validationReadIdentity && validationManageIdentity),
    canAcceptDelivery: Boolean(validationReadIdentity && deliveryAcceptIdentity),
    canReadSupportBundles: Boolean(supportBundleReadIdentity),
    canManageSupportBundles: Boolean(
      supportBundleReadIdentity && accountId && role === "security_admin"
    ),
    canReadHealth: Boolean(healthReadIdentity),
    canReadDiagnostics: Boolean(healthReadIdentity && diagnosticReadIdentity),
    canApproveDiagnostics: Boolean(
      healthReadIdentity && diagnosticReadIdentity && diagnosticApproveIdentity
    ),
    canReadUpgrades: Boolean(upgradeReadIdentity),
    canManageUpgrades: Boolean(upgradeReadIdentity && upgradeManageIdentity),
    canRollbackUpgrades: Boolean(upgradeReadIdentity && rollbackManageIdentity),
    canReadLicenses: Boolean(licenseReadIdentity),
    canRefreshLicenses: Boolean(licenseReadIdentity && licenseRefreshIdentity),
    canImportLicenses: Boolean(licenseReadIdentity && licenseImportIdentity),
    canReadBackups: Boolean(backupReadIdentity),
    canCreateBackups: Boolean(backupReadIdentity && backupCreateIdentity),
    canManageDrills: Boolean(backupReadIdentity && drillManageIdentity),
    canApproveRestores: Boolean(backupReadIdentity && restoreApproveIdentity),
    canReadOfflineBundles: Boolean(offlineReadIdentity),
    canTransferOfflineBundles: Boolean(offlineReadIdentity && offlineTransferIdentity),
    canResolveOfflineConflicts: Boolean(offlineReadIdentity && offlineResolveIdentity),
  });
}

// Current Workspace sessions expose authorization and workspace ownership,
// while legacy tenant/user mirrors are intentionally absent.
function workspacePrivateGatewayPageOwner(authContext, consoleShellState) {
  const session = workspaceConsoleSessionFromShell(consoleShellState);
  if (!session
    || JSON.stringify(permissionCodesFromAuthContext(authContext))
      !== JSON.stringify(permissionCodesFromAuthContext(session.context))
    || permissionRevision(authContext) !== session.context.permission_revision
    || String(authContext?.permission_fingerprint ?? "")
      !== session.context.permission_fingerprint) return null;
  const resource = workspaceResourcePageOwner(session, consoleShellState, {
    navigationId: PAGE_ID,
    permissions: [PERMISSIONS.EDGE_DEPLOYMENT_READ],
  });
  if (!resource) return null;
  const identity = (permission) => hasPermission(session.context, permission)
    ? JSON.stringify([resource.key, permission]) : "";
  const r = identity(PERMISSIONS.EDGE_DEPLOYMENT_READ);
  const m = identity(PERMISSIONS.EDGE_DEPLOYMENT_MANAGE);
  const br = identity(PERMISSIONS.DEPLOYMENT_BRANDING_READ);
  const bm = identity(PERMISSIONS.DEPLOYMENT_BRANDING_MANAGE);
  const tr = identity(PERMISSIONS.EDGE_TELEMETRY_READ);
  const tm = identity(PERMISSIONS.EDGE_TELEMETRY_MANAGE);
  const ur = identity(PERMISSIONS.EDGE_USAGE_READ);
  const sr = identity(PERMISSIONS.EDGE_SECRET_REFERENCE_READ);
  const ir = identity(PERMISSIONS.EDGE_INSTALLER_READ);
  const im = identity(PERMISSIONS.EDGE_INSTALLATION_MANAGE);
  const vr = identity(PERMISSIONS.EDGE_VALIDATION_READ);
  const vm = identity(PERMISSIONS.EDGE_VALIDATION_MANAGE);
  const da = identity(PERMISSIONS.EDGE_DELIVERY_ACCEPT);
  const sbr = identity(PERMISSIONS.EDGE_SUPPORT_BUNDLE_READ);
  const hr = identity(PERMISSIONS.EDGE_HEALTH_READ);
  const dr = identity(PERMISSIONS.EDGE_DIAGNOSTIC_READ);
  const dpa = identity(PERMISSIONS.EDGE_DIAGNOSTIC_APPROVE);
  const urd = identity(PERMISSIONS.EDGE_UPGRADE_READ);
  const um = identity(PERMISSIONS.EDGE_UPGRADE_MANAGE);
  const rb = identity(PERMISSIONS.EDGE_ROLLBACK_MANAGE);
  const lr = identity(PERMISSIONS.EDGE_LICENSE_READ);
  const lrf = identity(PERMISSIONS.EDGE_LICENSE_REFRESH);
  const lim = identity(PERMISSIONS.EDGE_LICENSE_IMPORT);
  const bkr = identity(PERMISSIONS.EDGE_BACKUP_READ);
  const bkc = identity(PERMISSIONS.EDGE_BACKUP_CREATE);
  const dm = identity(PERMISSIONS.EDGE_DRILL_MANAGE);
  const ra = identity(PERMISSIONS.EDGE_RESTORE_APPROVE);
  const obr = identity(PERMISSIONS.EDGE_OFFLINE_BUNDLE_READ);
  const obt = identity(PERMISSIONS.EDGE_OFFLINE_BUNDLE_TRANSFER);
  const obrk = identity(PERMISSIONS.EDGE_OFFLINE_CONFLICT_RESOLVE);
  return Object.freeze({
    page: PAGE_ID, workspaceId: session.owner.workspaceId, tenantId: "",
    readIdentity: r, manageIdentity: m, brandingReadIdentity: br,
    brandingManageIdentity: bm, telemetryReadIdentity: tr,
    telemetryManageIdentity: tm, usageReadIdentity: ur, secretReadIdentity: sr,
    installerReadIdentity: ir, installationManageIdentity: im,
    validationReadIdentity: vr, validationManageIdentity: vm,
    deliveryAcceptIdentity: da, supportBundleReadIdentity: sbr,
    healthReadIdentity: hr, diagnosticReadIdentity: dr,
    diagnosticApproveIdentity: dpa, upgradeReadIdentity: urd,
    upgradeManageIdentity: um, rollbackManageIdentity: rb,
    licenseReadIdentity: lr, licenseRefreshIdentity: lrf,
    licenseImportIdentity: lim, backupReadIdentity: bkr,
    backupCreateIdentity: bkc, drillManageIdentity: dm,
    restoreApproveIdentity: ra, offlineReadIdentity: obr,
    offlineTransferIdentity: obt, offlineResolveIdentity: obrk,
    accountId: "", role: session.owner.role, workspaceSession: true,
    navigationOwner: resource.key,
    canManage: Boolean(m), canReadBranding: Boolean(br),
    canManageBranding: Boolean(br && bm), canReadTelemetry: Boolean(tr),
    canManageTelemetry: Boolean(tm), canReadUsage: Boolean(ur),
    canReadSecrets: Boolean(sr), canManageSecrets: false,
    canReadInstallers: Boolean(ir), canManageInstallations: Boolean(ir && im),
    canReadValidations: Boolean(vr), canManageValidations: Boolean(vr && vm),
    canAcceptDelivery: Boolean(vr && da), canReadSupportBundles: Boolean(sbr),
    canManageSupportBundles: false, canReadHealth: Boolean(hr),
    canReadDiagnostics: Boolean(hr && dr),
    canApproveDiagnostics: Boolean(hr && dr && dpa), canReadUpgrades: Boolean(urd),
    canManageUpgrades: Boolean(urd && um), canRollbackUpgrades: Boolean(urd && rb),
    canReadLicenses: Boolean(lr), canRefreshLicenses: Boolean(lr && lrf),
    canImportLicenses: Boolean(lr && lim), canReadBackups: Boolean(bkr),
    canCreateBackups: Boolean(bkr && bkc), canManageDrills: Boolean(bkr && dm),
    canApproveRestores: Boolean(bkr && ra), canReadOfflineBundles: Boolean(obr),
    canTransferOfflineBundles: Boolean(obr && obt),
    canResolveOfflineConflicts: Boolean(obr && obrk),
  });
}

export function createPrivateGatewayPageState({
  clearDom = () => {},
  clearLocalSession = () => {},
} = {}) {
  let ownerKey = "";
  let canManage = false;
  let listState = "loading";
  let listError = null;
  let deployments = Object.freeze([]);
  let desiredStates = Object.freeze({});
  let telemetryStates = Object.freeze({});
  let secretReferences = Object.freeze([]);
  let installerCatalog = emptyInstallerCatalog();
  let installerSelection = emptyInstallerSelection();
  let validationStates = Object.freeze({});
  let supportBundleStates = Object.freeze({});
  let healthStates = Object.freeze({});
  let upgradeStates = Object.freeze({});
  let licenseStates = Object.freeze({});
  let backupStates = Object.freeze({});
  let offlineBundleStates = Object.freeze({});
  let localBackupStates = Object.freeze({});
  let workspaceId = "";
  let dialog = closedDialog();
  let stateDialog = closedStateDialog();
  let registration = null;
  let secretDialog = closedSecretDialog();
  let acceptanceDialog = closedAcceptanceDialog();
  let diagnosticDialog = closedDiagnosticDialog();
  let localSession = null;
  let feedback = null;
  let canReadTelemetry = false;
  let canManageTelemetry = false;
  let canReadUsage = false;
  let canReadSecrets = false;
  let canManageSecrets = false;
  let canReadInstallers = false;
  let canManageInstallations = false;
  let canReadValidations = false;
  let canManageValidations = false;
  let canAcceptDelivery = false;
  let canReadSupportBundles = false;
  let canManageSupportBundles = false;
  let canReadHealth = false;
  let canReadDiagnostics = false;
  let canApproveDiagnostics = false;
  let canReadUpgrades = false;
  let canManageUpgrades = false;
  let canRollbackUpgrades = false;
  let canReadLicenses = false;
  let canRefreshLicenses = false;
  let canImportLicenses = false;
  let canReadBackups = false;
  let canCreateBackups = false;
  let canManageDrills = false;
  let canApproveRestores = false;
  let canReadOfflineBundles = false;
  let canTransferOfflineBundles = false;
  let canResolveOfflineConflicts = false;
  let maintenanceIntent = null;
  let rollbackUpgradeIntent = null;
  let licenseRefreshIntent = null;
  let drillMutationIntent = null;
  let generations = {
    load: 0, create: 0, revoke: 0, copy: 0, command: 0, policy: 0,
    localSession: 0, secret: 0, validation: 0, acceptance: 0, supportBundle: 0,
    diagnostic: 0, maintenance: 0, rollbackUpgrade: 0, refreshLicense: 0,
    drillMutation: 0, localBackup: 0, localRestore: 0,
  };
  let operations = {
    load: null, create: null, revoke: null, copy: null, command: null, policy: null,
    localSession: null, secret: null, validation: null, acceptance: null,
    supportBundle: null, diagnostic: null, maintenance: null, rollbackUpgrade: null,
    refreshLicense: null, drillMutation: null, localBackup: null, localRestore: null,
  };

  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    canManage = owner.canManage === true;
    canReadTelemetry = owner.canReadTelemetry === true;
    canManageTelemetry = owner.canManageTelemetry === true;
    canReadUsage = owner.canReadUsage === true;
    canReadSecrets = owner.canReadSecrets === true;
    canManageSecrets = owner.canManageSecrets === true;
    canReadInstallers = owner.canReadInstallers === true;
    canManageInstallations = owner.canManageInstallations === true && canReadInstallers;
    canReadValidations = owner.canReadValidations === true;
    canManageValidations = owner.canManageValidations === true && canReadValidations;
    canAcceptDelivery = owner.canAcceptDelivery === true && canReadValidations;
    canReadSupportBundles = owner.canReadSupportBundles === true;
    canManageSupportBundles = owner.canManageSupportBundles === true
      && canReadSupportBundles;
    canReadHealth = owner.canReadHealth === true;
    canReadDiagnostics = owner.canReadDiagnostics === true && canReadHealth;
    canApproveDiagnostics = owner.canApproveDiagnostics === true && canReadDiagnostics;
    canReadUpgrades = owner.canReadUpgrades === true;
    canManageUpgrades = owner.canManageUpgrades === true && canReadUpgrades;
    canRollbackUpgrades = owner.canRollbackUpgrades === true && canReadUpgrades;
    canReadLicenses = owner.canReadLicenses === true;
    canRefreshLicenses = owner.canRefreshLicenses === true && canReadLicenses;
    canImportLicenses = owner.canImportLicenses === true && canReadLicenses;
    canReadBackups = owner.canReadBackups === true;
    canCreateBackups = owner.canCreateBackups === true && canReadBackups;
    canManageDrills = owner.canManageDrills === true && canReadBackups;
    canApproveRestores = owner.canApproveRestores === true && canReadBackups;
    canReadOfflineBundles = owner.canReadOfflineBundles === true;
    canTransferOfflineBundles = owner.canTransferOfflineBundles === true
      && canReadOfflineBundles;
    canResolveOfflineConflicts = owner.canResolveOfflineConflicts === true
      && canReadOfflineBundles;
    if (!canManage && dialog.open) closeDialogInternal();
    if (!canManageSecrets) {
      secretDialog = closedSecretDialog();
      localSession = null;
    }
    if (!canAcceptDelivery) acceptanceDialog = closedAcceptanceDialog();
    if (!canReadValidations) validationStates = Object.freeze({});
    if (!canReadSupportBundles) supportBundleStates = Object.freeze({});
    if (!canReadHealth) healthStates = Object.freeze({});
    if (!canApproveDiagnostics) diagnosticDialog = closedDiagnosticDialog();
    if (!canReadUpgrades) upgradeStates = Object.freeze({});
    if (!canReadLicenses) licenseStates = Object.freeze({});
    if (!canReadBackups) {
      backupStates = Object.freeze({});
      localBackupStates = Object.freeze({});
    }
    if (!canReadOfflineBundles) offlineBundleStates = Object.freeze({});
    return true;
  }

  function clear() {
    const pending = Object.values(operations).filter(Boolean);
    const localApiOrigin = localSession?.localApiOrigin ?? "";
    ownerKey = "";
    canManage = false;
    listState = "loading";
    listError = null;
    deployments = Object.freeze([]);
    desiredStates = Object.freeze({});
    telemetryStates = Object.freeze({});
    secretReferences = Object.freeze([]);
    installerCatalog = emptyInstallerCatalog();
    installerSelection = emptyInstallerSelection();
    validationStates = Object.freeze({});
    supportBundleStates = Object.freeze({});
    healthStates = Object.freeze({});
    upgradeStates = Object.freeze({});
    licenseStates = Object.freeze({});
    backupStates = Object.freeze({});
    offlineBundleStates = Object.freeze({});
    localBackupStates = Object.freeze({});
    workspaceId = "";
    dialog = closedDialog();
    stateDialog = closedStateDialog();
    registration = null;
    secretDialog = closedSecretDialog();
    acceptanceDialog = closedAcceptanceDialog();
    diagnosticDialog = closedDiagnosticDialog();
    localSession = null;
    feedback = null;
    canReadTelemetry = false;
    canManageTelemetry = false;
    canReadUsage = false;
    canReadSecrets = false;
    canManageSecrets = false;
    canReadInstallers = false;
    canManageInstallations = false;
    canReadValidations = false;
    canManageValidations = false;
    canAcceptDelivery = false;
    canReadSupportBundles = false;
    canManageSupportBundles = false;
    canReadHealth = false;
    canReadDiagnostics = false;
    canApproveDiagnostics = false;
    canReadUpgrades = false;
    canManageUpgrades = false;
    canRollbackUpgrades = false;
    canReadLicenses = false;
    canRefreshLicenses = false;
    canImportLicenses = false;
    canReadBackups = false;
    canCreateBackups = false;
    canManageDrills = false;
    canApproveRestores = false;
    canReadOfflineBundles = false;
    canTransferOfflineBundles = false;
    canResolveOfflineConflicts = false;
    maintenanceIntent = null;
    rollbackUpgradeIntent = null;
    licenseRefreshIntent = null;
    drillMutationIntent = null;
    operations = {
      load: null, create: null, revoke: null, copy: null, command: null, policy: null,
      localSession: null, secret: null, validation: null, acceptance: null,
      supportBundle: null, diagnostic: null, maintenance: null, rollbackUpgrade: null,
      refreshLicense: null, drillMutation: null, localBackup: null, localRestore: null,
    };
    generations = Object.fromEntries(
      Object.entries(generations).map(([lane, value]) => [lane, value + 1]),
    );
    try { clearDom(); } finally {
      for (const operation of pending) operation.controller?.abort?.();
      clearLocalSession(localApiOrigin);
    }
  }

  function beginLoad(owner, refreshGeneration) {
    if (!owns(owner) || !Number.isSafeInteger(refreshGeneration) || refreshGeneration < 1) return null;
    operations.load?.controller.abort();
    generations.load += 1;
    const operation = ticket("load", { refreshGeneration, controller: new AbortController() });
    operations.load = operation;
    listError = null;
    if (!deployments.length) listState = "loading";
    // A refresh can observe a switched Workspace cookie; one-use material cannot cross that boundary.
    clearRegistration();
    stateDialog = closedStateDialog();
    secretDialog = closedSecretDialog();
    acceptanceDialog = closedAcceptanceDialog();
    diagnosticDialog = closedDiagnosticDialog();
    return operation;
  }

  function isLoadCurrent(operation, owner, refreshGeneration) {
    return current("load", operation, owner) && operation.refreshGeneration === refreshGeneration;
  }

  function acceptLoad(
    operation,
    response,
    owner,
    refreshGeneration,
    stateResponses = [],
    telemetryResponses = [],
    secretResponse = null,
    installerResponse = null,
    healthResponses = [],
    upgradeResponses = [],
    licenseResponses = [],
    backupResponses = [],
    offlineBundleResponses = [],
  ) {
    if (!isLoadCurrent(operation, owner, refreshGeneration)
      || !validList(response)
      || !validDesiredResponses(stateResponses, response.deployments)
      || !validTelemetryResponses(
        telemetryResponses,
        response.deployments,
        canReadTelemetry || canReadUsage,
      )
      || !validSecretResponse(secretResponse, response.deployments, canReadSecrets, owner)
      || !validInstallerResponse(
        installerResponse,
        response.deployments,
        canReadInstallers,
      )
      || !validHealthResponses(
        healthResponses,
        response.deployments,
        canReadHealth,
        canReadDiagnostics,
      )
      || !validUpgradeResponses(upgradeResponses, response.deployments, canReadUpgrades)
      || !validLicenseResponses(licenseResponses, response.deployments, canReadLicenses)
      || !validBackupResponses(backupResponses, response.deployments, canReadBackups)
      || !validOfflineBundleResponses(
        offlineBundleResponses, response.deployments, canReadOfflineBundles,
      )) {
      return false;
    }
    const ids = new Set(response.deployments.map((item) => item.workspace_id));
    if (ids.size > 1) return false;
    deployments = Object.freeze([...response.deployments]);
    desiredStates = desiredStateMap(response.deployments, stateResponses);
    telemetryStates = telemetryStateMap(response.deployments, telemetryResponses);
    secretReferences = Object.freeze([...(secretResponse?.references ?? [])]);
    installerCatalog = canReadInstallers
      ? freezeInstallerCatalog(installerResponse)
      : emptyInstallerCatalog();
    installerSelection = reconcileInstallerSelection(
      installerSelection,
      response.deployments,
      installerCatalog,
    );
    const deploymentIds = new Set(response.deployments.map((item) => item.deployment_id));
    validationStates = canReadValidations
      ? retainOwnedMap(validationStates, deploymentIds)
      : Object.freeze({});
    supportBundleStates = canReadSupportBundles
      ? retainOwnedMap(supportBundleStates, deploymentIds)
      : Object.freeze({});
    healthStates = canReadHealth
      ? healthStateMap(response.deployments, healthResponses)
      : Object.freeze({});
    upgradeStates = canReadUpgrades
      ? upgradeStateMap(response.deployments, upgradeResponses)
      : Object.freeze({});
    licenseStates = canReadLicenses
      ? licenseStateMap(response.deployments, licenseResponses)
      : Object.freeze({});
    backupStates = canReadBackups
      ? backupStateMap(response.deployments, backupResponses)
      : Object.freeze({});
    offlineBundleStates = canReadOfflineBundles
      ? offlineBundleStateMap(response.deployments, offlineBundleResponses)
      : Object.freeze({});
    workspaceId = ids.size === 1 ? [...ids][0] : "";
    listState = "ready";
    listError = null;
    return true;
  }

  function rejectLoad(operation, error, owner, refreshGeneration) {
    if (!isLoadCurrent(operation, owner, refreshGeneration)) return false;
    listState = "error";
    listError = fixedError(error);
    return true;
  }

  function finishLoad(operation) { if (operations.load === operation) operations.load = null; }

  function selectInstallerEnvironment(owner, values = {}) {
    if (!owns(owner) || !canReadInstallers || busy()) return false;
    const deployment = deployments.find((item) => (
      item.deployment_id === values.deploymentId
      && !["revoked", "retired"].includes(item.status)
    ));
    const release = installerCatalog.releases.find(
      (item) => item.manifest.release_id === values.releaseId,
    );
    const profile = release?.manifest.profiles.find(
      (item) => item.install_profile_id === values.profileId,
    ) ?? release?.manifest.profiles.find((item) => item.profile_code === "standard")
      ?? release?.manifest.profiles[0];
    const architecture = release?.manifest.supported_arch.includes(values.architecture)
      ? values.architecture
      : (release?.manifest.supported_arch.includes("x86_64") ? "x86_64"
        : release?.manifest.supported_arch[0]);
    if (!deployment || !release || !profile || !architecture) return false;
    installerSelection = Object.freeze({
      deploymentId: deployment.deployment_id,
      releaseId: release.manifest.release_id,
      profileId: profile.install_profile_id,
      architecture,
    });
    return true;
  }

  function openCreate(owner) {
    if (!owns(owner) || !canManage || busy()) return false;
    clearRegistration();
    stateDialog = closedStateDialog();
    secretDialog = closedSecretDialog();
    acceptanceDialog = closedAcceptanceDialog();
    dialog = Object.freeze({ open: true, state: "form", error: null });
    feedback = null;
    return true;
  }

  function closeCreate(owner) {
    if (!owns(owner) || operations.create || operations.copy) return false;
    closeDialogInternal();
    return true;
  }

  function beginCreate(owner) {
    if (!owns(owner) || !canManage || dialog.state !== "form" || busy()) return null;
    generations.create += 1;
    const operation = ticket("create", { controller: new AbortController() });
    operations.create = operation;
    dialog = Object.freeze({ open: true, state: "submitting", error: null });
    feedback = null;
    return operation;
  }

  function acceptCreate(operation, response, owner) {
    if (!current("create", operation, owner) || !validRegistration(response)) return false;
    if (workspaceId && response.deployment.workspace_id !== workspaceId) return false;
    workspaceId = response.deployment.workspace_id;
    deployments = Object.freeze([
      response.deployment,
      ...deployments.filter((item) => item.deployment_id !== response.deployment.deployment_id),
    ]);
    desiredStates = Object.freeze({
      ...desiredStates,
      [response.deployment.deployment_id]: null,
    });
    telemetryStates = Object.freeze({
      ...telemetryStates,
      [response.deployment.deployment_id]: null,
    });
    validationStates = Object.freeze({
      ...validationStates,
      [response.deployment.deployment_id]: null,
    });
    supportBundleStates = Object.freeze({
      ...supportBundleStates,
      [response.deployment.deployment_id]: null,
    });
    listState = "ready";
    listError = null;
    if (response.registration_token && response.challenge) {
      registration = Object.freeze({
        deploymentId: response.deployment.deployment_id,
        token: response.registration_token,
        challenge: response.challenge,
        expiresAt: response.expires_at,
      });
      dialog = Object.freeze({ open: true, state: "registration", error: null });
    } else {
      clearRegistration();
      dialog = closedDialog();
    }
    feedback = Object.freeze({
      kind: "success",
      code: response.idempotent_replay ? "edge.registration_replayed" : "edge.registration_created",
      requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectCreate(operation, error, owner) {
    if (!current("create", operation, owner)) return false;
    dialog = Object.freeze({ open: true, state: "form", error: fixedError(error) });
    return true;
  }

  function finishCreate(operation) { if (operations.create === operation) operations.create = null; }

  function beginCopy(owner) {
    if (!owns(owner) || !registration || operations.copy) return null;
    generations.copy += 1;
    const operation = ticket("copy", {
      text: JSON.stringify({
        deployment_id: registration.deploymentId,
        registration_token: registration.token,
        challenge: registration.challenge,
        expires_at: registration.expiresAt,
      }, null, 2),
    });
    operations.copy = operation;
    return operation;
  }

  function acceptCopy(operation, owner) {
    if (!current("copy", operation, owner)) return false;
    operations.copy = null;
    clearRegistration();
    dialog = closedDialog();
    feedback = Object.freeze({ kind: "success", code: "edge.registration_copied", requestId: "-" });
    return true;
  }

  function rejectCopy(operation, owner) {
    if (!current("copy", operation, owner)) return false;
    operations.copy = null;
    dialog = Object.freeze({ open: true, state: "registration", error: fixedError({ code: "edge.unavailable" }) });
    return true;
  }

  function beginRevoke(owner, deploymentId) {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    if (!owns(owner) || !canManage || !deployment || deployment.status === "revoked" || busy()) return null;
    generations.revoke += 1;
    const operation = ticket("revoke", {
      controller: new AbortController(),
      deploymentId,
      revision: deployment.revision,
    });
    operations.revoke = operation;
    feedback = null;
    return operation;
  }

  function acceptRevoke(operation, response, owner) {
    if (!current("revoke", operation, owner)
      || response?.deployment?.deployment_id !== operation.deploymentId
      || response.deployment.status !== "revoked") return false;
    deployments = Object.freeze(deployments.map((item) => (
      item.deployment_id === operation.deploymentId ? response.deployment : item
    )));
    feedback = Object.freeze({
      kind: "success", code: "edge.deployment_revoked", requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectRevoke(operation, error, owner) {
    if (!current("revoke", operation, owner)) return false;
    feedback = Object.freeze({ kind: "error", ...fixedError(error) });
    return true;
  }

  function finishRevoke(operation) { if (operations.revoke === operation) operations.revoke = null; }

  function openStateCommand(owner, deploymentId, action) {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const entry = desiredStates[deploymentId] ?? null;
    const desired = entry?.state ?? null;
    const allowed = (action === "publish" && desired?.status !== "approval_pending")
      || (["approve", "reject"].includes(action) && desired?.status === "approval_pending")
      || (action === "rollback" && desired?.status !== "approval_pending"
        && Number.isSafeInteger(desired?.lkg_generation) && desired.lkg_generation > 0);
    if (!owns(owner) || !canManage || !deployment || deployment.status !== "active"
      || !allowed || busy()) return false;
    closeDialogInternal();
    secretDialog = closedSecretDialog();
    acceptanceDialog = closedAcceptanceDialog();
    stateDialog = Object.freeze({
      open: true,
      state: "form",
      action,
      deploymentId,
      error: null,
    });
    feedback = null;
    return true;
  }

  function closeStateCommand(owner) {
    if (!owns(owner) || operations.command) return false;
    stateDialog = closedStateDialog();
    return true;
  }

  function beginStateCommand(owner, input = {}) {
    const deploymentId = stateDialog.deploymentId;
    const action = stateDialog.action;
    const entry = desiredStates[deploymentId] ?? null;
    const desired = entry?.state ?? null;
    if (!owns(owner) || !canManage || stateDialog.state !== "form" || !entry || busy()) return null;
    const request = commandRequest(action, input, desired);
    if (!request) return null;
    generations.command += 1;
    const operation = ticket("command", {
      controller: new AbortController(),
      deploymentId,
      action,
      expectedRevision: entry.revision,
      request,
    });
    operations.command = operation;
    stateDialog = Object.freeze({ ...stateDialog, state: "submitting", error: null });
    return operation;
  }

  function acceptStateCommand(operation, response, owner) {
    if (!current("command", operation, owner)
      || response?.state?.deployment_id !== operation.deploymentId
      || !Number.isSafeInteger(response.revision)) return false;
    desiredStates = Object.freeze({
      ...desiredStates,
      [operation.deploymentId]: Object.freeze(response),
    });
    stateDialog = closedStateDialog();
    const successCodes = {
      publish: response.state.status === "approval_pending"
        ? "edge.desired_state_pending" : "edge.desired_state_published",
      approve: "edge.desired_state_approved",
      reject: "edge.desired_state_rejected",
      rollback: "edge.desired_state_rolled_back",
    };
    const code = successCodes[operation.action] ?? "edge.desired_state_published";
    feedback = Object.freeze({ kind: "success", code, requestId: safeChain(response.request_id) });
    return true;
  }

  function rejectStateCommand(operation, error, owner) {
    if (!current("command", operation, owner)) return false;
    stateDialog = Object.freeze({ ...stateDialog, state: "form", error: fixedError(error) });
    return true;
  }

  function finishStateCommand(operation) {
    if (operations.command === operation) operations.command = null;
  }

  function beginTelemetryPolicy(owner, deploymentId, telemetryLevel) {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const telemetry = telemetryStates[deploymentId] ?? null;
    if (!owns(owner) || !canManageTelemetry || !deployment
      || deployment.status !== "active" || !telemetry?.policy
      || !["minimal", "operational", "billing"].includes(telemetryLevel)
      || telemetry.policy.telemetry_level === telemetryLevel || busy()) return null;
    generations.policy += 1;
    const operation = ticket("policy", {
      controller: new AbortController(),
      deploymentId,
      telemetryLevel,
      expectedRevision: telemetry.policy.revision,
    });
    operations.policy = operation;
    feedback = null;
    return operation;
  }

  function acceptTelemetryPolicy(operation, response, owner) {
    if (!current("policy", operation, owner)
      || response?.policy?.deployment_id !== operation.deploymentId
      || response.policy.telemetry_level !== operation.telemetryLevel
      || !Number.isSafeInteger(response.revision)) return false;
    const previous = telemetryStates[operation.deploymentId];
    if (!previous) return false;
    telemetryStates = Object.freeze({
      ...telemetryStates,
      [operation.deploymentId]: Object.freeze({
        ...previous,
        policy: response.policy,
        revision: response.revision,
        request_id: response.request_id,
        trace_id: response.trace_id,
      }),
    });
    feedback = Object.freeze({
      kind: "success",
      code: response.idempotent_replay
        ? "edge.telemetry_policy_replayed"
        : "edge.telemetry_policy_updated",
      requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectTelemetryPolicy(operation, error, owner) {
    if (!current("policy", operation, owner)) return false;
    feedback = Object.freeze({ kind: "error", ...fixedError(error) });
    return true;
  }

  function finishTelemetryPolicy(operation) {
    if (operations.policy === operation) operations.policy = null;
  }

  function beginMaintenance(owner, deploymentId, input = {}, candidateIdempotencyKey = "") {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const upgrade = upgradeStates[deploymentId] ?? null;
    const weekdays = Array.isArray(input.weekdays)
      ? [...new Set(input.weekdays.map(Number))].sort((left, right) => left - right) : [];
    const freezeStartsAt = String(input.freezeStartsAt ?? "") || null;
    const freezeEndsAt = String(input.freezeEndsAt ?? "") || null;
    const request = {
      releaseChannel: String(input.releaseChannel ?? ""),
      timezoneName: String(input.timezoneName ?? ""),
      weekdays,
      startMinute: Number(input.startMinute),
      durationMinutes: Number(input.durationMinutes),
      freezeStartsAt,
      freezeEndsAt,
      status: String(input.status ?? ""),
      expectedRevision: upgrade?.maintenance?.revision ?? 0,
      reasonCode: String(input.reasonCode ?? ""),
    };
    if (!owns(owner) || !canManageUpgrades || !deployment || deployment.status !== "active"
      || !upgrade || !["preview", "stable", "extended"].includes(request.releaseChannel)
      || !/^[A-Za-z0-9_+./-]{3,64}$/u.test(request.timezoneName)
      || weekdays.length < 1 || weekdays.some((item) => !Number.isInteger(item)
        || item < 0 || item > 6)
      || !Number.isInteger(request.startMinute) || request.startMinute < 0
      || request.startMinute > 1439
      || !Number.isInteger(request.durationMinutes) || request.durationMinutes < 30
      || request.durationMinutes > 720
      || (freezeStartsAt === null) !== (freezeEndsAt === null)
      || !["active", "disabled"].includes(request.status)
      || !/^[a-z][a-z0-9_.-]{1,79}$/u.test(request.reasonCode) || busy()) return null;
    const fingerprint = JSON.stringify({ deploymentId, ...request });
    const idempotencyKey = maintenanceIntent?.fingerprint === fingerprint
      ? maintenanceIntent.idempotencyKey : String(candidateIdempotencyKey ?? "");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u.test(idempotencyKey)) return null;
    maintenanceIntent = Object.freeze({ fingerprint, idempotencyKey });
    generations.maintenance += 1;
    const operation = ticket("maintenance", {
      controller: new AbortController(), deploymentId, request: Object.freeze(request),
      idempotencyKey,
    });
    operations.maintenance = operation;
    feedback = null;
    return operation;
  }

  function acceptMaintenance(operation, response, owner) {
    if (!current("maintenance", operation, owner)
      || !validUpgradeResponse(response, operation.deploymentId)
      || response.maintenance === null
      || response.maintenance.revision <= operation.request.expectedRevision) return false;
    upgradeStates = Object.freeze({ ...upgradeStates, [operation.deploymentId]: response });
    maintenanceIntent = null;
    feedback = Object.freeze({
      kind: "success", code: "edge_upgrade.maintenance_updated",
      requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectMaintenance(operation, error, owner) {
    if (!current("maintenance", operation, owner)) return false;
    if (![0, 503].includes(Number(error?.status ?? -1))) maintenanceIntent = null;
    feedback = Object.freeze({ kind: "error", ...fixedError(error) });
    return true;
  }

  function finishMaintenance(operation) {
    if (operations.maintenance === operation) operations.maintenance = null;
  }

  function beginLicenseRefresh(owner, deploymentId, candidateIdempotencyKey = "") {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const license = licenseStates[deploymentId] ?? null;
    if (!owns(owner) || !canRefreshLicenses || !deployment || deployment.status !== "active"
      || !license || license.status !== "active" || busy()) return null;
    const fingerprint = JSON.stringify({
      deploymentId,
      licenseId: license.license_id,
      expectedRevision: license.revision,
      reasonCode: "online_refresh",
    });
    const idempotencyKey = licenseRefreshIntent?.fingerprint === fingerprint
      ? licenseRefreshIntent.idempotencyKey : String(candidateIdempotencyKey ?? "");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u.test(idempotencyKey)) return null;
    licenseRefreshIntent = Object.freeze({ fingerprint, idempotencyKey });
    generations.refreshLicense += 1;
    const operation = ticket("refreshLicense", {
      controller: new AbortController(),
      deploymentId,
      licenseId: license.license_id,
      expectedRevision: license.revision,
      reasonCode: "online_refresh",
      idempotencyKey,
    });
    operations.refreshLicense = operation;
    feedback = null;
    return operation;
  }

  function acceptLicenseRefresh(operation, response, owner) {
    const license = response?.license;
    if (!current("refreshLicense", operation, owner)
      || license?.license_id !== operation.licenseId
      || license.deployment_id !== operation.deploymentId
      || license.status !== "active"
      || !Number.isSafeInteger(license.revision)
      || license.revision <= operation.expectedRevision) return false;
    licenseStates = Object.freeze({ ...licenseStates, [operation.deploymentId]: license });
    licenseRefreshIntent = null;
    feedback = Object.freeze({
      kind: "success",
      code: response.idempotent_replay
        ? "edge_license.refresh_replayed" : "edge_license.refresh_completed",
      requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectLicenseRefresh(operation, error, owner) {
    if (!current("refreshLicense", operation, owner)) return false;
    if (![0, 503].includes(Number(error?.status ?? -1))) licenseRefreshIntent = null;
    feedback = Object.freeze({ kind: "error", ...fixedError(error) });
    return true;
  }

  function finishLicenseRefresh(operation) {
    if (operations.refreshLicense === operation) operations.refreshLicense = null;
  }

  function beginDrillMutation(owner, input = {}, candidateIdempotencyKey = "") {
    const deploymentId = String(input.deploymentId ?? "");
    const action = String(input.action ?? "");
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const status = backupStates[deploymentId] ?? null;
    if (!owns(owner) || !deployment || deployment.status !== "active" || !status || busy()) {
      return null;
    }
    let request;
    if (action === "request" && canManageDrills) {
      const sourceBackupId = String(input.sourceBackupId ?? "");
      const source = status.recent_backups?.find((item) => (
        item.backup_id === sourceBackupId && item.status === "completed"
          && item.check_outcome === "verified"
      ));
      if (!source
        || !/^[0-9]{4}-Q[1-4]$/u.test(String(input.quarterCode ?? ""))
        || !boundedInteger(input.rpoObjectiveSeconds, 900, 604_800)
        || !boundedInteger(input.rtoObjectiveSeconds, 900, 604_800)
        || !SHA256.test(String(input.isolatedTargetSha256 ?? ""))
        || !/^[a-z][a-z0-9_.-]{1,79}$/u.test(String(input.reasonCode ?? ""))) return null;
      request = Object.freeze({
        action,
        sourceBackupId,
        quarterCode: String(input.quarterCode),
        rpoObjectiveSeconds: input.rpoObjectiveSeconds,
        rtoObjectiveSeconds: input.rtoObjectiveSeconds,
        isolatedTargetSha256: String(input.isolatedTargetSha256),
        reasonCode: String(input.reasonCode),
      });
    } else if (action === "approve" && canApproveRestores) {
      const drillId = String(input.drillId ?? "");
      const drill = status.recent_drills?.find((item) => item.drill_id === drillId);
      if (!drill || drill.status !== "requested"
        || drill.requested_by_account_id === owner.accountId
        || !/^[a-z][a-z0-9_.-]{1,79}$/u.test(String(input.reasonCode ?? ""))) return null;
      request = Object.freeze({
        action,
        drillId,
        expectedRevision: drill.revision,
        reasonCode: String(input.reasonCode),
      });
    } else {
      return null;
    }
    const fingerprint = JSON.stringify({ deploymentId, ...request });
    const idempotencyKey = drillMutationIntent?.fingerprint === fingerprint
      ? drillMutationIntent.idempotencyKey : String(candidateIdempotencyKey ?? "");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u.test(idempotencyKey)) return null;
    drillMutationIntent = Object.freeze({ fingerprint, idempotencyKey });
    generations.drillMutation += 1;
    const operation = ticket("drillMutation", {
      controller: new AbortController(), deploymentId, workspaceId: deployment.workspace_id,
      request, idempotencyKey,
    });
    operations.drillMutation = operation;
    feedback = null;
    return operation;
  }

  function acceptDrillMutation(operation, response, owner) {
    const drill = response?.drill;
    if (!current("drillMutation", operation, owner)
      || response?.deployment_id !== operation.deploymentId
      || !validDrillSummary(drill)
      || (operation.request.action === "request"
        && drill.source_backup_id !== operation.request.sourceBackupId)
      || (operation.request.action === "approve"
        && (drill.drill_id !== operation.request.drillId
          || drill.revision <= operation.request.expectedRevision
          || drill.status !== "approved"))) return false;
    const currentStatus = backupStates[operation.deploymentId];
    backupStates = Object.freeze({
      ...backupStates,
      [operation.deploymentId]: Object.freeze({
        ...currentStatus,
        recent_drills: Object.freeze([
          drill,
          ...(currentStatus.recent_drills ?? []).filter(
            (item) => item.drill_id !== drill.drill_id,
          ),
        ].slice(0, 20)),
      }),
    });
    drillMutationIntent = null;
    feedback = Object.freeze({
      kind: "success",
      code: operation.request.action === "request"
        ? "edge_backup.drill_requested" : "edge_backup.drill_approved",
      requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectDrillMutation(operation, error, owner) {
    if (!current("drillMutation", operation, owner)) return false;
    if (![0, 503].includes(Number(error?.status ?? -1))) drillMutationIntent = null;
    feedback = Object.freeze({ kind: "error", ...fixedError(error) });
    return true;
  }

  function finishDrillMutation(operation) {
    if (operations.drillMutation === operation) operations.drillMutation = null;
  }

  function isDrillMutationCurrent(operation, owner) {
    return current("drillMutation", operation, owner);
  }

  function beginLocalBackup(owner, input = {}) {
    const deploymentId = String(input.deploymentId ?? "");
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    if (!owns(owner) || !canCreateBackups || !deployment || deployment.status !== "active"
      || !sessionActive(deploymentId)
      || !boundedInteger(input.sourceGeneration, 1, 2_147_483_647)
      || !boundedInteger(input.retentionSeconds, 3600, 31_536_000)
      || !["local", "customer_object_store"].includes(input.storageKind)
      || input.confirmed !== true || busy()) return null;
    generations.localBackup += 1;
    const operation = ticket("localBackup", {
      controller: new AbortController(), deploymentId,
      request: Object.freeze({
        sourceGeneration: input.sourceGeneration,
        retentionSeconds: input.retentionSeconds,
        storageKind: input.storageKind,
      }),
    });
    operations.localBackup = operation;
    feedback = null;
    return operation;
  }

  function acceptLocalBackup(operation, response, owner) {
    if (!current("localBackup", operation, owner)
      || !validLocalBackupSummary(response, operation.deploymentId)
      || response.source_generation !== operation.request.sourceGeneration
      || response.storage_kind !== operation.request.storageKind) return false;
    const previous = localBackupStates[operation.deploymentId] ?? {};
    localBackupStates = Object.freeze({
      ...localBackupStates,
      [operation.deploymentId]: Object.freeze({ ...previous, backup: response }),
    });
    feedback = Object.freeze({
      kind: "success", code: "local_backup.created", requestId: "-",
    });
    return true;
  }

  function rejectLocalBackup(operation, error, owner) {
    if (!current("localBackup", operation, owner)) return false;
    if (error?.code === "local_admin.authentication_failed") localSession = null;
    feedback = Object.freeze({ kind: "error", ...fixedError(error) });
    return true;
  }

  function finishLocalBackup(operation) {
    if (operations.localBackup === operation) operations.localBackup = null;
  }

  function beginLocalRestore(owner, input = {}) {
    const deploymentId = String(input.deploymentId ?? "");
    const backupId = String(input.backupId ?? "");
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const local = localBackupStates[deploymentId]?.backup;
    const cloud = backupStates[deploymentId]?.recent_backups?.find(
      (item) => item.backup_id === backupId && item.status === "completed",
    );
    if (!owns(owner) || !canApproveRestores || !deployment
      || deployment.status !== "active" || !sessionActive(deploymentId)
      || !BACKUP_ID.test(backupId) || (local?.backup_id !== backupId && !cloud)
      || !/^[0-9]{4}$/u.test(String(input.targetSchemaHead ?? ""))
      || input.confirmedTarget !== true || input.confirmedScope !== true
      || input.confirmedCheckpoint !== true || busy()) return null;
    generations.localRestore += 1;
    const operation = ticket("localRestore", {
      controller: new AbortController(), deploymentId,
      request: Object.freeze({ backupId, targetSchemaHead: String(input.targetSchemaHead) }),
    });
    operations.localRestore = operation;
    feedback = null;
    return operation;
  }

  function acceptLocalRestore(operation, response, owner) {
    if (!current("localRestore", operation, owner)
      || !validLocalRestoreSummary(response, operation.deploymentId)
      || response.backup_id !== operation.request.backupId
      || response.target_schema_head !== operation.request.targetSchemaHead) return false;
    const previous = localBackupStates[operation.deploymentId] ?? {};
    localBackupStates = Object.freeze({
      ...localBackupStates,
      [operation.deploymentId]: Object.freeze({ ...previous, restore: response }),
    });
    feedback = Object.freeze({
      kind: response.status === "succeeded" ? "success" : "error",
      code: response.status === "succeeded" ? "local_restore.completed" : response.failure_code,
      requestId: "-",
    });
    return true;
  }

  function rejectLocalRestore(operation, error, owner) {
    if (!current("localRestore", operation, owner)) return false;
    if (error?.code === "local_admin.authentication_failed") localSession = null;
    feedback = Object.freeze({ kind: "error", ...fixedError(error) });
    return true;
  }

  function finishLocalRestore(operation) {
    if (operations.localRestore === operation) operations.localRestore = null;
  }

  function beginUpgradeRollback(
    owner, deploymentId, targetReleaseId = null, reasonCode = "operator_requested",
    candidateIdempotencyKey = "",
  ) {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const upgrade = upgradeStates[deploymentId] ?? null;
    const target = targetReleaseId || upgrade?.head?.lkg_release_id || null;
    if (!owns(owner) || !canRollbackUpgrades || !deployment
      || deployment.status !== "active" || !upgrade || !target
      || target === upgrade.head.current_release_id
      || !/^[a-z][a-z0-9_.-]{1,79}$/u.test(String(reasonCode ?? "")) || busy()) return null;
    const fingerprint = JSON.stringify({
      deploymentId, targetReleaseId: target, expectedHeadRevision: upgrade.head.revision,
      reasonCode: String(reasonCode),
    });
    const idempotencyKey = rollbackUpgradeIntent?.fingerprint === fingerprint
      ? rollbackUpgradeIntent.idempotencyKey : String(candidateIdempotencyKey ?? "");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u.test(idempotencyKey)) return null;
    rollbackUpgradeIntent = Object.freeze({ fingerprint, idempotencyKey });
    generations.rollbackUpgrade += 1;
    const operation = ticket("rollbackUpgrade", {
      controller: new AbortController(),
      deploymentId,
      workspaceId,
      targetReleaseId: target,
      expectedHeadRevision: upgrade.head.revision,
      reasonCode: String(reasonCode),
      idempotencyKey,
    });
    operations.rollbackUpgrade = operation;
    feedback = null;
    return operation;
  }

  function isUpgradeRollbackCurrent(operation, owner) {
    return current("rollbackUpgrade", operation, owner);
  }

  function acceptUpgradeRollback(operation, response, owner) {
    if (!current("rollbackUpgrade", operation, owner)
      || !validUpgradeResponse(response, operation.deploymentId)
      || response.head.desired_generation <= upgradeStates[operation.deploymentId]
        ?.head?.desired_generation) return false;
    upgradeStates = Object.freeze({ ...upgradeStates, [operation.deploymentId]: response });
    rollbackUpgradeIntent = null;
    feedback = Object.freeze({
      kind: "success", code: "edge_upgrade.rollback_requested",
      requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectUpgradeRollback(operation, error, owner) {
    if (!current("rollbackUpgrade", operation, owner)) return false;
    if (![0, 503].includes(Number(error?.status ?? -1))) rollbackUpgradeIntent = null;
    feedback = Object.freeze({ kind: "error", ...fixedError(error) });
    return true;
  }

  function finishUpgradeRollback(operation) {
    if (operations.rollbackUpgrade === operation) operations.rollbackUpgrade = null;
  }

  function beginValidation(owner, deploymentId, action) {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const currentValidation = validationStates[deploymentId] ?? null;
    const canRead = action === "refresh" && canReadValidations;
    const canWrite = ["start", "retry_failed"].includes(action) && canManageValidations;
    const allowed = action === "start"
      ? currentValidation === null
      : action === "refresh"
        ? Boolean(currentValidation?.validation?.validation_run_id)
        : Boolean(
          currentValidation?.validation?.status === "completed"
          && currentValidation.validation.outcome === "failed"
          && currentValidation.delivery_acceptance === null
        );
    if (!owns(owner) || (!canRead && !canWrite) || !deployment
      || deployment.status !== "active" || !allowed || busy()) return null;
    generations.validation += 1;
    const operation = ticket("validation", {
      controller: new AbortController(),
      action,
      deploymentId,
      validationRunId: currentValidation?.validation?.validation_run_id ?? "",
    });
    operations.validation = operation;
    feedback = null;
    return operation;
  }

  function acceptValidation(operation, response, owner) {
    if (!current("validation", operation, owner)
      || !validValidationResponse(response, operation.deploymentId)) return false;
    const previous = validationStates[operation.deploymentId] ?? null;
    if ((operation.action === "refresh"
      && response.validation.validation_run_id !== operation.validationRunId)
      || (operation.action === "start"
        && response.validation.source_validation_run_id !== null)
      || (operation.action === "retry_failed" && (
        response.validation.source_validation_run_id !== operation.validationRunId
        || response.validation.attempt !== previous?.validation?.attempt + 1
      ))) return false;
    validationStates = Object.freeze({
      ...validationStates,
      [operation.deploymentId]: Object.freeze(response),
    });
    feedback = Object.freeze({
      kind: "success",
      code: operation.action === "retry_failed"
        ? "edge.validation_retry_started"
        : operation.action === "refresh"
          ? "edge.validation_refreshed"
          : "edge.validation_started",
      requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectValidation(operation, error, owner) {
    if (!current("validation", operation, owner)) return false;
    feedback = Object.freeze({ kind: "error", ...fixedError(error) });
    return true;
  }

  function finishValidation(operation) {
    if (operations.validation === operation) operations.validation = null;
  }

  function openDeliveryAcceptance(owner, deploymentId) {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const response = validationStates[deploymentId] ?? null;
    const validation = response?.validation;
    if (!owns(owner) || !canAcceptDelivery || !deployment
      || deployment.status !== "active" || validation?.status !== "completed"
      || validation.outcome !== "passed" || response.delivery_acceptance !== null
      || validation.effective_checks.length !== 8
      || validation.effective_checks.some((item) => item.outcome !== "passed")
      || busy()) return false;
    closeDialogInternal();
    stateDialog = closedStateDialog();
    acceptanceDialog = closedAcceptanceDialog();
    secretDialog = closedSecretDialog();
    acceptanceDialog = Object.freeze({
      open: true,
      state: "form",
      deploymentId,
      validationRunId: validation.validation_run_id,
      error: null,
    });
    feedback = null;
    return true;
  }

  function closeDeliveryAcceptance(owner) {
    if (!owns(owner) || operations.acceptance) return false;
    acceptanceDialog = closedAcceptanceDialog();
    return true;
  }

  function beginDeliveryAcceptance(owner) {
    const deployment = deployments.find(
      (item) => item.deployment_id === acceptanceDialog.deploymentId,
    );
    const response = validationStates[acceptanceDialog.deploymentId] ?? null;
    if (!owns(owner) || !canAcceptDelivery || acceptanceDialog.state !== "form"
      || !deployment || deployment.status !== "active"
      || response?.validation?.validation_run_id !== acceptanceDialog.validationRunId
      || response.validation.outcome !== "passed" || response.delivery_acceptance !== null
      || busy()) return null;
    generations.acceptance += 1;
    const operation = ticket("acceptance", {
      controller: new AbortController(),
      deploymentId: deployment.deployment_id,
      workspaceId: deployment.workspace_id,
      validationRunId: acceptanceDialog.validationRunId,
      expectedRevision: deployment.revision,
    });
    operations.acceptance = operation;
    acceptanceDialog = Object.freeze({
      ...acceptanceDialog,
      state: "submitting",
      error: null,
    });
    return operation;
  }

  function acceptDeliveryAcceptance(operation, response, owner) {
    if (!current("acceptance", operation, owner)
      || response?.acceptance?.validation_run_id !== operation.validationRunId) return false;
    const validationResponse = validationStates[operation.deploymentId];
    if (validationResponse?.validation?.validation_run_id !== operation.validationRunId) {
      return false;
    }
    validationStates = Object.freeze({
      ...validationStates,
      [operation.deploymentId]: Object.freeze({
        ...validationResponse,
        delivery_acceptance: response.acceptance,
        idempotent_replay: response.idempotent_replay,
        request_id: response.request_id,
        trace_id: response.trace_id,
      }),
    });
    acceptanceDialog = closedAcceptanceDialog();
    feedback = Object.freeze({
      kind: "success",
      code: response.idempotent_replay
        ? "edge.delivery_acceptance_replayed"
        : "edge.delivery_accepted",
      requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectDeliveryAcceptance(operation, error, owner) {
    if (!current("acceptance", operation, owner)) return false;
    acceptanceDialog = Object.freeze({
      ...acceptanceDialog,
      state: "form",
      error: fixedError(error),
    });
    return true;
  }

  function finishDeliveryAcceptance(operation) {
    if (operations.acceptance === operation) operations.acceptance = null;
  }

  function isDeliveryAcceptanceCurrent(operation, owner) {
    return current("acceptance", operation, owner);
  }

  function openDiagnosticDecision(owner, deploymentId, grantId, action) {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const response = healthStates[deploymentId] ?? null;
    const grant = response?.diagnostic_grants?.find(
      (item) => item.diagnostic_grant_id === grantId,
    );
    const allowed = action === "approve"
      ? grant?.status === "pending_customer"
      : ["pending_customer", "active"].includes(grant?.status);
    if (!owns(owner) || !canApproveDiagnostics || !deployment
      || deployment.status !== "active" || !grant || !allowed || busy()) return false;
    closeDialogInternal();
    stateDialog = closedStateDialog();
    acceptanceDialog = closedAcceptanceDialog();
    secretDialog = closedSecretDialog();
    diagnosticDialog = Object.freeze({
      open: true,
      state: "form",
      deploymentId,
      grantId,
      action,
      ttlSeconds: 300,
      error: null,
    });
    feedback = null;
    return true;
  }

  function closeDiagnosticDecision(owner) {
    if (!owns(owner) || operations.diagnostic) return false;
    diagnosticDialog = closedDiagnosticDialog();
    return true;
  }

  function beginDiagnosticDecision(owner, ttlSeconds = 300) {
    const deployment = deployments.find(
      (item) => item.deployment_id === diagnosticDialog.deploymentId,
    );
    const response = healthStates[diagnosticDialog.deploymentId] ?? null;
    const grant = response?.diagnostic_grants?.find(
      (item) => item.diagnostic_grant_id === diagnosticDialog.grantId,
    );
    const allowed = diagnosticDialog.action === "approve"
      ? grant?.status === "pending_customer"
      : ["pending_customer", "active"].includes(grant?.status);
    const ttl = Number(ttlSeconds);
    if (!owns(owner) || !canApproveDiagnostics || diagnosticDialog.state !== "form"
      || !deployment || deployment.status !== "active" || !grant || !allowed || busy()
      || (diagnosticDialog.action === "approve"
        && (!Number.isSafeInteger(ttl) || ttl < 60 || ttl > 300))) return null;
    generations.diagnostic += 1;
    const operation = ticket("diagnostic", {
      controller: new AbortController(),
      deploymentId: deployment.deployment_id,
      workspaceId: deployment.workspace_id,
      grantId: grant.diagnostic_grant_id,
      action: diagnosticDialog.action,
      ttlSeconds: diagnosticDialog.action === "approve" ? ttl : null,
    });
    operations.diagnostic = operation;
    diagnosticDialog = Object.freeze({
      ...diagnosticDialog,
      state: "submitting",
      ttlSeconds: ttl,
      error: null,
    });
    return operation;
  }

  function acceptDiagnosticDecision(operation, response, owner) {
    if (!current("diagnostic", operation, owner)
      || response?.grant?.diagnostic_grant_id !== operation.grantId
      || response.grant.deployment_id !== operation.deploymentId) return false;
    const previous = healthStates[operation.deploymentId];
    if (!previous) return false;
    const grants = previous.diagnostic_grants.map((item) => (
      item.diagnostic_grant_id === operation.grantId ? response.grant : item
    ));
    healthStates = Object.freeze({
      ...healthStates,
      [operation.deploymentId]: Object.freeze({
        ...previous,
        diagnostic_grants: Object.freeze(grants),
        request_id: response.request_id,
        trace_id: response.trace_id,
      }),
    });
    diagnosticDialog = closedDiagnosticDialog();
    feedback = Object.freeze({
      kind: "success",
      code: operation.action === "approve"
        ? "edge.diagnostic_grant_approved"
        : "edge.diagnostic_grant_revoked",
      requestId: safeChain(response.request_id),
    });
    return true;
  }

  function rejectDiagnosticDecision(operation, error, owner) {
    if (!current("diagnostic", operation, owner)) return false;
    diagnosticDialog = Object.freeze({
      ...diagnosticDialog,
      state: "form",
      error: fixedError(error),
    });
    return true;
  }

  function finishDiagnosticDecision(operation) {
    if (operations.diagnostic === operation) operations.diagnostic = null;
  }

  function isDiagnosticDecisionCurrent(operation, owner) {
    return current("diagnostic", operation, owner);
  }

  function beginSupportBundle(owner, deploymentId, action) {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const entry = supportBundleStates[deploymentId] ?? null;
    const local = entry?.local ?? null;
    const bundleId = local?.cloud_object_reference
      ?? entry?.cloud?.bundle?.support_bundle_id
      ?? local?.bundle_id
      ?? "";
    let allowed = false;
    if (action === "preview") {
      allowed = canManageSupportBundles && sessionActive(deploymentId)
        && (!local || ["uploaded", "failed", "cancelled", "expired"].includes(local.status));
    } else if (["approve", "cancel"].includes(action)) {
      allowed = canManageSupportBundles && sessionActive(deploymentId)
        && local?.status === "preview_ready";
    } else if (action === "refresh") {
      allowed = canReadSupportBundles && SUPPORT_BUNDLE_ID.test(bundleId);
    }
    if (!owns(owner) || !deployment || deployment.status !== "active"
      || !allowed || busy()) return null;
    generations.supportBundle += 1;
    const operation = ticket("supportBundle", {
      controller: new AbortController(),
      deploymentId,
      action,
      bundleId,
      revision: local?.revision ?? 0,
    });
    operations.supportBundle = operation;
    feedback = null;
    return operation;
  }

  function acceptSupportBundle(operation, response, owner) {
    if (!current("supportBundle", operation, owner)) return false;
    const previous = supportBundleStates[operation.deploymentId] ?? Object.freeze({
      local: null,
      cloud: null,
    });
    let next;
    if (operation.action === "refresh") {
      if (response?.bundle?.support_bundle_id !== operation.bundleId
        || response.bundle.deployment_id !== operation.deploymentId) return false;
      next = Object.freeze({ ...previous, cloud: Object.freeze(response) });
    } else {
      const local = response?.local ?? response;
      const cloud = response?.cloud ?? null;
      if (!validLocalBundleState(local, operation.deploymentId)
        || (operation.action === "preview" && local.status !== "preview_ready")
        || (operation.action === "approve" && local.status !== "uploaded")
        || (operation.action === "cancel" && local.status !== "cancelled")
        || (operation.action !== "preview" && local.bundle_id !== operation.bundleId)
        || (cloud && (
          cloud.bundle?.support_bundle_id !== local.cloud_object_reference
          || cloud.bundle?.deployment_id !== operation.deploymentId
        ))) return false;
      next = Object.freeze({ local: Object.freeze(local), cloud: cloud ? Object.freeze(cloud) : null });
    }
    supportBundleStates = Object.freeze({
      ...supportBundleStates,
      [operation.deploymentId]: next,
    });
    const codes = {
      preview: "local_support_bundle.preview_ready",
      approve: "local_support_bundle.uploaded",
      cancel: "local_support_bundle.cancelled",
      refresh: "edge.support_bundle_refreshed",
    };
    feedback = Object.freeze({
      kind: "success",
      code: codes[operation.action],
      requestId: safeChain(response?.cloud?.request_id ?? response?.request_id),
    });
    return true;
  }

  function rejectSupportBundle(operation, error, owner) {
    if (!current("supportBundle", operation, owner)) return false;
    const fixed = fixedError(error);
    if (fixed.code === "local_admin.authentication_failed") localSession = null;
    feedback = Object.freeze({ kind: "error", ...fixed });
    return true;
  }

  function finishSupportBundle(operation) {
    if (operations.supportBundle === operation) operations.supportBundle = null;
  }

  function openSecretDialog(owner, deploymentId, action, referenceId = "") {
    const deployment = deployments.find((item) => item.deployment_id === deploymentId);
    const reference = secretReferences.find((item) => item.reference_id === referenceId);
    const allowed = action === "handoff"
      || (action === "create" && sessionActive(deploymentId))
      || (action === "rotate" && sessionActive(deploymentId)
        && reference?.deployment_id === deploymentId && reference.status !== "disabled");
    if (!owns(owner) || !canManageSecrets || !deployment || !allowed || busy()) return false;
    closeDialogInternal();
    stateDialog = closedStateDialog();
    acceptanceDialog = closedAcceptanceDialog();
    secretDialog = Object.freeze({
      open: true,
      state: "form",
      action,
      deploymentId,
      referenceId: action === "rotate" ? referenceId : "",
      error: null,
    });
    feedback = null;
    return true;
  }

  function closeSecretDialog(owner) {
    if (!owns(owner) || operations.localSession || operations.secret) return false;
    secretDialog = closedSecretDialog();
    return true;
  }

  function beginLocalSession(owner, localApiOrigin) {
    const deployment = deployments.find(
      (item) => item.deployment_id === secretDialog.deploymentId,
    );
    const origin = String(localApiOrigin ?? "");
    if (!owns(owner) || !canManageSecrets || secretDialog.action !== "handoff"
      || secretDialog.state !== "form" || !deployment || !LOCAL_ORIGIN.test(origin)
      || busy()) return null;
    generations.localSession += 1;
    const operation = ticket("localSession", {
      controller: new AbortController(),
      deploymentId: deployment.deployment_id,
      workspaceId: deployment.workspace_id,
      tenantId: owner.tenantId,
      accountId: owner.accountId,
      localApiOrigin: origin,
    });
    operations.localSession = operation;
    secretDialog = Object.freeze({ ...secretDialog, state: "submitting", error: null });
    return operation;
  }

  function acceptLocalSession(operation, response, owner) {
    if (!current("localSession", operation, owner)
      || response?.deployment_id !== operation.deploymentId
      || response?.local_api_origin !== operation.localApiOrigin
      || !Number.isSafeInteger(response?.expires_at)) return false;
    localSession = Object.freeze({
      deploymentId: operation.deploymentId,
      localApiOrigin: operation.localApiOrigin,
      expiresAt: response.expires_at,
    });
    secretDialog = closedSecretDialog();
    feedback = Object.freeze({
      kind: "success", code: "local_admin.session_created", requestId: "-",
    });
    return true;
  }

  function rejectLocalSession(operation, error, owner) {
    if (!current("localSession", operation, owner)) return false;
    localSession = null;
    secretDialog = Object.freeze({ ...secretDialog, state: "form", error: fixedError(error) });
    return true;
  }

  function finishLocalSession(operation) {
    if (operations.localSession === operation) operations.localSession = null;
  }

  function beginSecretMutation(owner, action, values = {}) {
    const deploymentId = String(values.deploymentId ?? secretDialog.deploymentId ?? "");
    const referenceId = String(values.referenceId ?? secretDialog.referenceId ?? "");
    const reference = secretReferences.find((item) => item.reference_id === referenceId);
    if (!owns(owner) || !canManageSecrets || !sessionActive(deploymentId) || busy()) return null;
    let request;
    if (action === "create" && secretDialog.action === "create") {
      const providerConnectionId = String(values.providerConnectionId ?? "");
      const label = String(values.label ?? "");
      const kind = String(values.kind ?? "");
      if (!OWNER_ID.test(providerConnectionId) || !SAFE_LABEL.test(label)
        || !["api_key", "bearer_token", "basic_password", "oauth_client_secret"].includes(kind)) {
        return null;
      }
      request = Object.freeze({ providerConnectionId, label, kind });
    } else if (action === "rotate" && secretDialog.action === "rotate"
      && reference?.deployment_id === deploymentId && reference.status !== "disabled") {
      request = Object.freeze({ referenceId, revision: reference.revision });
    } else if (action === "delete" && reference?.deployment_id === deploymentId
      && reference.status !== "disabled") {
      request = Object.freeze({ referenceId, revision: reference.revision });
    } else if (action === "test" && reference?.deployment_id === deploymentId
      && reference.status !== "disabled") {
      request = Object.freeze({
        referenceId,
        providerConnectionId: reference.provider_connection_id,
      });
    } else {
      return null;
    }
    generations.secret += 1;
    const operation = ticket("secret", {
      controller: new AbortController(), deploymentId, action, request,
    });
    operations.secret = operation;
    if (["create", "rotate"].includes(action)) {
      secretDialog = Object.freeze({ ...secretDialog, state: "submitting", error: null });
    }
    feedback = null;
    return operation;
  }

  function acceptSecretMutation(operation, response, owner) {
    if (!current("secret", operation, owner)) return false;
    if (["create", "rotate"].includes(operation.action)) {
      if (!validSecretReference(response)
        || response.deployment_id !== operation.deploymentId
        || (owner.tenantId
          ? response.tenant_id !== owner.tenantId
          : response.workspace_id !== owner.workspaceId)) return false;
      secretReferences = Object.freeze([
        response,
        ...secretReferences.filter((item) => item.reference_id !== response.reference_id),
      ]);
      secretDialog = closedSecretDialog();
    } else if (operation.action === "delete") {
      if (response !== true) return false;
      secretReferences = Object.freeze(secretReferences.filter(
        (item) => item.reference_id !== operation.request.referenceId,
      ));
    } else if (operation.action === "test") {
      if (response?.reference_id !== operation.request.referenceId) return false;
      secretReferences = Object.freeze(secretReferences.map((item) => (
        item.reference_id === response.reference_id
          ? Object.freeze({
            ...item,
            active_version: response.version,
            last_test_outcome: response.outcome,
            capabilities: response.capabilities,
            updated_at: response.created_at,
          })
          : item
      )));
    } else return false;
    const codes = {
      create: "local_secret.created",
      rotate: "local_secret.rotated",
      delete: "local_secret.deleted",
      test: "local_secret.test_completed",
    };
    feedback = Object.freeze({
      kind: "success", code: codes[operation.action], requestId: "-",
    });
    return true;
  }

  function rejectSecretMutation(operation, error, owner) {
    if (!current("secret", operation, owner)) return false;
    const fixed = fixedError(error);
    if (fixed.code === "local_admin.authentication_failed") localSession = null;
    if (["create", "rotate"].includes(operation.action)) {
      secretDialog = Object.freeze({ ...secretDialog, state: "form", error: fixed });
    } else {
      feedback = Object.freeze({ kind: "error", ...fixed });
    }
    return true;
  }

  function finishSecretMutation(operation) {
    if (operations.secret === operation) operations.secret = null;
  }

  function blocksShellDomReplacement() {
    return dialog.open || stateDialog.open || secretDialog.open || acceptanceDialog.open
      || diagnosticDialog.open
      || Boolean(operations.create || operations.copy || operations.revoke
        || operations.command || operations.policy || operations.localSession
        || operations.secret || operations.validation || operations.acceptance
        || operations.supportBundle || operations.diagnostic || operations.maintenance
        || operations.rollbackUpgrade || operations.refreshLicense
        || operations.drillMutation || operations.localBackup || operations.localRestore);
  }

  function snapshot() {
    return Object.freeze({
      state: listState,
      error: listError,
      deployments,
      desiredStates,
      telemetryStates,
      secretReferences,
      installerCatalog,
      installerSelection,
      validationStates,
      supportBundleStates,
      healthStates,
      upgradeStates,
      licenseStates,
      backupStates,
      offlineBundleStates,
      localBackupStates,
      dialog,
      stateDialog,
      registration,
      secretDialog,
      acceptanceDialog,
      diagnosticDialog,
      localSession,
      feedback,
      canManage,
      canReadTelemetry,
      canManageTelemetry,
      canReadUsage,
      canReadSecrets,
      canManageSecrets,
      canReadInstallers,
      canManageInstallations,
      canReadValidations,
      canManageValidations,
      canAcceptDelivery,
      canReadSupportBundles,
      canManageSupportBundles,
      canReadHealth,
      canReadDiagnostics,
      canApproveDiagnostics,
      canReadUpgrades,
      canManageUpgrades,
      canRollbackUpgrades,
      canReadLicenses,
      canRefreshLicenses,
      canImportLicenses,
      canReadBackups,
      canCreateBackups,
      canManageDrills,
      canApproveRestores,
      canReadOfflineBundles,
      canTransferOfflineBundles,
      canResolveOfflineConflicts,
      revokingId: operations.revoke?.deploymentId ?? "",
      commandDeploymentId: operations.command?.deploymentId ?? "",
      busy: busy(),
      secretBusy: Boolean(operations.localSession || operations.secret),
      validationBusy: Boolean(operations.validation || operations.acceptance),
      supportBundleBusy: Boolean(operations.supportBundle),
      diagnosticBusy: Boolean(operations.diagnostic),
      upgradeBusy: Boolean(operations.maintenance || operations.rollbackUpgrade),
      licenseBusy: Boolean(operations.refreshLicense),
      backupBusy: Boolean(
        operations.drillMutation || operations.localBackup || operations.localRestore
      ),
    });
  }

  function ticket(lane, values) {
    return Object.freeze({ lane, ownerKey, generation: generations[lane], ...values });
  }

  function owns(owner) { const key = ownerKeyFor(owner); return Boolean(key && key === ownerKey); }

  function current(lane, operation, owner) {
    return Boolean(operation && operation === operations[lane]
      && operation.ownerKey === ownerKey && operation.ownerKey === ownerKeyFor(owner)
      && operation.generation === generations[lane]
      && !operation.controller?.signal?.aborted);
  }

  function busy() {
    return Boolean(operations.create || operations.revoke || operations.copy
      || operations.command || operations.policy || operations.localSession
      || operations.secret || operations.validation || operations.acceptance
      || operations.supportBundle || operations.diagnostic || operations.maintenance
      || operations.rollbackUpgrade || operations.refreshLicense
      || operations.drillMutation || operations.localBackup || operations.localRestore);
  }

  function sessionActive(deploymentId) {
    return Boolean(localSession && localSession.deploymentId === deploymentId
      && localSession.expiresAt > Math.floor(Date.now() / 1000));
  }

  function clearRegistration() { registration = null; }

  function closeDialogInternal() {
    clearRegistration();
    dialog = closedDialog();
  }

  return Object.freeze({
    enter, clear, beginLoad, isLoadCurrent, acceptLoad, rejectLoad, finishLoad,
    selectInstallerEnvironment,
    openCreate, closeCreate, beginCreate, acceptCreate, rejectCreate, finishCreate,
    beginCopy, acceptCopy, rejectCopy, beginRevoke, acceptRevoke, rejectRevoke,
    finishRevoke, openStateCommand, closeStateCommand, beginStateCommand,
    acceptStateCommand, rejectStateCommand, finishStateCommand,
    beginTelemetryPolicy, acceptTelemetryPolicy, rejectTelemetryPolicy,
    finishTelemetryPolicy,
    beginMaintenance, acceptMaintenance, rejectMaintenance, finishMaintenance,
    beginLicenseRefresh, acceptLicenseRefresh, rejectLicenseRefresh, finishLicenseRefresh,
    beginDrillMutation, isDrillMutationCurrent, acceptDrillMutation,
    rejectDrillMutation, finishDrillMutation,
    beginLocalBackup, acceptLocalBackup, rejectLocalBackup, finishLocalBackup,
    beginLocalRestore, acceptLocalRestore, rejectLocalRestore, finishLocalRestore,
    beginUpgradeRollback, isUpgradeRollbackCurrent, acceptUpgradeRollback,
    rejectUpgradeRollback, finishUpgradeRollback,
    beginValidation, acceptValidation, rejectValidation, finishValidation,
    openDeliveryAcceptance, closeDeliveryAcceptance, beginDeliveryAcceptance,
    acceptDeliveryAcceptance, rejectDeliveryAcceptance, finishDeliveryAcceptance,
    isDeliveryAcceptanceCurrent,
    openDiagnosticDecision, closeDiagnosticDecision, beginDiagnosticDecision,
    acceptDiagnosticDecision, rejectDiagnosticDecision, finishDiagnosticDecision,
    isDiagnosticDecisionCurrent,
    beginSupportBundle, acceptSupportBundle, rejectSupportBundle, finishSupportBundle,
    openSecretDialog, closeSecretDialog, beginLocalSession, acceptLocalSession,
    rejectLocalSession, finishLocalSession, beginSecretMutation,
    acceptSecretMutation, rejectSecretMutation, finishSecretMutation,
    blocksShellDomReplacement, snapshot,
  });
}

export function isPrivateGatewayAbortError(error) { return error?.name === "AbortError"; }

function ownerKeyFor(owner) {
  if (!owner || owner.page !== PAGE_ID || !(owner.tenantId || owner.workspaceId)
    || !owner.readIdentity || !owner.navigationOwner) return "";
  return JSON.stringify([
    owner.page, owner.tenantId, owner.workspaceId, owner.readIdentity, owner.manageIdentity,
    owner.telemetryReadIdentity, owner.telemetryManageIdentity, owner.usageReadIdentity,
    owner.secretReadIdentity, owner.accountId, owner.role,
    owner.installerReadIdentity, owner.installationManageIdentity,
    owner.validationReadIdentity, owner.validationManageIdentity,
    owner.deliveryAcceptIdentity, owner.supportBundleReadIdentity,
    owner.healthReadIdentity, owner.diagnosticReadIdentity,
    owner.diagnosticApproveIdentity,
    owner.upgradeReadIdentity, owner.upgradeManageIdentity, owner.rollbackManageIdentity,
    owner.licenseReadIdentity, owner.licenseRefreshIdentity, owner.licenseImportIdentity,
    owner.backupReadIdentity, owner.backupCreateIdentity, owner.drillManageIdentity,
    owner.restoreApproveIdentity,
    owner.offlineReadIdentity, owner.offlineTransferIdentity, owner.offlineResolveIdentity,
    owner.navigationOwner,
  ]);
}

function validList(response) {
  return Boolean(response && Array.isArray(response.deployments)
    && response.deployments.every((item) => DEPLOYMENT_ID.test(String(item?.deployment_id ?? ""))));
}

function validRegistration(response) {
  return Boolean(response?.deployment && DEPLOYMENT_ID.test(String(response.deployment.deployment_id ?? ""))
    && typeof response.idempotent_replay === "boolean");
}

function validHealthResponses(responses, deployments, readsHealth, readsDiagnostics) {
  if (!readsHealth) return Array.isArray(responses) && responses.length === 0;
  if (!Array.isArray(responses) || responses.length !== deployments.length) return false;
  const expected = new Set(deployments.map((item) => item.deployment_id));
  return responses.every((entry) => {
    const deploymentId = String(entry?.deploymentId ?? "");
    const response = entry?.response;
    return expected.has(deploymentId)
      && response?.health?.deployment_id === deploymentId
      && Number.isSafeInteger(response.health.generation)
      && response.health.generation > 0
      && Array.isArray(response.diagnostic_grants)
      && Array.isArray(response.diagnostic_results)
      && (readsDiagnostics || (
        response.diagnostic_grants.length === 0 && response.diagnostic_results.length === 0
      ));
  }) && new Set(responses.map((entry) => entry.deploymentId)).size === deployments.length;
}

function healthStateMap(deployments, responses) {
  const expected = new Set(deployments.map((item) => item.deployment_id));
  return Object.freeze(Object.fromEntries(responses.filter(
    (entry) => expected.has(entry.deploymentId),
  ).map((entry) => [entry.deploymentId, entry.response])));
}

function validUpgradeResponses(responses, deployments, required) {
  if (!required) return Array.isArray(responses) && responses.length === 0;
  if (!Array.isArray(responses) || responses.length !== deployments.length) return false;
  const expected = new Set(deployments.map((item) => item.deployment_id));
  const seen = new Set();
  return responses.every((entry) => {
    const deploymentId = String(entry?.deploymentId ?? "");
    if (!expected.has(deploymentId) || seen.has(deploymentId)
      || !validUpgradeResponse(entry?.response, deploymentId)) return false;
    seen.add(deploymentId);
    return true;
  });
}

function validUpgradeResponse(response, deploymentId) {
  return Boolean(response?.deployment_id === deploymentId
    && response.head?.deployment_id === deploymentId
    && Number.isSafeInteger(response.head.revision) && response.head.revision > 0
    && Number.isSafeInteger(response.head.desired_generation)
    && Number.isSafeInteger(response.head.applied_generation)
    && Number.isSafeInteger(response.head.lkg_generation)
    && response.head.lkg_generation <= response.head.applied_generation
    && response.head.applied_generation <= response.head.desired_generation
    && (response.maintenance === null || response.maintenance?.deployment_id === deploymentId)
    && Array.isArray(response.rollouts)
    && response.rollouts.every((item) => item.deployment_id === deploymentId)
    && Array.isArray(response.rollback_attempts)
    && response.rollback_attempts.every((item) => item.deployment_id === deploymentId));
}

function upgradeStateMap(deployments, responses) {
  const expected = new Set(deployments.map((item) => item.deployment_id));
  return Object.freeze(Object.fromEntries(responses.filter(
    (entry) => expected.has(entry.deploymentId),
  ).map((entry) => [entry.deploymentId, entry.response])));
}

function validLicenseResponses(responses, deployments, required) {
  if (!required) return Array.isArray(responses) && responses.length === 0;
  if (!Array.isArray(responses) || responses.length !== deployments.length) return false;
  const expected = new Set(deployments.map((item) => item.deployment_id));
  const seen = new Set();
  return responses.every((entry) => {
    const deploymentId = String(entry?.deploymentId ?? "");
    const license = entry?.response?.license ?? null;
    if (!expected.has(deploymentId) || seen.has(deploymentId)) return false;
    seen.add(deploymentId);
    return license === null || (
      license.deployment_id === deploymentId
      && /^lic_[0-9a-f]{32}$/u.test(String(license.license_id ?? ""))
      && ["active", "revoked"].includes(license.status)
      && Number.isSafeInteger(license.revision) && license.revision > 0
      && Number.isSafeInteger(license.generation) && license.generation > 0
      && Number.isSafeInteger(license.capacity?.node_limit)
      && Number.isSafeInteger(license.capacity?.concurrent_request_limit)
      && Array.isArray(license.capacity?.features)
    );
  });
}

function licenseStateMap(deployments, responses) {
  const expected = new Set(deployments.map((item) => item.deployment_id));
  return Object.freeze(Object.fromEntries(responses.filter(
    (entry) => expected.has(entry.deploymentId),
  ).map((entry) => [entry.deploymentId, entry.response?.license ?? null])));
}

function validBackupResponses(responses, deployments, required) {
  if (!required) return Array.isArray(responses) && responses.length === 0;
  if (!Array.isArray(responses) || responses.length !== deployments.length) return false;
  const expected = new Set(deployments.map((item) => item.deployment_id));
  const seen = new Set();
  return responses.every((entry) => {
    const deploymentId = String(entry?.deploymentId ?? "");
    const status = entry?.response?.status;
    if (!expected.has(deploymentId) || seen.has(deploymentId)
      || !validBackupStatusSummary(status, deploymentId)) return false;
    seen.add(deploymentId);
    return true;
  });
}

function backupStateMap(deployments, responses) {
  const expected = new Set(deployments.map((item) => item.deployment_id));
  return Object.freeze(Object.fromEntries(responses.filter(
    (entry) => expected.has(entry.deploymentId),
  ).map((entry) => [entry.deploymentId, entry.response.status])));
}

function validOfflineBundleResponses(responses, deployments, required) {
  if (!required) return Array.isArray(responses) && responses.length === 0;
  if (!Array.isArray(responses) || responses.length !== deployments.length) return false;
  const expected = new Set(deployments.map((item) => item.deployment_id));
  const seen = new Set();
  return responses.every((entry) => {
    const deploymentId = String(entry?.deploymentId ?? "");
    const state = entry?.response;
    if (!expected.has(deploymentId) || seen.has(deploymentId)
      || state?.deployment_id !== deploymentId || state?.asynchronous_management !== true
      || !Array.isArray(state.requests) || !Array.isArray(state.bundles)
      || !Array.isArray(state.conflicts)
      || state.requests.some((item) => item.deployment_id !== deploymentId)
      || state.bundles.some((item) => item.deployment_id !== deploymentId)
      || state.conflicts.some((item) => item.deployment_id !== deploymentId)) return false;
    seen.add(deploymentId);
    return true;
  });
}

function offlineBundleStateMap(deployments, responses) {
  const expected = new Set(deployments.map((item) => item.deployment_id));
  return Object.freeze(Object.fromEntries(responses.filter(
    (entry) => expected.has(entry.deploymentId),
  ).map((entry) => [entry.deploymentId, entry.response])));
}

function validBackupStatusSummary(value, deploymentId) {
  return Boolean(value
    && value.deployment_id === deploymentId
    && ["never", "current", "overdue", "failed"].includes(value.status)
    && boundedInteger(value.rpo_objective_seconds, 900, 604_800)
    && boundedInteger(value.latest_source_generation, 0, 2_147_483_647)
    && boundedInteger(value.revision, 1, Number.MAX_SAFE_INTEGER)
    && Array.isArray(value.recent_backups)
    && value.recent_backups.every(validBackupReport)
    && Array.isArray(value.recent_drills)
    && value.recent_drills.every(validDrillSummary)
    && (value.latest_backup === null || validBackupReport(value.latest_backup)));
}

function validBackupReport(value) {
  return Boolean(value
    && BACKUP_ID.test(String(value.backup_id ?? ""))
    && boundedInteger(value.source_generation, 1, 2_147_483_647)
    && /^[0-9]{4}$/u.test(String(value.schema_head ?? ""))
    && ["local", "customer_object_store"].includes(value.storage_kind)
    && ["completed", "failed", "deleted"].includes(value.status)
    && ["verified", "failed"].includes(value.check_outcome)
    && boundedInteger(value.chunk_count, 0, 1_000_000));
}

function validDrillSummary(value) {
  return Boolean(value
    && DRILL_ID.test(String(value.drill_id ?? ""))
    && BACKUP_ID.test(String(value.source_backup_id ?? ""))
    && ["requested", "approved", "running", "succeeded", "failed"].includes(value.status)
    && /^[0-9]{4}-Q[1-4]$/u.test(String(value.quarter_code ?? ""))
    && boundedInteger(value.rpo_objective_seconds, 900, 604_800)
    && boundedInteger(value.rto_objective_seconds, 900, 604_800)
    && SHA256.test(String(value.isolated_target_sha256 ?? ""))
    && value.production_untouched === true
    && boundedInteger(value.revision, 1, Number.MAX_SAFE_INTEGER));
}

function validLocalBackupSummary(value, deploymentId) {
  return Boolean(value
    && BACKUP_ID.test(String(value.backup_id ?? ""))
    && value.deployment_id === deploymentId
    && value.status === "ready"
    && boundedInteger(value.source_generation, 1, 2_147_483_647)
    && /^[0-9]{4}$/u.test(String(value.schema_head ?? ""))
    && ["local", "customer_object_store"].includes(value.storage_kind)
    && SHA256.test(String(value.manifest_sha256 ?? ""))
    && SHA256.test(String(value.merkle_root ?? ""))
    && SHA256.test(String(value.kms_key_id_sha256 ?? ""))
    && boundedInteger(value.chunk_count, 4, 1_000_000)
    && boundedInteger(value.revision, 1, 2_147_483_647));
}

function validLocalRestoreSummary(value, deploymentId) {
  return Boolean(value
    && RESTORE_ID.test(String(value.restore_id ?? ""))
    && BACKUP_ID.test(String(value.backup_id ?? ""))
    && value.deployment_id === deploymentId
    && /^[0-9]{4}$/u.test(String(value.target_schema_head ?? ""))
    && ["succeeded", "failed"].includes(value.status)
    && boundedInteger(value.revision, 1, 2_147_483_647)
    && (value.status !== "failed" || /^[a-z][a-z0-9_.-]{1,79}$/u.test(
      String(value.failure_code ?? ""),
    )));
}

function boundedInteger(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function validValidationResponse(response, deploymentId) {
  const validation = response?.validation;
  const acceptance = response?.delivery_acceptance ?? null;
  return Boolean(validation
    && VALIDATION_RUN_ID.test(String(validation.validation_run_id ?? ""))
    && validation.deployment_id === deploymentId
    && Number.isSafeInteger(validation.attempt) && validation.attempt > 0
    && ["requested", "completed"].includes(validation.status)
    && Array.isArray(validation.requested_check_codes)
    && Array.isArray(validation.checks)
    && Array.isArray(validation.effective_checks)
    && (acceptance === null
      || acceptance.validation_run_id === validation.validation_run_id));
}

function validLocalBundleState(value, deploymentId) {
  return Boolean(value
    && SUPPORT_BUNDLE_ID.test(String(value.bundle_id ?? ""))
    && value.deployment_id === deploymentId
    && ["preview_ready", "uploading", "uploaded", "failed", "cancelled", "expired"]
      .includes(value.status)
    && Number.isSafeInteger(value.revision) && value.revision > 0
    && Array.isArray(value.manifest?.entries)
    && value.manifest.bundle_id === value.bundle_id
    && value.manifest.deployment_id === deploymentId);
}

function retainOwnedMap(value, deploymentIds) {
  return Object.freeze(Object.fromEntries(Object.entries(value).filter(
    ([deploymentId]) => deploymentIds.has(deploymentId),
  )));
}

function validDesiredResponses(responses, deployments) {
  if (!Array.isArray(responses) || responses.length > deployments.length) return false;
  const deploymentIds = new Set(deployments.map((item) => item.deployment_id));
  const seen = new Set();
  return responses.every((item) => {
    const id = String(item?.deploymentId ?? "");
    const state = item?.response?.state ?? null;
    if (!deploymentIds.has(id) || seen.has(id)
      || !Number.isSafeInteger(item?.response?.revision)
      || item.response.revision < 0
      || (state !== null && state?.deployment_id !== id)) return false;
    seen.add(id);
    return true;
  });
}

function desiredStateMap(deployments, responses) {
  const values = Object.fromEntries(deployments.map((item) => [item.deployment_id, null]));
  for (const item of responses) values[item.deploymentId] = Object.freeze(item.response);
  return Object.freeze(values);
}

function validTelemetryResponses(responses, deployments, required) {
  if (!Array.isArray(responses)
    || (required && responses.length !== deployments.length)
    || (!required && responses.length !== 0)) return false;
  const deploymentIds = new Set(deployments.map((item) => item.deployment_id));
  const seen = new Set();
  return responses.every((item) => {
    const id = String(item?.deploymentId ?? "");
    if (!deploymentIds.has(id) || seen.has(id)
      || item?.response?.deployment_id !== id
      || !Number.isSafeInteger(item.response.revision)
      || item.response.revision < 0) return false;
    seen.add(id);
    return true;
  });
}

function telemetryStateMap(deployments, responses) {
  const values = Object.fromEntries(deployments.map((item) => [item.deployment_id, null]));
  for (const item of responses) values[item.deploymentId] = Object.freeze(item.response);
  return Object.freeze(values);
}

function validSecretResponse(response, deployments, required, owner) {
  if (!required) return response === null;
  if (!response || !Array.isArray(response.references) || response.references.length > 500) {
    return false;
  }
  const deploymentMap = new Map(
    deployments.map((item) => [item.deployment_id, item.workspace_id]),
  );
  const seen = new Set();
  return response.references.every((reference) => {
    if (!validSecretReference(reference) || seen.has(reference.reference_id)
      || (owner.tenantId
        ? reference.tenant_id !== owner.tenantId
        : reference.workspace_id !== owner.workspaceId)
      || deploymentMap.get(reference.deployment_id) !== reference.workspace_id) return false;
    seen.add(reference.reference_id);
    return true;
  });
}

function validSecretReference(value) {
  return Boolean(value && REFERENCE_ID.test(String(value.reference_id ?? ""))
    && DEPLOYMENT_ID.test(String(value.deployment_id ?? ""))
    && OWNER_ID.test(String(value.tenant_id ?? ""))
    && OWNER_ID.test(String(value.workspace_id ?? ""))
    && OWNER_ID.test(String(value.provider_connection_id ?? ""))
    && SAFE_LABEL.test(String(value.label ?? ""))
    && ["api_key", "bearer_token", "basic_password", "oauth_client_secret"].includes(value.kind)
    && ["active", "rotating", "degraded", "disabled"].includes(value.status)
    && Number.isSafeInteger(value.revision) && value.revision > 0
    && (value.active_version === null
      || (Number.isSafeInteger(value.active_version) && value.active_version > 0))
    && !(value.status === "active" && value.active_version === null)
    && !(value.status === "disabled" && value.active_version !== null));
}

function validInstallerResponse(response, deployments, required) {
  if (!required) return response === null;
  if (!response || !Array.isArray(response.releases) || !Array.isArray(response.downloads)
    || !Array.isArray(response.preflights) || !Array.isArray(response.installations)
    || response.releases.length > 100 || response.downloads.length > 3_200
    || response.preflights.length > 500 || response.installations.length > 500) return false;
  const deploymentIds = new Set(deployments.map((item) => item.deployment_id));
  const releases = new Map();
  for (const release of response.releases) {
    const releaseId = String(release?.manifest?.release_id ?? "");
    const profiles = release?.manifest?.profiles;
    const artifacts = release?.manifest?.artifacts;
    if (!INSTALLER_RELEASE_ID.test(releaseId) || releases.has(releaseId)
      || !Array.isArray(profiles) || !profiles.length || !Array.isArray(artifacts)
      || !artifacts.length || !Array.isArray(release.manifest.supported_arch)) return false;
    const profileIds = profiles.map((item) => String(item?.install_profile_id ?? ""));
    const artifactIds = artifacts.map((item) => String(item?.artifact_id ?? ""));
    if (profileIds.some((id) => !INSTALL_PROFILE_ID.test(id))
      || profileIds.length !== new Set(profileIds).size
      || artifactIds.some((id) => !/^ira_[0-9a-f]{32}$/u.test(id))
      || artifactIds.length !== new Set(artifactIds).size) return false;
    releases.set(releaseId, {
      profileIds: new Set(profileIds),
      artifactIds: new Set(artifactIds),
    });
  }
  const downloadKeys = new Set();
  for (const download of response.downloads) {
    const releaseId = String(download?.release_id ?? "");
    const artifactId = String(download?.artifact_id ?? "");
    const key = `${releaseId}\0${artifactId}`;
    if (!releases.get(releaseId)?.artifactIds.has(artifactId) || downloadKeys.has(key)
      || download.download_path !== `/edge/installers/${releaseId}/artifacts/${artifactId}`) {
      return false;
    }
    downloadKeys.add(key);
  }
  if ([...releases.entries()].some(([releaseId, value]) => (
    [...value.artifactIds].some((artifactId) => !downloadKeys.has(`${releaseId}\0${artifactId}`))
  ))) return false;
  const preflightIds = new Set();
  for (const preflight of response.preflights) {
    const id = String(preflight?.preflight_report_id ?? "");
    if (!PREFLIGHT_REPORT_ID.test(id) || preflightIds.has(id)
      || !deploymentIds.has(preflight?.deployment_id)
      || !INSTALLER_RELEASE_ID.test(String(preflight?.release_id ?? ""))
      || !INSTALL_PROFILE_ID.test(String(preflight?.install_profile_id ?? ""))
      || !["passed", "failed"].includes(preflight?.outcome)
      || !Array.isArray(preflight?.checks)) return false;
    preflightIds.add(id);
  }
  const installationIds = new Set();
  return response.installations.every((installation) => {
    const id = String(installation?.installation_id ?? "");
    if (!INSTALLATION_ID.test(id) || installationIds.has(id)
      || !deploymentIds.has(installation?.deployment_id)
      || !PREFLIGHT_REPORT_ID.test(String(installation?.preflight_report_id ?? ""))
      || !INSTALLER_RELEASE_ID.test(String(installation?.release_id ?? ""))
      || !INSTALL_PROFILE_ID.test(String(installation?.install_profile_id ?? ""))) return false;
    installationIds.add(id);
    return true;
  });
}

function freezeInstallerCatalog(response) {
  return Object.freeze({
    releases: Object.freeze([...response.releases]),
    downloads: Object.freeze([...response.downloads]),
    preflights: Object.freeze([...response.preflights]),
    installations: Object.freeze([...response.installations]),
    request_id: safeChain(response.request_id),
    trace_id: safeChain(response.trace_id),
  });
}

function reconcileInstallerSelection(current, deployments, catalog) {
  const availableDeployments = deployments.filter(
    (item) => !["revoked", "retired"].includes(item.status),
  );
  const deployment = availableDeployments.find(
    (item) => item.deployment_id === current.deploymentId,
  ) ?? availableDeployments[0];
  const release = catalog.releases.find(
    (item) => item.manifest.release_id === current.releaseId,
  ) ?? catalog.releases[0];
  const profile = release?.manifest.profiles.find(
    (item) => item.install_profile_id === current.profileId,
  ) ?? release?.manifest.profiles.find((item) => item.profile_code === "standard")
    ?? release?.manifest.profiles[0];
  const architecture = release?.manifest.supported_arch.includes(current.architecture)
    ? current.architecture
    : (release?.manifest.supported_arch.includes("x86_64") ? "x86_64"
      : release?.manifest.supported_arch[0]);
  if (!deployment || !release || !profile || !architecture) return emptyInstallerSelection();
  return Object.freeze({
    deploymentId: deployment.deployment_id,
    releaseId: release.manifest.release_id,
    profileId: profile.install_profile_id,
    architecture,
  });
}

function emptyInstallerCatalog() {
  return Object.freeze({
    releases: Object.freeze([]),
    downloads: Object.freeze([]),
    preflights: Object.freeze([]),
    installations: Object.freeze([]),
    request_id: "-",
    trace_id: "-",
  });
}

function emptyInstallerSelection() {
  return Object.freeze({
    deploymentId: "",
    releaseId: "",
    profileId: "",
    architecture: "",
  });
}

function commandRequest(action, input, desired) {
  if (action === "publish") {
    const sourceConfigSnapshotId = String(input.sourceConfigSnapshotId ?? "");
    return /^csn_[0-9a-f]{32}$/u.test(sourceConfigSnapshotId)
      ? Object.freeze({ sourceConfigSnapshotId }) : null;
  }
  if (action === "approve" && desired?.status === "approval_pending") {
    return Object.freeze({ desiredStateId: desired.desired_state_id, reasonCode: "approved_change" });
  }
  if (action === "reject" && desired?.status === "approval_pending"
    && ["risk_unacceptable", "change_not_required"].includes(input.reasonCode)) {
    return Object.freeze({ desiredStateId: desired.desired_state_id, reasonCode: input.reasonCode });
  }
  if (action === "rollback" && Number.isSafeInteger(desired?.lkg_generation)
    && desired.lkg_generation > 0
    && ["operator_requested", "apply_failed_recovery"].includes(input.reasonCode)) {
    return Object.freeze({
      lkgGeneration: desired.lkg_generation,
      reasonCode: input.reasonCode,
    });
  }
  return null;
}

function fixedError(error) {
  const candidate = String(error?.code ?? "");
  const code = Object.hasOwn(ERROR_MESSAGES, candidate) ? candidate : "edge.unavailable";
  return Object.freeze({ code, message: ERROR_MESSAGES[code], requestId: safeChain(error?.requestId) });
}

function safeChain(value) { const candidate = String(value ?? ""); return CHAIN_ID.test(candidate) ? candidate : "-"; }
function closedDialog() { return Object.freeze({ open: false, state: "closed", error: null }); }
function closedStateDialog() {
  return Object.freeze({
    open: false,
    state: "closed",
    action: "",
    deploymentId: "",
    error: null,
  });
}

function closedSecretDialog() {
  return Object.freeze({
    open: false,
    state: "closed",
    action: "",
    deploymentId: "",
    referenceId: "",
    error: null,
  });
}

function closedAcceptanceDialog() {
  return Object.freeze({
    open: false,
    state: "closed",
    deploymentId: "",
    validationRunId: "",
    error: null,
  });
}

function closedDiagnosticDialog() {
  return Object.freeze({
    open: false,
    state: "closed",
    deploymentId: "",
    grantId: "",
    action: "",
    ttlSeconds: 300,
    error: null,
  });
}

function normalizePermissionCodes(value) {
  if (!Array.isArray(value)) return null;
  const normalized = value.map(String).sort();
  return normalized.length === new Set(normalized).size ? normalized : null;
}

function mirroredTenantId(authContext) {
  const userTenant = String(authContext?.user?.tenant_id ?? "");
  const tenant = String(authContext?.tenant?.tenant_id ?? "");
  return userTenant && userTenant === tenant && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u.test(userTenant)
    ? userTenant : "";
}

function mirroredAccountId(authContext) {
  const accountId = String(authContext?.user?.user_id ?? "");
  return OWNER_ID.test(accountId) ? accountId : "";
}
