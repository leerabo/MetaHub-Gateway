/** PAGE-116 authorization owner and in-memory list/create/test lifecycle. */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";
import { createLocalEndpointRequestId } from "../../lib/localEndpoints.js";

const PAGE_ID = "local-models";
const LOCAL_ENDPOINT_ID = /^lme_[0-9a-f]{32}$/;
const ENGINES = new Set(["", "vllm", "ollama", "openai_compatible"]);
const STATUSES = new Set(["", "active", "disabled"]);
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;

const ERROR_MESSAGES = Object.freeze({
  "auth.permission_denied": "当前账号没有所需的本地模型权限",
  "local_endpoint.not_found": "当前租户中找不到该本地 Endpoint",
  "local_endpoint.conflict": "本地 Endpoint 状态已变化",
  "local_endpoint.idempotency_conflict": "该 request_id 已用于其他创建意图",
  "provider_health.probe_in_progress": "该 Endpoint 正在测试",
  "provider_health.probe_superseded": "Endpoint 配置已变化，请重新测试",
  "local_endpoint.request_invalid": "本地 Endpoint 请求不合法",
  "local_endpoint.engine_invalid": "本地推理引擎不受支持",
  "local_endpoint.url_invalid": "Endpoint URL 不符合安全要求",
  "local_endpoint.auth_invalid": "Endpoint 认证配置不合法",
  "local_endpoint.catalog_invalid": "Endpoint 模型目录响应不合法",
  "local_endpoint.unavailable": "本地 Endpoint 服务暂不可用",
  "local_endpoint.response_invalid": "本地 Endpoint 响应合同不完整",
});

/** The minimal read identity. A role label never supplies this permission. */
export function localEndpointReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.LOCAL_ENDPOINT_READ);
}

/** Build a complete PAGE-116 owner from auth/me and the exact backend navigation item. */
export function localEndpointPageOwner(authContext, consoleShellState) {
  const workspaceMode = consoleShellState?.workspaceNavigationContract === true
    && Boolean(consoleShellState.workspaceOwner?.workspaceId)
    && Array.isArray(consoleShellState.workspaceAuthorization?.permission_codes);
  const permissionContext = workspaceMode
    ? consoleShellState.workspaceAuthorization
    : authContext;
  const workspaceIdentity = workspaceMode
    ? JSON.stringify([
      "workspace",
      consoleShellState.workspaceOwner.workspaceId,
      consoleShellState.workspaceOwner.globalSessionId,
      consoleShellState.workspaceOwner.revokeGeneration,
      consoleShellState.workspaceOwner.permissionRevision,
      consoleShellState.workspaceOwner.membershipRevision,
      consoleShellState.workspaceOwner.permissionFingerprint,
    ])
    : "";
  const readIdentity = localEndpointReadIdentitySnapshot(authContext) || (
    workspaceMode && hasPermission(permissionContext, PERMISSIONS.LOCAL_ENDPOINT_READ)
      ? workspaceIdentity
      : ""
  );
  const manageIdentity = hasPermission(permissionContext, PERMISSIONS.LOCAL_ENDPOINT_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.LOCAL_ENDPOINT_MANAGE)
      || (workspaceMode ? workspaceIdentity : "")
    : "";
  const testIdentity = hasPermission(permissionContext, PERMISSIONS.LOCAL_ENDPOINT_TEST)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.LOCAL_ENDPOINT_TEST)
      || (workspaceMode ? workspaceIdentity : "")
    : "";
  const tenantId = mirroredTenantId(authContext)
    || (workspaceMode ? `workspace:${consoleShellState.workspaceOwner.workspaceId}` : "");
  // Workspace sessions keep the authoritative permission snapshot in the shell
  // authorization object; the legacy auth context has no top-level codes.
  const permissions = permissionCodesFromAuthContext(permissionContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === PAGE_ID)
    : null;
  const required = normalizePermissionCodes(item?.required_permissions);
  const navigationPermissions = normalizePermissionCodes(navigation?.permission_codes);
  const revision = workspaceMode
    ? Number(consoleShellState.workspaceAuthorization.permission_revision)
    : permissionRevision(authContext);
  const fingerprint = workspaceMode
    ? String(consoleShellState.workspaceAuthorization.permission_fingerprint ?? "")
    : authorizationFingerprint(authContext);
  if (
    !readIdentity
    || !tenantId
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== PAGE_ID
    || navigation?.active_navigation_item_id !== PAGE_ID
    || item?.target_section !== PAGE_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.LOCAL_ENDPOINT_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(permissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    page: PAGE_ID,
    tenantId,
    readIdentity,
    manageIdentity,
    testIdentity,
    navigationOwner: JSON.stringify([
      item.id,
      item.target_section,
      required,
      permissions,
      revision,
      fingerprint,
    ]),
    canManage: Boolean(manageIdentity),
    canTest: Boolean(testIdentity),
  });
}

/** Keep request bodies, request IDs, AbortControllers, URL and token outside public snapshots. */
export function createLocalEndpointPageState({
  clearDom = () => {},
  createRequestId = createLocalEndpointRequestId,
} = {}) {
  let ownerKey = "";
  let canManage = false;
  let canTest = false;
  let listState = "loading";
  let listError = null;
  let items = Object.freeze([]);
  let filters = Object.freeze({ engine: "", status: "" });
  let dialog = closedDialog();
  let feedback = null;
  let generations = { load: 0, create: 0, test: 0 };
  let operations = { load: null, create: null, test: null };

  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    canManage = owner.canManage === true;
    canTest = owner.canTest === true;
    if (!canManage && dialog.open) closeDialogInternal();
    if (!canTest && operations.test) {
      operations.test.controller.abort();
      operations.test = null;
      generations.test += 1;
    }
    return true;
  }

  /** Clear sensitive DOM before abort callbacks can observe an obsolete owner. */
  function clear() {
    const pending = Object.values(operations).filter(Boolean);
    ownerKey = "";
    canManage = false;
    canTest = false;
    listState = "loading";
    listError = null;
    items = Object.freeze([]);
    filters = Object.freeze({ engine: "", status: "" });
    dialog = closedDialog();
    feedback = null;
    operations = { load: null, create: null, test: null };
    generations = {
      load: generations.load + 1,
      create: generations.create + 1,
      test: generations.test + 1,
    };
    try {
      clearDom();
    } finally {
      for (const operation of pending) operation.controller.abort();
    }
  }

  function setFilters(owner, next) {
    if (!owns(owner) || operations.create || operations.test) return false;
    const engine = String(next?.engine ?? "");
    const status = String(next?.status ?? "");
    if (!ENGINES.has(engine) || !STATUSES.has(status)) return false;
    filters = Object.freeze({ engine, status });
    feedback = null;
    return true;
  }

  function beginLoad(owner, refreshGeneration) {
    if (!owns(owner) || !Number.isSafeInteger(refreshGeneration) || refreshGeneration < 1) return null;
    operations.load?.controller.abort();
    generations.load += 1;
    const operation = Object.freeze({
      lane: "load",
      ownerKey,
      generation: generations.load,
      refreshGeneration,
      controller: new AbortController(),
    });
    operations.load = operation;
    if (!items.length) listState = "loading";
    listError = null;
    return operation;
  }

  function isLoadCurrent(operation, owner, refreshGeneration) {
    return current("load", operation, owner)
      && operation.refreshGeneration === refreshGeneration;
  }

  function acceptLoad(operation, response, owner, refreshGeneration) {
    if (!isLoadCurrent(operation, owner, refreshGeneration) || !validList(response, owner.tenantId)) return false;
    items = Object.freeze([...response.local_endpoints]);
    listState = "ready";
    listError = null;
    return true;
  }

  function rejectLoad(operation, error, owner, refreshGeneration) {
    if (!isLoadCurrent(operation, owner, refreshGeneration)) return false;
    listState = "error";
    listError = fixedError(error, "local_endpoint.unavailable");
    return true;
  }

  function finishLoad(operation) {
    if (operations.load === operation) operations.load = null;
  }

  function openCreate(owner) {
    if (!owns(owner) || !canManage || isMutationBusy()) return false;
    dialog = Object.freeze({ open: true, state: "idle", error: null });
    feedback = null;
    return true;
  }

  function closeCreate(owner) {
    if (!owns(owner) || operations.create) return false;
    closeDialogInternal();
    return true;
  }

  function beginCreate(owner) {
    if (!owns(owner) || !canManage || !dialog.open || operations.create || operations.test) return null;
    generations.create += 1;
    const operation = Object.freeze({
      lane: "create",
      ownerKey,
      generation: generations.create,
      controller: new AbortController(),
      requestId: createRequestId(),
    });
    operations.create = operation;
    dialog = Object.freeze({ open: true, state: "submitting", error: null });
    feedback = null;
    return operation;
  }

  function acceptCreate(operation, response, owner) {
    if (!current("create", operation, owner) || !validCreate(response, owner.tenantId)) return false;
    const endpoint = response.local_endpoint;
    items = Object.freeze([
      endpoint,
      ...items.filter((item) => item.local_endpoint_id !== endpoint.local_endpoint_id),
    ]);
    listState = "ready";
    listError = null;
    dialog = closedDialog();
    feedback = Object.freeze({
      kind: "success",
      code: response.created ? "local_endpoint.created" : "local_endpoint.replayed",
      requestId: safeChain(response.request_id),
      localEndpointId: endpoint.local_endpoint_id,
    });
    return true;
  }

  function rejectCreate(operation, error, owner) {
    if (!current("create", operation, owner)) return false;
    dialog = Object.freeze({ open: true, state: "error", error: fixedError(error, "local_endpoint.unavailable") });
    return true;
  }

  function finishCreate(operation) {
    if (operations.create === operation) operations.create = null;
  }

  function beginTest(owner, localEndpointId) {
    const endpoint = items.find((item) => item.local_endpoint_id === localEndpointId);
    if (!owns(owner) || !canTest || !endpoint || operations.create || operations.test || dialog.open) return null;
    generations.test += 1;
    const operation = Object.freeze({
      lane: "test",
      ownerKey,
      generation: generations.test,
      controller: new AbortController(),
      localEndpointId,
    });
    operations.test = operation;
    feedback = null;
    return operation;
  }

  function acceptTest(operation, response, owner) {
    if (
      !current("test", operation, owner)
      || !validTest(response, owner.tenantId, operation.localEndpointId)
    ) return false;
    items = Object.freeze(items.map((item) => (
      item.local_endpoint_id === operation.localEndpointId ? response.local_endpoint : item
    )));
    feedback = Object.freeze({
      kind: response.test_result.status === "success" ? "success" : "error",
      code: response.test_result.error_code ?? "local_endpoint.test_succeeded",
      requestId: safeChain(response.request_id),
      localEndpointId: operation.localEndpointId,
    });
    return true;
  }

  function rejectTest(operation, error, owner) {
    if (!current("test", operation, owner)) return false;
    const fixed = fixedError(error, "local_endpoint.unavailable");
    feedback = Object.freeze({
      kind: "error",
      code: fixed.code,
      requestId: fixed.requestId,
      localEndpointId: operation.localEndpointId,
    });
    return true;
  }

  function finishTest(operation) {
    if (operations.test === operation) operations.test = null;
  }

  function blocksShellDomReplacement() {
    return dialog.open || Boolean(operations.create || operations.test);
  }

  function snapshot() {
    const visibleItems = items.filter((item) => (
      (!filters.engine || item.engine_kind === filters.engine)
      && (!filters.status || item.status === filters.status)
    ));
    return Object.freeze({
      state: listState,
      error: listError,
      items: Object.freeze(visibleItems),
      totalCount: items.length,
      filters,
      dialog,
      feedback,
      canManage,
      canTest,
      testingId: operations.test?.localEndpointId ?? "",
      busy: isMutationBusy(),
    });
  }

  function owns(owner) {
    const key = ownerKeyFor(owner);
    return Boolean(key && key === ownerKey);
  }

  function current(lane, operation, owner) {
    return Boolean(
      operation
      && operation === operations[lane]
      && operation.ownerKey === ownerKey
      && operation.ownerKey === ownerKeyFor(owner)
      && operation.generation === generations[lane]
      && !operation.controller.signal.aborted,
    );
  }

  function isMutationBusy() {
    return Boolean(operations.create || operations.test);
  }

  function closeDialogInternal() {
    dialog = closedDialog();
  }

  return Object.freeze({
    enter,
    clear,
    setFilters,
    beginLoad,
    isLoadCurrent,
    acceptLoad,
    rejectLoad,
    finishLoad,
    openCreate,
    closeCreate,
    beginCreate,
    acceptCreate,
    rejectCreate,
    finishCreate,
    beginTest,
    acceptTest,
    rejectTest,
    finishTest,
    blocksShellDomReplacement,
    snapshot,
  });
}

export function isLocalEndpointAbortError(error) {
  return error?.name === "AbortError";
}

function ownerKeyFor(owner) {
  if (
    !owner
    || owner.page !== PAGE_ID
    || !owner.tenantId
    || !owner.readIdentity
    || !owner.navigationOwner
  ) return "";
  return JSON.stringify([
    owner.page,
    owner.tenantId,
    owner.readIdentity,
    owner.manageIdentity,
    owner.testIdentity,
    owner.navigationOwner,
  ]);
}

function validList(response, tenantId) {
  return Boolean(response && Array.isArray(response.local_endpoints)
    && response.local_endpoints.every((item) => item.tenant_id === tenantId));
}

function validCreate(response, tenantId) {
  return Boolean(response && typeof response.created === "boolean"
    && response.local_endpoint?.tenant_id === tenantId);
}

function validTest(response, tenantId, localEndpointId) {
  return Boolean(response
    && response.local_endpoint?.tenant_id === tenantId
    && response.local_endpoint?.local_endpoint_id === localEndpointId
    && response.test_result);
}

function fixedError(error, fallback) {
  const candidate = String(error?.code ?? "");
  const code = Object.hasOwn(ERROR_MESSAGES, candidate) ? candidate : fallback;
  return Object.freeze({
    code,
    message: ERROR_MESSAGES[code] ?? ERROR_MESSAGES["local_endpoint.unavailable"],
    requestId: safeChain(error?.requestId),
  });
}

function safeChain(value) {
  const candidate = String(value ?? "");
  return CHAIN_ID.test(candidate) ? candidate : "-";
}

function closedDialog() {
  return Object.freeze({ open: false, state: "idle", error: null });
}

function normalizePermissionCodes(value) {
  if (!Array.isArray(value)) return null;
  const normalized = value.map(String).sort();
  return normalized.length === new Set(normalized).size ? normalized : null;
}

function mirroredTenantId(authContext) {
  const userTenant = String(authContext?.user?.tenant_id ?? "");
  const tenant = String(authContext?.tenant?.tenant_id ?? "");
  return userTenant && userTenant === tenant && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(userTenant)
    ? userTenant
    : "";
}
