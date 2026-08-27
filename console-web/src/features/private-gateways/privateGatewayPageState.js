/** PRD-120..126 owner-bound Edge identity, health, diagnostics, and lifecycle state. */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

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
  const readIdentity = privateGatewayReadIdentitySnapshot(authContext);
  const manageIdentity = hasPermission(authContext, PERMISSIONS.EDGE_DEPLOYMENT_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_DEPLOYMENT_MANAGE)
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
    accountId,
    role,
    navigationOwner: JSON.stringify([
      item.id, item.target_section, required, permissions, revision, fingerprint,
    ]),
    canManage: Boolean(manageIdentity),
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
  let generations = {
    load: 0, create: 0, revoke: 0, copy: 0, command: 0, policy: 0,
    localSession: 0, secret: 0, validation: 0, acceptance: 0, supportBundle: 0,
    diagnostic: 0,
  };
  let operations = {
    load: null, create: null, revoke: null, copy: null, command: null, policy: null,
    localSession: null, secret: null, validation: null, acceptance: null,
    supportBundle: null, diagnostic: null,
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
    operations = {
      load: null, create: null, revoke: null, copy: null, command: null, policy: null,
      localSession: null, secret: null, validation: null, acceptance: null,
      supportBundle: null, diagnostic: null,
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
        || response.tenant_id !== owner.tenantId) return false;
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
        || operations.supportBundle || operations.diagnostic);
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
      revokingId: operations.revoke?.deploymentId ?? "",
      commandDeploymentId: operations.command?.deploymentId ?? "",
      busy: busy(),
      secretBusy: Boolean(operations.localSession || operations.secret),
      validationBusy: Boolean(operations.validation || operations.acceptance),
      supportBundleBusy: Boolean(operations.supportBundle),
      diagnosticBusy: Boolean(operations.diagnostic),
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
      || operations.supportBundle || operations.diagnostic);
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
  if (!owner || owner.page !== PAGE_ID || !owner.tenantId || !owner.readIdentity || !owner.navigationOwner) return "";
  return JSON.stringify([
    owner.page, owner.tenantId, owner.readIdentity, owner.manageIdentity,
    owner.telemetryReadIdentity, owner.telemetryManageIdentity, owner.usageReadIdentity,
    owner.secretReadIdentity, owner.accountId, owner.role,
    owner.installerReadIdentity, owner.installationManageIdentity,
    owner.validationReadIdentity, owner.validationManageIdentity,
    owner.deliveryAcceptIdentity, owner.supportBundleReadIdentity,
    owner.healthReadIdentity, owner.diagnosticReadIdentity,
    owner.diagnosticApproveIdentity,
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
      || reference.tenant_id !== owner.tenantId
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
