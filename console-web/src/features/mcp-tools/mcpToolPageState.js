/** PAGE-114 owner-bound tab, list and Dialog lifecycle. */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";
import {
  createMcpIdempotencyKey,
  normalizeMcpNetworkPolicy,
} from "../../lib/mcpTools.js";

const PAGE_ID = "mcp-tools";
const TABS = Object.freeze(["servers", "tools", "invocations", "network"]);
const SERVER_ID = /^mcps_[0-9a-f]{32}$/;
const NETWORK_POLICY_ID = /^mcpnp_[0-9a-f]{32}$/;
const TOOL_ID = /^mcpt_[0-9a-f]{32}$/;
const INVOCATION_ID = /^mcpi_[0-9a-f]{32}$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const HASH = /^[0-9a-f]{64}$/;

/** Build the page owner from mirrored auth and backend navigation facts. */
export function mcpToolPageOwner(authContext, consoleShellState) {
  const readIdentity = authorizationIdentitySnapshot(authContext, PERMISSIONS.MCP_READ);
  const tenantId = mirroredTenantId(authContext);
  const permissions = permissionCodesFromAuthContext(authContext);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === PAGE_ID)
    : null;
  const required = normalizePermissionCodes(item?.required_permissions);
  const navigationPermissions = normalizePermissionCodes(navigation?.permission_codes);
  if (
    !readIdentity
    || !tenantId
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== PAGE_ID
    || navigation?.active_navigation_item_id !== PAGE_ID
    || item?.target_section !== PAGE_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.MCP_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(permissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    page: PAGE_ID,
    tenantId,
    readIdentity,
    navigationOwner: JSON.stringify([
      item.id,
      item.target_section,
      required,
      permissions,
      revision,
      fingerprint,
    ]),
    canManageServers: hasPermission(authContext, PERMISSIONS.MCP_SERVER_MANAGE),
    canManagePolicies: hasPermission(authContext, PERMISSIONS.MCP_POLICY_MANAGE),
    canManageNetwork: hasPermission(authContext, PERMISSIONS.MCP_NETWORK_MANAGE),
    canInvoke: hasPermission(authContext, PERMISSIONS.MCP_INVOKE),
  });
}

/** Identity used before replacing an authenticated page owner. */
export function mcpToolReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.MCP_READ);
}

/** Keep all cursors, operation owners and registration credentials outside public snapshots. */
export function createMcpToolPageState({
  clearDom = () => {},
  createIdempotencyKey = createMcpIdempotencyKey,
} = {}) {
  let ownerKey = "";
  let capabilities = emptyCapabilities();
  let activeTab = "servers";
  let tabData = emptyTabData();
  let pageCursors = { servers: null, tools: null, invocations: null, network: null };
  let nextCursors = { servers: null, tools: null, invocations: null, network: null };
  let cursorStacks = { servers: [], tools: [], invocations: [], network: [] };
  let generations = emptyGenerations();
  let operations = emptyOperations();
  let registerDialog = closedRegisterDialog();
  let toolDialog = closedToolDialog();
  let networkDialog = closedNetworkDialog();
  let feedback = null;

  function enter(owner) {
    const key = ownerKeyFor(owner);
    if (!key) {
      clear();
      return false;
    }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    capabilities = Object.freeze({
      canManageServers: owner.canManageServers === true,
      canManagePolicies: owner.canManagePolicies === true,
      canRefreshTools: owner.canManageServers === true && owner.canManagePolicies === true,
      canManageNetwork: owner.canManageNetwork === true,
      canInvoke: owner.canInvoke === true,
    });
    return true;
  }

  function clear() {
    clearDomSafely();
    for (const operation of Object.values(operations)) operation?.controller?.abort();
    ownerKey = "";
    capabilities = emptyCapabilities();
    activeTab = "servers";
    tabData = emptyTabData();
    pageCursors = { servers: null, tools: null, invocations: null, network: null };
    nextCursors = { servers: null, tools: null, invocations: null, network: null };
    cursorStacks = { servers: [], tools: [], invocations: [], network: [] };
    generations = incrementAll(generations);
    operations = emptyOperations();
    registerDialog = closedRegisterDialog();
    toolDialog = closedToolDialog();
    networkDialog = closedNetworkDialog();
    feedback = null;
  }

  function switchTab(owner, tab) {
    if (!owns(owner) || !TABS.includes(tab) || isBusy()) return false;
    if (activeTab === tab) return true;
    closeToolDialog(owner);
    closeNetworkDialogInternal();
    activeTab = tab;
    feedback = null;
    if (!tabData[tab].items.length) {
      tabData = replaceTab(tabData, tab, { state: "loading", items: [], error: null });
    }
    return true;
  }

  function beginList(owner, tab, refreshGeneration) {
    if (
      !owns(owner)
      || !TABS.includes(tab)
      || tab !== activeTab
      || !Number.isSafeInteger(refreshGeneration)
      || refreshGeneration < 1
    ) return null;
    operations[tab]?.controller?.abort();
    generations[tab] += 1;
    const ticket = Object.freeze({
      ownerKey,
      lane: tab,
      generation: generations[tab],
      refreshGeneration,
      controller: new AbortController(),
    });
    operations[tab] = ticket;
    tabData = replaceTab(tabData, tab, { ...tabData[tab], state: "loading", error: null });
    return ticket;
  }

  function isListCurrent(ticket, owner, refreshGeneration) {
    return currentTicket(ticket, owner, ticket?.lane, refreshGeneration)
      && TABS.includes(ticket.lane)
      && ticket.lane === activeTab;
  }

  function acceptList(ticket, payload, owner, refreshGeneration) {
    if (!isListCurrent(ticket, owner, refreshGeneration)) return false;
    const normalized = normalizeListPayload(ticket.lane, payload);
    if (!normalized) return rejectList(ticket, fixedError(null, "mcp.response_invalid"), owner, refreshGeneration);
    tabData = replaceTab(tabData, ticket.lane, {
      state: "ready",
      items: normalized.items,
      error: null,
      requestId: normalized.requestId,
    });
    if (ticket.lane === "network") refreshNetworkConflict(normalized.items);
    nextCursors[ticket.lane] = normalized.nextCursor;
    return true;
  }

  function rejectList(ticket, error, owner, refreshGeneration) {
    if (!isListCurrent(ticket, owner, refreshGeneration)) return false;
    tabData = replaceTab(tabData, ticket.lane, {
      ...tabData[ticket.lane],
      state: "error",
      error: fixedError(error, "mcp.unavailable"),
    });
    return true;
  }

  function finishList(ticket) {
    if (ticket?.lane && operations[ticket.lane] === ticket) operations[ticket.lane] = null;
  }

  function listQuery(tab) {
    return TABS.includes(tab) ? Object.freeze({ cursor: pageCursors[tab], limit: 50 }) : null;
  }

  function nextPage(owner) {
    const tab = activeTab;
    if (!owns(owner) || isBusy() || !nextCursors[tab]) return false;
    cursorStacks[tab] = [...cursorStacks[tab], pageCursors[tab]];
    pageCursors[tab] = nextCursors[tab];
    nextCursors[tab] = null;
    tabData = replaceTab(tabData, tab, { state: "loading", items: [], error: null });
    return true;
  }

  function previousPage(owner) {
    const tab = activeTab;
    if (!owns(owner) || isBusy() || cursorStacks[tab].length === 0) return false;
    pageCursors[tab] = cursorStacks[tab].at(-1) ?? null;
    cursorStacks[tab] = cursorStacks[tab].slice(0, -1);
    nextCursors[tab] = null;
    tabData = replaceTab(tabData, tab, { state: "loading", items: [], error: null });
    return true;
  }

  function openRegister(owner) {
    if (!owns(owner) || !capabilities.canManageServers || isBusy()) return false;
    registerDialog = Object.freeze({ open: true, state: "idle", error: null });
    feedback = null;
    return true;
  }

  function closeRegister(owner) {
    if (!owns(owner) || operations.register) return false;
    registerDialog = closedRegisterDialog();
    return true;
  }

  function beginRegister(owner) {
    if (!owns(owner) || !capabilities.canManageServers || !registerDialog.open || operations.register) {
      return null;
    }
    generations.register += 1;
    const ticket = Object.freeze({
      ownerKey,
      lane: "register",
      generation: generations.register,
      controller: new AbortController(),
      idempotencyKey: createIdempotencyKey("register"),
    });
    operations.register = ticket;
    registerDialog = Object.freeze({ open: true, state: "submitting", error: null });
    return ticket;
  }

  function acceptRegister(ticket, payload, owner) {
    if (!currentTicket(ticket, owner, "register")) return false;
    const server = normalizeServer(payload?.server ?? payload);
    if (!server) return rejectRegister(ticket, fixedError(null, "mcp.response_invalid"), owner);
    registerDialog = closedRegisterDialog();
    feedback = Object.freeze({
      kind: "success",
      code: "mcp.server_registered",
      requestId: safeChain(payload?.request_id),
      objectId: server.mcp_server_id,
    });
    const servers = tabData.servers.items.filter(
      (item) => item.mcp_server_id !== server.mcp_server_id,
    );
    tabData = replaceTab(tabData, "servers", {
      state: "ready",
      items: Object.freeze([server, ...servers]),
      error: null,
      requestId: safeChain(payload?.request_id),
    });
    pageCursors.servers = null;
    nextCursors.servers = null;
    cursorStacks.servers = [];
    return true;
  }

  function rejectRegister(ticket, error, owner) {
    if (!currentTicket(ticket, owner, "register")) return false;
    registerDialog = Object.freeze({
      open: true,
      state: "error",
      error: fixedError(error, "mcp.unavailable"),
    });
    return true;
  }

  function finishRegister(ticket) {
    if (operations.register === ticket) operations.register = null;
  }

  function beginServerRefresh(owner, serverId) {
    const server = tabData.servers.items.find((item) => item.mcp_server_id === serverId);
    if (
      !owns(owner)
      || !capabilities.canRefreshTools
      || !server
      || operations.serverAction
      || isDialogOpen()
    ) return null;
    generations.serverAction += 1;
    const ticket = Object.freeze({
      ownerKey,
      lane: "serverAction",
      generation: generations.serverAction,
      controller: new AbortController(),
      action: "refresh",
      serverId,
      expectedRevision: server.state_revision,
      idempotencyKey: createIdempotencyKey("refresh"),
    });
    operations.serverAction = ticket;
    feedback = null;
    return ticket;
  }

  function acceptServerRefresh(ticket, payload, owner) {
    if (!currentTicket(ticket, owner, "serverAction") || ticket.action !== "refresh") return false;
    const server = normalizeServer(payload?.server);
    if (!server || server.mcp_server_id !== ticket.serverId) {
      return rejectServerRefresh(ticket, fixedError(null, "mcp.response_invalid"), owner);
    }
    feedback = Object.freeze({
      kind: "success",
      code: "mcp.refresh_enqueued",
      requestId: safeChain(payload?.request_id),
      objectId: ticket.serverId,
    });
    tabData = replaceTab(tabData, "servers", {
      ...tabData.servers,
      items: Object.freeze(tabData.servers.items.map((item) => (
        item.mcp_server_id === server.mcp_server_id ? server : item
      ))),
      requestId: safeChain(payload?.request_id),
    });
    return true;
  }

  function rejectServerRefresh(ticket, error, owner) {
    if (!currentTicket(ticket, owner, "serverAction") || ticket.action !== "refresh") return false;
    feedback = Object.freeze({
      kind: "error",
      ...fixedError(error, "mcp.unavailable"),
      objectId: ticket.serverId,
    });
    return true;
  }

  function finishServerRefresh(ticket) {
    if (operations.serverAction === ticket) operations.serverAction = null;
  }

  function beginServerStatus(owner, serverId) {
    const server = tabData.servers.items.find((item) => item.mcp_server_id === serverId);
    if (
      !owns(owner)
      || !capabilities.canManageServers
      || !server
      || operations.serverAction
      || isDialogOpen()
    ) return null;
    const nextStatus = server.status === "disabled" ? "active" : "disabled";
    generations.serverAction += 1;
    const ticket = Object.freeze({
      ownerKey,
      lane: "serverAction",
      generation: generations.serverAction,
      controller: new AbortController(),
      action: "status",
      serverId,
      expectedRevision: server.state_revision,
      nextStatus,
      idempotencyKey: createIdempotencyKey(nextStatus === "active" ? "enable" : "disable"),
    });
    operations.serverAction = ticket;
    feedback = null;
    return ticket;
  }

  function acceptServerStatus(ticket, payload, owner) {
    if (!currentTicket(ticket, owner, "serverAction") || ticket.action !== "status") return false;
    const server = normalizeServer(payload?.server);
    if (
      !server
      || server.mcp_server_id !== ticket.serverId
      || server.status !== ticket.nextStatus
    ) return rejectServerStatus(ticket, fixedError(null, "mcp.response_invalid"), owner);
    feedback = Object.freeze({
      kind: "success",
      code: ticket.nextStatus === "active" ? "mcp.server_enabled" : "mcp.server_disabled",
      requestId: safeChain(payload?.request_id),
      objectId: ticket.serverId,
    });
    tabData = replaceTab(tabData, "servers", {
      ...tabData.servers,
      items: Object.freeze(tabData.servers.items.map((item) => (
        item.mcp_server_id === server.mcp_server_id ? server : item
      ))),
      requestId: safeChain(payload?.request_id),
    });
    return true;
  }

  function rejectServerStatus(ticket, error, owner) {
    if (!currentTicket(ticket, owner, "serverAction") || ticket.action !== "status") return false;
    feedback = Object.freeze({
      kind: "error",
      ...fixedError(error, "mcp.unavailable"),
      objectId: ticket.serverId,
    });
    return true;
  }

  function finishServerStatus(ticket) {
    if (operations.serverAction === ticket) operations.serverAction = null;
  }

  function openToolDialog(owner, toolId, kind) {
    const tool = tabData.tools.items.find((item) => item.mcp_tool_id === toolId);
    if (
      !owns(owner)
      || !tool
      || !["detail", "policy"].includes(kind)
      || (kind === "policy" && !capabilities.canManagePolicies)
      || isBusy()
    ) return false;
    closeToolDialogInternal();
    toolDialog = Object.freeze({ open: true, kind, toolId, state: "loading", error: null, tool: null });
    return true;
  }

  function beginToolDetail(owner) {
    const lane = toolDialog.kind;
    if (!owns(owner) || !toolDialog.open || !["detail", "policy"].includes(lane) || operations[lane]) {
      return null;
    }
    generations[lane] += 1;
    const ticket = Object.freeze({
      ownerKey,
      lane,
      generation: generations[lane],
      controller: new AbortController(),
      toolId: toolDialog.toolId,
    });
    operations[lane] = ticket;
    return ticket;
  }

  function acceptToolDetail(ticket, payload, owner) {
    if (!currentToolTicket(ticket, owner)) return false;
    const tool = normalizeToolDetail(payload?.tool ?? payload);
    if (!tool || tool.mcp_tool_id !== ticket.toolId) {
      return rejectToolDetail(ticket, fixedError(null, "mcp.response_invalid"), owner);
    }
    toolDialog = Object.freeze({ ...toolDialog, state: "ready", error: null, tool });
    return true;
  }

  function rejectToolDetail(ticket, error, owner) {
    if (!currentToolTicket(ticket, owner)) return false;
    toolDialog = Object.freeze({
      ...toolDialog,
      state: "error",
      error: fixedError(error, "mcp.unavailable"),
      tool: null,
    });
    return true;
  }

  function finishToolDetail(ticket) {
    if (ticket?.lane && operations[ticket.lane] === ticket) operations[ticket.lane] = null;
  }

  function closeToolDialog(owner) {
    if (!owns(owner) || operations.policyUpdate) return false;
    closeToolDialogInternal();
    return true;
  }

  function beginPolicyUpdate(owner) {
    if (
      !owns(owner)
      || !capabilities.canManagePolicies
      || !toolDialog.open
      || toolDialog.kind !== "policy"
      || toolDialog.state !== "ready"
      || !toolDialog.tool
      || operations.policyUpdate
    ) return null;
    generations.policyUpdate += 1;
    const ticket = Object.freeze({
      ownerKey,
      lane: "policyUpdate",
      generation: generations.policyUpdate,
      controller: new AbortController(),
      toolId: toolDialog.toolId,
    });
    operations.policyUpdate = ticket;
    toolDialog = Object.freeze({ ...toolDialog, state: "submitting", error: null });
    return ticket;
  }

  function acceptPolicyUpdate(ticket, payload, owner) {
    if (!currentTicket(ticket, owner, "policyUpdate") || toolDialog.toolId !== ticket.toolId) return false;
    const updated = normalizeTool(payload?.tool ?? payload);
    if (!updated || updated.mcp_tool_id !== ticket.toolId) {
      return rejectPolicyUpdate(ticket, fixedError(null, "mcp.response_invalid"), owner);
    }
    const items = tabData.tools.items.map((item) => (
      item.mcp_tool_id === updated.mcp_tool_id ? Object.freeze({ ...item, ...updated }) : item
    ));
    tabData = replaceTab(tabData, "tools", { ...tabData.tools, items: Object.freeze(items) });
    toolDialog = closedToolDialog();
    feedback = Object.freeze({
      kind: "success",
      code: "mcp.policy_updated",
      requestId: safeChain(payload?.request_id),
      objectId: updated.mcp_tool_id,
    });
    return true;
  }

  function rejectPolicyUpdate(ticket, error, owner) {
    if (!currentTicket(ticket, owner, "policyUpdate") || toolDialog.toolId !== ticket.toolId) return false;
    toolDialog = Object.freeze({
      ...toolDialog,
      state: "error",
      error: fixedError(error, "mcp.unavailable"),
    });
    return true;
  }

  function finishPolicyUpdate(ticket) {
    if (operations.policyUpdate === ticket) operations.policyUpdate = null;
  }

  function openNetworkDialog(owner, serverId) {
    const server = tabData.network.items.find((item) => item.mcp_server_id === serverId);
    if (
      !owns(owner)
      || !capabilities.canManageNetwork
      || !server?.network_policy
      || isBusy()
    ) return false;
    closeNetworkDialogInternal();
    networkDialog = Object.freeze({
      open: true,
      serverId,
      state: "ready",
      error: null,
      policy: server.network_policy,
      draft: networkPolicyDraft(server.network_policy),
    });
    feedback = null;
    return true;
  }

  function closeNetworkDialog(owner) {
    if (!owns(owner) || operations.networkUpdate) return false;
    closeNetworkDialogInternal();
    return true;
  }

  function beginNetworkPolicyUpdate(owner, policy) {
    if (
      !owns(owner)
      || !capabilities.canManageNetwork
      || !networkDialog.open
      || !networkDialog.policy
      || operations.networkUpdate
    ) return null;
    let normalized;
    try {
      normalized = normalizeMcpNetworkPolicy(policy);
    } catch {
      return null;
    }
    if (normalized.expected_revision !== networkDialog.policy.revision) return null;
    generations.networkUpdate += 1;
    const ticket = Object.freeze({
      ownerKey,
      lane: "networkUpdate",
      generation: generations.networkUpdate,
      controller: new AbortController(),
      serverId: networkDialog.serverId,
      expectedRevision: normalized.expected_revision,
      policy: normalized,
    });
    operations.networkUpdate = ticket;
    networkDialog = Object.freeze({
      ...networkDialog,
      state: "submitting",
      error: null,
      draft: normalized,
    });
    return ticket;
  }

  function acceptNetworkPolicyUpdate(ticket, payload, owner) {
    if (!currentNetworkTicket(ticket, owner)) return false;
    const updated = normalizeNetworkPolicySummary(
      payload?.network_policy ?? payload,
      ticket.serverId,
    );
    if (
      !updated
      || updated.revision !== ticket.expectedRevision + 1
      || !networkPolicyMatchesRequest(updated, ticket.policy)
      || updated.circuit_state !== "not_initialized"
      || updated.blocked_count !== 0
      || updated.last_block_reason !== null
      || updated.last_blocked_at !== null
    ) return rejectNetworkPolicyUpdate(ticket, fixedError(null, "mcp.response_invalid"), owner);
    for (const tab of ["servers", "network"]) {
      tabData = replaceTab(tabData, tab, {
        ...tabData[tab],
        items: Object.freeze(tabData[tab].items.map((item) => (
          item.mcp_server_id === ticket.serverId
            ? Object.freeze({ ...item, network_policy: updated })
            : item
        ))),
        requestId: safeChain(payload?.request_id),
      });
    }
    networkDialog = closedNetworkDialog();
    feedback = Object.freeze({
      kind: "success",
      code: "mcp.network_policy_updated",
      requestId: safeChain(payload?.request_id),
      objectId: updated.mcp_network_policy_id,
    });
    return true;
  }

  function rejectNetworkPolicyUpdate(ticket, error, owner) {
    if (!currentNetworkTicket(ticket, owner)) return false;
    const fixed = fixedError(error, "mcp.unavailable");
    networkDialog = Object.freeze({
      ...networkDialog,
      state: fixed.code === "mcp.state_conflict" ? "conflict" : "error",
      error: fixed,
      draft: ticket.policy,
    });
    return true;
  }

  function finishNetworkPolicyUpdate(ticket) {
    if (operations.networkUpdate === ticket) operations.networkUpdate = null;
  }

  function refreshNetworkConflict(items) {
    if (!networkDialog.open || networkDialog.state !== "conflict") return;
    const current = items.find((item) => item.mcp_server_id === networkDialog.serverId);
    if (!current?.network_policy) {
      networkDialog = closedNetworkDialog();
      feedback = Object.freeze({
        kind: "error",
        code: "mcp.server_not_found",
        requestId: "-",
        objectId: null,
      });
      return;
    }
    networkDialog = Object.freeze({
      ...networkDialog,
      policy: current.network_policy,
      draft: Object.freeze({
        ...networkDialog.draft,
        expected_revision: current.network_policy.revision,
      }),
    });
  }

  function blocksShellDomReplacement() {
    return isDialogOpen() || Boolean(
      operations.register
      || operations.serverAction
      || operations.policyUpdate
      || operations.networkUpdate
    );
  }

  function snapshot() {
    return Object.freeze({
      activeTab,
      tabs: Object.freeze(Object.fromEntries(TABS.map((tab) => [tab, Object.freeze({
        state: tabData[tab].state,
        error: tabData[tab].error,
        items: tabData[tab].items,
        requestId: tabData[tab].requestId ?? "-",
        canPrevious: cursorStacks[tab].length > 0,
        canNext: Boolean(nextCursors[tab]),
      })]))),
      registerDialog,
      toolDialog,
      networkDialog,
      feedback,
      canManageServers: capabilities.canManageServers,
      canManagePolicies: capabilities.canManagePolicies,
      canRefreshTools: capabilities.canRefreshTools,
      canManageNetwork: capabilities.canManageNetwork,
      canInvoke: capabilities.canInvoke,
      // Background list GETs do not disable already-rendered commands once their snapshot is accepted.
      busy: Boolean(
        operations.register
        || operations.serverAction
        || operations.detail
        || operations.policy
        || operations.policyUpdate
        || operations.networkUpdate
      ),
    });
  }

  function owns(owner) {
    const key = ownerKeyFor(owner);
    return Boolean(key && key === ownerKey);
  }

  function currentTicket(ticket, owner, lane, refreshGeneration) {
    return Boolean(
      ticket
      && ticket === operations[lane]
      && ticket.ownerKey === ownerKey
      && ticket.ownerKey === ownerKeyFor(owner)
      && ticket.generation === generations[lane]
      && !ticket.controller.signal.aborted
      && (refreshGeneration === undefined || ticket.refreshGeneration === refreshGeneration)
    );
  }

  function currentToolTicket(ticket, owner) {
    return currentTicket(ticket, owner, ticket?.lane)
      && toolDialog.open
      && toolDialog.kind === ticket.lane
      && toolDialog.toolId === ticket.toolId;
  }

  function currentNetworkTicket(ticket, owner) {
    return currentTicket(ticket, owner, "networkUpdate")
      && networkDialog.open
      && networkDialog.serverId === ticket.serverId;
  }

  function isDialogOpen() {
    return registerDialog.open || toolDialog.open || networkDialog.open;
  }

  function isBusy() {
    return Boolean(Object.values(operations).some(Boolean));
  }

  function closeToolDialogInternal() {
    for (const lane of ["detail", "policy"]) {
      generations[lane] += 1;
      operations[lane]?.controller?.abort();
      operations[lane] = null;
    }
    toolDialog = closedToolDialog();
  }

  function closeNetworkDialogInternal() {
    generations.networkUpdate += 1;
    operations.networkUpdate?.controller?.abort();
    operations.networkUpdate = null;
    networkDialog = closedNetworkDialog();
  }

  function clearDomSafely() {
    try {
      clearDom();
    } catch {
      // DOM cleanup is best effort; owner invalidation and Abort still happen below.
    }
  }

  return Object.freeze({
    enter,
    clear,
    switchTab,
    beginList,
    isListCurrent,
    acceptList,
    rejectList,
    finishList,
    listQuery,
    nextPage,
    previousPage,
    openRegister,
    closeRegister,
    beginRegister,
    acceptRegister,
    rejectRegister,
    finishRegister,
    beginServerRefresh,
    acceptServerRefresh,
    rejectServerRefresh,
    finishServerRefresh,
    beginServerStatus,
    acceptServerStatus,
    rejectServerStatus,
    finishServerStatus,
    openToolDialog,
    beginToolDetail,
    acceptToolDetail,
    rejectToolDetail,
    finishToolDetail,
    closeToolDialog,
    beginPolicyUpdate,
    acceptPolicyUpdate,
    rejectPolicyUpdate,
    finishPolicyUpdate,
    openNetworkDialog,
    closeNetworkDialog,
    beginNetworkPolicyUpdate,
    acceptNetworkPolicyUpdate,
    rejectNetworkPolicyUpdate,
    finishNetworkPolicyUpdate,
    blocksShellDomReplacement,
    snapshot,
  });
}

export function isMcpToolAbortError(error) {
  return error?.name === "AbortError";
}

function normalizeListPayload(tab, payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const serverTab = tab === "servers" || tab === "network";
  const key = serverTab ? "servers" : tab === "tools" ? "tools" : "invocations";
  const source = payload[key] ?? payload.items;
  if (!Array.isArray(source) || source.length > 100) return null;
  const normalizer = serverTab ? normalizeServer : tab === "tools" ? normalizeTool : normalizeInvocation;
  const items = source.map(normalizer);
  if (items.some((item) => !item)) return null;
  const nextCursor = payload.next_cursor == null ? null : String(payload.next_cursor);
  if (nextCursor && (nextCursor.length > 512 || !/^[A-Za-z0-9_.:+\/=~-]+$/.test(nextCursor))) return null;
  return Object.freeze({
    items: Object.freeze(items),
    nextCursor,
    requestId: safeChain(payload.request_id),
  });
}

function normalizeServer(value) {
  if (
    !value
    || !SERVER_ID.test(String(value.mcp_server_id ?? ""))
    || value.transport !== "streamable_http"
    || !["pending_sync", "active", "sync_failed", "disabled"].includes(value.status)
    || safeText(value.name, 80, "") === ""
    || safeText(value.endpoint_hint ?? value.masked_endpoint_hint, 240, "") === ""
  ) return null;
  const stateRevision = positiveInt(value.state_revision);
  const networkPolicy = normalizeNetworkPolicySummary(value.network_policy, value.mcp_server_id);
  if (!stateRevision || !networkPolicy) return null;
  return Object.freeze({
    mcp_server_id: value.mcp_server_id,
    name: safeText(value.name, 80),
    transport: safeEnum(value.transport, ["streamable_http"]),
    endpoint_hint: safeText(value.endpoint_hint ?? value.masked_endpoint_hint, 240),
    endpoint_has_path: value.endpoint_has_path === true,
    status: safeEnum(value.status, ["pending_sync", "active", "sync_failed", "disabled"]),
    protocol_version: safeText(value.protocol_version, 80, "-"),
    state_revision: stateRevision,
    tool_count: nonNegativeInt(value.tool_count ?? value.tools_count),
    active_tool_count: nonNegativeInt(value.active_tool_count),
    review_required_tool_count: nonNegativeInt(value.review_required_tool_count),
    network_policy: networkPolicy,
    last_synced_at: safeTime(value.last_synced_at ?? value.tools_refreshed_at),
    created_at: safeTime(value.created_at),
    updated_at: safeTime(value.updated_at),
  });
}

function normalizeNetworkPolicySummary(value, expectedServerId) {
  if (
    !value
    || typeof value !== "object"
    || Array.isArray(value)
    || !NETWORK_POLICY_ID.test(String(value.mcp_network_policy_id ?? ""))
    || !SERVER_ID.test(String(value.mcp_server_id ?? ""))
    || value.mcp_server_id !== expectedServerId
    || !["active", "disabled"].includes(value.status)
    || value.egress_mode !== "registered_endpoint_only"
    || !["not_initialized", "closed", "open", "half_open"].includes(value.circuit_state)
  ) return null;
  const revision = boundedInteger(value.revision, 1, Number.MAX_SAFE_INTEGER);
  const destinationCount = boundedInteger(value.destination_count, 0, 1);
  const connectTimeout = boundedInteger(value.connect_timeout_seconds, 1, 10);
  const probeTimeout = boundedInteger(value.probe_timeout_seconds, 1, 30);
  const callTimeout = boundedInteger(value.call_timeout_seconds, 1, 120);
  const failureThreshold = boundedInteger(value.failure_threshold, 1, 10);
  const openSeconds = boundedInteger(value.open_seconds, 10, 3600);
  const blockedCount = boundedInteger(value.blocked_count, 0, Number.MAX_SAFE_INTEGER);
  const updatedAt = safeUtcTime(value.updated_at);
  const reason = value.last_block_reason == null
    ? null
    : safeEnum(value.last_block_reason, [
      "policy_disabled",
      "address_denied",
      "mixed_dns_answers",
      "port_denied",
      "redirect_denied",
    ], true);
  const blockedAt = value.last_blocked_at == null ? null : safeUtcTime(value.last_blocked_at);
  if (
    revision == null
    || destinationCount !== (value.status === "active" ? 1 : 0)
    || connectTimeout == null
    || probeTimeout == null
    || callTimeout == null
    || failureThreshold == null
    || openSeconds == null
    || blockedCount == null
    || !updatedAt
    || (value.last_block_reason != null && !reason)
    || Boolean(reason) !== Boolean(blockedAt)
  ) return null;
  return Object.freeze({
    mcp_network_policy_id: value.mcp_network_policy_id,
    mcp_server_id: value.mcp_server_id,
    status: value.status,
    revision,
    egress_mode: "registered_endpoint_only",
    destination_count: destinationCount,
    connect_timeout_seconds: connectTimeout,
    probe_timeout_seconds: probeTimeout,
    call_timeout_seconds: callTimeout,
    failure_threshold: failureThreshold,
    open_seconds: openSeconds,
    circuit_state: value.circuit_state,
    blocked_count: blockedCount,
    last_block_reason: reason,
    last_blocked_at: blockedAt,
    updated_at: updatedAt,
  });
}

function networkPolicyDraft(policy) {
  return Object.freeze({
    expected_revision: policy.revision,
    status: policy.status,
    egress_mode: policy.egress_mode,
    connect_timeout_seconds: policy.connect_timeout_seconds,
    probe_timeout_seconds: policy.probe_timeout_seconds,
    call_timeout_seconds: policy.call_timeout_seconds,
    failure_threshold: policy.failure_threshold,
    open_seconds: policy.open_seconds,
  });
}

function networkPolicyMatchesRequest(policy, request) {
  return policy.status === request.status
    && policy.egress_mode === request.egress_mode
    && policy.connect_timeout_seconds === request.connect_timeout_seconds
    && policy.probe_timeout_seconds === request.probe_timeout_seconds
    && policy.call_timeout_seconds === request.call_timeout_seconds
    && policy.failure_threshold === request.failure_threshold
    && policy.open_seconds === request.open_seconds;
}

function normalizeTool(value) {
  const inputHash = value?.input_schema_original_sha256 ?? value?.input_schema_sha256;
  const outputHash = value?.output_schema_original_sha256 ?? value?.output_schema_sha256;
  if (
    !value
    || !TOOL_ID.test(String(value.mcp_tool_id ?? ""))
    || !SERVER_ID.test(String(value.mcp_server_id ?? ""))
    || !HASH.test(String(inputHash ?? ""))
    || (outputHash != null && !HASH.test(String(outputHash)))
    || !["standard", "high"].includes(value.risk_level)
    || !["review_required", "active", "disabled"].includes(value.status)
    || typeof value.approval_required !== "boolean"
  ) return null;
  const schemaRevision = positiveInt(value.schema_revision);
  const policyRevision = positiveInt(value.policy_revision);
  if (!schemaRevision || !policyRevision) return null;
  const permissions = normalizePermissionCodes(value.required_permission_codes ?? [], { allowEmpty: true });
  if (!permissions) return null;
  return Object.freeze({
    mcp_tool_id: value.mcp_tool_id,
    mcp_server_id: value.mcp_server_id,
    remote_name: safeText(value.remote_name ?? value.name, 128),
    title: safeText(value.title, 160, "-"),
    input_schema_original_sha256: safeHash(value.input_schema_original_sha256 ?? value.input_schema_sha256),
    input_schema_effective_sha256: safeHash(value.input_schema_effective_sha256 ?? value.input_schema_sha256),
    output_schema_original_sha256: safeHash(value.output_schema_original_sha256 ?? value.output_schema_sha256, true),
    output_schema_effective_sha256: safeHash(value.output_schema_effective_sha256 ?? value.output_schema_sha256, true),
    schema_revision: schemaRevision,
    policy_revision: policyRevision,
    risk_level: safeEnum(value.risk_level, ["standard", "high"]),
    approval_required: value.approval_required === true,
    approval_expires_seconds: positiveInt(value.approval_expires_seconds) || 3600,
    required_permission_codes: Object.freeze(permissions),
    allowed_role_count: nonNegativeInt(value.allowed_role_count ?? value.role_grant_count),
    allowed_user_count: nonNegativeInt(value.allowed_user_count ?? value.user_grant_count),
    status: safeEnum(value.status, ["review_required", "active", "disabled"]),
    updated_at: safeTime(value.updated_at),
  });
}

function normalizeToolDetail(value) {
  const summary = normalizeTool(value);
  if (!summary) return null;
  const roleIds = normalizeSafeIds(value.allowed_role_ids ?? []);
  const userIds = normalizeSafeIds(value.allowed_user_ids ?? []);
  const pointers = normalizePointers(value.result_redaction_pointers ?? []);
  if (!roleIds || !userIds || !pointers) return null;
  const inputOriginal = canonicalSchema(value.input_schema_original ?? value.input_schema_original_json, false);
  const inputEffective = canonicalSchema(value.input_schema_effective ?? value.input_schema_effective_json, false);
  const outputOriginal = canonicalSchema(value.output_schema_original ?? value.output_schema_original_json, true);
  const outputEffective = canonicalSchema(value.output_schema_effective ?? value.output_schema_effective_json, true);
  if (inputOriginal == null || inputEffective == null || outputOriginal === undefined || outputEffective === undefined) {
    return null;
  }
  return Object.freeze({
    ...summary,
    description: safeText(value.description, 500, ""),
    input_schema_original: inputOriginal,
    input_schema_effective: inputEffective,
    output_schema_original: outputOriginal,
    output_schema_effective: outputEffective,
    allowed_role_ids: Object.freeze(roleIds),
    allowed_user_ids: Object.freeze(userIds),
    result_redaction_pointers: Object.freeze(pointers),
  });
}

function normalizeInvocation(value) {
  const inputDlpStatus = value?.input_dlp?.status;
  const outputDlpStatus = value?.output_dlp?.status;
  if (
    !value
    || !INVOCATION_ID.test(String(value.mcp_invocation_id ?? ""))
    || !SERVER_ID.test(String(value.mcp_server_id ?? ""))
    || !TOOL_ID.test(String(value.mcp_tool_id ?? ""))
    || ![
      "approval_pending", "queued", "probing", "calling", "succeeded", "failed",
      "blocked", "rejected", "outcome_unknown",
    ].includes(value.status)
    || !["standard", "high"].includes(value.risk_level)
    || typeof value.approval_required !== "boolean"
    || ![null, "clean", "log_only", "blocked", "unavailable"].includes(inputDlpStatus ?? null)
    || ![null, "clean", "log_only", "blocked", "unavailable"].includes(outputDlpStatus ?? null)
    || !["not_called", "called", "unknown"].includes(value.execution_status)
    || !["not_applicable", "not_reported", "unknown"].includes(value.cost_status)
  ) return null;
  return Object.freeze({
    mcp_invocation_id: value.mcp_invocation_id,
    mcp_server_id: value.mcp_server_id,
    mcp_tool_id: value.mcp_tool_id,
    status: safeEnum(value.status, [
      "approval_pending", "queued", "probing", "calling", "succeeded", "failed",
      "blocked", "rejected", "outcome_unknown",
    ]),
    risk_level: safeEnum(value.risk_level, ["standard", "high"]),
    approval_required: value.approval_required === true,
    approval_task_id: safeOptionalId(value.approval_task_id),
    input_dlp_status: safeEnum(value.input_dlp?.status, ["clean", "log_only", "blocked", "unavailable"], true),
    input_dlp_match_count: nullableNonNegativeInt(value.input_dlp?.match_count),
    output_dlp_status: safeEnum(value.output_dlp?.status, ["clean", "log_only", "blocked", "unavailable"], true),
    output_dlp_match_count: nullableNonNegativeInt(value.output_dlp?.match_count),
    redaction_count: nonNegativeInt(value.result_redaction_count ?? value.redaction_count),
    probe_attempt_count: nonNegativeInt(value.probe_attempt_count),
    call_attempt_count: nonNegativeInt(value.call_attempt_count),
    duration_ms: nullableNonNegativeInt(value.duration_ms),
    request_bytes: nullableNonNegativeInt(value.request_bytes),
    response_bytes: nullableNonNegativeInt(value.response_bytes),
    execution_status: safeEnum(value.execution_status, ["not_called", "called", "unknown"]),
    invocation_count: positiveInt(value.invocation_count) || 1,
    tool_call_count: nullableNonNegativeInt(value.tool_call_count),
    cost_status: safeEnum(value.cost_status, ["not_applicable", "not_reported", "unknown"]),
    error_code: safeErrorCode(value.error_code),
    created_at: safeTime(value.created_at),
    completed_at: safeTime(value.completed_at),
  });
}

function canonicalSchema(value, optional) {
  if (value == null) return optional ? null : null;
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const text = JSON.stringify(parsed, null, 2);
    return new TextEncoder().encode(text).length <= 131072 ? text : null;
  } catch {
    return null;
  }
}

function ownerKeyFor(owner) {
  if (
    !owner?.readIdentity
    || !owner?.tenantId
    || !owner?.navigationOwner
    || owner?.page !== PAGE_ID
  ) return "";
  return JSON.stringify([
    owner.readIdentity,
    owner.tenantId,
    owner.navigationOwner,
    owner.canManageServers === true,
    owner.canManagePolicies === true,
    owner.canManageNetwork === true,
    owner.canInvoke === true,
  ]);
}

function mirroredTenantId(authContext) {
  const user = String(authContext?.user?.tenant_id ?? "").trim();
  const tenant = String(authContext?.tenant?.tenant_id ?? "").trim();
  if (user && tenant && user !== tenant) return "";
  return user || tenant;
}

function normalizePermissionCodes(value, { allowEmpty = false } = {}) {
  if (!Array.isArray(value) || (!allowEmpty && !value.length) || value.length > 256) return null;
  const normalized = value.map((item) => String(item ?? "").trim()).sort();
  if (
    normalized.some((item) => !/^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(item))
    || new Set(normalized).size !== normalized.length
  ) return null;
  return normalized;
}

function normalizeSafeIds(value) {
  if (!Array.isArray(value) || value.length > 100) return null;
  const normalized = value.map((item) => String(item ?? "").trim()).sort();
  return normalized.every((item) => SAFE_ID.test(item)) && new Set(normalized).size === normalized.length
    ? normalized
    : null;
}

function normalizePointers(value) {
  if (!Array.isArray(value) || value.length > 32) return null;
  const normalized = value.map((item) => String(item ?? "")).sort();
  return normalized.every((item) => item.length <= 512 && /^(?:\/(?:[^~]|~[01])*)*$/u.test(item))
    && new Set(normalized).size === normalized.length
    ? normalized
    : null;
}

function emptyTabData() {
  return Object.fromEntries(TABS.map((tab) => [tab, Object.freeze({
    state: tab === "servers" ? "loading" : "idle",
    items: Object.freeze([]),
    error: null,
    requestId: "-",
  })]));
}

function replaceTab(source, tab, value) {
  return { ...source, [tab]: Object.freeze(value) };
}

function emptyGenerations() {
  return {
    servers: 0,
    tools: 0,
    invocations: 0,
    network: 0,
    register: 0,
    serverAction: 0,
    detail: 0,
    policy: 0,
    policyUpdate: 0,
    networkUpdate: 0,
  };
}

function incrementAll(value) {
  return Object.fromEntries(Object.entries(value).map(([key, generation]) => [key, generation + 1]));
}

function emptyOperations() {
  return {
    servers: null,
    tools: null,
    invocations: null,
    network: null,
    register: null,
    serverAction: null,
    detail: null,
    policy: null,
    policyUpdate: null,
    networkUpdate: null,
  };
}

function emptyCapabilities() {
  return Object.freeze({
    canManageServers: false,
    canManagePolicies: false,
    canRefreshTools: false,
    canManageNetwork: false,
    canInvoke: false,
  });
}

function closedRegisterDialog() {
  return Object.freeze({ open: false, state: "idle", error: null });
}

function closedToolDialog() {
  return Object.freeze({ open: false, kind: "", toolId: "", state: "idle", error: null, tool: null });
}

function closedNetworkDialog() {
  return Object.freeze({
    open: false,
    serverId: "",
    state: "idle",
    error: null,
    policy: null,
    draft: null,
  });
}

function safeText(value, maximum, fallback = "-") {
  const text = String(value ?? "").trim();
  return text && text.length <= maximum && !/[\u0000-\u001f\u007f]/u.test(text) ? text : fallback;
}

function safeEnum(value, allowed, nullable = false) {
  if (nullable && value == null) return null;
  return allowed.includes(String(value ?? "")) ? String(value) : nullable ? null : "unknown";
}

function safeHash(value, optional = false) {
  if (optional && value == null) return null;
  return HASH.test(String(value ?? "")) ? String(value) : optional ? null : "";
}

function positiveInt(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 1 ? number : 0;
}

function boundedInteger(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum ? value : null;
}

function nonNegativeInt(value) {
  const number = Number(value ?? 0);
  return Number.isSafeInteger(number) && number >= 0 ? number : 0;
}

function nullableNonNegativeInt(value) {
  return value == null ? null : nonNegativeInt(value);
}

function safeTime(value) {
  const text = String(value ?? "");
  return text && !Number.isNaN(Date.parse(text)) ? text : null;
}

function safeUtcTime(value) {
  const text = String(value ?? "");
  return /(?:Z|\+00:00)$/u.test(text) && !Number.isNaN(Date.parse(text)) ? text : null;
}

function safeOptionalId(value) {
  return value == null ? null : SAFE_ID.test(String(value)) ? String(value) : null;
}

function safeErrorCode(value) {
  return value == null ? null : /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(String(value))
    ? String(value)
    : null;
}

function safeChain(value) {
  return SAFE_ID.test(String(value ?? "")) ? String(value) : "-";
}

function fixedError(error, fallback) {
  return Object.freeze({
    code: safeErrorCode(error?.code) || fallback,
    requestId: safeChain(error?.requestId ?? error?.request_id),
  });
}
