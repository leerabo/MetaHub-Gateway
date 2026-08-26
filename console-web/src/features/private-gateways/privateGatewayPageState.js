/** PRD-120 exact authorization owner and memory-only registration lifecycle. */

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
  "edge.not_found": "当前 Workspace 中找不到该 Deployment",
  "edge.conflict": "Deployment 状态已变化，请刷新后重试",
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
  let workspaceId = "";
  let dialog = closedDialog();
  let registration = null;
  let feedback = null;
  let generations = { load: 0, create: 0, revoke: 0, copy: 0 };
  let operations = { load: null, create: null, revoke: null, copy: null };

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
    workspaceId = "";
    dialog = closedDialog();
    registration = null;
    feedback = null;
    operations = { load: null, create: null, revoke: null, copy: null };
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
    return operation;
  }

  function isLoadCurrent(operation, owner, refreshGeneration) {
    return current("load", operation, owner) && operation.refreshGeneration === refreshGeneration;
  }

  function acceptLoad(operation, response, owner, refreshGeneration) {
    if (!isLoadCurrent(operation, owner, refreshGeneration) || !validList(response)) return false;
    const ids = new Set(response.deployments.map((item) => item.workspace_id));
    if (ids.size > 1) return false;
    deployments = Object.freeze([...response.deployments]);
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

  function blocksShellDomReplacement() {
    return dialog.open || Boolean(operations.create || operations.copy || operations.revoke);
  }

  function snapshot() {
    return Object.freeze({
      state: listState,
      error: listError,
      deployments,
      dialog,
      registration,
      feedback,
      canManage,
      revokingId: operations.revoke?.deploymentId ?? "",
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

  function busy() { return Boolean(operations.create || operations.revoke || operations.copy); }

  function clearRegistration() { registration = null; }

  function closeDialogInternal() {
    clearRegistration();
    dialog = closedDialog();
  }

  return Object.freeze({
    enter, clear, beginLoad, isLoadCurrent, acceptLoad, rejectLoad, finishLoad,
    openCreate, closeCreate, beginCreate, acceptCreate, rejectCreate, finishCreate,
    beginCopy, acceptCopy, rejectCopy, beginRevoke, acceptRevoke, rejectRevoke,
    finishRevoke, blocksShellDomReplacement, snapshot,
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

function fixedError(error) {
  const candidate = String(error?.code ?? "");
  const code = Object.hasOwn(ERROR_MESSAGES, candidate) ? candidate : "edge.unavailable";
  return Object.freeze({ code, message: ERROR_MESSAGES[code], requestId: safeChain(error?.requestId) });
}

function safeChain(value) { const candidate = String(value ?? ""); return CHAIN_ID.test(candidate) ? candidate : "-"; }
function closedDialog() { return Object.freeze({ open: false, state: "closed", error: null }); }

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
