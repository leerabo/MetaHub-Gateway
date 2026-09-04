function ownerKey(owner) {
  const workspaceId = String(owner?.workspaceId ?? "");
  const role = String(owner?.role ?? "");
  if (!/^wsp_[0-9a-f]{32}$/u.test(workspaceId)
    || !/^(?:owner|admin|developer|security_admin|finance_admin|legal_admin|auditor|viewer)$/u.test(role)) {
    return "";
  }
  return workspaceId + ":" + role;
}

export function createWorkspaceNavigationStore({ createIdempotencyKey } = {}) {
  let generation = 0;
  let owner = null;
  let navigation = null;
  let taskHome = null;
  let advancedTools = null;
  let status = "idle";
  let error = null;
  let sequence = 0;
  let pendingPreference = null;
  const active = new Set();

  function enter(nextOwner) {
    const nextKey = ownerKey(nextOwner);
    if (!nextKey) return false;
    if (ownerKey(owner) === nextKey) return true;
    leave();
    owner = Object.freeze({
      workspaceId: String(nextOwner.workspaceId),
      role: String(nextOwner.role),
    });
    generation += 1;
    status = "loading";
    return true;
  }

  function leave() {
    for (const ticket of active) ticket.controller.abort();
    active.clear();
    owner = null;
    navigation = null;
    taskHome = null;
    advancedTools = null;
    pendingPreference = null;
    status = "idle";
    error = null;
    generation += 1;
  }

  function changeRoute() {
    for (const ticket of active) ticket.controller.abort();
    active.clear();
    taskHome = null;
    advancedTools = null;
    pendingPreference = null;
    status = navigation ? "ready" : "idle";
    error = null;
    generation += 1;
  }

  function begin(lane) {
    if (!owner || !new Set([
      "navigation", "task-home", "advanced-tools", "advanced-open", "preference",
    ]).has(lane)) {
      return null;
    }
    for (const ticket of active) {
      if (ticket.lane !== lane) continue;
      ticket.controller.abort();
      active.delete(ticket);
    }
    const ticket = Object.freeze({
      id: ++sequence,
      lane,
      generation,
      ownerKey: ownerKey(owner),
      controller: new AbortController(),
    });
    active.add(ticket);
    status = "loading";
    error = null;
    return ticket;
  }

  function cancel(lane) {
    let cancelled = false;
    for (const ticket of [...active]) {
      if (ticket.lane !== lane) continue;
      ticket.controller.abort();
      active.delete(ticket);
      cancelled = true;
    }
    if (cancelled) {
      status = active.size > 0 ? "loading" : navigation ? "ready" : owner ? "loading" : "idle";
      error = null;
    }
    return cancelled;
  }

  function current(ticket) {
    return Boolean(ticket && active.has(ticket) && !ticket.controller.signal.aborted
      && ticket.generation === generation && ticket.ownerKey === ownerKey(owner));
  }

  function accept(ticket, payload) {
    if (!current(ticket)) return false;
    active.delete(ticket);
    if (ticket.lane === "navigation") {
      if (navigation && regressed(payload, navigation)) return conflict();
      navigation = payload;
      taskHome = compatible(taskHome, payload) ? taskHome : null;
      advancedTools = compatible(advancedTools, payload) ? advancedTools : null;
    } else if (!navigation || !compatible(payload, navigation)) {
      return conflict();
    } else if (ticket.lane === "task-home") {
      taskHome = payload;
    } else if (ticket.lane === "advanced-tools") {
      advancedTools = payload;
    } else if (ticket.lane === "advanced-open") {
      const listed = advancedTools?.tools?.find((tool) => tool.tool_code === payload.tool_code);
      if (!listed || listed.route_code !== payload.route_code
        || listed.entitlement_revision !== payload.entitlement_revision) return conflict();
    }
    status = "ready";
    error = null;
    return true;
  }

  function reject(ticket, nextError) {
    if (!current(ticket)) return false;
    active.delete(ticket);
    error = nextError ?? { code: "workspace_navigation.request_failed" };
    status = Number(nextError?.status) === 403
      ? "permission-revoked"
      : Number(nextError?.status) === 409 || Number(nextError?.status) === 412
        ? "conflict"
        : Number(nextError?.status) >= 500
          ? "dependency-unavailable"
          : "error";
    return true;
  }

  function beginPreference(changes) {
    if (!owner || !navigation || !changes || typeof changes !== "object"
      || Array.isArray(changes) || Object.keys(changes).length === 0) return null;
    const signature = JSON.stringify(Object.entries(changes).sort(([left], [right]) => (
      left.localeCompare(right)
    )));
    const idempotencyKey = pendingPreference?.signature === signature
      ? pendingPreference.idempotencyKey
      : createIdempotencyKey?.("preference");
    if (typeof idempotencyKey !== "string") return null;
    const ticket = begin("preference");
    if (!ticket) return null;
    active.delete(ticket);
    const preferenceTicket = Object.freeze({
      ...ticket,
      signature,
      idempotencyKey,
      workspaceId: owner.workspaceId,
      contractSnapshotId: navigation.contract_snapshot_id,
      revision: navigation.preference.revision,
      changes: Object.freeze({ ...changes }),
      controller: new AbortController(),
    });
    active.add(preferenceTicket);
    pendingPreference = Object.freeze({ signature, idempotencyKey });
    return preferenceTicket;
  }

  function acceptPreference(ticket, response) {
    const revisionDelta = response?.revision - ticket?.revision;
    if (!current(ticket) || ticket.lane !== "preference" || !navigation
      || response.contract_snapshot_id !== ticket.contractSnapshotId
      || response.permission_fingerprint !== navigation.permission_fingerprint
      || response.permission_revision !== navigation.permission_revision
      || response.membership_revision !== navigation.membership_revision
      || response.navigation_epoch !== navigation.navigation_epoch
      || !((response.changed === true && revisionDelta === 1)
        || (response.changed === false && revisionDelta === 0))) return conflictTicket(ticket);
    active.delete(ticket);
    navigation = Object.freeze({
      ...navigation,
      contract_generation: response.contract_generation,
      preference: Object.freeze({
        revision: response.revision,
        last_primary_route_code: response.last_primary_route_code,
        expanded_group_code: response.expanded_group_code,
        sidebar_collapsed: response.sidebar_collapsed,
      }),
    });
    pendingPreference = null;
    status = "ready";
    error = null;
    return true;
  }

  function rejectPreference(ticket, nextError) {
    if (!current(ticket) || ticket.lane !== "preference") return false;
    active.delete(ticket);
    error = nextError ?? { code: "workspace_navigation.request_failed" };
    status = Number(nextError?.status) === 409 || Number(nextError?.status) === 412
      ? "conflict"
      : Number(nextError?.status) === 403
        ? "permission-revoked"
        : "error";
    return true;
  }

  function snapshot() {
    return Object.freeze({
      generation,
      owner,
      navigation,
      taskHome,
      advancedTools,
      status,
      error,
      busy: active.size > 0,
    });
  }

  function conflict() {
    status = "conflict";
    error = { code: "workspace_navigation.response_conflict" };
    return false;
  }

  function conflictTicket(ticket) {
    if (ticket && active.has(ticket)) active.delete(ticket);
    return conflict();
  }

  return Object.freeze({
    enter,
    leave,
    changeRoute,
    begin,
    cancel,
    accept,
    reject,
    beginPreference,
    acceptPreference,
    rejectPreference,
    snapshot,
  });
}

function compatible(candidate, navigation) {
  return !candidate || (
    candidate.contract_snapshot_id === navigation.contract_snapshot_id
    && candidate.contract_generation === navigation.contract_generation
    && candidate.navigation_epoch === navigation.navigation_epoch
    && candidate.membership_revision === navigation.membership_revision
    && candidate.permission_revision === navigation.permission_revision
    && candidate.permission_fingerprint === navigation.permission_fingerprint
    && candidate.owner_ref === navigation.owner_ref
  );
}

function regressed(candidate, previous) {
  return candidate.contract_generation < previous.contract_generation
    || candidate.projection_generation < previous.projection_generation
    || candidate.navigation_epoch < previous.navigation_epoch
    || candidate.membership_revision < previous.membership_revision
    || candidate.permission_revision < previous.permission_revision;
}
