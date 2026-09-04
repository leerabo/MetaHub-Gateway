import { createWorkspaceNavigationStore } from "./workspaceNavigationStore.js";

export function createWorkspaceNavigationController({ api, createIdempotencyKey }) {
  if (!api || typeof api.workspaceSession !== "function"
    || typeof api.navigation !== "function"
    || typeof api.gettingStarted !== "function"
    || typeof api.advancedTools !== "function"
    || typeof api.updatePreference !== "function"
    || typeof api.openAdvancedTool !== "function") {
    throw new TypeError("workspace navigation API is incomplete");
  }
  const store = createWorkspaceNavigationStore({ createIdempotencyKey });
  let preferenceTail = Promise.resolve();
  let workspaceSessionController = null;
  let workspaceSessionSequence = 0;
  let pendingAdvancedOpen = null;

  async function loadNavigation() {
    const sequence = ++workspaceSessionSequence;
    workspaceSessionController?.abort();
    const sessionController = new AbortController();
    workspaceSessionController = sessionController;
    let session;
    try {
      session = await api.workspaceSession({ signal: sessionController.signal });
    } finally {
      if (workspaceSessionController === sessionController) {
        workspaceSessionController = null;
      }
    }
    if (sessionController.signal.aborted || sequence !== workspaceSessionSequence) {
      throw conflictError();
    }
    const context = session?.context;
    const previousOwner = store.snapshot().owner;
    const nextOwner = {
      workspaceId: context?.workspace_id,
      role: context?.role,
    };
    const ownerChanged = previousOwner?.workspaceId !== nextOwner.workspaceId
      || previousOwner?.role !== nextOwner.role;
    if (!store.enter({
      workspaceId: nextOwner.workspaceId,
      role: nextOwner.role,
    })) {
      throw new TypeError("workspace session owner is invalid");
    }
    if (ownerChanged) {
      preferenceTail = Promise.resolve();
      pendingAdvancedOpen = null;
    }
    const ticket = store.begin("navigation");
    if (!ticket) throw new TypeError("workspace navigation ticket is unavailable");
    try {
      const navigation = await api.navigation({ signal: ticket.controller.signal });
      if (!store.accept(ticket, navigation)) {
        throw conflictError();
      }
      return Object.freeze({ context, ...store.snapshot() });
    } catch (error) {
      store.reject(ticket, error);
      throw error;
    }
  }

  async function loadGettingStarted() {
    return loadLane("task-home", api.gettingStarted, "taskHome");
  }

  async function loadAdvancedTools() {
    return loadLane("advanced-tools", api.advancedTools, "advancedTools");
  }

  function cancelAdvancedTools() {
    store.cancel("advanced-tools");
    store.cancel("advanced-open");
  }

  function savePreference(changes) {
    const requestedGeneration = store.snapshot().generation;
    const operation = async () => {
      if (store.snapshot().generation !== requestedGeneration) throw conflictError();
      const ticket = store.beginPreference(changes);
      if (!ticket) throw new TypeError("workspace preference ticket is unavailable");
      try {
        const response = await api.updatePreference({
          workspaceId: ticket.workspaceId,
          contractSnapshotId: ticket.contractSnapshotId,
          revision: ticket.revision,
          changes: ticket.changes,
          idempotencyKey: ticket.idempotencyKey,
          signal: ticket.controller.signal,
        });
        if (!store.acceptPreference(ticket, response)) throw conflictError();
        return store.snapshot();
      } catch (error) {
        store.rejectPreference(ticket, error);
        throw error;
      }
    };
    const result = preferenceTail.then(operation, operation);
    preferenceTail = result.catch(() => undefined);
    return result;
  }

  async function openAdvancedTool(toolCode) {
    const current = store.snapshot();
    const tool = current.advancedTools?.tools?.find(
      (candidate) => candidate.tool_code === toolCode
        && candidate.availability === "available"
        && candidate.action_enabled,
    );
    if (!tool) throw conflictError();
    const ticket = store.begin("advanced-open");
    if (!ticket) throw conflictError();
    const signature = [
      tool.tool_code,
      tool.entitlement_revision,
      current.navigation.contract_snapshot_id,
      current.navigation.navigation_epoch,
      current.navigation.permission_fingerprint,
    ].join(":");
    const idempotencyKey = pendingAdvancedOpen?.signature === signature
      ? pendingAdvancedOpen.idempotencyKey
      : createIdempotencyKey("advanced-open");
    pendingAdvancedOpen = Object.freeze({ signature, idempotencyKey });
    try {
      const response = await api.openAdvancedTool({
        toolCode,
        entitlementRevision: tool.entitlement_revision,
        idempotencyKey,
        signal: ticket.controller.signal,
      });
      if (!store.accept(ticket, response)) throw conflictError();
      pendingAdvancedOpen = null;
      return response;
    } catch (error) {
      store.reject(ticket, error);
      throw error;
    }
  }

  async function loadLane(lane, loader, property) {
    if (!store.snapshot().navigation) throw conflictError();
    const ticket = store.begin(lane);
    if (!ticket) throw conflictError();
    try {
      const response = await loader({ signal: ticket.controller.signal });
      if (!store.accept(ticket, response)) throw conflictError();
      return store.snapshot()[property];
    } catch (error) {
      store.reject(ticket, error);
      throw error;
    }
  }

  function leave() {
    workspaceSessionSequence += 1;
    workspaceSessionController?.abort();
    workspaceSessionController = null;
    store.leave();
    preferenceTail = Promise.resolve();
    pendingAdvancedOpen = null;
  }

  function changeRoute() {
    workspaceSessionSequence += 1;
    workspaceSessionController?.abort();
    workspaceSessionController = null;
    store.changeRoute();
    preferenceTail = Promise.resolve();
    pendingAdvancedOpen = null;
  }

  return Object.freeze({
    loadNavigation,
    loadGettingStarted,
    loadAdvancedTools,
    cancelAdvancedTools,
    savePreference,
    openAdvancedTool,
    changeRoute,
    leave,
    snapshot: store.snapshot,
  });
}

function conflictError() {
  const error = new Error("空间导航状态已变化，请刷新后继续。");
  error.code = "workspace_navigation.response_conflict";
  error.status = 409;
  return error;
}
