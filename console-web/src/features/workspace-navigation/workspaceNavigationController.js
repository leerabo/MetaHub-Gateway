import { createWorkspaceNavigationStore, workspaceNavigationOwner } from "./workspaceNavigationStore.js";

export function createWorkspaceNavigationController({ api, createIdempotencyKey }) {
  if (!api || typeof api.identitySession !== "function" || typeof api.workspaceSession !== "function"
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
  let preferenceInFlight = 0;
  let preferenceNeedsReload = false;

  async function loadNavigation({ recoverPreference = false } = {}) {
    const sequence = ++workspaceSessionSequence;
    workspaceSessionController?.abort();
    store.cancel("navigation");
    const sessionController = new AbortController();
    workspaceSessionController = sessionController;
    const current = () => !sessionController.signal.aborted && sequence === workspaceSessionSequence;
    let ticket = null;
    try {
      // Global identity and current Workspace session use the same Cookie scope
      // and do not depend on each other's response. Read them together so the
      // shell startup latency is the slower request, rather than their sum.
      const [identityResult, sessionResult] = await Promise.allSettled([
        api.identitySession({ signal: sessionController.signal }),
        api.workspaceSession({ signal: sessionController.signal }),
      ]);
      if (!current()) throw conflictError();
      if (identityResult.status === "rejected") throw identityResult.reason;
      const identity = identityResult.value;
      let session;
      if (sessionResult.status === "rejected") {
        const error = sessionResult.reason;
        // A freshly authenticated account may not have a Workspace cookie yet.
        // Resolve the only obvious choice here so a normal login does not land on
        // a dead-end retry screen.
        if (!new Set([
          "workspace_session.required",
          "workspace_session.invalid",
          "workspace_session.expired",
          "workspace_session.revoked",
        ]).has(error?.code)
          || typeof api.workspaces !== "function"
          || typeof api.switchWorkspace !== "function") throw error;
        const directory = await api.workspaces({ signal: sessionController.signal });
        if (!current()) throw conflictError();
        const choices = Array.isArray(directory?.workspaces) ? directory.workspaces : [];
        if (choices.length !== 1 || choices[0]?.role === "service_account") throw error;
        await api.switchWorkspace({
          workspaceId: choices[0].workspace_id,
          signal: sessionController.signal,
        });
        if (!current()) throw conflictError();
        session = await api.workspaceSession({ signal: sessionController.signal });
      } else {
        session = sessionResult.value;
      }
      if (!current()) throw conflictError();
      const nextOwner = workspaceNavigationOwner(identity, session);
      const previousGeneration = store.snapshot().generation;
      if (!store.enter(nextOwner)) {
        throw new TypeError("workspace session owner is invalid");
      }
      if (store.snapshot().generation !== previousGeneration) {
        preferenceTail = Promise.resolve();
        pendingAdvancedOpen = null;
      }
      ticket = store.begin("navigation");
      if (!ticket) throw new TypeError("workspace navigation ticket is unavailable");
      const navigation = await api.navigation({ signal: ticket.controller.signal });
      if (!current() || !store.accept(ticket, navigation)) throw conflictError();
      return Object.freeze({ identity, context: session.context,
        authorization: session.authorization, ...store.snapshot() });
    } catch (error) {
      // Superseded failures must not look like a current 401/403 to the shell.
      if (!current()) throw conflictError();
      if (recoverPreference && store.snapshot().owner
        && (Number(error?.status) === 503 || error?.code?.endsWith(".network_unavailable"))) {
        // Keep the authorized menu usable, but require another successful read
        // before any preference write after this failed reconciliation.
        preferenceNeedsReload = true;
        store.cancel("navigation");
        throw error;
      }
      if (ticket) {
        if (!store.reject(ticket, error)) throw conflictError();
      } else {
        store.leave();
      }
      throw error;
    } finally {
      if (workspaceSessionController === sessionController) {
        workspaceSessionController = null;
      }
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
      if (!store.snapshot().owner) throw conflictError();
      // Aborting fetch does not undo a PATCH already committed by the server.
      // Reconcile its revision before the next write, within the same route owner.
      if (preferenceNeedsReload || !store.snapshot().navigation) {
        await loadNavigation({ recoverPreference: true });
        if (store.snapshot().generation !== requestedGeneration) throw conflictError();
        preferenceNeedsReload = false;
      }
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const ticket = store.beginPreference(changes);
        if (!ticket) throw conflictError();
        preferenceInFlight += 1;
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
          if (!store.rejectPreference(ticket, error)) throw conflictError();
          const lostResponse = error?.code === "workspace_navigation.network_unavailable";
          if (lostResponse) preferenceNeedsReload = true;
          if (attempt !== 0 || (!lostResponse && (Number(error?.status) !== 409
            || error?.code !== "identity.revision_conflict"))) throw error;
          await loadNavigation({ recoverPreference: true });
          if (store.snapshot().generation !== requestedGeneration) throw conflictError();
          preferenceNeedsReload = false;
        } finally {
          preferenceInFlight -= 1;
        }
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
      if (!store.reject(ticket, error)) throw conflictError();
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
      if (!store.reject(ticket, error)) throw conflictError();
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
    preferenceNeedsReload = false;
  }

  function changeRoute() {
    preferenceNeedsReload ||= preferenceInFlight > 0;
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
