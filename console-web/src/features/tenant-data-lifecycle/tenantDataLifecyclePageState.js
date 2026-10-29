import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

export function tenantDataLifecycleOwner(authContext, consoleShellState, mode = "read") {
  const active = consoleShellState?.activeNavigationItemId === "tenant-data-lifecycle";
  const permission = mode === "export" ? PERMISSIONS.TENANT_DATA_EXPORT : mode === "download" ? PERMISSIONS.TENANT_DATA_DOWNLOAD : mode === "delete" ? PERMISSIONS.TENANT_DATA_DELETE : PERMISSIONS.TENANT_DATA_READ;
  const readAllowed = mode === "read"
    ? hasPermission(authContext, permission) || hasPermission(authContext, PERMISSIONS.TENANT_SETTINGS_READ)
    : hasPermission(authContext, permission);
  if (!active || !authContext?.user || !readAllowed) return null;
  return Object.freeze({
    tenantId: String(authContext.user.tenant_id ?? ""),
    userId: String(authContext.user.user_id ?? ""),
    mode,
    permission,
    permissionRevision: Number(authContext.permission_revision ?? 0),
    authorizationFingerprint: String(authContext.authorization_fingerprint ?? ""),
  });
}

export function createTenantDataLifecyclePageState() {
  let ownerKey = "";
  let state = null;
  let pageGeneration = 0;
  const lanes = new Map(["read", "export", "download", "delete"].map((name) => [name, { generation: 0, controller: null, ticket: null }]));
  const identityKey = (owner) => owner
    ? `${owner.tenantId}:${owner.userId}:${owner.permissionRevision}:${owner.authorizationFingerprint}`
    : "";
  const abortLane = (name) => {
    const lane = lanes.get(name);
    lane?.controller?.abort();
    if (lane) lane.ticket = null;
  };
  const clearAllLanes = () => { for (const name of lanes.keys()) abortLane(name); };
  return {
    enter(owner) {
      const key = identityKey(owner);
      if (key !== ownerKey) {
        clearAllLanes();
        state = null;
        ownerKey = key;
        pageGeneration += 1;
      }
      return Boolean(ownerKey);
    },
    snapshot() { return state; },
    set(next) { state = next; return state; },
    beginLane(name, owner = null) {
      const lane = lanes.get(name);
      if (!lane || !ownerKey || (owner && identityKey(owner) !== ownerKey)) return null;
      abortLane(name);
      lane.generation += 1;
      lane.controller = new AbortController();
      lane.ticket = Object.freeze({ lane: name, generation: lane.generation, pageGeneration, ownerKey, controller: lane.controller, signal: lane.controller.signal });
      return lane.ticket;
    },
    begin() { return this.beginLane("read"); },
    isCurrent(ticket, owner, laneName = ticket?.lane) {
      const lane = lanes.get(laneName);
      return Boolean(ticket && lane && lane.ticket === ticket && ticket.generation === lane.generation && ticket.pageGeneration === pageGeneration && ticket.ownerKey === ownerKey && owner && identityKey(owner) === ownerKey && !ticket.signal.aborted);
    },
    finishLane(ticket) {
      const lane = lanes.get(ticket?.lane);
      if (lane?.ticket === ticket) lane.ticket = null;
    },
    laneBusy(name) { return Boolean(lanes.get(name)?.ticket); },
    clear() { clearAllLanes(); state = null; ownerKey = ""; pageGeneration += 1; },
  };
}
