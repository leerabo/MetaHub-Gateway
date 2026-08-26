/** PRD-120..123 owner-bound Edge identity, telemetry, and local Secret lifecycle. */

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
const OWNER_ID = /^[a-z0-9][a-z0-9_:-]{2,79}$/u;
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
  let workspaceId = "";
  let dialog = closedDialog();
  let stateDialog = closedStateDialog();
  let registration = null;
  let secretDialog = closedSecretDialog();
  let localSession = null;
  let feedback = null;
  let canReadTelemetry = false;
  let canManageTelemetry = false;
  let canReadUsage = false;
  let canReadSecrets = false;
  let canManageSecrets = false;
  let generations = {
    load: 0, create: 0, revoke: 0, copy: 0, command: 0, policy: 0,
    localSession: 0, secret: 0,
  };
  let operations = {
    load: null, create: null, revoke: null, copy: null, command: null, policy: null,
    localSession: null, secret: null,
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
    if (!canManage && dialog.open) closeDialogInternal();
    if (!canManageSecrets) {
      secretDialog = closedSecretDialog();
      localSession = null;
    }
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
    workspaceId = "";
    dialog = closedDialog();
    stateDialog = closedStateDialog();
    registration = null;
    secretDialog = closedSecretDialog();
    localSession = null;
    feedback = null;
    canReadTelemetry = false;
    canManageTelemetry = false;
    canReadUsage = false;
    canReadSecrets = false;
    canManageSecrets = false;
    operations = {
      load: null, create: null, revoke: null, copy: null, command: null, policy: null,
      localSession: null, secret: null,
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
  ) {
    if (!isLoadCurrent(operation, owner, refreshGeneration)
      || !validList(response)
      || !validDesiredResponses(stateResponses, response.deployments)
      || !validTelemetryResponses(
        telemetryResponses,
        response.deployments,
        canReadTelemetry || canReadUsage,
      )
      || !validSecretResponse(secretResponse, response.deployments, canReadSecrets, owner)) {
      return false;
    }
    const ids = new Set(response.deployments.map((item) => item.workspace_id));
    if (ids.size > 1) return false;
    deployments = Object.freeze([...response.deployments]);
    desiredStates = desiredStateMap(response.deployments, stateResponses);
    telemetryStates = telemetryStateMap(response.deployments, telemetryResponses);
    secretReferences = Object.freeze([...(secretResponse?.references ?? [])]);
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

  function openCreate(owner) {
    if (!owns(owner) || !canManage || busy()) return false;
    clearRegistration();
    stateDialog = closedStateDialog();
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
    return dialog.open || stateDialog.open || secretDialog.open
      || Boolean(operations.create || operations.copy || operations.revoke
        || operations.command || operations.policy || operations.localSession
        || operations.secret);
  }

  function snapshot() {
    return Object.freeze({
      state: listState,
      error: listError,
      deployments,
      desiredStates,
      telemetryStates,
      secretReferences,
      dialog,
      stateDialog,
      registration,
      secretDialog,
      localSession,
      feedback,
      canManage,
      canReadTelemetry,
      canManageTelemetry,
      canReadUsage,
      canReadSecrets,
      canManageSecrets,
      revokingId: operations.revoke?.deploymentId ?? "",
      commandDeploymentId: operations.command?.deploymentId ?? "",
      busy: busy(),
      secretBusy: Boolean(operations.localSession || operations.secret),
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
      || operations.secret);
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
    openCreate, closeCreate, beginCreate, acceptCreate, rejectCreate, finishCreate,
    beginCopy, acceptCopy, rejectCopy, beginRevoke, acceptRevoke, rejectRevoke,
    finishRevoke, openStateCommand, closeStateCommand, beginStateCommand,
    acceptStateCommand, rejectStateCommand, finishStateCommand,
    beginTelemetryPolicy, acceptTelemetryPolicy, rejectTelemetryPolicy,
    finishTelemetryPolicy,
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
