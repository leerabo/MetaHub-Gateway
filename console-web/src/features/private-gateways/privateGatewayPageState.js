/** PRD-120/121 owner-bound identity, desired-state, and one-use registration lifecycle. */

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
});

export function privateGatewayReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_DEPLOYMENT_READ);
}

export function privateGatewayPageOwner(authContext, consoleShellState) {
  const readIdentity = privateGatewayReadIdentitySnapshot(authContext);
  const manageIdentity = hasPermission(authContext, PERMISSIONS.EDGE_DEPLOYMENT_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.EDGE_DEPLOYMENT_MANAGE)
    : "";
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
    navigationOwner: JSON.stringify([
      item.id, item.target_section, required, permissions, revision, fingerprint,
    ]),
    canManage: Boolean(manageIdentity),
  });
}

export function createPrivateGatewayPageState({ clearDom = () => {} } = {}) {
  let ownerKey = "";
  let canManage = false;
  let listState = "loading";
  let listError = null;
  let deployments = Object.freeze([]);
  let desiredStates = Object.freeze({});
  let workspaceId = "";
  let dialog = closedDialog();
  let stateDialog = closedStateDialog();
  let registration = null;
  let feedback = null;
  let generations = { load: 0, create: 0, revoke: 0, copy: 0, command: 0 };
  let operations = { load: null, create: null, revoke: null, copy: null, command: null };

  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    canManage = owner.canManage === true;
    if (!canManage && dialog.open) closeDialogInternal();
    return true;
  }

  function clear() {
    const pending = Object.values(operations).filter(Boolean);
    ownerKey = "";
    canManage = false;
    listState = "loading";
    listError = null;
    deployments = Object.freeze([]);
    desiredStates = Object.freeze({});
    workspaceId = "";
    dialog = closedDialog();
    stateDialog = closedStateDialog();
    registration = null;
    feedback = null;
    operations = { load: null, create: null, revoke: null, copy: null, command: null };
    generations = Object.fromEntries(
      Object.entries(generations).map(([lane, value]) => [lane, value + 1]),
    );
    try { clearDom(); } finally {
      for (const operation of pending) operation.controller?.abort?.();
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
    return operation;
  }

  function isLoadCurrent(operation, owner, refreshGeneration) {
    return current("load", operation, owner) && operation.refreshGeneration === refreshGeneration;
  }

  function acceptLoad(operation, response, owner, refreshGeneration, stateResponses = []) {
    if (!isLoadCurrent(operation, owner, refreshGeneration)
      || !validList(response)
      || !validDesiredResponses(stateResponses, response.deployments)) return false;
    const ids = new Set(response.deployments.map((item) => item.workspace_id));
    if (ids.size > 1) return false;
    deployments = Object.freeze([...response.deployments]);
    desiredStates = desiredStateMap(response.deployments, stateResponses);
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

  function blocksShellDomReplacement() {
    return dialog.open || stateDialog.open
      || Boolean(operations.create || operations.copy || operations.revoke || operations.command);
  }

  function snapshot() {
    return Object.freeze({
      state: listState,
      error: listError,
      deployments,
      desiredStates,
      dialog,
      stateDialog,
      registration,
      feedback,
      canManage,
      revokingId: operations.revoke?.deploymentId ?? "",
      commandDeploymentId: operations.command?.deploymentId ?? "",
      busy: busy(),
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
    return Boolean(operations.create || operations.revoke || operations.copy || operations.command);
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
    blocksShellDomReplacement, snapshot,
  });
}

export function isPrivateGatewayAbortError(error) { return error?.name === "AbortError"; }

function ownerKeyFor(owner) {
  if (!owner || owner.page !== PAGE_ID || !owner.tenantId || !owner.readIdentity || !owner.navigationOwner) return "";
  return JSON.stringify([
    owner.page, owner.tenantId, owner.readIdentity, owner.manageIdentity, owner.navigationOwner,
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
